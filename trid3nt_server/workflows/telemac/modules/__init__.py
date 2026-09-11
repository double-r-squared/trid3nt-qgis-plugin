"""One wrapper per TELEMAC module: the dictionary, the composites, the outputs.

The machinery is in ``module.py`` (what a slot and a wrapper are), ``sheet.py``
(fill, then run) and ``outputs.py`` (the primitive set and the read of each).
Every other file here is one module's wrapper."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .module import Composite, Module, Output, Slot, SlotRefused, load_dictionary
from .outputs import (
    Measure,
    Primitive,
    extent,
    field,
    mass_balance,
    max_over_time,
    mesh,
    series,
)
from .sheet import Filled, Sheet, SheetIncomplete, draw, fill, run
from .artemis import ART
from .gaia import GAIA
from .telemac2d import T2D
from .telemac3d import T3D
from .waqtel import WAQTEL

__all__ = [
    "ART", "Composite", "Filled", "GAIA", "Measure", "Module", "Output",
    "Primitive", "Sheet", "SheetIncomplete", "Slot", "SlotRefused", "T2D", "T3D",
    "WAQTEL", "WRAPPERS", "draw", "extent", "field", "fill", "load_dictionary",
    "mass_balance", "max_over_time", "mesh", "run", "series", "wrapper_for",
]

#: The exposed wrappers, by the module name the engine knows each by. A coupled
#: body names its module rather than carrying its wrapper, so this is where the
#: serializer turns that name back into the dictionary its slots are checked
#: against.
WRAPPERS: Mapping[str, type] = MappingProxyType({
    "artemis": ART, "telemac2d": T2D, "telemac3d": T3D, "waqtel": WAQTEL,
    "gaia": GAIA})


def wrapper_for(module: str) -> type:
    """The wrapper for ``module``, or the refusal that names the exposed ones."""
    found = WRAPPERS.get(module)
    if found is None:
        raise SlotRefused(
            f"no wrapper for {module!r}; the exposed modules are "
            f"{sorted(WRAPPERS)}.")
    return found
