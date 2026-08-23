"""The Domain environment: the current spatial extent, read implicitly.

Spatial producers read it instead of threading ``aoi=`` everywhere; a step that
refines it declares ``.overrides_domain()`` and the interpreter rebinds.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

__all__ = ["Domain", "bind_domain", "current_domain", "domain_from_result", "reset_domain"]


@dataclass(frozen=True, slots=True)
class Domain:
    """The current AOI. ``label`` is what the run narrates it as."""

    bbox: tuple[float, float, float, float] | None = None
    geometry: dict[str, Any] | None = None
    label: str | None = None

    def as_doc(self) -> dict[str, Any]:
        return {"bbox": list(self.bbox) if self.bbox else None,
                "geometry": self.geometry, "label": self.label}

    @classmethod
    def from_doc(cls, doc: Any) -> "Domain | None":
        """Rebuild a recorded domain; ``None`` when the record carries none."""
        if not isinstance(doc, dict):
            return None
        bbox = doc.get("bbox")
        geometry = doc.get("geometry")
        if bbox is None and geometry is None:
            return None
        return cls(
            bbox=(tuple(float(v) for v in bbox)  # type: ignore[arg-type]
                  if bbox and len(tuple(bbox)) == 4 else None),
            geometry=geometry if isinstance(geometry, dict) else None,
            label=doc.get("label"),
        )


_DOMAIN: contextvars.ContextVar[Domain | None] = contextvars.ContextVar(
    "trid3nt_declarative_domain", default=None
)


def current_domain() -> Domain | None:
    """The domain in force for the step now running (``None`` outside a plan)."""
    return _DOMAIN.get()


def bind_domain(domain: Domain | None) -> contextvars.Token:
    return _DOMAIN.set(domain)


def reset_domain(token: contextvars.Token) -> None:
    _DOMAIN.reset(token)


def domain_from_result(result: Any) -> Domain | None:
    """Read a refined domain off a step result, or ``None`` if it carries none."""
    if isinstance(result, Domain):
        return result
    bbox = getattr(result, "bbox", None)
    if bbox is None and isinstance(result, dict):
        bbox = result.get("bbox")
    if not bbox or len(tuple(bbox)) != 4:
        return None
    label = getattr(result, "name", None)
    if label is None and isinstance(result, dict):
        label = result.get("name")
    return Domain(bbox=tuple(float(v) for v in bbox), label=label)  # type: ignore[arg-type]
