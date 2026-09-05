"""One wrapper per TELEMAC module: the catalog, the composites, the outputs.

The machinery is in ``module.py`` (what a slot and a wrapper are) and
``sheet.py`` (fill, then run). Every other file here is one module's wrapper.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .module import Composite, Module, Output, Slot, SlotRefused, load_catalog
from .sheet import Filled, Sheet, SheetIncomplete, draw, fill, run
from .gaia import GAIA
from .telemac2d import T2D
from .waqtel import WAQTEL

__all__ = [
    "Composite", "Filled", "GAIA", "Module", "Output", "Sheet",
    "SheetIncomplete", "Slot", "SlotRefused", "T2D", "WAQTEL", "WRAPPERS",
    "draw", "fill", "load_catalog", "run", "wrapper_for",
]

#: The exposed wrappers, by the module name the engine knows each by. A coupled
#: body names its module rather than carrying its wrapper, so this is where the
#: serializer turns that name back into the catalog its slots are checked
#: against.
WRAPPERS: Mapping[str, type] = MappingProxyType({
    "telemac2d": T2D, "waqtel": WAQTEL, "gaia": GAIA})


def wrapper_for(module: str) -> type:
    """The wrapper for ``module``, or the refusal that names the exposed ones."""
    found = WRAPPERS.get(module)
    if found is None:
        raise SlotRefused(
            f"no wrapper for {module!r}; the exposed modules are "
            f"{sorted(WRAPPERS)}.")
    return found
