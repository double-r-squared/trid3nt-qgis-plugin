"""Per-session state layer -- the ``SessionState`` dataclass and the
session-scoped registries it is backed by.

The split NATE named: state is separated from behavior. ``SessionState`` is a
plain ``@dataclass`` of per-connection fields plus one lifecycle accessor (the
``active_case_id`` property, backed by the session-scoped ``_SESSION_ACTIVE_CASE``
registry). All the turn/dispatch BEHAVIOR that reads a session already lives as
module-level functions taking ``state`` as their first argument -- those stay in
:mod:`._core` with the turn engine. This module holds only the data and the
registries that give it session (not connection) scope: the active-Case pointer
and the anon-identity mirror, both keyed by ``session_id`` so every connection of
a session -- including post-reconnect replacements -- shares one binding.

``_core`` re-imports every name here so its bare-global references and the
facade-proxied monkeypatch targets on ``trid3nt_server.server.<name>`` resolve
exactly as when the whole file was one module; the package facade re-exposes them
at ``trid3nt_server.server.<name>`` and propagates monkeypatch writes to this
module (it is in ``_EXTRACTION_MODULES``).

Deliberately NOT here (stays in ``_core`` with the connection loop): the
``_LiveTurn`` detached-turn registry (``_SESSION_LIVE_TURNS`` +
register/rebind/find), which the WS handler owns and drives on every connect /
disconnect / cancel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from trid3nt_contracts.auth import AuthTokenEnvelope

from ..agent.categories import AllowedToolSet
from ..agent.gates.circuit_breaker import ToolCircuitBreaker

if TYPE_CHECKING:
    import asyncio

    from trid3nt_contracts.ws import PipelineStep

    from ..emission.pipeline_emitter import PipelineEmitter

logger = logging.getLogger("trid3nt_server.server")


# Session-scoped active-Case registry. The client mounts two WebSocket
# connections per tab (Chat.tsx + App.tsx) sharing one session_id; the
# server builds a fresh ``SessionState`` per connection, so this registry
# keys the active Case by ``session_id`` instead, keeping every connection
# of a session -- including post-reconnect replacements -- on the same Case
# context. Bounded: oldest entries evicted past the cap (a stale session's
# next case-command re-establishes context).
_SESSION_ACTIVE_CASE: dict[str, str | None] = {}
_SESSION_ACTIVE_CASE_CAP = 4096

#: Sentinel for ``SessionState.case_context_synced_to`` -- distinct from None
#: because ``None`` is a legitimate "no active Case" binding.
_CASE_SYNC_NEVER = "__case-context-never-synced__"

#: Stream key for turns dispatched with no active Case (mirrors the
#: client's ROOT_STREAM_KEY in Chat.tsx).
_ROOT_STREAM_KEY = "__root__"


def _set_session_active_case(session_id: str, case_id: str | None) -> None:
    """Bind ``case_id`` as the active Case for every connection of ``session_id``."""
    if (
        session_id not in _SESSION_ACTIVE_CASE
        and len(_SESSION_ACTIVE_CASE) >= _SESSION_ACTIVE_CASE_CAP
    ):
        # Evict oldest (insertion order) -- bounded memory, see note above.
        _SESSION_ACTIVE_CASE.pop(next(iter(_SESSION_ACTIVE_CASE)))
    _SESSION_ACTIVE_CASE[session_id] = case_id


# Session-scoped ANON-ID registry: belt-and-suspenders mirror of
# ``_SESSION_ACTIVE_CASE`` for the dual-socket anon-identity race. The web
# mounts two WebSocket connections per tab sharing one session_id, each
# running its own auth handshake; in the rare first-connect window before a
# client hint is persisted, each connection would otherwise mint a
# different random anon ULID and fork the owner-scoped case-list. This
# registry records ``session_id -> anon_user_id`` on first mint/bind so a
# sibling connection of the same session reuses it instead. Bounded like
# ``_SESSION_ACTIVE_CASE``; only anonymous ids are recorded here.
_SESSION_ANON_ID: dict[str, str] = {}
_SESSION_ANON_ID_CAP = 4096


def _get_session_anon_id(session_id: str) -> str | None:
    """Return the anon ``user_id`` bound to ``session_id`` this process, if any."""
    return _SESSION_ANON_ID.get(session_id)


def _set_session_anon_id(session_id: str, anon_user_id: str) -> None:
    """Record ``anon_user_id`` as the session's anon identity (idempotent).

    Bounded + insertion-order eviction, mirroring ``_set_session_active_case``.
    No-op when ``anon_user_id`` is falsy (defensive -- never record an empty id).
    """
    if not session_id or not anon_user_id:
        return
    if (
        session_id not in _SESSION_ANON_ID
        and len(_SESSION_ANON_ID) >= _SESSION_ANON_ID_CAP
    ):
        # Evict oldest (insertion order) -- bounded memory, see note above.
        _SESSION_ANON_ID.pop(next(iter(_SESSION_ANON_ID)))
    _SESSION_ANON_ID[session_id] = anon_user_id


def _apply_session_anon_hint(
    session_id: str, tok: "AuthTokenEnvelope | None"
) -> "AuthTokenEnvelope | None":
    """Fill a MISSING anon hint from the session-scoped registry.

    cases-vanish fix (belt-and-suspenders). When a connection of ``session_id``
    presents no token AND no ``anonymous_user_id`` hint, but a sibling
    connection of the same session already bound an anon identity this process,
    return a copy of the envelope carrying that recorded id as the hint -- so
    ``authenticate_token`` reuses the SAME anon user instead of minting a fresh
    random ULID. This collapses the (now rare) first-connect no-hint window
    where the App + Chat sockets would otherwise fork the owner-scoped
    case-list.

    Strictly additive / non-clobbering:
    - A client-supplied hint always wins (it is the durable, cross-refresh id) --
      we only fill when the hint is absent.
    - A non-empty ``token`` is left untouched: a presented token resolves via
      ``authenticate_token``'s own fallback, never diverted to an anon id.
    - No registry entry -> the envelope is returned unchanged.
    """
    recorded = _get_session_anon_id(session_id)
    if not recorded:
        return tok
    # Only fill the anonymous path: a present token means the verify path owns
    # this connect (authed path unaffected).
    if tok is not None and (tok.token or "").strip():
        return tok
    # A client-supplied hint is the durable id -- never override it.
    if tok is not None and tok.anonymous_user_id:
        return tok
    if tok is None:
        return AuthTokenEnvelope(token="", anonymous_user_id=recorded)
    return tok.model_copy(update={"anonymous_user_id": recorded})


@dataclass
class SessionState:
    """Per-session in-memory state, held in-process for the life of the session.

    Durable restore is a separate path: ``Persistence.get_session_state``
    rehydrates chat history, loaded layers, and charts on ``case-open`` /
    ``case-select``. This dataclass is the live in-process mirror, not the
    durable store.

    Owns the per-session ``PipelineEmitter``, which owns the current
    ``PipelineSnapshot`` + ``loaded_layers`` accumulator and broadcasts real
    ``pipeline-state`` / ``session-state`` envelopes (replace-not-reconcile).
    ``current_pipeline_id`` / ``current_pipeline_steps`` mirror the pipeline for
    the LLM-streaming reply path (which doesn't go through the emitter -- there
    are no tool calls there)."""

    session_id: str
    chat_history: list[dict] = field(default_factory=list)
    current_pipeline_id: str | None = None
    current_pipeline_steps: list[PipelineStep] = field(default_factory=list)
    # In-flight turns keyed by STREAM (case_id, or _ROOT_STREAM_KEY for the
    # Cases root). Only a re-prompt in the SAME stream replaces (cancels)
    # that stream's turn; turns in other Cases keep running. Their
    # persistence follows the turn-Case pin and their model context is the
    # per-turn captured history list (see _stream_model_reply), so a
    # concurrent turn cannot re-aim either. Known v0.1 limit (display only):
    # the web routes live streaming envelopes to the last-submitted stream,
    # so a still-running turn's late envelopes may paint in the newer
    # stream until envelope case-tagging lands -- the persisted replay is
    # always correct.
    inflight_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    emitter: PipelineEmitter | None = None
    # Per-session turn counter.  Increments on every
    # user-message dispatch (model stream or /invoke directive). When
    # turn_count > MAX_TURNS_PER_SESSION the agent refuses further dispatch
    # and emits a ``session-state(status="max_turns_reached")`` envelope.
    # New WebSocket connection -> new SessionState -> fresh counter at 0.
    turn_count: int = 0
    # ``active_case_id`` is a PROPERTY backed by the module-level
    # ``_SESSION_ACTIVE_CASE`` registry (keyed by ``session_id``), not a
    # per-connection dataclass field, so the Case context is shared across
    # every connection of the session and survives reconnects. See
    # ``case_context_synced_to`` + ``_sync_case_context`` for the
    # per-connection in-memory catch-up (chat_history / emitter seed).
    #
    # Per-connection marker of which Case this connection's in-memory
    # context (chat_history + emitter loaded_layers) was last synced to. A
    # string sentinel (never a valid case id) means "never synced"; ``None``
    # is a legitimate value (no active Case).
    case_context_synced_to: str | None = _CASE_SYNC_NEVER
    # JOB 2 (active-AOI repair): durable cache of the active Case's persisted
    # AOI bbox (``CaseSummary.bbox`` == ``[lon_min, lat_min, lon_max,
    # lat_max]``). Set when the active Case is selected / synced (the same
    # ``session_state.case.bbox`` already read for the layers-present note) and
    # cleared on deselect. ``_turn_case_bbox`` reads THIS instead of the
    # non-existent ``state.active_case`` attribute (the pre-fix read always
    # returned None, so the agent had no active-AOI signal and re-geocoded /
    # re-fetched, starving the sim/fetch reuse short-circuits of an AOI anchor).
    # ``None`` is legitimate (no active Case, or a Case with no recorded bbox).
    case_bbox: Any = None
    # The session's ACTIVE canvas AOI -- structured ``aoi_bbox``
    # ([min_lon, min_lat, max_lon, max_lat], EPSG:4326) set/cleared by
    # ``_set_active_aoi_from_payload``. Read by dispatch-time bbox auto-fill:
    # explicit arg > active AOI > case bbox. ``None`` = no drawn AOI.
    active_aoi_bbox: list[float] | None = None
    # The turn's user-DRAWN geometry (the QGIS dock 'Draw region'
    # rubber-band rectangle) as ``{"geometry_type": "rectangle", "bbox": [...] }``
    # (EPSG:4326). Set/cleared per user-message by ``_set_drawn_geometry_from_payload``
    # and bound into a per-task ContextVar so composer gates read it as a
    # ``basis="user"`` spatial knob (e.g. geoclaw amr_regions). ``None`` = nothing
    # drawn. Distinct from ``active_aoi_bbox`` (the analysis extent): a drawn
    # region is a sub-region knob, not the AOI.
    drawn_geometry: dict | None = None
    # Per-session routing-visibility mode ('auto' | 'ask').
    # Set by the ``session-config`` envelope's ``mode`` field; ``None`` falls
    # back to the TRID3NT_MODE env default (see _session_routing_mode). Governs
    # tool-selection VISIBILITY only -- consent gates are never mode-dependent.
    routing_mode: str | None = None
    # BENCH pre-dispatch block hook: the armed, session-scoped
    # ``BenchBlockConfig`` set only by the bench harness via the
    # ``session-config`` path (``bench_tool_block`` key). ``None`` = normal
    # operation -- the dispatch guard is a single ``is not None`` check with
    # zero overhead when unarmed. When armed, the dispatch site blocks a
    # wrong / block-tier tool pick before the fn runs (see
    # tool_gating.bench_block_decision + _invoke_tool_via_emitter).
    bench_block_config: Any = None
    # Per-turn layer + map-command emission accumulators. Reset at
    # the start of every dispatch (model stream or /invoke tool). The
    # CaseChatMessage write at turn close reads from these so a Case replay
    # can re-bind layers via the same emission sequence.
    current_turn_layer_ids: list[str] = field(default_factory=list)
    current_turn_pipeline_id: str | None = None
    # Per-turn zoom-to accumulator -- persisted into the closing
    # agent row's ``map_command_emissions`` so Case reopen can snap the
    # camera back (web replays the LAST persisted zoom-to).
    current_turn_map_commands: list[dict] = field(default_factory=list)
    # Per-turn narration accumulator. ``_stream_model_reply`` resets it at
    # stream start and appends every ``TextDeltaEvent`` delta (across all
    # loop iterations -- they share one ``message_id`` bubble on the wire).
    # ``_dispatch_model_turn_and_persist`` joins it at turn close and persists
    # the agent's narration as a ``CaseChatMessage(role="agent")`` so a Case
    # reopen replays what the agent actually said.
    current_turn_narration: list[str] = field(default_factory=list)
    # BUG 1 (post-OPEN-14 acceptance rerun): set by the ``except
    # ContextWindowExceededError`` handler in ``_stream_model_reply`` when a
    # turn aborts on a clipped prompt. ``_dispatch_model_turn_and_persist``'s
    # finally reads + clears it and appends the text to whichever partial-
    # narration row it is about to persist, so the reader sees the abort
    # verdict in the SAME chat row as the (unverified) streamed text, not
    # only in a transient error envelope a dead/detached socket may drop.
    current_turn_context_abort_note: str | None = None
    # The Case this TURN is bound to. Pinned by ``_prepare_user_turn`` at
    # dispatch time (after the auto-create-from-root hand-off, before the
    # first write). Every turn-scoped persistence write -- chat rows, tool
    # cards, layer attribution, per-Case .qgs routing, charts -- targets THIS
    # binding via ``_turn_case_id``, never the live ``active_case_id``, which
    # a mid-stream ``case-command(select)`` can re-point mid-turn.
    current_turn_case_id: str | None = None
    # Per-connection authenticated user context.
    #
    # Populated by the connect-handshake (``_perform_auth_handshake``) after
    # the ``auth-token`` envelope verifies (or after the 5-second anonymous
    # fallback timeout). When set, every subsequent envelope for this
    # connection is scoped to ``authenticated_user_id`` -- Case lookups
    # (``Persistence.list_cases_for_user``) filter by it, and Case creation
    # binds it as ``owner_user_id``. ``None`` only between connect and the
    # handshake completion; never ``None`` after handshake.
    authenticated_user_id: str | None = None
    is_anonymous: bool = True
    tier: str = "free"
    auth_handshake_complete: bool = False
    # The web's keepalive (ws.ts) sends an empty ``session-resume`` envelope
    # every 25s on the open socket as a proof-of-life ping -- indistinguishable
    # from a genuine fresh-socket resume by the envelope alone. This flag is
    # the gate: a fresh ``SessionState`` is built per WebSocket connection, so
    # the FIRST ``session-resume`` on THIS connection replays layers, and
    # every later one is a keepalive ping (skip the layer replay; still emit
    # the ``session-state`` pong so the client's pong deadline clears). Reset
    # to False only by a brand-new connection's fresh SessionState.
    did_fresh_resume: bool = False
    # Per-connection latch for the active-Case REBIND decision - distinct
    # from ``did_fresh_resume`` (which gates the LAYER REPLAY). A session
    # mounts two sockets (App.tsx + Chat.tsx), each sending its own 25s
    # keepalive ``session-resume``. This flag flips True after the FIRST
    # resume on THIS connection, so the client-stamp rebind in
    # ``_handle_session_resume`` fires only on a genuine fresh resume, never
    # a keepalive ping. Explicit ``case-command(select)`` / ``user-message``
    # still rebind unconditionally (deliberate user intent). Reset to False
    # only by a brand-new connection's fresh SessionState.
    did_first_resume: bool = False
    # Per-session audit log of payload-warning events. Each entry is a dict
    # carrying ``warning_id``, ``tool_name``, ``estimated_mb``,
    # ``threshold_mb``, ``decision`` (set on confirmation), and the ULID
    # timestamps. Surfaces in tests + post-mortem; persisted to the active
    # Case as part of the chat turn record (best-effort).
    payload_warning_audit_log: list[dict] = field(default_factory=list)
    # Per-session post-hoc allowed-set tracker. The full tool catalog is
    # cached in the provider's ``CachedContent.tools[]`` slot at session
    # start and the ``allowed_function_names`` filter is enforced in our
    # code, not in the provider request. Every emitted ``function_call`` is
    # validated against this set via ``categories.validate_function_call``
    # before dispatch. The set is monotonically growing within a session --
    # it starts at the hot set and widens as the LLM opens categories
    # (``list_tools_in_category``) or successfully dispatches tools.
    allowed_tool_set: AllowedToolSet = field(default_factory=AllowedToolSet)
    # Per-session prompt-cache reference (legacy field name retained for the
    # ``cache-status`` envelope). GCP is decommissioned: the Vertex-only
    # CachedContent fast-path (``gemini_cache.py``) is REMOVED, so this is always
    # ``None``. Bedrock prompt caching is handled by ``bedrock_adapter`` via its
    # own ``cachePoint`` markers and reported through ``UsageMetadataEvent`` --
    # there is no per-session cache name to track here.
    gemini_cache_name: str | None = None
    # Per-session circuit breaker. Tracks consecutive failures per tool;
    # trips after TRID3NT_CIRCUIT_THRESHOLD (default 3) consecutive failures,
    # enforcing a TRID3NT_CIRCUIT_COOLDOWN_S (default 60s) cooldown.
    # ``_stream_model_reply`` checks ``is_tripped`` before every
    # ``_invoke_tool_via_emitter`` dispatch and records success/failure after
    # each attempt. A tripped breaker raises ``CircuitBreakerError``, which
    # ``summarize_tool_result`` surfaces as a structured envelope so the
    # model reads the signal and narrates the outage honestly.
    circuit_breaker: ToolCircuitBreaker = field(default_factory=ToolCircuitBreaker)
    # Per-TURN set of tools that have already surfaced a credential-request
    # this turn. The credential pipeline pauses + prompts + retries ONCE per
    # tool per turn: after the single retry the tool either succeeds (key
    # now in the session cache) or fails through the normal typed-error
    # surface. Without
    # this guard a still-invalid key would re-trip the auth error and
    # re-prompt forever. Reset at the start of every turn.
    credential_prompted_tools: set[str] = field(default_factory=set)
    # fix (bbox-gate-retry-loop, 2026-07-09): per-TURN memory of solver-confirm
    # / fetch-resolution gate ("tool-payload-warning") decisions, keyed by
    # ``_gate_memory_key(tool_name, params)`` (tool name + bbox rounded to
    # ~6 decimals, or the full normalized args when there is no bbox). A
    # model that retries a gated tool with corrected NON-bbox args (e.g.
    # ``fetch_landcover(dataset='nlcd')`` -> typed error -> retried with
    # ``dataset='nlcd_'`` -> typed error -> retried with ``dataset=
    # 'nlcd_2021')``) re-emitted an IDENTICAL confirm gate on the SAME bbox
    # every retry; the user only answered the FIRST one, and local gates
    # have no timeout by design, so the second gate hung the turn forever.
    # Only "proceed" / "narrow_scope" decisions are recorded here (a
    # "cancel" raises before reaching the write site, so a corrected retry
    # still re-gates - the user might reconsider). Reset at the start of
    # every new user-message dispatch (same site as
    # ``credential_prompted_tools`` above), so it never leaks across turns;
    # it lives on the per-session ``SessionState`` so it never leaks across
    # sessions or Cases either. Values are the DELTA the gate applied to the
    # params (e.g. ``{"resolution_m": 300}``), not the whole approved dict,
    # so a later retry keeps its own corrected non-bbox args.
    gate_decisions_this_turn: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    # In-chat model selector: the Bedrock model id chosen by the user for
    # the current turn. Updated on every ``user-message`` that carries a
    # non-None ``model_id``; persists across turns so consecutive messages
    # without one inherit the last-chosen model. ``None`` means "use the
    # server default" (``bedrock_adapter.bedrock_model_id()``). Only
    # consulted when MODEL_PROVIDER=bedrock; ignored on the Vertex path.
    selected_model: str | None = None

    # ------------------------------------------------------------------ #
    # Active-Case context -- session-scoped, NOT per-connection.
    # ------------------------------------------------------------------ #

    @property
    def active_case_id(self) -> str | None:
        """The active Case for this SESSION (shared across its connections).

        ``None`` for fresh sessions (no Case selected yet -- the M1 stateless
        demo path remains supported). Updated by ``case-command(create|select)``
        on ANY connection of the session; cleared on ``delete`` of the active
        Case. When non-None, the tool-call wrapper
        (``_invoke_tool_via_emitter``) carries the case context into tools
        that opt in via ``case_id`` (currently ``publish_layer``); chat +
        layer persistence route every turn into the Case record.
        """
        return _SESSION_ACTIVE_CASE.get(self.session_id)

    @active_case_id.setter
    def active_case_id(self, value: str | None) -> None:
        _set_session_active_case(self.session_id, value)
