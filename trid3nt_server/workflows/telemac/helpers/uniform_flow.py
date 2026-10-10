"""Uniform flow over a measured section: the depth a channel conveys a flow at.

One derivation, read at one discharge for a reach outflow or swept over a range for a catchment
outlet. It imports no gauge or datum; every input it cannot measure refuses by name.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence

__all__ = ["UniformFlowError", "derive_rating_curve", "normal_depth_stage"]


class UniformFlowError(RuntimeError):
    """A uniform-flow depth cannot be derived; carries an open-set ``error_code``."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# The friction laws a depth reads under: the exponent each puts on the hydraulic radius and whether its coefficient is a reciprocal conveyance. Strickler and Manning are one law; Chezy has the radius under a square root. Any other law refuses.
_CONVEYANCE: dict[int, tuple[str, float, bool]] = {
    2: ("Chezy", 0.5, False),
    3: ("Strickler", 2.0 / 3.0, False),
    4: ("Manning", 2.0 / 3.0, True),
}
# The stage bracket, as depths above the section's lowest painted node: search start, the floor never returned, and the depth past which the section is not a river reach.
_STAGE_SEED_M = 0.1
_STAGE_FLOOR_M = 1.0e-3
_STAGE_CEILING_M = 1000.0


def _wetted(section: Sequence[tuple[float, float]],
            stage: float) -> tuple[float, float]:
    """Wetted area and perimeter of the measured section at a water elevation.

    Panels are trapezoids cut at the waterline; the end points are vertical walls.
    """
    # Above the higher end point the section rises vertically, so a stage is defined everywhere and a flat face is a rectangle, not a division by zero.
    area = perimeter = 0.0
    for (o1, z1), (o2, z2) in zip(section, section[1:]):
        d1, d2 = stage - z1, stage - z2
        if d1 <= 0.0 and d2 <= 0.0:
            continue
        run = o2 - o1
        if d1 > 0.0 and d2 > 0.0:
            area += 0.5 * (d1 + d2) * run
            perimeter += math.hypot(run, z2 - z1)
            continue
        wet = run * (d1 / (d1 - d2) if d1 > 0.0 else d2 / (d2 - d1))
        depth = d1 if d1 > 0.0 else d2
        area += 0.5 * depth * wet
        perimeter += math.hypot(wet, depth)
    return area, perimeter + sum(max(stage - section[end][1], 0.0)
                                 for end in (0, -1))


def _conveyance(law: int, coefficient: float) -> tuple[str, float, float]:
    """``(law name, radius exponent, conveyance)`` for a friction law, or a refusal.

    Discharge is linear in conveyance, so callers multiply, never branch.
    """
    if law not in _CONVEYANCE:
        raise UniformFlowError(
            "TELEMAC_OUTFLOW_FRICTION_UNREADABLE",
            f"the steering file is written under bottom-friction law {law}, whose "
            f"coefficient is not a conveyance {sorted(_CONVEYANCE)} reads, so a "
            "uniform-flow depth cannot be derived under the roughness this run "
            "is actually solved at.")
    law_name, exponent, reciprocal = _CONVEYANCE[law]
    return law_name, exponent, (1.0 / coefficient if reciprocal else coefficient)


def _uniform_flow(section: Sequence[tuple[float, float]], *, law: int,
                  coefficient: float, slope: float) -> Callable[[float], float]:
    """The discharge this section conveys at a water elevation, under uniform flow.

    One closure, so solving and evaluating cannot spell conveyance differently.
    """
    _name, exponent, conveyance = _conveyance(law, coefficient)
    root_slope = math.sqrt(slope)

    def discharge(stage: float) -> float:
        area, perimeter = _wetted(section, stage)
        if area <= 0.0 or perimeter <= 0.0:
            return 0.0
        return conveyance * area * (area / perimeter) ** exponent * root_slope

    return discharge


def _stage_conveying(discharge: Callable[[float], float], thalweg: float,
                     q_m3s: float, *, slope: float) -> float:
    """The elevation at which ``discharge`` reaches ``q_m3s``, or a refusal.

    A channel needing a kilometre of water is not this discharge's channel.
    """
    from scipy.optimize import brentq

    top = thalweg + _STAGE_SEED_M
    while discharge(top) < q_m3s:
        top = thalweg + 2.0 * (top - thalweg)
        if top - thalweg > _STAGE_CEILING_M:
            raise UniformFlowError(
                "TELEMAC_OUTFLOW_STAGE_UNCONVEYABLE",
                f"the measured section conveys {q_m3s:g} m3/s only more than "
                f"{_STAGE_CEILING_M:g} m above its own bed at slope "
                f"{slope:.6g}; the discharge and the channel this run states "
                "describe different rivers.")
    return float(brentq(lambda s: discharge(s) - q_m3s,
                        thalweg + _STAGE_FLOOR_M, top, xtol=1.0e-4))


def normal_depth_stage(bed: Mapping[str, Any], *, law: int,
                       coefficient: float, discharge_q: float) -> dict[str, Any]:
    """The outflow stage as normal depth -> the elevation and what derived it.

    ``bed`` is the reach measured on the accepted mesh, at this deck's roughness.
    """
    section = [(float(o), float(z))
               for o, z in (bed.get("outflow_section") or ())]
    if len(section) < 2:
        raise UniformFlowError(
            "TELEMAC_OUTFLOW_SECTION_UNMEASURED",
            f"the outflow stage is a normal depth over the channel the outflow "
            f"face cuts, and the measured reach carries {len(section)} point(s) "
            "of that section.")
    length = float(bed.get("reach_length_m") or 0.0)
    drop = float(bed["bed_drop_m"])
    slope = drop / length if length > 0.0 else 0.0
    if slope <= 0.0:
        raise UniformFlowError(
            "TELEMAC_OUTFLOW_SLOPE_UNMEASURED",
            f"the friction slope is the measured fall {drop:.3f} m over the "
            f"measured reach length {length:.1f} m, which is {slope:.6g}; a reach "
            "that does not fall downstream has no uniform-flow depth, so its "
            "outflow level has to come from a gauge or a rating curve rather "
            "than from the reach itself.")
    if coefficient <= 0.0 or discharge_q <= 0.0:
        raise UniformFlowError(
            "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE",
            f"a normal depth needs a positive roughness and a positive "
            f"discharge; this file states law {law} coefficient "
            f"{coefficient:g} and {discharge_q:g} m3/s.")
    discharge = _uniform_flow(section, law=law, coefficient=coefficient,
                              slope=slope)
    law_name = _CONVEYANCE[law][0]
    thalweg = min(z for _offset, z in section)
    stage = _stage_conveying(discharge, thalweg, discharge_q, slope=slope)
    return {"stage_m": stage, "depth_m": stage - thalweg, "slope": slope,
            "drop_m": drop, "length_m": length, "law": law_name,
            "coefficient": coefficient, "q_m3s": discharge_q}


# Points on the rating curve, spaced evenly in discharge: the engine looks the curve up by discharge, so this bounds each interval's slope at the channel's dZ/dQ. Even stage spacing crushes the low end into an interval of centimetres per trickle (4.25 m per m3/s on one outlet), lifting water back into a catchment not yet running off.
_RATING_POINTS = 20


def derive_rating_curve(section: Sequence[tuple[float, float]], *, law: int,
                        coefficient: float, slope: float,
                        q_ceiling_m3s: float) -> dict[str, Any]:
    """The section's stage-discharge curve under uniform flow -> what derived it.

    Rows are ``(discharge, elevation)`` lowest first, the dry section at zero.
    """
    # The same normal-depth derivation as a reach's outflow stage, over a range; nothing is fitted. ``q_ceiling_m3s`` is the caller's to base: the curve is flat above it (the engine holds the last point), so a low ceiling caps the level rather than extrapolating.
    import numpy as np

    rows = [(float(o), float(z)) for o, z in section]
    if len(rows) < 2:
        raise UniformFlowError(
            "TELEMAC_OUTFLOW_SECTION_UNMEASURED",
            f"a rating curve is a uniform-flow depth over the channel the outlet "
            f"face cuts, and that face carries {len(rows)} painted point(s).")
    if slope <= 0.0 or coefficient <= 0.0 or q_ceiling_m3s <= 0.0:
        raise UniformFlowError(
            "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE",
            f"a rating curve needs a positive slope, roughness and flow range; "
            f"this outlet states slope {slope:.6g}, coefficient "
            f"{coefficient:g} and a ceiling of {q_ceiling_m3s:g} m3/s.")
    discharge = _uniform_flow(rows, law=law, coefficient=coefficient, slope=slope)
    thalweg = min(z for _offset, z in rows)
    stage_max = _stage_conveying(discharge, thalweg, q_ceiling_m3s, slope=slope)
    flows = np.linspace(0.0, float(q_ceiling_m3s), _RATING_POINTS)
    # The dry section carries no flow and is the level the engine holds the outlet at below the curve, so it is stated, not solved.
    return {
        "rows": [(round(float(q), 6),
                  round(thalweg if q <= 0.0 else
                        _stage_conveying(discharge, thalweg, float(q),
                                         slope=slope), 4))
                 for q in flows],
        "law": _CONVEYANCE[law][0], "coefficient": float(coefficient),
        "slope": float(slope), "thalweg_m": float(thalweg),
        "q_ceiling_m3s": float(q_ceiling_m3s), "stage_max_m": float(stage_max),
    }
