"""
mcp_server.py — MCP server exposing 4 tools to Claude Desktop.

Tools:
    search_knowledge   — semantic search across entire corpus
    get_document       — retrieve full document text and metadata
    list_recent        — list recently ingested documents
    get_video_segment  — retrieve transcript at a specific timestamp

Transport: SSE (Server-Sent Events) over HTTPS via Caddy proxy.
Auth: Bearer token checked in middleware — invalid tokens get 401.

Claude Desktop config (~/.claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "ragbase": {
          "url": "https://your-domain.com/mcp/sse",
          "headers": {
            "Authorization": "Bearer YOUR_TOKEN"
          }
        }
      }
    }

Security model:
    - Caddy terminates TLS → forwards to localhost:8001
    - Bearer token validated on every request in middleware
    - No direct internet access to port 8001 (firewall blocks it)
    - DB is read-only from MCP perspective (search, get)
"""
import os
import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from src import database as db
from src.embeddings import embed_query
from src.config_loader import general_settings
from src.logger import get_logger

logger = get_logger()
CFG    = general_settings["search"]

# FastMCP creates the MCP server with all protocol handling
mcp = FastMCP(
    name="RAGBase",
    instructions=(
        "Personal knowledge base containing documents, emails, images, and videos. "
        "Use search_knowledge for semantic queries. "
        "Use get_document to retrieve full content. "
        "Use list_recent to browse recent additions. "
        "Use get_video_segment to find transcript at a specific timestamp."
    ),
)


# ── Tool 1: search_knowledge ──────────────────────────────────────────────────

@mcp.tool()
def search_knowledge(
    query:       str,
    top_k:       int  = 10,
    tag_filter:  Optional[str] = None,
    file_type:   Optional[str] = None,
) -> str:
    """
    Semantic search across the entire knowledge base.

    Args:
        query:      Natural language search query
        top_k:      Number of results (1-50, default 10)
        tag_filter: Filter by tag (e.g. 'research', 'finance')
        file_type:  Filter by type (pdf, word, email, image, video, text)

    Returns:
        Formatted search results with citations (filename, page, timestamp)
    """
    logger.event("mcp_search", query=query[:60], top_k=top_k, tag=tag_filter)

    top_k = min(max(1, top_k), CFG["max_top_k"])

    query_emb = embed_query(query)
    results   = db.vector_search(
        query_embedding=query_emb,
        top_k=top_k,
        tag_filter=tag_filter,
        file_type_filter=file_type,
    )

    if not results:
        return "No results found for that query."

    lines = [f"## Search Results for: '{query}'\n"]

    for i, r in enumerate(results, 1):
        citation = f"[{r['filename']}"
        if r.get("page_number"):
            citation += f", p.{r['page_number']}"
        if r.get("timestamp_start") is not None:
            ts = int(r["timestamp_start"])
            citation += f", {ts//60}:{ts%60:02d}"
        citation += f"] (score: {r['score']:.3f})"

        lines.append(f"### Result {i} — {citation}")
        lines.append(r["content"])
        lines.append("")

    return "\n".join(lines)


# ── Tool 2: get_document ──────────────────────────────────────────────────────

@mcp.tool()
def get_document(doc_id: str) -> str:
    """
    Retrieve full content and metadata for a specific document.

    Args:
        doc_id: Document UUID (from search_knowledge results)

    Returns:
        Full document text with metadata header
    """
    logger.event("mcp_get_doc", doc_id=doc_id)

    doc = db.get_document(doc_id)
    if not doc:
        return f"Document not found: {doc_id}"

    chunks = db.get_chunks_by_doc(doc_id)
    full_text = "\n\n".join(c["content"] for c in chunks)

    header = (
        f"# {doc.get('title') or doc['filename']}\n"
        f"**File:** {doc['filename']}\n"
        f"**Type:** {doc['file_type']}\n"
        f"**Ingested:** {doc['ingested_at'][:10]}\n"
        f"**Tags:** {', '.join(doc.get('tags') or []) or 'none'}\n"
        f"**Chunks:** {doc['chunk_count']} | **Words:** {doc['word_count']}\n\n"
        f"---\n\n"
    )

    return header + full_text


# ── Tool 3: list_recent ───────────────────────────────────────────────────────

@mcp.tool()
def list_recent(
    limit:     int  = 20,
    file_type: Optional[str] = None,
    tag:       Optional[str] = None,
) -> str:
    """
    List recently ingested documents.

    Args:
        limit:     Number of documents (default 20, max 100)
        file_type: Filter by type (pdf, word, email, image, video, text)
        tag:       Filter by tag

    Returns:
        Formatted list with document IDs for use with get_document
    """
    logger.event("mcp_list_recent", limit=limit, file_type=file_type, tag=tag)

    limit = min(max(1, limit), 100)
    docs  = db.list_recent_documents(limit=limit, file_type=file_type, tag=tag)

    if not docs:
        return "No documents found with those filters."

    lines = ["## Recent Documents\n"]
    for doc in docs:
        tags = ", ".join(doc.get("tags") or []) or "—"
        lines.append(
            f"- **{doc['filename']}** ({doc['file_type']}) | "
            f"Ingested: {doc['ingested_at'][:10]} | "
            f"Tags: {tags} | "
            f"ID: `{doc['id']}`"
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
    Get transcript segment at a specific timestamp in a video/audio file.

    Args:
        doc_id:    Document UUID of the video/audio file
        timestamp: Time in seconds from the start
        window:    Context window in seconds (default 60)

    Returns:
        Transcript text covering [timestamp-window/2, timestamp+window/2]
        with exact timestamps for each segment.
    """
    logger.event("mcp_video_segment", doc_id=doc_id, ts=timestamp)

    doc = db.get_document(doc_id)
    if not doc:
        return f"Document not found: {doc_id}"

    if doc["file_type"] not in ("video", "audio"):
        return f"Document '{doc['filename']}' is not a video/audio file."

    chunks  = db.get_chunks_by_doc(doc_id)
    half_w  = window / 2
    t_start = max(0, timestamp - half_w)
    t_end   = timestamp + half_w

    matching = [
        c for c in chunks
        if c.get("timestamp_start") is not None
        and c["timestamp_start"] <= t_end
        and (c.get("timestamp_end") or 0) >= t_start
    ]

    if not matching:
        return (
            f"No transcript found near {timestamp:.0f}s "
            f"in '{doc['filename']}'. "
            f"Video duration: check with get_document."
        )

    lines = [
        f"## Transcript: {doc['filename']}\n"
        f"**Around {int(timestamp//60)}:{int(timestamp%60):02d}** "
        f"(±{int(window//2)}s)\n"
    ]

    for chunk in matching:
        ts = int(chunk.get("timestamp_start") or 0)
        te = int(chunk.get("timestamp_end") or 0)
        lines.append(f"[{ts//60}:{ts%60:02d} → {te//60}:{te%60:02d}]")
        lines.append(chunk["content"])
        lines.append("")

    return "\n".join(lines)


# ── Bearer token middleware ───────────────────────────────────────────────────

def create_authenticated_app():
    """
    Wrap the MCP SSE app with bearer token authentication.
    Returns a Starlette ASGI app with auth middleware applied.
    """
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN")
    if not BEARER_TOKEN:
        raise RuntimeError("MCP_BEARER_TOKEN not set in environment")

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Health check doesn't require auth
            if request.url.path == "/health":
                return await call_next(request)

            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(
                    {"error": "Missing Bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = auth.removeprefix("Bearer ").strip()
            if token != BEARER_TOKEN:
                logger.warning(
                    f"MCP | Rejected invalid token from "
                    f"{request.client.host if request.client else 'unknown'}"
                )
                return JSONResponse(
                    {"error": "Invalid token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )

            return await call_next(request)

    # Get the SSE ASGI app from FastMCP
    sse_app = mcp.get_asgi_app()

    app = Starlette(
        routes=[Mount("/", app=sse_app)],
    )
    app.add_middleware(BearerAuthMiddleware)
    return app


if __name__ == "__main__":
    import uvicorn

    port = general_settings["app"]["mcp_port"]
    logger.info(f"MCP | Starting on localhost:{port}")
    app  = create_authenticated_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")