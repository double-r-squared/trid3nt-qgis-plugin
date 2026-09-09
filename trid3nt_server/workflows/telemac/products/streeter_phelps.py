"""Streeter-Phelps (1925) closed-form dissolved-oxygen sag - the WAQTEL O2 V&V.

The deterministic analytical reference the DO-sag template overlays against its
computed profile. Pure arithmetic - no solver, no I/O."""

# The WAQTEL O2 module (WATER QUALITY PROCESS = 2) reduces EXACTLY to this ODE
# when the eutrophication and benthic sources are zeroed (photosynthesis P,
# respiration R, benthic demand BEN = 0), nitrification is off (K4 = 0),
# reaeration uses a constant k2 (FORMULA FOR COMPUTING K2 = 0) and saturation a
# constant Cs (FORMULA FOR COMPUTING CS = 0) at 20 C: the O2 source term becomes
# ``dD/dt = k1 L - k2 D`` with ``dL/dt = -k1 L``, where D = Cs - O2 and L = CBOD.
from __future__ import annotations

import math

__all__ = ["sp_do_profile", "sp_critical_point"]


def sp_do_profile(
    distance_m: list[float],
    velocity_mps: float,
    saturation_mgl: float,
    bod0_mgl: float,
    deficit0_mgl: float,
    k1_per_day: float,
    k2_per_day: float,
) -> tuple[list[float], list[float]]:
    """DO(x) and deficit D(x) along a uniform reach (travel time ``t = x/U``).

    ``(do_mgl, deficit_mgl)`` on ``distance_m`` from the mix point; k per day."""
    # ``L(t) = L0 e^{-k1 t}``; ``D(t) = k1 L0/(k2-k1)(e^{-k1 t}-e^{-k2 t}) + D0
    # e^{-k2 t}``; ``O2(t) = Cs - D(t)``, with the ``k1 == k2`` limit handled.
    k1 = float(k1_per_day) / 86400.0
    k2 = float(k2_per_day) / 86400.0
    U = max(float(velocity_mps), 1e-9)
    Cs = float(saturation_mgl)
    L0 = float(bod0_mgl)
    D0 = float(deficit0_mgl)
    do_out: list[float] = []
    d_out: list[float] = []
    for x in distance_m:
        t = max(float(x), 0.0) / U
        if abs(k2 - k1) < 1e-12:
            D = (k1 * L0 * t + D0) * math.exp(-k1 * t)
        else:
            D = (k1 * L0 / (k2 - k1)) * (math.exp(-k1 * t) - math.exp(-k2 * t)) \
                + D0 * math.exp(-k2 * t)
        d_out.append(D)
        do_out.append(Cs - D)
    return do_out, d_out


def sp_critical_point(
    velocity_mps: float,
    saturation_mgl: float,
    bod0_mgl: float,
    deficit0_mgl: float,
    k1_per_day: float,
    k2_per_day: float,
) -> dict[str, float]:
    """Critical (sag) travel time, downstream distance, and minimum DO.

    Returns ``tc_day``, ``xc_m``, ``min_do_mgl`` and ``max_deficit_mgl``."""
    # ``tc = 1/(k2-k1) ln[(k2/k1)(1 - D0(k2-k1)/(k1 L0))]``;
    # ``Dc = (k1/k2) L0 e^{-k1 tc}``; ``min DO = Cs - Dc``.
    k1 = float(k1_per_day)
    k2 = float(k2_per_day)
    Cs = float(saturation_mgl)
    L0 = float(bod0_mgl)
    D0 = float(deficit0_mgl)
    if L0 <= 0.0:
        return dict(tc_day=0.0, xc_m=0.0, min_do_mgl=Cs - D0, max_deficit_mgl=D0)
    if abs(k2 - k1) < 1e-9:
        tc_day = max(1.0 / k1 * (1.0 - D0 / L0), 0.0)
    else:
        arg = (k2 / k1) * (1.0 - D0 * (k2 - k1) / (k1 * L0))
        tc_day = max(math.log(arg) / (k2 - k1), 0.0) if arg > 0.0 else 0.0
    Dc = (k1 / k2) * L0 * math.exp(-k1 * tc_day) if k2 > 0.0 else 0.0
    return dict(
        tc_day=tc_day,
        xc_m=float(velocity_mps) * tc_day * 86400.0,
        min_do_mgl=Cs - Dc,
        max_deficit_mgl=Dc,
    )
