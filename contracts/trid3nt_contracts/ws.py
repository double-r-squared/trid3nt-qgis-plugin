"""The WebSocket protocol: the envelope and every message payload.

Every message shares one envelope - ``type`` kebab-case, ``id`` a ULID, ``ts``
ISO-8601 ``Z``, ``payload`` always an object. No payload here carries a cost
field, and ``cancelled`` is a distinct step state, never folded into failed.
"""

from __future__ import annotations

from typing import Any, ClassVar, Generic, Literal, TypeVar

from pydantic import Field, field_validator

from .common import (
    BBox,
    GraceModel,
    ULIDStr,
    UTCDatetime,
    _validate_bbox,  # shared EPSG:4326 ordering rules for aoi_bbox
    new_ulid,
    now_utc,
)

__all__ = [
    "Envelope",
    "ErrorCode",
    # client -> agent
    "DrawnGeometry",
    "UserMessagePayload",
    "CancelPayload",
    "SessionResumePayload",
    # client -> agent, user-input replies
    "SpatialInputResponsePayload",
    # agent -> client
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
    "SessionStateStatus",
    "SessionStatePayload",
    "ErrorPayload",
    "ReferenceLayer",
    "SuggestedView",
    "SpatialInputRequestPayload",
    # the tool-selection picker
    "ToolChoiceMode",
    "ToolCandidatesReason",
    "ToolCandidate",
    "ToolCandidatesPayload",
    "ToolChoicePayload",
    # map-command args
    "LoadLayerArgs",
    "ZoomToArgs",
    "SetTemporalConfigArgs",
    "MapTemporal",
    # registry
    "CLIENT_TO_AGENT_PAYLOADS",
    "AGENT_TO_CLIENT_PAYLOADS",
    "ALL_PAYLOADS",
]



PayloadT = TypeVar("PayloadT", bound=GraceModel)


class Envelope(GraceModel, Generic[PayloadT]):
    """The shared message envelope.
    ``type`` is the kebab-case discriminator, set by whichever side serializes;
    ``id`` and ``ts`` default to a fresh ULID and now."""

    type: str  # kebab-case discriminator (see ``*_TYPE`` on payloads)
    id: ULIDStr = Field(default_factory=new_ulid)
    ts: UTCDatetime = Field(default_factory=now_utc)
    session_id: ULIDStr
    # The Case that OWNS the turn this envelope belongs to, when one is bound.
    # A client routes a live envelope to that Case's stream: "the stream the
    # user last messaged" misattributes a still-running turn the moment the user
    # switches Cases. ``None`` is no Case context - a root turn, a lifecycle
    # envelope - and a client falls back to submit-time routing.
    case_id: ULIDStr | None = None
    payload: PayloadT



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
    # A prompt that stayed over the model's context window even after one
    # recompaction and retry. DISTINCT from LLM_UNAVAILABLE: a genuinely
    # oversized Case, not a transient outage, so the honest guidance is to start
    # a new case or switch models rather than to retry.
    "CONTEXT_WINDOW_EXCEEDED",
]



# The ROUTING-VISIBILITY mode for a turn. It governs ONLY whether tool
# selection is surfaced as a picker card. The CONSENT surface - payload
# warnings, granularity, solver confirm, code-exec approval, credential entry,
# region choice, spatial input - is NEVER mode-dependent: a gate answers "may I
# do this", a mode answers "which tool", and the two layers never mix.
#
# - ``"auto"``: selection is autonomous, with no pick card EXCEPT on a MEASURED
#   ambiguity signal, where a card may still surface.
# - ``"ask"``: selection is surfaced as a card, staged along the analysis flow.
ToolChoiceMode = Literal["auto", "ask"]


class DrawnGeometry(GraceModel):
    """A user-drawn geometry attached to a ``user-message``.
    A per-turn STRUCTURED knob, distinct from the analysis AOI, consumed as
    user-supplied. Only a rectangle is wired; the discriminator leaves room."""

    geometry_type: Literal["rectangle"] = "rectangle"
    bbox: list[float]

    @field_validator("bbox")
    @classmethod
    def _validate_drawn_bbox(cls, value: list[float]) -> list[float]:
        """Exactly 4 floats in ``common.BBox`` EPSG:4326 order."""
        if len(value) != 4:
            raise ValueError(
                "drawn_geometry.bbox must be [min_lon, min_lat, max_lon, "
                f"max_lat] (exactly 4 floats), got {len(value)}: {value!r}"
            )
        min_lon, min_lat, max_lon, max_lat = value
        _validate_bbox((min_lon, min_lat, max_lon, max_lat))
        return [float(v) for v in value]


class UserMessagePayload(GraceModel):
    """``user-message``: one user-submitted turn."""

    MESSAGE_TYPE: ClassVar[str] = "user-message"

    text: str
    # The model chosen for THIS turn. ``None`` uses the server default. Sent on
    # every message, so the model can change between turns with no restart.
    model_id: str | None = None
    # The Case the CLIENT is currently in. It is the AUTHORITY for turn
    # binding, so a turn runs in the Case the user is looking at rather than
    # against a stale server-side pointer. ``None`` falls back to that pointer.
    case_id: str | None = None
    # True enables the model's reasoning channel for this turn, and the server
    # forwards its deltas as thinking chunks. ``None`` suppresses it. Only an
    # adapter with a reasoning channel reads this; the rest ignore it.
    show_thinking: bool | None = None
    # The client's current AOI, ``[min_lon, min_lat, max_lon, max_lat]`` in
    # EPSG:4326 - a STRUCTURED knob rather than a prose line inside ``text``.
    # ``None`` leaves the server to infer location from the text. A plain
    # 4-float list, not a ``BBox``, so a consumer reads the wire shape directly.
    # This is the PER-TURN AOI; the persistent Case bbox has its own message.
    aoi_bbox: list[float] | None = None
    # The routing-visibility mode for THIS turn. There is no session-config
    # message: the per-message carrier IS the config path. ``None`` is treated
    # as auto, and consent gates are unaffected either way.
    tool_choice_mode: ToolChoiceMode | None = None
    # A geometry drawn for THIS turn, distinct from ``aoi_bbox``: the AOI is
    # the analysis extent, this is a sub-region KNOB an input-review gate takes
    # as user-supplied. ``None`` leaves a composer on its own proposal. One
    # rectangle, one turn - a client clears it on send.
    drawn_geometry: DrawnGeometry | None = None

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
    """``cancel``: cancel the in-flight pipeline."""

    MESSAGE_TYPE: ClassVar[str] = "cancel"

    reason: str | None = None


class SessionResumePayload(GraceModel):
    """``session-resume``: resume the session the envelope names."""

    MESSAGE_TYPE: ClassVar[str] = "session-resume"

    # The Case the CLIENT is currently in, stamped on reconnect. The server
    # RE-BINDS its active-Case pointer to this before replaying layers, so a
    # reconnect replays the Case the user is actually in: a selection tapped
    # mid-reconnect never reaches the server, and a resume carrying nothing
    # would replay a stale one. ``None`` keeps the server's own pointer.
    case_id: str | None = None




class SpatialInputResponsePayload(GraceModel):
    """``spatial-input-response``: the user picked a geometry, or cancelled.
    Three shapes on one payload: a point or bbox sets ``coordinates``, a draw
    sets a role-tagged ``features``, a cancellation sets neither. A point pick
    also carries the ``name`` the user gave it."""

    # No payload-size gate applies here: a drawn collection is kilobytes by
    # construction, and the warn/block discipline governs TOOL OUTPUT.

    MESSAGE_TYPE: ClassVar[str] = "spatial-input-response"

    request_id: ULIDStr
    geometry_type: Literal["point", "bbox", "vector_draw"] | None = None
    coordinates: list[float] | None = None
    features: dict[str, Any] | None = None
    #: What the user called the picked point; the slot that asked carries it on.
    name: str | None = None
    cancelled: bool = False

    @field_validator("features")
    @classmethod
    def _validate_features(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """STRUCTURE only, so this package needs no geometry library: every
        feature carries a known ``properties.role``, and a ``"line"`` is a
        ``LineString`` with at least two positions."""
        if value is None:
            return None
        return _validate_spatial_input_feature_collection(value)


def _validate_spatial_input_feature_collection(
    fc: dict[str, Any],
) -> dict[str, Any]:
    """Enforce the role vocabulary on a drawn FeatureCollection.
    A pure-structure check - nothing here parses geometry."""
    if fc.get("type") != "FeatureCollection":
        raise ValueError(
            f"features must be a GeoJSON FeatureCollection, "
            f"got type={fc.get('type')!r}"
        )
    feats = fc.get("features")
    if not isinstance(feats, list):
        raise ValueError("features.features must be a list")
    # "line" is a NEUTRAL elevation or section LineString - it carries no
    # barrier semantics.
    valid_roles = {"aoi", "point", "line"}
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
            # A plain LineString with at least two positions. No barrier type
            # is required or read.
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
    return fc




class AgentMessageChunkPayload(GraceModel):
    """``agent-message-chunk``: one streamed token group of an answer."""

    MESSAGE_TYPE: ClassVar[str] = "agent-message-chunk"

    message_id: ULIDStr
    delta: str  # new content since the last chunk (not accumulated)
    done: bool = False


class AgentThinkingChunkPayload(GraceModel):
    """``agent-thinking-chunk``: one streamed reasoning-channel token group.
    ``message_id`` is SHARED with the answer chunks of the same bubble, and an
    empty final frame closes the thinking stream for it."""

    MESSAGE_TYPE: ClassVar[str] = "agent-thinking-chunk"

    message_id: ULIDStr
    delta: str  # new reasoning content since the last chunk (not accumulated)
    done: bool = False


class ToolCallStartPayload(GraceModel):
    """``tool-call-start``: a tool invocation has begun."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-start"

    call_id: ULIDStr
    step_id: ULIDStr
    tool_name: str
    # An OPEN enum: a receiver must tolerate a category it does not know.
    tool_category: str
    params: dict = Field(default_factory=dict)  # sanitized


class ToolCallProgressPayload(GraceModel):
    """``tool-call-progress``: optional progress for an in-flight tool."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-progress"

    call_id: ULIDStr
    percent: int | None = Field(default=None, ge=0, le=100)
    status: str | None = None


class ToolCallCompletePayload(GraceModel):
    """``tool-call-complete``: a tool finished successfully.
    ``metrics`` is tool-specific structured data - the numbers a narrative cites
    live there. The full result body is referenced, never inlined."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-complete"

    call_id: ULIDStr
    result_summary: str  # a human-readable one-liner
    result_uri: str | None = None  # set when the result is a stored artifact
    metrics: dict = Field(default_factory=dict)


class ToolCallFailedPayload(GraceModel):
    """``tool-call-failed``: a tool errored out."""

    MESSAGE_TYPE: ClassVar[str] = "tool-call-failed"

    call_id: ULIDStr
    error_code: str  # an enum-like string; the set is open
    message: str  # human-readable
    retryable: bool = False


# pipeline-state ------------------------------------------------------------ #

# ``cancelled`` is a distinct terminal state, never folded into failed.
PipelineStepState = Literal["pending", "running", "complete", "failed", "cancelled"]


class PipelineStep(GraceModel):
    """One step in the pipeline snapshot.
    Every number here is deterministic - measured or mirrored from a control
    plane - and never a model's estimate.
    """

    step_id: ULIDStr
    name: str
    tool_name: str
    state: PipelineStepState
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    # The AUTHORITATIVE wall-clock elapsed time, stamped on the terminal
    # transition and derived from the two stamps above. ``None`` while pending
    # or running, so a client may tick cosmetically until this lands and then
    # locks to it. Milliseconds, so a sub-second tool reads honestly.
    duration_ms: int | None = Field(default=None, ge=0)
    # The card KIND. ``"tool"`` is an on-box tool card; ``"compute"`` is the
    # off-box solver card, bound to a batch job whose status is mirrored
    # VERBATIM from the control plane rather than interpreted.
    role: Literal["tool", "compute"] = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    # WHICH engine, and which of its modules, the compute card is a run of. A
    # run is identified by these two and never by the question it answered; both
    # are ``None`` on a plain tool card, which is a run of nothing.
    engine: str | None = None
    module: str | None = None
    # The nested sub-step timeline. ``parent_step_id`` is set on a CHILD, and a
    # client NESTS such a step instead of rendering it top-level. The three
    # substep fields are set on the PARENT and describe the currently-running
    # child for a live breadcrumb; they are cleared when the parent terminates.
    # ``substep_total`` is ``None`` when the child count is not yet known.
    parent_step_id: ULIDStr | None = None
    substep_label: str | None = None
    substep_index: int | None = Field(default=None, ge=1)
    substep_total: int | None = Field(default=None, ge=1)


class PipelineStatePayload(GraceModel):
    """``pipeline-state``: the FULL snapshot of the current pipeline.
    It REPLACES a client's pipeline view outright; there are no deltas to
    reconcile."""

    MESSAGE_TYPE: ClassVar[str] = "pipeline-state"

    pipeline_id: ULIDStr
    steps: list[PipelineStep] = Field(default_factory=list)


# solve-progress ------------------------------------------------------------ #


class SolveProgressPayload(GraceModel):
    """``solve-progress``: one LIVE telemetry tick during a long solver run.
    Every field is solver- or perf-model-sourced, and ``eta_seconds`` is ``None``
    when nothing can estimate it rather than a fabricated number."""

    MESSAGE_TYPE: ClassVar[str] = "solve-progress"

    run_id: str
    solver: str
    grid_resolution_m: float | None = None
    active_cell_count: int | None = None
    vcpus: int | None = None
    elapsed_seconds: float = Field(ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    # The batch status this tick reflects, mirrored VERBATIM from the control
    # plane. ``None`` for a tick not bound to a batch job.
    phase: str | None = None


# tool-io ------------------------------------------------------------------- #


class ToolIoPayload(GraceModel):
    """``tool-io``: the RAW args and response for one dispatch, keyed by step.
    A pipeline step carries only label, state and timing; this sidecar makes the
    EXACT args and response visible, so a smoothed-over failure stays findable."""

    MESSAGE_TYPE: ClassVar[str] = "tool-io"

    #: Server-side truncation cap per field (bytes). Keeps the chat light; the
    #: expander is a debugging affordance, not a data-transfer channel.
    MAX_FIELD_BYTES: ClassVar[int] = 32_768

    #: Matches the step of the dispatch's own card, so a client merges this
    #: onto the right card by id.
    step_id: ULIDStr
    tool_name: str
    #: Pre-serialized JSON STRINGS: a non-serializable value degrades to a
    #: string rather than breaking the envelope.
    raw_args: str = ""
    function_response: str = ""
    #: Mirrors the honesty signal, so the response block can be styled without
    #: re-parsing the JSON.
    is_error: bool = False
    #: A truncation flag with the ORIGINAL byte length beside it, so a reader
    #: can say honestly how much is missing.
    args_truncated: bool = False
    response_truncated: bool = False
    args_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)


# map-command --------------------------------------------------------------- #


class MapTemporal(GraceModel):
    """Temporal block for ``load-layer`` args."""

    start: UTCDatetime
    end: UTCDatetime
    step_seconds: int = Field(gt=0)


class LoadLayerArgs(GraceModel):
    """``load-layer`` args. Field-for-field alignable with ``LayerURI``."""

    COMMAND: ClassVar[str] = "load-layer"

    layer_id: str
    temporal: MapTemporal | None = None


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


# map-command command vocabulary (open enum).
MapCommand = Literal[
    "load-layer",
    "zoom-to",
]


class MapCommandPayload(GraceModel):
    """``map-command``: one umbrella message with a ``command`` discriminator.
    ``args`` stays a dict on the wire; a consumer validates it against the
    matching args model selected by ``command``."""

    MESSAGE_TYPE: ClassVar[str] = "map-command"

    command: MapCommand
    args: dict = Field(default_factory=dict)


SessionStateStatus = Literal["active", "max_turns_reached"]
"""Session status at the moment a ``session-state`` is sent. ``active`` is
normal; ``max_turns_reached`` means no further tool call will be dispatched and
a new session is required."""


class SessionStatePayload(GraceModel):
    """``session-state``: everything a client needs to reconstruct a session.
    The nested fields are the JSON serialization of the collection models,
    carried as plain dicts here to keep this module acyclic.
    """

    MESSAGE_TYPE: ClassVar[str] = "session-state"

    chat_history: list[dict] = Field(default_factory=list)
    loaded_layers: list[dict] = Field(default_factory=list)
    pipeline_history: list[dict] = Field(default_factory=list)
    current_pipeline: dict | None = None
    map_view: dict | None = None
    status: SessionStateStatus = "active"


class ErrorPayload(GraceModel):
    """``error``: a global error, not tied to any one tool call."""

    MESSAGE_TYPE: ClassVar[str] = "error"

    error_code: ErrorCode
    message: str
    retryable: bool = False
    retry_after_seconds: int | None = None


# spatial-input-request ----------------------------------------------------- #


class ReferenceLayer(GraceModel):
    """An optional helper layer shown only during a spatial-input request."""

    layer_id: str
    wms_url: str


class SuggestedView(GraceModel):
    """Where the client zooms to make picking easier."""

    bbox: BBox
    zoom: float


class SpatialInputRequestPayload(GraceModel):
    """``spatial-input-request``: the user is asked to pick a geometry.
    ``mode`` selects the affordance: a point returns one coordinate pair, a
    bbox returns four, and a draw returns a role-tagged ``FeatureCollection``.
    """

    MESSAGE_TYPE: ClassVar[str] = "spatial-input-request"

    request_id: ULIDStr
    mode: Literal["point", "bbox", "vector_draw"]
    title: str
    description: str
    # Selects the draw affordance and its semantics, on a draw request only.
    #
    # - ``"aoi"`` - region selection: only the area tools are offered and submit
    #   gates on at least one polygon, returned tagged as an aoi.
    # - ``"line"`` - a NEUTRAL elevation or section line, returned tagged as a
    #   line. It carries no barrier semantics.
    purpose: Literal["aoi", "line"] = "aoi"
    suggested_view: SuggestedView | None = None
    reference_layers: list[ReferenceLayer] = Field(default_factory=list)
    default_timeout_seconds: int = 300


# A routing tie is a real error species: a plausible-but-wrong tool sends the
# turn down a path one click could have prevented. The request is emitted either
# because the turn runs in ask mode or because auto mode MEASURED a retrieval
# near-tie, and ``reason`` says which.
#
# FAIL-OPEN: an unanswered request times out server-side and the turn proceeds
# with the top-ranked pick. The workflow never blocks on the card.


#: Why the picker surfaced: a MEASURED retrieval near-tie, or ask mode
#: surfacing every staged selection.
ToolCandidatesReason = Literal["ambiguity", "ask_mode"]


class ToolCandidate(GraceModel):
    """One ranked tool candidate inside a ``tool-candidates`` request."""

    #: The registry tool name, echoed VERBATIM by the reply. The tool is
    #: re-resolved from it, so a client never invents one.
    tool_name: str = Field(min_length=1, max_length=200)
    #: A one-line summary rendered beside the name. May be empty.
    summary: str = Field(default="", max_length=500)
    #: The retrieval ranker's own score, verbatim, never an estimate.
    #: UNCONSTRAINED: backends differ and a similarity may be negative.
    #: Candidates arrive best-first, so this only shows the margin.
    score: float = 0.0


class ToolCandidatesPayload(GraceModel):
    """``tool-candidates``: agent -> client, the tool picker."""


    MESSAGE_TYPE: ClassVar[str] = "tool-candidates"

    #: Correlates the request, the reply and the paused selection. Echoed
    #: verbatim.
    request_id: ULIDStr
    #: The analysis-flow stage this pick belongs to, so staged waves read as a
    #: narrative rather than a flood.
    stage_label: str = Field(min_length=1, max_length=120)
    #: Ranked best-first. MAY be EMPTY when retrieval degraded: the card then
    #: offers only free text and let-the-agent-decide - an honest degrade rather
    #: than an invented list.
    candidates: list[ToolCandidate] = Field(default_factory=list)
    reason: ToolCandidatesReason
    #: How long the SERVER waits before proceeding with its own top pick.
    timeout_s: float = Field(default=60.0, gt=0)


class ToolChoicePayload(GraceModel):
    """``tool-choice``: client -> agent, the picker reply.
    One of three shapes: a ``tool_name`` pick echoed verbatim, ``free_text``
    guidance instead, or both ``None`` for let-the-agent-decide."""

    # The two should not both arrive; ``tool_name`` wins if they do, being the
    # stronger signal.

    MESSAGE_TYPE: ClassVar[str] = "tool-choice"

    request_id: ULIDStr
    tool_name: str | None = Field(default=None, max_length=200)
    free_text: str | None = Field(default=None, max_length=4096)



CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    UserMessagePayload.MESSAGE_TYPE: UserMessagePayload,
    CancelPayload.MESSAGE_TYPE: CancelPayload,
    SessionResumePayload.MESSAGE_TYPE: SessionResumePayload,
    SpatialInputResponsePayload.MESSAGE_TYPE: SpatialInputResponsePayload,
    ToolChoicePayload.MESSAGE_TYPE: ToolChoicePayload,
}

# The per-module payload fragments are splatted into the routing dicts HERE:
# each module owns its typed payloads, and this module owns the registry.
from .secrets import (  # noqa: E402 - imported below the dict literals
    SECRET_AGENT_TO_CLIENT_PAYLOADS,
    SECRET_CLIENT_TO_AGENT_PAYLOADS,
)

# The payload warning is agent -> client; its confirmation is client -> agent.
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
    SessionStatePayload.MESSAGE_TYPE: SessionStatePayload,
    ErrorPayload.MESSAGE_TYPE: ErrorPayload,
    SpatialInputRequestPayload.MESSAGE_TYPE: SpatialInputRequestPayload,
    ToolCandidatesPayload.MESSAGE_TYPE: ToolCandidatesPayload,
}
AGENT_TO_CLIENT_PAYLOADS.update(SECRET_AGENT_TO_CLIENT_PAYLOADS)
AGENT_TO_CLIENT_PAYLOADS[
    PayloadWarningEnvelopePayload.MESSAGE_TYPE
] = PayloadWarningEnvelopePayload

# The chart emission is agent -> client.
from .chart_contracts import (  # noqa: E402
    CHART_AGENT_TO_CLIENT_PAYLOADS,
)

AGENT_TO_CLIENT_PAYLOADS.update(CHART_AGENT_TO_CLIENT_PAYLOADS)

# Both code-exec envelopes are agent -> client; the approval REPLY rides the
# existing payload-confirmation message rather than a new shape.
from .sandbox_contracts import (  # noqa: E402
    SANDBOX_AGENT_TO_CLIENT_PAYLOADS,
)

AGENT_TO_CLIENT_PAYLOADS.update(SANDBOX_AGENT_TO_CLIENT_PAYLOADS)

# The region-narrowing request is agent -> client and its reply is
# client -> agent.
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
    ZoomToArgs.COMMAND: ZoomToArgs,
}
