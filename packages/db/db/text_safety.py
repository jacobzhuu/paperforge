"""Sanitize strings before they enter Postgres ``text`` / ``jsonb`` columns.

Postgres rejects NUL (``\\x00``) in text fields. PDF extractors occasionally emit
NULs and other illegal control characters when fonts or embedded objects are
corrupt. Strip them at the storage boundary so a single bad document cannot
abort an entire ingest stage.
"""

from __future__ import annotations

import re
from typing import Any

# Keep tab / LF / CR; drop NUL and the remaining C0 controls plus DEL.
_ILLEGAL_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_pg_text(value: Any, *, max_chars: int | None = None) -> str | None:
    """Return a Postgres-safe text value, or ``None`` when the input is empty."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    cleaned = _ILLEGAL_CONTROLS.sub("", text)
    if max_chars is not None:
        cleaned = cleaned[:max_chars]
    return cleaned if cleaned else None


__all__ = ["sanitize_pg_text"]
