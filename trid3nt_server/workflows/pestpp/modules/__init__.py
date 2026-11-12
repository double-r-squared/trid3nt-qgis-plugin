"""The PEST++ wrapper: one dictionary of the release's control options, read as the other engines' are.

``module.py`` holds what the wrapper is; the slot, fill and sheet surface every engine shares lives in
``workflows/runtime``. One module word is exposed, "pestpp": every tool of the release reads the one
control file, so its options are one surface and each row says which tools read it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .module import LOCKED, PESTPP, PestppModule, module_input_dir
from ...runtime.module import SlotRefused

__all__ = ["LOCKED", "PESTPP", "PestppModule", "WRAPPERS", "module_input_dir", "wrapper_for"]

WRAPPERS: Mapping[str, type] = MappingProxyType({"pestpp": PESTPP})


def wrapper_for(module: str) -> type:
    """The wrapper for ``module``, or the refusal that names the exposed one."""
    found = WRAPPERS.get(module)
    if found is None:
        raise SlotRefused(f"no wrapper for {module!r}; the exposed modules are {sorted(WRAPPERS)}.")
    return found
