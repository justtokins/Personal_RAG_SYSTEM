"""
ingest.py — Full ingestion pipeline orchestrator.

Why ProcessPoolExecutor was removed:
    Under uvicorn, spawning subprocesses causes signal handler conflicts
    and requires the worker function to be importable from a completely
    clean Python environment. The overhead of spawning processes for
    text splitting — which takes 1-3 seconds even for large PDFs — is
    greater than the speedup on typical document sizes.

    The real bottleneck is embedding (Stage 2), which is already
    optimised via SentenceTransformer batch encoding. Text splitting
    on 20 pages takes ~0.3 seconds — not worth multiprocessing.

    For genuinely large batch jobs (100+ documents), use the
    /api/scan endpoint which uses ThreadPoolExecutor for concurrent
    I/O-bound extraction across multiple files simultaneously.

Thread model:
    ThreadPoolExecutor(max_workers=N) for concurrent multi-file extraction.
    I/O-bound work (reading PDFs, running Tesseract, ffmpeg) benefits
    from threading even with the GIL because most time is spent
    waiting for I/O or in C extensions that release the GIL.

File size limits:
    Max file size configurable in general_settings.json.
    Default: 2GB for video, 500MB for everything else.
    Prevents runaway Whisper jobs on massive files.

Path validation:
    All file paths validated to be within the allowed Dropbox sync
    directory before processing. Prevents path traversal attacks via
    the /api/ingest endpoint.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import xxhash

from src import database as db
from src.config_loader import general_settings
from src.embeddings import embed_chunks
from src.extractors import extract, is_supported
from src.logger import get_logger
from src.tagger import assign_tags

logger = get_logger()
CFG    = general_settings["ingestion"]
PATHS  = general_settings["paths"]

# Workers for concurrent multi-file extraction (I/O-bound)
import multiprocessing as mp
NUM_WORKERS = max(1, mp.cpu_count() - 1)

# File size limits (bytes)
VIDEO_EXTS   = set(CFG["video_extensions"])
MAX_VIDEO_SZ = CFG.get("max_video_bytes",  2 * 1024 ** 3)   # 2 GB
MAX_OTHER_SZ = CFG.get("max_file_bytes",   500 * 1024 ** 2)  # 500 MB


# ── Path validation ───────────────────────────────────────────────────────────

def _validate_path(path: str) -> str:
    """
    Ensure path is within the allowed Dropbox sync directory.
    Raises ValueError for paths outside the allowed root.

    This prevents path traversal attacks from the /api/ingest endpoint
    where a malicious caller could pass /etc/passwd or /opt/ragbase/.env.
    """
    allowed   = Path(PATHS["dropbox_sync"]).resolve()
    requested = Path(path).resolve()

    if not str(requested).startswith(str(allowed)):
        raise ValueError(
            f"Path '{path}' is outside the allowed sync directory '{allowed}'"
        )
    return str(requested)


# ── File hashing ──────────────────────────────────────────────────────────────

def _hash_file(path: str) -> str:
    """
    xxhash64 is ~10x faster than SHA256 for large files.
    Sufficient for deduplication — not a cryptographic requirement.
    """
    h = xxhash.xxh64()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Single file ingestion ─────────────────────────────────────────────────────

def ingest_file(file_path: str, skip_path_validation: bool = False) -> dict:
    """
    Ingest one file end-to-end.

    skip_path_validation: set True only for internal scan_and_ingest()
    calls where the path was already validated by the filesystem scan.

    Returns:
        { "status": "complete"|"skipped"|"failed",
          "doc_id": str|None,
          "error":  str|None }
    """
    # ── Validate path ──────────────────────────────────────────────
    try:
        if not skip_path_validation:
            file_path = _validate_path(file_path)
    except ValueError as e:
        logger.warning(f"INGEST | Path validation failed: {e}")
        return {"status": "failed", "doc_id": None, "error": str(e)}

    path = Path(file_path)

    if not path.exists():
        return {"status": "failed", "doc_id": None,
                "error": "File not found"}

    if not is_supported(file_path):
        return {"status": "skipped", "doc_id": None,
                "error": "Unsupported file type"}

    # ── File size gate ─────────────────────────────────────────────
    file_size = path.stat().st_size
    is_video  = path.suffix.lower() in VIDEO_EXTS
    size_limit = MAX_VIDEO_SZ if is_video else MAX_OTHER_SZ

    if file_size > size_limit:
        limit_mb = size_limit // (1024 ** 2)
        err = f"File too large: {file_size // (1024**2)}MB > {limit_mb}MB limit"
        logger.warning(f"INGEST | {path.name} | {err}")
        return {"status": "failed", "doc_id": None, "error": err}

    logger.info(f"INGEST | Starting: {path.name} ({file_size // 1024}KB)")
    t0 = time.perf_counter()

    # ── Deduplication ──────────────────────────────────────────────
    file_hash = _hash_file(file_path)
    existing  = db.doc_exists_by_hash(file_hash)
    if existing:
        logger.info(
            f"INGEST | Duplicate skipped: {path.name} "
            f"(already ingested as {existing})"
        )
        return {"status": "skipped", "doc_id": existing, "error": None}

    # ── Queue entry ────────────────────────────────────────────────
    qid = db.queue_file(file_path, file_size)
    db.update_queue_status(qid, "processing")

    try:
        # ── Stage 1: Extract ───────────────────────────────────────
        extraction = extract(file_path)
        full_text  = extraction["text"]
        chunks     = extraction["chunks"]
        title      = extraction.get("title")
        metadata   = extraction.get("metadata", {})
        word_count = extraction.get("word_count", 0)

        if not chunks:
            raise ValueError("No content could be extracted from this file")

        t_extracted = time.perf_counter()
        logger.info(
            f"INGEST | Extracted {len(chunks)} chunks "
            f"in {t_extracted - t0:.1f}s | {path.name}"
        )

        # ── Stage 2: Auto-tag ──────────────────────────────────────
        tags = assign_tags(full_text)

        # ── Stage 3: File type classification ─────────────────────
        ext_map = {
            ".pdf":  "pdf",  ".docx": "word",  ".doc": "word",
            ".md":   "markdown", ".txt": "text", ".markdown": "text",
            ".msg":  "email", ".eml": "email",
            ".png":  "image", ".jpg": "image", ".jpeg": "image",
            ".tiff": "image", ".bmp": "image", ".webp": "image",
            ".mp4":  "video", ".mov": "video", ".avi": "video",
            ".mkv":  "video", ".m4a": "audio", ".mp3": "audio",
            ".wav":  "audio",
        }
        file_type = ext_map.get(path.suffix.lower(), "other")

        # ── Stage 4: Insert document record ───────────────────────
        doc_id = db.insert_document(
            path=file_path,
            filename=path.name,
            file_type=file_type,
            file_hash=file_hash,
            title=title,
            tags=tags,
            word_count=word_count,
            metadata=metadata,
        )

        # ── Stage 5: Batch embed ───────────────────────────────────
        chunk_texts = [c["content"] for c in chunks]
        t_embed_start = time.perf_counter()
        embeddings  = embed_chunks(chunk_texts)
        logger.info(
            f"INGEST | Embedded {len(chunks)} chunks "
            f"in {time.perf_counter() - t_embed_start:.1f}s"
        )

        # ── Stage 6: Batch insert chunks + vectors ─────────────────
        db.insert_chunks_batch(doc_id, chunks, embeddings)
        db.update_document_chunks(doc_id, len(chunks))

        db.update_queue_status(qid, "complete")

        total = time.perf_counter() - t0
        logger.event(
            "ingest_complete",
            actor="cli" if skip_path_validation else "api",
            file=path.name,
            doc_id=doc_id,
            chunks=len(chunks),
            words=word_count,
            tags=tags,
            elapsed=f"{total:.1f}s",
        )
        return {"status": "complete", "doc_id": doc_id, "error": None}

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        db.update_queue_status(qid, "failed", error=error_msg)
        logger.error(f"INGEST | FAILED: {path.name} | {error_msg}")

        # Move to failed directory so it doesn't re-queue on next scan
        failed_dir = Path(PATHS["failed_dir"])
        failed_dir.mkdir(parents=True, exist_ok=True)
        try:
            dest = failed_dir / path.name
            # Avoid overwrite collision
            if dest.exists():
                dest = failed_dir / f"{path.stem}_{qid[:8]}{path.suffix}"
            path.rename(dest)
            logger.info(f"INGEST | Moved failed file to {dest}")
        except Exception as mv_err:
            logger.warning(f"INGEST | Could not move failed file: {mv_err}")

        return {"status": "failed", "doc_id": None, "error": error_msg}


# ── Batch scan ────────────────────────────────────────────────────────────────

def scan_and_ingest(folder: Optional[str] = None) -> dict:
    """
    Scan the Dropbox sync folder for new files and ingest them.

    Called by APScheduler every 5 minutes and by POST /api/scan.

    Extraction is I/O-bound (reading files, OCR, ffmpeg, PDF parsing).
    ThreadPoolExecutor allows concurrent extraction across multiple files.
    Embedding is compute-bound — batched per file, sequential across files.

    Videos are processed after all other files to avoid saturating
    CPU with Whisper while other fast files are waiting.
    """
    scan_dir = Path(folder or PATHS["dropbox_sync"])
    if not scan_dir.exists():
        logger.warning(f"SCAN | Dropbox folder not found: {scan_dir}")
        return {"scanned": 0, "complete": 0, "failed": 0, "skipped": 0}

    new_files: list[str] = []
    for f in scan_dir.rglob("*"):
        if not f.is_file():
            continue
        if not is_supported(str(f)):
            continue
        if db.path_already_queued(str(f)):
            continue
        new_files.append(str(f))

    logger.info(f"SCAN | {len(new_files)} new files found in {scan_dir}")
    if not new_files:
        return {"scanned": 0, "complete": 0, "failed": 0, "skipped": 0}

    # Separate videos (long Whisper jobs) from everything else
    videos = [f for f in new_files if Path(f).suffix.lower() in VIDEO_EXTS]
    others = [f for f in new_files if Path(f).suffix.lower() not in VIDEO_EXTS]

    stats = {
        "scanned":  len(new_files),
        "complete": 0,
        "failed":   0,
        "skipped":  0,
    }

    def _record(result: dict):
        s = result.get("status", "failed")
        stats[s] = stats.get(s, 0) + 1

    # Non-video files: concurrent extraction via ThreadPoolExecutor
    if others:
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
            futures = {
                pool.submit(ingest_file, f, True): f
                for f in others
            }
            for future in as_completed(futures):
                try:
                    _record(future.result())
                except Exception as e:
                    logger.error(f"SCAN | Worker exception: {e}")
                    stats["failed"] += 1

    # Videos: sequential (Whisper saturates CPU on its own)
    for video in videos:
        try:
            _record(ingest_file(video, skip_path_validation=True))
        except Exception as e:
            logger.error(f"SCAN | Video ingestion exception: {e}")
            stats["failed"] += 1

    logger.event("scan_complete", actor="scheduler", **stats)
    return stats


# ── Reindex ───────────────────────────────────────────────────────────────────

def reindex_all() -> dict:
    """
    Clear all embeddings and re-process the entire corpus.
    Use after swapping embedding models.
    See scripts/reindex.py for the CLI wrapper.
    """
    logger.warning("REINDEX | Clearing all vectors and chunks")

    with db.get_conn() as conn:
        conn.execute("DELETE FROM vec_chunks")
        conn.execute("DELETE FROM chunks")
        conn.execute("UPDATE documents SET chunk_count=0")

    with db.get_conn() as conn:
        docs = conn.execute(
            "SELECT id, path FROM documents"
        ).fetchall()

    logger.info(f"REINDEX | Re-ingesting {len(docs)} documents")
    stats = {"total": len(docs), "complete": 0, "failed": 0}

    for doc in docs:
        path   = doc["path"]
        doc_id = doc["id"]

        if not Path(path).exists():
            logger.warning(f"REINDEX | File missing: {path}")
            stats["failed"] += 1
            continue

        try:
            extraction  = extract(path)
            chunks      = extraction["chunks"]
            chunk_texts = [c["content"] for c in chunks]
            embeddings  = embed_chunks(chunk_texts)
            db.insert_chunks_batch(doc_id, chunks, embeddings)
            db.update_document_chunks(doc_id, len(chunks))
            stats["complete"] += 1
            logger.info(f"REINDEX | ✓ {Path(path).name}")
        except Exception as e:
            logger.error(f"REINDEX | ✗ {path}: {e}")
            stats["failed"] += 1

    logger.event("reindex_complete", actor="cli", **stats)
    return stats