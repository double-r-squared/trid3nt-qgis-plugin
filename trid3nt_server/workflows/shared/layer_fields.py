"""Reading one field off whatever shape a fetched layer arrived in.

A model, a replayed model or a plain mapping all answer here, and an absent field
answers ``None`` rather than raising.
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
