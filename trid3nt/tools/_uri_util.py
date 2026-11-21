"""Layer-uri helper shared by tools that must not import the cases package."""

from __future__ import annotations

__all__ = ["_strip_query"]


def _strip_query(uri: str) -> str:
    return uri.split("?", 1)[0].rstrip("/")
