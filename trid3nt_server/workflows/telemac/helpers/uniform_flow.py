"""Uniform flow over a measured section: the depth a channel conveys a flow at.

ONE derivation, read two ways. A reach's outflow level is the depth at which the
section its outflow face cuts conveys the discharge the deck prescribes upstream:
that is the community default wherever the bed came from a surface rather than a
survey, because it imports no gauge, no rating curve and no datum of its own. A
catchment's outlet reads the SAME derivation swept over a range of discharges, so
the level there rises and falls with the hydrograph.

It is not a measured boundary and never claims to be. Uniform flow is a fiction
near a structure, a confluence or a tidal cap, and the honest use of it is an
outlet far enough downstream of the interest to be numerically convenient rather
than physically load-bearing.

Every input it cannot measure REFUSES by name: a reach with no fall has no
uniform-flow depth at all, and defaulting past that would put a made-up level on
the one boundary the run's water surface is anchored to.
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


#: The friction laws a uniform-flow depth reads under, as the exponent each puts
#: on the hydraulic radius and whether its coefficient is a RECIPROCAL
#: conveyance. Strickler and Manning are one law through reciprocal coefficients;
#: Chezy is its own, with the radius under a square root. A law outside these
#: states a coefficient no conveyance here can read, and the stage refuses rather
#: than being derived under a law nobody wrote.
_CONVEYANCE: dict[int, tuple[str, float, bool]] = {
    2: ("Chezy", 0.5, False),
    3: ("Strickler", 2.0 / 3.0, False),
    4: ("Manning", 2.0 / 3.0, True),
}
#: The bracket the stage is found inside, as depths above the section's lowest
#: painted node: where the search starts, the floor it never returns, and the
#: depth past which a section carrying this discharge is not a river reach.
_STAGE_SEED_M = 0.1
_STAGE_FLOOR_M = 1.0e-3
_STAGE_CEILING_M = 1000.0


def _wetted(section: Sequence[tuple[float, float]],
            stage: float) -> tuple[float, float]:
    """Wetted area and perimeter of the measured section at a water elevation.

    Each panel between two surveyed points is a trapezoid, cut at the waterline
    where only one of its ends is under water. The two END points are the
    section's walls: above the higher of them the section rises vertically rather
    than spreading into ground the mesh does not hold, so a stage is defined
    everywhere and a flat face is a rectangle rather than a division by zero.
    """
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
    """``(law name, radius exponent, conveyance)`` for a friction law -> refuses.

    The conveyance is the coefficient the discharge is LINEAR in, which is the
    coefficient itself under Strickler and Chezy and its reciprocal under
    Manning; every caller multiplies by it rather than branching on the law.
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

    One closure, so the stage a discharge is solved for and the discharge a stage
    is evaluated at cannot come from two spellings of the same conveyance.
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
    """The elevation at which ``discharge`` reaches ``q_m3s`` -> refuses.

    The bracket opens a decimetre over the section's lowest painted node and
    doubles until the section carries the flow; a channel that needs a kilometre
    of water to convey it is not the channel this discharge belongs to.
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
    """The outflow stage as NORMAL DEPTH -> the elevation and what derived it.

    ``bed`` is the reach MEASURED on the accepted mesh: the friction slope is the
    fall it carries between its two role faces over the length of the line it was
    built on, and the channel is the section its outflow face cuts through the
    painted bed. The roughness and the discharge are the ones THIS deck writes.
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


#: How many points the derived rating curve carries, spaced uniformly in
#: DISCHARGE from nothing up to the range's ceiling. Discharge is what the engine
#: LOOKS the curve up by, so even spacing there bounds the slope of every
#: interval at the physical dZ/dQ of the channel. Spacing them evenly in stage
#: instead crushes the low-flow end into a first interval carrying almost no
#: discharge and several centimetres of stage - measured on the Coweeta outlet,
#: 4.25 m of level per m3/s - and a boundary that swings metres on a trickle
#: lifts water back into a catchment that has not started running off yet.
_RATING_POINTS = 20


def derive_rating_curve(section: Sequence[tuple[float, float]], *, law: int,
                        coefficient: float, slope: float,
                        q_ceiling_m3s: float) -> dict[str, Any]:
    """The section's stage-discharge curve under uniform flow -> what derived it.

    The SAME normal-depth derivation the outflow stage of a reach is, evaluated
    over a range of discharges instead of at one: at each stage the section
    conveys a discharge, and the pairs are the rating curve the engine reads a
    level off. It imports no gauge - a gauged curve is the calibration-era swap
    through this same keyword - and nothing here is fitted.

    ``q_ceiling_m3s`` is the top of the range, and its BASIS is the caller's to
    state; the curve is flat above it because the engine holds the last point,
    so a ceiling below the flow that arrives caps the level rather than
    extrapolating a channel nobody measured.

    Returns the rows as ``(discharge, elevation)`` pairs, lowest first, with the
    dry section at zero discharge as the first one.
    """
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
    # The dry section carries no flow, and it is the level the engine holds the
    # outlet at below the curve, so it is stated rather than solved for.
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
