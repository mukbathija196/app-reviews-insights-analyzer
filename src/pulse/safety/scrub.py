"""PII redaction for review text."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Redaction:
    category: str    # e.g. "email", "phone", "pan"
    original: str
    replacement: str
    start: int
    end: int


def scrub(text: str) -> tuple[str, list[Redaction]]:
    """Return (scrubbed_text, redactions) using regex and optional Presidio pass."""
    redactions: list[Redaction] = []
    scrubbed = text

    # Order matters: URLs first so embedded emails/tokens are not partially leaked.
    scrubbed = _apply_pattern(scrubbed, URL_WITH_QUERY_RE, "url", "[url]", redactions)
    scrubbed = _apply_pattern(scrubbed, EMAIL_RE, "email", "[email]", redactions)
    scrubbed = _apply_pattern(
        scrubbed,
        PHONE_RE,
        "phone",
        "[phone]",
        redactions,
        validator=_is_valid_phone,
    )
    scrubbed = _apply_pattern(scrubbed, PAN_RE, "pan", "[pan]", redactions)
    scrubbed = _apply_pattern(
        scrubbed,
        CARD_CANDIDATE_RE,
        "card",
        "[card]",
        redactions,
        validator=_is_luhn_match,
    )
    scrubbed = _apply_pattern(
        scrubbed,
        AADHAAR_RE,
        "aadhaar",
        "[aadhaar]",
        redactions,
        validator=_is_valid_aadhaar,
    )
    scrubbed = _apply_presidio(scrubbed, redactions)
    return scrubbed, redactions


__all__ = ["scrub", "Redaction"]


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+91[-\s]?)?(?:[6-9]\d{9})\b")
PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
AADHAAR_RE = re.compile(r"\b(?:\d{4}[-\s]?){2}\d{4}\b(?![-\s]?\d)")
URL_WITH_QUERY_RE = re.compile(r"https?://[^\s]+?\?[^\s]+")
CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _apply_pattern(
    text: str,
    pattern: re.Pattern[str],
    category: str,
    replacement: str,
    redactions: list[Redaction],
    validator: Callable[[str], bool] | None = None,
) -> str:
    def _replace(match: re.Match[str]) -> str:
        original = match.group(0)
        if validator and not validator(original):
            return original
        redactions.append(
            Redaction(
                category=category,
                original=original,
                replacement=replacement,
                start=match.start(),
                end=match.end(),
            )
        )
        return replacement

    return pattern.sub(_replace, text)


def _is_valid_phone(value: str) -> bool:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    return len(digits) == 10 and digits[0] in {"6", "7", "8", "9"}


def _is_valid_aadhaar(value: str) -> bool:
    digits = "".join(ch for ch in value if ch.isdigit())
    return len(digits) == 12 and digits[0] in {"2", "3", "4", "5", "6", "7", "8", "9"}


def _is_luhn_match(value: str) -> bool:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 13 or len(digits) > 19:
        return False
    return _luhn_valid(digits)


def _luhn_valid(number: str) -> bool:
    total = 0
    reverse_digits = number[::-1]
    for idx, ch in enumerate(reverse_digits):
        n = int(ch)
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _apply_presidio(text: str, redactions: list[Redaction]) -> str:
    try:
        from presidio_analyzer import AnalyzerEngine
    except Exception:
        return text

    analyzer = AnalyzerEngine()
    entities = analyzer.analyze(
        text=text,
        language="en",
        entities=["PERSON", "LOCATION"],
    )
    # apply from right-to-left to preserve spans
    updated = text
    for entity in sorted(entities, key=lambda e: e.start, reverse=True):
        original = updated[entity.start:entity.end]
        if not original.strip():
            continue
        replacement = f"[{entity.entity_type.lower()}]"
        updated = f"{updated[:entity.start]}{replacement}{updated[entity.end:]}"
        redactions.append(
            Redaction(
                category=entity.entity_type.lower(),
                original=original,
                replacement=replacement,
                start=entity.start,
                end=entity.end,
            )
        )
    return updated
