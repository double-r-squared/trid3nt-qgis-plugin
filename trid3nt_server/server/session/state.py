"""Per-session state: the ``SessionState`` dataclass and its registries.

The dataclass holds per-connection fields; the registries here are keyed by
``session_id``, so every connection of a session, reconnects included, shares
one binding."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from trid3nt_server.gates.circuit_breaker import ToolCircuitBreaker

if TYPE_CHECKING:
    import asyncio

    from trid3nt_contracts.ws import PipelineStep

    from trid3nt_server.render.pipeline_emitter import PipelineEmitter

logger = logging.getLogger("trid3nt_server.server")


# Session-scoped active-Case registry. A client mounts two connections per
# session and the server builds a fresh ``SessionState`` per connection, so the
# active Case is keyed by ``session_id`` instead, keeping every connection of a
# session on the same Case. Bounded: the oldest entry is evicted past the cap,
# and a stale session's next case command re-establishes its context.
_SESSION_ACTIVE_CASE: dict[str, str | None] = {}
_SESSION_ACTIVE_CASE_CAP = 4096

#: Sentinel for ``SessionState.case_context_synced_to`` -- distinct from None
#: because ``None`` is a legitimate "no active Case" binding.
_CASE_SYNC_NEVER = "__case-context-never-synced__"

#: Stream key for turns dispatched with no active Case; the client uses the
#: same key.
_ROOT_STREAM_KEY = "__root__"


def _set_session_active_case(session_id: str, case_id: str | None) -> None:
    """Bind ``case_id`` as the active Case for every connection of ``session_id``."""
    if (
        session_id not in _SESSION_ACTIVE_CASE
        and len(_SESSION_ACTIVE_CASE) >= _SESSION_ACTIVE_CASE_CAP
    ):
        # Evict the oldest by insertion order: bounded memory.
        _SESSION_ACTIVE_CASE.pop(next(iter(_SESSION_ACTIVE_CASE)))
    _SESSION_ACTIVE_CASE[session_id] = case_id


@dataclass
class SessionState:
    """Per-session in-memory state, the live mirror rather than the durable
    store: a Case re-open rehydrates chat, layers and charts from persistence,
    while this dies with the process."""

    session_id: str
    chat_history: list[dict] = field(default_factory=list)
    current_pipeline_id: str | None = None
    current_pipeline_steps: list[PipelineStep] = field(default_factory=list)
    # In-flight turns keyed by STREAM: a case id, or the root key. Only a
    # re-prompt in the SAME stream cancels that stream's turn; turns in other
    # Cases keep running. Their persistence follows the turn's Case pin and
    # their model context is the per-turn captured history, so a concurrent
    # turn cannot re-aim either. KNOWN LIMIT, display only: a client routes
    # live envelopes to the last-submitted stream, so a still-running turn's
    # late envelopes may paint in the newer stream; the persisted replay is
    # always correct.
    inflight_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    emitter: PipelineEmitter | None = None
    # Per-session turn counter, incremented on every user-message dispatch. Past
    # the cap the agent refuses further dispatch and emits the max-turns
    # envelope. A new connection builds a fresh state, so the counter restarts.
    turn_count: int = 0
    # Per-connection marker of which Case this connection's in-memory context -
    # chat history and the emitter's loaded layers - was last synced to. The
    # string sentinel, never a valid case id, means never synced, and ``None``
    # is the legitimate "no active Case" value.
    case_context_synced_to: str | None = _CASE_SYNC_NEVER
    # Durable cache of the active Case's persisted AOI bbox, set when that Case
    # is selected or synced and cleared on deselect. It is the AOI anchor the
    # reuse short-circuits and the bbox auto-fill read; ``None`` is legitimate,
    # meaning no active Case or a Case with no recorded bbox.
    case_bbox: Any = None
    # The session's ACTIVE canvas AOI -- structured ``aoi_bbox``
    # ([min_lon, min_lat, max_lon, max_lat], EPSG:4326) set/cleared by
    # ``_set_active_aoi_from_payload``. Read by dispatch-time bbox auto-fill:
    # explicit arg > active AOI > case bbox. ``None`` = no drawn AOI.
    active_aoi_bbox: list[float] | None = None
    # The turn's user-DRAWN geometry, set or cleared per user-message and bound
    # into a per-task context var so a gate reads it as a user-basis spatial
    # knob. Distinct from the active AOI: a drawn region is a sub-region knob,
    # not the analysis extent. ``None`` means nothing was drawn.
    drawn_geometry: dict | None = None
    # Per-session routing-visibility mode ('auto' | 'ask').
    # Set by the ``session-config`` envelope's ``mode`` field; ``None`` falls
    # back to the TRID3NT_MODE env default (see _session_routing_mode). Governs
    # tool-selection VISIBILITY only -- consent gates are never mode-dependent.
    routing_mode: str | None = None
    # The armed bench block config, set only by the bench harness. ``None`` is
    # normal operation and the dispatch guard is then a single identity check;
    # armed, the dispatch site blocks a wrong or block-tier pick before the tool
    # function runs.
    bench_block_config: Any = None
    # Per-turn layer + map-command emission accumulators. Reset at
    # the start of every dispatch (model stream or /invoke tool). The
    # CaseChatMessage write at turn close reads from these so a Case replay
    # can re-bind layers via the same emission sequence.
    current_turn_layer_ids: list[str] = field(default_factory=list)
    current_turn_pipeline_id: str | None = None
    # Per-turn zoom-to accumulator, persisted onto the closing agent row so a
    # Case reopen can snap the camera back by replaying the last one.
    current_turn_map_commands: list[dict] = field(default_factory=list)
    # Per-turn narration accumulator: reset at stream start, appended per text
    # delta across every loop iteration, and joined at turn close into the
    # persisted agent row, so a Case reopen replays what the agent said.
    current_turn_narration: list[str] = field(default_factory=list)
    # Set when a turn aborts on a clipped prompt. The turn wrapper reads and
    # clears it, appending the text to whichever partial-narration row it is
    # about to persist, so the abort verdict lands in the SAME chat row as the
    # streamed text rather than only in an envelope a dead socket may drop.
    current_turn_context_abort_note: str | None = None
    # The Case this TURN is bound to, pinned at dispatch time before the first
    # write. Every turn-scoped write - chat rows, tool cards, layer attribution,
    # project routing, charts - targets THIS binding, never the live pointer,
    # which a mid-stream select can re-point.
    current_turn_case_id: str | None = None
    # Per-connection authenticated user context, populated by the connect
    # handshake. Once set, every later envelope on this connection is scoped to
    # it: Case lookups filter by it and a created Case is owned by it. ``None``
    # only between connect and handshake completion.
    authenticated_user_id: str | None = None
    is_anonymous: bool = True
    auth_handshake_complete: bool = False
    # A client's keepalive sends an empty ``session-resume`` as a proof-of-life
    # ping, indistinguishable from a genuine fresh-socket resume by the envelope
    # alone. This flag is the gate: the FIRST resume on THIS connection replays
    # layers and every later one is a ping, which still gets its session-state
    # pong so the client's deadline clears.
    did_fresh_resume: bool = False
    # Per-connection latch for the active-Case REBIND decision, distinct from
    # the flag above, which gates the layer replay. A session mounts two sockets
    # and each sends its own keepalive, so this flips after the FIRST resume on
    # THIS connection and the client-stamp rebind fires only on a genuine fresh
    # resume. Explicit user intent still rebinds unconditionally.
    did_first_resume: bool = False
    # Per-session audit log of payload-warning events. Each entry is a dict
    # carrying ``warning_id``, ``tool_name``, ``estimated_mb``,
    # ``threshold_mb``, ``decision`` (set on confirmation), and the ULID
    # timestamps. Surfaces in tests + post-mortem; persisted to the active
    # Case as part of the chat turn record (best-effort).
    payload_warning_audit_log: list[dict] = field(default_factory=list)
    # Monotonic visible-tool accumulator: every tool once made visible stays in
    # it, so a once-visible tool never leaves mid-task. It grows within a
    # session and a new session starts fresh.
    visible_tools: set[str] = field(default_factory=set)
    # Per-session provider-side prompt-cache handle, reported through the
    # cache-status envelope. Every live path caches through its own in-request
    # breakpoints, so no handle is tracked and this stays ``None``.
    model_cache_ref: str | None = None
    # Per-session circuit breaker, tripped by consecutive per-tool failures and
    # enforcing a cooldown. The stream checks it before every dispatch and
    # records the outcome after; a tripped breaker raises, and the result
    # summary surfaces that as a structured envelope the model narrates.
    circuit_breaker: ToolCircuitBreaker = field(default_factory=ToolCircuitBreaker)
    # Per-TURN set of tools that already surfaced a credential request. The
    # pipeline prompts and retries ONCE per tool per turn; without this guard a
    # still-invalid key would re-trip the auth error and re-prompt forever.
    credential_prompted_tools: set[str] = field(default_factory=set)
    # Per-TURN memory of gate decisions, keyed by tool name plus the rounded
    # bbox, or the full normalized args when there is no bbox. A model that
    # retries a gated tool with corrected NON-bbox args would otherwise re-emit
    # an identical gate on the same bbox every retry, and since a local gate has
    # no timeout, the unanswered second gate hangs the turn forever. Only
    # proceed and narrow-scope decisions are recorded - a cancel raises before
    # the write, so a corrected retry still re-gates. Values are the DELTA the
    # gate applied, not the whole approved dict, so a retry keeps its own
    # corrected args.
    gate_decisions_this_turn: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    # In-chat model selector: the model id chosen by the user for the current
    # turn. Updated on every ``user-message`` that carries a non-None
    # ``model_id``; persists across turns so consecutive messages without one
    # inherit the last-chosen model. ``None`` means "use the server default"
    # (the active adapter's own model resolver).
    selected_model: str | None = None

    # Active-Case context -- session-scoped, NOT per-connection.

    @property
    def active_case_id(self) -> str | None:
        """The active Case for this SESSION, shared across its connections and
        ``None`` when none is selected; a create or select on ANY connection
        updates it, and deleting the active Case clears it."""
        return _SESSION_ACTIVE_CASE.get(self.session_id)

    @active_case_id.setter
    def active_case_id(self, value: str | None) -> None:
        _set_session_active_case(self.session_id, value)
