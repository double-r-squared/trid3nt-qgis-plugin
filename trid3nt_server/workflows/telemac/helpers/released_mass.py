"""What a finite release PUT IN: discharge x concentration x window, in kilograms.

The one relation a deposited, recovered or remaining fraction is measured
against, so no answer is held against an assumed load.
"""

from __future__ import annotations

from typing import Any

__all__ = ["injected_mass_kg", "released_mass_kg"]

#: mg/L -> kg/m3, the unit the mass is counted in.
_MGL_TO_KGM3 = 1.0e-3


def released_mass_kg(discharge_m3s: float, concentration_mgl: float,
                     duration_s: float) -> float:
    """The mass a release of this strength, at this rate, over this window put in.

    A negative concentration carries no mass; it never counts as a withdrawal."""
    return round(float(discharge_m3s)
                 * max(float(concentration_mgl) * _MGL_TO_KGM3, 0.0)
                 * float(duration_s), 3)


def injected_mass_kg(params: Any) -> float:
    """The mass a sediment pulse put in, off the three values the sheet states."""
    return released_mass_kg(params.source_q_m3s,
                            params.sediment_concentration_mgl,
                            params.spill_duration_s)
