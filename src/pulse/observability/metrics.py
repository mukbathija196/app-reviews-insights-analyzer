"""Simple counter/histogram/gauge metrics writer. Implemented in Phase 7."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class MetricsCollector:
    """Writes JSONL metric events to data/runs/{run_id}/metrics.jsonl."""

    def __init__(self, run_id: str, data_dir: Path) -> None:
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self._path = run_dir / "metrics.jsonl"

    def counter(self, name: str, value: int = 1, **labels: str) -> None:
        self._write(metric_type="counter", name=name, value=value, labels=labels)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self._write(metric_type="gauge", name=name, value=value, labels=labels)

    def histogram(self, name: str, value: float, **labels: str) -> None:
        self._write(metric_type="histogram", name=name, value=value, labels=labels)

    def _write(
        self,
        *,
        metric_type: str,
        name: str,
        value: float | int,
        labels: dict[str, str],
    ) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "type": metric_type,
            "name": name,
            "value": value,
            "labels": labels,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_metrics(path: Path) -> list[dict[str, object]]:
    """Read metrics JSONL, ignoring corrupted/partial lines."""
    if not path.exists():
        return []
    out: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append({str(k): v for k, v in payload.items()})
    return out


__all__ = ["MetricsCollector", "read_metrics"]
