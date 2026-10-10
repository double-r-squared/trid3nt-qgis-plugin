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
    ContractModel,
    ULIDStr,
    UTCDatetime,
    _validate_bbox,  # shared EPSG:4326 ordering rules for aoi_bbox
    new_ulid,
    now_utc,
)

__all__ = [
    "Envelope",
    "ErrorCode",
    "DrawnGeometry",
    "UserMessagePayload",
    "CancelPayload",
    "SessionResumePayload",
    "SpatialInputResponsePayload",
    "AgentMessageChunkPayload",
    "AgentThinkingChunkPayload",
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
    "ToolChoiceMode",
    "ToolCandidatesReason",
    "ToolCandidate",
    "ToolCandidatesPayload",
    "ToolChoicePayload",
    "LayerMode",
    "LayerRequestPayload",
    "LayerResponsePayload",
    "CLIENT_TO_AGENT_PAYLOADS",
    "AGENT_TO_CLIENT_PAYLOADS",
    "ALL_PAYLOADS",
]



PayloadT = TypeVar("PayloadT", bound=ContractModel)


class Envelope(ContractModel, Generic[PayloadT]):
    """The shared message envelope.
    ``type`` is the kebab-case discriminator, set by whichever side serializes;
    ``id`` and ``ts`` default to a fresh ULID and now."""

    type: str  # kebab-case discriminator (see ``*_TYPE`` on payloads)
    id: ULIDStr = Field(default_factory=new_ulid)
    ts: UTCDatetime = Field(default_factory=now_utc)
    session_id: ULIDStr
    # The Case owning this turn; None is no Case context and a client falls
    # back to submit-time routing.
    case_id: ULIDStr | None = None
    payload: PayloadT



ErrorCode = Literal[
    "AUTH_FAILED",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    # The provider's own message rides verbatim.
    "UPSTREAM_API_ERROR",
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
    # A declined gate card is the user's decision, not a failure; each gate has its own code.
    "PAYLOAD_WARNING_CANCELLED",
    "CODE_EXEC_CANCELLED",
    "SOLVER_CONFIRMATION_CANCELLED",
    # Distinct from LLM_UNAVAILABLE: still over the context window after one recompaction and retry.
    "CONTEXT_WINDOW_EXCEEDED",
]



# Routing-visibility mode only: "auto" selects tools autonomously (a card may surface on a measured
# ambiguity), "ask" surfaces a picker card. Consent gates are never mode-dependent.
ToolChoiceMode = Literal["auto", "ask"]


class DrawnGeometry(ContractModel):
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


class UserMessagePayload(ContractModel):
    """``user-message``: one user-submitted turn."""

    MESSAGE_TYPE: ClassVar[str] = "user-message"

    text: str
    # None uses the server default; sent on every message.
    model_id: str | None = None
    # The client's current Case is the authority for turn binding; None falls back to the server pointer.
    case_id: str | None = None
    # True forwards the reasoning channel's deltas as thinking chunks; adapters without one ignore it.
    show_thinking: bool | None = None
    # The per-turn AOI as [min_lon, min_lat, max_lon, max_lat] in EPSG:4326; the persistent Case bbox
    # has its own message. None leaves the server to infer location from the text.
    aoi_bbox: list[float] | None = None
    # None is auto; consent gates are unaffected either way.
    tool_choice_mode: ToolChoiceMode | None = None
    # A sub-region knob an input-review gate takes as user-supplied, distinct from aoi_bbox.
    # One rectangle, one turn - a client clears it on send.
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


class CancelPayload(ContractModel):
    """``cancel``: cancel the in-flight pipeline."""

    MESSAGE_TYPE: ClassVar[str] = "cancel"

    reason: str | None = None


class SessionResumePayload(ContractModel):
    """``session-resume``: resume the session the envelope names."""

    MESSAGE_TYPE: ClassVar[str] = "session-resume"

    # The server re-binds its active-Case pointer to this before replaying layers; None keeps its own.
    case_id: str | None = None




class SpatialInputResponsePayload(ContractModel):
    """``spatial-input-response``: the user picked a geometry, or cancelled.
    Three shapes on one payload: a point or bbox sets ``coordinates``, a draw
    sets a role-tagged ``features``, a cancellation sets neither. A point pick
    also carries the ``name`` the user gave it."""

    # No payload-size gate: a drawn collection is kilobytes by construction.

    MESSAGE_TYPE: ClassVar[str] = "spatial-input-response"

    request_id: ULIDStr
    geometry_type: Literal["point", "bbox", "vector_draw"] | None = None
    coordinates: list[float] | None = None
    features: dict[str, Any] | None = None
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
    # "line" is a neutral elevation or section LineString with no barrier semantics.
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




class AgentMessageChunkPayload(ContractModel):
    """``agent-message-chunk``: one streamed token group of an answer."""

    MESSAGE_TYPE: ClassVar[str] = "agent-message-chunk"

    message_id: ULIDStr
    delta: str  # new content since the last chunk (not accumulated)
    done: bool = False


class AgentThinkingChunkPayload(ContractModel):
    """``agent-thinking-chunk``: one streamed reasoning-channel token group.
    ``message_id`` is SHARED with the answer chunks of the same bubble, and an
    empty final frame closes the thinking stream for it."""

    MESSAGE_TYPE: ClassVar[str] = "agent-thinking-chunk"

    message_id: ULIDStr
    delta: str  # new reasoning content since the last chunk (not accumulated)
    done: bool = False



PipelineStepState = Literal["pending", "running", "complete", "failed", "cancelled"]


class PipelineStep(ContractModel):
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
    # Why a step ended red; the code set is open and the message is capped so a stack trace cannot ride out.
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=512)
    # Authoritative elapsed milliseconds from the two stamps; None while pending or running.
    duration_ms: int | None = Field(default=None, ge=0)
    # "compute" is the off-box solver card; its status is mirrored verbatim from the control plane.
    role: Literal["tool", "compute"] = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    # Engine and module of a compute card; both None on a plain tool card.
    engine: str | None = None
    module: str | None = None
    # parent_step_id marks a child a client nests; the substep fields sit on the parent and clear when it terminates.
    parent_step_id: ULIDStr | None = None
    substep_label: str | None = None
    substep_index: int | None = Field(default=None, ge=1)
    substep_total: int | None = Field(default=None, ge=1)


class PipelineStatePayload(ContractModel):
    """``pipeline-state``: the FULL snapshot of the current pipeline.
    It REPLACES a client's pipeline view outright; there are no deltas to
    reconcile."""

    MESSAGE_TYPE: ClassVar[str] = "pipeline-state"

    pipeline_id: ULIDStr
    steps: list[PipelineStep] = Field(default_factory=list)




class SolveProgressPayload(ContractModel):
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
    # Mirrored verbatim from the control plane; None when not bound to a batch job.
    phase: str | None = None




class ToolIoPayload(ContractModel):
    """``tool-io``: the RAW args and response for one dispatch, keyed by step.
    A pipeline step carries only label, state and timing; this sidecar makes the
    EXACT args and response visible, so a smoothed-over failure stays findable."""

    MESSAGE_TYPE: ClassVar[str] = "tool-io"

    #: Per-field truncation cap in bytes.
    MAX_FIELD_BYTES: ClassVar[int] = 32_768

    step_id: ULIDStr
    tool_name: str
    #: Pre-serialized JSON strings; a non-serializable value degrades to a string.
    raw_args: str = ""
    function_response: str = ""
    is_error: bool = False
    #: Original byte length beside the truncation flag.
    args_truncated: bool = False
    response_truncated: bool = False

    @classmethod
    def json_field(cls, value: Any) -> tuple[str, bool, int]:
        """One field serialized to ``(json_string, truncated, orig_bytes)``; never raises (an unserializable value
        degrades to ``str()``). The cut is on a UTF-8 byte boundary, so the prefix stays valid text but need not be valid JSON."""
        import json

        try:
            text = json.dumps(value, indent=2, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 -- never raise on serialization
            text = str(value)
        orig_bytes = len(text.encode("utf-8"))
        if orig_bytes <= cls.MAX_FIELD_BYTES:
            return text, False, orig_bytes
        cut = text.encode("utf-8")[: cls.MAX_FIELD_BYTES]
        return cut.decode("utf-8", errors="ignore"), True, orig_bytes
    args_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)




MapCommand = Literal[
    "load-layer",
    "zoom-to",
]


class MapCommandPayload(ContractModel):
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


class SessionStatePayload(ContractModel):
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


class ErrorPayload(ContractModel):
    """``error``: a global error, not tied to any one tool call."""

    MESSAGE_TYPE: ClassVar[str] = "error"

    error_code: ErrorCode
    message: str
    retryable: bool = False
    retry_after_seconds: int | None = None




class ReferenceLayer(ContractModel):
    """An optional helper layer shown only during a spatial-input request."""

    layer_id: str
    wms_url: str


class SuggestedView(ContractModel):
    """Where the client zooms to make picking easier."""

    bbox: BBox
    zoom: float


class SpatialInputRequestPayload(ContractModel):
    """``spatial-input-request``: the user is asked to pick a geometry.
    ``mode`` selects the affordance: a point returns one coordinate pair, a
    bbox returns four, and a draw returns a role-tagged ``FeatureCollection``.
    """

    MESSAGE_TYPE: ClassVar[str] = "spatial-input-request"

    request_id: ULIDStr
    mode: Literal["point", "bbox", "vector_draw"]
    title: str
    description: str
    # Draw affordance, on a draw request only: aoi, line (neutral, no barrier semantics), domain (the
    # closed run outline, tagged aoi), boundary run (a stretch of it, tagged line).
    purpose: Literal["aoi", "line", "domain", "boundary run"] = "aoi"
    suggested_view: SuggestedView | None = None
    reference_layers: list[ReferenceLayer] = Field(default_factory=list)
    default_timeout_seconds: int = 300


# Emitted in ask mode or on a measured retrieval near-tie. Fail-open: an unanswered request times
# out and the turn proceeds with the top pick.


ToolCandidatesReason = Literal["ambiguity", "ask_mode"]


class ToolCandidate(ContractModel):
    """One ranked tool candidate inside a ``tool-candidates`` request."""

    #: Registry tool name echoed verbatim; the tool is re-resolved from it.
    tool_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=500)
    #: The ranker's own score, verbatim and unconstrained (may be negative).
    score: float = 0.0


class ToolCandidatesPayload(ContractModel):
    """``tool-candidates``: agent -> client, the tool picker."""


    MESSAGE_TYPE: ClassVar[str] = "tool-candidates"

    request_id: ULIDStr
    stage_label: str = Field(min_length=1, max_length=120)
    #: Best-first; may be empty when retrieval degraded, leaving only free text.
    candidates: list[ToolCandidate] = Field(default_factory=list)
    reason: ToolCandidatesReason
    timeout_s: float = Field(default=60.0, gt=0)


class ToolChoicePayload(ContractModel):
    """``tool-choice``: client -> agent, the picker reply.
    One of three shapes: a ``tool_name`` pick echoed verbatim, ``free_text``
    guidance instead, or both ``None`` for let-the-agent-decide."""

    # tool_name wins if both arrive.

    MESSAGE_TYPE: ClassVar[str] = "tool-choice"

    request_id: ULIDStr
    tool_name: str | None = Field(default=None, max_length=200)
    free_text: str | None = Field(default=None, max_length=4096)




# The daemon borrows the session's QGIS data providers; the session only opens the layer and, for a
# materialised row, exports and uploads it.

LayerMode = Literal["open", "materialise"]


class LayerRequestPayload(ContractModel):
    """``layer-request``: one provider layer for the session to open, agent ->
    client. The uri is the provider's OWN datasource string, already templated
    over the bbox and the row's ask; the session passes it through verbatim."""

    MESSAGE_TYPE: ClassVar[str] = "layer-request"

    #: Names the uploaded object and correlates the pair.
    key: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=64)
    uri: str = Field(min_length=1, max_length=8192)
    name: str = Field(min_length=1, max_length=200)
    #: Camera on an opened overlay, export window on a materialised row; None is the whole layer.
    bbox: BBox | None = None
    #: Pixel spacing in metres; None is the layer's grid. The export honours it.
    resolution_m: float | None = Field(default=None, gt=0.0)
    mode: LayerMode
    #: Credential by name; the session attaches authcfg= so the key never reaches the daemon.
    #: A name nothing has stored is a refusal.
    credential: str | None = Field(default=None, min_length=1, max_length=120)


class LayerResponsePayload(ContractModel):
    """``layer-response``: what the session did with it, client -> agent.
    ``uri`` is the staged store object a materialised row was uploaded to, and is
    unset for an opened overlay; ``error`` is the provider's own text."""

    MESSAGE_TYPE: ClassVar[str] = "layer-response"

    key: str = Field(min_length=1, max_length=200)
    uri: str | None = Field(default=None, max_length=2048)
    error: str | None = Field(default=None, max_length=16 * 1024)


CLIENT_TO_AGENT_PAYLOADS: dict[str, type[ContractModel]] = {
    UserMessagePayload.MESSAGE_TYPE: UserMessagePayload,
    CancelPayload.MESSAGE_TYPE: CancelPayload,
    SessionResumePayload.MESSAGE_TYPE: SessionResumePayload,
    SpatialInputResponsePayload.MESSAGE_TYPE: SpatialInputResponsePayload,
    ToolChoicePayload.MESSAGE_TYPE: ToolChoicePayload,
    LayerResponsePayload.MESSAGE_TYPE: LayerResponsePayload,
}

from .secrets import (  # noqa: E402 - imported below the dict literals
    SECRET_AGENT_TO_CLIENT_PAYLOADS,
    SECRET_CLIENT_TO_AGENT_PAYLOADS,
)

from .payload_warning import (  # noqa: E402
    PayloadConfirmationEnvelopePayload,
    PayloadWarningEnvelopePayload,
)

CLIENT_TO_AGENT_PAYLOADS.update(SECRET_CLIENT_TO_AGENT_PAYLOADS)
CLIENT_TO_AGENT_PAYLOADS[
    PayloadConfirmationEnvelopePayload.MESSAGE_TYPE
] = PayloadConfirmationEnvelopePayload

AGENT_TO_CLIENT_PAYLOADS: dict[str, type[ContractModel]] = {
    AgentMessageChunkPayload.MESSAGE_TYPE: AgentMessageChunkPayload,
    AgentThinkingChunkPayload.MESSAGE_TYPE: AgentThinkingChunkPayload,
    PipelineStatePayload.MESSAGE_TYPE: PipelineStatePayload,
    SolveProgressPayload.MESSAGE_TYPE: SolveProgressPayload,
    ToolIoPayload.MESSAGE_TYPE: ToolIoPayload,
    MapCommandPayload.MESSAGE_TYPE: MapCommandPayload,
    SessionStatePayload.MESSAGE_TYPE: SessionStatePayload,
    ErrorPayload.MESSAGE_TYPE: ErrorPayload,
    SpatialInputRequestPayload.MESSAGE_TYPE: SpatialInputRequestPayload,
    ToolCandidatesPayload.MESSAGE_TYPE: ToolCandidatesPayload,
    LayerRequestPayload.MESSAGE_TYPE: LayerRequestPayload,
}
AGENT_TO_CLIENT_PAYLOADS.update(SECRET_AGENT_TO_CLIENT_PAYLOADS)
AGENT_TO_CLIENT_PAYLOADS[
    PayloadWarningEnvelopePayload.MESSAGE_TYPE
] = PayloadWarningEnvelopePayload

from .chart_contracts import (  # noqa: E402
    CHART_AGENT_TO_CLIENT_PAYLOADS,
)

AGENT_TO_CLIENT_PAYLOADS.update(CHART_AGENT_TO_CLIENT_PAYLOADS)

from .processing_contracts import (  # noqa: E402
    PROCESSING_AGENT_TO_CLIENT_PAYLOADS,
    PROCESSING_CLIENT_TO_AGENT_PAYLOADS,
)

AGENT_TO_CLIENT_PAYLOADS.update(PROCESSING_AGENT_TO_CLIENT_PAYLOADS)
CLIENT_TO_AGENT_PAYLOADS.update(PROCESSING_CLIENT_TO_AGENT_PAYLOADS)

ALL_PAYLOADS: dict[str, type[ContractModel]] = {
    **CLIENT_TO_AGENT_PAYLOADS,
    **AGENT_TO_CLIENT_PAYLOADS,
}
