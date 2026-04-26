"""Apple App Store review fetcher."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from time import sleep
from typing import Any

import requests
from dateutil.parser import isoparse

from pulse.ingestion.base import RawReview, ReviewSource
from pulse.run import ProductId

APP_STORE_REVIEWS_URL = (
    "https://itunes.apple.com/{country}/rss/customerreviews/"
    "page={page}/id={app_id}/sortby=mostrecent/json"
)
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup?id={app_id}&country={country}"
APP_STORE_FALLBACK_URL = "https://apps.apple.com/{country}/app/id{app_id}"
SERIALIZED_DATA_RE = re.compile(
    r'<script type="application/json" id="serialized-server-data">(.*?)</script>',
    re.S,
)


class AppStoreSource(ReviewSource):
    """Fetches public customer reviews via the iTunes customer-reviews RSS JSON endpoint."""

    source_id = "app_store"

    def __init__(
        self,
        app_id: str,
        country: str = "in",
        *,
        max_pages: int = 25,
        request_timeout_s: float = 20.0,
        per_page_sleep_s: float = 0.25,
        session: requests.Session | None = None,
    ) -> None:
        self.app_id = app_id
        self.country = country
        self.max_pages = max_pages
        self.request_timeout_s = request_timeout_s
        self.per_page_sleep_s = per_page_sleep_s
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "weekly-review-pulse/0.1 "
                    "(contact: engineering@example.com)"
                ),
                "Accept": "application/json",
            }
        )

    def fetch(
        self,
        product: ProductId,
        since: datetime,
        until: datetime,
    ) -> Iterable[RawReview]:
        since_utc = _as_utc(since)
        until_utc = _as_utc(until)
        rss_emitted = 0
        for page in range(1, self.max_pages + 1):
            url = APP_STORE_REVIEWS_URL.format(
                country=self.country,
                page=page,
                app_id=self.app_id,
            )
            response = self.session.get(url, timeout=self.request_timeout_s)
            response.raise_for_status()
            payload = response.json()

            entries: list[dict[str, Any]] = payload.get("feed", {}).get("entry", [])
            if not entries:
                break

            # Apple includes one metadata entry at the top without review fields.
            page_had_review = False
            stop_due_to_since = False

            for entry in entries:
                parsed = _parse_entry(entry, product=product, country=self.country)
                if parsed is None:
                    continue
                page_had_review = True

                if parsed.posted_at < since_utc:
                    stop_due_to_since = True
                    continue
                if parsed.posted_at > until_utc:
                    continue
                rss_emitted += 1
                yield parsed

            if stop_due_to_since:
                break
            if not page_had_review:
                break
            sleep(self.per_page_sleep_s)

        # Apple RSS is frequently empty now. Fallback to parsing embedded reviews
        # from the app's public web page state when RSS emitted no reviews.
        if rss_emitted == 0:
            yield from self._fetch_from_app_page(
                product=product,
                since=since_utc,
                until=until_utc,
            )

    def _fetch_from_app_page(
        self,
        *,
        product: ProductId,
        since: datetime,
        until: datetime,
    ) -> Iterable[RawReview]:
        page_url = self._resolve_app_page_url()
        response = self.session.get(page_url, timeout=self.request_timeout_s)
        response.raise_for_status()
        html = response.text
        parsed_reviews = _parse_reviews_from_serialized_server_data(
            html,
            product=product,
            country=self.country,
        )

        seen: set[str] = set()
        for review in parsed_reviews:
            if review.native_id in seen:
                continue
            seen.add(review.native_id)
            if review.posted_at < since or review.posted_at > until:
                continue
            yield review

    def _resolve_app_page_url(self) -> str:
        lookup_url = ITUNES_LOOKUP_URL.format(app_id=self.app_id, country=self.country)
        response = self.session.get(lookup_url, timeout=self.request_timeout_s)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if results and isinstance(results, list):
            track_url = results[0].get("trackViewUrl")
            if track_url:
                return str(track_url)
        return APP_STORE_FALLBACK_URL.format(country=self.country, app_id=self.app_id)


__all__ = ["AppStoreSource"]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_entry(
    entry: dict[str, Any],
    *,
    product: ProductId,
    country: str,
) -> RawReview | None:
    rating_value = _nested(entry, "im:rating", "label")
    body = _nested(entry, "content", "label")
    posted = _nested(entry, "updated", "label")
    if not rating_value or not body or not posted:
        return None

    title = _nested(entry, "title", "label")
    native_id = _nested(entry, "id", "label") or _nested(entry, "id", "attributes", "im:id")
    if not native_id:
        return None

    app_version = _nested(entry, "im:version", "label")
    lang = _nested(entry, "im:contentType", "attributes", "term") or "en"

    posted_at = _as_utc(isoparse(posted))
    fetched_at = datetime.now(UTC)
    return RawReview(
        native_id=str(native_id),
        product=product,
        source="app_store",
        rating=int(rating_value),
        title=str(title) if title else None,
        body=str(body),
        lang=str(lang),
        country=country,
        posted_at=posted_at,
        app_version=str(app_version) if app_version else None,
        fetched_at=fetched_at,
    )


def _parse_reviews_from_serialized_server_data(
    html: str,
    *,
    product: ProductId,
    country: str,
) -> list[RawReview]:
    match = SERIALIZED_DATA_RE.search(html)
    if not match:
        return []
    serialized = match.group(1)
    payload: dict[str, Any] = json.loads(serialized)
    data = payload.get("data", [])
    fetched_at = datetime.now(UTC)
    reviews: list[RawReview] = []
    for obj in _walk_objects(data):
        if not isinstance(obj, dict):
            continue
        if obj.get("$kind") != "Review":
            continue
        native_id = obj.get("id")
        body = obj.get("contents")
        posted = obj.get("date")
        rating = obj.get("rating")
        if not native_id or not body or not posted or rating is None:
            continue
        title = obj.get("title")
        reviews.append(
            RawReview(
                native_id=str(native_id),
                product=product,
                source="app_store",
                rating=int(rating),
                title=str(title) if title else None,
                body=str(body),
                lang="en",
                country=country,
                posted_at=_as_utc(isoparse(str(posted))),
                app_version=None,
                fetched_at=fetched_at,
            )
        )
    return reviews


def _nested(obj: dict[str, Any], *keys: str) -> Any | None:
    current: Any = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_objects(item)
