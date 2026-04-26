"""Phase 1 unit tests — normalization, sqlite storage, and ingest CLI."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from pulse import cli
from pulse.ingestion.app_store import AppStoreSource, _parse_reviews_from_serialized_server_data
from pulse.ingestion.base import RawReview
from pulse.ingestion.normalize import (
    content_hash,
    is_english_text,
    is_review_eligible,
    normalize,
    sanitize_text,
)
from pulse.ingestion.play_store import PlayStoreSource
from pulse.storage.sqlite import ReviewStore

runner = CliRunner()


def _raw_review(
    *,
    native_id: str = "r-1",
    source: str = "app_store",
    body: str = "Great app for long term investing goals",
    title: str | None = "Works well",
    rating: int = 5,
    posted_at: datetime | None = None,
) -> RawReview:
    ts = posted_at or datetime.now(UTC)
    return RawReview(
        native_id=native_id,
        product="groww",
        source=source,  # type: ignore[arg-type]
        rating=rating,
        title=title,
        body=body,
        lang="en",
        country="in",
        posted_at=ts,
        app_version="1.0.0",
        fetched_at=ts + timedelta(minutes=1),
    )


class TestNormalize:
    def test_normalize_review_id_stable(self) -> None:
        raw = _raw_review(native_id="stable")
        r1 = normalize(raw)
        r2 = normalize(raw)
        assert r1.review_id == r2.review_id

    def test_normalize_builds_stable_review_id_and_hash(self) -> None:
        raw = _raw_review()
        review = normalize(raw)
        assert review.review_id == "app_store:groww:r-1"
        assert len(review.content_hash) == 64

    def test_content_hash_ignores_whitespace_noise(self) -> None:
        hash_a = content_hash(" Hello   World ", " this    is \n a test ")
        hash_b = content_hash("Hello World", "this is a test")
        assert hash_a == hash_b

    def test_normalize_content_hash_changes_on_edit(self) -> None:
        hash_a = content_hash("Works", "Great for investing every day")
        hash_b = content_hash("Works", "Great for investing every single day")
        assert hash_a != hash_b

    def test_normalize_truncates_long_body(self) -> None:
        body = "a" * 3005
        review = normalize(_raw_review(body=body))
        assert review.truncated is True
        assert len(review.body) == 2000

    def test_normalize_clamps_rating_between_1_and_5(self) -> None:
        low = normalize(_raw_review(rating=-10))
        high = normalize(_raw_review(rating=10))
        assert low.rating == 1
        assert high.rating == 5

    def test_sanitize_text_removes_emojis(self) -> None:
        assert sanitize_text("Great app 😊🚀 for investing") == "Great app for investing"

    def test_review_eligibility_rejects_short_reviews(self) -> None:
        raw = _raw_review(body="Too short now", title=None)
        review = normalize(raw)
        eligible, reason = is_review_eligible(raw, review)
        assert eligible is False
        assert reason == "lt_5_words"

    def test_review_eligibility_rejects_non_english(self) -> None:
        raw = _raw_review(
            body="Esta aplicacion es excelente para invertir dinero",
            title=None,
        )
        review = normalize(raw)
        eligible, reason = is_review_eligible(raw, review)
        assert eligible is False
        assert reason in {"non_english_lang_hint", "non_english_detected"}

    def test_review_eligibility_accepts_english_with_emojis(self) -> None:
        raw = _raw_review(body="Great app 😊 works really well every day")
        review = normalize(raw)
        eligible, reason = is_review_eligible(raw, review)
        assert eligible is True
        assert reason is None
        assert "😊" not in review.body

    def test_is_english_text(self) -> None:
        assert is_english_text("Great app for long term investing goals")
        assert not is_english_text("Muy buena aplicacion para invertir dinero")

    def test_future_posted_at_is_clamped(self) -> None:
        future = datetime.now(UTC) + timedelta(days=10)
        review = normalize(_raw_review(posted_at=future))
        assert review.posted_at <= datetime.now(UTC)


class TestReviewStore:
    def test_migrate_and_upsert_reviews(self, tmp_path: Path) -> None:
        db = tmp_path / "pulse.sqlite"
        store = ReviewStore(db)
        store.migrate()

        raw = _raw_review(native_id="n1")
        review = normalize(raw)

        assert store.upsert_raw_review(raw) is True
        assert store.upsert_raw_review(raw) is False

        assert store.upsert_review(review) is True
        assert store.upsert_review(review) is False
        assert store.count_reviews("groww") == 1
        assert store.count_reviews("groww", "app_store") == 1

    def test_upsert_review_updates_on_newer_fetch(self, tmp_path: Path) -> None:
        db = tmp_path / "pulse.sqlite"
        store = ReviewStore(db)
        store.migrate()

        older = normalize(
            _raw_review(
                native_id="n2",
                body="Old body",
                posted_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        newer = normalize(
            _raw_review(
                native_id="n2",
                body="Updated body",
                posted_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        assert store.upsert_review(older) is True
        assert store.upsert_review(newer) is True
        assert store.count_reviews("groww") == 1

    def test_dedup_prefers_newer_content_hash(self, tmp_path: Path) -> None:
        db = tmp_path / "pulse.sqlite"
        store = ReviewStore(db)
        store.migrate()
        now = datetime.now(UTC)
        old_raw = RawReview(
            native_id="same",
            product="groww",
            source="play_store",
            rating=5,
            title="Old",
            body="Old body long enough for tests",
            lang="en",
            country="in",
            posted_at=now,
            app_version="1.0",
            fetched_at=now,
        )
        new_raw = RawReview(
            native_id="same",
            product="groww",
            source="play_store",
            rating=4,
            title="New",
            body="New body long enough for tests and changed",
            lang="en",
            country="in",
            posted_at=now,
            app_version="1.1",
            fetched_at=now + timedelta(minutes=5),
        )
        assert store.upsert_review(normalize(old_raw))
        assert store.upsert_review(normalize(new_raw))
        assert store.count_reviews("groww", "play_store") == 1

    def test_fetch_cursor_roundtrip(self, tmp_path: Path) -> None:
        db = tmp_path / "pulse.sqlite"
        store = ReviewStore(db)
        store.migrate()
        assert store.get_fetch_cursor("groww", "app_store") is None
        store.set_fetch_cursor("groww", "app_store", "cursor-1")
        assert store.get_fetch_cursor("groww", "app_store") == "cursor-1"


class TestIngestCli:
    def test_ingest_command_writes_counts(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        class FakeAppStoreSource:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                pass

            def fetch(self, product: str, since: datetime, until: datetime):  # noqa: ANN201
                yield _raw_review(
                    native_id="a1",
                    source="app_store",
                    body="Great app for long term investing goals",
                )
                yield _raw_review(
                    native_id="a2",
                    source="app_store",
                    body="Muy buena aplicacion para invertir dinero",
                )

        class FakePlayStoreSource:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                pass

            def fetch(self, product: str, since: datetime, until: datetime):  # noqa: ANN201
                yield _raw_review(
                    native_id="p1",
                    source="play_store",
                    body="Great app 😊 works really well every day",
                )
                yield _raw_review(
                    native_id="p2",
                    source="play_store",
                    body="Not good",
                )

        monkeypatch.setattr(cli, "AppStoreSource", FakeAppStoreSource)
        monkeypatch.setattr(cli, "PlayStoreSource", FakePlayStoreSource)
        monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))

        result = runner.invoke(cli.app, ["ingest", "--product", "groww", "--weeks", "12"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["product"] == "groww"
        assert payload["counts"]["app_store"]["fetched"] == 2
        assert payload["counts"]["play_store"]["fetched"] == 2
        assert payload["counts"]["app_store"]["filtered_out"] == 1
        assert payload["counts"]["play_store"]["filtered_out"] == 1
        assert payload["total_cached_reviews"] == 2

    def test_one_source_fails_other_proceeds(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        class FailingAppStoreSource:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                pass

            def fetch(self, product: str, since: datetime, until: datetime):  # noqa: ANN201
                raise RuntimeError("app-store unavailable")

        class OkPlayStoreSource:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                pass

            def fetch(self, product: str, since: datetime, until: datetime):  # noqa: ANN201
                yield _raw_review(
                    native_id="p1",
                    source="play_store",
                    body="Great app works very well every day",
                )

        monkeypatch.setattr(cli, "AppStoreSource", FailingAppStoreSource)
        monkeypatch.setattr(cli, "PlayStoreSource", OkPlayStoreSource)
        monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
        result = runner.invoke(cli.app, ["ingest", "--product", "groww", "--weeks", "12"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["counts"]["play_store"]["inserted_reviews"] == 1
        assert any("app_store_source_unavailable" in w for w in payload["warnings"])


class TestAppStoreFallbackParser:
    def test_parse_reviews_from_serialized_server_data(self) -> None:
        html = """
        <html><body>
        <script type="application/json" id="serialized-server-data">
        {"data":[{"$kind":"Review","id":"111","title":"Great","date":"2026-04-10T12:00:00.000Z",
        "contents":"Great app for long term investing goals","rating":5,"reviewerName":"A"},
        {"$kind":"Other","id":"x"},
        {"nested":{"$kind":"Review","id":"222","title":"Nice","date":"2026-04-09T12:00:00.000Z",
        "contents":"Useful app for investing and tracking portfolio well","rating":4}}]}
        </script>
        </body></html>
        """
        reviews = _parse_reviews_from_serialized_server_data(
            html,
            product="groww",
            country="in",
        )
        ids = {r.native_id for r in reviews}
        assert ids == {"111", "222"}
        assert all(r.source == "app_store" for r in reviews)


class TestAppStoreAdapter:
    def test_app_store_parser_happy_path(self) -> None:
        entry = {
            "im:rating": {"label": "5"},
            "content": {"label": "Great app for investing and learning"},
            "updated": {"label": "2026-04-10T12:00:00-07:00"},
            "title": {"label": "Great"},
            "id": {"label": "review-1"},
        }
        session = _FakeSession([{"feed": {"entry": [entry]}}])
        src = AppStoreSource(app_id="1404871703", session=session, max_pages=1)
        out = list(
            src.fetch(
                product="groww",
                since=datetime(2026, 1, 1, tzinfo=UTC),
                until=datetime(2026, 12, 31, tzinfo=UTC),
            )
        )
        assert len(out) == 1
        assert out[0].native_id == "review-1"
        assert out[0].rating == 5

    def test_app_store_pagination_stops_at_window(self) -> None:
        new_entry = {
            "im:rating": {"label": "5"},
            "content": {"label": "Great app for investing and learning"},
            "updated": {"label": "2026-04-10T12:00:00-07:00"},
            "title": {"label": "Great"},
            "id": {"label": "review-1"},
        }
        old_entry = {
            "im:rating": {"label": "4"},
            "content": {"label": "Old review should stop paging"},
            "updated": {"label": "2020-01-10T12:00:00-07:00"},
            "title": {"label": "Old"},
            "id": {"label": "review-2"},
        }
        session = _FakeSession(
            [
                {"feed": {"entry": [new_entry]}},
                {"feed": {"entry": [old_entry]}},
                {"feed": {"entry": [new_entry]}},
            ]
        )
        src = AppStoreSource(app_id="1404871703", session=session, max_pages=5)
        out = list(
            src.fetch(
                product="groww",
                since=datetime(2026, 1, 1, tzinfo=UTC),
                until=datetime(2026, 12, 31, tzinfo=UTC),
            )
        )
        assert len(out) == 1
        assert session.calls == 2


class TestPlayStoreAdapter:
    def test_play_store_adapter_sorts_newest(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        captured: dict[str, object] = {}

        class FakeSort:
            NEWEST = "NEWEST"

        def fake_reviews(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            captured["sort"] = kwargs.get("sort")
            return ([], None)

        fake_module = types.SimpleNamespace(Sort=FakeSort, reviews=fake_reviews)
        monkeypatch.setitem(sys.modules, "google_play_scraper", fake_module)
        src = PlayStoreSource(package_name="com.nextbillion.groww")
        out = list(
            src.fetch(
                product="groww",
                since=datetime(2026, 1, 1, tzinfo=UTC),
                until=datetime(2026, 12, 31, tzinfo=UTC),
            )
        )
        assert out == []
        assert captured["sort"] == FakeSort.NEWEST


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeSession:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, url: str, timeout: float):  # noqa: ANN001, ANN201
        _ = (url, timeout)
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return _FakeResponse(payload)
