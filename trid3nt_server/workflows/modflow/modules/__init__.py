"""One wrapper per MODFLOW body: the dictionary and what a solved run writes.

``module.py`` holds what a wrapper is; the slot, fill and sheet surface every
engine shares lives in ``workflows/runtime``. Every other file is one body's
wrapper.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .gwf import COUPLED_BODIES, GWF
from .gwe import GWE
from .gwt import GWT
from ...runtime.module import Composite, Module, Output, Slot, SlotRefused, load_module_input

__all__ = [
    "COUPLED_BODIES",
    "GWF", "GWE", "GWT", "Composite", "Module", "Output", "Slot", "SlotRefused",
    "WRAPPERS", "load_module_input", "wrapper_for",
]

# The exposed wrappers by engine body name; a coupled body's module name resolves to the dictionary its slots are checked against.
WRAPPERS: Mapping[str, type] = MappingProxyType({
    "gwf": GWF, "gwt": GWT, "gwe": GWE})


def wrapper_for(module: str) -> type:
    """The wrapper for ``module``, or the refusal that names the exposed ones."""
    found = WRAPPERS.get(module)
    if found is None:
        raise SlotRefused(
            f"no wrapper for {module!r}; the exposed modules are "
            f"{sorted(WRAPPERS)}.")
    return found
