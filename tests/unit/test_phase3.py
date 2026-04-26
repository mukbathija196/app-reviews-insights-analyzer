"""Phase 3 unit tests — embeddings cache, clustering/severity, theme provider, validator, report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pulse.ingestion.base import Review
from pulse.reasoning.cluster import Cluster, cluster_reviews, rank_clusters
from pulse.reasoning.embed import embed_reviews
from pulse.reasoning.theme import (
    ActionIdea,
    AudienceHelp,
    MissingCredentialsError,
    ProviderError,
    Quote,
    Theme,
    ThemeGenerationStats,
    get_provider,
    name_themes,
)
from pulse.reasoning.validate import is_quote_valid, validate_themes
from pulse.rendering.report import MIN_THEMES_FOR_SIGNAL, build_report
from pulse.run import RunSpec
from pulse.storage.sqlite import ReviewStore

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


def _theme(
    *,
    theme_name: str = "t",
    one_liner: str = "ol",
    quotes: list[Quote] | None = None,
    action_ideas: list[ActionIdea] | None = None,
    who_this_helps: list[AudienceHelp] | None = None,
    cluster_id: int = 0,
    n_reviews: int = 1,
    leadership_summary: str = "",
    severity: str = "medium",
    confidence: str = "medium",
) -> Theme:
    return Theme(
        theme_name=theme_name,
        one_liner=one_liner,
        leadership_summary=leadership_summary,
        severity=severity,
        confidence=confidence,
        quotes=quotes or [],
        action_ideas=action_ideas or [],
        who_this_helps=who_this_helps or [],
        cluster_id=cluster_id,
        n_reviews=n_reviews,
    )


def _review(
    review_id: str,
    *,
    rating: int = 3,
    body: str = "sample review body",
    title: str | None = None,
    posted_at: datetime | None = None,
    source: str = "play_store",
    product: str = "groww",
) -> Review:
    return Review(
        review_id=review_id,
        product=product,
        source=source,  # type: ignore[arg-type]
        rating=rating,
        title=title,
        body=body,
        lang="en",
        country="in",
        posted_at=posted_at or NOW,
        app_version="1.0.0",
        fetched_at=NOW,
        content_hash="h",
    )


# ── Embeddings cache ─────────────────────────────────────────────────────────

@dataclass
class CountingEmbedder:
    """Deterministic embedder that records invocation counts."""

    model_name: str = "fake-model"
    calls: int = 0
    texts_seen: list[str] | None = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.texts_seen is None:
            self.texts_seen = []
        self.texts_seen.extend(texts)
        # Deterministic 3-d embedding derived from text length for assertions.
        return [[float(len(t)), float(sum(map(ord, t)) % 97), 0.5] for t in texts]


class TestEmbeddingsCached:
    def test_second_call_hits_cache(self, tmp_path: Path) -> None:
        # EVAL: test_embeddings_cached
        store = ReviewStore(tmp_path / "p.sqlite")
        store.migrate()
        embedder = CountingEmbedder()
        reviews = [_review(f"play_store:groww:{i}", body=f"body {i}") for i in range(4)]

        first = embed_reviews(reviews, embedder=embedder, store=store)
        assert len(first) == 4
        assert embedder.calls == 1
        assert embedder.texts_seen is not None and len(embedder.texts_seen) == 4

        # Second call must hit the SQLite cache → embedder not invoked again.
        second = embed_reviews(reviews, embedder=embedder, store=store)
        assert second == first
        assert embedder.calls == 1, "Cache miss: embedder was re-invoked"

    def test_cache_key_includes_model_name(self, tmp_path: Path) -> None:
        store = ReviewStore(tmp_path / "p.sqlite")
        store.migrate()
        embedder_a = CountingEmbedder(model_name="model-a")
        embedder_b = CountingEmbedder(model_name="model-b")
        reviews = [_review("play_store:groww:1", body="same text")]

        embed_reviews(reviews, embedder=embedder_a, store=store)
        embed_reviews(reviews, embedder=embedder_b, store=store)

        assert embedder_a.calls == 1
        assert embedder_b.calls == 1, "Model-b should miss cache, different model_name"

    def test_no_store_always_encodes(self) -> None:
        embedder = CountingEmbedder()
        reviews = [_review("play_store:groww:1")]
        embed_reviews(reviews, embedder=embedder, store=None)
        embed_reviews(reviews, embedder=embedder, store=None)
        assert embedder.calls == 2


# ── Cluster severity formula ─────────────────────────────────────────────────

class TestClusterRankSeverity:
    def test_severity_formula_ordering(self) -> None:
        # EVAL: test_cluster_rank_uses_severity_formula
        # Three synthetic clusters with known sizes/ratings/ages.
        # severity = size * (1 - mean_rating/5) * recency_weight
        # Cluster A: 10 reviews, rating=1, fresh (age 0d) → severity highest
        # Cluster B: 10 reviews, rating=1, old (age 84d) → recency decays → lower than A
        # Cluster C: 20 reviews, rating=5, fresh → zero severity (all 5-star)
        reviews = []
        labels = []
        for i in range(10):
            reviews.append(
                _review(f"play_store:groww:a{i}", rating=1, posted_at=NOW)
            )
            labels.append(0)
        for i in range(10):
            reviews.append(
                _review(
                    f"play_store:groww:b{i}",
                    rating=1,
                    posted_at=NOW - timedelta(days=84),
                )
            )
            labels.append(1)
        for i in range(20):
            reviews.append(
                _review(f"play_store:groww:c{i}", rating=5, posted_at=NOW)
            )
            labels.append(2)

        clusters = rank_clusters(reviews, labels, now_utc=NOW)
        ids = [c.cluster_id for c in clusters]
        assert ids == [0, 1, 2], f"Expected [0,1,2] by severity, got {ids}"
        assert clusters[0].severity > clusters[1].severity > clusters[2].severity
        # All-5-star cluster must have zero severity per formula (1 - 5/5 = 0).
        assert clusters[2].severity == pytest.approx(0.0)

    def test_noise_label_excluded(self) -> None:
        reviews = [_review(f"play_store:groww:{i}", rating=2) for i in range(3)]
        labels = [-1, -1, -1]
        clusters = rank_clusters(reviews, labels, now_utc=NOW)
        assert clusters == []

    def test_too_few_reviews_returns_empty(self) -> None:
        # EC-3.1 — low-signal input should not crash, returns [].
        reviews = [_review(f"play_store:groww:{i}", rating=2) for i in range(5)]
        empty_embeds = [[0.1, 0.2, 0.3] for _ in reviews]
        clusters = cluster_reviews(reviews, empty_embeds, top_k=3)
        assert clusters == []


# ── Theme provider: JSON schema + refusal ────────────────────────────────────

class _FakeProvider:
    """Scripted provider returning responses in sequence."""

    name = "fake"
    model = "fake-model"

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def generate_json(self, system: str, user: str) -> str:
        self.calls += 1
        if not self.responses:
            raise AssertionError("Fake provider out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cluster_of(reviews: list[Review], cluster_id: int = 0) -> Cluster:
    return Cluster(
        cluster_id=cluster_id,
        reviews=list(reviews),
        severity=1.0,
        mean_rating=sum(r.rating for r in reviews) / len(reviews),
        recency_weight=1.0,
    )


class TestThemeJsonSchemaEnforced:
    def test_invalid_json_retries_then_drops(self) -> None:
        # EVAL: test_theme_json_schema_enforced + EC-3.5
        reviews = [_review(f"play_store:groww:{i}", body="app crashes on open") for i in range(5)]
        cluster = _cluster_of(reviews)
        provider = _FakeProvider(["not-json", "still not json"])
        stats = ThemeGenerationStats()

        themes = name_themes([cluster], provider=provider, stats=stats)

        assert themes == [], "Invalid JSON twice must drop the theme"
        assert stats.json_retries == 1
        assert stats.dropped_invalid_json == 1
        assert provider.calls == 2

    def test_invalid_then_valid_json_keeps_theme(self) -> None:
        reviews = [_review(f"play_store:groww:{i}", body="app crashes on open") for i in range(3)]
        cluster = _cluster_of(reviews)
        valid = json.dumps(
            {
                "theme_name": "Crashes on open",
                "one_liner": "Users report crashes when opening the app.",
                "leadership_summary": (
                    "Crash on cold start blocks the core flow and will hurt "
                    "activation and retention if it persists into next week."
                ),
                "severity": "high",
                "confidence": "medium",
                "quotes": [
                    {"review_id": "play_store:groww:0", "text": "app crashes on open"}
                ],
                "action_ideas": [
                    {
                        "title": "Fix crash",
                        "rationale": "ship patch",
                        "impact": "restores first-launch completion",
                    }
                ],
                "who_this_helps": [
                    {"audience": "product", "why": "prioritize the crash fix"}
                ],
            }
        )
        provider = _FakeProvider(["{broken", valid])
        stats = ThemeGenerationStats()
        themes = name_themes([cluster], provider=provider, stats=stats)
        assert len(themes) == 1
        assert themes[0].theme_name == "Crashes on open"
        assert themes[0].severity == "high"
        assert themes[0].confidence == "medium"
        assert themes[0].leadership_summary.startswith("Crash on cold start")
        assert themes[0].action_ideas[0].impact == "restores first-launch completion"
        assert themes[0].who_this_helps[0].audience == "product"
        assert themes[0].who_this_helps[0].why == "prioritize the crash fix"
        assert stats.json_retries == 1
        assert stats.dropped_invalid_json == 0

    def test_refusal_drops_theme(self) -> None:
        # EC-3.6
        reviews = [_review(f"play_store:groww:{i}", body="neutral text") for i in range(3)]
        cluster = _cluster_of(reviews)
        refusal = json.dumps(
            {
                "theme_name": "",
                "one_liner": "",
                "quotes": [],
                "action_ideas": [],
                "who_this_helps": [],
            }
        )
        provider = _FakeProvider([refusal])
        stats = ThemeGenerationStats()
        themes = name_themes([cluster], provider=provider, stats=stats)
        assert themes == []
        assert stats.dropped_refusal == 1


class TestProviderFactory:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # EC-0.3 / EC-3.7
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        with pytest.raises(MissingCredentialsError):
            get_provider()

    def test_unknown_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "nonsense")
        with pytest.raises(ProviderError):
            get_provider()


# ── Validator: substring + PII placeholder ───────────────────────────────────

class TestValidateQuotes:
    def test_requires_exact_substring(self) -> None:
        # EVAL: test_validate_requires_exact_substring
        review = _review(
            "play_store:groww:1",
            body="The app freezes at open and I lose my session",
        )
        reviews_by_id = {review.review_id: review}
        assert is_quote_valid(
            Quote(review_id=review.review_id, text="app freezes"),
            reviews_by_id,
        )
        assert not is_quote_valid(
            Quote(review_id=review.review_id, text="app is broken"),
            reviews_by_id,
        )

    def test_rejects_pii_placeholder(self) -> None:
        # EVAL: test_validate_rejects_pii_placeholder
        review = _review(
            "play_store:groww:1",
            body="Contact [email] for help and see [url]",
        )
        reviews_by_id = {review.review_id: review}
        assert not is_quote_valid(
            Quote(review_id=review.review_id, text="Contact [email] for help"),
            reviews_by_id,
        )

    def test_nfkc_and_whitespace_normalization(self) -> None:
        # EC-3.9 — curly quotes + NBSP collapse before comparison.
        review = _review(
            "play_store:groww:1",
            body='The app "freezes" at open',
        )
        reviews_by_id = {review.review_id: review}
        quote = Quote(
            review_id=review.review_id,
            text="The app \u201cfreezes\u201d\u00a0at open",
        )
        assert is_quote_valid(quote, reviews_by_id)

    def test_unknown_review_id_rejected(self) -> None:
        reviews_by_id = {
            "play_store:groww:1": _review("play_store:groww:1", body="hello world"),
        }
        assert not is_quote_valid(
            Quote(review_id="play_store:groww:unknown", text="hello"),
            reviews_by_id,
        )

    def test_validate_themes_drops_all_invalid(self) -> None:
        # EC-3.4 — hallucinated quote leads to theme drop.
        review = _review("play_store:groww:1", body="The app is slow")
        reviews_by_id = {review.review_id: review}
        theme = _theme(
            theme_name="Hallucinated",
            one_liner="fake",
            quotes=[Quote(review_id=review.review_id, text="app is broken and on fire")],
            who_this_helps=[AudienceHelp(audience="product", why="noop")],
        )
        assert validate_themes([theme], reviews_by_id) == []

    def test_validate_themes_keeps_valid_subset(self) -> None:
        review = _review("play_store:groww:1", body="The app is slow and crashes often")
        reviews_by_id = {review.review_id: review}
        theme = _theme(
            theme_name="Performance",
            one_liner="slow",
            quotes=[
                Quote(review_id=review.review_id, text="app is slow"),
                Quote(review_id=review.review_id, text="non-existent substring"),
            ],
            action_ideas=[ActionIdea(title="t", rationale="r", impact="i")],
            who_this_helps=[AudienceHelp(audience="product", why="noop")],
            cluster_id=1,
        )
        result = validate_themes([theme], reviews_by_id)
        assert len(result) == 1
        assert [q.text for q in result[0].quotes] == ["app is slow"]


# ── Report: low-signal ────────────────────────────────────────────────────────

class TestLowSignalReport:
    def test_low_signal_when_few_themes(self) -> None:
        # EVAL: test_low_signal_report
        spec = RunSpec(product="groww", iso_week="2026-W16")
        report = build_report(spec, themes=[])
        assert report.low_signal is True
        assert report.themes == []
        assert report.reason == "insufficient_reviews"

    def test_single_theme_is_still_low_signal(self) -> None:
        spec = RunSpec(product="groww", iso_week="2026-W16")
        theme = _theme(
            theme_name="One",
            one_liner="only",
            quotes=[Quote(review_id="play_store:groww:1", text="ok")],
            n_reviews=5,
        )
        report = build_report(spec, themes=[theme], total_reviews=50)
        assert MIN_THEMES_FOR_SIGNAL == 2
        assert report.low_signal is True
        assert report.reason == "insufficient_themes"

    def test_happy_report_not_low_signal(self) -> None:
        spec = RunSpec(product="groww", iso_week="2026-W16")
        themes = [
            _theme(
                theme_name=str(i),
                one_liner="ok",
                quotes=[Quote(review_id="play_store:groww:1", text="ok")],
                cluster_id=i,
                n_reviews=10,
            )
            for i in range(2)
        ]
        report = build_report(spec, themes=themes, total_reviews=100)
        assert report.low_signal is False
        assert report.reason == ""
