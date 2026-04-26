"""Quote grounding validator — every published quote must be verbatim.

EC-3.9: both quote and source are NFKC-normalized and whitespace-collapsed
before the substring check. Quotes containing PII placeholders emitted by
``pulse.safety.scrub`` are refused even if they happen to appear in the body.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from pulse.ingestion.base import Review
from pulse.reasoning.theme import Quote, Theme

# Any placeholder produced by safety.scrub.
PII_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "[email]",
        "[phone]",
        "[pan]",
        "[aadhaar]",
        "[url]",
        "[card]",
        "[person]",
        "[location]",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")

# Smart-quote / dash folding (not covered by NFKC) — EC-3.9.
_QUOTE_FOLDING = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


def _normalize(text: str) -> str:
    """NFKC + quote folding + whitespace-collapse for substring comparison."""
    folded = (text or "").translate(_QUOTE_FOLDING)
    normalized = unicodedata.normalize("NFKC", folded)
    return _WHITESPACE_RE.sub(" ", normalized).strip().lower()


def _contains_pii_placeholder(text: str) -> bool:
    lowered = text.lower()
    return any(placeholder in lowered for placeholder in PII_PLACEHOLDERS)


def is_quote_valid(quote: Quote, reviews_by_id: dict[str, Review]) -> bool:
    """Return True iff ``quote`` is safe to publish.

    Rules (fail-closed):
      1. `review_id` must exist in the run's dataset.
      2. Quote text must not contain any PII placeholder (EC-2.x belt-and-suspenders).
      3. NFKC- and whitespace-normalized quote must be a contiguous substring
         of the normalized scrubbed review body (EC-3.9).
    """
    review = reviews_by_id.get(quote.review_id)
    if review is None:
        return False
    if not quote.text or not quote.text.strip():
        return False
    if _contains_pii_placeholder(quote.text):
        return False

    normalized_quote = _normalize(quote.text)
    if not normalized_quote:
        return False
    normalized_body = _normalize(review.body)
    return normalized_quote in normalized_body


def validate_themes(
    themes: list[Theme],
    reviews_by_id: dict[str, Review],
) -> list[Theme]:
    """Drop invalid quotes; drop themes that have zero valid quotes left.

    This is the backstop for EC-3.4 (hallucinated quotes) and EC-3.9 (unicode
    drift). It never "repairs" — it only filters.
    """
    out: list[Theme] = []
    for theme in themes:
        kept = [q for q in theme.quotes if is_quote_valid(q, reviews_by_id)]
        if not kept:
            continue
        out.append(replace(theme, quotes=kept))
    return out


__all__ = [
    "PII_PLACEHOLDERS",
    "is_quote_valid",
    "validate_themes",
]
