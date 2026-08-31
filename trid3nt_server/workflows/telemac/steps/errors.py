"""Typed failures of the TELEMAC reach pipeline.

Each carries an open-set ``error_code`` the emitter renders as a typed error
frame. The RETRYABLE one is a GATE, not a failure: it carries ``.suggestions``
the adapter harvests off the raised exception so the model can retry with
corrected args, so it must PROPAGATE rather than flatten into an envelope.
"""

from __future__ import annotations

__all__ = [
    "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError",
    "ReachBanksUnmapped",
    "TelemacReleaseOutsideDomainError",
]


class TelemacDyeScenarioError(RuntimeError):
    """Base class for reach-pipeline failures; carries a typed ``error_code``."""

    error_code: str = "TELEMAC_DYE_SCENARIO_ERROR"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelemacDyeScenarioInputError(TelemacDyeScenarioError):
    """An input the pipeline cannot model: no AOI, or a malformed knob."""

    def __init__(self, message: str) -> None:
        super().__init__("TELEMAC_DYE_SCENARIO_INPUT_INVALID", message)


class ReachBanksUnmapped(TelemacDyeScenarioError):
    """No mapped water polygon covers this reach, so it has NO DOMAIN. TERMINAL.

    A reach domain is the real mapped water polygon or nothing: a line has no
    banks, so widening the flowline into a ribbon would answer a question about a
    shape nobody surveyed. There is no rung to retry with - the three ways a
    domain can be SUPPLIED are named in the message, and every one of them is an
    act outside this call.
    """

    def __init__(self) -> None:
        super().__init__(
            "REACH_BANKS_UNMAPPED",
            "No mapped water polygon covers this reach, so there is no domain to "
            "mesh. NHDArea maps water surfaces wide enough to have two banks; a "
            "narrow creek is a flowline only, and a flowline is a centreline "
            "rather than a shape. Draw or supply the reach polygon, name a case "
            "layer that holds it, or pick a reach with mapped water coverage. A "
            "stream this narrow may also be below the range where a 2D depth-"
            "averaged solve is the useful answer at all.",
        )


class TelemacReleaseOutsideDomainError(TelemacDyeScenarioError):
    """The supplied release point lies outside the domain polygon the run solves.

    Decided before anything is staged, against the mapped polygon itself: a point
    the domain does not contain is a release the run cannot put anywhere without
    moving it somewhere else, which would answer a different question. Retryable:
    the corrective args ride the tool-retry loop.
    """

    retryable = True

    def __init__(self, lon: float, lat: float,
                 distance_m: float | None = None) -> None:
        self.lon = float(lon)
        self.lat = float(lat)
        self.distance_m = float(distance_m) if distance_m is not None else None
        dist_txt = (f", {self.distance_m:.0f} m outside its nearest edge"
                    if self.distance_m is not None else "")
        super().__init__(
            "TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN",
            f"The release point ({self.lon:.5f}, {self.lat:.5f}) is not inside "
            f"the domain polygon this run solves over{dist_txt}. Nothing was "
            "relocated for you: releasing the substance somewhere else would "
            "answer a different question. Retry with a point INSIDE the modeled "
            "water body, widen the domain so the point falls in it, or omit "
            "release_coords to release at spill_fraction along the reach.",
        )
        self.suggestions = [  # type: ignore[attr-defined]
            "Retry with release_coords INSIDE the modeled water polygon (not on "
            "the bank, and not at a nearby gage).",
            "Or widen the domain - a longer reach, or a section cut that covers "
            "the point - so the release falls inside it.",
            "Or omit release_coords to release at spill_fraction along the reach.",
        ]
