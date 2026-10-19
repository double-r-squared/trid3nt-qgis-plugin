"""One wrapper per GeoClaw body: the dictionary over Clawpack's own Data classes.

``module.py`` holds what a wrapper is; the slot, fill and sheet surface every
engine shares lives in ``workflows/runtime``. The one exposed module word is
"geoclaw" - surge, friction, fgmax and the rest are data groups on the same
body.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .geoclaw import GEO
from ...runtime.module import Module, Slot, SlotRefused, load_module_input

__all__ = [
    "GEO", "Module", "Slot", "SlotRefused", "WRAPPERS", "load_module_input",
    "wrapper_for",
]

WRAPPERS: Mapping[str, type] = MappingProxyType({"geoclaw": GEO})


def wrapper_for(module: str) -> type:
    """The wrapper for ``module``, or the refusal that names the exposed ones."""
    found = WRAPPERS.get(module)
    if found is None:
        raise SlotRefused(
            f"no wrapper for {module!r}; the exposed modules are "
            f"{sorted(WRAPPERS)}.")
    return found
