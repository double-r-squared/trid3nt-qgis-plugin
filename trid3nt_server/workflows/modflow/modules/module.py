"""The MODFLOW wrapper over the engine-neutral module surface.

Where the dictionary lives and which bodies the engine exposes; the fill holds
this dictionary, and mf6's own writer judges what the authoring writes.
"""

from __future__ import annotations

from pathlib import Path

from ...runtime.module import Module

__all__ = ["ModflowModule", "module_input_dir"]


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


class ModflowModule(Module):
    """The MODFLOW wrapper: its dictionary and the registry of exposed bodies."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)
