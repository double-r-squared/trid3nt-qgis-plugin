"""Typed failures of the TELEMAC reach pipeline.

Each carries an open-set ``error_code`` the emitter renders as a typed error
frame. The two RETRYABLE ones are GATES, not failures: they carry
``.suggestions`` the adapter harvests off the raised exception so the model can
retry with corrected args, so they must PROPAGATE rather than flatten into an
envelope.
"""

from __future__ import annotations

__all__ = [
    "TelemacBanksUnavailableError",
    "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError",
    "TelemacReachDegenerateError",
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


class TelemacBanksUnavailableError(TelemacDyeScenarioError):
    """``bank_source="nhd_area"`` found no NHDArea coverage for the reach.

    No inexplicit mesh-source fallback: the constant-width ribbon is never
    substituted for missing real banks. Retryable so the named retry
    (``bank_source="constant_ribbon"``) rides the tool-retry loop and the user
    approves the substitution conversationally.
    """

    retryable = True

    def __init__(self, assumed_channel_width_m: float | None) -> None:
        self.assumed_channel_width_m = (
            float(assumed_channel_width_m)
            if assumed_channel_width_m is not None
            else None
        )
        width_txt = (
            f"an assumed constant {self.assumed_channel_width_m:g} m channel-width "
            "ribbon"
            if self.assumed_channel_width_m is not None
            else "an assumed constant channel-width ribbon"
        )
        super().__init__(
            "TELEMAC_BANKS_UNAVAILABLE",
            "No USGS NHDArea water polygon covers this river reach, so real "
            'per-station banks could not be sampled for bank_source="nhd_area". '
            "No bank geometry was substituted automatically -- switching to an "
            "assumed channel width is a user decision. Retry with "
            f'bank_source="constant_ribbon" to mesh {width_txt} instead, or name a '
            "reach with mapped NHDArea coverage.",
        )
        self.suggestions = [  # type: ignore[attr-defined]
            'Retry with bank_source="constant_ribbon" to mesh '
            + width_txt
            + " (an assumed width, not real surveyed banks).",
            "Or name a larger/mapped river reach that has USGS NHDArea coverage.",
        ]


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


class TelemacReachDegenerateError(TelemacDyeScenarioError):
    """The channel is wider than the reach is long, so the mesher would busy-loop.

    The worker gates this BEFORE meshing (never a hang); retryable so the
    corrective args ride the tool-retry loop.
    """

    retryable = True

    def __init__(
        self,
        reach_length_m: float | None = None,
        channel_width_m: float | None = None,
    ) -> None:
        self.reach_length_m = (
            float(reach_length_m) if reach_length_m is not None else None
        )
        self.channel_width_m = (
            float(channel_width_m) if channel_width_m is not None else None
        )
        geom_txt = (
            f" (a {self.reach_length_m:.0f} m reach with a "
            f"{self.channel_width_m:.0f} m channel width)"
            if self.reach_length_m is not None
            and self.channel_width_m is not None
            else ""
        )
        super().__init__(
            "TELEMAC_REACH_DEGENERATE",
            "The reach geometry is degenerate: the channel is wider than the "
            f"reach is long{geom_txt}, so the mesh could not be built. Retry "
            "with a longer reach_length_km, a location that NAMES the river "
            "(the seed ladder re-seeds onto the named mainstem instead of a "
            "short tributary stub), or bank_source=\"constant_ribbon\" with a "
            "smaller channel_width_m.",
        )
        self.suggestions = [  # type: ignore[attr-defined]
            "Retry with a longer reach_length_km (mesh more of the river).",
            "Give a location that NAMES the river (\"Eel River near Scotia, "
            "California\") so the seed ladder lands on the named mainstem "
            "rather than a short tributary stub.",
            'Retry with bank_source="constant_ribbon" and a smaller '
            "channel_width_m.",
        ]
