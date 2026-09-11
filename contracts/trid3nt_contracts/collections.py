"""Document-store collection schemas - the metadata side of a metadata/payload
split, where the object store holds payloads keyed by URIs recorded here.

pydantic forbids a field literally named ``_id``, so each document model exposes
``id`` aliased to it and dumps with ``by_alias=True``; ``MONGO_DUMP_KWARGS`` is
that canonical dump. No cost field lives on any of these.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from .catalog import CatalogEntry
from .common import GraceModel, ULIDStr, UTCDatetime
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
    "ProjectDocument",
    "UserSpatialInput",
    "RunDocument",
    "ArticleDocument",
    "ChatMessage",
    "ToolCallSummary",
    "PipelineStepSummary",
    "PipelineSnapshot",
    "MapView",
    "SessionDocument",
    "CatalogEntryDocument",
    "CatalogAuditEventType",
    "CatalogAuditLogDocument",
    "MONGO_DUMP_KWARGS",
    "EMBEDDING_MODEL_DEFAULT",
    "EMBEDDING_DIMENSIONS_DEFAULT",
    "RUNS_VECTOR_INDEX",
    "ARTICLES_VECTOR_INDEX",
    "VECTOR_INDEXES",
    "SESSIONS_TTL",
    "CATALOG_ENTRIES_INDEXES",
    "CATALOG_AUDIT_LOG_INDEXES",
]


#: Canonical kwargs for producing the BSON/wire form of any document model.
MONGO_DUMP_KWARGS: dict[str, Any] = {"mode": "json", "by_alias": True}

#: Embedding model and default dimension, shared across collections. A smaller
#: dimension trades recall for index size; the index configs below are DOCUMENTED
#: CONSTANTS, not a locked provisioning config.
EMBEDDING_MODEL_DEFAULT = "text-embedding-005"
EMBEDDING_DIMENSIONS_DEFAULT = 768


class DocModel(GraceModel):
    """Base for collection documents that alias ``_id``.
    ``populate_by_name`` lets the id be set as either ``id`` or ``_id``, and
    dumped back to ``_id`` under ``by_alias``."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )




class ProjectLayerSummary(GraceModel):
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
    temporal: bool  # carries a temporal config

    # Layer-stack arbitration. A client absent both falls back to fully opaque
    # and its own default order.
    opacity: float | None = None     # 0.0-1.0
    z_index: int | None = None       # lower draws first

    # The CRS for a ``layer_type="mesh"`` row: a mesh reader reports an empty
    # CRS for these formats, so the run has to state it. ``None`` otherwise.
    crs_authid: str | None = None

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


class ProjectDocument(DocModel):
    """``projects``: the metadata index over published project files."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")  # the project id used everywhere
    session_id: ULIDStr  # owning session
    qgs_uri: str  # the canonical published project file
    name: str  # human-readable
    description: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # EPSG:4326
    hazard_types: list[str] = Field(default_factory=list)
    layers: list[ProjectLayerSummary] = Field(default_factory=list)
    created_at: UTCDatetime
    updated_at: UTCDatetime
    deleted_at: UTCDatetime | None = None  # soft delete




class UserSpatialInput(GraceModel):
    """A user-provided spatial input recorded on a run."""

    request_id: ULIDStr  # the request that solicited this input
    geometry_type: Literal["point", "bbox"]
    coordinates: list[float]  # [lon, lat] for point; bbox 4-tuple for bbox
    name: str | None = None  # what the user called a picked point
    prompt_title: str
    submitted_at: UTCDatetime


class RunDocument(DocModel):
    """``runs``: every solver execution and every discovery operation.
    The envelope is embedded as a DICT, validated at the API boundary, so an
    envelope change never forces a collection migration."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")  # the solver run id
    project_id: ULIDStr
    session_id: ULIDStr

    status: Literal["pending", "running", "complete", "failed", "cancelled"]
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    duration_seconds: float | None = None

    # Denormalized off the envelope, so a query needs no join.
    run_type: Literal["modeled", "discovered"]
    hazard_type: str
    workflow_name: str

    bbox: tuple[float, float, float, float]
    event_time_start: UTCDatetime | None = None
    event_time_end: UTCDatetime | None = None

    # The full envelope as a dict. ``None`` until the run completes.
    assessment: dict | None = None

    embedding: list[float] | None = None
    embedding_model: str | None = None

    error_code: str | None = None
    error_message: str | None = None

    cancellation_reason: str | None = None
    cancelled_at: UTCDatetime | None = None

    user_spatial_inputs: list[UserSpatialInput] = Field(default_factory=list)

    # Populated only for a news-derived run.
    event_id: ULIDStr | None = None
    article_ids: list[ULIDStr] = Field(default_factory=list)




class ArticleDocument(DocModel):
    """``articles``: the fetched news-article corpus."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")
    url: str
    url_hash: str  # SHA-256 of normalized URL for dedup
    title: str
    publisher: str | None = None
    author: str | None = None

    text: str  # the cleaned extracted text
    text_length: int = Field(ge=0)
    html_uri: str | None = None  # set when the full HTML was retained

    published_at: UTCDatetime | None = None
    fetched_at: UTCDatetime

    embedding: list[float] | None = None
    embedding_model: str | None = None

    extraction_status: Literal["pending", "extracted", "failed", "no_events"]
    extracted_event_ids: list[ULIDStr] = Field(default_factory=list)
    last_processed_at: UTCDatetime | None = None




class ToolCallSummary(GraceModel):
    """A completed/failed/cancelled tool call recorded in chat history."""

    call_id: ULIDStr
    tool_name: str
    state: Literal["complete", "failed", "cancelled"]
    result_summary: str | None = None
    result_uri: str | None = None
    error_code: str | None = None
    started_at: UTCDatetime
    completed_at: UTCDatetime | None = None


class ChatMessage(GraceModel):
    """One chat turn. ``message_id`` matches the wire id for an agent turn."""

    message_id: ULIDStr
    role: Literal["user", "agent"]
    content: str  # for an agent turn, the text accumulated after streaming
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    created_at: UTCDatetime


class PipelineStepSummary(GraceModel):
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


class PipelineSnapshot(GraceModel):
    """A persisted pipeline run."""

    pipeline_id: ULIDStr
    started_at: UTCDatetime
    completed_at: UTCDatetime | None = None
    final_state: Literal["complete", "failed", "cancelled"] | None = None
    steps: list[PipelineStepSummary] = Field(default_factory=list)


class MapView(GraceModel):
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


# Vector-search index configs - DOCUMENTED CONSTANTS, not a locked config
# The dimension is the shared default; provisioning may land elsewhere after a
# recall-versus-cost check.


def _vector_index(name: str, *filter_paths: str) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": EMBEDDING_DIMENSIONS_DEFAULT,
            "similarity": "cosine",
        }
    ]
    for path in filter_paths:
        fields.append({"type": "filter", "path": path})
    return {"name": name, "type": "vectorSearch", "fields": fields}


RUNS_VECTOR_INDEX = _vector_index("runs_embedding_vsi", "hazard_type", "run_type")
ARTICLES_VECTOR_INDEX = _vector_index("articles_embedding_vsi", "extraction_status")

#: The vector-search indexes - the minimum useful set.
VECTOR_INDEXES: dict[str, dict[str, Any]] = {
    "runs": RUNS_VECTOR_INDEX,
    "articles": ARTICLES_VECTOR_INDEX,
}


#: TTL index spec for sessions: a document is deleted 30 days after
#: ``expires_at``. This is the CONTRACT; provisioning creates the index.
SESSIONS_TTL: dict[str, Any] = {
    "collection": "sessions",
    "field": "expires_at",
    "expire_after_seconds": 30 * 24 * 60 * 60,  # 30 days past expires_at
}

#: TTL window for an ANONYMOUS Case. An authenticated Case is durable forever
#: and carries no expiry at all; only an anonymous one OPTS IN by being written
#: ephemeral. The stamped value is NUMERIC epoch seconds, not the ISO string the
#: sessions TTL uses, because the store's native TTL requires a number.
CASES_ANON_TTL_SECONDS: int = int(
    os.environ.get("CASES_ANON_TTL_SECONDS", 7 * 24 * 60 * 60)  # 7 days
)


# Neither collection is TTL-eligible: an entry is durable until a curator
# deprecates it, and the status lifecycle does the soft-delete work; the audit
# log is append-only retention, because proposal and review provenance has to
# survive indefinitely.


class CatalogEntryDocument(CatalogEntry):
    """``catalog_entries``: one curated catalog entry, unwrapped.
    The collection schema IS the entry schema: no wrapper fields, and no ``_id``
    alias, because one shape across wire, file and store is worth more."""


#: Audit-log event vocabulary.
#:
#: - ``add`` - a curator added an entry directly.
#: - ``update`` - a curator edited an entry's metadata.
#: - ``deprecate`` - a curator flipped an entry to deprecated.
#: - ``user_proposed`` - a user accepted an offered addition; the entry is
#:   written pending curator review.
#: - ``curator_approved`` - a curator activated a user-proposed entry.
#: - ``curator_rejected`` - a curator removed one that did not pass review.
CatalogAuditEventType = Literal[
    "add",
    "update",
    "deprecate",
    "user_proposed",
    "curator_approved",
    "curator_rejected",
]


class CatalogAuditLogDocument(DocModel):
    """``catalog_audit_log``: the APPEND-ONLY audit trail for the catalog.
    One document per mutation, durable and never TTL-cleaned: a reference on a
    run must stay resolvable back to its proposal and review context."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")
    entry_id: str = Field(min_length=1)  # the ``CatalogEntry.id`` this applies to
    #: Populated when the event originated inside an active session.
    session_id: ULIDStr | None = None
    #: An opaque user identifier, when identity is available.
    user_id: str | None = None
    event_type: CatalogAuditEventType
    #: Open dict; its shape varies by ``event_type`` - probe findings and a
    #: request id for a proposal, a curator note for a review, a diff for an
    #: update.
    event_payload: dict = Field(default_factory=dict)
    timestamp: UTCDatetime


# catalog_entries indexes - declared here, provisioned elsewhere

CATALOG_ENTRIES_INDEXES: list[dict[str, Any]] = [
    # source_class: search by domain.
    {"key": [("source_class", 1)], "name": "catalog_entries_source_class_1"},
    # (status, source_class): the common active-only-by-source query.
    {
        "key": [("status", 1), ("source_class", 1)],
        "name": "catalog_entries_status_1_source_class_1",
    },
]


# catalog_audit_log indexes - declared here, provisioned elsewhere

CATALOG_AUDIT_LOG_INDEXES: list[dict[str, Any]] = [
    # entry_id + timestamp descending: the trail-for-one-entry query.
    {
        "key": [("entry_id", 1), ("timestamp", -1)],
        "name": "catalog_audit_log_entry_id_1_timestamp_-1",
    },
]
