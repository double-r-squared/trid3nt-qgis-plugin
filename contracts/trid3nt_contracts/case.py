"""The Case persistence envelopes.

A "Case" is the user-facing name for a project document; ``case_id`` IS the
project id. This module owns the wire shapes behind the Case flow - the
left-rail summary, the persisted chat exchange, the rehydration replay and the
lifecycle command. No cost field appears on any of them.
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
    # Case persistence envelopes
    "CaseStatus",
    "CaseSummary",
    "CaseChatMessage",
    "CaseSessionState",
    "ToolCardRecord",
    "ToolCardState",
    "PersistedSubStepRecord",
    # WebSocket envelopes
    "CaseListEnvelopePayload",
    "CaseOpenEnvelopePayload",
    "CaseCommand",
    "CaseCommandEnvelopePayload",
]


# --------------------------------------------------------------------------- #
# Case persistence envelopes
# --------------------------------------------------------------------------- #

# Closed enum: Case lifecycle status. ``deleted`` is a soft-delete tombstone,
# not a removal. The list is CLOSED - a new status is an explicit amendment
# rather than a value that quietly appears on the wire.
CaseStatus = Literal["active", "archived", "deleted"]


class CaseSummary(GraceModel):
    """Top-level Case record - the left-rail entity.
    Denormalized from the project document so a client renders the list without
    joining sessions or runs.
    """

    schema_version: Literal["v1"] = "v1"

    case_id: ULIDStr  # maps 1:1 to the project document's id
    title: str  # user-edited; the project document's ``name`` is the store
    created_at: UTCDatetime
    updated_at: UTCDatetime
    status: CaseStatus = "active"

    bbox: BBox | None = None
    # Primary hazard label is denormalized from the Case's runs; open enum so
    # registering a new hazard does not break the Case envelope.
    primary_hazard: str | None = None

    # A flat list of layer ids only: full layer detail arrives on Case open, so
    # the left-rail summary stays cheap.
    layer_summary: list[str] = Field(default_factory=list)

    # The per-Case mirror of the in-memory layer set, so a re-open on a fresh
    # connection rehydrates deterministically instead of showing nothing.
    # Entries are full layer-summary ``model_dump(mode="json")`` shapes.
    # Dedup is by ``uri``: republishing a layer overwrites its entry in place.
    loaded_layer_summaries: list[dict] = Field(default_factory=list)

    # Lazy-init: a fresh Case has no published project file, and the URI is
    # written on first layer emission.
    qgs_project_uri: str | None = None


# Persisted tool-card lifecycle states. NOTHING about the chat is transient, so
# a long-running solve card is written the moment it is minted (``running``) and
# UPDATED IN PLACE to its terminal state: ONE row whose ``state`` walks, keyed by
# a stable ``message_id`` - an upsert, never a duplicate. A short atomic-tool
# dispatch instead persists once, at terminal, so a cancelled one leaves no row;
# a cancelled SOLVE does persist, because a stopped sim is a finished sim the
# user must be able to trace afterwards. A child step only ever carries the two
# terminal values; the wider type is a harmless superset for it.
ToolCardState = Literal["running", "complete", "failed", "cancelled"]


class PersistedSubStepRecord(GraceModel):
    """Replayable record of ONE nested CHILD step under a tool card.
    Field names reuse the live step and card shapes VERBATIM, so a replay
    synthesizes a step straight off this record."""

    schema_version: Literal["v1"] = "v1"

    # Carried for fidelity and keying, but a replay RE-PARENTS children onto
    # the step id it synthesizes: the live wire ids are absent from a snapshot.
    step_id: ULIDStr
    parent_step_id: ULIDStr | None = None
    #: The child's RAW tool name; a client humanizes it for display.
    name: str | None = None
    tool_name: str
    state: ToolCardState
    #: Authoritative wall-clock elapsed time. ``None`` when the child never
    #: reached a timed terminal state.
    duration_ms: int | None = Field(default=None, ge=0)
    #: Present on a FAILED child, so it replays red WITH its reason.
    error_code: str | None = None
    error_message: str | None = None

    # Child tool-io, under the SAME field names the live sidecar carries.
    # Present only when the dispatch's IO was captured; absent IO leaves the
    # child's expander absent rather than fabricating one. ``raw_args`` and
    # ``function_response`` are pre-serialized JSON strings.
    raw_args: str | None = None
    function_response: str | None = None
    is_error: bool | None = None
    args_truncated: bool | None = None
    response_truncated: bool | None = None
    args_bytes: int | None = None
    response_bytes: int | None = None


class ToolCardRecord(GraceModel):
    """Replayable record of ONE tool dispatch inside a Case turn.
    The persisted twin of a live card - the minimal state to re-render it
    without replaying the pipeline. Every IO field is optional."""

    schema_version: Literal["v1"] = "v1"

    tool_name: str  # the registry tool name
    state: ToolCardState
    # The authoritative stamps, copied from the terminal step where there is
    # one and measured around the dispatch otherwise.
    started_at: UTCDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    # The card label at dispatch time. A client MAY override it with its own
    # humanizer keyed on ``tool_name``.
    label: str | None = None

    # Persisted tool-io, under the SAME field names the live sidecar carries so
    # persisted shape equals wire shape. ``raw_args`` and ``function_response``
    # are pre-serialized JSON strings; ``*_truncated`` and ``*_bytes`` carry the
    # large-payload note, and ``is_error`` the honesty signal.
    raw_args: str | None = None
    function_response: str | None = None
    is_error: bool | None = None
    args_truncated: bool | None = None
    response_truncated: bool | None = None
    args_bytes: int | None = None
    response_bytes: int | None = None

    # The ordered CHILD steps under this card, chronological by start. A replay
    # rebuilds the nested timeline from these, READ-ONLY - it never re-dispatches.
    # Additive and optional: a row without them replays as a plain card.
    children: list[PersistedSubStepRecord] | None = None


class CaseChatMessage(GraceModel):
    """One persisted chat exchange in a Case session.
    It carries the per-turn layer and map-command emissions, so a re-open
    replays the FULL stream in arrival order and re-binds what the turn did."""

    schema_version: Literal["v1"] = "v1"

    message_id: ULIDStr  # matches the wire envelope id for agent messages
    case_id: ULIDStr  # owning Case
    # One ``tool`` row per dispatched tool, interleaved with the user and agent
    # turns by ``created_at``.
    role: Literal["user", "agent", "system", "tool"]
    # Accumulated text once streaming completes. On a ``tool`` row this is the
    # same record as ``tool_card``, serialized - never free text.
    content: str

    # The reasoning-channel text streamed for the SAME bubble as this row's
    # answer. Set ONLY on ``role="agent"`` rows whose turn had thinking on.
    # The field NAME is a fixed cross-surface interface: a reader keys on it.
    #
    # NEVER REHYDRATED: display-replay material only. Thinking text must never
    # re-enter LLM-bound contents, by any path.
    thinking: str | None = None

    # typed tool-card payload; set IFF ``role == "tool"``.
    tool_card: ToolCardRecord | None = None

    # The pipeline this turn dispatched, if any. ``None`` for a pure-chat turn.
    pipeline_id: ULIDStr | None = None

    # Per-turn layer emissions: layer_ids the agent surfaced this turn so the
    # rehydration replay knows which layers to re-register.
    layer_emissions: list[str] = Field(default_factory=list)

    # ``[{"command": "...", "args": {...}}, ...]``. The typed args are validated
    # at emit time; they stay dicts here to keep this module acyclic.
    map_command_emissions: list[dict] = Field(default_factory=list)

    created_at: UTCDatetime


class CaseSessionState(GraceModel):
    """The rehydration envelope returned when a user opens a Case.
    Enough to reconstruct the whole session. The dict-typed fields mirror the
    live session-state payload, untyped here to keep this module acyclic."""

    schema_version: Literal["v1"] = "v1"

    case: CaseSummary
    chat_history: list[CaseChatMessage] = Field(default_factory=list)
    loaded_layers: list[dict] = Field(default_factory=list)  # ProjectLayerSummary[]
    pipeline_history: list[dict] = Field(default_factory=list)  # PipelineSnapshot[]
    current_pipeline: dict | None = None  # PipelineSnapshot | None
    # The chart replay set: one ``ChartEmissionPayload`` dict per persisted
    # chart, in emitted-at order. Empty for a Case that emitted none.
    charts: list[dict] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# WebSocket envelopes for the Case lifecycle
# --------------------------------------------------------------------------- #


class CaseListEnvelopePayload(GraceModel):
    """``case-list``: server -> client, every Case.
    Emitted on connect and re-emitted after any lifecycle command, so the
    left rail is never reconciled client-side.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-list"

    envelope_type: Literal["case-list"] = "case-list"
    cases: list[CaseSummary] = Field(default_factory=list)


class CaseOpenEnvelopePayload(GraceModel):
    """``case-open``: server -> client, rehydrate the selected Case.
    ``session_state`` is ``None`` when the Case cannot be rehydrated - archived
    or deleted between the list and the select - and the client shows empty.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-open"

    envelope_type: Literal["case-open"] = "case-open"
    session_state: CaseSessionState | None = None


# Closed enum: Case lifecycle commands. CLOSED because the dispatch table has
# to enumerate a handler for each. ``deselect`` means the client navigated OUT
# to the Cases root and carries no case_id; without it the session-scoped
# active Case keeps pointing at the last one opened, and a root prompt
# dispatches into a stale Case.
CaseCommand = Literal[
    "create", "select", "deselect", "rename", "archive", "delete", "set-bbox"
]


class CaseCommandEnvelopePayload(GraceModel):
    """``case-command``: client -> server, one Case lifecycle command.
    No cancellation field - cancellation rides its own message rather than
    becoming a lifecycle command.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-command"

    envelope_type: Literal["case-command"] = "case-command"
    command: CaseCommand
    # Required for every command except ``create``, where the server mints the
    # id, and ``deselect``, which clears the binding and has no target.
    case_id: ULIDStr | None = None
    # Command-specific args, validated against the command's own schema before
    # dispatch. Kept a dict here so one envelope covers every command.
    args: dict = Field(default_factory=dict)
