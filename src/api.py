"""
api.py — FastAPI web server: dashboard UI + REST API + scheduler.

Endpoints:
    GET  /              → dashboard HTML
    GET  /api/stats     → queue and corpus stats
    GET  /api/queue     → ingestion queue items
    GET  /api/documents → recent documents with tag/type filter
    GET  /api/search    → manual semantic search
    POST /api/ingest    → manually trigger ingestion of a specific file
    POST /api/scan      → manually trigger full Dropbox scan
    POST /api/backup    → manually trigger backup
    GET  /health        → health check (no auth)

All API endpoints require the same bearer token as the MCP server.
The dashboard HTML is served without auth (it fetches data via JS with the token).

Scheduler:
    APScheduler runs two background jobs:
        - scan_and_ingest() every 5 minutes
        - run_backup() nightly at 02:00 UTC
"""
import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src import database as db
from src.config_loader import general_settings
from src.embeddings import embed_query
from src.ingest import scan_and_ingest, ingest_file
from src.backup import run_backup
from src.logger import get_logger

logger    = get_logger()
CFG       = general_settings
BEARER    = os.getenv("MCP_BEARER_TOKEN", "")
security  = HTTPBearer()


# ── Auth dependency ───────────────────────────────────────────────────────────

def require_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not BEARER:
        raise HTTPException(500, "MCP_BEARER_TOKEN not configured")
    if creds.credentials != BEARER:
        raise HTTPException(401, "Invalid bearer token")
    return creds.credentials


# ── App factory ───────────────────────────────────────────────────────────────

def create_api() -> FastAPI:
    app = FastAPI(
        title="RAGBase",
        description="Personal knowledge base dashboard and API",
        version=CFG["app"]["version"],
        docs_url=None,   # disable Swagger in production
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # ── Scheduler ─────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")

    @app.on_event("startup")
    async def startup():
        db.setup_db()
        logger.info("API | Database ready")

        scheduler.add_job(
            _scheduled_scan,
            trigger=IntervalTrigger(seconds=CFG["ingestion"]["poll_interval_seconds"]),
            id="scan_ingest",
            replace_existing=True,
        )
        scheduler.add_job(
            _scheduled_backup,
            trigger=CronTrigger(
                hour=CFG["backup"]["schedule_hour"],
                minute=CFG["backup"]["schedule_minute"],
            ),
            id="nightly_backup",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("API | Scheduler started")

    @app.on_event("shutdown")
    async def shutdown():
        scheduler.shutdown(wait=False)
        logger.info("API | Scheduler stopped")

    # ── Dashboard ─────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        from src.dashboard import render_dashboard
        return HTMLResponse(render_dashboard())

    # ── Health (no auth) ──────────────────────────────────────────
    @app.get("/health")
    async def health():
        stats = await asyncio.to_thread(db.get_queue_stats)
        return {
            "status":          "ok",
            "total_documents": stats["total_documents"],
            "total_chunks":    stats["total_chunks"],
        }

    # ── Stats ─────────────────────────────────────────────────────
    @app.get("/api/stats")
    async def stats(_: str = Depends(require_token)):
        data = await asyncio.to_thread(db.get_queue_stats)
        return data

    # ── Queue ─────────────────────────────────────────────────────
    @app.get("/api/queue")
    async def queue(
        status: Optional[str] = None,
        limit:  int           = 50,
        _: str = Depends(require_token),
    ):
        items = await asyncio.to_thread(db.get_queue_items, status, limit)
        return {"items": items, "count": len(items)}

    # ── Documents ─────────────────────────────────────────────────
    @app.get("/api/documents")
    async def documents(
        limit:     int           = 20,
        file_type: Optional[str] = None,
        tag:       Optional[str] = None,
        _: str = Depends(require_token),
    ):
        docs = await asyncio.to_thread(
            db.list_recent_documents, limit, file_type, tag
        )
        return {"documents": docs, "count": len(docs)}

    # ── Search ────────────────────────────────────────────────────
    @app.get("/api/search")
    async def search(
        q:         str,
        top_k:     int           = 10,
        tag:       Optional[str] = None,
        file_type: Optional[str] = None,
        _: str = Depends(require_token),
    ):
        query_emb = await asyncio.to_thread(embed_query, q)
        results   = await db.async_vector_search(
            query_embedding=query_emb,
            top_k=min(top_k, 50),
            tag_filter=tag,
            file_type_filter=file_type,
        )
        return {"query": q, "results": results, "count": len(results)}

    # ── Manual ingest ─────────────────────────────────────────────
    @app.post("/api/ingest")
    async def manual_ingest(
        request: Request,
        _: str = Depends(require_token),
    ):
        body = await request.json()
        path = body.get("path")
        if not path:
            raise HTTPException(400, "path is required")
        result = await asyncio.to_thread(ingest_file, path)
        return result

    # ── Manual scan ───────────────────────────────────────────────
    @app.post("/api/scan")
    async def manual_scan(_: str = Depends(require_token)):
        result = await asyncio.to_thread(scan_and_ingest)
        return result

    # ── Manual backup ─────────────────────────────────────────────
    @app.post("/api/backup")
    async def manual_backup(_: str = Depends(require_token)):
        result = await asyncio.to_thread(run_backup)
        return result

    return app


# ── Scheduled task wrappers ───────────────────────────────────────────────────
# APScheduler calls these — they run in the event loop

async def _scheduled_scan():
    logger.info("SCHEDULER | Running scan_and_ingest")
    try:
        result = await asyncio.to_thread(scan_and_ingest)
        logger.event("scheduled_scan", actor="scheduler", **result)
    except Exception as e:
        logger.error(f"SCHEDULER | scan_and_ingest failed: {e}")


async def _scheduled_backup():
    logger.info("SCHEDULER | Running nightly backup")
    try:
        result = await asyncio.to_thread(run_backup)
        logger.event("scheduled_backup", actor="scheduler", **result)
    except Exception as e:
        logger.error(f"SCHEDULER | backup failed: {e}")