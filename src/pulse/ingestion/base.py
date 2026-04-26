"""Shared protocols and data classes for the ingestion layer. Implemented in Phase 1."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

ProductId = str


@dataclass(frozen=True)
class RawReview:
    """Unprocessed review as returned directly by a source adapter."""

    native_id: str
    product: ProductId
    source: Literal["app_store", "play_store"]
    rating: int
    title: str | None
    body: str
    lang: str
    country: str
    posted_at: datetime
    app_version: str | None
    fetched_at: datetime


@dataclass(frozen=True)
class Review:
    """Normalized, deduplicated review ready for the safety and reasoning layers."""

    review_id: str           # "{source}:{product}:{native_id}"
    product: ProductId
    source: Literal["app_store", "play_store"]
    rating: int              # 1..5
    title: str | None
    body: str                # PII-scrubbed in Phase 2; raw until then
    lang: str                # BCP-47
    country: str             # ISO-3166-1 alpha-2
    posted_at: datetime      # UTC
    app_version: str | None
    fetched_at: datetime     # UTC
    content_hash: str        # sha256 of normalized title+body
    truncated: bool = False  # True when body was clipped to max_body_chars


class ReviewSource(Protocol):
    """Interface every store adapter must satisfy."""

    source_id: Literal["app_store", "play_store"]

    def fetch(
        self,
        product: ProductId,
        since: datetime,
        until: datetime,
    ) -> Iterable[RawReview]:
        """Yield raw reviews posted in [since, until] UTC."""
        ...
