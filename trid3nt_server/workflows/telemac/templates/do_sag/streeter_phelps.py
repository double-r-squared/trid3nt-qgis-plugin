"""The analytic reference this question's chart draws beside its solved profile.

The closed form itself is the pure relation in ``helpers/oxygen_sag``; what is
here is the one thing that is this template's: which lines ride the oxygen
profile, and where the closed form is anchored so it tests the kinetics rather
than the mixing."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from trid3nt_server.workflows.telemac.helpers.oxygen_sag import do_profile
from trid3nt_server.workflows.telemac.modules.outputs import Line, Profile

__all__ = ["overlay"]


def overlay(*, saturation_mgl: float, k1_per_day: float, k2_per_day: float
            ) -> Callable[[Profile, Mapping[Any, Any], Mapping[str, Any]],
                          list[Line]]:
    """The chart's reference, closed over the kinetics the DECK states.

    The closed form grades the solve only while the two hold the same rates and
    the same saturation, so the curve is drawn at the deck's own O2 keywords
    rather than at a second declaration of them."""

    def lines(read: Profile, reads: Mapping[Any, Any],
              params: Mapping[str, Any]) -> list[Line]:
        """The lines drawn beside the solved oxygen profile: the organic load the
        run carried, the closed form from the modelled mix point at the solved
        along-reach speed, and the standard the sag is judged against."""
        x = [float(v) for v in read.distance_m]
        do = [float(v) for v in read.values]
        standard = float(params["do_standard_mgl"])
        drawn = [Line(label=f"{standard:g} {read.units} standard",
                      x=[x[0], x[-1]], values=[standard, standard])]
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
