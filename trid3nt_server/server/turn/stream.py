"""The streaming model-reply loop: the multi-iteration turn engine driver."""

from __future__ import annotations

import asyncio
import logging
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.ws import AgentMessageChunkPayload, AgentThinkingChunkPayload, PipelineStatePayload, PipelineStep
from trid3nt_server.adapters.adapter import CompactionCompleteEvent, CompactionStartEvent, FunctionCallEvent, MAX_TURN_ITERATIONS, ModelSettings, SYSTEM_PROMPT, TextDeltaEvent, ThinkingDeltaEvent, UpstreamProviderError, UsageMetadataEvent, build_contents_from_history, build_function_call_content, build_function_response_content, build_layers_present_note, build_tool_declarations, build_user_text_content, classify_provider_error_class, classify_result_usable, stream_events_with_contents, summarize_tool_result
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.meta.code_exec_tool.code_exec_tool import is_code_exec_result
from trid3nt_server.tools.processing.charts_common import is_chart_emission_result
from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR
from trid3nt_server.emission.pipeline_emitter import bind_turn_case, bind_turn_drawn_geometry, complete_compaction_card, mint_compaction_card
from trid3nt_server.emission.uri_registry import get_uri_registry
from trid3nt_server.gates.circuit_breaker import CircuitBreakerError
# The gate engine (trid3nt_server.gates.confirm) is imported function-locally in
# _stream_model_reply -- deferred to break the server<->gates load cycle.
from trid3nt_server.gates.context_budget import ContextWindowExceededError, FABRICATION_CAVEAT, build_context_window_abort_note, looks_like_fabricated_action_claim
from trid3nt_server.gates.runaway_guard import ABORT_STEP_CAP, ABORT_WALL_CLOCK, LoopWatchdog, abort_message, max_turn_seconds, step_cap_for_model
from trid3nt_server.gates.tool_gating import BenchBlockedError
from trid3nt_server.server.config import _env_flag, _tool_retrieval_k
from trid3nt_server.server.dispatch.emitter import _invoke_tool_via_emitter
from trid3nt_server.server.dispatch.helpers import _DELIVERABLE_COMPLETE_DIRECTIVE, _DISCOVERY_EXPAND_CAP, _EMPTY_COMPLETION_NUDGE, _EMPTY_COMPLETION_RETRY_CAP, _POST_DELIVERABLE_WRAPUP_ROUNDS, _default_declarable_registry, _dispatch_made_progress, _gate_expander_tool_names, _is_terminal_composer, _tool_names_from_search_result
from trid3nt_server.server.dispatch.persist import _TURN_NARRATION_BY_TASK, _TURN_OPEN_SEGMENT_BY_TASK, _TURN_SEGMENTS_PERSISTED_BY_TASK, _TURN_TERMINAL_ACC_PERSISTED_BY_TASK, _finalize_segment, _persist_chat_turn, _persist_terminal_failure_card
from trid3nt_server.server.dispatch.results import _maybe_emit_chart, _maybe_emit_code_exec_result
from trid3nt_server.server.session.case_state import _turn_case_bbox, _turn_case_id
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _aoi_zoom_to_bbox, _coerce_bbox4
from trid3nt_server.server.turn.cases import _emit_case_list, _maybe_autoname_case
from trid3nt_server.server.turn.engine import _CONTINUATION_NUDGE, _asks_for_data_or_analysis, _geocode_drift_note, _maybe_emit_tool_candidates, _session_routing_mode, _union_pinned_tool
from trid3nt_server.server.turn.wire import _emit_cache_status, _emit_turn_complete, _new_envelope, _send_agent_abort, _send_error, _send_loop_exhausted, _session_safe_send
from trid3nt_server.telemetry import compute_args_hash, emit_shadow_selection_event, emit_tool_call_event, emit_turn_telemetry
from typing import Any
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("trid3nt_server.server")

async def _stream_model_reply(
    websocket: ServerConnection,
    state: SessionState,
    settings: ModelSettings,
    user_text: str,
    model_id: str | None = None,
    show_thinking: bool = False,
) -> None:
    """Stream one user-message reply with multi-turn tool dispatch.

    The canonical agent loop:

        contents = history + user_text
        for _ in range(MAX_TURN_ITERATIONS):
            stream the model:
                text deltas -> forward as agent-message-chunk
                function_calls -> collect (this turn)
            if no function_calls this turn:
                break  # final narrative turn
            for each call:
                result = await _invoke_tool_via_emitter(...)
                summary = summarize_tool_result(name, result, error)
                append model Content (function_call) + function Content (response)
            # then loop: the model now sees the call + result and decides next
            # tool call OR narrates the answer.

    Cancellation: ``asyncio.CancelledError`` aborts the whole loop and emits a
    cancelled ``pipeline-state`` for the outer ``llm_generation`` step.
    """
    from trid3nt_server.gates.confirm import (
        SPATIAL_INPUT_SENTINEL_KEY,
        _handle_request_spatial_input,
        _maybe_handle_region_choice,
    )

    logger.info(
        "user-message session=%s text=%r",
        state.session_id,
        user_text[:80],
    )

    # Auto-name an Untitled Case from its FIRST user message BEFORE dispatch
    # (a failed narration must not skip it). Deterministic heuristic, no LLM
    # call; best-effort and never-raise. The end-of-turn call below stays as
    # a no-op fallback covering a mid-stream case switch.
    try:
        if await _maybe_autoname_case(state, user_text):
            await _emit_case_list(websocket, state, force=True)
    except Exception:  # noqa: BLE001 -- naming is a nicety, never break the turn
        logger.debug(
            "pre-dispatch case auto-name failed session=%s", state.session_id,
            exc_info=True,
        )

    # One bubble per CONTIGUOUS narration run: message_id is minted lazily on
    # the first text of a segment, finalized when the next function-call
    # round dispatches, and a new segment opens after that round. ``None``
    # means no open segment (no leading-text-before-first-tool-call bubble).
    current_message_id: str | None = None
    pipeline_id = new_ulid()
    step_id = new_ulid()
    state.current_pipeline_id = pipeline_id
    # Fresh per-stream narration accumulator: one stream == one message_id
    # bubble == one persisted role="agent" CaseChatMessage at turn close.
    # Captured as LOCALS in the synchronous prefix (before any await) because
    # per-Case turn concurrency re-points SessionState fields mid-stream; this
    # turn must keep appending to ITS OWN lists. Also registered under the
    # running task so the dispatch wrapper's finally joins THIS turn's list
    # (never the live field), even on crash/cancel.
    state.current_turn_narration = []
    # BUG 1: reset the per-turn context-window-abort note. A new turn is a
    # fresh request -- any prior turn's abort note must never leak forward.
    state.current_turn_context_abort_note = None
    # job VAULT-READ: reset the per-turn credential-prompt guard. A new user
    # turn is a fresh request -- a tool that prompted for a key last turn may
    # legitimately prompt again this turn (the key may still be missing).
    state.credential_prompted_tools = set()
    # Reset the per-turn solver-confirm/fetch-resolution gate-decision memory.
    # A new user turn is a fresh request - a tool+bbox pair confirmed last turn
    # must gate again this turn (see ``gate_decisions_this_turn`` docstring above).
    state.gate_decisions_this_turn = {}
    turn_narration = state.current_turn_narration
    turn_history = state.chat_history
    # Per-segment buffer for the CURRENTLY OPEN bubble only; reset via
    # .clear() at each boundary (same list object stays registered).
    # Captured + registered in the synchronous prefix so a crash/cancel
    # mid-segment lets the wrapper's finally persist the tail.
    _segment_buf: list[str] = []
    # Per-segment reasoning-text buffer, filled only when the per-turn
    # ``show_thinking`` toggle is ON. ``_finalize_segment`` persists it as the
    # ``thinking`` field on the SAME agent row as the segment's answer
    # (same-bubble contract; QGIS plugin reads this field) and clears it. A
    # thinking-only segment keeps its buffer so it attaches to the turn's NEXT
    # persisted row; thinking with no text anywhere persists no phantom
    # bubble. NEVER-REHYDRATE: this is display replay material only --
    # build_contents_from_history strips it BY RULE.
    _thinking_buf: list[str] = []
    _reg_task = asyncio.current_task()
    if _reg_task is not None:
        _TURN_NARRATION_BY_TASK[_reg_task] = turn_narration
        _TURN_OPEN_SEGMENT_BY_TASK[_reg_task] = _segment_buf
        _TURN_SEGMENTS_PERSISTED_BY_TASK[_reg_task] = 0
        # False until the terminal finalize actually
        # snapshots the accumulator onto a persisted row (see registry doc).
        _TURN_TERMINAL_ACC_PERSISTED_BY_TASK[_reg_task] = False

    # Emit a one-step "thinking" pipeline snapshot so the client has a
    # cancellable handle. The loop driver keeps this single outer step; each
    # dispatched tool gets its own step through the emitter.
    thinking_step = PipelineStep(
        step_id=step_id,
        name="llm_generation",
        tool_name="model_generate",
        state="running",
    )
    state.current_pipeline_steps = [thinking_step]
    await _session_safe_send(websocket, state.session_id,
        _new_envelope(
            "pipeline-state",
            state.session_id,
            PipelineStatePayload(pipeline_id=pipeline_id, steps=[thinking_step]),
        )
    )

    # No model client is constructed here -- every live provider adapter
    # (bedrock / openai / scripted) opens its own client at the boundary and
    # ignores ``client``. Provider resolved once here and reused by the cache
    # guard below.
    from trid3nt_server.adapters.bedrock_adapter import model_provider as _model_provider

    _provider = _model_provider()
    # #225 per-model telemetry: resolve the EFFECTIVE model that actually
    # serves this turn (not the possibly-None explicit selection) so telemetry
    # rows for DEFAULT-model turns are tagged with the real model instead of
    # collapsing into the "unknown" bucket in the by_model accuracy slice. On
    # the openai/OpenRouter path this applies openai_model's own precedence
    # (selection -> TRID3NT_OPENAI_MODEL); on bedrock, the selection or the
    # configured default. Best-effort -- a resolution error must never break
    # the turn, so fall back to the raw selection.
    try:
        if _provider == "openai":
            from trid3nt_server.adapters import openai_adapter as _oa  # noqa: WPS433
            _effective_model = _oa.openai_model(model_id)
        elif _provider == "bedrock":
            from trid3nt_server.adapters.bedrock_adapter import bedrock_model_id as _bmid  # noqa: WPS433
            _effective_model = model_id or _bmid()
        else:
            _effective_model = model_id
    except Exception:  # noqa: BLE001 -- telemetry tag only, never fatal
        _effective_model = model_id
    # No model client is built here -- the provider adapters ignore ``client``.
    client = None
    first_token_logged = False
    started_at = asyncio.get_running_loop().time()

    # Build tool declarations + system prompt for this request.
    #
    # Tool-retrieval is the BUILT-IN surfacing path (unconditional enforce): each
    # turn we compute the visible set via retrieve_visible_tools, UNION it into
    # the Case's monotonic visible set so a once-visible tool never leaves within
    # a Case, and subset TOOL_REGISTRY to the result before build_tool_declarations.
    # Any retrieval error / empty result FAILS OPEN to the full registry, logged.
    # The cachePoint TAIL is inserted downstream by bedrock_adapter (after tools),
    # so subsetting the dict here preserves it. K is the only lever
    # (TRID3NT_TOOL_RETRIEVAL_K).
    #
    # DEFAULT declarable set: the full registry MINUS tier=catalog/internal
    # (never the raw TOOL_REGISTRY). Engine
    # templates are ordinary members of this default set now -- callable
    # directly, no concierge. See _default_declarable_registry.
    _retrieval_registry = _default_declarable_registry()
    try:
        from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

        _retrieval_k = _tool_retrieval_k()
        _visible = retrieve_visible_tools(
            user_text, state.visible_tools, _retrieval_k
        )
        if not _visible:
            # FAIL-OPEN: an empty result must never trim the catalog.
            raise ValueError("retrieve_visible_tools returned empty")
        # Selection telemetry (fire-and-forget, never-raise) -- feeds the
        # recall@k dashboard.
        try:
            emit_shadow_selection_event(
                session_id=state.session_id,
                turn_id=pipeline_id,
                user_text=user_text,
                visible_tools=_visible,
                mode="enforce",
                k=_retrieval_k,
                full_registry_size=len(TOOL_REGISTRY),
                model_id=_effective_model,
            )
        except Exception:  # noqa: BLE001 -- telemetry must never break dispatch
            logger.warning(
                "tool-retrieval: selection emit failed", exc_info=True
            )
        # UNION the visible set into the Case's monotonic visible set FIRST
        # (so it never shrinks across turns), then subset the registry to the
        # CORE_FLOOR + accrued snapshot intersected with the registry (real,
        # registered tools only).
        try:
            state.visible_tools |= set(_visible)
            _allowed_snapshot = set(CORE_FLOOR) | set(state.visible_tools)
        except Exception:  # noqa: BLE001 -- never shrink on a snapshot fault
            logger.warning(
                "tool-retrieval: visible-set union failed; "
                "FAIL-OPEN to full registry",
                exc_info=True,
            )
            _allowed_snapshot = set(TOOL_REGISTRY)
        _subset = _allowed_snapshot & set(TOOL_REGISTRY)
        if _subset:
            _retrieval_registry = {
                name: entry
                for name, entry in TOOL_REGISTRY.items()
                if name in _subset
            }
            logger.info(
                "tool-retrieval enforce: %d/%d tools visible "
                "(turn=%s session=%s)",
                len(_retrieval_registry),
                len(TOOL_REGISTRY),
                pipeline_id,
                state.session_id,
            )
        else:
            logger.warning(
                "tool-retrieval enforce: empty subset; "
                "FAIL-OPEN to full registry"
            )
    except Exception:  # noqa: BLE001 -- any fault FAILS OPEN to the full catalog
        logger.warning(
            "tool-retrieval: selection failed; FAIL-OPEN to full registry",
            exc_info=True,
        )
        # FAIL-OPEN to the tier-filtered default (NOT raw TOOL_REGISTRY):
        # drops only tier=catalog/internal. Engine templates are ordinary
        # members here. See _default_declarable_registry.
        _retrieval_registry = _default_declarable_registry()

    # TOP-K TOOL GATING: the openai adapter path was sending ALL ~190 tool
    # schemas every round. Gate the per-turn tool list to the retrieval
    # top-k (TRID3NT_TOOL_GATING_TOPK, default 24; 0 disables) PLUS the
    # always-include floors -- the META set (hot set + catalog pair +
    # web_fetch), every tool already used this case-session, and any tool
    # the user NAMED in the message. Scoped to MODEL_PROVIDER=openai:
    # bedrock/scripted/vertex tool lists are byte-unchanged. FAIL-OPEN on a
    # cold index / empty ranking / any fault.
    if _provider == "openai":
        try:
            from trid3nt_server.gates.tool_gating import (
                WIDEN_K,
                gate_tool_registry,
                gating_topk,
                gating_widen_threshold,
                should_widen_for_poor_fit,
            )
            from trid3nt_server.tools.search.tool_retrieval import retrieve_ranked_tools

            _gate_k = gating_topk()
            if _gate_k > 0:
                _gate_ranked = retrieve_ranked_tools(user_text, k=_gate_k)
                # POOR-FIT WIDENING: a LOW top-1 retrieval score means the ranking is
                # uncertain for this ask -- widen the gate k once (24 -> WIDEN_K) so
                # recall does not silently drop on a vague/ambiguous turn. Fires at most
                # once per turn, only when the widened k exceeds the current k.
                # Threshold is env-tunable (TRID3NT_GATING_WIDEN_THRESHOLD).
                _widen_threshold = gating_widen_threshold()
                if (
                    WIDEN_K > _gate_k
                    and should_widen_for_poor_fit(_gate_ranked, _widen_threshold)
                ):
                    _top_score = _gate_ranked[0][1] if _gate_ranked else None
                    _gate_k = WIDEN_K
                    _gate_ranked = retrieve_ranked_tools(user_text, k=_gate_k)
                    logger.info(
                        "tool-gating: POOR-FIT widen k->%d (top_score=%.5f < "
                        "threshold=%.5f) turn=%s session=%s",
                        _gate_k,
                        _top_score if _top_score is not None else -1.0,
                        _widen_threshold,
                        pipeline_id,
                        state.session_id,
                    )
                _used_tools = set(state.visible_tools)
                _gated = gate_tool_registry(
                    user_text,
                    _retrieval_registry,
                    _gate_ranked,
                    _gate_k,
                    used_tools=_used_tools,
                )
                if _gated is not None:
                    logger.info(
                        "tool-gating: %d/%d tools visible (topk=%d used=%d "
                        "turn=%s session=%s)",
                        len(_gated),
                        len(_retrieval_registry),
                        _gate_k,
                        len(_used_tools),
                        pipeline_id,
                        state.session_id,
                    )
                    _retrieval_registry = _gated
                else:
                    logger.info(
                        "tool-gating: no-op (%d tools already visible via the "
                        "retrieval-enforce layer; topk=%d ranked=%d) turn=%s",
                        len(_retrieval_registry),
                        _gate_k,
                        len(_gate_ranked),
                        pipeline_id,
                    )
        except Exception:  # noqa: BLE001 -- gating faults FAIL OPEN (all tools)
            logger.warning(
                "tool-gating: fault; FAIL-OPEN to ungated registry",
                exc_info=True,
            )

    # Auto/ask tool-candidates gate. May PAUSE here (bounded --
    # see _tool_choice_timeout_s) awaiting the user's tool-choice. A pinned
    # tool is unioned into the visible registry + allowed set BEFORE
    # declarations are built so the model can actually call it. Any fault
    # proceeds autonomously.
    _pin_notes: list[str] = []
    try:
        _pinned_tool, _pin_notes = await _maybe_emit_tool_candidates(
            websocket, state, user_text
        )
        _retrieval_registry = _union_pinned_tool(
            _pinned_tool, _retrieval_registry, state
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- the picker is an optimization
        logger.warning(
            "tool-candidates gate failed; proceeding autonomously",
            exc_info=True,
        )
    tool_decls = build_tool_declarations(_retrieval_registry)

    # Prompt caching is the adapter's own concern (Bedrock uses ``cachePoint``
    # markers); there is no separate cached-content fast-path, so this is always
    # ``None``. The field is retained for the ``cache-status`` envelope payload
    # (``_emit_cache_status``) which reports cache-hit metrics.
    state.model_cache_ref = None

    # Seed the multi-turn contents list with chat history + this user_text.
    # The entry-captured list -- a mid-stream case switch rebinds
    # state.chat_history, never mutates this one.
    #
    # Inject the already-loaded-layers + reuse-AOI note on EVERY live turn
    # (lists each layer already on the map plus the Case AOI bbox with a
    # REUSE instruction) so a long live turn does not re-geocode or re-fetch
    # layers already present. Appended as the LAST history turn before the
    # user message; ``None`` (no layers, no bbox) is a no-op. Built from the
    # LIVE emitter + cached Case AOI so it reflects this turn's current truth.
    turn_history_for_contents = turn_history
    try:
        loaded_layers = (
            [layer.model_dump(mode="json") for layer in state.emitter.loaded_layers]
            if state.emitter is not None
            else []
        )
        case_state_note = build_layers_present_note(
            loaded_layers, case_bbox=_turn_case_bbox(state)
        )
        if case_state_note:
            turn_history_for_contents = list(turn_history) + [
                {"role": "user", "text": case_state_note}
            ]
    except Exception:  # noqa: BLE001 -- the note is an optimization, never fatal
        logger.debug("per-turn case-state note build failed", exc_info=True)
    contents = build_contents_from_history(user_text, turn_history_for_contents)

    # Feed the tool-candidates outcome into the model
    # context -- the pin directive ("Use the tool 'X'"), the user's free-text
    # clarification, or the timeout proceed-autonomously note. Appended AFTER
    # the user message so the model reads the ask, then the user's routing
    # decision. No-op when the gate never fired (the common path).
    for _pin_note in _pin_notes:
        contents.append(build_user_text_content(_pin_note))

    # Per-turn usage metadata harvested from the stream.
    last_usage: UsageMetadataEvent | None = None

    # RUNAWAY-AGENT GUARD: three independent per-turn bounds route to a
    # single clean ABORT that terminates the turn (releasing busy) instead of
    # letting the model<->tool loop run away and wedge the shared box:
    #   1. STEP CAP -- min of the historical MAX_TURN_ITERATIONS and the
    #      model-tier step cap (cheap/Nova/Haiku tiers get HALF).
    #   2. WALL-CLOCK -- a per-turn deadline aborts a slow turn even under
    #      the step cap.
    #   3. LOOP WATCHDOG -- aborts when the SAME tool+args (or identical
    #      round signature) repeats N rounds in a row with no progress.
    # ``_agent_abort`` is set to (reason_code, message) the moment a guard
    # fires; the loop breaks and the post-loop block surfaces the honest
    # typed envelope (honesty floor) like the loop_exhausted fail-stop.
    _step_cap = min(MAX_TURN_ITERATIONS, step_cap_for_model(model_id))
    _turn_deadline = started_at + max_turn_seconds()
    _watchdog = LoopWatchdog()
    _agent_abort: tuple[str, str] | None = None

    # CRISP-END-AFTER-DELIVERABLE: once a terminal composer (run_model_* &
    # friends) has produced its artifact, the model should narrate a short
    # summary and STOP rather than spin to the loop_exhausted cap.
    # _deliverable_done latches on first delivery; _post_deliverable_idle
    # counts consecutive no-progress rounds and resets on a producing round
    # (multi-deliverable flows are never cut off). See
    # _POST_DELIVERABLE_WRAPUP_ROUNDS.
    _deliverable_done = False
    _post_deliverable_idle = 0
    _crisp_concluded = False

    # OPEN-14 FABRICATION BACKSTOP: tracks whether ANY round of this turn
    # dispatched a tool call. A turn that ends with this still False AND
    # whose closing narration claims a completed geospatial action gets an
    # honest caveat appended -- see the ``not turn_function_calls`` block
    # below. Never set True by a round that merely REQUESTED calls that
    # later failed validation -- turn_function_calls is the model's raw
    # request (a model that TRIED to act is not fabricating; one that never
    # tried and claims success is).
    _turn_ever_called_tool = False

    # OPEN-16 EMPTY-COMPLETION RETRY: per-turn counter of empty-round retries
    # already spent, capped at ``_EMPTY_COMPLETION_RETRY_CAP``. Past the cap the
    # empty round falls through to the existing terminal break (never an infinite
    # loop). Local-path only (guarded on ``_provider == "openai"`` below).
    _empty_retries = 0

    # Turn-loop invariants + guard (d) tracker:
    #   _turn_tools_dispatched -- every tool NAME this turn requested (the
    #       bare-geocode backstop reads it: a turn whose ONLY tool was
    #       geocode_location while the user asked for data gets one nudge).
    #   _continuation_nudged  -- the ONE-per-turn continuation-nudge budget
    #       shared by both invariants (never more than one nudge per turn).
    #   _turn_geocode_bbox    -- the last successful geocode_location bbox
    #       this turn; guard (d) appends an advisory drift WARNING to any
    #       later call whose bbox intersects neither this nor the active AOI.
    _turn_tools_dispatched: set[str] = set()
    _continuation_nudged = False
    _turn_geocode_bbox: list[float] | None = None

    # BENCH pre-dispatch block hook: latched True by the dispatch except-path
    # when a WRONG-pick block fired this round, so the turn ends after the
    # round's function-responses are on the wire (see the check after the
    # per-call loop). Unarmed sessions never touch it.
    _bench_wrong_pick_end = False

    # DISCOVERY-EXPANDS-GATE (task 2): tool names the tool-search tool
    # (search_tools) returned THIS turn that were unioned into the visible gate
    # for subsequent rounds, capped at ``_DISCOVERY_EXPAND_CAP`` per turn.
    # ``_tool_decls_dirty`` requests a one-time rebuild of ``tool_decls`` after
    # the round so the next model round sees the widened set.
    _discovery_expanded: set[str] = set()
    _tool_decls_dirty = False

    # PER-TURN TELEMETRY accumulators: token counts SUM the adapter's
    # per-round UsageMetadataEvents across the whole turn; a provider that
    # reports no usage leaves them None (tolerated, never fabricated).
    # _turn_error_class is stamped by the exception handlers below and
    # stays None on a clean turn. The record is emitted in the finally at
    # the end of this function -- one record per turn, every outcome.
    _turn_prompt_tokens: int | None = None
    _turn_completion_tokens: int | None = None
    _turn_reasoning_tokens: int | None = None
    _turn_tool_dispatch_count = 0
    _turn_error_class: str | None = None

    iterations = 0
    try:
        while iterations < _step_cap:
            # GUARD 2 (wall-clock): abort BEFORE the next (potentially long)
            # model round if this turn has already overrun its budget. Checked
            # at the top of every iteration so a turn whose rounds are each slow
            # cannot exceed the wall-clock bound by more than one round.
            if asyncio.get_running_loop().time() >= _turn_deadline:
                _agent_abort = (ABORT_WALL_CLOCK, abort_message(ABORT_WALL_CLOCK))
                break
            iterations += 1
            # Per-turn collectors: text emitted, function-calls the model requested.
            turn_text_parts: list[str] = []
            turn_function_calls: list[FunctionCallEvent] = []
            last_usage = None
            # Compaction UX: the step_id of the currently-open compaction card, if
            # any -- set on CompactionStartEvent, read + cleared on the matching
            # CompactionCompleteEvent. Local to this round: the adapter always
            # pairs the two 1:1 within one stream_events_with_contents call, and a
            # round can legitimately mint+complete more than one (proactive, then a
            # later reactive retry).
            _compaction_step_id: str | None = None

            # Wave semantics: in ASK mode, surface a pre-dispatch
            # tool-candidates WAVE before EACH subsequent round (round 1 is covered
            # by the pre-loop emission above). Excluding this turn's
            # already-dispatched tools advances the stage label so a multi-step
            # turn reads as a SEQUENCE of stage-labeled picks, not one blob. AUTO
            # mode is unchanged. Fail-open: the wave is an optimization, never a
            # wall.
            if iterations > 1 and _session_routing_mode(state) == "ask":
                try:
                    _w_pinned, _w_notes = await _maybe_emit_tool_candidates(
                        websocket,
                        state,
                        user_text,
                        exclude_tools=_turn_tools_dispatched,
                    )
                    _w_reg = _union_pinned_tool(
                        _w_pinned, _retrieval_registry, state
                    )
                    if _w_reg is not _retrieval_registry:
                        _retrieval_registry = _w_reg
                        tool_decls = build_tool_declarations(_retrieval_registry)
                    for _w_note in _w_notes:
                        contents.append(build_user_text_content(_w_note))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- wave is best-effort
                    logger.warning(
                        "per-round tool-candidates wave failed; proceeding",
                        exc_info=True,
                    )

            async for event in stream_events_with_contents(
                client,
                settings.model,
                contents,
                tool_declarations=tool_decls,
                system_prompt=SYSTEM_PROMPT,
                model_cache_ref=state.model_cache_ref,
                model_id=model_id,
                show_thinking=show_thinking,
            ):
                if not first_token_logged:
                    first_token_logged = True
                    elapsed_ms = (asyncio.get_running_loop().time() - started_at) * 1000.0
                    logger.info(
                        "first-token session=%s elapsed_ms=%.1f model=%s",
                        state.session_id,
                        elapsed_ms,
                        settings.model,
                    )

                if isinstance(event, TextDeltaEvent):
                    # Open a NEW bubble on the first text of a segment.
                    if current_message_id is None:
                        current_message_id = new_ulid()
                    chunk = AgentMessageChunkPayload(
                        message_id=current_message_id, delta=event.delta, done=False
                    )
                    await _session_safe_send(websocket, state.session_id,
                        _new_envelope("agent-message-chunk", state.session_id, chunk)
                    )
                    turn_text_parts.append(event.delta)
                    # Accumulate across ALL iterations -- the turn
                    # close persists the full narration for Case replay.
                    # Entry-captured list, never the live field.
                    turn_narration.append(event.delta)
                    # Also feed the OPEN-segment buffer so the
                    # boundary finalize (A3 / A4) persists exactly this run's
                    # text, and a crash leaves the un-finalized tail for the
                    # wrapper. Same registered list object -- never rebound.
                    _segment_buf.append(event.delta)

                elif isinstance(event, ThinkingDeltaEvent):
                    # Forward the model's reasoning-channel deltas so the web/QGIS
                    # clients render the greyed foldable thinking block. Gated on the
                    # per-turn user toggle -- with it off the /no_think suppressor is armed
                    # and the channel is not generated, but a model that leaks reasoning
                    # anyway must not reach a client that asked for it to stay hidden.
                    # Shares the segment's message_id (the thinking block and its answer
                    # live in the SAME bubble). The deltas also accumulate in
                    # _thinking_buf so _finalize_segment persists them as the ``thinking``
                    # field on the SAME agent row as the answer; a thinking-only segment
                    # still persists no row of its own (no phantom bubble) -- its buffered
                    # thinking rides the turn's next persisted row.
                    if show_thinking:
                        if current_message_id is None:
                            current_message_id = new_ulid()
                        await _session_safe_send(websocket, state.session_id,
                            _new_envelope(
                                "agent-thinking-chunk",
                                state.session_id,
                                AgentThinkingChunkPayload(
                                    message_id=current_message_id,
                                    delta=event.delta,
                                    done=False,
                                ),
                            )
                        )
                        # Thinking persistence: accumulate the reasoning text
                        # for THIS segment so
                        # ``_finalize_segment`` persists it as the ``thinking``
                        # field on the same agent row as the answer.
                        _thinking_buf.append(event.delta)

                elif isinstance(event, FunctionCallEvent):
                    logger.info(
                        "model function-call session=%s iter=%d tool=%s call_id=%s args=%r",
                        state.session_id,
                        iterations,
                        event.name,
                        event.call_id,
                        event.args,
                    )
                    turn_function_calls.append(event)

                elif isinstance(event, UsageMetadataEvent):
                    # The model surfaces aggregate usage on the terminal chunk. Cache the event
                    # so the post-turn block can pipe cached_content_token_count into
                    # per-tool telemetry and emit a single cache-status envelope for the
                    # live cache hit-rate UI.
                    last_usage = event
                    # PER-TURN TELEMETRY: sum the
                    # reported counts across the turn's model rounds. A round
                    # that reports None for a figure leaves that accumulator
                    # untouched (null stays null when NO round reports it --
                    # tolerate absent, never fabricate).
                    if event.prompt_token_count is not None:
                        _turn_prompt_tokens = (
                            (_turn_prompt_tokens or 0) + event.prompt_token_count
                        )
                    if event.candidates_token_count is not None:
                        _turn_completion_tokens = (
                            (_turn_completion_tokens or 0)
                            + event.candidates_token_count
                        )
                    if event.reasoning_token_count is not None:
                        _turn_reasoning_tokens = (
                            (_turn_reasoning_tokens or 0)
                            + event.reasoning_token_count
                        )
                    logger.info(
                        "model usage session=%s iter=%d cached=%s total=%s "
                        "prompt=%s candidates=%s hit=%s",
                        state.session_id,
                        iterations,
                        event.cached_content_token_count,
                        event.total_token_count,
                        event.prompt_token_count,
                        event.candidates_token_count,
                        event.cache_hit,
                    )

                elif isinstance(event, CompactionStartEvent):
                    # Compaction UX: mint the durable running "Compacting conversation..."
                    # card the instant the adapter announces compaction is about to run
                    # (proactive or the reactive clip-guard retry). Mirrors the two-card SIM
                    # observability's running-card mint; best-effort so a mint failure can
                    # never block the turn.
                    _compaction_step_id = await mint_compaction_card(
                        emitter=state.emitter
                    )

                elif isinstance(event, CompactionCompleteEvent):
                    # Compaction UX (Part A): flip the card minted above to
                    # its terminal "Conversation compacted (Nk -> Mk tokens)"
                    # state. No-op (best-effort) if the mint above failed or
                    # never fired (emitter unbound) -- see
                    # complete_compaction_card's own None-guard.
                    await complete_compaction_card(
                        emitter=state.emitter,
                        step_id=_compaction_step_id,
                        before_tokens=event.before_tokens,
                        after_tokens=event.after_tokens,
                    )
                    _compaction_step_id = None

            # Emit a cache-status envelope so the UI can render the cache
            # hit-rate live. Best-effort -- a serialization failure logs but
            # does not break the turn (the envelope is observability, not
            # part of the agent loop's correctness contract).
            if last_usage is not None:
                await _emit_cache_status(websocket, state, last_usage)

            # Turn ended.  If the model emitted no function_calls this turn, it
            # is finished -- either narrated the answer or had nothing more to
            # do.  Break out of the loop.
            if not turn_function_calls:
                # OPEN-16 EMPTY-COMPLETION RETRY: a round with ZERO tool calls and ZERO
                # non-whitespace text is the qwen3 empty-completion shape -- retry with a
                # corrective user nudge, bounded by _EMPTY_COMPLETION_RETRY_CAP, rather
                # than silently dropping the request. Runs BEFORE the OPEN-14 fabrication
                # backstop (disjoint: an empty round has no closing text to fabricate
                # from). Scoped to the LOCAL (MODEL_PROVIDER=openai) path only -- Bedrock's
                # production narration (a legitimately empty round) stays byte-unchanged.
                # The empty round still counts toward _step_cap and never trips the loop
                # watchdog, so a retry cannot escape the step cap or the runaway guard.
                _empty_round = not "".join(turn_text_parts).strip()
                if (
                    _provider == "openai"
                    and _empty_round
                    and _empty_retries < _EMPTY_COMPLETION_RETRY_CAP
                ):
                    _empty_retries += 1
                    logger.warning(
                        "empty-completion retry %d/%d session=%s iter=%d",
                        _empty_retries,
                        _EMPTY_COMPLETION_RETRY_CAP,
                        state.session_id,
                        iterations,
                    )
                    # Corrective user-role nudge, built with the same plain-text
                    # Content idiom the initial user message uses (adapter.
                    # build_user_text_content) -- no hand-rolled google.genai
                    # types here. Appended so the retried round sees "your last
                    # turn was empty, act or answer".
                    contents.append(build_user_text_content(_EMPTY_COMPLETION_NUDGE))
                    # Observability is log-only (above): a retry must not inject
                    # a transient note into the persisted narration segment, and
                    # inventing a new envelope type is out of scope -- the
                    # log.warning is the durable retry witness.
                    continue
                # Stage 3 TURN-LOOP INVARIANTS: ONE continuation nudge per turn, shared
                # budget, injected as user-role content with the round retried:
                #   (a) NO-SILENT-END -- terminating with tool results but ZERO
                #       assistant text since the last tool round. Skipped when OPEN-16
                #       already nudged this turn (_empty_retries > 0).
                #   (b) BARE-GEOCODE BACKSTOP -- the turn's ONLY tool was
                #       geocode_location while the user asked for data or analysis.
                # Kill-switch: TRID3NT_TURN_INVARIANTS=0. The nudge round still counts
                # toward the step cap; the shared budget guarantees at most one nudge per
                # turn (unified with OPEN-16 -- no stacking). Every terminal round logs
                # one INFO line per invariant: FIRED, or SKIPPED with its reason.
                if (
                    not _continuation_nudged
                    and _empty_retries == 0
                    and _env_flag("TRID3NT_TURN_INVARIANTS", True)
                ):
                    _nudge_reason: str | None = None
                    if (
                        _turn_ever_called_tool
                        and not "".join(turn_text_parts).strip()
                    ):
                        _nudge_reason = "no-silent-end"
                    elif _turn_tools_dispatched == {
                        "geocode_location"
                    } and _asks_for_data_or_analysis(user_text):
                        _nudge_reason = "bare-geocode"
                    if _nudge_reason is not None:
                        _continuation_nudged = True
                        logger.info(
                            "turn-invariant nudge (%s) session=%s iter=%d",
                            _nudge_reason,
                            state.session_id,
                            iterations,
                        )
                        contents.append(
                            build_user_text_content(_CONTINUATION_NUDGE)
                        )
                        continue
                    # Neither invariant fired -- log each skip with its reason.
                    logger.info(
                        "turn-invariant no-silent-end skipped session=%s "
                        "iter=%d reason=%s",
                        state.session_id,
                        iterations,
                        (
                            "no-tools-dispatched"
                            if not _turn_ever_called_tool
                            else "has-closing-text"
                        ),
                    )
                    logger.info(
                        "turn-invariant bare-geocode skipped session=%s "
                        "iter=%d reason=%s tools=%s",
                        state.session_id,
                        iterations,
                        (
                            "tools-not-geocode-only"
                            if _turn_tools_dispatched != {"geocode_location"}
                            else "not-a-data-or-analysis-ask"
                        ),
                        sorted(_turn_tools_dispatched),
                    )
                elif _env_flag("TRID3NT_TURN_INVARIANTS", True):
                    logger.info(
                        "turn-invariants skipped session=%s iter=%d reason=%s",
                        state.session_id,
                        iterations,
                        (
                            "nudge-budget-spent"
                            if _continuation_nudged
                            else "empty-completion-retry-owned-this-turn"
                        ),
                    )
                else:
                    logger.info(
                        "turn-invariants skipped session=%s iter=%d "
                        "reason=disabled-by-env",
                        state.session_id,
                        iterations,
                    )
                logger.info(
                    "model loop terminal session=%s iter=%d text_chunks=%d",
                    state.session_id,
                    iterations,
                    len(turn_text_parts),
                )
                # OPEN-14 FABRICATION BACKSTOP: fires only on the FIRST and ONLY
                # tool-call-free round this turn ever had. Conservative: only fires when
                # the closing text pairs a completed-action verb with a geospatial-output
                # noun in the same sentence -- see
                # context_budget.looks_like_fabricated_action_claim. Ordinary Q&A
                # answers, and any turn that dispatched even one tool call, never
                # trigger this. Scoped to the LOCAL (MODEL_PROVIDER=openai) path only --
                # a local-model-path guard that must not vary Bedrock's production
                # narration.
                if _provider == "openai" and not _turn_ever_called_tool:
                    _closing_text = "".join(turn_text_parts)
                    if looks_like_fabricated_action_claim(_closing_text):
                        logger.warning(
                            "context-budget: fabrication backstop fired "
                            "session=%s iter=%d (zero tool calls this turn)",
                            state.session_id,
                            iterations,
                        )
                        if current_message_id is None:
                            current_message_id = new_ulid()
                        _caveat = f"\n\n{FABRICATION_CAVEAT}"
                        await _session_safe_send(websocket, state.session_id,
                            _new_envelope(
                                "agent-message-chunk",
                                state.session_id,
                                AgentMessageChunkPayload(
                                    message_id=current_message_id, delta=_caveat, done=False
                                ),
                            )
                        )
                        turn_narration.append(_caveat)
                        _segment_buf.append(_caveat)
                break
            _turn_ever_called_tool = True
            # Stage 3 invariants: record this round's requested tool names
            # (the bare-geocode backstop compares the turn's full set).
            _turn_tools_dispatched.update(c.name for c in turn_function_calls)

            # GUARD 3 (loop watchdog): compute THIS round's (tool, args_hash)
            # signature now, but feed it to the watchdog AFTER dispatch together
            # with a PROGRESS witness. A no-progress runaway -- the SAME tool+args
            # (or identical round signature) N rounds in a row that keeps returning
            # nothing new -- trips the watchdog and aborts. A round that PRODUCES a
            # layer/artifact, or one the circuit breaker owns (all calls failed /
            # short-circuited), is NOT counted: a producing loop runs to the
            # step-cap / loop-exhausted envelope, and the breaker handles the
            # failing tool. Recording after dispatch (vs before) costs at most ONE
            # extra identical round before the trip, still far under the step cap.
            _round_sig = [
                (c.name, compute_args_hash(c.args)) for c in turn_function_calls
            ]
            # Per-round progress witness, OR'd across the round's calls. Seeded
            # True only if EVERY call ends up failing / short-circuited (the
            # breaker's territory) -- tracked as no calls-succeeded-without-output
            # below. Starts False; set True by a producing dispatch.
            _round_made_progress = False
            _round_had_failure = False
            _round_had_success = False

            # A function-call round is about to dispatch -- close the current
            # narration bubble (if any text was emitted) BEFORE the tool cards for
            # this round land on the wire, so the next run of text opens a fresh
            # bubble that interleaves AFTER them. Fires ONCE per round, before ALL
            # calls dispatch, so multiple function calls in one round close exactly
            # one prior bubble. _finalize_segment sends done=True AND persists this
            # segment's own role="agent" row.
            if current_message_id is not None:
                await _finalize_segment(
                    websocket, state, current_message_id, _segment_buf,
                    thinking_parts=_thinking_buf,
                )
                current_message_id = None  # next text opens a fresh segment

            # Otherwise: dispatch each call, then append the call + summarized
            # response back into contents so the next model turn sees them.
            for call in turn_function_calls:
                # Dispatch through the registry + emitter (Invariant 2 -- the LLM's
                # tool choice IS the classification). Routing failures (TOOL_NOT_FOUND,
                # PAYLOAD_WARNING_CANCELLED) raise typed exceptions so the except-block
                # below routes them through summarize_tool_result(error=...) -- a
                # structured {status: "error", error_code: str, retryable: bool}
                # envelope the model can distinguish from "tool ran and returned nothing"
                # (a typed error).
                dispatch_error: BaseException | None = None
                result: Any = None
                # CRISP-END: set True iff THIS call is a
                # top-level run-a-model composer that produced its deliverable.
                _call_is_terminal_deliverable = False
                _tool_start = asyncio.get_running_loop().time()
                try:
                    # Per-session circuit breaker: short-circuit before dispatch
                    # if the tool has failed repeatedly this session. Raises
                    # CircuitBreakerError, routed by the except-block below through
                    # summarize_tool_result(error=...) so the model reads the
                    # structured cooldown signal (not retryable).
                    if state.circuit_breaker.is_tripped(call.name):
                        remaining = state.circuit_breaker.cooldown_remaining_s(call.name)
                        raise CircuitBreakerError(call.name, remaining)
                    # A hallucinated (non-registered) name is caught at dispatch:
                    # _invoke_tool_via_emitter raises ToolNotFoundError, routed
                    # through summarize_tool_result(error=...) as the structured
                    # envelope the model reads to retry.
                    result = await _invoke_tool_via_emitter(
                        websocket, state, call.name, call.args
                    )
                    # request_spatial_input PAUSES the turn awaiting a
                    # user-drawn FeatureCollection. The catalog tool returns the
                    # SPATIAL_INPUT_SENTINEL_KEY sentinel (it has no websocket access);
                    # here -- where the live socket + the session future registry ARE
                    # reachable -- we emit the spatial-input-request, await the drawn
                    # reply, and REPLACE result with the parsed, role-split geometry
                    # (aoi_bbox + points + the section line). Mirrors the
                    # geocode_location -> region-choice pause/resume seam. Fail-open:
                    # timeout / cancel / no client / malformed draw all become a TYPED
                    # result (honesty floor), never a fabricated AOI.
                    if (
                        call.name == "request_spatial_input"
                        and isinstance(result, dict)
                        and result.get(SPATIAL_INPUT_SENTINEL_KEY) is True
                    ):
                        result = await _handle_request_spatial_input(
                            websocket, state, call.args or {}
                        )
                    # Region-disambiguation picker: when geocode_location came back as a
                    # state-bbox-fallback snap, offer the user a narrower sub-region
                    # (default: counties) on top of the whole-state default. PAUSES the
                    # turn awaiting the region-choice-provided reply; on a "region" pick
                    # this MUTATES result["bbox"] in place so the immediate zoom-to below
                    # AND the function_response the model reads next turn use the narrowed
                    # extent. Fail-open: headless client / timeout / whole-state pick keeps
                    # the state bbox unchanged. MUST run BEFORE the zoom-to so the camera
                    # snaps to the final extent.
                    if (
                        call.name == "geocode_location"
                        and isinstance(result, dict)
                    ):
                        await _maybe_handle_region_choice(
                            websocket, state, result
                        )
                    # Demo UX: snap the map to a geocoded location
                    # IMMEDIATELY -- the user should not wait for a downstream
                    # layer publish to see the map move. Best-effort.
                    if (
                        call.name == "geocode_location"
                        and isinstance(result, dict)
                        and result.get("bbox")
                        and state.emitter is not None
                    ):
                        try:
                            await state.emitter.emit_map_command(
                                "zoom-to", {"bbox": list(result["bbox"])}
                            )
                            # Accumulate the turn's zoom-to so the closing CaseChatMessage persists
                            # it in map_command_emissions -- Case-reopen snap-to-location replays
                            # the LAST persisted zoom-to.
                            state.current_turn_map_commands.append(
                                {
                                    "command": "zoom-to",
                                    "args": {"bbox": list(result["bbox"])},
                                }
                            )
                        except Exception:  # noqa: BLE001 -- UX nicety only
                            logger.debug("geocode zoom-to emit failed", exc_info=True)
                    # SNAP-TO-AOI INDEPENDENT OF GEOLOCATE: the camera must snap whenever an
                    # AOI/bbox is SET, not only on a geocode_location result -- when the
                    # user gives coordinates directly the model skips geocode_location, so
                    # generalize: ANY tool result carrying a usable bbox / aoi_bbox snaps
                    # the camera (deduped against the turn's last zoom-to so a chain of
                    # bbox-bearing tools does not re-snap to the SAME extent).
                    # geocode_location is emitted above; skip it here to avoid a double-emit.
                    if call.name != "geocode_location" and state.emitter is not None:
                        aoi = _aoi_zoom_to_bbox(
                            result, state.current_turn_map_commands
                        )
                        if aoi is not None:
                            try:
                                await state.emitter.emit_map_command(
                                    "zoom-to", {"bbox": list(aoi)}
                                )
                                state.current_turn_map_commands.append(
                                    {"command": "zoom-to", "args": {"bbox": list(aoi)}}
                                )
                            except Exception:  # noqa: BLE001 -- UX nicety only
                                logger.debug(
                                    "aoi-set zoom-to emit failed", exc_info=True
                                )
                    # Emit a chart-emission WS envelope whenever a chart-generation tool
                    # returns a ChartEmissionPayload-shaped dict (key signal: envelope_type
                    # == "chart-emission" + a dict vega_lite_spec). Fires IN ADDITION to the
                    # standard function_response -- the client gets the full Vega-Lite spec
                    # on the envelope (vega-embed rendering + stacked gallery), and a
                    # COMPACT data summary on the function_response (stripped by
                    # summarize_tool_result so the model narrates from numbers, not inline
                    # rows). Also persists a SessionChartRecord so the chart replays on Case
                    # rehydration.
                    if is_chart_emission_result(result):
                        await _maybe_emit_chart(websocket, state, result)
                    # Emit a code-exec-result WS envelope whenever code_exec_request returns
                    # a result carrying the full code-exec-result payload (key signal:
                    # _code_exec_result with envelope_type == "code-exec-result"). Fires IN
                    # ADDITION to the standard function_response -- the client gets the full
                    # result card via the envelope, the model gets the COMPACT summary (spec
                    # stripped by summarize_tool_result).
                    if is_code_exec_result(result):
                        await _maybe_emit_code_exec_result(websocket, state, result)
                    # Record success so the consecutive-failure counter
                    # resets -- a recovered tool should not stay penalised.
                    state.circuit_breaker.record_success(call.name)
                    # Loop-watchdog progress witness: a successful call that PRODUCED a real
                    # artifact (layer/handle/feature set) resets the no-progress streak even
                    # if the model repeats the same call; a bare-ack return does NOT -- that
                    # is the no-op-repeat wedge shape the watchdog exists to catch.
                    _round_had_success = True
                    _call_made_progress = _dispatch_made_progress(result)
                    if _call_made_progress:
                        _round_made_progress = True
                    # CRISP-END: a top-level run-a-model composer that just produced its
                    # artifact IS the answer. Latch the deliverable + reset the
                    # post-deliverable idle streak, and stamp a one-time wrap-up directive
                    # below so the model summarizes and stops instead of spinning to the
                    # loop_exhausted cap.
                    _call_is_terminal_deliverable = (
                        _call_made_progress and _is_terminal_composer(call.name)
                    )
                    if _call_is_terminal_deliverable:
                        _deliverable_done = True
                        _post_deliverable_idle = 0
                    # On a successful dispatch, keep the tool in the Case's
                    # monotonic visible set so the LLM can re-issue it on a later
                    # turn with refined args (never hidden mid-task by the enforce
                    # subset -- covers the rare fail-open turn where the dispatched
                    # tool was not in the retrieved set).
                    state.visible_tools.add(call.name)
                    # DISCOVERY-EXPANDS-GATE: when the tool-search tool returns
                    # candidate tool names, UNION them into this turn's visible gate
                    # (and the Case visible set, so they stay reachable) for
                    # SUBSEQUENT rounds, capped at _DISCOVERY_EXPAND_CAP (bounds an
                    # unbounded ranked tail). Only names that are real, registered,
                    # and not already visible count toward the cap; the rebuild of
                    # tool_decls is deferred to once-per-round below.
                    if call.name in _gate_expander_tool_names():
                        _hits = _tool_names_from_search_result(result)
                        _added_now: list[str] = []
                        for _cand in _hits:
                            if len(_discovery_expanded) >= _DISCOVERY_EXPAND_CAP:
                                break
                            if (
                                _cand in TOOL_REGISTRY
                                and _cand not in _retrieval_registry
                                and _cand not in _discovery_expanded
                            ):
                                _discovery_expanded.add(_cand)
                                _added_now.append(_cand)
                        if _added_now:
                            _retrieval_registry = dict(_retrieval_registry)
                            for _cand in _added_now:
                                _retrieval_registry[_cand] = TOOL_REGISTRY[_cand]
                            state.visible_tools.update(_added_now)
                            _tool_decls_dirty = True
                            logger.info(
                                "discovery-expand: +%d tool(s) into the gate "
                                "(turn total=%d/%d) via %s session=%s: %s",
                                len(_added_now),
                                len(_discovery_expanded),
                                _DISCOVERY_EXPAND_CAP,
                                call.name,
                                state.session_id,
                                _added_now,
                            )
                except asyncio.CancelledError:
                    # Propagate cancel through the loop -- handled below.
                    raise
                except Exception as exc:  # noqa: BLE001 -- surface to the model
                    logger.exception(
                        "tool dispatch raised session=%s tool=%s err=%s",
                        state.session_id,
                        call.name,
                        exc,
                    )
                    # Record failure, passing the exception so the breaker counts ONLY
                    # upstream/transient faults toward the trip threshold. Deterministic
                    # CLIENT/arg errors (*ArgError, BboxInvalidError, ValueError/TypeError
                    # arg-shape errors) are model-side faults the model can self-correct and
                    # retry -- they must NOT trip a breaker that would then block the
                    # corrected-args retry. CircuitBreakerError is excluded entirely (the
                    # breaker already fired; do not increment again). BenchBlockedError is
                    # likewise excluded: a bench block is a harness artifact, not a tool
                    # fault, and must never penalize the tool's breaker.
                    if not isinstance(exc, (CircuitBreakerError, BenchBlockedError)):
                        state.circuit_breaker.record_failure(call.name, exc)
                    dispatch_error = exc
                    # BENCH pre-dispatch block hook: a WRONG-pick block ends the turn (the
                    # model must not get to pick again; the bench grades the wrong pick and
                    # moves on). A correct-block does NOT end the turn here (the bench ends
                    # it client-side after grading CORRECT_BLOCKED). Latched; the break
                    # happens once this round's calls are all recorded so the blocked
                    # tool's function-response still reaches the wire.
                    if (
                        isinstance(exc, BenchBlockedError)
                        and exc.blocked_class == "wrong_pick"
                    ):
                        _bench_wrong_pick_end = True
                    # A failed / circuit-broken call is the CIRCUIT BREAKER's territory (it
                    # delivers CIRCUIT_BREAKER_TRIPPED so the model adapts and the turn
                    # continues). Mark the round so the watchdog does NOT also count it --
                    # the breaker, not the watchdog, owns a stream-of-failures turn.
                    _round_had_failure = True
                _tool_latency_ms = (asyncio.get_running_loop().time() - _tool_start) * 1000.0

                summary = summarize_tool_result(
                    call.name, result, error=dispatch_error
                )
                _uri_reg = get_uri_registry(state.session_id)
                # EMIT SEAM: the LLM-facing function_response shows SHORT
                # layer handles (L<n>) wherever a registered layer URI would appear --
                # the single biggest hallucination surface (~30 tokens per raw URI
                # echo). ONLY this LLM surface changes: the plugin-bound wire envelopes
                # keep carrying the REAL uri the plugin renders from. The rewrite never
                # raises (falls back to the unrewritten summary).
                summary = _uri_reg.rewrite_result_for_llm(summary)
                # Stage 3 guard (d): geocode drift warning. A successful
                # geocode_location pins this turn's geocoded bbox; any LATER call whose
                # bbox arg intersects NEITHER that bbox NOR the active AOI gets an
                # advisory WARNING appended to its function_response (never blocks).
                # Kill-switch: TRID3NT_GEOCODE_DRIFT_WARN=0.
                if call.name == "geocode_location":
                    if dispatch_error is None and isinstance(result, dict):
                        _gc_bbox = _coerce_bbox4(result.get("bbox"))
                        if _gc_bbox is not None:
                            _turn_geocode_bbox = list(_gc_bbox)
                elif (
                    _turn_geocode_bbox is not None
                    and isinstance(summary, dict)
                    and _env_flag("TRID3NT_GEOCODE_DRIFT_WARN", True)
                ):
                    _drift_note = _geocode_drift_note(
                        call.args, _turn_geocode_bbox, state.active_aoi_bbox
                    )
                    if _drift_note:
                        summary["aoi_drift_warning"] = _drift_note
                        logger.info(
                            "geocode-drift WARNING session=%s tool=%s "
                            "geocoded=%s",
                            state.session_id,
                            call.name,
                            _turn_geocode_bbox,
                        )
                # Surface the layer handles this dispatch registered so the model passes
                # HANDLES -- never raw storage paths -- into downstream *_uri params.
                # The announcement maps {layer_id: L<n>}; the server resolves either
                # form to the exact URIs it recorded (uri_registry.py).
                _new_handles = _uri_reg.drain_announcements()
                if _new_handles and dispatch_error is None:
                    summary["layer_handles"] = {
                        _layer_id: (_uri_reg.short_for_uri(_uri) or _layer_id)
                        for _layer_id, _uri in _new_handles.items()
                    }
                    # Emission is automatic: a produced layer is already on the
                    # map, so the note carries the one thing the model must do
                    # with a handle - pass it, never rebuild it.
                    summary["layer_handles_note"] = (
                        "These layers are already on the user's map. Pass the "
                        "short handle (the L<n> value above) or the layer name "
                        "(the key) for any *_uri tool parameter — the server "
                        "resolves handles to the exact stored URIs. Do "
                        "NOT construct or echo s3:// paths or any other "
                        "storage URI."
                    )
                # A top-level run-a-model composer just delivered its artifact -- stamp
                # a one-time wrap-up directive on its function_response so the model
                # summarizes and STOPS rather than emitting more tool calls until the
                # loop_exhausted cap.
                if _call_is_terminal_deliverable and isinstance(summary, dict):
                    summary["completion_directive"] = _DELIVERABLE_COMPLETE_DIRECTIVE
                logger.info(
                    "function-response queued session=%s iter=%d tool=%s summary_keys=%s",
                    state.session_id,
                    iterations,
                    call.name,
                    sorted(summary.keys()),
                )

                # tool-card-expand-output: emit the raw input args + the raw
                # function_response on a tool-io sidecar keyed by THIS dispatch's
                # pipeline step. The web merges it into the matching tool card's
                # expander so a server-side / upstream-API failure the narration hides
                # becomes visible. Best-effort: a missing step_id (a dispatch that
                # never reached the emitter) skips the emit; the emitter itself never
                # raises on a bad payload.
                _io_step = (
                    state.emitter.last_tool_step
                    if state.emitter is not None
                    else None
                )
                # Guard against a STALE last_tool_step: a dispatch that raised
                # BEFORE the emitter created a step (ToolNotFoundError, payload-
                # warning cancel) leaves the prior tool's step on the accessor.
                # Only stamp IO when the recorded step is THIS tool's step.
                if _io_step is not None and _io_step.tool_name != call.name:
                    _io_step = None
                if _io_step is not None:
                    _io_is_error = dispatch_error is not None or (
                        isinstance(summary, dict)
                        and summary.get("status") == "error"
                    )
                    try:
                        await state.emitter.emit_tool_io(
                            step_id=_io_step.step_id,
                            tool_name=call.name,
                            raw_args=call.args,
                            function_response=summary,
                            is_error=_io_is_error,
                        )
                    except Exception:  # noqa: BLE001 -- expander is best-effort
                        logger.debug(
                            "tool-io emit failed session=%s tool=%s",
                            state.session_id,
                            call.name,
                            exc_info=True,
                        )

                # Fire-and-forget telemetry for this LLM-initiated function_call.
                # Non-blocking -- emit_tool_call_event wraps the write in
                # asyncio.ensure_future so no await is needed; a write failure logs at
                # WARNING and never raises. A workflow that swallowed its own exception
                # and returned a failed/partial envelope raises NO dispatch_error, but
                # summarize_tool_result stamps status="error" (honesty floor) -- derive
                # the telemetry success flag and error_code from that summary so a
                # returned-failure is recorded as a FAILURE in telemetry/routing, not a
                # silent success. A genuinely-raised exception still wins and keeps its
                # own code.
                _tel_error_code: str | None = None
                _tel_success = dispatch_error is None
                if dispatch_error is not None:
                    _tel_error_code = str(
                        getattr(dispatch_error, "error_code", None)
                        or type(dispatch_error).__name__.upper()
                    )
                elif isinstance(summary, dict) and summary.get("status") == "error":
                    _tel_success = False
                    _summary_code = summary.get("error_code")
                    _tel_error_code = (
                        str(_summary_code) if _summary_code is not None else None
                    )
                # The adapter surfaces ``UsageMetadataEvent`` at the end of each
                # model stream; ``last_usage`` carries the most recent observation.
                # Pipe ``cached_content_token_count`` through so the telemetry
                # record reflects the prompt-cache discount.
                _tel_cached_tokens = (
                    last_usage.cached_content_token_count
                    if last_usage is not None
                    else None
                )
                # Derive result_usable at the SAME chokepoint, reusing the honesty-floor
                # signal already stamped on summary (NO_RENDERABLE_LAYER / failure-
                # tagged modeled envelope). A layer-producing tool that returned
                # status="ok" with an empty layers list is success=True but
                # result_usable=False. routed_ok stays None here -- the supersession
                # heuristic is a same-session ADJACENT-chain signal only computable at
                # aggregation time (catalog_http._aggregate_records).
                _tel_result_usable = classify_result_usable(
                    call.name, result, summary
                )
                await emit_tool_call_event(
                    session_id=state.session_id,
                    ts=now_utc().isoformat(),
                    tool_name=call.name,
                    source="llm",
                    args_hash=compute_args_hash(call.args),
                    success=_tel_success,
                    latency_ms=_tool_latency_ms,
                    error_code=_tel_error_code,
                    cached_content_token_count=_tel_cached_tokens,
                    result_usable=_tel_result_usable,
                    model_id=_effective_model,
                    # turn_id = the per-user-message dispatch (pipeline) id: the
                    # recall@k join key against this turn's shadow-selection row.
                    turn_id=pipeline_id,
                )
                # PER-TURN TELEMETRY: one dispatched tool call counted at the
                # same chokepoint the per-tool record is emitted from.
                _turn_tool_dispatch_count += 1
                # Pass the thought_signature harvested off the function_call Part
                # through to the replayed model turn. A provider that emits an opaque
                # reasoning signature requires the same byte-blob on the replayed
                # function_call Part; a provider that emits None is a no-op -- the
                # helper forwards whatever was harvested with no behavior change.
                contents.append(
                    build_function_call_content(
                        call.name,
                        call.args,
                        call.call_id,
                        thought_signature=call.thought_signature,
                    )
                )
                contents.append(
                    build_function_response_content(call.name, summary, call.call_id)
                )

            # DISCOVERY-EXPANDS-GATE (task 2): a tool-search this round widened
            # the visible gate -- rebuild ``tool_decls`` ONCE so the NEXT model
            # round sees the unioned tools. No-op unless a search actually added
            # (the common path never sets the dirty flag).
            if _tool_decls_dirty:
                tool_decls = build_tool_declarations(_retrieval_registry)
                _tool_decls_dirty = False
                logger.info(
                    "discovery-expand: rebuilt tool declarations (%d tools "
                    "visible) turn=%s session=%s",
                    len(_retrieval_registry),
                    pipeline_id,
                    state.session_id,
                )

            # BENCH pre-dispatch block hook: a WRONG-pick block this round ends the
            # turn. The blocked tool's typed function-response is already on the
            # wire above; break to the clean post-loop finalize so a turn-complete
            # is emitted and the bench grades the wrong pick and advances (a clean
            # conclusion, not an _agent_abort runaway).
            if _bench_wrong_pick_end:
                logger.info(
                    "bench-block: wrong-pick -> ending turn session=%s iter=%d",
                    state.session_id,
                    iterations,
                )
                break

            # GUARD 3 (loop watchdog) POST-DISPATCH record: the round counts toward
            # the no-progress streak ONLY when it had calls, did NOT produce a real
            # artifact, and was NOT a failure / circuit-broken round (the breaker's
            # territory). A producing round, an all-failed round, or a
            # short-circuited round resets the streak so the watchdog never
            # pre-empts loop-exhausted or CIRCUIT_BREAKER_TRIPPED. A genuine
            # no-op-repeat runaway (same successful call returning nothing new)
            # trips and aborts.
            _round_progressed = _round_made_progress or (
                _round_had_failure and not _round_had_success
            )
            _wd_trip = _watchdog.record_round(
                _round_sig, made_progress=_round_progressed
            )
            if _wd_trip is not None:
                logger.warning(
                    "loop watchdog tripped session=%s iter=%d sig=%r "
                    "made_progress=%s had_failure=%s had_success=%s",
                    state.session_id,
                    iterations,
                    _round_sig,
                    _round_made_progress,
                    _round_had_failure,
                    _round_had_success,
                )
                _agent_abort = (_wd_trip, abort_message(_wd_trip))
                break

            # CRISP-END-AFTER-DELIVERABLE: once a terminal composer has delivered, a
            # round that produces NOTHING NEW is the model spinning past a finished
            # answer. This SAFETY budget concludes the turn CLEANLY after a couple
            # of idle rounds -- a normal (no-tool) break, NOT the loop_exhausted
            # cap. A producing round resets the streak so genuine follow-up work is
            # never cut off. _agent_abort stays None: this is a clean conclusion,
            # closed exactly like a natural text-terminal exit.
            if _deliverable_done:
                if _round_made_progress:
                    _post_deliverable_idle = 0
                else:
                    _post_deliverable_idle += 1
                    if _post_deliverable_idle >= _POST_DELIVERABLE_WRAPUP_ROUNDS:
                        logger.info(
                            "crisp-end: deliverable done + %d idle round(s) "
                            "session=%s iter=%d -- concluding turn cleanly "
                            "(no loop_exhausted)",
                            _post_deliverable_idle,
                            state.session_id,
                            iterations,
                        )
                        _crisp_concluded = True
                        break

            # Loop: re-stream with the appended call + response so the model can
            # decide its next move (another tool call OR a narrative wrap-up).
        else:
            # Loop fell through the STEP CAP without a clean (no-tool-call) exit. A
            # guard (wall-clock / watchdog) abort instead breaks with _agent_abort
            # set and is surfaced below, so this else only handles natural
            # exhaustion of the step cap.
            #
            # RECONCILE THE STEP CAP WITH MAX_TURN_ITERATIONS: when the binding
            # bound is the historical MAX_TURN_ITERATIONS (full-tier models, where
            # _step_cap >= MAX_TURN_ITERATIONS), natural exhaustion emits the
            # pre-existing loop_exhausted / MAX_ITERATIONS_REACHED envelope (the
            # user-facing contract the web UI + tests rely on). Only when the cap
            # was TIGHTENED for a cheap/loop-prone tier (_step_cap <
            # MAX_TURN_ITERATIONS) is this a NEW, tighter runaway backstop, surfaced
            # as AGENT_STEP_LIMIT_REACHED. AGENT_LOOP_DETECTED stays reserved for
            # the watchdog (a genuine no-progress repeat), never natural exhaustion.
            if _agent_abort is None and _step_cap < MAX_TURN_ITERATIONS:
                _agent_abort = (
                    ABORT_STEP_CAP, abort_message(ABORT_STEP_CAP)
                )
            logger.warning(
                "model loop hit step cap=%d (full=%d) session=%s -- "
                "emitting %s envelope",
                _step_cap,
                MAX_TURN_ITERATIONS,
                state.session_id,
                "agent-abort" if _agent_abort is not None else "loop_exhausted",
            )
            # Full-tier natural exhaustion: the historical loop_exhausted path.
            if _agent_abort is None:
                await _send_loop_exhausted(websocket, state.session_id)
                # The client waits for a stream-closing done=True to stop
                # spinning. A cap-hit turn ended mid tool-dispatch with no
                # trailing narration (``current_message_id is None``), so the
                # final-segment finalize below no-ops -- emit a standalone
                # terminator here with a fresh id (mirrors the abort path).
                if current_message_id is None:
                    await _session_safe_send(websocket, state.session_id,
                        _new_envelope(
                            "agent-message-chunk",
                            state.session_id,
                            AgentMessageChunkPayload(
                                message_id=new_ulid(), delta="", done=True
                            ),
                        )
                    )

        # A guard fired (tightened step cap, wall-clock, or loop watchdog).
        # Surface the honest typed abort envelope and a stream-closing terminal
        # frame, then fall through to the normal finalize/pipeline-complete path
        # so the turn TERMINATES cleanly and its busy/lock state releases. The
        # model CONTEXT is preserved (contents already hold the partial chain);
        # we just stop dispatching. NOT a loop continuation -- this only runs on
        # abort.
        if _agent_abort is not None:
            _abort_code, _abort_msg = _agent_abort
            await _send_agent_abort(
                websocket, state.session_id, _abort_code, _abort_msg
            )
            # A runaway loop exits mid tool-dispatch with no trailing narration,
            # so ``current_message_id is None`` and the segment finalize below
            # no-ops. The client still waits for a stream-closing done=True to
            # stop spinning, so emit a standalone terminator with a fresh id.
            if current_message_id is None:
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=new_ulid(), delta="", done=True
                        ),
                    )
                )

        # Terminal frame for the FINAL narration segment. Only fires if a
        # segment is actually open (text was emitted after the last tool
        # round); a turn with no trailing narration has current_message_id is
        # None (no phantom empty bubble). is_terminal=True lets it snapshot the
        # turn's layer/zoom accumulator.
        if current_message_id is not None:
            await _finalize_segment(
                websocket,
                state,
                current_message_id,
                _segment_buf,
                is_terminal=True,
                thinking_parts=_thinking_buf,
            )
            current_message_id = None
        else:
            # The turn exited with NO open segment on the wire. Two distinct shapes:
            #   (a) A narration segment WAS already streamed+finalized this turn
            #       (segments_done > 0) -- the client already got the closing
            #       summary; emit NOTHING here (re-streaming turn_narration would
            #       DOUBLE the text and duplicate the chat rows).
            #   (b) NO segment was ever streamed (segments_done == 0) yet the turn
            #       accumulated real narration text across iterations (e.g. the
            #       ONLY narration arrived in a round that ALSO carried the
            #       terminating tool call, so the boundary finalize cleared its
            #       buffer before it reached the wire as its own terminal segment).
            #
            # Recovery for (b): open ONE fresh terminal segment, replay the full
            # accumulated turn_narration as chunks, then finalize it terminal
            # (done=True wire frame + persisted role="agent" row that also
            # snapshots the layer/zoom accumulator). Honesty floor: replay EXACTLY
            # what the model accumulated -- never synthesize a success summary. Guarded
            # so an empty-narration turn emits NO bubble.
            _seg_done = 0
            _cur_task = asyncio.current_task()
            if _cur_task is not None:
                _seg_done = _TURN_SEGMENTS_PERSISTED_BY_TASK.get(_cur_task, 0)
            _closing = "".join(turn_narration).strip()
            if _seg_done == 0 and _closing:
                recovered_id = new_ulid()
                # Stream the recovered narration so the live client renders the
                # closing bubble (one message_id == one bubble). Each chunk
                # carries done=False; the terminal done=True comes from
                # ``_finalize_segment`` below.
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=recovered_id, delta=_closing, done=False
                        ),
                    )
                )
                # Persist + close via the SAME terminal-segment path so the
                # closing row carries the turn's layer/zoom accumulator exactly
                # like a normal text-terminal turn. ``_finalize_segment`` joins
                # its buffer arg, so feed the recovered text in as the buffer.
                await _finalize_segment(
                    websocket,
                    state,
                    recovered_id,
                    [_closing],
                    is_terminal=True,
                    thinking_parts=_thinking_buf,
                )
            elif _crisp_concluded and _seg_done == 0:
                # CRISP-END edge case: the turn delivered a composer artifact and
                # concluded via the post-deliverable idle safety, but emitted ZERO
                # narration anywhere -- so neither the segment-finalize nor the
                # recovery branch above sent a stream-closing frame. Emit a standalone
                # terminator with a fresh id (mirrors the loop_exhausted / abort paths).
                # Honesty floor: no synthesized summary, just the close-frame.
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=new_ulid(), delta="", done=True
                        ),
                    )
                )

        # Complete the outer pipeline snapshot (LLM generation phase).
        thinking_step = PipelineStep(
            step_id=step_id,
            name="llm_generation",
            tool_name="model_generate",
            state="complete",
        )
        state.current_pipeline_steps = [thinking_step]
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "pipeline-state",
                state.session_id,
                PipelineStatePayload(pipeline_id=pipeline_id, steps=[thinking_step]),
            )
        )
        # Append to the entry-captured list -- after a mid-stream
        # case switch this turn's text must not leak into the NEW Case's
        # LLM context (the carryover class).
        turn_history.append({"role": "user", "text": user_text})
        # Name an Untitled Case from its first prompt + refresh the left rail.
        # The PRIMARY autoname is a pre-dispatch call; this tail is a
        # guarded no-op fallback that only fires when a mid-stream case switch
        # re-pinned active_case_id to a fresh Untitled case not yet seen by the
        # pre-dispatch call.
        if await _maybe_autoname_case(state, user_text):
            await _emit_case_list(websocket, state, force=True)

    except asyncio.CancelledError:
        # Invariant 8 -- distinct cancelled step state, not failed. A
        # partially-open narration segment's done=True is intentionally NOT
        # sent here (a cancelled stream has no clean terminal); current_turn_
        # narration still holds the partial text and the dispatch wrapper's
        # finally persists the un-finalized open-segment tail best-effort, so no
        # narration is lost.
        _turn_error_class = "cancelled"
        cancelled_step = PipelineStep(
            step_id=step_id,
            name="llm_generation",
            tool_name="model_generate",
            state="cancelled",
        )
        state.current_pipeline_steps = [cancelled_step]
        try:
            await websocket.send(
                _new_envelope(
                    "pipeline-state",
                    state.session_id,
                    PipelineStatePayload(pipeline_id=pipeline_id, steps=[cancelled_step]),
                )
            )
        except Exception:  # noqa: BLE001 -- socket may be down on cancel
            pass
        raise
    except ConnectionClosed as exc:
        # The CLIENT transport died mid-turn. This is NOT a model failure
        # -- the LLM stream rides the provider transport, never the client
        # websocket, so a
        # ConnectionClosed reaching this scope can only be a residual raw send
        # to the dead client socket. Every known per-turn send now routes
        # through _session_safe_send (never raises; sibling-socket fallback),
        # so this branch is a backstop: log once and end the turn quietly. NO
        # LLM_UNAVAILABLE error envelope and NO terminal-failure card -- a
        # client transport drop is not a model failure. The persisted
        # chat/tool rows plus the session-resume replay carry the turn's
        # results to the client when it reconnects.
        _turn_error_class = "client_disconnect"
        logger.warning(
            "client websocket closed mid-turn (transport drop, not a model "
            "failure) session=%s: %s",
            state.session_id,
            exc,
        )
    except ContextWindowExceededError as exc:
        # OPEN-14 REACTIVE CLIP GUARD: the local (Ollama/OpenAI-compat) model's
        # prompt was clipped by num_ctx even after one recompaction + retry.
        # Distinct typed envelope -- NOT the generic LLM_UNAVAILABLE bucket --
        # so the honesty floor tells the user exactly why the turn stopped (a
        # genuinely oversized Case, not a transient model outage). Mirrors the
        # terminal-failure-card persist so a reconnect / Case-reopen replay
        # shows the failed card rather than a phantom "still running" spinner.
        _turn_error_class = "context_window"
        logger.warning(
            "context-budget: turn aborted, context window exceeded session=%s "
            "num_ctx=%d",
            state.session_id,
            exc.num_ctx,
        )
        # Persist the terminal-failure card BEFORE attempting the error-envelope
        # send (not the reverse) -- persist never touches the socket or this
        # payload, so it cannot be starved by any failure in the send path.
        # Both steps are individually try/excepted with explicit logging.
        #
        # The fabrication backstop (item 4, context_budget) also applies on this
        # exception path: zero tool calls dispatched this whole turn, AND the
        # accumulated narration matches the claim regex, appends the same
        # honest caveat as the normal zero-tool-call terminal branch.
        _aborted_narration = "".join(turn_narration)
        _fabricated_claim = not _turn_ever_called_tool and looks_like_fabricated_action_claim(
            _aborted_narration
        )
        if _fabricated_claim:
            logger.warning(
                "context-budget: fabrication backstop fired on abort path "
                "session=%s (zero tool calls this turn)",
                state.session_id,
            )
        state.current_turn_context_abort_note = build_context_window_abort_note(
            fabricated_claim=_fabricated_claim
        )
        try:
            await _persist_terminal_failure_card(
                state,
                error_code="CONTEXT_WINDOW_EXCEEDED",
                message=str(exc),
                case_id=_turn_case_id(state),
            )
        except Exception:  # noqa: BLE001 -- persist is best-effort but must
            # never be allowed to skip the (equally best-effort) error send
            # below; _persist_terminal_failure_card already swallows +
            # `logger.exception`s internally, this is defense-in-depth only.
            logger.exception(
                "context-budget: terminal-failure card persist raised "
                "session=%s",
                state.session_id,
            )
        try:
            await _send_error(
                websocket,
                state.session_id,
                "CONTEXT_WINDOW_EXCEEDED",
                str(exc),
                retryable=False,
            )
        except Exception:  # noqa: BLE001 -- _send_error/_session_safe_send
            # _send_error/_session_safe_send already never raise on an ordinary
            # send failure; this is defense-in-depth logging only. NOTE: a genuine
            # asyncio.CancelledError here is NOT caught (it is a BaseException, not
            # an Exception) and is intentionally left to propagate -- cancellation
            # must never be swallowed. By the time we reach this send the
            # terminal-failure persist has ALREADY completed, so a cancelled/dead-
            # socket send can no longer suppress it.
            logger.exception(
                "context-budget: error-envelope send raised session=%s",
                state.session_id,
            )
    except UpstreamProviderError as exc:
        # UPSTREAM-PROVIDER DISCIPLINE (never internalize upstream failure).
        # The adapter already retried the transient provider
        # failure with backoff and exhausted its budget -- this turn ends with
        # an HONEST provider-unavailable narration (typed, provider NAMED,
        # verbatim detail), never a silent empty turn and never recorded as an
        # internal error (error_class="upstream_provider" on the per-turn
        # telemetry record). The wire error_code stays the contract-valid
        # LLM_UNAVAILABLE (retryable) -- the closed ErrorCode Literal is a
        # contracts surface this lane may not widen -- while the free-form
        # failure-card code carries the DISTINCT UPSTREAM_PROVIDER_UNAVAILABLE.
        _turn_error_class = "upstream_provider"
        logger.error(
            "upstream provider unavailable session=%s provider=%s attempts=%d "
            "verbatim=%s",
            state.session_id,
            exc.provider,
            exc.attempts,
            exc.detail,
        )
        _narration = (
            f"The upstream model provider ({exc.provider}) is currently "
            f"unavailable -- the request was retried {exc.attempts} time(s) "
            f"and the provider kept failing. Provider error: {exc.detail}. "
            "This is a temporary provider-side outage, not a problem with "
            "your request; please try again shortly or switch models."
        )
        # Honest closing narration IN CHAT (one bubble, streamed + terminal
        # done=True) and persisted as an agent row so a Case reopen replays
        # the same honest ending. Best-effort sends via _session_safe_send.
        _upstream_msg_id = current_message_id or new_ulid()
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "agent-message-chunk",
                state.session_id,
                AgentMessageChunkPayload(
                    message_id=_upstream_msg_id, delta=_narration, done=False
                ),
            )
        )
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "agent-message-chunk",
                state.session_id,
                AgentMessageChunkPayload(
                    message_id=_upstream_msg_id, delta="", done=True
                ),
            )
        )
        try:
            await _persist_chat_turn(
                state,
                role="agent",
                content=_narration,
                pipeline_id=state.current_turn_pipeline_id,
                layer_emissions=[],
                case_id=_turn_case_id(state),
            )
        except Exception:  # noqa: BLE001 -- persist is best-effort
            logger.exception(
                "upstream-provider narration persist failed session=%s",
                state.session_id,
            )
        try:
            await _persist_terminal_failure_card(
                state,
                error_code="UPSTREAM_PROVIDER_UNAVAILABLE",
                message=str(exc),
                case_id=_turn_case_id(state),
            )
        except Exception:  # noqa: BLE001 -- defense-in-depth logging only
            logger.exception(
                "upstream-provider failure-card persist raised session=%s",
                state.session_id,
            )
        await _send_error(
            websocket,
            state.session_id,
            "LLM_UNAVAILABLE",
            f"Upstream provider unavailable ({exc.provider}): {exc.detail}",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 -- surface as A.6 LLM_UNAVAILABLE
        # PER-TURN TELEMETRY: a NON-transient provider rejection (auth / bad
        # request) classifies as ``provider_request`` (fail-fast, its own
        # class); anything else is honestly ``internal``. Upstream transients
        # that escaped the retry seam classify ``upstream_provider``.
        _turn_error_class = classify_provider_error_class(exc)
        logger.exception("model stream failed: %s", exc)
        await _send_error(
            websocket,
            state.session_id,
            "LLM_UNAVAILABLE",
            f"Model generation failed: {exc}",
            retryable=True,
        )
        # The error envelope above marks the in-memory pipeline failed on THIS
        # live socket, but a WS reconnect / Case-reopen replays from
        # chat_history, where nothing records this terminal failure -- any tool
        # card the user last saw spinning would replay as "running" forever.
        # Persist a role="tool" FAILED tool-card row (mirroring the existing
        # tool-card shape) so the session-resume replay renders the failed card.
        # Honesty floor: this writes a FAILURE -- never a success.
        #
        # EXCEPTION: a RuntimeError whose __cause__ is StopIteration is the
        # PEP-479 async-generator-exhaustion artifact -- the model stream
        # generator ran dry, NOT a genuine model failure (a finite mocked stream
        # shape only). Persisting a failed tool card here would inject a
        # phantom failure row into an otherwise-clean tool-terminal turn, so we
        # skip it.
        if not isinstance(exc.__cause__, StopIteration):
            await _persist_terminal_failure_card(
                state,
                error_code="LLM_UNAVAILABLE",
                message=f"Model generation failed: {exc}",
                case_id=_turn_case_id(state),
            )
    finally:
        # PER-TURN TELEMETRY: exactly ONE record per turn, every outcome
        # (clean / abort / cancel / provider failure). emit_turn_telemetry is
        # fire-and-forget + never raises, but the whole call is still wrapped
        # so a telemetry fault can never mask the turn's own outcome (including
        # a propagating CancelledError).
        try:
            _turn_wall_ms = (
                asyncio.get_running_loop().time() - started_at
            ) * 1000.0
            emit_turn_telemetry(
                turn_id=pipeline_id,
                session_id=state.session_id,
                case_id=_turn_case_id(state),
                model_id=_effective_model,
                provider=_provider,
                prompt_tokens=_turn_prompt_tokens,
                completion_tokens=_turn_completion_tokens,
                reasoning_tokens=_turn_reasoning_tokens,
                turn_wall_ms=_turn_wall_ms,
                tool_dispatch_count=_turn_tool_dispatch_count,
                error_class=_turn_error_class,
            )
        except Exception:  # noqa: BLE001 -- telemetry never breaks the turn
            logger.warning(
                "per-turn telemetry emit failed session=%s", state.session_id,
                exc_info=True,
            )

# --------------------------------------------------------------------------- #
# Dispatch wrappers with chat persistence
# --------------------------------------------------------------------------- #


async def _dispatch_model_turn_and_persist(
    websocket: ServerConnection,
    state: SessionState,
    settings: ModelSettings,
    user_text: str,
    model_id: str | None = None,
    show_thinking: bool = False,
) -> None:
    """Stream the model reply, then persist the agent's reply to the active Case.

    Wraps ``_stream_model_reply`` so the Case chat-history append happens
    after the stream completes (the streamed text is the canonical
    ``content`` field on ``CaseChatMessage``). On cancel/error we still
    attempt a best-effort persist of whatever the narration accumulator
    captured before the stream died.

    The persisted ``content`` is the REAL accumulated narration --
    ``_stream_model_reply`` resets ``state.current_turn_narration`` at
    stream start and appends every ``TextDeltaEvent`` delta across all loop
    iterations.
    """
    # Capture the turn's Case at task entry -- the finally-persist
    # below must land in the Case that OWNED this turn even when the user
    # switched Cases (or a newer turn re-pinned the binding) mid-stream.
    turn_case_id = _turn_case_id(state)
    # Bind the owning Case into the per-task ContextVar so EVERY
    # envelope this turn emits (chunks, pipeline-state, session-state, …)
    # carries Envelope.case_id and the web routes it to the right stream.
    bind_turn_case(turn_case_id)
    # Bind this turn's user-drawn geometry so composer gates read it
    # (current_turn_drawn_geometry) as a basis="user" spatial knob.
    bind_turn_drawn_geometry(state.drawn_geometry)
    # Per-turn object capture: a concurrent turn (or Case switch) re-points
    # both SessionState fields mid-stream, so this wrapper gauges completion
    # against THIS turn's history list and joins the narration list
    # registered under the running task (mocked streams that never
    # registered fall back to the live field).
    turn_history = state.chat_history
    pre_chat_len = len(turn_history)
    try:
        await _stream_model_reply(
            websocket, state, settings, user_text,
            model_id=model_id,
            show_thinking=show_thinking,
        )
    finally:
        # Close out the turn's narration persistence. Each FINALIZED
        # narration segment is already persisted in-loop by
        # ``_finalize_segment`` (interleaved with the mid-turn tool rows), so
        # this wrapper must NOT re-persist finalized segments -- it only owns
        # the un-finalized remainder + the legacy fallbacks:
        #
        #   * ``open_tail``     -- text in a segment the stream NEVER finalized
        #                         (crash/cancel mid-segment). Persisted as ONE
        #                         agent row (the de-facto terminal row) so no
        #                         narration is lost; layer_emissions=None lets
        #                         it carry the layer/zoom accumulator.
        #   * ``segments_done`` -- count of finalized agent rows this turn.
        #                         When 0 AND the stream completed cleanly with
        #                         no open tail, write the legacy single marker
        #                         row (content == joined narration, possibly "").
        #
        # All three per-task registries are popped (mocked-stream tests that
        # never registered fall back to the live field).
        _own_task = asyncio.current_task()
        if _own_task is not None:
            turn_narration = _TURN_NARRATION_BY_TASK.pop(_own_task, None)
            open_segment = _TURN_OPEN_SEGMENT_BY_TASK.pop(_own_task, None)
            segments_done = _TURN_SEGMENTS_PERSISTED_BY_TASK.pop(_own_task, 0)
            terminal_acc_persisted = _TURN_TERMINAL_ACC_PERSISTED_BY_TASK.pop(
                _own_task, False
            )
        else:
            turn_narration = None
            open_segment = None
            segments_done = 0
            terminal_acc_persisted = False
        if turn_narration is None:
            turn_narration = state.current_turn_narration
        narration = "".join(turn_narration).strip()
        open_tail = "".join(open_segment or []).strip()
        stream_completed = len(turn_history) > pre_chat_len
        # When the turn aborted on ``ContextWindowExceededError``,
        # ``_stream_model_reply``'s except handler stashed the honest abort
        # verdict here. Read + clear it once so it lands on exactly the row
        # that carries the (unverified) streamed text below, and never leaks
        # into a later turn.
        _abort_note = state.current_turn_context_abort_note
        state.current_turn_context_abort_note = None
        if turn_case_id:
            if open_tail:
                # Crash/cancel left an un-finalized open segment carrying text
                # (its done=True never fired). Persist the tail so the partial
                # narration survives; as the de-facto terminal row it also
                # captures the turn's layer/zoom accumulator (layer_emissions
                # default None). No double-persist: finalized segments already
                # cleared their buffer, so this is ONLY the un-finalized text.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content=(open_tail + _abort_note) if _abort_note else open_tail,
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            elif segments_done == 0 and (narration or stream_completed or _abort_note):
                # No segment was finalized AND no open tail: either a clean
                # narration-LESS completed turn (content="" marker -- replay row
                # count unchanged from pre-fix), a mocked-stream test that
                # populated only ``current_turn_narration`` (legacy one-row
                # contract), or an abort with NO streamed text at all (still
                # write the row so the abort note itself is not lost). Mirror
                # the previous single-row write exactly, plus the note.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content=(narration + _abort_note) if _abort_note else narration,
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            elif (
                not terminal_acc_persisted
                and (state.current_turn_map_commands or state.current_turn_layer_ids)
            ):
                # Invariant: EVERY turn that emitted a zoom-to/layer must
                # persist at least one chat row carrying it. When
                # segments_done > 0 (interleaved rows already persisted) and
                # no open tail, but the turn's final round ended in tool
                # calls with no trailing narration, none of the persisted
                # segment rows carried the zoom-to/layer accumulator. Write
                # an EMPTY marker row (content="" -- no phantom bubble) with
                # layer_emissions=None so ``_persist_chat_turn`` snapshots
                # ``current_turn_layer_ids``/``current_turn_map_commands``
                # onto it. ``terminal_acc_persisted`` guards a double-write
                # when the turn DID end in narration; the non-empty
                # accumulator guard means an accumulator-less + text-less
                # turn writes nothing.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content="",
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            # else: either the terminal segment already snapshotted the
            # accumulator (segments_done > 0 ending in narration), or the turn
            # emitted no zoom-to/layer accumulator at all -> every narration run
            # was already persisted as its own interleaved row. Nothing to add.
        # C2: whole-turn idle signal -- fires on EVERY exit (clean, cancel,
        # error) so the client settles any card still spinning ``running`` after
        # the turn ends (its terminal pipeline-state frame may have died on a
        # dropped socket). Outside the ``if turn_case_id`` guard so a root-stream
        # turn (no Case) still idles its cards; ``_emit_turn_complete`` reads the
        # turn's Case from the ContextVar bound at task entry. Best-effort.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )
