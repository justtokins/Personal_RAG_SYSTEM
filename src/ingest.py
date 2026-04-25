"""
ingest.py — Full ingestion pipeline orchestrator.

Pipeline per file:
    1. Hash file (xxhash) → check dedup
    2. Queue entry created
    3. Extract text + chunks (type-specific extractor)
    4. Auto-tag via Claude API (async, non-blocking)
    5. Batch embed all chunks (SentenceTransformer, GPU-optional)
    6. Insert document + chunks + vectors in one DB transaction
    7. Queue entry marked complete / failed

Cron integration:
    The `scan_and_ingest()` function is called by the cron job.
    It scans the Dropbox sync folder, skips already-processed files,
    and processes new ones using a ThreadPoolExecutor for I/O-bound
    tasks (reading, OCR) while embedding is batched at the end.

Performance:
    - Multicore extraction: ThreadPoolExecutor for I/O-bound work
    - Batch embedding: all chunks embedded in one SentenceTransformer call
    - Single DB transaction per document

20-page PDF target: < 60 seconds on 4GB/4-core droplet.
1-hour video target: < 20 minutes (Whisper medium, CPU).
"""
from __future__ import annotations

import hashlib
import multiprocessing as mp
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

logger     = get_logger()
CFG        = general_settings["ingestion"]
PATHS      = general_settings["paths"]
NUM_WORKERS = max(1, mp.cpu_count() - 1)


# ── File hashing ──────────────────────────────────────────────────────────────

def _hash_file(path: str) -> str:
    """xxhash is ~10x faster than SHA256 for large files."""
    h = xxhash.xxh64()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Single file ingestion ─────────────────────────────────────────────────────

def ingest_file(file_path: str) -> dict:
    """
    Ingest one file end-to-end.

    Returns:
        { "status": "complete"|"skipped"|"failed", "doc_id": str|None, "error": str|None }
    """
    path = Path(file_path)

    if not path.exists():
        return {"status": "failed", "doc_id": None, "error": "File not found"}

    if not is_supported(file_path):
        return {"status": "skipped", "doc_id": None, "error": "Unsupported type"}

    logger.info(f"INGEST | Starting: {path.name}")

    # ── Deduplication ──────────────────────────────────────────
    file_hash  = _hash_file(file_path)
    existing   = db.doc_exists_by_hash(file_hash)
    if existing:
        logger.info(f"INGEST | Duplicate — already ingested as {existing}: {path.name}")
        return {"status": "skipped", "doc_id": existing, "error": None}

    # ── Queue entry ────────────────────────────────────────────
    file_size  = path.stat().st_size
    qid        = db.queue_file(file_path, file_size)
    db.update_queue_status(qid, "processing")

    try:
        # ── Stage 1: Extract ───────────────────────────────────
        extraction = extract(file_path)
        full_text  = extraction["text"]
        chunks     = extraction["chunks"]
        title      = extraction.get("title")
        metadata   = extraction.get("metadata", {})
        word_count = extraction.get("word_count", 0)

        if not chunks:
            raise ValueError("No content extracted from file")

        # ── Stage 2: Auto-tag (Claude API) ─────────────────────
        tags = assign_tags(full_text)

        # ── Stage 3: Determine file type ───────────────────────
        ext_to_type = {
            ".pdf": "pdf", ".docx": "word", ".doc": "word",
            ".md": "markdown", ".txt": "text",
            ".msg": "email", ".eml": "email",
            ".png": "image", ".jpg": "image", ".jpeg": "image",
            ".tiff": "image", ".bmp": "image", ".webp": "image",
            ".mp4": "video", ".mov": "video", ".avi": "video",
            ".mkv": "video", ".m4a": "audio", ".mp3": "audio",
            ".wav": "audio",
        }
        file_type = ext_to_type.get(path.suffix.lower(), "other")

        # ── Stage 4: Insert document record ────────────────────
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

        # ── Stage 5: Batch embed all chunks ────────────────────
        chunk_texts  = [c["content"] for c in chunks]
        embeddings   = embed_chunks(chunk_texts)

        # ── Stage 6: Batch insert chunks + vectors ─────────────
        db.insert_chunks_batch(doc_id, chunks, embeddings)
        db.update_document_chunks(doc_id, len(chunks))

        # ── Mark complete ───────────────────────────────────────
        db.update_queue_status(qid, "complete")
        logger.event(
            "ingest_complete",
            file=path.name,
            doc_id=doc_id,
            chunks=len(chunks),
            tags=tags,
            words=word_count,
        )
        return {"status": "complete", "doc_id": doc_id, "error": None}

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        db.update_queue_status(qid, "failed", error=error_msg)
        logger.error(f"INGEST | FAILED: {path.name} | {error_msg}")

        # Move to failed directory
        failed_dir = Path(PATHS["failed_dir"])
        failed_dir.mkdir(parents=True, exist_ok=True)
        try:
            path.rename(failed_dir / path.name)
        except Exception:
            pass

        return {"status": "failed", "doc_id": None, "error": error_msg}


# ── Batch / folder ingestion ──────────────────────────────────────────────────

def scan_and_ingest(folder: Optional[str] = None) -> dict:
    """
    Scan a folder for new files and ingest them.

    Called by the cron job every 5 minutes.
    Skips files already in the queue (any non-failed status).
    Uses ThreadPoolExecutor for concurrent extraction (I/O-bound).

    Returns summary stats dict.
    """
    scan_dir = Path(folder or PATHS["dropbox_sync"])
    if not scan_dir.exists():
        logger.warning(f"SCAN | Dropbox folder not found: {scan_dir}")
        return {"scanned": 0, "queued": 0, "complete": 0, "failed": 0, "skipped": 0}

    # Collect all supported files not yet processed
    new_files: list[str] = []
    for f in scan_dir.rglob("*"):
        if not f.is_file():
            continue
        if not is_supported(str(f)):
            continue
        if db.path_already_queued(str(f)):
            continue
        new_files.append(str(f))

    logger.info(f"SCAN | Found {len(new_files)} new files in {scan_dir}")

    if not new_files:
        return {"scanned": 0, "queued": 0, "complete": 0, "failed": 0, "skipped": 0}

    # Separate video files (long, single-threaded Whisper) from others
    video_exts = set(general_settings["ingestion"]["video_extensions"])
    videos     = [f for f in new_files if Path(f).suffix.lower() in video_exts]
    others     = [f for f in new_files if Path(f).suffix.lower() not in video_exts]

    stats = {"scanned": len(new_files), "complete": 0, "failed": 0, "skipped": 0}

    # Process non-video files concurrently
    if others:
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
            futures = {pool.submit(ingest_file, f): f for f in others}
            for future in as_completed(futures):
                result = future.result()
                stats[result["status"]] = stats.get(result["status"], 0) + 1

    # Process videos sequentially (Whisper is already compute-saturating)
    for video in videos:
        result = ingest_file(video)
        stats[result["status"]] = stats.get(result["status"], 0) + 1

    stats["queued"] = len(new_files)
    logger.event("scan_complete", **stats)
    return stats


def reindex_all() -> dict:
    """
    Re-process all previously ingested files.
    Used when swapping embedding models.

    This:
    1. Clears all chunks and vectors
    2. Re-extracts and re-embeds every document
    3. Preserves document records (id, tags, metadata)

    Run: python scripts/reindex.py
    """
    import sqlite3

    logger.warning("REINDEX | Starting full reindex — this will clear all vectors")

    # Clear all chunks and vectors
    with db.get_conn() as conn:
        conn.execute("DELETE FROM vec_chunks")
        conn.execute("DELETE FROM chunks")
        conn.execute("UPDATE documents SET chunk_count=0")
        conn.execute("UPDATE ingest_queue SET status='queued' WHERE status='complete'")

    # Get all document paths
    with db.get_conn() as conn:
        docs = conn.execute(
            "SELECT id, path, file_hash FROM documents"
        ).fetchall()

    logger.info(f"REINDEX | Re-ingesting {len(docs)} documents")

    stats = {"total": len(docs), "complete": 0, "failed": 0, "skipped": 0}

    for doc in docs:
        path     = doc["path"]
        doc_id   = doc["id"]
        if not Path(path).exists():
            logger.warning(f"REINDEX | File missing: {path}")
            stats["failed"] += 1
            continue

        try:
            extraction = extract(path)
            chunks     = extraction["chunks"]
            chunk_texts = [c["content"] for c in chunks]
            embeddings  = embed_chunks(chunk_texts)
            db.insert_chunks_batch(doc_id, chunks, embeddings)
            db.update_document_chunks(doc_id, len(chunks))
            stats["complete"] += 1
            logger.info(f"REINDEX | Done: {Path(path).name}")
        except Exception as e:
            logger.error(f"REINDEX | Failed {path}: {e}")
            stats["failed"] += 1

    logger.event("reindex_complete", **stats)
    return stats