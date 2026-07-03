"""Small, side-effect-free helpers shared across Music Manager modules."""

from __future__ import annotations

from typing import Any, Iterable


def clean_error(error: BaseException) -> str:
    """Return an exception description suitable for a report cell."""
    message = " ".join(str(error).split())
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


def first_tag(tags: Any, keys: Iterable[str]) -> str:
    """Return the first non-empty value from a Mutagen tag mapping."""
    if tags is None:
        return ""

    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, KeyError, TypeError):
            continue

        # Native ID3 frames expose their values through ``text``.
        value = getattr(value, "text", value)
        if isinstance(value, list):
            value = value[0] if value else ""
        # Native MP4 track numbers use a list containing (track, total).
        if isinstance(value, tuple):
            if value and all(isinstance(item, int) for item in value[:2]):
                current = value[0]
                total = value[1] if len(value) > 1 else 0
                value = f"{current}/{total}" if total else current
            else:
                value = value[0] if value else ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""
