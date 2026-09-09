"""WAQTEL water quality: the documented relations and the O2 process block.

The WAQTEL block is how TELEMAC is told what chemistry to run; the two
derivations above it are literature relations, never site values."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.runtime import Step

__all__ = ["WaqtelO2", "do_saturation_mgl", "upstream_do_mgl", "waqtel_o2_process"]

logger = logging.getLogger("trid3nt_server.workflows.telemac.helpers.water_quality")

_RUNNER = "trid3nt_server.workflows.telemac.helpers.water_quality.waqtel_o2_process"


def do_saturation_mgl(params: Any) -> float:
    """Freshwater DO saturation Cs (mg/L) from water temperature (Elmore-Hayes, 1 atm).

    A literature relation, not a site value; ~9.0 mg/L at 20 C."""
    t = max(0.0, min(40.0, float(params.water_temp_c)))
    return round(14.652 - 0.41022 * t + 0.0079910 * t * t - 0.000077774 * t ** 3, 3)


def upstream_do_mgl(params: Any) -> float:
    """Inflow DO when none is supplied: a stream at saturation upstream of the sag."""
    return float(params.do_saturation_mgl)


def WaqtelO2(*, effluent_bod_mgl: Any, effluent_q_m3s: Any,  # noqa: N802
             effluent_do_mgl: Any, upstream_do_mgl: Any,
             do_saturation_mgl: Any, water_temp_c: Any, k1_per_day: Any,
             k2_per_day: Any, do_standard_mgl: Any) -> Step:
    """The declared WAQTEL O2 process block - resolved ONCE, read twice.

    The saturation clamp must not be applied twice or to only one reader."""
    return Step(runner=_RUNNER, stage="prep", kwargs={
        "effluent_bod_mgl": effluent_bod_mgl,
        "effluent_q_m3s": effluent_q_m3s,
        "effluent_do_mgl": effluent_do_mgl,
        "upstream_do_mgl": upstream_do_mgl,
        "do_saturation_mgl": do_saturation_mgl,
        "water_temp_c": water_temp_c,
        "k1_per_day": k1_per_day,
        "k2_per_day": k2_per_day,
        "do_standard_mgl": do_standard_mgl,
    })


def waqtel_o2_process(*, effluent_bod_mgl: float, effluent_q_m3s: float,
                      effluent_do_mgl: float, upstream_do_mgl: float,
                      do_saturation_mgl: float, water_temp_c: float,
                      k1_per_day: float, k2_per_day: float,
                      do_standard_mgl: float) -> dict[str, Any]:
    """The WAQTEL O2 configuration the author and the postprocess share.

    The effluent trio is the outfall itself; the river above it is clean."""
    # DO cannot ride in above its own saturation - a physics coupling between two
    # params, so it cannot be a declared static bound. The same ceiling holds for
    # the effluent: a discharge cannot carry more oxygen than the water can hold.
    sat = float(do_saturation_mgl)
    up_do = min(max(float(upstream_do_mgl), 0.0), sat)
    if up_do != float(upstream_do_mgl):
        logger.info("waqtel o2: upstream_do_mgl %.3g pinned to saturation %.3g mg/L",
                    upstream_do_mgl, do_saturation_mgl)
    eff_do = min(max(float(effluent_do_mgl), 0.0), sat)
    return {
        "effluent_bod_mgl": float(effluent_bod_mgl),
        "effluent_q_m3s": float(effluent_q_m3s),
        "effluent_do_mgl": eff_do,
        "upstream_do_mgl": up_do,
        "saturation_mgl": sat,
        "water_temp_c": float(water_temp_c),
        "k1_per_day": float(k1_per_day),
        "k2_per_day": float(k2_per_day),
        "k2_formula": 0,      # constant k2 (the S-P idealization; the user sets k2)
        "standard_mgl": float(do_standard_mgl),
    }
