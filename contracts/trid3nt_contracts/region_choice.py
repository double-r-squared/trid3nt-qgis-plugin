"""The region-narrowing picker: a request that pauses a turn, and its reply.

A vague regional query that has no precise match snaps to the whole state, and
that snapped bbox stays the DEFAULT - a headless run proceeds on it unchanged.
These envelopes add an interactive narrowing on top, correlated by an
unguessable ``request_id``, fail-open: no answer keeps the honest default.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .common import (
    BBox,
    GraceModel,
    ULIDStr,
)

__all__ = [
    "RegionAdminLevel",
    "RegionCandidate",
    "RegionChoiceRequestEnvelopePayload",
    "RegionChoiceProvidedEnvelopePayload",
    "REGION_CHOICE_PAYLOADS",
    "REGION_CHOICE_CLIENT_TO_AGENT_PAYLOADS",
    "REGION_CHOICE_AGENT_TO_CLIENT_PAYLOADS",
]


# --------------------------------------------------------------------------- #
# Admin-level vocabulary - a closed Literal
# --------------------------------------------------------------------------- #

# The granularity a region candidate is drawn at. The Literal is CLOSED because
# each level has its own fetch plumbing: admitting an unknown level here would
# let a request carry a granularity nothing can actually produce. A coarser or
# finer level is an explicit amendment, never an open enum.
RegionAdminLevel = Literal[
    "county",
]


# --------------------------------------------------------------------------- #
# RegionCandidate - one selectable sub-region
# --------------------------------------------------------------------------- #


class RegionCandidate(GraceModel):
    """A single selectable sub-region of the snapped state.
    One per administrative feature within the detected state."""

    #: Stable across requests for the same state, and echoed VERBATIM by the
    #: reply. Free-form: the schema constrains length, not format.
    region_id: str = Field(min_length=1, max_length=120)
    #: Human label for the card and the map tooltip.
    name: str = Field(min_length=1, max_length=200)
    #: EPSG:4326 total bounds of the region polygon - the bbox the workflow
    #: continues with when this candidate is picked.
    bbox: BBox
    admin_level: RegionAdminLevel = "county"


# --------------------------------------------------------------------------- #
# RegionChoiceRequest - server -> client
# --------------------------------------------------------------------------- #


class RegionChoiceRequestEnvelopePayload(GraceModel):
    """``region-choice-request``: server -> client, narrow the snapped region.
    The turn PAUSES on this and FAILS OPEN - on timeout or with no interactive
    client the whole-state bbox, already resolved, is used unchanged.
    """

    MESSAGE_TYPE: ClassVar[str] = "region-choice-request"

    envelope_type: Literal["region-choice-request"] = "region-choice-request"
    #: Correlates request, reply and the paused turn. Echoed VERBATIM.
    request_id: ULIDStr
    state_name: str = Field(min_length=1, max_length=120)
    #: The 2-letter code, so a client labels without a name -> code table.
    state_code: str = Field(min_length=1, max_length=2)
    #: The bbox the geocode snapped to, and the one used if the pick declines.
    state_bbox: BBox
    #: May be EMPTY when the region set could not be built; the client then
    #: offers only the whole-state default rather than an invented list.
    candidates: list[RegionCandidate] = Field(default_factory=list)
    #: A closed Literal, so the fail-open behaviour is explicit rather than
    #: implied by an absent field.
    default_action: Literal["use_whole_state"] = "use_whole_state"
    #: The user-facing prompt. It MUST say the snap to the whole state happened
    #: and that a narrower pick is on offer - never a silent widening.
    message: str = Field(min_length=1, max_length=1024)


# --------------------------------------------------------------------------- #
# RegionChoiceProvided - client -> server
# --------------------------------------------------------------------------- #


class RegionChoiceProvidedEnvelopePayload(GraceModel):
    """``region-choice-provided``: client -> server, the user's pick.
    Resumes the paused turn. No timeout or cancel field: a ``whole_state`` reply
    IS the decline path, and a hard cancel has its own message."""

    MESSAGE_TYPE: ClassVar[str] = "region-choice-provided"

    envelope_type: Literal["region-choice-provided"] = "region-choice-provided"
    #: Resolves the exact paused turn to resume.
    request_id: ULIDStr
    choice: Literal["region", "whole_state"]
    #: Set when ``choice == "region"``. AUTHORITATIVE: the candidate's bbox is
    #: re-resolved from this id, so a tampered bbox cannot redirect the run.
    selected_region_id: str | None = Field(default=None, max_length=120)
    #: The echoed bbox, used only as the fallback when the id cannot be
    #: re-resolved - never in preference to it.
    selected_bbox: BBox | None = None


# --------------------------------------------------------------------------- #
# Routing registry fragments
# --------------------------------------------------------------------------- #

# Client -> server envelopes this module contributes.
REGION_CHOICE_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    RegionChoiceProvidedEnvelopePayload.MESSAGE_TYPE: (
        RegionChoiceProvidedEnvelopePayload
    ),
}

# Server -> client envelopes this module contributes.
REGION_CHOICE_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    RegionChoiceRequestEnvelopePayload.MESSAGE_TYPE: (
        RegionChoiceRequestEnvelopePayload
    ),
}

# Aggregate for downstream consumers that don't care about direction.
REGION_CHOICE_PAYLOADS: dict[str, type[GraceModel]] = {
    **REGION_CHOICE_CLIENT_TO_AGENT_PAYLOADS,
    **REGION_CHOICE_AGENT_TO_CLIENT_PAYLOADS,
}
