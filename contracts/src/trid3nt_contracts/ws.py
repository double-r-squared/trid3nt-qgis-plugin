"""WebSocket protocol: envelope + every message type (SRS Appendix A, FR-AS-5).

All messages share the A.1 envelope (``type``/``id``/``ts``/``session_id``/
``payload``). ``type`` is kebab-case; ``id`` is a ULID; ``ts`` is ISO-8601 ``Z``;
``payload`` is always an object (``{}`` when empty).

This module defines:
- ``Envelope[PayloadT]``: the generic wire wrapper.
- One ``*Payload`` model per message type (A.3, A.4, A.4b).
- The ``map-command`` internal ``command`` discriminator (A.4) with one args
  model per command.
- ``ErrorCode``: the A.6 SCREAMING_SNAKE_CASE error-code enum.
- The ``research_mode`` field on ``user-message`` (orchestrator pinned
  toggle-carrier seam, FR-WC-15) — an Appendix A amendment; see the report's
  amendment log for the exact proposed SRS diff.

Invariants this module is responsible for:
- **9. No cost theater.** ``ConfirmationRequestPayload`` carries no cost field.
- **8. Cancellation is first-class.** ``cancelled`` is a distinct ``state`` in
  ``pipeline-state`` step states, separate from ``failed``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Generic, Literal, TypeVar

from pydantic import Field, field_validator

from .common import (
    BBox,
    GraceModel,
    ULIDStr,
    UTCDatetime,
    _validate_bbox,  # shared EPSG:4326 ordering rules for aoi_bbox (ADR 0017)
    new_ulid,
    now_utc,
)

__all__ = [
    "Envelope",
    "ErrorCode",
    # client -> agent (A.3)
    "ResearchMode",
    "UserMessagePayload",
    "CancelPayload",
    "ConfirmResponsePayload",
    "SessionResumePayload",
    # client -> agent (A.4b)
    "SpatialInputResponsePayload",
    "DisambiguationResponsePayload",
    "ClarificationResponsePayload",
    # agent -> client (A.4)
    "AgentMessageChunkPayload",
    "AgentThinkingChunkPayload",
    "ToolCallStartPayload",
    "ToolCallProgressPayload",
    "ToolCallCompletePayload",
    "ToolCallFailedPayload",
    "PipelineStepState",
    "PipelineStep",
    "PipelineStatePayload",
    "SolveProgressPayload",
    "ToolIoPayload",
    "MapCommandPayload",
    "ConfirmationRequestPayload",
    "SessionStateStatus",
    "SessionStatePayload",
    "ErrorPayload",
    "LocationResolvedPayload",
    "ReferenceLayer",
    "SuggestedView",
    "SpatialInputRequestPayload",
    "DisambiguationCandidate",
    "DisambiguationRequestPayload",
    "ClarificationOption",
    "ClarificationRequestPayload",
    # agent -> client (sprint-08 forward-looking) — FR-FR-1 + §F.1.2 Mode 2
    "RecoveryChoiceOption",
    "RecoveryChoicePayload",
    "ProbeFindings",
    "SuggestedCatalogEntry",
    "OfferCatalogAdditionPayload",
    # ADR 0018 auto/ask modes -- tool-selection picker (Stage 3, 2026-07-22)
    "ToolChoiceMode",
    "ToolCandidatesReason",
    "ToolCandidate",
    "ToolCandidatesPayload",
    "ToolChoicePayload",
    # client -> agent (sprint-08 forward-looking) — FR-FR-1 + §F.1.2 Mode 2
    "RecoveryChoice",
    "RecoveryChoiceResponsePayload",
    "CatalogAdditionDecision",
    "CatalogAdditionResponsePayload",
    # map-command args (A.4)
    "LoadLayerArgs",
    "RemoveLayerArgs",
    "SetLayerVisibilityArgs",
    "SetLayerOpacityArgs",
    "SetLayerOrderArgs",
    "ZoomToArgs",
    "SetTemporalConfigArgs",
    "StartAnimationArgs",
    "StopAnimationArgs",
    "InvalidateTilesArgs",
    "MapTemporal",
    # registry
    "CLIENT_TO_AGENT_PAYLOADS",
    "AGENT_TO_CLIENT_PAYLOADS",
    "ALL_PAYLOADS",
]


# --------------------------------------------------------------------------- #
# Envelope (A.1)
# --------------------------------------------------------------------------- #

PayloadT = TypeVar("PayloadT", bound=GraceModel)


class Envelope(GraceModel, Generic[PayloadT]):
    """The shared message envelope (A.1).

    ``type`` is the kebab-case discriminator; it is set per message type by the
    caller (the agent service / client serialize the right value). ``id`` and
    ``ts`` default to a fresh ULID and current UTC. ``payload`` is always an
    object.
    """

    type: str  # kebab-case discriminator (see ``*_TYPE`` on payloads)
    id: ULIDStr = Field(default_factory=new_ulid)
    ts: UTCDatetime = Field(default_factory=now_utc)
    session_id: ULIDStr
    # job-0277 (proposed A.1 amendment): the Case that OWNS the turn this
    # envelope belongs to, when one is bound. With per-Case chat streams and
    # stream-scoped turn concurrency (job-0269), the client must route live
    # streaming envelopes to the OWNING Case's stream — "the stream the user
    # last messaged" misattributes a still-running turn's cards/narration the
    # moment the user switches Cases. None = no Case context (root turns,
    # lifecycle envelopes) — clients fall back to submit-time routing.
    case_id: ULIDStr | None = None
    payload: PayloadT


# --------------------------------------------------------------------------- #
# Error codes (A.6)
# --------------------------------------------------------------------------- #

ErrorCode = Literal[
    "AUTH_FAILED",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    "LLM_UNAVAILABLE",
    "TOOL_NOT_FOUND",
    "TOOL_PARAMS_INVALID",
    "TOOL_TIMEOUT",
    "DEM_SOURCE_UNAVAILABLE",
    "SOLVER_FAILED",
    "CONFIRMATION_TIMEOUT",
    "SPATIAL_INPUT_TIMEOUT",
    "DISAMBIGUATION_TIMEOUT",
    "CLARIFICATION_TIMEOUT",
    "USER_INPUT_CANCELLED",
    "CANCELLED",
    # OPEN-14 (context-budget, LOCAL/OpenAI model path only): a turn's prompt
    # was clipped by the model's num_ctx even after one recompaction + retry
    # (context_budget.ContextWindowExceededError). Distinct from
    # LLM_UNAVAILABLE -- a genuinely oversized Case, not a transient model
    # outage -- so the client can render the honest "start a new case or
    # switch models" guidance instead of an offer to retry.
    #
    # BUG 1 (post-OPEN-14 acceptance rerun, 2026-07-12): this value was
    # missing here while server.py's abort handler already sent it as the
    # literal ``error_code`` on every ``ErrorPayload`` -- so every single
    # CONTEXT_WINDOW_EXCEEDED abort raised a pydantic ValidationError inside
    # ``_send_error`` (constructing the payload), unconditionally, dead
    # socket or not. That raise, uncaught by the pre-fix except-block, is why
    # the terminal-failure-card persist call right after it was never
    # reached.
    "CONTEXT_WINDOW_EXCEEDED",
]


# =========================================================================== #
# Client -> Agent messages (A.3)
# =========================================================================== #

# Research-mode toggle carrier (FR-WC-15 / orchestrator pinned seam). v0.1
# always runs research mode regardless; the carrier is pinned now so nobody
# invents a second path. "deep_research" selection in v0.1 proceeds in research
# mode (FR-HEP-4). This is an Appendix A amendment — see report amendment log.
ResearchMode = Literal["research", "deep_research"]

# ADR 0018 (auto/ask modes, Stage 3 2026-07-22): the ROUTING-VISIBILITY mode
# for a turn. Governs ONLY whether tool selection is surfaced as a
# ``tool-candidates`` picker card -- the consent surface (payload warnings,
# granularity, solver confirm, code-exec approval, credential entry, region
# choice, spatial input) is NEVER mode-dependent (gates answer "may I do
# this"; modes answer "which tool"; the two layers never mix).
#
# - ``"auto"``: tool selection is autonomous; no pick cards -- EXCEPT on a
#   MEASURED ambiguity signal (top-1 vs top-2 retrieval near-tie), where the
#   server may still emit a ``tool-candidates`` card with
#   ``reason="ambiguity"``.
# - ``"ask"``: tool selection is surfaced as a ``tool-candidates`` card
#   (``reason="ask_mode"``), staged in waves along the natural analysis flow
#   (acquisition -> preprocessing -> analysis -> visualization).
ToolChoiceMode = Literal["auto", "ask"]


class UserMessagePayload(GraceModel):
    """``user-message`` (A.3): user-submitted text input."""

    MESSAGE_TYPE: ClassVar[str] = "user-message"

    text: str
    research_mode: ResearchMode = "research"  # Appendix A amendment (FR-WC-15)
    # In-chat model selector (NATE 2026-06-17): optional Bedrock model id
    # chosen by the user before submitting.  ``None`` means "use the server
    # default" (``BEDROCK_MODEL_ID`` env / ``bedrock_adapter.bedrock_model_id()``).
    # The client sends this on every user-message so the agent can hot-swap the
    # model between turns without a session restart.
    model_id: str | None = None
    # job-CASE-AUTHORITY (Appendix A.3 amendment): the Case the CLIENT is
    # currently in, stamped on every user-message. The server treats it as the
    # authority for turn-binding — a 'resize bbox' turn runs in the client's
    # current Case, never a stale in-memory server pointer. ``None`` (older
    # client that does not stamp) preserves the prior behavior: the server
    # falls back to its own ``_SESSION_ACTIVE_CASE`` pointer. Same field name +
    # shape as ``SessionResumePayload.case_id`` and the web ``contracts.ts``
    # mirror so the client<->server contract is identical across both messages.
    case_id: str | None = None
    # Local-build thinking visibility (NATE live-feedback 2026-07-08): when
    # True the OpenAI-compatible local adapter enables the model's reasoning
    # channel for this turn (omits the /no_think system suffix) and the server
    # forwards reasoning deltas as ``agent-thinking-chunk`` envelopes. ``None``
    # (older client / cloud client that does not stamp) preserves the prior
    # behavior: thinking suppressed per TRID3NT_OPENAI_EXTRA_SYSTEM. Ignored by
    # the Bedrock path.
    show_thinking: bool | None = None
    # Structured per-message AOI (ADR 0017 mechanism 2, 2026-07-22): the
    # client's current AOI as ``[min_lon, min_lat, max_lon, max_lat]``
    # (EPSG:4326) -- replacing the bracketed in-text prose line the QGIS dock
    # used to append to ``text`` ("[QGIS map canvas AOI (EPSG:4326): bbox =
    # ...]"). ``None`` / absent (older client, web client, or no AOI set)
    # preserves the prior behavior exactly: the server infers location from
    # the message text. Element order + EPSG:4326 ordering rules mirror every
    # other bbox carrier (``common.BBox``, case-create/set-bbox ``args.bbox``);
    # kept a plain 4-float list (not ``BBox``) so consumers read the
    # wire-identical shape. This is the PER-TURN AOI; the persistent Case bbox
    # still rides ``case-command`` unchanged.
    aoi_bbox: list[float] | None = None
    # ADR 0018 (auto/ask modes, Stage 3 2026-07-22): the routing-visibility
    # mode for THIS turn -- the per-message settings carrier the QGIS dock /
    # web settings toggle stamps, following the ``show_thinking`` /
    # ``model_id`` precedent exactly (no session-config envelope exists; this
    # IS the config path the server consumes). ``None`` / absent (older
    # client, or a client that leaves the default) preserves the prior
    # behavior: the server treats the turn as ``"auto"``. ``"ask"`` asks the
    # server to surface tool selection as ``tool-candidates`` picker cards
    # (see ``ToolCandidatesPayload``); consent gates are unaffected either
    # way (``ToolChoiceMode`` docstring).
    tool_choice_mode: ToolChoiceMode | None = None

    @field_validator("aoi_bbox")
    @classmethod
    def _validate_aoi_bbox(cls, value: list[float] | None) -> list[float] | None:
        """Exactly 4 floats in ``common.BBox`` EPSG:4326 order, or None."""
        if value is None:
            return None
        if len(value) != 4:
            raise ValueError(
                "aoi_bbox must be [min_lon, min_lat, max_lon, max_lat] "
                f"(exactly 4 floats), got {len(value)} elements: {value!r}"
            )
        min_lon, min_lat, max_lon, max_lat = value
        _validate_bbox((min_lon, min_lat, max_lon, max_lat))
        return [float(v) for v in value]


class CancelPayload(GraceModel):
    """``cancel`` (A.3): cancel the in-flight pipeline."""

    MESSAGE_TYPE: ClassVar[str] = "cancel"

    reason: str | None = None


class ConfirmResponsePayload(GraceModel):
    """``confirm-response`` (A.3): user response to a confirmation-request."""

    MESSAGE_TYPE: ClassVar[str] = "confirm-response"

    request_id: ULIDStr
    approved: bool


class SessionResumePayload(GraceModel):
    """``session-resume`` (A.3): resume an existing session (id in envelope)."""

    MESSAGE_TYPE: ClassVar[str] = "session-resume"

    # job-CASE-AUTHORITY (Appendix A.3 amendment): the Case the CLIENT is
    # currently in, stamped on reconnect. On resume the server RE-BINDS its
    # active-Case pointer to this value before replaying the Case's layers — so
    # a reconnect replays the Case the user is actually in, never a stale
    # server-side pointer (the SNAP root cause: a select tapped mid-reconnect
    # never reaches the server, and the bare ``session-resume {}`` replays the
    # server's stale active Case). ``None`` (older client, or a fresh session
    # with no Case yet) preserves the prior behavior: the server keeps its own
    # pointer and replays whatever it last had. Same field name + shape as
    # ``UserMessagePayload.case_id`` and the web ``contracts.ts`` mirror.
    case_id: str | None = None


# =========================================================================== #
# Client -> Agent (user input responses) (A.4b)
# =========================================================================== #


class SpatialInputResponsePayload(GraceModel):
    """``spatial-input-response`` (A.4b): user picked a geometry, or cancelled.

    Three shapes ride this one payload, keyed by ``geometry_type``:

    - ``"point"`` / ``"bbox"`` — the original FR-WC-13 pick-mode reply:
      ``coordinates`` is set (``[lon, lat]`` for point,
      ``[minLon, minLat, maxLon, maxLat]`` for bbox); ``features`` stays None.
    - ``"vector_draw"`` — the FR-WC-16 urban vector-draw reply: ``features`` is
      a GeoJSON ``FeatureCollection`` of the drawn geometry; ``coordinates``
      stays None. Each ``Feature.properties`` carries a ``role`` ∈
      {``"aoi"``, ``"barrier"``, ``"point"``}. For a ``"barrier"`` LineString,
      ``properties.barrier_type`` ∈ {``"wall"``, ``"flap_gate"``} (mirrors
      ``swmm_contracts.BarrierType``) and, for a ``"flap_gate"``, an OPTIONAL
      ``properties.flap_direction`` ∈ {``"in"``, ``"out"``} (or a numeric
      bearing) recording the one-way orientation; an OPTIONAL
      ``properties.protected_side`` ∈ {``"left"``, ``"right"``} mirrors the
      engine seam (``swmm_mesh_builder._resolve_protected``).
    - For a cancellation: ``cancelled=True`` and every geometry field stays
      None.

    The drawn ``features`` round-trips straight into the urban engine: the
    ``"barrier"`` features are exactly the tagged-``LineString``
    ``FeatureCollection`` that ``swmm_contracts.SWMMRunArgs.barriers`` accepts
    (filter to ``role == "barrier"`` and they validate field-for-field).

    Large-payload note (Invariant + large-payload norm): a drawn
    ``FeatureCollection`` is small by construction (a handful of short
    ``LineString`` / ``Polygon`` rings — kilobytes, not the megabytes a raster
    fetch produces), so NO ``estimate_payload_mb`` / payload-warning gate
    applies to it. The 25 MB warn / 250 MB hard-block discipline
    (``payload_warning.py``) governs TOOL-OUTPUT payloads, not this small
    user-drawn input; no cap is imposed here beyond the structural validator.
    """

    MESSAGE_TYPE: ClassVar[str] = "spatial-input-response"

    request_id: ULIDStr
    geometry_type: Literal["point", "bbox", "vector_draw"] | None = None
    coordinates: list[float] | None = None
    features: dict[str, Any] | None = None
    cancelled: bool = False

    @field_validator("features")
    @classmethod
    def _validate_features(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Structurally validate the drawn GeoJSON ``FeatureCollection``.

        Validates STRUCTURE only (no geometry-library dependency in contracts),
        mirroring ``swmm_contracts._validate_barrier_feature_collection``:
        a ``FeatureCollection`` whose every ``Feature`` carries a
        ``properties.role`` ∈ {"aoi", "barrier", "point", "line"}; a
        ``"barrier"`` feature must be a ``LineString`` (>= 2 positions) tagged
        with ``properties.barrier_type`` ∈ {"wall", "flap_gate"}; a ``"line"``
        feature is a plain (untagged) ``LineString`` (>= 2 positions).
        """
        if value is None:
            return None
        return _validate_spatial_input_feature_collection(value)


def _validate_spatial_input_feature_collection(
    fc: dict[str, Any],
) -> dict[str, Any]:
    """Shared structural validator for a role-tagged drawn FeatureCollection.

    Enforces FR-WC-16's role + per-segment barrier vocabulary while staying a
    pure-structure check (no shapely/geojson import in the contracts package).
    """
    if fc.get("type") != "FeatureCollection":
        raise ValueError(
            f"features must be a GeoJSON FeatureCollection, "
            f"got type={fc.get('type')!r}"
        )
    feats = fc.get("features")
    if not isinstance(feats, list):
        raise ValueError("features.features must be a list")
    # "line" is a NEUTRAL elevation/section LineString (compute_terrain_profile /
    # compute_cross_section) -- a drawn line that carries no barrier semantics and
    # needs no wall/flap_gate tag. ADDITIVE: it never relaxes the barrier rules.
    valid_roles = {"aoi", "barrier", "point", "line"}
    valid_barrier_types = {"wall", "flap_gate"}
    valid_flap_directions = {"in", "out"}
    valid_protected_sides = {"left", "right"}
    for idx, feat in enumerate(feats):
        if not isinstance(feat, dict) or feat.get("type") != "Feature":
            raise ValueError(f"features.features[{idx}] must be a GeoJSON Feature")
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or "type" not in geom:
            raise ValueError(
                f"features.features[{idx}].geometry must be a GeoJSON geometry"
            )
        props = feat.get("properties") or {}
        role = props.get("role")
        if role not in valid_roles:
            raise ValueError(
                f"features.features[{idx}].properties.role must be one of "
                f"{sorted(valid_roles)}, got {role!r}"
            )
        if role == "line":
            # A neutral elevation/section line: a plain LineString (>= 2
            # positions), NO barrier_type required. Surfaced as the result's
            # `line`/`linestring` geometry for compute_terrain_profile.
            if geom.get("type") != "LineString":
                raise ValueError(
                    f"features.features[{idx}] role='line' geometry must be a "
                    f"LineString (got {geom.get('type')!r})"
                )
            coords = geom.get("coordinates")
            if not isinstance(coords, list) or len(coords) < 2:
                raise ValueError(
                    f"features.features[{idx}].geometry.coordinates must be a "
                    f"LineString with >= 2 positions"
                )
        if role == "barrier":
            if geom.get("type") != "LineString":
                raise ValueError(
                    f"features.features[{idx}] role='barrier' geometry must be a "
                    f"LineString (got {geom.get('type')!r})"
                )
            coords = geom.get("coordinates")
            if not isinstance(coords, list) or len(coords) < 2:
                raise ValueError(
                    f"features.features[{idx}].geometry.coordinates must be a "
                    f"LineString with >= 2 positions"
                )
            tag = props.get("barrier_type")
            if tag not in valid_barrier_types:
                raise ValueError(
                    f"features.features[{idx}].properties.barrier_type must be "
                    f"one of {sorted(valid_barrier_types)}, got {tag!r}"
                )
            # flap_direction is OPTIONAL; when present it is a closed enum OR a
            # numeric bearing (degrees). protected_side is OPTIONAL closed enum.
            flap_dir = props.get("flap_direction")
            if (
                flap_dir is not None
                and flap_dir not in valid_flap_directions
                and not isinstance(flap_dir, (int, float))
            ):
                raise ValueError(
                    f"features.features[{idx}].properties.flap_direction must be "
                    f"one of {sorted(valid_flap_directions)} or a numeric "
                    f"bearing, got {flap_dir!r}"
                )
            protected = props.get("protected_side")
            if protected is not None and protected not in valid_protected_sides:
                raise ValueError(
                    f"features.features[{idx}].properties.protected_side must be "
                    f"one of {sorted(valid_protected_sides)}, got {protected!r}"
                )
    return fc


class DisambiguationResponsePayload(GraceModel):
    """``disambiguation-response`` (A.4b): user chose a candidate, or cancelled."""

    MESSAGE_TYPE: ClassVar[str] = "disambiguation-response"

    request_id: ULIDStr
    candidate_id: str | None = None
    cancelled: bool = False


class ClarificationResponsePayload(GraceModel):
    """``clarification-response`` (A.4b): user chose an option, or cancelled."""

    MESSAGE_TYPE: ClassVar[str] = "clarification-response"

    request_id: ULIDStr
    option_id: str | None = None
    cancelled: bool = False


# =========================================================================== #
# Agent -> Client messages (A.4)
# =========================================================================== #


class AgentMessageChunkPayload(GraceModel):
    """``agent-message-chunk`` (A.4): a streamed token group from the LLM."""

    MESSAGE_TYPE: ClassVar[str] = "agent-message-chunk"

    message_id: ULIDStr
    delta: str  # new content since the last chunk (not accumulated)
    done: bool = False


class AgentThinkingChunkPayload(GraceModel):
    """``agent-thinking-chunk``: a streamed reasoning-channel token group.

    Local-build feature (NATE live-feedback 2026-07-08): the OpenAI-compatible
    adapter surfaces the model's reasoning deltas (e.g. Ollama qwen3
    ``delta.reasoning``) and the server forwards them live so the web can
    render a greyed, collapsible "thinking" block inside the SAME bubble as
    the eventual answer. ``message_id`` is shared with the segment's
    ``agent-message-chunk`` frames; a ``delta="" done=True`` frame closes the
    thinking stream for that bubble. Shape mirrors AgentMessageChunkPayload.
    """

    MESSAGE_TYPE: ClassVar[str] = "agent-thinking-chunk"

    message_id: ULIDStr
    delta: str  # new reasoning content since the last chunk (not accumulated)
    done: bool = False


class ToolCallStartPayload(GraceModel):
    """``tool-call-start`` (A.4): a tool invocation has begun."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-start"

    call_id: ULIDStr
    step_id: ULIDStr
    tool_name: str
    # tool_category vocabulary (FR-TA-3 convention; open enum). Mirrors the tool
    # categories in FR-TA-2. See report OQ-S5 for the documented vocabulary.
    tool_category: str
    params: dict = Field(default_factory=dict)  # sanitized parameters


class ToolCallProgressPayload(GraceModel):
    """``tool-call-progress`` (A.4): optional progress for an in-flight tool."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-progress"

    call_id: ULIDStr
    percent: int | None = Field(default=None, ge=0, le=100)
    status: str | None = None


class ToolCallCompletePayload(GraceModel):
    """``tool-call-complete`` (A.4): a tool finished successfully.

    ``metrics`` is tool-specific structured data (invariant 1: the numbers the
    narrative cites live here, never free text). For the flood depth tool this
    carries ``FloodMetrics``-shaped fields; the full result body lives in GCS /
    MongoDB and is referenced by ``result_uri``.
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-call-complete"

    call_id: ULIDStr
    result_summary: str  # human-readable one-liner for chat display
    result_uri: str | None = None  # present when the result is a stored artifact
    metrics: dict = Field(default_factory=dict)  # tool-specific structured data


class ToolCallFailedPayload(GraceModel):
    """``tool-call-failed`` (A.4): a tool errored out."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-failed"

    call_id: ULIDStr
    error_code: str  # enum-like string (per tool category; open)
    message: str  # human-readable, surfaced in chat
    retryable: bool = False


# pipeline-state (A.4) ------------------------------------------------------- #

# cancelled is a distinct terminal state, separate from failed (invariant 8).
PipelineStepState = Literal["pending", "running", "complete", "failed", "cancelled"]


class PipelineStep(GraceModel):
    """One step in the pipeline snapshot.

    ``duration_ms`` (job-0264, ELEVATED tool-timer requirement) is the
    authoritative wall-clock elapsed time the workflow stamps on the
    **terminal** transition (complete / failed / cancelled). It is derived
    deterministically from ``completed_at - started_at`` by the
    ``PipelineEmitter`` — NOT an LLM estimate (Invariant 1). Optional and
    ``None`` for pending/running steps: the client renders a cosmetic live
    ticker until this lands, then locks the card to this number. Milliseconds
    so a sub-second tool reads honestly (0 displays as "0:00"). ``ge=0``.

    Two-card sim observability (task-149): ``role`` discriminates the card
    KIND — the default ``"tool"`` is an on-box atomic-tool card (every existing
    payload), while ``"compute"`` is the off-box solver card that binds to an
    AWS Batch job. For a ``"compute"`` card, ``batch_job_id`` carries the Batch
    ``jobId`` it tracks and ``batch_status`` the last ``DescribeJobs`` status
    (SUBMITTED / RUNNABLE / STARTING / RUNNING / SUCCEEDED / FAILED). All three
    are optional with defaults that keep every existing tool card byte-identical
    on the wire (``role="tool"``, both ids ``None``). Never an LLM estimate
    (Invariant 1): ``batch_status`` mirrors the Batch control-plane verbatim.

    Nested sub-step timeline (task-168): a composer's INTERNAL atomic-tool calls
    (``fetch_*`` / deck build / ``run_solver`` / ``postprocess_*`` /
    ``publish_layer`` / ``compute_*``) are surfaced as CHILD steps nested under
    the parent workflow card instead of separate top-level interleaved cards.
    Four additive fields drive the "live breadcrumb + expand" UX:

    - ``parent_step_id`` -- set on a CHILD step; the ``step_id`` of the parent
      workflow step this child belongs to. When set, the client NESTS this step
      under the parent and does NOT render it as a top-level card. ``None`` (the
      default) means a normal top-level step -- every existing payload.
    - ``substep_label`` -- set on the PARENT; the raw tool name of the
      currently-running child (the web humanizes it into a breadcrumb label).
      Cleared (``None``) when the parent reaches a terminal state.
    - ``substep_index`` -- set on the PARENT; the 1-based index of the
      currently-running child (the breadcrumb "k" in "fetching topobathy k/N").
      Cleared when the parent completes.
    - ``substep_total`` -- set on the PARENT; the planned child count (the
      breadcrumb "N"). ``None`` when unknown -- the web then shows just the
      humanized label + index with no "/N".

    All four default ``None`` so every existing serialization stays
    byte-compatible (a step with no children + no parent dumps identically to
    the pre-task-168 wire shape). Deterministic / workflow-attributed, never an
    LLM estimate (Invariant 1).
    """

    step_id: ULIDStr
    name: str
    tool_name: str
    state: PipelineStepState
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    duration_ms: int | None = Field(default=None, ge=0)
    role: Literal["tool", "compute"] = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    # Nested sub-step timeline (task-168). ``parent_step_id`` is set on a CHILD;
    # ``substep_label`` / ``substep_index`` / ``substep_total`` are set on the
    # PARENT and describe the currently-running child for the live breadcrumb.
    # All default None => byte-compatible with every existing payload.
    parent_step_id: ULIDStr | None = None
    substep_label: str | None = None
    substep_index: int | None = Field(default=None, ge=1)
    substep_total: int | None = Field(default=None, ge=1)


class PipelineStatePayload(GraceModel):
    """``pipeline-state`` (A.4): full snapshot of the current pipeline.

    The full snapshot replaces the client's pipeline view on each message;
    deltas are not used.
    """

    MESSAGE_TYPE: ClassVar[str] = "pipeline-state"

    pipeline_id: ULIDStr
    steps: list[PipelineStep] = Field(default_factory=list)


# solve-progress (live big-sim telemetry — tool-accuracy panel, NATE 2026-06-17)


class SolveProgressPayload(GraceModel):
    """``solve-progress`` (A.4 extension): LIVE big-sim telemetry tick.

    Emitted by the agent during a long solver run (SFINCS / MODFLOW) so the web
    client renders grid resolution / active-cell count / vCPU / elapsed / ETA
    inline on the running tool/pipeline card — replacing a silent multi-minute
    spinner with honest live progress. The web track owns the card rendering;
    this payload is the shared wire contract.

    Invariant 1 (Determinism boundary): every field is solver/perf-model
    sourced, never an LLM estimate — ``elapsed_seconds`` is wall-clock and
    ``eta_seconds`` comes from the autoscale ``estimated_solve_seconds`` (the
    perf model) when available, else ``None`` (no fabricated ETA).
    """

    MESSAGE_TYPE: ClassVar[str] = "solve-progress"

    run_id: str
    solver: str
    grid_resolution_m: float | None = None
    active_cell_count: int | None = None
    vcpus: int | None = None
    elapsed_seconds: float = Field(ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    # Two-card sim observability (task-149): the DescribeJobs status this
    # progress tick reflects (SUBMITTED / RUNNABLE / STARTING / RUNNING /
    # SUCCEEDED / FAILED). Optional/None for ticks not bound to a Batch job;
    # mirrors the Batch control-plane verbatim (Invariant 1, never inferred).
    phase: str | None = None


# tool-io (tool-card-expand-output spec) ------------------------------------- #


class ToolIoPayload(GraceModel):
    """``tool-io`` (A.4 extension): the RAW input args + function_response for one
    tool dispatch, keyed by the pipeline step it belongs to.

    The ``pipeline-state`` ``PipelineStep`` carries only the humanized label +
    state + timing — it deliberately does NOT carry the raw I/O. This envelope
    is the additive sidecar that lets the chat tool-card's expander reveal the
    EXACT args the agent sent and the EXACT ``function_response`` the agent read
    back (the dict ``summarize_tool_result`` produced). Surfacing it makes
    server-side / upstream-API failures the narration hides directly visible
    (per ``feedback_tool_card_expand_output``).

    ``step_id`` matches the ``PipelineStep.step_id`` of the dispatch's card so
    the web merges this into the right card by id (the emitter mints the step
    inside ``emit_tool_call``; the server reads it back off
    ``emitter.last_tool_step`` and stamps it here).

    ``raw_args`` / ``function_response`` are pre-serialized JSON STRINGS (the
    server json-dumps them so a non-JSON-serializable value degrades to a
    string rather than breaking the envelope). Large payloads are TRUNCATED at
    the server to ``MAX_FIELD_BYTES`` (large-payload norm — the chat must never
    ship a multi-MB blob just for an expander); ``args_truncated`` /
    ``response_truncated`` flag a truncation and the ``*_bytes`` fields carry the
    ORIGINAL byte length so the UI can render an honest "truncated, N bytes"
    note. ``is_error`` mirrors the honesty-floor signal (the function_response
    carried ``status == "error"`` or the dispatch raised) so the expander styles
    the response block red without re-parsing the JSON.
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-io"

    #: Server-side truncation cap per field (bytes). Keeps the chat light; the
    #: expander is a debugging affordance, not a data-transfer channel.
    MAX_FIELD_BYTES: ClassVar[int] = 32_768

    step_id: ULIDStr
    tool_name: str
    raw_args: str = ""
    function_response: str = ""
    is_error: bool = False
    args_truncated: bool = False
    response_truncated: bool = False
    args_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)


# map-command (A.4) ---------------------------------------------------------- #


class MapTemporal(GraceModel):
    """Temporal block for ``load-layer`` args."""

    start: UTCDatetime
    end: UTCDatetime
    step_seconds: int = Field(gt=0)


class LoadLayerArgs(GraceModel):
    """``load-layer`` args. Field-for-field alignable with ``LayerURI``."""

    COMMAND: ClassVar[str] = "load-layer"

    layer_id: str
    wms_url: str
    style_preset: str
    temporal: MapTemporal | None = None


class RemoveLayerArgs(GraceModel):
    COMMAND: ClassVar[str] = "remove-layer"
    layer_id: str


class SetLayerVisibilityArgs(GraceModel):
    COMMAND: ClassVar[str] = "set-layer-visibility"
    layer_id: str
    visible: bool


class SetLayerOpacityArgs(GraceModel):
    COMMAND: ClassVar[str] = "set-layer-opacity"
    layer_id: str
    opacity: float = Field(ge=0.0, le=1.0)


class SetLayerOrderArgs(GraceModel):
    COMMAND: ClassVar[str] = "set-layer-order"
    layer_ids: list[str]  # ordered, top to bottom


class ZoomToArgs(GraceModel):
    COMMAND: ClassVar[str] = "zoom-to"
    bbox: BBox


class SetTemporalConfigArgs(GraceModel):
    COMMAND: ClassVar[str] = "set-temporal-config"
    layer_id: str
    start: UTCDatetime
    end: UTCDatetime
    step_seconds: int = Field(gt=0)
    current: UTCDatetime | None = None


class StartAnimationArgs(GraceModel):
    COMMAND: ClassVar[str] = "start-animation"
    layer_id: str
    speed: Literal[0.5, 1, 2, 5, 10] | None = None


class StopAnimationArgs(GraceModel):
    COMMAND: ClassVar[str] = "stop-animation"
    layer_id: str


class InvalidateTilesArgs(GraceModel):
    COMMAND: ClassVar[str] = "invalidate-tiles"
    layer_id: str | None = None  # omit to invalidate all


# map-command command vocabulary (open enum).
MapCommand = Literal[
    "load-layer",
    "remove-layer",
    "set-layer-visibility",
    "set-layer-opacity",
    "set-layer-order",
    "zoom-to",
    "set-temporal-config",
    "start-animation",
    "stop-animation",
    "invalidate-tiles",
]


class MapCommandPayload(GraceModel):
    """``map-command`` (A.4): one umbrella type with a ``command`` discriminator.

    ``args`` is the command-specific args object (one of the ``*Args`` models
    above). It is kept as a ``dict`` at the envelope level; the consumer
    validates it against the matching ``*Args`` model by ``command``. This is
    intentional: ten near-identical sibling top-level types would create churn
    (A.7 rationale).
    """

    MESSAGE_TYPE: ClassVar[str] = "map-command"

    command: MapCommand
    args: dict = Field(default_factory=dict)


class ConfirmationRequestPayload(GraceModel):
    """``confirmation-request`` (A.4): agent needs user approval.

    No cost field anywhere (invariant 9 / A.4): surfacing approximate cost is
    worse than none.
    """

    MESSAGE_TYPE: ClassVar[str] = "confirmation-request"

    request_id: ULIDStr
    title: str
    description: str
    estimated_duration_seconds: int | None = None
    default_timeout_seconds: int = 60


SessionStateStatus = Literal["active", "max_turns_reached"]
"""Status of the session at the moment a ``session-state`` envelope is sent.

- ``active``: normal operation (default).
- ``max_turns_reached``: the agent has hit ``MAX_TURNS_PER_SESSION`` (FR-FR-3).
  No further tool calls will be dispatched; the user must start a new session.

Added by job-0048 (sprint-08, FR-FR-3).
"""


class SessionStatePayload(GraceModel):
    """``session-state`` (A.4): lets the client reconstruct the session.

    The nested shapes are the JSON serialization of the Appendix D.6 models
    (``ChatMessage``, ``ProjectLayerSummary``, ``PipelineSnapshot``,
    ``MapView``). They are carried as plain ``dict``/``list`` here to avoid a
    circular contract dependency between ws.py and collections.py; the agent
    serializes the real D.6 models into them. See report OQ-S4.

    ``status`` is ``"active"`` in normal operation; ``"max_turns_reached"``
    when the FR-FR-3 cap fires (job-0048). Defaults to ``"active"`` so
    existing consumers do not need to change.
    """

    MESSAGE_TYPE: ClassVar[str] = "session-state"

    chat_history: list[dict] = Field(default_factory=list)
    loaded_layers: list[dict] = Field(default_factory=list)
    pipeline_history: list[dict] = Field(default_factory=list)
    current_pipeline: dict | None = None
    map_view: dict | None = None
    status: SessionStateStatus = "active"  # FR-FR-3 / job-0048


class ErrorPayload(GraceModel):
    """``error`` (A.4): global error not tied to a specific tool call."""

    MESSAGE_TYPE: ClassVar[str] = "error"

    error_code: ErrorCode
    message: str
    retryable: bool = False
    retry_after_seconds: int | None = None


class LocationResolvedPayload(GraceModel):
    """``location-resolved`` (A.4): a meaningful location was resolved.

    Emitted as a side effect of resolution-producing tools; the client
    auto-snaps the map to ``bbox``.
    """

    MESSAGE_TYPE: ClassVar[str] = "location-resolved"

    resolved_id: ULIDStr
    label: str
    bbox: BBox
    granularity: Literal["country", "region", "state", "city", "facility", "bbox"]
    source: Literal[
        "news_extraction", "user_prompt", "disambiguation", "geocoding", "tool_result"
    ]
    animate: bool = True


# spatial-input-request (A.4) ------------------------------------------------ #


class ReferenceLayer(GraceModel):
    """An optional helper layer shown only during a spatial-input request."""

    layer_id: str
    wms_url: str
    style_preset: str


class SuggestedView(GraceModel):
    """Where the client zooms to make picking easier."""

    bbox: BBox
    zoom: float


class SpatialInputRequestPayload(GraceModel):
    """``spatial-input-request`` (A.4): agent needs the user to pick a geometry.

    ``mode`` selects the client pick affordance:

    - ``"point"`` — single map click; the reply carries ``coordinates=[lon, lat]``.
    - ``"bbox"`` — a drag-rectangle; the reply carries
      ``coordinates=[minLon, minLat, maxLon, maxLat]``.
    - ``"vector_draw"`` — FR-WC-16 urban vector-draw: the client opens a
      terra-draw surface (rectangle / polygon / polyline + select-edit) and the
      reply carries ``features`` (a GeoJSON ``FeatureCollection`` with
      ``role``-tagged + per-segment ``barrier_type``/``flap_direction``
      properties). Use this when the agent needs the user to draw AOIs and
      tagged structural barriers (walls / flap gates) for the urban-flood
      (SWMM) engine.
    """

    MESSAGE_TYPE: ClassVar[str] = "spatial-input-request"

    request_id: ULIDStr
    mode: Literal["point", "bbox", "vector_draw"]
    title: str
    description: str
    # ``purpose`` (vector_draw only) selects the draw affordance + semantics:
    #
    # - ``"barrier"`` (DEFAULT -- the original SWMM urban-flood flow): a drawn
    #   LineString is a structural barrier that MUST be tagged wall / flap_gate
    #   before Submit; it round-trips into ``SWMMRunArgs.barriers``.
    # - ``"line"`` -- a NEUTRAL elevation/section line (e.g. for
    #   ``compute_terrain_profile`` / ``compute_cross_section``): a drawn
    #   LineString is submitted as a plain ``role=="line"`` LineString with NO
    #   barrier tagging required. The reply's first line geometry is surfaced as
    #   the ``line`` / ``linestring`` fields. ADDITIVE -- the default keeps the
    #   barrier flow byte-for-byte unchanged.
    # - ``"aoi"`` -- area-of-interest selection: only the rect/polygon draw
    #   tools are shown (no line/barrier tool), no tagging is required, and
    #   submit gates on having drawn at least one polygon. Drawn polygons carry
    #   ``role=="aoi"`` exactly as in the barrier flow (neutral, no barrier
    #   semantics). Use when the model needs the user to outline a region /
    #   study area for any tool that accepts an AOI or bbox.
    purpose: Literal["barrier", "line", "aoi"] = "barrier"
    suggested_view: SuggestedView | None = None
    reference_layers: list[ReferenceLayer] = Field(default_factory=list)
    default_timeout_seconds: int = 300


# disambiguation-request (A.4) ----------------------------------------------- #


class DisambiguationCandidate(GraceModel):
    """One enumerated candidate for an ambiguous entity."""

    id: str
    label: str
    bbox: BBox
    context: str | None = None


class DisambiguationRequestPayload(GraceModel):
    """``disambiguation-request`` (A.4): pick one of several candidates."""

    MESSAGE_TYPE: ClassVar[str] = "disambiguation-request"

    request_id: ULIDStr
    title: str
    description: str
    candidates: list[DisambiguationCandidate]
    default_timeout_seconds: int = 120


# clarification-request (A.4) ------------------------------------------------ #


class ClarificationOption(GraceModel):
    """One substantively-different path the agent could take. ``description``
    is required (A.4): it shows the user what each path produces."""

    id: str
    label: str
    description: str


class ClarificationRequestPayload(GraceModel):
    """``clarification-request`` (A.4): choose between different response paths."""

    MESSAGE_TYPE: ClassVar[str] = "clarification-request"

    request_id: ULIDStr
    question: str
    options: list[ClarificationOption] = Field(min_length=2, max_length=4)
    default_timeout_seconds: int = 60


# =========================================================================== #
# recovery-choice + recovery-choice-response (sprint-08 — FR-FR-1 substrate)
# =========================================================================== #
# Forward-looking — §3.10 FR-FR-1 deny/retry/chat recovery gate. The web-client
# implementation follows the existing `request_clarification` modal pattern; the
# response carries the user's selection (`deny` | `retry` | `chat`) and, when
# `choice == "chat"`, the focused free-text the user typed to nudge the agent.
#
# Routing per FR-FR-2: only emitted for "recoverable" error classes (transient
# upstream, recoverable-with-context). Substrate-integrity / user-initiated /
# budget-overrun error codes fail closed without gating.


#: The three actions a recovery-choice modal can return (FR-FR-1).
RecoveryChoiceOption = Literal["deny", "retry", "chat"]


class RecoveryChoicePayload(GraceModel):
    """``recovery-choice`` (A.4 — sprint-08 amendment, FR-FR-1).

    Agent emits this when an atomic-tool step fails with a *recoverable*
    error class (FR-FR-2 routing table). The client renders a small
    out-of-chat modal (mirrors the §F.3 popup discipline) offering the user
    deny / retry / chat actions.

    Fields:

    - ``request_id`` — ULID identifying the gate; the response carries it back.
    - ``failed_step_id`` — the ULID of the pipeline step the gate is about.
      The client surfaces this so the user knows which step is being decided.
    - ``error_code`` — Appendix A.6 SCREAMING_SNAKE_CASE code that the failed
      step's PipelineStepSummary carried. Open set (regex-validated shape).
    - ``error_message`` — short human-readable explanation (e.g. ``"USGS 3DEP
      returned HTTP 503 — service unavailable"``). Capped at 512 chars to
      mirror the PipelineStepSummary discipline.
    - ``context`` — short free-text describing what the agent was doing when
      the step failed (e.g. ``"fetching DEM at Fort Myers bbox for flood
      scenario"``). Helps the user pick the right action.
    - ``options`` — non-empty subset of {``"deny"``, ``"retry"``, ``"chat"``}.
      The routing table per FR-FR-2 may narrow this (e.g. omit ``"retry"`` for
      ``GEOCODE_NO_MATCH`` where retry is futile). The client renders one
      button per option.
    - ``ttl_seconds`` — gate validity (seconds since envelope ``ts``); on
      expiry the gate becomes a typed failure (``CONFIRMATION_TIMEOUT``-style
      error from the agent). Default 300s per the SRS example.

    No cost field anywhere (Invariant 9).
    """

    MESSAGE_TYPE: ClassVar[str] = "recovery-choice"

    request_id: ULIDStr
    failed_step_id: ULIDStr
    error_code: str  # SCREAMING_SNAKE_CASE per A.6 (open set)
    error_message: str = Field(max_length=512)
    context: str = Field(max_length=512)
    options: list[RecoveryChoiceOption] = Field(min_length=1, max_length=3)
    ttl_seconds: int = Field(default=300, ge=1)


#: The user's selection from a ``recovery-choice`` modal.
RecoveryChoice = Literal["deny", "retry", "chat"]


class RecoveryChoiceResponsePayload(GraceModel):
    """``recovery-choice-response`` (A.4b — sprint-08 amendment, FR-FR-1).

    User has picked one of the three actions OR cancelled the gate.

    Fields:

    - ``request_id`` — matches the originating ``recovery-choice`` request.
    - ``choice`` — ``"deny"`` / ``"retry"`` / ``"chat"`` OR None when the user
      cancelled. (Cancellation rare; modeled the same way as the existing
      ``clarification-response`` / ``disambiguation-response`` shapes.)
    - ``chat_text`` — populated only when ``choice == "chat"``; carries the
      focused single-line nudge the user typed. Capped at 4096 chars.
    - ``cancelled`` — set to True when the user explicitly dismissed the modal.

    Cross-shape discipline (lightweight — full enforcement is the consumer's
    responsibility, matching the existing A.4b response shapes): ``chat_text``
    SHOULD be populated when ``choice == "chat"`` and SHOULD be None
    otherwise; the agent service validates at receipt time per FR-AS-11.
    """

    MESSAGE_TYPE: ClassVar[str] = "recovery-choice-response"

    request_id: ULIDStr
    choice: RecoveryChoice | None = None
    chat_text: str | None = Field(default=None, max_length=4096)
    cancelled: bool = False


# =========================================================================== #
# offer-catalog-addition + catalog-addition-response (sprint-08 — §F.1.2 Mode 2)
# =========================================================================== #
# Forward-looking — §F.1.2 Mode 2 bounded-growth-path. Agent encounters a
# candidate `.gov` / `.edu` URL during research, performs a conformity probe,
# and surfaces a review modal with the probe findings + a suggested catalog
# entry shape. User accepts (-> writes to catalog_entries with status
# `user_proposed_pending_curator_review`), rejects, or edits the suggested
# entry before accepting.
#
# Note: the suggested-entry shape carried in `payload.suggested_catalog_entry`
# is a `CatalogEntry`-shaped dict (per the SRS §F.1.2 Mode 2 envelope example),
# NOT a fully-validated nested `CatalogEntry` model. We model it as a
# permissive sub-model below so the wire shape is documented + introspectable,
# while keeping the field tolerant of the case where the probe-time draft
# doesn't yet carry a Secret Manager reference. The agent service round-trips
# this through the full `CatalogEntry` model before writing to MongoDB.


class ProbeFindings(GraceModel):
    """Conformity-probe results captured by the agent during Mode 2 discovery.

    All fields are optional because a given probe may not be able to determine
    every axis (the OGC GetCapabilities check may fail while the STAC root
    check succeeds, etc.). The client renders findings as a structured table
    in the review modal so the user can sanity-check the agent's classification.
    """

    tls_cert_org: str | None = None  # e.g., "U.S. Department of …"
    access_tier_inferred: Literal[1, 2, 3, 4] | None = None  # §F.1.1 tier
    supports_range_requests: bool | None = None
    stac_root_found: bool | None = None
    ogc_capabilities_found: bool | None = None
    license_observed: str | None = None
    content_type: str | None = None
    last_modified_header: str | None = None


class SuggestedCatalogEntry(GraceModel):
    """Agent-drafted catalog entry surfaced inside an ``offer-catalog-addition``.

    Permissive shape: the agent supplies the fields it can infer from the
    conformity probe; the user may edit any of them in the review modal before
    accepting. The agent service round-trips an accepted draft through the
    full ``CatalogEntry`` model (which enforces cross-field rules) before
    writing to the ``catalog_entries`` collection.

    Mirrors the SRS §F.1.2 Mode 2 envelope example fields:
    ``id`` / ``name`` / ``description`` / ``urls`` / ``access_tier`` /
    ``credential_tier`` / ``ttl_class`` / ``source_class`` / ``license_claim``
    / ``how_to_use``. Probe-time drafts may omit ``description``, ``vintage``,
    or the conditional ``api_key_secret_ref``; the curator review fills any
    gaps before flipping ``status`` to ``"active"``.

    Renamed from the SRS sketch: ``license_claim`` here (the SRS prose uses
    ``license`` inside the suggested-entry block but the outer ``CatalogEntry``
    also uses ``license``; the ``_claim`` suffix marks that this is the probe's
    *observation*, not the curator-attested value). Surfaced in Open Questions.
    """

    id: str | None = None
    name: str | None = None
    description: str | None = None
    urls: list[str] = Field(default_factory=list)
    access_tier: Literal[1, 2, 3, 4] | None = None
    credential_tier: Literal[1, 2, 3] | None = None
    ttl_class: Literal["static-30d", "semi-static-7d", "dynamic-1h", "live-no-cache"] | None = None
    source_class: str | None = None
    license_claim: str | None = None
    how_to_use: str | None = None


class OfferCatalogAdditionPayload(GraceModel):
    """``offer-catalog-addition`` (A.4 — sprint-08 amendment, §F.1.2 Mode 2).

    Agent encountered a candidate `.gov` / `.edu` URL during research, ran a
    conformity probe, and is offering to add it to the catalog. The client
    renders a dedicated review modal (mirrors §F.3 secret-form pattern — popup,
    focus-trapped, separate from chat envelope) showing the URL + probe
    findings + the suggested catalog entry.

    Fields:

    - ``request_id`` — ULID identifying the offer; the response carries it back.
    - ``url`` — the candidate URL (must be `.gov` or `.edu` per Mode 2 trust
      model; the agent service enforces this before emission).
    - ``discovered_via`` — how the agent encountered the URL (``"user-query"``
      / ``"web-research"`` / ``"catalog-cross-reference"`` / ``"other"``).
      Open ``Literal`` so new discovery surfaces can be added without a
      breaking schema change.
    - ``probe_findings`` — structured ``ProbeFindings`` block (all sub-fields
      optional; rendered as a table in the modal).
    - ``suggested_catalog_entry`` — agent-drafted ``SuggestedCatalogEntry``;
      the user may edit any field before accepting.
    - ``ttl_seconds`` — offer validity. Default 600s (10 minutes — review
      modals get more time than retry gates because the user is reading +
      sanity-checking provenance).
    """

    MESSAGE_TYPE: ClassVar[str] = "offer-catalog-addition"

    request_id: ULIDStr
    url: str = Field(min_length=1)
    discovered_via: Literal[
        "user-query",
        "web-research",
        "catalog-cross-reference",
        "other",
    ]
    probe_findings: ProbeFindings
    suggested_catalog_entry: SuggestedCatalogEntry
    ttl_seconds: int = Field(default=600, ge=1)


#: User's decision on a Mode 2 offer-catalog-addition review modal.
CatalogAdditionDecision = Literal["accept", "reject"]


class CatalogAdditionResponsePayload(GraceModel):
    """``catalog-addition-response`` (A.4b — sprint-08 amendment, §F.1.2 Mode 2).

    User has accepted / rejected the offered catalog addition.

    Fields:

    - ``request_id`` — matches the originating ``offer-catalog-addition``.
    - ``decision`` — ``"accept"`` or ``"reject"`` (or None when ``cancelled``).
    - ``edited_catalog_entry`` — populated only when ``decision == "accept"``
      AND the user edited any field in the modal. When None on accept, the
      agent writes the original ``suggested_catalog_entry`` as-is (modulo
      cross-field validation). Mirrors the same permissive shape as the
      offer's ``suggested_catalog_entry`` so the round-trip is field-for-field.
    - ``reject_reason`` — free-text reason populated only when
      ``decision == "reject"``. Capped at 512 chars. Optional — the user may
      decline without explanation.
    - ``cancelled`` — set when the user dismissed the modal without deciding.

    Decision M (claim provenance): the response is logged to
    ``catalog_audit_log`` (D.12) with ``event_type: "user_proposed"`` on
    accept; reject events are also audited (open dict carries the
    ``reject_reason``).
    """

    MESSAGE_TYPE: ClassVar[str] = "catalog-addition-response"

    request_id: ULIDStr
    decision: CatalogAdditionDecision | None = None
    edited_catalog_entry: SuggestedCatalogEntry | None = None
    reject_reason: str | None = Field(default=None, max_length=512)
    cancelled: bool = False


# =========================================================================== #
# tool-candidates + tool-choice (ADR 0018 auto/ask modes -- Stage 3, 2026-07-22)
# =========================================================================== #
# The tool-selection picker seam. Retrieval/routing ties are a real error
# species: the model picks a plausible-but-wrong tool and the turn goes down a
# sad path the user could have prevented in one click (ADR 0018). The server
# (Lane S) emits ``tool-candidates`` when either (a) the turn runs in ASK mode
# (``UserMessagePayload.tool_choice_mode == "ask"``) or (b) AUTO mode measured
# a retrieval near-tie (top-1 vs top-2 score) -- ``reason`` says which. The
# client renders the ranked candidates as an inline picker card (radio choices
# + a free-text option + "let the agent decide") and answers with
# ``tool-choice``, correlated by ``request_id``.
#
# Timeout discipline (fail-open, mirrors the region-choice / credential
# pause): an unanswered request times out SERVER-side after ``timeout_s`` and
# the turn proceeds with the agent's own top-ranked pick -- the workflow never
# blocks on the interactive card. The client folds an unanswered card to an
# "agent proceeded" chip when subsequent turn events arrive.
#
# Invariant 1 (determinism boundary): ``score`` is the retrieval ranker's own
# number, never an LLM estimate. Invariant 9: no cost field anywhere.
# Consent gates are NEVER mode-dependent (``ToolChoiceMode`` docstring).


#: Why the picker surfaced: a measured retrieval near-tie in AUTO mode, or
#: the user's ASK mode surfacing every staged selection.
ToolCandidatesReason = Literal["ambiguity", "ask_mode"]


class ToolCandidate(GraceModel):
    """One ranked tool candidate inside a ``tool-candidates`` request.

    - ``tool_name`` -- the registry tool name, echoed VERBATIM in the
      ``tool-choice`` reply when picked (the server re-resolves the tool by
      this name; the client never invents one).
    - ``summary`` -- one-line human summary the card renders beside the name
      (drawn from the tool's docstring/description server-side; may be empty
      for a defensively-built candidate).
    - ``score`` -- the retrieval ranker's score for this candidate, verbatim
      (Invariant 1: never an LLM estimate). Unconstrained float: ranker
      backends differ (cosine similarity may be negative). Candidates arrive
      ranked best-first; ``score`` lets the client show/inspect the margin.
    """

    tool_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=500)
    score: float = 0.0


class ToolCandidatesPayload(GraceModel):
    """``tool-candidates`` (A.4 extension): agent -> client tool picker.

    Fields:

    - ``request_id`` -- ULID correlating this request with the ``tool-choice``
      reply (and the server's paused-selection record). Echoed verbatim.
    - ``stage_label`` -- the analysis-flow stage this pick belongs to (e.g.
      ``"Data step"``, ``"Analysis step"``) so staged ASK-mode waves read as
      a narrative, not a flood. Plain prose, client-rendered as the card
      title.
    - ``candidates`` -- the ranked candidates, best-first. MAY be empty when
      the retrieval side degraded (the client then offers only the free-text
      + let-agent-decide affordances -- honest degrade, mirroring the
      region-choice empty-candidates rule).
    - ``reason`` -- ``"ambiguity"`` (AUTO-mode measured near-tie) or
      ``"ask_mode"`` (the user asked to see every staged selection).
    - ``timeout_s`` -- how long the SERVER waits before proceeding with its
      own top-ranked pick (fail-open; the client card notes the state when
      the turn moves on).
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-candidates"

    request_id: ULIDStr
    stage_label: str = Field(min_length=1, max_length=120)
    candidates: list[ToolCandidate] = Field(default_factory=list)
    reason: ToolCandidatesReason
    timeout_s: float = Field(default=60.0, gt=0)


class ToolChoicePayload(GraceModel):
    """``tool-choice`` (A.4b extension): client -> agent picker reply.

    Exactly one of three shapes, keyed by which fields are set:

    - ``tool_name`` set -- the user picked a candidate; the value is a
      ``ToolCandidate.tool_name`` echoed VERBATIM. ``free_text`` is None.
    - ``free_text`` set -- the user typed guidance instead of picking (the
      card's free-text option); the server feeds it to the selection step.
      ``tool_name`` is None.
    - both None -- "let the agent decide": the server proceeds immediately
      with its own top-ranked pick (the same outcome as the timeout, but
      user-initiated and instant).

    Cross-shape discipline (lightweight -- full enforcement is the consumer's
    responsibility, matching the A.4b response shapes): ``tool_name`` and
    ``free_text`` SHOULD NOT both be set; the server prefers ``tool_name``
    when both arrive (the explicit pick is the stronger signal).
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-choice"

    request_id: ULIDStr
    tool_name: str | None = Field(default=None, max_length=200)
    free_text: str | None = Field(default=None, max_length=4096)


# =========================================================================== #
# Registries: kebab-case type -> payload model
# =========================================================================== #

CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    UserMessagePayload.MESSAGE_TYPE: UserMessagePayload,
    CancelPayload.MESSAGE_TYPE: CancelPayload,
    ConfirmResponsePayload.MESSAGE_TYPE: ConfirmResponsePayload,
    SessionResumePayload.MESSAGE_TYPE: SessionResumePayload,
    SpatialInputResponsePayload.MESSAGE_TYPE: SpatialInputResponsePayload,
    DisambiguationResponsePayload.MESSAGE_TYPE: DisambiguationResponsePayload,
    ClarificationResponsePayload.MESSAGE_TYPE: ClarificationResponsePayload,
    # sprint-08 — FR-FR-1 + §F.1.2 Mode 2
    RecoveryChoiceResponsePayload.MESSAGE_TYPE: RecoveryChoiceResponsePayload,
    CatalogAdditionResponsePayload.MESSAGE_TYPE: CatalogAdditionResponsePayload,
    # ADR 0018 auto/ask modes -- the picker reply (Stage 3, 2026-07-22)
    ToolChoicePayload.MESSAGE_TYPE: ToolChoicePayload,
}

# sprint-12-mega Wave 1.5 (job-0115): resolve OQ-0100-WS-REGISTRY-WIRING by
# splatting the per-Case secrets envelopes (§F.3) into the routing dicts. The
# secrets module owns the typed payloads + provider vocabulary; ws.py owns the
# Appendix A.3/A.4 registry surface, so the wiring lives here. Cases envelope
# wiring lands separately in Wave 2 with the Case UX agent job (do NOT add
# case-* payloads here).
from .secrets import (  # noqa: E402 — module-level imports below the dict literals
    SECRET_AGENT_TO_CLIENT_PAYLOADS,
    SECRET_CLIENT_TO_AGENT_PAYLOADS,
)

# sprint-12-mega Wave 2 (job-0127): tool payload-warning envelopes. The
# warning is agent->client (gate emission); the confirmation is client->agent
# (user decision). See payload_warning.py for the contract; see
# server/src/trid3nt_server/server.py for the dispatcher gate.
from .payload_warning import (  # noqa: E402
    PayloadConfirmationEnvelopePayload,
    PayloadWarningEnvelopePayload,
)

CLIENT_TO_AGENT_PAYLOADS.update(SECRET_CLIENT_TO_AGENT_PAYLOADS)
CLIENT_TO_AGENT_PAYLOADS[
    PayloadConfirmationEnvelopePayload.MESSAGE_TYPE
] = PayloadConfirmationEnvelopePayload

AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    AgentMessageChunkPayload.MESSAGE_TYPE: AgentMessageChunkPayload,
    AgentThinkingChunkPayload.MESSAGE_TYPE: AgentThinkingChunkPayload,
    ToolCallStartPayload.MESSAGE_TYPE: ToolCallStartPayload,
    ToolCallProgressPayload.MESSAGE_TYPE: ToolCallProgressPayload,
    ToolCallCompletePayload.MESSAGE_TYPE: ToolCallCompletePayload,
    ToolCallFailedPayload.MESSAGE_TYPE: ToolCallFailedPayload,
    PipelineStatePayload.MESSAGE_TYPE: PipelineStatePayload,
    SolveProgressPayload.MESSAGE_TYPE: SolveProgressPayload,
    ToolIoPayload.MESSAGE_TYPE: ToolIoPayload,
    MapCommandPayload.MESSAGE_TYPE: MapCommandPayload,
    ConfirmationRequestPayload.MESSAGE_TYPE: ConfirmationRequestPayload,
    SessionStatePayload.MESSAGE_TYPE: SessionStatePayload,
    ErrorPayload.MESSAGE_TYPE: ErrorPayload,
    LocationResolvedPayload.MESSAGE_TYPE: LocationResolvedPayload,
    SpatialInputRequestPayload.MESSAGE_TYPE: SpatialInputRequestPayload,
    DisambiguationRequestPayload.MESSAGE_TYPE: DisambiguationRequestPayload,
    ClarificationRequestPayload.MESSAGE_TYPE: ClarificationRequestPayload,
    # sprint-08 — FR-FR-1 + §F.1.2 Mode 2
    RecoveryChoicePayload.MESSAGE_TYPE: RecoveryChoicePayload,
    OfferCatalogAdditionPayload.MESSAGE_TYPE: OfferCatalogAdditionPayload,
    # ADR 0018 auto/ask modes -- the picker request (Stage 3, 2026-07-22)
    ToolCandidatesPayload.MESSAGE_TYPE: ToolCandidatesPayload,
}
AGENT_TO_CLIENT_PAYLOADS.update(SECRET_AGENT_TO_CLIENT_PAYLOADS)
AGENT_TO_CLIENT_PAYLOADS[
    PayloadWarningEnvelopePayload.MESSAGE_TYPE
] = PayloadWarningEnvelopePayload

# sprint-13 (job-0223): chart-emission envelope. Agent->client (A.4); the agent
# emits a Vega-Lite chart spec for the conversational data-analysis layer. See
# chart_contracts.py for the contract; the per-module fragment is splatted here
# following the secrets / payload_warning precedent.
from .chart_contracts import (  # noqa: E402
    CHART_AGENT_TO_CLIENT_PAYLOADS,
)

AGENT_TO_CLIENT_PAYLOADS.update(CHART_AGENT_TO_CLIENT_PAYLOADS)

# sprint-13 (job-0233): python-sandbox code-exec envelopes. Both agent->client
# (A.4): ``code-exec-request`` (confirm card before the sandbox runs) +
# ``code-exec-result`` (the run outcome). The confirmation REPLY rides the
# existing ``tool-payload-confirmation`` message (no new client->agent shape).
# See sandbox_contracts.py for the contracts; splatted here following the
# secrets / payload_warning / chart-emission precedent.
from .sandbox_contracts import (  # noqa: E402
    SANDBOX_AGENT_TO_CLIENT_PAYLOADS,
)

AGENT_TO_CLIENT_PAYLOADS.update(SANDBOX_AGENT_TO_CLIENT_PAYLOADS)

# region-disambiguation picker envelopes (state-bbox-fallback narrowing). The
# request is agent->client (``region-choice-request``: snap to whole state +
# offer narrower sub-regions); the reply is client->agent
# (``region-choice-provided``: the user's pick). MIRRORS the credential-request
# seam in secrets.py. See region_choice.py for the contracts; the agent
# pause/resume seam lives in server/src/trid3nt_server/server.py.
from .region_choice import (  # noqa: E402
    REGION_CHOICE_AGENT_TO_CLIENT_PAYLOADS,
    REGION_CHOICE_CLIENT_TO_AGENT_PAYLOADS,
)

CLIENT_TO_AGENT_PAYLOADS.update(REGION_CHOICE_CLIENT_TO_AGENT_PAYLOADS)
AGENT_TO_CLIENT_PAYLOADS.update(REGION_CHOICE_AGENT_TO_CLIENT_PAYLOADS)

ALL_PAYLOADS: dict[str, type[GraceModel]] = {
    **CLIENT_TO_AGENT_PAYLOADS,
    **AGENT_TO_CLIENT_PAYLOADS,
}

# map-command command -> args model
MAP_COMMAND_ARGS: dict[str, type[GraceModel]] = {
    LoadLayerArgs.COMMAND: LoadLayerArgs,
    RemoveLayerArgs.COMMAND: RemoveLayerArgs,
    SetLayerVisibilityArgs.COMMAND: SetLayerVisibilityArgs,
    SetLayerOpacityArgs.COMMAND: SetLayerOpacityArgs,
    SetLayerOrderArgs.COMMAND: SetLayerOrderArgs,
    ZoomToArgs.COMMAND: ZoomToArgs,
    SetTemporalConfigArgs.COMMAND: SetTemporalConfigArgs,
    StartAnimationArgs.COMMAND: StartAnimationArgs,
    StopAnimationArgs.COMMAND: StopAnimationArgs,
    InvalidateTilesArgs.COMMAND: InvalidateTilesArgs,
}
