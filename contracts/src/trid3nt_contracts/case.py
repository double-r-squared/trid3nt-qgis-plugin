"""Case persistence envelopes (FR-MP-6, Appendix A.6/A.7 amendments, sprint-12).

A "Case" is the user-facing name for a `projects` document (FR-MP-5 nomenclature
stays canonical in storage). This module owns the **wire-shape envelopes** that
back the FR-MP-6 Case UX flow:

- ``CaseSummary`` — the left-rail entity (denormalized from ``ProjectDocument``).
- ``CaseChatMessage`` — a single persisted chat exchange in a Case session
  (extends ``ChatMessage`` semantics with per-turn layer/map-command emissions
  so the rehydration replay can re-bind a Case session deterministically).
- ``CaseSessionState`` — the rehydration envelope returned when a user opens
  a Case (the "replay envelope").
- ``CaseListEnvelope`` / ``CaseOpenEnvelope`` — server -> client A.4 messages
  for the left-rail listing and Case open/rehydrate transitions.
- ``CaseCommandEnvelope`` — client -> server A.3 message for Case lifecycle
  commands (``create`` / ``select`` / ``rename`` / ``archive`` / ``delete``).

This module is **Wave 1 of sprint-12-mega**: every downstream Wave 2 Case UX job
(agent and web specialists) consumes these shapes. The shapes are pydantic v2
``GraceModel`` subclasses (the project-wide convention; see ``common.py`` and
``schema.md`` "pydantic v2, not tentative anymore"). The kickoff sketched the
shapes as ``dataclass``; the conservative-interpretation translation to
``GraceModel`` is logged as ``OQ-0099-DATACLASS-VS-PYDANTIC`` in the report.

Invariants this module is responsible for:

- **8. Cancellation is first-class.** ``case-command`` carries no ad-hoc
  cancellation field; cancellation flows through the existing ``cancel``
  message (Appendix A.3), not a Case lifecycle command.
- **9. No cost theater.** No cost field anywhere on Case envelopes — neither
  on ``CaseSummary`` (no aggregate cost), nor on ``CaseCommandEnvelope``, nor
  on the rehydration replay. Cost surfacing is forbidden everywhere (A.4 /
  invariant 9).

SRS references:
- FR-MP-6 (Case UX flow) — `docs/srs/03-functional-requirements.md`.
- Appendix A.3 (client -> server messages) and A.4 (server -> client messages)
  for the envelope-type discipline.
- Appendix D.2 (``projects``) and D.6 (``sessions``) for the underlying storage
  shapes the Case envelopes denormalize from.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .common import (
    BBox,
    GraceModel,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    # Case persistence envelopes (FR-MP-6)
    "CaseStatus",
    "CaseSummary",
    "CaseChatMessage",
    "CaseSessionState",
    "ToolCardRecord",
    "ToolCardState",
    "PersistedSubStepRecord",
    # Data-island thin manifest (#165 — cold-serve cases+layers from S3)
    "CaseManifestLayer",
    "CaseManifest",
    # WebSocket envelopes (A.4 / A.3 amendments)
    "CaseListEnvelopePayload",
    "CaseOpenEnvelopePayload",
    "CaseCommand",
    "CaseCommandEnvelopePayload",
]


# --------------------------------------------------------------------------- #
# Case persistence envelopes (FR-MP-6)
# --------------------------------------------------------------------------- #

# Closed enum: Case lifecycle status. ``deleted`` is a soft-delete tombstone
# that mirrors ``ProjectDocument.deleted_at`` (D.2). The list is intentionally
# closed at v0.1 — a new status is an SRS amendment, not a silent open-enum.
CaseStatus = Literal["active", "archived", "deleted"]


class CaseSummary(GraceModel):
    """Top-level Case record — the left-rail entity (FR-MP-6 landing state).

    Denormalized from ``ProjectDocument`` (D.2) so the client can render the
    Cases list without joining sessions/runs. The Case identifier maps 1:1 to
    ``projects._id`` (FR-MP-6: UI labels say "Case", schema/code say
    "Project"); ``case_id`` here IS the ``project_id``.

    ``qgs_project_uri`` is lazy-init by design — a fresh Case has no published
    ``.qgs`` yet; ``publish_layer`` writes the URI on first layer emission
    (see ``ProjectDocument.qgs_uri``, FR-MP-3).

    Invariant 9: no cost field anywhere. The summary carries no aggregate
    cost / spent / quota fields.
    """

    schema_version: Literal["v1"] = "v1"

    case_id: ULIDStr  # ULID; maps 1:1 to projects._id (FR-MP-5 / FR-MP-6)
    title: str  # user-edited; ``ProjectDocument.name`` is the storage field
    created_at: UTCDatetime  # ISO-8601 UTC
    updated_at: UTCDatetime  # ISO-8601 UTC
    status: CaseStatus = "active"

    bbox: BBox | None = None  # [minLon, minLat, maxLon, maxLat] EPSG:4326
    # Primary hazard label is denormalized from the Case's runs; open enum so
    # registering a new hazard does not break the Case envelope (Decision G).
    primary_hazard: str | None = None

    # Layer summary is a flat list of layer_ids the Case currently has loaded.
    # Full layer detail lives in CaseSessionState.loaded_layers on Case open;
    # the left-rail summary stays cheap.
    layer_summary: list[str] = Field(default_factory=list)

    # job-0172 Part B: per-Case persisted ``ProjectLayerSummary`` dicts. The
    # PipelineEmitter holds these per-connection in memory; we mirror them
    # onto the Case document so a Case re-open (fresh connection, fresh
    # emitter) rehydrates ``loaded_layers`` deterministically rather than
    # showing an empty LayerPanel. Entries are full ``ProjectLayerSummary``
    # ``model_dump(mode="json")`` shapes (matches Appendix D.2 envelope
    # discipline + ``CaseSessionState.loaded_layers``). Dedup is by ``uri``:
    # republishing the same layer overwrites the existing entry in place.
    loaded_layer_summaries: list[dict] = Field(default_factory=list)

    qgs_project_uri: str | None = None  # gs://.../{case_id}.qgs (lazy-init)


# Persisted tool-card lifecycle states (durable, replayable).
#
# job-0267 originally pinned this to the two TERMINAL outcomes (``complete`` /
# ``failed``): an on-box atomic tool persists exactly once, at terminal, and a
# cancelled dispatch left NO row (Invariant 8). A long-running off-box SOLVE is
# different — its SIM/dispatch card spans a WS reconnect window, so the "nothing
# about the chat is transient" durability principle (NATE) requires the card to
# be persisted the moment it is minted (``running``) and UPDATED IN PLACE to its
# terminal state: a SINGLE row whose ``state`` walks running -> terminal, keyed
# by a stable ``message_id`` (upsert, never a duplicate). ``cancelled`` is now
# ALSO persisted for a solve card — a stopped sim is a FINISHED sim the user must
# be able to trace after the fact (this supersedes Invariant 8's "no row" for the
# durable solve card specifically; the atomic-tool path still leaves no row on
# cancel because it persists only at terminal). Children
# (``PersistedSubStepRecord``) only ever carry the two terminal values at
# runtime; the wider type is a harmless superset for them.
ToolCardState = Literal["running", "complete", "failed", "cancelled"]


class PersistedSubStepRecord(GraceModel):
    """Replayable record of ONE nested CHILD step under a tool-card (task-168).

    The live nested sub-step cards (commit 256a587) surface a composer's INTERNAL
    atomic-tool calls (``fetch_*`` / deck build / ``run_solver`` / ``postprocess_*``
    / ``publish_layer`` / ``compute_*``) as CHILD rows nested under the top-level
    workflow card, driven by wire-only ``pipeline-state`` envelopes
    (``PipelineStep.parent_step_id``). Those envelopes were LOST on Case reopen
    and on the box-off cold view -- the nested timeline went blank. This record
    is the persisted twin of one child step so the rehydration replay (warm via
    the agent AND cold via the serverless case-view snapshot) rebuilds the SAME
    nested sub-step timeline the live feature renders, READ-ONLY (no re-dispatch).

    Field names reuse ``PipelineStepSummary`` (collections.py) / ``ToolCardRecord``
    VERBATIM (do NOT invent new names) so the web replay path can synthesize a
    ``PipelineStepSummary`` directly off this record and mirror
    ``web/src/contracts.ts`` ``PersistedSubStepRecord`` 1:1:

    - ``step_id`` / ``parent_step_id`` -- the persisted ids. The web replay path
      RE-PARENTS children to the synthesized replay parent step_id (the wire ids
      are absent from the replayed snapshot), so these are carried for
      fidelity/keying but parenting is rebuilt deterministically on replay.
    - ``name`` / ``tool_name`` -- the child's raw tool name (the web humanizes it).
    - ``state`` -- the CLOSED terminal two-value enum (``ToolCardState``);
      cancelled children persist nothing, matching the parent contract.
    - ``duration_ms`` -- authoritative wall-clock elapsed time (``None`` on old
      docs / when the child never reached a timed terminal).
    - ``error_code`` / ``error_message`` -- present on a FAILED child (honesty
      floor: a failed child replays RED with its reason).
    - the 7 tool-io fields -- the SAME ``ToolIoPayload`` field names as
      ``ToolCardRecord`` so a child's expander rehydrates on replay too. ALL
      optional; absent IO -> the child's chevron stays absent (no fabrication).

    Additive: ``ToolCardRecord.children`` is absent/empty on every pre-task-168
    row, which then replays as a plain top-level card (no nested timeline).
    """

    schema_version: Literal["v1"] = "v1"

    step_id: ULIDStr
    parent_step_id: ULIDStr | None = None
    name: str | None = None
    tool_name: str
    state: ToolCardState
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None

    # task-168 child tool-io fields (same names as ``ToolIoPayload`` /
    # ``ToolCardRecord``). Present only when the child dispatch's IO was captured;
    # ``None`` otherwise. ``raw_args`` / ``function_response`` are pre-serialized
    # JSON strings (same ``_json_for_tool_io`` helper the parent uses).
    raw_args: str | None = None
    function_response: str | None = None
    is_error: bool | None = None
    args_truncated: bool | None = None
    response_truncated: bool | None = None
    args_bytes: int | None = None
    response_bytes: int | None = None


class ToolCardRecord(GraceModel):
    """Replayable record of ONE tool dispatch inside a Case turn (job-0267).

    The live UI renders tool usage cards inline in the chat scroll from
    ``pipeline-state`` envelopes (``feedback_chat_tool_interleave``); those
    envelopes are wire-only and were LOST on Case reopen. This record is the
    persisted twin: minimal terminal state of one dispatched registry tool so
    the rehydration replay (``CaseSessionState.chat_history``) can re-render
    the card without replaying the live pipeline.

    ``duration_ms`` / ``started_at`` mirror the authoritative job-0264 stamps
    on ``PipelineStepSummary`` (the agent copies them from the emitter's
    terminal step, falling back to a wall-clock measure around the dispatch).
    ``label`` is the human-facing step name the live card showed (the registry
    display name); the client MAY override it with its own humanizer keyed
    on ``tool_name``.

    C1 (the IO-rehydration fix): the live chat tool-card expander (input args +
    ``function_response`` chevron) is driven by the wire-only ``tool-io``
    sidecar (``ToolIoPayload``), which was LOST on Case reopen — the chevron
    went blank. The 7 IO fields below persist the SAME values the live sidecar
    carries, under the EXACT same field names (reuse, do NOT invent new ones),
    directly on the TYPED record so ``get_session_state`` replay carries them on
    ``m.tool_card`` (the contract-blessed access path the web renderer reads).
    They mirror ``web/src/contracts.ts`` ``ToolCardRecord`` 1:1. ``raw_args`` /
    ``function_response`` are pre-serialized JSON STRINGS (the agent json-dumps +
    pretty-prints them with the SAME ``_json_for_tool_io`` helper the live emit
    uses, so persisted shape == wire shape); ``*_truncated`` / ``*_bytes`` carry
    the large-payload truncation note; ``is_error`` mirrors the honesty-floor
    signal. ALL optional / default ``None`` so pre-C1 documents (no IO fields)
    validate + replay unchanged — the chevron simply stays absent for an old
    card with no persisted IO.
    """

    schema_version: Literal["v1"] = "v1"

    tool_name: str  # registry tool name (e.g. "fetch_3dep_dem")
    state: ToolCardState
    started_at: UTCDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    label: str | None = None  # human-facing card label at dispatch time

    # C1 — persisted tool-io fields (same names as ``ToolIoPayload`` /
    # ``web/src/contracts.ts:698-704``). Present only when the dispatch's IO was
    # captured; ``None`` on pre-C1 / IO-less cards. ``raw_args`` /
    # ``function_response`` are pre-serialized JSON strings.
    raw_args: str | None = None
    function_response: str | None = None
    is_error: bool | None = None
    args_truncated: bool | None = None
    response_truncated: bool | None = None
    args_bytes: int | None = None
    response_bytes: int | None = None

    # task-168 nested sub-step persistence -- the ordered CHILD steps (a
    # composer's internal atomic-tool calls) under this top-level card. Additive
    # + optional: absent / empty on every pre-task-168 row (which then replays as
    # a plain top-level card, no nested timeline). The replay path (warm reopen +
    # box-off cold view) rebuilds the nested timeline from these so they nest
    # exactly like the live render, READ-ONLY (no re-dispatch). Ordered
    # chronologically (start order). Mirrors ``web/src/contracts.ts``
    # ``ToolCardRecord.children`` 1:1.
    children: list[PersistedSubStepRecord] | None = None


class CaseChatMessage(GraceModel):
    """One persisted chat exchange in a Case session (FR-MP-6 persistence).

    Mirrors ``ChatMessage`` (D.6) but carries the per-turn **layer / map-command
    emissions** so Case rehydration can replay deterministically: when a user
    re-opens a Case, the client re-binds layers via the same emission sequence
    the original turn produced.

    Invariant 1 (determinism boundary): ``map_command_emissions`` carries the
    typed map-command args (``LoadLayerArgs`` / ``ZoomToArgs`` / etc., dumped
    via ``model_dump(mode="json")``) so the replay path doesn't re-parse free
    text. We hold them as ``dict`` here to avoid a cross-module import cycle
    (ws.py imports from common only). The agent service round-trips each entry
    through ``MAP_COMMAND_ARGS`` validation before write.

    job-0267 (full-stream persistence): ``role`` gains the ``"tool"`` value —
    one ``role="tool"`` message per dispatched registry tool, interleaved with
    the ``user`` / ``agent`` turns by ``created_at``, so a Case reopen replays
    the FULL stream (user prompt → tool cards → agent narration) in arrival
    order. For tool messages the typed payload is ``tool_card``
    (``ToolCardRecord``) — the contract-blessed access path the web renderer
    consumes — and ``content`` carries the same record as a JSON string
    (belt-and-suspenders for non-contract consumers; never free text).
    ``tool_card`` is ``None`` for every other role, and pre-job-0267 documents
    (no ``tool_card`` field at all) validate unchanged.
    """

    schema_version: Literal["v1"] = "v1"

    message_id: ULIDStr  # matches the WS envelope id for agent messages
    case_id: ULIDStr  # owning Case
    role: Literal["user", "agent", "system", "tool"]
    content: str  # accumulated text after streaming completes

    # Thinking persistence (LANE CORE, 2026-07-22): the reasoning-channel text
    # streamed for the SAME bubble as this row's answer, persisted so a Case
    # reopen can re-render the greyed foldable thinking block (same-bubble
    # contract as the live ``agent-thinking-chunk`` wire). Set ONLY on
    # ``role="agent"`` rows of turns whose per-turn ``show_thinking`` toggle was
    # ON; ``None`` otherwise and on every pre-existing document (additive).
    # The field NAME "thinking" is the fixed cross-lane interface contract
    # (the QGIS plugin lane reads this exact key).
    #
    # NEVER-REHYDRATE INVARIANT (NATE): this field is display-replay material
    # ONLY. ``adapter.build_contents_from_history`` / ``adapter.
    # rehydrate_history_from_case`` skip it BY RULE (``adapter.
    # NEVER_REHYDRATE_FIELDS``) -- thinking text must never re-enter LLM-bound
    # contents, including via the full-fidelity ``parts_blob`` path.
    thinking: str | None = None

    # job-0267: typed tool-card payload; set IFF ``role == "tool"``.
    tool_card: ToolCardRecord | None = None

    # Link to the PipelineRecord (D.6 PipelineSnapshot) this turn dispatched,
    # if any. None for pure-chat turns that emitted no pipeline.
    pipeline_id: ULIDStr | None = None

    # Per-turn layer emissions: layer_ids the agent surfaced this turn so the
    # rehydration replay knows which layers to re-register.
    layer_emissions: list[str] = Field(default_factory=list)

    # Per-turn map-command emissions: ``[{"command": "...", "args": {...}}, ...]``.
    # The agent validates each entry against ``ws.MAP_COMMAND_ARGS`` at emit
    # time; here they are dicts to keep the contract acyclic.
    map_command_emissions: list[dict] = Field(default_factory=list)

    created_at: UTCDatetime


class CaseSessionState(GraceModel):
    """The rehydration envelope returned when a user opens a Case (FR-MP-6 resume).

    The client uses this to reconstruct the full Case session: the chat panel
    re-renders ``chat_history``, the LayerPanel re-registers ``loaded_layers``
    against QGIS Server (the published ``.qgs`` is the source-of-truth per
    FR-MP-3), the PipelineStrip reflects ``current_pipeline`` and the audit
    history reflects ``pipeline_history``.

    ``loaded_layers`` and ``pipeline_history`` / ``current_pipeline`` are kept
    as ``dict`` / ``list[dict]`` here to mirror the ``SessionStatePayload``
    shape (ws.SessionStatePayload) — collections.py owns the concrete
    ``ProjectLayerSummary`` / ``PipelineSnapshot`` / ``PipelineStepSummary``
    shapes; the agent serializes them into this envelope via
    ``model_dump(mode="json")`` before sending.
    """

    schema_version: Literal["v1"] = "v1"

    case: CaseSummary
    chat_history: list[CaseChatMessage] = Field(default_factory=list)
    loaded_layers: list[dict] = Field(default_factory=list)  # ProjectLayerSummary[]
    pipeline_history: list[dict] = Field(default_factory=list)  # PipelineSnapshot[]
    current_pipeline: dict | None = None  # PipelineSnapshot | None
    # job-0294b (sprint-14-aws): the persisted chart replay set. job-0230
    # ``$push``es SessionChartRecords onto the sessions doc, but the read side
    # was never wired — a re-opened Case dropped its charts. Each entry here is
    # a ``ChartEmissionPayload`` dict (the record's ``.payload``, emitted-at
    # order); the client rehydrates ChartStack/ChartGallery from it (App.tsx
    # ``activeSession.charts``). Empty for Cases that emitted no charts.
    charts: list[dict] = Field(default_factory=list)  # ChartEmissionPayload[]


# --------------------------------------------------------------------------- #
# Data-island thin manifest (#165 — cold-serve cases+layers from S3)
# --------------------------------------------------------------------------- #
#
# The materialized case-view SNAPSHOT (Lane A1) is the FULL ``case-open``
# payload (chat history, tool cards, charts, inline GeoJSON, ...) — fat by
# design so the cold-view path renders a Case byte-identically to the live
# wire. The data-island North Star (project_scale_to_zero_island_architecture)
# wants the DATA island to be self-serving: a future cold path that lists
# cases + their layers (title / bbox / hazard / layer asset URLs) straight from
# S3 with the agent box asleep, WITHOUT downloading the fat snapshot per Case.
#
# This ``CaseManifest`` is that THIN per-case index. Phase note: it is written
# ALONGSIDE the snapshot (dual-write); the snapshot is NOT retired here — cold
# serving + snapshot retirement are later phases. The layer list is sourced
# from the SAME data ``case_list`` marshals — the Case doc's
# ``loaded_layer_summaries`` (``ProjectLayerSummary`` dicts) — so the manifest
# never diverges from what the live list shows.


class CaseManifestLayer(GraceModel):
    """One layer row in a Case's thin manifest (#165 data-island index).

    A projection of ``ProjectLayerSummary`` (D.2 / ``collections.py``) carrying
    only what a cold-serve path needs to register the layer on the map:

    - ``asset_url`` is the DISPLAY face — the browser-readable artifact the cold
      path serves (resolved to a served / pre-signed URL by the future
      materializer): a raster tile-template, a vector ``.geojson`` asset
      (frozen Wave A contract: ``case-data/<case_id>/<layer_id>.geojson``), or a
      QGIS WMS GetMap URL. It mirrors ``ProjectLayerSummary.wms_url`` (the slot
      ``observe_published_layer`` routes every display face into), falling back
      to ``uri`` only when no display face was registered.
    - ``wms_url`` is the OPTIONAL QGIS WMS GetMap face when one was minted
      (present only for the WMS display branch); ``None`` for raster-tile /
      vector-geojson layers.
    - ``bbox`` is per-layer extent when known. Persisted ``ProjectLayerSummary``
      dicts carry no per-layer bbox today (the live zoom-to bbox lives only on
      the transient ``LayerURI``), so this is ``None`` in practice until a later
      phase persists it; the Case-level ``CaseManifest.bbox`` is the AOI fallback.

    No cost field (invariant 9). ``extra="forbid"`` (GraceModel) so a stray
    storage key never silently leaks into the manifest.
    """

    schema_version: Literal["v1"] = "v1"

    layer_id: str
    name: str
    layer_type: Literal["raster", "vector"]
    style_preset: str
    asset_url: str  # display face the cold path serves (wms_url, else uri)
    bbox: BBox | None = None  # per-layer extent when known; AOI fallback at Case
    wms_url: str | None = None  # optional QGIS WMS GetMap face (display branch)


class CaseManifest(GraceModel):
    """Thin per-case S3 manifest (#165 — cold-serve cases+layers, agent asleep).

    Written ALONGSIDE the fat case-view snapshot (dual-write) at the SAME Case
    mutation call-sites. A future cold path lists Cases + their layers from
    these manifests WITHOUT the agent and WITHOUT downloading the fat snapshot.

    The owner is NOT a body field — it travels in S3 OBJECT METADATA exactly as
    the snapshot does (``_doc_to_case_summary`` drops owner-link fields, so the
    body is owner-free; the signer owner-matches off ``head_object`` metadata).

    Fields mirror the ``CaseSummary`` denormalization the left rail already
    consumes (``title`` / ``bbox`` / ``primary_hazard``), plus the projected
    ``layers`` list. ``updated_at`` stamps the manifest write so a reader can
    tell staleness; ``schema_version`` versions the manifest shape independently
    of the layer-row shape.
    """

    schema_version: Literal["v1"] = "v1"

    case_id: ULIDStr
    updated_at: UTCDatetime  # ISO-8601 UTC — when the manifest was materialized
    title: str
    bbox: BBox | None = None  # [minLon, minLat, maxLon, maxLat] EPSG:4326 (AOI)
    primary_hazard: str | None = None
    layers: list[CaseManifestLayer] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# WebSocket envelopes for Case lifecycle (A.4 / A.3 amendments)
# --------------------------------------------------------------------------- #


class CaseListEnvelopePayload(GraceModel):
    """``case-list`` (A.4 amendment): server -> client list of all Cases.

    Emitted on session connect (initial landing state per FR-MP-6) and
    refreshed after any Case lifecycle command (``create`` / ``rename`` /
    ``archive`` / ``delete``). The client renders the left rail from this list.

    ``envelope_type`` is a Literal discriminator (the message ``type`` on the
    A.1 envelope is ``"case-list"``); we mirror the field here as a typed
    literal so the payload is self-describing when serialized standalone.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-list"

    envelope_type: Literal["case-list"] = "case-list"
    cases: list[CaseSummary] = Field(default_factory=list)


class CaseOpenEnvelopePayload(GraceModel):
    """``case-open`` (A.4 amendment): server -> client rehydrate selected Case.

    Emitted in response to a ``case-command`` with ``command=select`` (or on
    successful ``create``). ``session_state`` is ``None`` when the server
    cannot rehydrate (e.g. the Case was archived/deleted between list and
    select); the client falls back to the empty state in that case.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-open"

    envelope_type: Literal["case-open"] = "case-open"
    session_state: CaseSessionState | None = None


# Closed enum: Case lifecycle commands. The set is closed at v0.1 — a new
# command is an SRS amendment (FR-MP-6) not a silent open-enum, because the
# server-side dispatch table needs to enumerate handlers.
# job-0269 (proposed FR-MP-6/A.3 amendment): ``deselect`` — the client
# navigated OUT of the active Case to the Cases root. Carries no case_id.
# Without it the session-scoped active Case silently kept pointing at the
# last-opened Case, so root prompts skipped auto-create and dispatched into
# the stale Case.
CaseCommand = Literal[
    "create", "select", "deselect", "rename", "archive", "delete", "set-bbox"
]


class CaseCommandEnvelopePayload(GraceModel):
    """``case-command`` (A.3 amendment): client -> server Case lifecycle command.

    Fields:

    - ``command`` — one of ``create`` / ``select`` / ``deselect`` / ``rename``
      / ``archive`` / ``delete`` (closed enum).
    - ``case_id`` — required for every command except ``create`` (the server
      generates the ULID on create and replies with a ``case-open``) and
      ``deselect`` (which clears the active-Case binding; no target).
    - ``args`` — command-specific args dict. For ``rename`` it carries
      ``{"title": "<new title>"}``; for ``create`` it MAY carry an initial
      ``{"title": "..."}`` hint. The server validates the args dict against
      the command-specific schema before dispatch; we keep it as ``dict`` at
      the envelope level to mirror the ``MapCommandPayload`` pattern (one
      umbrella type with a ``command`` discriminator).

    No cost field anywhere (invariant 9). No cancellation field — cancellation
    flows through the existing A.3 ``cancel`` message (invariant 8).
    """

    MESSAGE_TYPE: ClassVar[str] = "case-command"

    envelope_type: Literal["case-command"] = "case-command"
    command: CaseCommand
    case_id: ULIDStr | None = None
    args: dict = Field(default_factory=dict)
