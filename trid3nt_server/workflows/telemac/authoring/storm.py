"""The measured storm's user Fortran, as content: the engine's own runoff routine
of the deck's rainfall-runoff model, reading the block hyetograph.

The engine fixes which rain definition each runoff routine reads as a compile-time
PARAMETER no keyword reaches, so a run driven by a block file compiles its model's
copy over the library's.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BLOCK_ROUTINE_FILENAME", "block_routine"]

# Each routine keeps the engine's own file name: the copy replaces the library symbol of the same subroutine. Each is
# opentelemac v9.0.0's source with RAINDEF=3 (the block-type hyetograph branch) and no other change.
BLOCK_ROUTINE_FILENAME = "runoff_scs_cn.f"
# By RAINFALL-RUNOFF MODEL. Every model but Green-Ampt keeps the SCS routine the block file was first written for.
_ROUTINES = {1: BLOCK_ROUTINE_FILENAME, 3: "runoff_greenampt.f"}
_DIR = Path(__file__).resolve().parent / "storm_routine"


def block_routine(model: int | None = None) -> tuple[str, str]:
    """The run's routine for RAINFALL-RUNOFF MODEL ``model`` -> ``(file name, text)``: the one reading FORMATTED DATA FILE 1 as rain blocks."""
    name = _ROUTINES.get(int(model) if model is not None else 1, BLOCK_ROUTINE_FILENAME)
    return name, (_DIR / name).read_text(encoding="utf-8")
