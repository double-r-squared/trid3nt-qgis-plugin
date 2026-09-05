"""One wrapper per TELEMAC module: the catalog, the composites, the outputs.

The machinery is in ``module.py`` (what a slot and a wrapper are) and
``sheet.py`` (fill, then run). Every other file here is one module's wrapper.
"""

from __future__ import annotations

from .module import Composite, Module, Output, Slot, SlotRefused, load_catalog
from .sheet import Filled, Sheet, SheetIncomplete, fill, run
from .telemac2d import T2D

__all__ = [
    "Composite", "Filled", "Module", "Output", "Sheet", "SheetIncomplete",
    "Slot", "SlotRefused", "T2D", "fill", "load_catalog", "run",
]
