"""Structured JSON-line logging. Implemented in Phase 7."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON. Always includes run_id, stage, status."""

    RESERVED = {"run_id", "stage", "status", "duration_ms", "tokens"}

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": "",
            "stage": "",
            "status": "",
        }
        for key in self.RESERVED:
            if hasattr(record, key):
                base[key] = getattr(record, key)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Set up root logger with JSON formatter to stdout."""
    effective_level = level or os.environ.get("LOG_LEVEL", "INFO")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(effective_level.upper())


def get_run_logger(run_id: str, data_dir: Path) -> logging.Logger:
    """Return a per-run JSON logger writing to data/runs/{run_id}/logs.jsonl."""
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logger_name = f"pulse.run.{run_id}"
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    file_handler = logging.FileHandler(run_dir / "logs.jsonl", encoding="utf-8")
    file_handler.setFormatter(_JsonFormatter())
    logger.addHandler(file_handler)
    return logger


__all__ = ["configure_logging", "get_run_logger"]
