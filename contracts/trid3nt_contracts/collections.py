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

#: SCREAMING_SNAKE_CASE error-code pattern; the set is open, validated by shape and never against a registry.
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
    # ``mesh`` is staged rather than streamed.
    layer_type: Literal["raster", "vector", "mesh"]
    uri: str
    visible: bool
    role: Literal["primary", "context", "input"]
    origin: Literal["user"] | None = None
    temporal: bool  # computed: the row states its own [valid_from, valid_to)

    # Layer-stack arbitration; a client given neither is fully opaque in its own default order.
    opacity: float | None = None     # 0.0-1.0
    z_index: int | None = None       # lower draws first

    # CRS for a mesh row, whose reader reports an empty CRS; None otherwise.
    crs_authid: str | None = None

    # MDAL dataset files a mesh row carries beside its own groups, loaded before its declared group is bound; [] otherwise.
    dataset_uris: list[str] = Field(default_factory=list)

    # Which dataset group of the mesh this row paints; a run publishes one layer per group, so the group is half the row's identity.
    dataset_group: str | None = None

    # The instant a mesh row's dataset times are counted from (a SELAFIN records no origin); None otherwise.
    reference_time: str | None = None

    # ISO-8601 UTC window of one frame of an ordered sequence, stamped as the layer's fixed temporal range; None otherwise.
    valid_from: str | None = None
    valid_to: str | None = None

    legend: LegendKey | None = None

    # The physical quantity: the layer's identity, by which a still, a frame and an animation of one field share a scale.
    quantity: str | None = None

    # Which tracer, counted from 1 in the deck's order; a tracer's name is the run's, so position is the stable key.
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
    #: Only where a workflow can genuinely attribute progress; never estimated.
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    #: Only when the step failed; the code set is open and validated by shape, the message capped against stack traces.
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX_LEN)
    #: Authoritative elapsed time from the two stamps above; None while pending or running.
    duration_ms: int | None = Field(default=None, ge=0)
    # Mirrored from the live step so a replay carries the off-box solver card; ``batch_status`` is the backend's own, verbatim.
    role: Literal["tool", "compute"] = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    # Mirrored from the live step; ``parent_step_id`` marks a child, the substep fields are the parent's breadcrumb, cleared at terminal.
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


#: TTL index spec: a document is deleted 30 days after ``expires_at``; provisioning creates the index.
SESSIONS_TTL: dict[str, Any] = {
    "collection": "sessions",
    "field": "expires_at",
    "expire_after_seconds": 30 * 24 * 60 * 60,  # 30 days past expires_at
}
