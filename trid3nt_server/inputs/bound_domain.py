"""The Domain environment: the current spatial extent, read implicitly.

Spatial producers read it instead of threading ``aoi=`` everywhere; a step that
refines it rebinds it.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

__all__ = ["Domain", "bind_domain", "current_domain", "reset_domain"]


@dataclass(frozen=True, slots=True)
class Domain:
    """The current AOI. ``label`` is what the run narrates it as."""

    bbox: tuple[float, float, float, float] | None = None
    geometry: dict[str, Any] | None = None
    label: str | None = None


_DOMAIN: contextvars.ContextVar[Domain | None] = contextvars.ContextVar(
    "trid3nt_declarative_domain", default=None
)


def current_domain() -> Domain | None:
    """The domain in force for the stage now running (``None`` outside a run)."""
    return _DOMAIN.get()


def bind_domain(domain: Domain | None) -> contextvars.Token:
    return _DOMAIN.set(domain)


def reset_domain(token: contextvars.Token) -> None:
    _DOMAIN.reset(token)
