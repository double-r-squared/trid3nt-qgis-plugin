"""WAQTEL water-quality steps: the documented relations and the O2 process block.

Engine tier: the WAQTEL block is how TELEMAC is told what chemistry to run. The
two derivations above it are literature relations any dissolved-oxygen question
would use; they live beside their consumer until the shared water-quality front
exists.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.lib import Step

__all__ = ["WaqtelO2", "do_saturation_mgl", "upstream_do_mgl", "waqtel_o2_process"]

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.water_quality")

_RUNNER = "trid3nt_server.workflows.telemac.steps.water_quality.waqtel_o2_process"


def do_saturation_mgl(params: Any) -> float:
    """Freshwater DO saturation Cs (mg/L) from water temperature (Elmore-Hayes, 1 atm).

    A narrated literature relation, not a site value; ~9.0 mg/L at 20 C.
    """
    t = max(0.0, min(40.0, float(params.water_temp_c)))
    return round(14.652 - 0.41022 * t + 0.0079910 * t * t - 0.000077774 * t ** 3, 3)


def upstream_do_mgl(params: Any) -> float:
    """Inflow DO when none is supplied: a stream at saturation upstream of the sag."""
    return float(params.do_saturation_mgl)


def WaqtelO2(*, discharge_bod_mgl: Any, upstream_do_mgl: Any,  # noqa: N802
             do_saturation_mgl: Any, water_temp_c: Any, k1_per_day: Any,
             k2_per_day: Any, do_standard_mgl: Any) -> Step:
    """The declared WAQTEL O2 process block - resolved ONCE, read twice.

    The deck writer and the postprocess both need the same numbers, and the
    saturation clamp below must not be applied twice or to only one of them, so
    the block is a step whose result they both ``Ref``.
    """
    return Step(runner=_RUNNER, stage="prep", kwargs={
        "discharge_bod_mgl": discharge_bod_mgl,
        "upstream_do_mgl": upstream_do_mgl,
        "do_saturation_mgl": do_saturation_mgl,
        "water_temp_c": water_temp_c,
        "k1_per_day": k1_per_day,
        "k2_per_day": k2_per_day,
        "do_standard_mgl": do_standard_mgl,
    })


def waqtel_o2_process(*, discharge_bod_mgl: float, upstream_do_mgl: float,
                      do_saturation_mgl: float, water_temp_c: float,
                      k1_per_day: float, k2_per_day: float,
                      do_standard_mgl: float) -> dict[str, Any]:
    """The WAQTEL O2 configuration the deck and the postprocess share."""
    # DO cannot ride in above its own saturation - a physics coupling between two
    # params, so it cannot be a declared static bound.
    up_do = min(max(float(upstream_do_mgl), 0.0), float(do_saturation_mgl))
    if up_do != float(upstream_do_mgl):
        logger.info("waqtel o2: upstream_do_mgl %.3g pinned to saturation %.3g mg/L",
                    upstream_do_mgl, do_saturation_mgl)
    return {
        "bod_mgl": float(discharge_bod_mgl),
        "upstream_do_mgl": up_do,
        "saturation_mgl": float(do_saturation_mgl),
        "water_temp_c": float(water_temp_c),
        "k1_per_day": float(k1_per_day),
        "k2_per_day": float(k2_per_day),
        "k2_formula": 0,      # constant k2 (the S-P idealization; the user sets k2)
        "standard_mgl": float(do_standard_mgl),
    }
