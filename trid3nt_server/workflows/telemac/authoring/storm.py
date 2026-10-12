"""The measured storm's user Fortran, as content: the engine's own SCS runoff
routine reading the block hyetograph.

The engine fixes which rain definition the routine reads as a compile-time PARAMETER no keyword
reaches, so a run driven by a block file compiles this copy over the library's.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BLOCK_ROUTINE_FILENAME", "block_routine"]

# The engine's routine keeps its own file name: the copy replaces the library symbol of the same subroutine.
BLOCK_ROUTINE_FILENAME = "runoff_scs_cn.f"
# opentelemac v9.0.0's source with RAINDEF=3 (the block-type hyetograph branch) and no other change.
_ROUTINE = Path(__file__).resolve().parent / "storm_routine" / BLOCK_ROUTINE_FILENAME


def block_routine() -> str:
    """The run's ``runoff_scs_cn.f``: the routine that reads FORMATTED DATA FILE 1 as rain blocks."""
    return _ROUTINE.read_text(encoding="utf-8")
