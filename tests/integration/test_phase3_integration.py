"""Phase 3 integration tests — reason CLI end-to-end with a fake provider."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from pulse import cli
from pulse.ingestion.base import Review
from pulse.reasoning.cluster import cluster_reviews
from pulse.reasoning.embed import embed_reviews
from pulse.storage.sqlite import ReviewStore

runner = CliRunner()
NOW = datetime(2026, 4, 22, tzinfo=UTC)


class DeterministicEmbedder:
    """Text-length + topic-hash embedder for stable clustering across runs."""

    model_name = "deterministic-test-model"

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            topic_score = 1.0 if "crash" in t.lower() else 0.0
            length = float(len(t)) / 100.0
            out.append([topic_score, 1.0 - topic_score, length, 0.1, 0.1])
        return out


def _review(idx: int, body: str, rating: int, days_ago: int, source: str = "play_store") -> Review:
    return Review(
        review_id=f"{source}:groww:{idx}",
        product="groww",
        source=source,  # type: ignore[arg-type]
        rating=rating,
        title=None,
        body=body,
        lang="en",
        country="in",
        posted_at=NOW - timedelta(days=days_ago),
        app_version="1.0.0",
        fetched_at=NOW,
        content_hash=f"h{idx}",
    )


def _seed_reviews(store: ReviewStore) -> list[Review]:
    store.migrate()
    crash_bodies = [
        "The app keeps crashing when I open it",
        "App crashes on startup every time",
        "It crashes after I log in",
        "Crashes immediately on launch",
        "The app crashes when I try to buy stocks",
        "It crashes when I open the portfolio tab",
        "App crashes every single time",
        "Keeps crashing on this phone",
        "App crash whenever I refresh",
        "Crashes after the latest update",
    ]
    slow_bodies = [
        "The app is extremely slow to load",
        "Performance is very slow these days",
        "Loading spinner never ends, so slow",
        "App takes forever to show my portfolio",
        "Slow even on wifi",
        "Slow to process orders",
        "Really slow user interface",
        "Slow especially during market hours",
        "Response time is slow",
        "Slow updates on prices",
    ]

    reviews: list[Review] = []
    for i, b in enumerate(crash_bodies):
        reviews.append(_review(i + 1, b, rating=1, days_ago=2))
    for i, b in enumerate(slow_bodies):
        reviews.append(_review(100 + i, b, rating=2, days_ago=5))

    for r in reviews:
        store.upsert_review(r)
    return reviews


def test_seeded_cluster_determinism(tmp_path: Path) -> None:
    # EC-3.10 — same seed + same embeddings → same cluster labels & ordering.
    store = ReviewStore(tmp_path / "pulse.sqlite")
    reviews = _seed_reviews(store)
    embedder = DeterministicEmbedder()
    emb1 = embed_reviews(reviews, embedder=embedder, store=store)
    emb2 = embed_reviews(reviews, embedder=embedder, store=store)
    assert len(emb1) == len(emb2)
    for a, b in zip(emb1, emb2, strict=True):
        assert len(a) == len(b)
        for x, y in zip(a, b, strict=True):
            assert abs(x - y) < 1e-5

    c1 = cluster_reviews(reviews, emb1, top_k=3, random_state=42)
    c2 = cluster_reviews(reviews, emb2, top_k=3, random_state=42)
    assert [c.cluster_id for c in c1] == [c.cluster_id for c in c2]
    assert [c.size for c in c1] == [c.size for c in c2]
    # Severity is deterministic for a fixed now_utc; small float diff tolerated.
    for a, b in zip(c1, c2, strict=True):
        assert abs(a.severity - b.severity) < 1e-4


def test_reason_cli_low_signal_on_sparse_data(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # EC-3.1 — below min_reviews threshold → low_signal report, no LLM call.
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path / "data"))
    store = ReviewStore(tmp_path / "data" / "pulse.sqlite")
    store.migrate()
    for i in range(3):
        store.upsert_review(_review(i, "too few reviews here", rating=2, days_ago=1))

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    result = runner.invoke(
        cli.app,
        [
            "reason",
            "--product",
            "groww",
            "--iso-week",
            "2026-W16",
            "--weeks",
            "12",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["low_signal"] is True
    assert payload["reason"] == "insufficient_reviews"
    assert payload["themes"] == []
