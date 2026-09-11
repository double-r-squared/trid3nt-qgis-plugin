"""What the pulse PUT IN: discharge x concentration x window, in kilograms.

The deposited fraction is measured against this, never against an assumed load."""

from __future__ import annotations

from typing import Any

__all__ = ["injected_mass_kg"]

#: mg/L -> kg/m3, the unit the mass is counted in.
_MGL_TO_KGM3 = 1.0e-3


def injected_mass_kg(params: Any) -> float:
    """The mass the finite pulse released, from the three sheet values that state it."""
    return round(float(params.source_q_m3s)
                 * max(float(params.sediment_concentration_mgl) * _MGL_TO_KGM3, 0.0)
                 * float(params.spill_duration_s), 3)
