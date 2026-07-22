"""MongoDB collection schemas (SRS Appendix D, FR-MP-5, Decision F/L).

Five collections, each a pydantic model mapping to a BSON document. The wire/
storage form is ``model.model_dump(mode="json", by_alias=True)`` with the
document id serialized as ``_id``.

pydantic forbids a field literally named ``_id`` (leading underscore), so each
document model exposes ``id`` with ``alias="_id"`` and ``populate_by_name=True``:
construct with ``id=...`` (or ``_id=...``), dump with ``by_alias=True`` to get
``{"_id": ...}`` for Mongo. ``MONGO_DUMP_KWARGS`` captures the canonical dump
options.

Invariants this module is responsible for:
- **6. Metadata-payload pattern.** These schemas are the MongoDB side of the
  metadata-payload split; GCS holds payloads keyed by URIs stored here.
- **8. Cancellation is first-class.** ``RunDocument.status`` carries
  ``cancelled`` as a distinct terminal state.
- **9. No cost theater.** No cost field on ``runs`` or anywhere (D.7).

OQ-7 (embedding dimension) is surfaced in the report. The vector index configs
below use the SRS-stated default of 768 dims; ``infra`` provisions the indexes
to whatever the user lands after the recall-vs-cost check. They are documented
constants here, NOT a locked Atlas config.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from .catalog import CatalogEntry
from .common import GraceModel, ULIDStr, UTCDatetime
from .event import EventMetadata
from .execution import LegendKey

#: SCREAMING_SNAKE_CASE error-code pattern (Appendix A.6).
#: Open set per A.6: codes are validated by shape, not against a closed registry,
#: so every workflow/tool may register new codes without a schema change.
_ERROR_CODE_RE: re.Pattern[str] = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")

#: Cap on ``error_message`` length to discourage stack-trace leakage (D.6).
_ERROR_MESSAGE_MAX_LEN: int = 512

__all__ = [
    "DocModel",
    "ProjectLayerSummary",
    "ProjectDocument",
    "UserSpatialInput",
    "RunDocument",
    "ArticleDocument",
    "EventDocument",
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
    "EVENTS_VECTOR_INDEX",
    "VECTOR_INDEXES",
    "SESSIONS_TTL",
    "CATALOG_ENTRIES_INDEXES",
    "CATALOG_AUDIT_LOG_INDEXES",
]


#: Canonical kwargs for producing the BSON/wire form of any document model.
MONGO_DUMP_KWARGS: dict[str, Any] = {"mode": "json", "by_alias": True}

#: Embedding model + default dimension shared across collections (D.7).
#: OQ-7: 768 is the SRS default; 256/128 trade recall for index size/cost. The
#: index configs below use this default and are NOT a locked Atlas config.
EMBEDDING_MODEL_DEFAULT = "text-embedding-005"
EMBEDDING_DIMENSIONS_DEFAULT = 768


class DocModel(GraceModel):
    """Base for collection documents that use ``_id`` aliasing.

    Adds ``populate_by_name=True`` on top of ``GraceModel`` so the id field can
    be set by either ``id`` or ``_id`` and dumped to ``_id`` with ``by_alias``.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )


# --------------------------------------------------------------------------- #
# D.2 projects
# --------------------------------------------------------------------------- #


class ProjectLayerSummary(GraceModel):
    """Denormalized layer entry on a project (and on session map state).

    The ``wms_url`` field carries the QGIS Server WMS endpoint the client uses
    for MapLibre source registration; ``uri`` remains the underlying GCS file
    pointer (gs://...). ``style_preset`` drives client-side legend matching.
    ``opacity`` (0.0–1.0) and ``z_index`` enable layer-stack arbitration;
    clients fall back to ``1.0`` / a default order if both are absent.

    ``legend`` is the DATA-DRIVEN render key mirrored from ``LayerURI.legend``
    (see ``execution.LegendKey``): the colormap is the semantic per-variable
    choice, the range is the REAL data range. Additive + optional -- ``None``
    means legacy ``style_preset`` rendering, so layers without a legend render
    exactly as before. The pipeline emitter copies it onto this summary.

    Closes OQ-62-LAYERURI-URI-FIELD, OQ-W-65-STYLE-PRESET, OQ-0068-ZIDX.
    """

    layer_id: str
    name: str
    layer_type: Literal["raster", "vector"]
    uri: str
    style_preset: str
    visible: bool
    role: Literal["primary", "context", "input"]
    temporal: bool  # has WMS-T config

    # --- Fields added by job-0072 (D.2 amendment) --- #
    wms_url: str | None = None       # QGIS Server WMS URL for MapLibre tile registration
    opacity: float | None = None     # 0.0–1.0; client falls back to 1.0 if absent
    z_index: int | None = None       # MapLibre layer-order arbitration; lower draws first

    # --- Data-driven render key (additive; None => legacy style_preset path) --- #
    legend: LegendKey | None = None  # mirrored from LayerURI.legend; see execution.LegendKey


class ProjectDocument(DocModel):
    """``projects`` (D.2): metadata index over .qgs files in GCS."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")  # the project_id used everywhere
    session_id: ULIDStr  # owning session
    qgs_uri: str  # gs://.../project_<id>.qgs (canonical)
    name: str  # human-readable
    description: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # EPSG:4326
    hazard_types: list[str] = Field(default_factory=list)
    layers: list[ProjectLayerSummary] = Field(default_factory=list)
    created_at: UTCDatetime
    updated_at: UTCDatetime
    deleted_at: UTCDatetime | None = None  # soft delete


# --------------------------------------------------------------------------- #
# D.3 runs
# --------------------------------------------------------------------------- #


class UserSpatialInput(GraceModel):
    """A user-provided spatial input recorded on a run (FR-AS-10)."""

    request_id: ULIDStr  # the WebSocket request that solicited this input
    geometry_type: Literal["point", "bbox"]
    coordinates: list[float]  # [lon, lat] for point; bbox 4-tuple for bbox
    prompt_title: str
    submitted_at: UTCDatetime


class RunDocument(DocModel):
    """``runs`` (D.3): every solver execution or discovery operation.

    Embeds the full ``AssessmentEnvelope`` as ``assessment: dict`` (None until
    complete) — a dict, not a nested model, so envelope schema changes don't
    force a collection migration (D.7). Validation happens at the API boundary
    in the agent before write. ``status`` carries ``cancelled`` as a distinct
    terminal state (invariant 8). No cost field (invariant 9 / D.7).
    """

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")  # this is the solver_run_id
    project_id: ULIDStr
    session_id: ULIDStr

    status: Literal["pending", "running", "complete", "failed", "cancelled"]
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    duration_seconds: float | None = None

    run_type: Literal["modeled", "discovered"]  # mirrors envelope_type
    hazard_type: str  # denormalized from envelope
    workflow_name: str  # denormalized from envelope

    bbox: tuple[float, float, float, float]  # denormalized for queries
    event_time_start: UTCDatetime | None = None
    event_time_end: UTCDatetime | None = None

    # Full AssessmentEnvelope as dict; None until status == "complete".
    assessment: dict | None = None

    embedding: list[float] | None = None
    embedding_model: str | None = None

    error_code: str | None = None
    error_message: str | None = None

    cancellation_reason: str | None = None
    cancelled_at: UTCDatetime | None = None

    user_spatial_inputs: list[UserSpatialInput] = Field(default_factory=list)

    event_id: ULIDStr | None = None  # if news-derived
    article_ids: list[ULIDStr] = Field(default_factory=list)  # if news-derived


# --------------------------------------------------------------------------- #
# D.4 articles
# --------------------------------------------------------------------------- #


class ArticleDocument(DocModel):
    """``articles`` (D.4): fetched news article corpus."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")
    url: str
    url_hash: str  # SHA-256 of normalized URL for dedup
    title: str
    publisher: str | None = None
    author: str | None = None

    text: str  # extracted article text (cleaned)
    text_length: int = Field(ge=0)
    html_uri: str | None = None  # GCS URI if full HTML retained

    published_at: UTCDatetime | None = None
    fetched_at: UTCDatetime

    embedding: list[float] | None = None
    embedding_model: str | None = None

    extraction_status: Literal["pending", "extracted", "failed", "no_events"]
    extracted_event_ids: list[ULIDStr] = Field(default_factory=list)
    last_processed_at: UTCDatetime | None = None


# --------------------------------------------------------------------------- #
# D.5 events  (the collection schema *is* EventMetadata)
# --------------------------------------------------------------------------- #


class EventDocument(EventMetadata):
    """``events`` (D.5): an EventMetadata document. ``event_id`` is the ``_id``.

    The collection schema *is* the ``EventMetadata`` schema (Appendix C); no
    wrapper fields are added. The Mongo ``_id`` is ``event_id`` (a ULID); the
    write path sets ``_id = event_id`` at insert time. We do not alias here to
    keep ``EventMetadata`` a single shape across wire and storage.
    """


# --------------------------------------------------------------------------- #
# D.6 sessions
# --------------------------------------------------------------------------- #


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
    """One chat turn. ``message_id`` matches the WS message id for agent msgs."""

    message_id: ULIDStr
    role: Literal["user", "agent"]
    content: str  # for agent messages, final accumulated text after streaming
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    created_at: UTCDatetime


class PipelineStepSummary(GraceModel):
    """A step in a persisted pipeline snapshot. ``cancelled`` is distinct.

    Optional progress + error fields (job-0030, sprint-06 M4 pre-flight,
    resolving job-0026 OQ-W-26-PIPELINE-STEP-FIELDS):

    - ``progress_percent`` is an integer 0..100, populated by the workflow
      when it can reasonably attribute progress (e.g. solver chunk N of M,
      n-of-M rows processed). Optional everywhere — never an LLM estimate
      (Invariant 1: determinism boundary).
    - ``error_code`` is a ``SCREAMING_SNAKE_CASE`` literal aligned with the
      Appendix A.6 error-code convention; populated only when ``state ==
      "failed"``. The set of valid codes is **open** per A.6 (every workflow
      may register its own); validation is shape-only (regex).
    - ``error_message`` is a short human-readable accompanier, capped at
      512 chars to discourage stack-trace leakage. Free text.
    - ``duration_ms`` (job-0264, ELEVATED tool-timer requirement) is the
      authoritative wall-clock elapsed time, derived deterministically from
      ``completed_at - started_at`` and stamped on the **terminal** transition
      (complete / failed / cancelled) by the ``PipelineEmitter``. Never an LLM
      estimate (Invariant 1). Optional / ``None`` for pending/running. ``ge=0``.

    Tightening these to required on ``state == "running"`` / ``state ==
    "failed"`` is a deliberate follow-up — see report Open Questions.
    No cost field anywhere (Invariant 9).
    """

    step_id: ULIDStr
    name: str
    tool_name: str
    state: Literal["pending", "running", "complete", "failed", "cancelled"]
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX_LEN)
    duration_ms: int | None = Field(default=None, ge=0)
    # Two-card sim observability (task-149): mirror the ws.PipelineStep card-kind
    # discriminator + Batch binding so a persisted/replayed snapshot and a
    # cold-case rehydration carry the off-box solver card across a reconnect.
    # ``role`` defaults to ``"tool"`` and the ids to ``None`` so every existing
    # persisted step is byte-identical (back-compat); ``"compute"`` is the
    # Batch-bound solver card. ``batch_status`` mirrors DescribeJobs verbatim
    # (Invariant 1, never an LLM estimate).
    role: Literal["tool", "compute"] = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    # Nested sub-step timeline (task-168): mirror the ws.PipelineStep parent/child
    # fields so a persisted/replayed snapshot and a cold-case rehydration carry
    # the nested timeline across a reconnect. ``parent_step_id`` marks a CHILD;
    # ``substep_label`` / ``substep_index`` / ``substep_total`` are the PARENT's
    # live-breadcrumb fields (cleared on the parent's terminal transition). All
    # default None so every existing persisted step is byte-identical (back-compat).
    parent_step_id: ULIDStr | None = None
    substep_label: str | None = None
    substep_index: int | None = Field(default=None, ge=1)
    substep_total: int | None = Field(default=None, ge=1)

    @field_validator("error_code")
    @classmethod
    def _validate_error_code_shape(cls, value: str | None) -> str | None:
        """Enforce SCREAMING_SNAKE_CASE shape per Appendix A.6 convention."""
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
    """``sessions`` (D.6): chat session state. TTL-cleaned via ``expires_at``."""

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")  # this is the session_id
    client_fingerprint: str | None = None  # cookie-derived opaque identifier

    created_at: UTCDatetime
    last_active_at: UTCDatetime
    expires_at: UTCDatetime  # TTL cleanup driver; updated on each interaction

    chat_history: list[ChatMessage] = Field(default_factory=list)
    project_ids: list[ULIDStr] = Field(default_factory=list)
    pipeline_history: list[PipelineSnapshot] = Field(default_factory=list)
    current_pipeline: PipelineSnapshot | None = None

    loaded_layers: list[ProjectLayerSummary] = Field(default_factory=list)
    map_view: MapView | None = None


# --------------------------------------------------------------------------- #
# Atlas Vector Search index configs (documented constants — NOT locked) (D.3-5)
# --------------------------------------------------------------------------- #
# OQ-7: numDimensions uses the SRS default (768). infra provisions to whatever
# the user lands after the recall-vs-cost check on a small corpus.


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
EVENTS_VECTOR_INDEX = _vector_index("events_embedding_vsi", "event_type", "time_classification")

#: The three Atlas Vector Search indexes (the minimum useful set, D.8).
VECTOR_INDEXES: dict[str, dict[str, Any]] = {
    "runs": RUNS_VECTOR_INDEX,
    "articles": ARTICLES_VECTOR_INDEX,
    "events": EVENTS_VECTOR_INDEX,
}


# --------------------------------------------------------------------------- #
# sessions TTL config (D.6)
# --------------------------------------------------------------------------- #
#: Mongo TTL index spec for sessions: delete documents 30 days after
#: ``expires_at``. ``infra`` creates the actual index; this is the contract.
SESSIONS_TTL: dict[str, Any] = {
    "collection": "sessions",
    "field": "expires_at",
    "expire_after_seconds": 30 * 24 * 60 * 60,  # 30 days past expires_at
}

#: TTL window for ANONYMOUS (pre-Auth) Cases (#147 ephemeral-cases track).
#:
#: Authed Cases are durable forever and carry NO ``expires_at`` — only an
#: anonymous Case opts in to expiry by being written ``ephemeral=True``
#: (``persistence.upsert_case`` / ``touch_case``). DynamoDB-native TTL needs a
#: NUMERIC epoch-seconds attribute (unlike the ISO ``expires_at`` strings the
#: sessions collection uses for the Mongo TTL index), so the value stamped on
#: the case doc is ``int(now + CASES_ANON_TTL_SECONDS)``.
#:
#: Env-overridable via ``CASES_ANON_TTL_SECONDS`` (mirrors the env-config
#: pattern used elsewhere); defaults to 7 days.
CASES_ANON_TTL_SECONDS: int = int(
    os.environ.get("CASES_ANON_TTL_SECONDS", 7 * 24 * 60 * 60)  # 7 days
)


# --------------------------------------------------------------------------- #
# Mode 1 catalog substrate (sprint-08): catalog_entries + catalog_audit_log
# --------------------------------------------------------------------------- #
# Forward-looking — Decision F + §F.1.2 Mode 1 binding for sprint-08.
#
# Numbering note: SRS Appendix D already uses D.7..D.10 for cross-cutting /
# storage-sizing / design-rationale / known-open-choices meta sections. The
# new collections therefore land at **D.11 catalog_entries** and **D.12
# catalog_audit_log** rather than D.8/D.9 (the kickoff's numbering assumed
# D.1-D.6 were the only existing sections). Surfaced in report Open Questions
# so the user can override during audit if D.8/D.9 is preferred (would require
# renumbering the existing meta sections).
#
# Neither collection is TTL-eligible:
# - ``catalog_entries`` are durable until a curator deprecates / removes them
#   (status lifecycle does the soft-delete work).
# - ``catalog_audit_log`` is append-only retention; Mode 2 user-proposed +
#   curator-review provenance must survive indefinitely per Decision M.


class CatalogEntryDocument(CatalogEntry):
    """``catalog_entries`` (D.11): one curated Mode 1 catalog entry.

    The collection schema *is* the ``CatalogEntry`` schema (Appendix F /
    FR-PHC-2 + §F.1.2 Mode 1); no wrapper fields are added. The Mongo ``_id``
    is the entry ``id`` (a stable string identifier curated at entry-creation
    time, e.g. ``"usgs-3dep-dem-1m"``, ``"worldpop-1km-aggregated"``); the
    write path sets ``_id = id`` at insert time.

    We do NOT alias here — keeping ``CatalogEntry`` a single shape across wire,
    YAML, and Mongo is more useful than the ``_id`` alias would be, and the
    ``id`` field is already a free-form stable string (not a ULID), so the
    Mongo-side aliasing of the ULID-based ``DocModel`` doesn't apply.

    Indexes (declared in ``CATALOG_ENTRIES_INDEXES`` below): one on
    ``source_class`` for ``catalog_search`` by domain, and a compound on
    ``(status, source_class)`` for the common "active-only by source"
    query path (``status: "active"`` filter + ``source_class`` selector).
    """


#: Audit-log event vocabulary per §F.1.2 Mode 1 + Mode 2.
#:
#: - ``add`` — curator added a new entry directly (Mode 1 path).
#: - ``update`` — curator edited an existing entry's metadata.
#: - ``deprecate`` — curator flipped ``status`` to ``"deprecated"``.
#: - ``user_proposed`` — Mode 2 user accepted an ``offer-catalog-addition``;
#:   entry written with ``status: "user_proposed_pending_curator_review"``.
#: - ``curator_approved`` — curator flipped a user-proposed entry to
#:   ``status: "active"``.
#: - ``curator_rejected`` — curator removed a user-proposed entry that did
#:   not pass review.
CatalogAuditEventType = Literal[
    "add",
    "update",
    "deprecate",
    "user_proposed",
    "curator_approved",
    "curator_rejected",
]


class CatalogAuditLogDocument(DocModel):
    """``catalog_audit_log`` (D.12): append-only audit trail for the catalog.

    Every catalog mutation lands one document here. Mode 2 user-proposed entries
    produce a ``user_proposed`` event at acceptance; curator-side approval /
    rejection produce a ``curator_approved`` / ``curator_rejected`` event
    against the same ``entry_id``. Decision M (claim provenance) requires this
    trail to be inspectable: the catalog query path may surface user-proposed
    entries as provisional, and downstream run-document `CatalogReference`
    fields can be resolved back through this collection to recover the
    proposal + review context.

    Fields:

    - ``id`` — ULID, the audit-event id (this is the document ``_id``).
    - ``entry_id`` — the ``CatalogEntry.id`` this event applies to. Indexed
      for the ``(entry_id, timestamp DESC)`` query path.
    - ``session_id`` — optional ULID; populated when the event originated
      inside an active session (Mode 2 user-proposed flow).
    - ``user_id`` — optional opaque user identifier; populated when user
      identity is available (post-M6+ user accounts). v0.1 leaves this None
      since identity machinery is not yet wired; the field is here so the
      audit trail is forward-compatible.
    - ``event_type`` — ``CatalogAuditEventType`` literal.
    - ``event_payload`` — open dict (shape varies by ``event_type``); for
      ``user_proposed`` it carries the conformity-probe findings + the
      ``offer-catalog-addition`` request id; for ``curator_approved`` /
      ``curator_rejected`` it carries the curator note; for ``update`` it
      carries the diff.
    - ``timestamp`` — UTC datetime when the event was recorded.

    No TTL — the audit trail is durable. No cost field anywhere (Invariant 9).
    """

    schema_version: Literal["v1"] = "v1"

    id: ULIDStr = Field(alias="_id")
    entry_id: str = Field(min_length=1)  # references CatalogEntry.id
    session_id: ULIDStr | None = None
    user_id: str | None = None
    event_type: CatalogAuditEventType
    event_payload: dict = Field(default_factory=dict)
    timestamp: UTCDatetime


# --------------------------------------------------------------------------- #
# D.11 catalog_entries indexes (declared; infra provisions)
# --------------------------------------------------------------------------- #

CATALOG_ENTRIES_INDEXES: list[dict[str, Any]] = [
    # source_class: catalog_search by domain (e.g. "dem", "landcover", "flood_zone").
    {"key": [("source_class", 1)], "name": "catalog_entries_source_class_1"},
    # (status, source_class): the common "active-only by source" query.
    {
        "key": [("status", 1), ("source_class", 1)],
        "name": "catalog_entries_status_1_source_class_1",
    },
]


# --------------------------------------------------------------------------- #
# D.12 catalog_audit_log indexes (declared; infra provisions)
# --------------------------------------------------------------------------- #

CATALOG_AUDIT_LOG_INDEXES: list[dict[str, Any]] = [
    # entry_id + timestamp DESC: the audit-trail-for-an-entry query path.
    {
        "key": [("entry_id", 1), ("timestamp", -1)],
        "name": "catalog_audit_log_entry_id_1_timestamp_-1",
    },
]
