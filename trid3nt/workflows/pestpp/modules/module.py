"""The PEST++ wrapper over the engine-neutral module surface: where its dictionary lives and what it locks.

A locked option is one the study's own files decide: the outputs are read from what PEST++ writes under
the value the study states, so another value breaks the read rather than changing the study.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ...runtime.module import Module, SlotRefused

__all__ = ["LOCKED", "PESTPP", "PestppModule", "module_input_dir"]

#: Each locked option -> why the study states it itself.
LOCKED: Mapping[str, str] = MappingProxyType({
    "save_binary": "the study's ensembles and Jacobian are read from the CSV files PEST++ writes when it is false",
    "ies_save_binary": "it is save_binary under another name, and the study's ensembles are read from the CSV "
                       "files PEST++ writes when that is false",
    "ies_csv_by_reals": "the parameter ensemble is read one realization per row, as PEST++ writes it when this "
                        "is true",
    "sweep_output_csv_file": "everything the manager writes is uploaded by the control file's own stem, pestpp, "
                             "so a sweep written under another name never leaves the box",
})


def module_input_dir() -> Path:
    """Where the committed module input lives."""
    return Path(__file__).resolve().parent / "module_input"


def _check(keyword: str, value: Any) -> str | None:
    """A locked option's refusal, else the dictionary row's own type and choices check."""
    why = LOCKED.get(str(keyword).lower())
    if why is not None:
        return f"{keyword} is the study's own to state: {why}."
    try:
        PESTPP.MODULE_INPUT[str(keyword).upper()].check(value)
    except SlotRefused as refused:
        return str(refused)
    return None


class PestppModule(Module):
    """The PEST++ wrapper: its dictionary, and the one check fill can make before the binary reads it."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def engine_check(cls, module: str) -> Callable[[str, Any], str | None]:
        """A locked option refuses naming why, and a value of another type than its row's refuses; the
        binary has no fill-time check, so what the row cannot judge is the binary's at launch."""
        return _check


PESTPP = PestppModule("pestpp")
