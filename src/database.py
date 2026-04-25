"""
database.py — SQLite + sqlite-vec vector database.

Schema overview:
    documents     — one row per ingested file with metadata and tags
    chunks        — text chunks extracted from documents
    vec_chunks    — sqlite-vec virtual table storing 384-dim embeddings
    ingest_queue  — tracks file processing state for the dashboard

Why SQLite + sqlite-vec instead of ChromaDB or Postgres?
    - Zero external services — everything in one file on disk
    - sqlite-vec does ANN (approximate nearest neighbour) search
      using HNSW internally — sub-2-second search at 1M+ chunks
    - The entire corpus is one .db file → trivial to backup / restore
    - SQLCipher can encrypt the backup file directly

sqlite-vec search API:
    SELECT chunk_id, distance
    FROM vec_chunks
    WHERE embedding MATCH ?
      AND k = 10
    ORDER BY distance

Distance is L2 by default. With normalize_embeddings=True on BGE-small,
L2 distance is equivalent to cosine distance — smaller = more similar.
"""
import asyncio
import json
import sqlite3
import struct
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sqlite_vec

from src.config_loader import general_settings
from src.logger import get_logger

logger = get_logger()

DB_PATH    = general_settings["paths"]["db_path"]
EMBED_DIMS = general_settings["embedding"]["dimensions"]


# ── sqlite-vec serialisation ──────────────────────────────────────────────────

def _serialize(v: list[float]) -> bytes:
    """Pack a float list into little-endian IEEE 754 bytes for sqlite-vec."""
    return struct.pack(f"{len(v)}f", *v)


# ── Connection factory ────────────────────────────────────────────────────────

def _make_conn(path: str = DB_PATH) -> sqlite3.Connection:
    """
    Open a connection with sqlite-vec loaded and WAL mode enabled.

    WAL (Write-Ahead Logging) allows concurrent readers while a writer
    is active — essential for the dashboard reading while ingestion writes.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")  # wait 5s if locked
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn(path: str = DB_PATH):
    """Context manager — auto-commits on success, rolls back on exception."""
    conn = _make_conn(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema setup ──────────────────────────────────────────────────────────────

def setup_db(path: str = DB_PATH) -> None:
    """
    Create all tables and indexes if they do not exist.
    Safe to call on every startup — uses CREATE IF NOT EXISTS.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with get_conn(path) as conn:
        conn.executescript(f"""
            -- ── Documents ──────────────────────────────────────
            CREATE TABLE IF NOT EXISTS documents (
                id           TEXT PRIMARY KEY,
                path         TEXT NOT NULL UNIQUE,
                filename     TEXT NOT NULL,
                file_type    TEXT NOT NULL,
                file_hash    TEXT NOT NULL,
                title        TEXT,
                tags         TEXT DEFAULT '[]',   -- JSON array
                word_count   INTEGER DEFAULT 0,
                chunk_count  INTEGER DEFAULT 0,
                ingested_at  TEXT NOT NULL,
                metadata     TEXT DEFAULT '{{}}', -- JSON object
                status       TEXT DEFAULT 'complete'
            );

            CREATE INDEX IF NOT EXISTS idx_doc_hash
                ON documents(file_hash);
            CREATE INDEX IF NOT EXISTS idx_doc_type
                ON documents(file_type);
            CREATE INDEX IF NOT EXISTS idx_doc_ingested
                ON documents(ingested_at DESC);

            -- ── Chunks ─────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS chunks (
                id              TEXT PRIMARY KEY,
                document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                content         TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                page_number     INTEGER,
                timestamp_start REAL,    -- video: seconds from start
                timestamp_end   REAL,
                metadata        TEXT DEFAULT '{{}}'
            );

            CREATE INDEX IF NOT EXISTS idx_chunk_doc
                ON chunks(document_id);

            -- ── Vector table ────────────────────────────────────
            -- sqlite-vec virtual table — one row per chunk embedding
            -- FLOAT[{EMBED_DIMS}] matches BGE-small-en-v1.5 output
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_id  TEXT PARTITION KEY,
                embedding FLOAT[{EMBED_DIMS}]
            );

            -- ── Ingestion queue ─────────────────────────────────
            CREATE TABLE IF NOT EXISTS ingest_queue (
                id           TEXT PRIMARY KEY,
                path         TEXT NOT NULL,
                filename     TEXT NOT NULL,
                status       TEXT DEFAULT 'queued',  -- queued|processing|complete|failed|skipped
                error        TEXT,
                queued_at    TEXT NOT NULL,
                processed_at TEXT,
                file_size    INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_queue_status
                ON ingest_queue(status, queued_at DESC);
        """)

    logger.info(f"DB | Schema ready at {path}")


# ── Document operations ───────────────────────────────────────────────────────

def doc_exists_by_hash(file_hash: str) -> Optional[str]:
    """Return document id if this file hash is already ingested, else None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE file_hash = ?", (file_hash,)
        ).fetchone()
    return row["id"] if row else None


def insert_document(
    *,
    path:       str,
    filename:   str,
    file_type:  str,
    file_hash:  str,
    title:      Optional[str] = None,
    tags:       list[str] = None,
    word_count: int = 0,
    metadata:   dict = None,
) -> str:
    """Insert a new document record. Returns the new UUID."""
    doc_id = str(uuid.uuid4())
    now    = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO documents
               (id, path, filename, file_type, file_hash, title, tags,
                word_count, ingested_at, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                doc_id, path, filename, file_type, file_hash,
                title,
                json.dumps(tags or []),
                word_count,
                now,
                json.dumps(metadata or {}),
            ),
        )
    logger.debug(f"DB | Document inserted | id={doc_id} | file={filename}")
    return doc_id


def update_document_chunks(doc_id: str, chunk_count: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET chunk_count=? WHERE id=?",
            (chunk_count, doc_id),
        )


def get_document(doc_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tags"]     = json.loads(d.get("tags") or "[]")
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    return d


def list_recent_documents(
    limit: int = 20,
    file_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> list[dict]:
    query  = "SELECT * FROM documents WHERE 1=1"
    params: list = []

    if file_type:
        query += " AND file_type=?"
        params.append(file_type)
    if tag:
        # Tags stored as JSON array — use JSON_EACH for correct filtering
        query = (
            "SELECT d.* FROM documents d, json_each(d.tags) t "
            "WHERE t.value=?"
        )
        params = [tag]
        if file_type:
            query += " AND d.file_type=?"
            params.append(file_type)

    query += " ORDER BY ingested_at DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        d["tags"]     = json.loads(d.get("tags") or "[]")
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        result.append(d)
    return result


# ── Chunk operations ──────────────────────────────────────────────────────────

def insert_chunks_batch(
    document_id: str,
    chunks: list[dict],
    embeddings: list[list[float]],
) -> None:
    """
    Insert all chunks and their embeddings in one transaction.

    chunks: list of dicts with keys:
        content, chunk_index, page_number (optional),
        timestamp_start (optional), timestamp_end (optional), metadata (optional)

    embeddings: parallel list of float lists, one per chunk.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"Chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must match"
        )

    chunk_rows   = []
    vec_rows     = []

    for chunk, emb in zip(chunks, embeddings):
        cid = str(uuid.uuid4())
        chunk_rows.append((
            cid,
            document_id,
            chunk["content"],
            chunk["chunk_index"],
            chunk.get("page_number"),
            chunk.get("timestamp_start"),
            chunk.get("timestamp_end"),
            json.dumps(chunk.get("metadata") or {}),
        ))
        vec_rows.append((cid, _serialize(emb)))

    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO chunks
               (id, document_id, content, chunk_index, page_number,
                timestamp_start, timestamp_end, metadata)
               VALUES (?,?,?,?,?,?,?,?)""",
            chunk_rows,
        )
        conn.executemany(
            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?,?)",
            vec_rows,
        )

    logger.debug(f"DB | {len(chunks)} chunks + embeddings inserted | doc={document_id}")


def get_chunks_by_doc(document_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Vector search ─────────────────────────────────────────────────────────────

def vector_search(
    query_embedding: list[float],
    top_k: int = 10,
    tag_filter: Optional[str] = None,
    file_type_filter: Optional[str] = None,
) -> list[dict]:
    """
    Semantic search using sqlite-vec ANN (HNSW).

    Returns list of result dicts with keys:
        chunk_id, distance, content, document_id,
        filename, file_type, tags, page_number,
        timestamp_start, timestamp_end

    The JOIN with chunks and documents adds metadata without
    a second query — sqlite optimises this well with the indexes.

    tag_filter: if provided, only return results from documents
    with this tag. Applied as a post-filter (sqlite-vec does not
    support pre-filtering on metadata columns natively).
    """
    serialized = _serialize(query_embedding)

    with get_conn() as conn:
        # Fetch more results than top_k if filtering, to account for
        # results that will be filtered out
        fetch_k = top_k * 5 if (tag_filter or file_type_filter) else top_k

        rows = conn.execute(
            f"""
            SELECT
                v.chunk_id,
                v.distance,
                c.content,
                c.document_id,
                c.page_number,
                c.timestamp_start,
                c.timestamp_end,
                d.filename,
                d.file_type,
                d.tags,
                d.path
            FROM vec_chunks v
            JOIN chunks c   ON c.id = v.chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE v.embedding MATCH ?
              AND k = {fetch_k}
            ORDER BY v.distance
            """,
            [serialized],
        ).fetchall()

    results = []
    for row in rows:
        tags = json.loads(row["tags"] or "[]")

        if tag_filter and tag_filter not in tags:
            continue
        if file_type_filter and row["file_type"] != file_type_filter:
            continue

        results.append({
            "chunk_id":       row["chunk_id"],
            "distance":       row["distance"],
            "score":          round(1 - row["distance"], 4),
            "content":        row["content"],
            "document_id":    row["document_id"],
            "page_number":    row["page_number"],
            "timestamp_start": row["timestamp_start"],
            "timestamp_end":  row["timestamp_end"],
            "filename":       row["filename"],
            "file_type":      row["file_type"],
            "tags":           tags,
            "path":           row["path"],
        })

        if len(results) >= top_k:
            break

    return results


# ── Queue operations ──────────────────────────────────────────────────────────

def queue_file(path: str, file_size: int = 0) -> str:
    """Add a file to the ingestion queue. Returns queue entry id."""
    qid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO ingest_queue
               (id, path, filename, status, queued_at, file_size)
               VALUES (?,?,?,?,?,?)""",
            (qid, path, Path(path).name, "queued", now, file_size),
        )
    return qid


def update_queue_status(
    qid: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE ingest_queue
               SET status=?, error=?, processed_at=?
               WHERE id=?""",
            (status, error, now, qid),
        )


def get_queue_stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM ingest_queue GROUP BY status"
        ).fetchall()
        total_docs = conn.execute(
            "SELECT COUNT(*) as n FROM documents"
        ).fetchone()["n"]
        total_chunks = conn.execute(
            "SELECT COUNT(*) as n FROM chunks"
        ).fetchone()["n"]

    stats = {s: 0 for s in ["queued", "processing", "complete", "failed", "skipped"]}
    for row in rows:
        stats[row["status"]] = row["n"]

    stats["total_documents"] = total_docs
    stats["total_chunks"]    = total_chunks
    return stats


def get_queue_items(
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    query  = "SELECT * FROM ingest_queue"
    params: list = []
    if status:
        query += " WHERE status=?"
        params.append(status)
    query += " ORDER BY queued_at DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def path_already_queued(path: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM ingest_queue WHERE path=? AND status IN ('queued','processing','complete')",
            (path,),
        ).fetchone()
    return row is not None


# ── Async wrappers ────────────────────────────────────────────────────────────

async def async_vector_search(
    query_embedding: list[float],
    top_k: int = 10,
    tag_filter: Optional[str] = None,
    file_type_filter: Optional[str] = None,
) -> list[dict]:
    """Non-blocking vector search for async routes."""
    return await asyncio.to_thread(
        vector_search, query_embedding, top_k, tag_filter, file_type_filter
    )


async def async_list_recent(
    limit: int = 20,
    file_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> list[dict]:
    return await asyncio.to_thread(list_recent_documents, limit, file_type, tag)