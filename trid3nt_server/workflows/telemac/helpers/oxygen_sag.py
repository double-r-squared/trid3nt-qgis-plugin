"""Streeter-Phelps (1925): the closed-form dissolved-oxygen deficit down a reach.

Pure arithmetic over a travel time. The WAQTEL O2 process reduces EXACTLY to
this ODE when the eutrophication and benthic sources are zeroed (photosynthesis
P, respiration R, benthic demand BEN = 0), nitrification is off (K4 = 0),
reaeration uses a constant k2 and saturation a constant Cs at 20 C: the source
term becomes ``dD/dt = k1 L - k2 D`` with ``dL/dt = -k1 L``, D = Cs - O2.
"""

from __future__ import annotations

import math

__all__ = ["critical_point", "do_profile"]

#: Seconds in a day: the rate constants are stated per day and the travel time
#: down a reach is in seconds.
_DAY_S = 86400.0


def do_profile(distance_m: list[float], velocity_mps: float,
               saturation_mgl: float, bod0_mgl: float, deficit0_mgl: float,
               k1_per_day: float, k2_per_day: float
               ) -> tuple[list[float], list[float]]:
    """DO(x) and deficit D(x) along a uniform reach at travel time ``t = x/U``.

    ``L(t) = L0 e^{-k1 t}``; ``D(t) = k1 L0/(k2-k1)(e^{-k1 t}-e^{-k2 t}) + D0
    e^{-k2 t}``; ``O2(t) = Cs - D(t)``, with the ``k1 == k2`` limit handled."""
    k1 = float(k1_per_day) / _DAY_S
    k2 = float(k2_per_day) / _DAY_S
    speed = max(float(velocity_mps), 1e-9)
    saturation = float(saturation_mgl)
    load = float(bod0_mgl)
    deficit = float(deficit0_mgl)
    oxygen_out: list[float] = []
    deficit_out: list[float] = []
    for distance in distance_m:
        travel = max(float(distance), 0.0) / speed
        if abs(k2 - k1) < 1e-12:
            here = (k1 * load * travel + deficit) * math.exp(-k1 * travel)
        else:
            here = (k1 * load / (k2 - k1)) * (
                math.exp(-k1 * travel) - math.exp(-k2 * travel)
            ) + deficit * math.exp(-k2 * travel)
        deficit_out.append(here)
        oxygen_out.append(saturation - here)
    return oxygen_out, deficit_out


def critical_point(velocity_mps: float, saturation_mgl: float, bod0_mgl: float,
                   deficit0_mgl: float, k1_per_day: float, k2_per_day: float
                   ) -> dict[str, float]:
    """Where the deficit is deepest: travel time, distance downstream, minimum DO.

    ``tc = 1/(k2-k1) ln[(k2/k1)(1 - D0(k2-k1)/(k1 L0))]``; ``Dc = (k1/k2) L0
    e^{-k1 tc}``; ``min DO = Cs - Dc``."""
    k1 = float(k1_per_day)
    k2 = float(k2_per_day)
    saturation = float(saturation_mgl)
    load = float(bod0_mgl)
    deficit = float(deficit0_mgl)
    if load <= 0.0:
        return dict(tc_day=0.0, xc_m=0.0, min_do_mgl=saturation - deficit,
                    max_deficit_mgl=deficit)
    if abs(k2 - k1) < 1e-9:
        tc_day = max(1.0 / k1 * (1.0 - deficit / load), 0.0)
    else:
        argument = (k2 / k1) * (1.0 - deficit * (k2 - k1) / (k1 * load))
        tc_day = max(math.log(argument) / (k2 - k1), 0.0) if argument > 0.0 else 0.0
    critical = (k1 / k2) * load * math.exp(-k1 * tc_day) if k2 > 0.0 else 0.0
    return dict(tc_day=tc_day, xc_m=float(velocity_mps) * tc_day * _DAY_S,
                min_do_mgl=saturation - critical, max_deficit_mgl=critical)
