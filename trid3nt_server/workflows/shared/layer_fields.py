"""Reading one field off whatever shape a fetched layer arrived in.

A registered fetcher returns a ``LayerURI``; the ledger replays it as the same
model, and a hand-rolled producer or a stub returns a plain mapping. Every reader
wants the same field out of all three, so the shape test lives here once instead
of at each call site - and absence answers ``None`` rather than raising, because
the optional fields (a ``fallback_note`` on an undegraded fetch) are absent far
more often than they are present.
"""

from __future__ import annotations

from typing import Any

__all__ = ["layer_field"]


def layer_field(result: Any, field: str) -> Any:
    if result is None:
        return None
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field)
    return None
