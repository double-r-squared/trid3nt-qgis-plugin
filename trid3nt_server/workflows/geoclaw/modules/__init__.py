"""One wrapper per GeoClaw body: the dictionary over Clawpack's own Data classes.

``module.py`` holds what a wrapper is; the slot, fill and sheet surface every
engine shares lives in ``workflows/runtime``. Two module words are
exposed: "geoclaw" (shallow water) and "geoclaw_bouss" (the same rundata plus
Clawpack's BoussData keywords) - surge, friction, fgmax and the rest are data
groups on the same body.
"""

from __future__ import annotations

from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

from .geoclaw import GEO, GEOB
from ...runtime.module import Module, Slot, SlotRefused, load_module_input

__all__ = [
    "GEO", "GEOB", "Module", "Slot", "SlotRefused", "WRAPPERS", "load_module_input",
    "body_on", "wrapper_for",
]

WRAPPERS: Mapping[str, type] = MappingProxyType({"geoclaw": GEO, "geoclaw_bouss": GEOB})


def wrapper_for(module: str) -> type:
    """The wrapper for ``module``, or the refusal that names the exposed ones."""
    found = WRAPPERS.get(module)
    if found is None:
        raise SlotRefused(
            f"no wrapper for {module!r}; the exposed modules are "
            f"{sorted(WRAPPERS)}.")
    return found


@lru_cache(maxsize=None)
def body_on(body: type, module: str) -> type:
    """The body's assertions restated over the wrapper of ``module``: a template written over one module run on a sibling that has all its keywords."""
    if body.MODULE == module:
        return body
    return type(body)(body.__name__, (wrapper_for(module),), dict(body.ASSERTED))
