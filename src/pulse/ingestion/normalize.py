"""Review normalization and deduplication."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime

from pulse.ingestion.base import RawReview, Review

MAX_BODY_CHARS = 2_000
MIN_BODY_WORDS = 5
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"\b[\w']+\b")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport/map
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF"          # misc symbols
    "\u2700-\u27BF"          # dingbats
    "]+",
    flags=re.UNICODE,
)
_ENGLISH_LANG_HINTS = {"en", "en-us", "en-gb", "english"}


def normalize(raw: RawReview) -> Review:
    """Convert a RawReview to a normalized Review."""
    normalized_title = sanitize_text(raw.title) if raw.title else None
    normalized_body = sanitize_text(raw.body)
    truncated = len(normalized_body) > MAX_BODY_CHARS
    if truncated:
        normalized_body = normalized_body[:MAX_BODY_CHARS].rstrip()

    rating = max(1, min(5, raw.rating))

    review = Review(
        review_id=f"{raw.source}:{raw.product}:{raw.native_id}",
        product=raw.product,
        source=raw.source,
        rating=rating,
        title=normalized_title,
        body=normalized_body,
        lang=raw.lang.strip().lower() if raw.lang else "en",
        country=raw.country.strip().lower() if raw.country else "in",
        posted_at=_as_utc(raw.posted_at),
        app_version=raw.app_version.strip() if raw.app_version else None,
        fetched_at=_as_utc(raw.fetched_at),
        content_hash=content_hash(normalized_title, normalized_body),
        truncated=truncated,
    )
    # Clamp impossible future timestamps caused by store-side clock anomalies.
    now_utc = datetime.now(UTC)
    if review.posted_at > now_utc:
        review = replace(review, posted_at=now_utc)
    return review


def content_hash(title: str | None, body: str) -> str:
    """Return SHA-256 of whitespace-normalized title + body."""
    normalized_title = sanitize_text(title) if title else ""
    normalized_body = sanitize_text(body)
    payload = f"{normalized_title}\n{normalized_body}".encode()
    return hashlib.sha256(payload).hexdigest()


def sanitize_text(value: str | None) -> str:
    """Remove emojis and collapse whitespace."""
    if not value:
        return ""
    no_emoji = _EMOJI_RE.sub(" ", value)
    return _normalize_ws(no_emoji)


def is_review_eligible(raw: RawReview, normalized: Review) -> tuple[bool, str | None]:
    """Return eligibility for ingestion with exclusion reason when filtered."""
    if count_words(normalized.body) < MIN_BODY_WORDS:
        return False, "lt_5_words"

    lang_hint = (raw.lang or "").strip().lower()
    if lang_hint and not _is_english_hint(lang_hint):
        return False, "non_english_lang_hint"

    if not is_english_text(normalized.body):
        return False, "non_english_detected"

    return True, None


def count_words(value: str) -> int:
    return len(_WORD_RE.findall(value))


def is_english_text(value: str) -> bool:
    """Detect whether text is English."""
    cleaned = sanitize_text(value)
    if not cleaned:
        return False
    # Very short text is too noisy for language detection.
    if count_words(cleaned) < MIN_BODY_WORDS:
        return False

    try:
        from langdetect import DetectorFactory, LangDetectException, detect
    except ImportError as exc:
        raise RuntimeError(
            "langdetect is required for Phase 1 language filtering. Run: uv sync"
        ) from exc

    DetectorFactory.seed = 0
    try:
        return str(detect(cleaned)) == "en"
    except LangDetectException:
        return False


__all__ = [
    "MIN_BODY_WORDS",
    "count_words",
    "content_hash",
    "is_english_text",
    "is_review_eligible",
    "normalize",
    "sanitize_text",
]


def _normalize_ws(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_english_hint(lang_hint: str) -> bool:
    if lang_hint in _ENGLISH_LANG_HINTS:
        return True
    return lang_hint.startswith("en-")
