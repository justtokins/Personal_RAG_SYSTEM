"""
main.py — RAGBase application entry point.

Starts two servers:
    API server  (port 8000) — dashboard + REST API + cron scheduler
    MCP server  (port 8001) — Claude Desktop MCP tools

Both servers run as separate systemd services in production.
This entry point is used for development only:
    python main.py api    → start API server
    python main.py mcp    → start MCP server
    python main.py both   → start both (dev only, not for production)
"""
import asyncio
import sys

from dotenv import load_dotenv
load_dotenv()

from src.config_loader import general_settings
from src.logger import get_logger

logger = get_logger(
    level=general_settings["app"]["log_level"],
    log_file=general_settings["app"].get("log_file"),
)


def run_api():
    import uvicorn
    from src.api import create_api

    app  = create_api()
    port = general_settings["app"]["api_port"]
    host = general_settings["app"]["host"]

    logger.info(f"API | Starting on {host}:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )


def run_mcp():
    import uvicorn
    from src.mcp_server import create_authenticated_app

    app  = create_authenticated_app()
    port = general_settings["app"]["mcp_port"]

    logger.info(f"MCP | Starting on 127.0.0.1:{port}")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def run_both():
    """Development only — run both servers in the same process."""
    import threading

    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    run_mcp()


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "api"

    if command == "api":
        run_api()
    elif command == "mcp":
        run_mcp()
    elif command == "both":
        run_both()
    else:
        print(f"Unknown command: {command}")
        print("Usage: python main.py [api|mcp|both]")
        sys.exit(1)
