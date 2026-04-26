"""Phase 7 unit tests — observability artifacts, diff, and doctor."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from typer.testing import CliRunner

from pulse.cli import app
from pulse.observability.cleanup import cleanup_artifacts
from pulse.observability.metrics import MetricsCollector, read_metrics
from pulse.run import Pipeline, build_run_spec

runner = CliRunner()


def test_metrics_collector_writes_jsonl(tmp_path: Path) -> None:
    metrics = MetricsCollector("run-1", tmp_path / "data")
    metrics.counter("stage_invocations", 1, stage="ingest", status="ok")
    metrics.histogram("stage_duration_ms", 123.0, stage="ingest")
    metrics.gauge("themes_after_validate", 3.0, stage="reason")
    path = tmp_path / "data" / "runs" / "run-1" / "metrics.jsonl"
    assert path.exists()
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 3
    assert {line["type"] for line in lines} == {"counter", "histogram", "gauge"}


def test_dry_run_writes_required_artifacts(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    spec = build_run_spec(product="groww", iso_week="2026-W17", dry_run=True)
    asyncio.run(Pipeline(spec).execute())
    base = tmp_path / "data" / "artifacts" / spec.run_id
    assert (base / "docops.json").exists()
    assert (base / "email.html").exists()
    assert (base / "email.txt").exists()
    assert (base / "clusters.json").exists()
    assert (base / "themes.json").exists()


def test_diff_command_reports_theme_delta(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    runs_dir = tmp_path / "data" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    a = {
        "run_id": "a",
        "report": {"themes": [{"theme_name": "Fees"}, {"theme_name": "Crashes"}]},
    }
    b = {
        "run_id": "b",
        "report": {"themes": [{"theme_name": "Fees"}, {"theme_name": "Onboarding"}]},
    }
    (runs_dir / "a.json").write_text(json.dumps(a), encoding="utf-8")
    (runs_dir / "b.json").write_text(json.dumps(b), encoding="utf-8")
    result = runner.invoke(app, ["diff", "--from", "a", "--to", "b"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["added_themes"] == ["Onboarding"]
    assert out["removed_themes"] == ["Crashes"]


def test_log_lines_are_json_with_required_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    spec = build_run_spec(product="groww", iso_week="2026-W17", dry_run=True)
    asyncio.run(Pipeline(spec).execute())
    log_path = tmp_path / "data" / "runs" / spec.run_id / "logs.jsonl"
    assert log_path.exists()
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "expected at least one log line"
    for line in lines:
        payload = json.loads(line)
        assert "run_id" in payload
        assert "stage" in payload
        assert "status" in payload


def test_log_lines_do_not_leak_groq_key(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake_secret = "gsk_PHASE7_FAKE_SECRET_1234567890ABC"
    monkeypatch.setenv("GROQ_API_KEY", fake_secret)
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    spec = build_run_spec(product="groww", iso_week="2026-W17", dry_run=True)
    asyncio.run(Pipeline(spec).execute())
    log_path = tmp_path / "data" / "runs" / spec.run_id / "logs.jsonl"
    body = log_path.read_text(encoding="utf-8")
    assert fake_secret not in body


def test_metrics_counters_increment_for_stages(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    spec = build_run_spec(product="groww", iso_week="2026-W17", dry_run=True)
    asyncio.run(Pipeline(spec).execute())
    metrics_path = tmp_path / "data" / "runs" / spec.run_id / "metrics.jsonl"
    events = read_metrics(metrics_path)
    stage_counters = [
        x for x in events if x.get("type") == "counter" and x.get("name") == "stage_invocations"
    ]
    assert len(stage_counters) == 4  # ingest, reason, render, deliver


def test_read_metrics_ignores_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        '{"type":"counter","name":"a","value":1}\n{"type":"histogram"',
        encoding="utf-8",
    )
    events = read_metrics(path)
    assert len(events) == 1
    assert events[0]["name"] == "a"


def test_doctor_catches_checked_in_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "leak.py").write_text(
        'KEY = "gsk_ABCDEF1234567890ABCDEF"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0
    out = json.loads(result.stdout)
    assert out["ok"] is False
    assert out["secret_findings"]


def test_doctor_passes_on_clean_repo(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["ok"] is True


def test_cleanup_artifacts_removes_old_dirs(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    old_dir = root / "old-run"
    new_dir = root / "new-run"
    old_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)
    old_file = old_dir / "x.txt"
    new_file = new_dir / "x.txt"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")
    # make old older than 1 day
    old_ts = (Path(__file__).stat().st_mtime - 3 * 86400)
    os.utime(old_dir, (old_ts, old_ts))
    os.utime(old_file, (old_ts, old_ts))
    result = cleanup_artifacts(root, retention_days=1)
    assert "old-run" in result["deleted"]
    assert new_dir.exists()


def test_repeated_dry_run_warns_artifact_overwrite(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    spec = build_run_spec(product="groww", iso_week="2026-W17", dry_run=True, force=False)
    asyncio.run(Pipeline(spec).execute())
    second = asyncio.run(Pipeline(spec).execute())
    assert "artifacts_overwrite_existing_run_id" in second.warnings

