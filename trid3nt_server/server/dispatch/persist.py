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

#: The closed ``ErrorCode`` Literal as a runtime set: the honesty-floor
#: catch-all uses it to tell a tool's OWN typed code from an out-of-enum code
#: that must be surfaced as a marker on INTERNAL_ERROR.
_VALID_ERROR_CODES: frozenset[str] = frozenset(get_args(ErrorCode))

#: Per-task narration-list registry, so a wrapper joins THIS turn's list even
#: when a concurrent turn has re-pointed the session's narration field. The
#: entry is registered in the synchronous prefix, so a crash or cancel still
#: leaves it, and the keys are weak, so an unpopped entry dies with its task.
_TURN_NARRATION_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task registry of the list backing the currently OPEN narration segment.
#: Each finalize clears that same list object rather than rebinding it, so the
#: wrapper always reads the live buffer; its finally persists the un-finalized
#: remainder as the tail row, so no narration is lost and a finalized segment is
#: never persisted twice.
_TURN_OPEN_SEGMENT_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, list[str]]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task count of narration SEGMENTS finalized and persisted this turn,
#: incremented only on a non-empty agent row. The wrapper's finally reads it to
#: decide whether the single marker row for a narration-less turn is still
#: needed, or whether the per-segment rows already carried the narration.
_TURN_SEGMENTS_PERSISTED_BY_TASK: "weakref.WeakKeyDictionary[asyncio.Task, int]" = (
    weakref.WeakKeyDictionary()
)

#: Per-task flag set True only when a row that snapshotted the turn's zoom-to
#: and layer accumulator was actually persisted. The wrapper's finally reads it
#: to decide whether a tool-terminal turn - one whose final round ended in tool
#: calls with no trailing narration - still needs a closing accumulator-bearing
#: marker row. An empty terminal segment leaves the accumulator unwritten, so
#: the marker is needed there too.
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
    """Close ONE narration bubble: send its terminal chunk, then persist the
    segment's text as its own ``role="agent"`` row so replay interleaves with
    the tool rows. An empty segment persists nothing."""
    # Only the TERMINAL segment passes ``layer_emissions=None``, so the turn's
    # layer and zoom-to accumulators are snapshotted onto the closing row alone
    # rather than duplicated across every segment. A thinking-only segment keeps
    # its buffer, so the reasoning attaches to the next persisted agent row
    # instead of being dropped.
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
            # A terminal non-empty segment row just snapshotted the turn's
            # zoom-to and layer accumulator, so the wrapper's finally must not
            # write a duplicate closing marker; the marker is only for the
            # tool-terminal shape, where this never fires.
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
    """Append one ``CaseChatMessage`` for the active Case; a missing binding or
    no active Case short-circuits and a failed write is logged, never raised.
    ``message_id`` upserts a stable row instead of appending a fresh one."""
    # ``case_id`` pins the target Case explicitly - the dispatch wrappers
    # capture it at task entry, so a cancel-and-redispatch race cannot re-aim
    # the write; omitted, it resolves through the turn's Case rather than the
    # raw write-time pointer. The upsert path is what lets a solve card
    # persisted ``running`` walk to its terminal state in the SAME row.
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
        # Reasoning text for the same bubble, None on every non-agent row.
        # Display replay ONLY: it is never rehydrated into model-bound contents.
        thinking=thinking,
        pipeline_id=pipeline_id,
        tool_card=tool_card,
        layer_emissions=(
            list(state.current_turn_layer_ids)
            if layer_emissions is None
            else list(layer_emissions)
        ),
        # Zoom-to emissions ride the rows that snapshot the accumulator, so a
        # Case reopen can replay the last one; tool rows carry none.
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
    """Persist one replayable tool-card row for the active Case, on a complete
    or failed dispatch; a cancelled dispatch persists nothing. Best-effort and
    never raises."""
    # Storage shape is ``CaseChatMessage(role="tool")`` in the same collection
    # as user and agent turns, so replay interleaves the stream by created_at
    # with no extra query; the typed ``tool_card`` is the integration path and
    # ``content`` is a JSON twin. Timing comes from the emitter's own stamps,
    # the wall-clock fallbacks only engaging when the wire died before the
    # terminal transition.
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
        # The persisted IO must ride the TYPED record, which is what replay
        # reads; the ``content`` JSON twin carries the identical values for
        # non-contract consumers. Computed only when at least one of raw_args /
        # function_response was provided, so a directive-path row stays IO-less
        # and existing documents validate unchanged.
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
        # Carry the ordered CHILD substeps the emitter snapshotted at the
        # terminal transition: the live steps are already cleared by then, so
        # the snapshot is the only source. The tool match guards against a stale
        # prior-dispatch snapshot attaching to this row.
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
            **_io_fields,  # typed IO on the record is the integration path
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
    """Persist a FAILED tool-card row for a terminal turn failure that did not
    flow through the dispatch path's own failed-card persist, so a reconnect
    never replays a card stuck ``running``. Best-effort, never raises."""
    # Honesty floor: this writes ONLY on a real terminal failure. The record
    # contract carries no error code, so the code and message ride the row
    # content and the label. Identity and timing prefer the emitter's last tool
    # step, falling back to a synthetic model-generation card.
    import json

    try:
        target_case = case_id if case_id is not None else _turn_case_id(state)
        if not target_case:
            return
        emitter_step = (
            state.emitter.last_tool_step if state.emitter is not None else None
        )
        # Identify the failing operation: the last live tool step when there is
        # one, else the model-generation step. Timing mirrors the live card so
        # the replayed failed card lands where the running one was, and the
        # captured child substeps ride along when the failing operation IS that
        # tool step; a pure model-stream failure has no children.
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
        # The JSON twin carries the typed record plus the error_code and message
        # the record contract cannot hold, so a non-contract replay consumer
        # still sees the failure reason.
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
    """Append a ``SessionChartRecord`` to the session document, keyed by the
    active Case when one is selected so the charts replay with its chat, else by
    the session; upsert, best-effort, and never raised."""
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
