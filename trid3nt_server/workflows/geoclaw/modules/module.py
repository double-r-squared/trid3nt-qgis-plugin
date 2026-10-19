"""The GeoClaw wrapper over the engine-neutral module surface.

Where the dictionary lives and the one body the engine exposes; the fill holds
this dictionary, and Clawpack's own writer judges what the authoring writes
(setrun's Data classes). One module word, "geoclaw": every rundata group is a
keyword prefix on the same body, and what a solved run writes is a later leg's
to pin.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import Module

__all__ = ["GeoclawModule", "module_input_dir"]


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


class GeoclawModule(Module):
    """The GeoClaw wrapper: its dictionary and the registry of the one exposed body."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        """The filled sheet a body's slots make: the generic sheet, held immutable.

        Nothing is armed or written into files here - the authoring that turns
        slots into setrun values and Clawpack's .data files is a later leg's."""
        from ...runtime.sheet import Sheet

        return Sheet(body=cls, filled=MappingProxyType(filled),
                     files=MappingProxyType(files),
                     run=MappingProxyType(run))
