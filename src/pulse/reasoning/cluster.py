"""UMAP + HDBSCAN clustering with severity ranking."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pulse.ingestion.base import Review

CLUSTER_NOISE_LABEL = -1


@dataclass(frozen=True)
class Cluster:
    """A dense group of semantically similar reviews with a severity score."""

    cluster_id: int
    reviews: list[Review]
    severity: float
    mean_rating: float
    recency_weight: float

    @property
    def size(self) -> int:
        return len(self.reviews)


def cluster_reviews(
    reviews: list[Review],
    embeddings: list[list[float]],
    *,
    top_k: int = 3,
    random_state: int = 42,
    min_cluster_size: int | None = None,
    now_utc: datetime | None = None,
) -> list[Cluster]:
    """Return top-k severity-ranked clusters.

    Pipeline:
      1. UMAP → ~10 dims (cosine metric).
      2. HDBSCAN with dynamic ``min_cluster_size``.
      3. Rank remaining (non-noise) clusters by
         ``severity = size × (1 − mean_rating/5) × recency_weight``.

    Returns an empty list when the dataset is too small to cluster meaningfully
    (satisfies EC-3.1 — the caller emits a low-signal report).
    """
    if len(reviews) != len(embeddings):
        raise ValueError(
            f"reviews / embeddings length mismatch: {len(reviews)} vs {len(embeddings)}"
        )

    n = len(reviews)
    dyn_min_cluster_size = min_cluster_size if min_cluster_size is not None else max(5, n // 30)

    # Not enough data to meaningfully cluster → caller will treat as low-signal.
    if n < max(2 * dyn_min_cluster_size, 10):
        return []

    labels = _compute_labels(
        embeddings,
        random_state=random_state,
        min_cluster_size=dyn_min_cluster_size,
    )
    clusters = rank_clusters(reviews, labels, now_utc=now_utc)
    return clusters[:top_k]


def _compute_labels(
    embeddings: list[list[float]],
    *,
    random_state: int,
    min_cluster_size: int,
) -> list[int]:
    """Run UMAP → HDBSCAN and return a cluster label per embedding."""
    try:
        import numpy as np
        import umap
        from hdbscan import HDBSCAN
    except ImportError as exc:
        raise RuntimeError(
            "Clustering dependencies missing. Install with 'uv sync --extra reasoning'."
        ) from exc

    matrix = np.asarray(embeddings, dtype="float32")
    n_samples = matrix.shape[0]
    # UMAP requires n_neighbors < n_samples.
    n_neighbors = min(15, max(2, n_samples - 1))
    target_dim = min(10, max(2, n_samples - 2))

    reducer = umap.UMAP(
        n_components=target_dim,
        n_neighbors=n_neighbors,
        min_dist=0.0,
        metric="cosine",
        random_state=random_state,
    )
    projected = reducer.fit_transform(matrix)

    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=max(3, min_cluster_size // 2),
        metric="euclidean",
        prediction_data=False,
    )
    labels = clusterer.fit_predict(projected)
    return [int(x) for x in labels]


def rank_clusters(
    reviews: list[Review],
    labels: list[int],
    *,
    now_utc: datetime | None = None,
) -> list[Cluster]:
    """Group reviews by label and rank by severity.

    Exposed independently from the UMAP/HDBSCAN step so tests can assert the
    severity formula without pulling in the full reasoning stack.
    """
    if len(reviews) != len(labels):
        raise ValueError(
            f"reviews / labels length mismatch: {len(reviews)} vs {len(labels)}"
        )

    now_utc = now_utc or datetime.now(UTC)

    buckets: dict[int, list[Review]] = {}
    for review, label in zip(reviews, labels, strict=True):
        if label == CLUSTER_NOISE_LABEL:
            continue
        buckets.setdefault(label, []).append(review)

    clusters: list[Cluster] = []
    for label, members in buckets.items():
        mean_rating = sum(r.rating for r in members) / len(members)
        recency = _recency_weight(members, now_utc=now_utc)
        severity = len(members) * (1.0 - mean_rating / 5.0) * recency
        clusters.append(
            Cluster(
                cluster_id=int(label),
                reviews=sorted(members, key=lambda r: r.posted_at, reverse=True),
                severity=severity,
                mean_rating=mean_rating,
                recency_weight=recency,
            )
        )

    clusters.sort(
        key=lambda c: (c.severity, c.size, -c.cluster_id),
        reverse=True,
    )
    return clusters


def _recency_weight(
    reviews: list[Review],
    *,
    now_utc: datetime,
    half_life_days: float = 21.0,
) -> float:
    """Exponential recency weight in (0, 1]. Younger clusters weigh more."""
    if not reviews:
        return 0.0
    ages_days: list[float] = []
    for review in reviews:
        posted = review.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        age = max(timedelta(0), now_utc - posted).total_seconds() / 86400.0
        ages_days.append(age)
    mean_age = sum(ages_days) / len(ages_days)
    return math.exp(-mean_age / half_life_days)


__all__ = [
    "CLUSTER_NOISE_LABEL",
    "Cluster",
    "cluster_reviews",
    "rank_clusters",
]
