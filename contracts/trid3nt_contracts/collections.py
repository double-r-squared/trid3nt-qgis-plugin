"""Document-store collection schemas - the metadata side of a metadata/payload
split, where the object store holds payloads keyed by URIs recorded here.

pydantic forbids a field literally named ``_id``, so each document model exposes
``id`` aliased to it and dumps with ``by_alias=True``. No cost field lives on any of these.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from .common import ContractModel, ULIDStr, UTCDatetime
from .execution import LegendKey

#: SCREAMING_SNAKE_CASE error-code pattern. The SET is open: a code is validated
#: by SHAPE and never against a registry, so a workflow registers its own codes
#: without a schema change.
_ERROR_CODE_RE: re.Pattern[str] = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")

#: Cap on ``error_message`` length, to discourage stack-trace leakage.
_ERROR_MESSAGE_MAX_LEN: int = 512

__all__ = [
    "DocModel",
    "ProjectLayerSummary",
    "ChatMessage",
    "ToolCallSummary",
    "PipelineStepSummary",
    "PipelineSnapshot",
    "MapView",
    "SessionDocument",
    "SESSIONS_TTL",
]


class DocModel(ContractModel):
    """Base for collection documents that alias ``_id``.
    ``populate_by_name`` lets the id be set as either ``id`` or ``_id``, and
    dumped back to ``_id`` under ``by_alias``."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )




class ProjectLayerSummary(ContractModel):
    """Denormalized layer entry on a project, and on session map state.
    One store, one scheme: ``uri`` is the layer's ONE reference. The remaining
    fields mirror the produced layer, so a client re-registers without a join.
    """

    layer_id: str
    name: str
    # ``mesh`` is an unstructured solver mesh, STAGED rather than streamed.
    layer_type: Literal["raster", "vector", "mesh"]
    uri: str
    visible: bool
    role: Literal["primary", "context", "input"]
    #: WHERE the layer came from, read by a person. ``user`` is a file the user
    #: pushed in themselves; ``None`` is the system saying nothing.
    origin: Literal["user"] | None = None
    temporal: bool  # computed: the row states its own [valid_from, valid_to)

    # Layer-stack arbitration. A client absent both falls back to fully opaque
    # and its own default order.
    opacity: float | None = None     # 0.0-1.0
    z_index: int | None = None       # lower draws first

    # The CRS for a ``layer_type="mesh"`` row: a mesh reader reports an empty
    # CRS for these formats, so the run has to state it. ``None`` otherwise.
    crs_authid: str | None = None

    # The MDAL dataset files a mesh row carries beside the groups its own file
    # holds, loaded onto the layer before its declared group is bound. ``[]``
    # for every other row.
    dataset_uris: list[str] = Field(default_factory=list)

    # WHICH of a mesh's dataset groups this row paints. One mesh file carries
    # many, and a run publishes one layer per group it wants read, so the group
    # is half of what identifies a mesh row. ``None`` for every other row.
    dataset_group: str | None = None

    # The instant a mesh row's dataset times are counted from. A SELAFIN records
    # no origin, so without it a scrubber reads 1900. ``None`` otherwise.
    reference_time: str | None = None

    # One frame of an ordered sequence states its own window, ISO-8601 UTC, and
    # the map stamps it as the layer's fixed temporal range. ``None`` for a
    # layer that is not one frame.
    valid_from: str | None = None
    valid_to: str | None = None

    #: The layer's RESOLVED style: the concrete range, the colours and the
    #: document the map loads.
    legend: LegendKey | None = None

    # The physical quantity, as the producer names it - a layer's identity, and
    # what a still, a frame and an animation of ONE field are matched by when
    # they are held to a single scale. ``None`` when none was declared.
    quantity: str | None = None

    # WHICH tracer of the run this row carries, counted from 1 in the order the
    # deck declares them. A tracer's NAME is the run's - a named release renames
    # it - so its position is the only stable way to ask for one. ``None`` on
    # every row that is not a tracer.
    tracer: int | None = None


class ToolCallSummary(ContractModel):
    """A completed/failed/cancelled tool call recorded in chat history."""

    call_id: ULIDStr
    tool_name: str
    state: Literal["complete", "failed", "cancelled"]
    result_summary: str | None = None
    result_uri: str | None = None
    error_code: str | None = None
    started_at: UTCDatetime
    completed_at: UTCDatetime | None = None


class ChatMessage(ContractModel):
    """One chat turn. ``message_id`` matches the wire id for an agent turn."""

    message_id: ULIDStr
    role: Literal["user", "agent"]
    content: str  # for an agent turn, the text accumulated after streaming
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    created_at: UTCDatetime


class PipelineStepSummary(ContractModel):
    """A step in a persisted pipeline snapshot, ``cancelled`` distinct from
    ``failed``. Every number here is measured, never an estimate, and no cost
    field appears.
    """

    step_id: ULIDStr
    name: str
    tool_name: str
    state: Literal["pending", "running", "complete", "failed", "cancelled"]
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    #: Populated only where a workflow can genuinely attribute progress -
    #: chunk N of M, row n of M. Optional everywhere, and never estimated.
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    #: Populated only when the step FAILED. The code set is open and validated
    #: by shape; the message is short and capped to discourage a stack trace.
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX_LEN)
    #: The AUTHORITATIVE wall-clock elapsed time, derived from the two stamps
    #: above at the terminal transition. ``None`` while pending or running.
    duration_ms: int | None = Field(default=None, ge=0)
    # The card-kind discriminator and the compute binding, mirrored from the
    # live step, so a replayed snapshot carries the off-box solver card across a
    # reconnect. ``"compute"`` is the solver card; ``batch_status`` mirrors the
    # backend's own status VERBATIM rather than being interpreted.
    role: Literal["tool", "compute"] = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    # The nested sub-step timeline, mirrored from the live step so a replay
    # carries it across a reconnect. ``parent_step_id`` marks a CHILD; the three
    # substep fields are the PARENT's live breadcrumb, cleared when the parent
    # reaches a terminal state.
    parent_step_id: ULIDStr | None = None
    substep_label: str | None = None
    substep_index: int | None = Field(default=None, ge=1)
    substep_total: int | None = Field(default=None, ge=1)

    @field_validator("error_code")
    @classmethod
    def _validate_error_code_shape(cls, value: str | None) -> str | None:
        """Shape only - the code SET is open, so nothing is checked against a
        registry."""
        if value is None:
            return value
        if not _ERROR_CODE_RE.match(value):
            raise ValueError(
                f"error_code must be SCREAMING_SNAKE_CASE (matching {_ERROR_CODE_RE.pattern!r}); "
                f"got {value!r}"
            )
        return value


class PipelineSnapshot(ContractModel):
    """A persisted pipeline run."""

    pipeline_id: ULIDStr
    started_at: UTCDatetime
    completed_at: UTCDatetime | None = None
    final_state: Literal["complete", "failed", "cancelled"] | None = None
    steps: list[PipelineStepSummary] = Field(default_factory=list)


class MapView(ContractModel):
    """Current client map view."""

    center: tuple[float, float]  # [lon, lat]
    zoom: float
    bbox: tuple[float, float, float, float]


class SessionDocument(DocModel):
    """``sessions``: chat session state, TTL-cleaned on ``expires_at``."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")  # the session id
    client_fingerprint: str | None = None  # an opaque client identifier

    created_at: UTCDatetime
    last_active_at: UTCDatetime
    expires_at: UTCDatetime  # drives TTL cleanup; bumped on each interaction

    chat_history: list[ChatMessage] = Field(default_factory=list)
    project_ids: list[ULIDStr] = Field(default_factory=list)
    pipeline_history: list[PipelineSnapshot] = Field(default_factory=list)
    current_pipeline: PipelineSnapshot | None = None

    loaded_layers: list[ProjectLayerSummary] = Field(default_factory=list)
    map_view: MapView | None = None


#: TTL index spec for sessions: a document is deleted 30 days after
#: ``expires_at``. This is the CONTRACT; provisioning creates the index.
SESSIONS_TTL: dict[str, Any] = {
    "collection": "sessions",
    "field": "expires_at",
    "expire_after_seconds": 30 * 24 * 60 * 60,  # 30 days past expires_at
}
