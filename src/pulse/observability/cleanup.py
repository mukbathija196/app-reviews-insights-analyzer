"""Artifact cleanup helpers for retention-based pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path


def cleanup_artifacts(artifacts_root: Path, retention_days: int = 90) -> dict[str, object]:
    """Delete artifact directories older than retention_days by mtime."""
    artifacts_root.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted: list[str] = []
    kept = 0
    for entry in artifacts_root.iterdir():
        if not entry.is_dir():
            continue
        modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
        if modified < cutoff:
            for child in entry.rglob("*"):
                if child.is_file():
                    child.unlink(missing_ok=True)
            for child_dir in sorted([d for d in entry.rglob("*") if d.is_dir()], reverse=True):
                child_dir.rmdir()
            entry.rmdir()
            deleted.append(entry.name)
        else:
            kept += 1
    return {
        "retention_days": retention_days,
        "deleted": deleted,
        "kept": kept,
    }


__all__ = ["cleanup_artifacts"]
