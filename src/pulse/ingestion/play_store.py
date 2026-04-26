"""Google Play Store review fetcher."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from time import sleep
from typing import Any

from pulse.ingestion.base import RawReview, ReviewSource
from pulse.run import ProductId


class PlayStoreSource(ReviewSource):
    """Fetches reviews via `google-play-scraper` (no API key required)."""

    source_id = "play_store"

    def __init__(
        self,
        package_name: str,
        *,
        lang: str = "en",
        country: str = "in",
        request_pause_s: float = 1.0,
        max_batches: int = 20,
        batch_size: int = 200,
    ) -> None:
        self.package_name = package_name
        self.lang = lang
        self.country = country
        self.request_pause_s = request_pause_s
        self.max_batches = max_batches
        self.batch_size = batch_size

    def fetch(
        self,
        product: ProductId,
        since: datetime,
        until: datetime,
    ) -> Iterable[RawReview]:
        try:
            from google_play_scraper import Sort
            from google_play_scraper import reviews as gp_reviews
        except ImportError as exc:
            raise RuntimeError(
                "google-play-scraper is required for PlayStoreSource. "
                "Run: uv sync"
            ) from exc

        since_utc = _as_utc(since)
        until_utc = _as_utc(until)

        continuation_token: str | None = None
        for _ in range(self.max_batches):
            result_rows, continuation_token = gp_reviews(
                self.package_name,
                lang=self.lang,
                country=self.country,
                sort=Sort.NEWEST,
                count=self.batch_size,
                continuation_token=continuation_token,
            )
            if not result_rows:
                break

            stop_due_to_since = False
            for row in result_rows:
                parsed = _parse_row(
                    row,
                    product=product,
                    lang=self.lang,
                    country=self.country,
                )
                if parsed is None:
                    continue
                if parsed.posted_at < since_utc:
                    stop_due_to_since = True
                    continue
                if parsed.posted_at > until_utc:
                    continue
                yield parsed

            if stop_due_to_since or continuation_token is None:
                break
            sleep(self.request_pause_s)


__all__ = ["PlayStoreSource"]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_row(
    row: dict[str, Any],
    *,
    product: ProductId,
    lang: str,
    country: str,
) -> RawReview | None:
    native_id = row.get("reviewId")
    body = row.get("content")
    score = row.get("score")
    posted = row.get("at")
    if not native_id or not body or score is None or posted is None:
        return None
    posted_at = posted if isinstance(posted, datetime) else None
    if posted_at is None:
        return None

    return RawReview(
        native_id=str(native_id),
        product=product,
        source="play_store",
        rating=int(score),
        title=None,
        body=str(body),
        lang=lang,
        country=country,
        posted_at=_as_utc(posted_at),
        app_version=(
            str(row.get("reviewCreatedVersion"))
            if row.get("reviewCreatedVersion")
            else None
        ),
        fetched_at=datetime.now(UTC),
    )
