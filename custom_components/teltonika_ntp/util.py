"""Helpers that treat data from the device as untrusted."""

from __future__ import annotations

from typing import Any

# Longest text taken from the device into Home Assistant (names, versions, errors).
MAX_TEXT_LENGTH = 64
MAX_ERROR_LENGTH = 200


def as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def clean_text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str | None:
    """Return a printable, length-limited string, or None for empty or non-text values.

    Control characters (newlines, escape codes) are replaced so device text cannot
    break log lines or UI labels.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = "".join(char if char.isprintable() else " " for char in str(value)).strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
