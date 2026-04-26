"""Phase 6 unit tests — pipeline run records and status surfacing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pulse.run import Pipeline, build_run_spec, load_recent_run_records


def test_run_record_shape(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    spec = build_run_spec(product="groww", iso_week="2026-W16", dry_run=True)
    result = asyncio.run(Pipeline(spec).execute())

    assert result.run_id == spec.run_id
    assert result.product == "groww"
    assert result.iso_week == "2026-W16"
    assert isinstance(result.timings_ms, dict)
    assert isinstance(result.report, dict)

    run_file = tmp_path / "data" / "runs" / f"{spec.run_id}.json"
    assert run_file.exists()
    payload = json.loads(run_file.read_text(encoding="utf-8"))
    for key in (
        "run_id",
        "product",
        "iso_week",
        "status",
        "dry_run",
        "report",
        "delivery",
        "timings_ms",
        "warnings",
        "started_at",
        "finished_at",
    ):
        assert key in payload


def test_force_rebuilds_run_id() -> None:
    a = build_run_spec(product="groww", iso_week="2026-W16", dry_run=True, force=False)
    b = build_run_spec(product="groww", iso_week="2026-W16", dry_run=True, force=True)
    assert a.run_id != b.run_id


def test_status_loads_recent_records(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    s1 = build_run_spec(product="groww", iso_week="2026-W15", dry_run=True)
    s2 = build_run_spec(product="groww", iso_week="2026-W16", dry_run=True)
    asyncio.run(Pipeline(s1).execute())
    asyncio.run(Pipeline(s2).execute())
    records = load_recent_run_records("groww", last=2)
    assert len(records) == 2
    assert all(r["product"] == "groww" for r in records)
