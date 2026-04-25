"""
logger.py — Structured rotating file + console logger.
Adapted from the RAGBase production base.
"""
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


class StructuredLogger:
    def __init__(
        self,
        name: str = "ragbase",
        level: str = "INFO",
        log_file: Optional[str] = None,
        max_bytes: int = 10_485_760,
        backup_count: int = 5,
    ):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level.upper()))
        self.logger.handlers = []

        fmt = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(getattr(logging, level.upper()))
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
            fh.setLevel(getattr(logging, level.upper()))
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)

    def debug(self, msg, **kw):    self.logger.debug(msg, **kw)
    def info(self, msg, **kw):     self.logger.info(msg, **kw)
    def warning(self, msg, **kw):  self.logger.warning(msg, **kw)
    def error(self, msg, **kw):    self.logger.error(msg, **kw)
    def critical(self, msg, **kw): self.logger.critical(msg, **kw)

    def event(self, name: str, **details):
        parts = " | ".join(f"{k}={v}" for k, v in details.items())
        self.logger.info(f"EVENT:{name} | {parts}")


_logger: Optional[StructuredLogger] = None


def get_logger(
    name: str = "ragbase",
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> StructuredLogger:
    global _logger
    if _logger is None:
        _logger = StructuredLogger(name=name, level=level, log_file=log_file)
    return _logger