"""The streaming model-reply loop: the multi-iteration turn engine driver."""

from __future__ import annotations

import asyncio
import json
import logging
from trid3nt_contracts import new_ulid
from trid3nt_contracts.ws import AgentMessageChunkPayload, AgentThinkingChunkPayload, PipelineStatePayload, PipelineStep
from trid3nt_contracts.message import Message
from trid3nt_server.model.adapters.model_selection import ModelSettings
from trid3nt_server.model.adapters.adapter import CompactionCompleteEvent, CompactionStartEvent, FunctionCallEvent, MAX_TURN_ITERATIONS, TextDeltaEvent, ThinkingDeltaEvent, UpstreamProviderError, UsageMetadataEvent, build_contents_from_history, build_layers_present_note, build_tool_declarations, classify_provider_error_class, classify_result_usable, stream_events_with_contents, summarize_tool_result, system_prompt
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.render.charts import is_chart_emission_result
from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR
from trid3nt_server.render.pipeline_emitter import bind_turn_case
from trid3nt_server.server.turn.compaction import complete_compaction_card, mint_compaction_card
from trid3nt_server.render.uri_registry import get_uri_registry
from trid3nt_server.model.guards.circuit_breaker import CircuitBreakerError
# The gate engine (trid3nt_server.inputs.gate.confirm) is imported function-locally in
# _stream_model_reply -- deferred to break the server<->gates load cycle.
from trid3nt_server.model.guards.context_budget import ContextWindowExceededError, FABRICATION_CAVEAT, build_context_window_abort_note, looks_like_fabricated_action_claim
from trid3nt_server.model.guards.runaway_guard import ABORT_STEP_CAP, ABORT_WALL_CLOCK, LoopWatchdog, abort_message, max_turn_seconds, step_cap_for_model
from trid3nt_server.server.config import _env_flag, _tool_retrieval_k
from trid3nt_server.server.dispatch.emitter import _invoke_tool_via_emitter
from trid3nt_server.server.dispatch.helpers import _DELIVERABLE_COMPLETE_DIRECTIVE, _DISCOVERY_EXPAND_CAP, _EMPTY_COMPLETION_NUDGE, _EMPTY_COMPLETION_RETRY_CAP, _POST_DELIVERABLE_WRAPUP_ROUNDS, _default_declarable_registry, _dispatch_made_progress, _gate_expander_tool_names, _is_terminal_composer, _tool_names_from_search_result
from trid3nt_server.server.dispatch.persist import _TURN_NARRATION_BY_TASK, _TURN_OPEN_SEGMENT_BY_TASK, _TURN_SEGMENTS_PERSISTED_BY_TASK, _TURN_TERMINAL_ACC_PERSISTED_BY_TASK, _finalize_segment, _persist_chat_turn, _persist_terminal_failure_card
from trid3nt_server.server.dispatch.results import _maybe_emit_chart
from trid3nt_server.server.session.case_state import _turn_case_bbox, _turn_case_id
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.inputs.extent import as_bbox
from trid3nt_server.server.spatial import _aoi_zoom_to_bbox
from trid3nt_server.server.turn.cases import _emit_case_list, _maybe_autoname_case
from trid3nt_server.server.turn.engine import _CONTINUATION_NUDGE, _asks_for_data_or_analysis, _geocode_drift_note, _maybe_emit_tool_candidates, _session_routing_mode, _union_pinned_tool
from trid3nt_server.server.turn.wire import _emit_turn_complete, _new_envelope, _send_error, _send_loop_exhausted, _session_safe_send
from typing import Any
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("trid3nt_server.server")

def _effective_model_id(requested: str | None) -> str:
    """The model id a turn runs on: the active provider adapter's resolution of
    ``requested``, else the selection or, with none, the provider name."""
    from trid3nt_server.model.adapters.model_selection import model_provider

    provider = model_provider()
    if provider == "openai":
        from trid3nt_server.model.adapters.openai_adapter import openai_model

        return openai_model(requested)
    if provider == "anthropic":
        from trid3nt_server.model.adapters.anthropic_adapter import anthropic_model

        return anthropic_model(requested)
    return requested or provider


async def _stream_model_reply(
    websocket: ServerConnection,
    state: SessionState,
    settings: ModelSettings,
    user_text: str,
    model_id: str | None = None,
    show_thinking: bool = False,
) -> None:
    """Stream one user-message reply with multi-turn tool dispatch: each round forwards text
    deltas, dispatches the round's tool calls and feeds their results back, until a round makes
    no call; a cancel aborts the whole loop."""
    from trid3nt_server.inputs.gate.confirm import (
        SPATIAL_INPUT_SENTINEL_KEY,
        _handle_request_spatial_input,
    )

    logger.info(
        "user-message session=%s text=%r",
        state.session_id,
        user_text[:80],
    )

    # Auto-name an Untitled Case from its FIRST user message BEFORE dispatch (a failed narration
    # must not skip it). Deterministic heuristic, no LLM call; best-effort and never-raise. The
    # end-of-turn call below stays as a no-op fallback covering a mid-stream case switch.
    try:
        if await _maybe_autoname_case(state, user_text):
            await _emit_case_list(websocket, state, force=True)
    except Exception:  # noqa: BLE001 -- naming is a nicety, never break the turn
        logger.debug(
            "pre-dispatch case auto-name failed session=%s", state.session_id,
            exc_info=True,
        )

    # One bubble per CONTIGUOUS narration run: message_id is minted lazily on the first text of a
    # segment, finalized when the next function-call round dispatches, and a new segment opens after
    # that round. ``None`` means no open segment (no leading-text-before-first-tool-call bubble).
    current_message_id: str | None = None
    pipeline_id = new_ulid()
    step_id = new_ulid()
    state.current_pipeline_id = pipeline_id
    # Captured as LOCALS before any await: per-Case turn concurrency re-points SessionState fields
    # mid-stream, and this turn must keep appending to its own lists.
    state.current_turn_narration = []
    state.current_turn_context_abort_note = None
    state.gate_decisions_this_turn = {}
    turn_narration = state.current_turn_narration
    turn_history = state.chat_history
    # Per-segment buffer for the CURRENTLY OPEN bubble only; reset via .clear() at each boundary
    # (same list object stays registered). Captured + registered in the synchronous prefix so a
    # crash/cancel mid-segment lets the wrapper's finally persist the tail.
    _segment_buf: list[str] = []
    # Per-segment reasoning-text buffer, filled only when the per-turn ``show_thinking`` toggle is
    # ON. ``_finalize_segment`` persists it as the ``thinking`` field on the SAME agent row as the
    # segment's answer. Display replay only: build_contents_from_history strips it.
    _thinking_buf: list[str] = []
    _reg_task = asyncio.current_task()
    if _reg_task is not None:
        _TURN_NARRATION_BY_TASK[_reg_task] = turn_narration
        _TURN_OPEN_SEGMENT_BY_TASK[_reg_task] = _segment_buf
        _TURN_SEGMENTS_PERSISTED_BY_TASK[_reg_task] = 0
        _TURN_TERMINAL_ACC_PERSISTED_BY_TASK[_reg_task] = False

    thinking_step = PipelineStep(
        step_id=step_id,
        name="llm_generation",
        tool_name="model_generate",
        state="running",
    )
    await _session_safe_send(websocket, state.session_id,
        _new_envelope(
            "pipeline-state",
            state.session_id,
            PipelineStatePayload(pipeline_id=pipeline_id, steps=[thinking_step]),
        )
    )

    from trid3nt_server.model.adapters.model_selection import model_provider as _model_provider

    _provider = _model_provider()
    # The EFFECTIVE model, so a default-model turn's log line names the real
    # model; a log tag only, so a resolution error falls back to the selection.
    try:
        _effective_model = _effective_model_id(model_id)
    except Exception:  # noqa: BLE001 -- a log tag only, never fatal
        _effective_model = model_id
    # Provider adapters open their own client at the boundary and ignore ``client``.
    client = None
    first_token_logged = False
    started_at = asyncio.get_running_loop().time()

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
        # UNION the visible set into the Case's monotonic visible set FIRST (so it never shrinks
        # across turns), then subset the registry to the CORE_FLOOR + accrued snapshot intersected
        # with the registry (real, registered tools only).
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

    # Auto/ask tool-candidates gate. May PAUSE here (bounded -- see _tool_choice_timeout_s) awaiting
    # the user's tool-choice. A pinned tool is unioned into the visible registry + allowed set
    # BEFORE declarations are built so the model can actually call it.
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

    # Seed the multi-turn contents list with chat history + this user_text. The entry-captured list
    # -- a mid-stream case switch rebinds state.chat_history, never mutates this one.
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

    # Feed the tool-candidates outcome into the model context -- the pin directive ("Use the tool
    # 'X'"), the user's free-text clarification, or the timeout proceed-autonomously note. Appended
    # AFTER the user message so the model reads the ask, then the user's routing decision.
    for _pin_note in _pin_notes:
        contents.append(Message.user_text(_pin_note))

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

    # CRISP-END-AFTER-DELIVERABLE: once a terminal composer (run_model_* & friends) has produced its
    # artifact, the model should narrate a short summary and STOP rather than spin to the
    # loop_exhausted cap.
    _deliverable_done = False
    _post_deliverable_idle = 0
    _crisp_concluded = False

    # Fabrication backstop: set by any round that REQUESTED a call, even one that later failed
    # validation (a model that tried to act is not fabricating). A turn ending with this False AND a
    # closing narration claiming a completed geospatial action gets an honest caveat appended.
    _turn_ever_called_tool = False

    # Empty-completion retry: per-turn counter of empty-round retries already spent, capped at
    # ``_EMPTY_COMPLETION_RETRY_CAP``. Past the cap the empty round falls through to the terminal
    # break; a retry round still counts toward the step cap and the watchdog.
    _empty_retries = 0

    # _continuation_nudged is the one-per-turn nudge budget shared by both turn-loop invariants.
    _turn_tools_dispatched: set[str] = set()
    _continuation_nudged = False
    _turn_geocode_bbox: list[float] | None = None

    # Tool names search_tools returned THIS turn and unioned into the visible gate, capped at
    # ``_DISCOVERY_EXPAND_CAP``; ``_tool_decls_dirty`` rebuilds ``tool_decls`` once after the round.
    _discovery_expanded: set[str] = set()
    _tool_decls_dirty = False

    # PER-TURN accumulators: token counts SUM the adapter's per-round UsageMetadataEvents across the
    # whole turn; a provider that reports no usage leaves them None (tolerated, never fabricated).
    # _turn_error_class is stamped by the exception handlers below and stays None on a clean turn.
    _turn_prompt_tokens: int | None = None
    _turn_completion_tokens: int | None = None
    _turn_reasoning_tokens: int | None = None
    _turn_tool_dispatch_count = 0
    _turn_error_class: str | None = None

    iterations = 0
    try:
        while iterations < _step_cap:
            # Wall-clock guard: abort BEFORE the next, potentially long, model round if this turn
            # has already overrun its budget. Checked at the top of every iteration so a turn whose
            # rounds are each slow cannot exceed the wall-clock bound by more than one round.
            if asyncio.get_running_loop().time() >= _turn_deadline:
                _agent_abort = (ABORT_WALL_CLOCK, abort_message(ABORT_WALL_CLOCK))
                break
            iterations += 1
            turn_text_parts: list[str] = []
            turn_function_calls: list[FunctionCallEvent] = []
            last_usage = None
            # Compaction UX: the step_id of the currently-open compaction card, if any -- set on
            # CompactionStartEvent, read + cleared on the matching CompactionCompleteEvent.
            _compaction_step_id: str | None = None

            # Wave semantics: in ASK mode, surface a pre-dispatch tool-candidates WAVE before EACH
            # subsequent round (round 1 is covered by the pre-loop emission above).
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
                        contents.append(Message.user_text(_w_note))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- wave is best-effort
                    logger.warning(
                        "per-round tool-candidates wave failed; proceeding",
                        exc_info=True,
                    )

            async for event in stream_events_with_contents(
                contents,
                tool_declarations=tool_decls,
                system_prompt=system_prompt(),
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
                    # Entry-captured list across ALL iterations, never the live field.
                    turn_narration.append(event.delta)
                    # Also feed the OPEN-segment buffer so the boundary finalize persists exactly
                    # this run's text, and a crash leaves the un-finalized tail for the wrapper.
                    # Same registered list object -- never rebound.
                    _segment_buf.append(event.delta)

                elif isinstance(event, ThinkingDeltaEvent):
                    # Gated on the per-turn toggle: a model that leaks reasoning with it off must not
                    # reach a client that asked for it hidden. Shares the segment's message_id.
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
                    last_usage = event
                    # PER-TURN: sum the reported counts across the turn's model rounds. A round that
                    # reports None for a figure leaves that accumulator untouched (null stays null
                    # when NO round reports it -- tolerate absent, never fabricate).
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
                    # Best-effort: a failed mint never blocks the turn.
                    _compaction_step_id = await mint_compaction_card(
                        emitter=state.emitter
                    )

                elif isinstance(event, CompactionCompleteEvent):
                    # No-op when the mint above failed or never fired.
                    await complete_compaction_card(
                        emitter=state.emitter,
                        step_id=_compaction_step_id,
                        before_tokens=event.before_tokens,
                        after_tokens=event.after_tokens,
                    )
                    _compaction_step_id = None

            if not turn_function_calls:
                # Empty-completion retry: ZERO tool calls and ZERO non-whitespace text is the qwen3
                # empty-completion shape. Local (openai) path only; runs BEFORE the fabrication
                # backstop (an empty round has no closing text to fabricate from).
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
                    # Log-only: a retry must not inject a note into the persisted narration.
                    contents.append(Message.user_text(_EMPTY_COMPLETION_NUDGE))
                    continue
                # Turn-loop invariants share ONE continuation nudge per turn: (a) NO-SILENT-END
                # (tool results but no assistant text since the last tool round) and (b)
                # BARE-GEOCODE (geocode_location was the only tool though data was asked for).
                # Skipped after an empty-completion retry; kill-switch TRID3NT_TURN_INVARIANTS=0.
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
                            Message.user_text(_CONTINUATION_NUDGE)
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
                # Fabrication backstop: only a turn that never called a tool, and only when the
                # closing text pairs a completed-action verb with a geospatial-output noun (see
                # looks_like_fabricated_action_claim). Local (openai) path only.
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
            _turn_tools_dispatched.update(c.name for c in turn_function_calls)

            # The round signature is fed to the watchdog AFTER dispatch with a progress witness;
            # recording late costs at most one extra identical round before the trip.
            _round_sig = [
                (c.name, json.dumps(c.args or {}, sort_keys=True, default=str))
                for c in turn_function_calls
            ]
            # Progress witness, OR'd across the round's calls; set True by a producing dispatch.
            _round_made_progress = False
            _round_had_failure = False
            _round_had_success = False

            # Closes the open bubble ONCE per round, BEFORE the round's tool cards land, so the
            # next text opens a bubble that interleaves after them.
            if current_message_id is not None:
                await _finalize_segment(
                    websocket, state, current_message_id, _segment_buf,
                    thinking_parts=_thinking_buf,
                )
                current_message_id = None  # next text opens a fresh segment

            for call in turn_function_calls:
                dispatch_error: BaseException | None = None
                result: Any = None
                _call_is_terminal_deliverable = False
                _tool_start = asyncio.get_running_loop().time()
                try:
                    # Per-session circuit breaker: short-circuit before dispatch if the tool has
                    # failed repeatedly this session.
                    if state.circuit_breaker.is_tripped(call.name):
                        remaining = state.circuit_breaker.cooldown_remaining_s(call.name)
                        raise CircuitBreakerError(call.name, remaining)
                    # A hallucinated name raises ToolNotFoundError here, surfaced to the model
                    # through summarize_tool_result(error=...).
                    result = await _invoke_tool_via_emitter(
                        websocket, state, call.name, call.args
                    )
                    # The tool has no websocket access; here the live socket is reachable, so the
                    # turn PAUSES for the drawn reply. Timeout/cancel/malformed draw all become a
                    # typed result, never a fabricated AOI.
                    if (
                        call.name == "request_spatial_input"
                        and isinstance(result, dict)
                        and result.get(SPATIAL_INPUT_SENTINEL_KEY) is True
                    ):
                        result = await _handle_request_spatial_input(
                            websocket, state, call.args or {}
                        )
                    # Snap the map at once, before any downstream layer publish. Best-effort.
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
                    # ANY tool result carrying a usable bbox / aoi_bbox snaps the camera (the model
                    # skips geocode_location when given coordinates); a repeat of the turn's last
                    # zoom-to extent is deduped.
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
                    # In addition to the function response: the client gets the full spec, the
                    # model a compact summary.
                    if is_chart_emission_result(result):
                        await _maybe_emit_chart(websocket, state, result)
                    state.circuit_breaker.record_success(call.name)
                    # Watchdog progress witness: a call that PRODUCED an artifact (layer, handle,
                    # feature set) resets the no-progress streak even when repeated; a bare ack
                    # does not.
                    _round_had_success = True
                    _call_made_progress = _dispatch_made_progress(result)
                    if _call_made_progress:
                        _round_made_progress = True
                    # A top-level run-a-model composer's artifact IS the answer: latch it.
                    _call_is_terminal_deliverable = (
                        _call_made_progress and _is_terminal_composer(call.name)
                    )
                    if _call_is_terminal_deliverable:
                        _deliverable_done = True
                        _post_deliverable_idle = 0
                    state.visible_tools.add(call.name)
                    # Search hits join this turn's gate and the Case visible set for later rounds;
                    # only real, registered, not-yet-visible names count toward the cap.
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
                    raise
                except Exception as exc:  # noqa: BLE001 -- surface to the model
                    if getattr(exc, "declined", False):
                        # The user answered a gate card with cancel. Nothing
                        # went wrong, so it is not logged as a fault.
                        logger.info(
                            "tool declined at a gate card session=%s tool=%s "
                            "code=%s",
                            state.session_id,
                            call.name,
                            getattr(exc, "error_code", None),
                        )
                    else:
                        logger.exception(
                            "tool dispatch raised session=%s tool=%s err=%s",
                            state.session_id,
                            call.name,
                            exc,
                        )
                    # The breaker counts ONLY upstream/transient faults: model-side arg errors must
                    # not trip it and block the corrected retry; CircuitBreakerError never recounts.
                    if not isinstance(exc, CircuitBreakerError):
                        state.circuit_breaker.record_failure(call.name, exc)
                    dispatch_error = exc
                    # A failed call is the breaker's territory, not the watchdog's.
                    _round_had_failure = True
                _tool_latency_ms = (asyncio.get_running_loop().time() - _tool_start) * 1000.0

                summary = summarize_tool_result(
                    call.name, result, error=dispatch_error
                )
                _uri_reg = get_uri_registry(state.session_id)
                # Only the LLM-facing summary shows short layer handles (L<n>) for registered URIs
                # (a raw URI echo is a hallucination surface); wire envelopes keep the real uri.
                summary = _uri_reg.rewrite_result_for_llm(summary)
                # Geocode drift warning. A successful geocode_location pins this turn's geocoded
                # bbox; any LATER call whose bbox arg intersects NEITHER that bbox NOR the active
                # AOI gets an advisory WARNING appended to its function_response (never blocks).
                if call.name == "geocode_location":
                    if dispatch_error is None and isinstance(result, dict):
                        _gc_bbox = as_bbox(result.get("bbox"))
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
                # Surface the layer handles this dispatch registered so the model passes HANDLES --
                # never raw storage paths -- into downstream *_uri params.
                _new_handles = _uri_reg.drain_announcements()
                if _new_handles and dispatch_error is None:
                    summary["layer_handles"] = {
                        _layer_id: (_uri_reg.short_for_uri(_uri) or _layer_id)
                        for _layer_id, _uri in _new_handles.items()
                    }
                    summary["layer_handles_note"] = (
                        "These layers are already on the user's map. Pass the "
                        "short handle (the L<n> value above) or the layer name "
                        "(the key) for any *_uri tool parameter — the server "
                        "resolves handles to the exact stored URIs. Do "
                        "NOT construct or echo s3:// paths or any other "
                        "storage URI."
                    )
                # Wrap-up directive: the model summarizes and STOPS instead of spinning to the cap.
                if _call_is_terminal_deliverable and isinstance(summary, dict):
                    summary["completion_directive"] = _DELIVERABLE_COMPLETE_DIRECTIVE
                logger.info(
                    "function-response queued session=%s iter=%d tool=%s summary_keys=%s",
                    state.session_id,
                    iterations,
                    call.name,
                    sorted(summary.keys()),
                )

                _io_step = (
                    state.emitter.last_tool_step
                    if state.emitter is not None
                    else None
                )
                # Guard against a STALE last_tool_step: a dispatch that raised BEFORE the emitter
                # created a step (ToolNotFoundError) leaves the prior tool's step on the accessor.
                # Only stamp IO when the recorded step is THIS tool's step.
                if _io_step is not None and _io_step.tool_name != call.name:
                    _io_step = None
                if _io_step is not None:
                    # The summary IS the verdict: a raised exception already
                    # stamps status="error" there, and a gate DECLINE stamps
                    # status="declined", which must not paint the row red.
                    _io_is_error = (
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

                # A workflow that swallowed its exception returns a failed envelope and raises
                # nothing, so success and error_code are also read off the summary status.
                _call_error_code: str | None = None
                _call_success = dispatch_error is None
                if dispatch_error is not None:
                    _call_error_code = str(
                        getattr(dispatch_error, "error_code", None)
                        or type(dispatch_error).__name__.upper()
                    )
                elif isinstance(summary, dict) and summary.get("status") == "error":
                    _call_success = False
                    _summary_code = summary.get("error_code")
                    _call_error_code = (
                        str(_summary_code) if _summary_code is not None else None
                    )
                logger.info("tool_call %s", json.dumps({
                    "session_id": state.session_id,
                    "turn_id": pipeline_id,
                    "tool_name": call.name,
                    "model_id": _effective_model,
                    "success": _call_success,
                    "error_code": _call_error_code,
                    "latency_ms": _tool_latency_ms,
                    "cached_content_token_count": (
                        last_usage.cached_content_token_count
                        if last_usage is not None else None),
                    "result_usable": classify_result_usable(
                        call.name, result, summary),
                }, default=str))
                _turn_tool_dispatch_count += 1
                contents.append(
                    Message.call(call.name, call.args, call.call_id)
                )
                contents.append(
                    Message.response(call.name, summary, call.call_id)
                )

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

            # A producing round or an all-failed/short-circuited round resets the streak, so the
            # watchdog never pre-empts loop-exhausted or CIRCUIT_BREAKER_TRIPPED.
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

            # After a delivery, idle rounds conclude the turn CLEANLY (a normal break, not
            # loop_exhausted); a producing round resets the streak. _agent_abort stays None.
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

        else:
            # Natural exhaustion of the step cap (guard aborts break with _agent_abort set). At
            # the full MAX_TURN_ITERATIONS it emits the loop_exhausted envelope clients rely on;
            # a cap tightened for a cheap tier is AGENT_STEP_LIMIT_REACHED. AGENT_LOOP_DETECTED
            # is reserved for the watchdog.
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
            if _agent_abort is None:
                await _send_loop_exhausted(websocket, state.session_id)
                # The client spins until a stream-closing done=True arrives.
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

        if _agent_abort is not None:
            _abort_code, _abort_msg = _agent_abort
            await _send_loop_exhausted(
                websocket, state.session_id, _abort_code, _abort_msg
            )
            # Mid-dispatch exit has no open segment, so the finalize below no-ops; the client
            # still needs a stream-closing done=True.
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
            # No open segment. If one was already streamed (_seg_done > 0) emit NOTHING: replaying
            # the narration would double the text. Otherwise recover the accumulated narration
            # EXACTLY as the model produced it (an empty turn emits no bubble).
            _seg_done = 0
            _cur_task = asyncio.current_task()
            if _cur_task is not None:
                _seg_done = _TURN_SEGMENTS_PERSISTED_BY_TASK.get(_cur_task, 0)
            _closing = "".join(turn_narration).strip()
            if _seg_done == 0 and _closing:
                recovered_id = new_ulid()
                # done=False here; the terminal done=True comes from ``_finalize_segment``.
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=recovered_id, delta=_closing, done=False
                        ),
                    )
                )
                # _finalize_segment joins its buffer arg, so the recovered text is passed as it.
                await _finalize_segment(
                    websocket,
                    state,
                    recovered_id,
                    [_closing],
                    is_terminal=True,
                    thinking_parts=_thinking_buf,
                )
            elif _crisp_concluded and _seg_done == 0:
                # Delivered, then concluded with ZERO narration: no branch above closed the stream.
                await _session_safe_send(websocket, state.session_id,
                    _new_envelope(
                        "agent-message-chunk",
                        state.session_id,
                        AgentMessageChunkPayload(
                            message_id=new_ulid(), delta="", done=True
                        ),
                    )
                )

        thinking_step = PipelineStep(
            step_id=step_id,
            name="llm_generation",
            tool_name="model_generate",
            state="complete",
        )
        await _session_safe_send(websocket, state.session_id,
            _new_envelope(
                "pipeline-state",
                state.session_id,
                PipelineStatePayload(pipeline_id=pipeline_id, steps=[thinking_step]),
            )
        )
        # Entry-captured list: after a mid-stream case switch this text must not leak into the
        # NEW Case's LLM context.
        turn_history.append({"role": "user", "text": user_text})
        if await _maybe_autoname_case(state, user_text):
            await _emit_case_list(websocket, state, force=True)

    except asyncio.CancelledError:
        # A cancelled step state, never failed; the open segment's done=True is NOT sent and
        # the dispatch wrapper's finally persists its un-finalized tail.
        _turn_error_class = "cancelled"
        cancelled_step = PipelineStep(
            step_id=step_id,
            name="llm_generation",
            tool_name="model_generate",
            state="cancelled",
        )
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
        # The CLIENT transport died mid-turn. This is NOT a model failure -- the LLM stream rides
        # the provider transport, never the client websocket, so a ConnectionClosed reaching this
        # scope can only be a residual raw send to the dead client socket.
        _turn_error_class = "client_disconnect"
        logger.warning(
            "client websocket closed mid-turn (transport drop, not a model "
            "failure) session=%s: %s",
            state.session_id,
            exc,
        )
    except ContextWindowExceededError as exc:
        # A local model's prompt was clipped by num_ctx even after one recompaction + retry:
        # a typed envelope, NOT the generic LLM_UNAVAILABLE bucket.
        _turn_error_class = "context_window"
        logger.warning(
            "context-budget: turn aborted, context window exceeded session=%s "
            "num_ctx=%d",
            state.session_id,
            exc.num_ctx,
        )
        # Persist the failure card BEFORE the error send: persist never touches the socket, so
        # a send failure cannot starve it. The fabrication backstop also applies on this path.
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
            # asyncio.CancelledError is a BaseException and deliberately propagates; the card
            # persist above has already completed.
            logger.exception(
                "context-budget: error-envelope send raised session=%s",
                state.session_id,
            )
    except UpstreamProviderError as exc:
        # The adapter already retried with backoff and exhausted its budget: end with an honest
        # provider-unavailable narration, never an internal error. The wire code stays the
        # contract-valid LLM_UNAVAILABLE; the failure card carries UPSTREAM_PROVIDER_UNAVAILABLE.
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
        # Persisted as an agent row so a Case reopen replays the same ending.
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
    except Exception as exc:  # noqa: BLE001 -- surface as LLM_UNAVAILABLE
        _turn_error_class = classify_provider_error_class(exc)
        logger.exception("model stream failed: %s", exc)
        await _send_error(
            websocket,
            state.session_id,
            "LLM_UNAVAILABLE",
            f"Model generation failed: {exc}",
            retryable=True,
        )
        # Replay reads chat_history, so persist the failed card or a spinning tool card replays
        # as "running" forever. A RuntimeError caused by StopIteration is async-generator
        # exhaustion, not a model failure: persisting would inject a phantom failure row.
        if not isinstance(exc.__cause__, StopIteration):
            await _persist_terminal_failure_card(
                state,
                error_code="LLM_UNAVAILABLE",
                message=f"Model generation failed: {exc}",
                case_id=_turn_case_id(state),
            )
    finally:
        try:
            logger.info("turn %s", json.dumps({
                "turn_id": pipeline_id,
                "session_id": state.session_id,
                "case_id": _turn_case_id(state),
                "model_id": _effective_model,
                "provider": _provider,
                "prompt_tokens": _turn_prompt_tokens,
                "completion_tokens": _turn_completion_tokens,
                "reasoning_tokens": _turn_reasoning_tokens,
                "turn_wall_ms": round((asyncio.get_running_loop().time()
                                       - started_at) * 1000.0, 1),
                "tool_dispatch_count": _turn_tool_dispatch_count,
                "error_class": _turn_error_class,
            }, default=str))
        except Exception:  # noqa: BLE001 -- the line never breaks the turn
            logger.warning("turn line failed session=%s", state.session_id,
                           exc_info=True)


async def _dispatch_model_turn_and_persist(
    websocket: ServerConnection,
    state: SessionState,
    settings: ModelSettings,
    user_text: str,
    model_id: str | None = None,
    show_thinking: bool = False,
) -> None:
    """Stream the model reply, then persist it to the active Case: the persisted
    content is the REAL accumulated narration, and a cancel or error still
    persists whatever the accumulator captured before the stream died."""
    # Captured at task entry: the finally-persist must land in the Case that OWNED this turn
    # even if Cases switch mid-stream. The bind makes every emitted envelope carry its case_id.
    turn_case_id = _turn_case_id(state)
    bind_turn_case(turn_case_id)
    # A concurrent turn re-points SessionState fields mid-stream, so completion is gauged
    # against THIS turn's history list and the narration list registered under the running task.
    turn_history = state.chat_history
    pre_chat_len = len(turn_history)
    try:
        await _stream_model_reply(
            websocket, state, settings, user_text,
            model_id=model_id,
            show_thinking=show_thinking,
        )
    finally:
        # Finalized segments were already persisted in-loop and must not be re-persisted: only
        # the un-finalized ``open_tail`` and the ``segments_done == 0`` fallback row are written
        # here. All per-task registries are popped.
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
        # Stashed by the ContextWindowExceededError handler; read and cleared once so it lands on
        # exactly one row and never leaks into a later turn.
        _abort_note = state.current_turn_context_abort_note
        state.current_turn_context_abort_note = None
        if turn_case_id:
            if open_tail:
                # Crash/cancel left an open segment (done=True never fired); as the de-facto
                # terminal row it also carries the layer/zoom accumulator.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content=(open_tail + _abort_note) if _abort_note else open_tail,
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
            elif segments_done == 0 and (narration or stream_completed or _abort_note):
                # No segment persisted and no open tail: write the single row (content possibly "")
                # so an abort note or a narration-less completed turn is not lost.
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
                # Invariant: EVERY turn that emitted a zoom-to/layer persists at least one chat row
                # carrying it; an empty marker row (no phantom bubble) snapshots the accumulator.
                await _persist_chat_turn(
                    state,
                    role="agent",
                    content="",
                    pipeline_id=state.current_turn_pipeline_id,
                    case_id=turn_case_id,
                )
        # Fires on EVERY exit so the client settles cards still ``running`` (their terminal frame
        # may have died on a dropped socket); outside the case guard so a Case-less turn idles too.
        await _emit_turn_complete(
            websocket, state, pipeline_id=state.current_turn_pipeline_id
        )
