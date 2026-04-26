"""Local CPU embeddings via sentence-transformers with SQLite caching."""

from __future__ import annotations

from typing import Protocol

from pulse.ingestion.base import Review
from pulse.storage.sqlite import ReviewStore

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class Embedder(Protocol):
    """Minimal encoder interface used by the reasoning pipeline.

    Kept narrow so tests can drop in deterministic stand-ins.
    """

    model_name: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Lazy-loaded sentence-transformers wrapper (CPU-only)."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers not installed. Install the reasoning extras: "
                    "'uv sync --extra reasoning'."
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(  # type: ignore[attr-defined]
            texts,
            batch_size=32,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return [list(map(float, v)) for v in vectors]


def embed_reviews(
    reviews: list[Review],
    *,
    embedder: Embedder | None = None,
    store: ReviewStore | None = None,
    model_name: str | None = None,
) -> list[list[float]]:
    """Return embedding vectors aligned to ``reviews``.

    When ``store`` is provided, cached vectors are reused (EVAL: test_embeddings_cached)
    and freshly computed ones are persisted.
    """
    if not reviews:
        return []

    if embedder is None:
        embedder = SentenceTransformerEmbedder(model_name or DEFAULT_EMBEDDING_MODEL)

    key = model_name or embedder.model_name

    # Fetch cached embeddings (if store provided).
    results: list[list[float] | None] = [None] * len(reviews)
    missing_indices: list[int] = []
    missing_texts: list[str] = []

    for idx, review in enumerate(reviews):
        cached = store.get_embedding(review.review_id, key) if store is not None else None
        if cached is not None:
            results[idx] = cached
        else:
            missing_indices.append(idx)
            missing_texts.append(_review_text(review))

    if missing_texts:
        fresh_vectors = embedder.encode(missing_texts)
        if len(fresh_vectors) != len(missing_texts):
            raise RuntimeError(
                "Embedder returned mismatched vector count: "
                f"expected {len(missing_texts)}, got {len(fresh_vectors)}"
            )
        for idx, vector in zip(missing_indices, fresh_vectors, strict=True):
            results[idx] = vector
            if store is not None:
                store.put_embedding(reviews[idx].review_id, key, vector)

    assert all(v is not None for v in results)  # narrowing for type-checker
    return [v for v in results if v is not None]


def _review_text(review: Review) -> str:
    parts: list[str] = []
    if review.title:
        parts.append(review.title.strip())
    parts.append(review.body.strip())
    return "\n".join(p for p in parts if p)


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "Embedder",
    "SentenceTransformerEmbedder",
    "embed_reviews",
]
