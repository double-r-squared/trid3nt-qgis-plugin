"""Chat/tool-card/chart persistence joins + per-turn narration registries."""

from __future__ import annotations

import asyncio
import weakref
import logging
from datetime import datetime
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.case import CaseChatMessage, ToolCardRecord
from trid3nt_contracts.ws import AgentMessageChunkPayload, ErrorCode
from trid3nt_server.emission.pipeline_emitter import _json_for_tool_io
from trid3nt_server.server.session.case_state import _touch_session_record, _turn_case_id
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.turn.wire import _new_envelope, _session_safe_send
from typing import Any, get_args
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

#: The closed A.6 ``ErrorCode`` Literal, as a runtime set -- the honesty-floor
#: catch-all uses it to tell a tool's OWN typed code (a valid wire code) from an
#: out-of-enum code that must be surfaced as a ``[MARKER]`` on INTERNAL_ERROR.
_VALID_ERROR_CODES: frozenset[str] = frozenset(get_args(ErrorCode))

#: Per-task narration-list registry. ``_stream_model_reply`` registers its
#: turn's narration list under the running asyncio task (in the synchronous
#: prefix, so crash/cancel still leaves the entry) and
#: ``_dispatch_model_turn_and_persist`` pops it in its finally -- the wrapper then
#: joins THIS turn's list even when a concurrent turn has re-pointed
#: ``state.current_turn_narration``. Weak keys: an entry whose task was
#: never popped (direct stream callers) vanishes with the task, no leak.
_TURN_NARRATION_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task OPEN-segment registry. ``_stream_model_reply`` registers the
#: list backing the currently open narration segment (received text not yet
#: finalized). On each finalize the in-loop code ``.clear()``s this same
#: list object (never rebinds it) so the wrapper always reads the live open
#: buffer. ``_dispatch_model_turn_and_persist`` pops it in its finally and
#: persists the un-finalized remainder as the tail row, so no narration is
#: lost and finalized segments are never double-persisted.
_TURN_OPEN_SEGMENT_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task count of narration SEGMENTS finalized+persisted this turn.
#: ``_finalize_segment`` increments it only when it actually writes a
#: non-empty ``role="agent"`` row. The wrapper's finally reads it to decide
#: whether the legacy single marker row (narration-less completed turn)
#: still needs writing (segments_done == 0) or whether the per-segment rows
#: already carried the narration (segments_done > 0 -> skip the marker).
_TURN_SEGMENTS_PERSISTED_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, int]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task flag set True ONLY when a row that snapshotted the turn's
#: zoom-to/layer accumulator was actually persisted -- i.e. the in-loop
#: TERMINAL ``_finalize_segment`` wrote a non-empty ``role="agent"`` row
#: (``is_terminal=True`` -> ``layer_emissions=None`` -> ``_persist_chat_turn``
#: snapshots ``current_turn_layer_ids`` + ``current_turn_map_commands``).
#: The wrapper's finally reads it to decide whether a tool-terminal turn
#: (final round ended in tool calls with no trailing narration -> no
#: terminal finalize fired -> accumulator orphaned) still needs a closing
#: accumulator-bearing marker row so the Case-reopen zoom-snap
#: (``extractLastZoomTo``) + layer attribution survive. Not set when the
#: terminal segment was empty/whitespace -- that turn's accumulator is
#: likewise unwritten and the marker is needed.
_TURN_TERMINAL_ACC_PERSISTED_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, bool]" = (
    weakref.WeakKeyDictionary()
)

async def _finalize_segment(
    websocket: ServerConnection,
    state: SessionState,
    message_id: str,
    segment_parts: list[str],
    *,
    is_terminal: bool = False,
    thinking_parts: list[str] | None = None,
) -> None:
    """Close ONE narration bubble + persist it as its own agent row.

    Each contiguous run of agent text between tool-call rounds is a SEGMENT.
    Closing a segment does two things at the boundary "agent text is about to
    be interrupted by tool cards (or the turn is ending)":

    (1) Send the terminal ``done=True`` ``agent-message-chunk`` for THIS
        bubble's ``message_id`` so the live client marks the bubble complete
        (web ``appendDelta`` sets ``done``). This MUST only fire for an id
        that already received text -- the caller guarantees that by only
        calling here when ``current_message_id is not None``.
    (2) Persist a ``role="agent"`` ``CaseChatMessage`` carrying ONLY this
        segment's text, so the persisted row order interleaves with the
        mid-turn tool rows (``_persist_tool_card``) and the replay
        reconstructs the live interleaved train. An empty segment persists
        NOTHING (no phantom bubble on replay; no row-count regression).

    ``layer_emissions``: non-terminal segments pass ``[]`` so they do NOT each
    duplicate the whole-turn ``current_turn_layer_ids`` /
    ``current_turn_map_commands`` accumulators. The TERMINAL segment
    (``is_terminal=True`` -- the final narration run of the turn) passes
    ``None`` so ``_persist_chat_turn`` snapshots the accumulators onto it,
    keeping layer attribution + zoom-to on the de-facto closing row.

    Best-effort persist (inherits ``_persist_chat_turn``'s swallow); the wire
    ``done=True`` still fires even if persistence is unbound. Clears the
    segment buffer and bumps the per-task finalized-count on a non-empty
    write.

    ``thinking_parts``: the per-segment reasoning-text buffer accumulated
    while the per-turn ``show_thinking`` toggle was ON. When THIS segment
    persists a non-empty row, its joined text rides the row's ``thinking``
    field (same-bubble contract) and the buffer is cleared. A thinking-only
    segment (no answer text -> no row, the no-phantom-bubble invariant)
    KEEPS its buffer so the thinking attaches to the turn's next persisted
    agent row instead of being dropped. Same clear-not-rebind discipline as
    ``segment_parts``.
    """
    text = "".join(segment_parts).strip()
    # (1) wire terminal for this bubble -- always fires (id has text).
    await _session_safe_send(websocket, state.session_id,
        _new_envelope(
            "agent-message-chunk",
            state.session_id,
            AgentMessageChunkPayload(message_id=message_id, delta="", done=True),
        )
    )
    # (2) per-segment persist -- only when there is real text.
    if text:
        thinking_text = (
            "".join(thinking_parts).strip() if thinking_parts else ""
        )
        await _persist_chat_turn(
            state,
            role="agent",
            content=text,
            pipeline_id=state.current_turn_pipeline_id,
            # Terminal segment owns the layer/zoom attribution; non-terminal
            # segments carry none (the accumulator rides the last row only).
            layer_emissions=None if is_terminal else [],
            case_id=_turn_case_id(state),
            thinking=thinking_text or None,
        )
        # Thinking consumed by this row -- clear the SAME list object (do not
        # rebind), mirroring the segment-buffer discipline below.
        if thinking_parts:
            thinking_parts.clear()
        _task = asyncio.current_task()
        if _task is not None:
            _TURN_SEGMENTS_PERSISTED_BY_TASK[_task] = (
                _TURN_SEGMENTS_PERSISTED_BY_TASK.get(_task, 0) + 1
            )
            # A TERMINAL non-empty segment row just
            # snapshotted the turn's zoom-to/layer accumulator
            # (``layer_emissions=None`` above). Record that so the wrapper's
            # finally does NOT also write a duplicate closing marker row -- the
            # marker is ONLY for the tool-terminal shape where this never fires.
            if is_terminal:
                _TURN_TERMINAL_ACC_PERSISTED_BY_TASK[_task] = True
    # The open buffer is now closed: clear the SAME list object (do not rebind)
    # so the task-registered open buffer the wrapper reads is always current.
    segment_parts.clear()

async def _persist_chat_turn(
    state: SessionState,
    *,
    role: str,
    content: str,
    pipeline_id: str | None = None,
    tool_card: ToolCardRecord | None = None,
    layer_emissions: list[str] | None = None,
    case_id: str | None = None,
    message_id: str | None = None,
    thinking: str | None = None,
) -> None:
    """Append one ``CaseChatMessage`` to Mongo for the active Case.

    Best-effort: a missing Persistence binding OR no active Case context
    short-circuits (the in-memory chat keeps working). A failed write
    is logged but not raised -- chat persistence is a side-effect, not the
    happy path of message delivery.

    The chat-message collection is part of the
    agent's own session record (it is per-turn replay material, not a
    solver result), so this write does NOT pause for user approval.

    ``tool_card`` carries the typed ``ToolCardRecord`` for ``role="tool"``
    rows; ``layer_emissions`` overrides the default per-turn accumulator
    snapshot (tool rows pass ``[]`` so the turn's layer ids stay attributed
    to the closing agent row).

    ``case_id`` pins the target Case explicitly (the dispatch wrappers
    capture it at task entry so even a cancel-and-redispatch race cannot
    re-aim the write); when omitted it resolves via ``_turn_case_id`` --
    never the raw write-time ``active_case_id``.

    Durable-card lifecycle: ``message_id``, when supplied, pins the row's
    stable id and routes the write through ``upsert_chat_message``
    (insert-or-replace) instead of ``append_chat_message`` -- so a SOLVE
    card persisted ``running`` at mint can be UPDATED IN PLACE to its
    terminal state without a duplicate row. Omitted (the default) keeps the
    append-a-fresh-row behavior every existing caller relies on.
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    if not target_case:
        return
    p = get_persistence()
    if p is None:
        return
    msg = CaseChatMessage(
        message_id=message_id or new_ulid(),
        case_id=target_case,
        role=role,  # type: ignore[arg-type]
        content=content,
        # Thinking persistence: reasoning-channel text
        # for the same bubble; None on every non-agent row and on turns with
        # show_thinking off. Display replay ONLY -- never rehydrated into
        # LLM-bound contents (adapter.NEVER_REHYDRATE_FIELDS).
        thinking=thinking,
        pipeline_id=pipeline_id,
        tool_card=tool_card,
        layer_emissions=(
            list(state.current_turn_layer_ids)
            if layer_emissions is None
            else list(layer_emissions)
        ),
        # Persist the turn's zoom-to emissions (geocode snap) on
        # rows that snapshot the accumulator (agent/user rows) -- the
        # Case-reopen snap-to-location replays the LAST one (web).
        # Tool rows pass layer_emissions=[] and get [] here too.
        map_command_emissions=(
            list(state.current_turn_map_commands)
            if layer_emissions is None
            else []
        ),
        created_at=now_utc(),
    )
    try:
        if message_id is not None:
            # Durable-card lifecycle: insert-or-replace the SAME row so a
            # running card walks to terminal in place (no duplicate).
            await p.upsert_chat_message(msg)
        else:
            await p.append_chat_message(msg)
        # Per-turn session heartbeat: the chat turn is the
        # activity signal that keeps the session record's TTL fresh and
        # the turn's Case registered in ``project_ids``.
        await _touch_session_record(state, case_id=target_case)
        logger.debug(
            "chat-persist session=%s case=%s role=%s msg_id=%s pipeline_id=%s layers=%d",
            state.session_id,
            target_case,
            role,
            msg.message_id,
            pipeline_id,
            len(msg.layer_emissions),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "chat-persist failed session=%s case=%s role=%s",
            state.session_id,
            target_case,
            role,
        )

async def _persist_tool_card(
    state: SessionState,
    *,
    tool_name: str,
    label: str,
    card_state: str,
    started_at_fallback: datetime,
    duration_ms_fallback: int,
    case_id: str | None = None,
    raw_args: Any = None,
    function_response: Any = None,
    io_is_error: bool = False,
    message_id: str | None = None,
) -> None:
    """Persist one replayable tool-card row for the active Case.

    Written by ``_invoke_tool_via_emitter`` on every terminal tool dispatch
    (complete OR failed; cancelled dispatches persist nothing -- Invariant
    8). Storage shape: ``CaseChatMessage(role="tool")`` in the SAME chat
    collection as user/agent turns, so the rehydration replay interleaves
    the full stream by ``created_at`` with zero extra queries. The typed
    payload is ``tool_card`` (``ToolCardRecord``); ``content`` carries the
    identical record as a JSON string for non-contract consumers.

    Timing source of truth: the emitter's ``last_tool_step`` (the
    authoritative ``started_at`` / ``duration_ms`` stamps the live card
    displayed). The wall-clock fallbacks only engage when the emitter stamp
    is unavailable (e.g. the wire died before the terminal transition).

    Tool-card IO persistence: when ``raw_args`` / ``function_response`` are
    supplied, the SAME input args + output response the live ``tool-io``
    sidecar carries (``PipelineEmitter.emit_tool_io``) are serialized with
    the SAME helper (``_json_for_tool_io`` -- identical truncation/byte
    semantics) and populated on the TYPED ``ToolCardRecord`` under the EXACT
    live ``ToolIoPayload`` field names -- ``raw_args`` / ``function_response``
    / ``args_truncated`` / ``response_truncated`` / ``args_bytes`` /
    ``response_bytes`` / ``is_error`` (all optional/nullable).
    ``get_session_state`` replay carries them on ``m.tool_card``; the web
    renderer rehydrates the tool-card expander on Case reopen by reading
    them off the TYPED record -- the ``content`` JSON twin carries the
    identical values for non-contract consumers but is not the integration
    path.

    Best-effort, never raises: record construction is wrapped here and the
    underlying ``_persist_chat_turn`` already swallows write failures.
    """
    try:
        started_at = started_at_fallback
        duration_ms: int = max(0, int(duration_ms_fallback))
        emitter_step = (
            state.emitter.last_tool_step if state.emitter is not None else None
        )
        if emitter_step is not None and emitter_step.tool_name == tool_name:
            if emitter_step.started_at is not None:
                started_at = emitter_step.started_at
            if emitter_step.duration_ms is not None:
                duration_ms = emitter_step.duration_ms
        # The persisted IO must ride the TYPED ``ToolCardRecord`` -- replay
        # reads it off ``m.tool_card``, NOT the row ``content`` JSON.
        # Compute the IO ONCE with the SAME ``_json_for_tool_io`` helper +
        # field names the live ``tool-io`` sidecar uses, populate the typed
        # record's IO fields, and keep the identical values on the
        # ``content`` JSON twin for non-contract consumers. Only when at
        # least one of raw_args/function_response was provided (the
        # LLM-dispatch path) -- the /invoke directive path passes neither,
        # so its rows stay IO-less and the typed record's IO fields default
        # to ``None`` (existing documents validate + replay unchanged).
        _io_fields: dict[str, Any] = {}
        if raw_args is not None or function_response is not None:
            args_str, args_trunc, args_bytes = _json_for_tool_io(raw_args)
            resp_str, resp_trunc, resp_bytes = _json_for_tool_io(function_response)
            _io_fields = {
                "raw_args": args_str,
                "function_response": resp_str,
                "args_truncated": args_trunc,
                "response_truncated": resp_trunc,
                "args_bytes": args_bytes,
                "response_bytes": resp_bytes,
                "is_error": bool(io_is_error),
            }
        # Carry the ordered CHILD substeps captured by the emitter at this
        # dispatch's terminal transition. The emitter snapshots them onto
        # ``last_tool_children`` WHILE the children still exist in ``_steps``
        # -- ``close_pipeline`` (run just before this hook) has already
        # cleared ``_steps``, so this durable snapshot is the only source.
        # Reading it onto ``ToolCardRecord.children`` makes the nested
        # timeline replay READ-ONLY on a Case reopen (additive JSON -- a card
        # with no children stays ``None``). Guard the tool match so a stale
        # prior-dispatch snapshot can never attach to this row.
        _children: list | None = None
        emitter_children = (
            state.emitter.last_tool_children if state.emitter is not None else None
        )
        if (
            emitter_children
            and emitter_step is not None
            and emitter_step.tool_name == tool_name
        ):
            _children = list(emitter_children)
        record = ToolCardRecord(
            tool_name=tool_name,
            state=card_state,  # type: ignore[arg-type]
            started_at=started_at,
            duration_ms=duration_ms,
            label=label,
            children=_children,
            **_io_fields,  # C1: typed IO on the record == the integration path
        )
        # Content JSON twin: model_dump_json now already carries the IO fields
        # (they live on the typed record), so a single dump matches the wire
        # shape for non-contract consumers without a separate merge.
        content = record.model_dump_json()
        await _persist_chat_turn(
            state,
            role="tool",
            content=content,
            pipeline_id=state.current_turn_pipeline_id,
            tool_card=record,
            layer_emissions=[],
            case_id=case_id,
            message_id=message_id,
        )
    except Exception:  # noqa: BLE001 -- replay material, never the happy path
        logger.exception(
            "tool-card persist failed session=%s case=%s tool=%s",
            state.session_id,
            case_id if case_id is not None else _turn_case_id(state),
            tool_name,
        )

async def _persist_terminal_failure_card(
    state: SessionState,
    *,
    error_code: str,
    message: str,
    case_id: str | None = None,
) -> None:
    """Persist a ``role="tool"`` FAILED tool-card row for a terminal turn
    failure that did NOT flow through ``_invoke_tool_via_emitter``'s own
    failed-card persist.

    A terminal solve/tool failure must surface to the user even across a
    socket cycle -- otherwise a WS reconnect / Case-reopen replays the last
    tool card stuck in its ``running`` state forever.

    Writes the SAME ``role="tool"`` ``CaseChatMessage`` + ``ToolCardRecord``
    shape ``_persist_tool_card`` produces, with ``state="failed"``. The
    ``ToolCardRecord`` contract (case.py) has no error_code/message fields,
    so the A.6 ``error_code`` + human message ride in the row ``content`` (a
    JSON twin, exactly like the complete-card content) and the ``label`` so
    the web replay surfaces the failure reason. Honesty floor: this writes
    ONLY on a real terminal failure -- it never fabricates a success.

    Prefers the emitter's authoritative ``last_tool_step`` for the failing
    tool's identity + timing (so the persisted failed card matches the live
    card the user last saw spinning); falls back to a synthetic
    ``llm_generation`` card when no tool step is available (a pure
    model-stream failure with no in-flight tool). Best-effort, never raises.
    """
    import json

    try:
        target_case = case_id if case_id is not None else _turn_case_id(state)
        if not target_case:
            return
        emitter_step = (
            state.emitter.last_tool_step if state.emitter is not None else None
        )
        # Identify the failing operation: the last live tool step (the solve
        # / tool the user saw running) when present, else the model-
        # generation step. ``duration_ms`` / ``started_at`` mirror the live
        # card so the replayed failed card lands where the running one was.
        # When the failing operation IS the last live tool step, carry its
        # captured child substeps so the replayed failed card still nests
        # its sub-step timeline; the synthetic ``model_generate`` branch (a
        # pure model-stream failure, no in-flight tool) has no children.
        _children: list | None = None
        if emitter_step is not None and emitter_step.tool_name:
            tool_name = emitter_step.tool_name
            label = emitter_step.name or emitter_step.tool_name
            started_at = emitter_step.started_at or now_utc()
            duration_ms = emitter_step.duration_ms
            emitter_children = (
                state.emitter.last_tool_children
                if state.emitter is not None
                else None
            )
            if emitter_children:
                _children = list(emitter_children)
        else:
            tool_name = "model_generate"
            label = "llm_generation"
            started_at = now_utc()
            duration_ms = 0
        record = ToolCardRecord(
            tool_name=tool_name,
            state="failed",
            started_at=started_at,
            duration_ms=duration_ms,
            # Surface the failure reason in the human-facing label so the
            # replayed card explains WHY it failed.
            label=f"{label} — {error_code}",
            children=_children,
        )
        # The JSON-twin content carries the typed record PLUS the error_code +
        # message (the record contract cannot hold them) so non-contract
        # replay consumers still see the failure reason.
        content_payload = json.loads(record.model_dump_json())
        content_payload["error_code"] = error_code
        content_payload["message"] = message
        await _persist_chat_turn(
            state,
            role="tool",
            content=json.dumps(content_payload),
            pipeline_id=state.current_turn_pipeline_id,
            tool_card=record,
            layer_emissions=[],
            case_id=target_case,
        )
        logger.info(
            "terminal-failure card persisted session=%s case=%s tool=%s code=%s",
            state.session_id,
            target_case,
            tool_name,
            error_code,
        )
    except Exception:  # noqa: BLE001 -- replay material, never the happy path
        logger.exception(
            "terminal-failure card persist failed session=%s case=%s code=%s",
            state.session_id,
            case_id if case_id is not None else _turn_case_id(state),
            error_code,
        )

async def _persist_chart_record(state: SessionState, payload: dict) -> None:
    """Append a ``SessionChartRecord`` to the session document.

    Resolves the ``Persistence`` singleton and ``$push``es the record onto the
    session document's append-only ``charts`` array via the underlying MCP
    ``update-one`` call (charts go directly on the MCP client like telemetry,
    keeping the Persistence public API narrow).

    Keyed by the active Case id when one is selected (so charts replay on Case
    rehydration via the same document the chat history lives on), else by the
    session id. ``upsert=True`` so the first chart on a fresh session document
    creates it.

    Never raises -- a persistence failure is logged at WARNING. This is only
    the write half of the contract; replay is session-resume scope.
    """
    persistence = get_persistence()
    if persistence is None:
        # In-memory / no-persistence path: charts live only in-flight.
        logger.debug(
            "chart persistence skipped (no Persistence bound) session=%s",
            state.session_id,
        )
        return

    try:
        from trid3nt_contracts.chart_contracts import (
            ChartEmissionPayload,
            SessionChartRecord,
        )
        from trid3nt_server.persistence import DEFAULT_DATABASE, SESSIONS_COLLECTION

        # Charts are turn-scoped emissions -- key them by the Case
        # that OWNS the turn, not whatever Case is visible at write time.
        doc_id = _turn_case_id(state) or state.session_id
        record = SessionChartRecord(
            session_id=doc_id,
            payload=ChartEmissionPayload.model_validate(payload),
            emitted_at=now_utc(),
        )
        body = record.model_dump(mode="json")
        await persistence._store.call_tool(  # noqa: SLF001 -- telemetry-writer pattern
            "update-one",
            {
                "database": DEFAULT_DATABASE,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": doc_id},
                "update": {"$push": {"charts": body}},
                "upsert": True,
            },
        )
        logger.info(
            "chart persisted session=%s doc_id=%s chart_id=%s",
            state.session_id,
            doc_id,
            payload.get("chart_id"),
        )
    except Exception:  # noqa: BLE001 -- persistence must not break the loop
        logger.warning(
            "chart persistence failed session=%s chart_id=%s",
            state.session_id,
            payload.get("chart_id"),
            exc_info=True,
        )
