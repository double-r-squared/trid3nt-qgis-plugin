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
    ContractModel,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    "CaseStatus",
    "CaseSummary",
    "CaseChatMessage",
    "CaseSessionState",
    "ToolCardRecord",
    "ToolCardState",
    "PersistedSubStepRecord",
    "CaseListEnvelopePayload",
    "CaseOpenEnvelopePayload",
    "CaseCommand",
    "CaseCommandEnvelopePayload",
]



# Closed: a new status is an explicit amendment. ``deleted`` is a soft-delete tombstone.
CaseStatus = Literal["active", "archived", "deleted"]


class CaseSummary(ContractModel):
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
    # Open enum so registering a new hazard does not break the envelope.
    primary_hazard: str | None = None

    # Layer ids only; full detail arrives on Case open.
    layer_summary: list[str] = Field(default_factory=list)

    # Per-Case mirror of the in-memory layer set, so a re-open rehydrates deterministically; deduped by ``uri``.
    loaded_layer_summaries: list[dict] = Field(default_factory=list)

    # Written on first layer emission; a fresh Case has no project file.
    qgs_project_uri: str | None = None


# A long-running solve card is written at once (``running``) and updated in place to its terminal state: one
# row keyed by a stable ``message_id``. A short atomic dispatch persists once, at terminal; a cancelled solve
# persists too. A child step carries only the two terminal values.
ToolCardState = Literal["running", "complete", "failed", "cancelled"]


class PersistedSubStepRecord(ContractModel):
    """Replayable record of ONE nested CHILD step under a tool card.
    Field names reuse the live step and card shapes VERBATIM, so a replay
    synthesizes a step straight off this record."""

    schema_version: Literal["v1"] = "v1"

    # A replay re-parents children onto the step id it synthesizes; the live wire ids are absent from a snapshot.
    step_id: ULIDStr
    parent_step_id: ULIDStr | None = None
    name: str | None = None
    tool_name: str
    state: ToolCardState
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None

    # Child tool-io under the live sidecar's field names; absent IO leaves the expander absent.
    raw_args: str | None = None
    function_response: str | None = None
    is_error: bool | None = None
    args_truncated: bool | None = None
    response_truncated: bool | None = None
    args_bytes: int | None = None
    response_bytes: int | None = None


class ToolCardRecord(ContractModel):
    """Replayable record of ONE tool dispatch inside a Case turn.
    The persisted twin of a live card - the minimal state to re-render it
    without replaying the pipeline. Every IO field is optional."""

    schema_version: Literal["v1"] = "v1"

    tool_name: str  # the registry tool name
    state: ToolCardState
    started_at: UTCDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    # The card label at dispatch time; a client may override it keyed on ``tool_name``.
    label: str | None = None

    # Persisted tool-io under the live sidecar's field names; ``raw_args`` and ``function_response`` are pre-serialized JSON strings.
    raw_args: str | None = None
    function_response: str | None = None
    is_error: bool | None = None
    args_truncated: bool | None = None
    response_truncated: bool | None = None
    args_bytes: int | None = None
    response_bytes: int | None = None

    # Ordered child steps, chronological by start; a replay rebuilds the nested timeline read-only. Optional.
    children: list[PersistedSubStepRecord] | None = None


class CaseChatMessage(ContractModel):
    """One persisted chat exchange in a Case session.
    It carries the per-turn layer and map-command emissions, so a re-open
    replays the FULL stream in arrival order and re-binds what the turn did."""

    schema_version: Literal["v1"] = "v1"

    message_id: ULIDStr  # matches the wire envelope id for agent messages
    case_id: ULIDStr  # owning Case
    role: Literal["user", "agent", "system", "tool"]
    # On a ``tool`` row this is ``tool_card`` serialized, never free text.
    content: str

    # Reasoning-channel text of the same bubble, on ``role="agent"`` rows only; the field name is a fixed
    # cross-surface interface. Display-replay material: never rehydrated into LLM-bound contents.
    thinking: str | None = None

    # Set iff ``role == "tool"``.
    tool_card: ToolCardRecord | None = None

    pipeline_id: ULIDStr | None = None

    # Layer ids the agent surfaced this turn, for the replay to re-register.
    layer_emissions: list[str] = Field(default_factory=list)

    # Typed args are validated at emit time and stay dicts here to keep this module acyclic.
    map_command_emissions: list[dict] = Field(default_factory=list)

    created_at: UTCDatetime


class CaseSessionState(ContractModel):
    """The rehydration envelope returned when a user opens a Case.
    Enough to reconstruct the whole session. The dict-typed fields mirror the
    live session-state payload, untyped here to keep this module acyclic."""

    schema_version: Literal["v1"] = "v1"

    case: CaseSummary
    chat_history: list[CaseChatMessage] = Field(default_factory=list)
    loaded_layers: list[dict] = Field(default_factory=list)  # ProjectLayerSummary[]
    pipeline_history: list[dict] = Field(default_factory=list)  # PipelineSnapshot[]
    current_pipeline: dict | None = None  # PipelineSnapshot | None
    # One ``ChartEmissionPayload`` dict per persisted chart, in emitted-at order.
    charts: list[dict] = Field(default_factory=list)




class CaseListEnvelopePayload(ContractModel):
    """``case-list``: server -> client, every Case.
    Emitted on connect and re-emitted after any lifecycle command, so the
    left rail is never reconciled client-side.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-list"

    envelope_type: Literal["case-list"] = "case-list"
    cases: list[CaseSummary] = Field(default_factory=list)


class CaseOpenEnvelopePayload(ContractModel):
    """``case-open``: server -> client, rehydrate the selected Case.
    ``session_state`` is ``None`` when the Case cannot be rehydrated - archived
    or deleted between the list and the select - and the client shows empty.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-open"

    envelope_type: Literal["case-open"] = "case-open"
    session_state: CaseSessionState | None = None


# Closed because the dispatch table enumerates a handler for each.
CaseCommand = Literal["create", "select", "rename", "delete", "set-bbox"]


class CaseCommandEnvelopePayload(ContractModel):
    """``case-command``: client -> server, one Case lifecycle command.
    No cancellation field - cancellation rides its own message rather than
    becoming a lifecycle command.
    """

    MESSAGE_TYPE: ClassVar[str] = "case-command"

    envelope_type: Literal["case-command"] = "case-command"
    command: CaseCommand
    # Required for every command except ``create`` and ``deselect``.
    case_id: ULIDStr | None = None
    # Validated against the command's own schema before dispatch; a dict so one envelope covers every command.
    args: dict = Field(default_factory=dict)
