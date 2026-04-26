"""Phase 0 unit tests — config loading, RunSpec determinism, CLI smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pulse.cli import app
from pulse.config import load_products, load_pulse_config
from pulse.run import RunSpec, _derive_run_id

# ── Helpers ───────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ── EC0 / EVAL0: Config loading ───────────────────────────────────────────────

class TestConfigLoadsProducts:
    def test_groww_is_present(self) -> None:
        registry = load_products()
        assert "groww" in registry.ids()

    def test_groww_has_required_fields(self) -> None:
        registry = load_products()
        groww = registry.get("groww")
        assert groww.display_name == "Groww"
        assert groww.app_store.app_id == "1404871703"
        assert groww.play_store.package == "com.nextbillion.groww"
        assert groww.doc_title == "Weekly Review Pulse — Groww"

    def test_unknown_product_raises_key_error(self) -> None:
        registry = load_products()
        with pytest.raises(KeyError, match="Unknown product"):
            registry.get("nonexistent_product")

    def test_pulse_config_loads(self) -> None:
        cfg = load_pulse_config()
        assert cfg.run.window_weeks == 12
        assert cfg.run.email_mode == "draft"
        assert cfg.run.top_k_themes == 3

    def test_pulse_config_models(self) -> None:
        cfg = load_pulse_config()
        assert "MiniLM" in cfg.models.embedding or "sentence-transformers" in cfg.models.embedding


# ── EVAL0: RunSpec determinism ─────────────────────────────────────────────────

class TestRunSpecDeterministic:
    def test_same_inputs_produce_same_run_id(self) -> None:
        id_a = _derive_run_id("groww", "2026-W16")
        id_b = _derive_run_id("groww", "2026-W16")
        assert id_a == id_b

    def test_different_product_produces_different_run_id(self) -> None:
        id_groww = _derive_run_id("groww", "2026-W16")
        id_other = _derive_run_id("indmoney", "2026-W16")
        assert id_groww != id_other

    def test_different_week_produces_different_run_id(self) -> None:
        id_w16 = _derive_run_id("groww", "2026-W16")
        id_w17 = _derive_run_id("groww", "2026-W17")
        assert id_w16 != id_w17

    def test_runspec_auto_derives_run_id(self) -> None:
        spec = RunSpec(product="groww", iso_week="2026-W16")
        expected = _derive_run_id("groww", "2026-W16")
        assert spec.run_id == expected

    def test_two_runspecs_with_same_inputs_share_run_id(self) -> None:
        spec_a = RunSpec(product="groww", iso_week="2026-W16")
        spec_b = RunSpec(product="groww", iso_week="2026-W16")
        assert spec_a.run_id == spec_b.run_id

    def test_runspec_is_frozen(self) -> None:
        spec = RunSpec(product="groww", iso_week="2026-W16")
        with pytest.raises((AttributeError, TypeError)):
            spec.product = "other"  # type: ignore[misc]


# ── EVAL0: ISO week validation ────────────────────────────────────────────────

class TestIsoWeekValidation:
    @pytest.mark.parametrize("valid", ["2026-W01", "2026-W16", "2026-W53", "2025-W52"])
    def test_valid_iso_weeks_accepted(self, valid: str) -> None:
        spec = RunSpec(product="groww", iso_week=valid)
        assert spec.iso_week == valid

    @pytest.mark.parametrize("invalid", ["2026-W60", "2026-16", "26-W16", "2026W16", ""])
    def test_invalid_iso_weeks_rejected(self, invalid: str) -> None:
        with pytest.raises(ValueError, match="Invalid ISO week"):
            RunSpec(product="groww", iso_week=invalid)


# ── EVAL0: CLI smoke tests ─────────────────────────────────────────────────────

runner = CliRunner()


class TestCliHelp:
    def test_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_run_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "--dry-run" in result.output

    def test_backfill_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["backfill", "--help"])
        assert result.exit_code == 0

    def test_status_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0


_ARGS_DRY = ["run", "--product", "groww", "--iso-week", "2026-W16", "--dry-run"]


class TestCliRunDryRun:
    def test_dry_run_prints_run_spec_json(self) -> None:
        result = runner.invoke(app, _ARGS_DRY)
        assert result.exit_code == 0
        data = json.loads(result.output.strip().split("\n\n")[0])
        assert data["product"] == "groww"
        assert data["iso_week"] == "2026-W16"
        assert data["dry_run"] is True
        assert len(data["run_id"]) == 40

    def test_dry_run_run_id_is_deterministic(self) -> None:
        r1 = runner.invoke(app, _ARGS_DRY)
        r2 = runner.invoke(app, _ARGS_DRY)
        d1 = json.loads(r1.output.strip().split("\n\n")[0])
        d2 = json.loads(r2.output.strip().split("\n\n")[0])
        assert d1["run_id"] == d2["run_id"]

    def test_unknown_product_exits_nonzero(self) -> None:
        result = runner.invoke(
            app, ["run", "--product", "nonexistent", "--iso-week", "2026-W16", "--dry-run"]
        )
        assert result.exit_code != 0

    def test_invalid_iso_week_exits_nonzero(self) -> None:
        result = runner.invoke(
            app, ["run", "--product", "groww", "--iso-week", "2026-W99", "--dry-run"]
        )
        assert result.exit_code != 0


class TestCliBackfill:
    def test_backfill_expands_range(self) -> None:
        result = runner.invoke(
            app,
            ["backfill", "--product", "groww", "--from", "2026-W10", "--to", "2026-W12",
             "--dry-run"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output.strip().split("\n\n")[0])
        assert isinstance(data, list)
        assert len(data) == 3
        assert data[0]["iso_week"] == "2026-W10"
        assert data[2]["iso_week"] == "2026-W12"

    def test_backfill_year_boundary(self) -> None:
        result = runner.invoke(
            app,
            ["backfill", "--product", "groww", "--from", "2025-W51", "--to", "2026-W02",
             "--dry-run"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output.strip().split("\n\n")[0])
        assert isinstance(data, list)
        assert len(data) == 4  # W51, W52, W01, W02 (2025 has 52 weeks)
