"""The Streeter-Phelps closed form, drawn beside this question's solved profile.

WAQTEL's O2 process reduces EXACTLY to ``dD/dt = k1 L - k2 D`` with
``dL/dt = -k1 L`` (D = Cs - O2) when photosynthesis, respiration, benthic demand
and nitrification are zeroed and k2 and Cs are constant; the overlay anchors the
closed form at the load peak so it tests the kinetics rather than the mixing."""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from trid3nt_server.workflows.telemac.modules.outputs import (
    Line, Profile, reference_line,
)

__all__ = ["do_profile", "overlay"]

#: The rate constants are per day and the travel time down a reach is seconds.
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


#: The standard the sag is judged against, drawn flat across the reach.
_STANDARD = reference_line("do_standard_mgl", label="standard")


def overlay(*, saturation_mgl: float, k1_per_day: float, k2_per_day: float
            ) -> Callable[[Profile, Mapping[Any, Any], Mapping[str, Any]],
                          list[Line]]:
    """The chart's reference, closed over the kinetics the DECK states.

    The closed form grades the solve only while both hold the same rates and saturation, so it is drawn at the deck's own O2 keywords."""

    def lines(read: Profile, reads: Mapping[Any, Any],
              params: Mapping[str, Any]) -> list[Line]:
        """The lines drawn beside the solved oxygen profile: the organic load the
        run carried, the closed form from the modelled mix point at the solved
        along-reach speed, and the standard the sag is judged against."""
        x = [float(v) for v in read.distance_m]
        do = [float(v) for v in read.values]
        drawn = list(_STANDARD(read, reads, params))
        load = next((r for key, r in reads.items()
                     if key.kind == "profile" and key.variable == "T3"), None)
        if load is None or not len(load.values) or max(load.values) <= 0.0:
            return drawn
        bod = [float(v) for v in load.values]
        drawn.append(Line(label="organic load", x=x[:len(bod)], values=bod))
        velocity = read.measures.get("velocity_mps")
        anchor = max(range(len(bod)), key=bod.__getitem__)
        if not velocity or velocity <= 0.0 or anchor >= len(x) - 2:
            return drawn
        closed, _ = do_profile(
            [x[i] - x[anchor] for i in range(anchor, len(x))], velocity,
            saturation_mgl, bod[anchor], saturation_mgl - do[anchor],
            k1_per_day, k2_per_day)
        drawn.append(Line(label="Streeter-Phelps closed form", x=x[anchor:],
                          values=[float(v) for v in closed]))
        return drawn

    return lines
