"""Identifiers that end up in stream names and Git ref names."""

from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def require_identifier(kind: str, value: str) -> str:
    """Letters, digits, dot, underscore and hyphen only, so the identifier can be placed
    in a ref or a stream name without changing its shape."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value.endswith("."):
        raise ValueError(
            f"a {kind} id is 1 to 64 letters, digits, dots, underscores or hyphens, "
            "starting with a letter or digit"
        )
    return value
