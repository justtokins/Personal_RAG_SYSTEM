"""
mcp_server.py — MCP server exposing 4 tools to Claude Desktop.

FastMCP import verification:
    mcp==1.0.0 ships FastMCP at mcp.server.fastmcp.FastMCP
    The ASGI app is obtained via mcp.sse_app() NOT get_asgi_app()

    Verify after install:
        python -c "from mcp.server.fastmcp import FastMCP; print('ok')"
        python -c "import mcp; print(dir(mcp))"

    If the import path changes in a future version, only this file
    needs to be updated.

Bearer auth:
    Implemented as a Starlette BaseHTTPMiddleware wrapping the
    FastMCP SSE ASGI app. Every request (except /health) must carry
    Authorization: Bearer <MCP_BEARER_TOKEN>.

    Invalid tokens → 401. Token logged as warning with client IP.
    No brute-force protection needed — Caddy rate limits connections
    and the token is 32 bytes of URL-safe random = 2^256 search space.

Claude Desktop config:
    ~/.claude/claude_desktop_config.json:
    {
      "mcpServers": {
        "ragbase": {
          "url": "https://your-domain.com/mcp/sse",
          "headers": { "Authorization": "Bearer YOUR_TOKEN" }
        }
      }
    }
"""
import os
from typing import Optional

from src import database as db
from src.embeddings import embed_query
from src.config_loader import general_settings
from src.logger import get_logger

logger = get_logger()
SCFG   = general_settings["search"]


# ── FastMCP server ────────────────────────────────────────────────────────────

def _create_mcp():
    """
    Import FastMCP and create the server instance.
    Deferred to runtime so import errors produce clear messages.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise ImportError(
            "mcp package not installed or wrong version. "
            "Run: pip install mcp==1.0.0\n"
            f"Original error: {e}"
        )
    return FastMCP(
        name="RAGBase",
        instructions=(
            "Personal knowledge base: documents, emails, images, videos. "
            "search_knowledge: semantic search across all content. "
            "get_document: full text and metadata by document ID. "
            "list_recent: browse recent additions with optional filters. "
            "get_video_segment: transcript at a specific video timestamp."
        ),
    )


mcp = _create_mcp()


# ── Tool 1: search_knowledge ──────────────────────────────────────────────────

@mcp.tool()
def search_knowledge(
    query:      str,
    top_k:      int            = 10,
    tag_filter: Optional[str]  = None,
    file_type:  Optional[str]  = None,
) -> str:
    """
    Semantic search across the entire knowledge base.

    Args:
        query:      Natural language search query
        top_k:      Number of results to return (1–50, default 10)
        tag_filter: Restrict to documents with this tag
        file_type:  Restrict to file type: pdf|word|email|image|video|text

    Returns:
        Formatted results with filename, page/timestamp citations and score.
    """
    logger.event("mcp_search", actor="mcp", q=query[:60], top_k=top_k, tag=tag_filter)

    top_k     = min(max(1, top_k), SCFG["max_top_k"])
    query_emb = embed_query(query)
    results   = db.vector_search(
        query_embedding=query_emb,
        top_k=top_k,
        tag_filter=tag_filter,
        file_type_filter=file_type,
    )

    if not results:
        return "No results found. Try broader search terms or check filters."

    lines = [f"## Results for: '{query}'\n"]
    for i, r in enumerate(results, 1):
        citation = f"[{r['filename']}"
        if r.get("page_number"):
            citation += f", p.{r['page_number']}"
        if r.get("timestamp_start") is not None:
            ts = int(r["timestamp_start"])
            citation += f", {ts // 60}:{ts % 60:02d}"
        citation += f"] score={r['score']:.3f}"

        lines.append(f"### {i}. {citation}")
        lines.append(r["content"])
        lines.append("")

    return "\n".join(lines)


# ── Tool 2: get_document ──────────────────────────────────────────────────────

@mcp.tool()
def get_document(doc_id: str) -> str:
    """
    Retrieve full content and metadata for a document.

    Args:
        doc_id: Document UUID from search_knowledge results.

    Returns:
        Full document text with metadata header.
    """
    logger.event("mcp_get_doc", actor="mcp", doc_id=doc_id)

    doc = db.get_document(doc_id)
    if not doc:
        return f"Document not found: {doc_id}"

    chunks    = db.get_chunks_by_doc(doc_id)
    full_text = "\n\n".join(c["content"] for c in chunks)

    header = (
        f"# {doc.get('title') or doc['filename']}\n"
        f"**File:** {doc['filename']}  \n"
        f"**Type:** {doc['file_type']}  \n"
        f"**Ingested:** {doc['ingested_at'][:10]}  \n"
        f"**Tags:** {', '.join(doc.get('tags') or []) or 'none'}  \n"
        f"**Chunks:** {doc['chunk_count']} | "
        f"**Words:** {doc['word_count']}  \n\n---\n\n"
    )
    return header + full_text


# ── Tool 3: list_recent ───────────────────────────────────────────────────────

@mcp.tool()
def list_recent(
    limit:     int            = 20,
    file_type: Optional[str]  = None,
    tag:       Optional[str]  = None,
) -> str:
    """
    List recently ingested documents.

    Args:
        limit:     Number of results (1–100, default 20)
        file_type: Filter by type: pdf|word|email|image|video|text
        tag:       Filter by tag

    Returns:
        Document list with IDs usable in get_document.
    """
    logger.event("mcp_list_recent", actor="mcp", limit=limit, file_type=file_type, tag=tag)

    limit = min(max(1, limit), 100)
    docs  = db.list_recent_documents(limit=limit, file_type=file_type, tag=tag)

    if not docs:
        return "No documents found with those filters."

    lines = ["## Recent Documents\n"]
    for doc in docs:
        tags = ", ".join(doc.get("tags") or []) or "—"
        lines.append(
            f"- **{doc['filename']}** ({doc['file_type']}) | "
            f"{doc['ingested_at'][:10]} | tags: {tags} | "
            f"id: `{doc['id']}`"
        )
    return "\n".join(lines)


# ── Tool 4: get_video_segment ─────────────────────────────────────────────────

@mcp.tool()
def get_video_segment(
    doc_id:    str,
    timestamp: float,
    window:    float = 60.0,
) -> str:
    """
    Get transcript near a specific timestamp in a video or audio file.

    Args:
        doc_id:    Document UUID of the video/audio
        timestamp: Seconds from the start of the recording
        window:    Context window in seconds (default 60)

    Returns:
        Transcript covering [timestamp − window/2, timestamp + window/2]
        with per-segment timestamps.
    """
    logger.event("mcp_video_seg", actor="mcp", doc_id=doc_id, ts=timestamp)

    doc = db.get_document(doc_id)
    if not doc:
        return f"Document not found: {doc_id}"
    if doc["file_type"] not in ("video", "audio"):
        return f"'{doc['filename']}' is not a video/audio file."

    chunks  = db.get_chunks_by_doc(doc_id)
    half_w  = window / 2
    t_start = max(0.0, timestamp - half_w)
    t_end   = timestamp + half_w

    matching = [
        c for c in chunks
        if c.get("timestamp_start") is not None
        and c["timestamp_start"] <= t_end
        and (c.get("timestamp_end") or 0.0) >= t_start
    ]

    if not matching:
        return (
            f"No transcript found near {int(timestamp)}s "
            f"in '{doc['filename']}'."
        )

    lines = [
        f"## {doc['filename']}\n"
        f"Around **{int(timestamp) // 60}:{int(timestamp) % 60:02d}** "
        f"(±{int(half_w)}s)\n"
    ]
    for chunk in matching:
        ts = int(chunk.get("timestamp_start") or 0)
        te = int(chunk.get("timestamp_end") or 0)
        lines.append(f"[{ts // 60}:{ts % 60:02d} → {te // 60}:{te % 60:02d}]")
        lines.append(chunk["content"])
        lines.append("")

    return "\n".join(lines)


# ── Bearer auth middleware ────────────────────────────────────────────────────

def create_authenticated_app():
    """
    Wrap the MCP SSE app with bearer token authentication.

    mcp.sse_app() returns the Starlette ASGI app for SSE transport.
    We mount it under a Starlette app with auth middleware applied.

    If mcp.sse_app() is not available in the installed version,
    try mcp.get_asgi_app() as a fallback.
    """
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN")
    if not BEARER_TOKEN:
        raise RuntimeError("MCP_BEARER_TOKEN not set in environment")

    # Obtain the SSE ASGI app from FastMCP
    # Try both known method names across mcp package versions
    if hasattr(mcp, "sse_app"):
        sse_app = mcp.sse_app()
    elif hasattr(mcp, "get_asgi_app"):
        sse_app = mcp.get_asgi_app()
    else:
        raise RuntimeError(
            "Cannot find SSE app method on FastMCP instance. "
            "Check mcp package version: pip show mcp"
        )

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in ("/health", "/"):
                return await call_next(request)

            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(
                    {"error": "Missing Authorization header"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = auth.removeprefix("Bearer ").strip()
            if token != BEARER_TOKEN:
                client_ip = (
                    request.client.host if request.client else "unknown"
                )
                logger.warning(
                    f"MCP | Rejected invalid token from {client_ip}"
                )
                return JSONResponse(
                    {"error": "Invalid token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    outer = Starlette(routes=[Mount("/", app=sse_app)])
    outer.add_middleware(BearerAuthMiddleware)
    return outer


if __name__ == "__main__":
    import uvicorn
    port = general_settings["app"]["mcp_port"]
    logger.info(f"MCP | Starting on 127.0.0.1:{port}")
    uvicorn.run(
        create_authenticated_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )