"""The ice module's solar clock as user Fortran: the engine's own thermal
module with the solar hour folded onto one day.

The engine tests the solar hour against sunrise and sunset without folding it,
so a run whose clock opens at a UTC hour reads every hour past 24, or below 0,
as night. A run that carries a moment compiles this copy over the library's.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["SOLAR_ROUTINE_FILENAME", "solar_routine"]

# The engine's file keeps its own name: the copy replaces the library module of the same name.
SOLAR_ROUTINE_FILENAME = "thermal_khione.f"
# opentelemac v9.0.0's source with two lines of SUBROUTINE SOLAR changed and no other:
#   HS1 = MODULO(T1 - ETADIR / 15.D0 * (ALSM - ALLM) + ET, 24.D0)
#   HS2 = HS1 + (T2 - T1)
# HS2 rides on the folded HS1 because the two are one interval: folded apart, a step across midnight has its end before its start.
_ROUTINE = Path(__file__).resolve().parent / "solar_routine" / SOLAR_ROUTINE_FILENAME


def solar_routine() -> str:
    """The run's ``thermal_khione.f``: the thermal budget whose sun rises on the domain's own day."""
    return _ROUTINE.read_text(encoding="utf-8")
