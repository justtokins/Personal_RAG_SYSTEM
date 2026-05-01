"""
database.py — SQLite + sqlite-vec vector database.

sqlite-vec 0.1.6 API notes (verified against source):
    CREATE VIRTUAL TABLE vec_chunks USING vec0(
        embedding FLOAT[384]
    );

    The vec0 table has an implicit INTEGER rowid.
    We link chunks → vec_chunks via matching rowids.

    Search syntax:
        SELECT rowid, distance
        FROM vec_chunks
        WHERE embedding MATCH ?    -- serialized float blob
          AND k = ?                -- integer, not string interpolation
        ORDER BY distance

    distance is L2 (Euclidean). For unit vectors (normalize=True in
    embeddings.py), L2 distance and cosine distance are monotonically
    equivalent: lower distance = higher similarity.

Dropbox upload model:
    The chunk_id linkage is done by inserting in the same transaction
    and recording the sqlite last_insert_rowid() per chunk.

Connection model:
    Each call opens a fresh connection and closes it in the finally block.
    check_same_thread=False is NOT used — each thread gets its own connection.
    WAL mode allows concurrent readers with one writer.
    busy_timeout=30000 (30s) handles long video ingestion write locks.

    IMPORTANT: All sqlite3.Row objects are converted to plain dicts
    BEFORE the connection closes. sqlite3.Row holds a reference to the
    cursor/connection and will raise after close.

Audit log:
    audit_log is append-only — no UPDATE or DELETE ever runs on it.
    It records every MCP tool call and every ingestion event with full
    context so the client can query the trail independently of app logs.
    Required by client security spec.
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

logger     = get_logger()
DB_PATH    = general_settings["paths"]["db_path"]
EMBED_DIMS = general_settings["embedding"]["dimensions"]
MIN_SCORE  = general_settings["search"].get("min_score_threshold", 0.0)


# ── Serialisation ─────────────────────────────────────────────────────────────

def _serialize(v: list[float]) -> bytes:
    """Pack float list into little-endian IEEE 754 for sqlite-vec MATCH."""
    return struct.pack(f"{len(v)}f", *v)


def _distance_to_score(distance: float) -> float:
    return round(1.0 / (1.0 + max(0.0, distance)), 4)


# ── Connection factory ────────────────────────────────────────────────────────

def _make_conn(path: str = DB_PATH) -> sqlite3.Connection:
    """
    WAL mode: readers never block writers, writers never block readers.
    busy_timeout=30000: wait up to 30 seconds if DB is locked by a
    long-running ingestion write (e.g. 10k-chunk video insert).
    """
    conn = sqlite3.connect(path, check_same_thread=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")   
    conn.execute("PRAGMA cache_size=-64000")   
    return conn


@contextmanager
def get_conn(path: str = DB_PATH):
    """
    Context manager yielding a connection.
    Commits on clean exit, rolls back on exception, always closes.

    IMPORTANT: Callers must convert sqlite3.Row objects to dicts INSIDE
    this context (before the connection closes). Accessing Row fields
    after connection.close() raises sqlite3.ProgrammingError.
    All read functions in this module call .fetchall() + [dict(r) for r in rows]
    before the context exits.
    """
    conn = _make_conn(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def setup_db(path: str = DB_PATH) -> None:
    """
    Create all tables if they do not exist.

    Schema design:
        documents   — one row per file, metadata + tags
        chunks      — text chunks with page/timestamp metadata
        vec_chunks  — sqlite-vec virtual table, rowid = chunk rowid
        ingest_queue — file processing state for the dashboard
        audit_log   — append-only record of all MCP calls and ingestion events

    The vec_chunks rowid matches the chunks rowid in the same
    transaction. This lets us JOIN without a separate ID column.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with get_conn(path) as conn:
        # Documents
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id          TEXT PRIMARY KEY,
                path        TEXT NOT NULL UNIQUE,
                filename    TEXT NOT NULL,
                file_type   TEXT NOT NULL,
                file_hash   TEXT NOT NULL,
                title       TEXT,
                tags        TEXT DEFAULT '[]',
                word_count  INTEGER DEFAULT 0,
                chunk_count INTEGER DEFAULT 0,
                ingested_at TEXT NOT NULL,
                metadata    TEXT DEFAULT '{}'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_hash     ON documents(file_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_type     ON documents(file_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_ingested ON documents(ingested_at DESC)"
        )

        # Chunks
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id              TEXT NOT NULL UNIQUE,
                document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                content         TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                page_number     INTEGER,
                timestamp_start REAL,
                timestamp_end   REAL,
                metadata        TEXT DEFAULT '{}'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(document_id)"
        )

        # sqlite-vec virtual table
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                embedding FLOAT[{EMBED_DIMS}]
            )
        """)

        # Ingestion queue
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingest_queue (
                id           TEXT PRIMARY KEY,
                path         TEXT NOT NULL,
                filename     TEXT NOT NULL,
                status       TEXT DEFAULT 'queued',
                error        TEXT,
                queued_at    TEXT NOT NULL,
                processed_at TEXT,
                file_size    INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_queue_status "
            "ON ingest_queue(status, queued_at DESC)"
        )

        # ── Audit log — append-only, never updated or deleted ─────────────
        # Required by client security spec.
        # event_type: 'mcp_call' | 'ingest' | 'scan' | 'backup' | 'search'
        # actor:      'mcp' | 'api' | 'scheduler' | 'cli'
        # detail:     JSON blob with event-specific fields
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          TEXT PRIMARY KEY,
                ts          TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                actor       TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_log(ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type, ts DESC)"
        )

    logger.info(f"DB | Schema ready at {path}")


# ── Audit log ─────────────────────────────────────────────────────────────────

def audit(
    event_type: str,
    actor:      str,
    **detail,
) -> None:
    """
    Write one append-only audit record.

    Never raises — a logging failure must not interrupt the operation
    being logged. Errors are written to the application log only.

    Args:
        event_type: 'mcp_call' | 'ingest' | 'scan' | 'backup' | 'search'
        actor:      'mcp' | 'api' | 'scheduler' | 'cli'
        **detail:   Arbitrary key-value pairs serialised to JSON
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        rid = str(uuid.uuid4())
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (id, ts, event_type, actor, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (rid, now, event_type, actor, json.dumps(detail, default=str)),
            )
    except Exception as e:
        logger.error(f"AUDIT | Failed to write audit record: {e}")


def get_audit_log(
    event_type: Optional[str] = None,
    actor:      Optional[str] = None,
    since:      Optional[str] = None,   # ISO timestamp
    limit:      int = 100,
) -> list[dict]:
    """Query the audit log. Used by the dashboard and CLI."""
    query  = "SELECT * FROM audit_log WHERE 1=1"
    params: list = []
    if event_type:
        query += " AND event_type=?"
        params.append(event_type)
    if actor:
        query += " AND actor=?"
        params.append(actor)
    if since:
        query += " AND ts >= ?"
        params.append(since)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(min(limit, 1000))

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["detail"] = json.loads(d.get("detail") or "{}")
        results.append(d)
    return results


# ── Document operations ───────────────────────────────────────────────────────

def doc_exists_by_hash(file_hash: str) -> Optional[str]:
   
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE file_hash=?", (file_hash,)
        ).fetchone()
        return row["id"] if row else None   # extract string inside context


def insert_document(
    *,
    path:       str,
    filename:   str,
    file_type:  str,
    file_hash:  str,
    title:      Optional[str] = None,
    tags:       Optional[list] = None,
    word_count: int = 0,
    metadata:   Optional[dict] = None,
) -> str:
    doc_id = str(uuid.uuid4())
    now    = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO documents
               (id,path,filename,file_type,file_hash,title,tags,
                word_count,ingested_at,metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, path, filename, file_type, file_hash,
             title, json.dumps(tags or []),
             word_count, now, json.dumps(metadata or {})),
        )
    logger.debug(f"DB | Document inserted id={doc_id} file={filename}")
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
        d = dict(row)    # convert inside context

    d["tags"]     = json.loads(d.get("tags")     or "[]")
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    return d


def list_recent_documents(
    limit:     int = 20,
    file_type: Optional[str] = None,
    tag:       Optional[str] = None,
) -> list[dict]:
   
    if tag:
        query  = (
            "SELECT DISTINCT d.* FROM documents d, json_each(d.tags) t "
            "WHERE t.value=?"
        )
        params: list = [tag]
        if file_type:
            query += " AND d.file_type=?"
            params.append(file_type)
    else:
        query  = "SELECT * FROM documents WHERE 1=1"
        params = []
        if file_type:
            query += " AND file_type=?"
            params.append(file_type)

    query += " ORDER BY ingested_at DESC LIMIT ?"
    params.append(min(limit, 500))   # hard cap — dashboard doesn't need more

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        raw  = [dict(r) for r in rows]   # convert inside context

    result = []
    for d in raw:
        d["tags"]     = json.loads(d.get("tags")     or "[]")
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        result.append(d)
    return result


# ── Chunk operations ──────────────────────────────────────────────────────────

def insert_chunks_batch(
    document_id: str,
    chunks:      list[dict],
    embeddings:  list[list[float]],
) -> None:
    """
    Insert all chunks and embeddings in one transaction.

    sqlite-vec 0.1.6 rowid linkage:
        1. Insert chunk row → get its rowid via lastrowid
        2. Insert vec_chunks row with the same rowid
        3. JOIN at search time using rowid equality

    We cannot use executemany for the vec_chunks step because
    we need the lastrowid of each chunk individually.
    All inserts happen in a single transaction for atomicity.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) mismatch"
        )

    with get_conn() as conn:
        for chunk, emb in zip(chunks, embeddings):
            cid = str(uuid.uuid4())
            cur = conn.execute(
                """INSERT INTO chunks
                   (id, document_id, content, chunk_index,
                    page_number, timestamp_start, timestamp_end, metadata)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    cid,
                    document_id,
                    chunk["content"],
                    chunk["chunk_index"],
                    chunk.get("page_number"),
                    chunk.get("timestamp_start"),
                    chunk.get("timestamp_end"),
                    json.dumps(chunk.get("metadata") or {}),
                ),
            )
            chunk_rowid = cur.lastrowid
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)",
                (chunk_rowid, _serialize(emb)),
            )

    logger.debug(
        f"DB | {len(chunks)} chunks+embeddings inserted | doc={document_id}"
    )


def get_chunks_by_doc(document_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
        return [dict(r) for r in rows]   # convert inside context


# ── Vector search ─────────────────────────────────────────────────────────────

def vector_search(
    query_embedding:  list[float],
    top_k:            int = 10,
    tag_filter:       Optional[str] = None,
    file_type_filter: Optional[str] = None,
) -> list[dict]:

    serialized = _serialize(query_embedding)
    fetch_k    = top_k * 5 if (tag_filter or file_type_filter) else top_k

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                v.rowid      AS vec_rowid,
                v.distance,
                c.id         AS chunk_id,
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
            JOIN chunks    c ON c.rowid = v.rowid
            JOIN documents d ON d.id    = c.document_id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            """,
            (serialized, fetch_k),
        ).fetchall()
        raw = [dict(r) for r in rows]   # convert inside context

    results = []
    for row in raw:
        score = _distance_to_score(row["distance"])

        # FIX: apply threshold that was configured but never enforced
        if score < MIN_SCORE:
            continue

        tags = json.loads(row["tags"] or "[]")

        if tag_filter and tag_filter not in tags:
            continue
        if file_type_filter and row["file_type"] != file_type_filter:
            continue

        results.append({
            "chunk_id":        row["chunk_id"],
            "distance":        row["distance"],
            "score":           score,
            "content":         row["content"],
            "document_id":     row["document_id"],
            "page_number":     row["page_number"],
            "timestamp_start": row["timestamp_start"],
            "timestamp_end":   row["timestamp_end"],
            "filename":        row["filename"],
            "file_type":       row["file_type"],
            "tags":            tags,
            "path":            row["path"],
        })

        if len(results) >= top_k:
            break

    return results


# ── Queue operations ──────────────────────────────────────────────────────────

def queue_file(path: str, file_size: int = 0) -> str:
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
    qid: str, status: str, error: Optional[str] = None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status=?,error=?,processed_at=? WHERE id=?",
            (status, error, now, qid),
        )


def get_queue_stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM ingest_queue GROUP BY status"
        ).fetchall()
        total_docs   = conn.execute(
            "SELECT COUNT(*) as n FROM documents"
        ).fetchone()["n"]
        total_chunks = conn.execute(
            "SELECT COUNT(*) as n FROM chunks"
        ).fetchone()["n"]
        # convert all Row objects before connection closes
        status_rows  = [dict(r) for r in rows]

    stats = {s: 0 for s in
             ["queued", "processing", "complete", "failed", "skipped"]}
    for row in status_rows:
        stats[row["status"]] = row["n"]
    stats["total_documents"] = total_docs
    stats["total_chunks"]    = total_chunks
    return stats


def get_queue_items(
    status: Optional[str] = None,
    limit:  int = 50,
) -> list[dict]:
    query  = "SELECT * FROM ingest_queue"
    params: list = []
    if status:
        query += " WHERE status=?"
        params.append(status)
    query += " ORDER BY queued_at DESC LIMIT ?"
    params.append(min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]   # convert inside context


def path_already_queued(path: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM ingest_queue "
            "WHERE path=? AND status IN ('queued','processing','complete')",
            (path,),
        ).fetchone()
        return row is not None   # evaluate inside context


# ── Async wrappers ────────────────────────────────────────────────────────────

async def async_vector_search(
    query_embedding:  list[float],
    top_k:            int = 10,
    tag_filter:       Optional[str] = None,
    file_type_filter: Optional[str] = None,
) -> list[dict]:
    return await asyncio.to_thread(
        vector_search, query_embedding, top_k, tag_filter, file_type_filter
    )


async def async_list_recent(
    limit:     int = 20,
    file_type: Optional[str] = None,
    tag:       Optional[str] = None,
) -> list[dict]:
    return await asyncio.to_thread(list_recent_documents, limit, file_type, tag)