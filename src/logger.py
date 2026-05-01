"""
logger.py — Structured rotating file + console logger with audit trail.

Two distinct logging systems:

1. Application logger (StructuredLogger)
   - Rotating file + console output
   - Mixed INFO/DEBUG/WARNING/ERROR from all modules
   - For developers and operators debugging the system
   - Gets rotated, compressed, and eventually deleted

2. Audit logger (write_audit / via database.audit())
   - Append-only records in the audit_log database table
   - Every MCP tool call, ingestion event, scan, backup
   - For the client's security audit trail
   - Never rotated or deleted — permanent record
   - Queryable: SELECT * FROM audit_log WHERE event_type='mcp_call'

Singleton fix:
    The original get_logger() ignored name/level/log_file arguments
    after the first call, silently returning a misconfigured logger.
    Fixed: the singleton is keyed by name so different named loggers
    (e.g. 'ragbase.audit' vs 'ragbase') are independent instances.
    The root 'ragbase' logger is still a singleton — calling
    get_logger() multiple times with no args always returns the same
    instance, which is the correct and expected behaviour.
"""
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


class StructuredLogger:
    """
    Rotating file + console logger with structured event emission.

    event() writes a structured line to the application log AND
    a record to the database audit_log via db.audit(). This means:
        - Application log: human-readable, rotated, for ops
        - Database audit: machine-queryable, permanent, for audit
    """

    def __init__(
        self,
        name:         str = "ragbase",
        level:        str = "INFO",
        log_file:     Optional[str] = None,
        max_bytes:    int = 10_485_760,   # 10MB
        backup_count: int = 5,
    ):
        self.name   = name
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        self.logger.handlers = []   # clear any handlers from a previous call
        self.logger.propagate = False  # don't double-log via root logger

        fmt = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(getattr(logging, level.upper(), logging.INFO))
        console.setFormatter(fmt)
        self.logger.addHandler(console)

        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setLevel(getattr(logging, level.upper(), logging.INFO))
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)

    def debug(self,    msg: str, **kw): self.logger.debug(msg,    **kw)
    def info(self,     msg: str, **kw): self.logger.info(msg,     **kw)
    def warning(self,  msg: str, **kw): self.logger.warning(msg,  **kw)
    def error(self,    msg: str, **kw): self.logger.error(msg,    **kw)
    def critical(self, msg: str, **kw): self.logger.critical(msg, **kw)

    def event(
        self,
        name:  str,
        actor: str = "system",
        **details,
    ) -> None:
        """
        Emit a structured event to both the application log and the
        database audit trail.

        Args:
            name:    Event name, e.g. 'ingest_complete', 'mcp_search'
            actor:   Who triggered it: 'mcp' | 'api' | 'scheduler' | 'cli'
            **details: Arbitrary key-value context (file, doc_id, chunks, etc.)

        The application log line is human-readable for operators.
        The database record is machine-queryable for audit.

        FIX: The original event() only wrote to the application log as a
        flat string — not queryable, rotated away, mixed with all other logs.
        Now writes a structured record to audit_log via db.audit().
        Import is deferred to avoid circular import (db imports logger).
        """
        # Application log — human-readable
        parts = " | ".join(f"{k}={v}" for k, v in details.items())
        self.logger.info(f"EVENT:{name} | actor={actor} | {parts}")

        # Database audit trail — permanent, queryable
        # Deferred import: database imports logger, so we can't import
        # database at module level here without a circular dependency.
        try:
            from src import database as db
            db.audit(event_type=name, actor=actor, **details)
        except Exception as e:
            # Never let audit failure crash the operation being logged.
            # Write to application log only so the failure is visible.
            self.logger.error(
                f"AUDIT | Failed to write audit record for event={name}: {e}"
            )


# ── Singleton registry ────────────────────────────────────────────────────────
# FIX: Original used a single _logger variable, so get_logger('ragbase.audit')
# would silently return the root 'ragbase' logger if called after the first
# get_logger() call. Now keyed by name — each named logger is independent.
# The common case (get_logger() with no args) still returns the same singleton.

_loggers: dict[str, StructuredLogger] = {}


def get_logger(
    name:         str = "ragbase",
    level:        str = "INFO",
    log_file:     Optional[str] = None,
    max_bytes:    int = 10_485_760,
    backup_count: int = 5,
) -> StructuredLogger:
    """
    Return a StructuredLogger singleton keyed by name.

    First call for a given name creates and configures the logger.
    Subsequent calls with the same name return the existing instance —
    level/log_file/etc. arguments are ignored after first creation.

    This is intentional: logger configuration happens once at startup
    (in main.py) and all other modules call get_logger() with no args
    to get the pre-configured root logger.

    To reconfigure a logger (e.g. in tests), delete it from _loggers first.
    """
    global _loggers
    if name not in _loggers:
        _loggers[name] = StructuredLogger(
            name=name,
            level=level,
            log_file=log_file,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
    return _loggers[name]


def reset_loggers() -> None:
    """
    Clear all logger singletons. For use in tests only.
    Calling this in production will cause loggers to be recreated
    without their file handlers on the next get_logger() call.
    """
    global _loggers
    _loggers.clear()