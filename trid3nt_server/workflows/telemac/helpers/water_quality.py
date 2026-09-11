"""The two documented water-quality relations a declaration derives from: a
literature saturation, never a site value."""

from __future__ import annotations

from typing import Any

__all__ = ["do_saturation_mgl", "upstream_do_mgl"]


def do_saturation_mgl(params: Any) -> float:
    """Freshwater DO saturation Cs (mg/L) from water temperature (Elmore-Hayes, 1 atm).

    A literature relation, not a site value; ~9.0 mg/L at 20 C."""
    t = max(0.0, min(40.0, float(params.water_temp_c)))
    return round(14.652 - 0.41022 * t + 0.0079910 * t * t - 0.000077774 * t ** 3, 3)


def upstream_do_mgl(params: Any) -> float:
    """Inflow DO when none is supplied: a stream at saturation upstream of the discharge."""
    return float(params.do_saturation_mgl)
