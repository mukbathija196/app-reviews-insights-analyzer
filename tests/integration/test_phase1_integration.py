"""Phase 1 integration-style tests using fake sources and real sqlite writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from pulse import cli
from pulse.ingestion.base import RawReview

runner = CliRunner()


def _raw(native_id: str, source: str, body: str, fetched_at: datetime) -> RawReview:
    return RawReview(
        native_id=native_id,
        product="groww",
        source=source,  # type: ignore[arg-type]
        rating=4,
        title="title",
        body=body,
        lang="en",
        country="in",
        posted_at=fetched_at - timedelta(minutes=1),
        app_version="1.0.0",
        fetched_at=fetched_at,
    )


def test_ingest_cli_populates_sqlite_and_rerun_near_noop(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)

    class FakeAppStoreSource:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def fetch(self, product: str, since: datetime, until: datetime):  # noqa: ANN201
            yield _raw("a1", "app_store", "Great app for investing every day", now)

    class FakePlayStoreSource:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def fetch(self, product: str, since: datetime, until: datetime):  # noqa: ANN201
            yield _raw("p1", "play_store", "Helpful app for tracking goals", now)

    monkeypatch.setattr(cli, "AppStoreSource", FakeAppStoreSource)
    monkeypatch.setattr(cli, "PlayStoreSource", FakePlayStoreSource)
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))

    first = runner.invoke(cli.app, ["ingest", "--product", "groww", "--weeks", "12"])
    assert first.exit_code == 0
    p1 = json.loads(first.output)
    assert p1["total_cached_reviews"] == 2

    second = runner.invoke(cli.app, ["ingest", "--product", "groww", "--weeks", "12"])
    assert second.exit_code == 0
    p2 = json.loads(second.output)
    # second run sees fetched rows, but no new inserts
    assert p2["counts"]["app_store"]["inserted_reviews"] == 0
    assert p2["counts"]["play_store"]["inserted_reviews"] == 0
    assert p2["total_cached_reviews"] == 2
