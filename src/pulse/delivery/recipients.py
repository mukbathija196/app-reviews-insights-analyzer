"""Validation and masking for user-supplied report recipients."""

from __future__ import annotations

import re

# Deliberately strict: one address, no display names, no header-injection characters.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_MAX_EMAIL_LEN = 254


def normalize_email(value: str) -> str:
    """Return a trimmed, lower-cased address or raise ValueError if it is not valid."""
    email = (value or "").strip().lower()
    if not email or len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email address: {value!r}")
    return email


def mask_email(email: str) -> str:
    """Mask an address for logs and run records, e.g. 'jane@example.com' -> 'j***@example.com'."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


__all__ = ["mask_email", "normalize_email"]
