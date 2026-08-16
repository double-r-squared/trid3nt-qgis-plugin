"""Region-disambiguation picker envelopes (state-bbox-fallback narrowing).

job-0346 added a state-snap fallback to ``geocode_location``: a vague /
regional query ("south Florida", "the Texas panhandle") that has no precise
OSM match snaps to the **whole state** bbox and stamps
``source="state-bbox-fallback"`` + an honest ``fallback_reason``. That whole-
state bbox stays the DEFAULT / automated answer — a headless workflow (no
interactive client) proceeds with it unchanged.

This module adds the INTERACTIVE narrowing layer on top of that default: when a
geocode result comes back as a state-snap, the agent surfaces a user choice to
NARROW to a sub-region of the state (default granularity: counties). The picker
is BOTH-synced — the client renders the candidate regions as an in-chat card
list AND as a tappable choropleth on the map; either affordance answers the
same request.

The contract MIRRORS the proven just-in-time credential-request seam
(``secrets.CredentialRequestEnvelopePayload`` / ``CredentialProvidedEnvelopePayload``):
a server -> client REQUEST that pauses the turn, and a client -> server
PROVIDED reply that resumes it, correlated by an unguessable ``request_id``
ULID. The two modules are intentionally analogous so the agent's pause/resume
registry, the web's interactive-card rendering, and the envelope-serialization
discipline are the same shape in both flows.

This module owns:

- ``RegionAdminLevel`` — closed Literal of the admin granularities a region
  candidate can be drawn at. ``"county"`` is the v0.1 shipping default; the
  Literal is closed so a coarser/finer level is an explicit amendment, not a
  silent open enum (the agent-side region-set builder has per-level fetch
  plumbing).
- ``RegionCandidate`` — one selectable sub-region: ``region_id`` (stable id the
  reply echoes), ``name`` (human label, e.g. "Lee County"), ``bbox``
  (EPSG:4326 ``total_bounds`` of the region polygon), ``admin_level``.
- ``RegionChoiceRequestEnvelopePayload`` — server -> client: the whole-state
  default plus the candidate sub-regions and an honest prompt string.
- ``RegionChoiceProvidedEnvelopePayload`` — client -> server: the user's
  decision (``region`` or ``whole_state``), correlated by ``request_id``.
- The per-module routing-registry fragments (splatted into ``ws.py`` following
  the secrets / payload-warning / chart-emission precedent).

Invariants this module is responsible for:

- **Fallback norm (honesty floor).** The request always names that the agent
  snapped to the WHOLE state and is OFFERING a narrower pick — never a silent
  widening, never a hallucinated precise match. The default action is
  ``"use_whole_state"`` so a non-answer (headless / declined) keeps the honest
  state-bbox result.
- **9. No cost theater.** No cost / quota / usage fields anywhere.
- **8. Cancellation is first-class.** There is no per-envelope timeout/cancel
  field — a "use the whole state" reply IS the decline path (it keeps the
  already-correct default), and a hard cancel flows through the A.3 ``cancel``
  message, exactly as the credential flow does.

SRS references:
- Appendix A.3 (client -> server) / A.4 (server -> client) for envelope-type
  discipline (kebab-case ``type``, ``payload`` always an object).
- Mirrors ``secrets.CredentialRequestEnvelopePayload`` /
  ``CredentialProvidedEnvelopePayload`` (the proven interactive-card seam).
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
# Admin-level vocabulary (closed Literal — amendment to add a member)
# --------------------------------------------------------------------------- #

# The granularity a region candidate is drawn at. ``"county"`` is the v0.1
# shipping default (the agent's region-set builder fetches TIGER counties for
# the detected state). The Literal is closed because each level has its own
# fetch plumbing in the agent (TIGER COUNTY vs PLACE vs ZCTA shapefiles) —
# registering an unknown level at the schema layer would let the request carry
# a granularity the builder cannot actually produce. Coarser ("state_region")
# / finer ("place", "zcta") levels are an explicit amendment.
RegionAdminLevel = Literal[
    "county",
]


# --------------------------------------------------------------------------- #
# RegionCandidate — one selectable sub-region
# --------------------------------------------------------------------------- #


class RegionCandidate(GraceModel):
    """A single selectable sub-region of the snapped state.

    The agent's region-set builder produces one of these per administrative
    feature (default: per county) within the detected state: it fetches the
    TIGER boundaries for the state and emits, for each feature, a stable
    ``region_id``, the human-readable ``name``, the ``bbox`` (the polygon's
    ``total_bounds`` in EPSG:4326), and the ``admin_level`` the feature was
    drawn at.

    Fields:

    - ``region_id`` — stable identifier the client echoes verbatim in the
      ``RegionChoiceProvidedEnvelopePayload`` when the user picks this region.
      Drawn from the TIGER feature's GEOID (e.g. ``"county-12071"`` for Lee
      County FL) so it is stable across requests for the same state. Free-form
      non-empty string (≤120 chars); the schema does not constrain the format.
    - ``name`` — human label rendered on the in-chat card and the map
      choropleth tooltip (e.g. ``"Lee County"``). ≤200 chars.
    - ``bbox`` — EPSG:4326 ``[min_lon, min_lat, max_lon, max_lat]`` total bounds
      of the region polygon. This is the bbox the agent continues the workflow
      with when the user selects this region.
    - ``admin_level`` — the granularity this candidate was drawn at (closed
      ``RegionAdminLevel`` Literal; ``"county"`` by default).
    """

    region_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    bbox: BBox
    admin_level: RegionAdminLevel = "county"


# --------------------------------------------------------------------------- #
# RegionChoiceRequest — server -> client (A.4 amendment)
# --------------------------------------------------------------------------- #


class RegionChoiceRequestEnvelopePayload(GraceModel):
    """``region-choice-request`` (A.4): server -> client narrow-the-region prompt.

    Emitted when a ``geocode_location`` tool result comes back as a state-snap
    (``source == "state-bbox-fallback"``). The agent has ALREADY resolved the
    whole-state bbox (the honest default the automated path uses); this request
    surfaces, on top of that default, a user choice to NARROW to a sub-region
    of the state (default granularity: counties). The client renders the
    candidates BOTH as an in-chat card list and as a tappable county
    choropleth on the map (both synced to the same ``request_id``); either
    affordance answers via ``RegionChoiceProvidedEnvelopePayload``.

    The agent PAUSES the turn awaiting the reply (mirroring the credential-
    request pause/resume), with a fail-open default: on timeout / no client the
    whole-state bbox (already the geocode result) is used unchanged — the
    workflow never blocks on the interactive pick.

    Fields:

    - ``request_id`` — ULID correlating this request with the
      ``RegionChoiceProvidedEnvelopePayload`` reply (and with the agent's
      paused-turn record). The client MUST echo it verbatim.
    - ``state_name`` — the detected state's full name (e.g. ``"Florida"``).
      Surfaced in the prompt + as the whole-state option's label. ≤120 chars.
    - ``state_code`` — the 2-letter state code (e.g. ``"FL"``). Lets the client
      label / disambiguate without a name -> code table. ≤2 chars.
    - ``state_bbox`` — the whole-state EPSG:4326 bbox the geocode snapped to;
      the bbox used if the user chooses ``use_whole_state`` (the default
      action). The client highlights this extent on the map.
    - ``candidates`` — the candidate sub-regions to choose from (default:
      counties of the state). May be empty when the region-set build failed —
      the client then offers only the whole-state default (honest degrade).
    - ``default_action`` — the action taken if the user does not pick a region.
      Always ``"use_whole_state"`` at v0.1 (the honest, already-resolved
      default). A closed Literal so the contract is explicit about the
      fail-open behaviour.
    - ``message`` — the agent's user-facing prompt: it MUST say the agent
      snapped to the whole state and is offering a narrower pick (fallback
      honesty floor). Plain prose; ≤1024 chars.

    Invariant 9 (no cost theater): no cost / quota field. Invariant 8
    (cancellation is first-class): no per-envelope timeout/cancel field — a
    "use the whole state" reply IS the decline path; a hard cancel rides the
    A.3 ``cancel`` message.
    """

    MESSAGE_TYPE: ClassVar[str] = "region-choice-request"

    envelope_type: Literal["region-choice-request"] = "region-choice-request"
    request_id: ULIDStr
    state_name: str = Field(min_length=1, max_length=120)
    state_code: str = Field(min_length=1, max_length=2)
    state_bbox: BBox
    candidates: list[RegionCandidate] = Field(default_factory=list)
    default_action: Literal["use_whole_state"] = "use_whole_state"
    message: str = Field(min_length=1, max_length=1024)


# --------------------------------------------------------------------------- #
# RegionChoiceProvided — client -> server (A.3 amendment)
# --------------------------------------------------------------------------- #


class RegionChoiceProvidedEnvelopePayload(GraceModel):
    """``region-choice-provided`` (A.3): client -> server the user's region pick.

    Sent when the user answers a ``RegionChoiceRequestEnvelopePayload`` — either
    by tapping a candidate (in-chat card OR map choropleth, both synced) or by
    choosing "use the whole state". It resumes the agent's paused turn,
    correlated by ``request_id``. Carries the picked region's identity so the
    agent continues the workflow with the right bbox.

    Fields:

    - ``request_id`` — the ``request_id`` of the
      ``RegionChoiceRequestEnvelopePayload`` this answers. The agent uses it to
      resolve the exact paused turn to resume.
    - ``choice`` — ``"region"`` when the user narrowed to a sub-region;
      ``"whole_state"`` when the user kept the whole-state default (the
      honest already-resolved bbox). Closed Literal.
    - ``selected_region_id`` — the ``region_id`` of the chosen
      ``RegionCandidate`` when ``choice == "region"``; ``None`` for
      ``whole_state``. The agent re-resolves the candidate's bbox by this id
      (authoritative over a client-sent bbox), falling back to
      ``selected_bbox`` only if the id is unknown.
    - ``selected_bbox`` — the chosen region's EPSG:4326 bbox, echoed from the
      candidate. Present when ``choice == "region"``; ``None`` for
      ``whole_state``. A convenience / fallback when the agent cannot
      re-resolve ``selected_region_id`` (e.g. the region set was rebuilt) —
      the agent prefers re-resolving by ``selected_region_id`` so a tampered
      bbox cannot redirect the workflow.

    Invariant 9: no cost field. Invariant 8: cancellation is first-class — a
    ``whole_state`` reply IS the decline path; a hard cancel rides A.3
    ``cancel``.
    """

    MESSAGE_TYPE: ClassVar[str] = "region-choice-provided"

    envelope_type: Literal["region-choice-provided"] = "region-choice-provided"
    request_id: ULIDStr
    choice: Literal["region", "whole_state"]
    selected_region_id: str | None = Field(default=None, max_length=120)
    selected_bbox: BBox | None = None


# --------------------------------------------------------------------------- #
# Routing registry (per-module — spread into ws.ALL_PAYLOADS in ws.py)
# --------------------------------------------------------------------------- #

# Client -> server envelopes this module contributes (A.3).
REGION_CHOICE_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    RegionChoiceProvidedEnvelopePayload.MESSAGE_TYPE: (
        RegionChoiceProvidedEnvelopePayload
    ),
}

# Server -> client envelopes this module contributes (A.4).
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
