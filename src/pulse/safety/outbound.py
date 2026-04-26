"""Outbound PII check applied to rendered outputs before delivery."""

from __future__ import annotations

from pulse.safety.scrub import scrub


def outbound_scrub(text: str) -> str:
    """Second-pass scrub on rendered Doc/email text."""
    scrubbed, _ = scrub(text)
    return scrubbed


__all__ = ["outbound_scrub"]
