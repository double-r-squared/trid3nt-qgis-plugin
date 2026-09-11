"""Inbound non-turn envelope handlers: dev-invoke, secret-add, layer-delete + bg-task drain."""

from __future__ import annotations

import asyncio
import os
import logging
from trid3nt_contracts.secrets import SecretAddEnvelopePayload
from trid3nt_server.credentials.resolver import set_session_credential
from trid3nt_server.server.dispatch.emitter import _dispatch_tool_and_persist, _ensure_emitter
from trid3nt_server.server.dispatch.results import _reconstruct_run_signature
from trid3nt_server.server.session.case_state import _delete_case_loaded_layer, _turn_case_id
from trid3nt_server.server.session.state import SessionState, _ROOT_STREAM_KEY
from trid3nt_server.server.turn.engine import _prepare_user_turn
from trid3nt_server.server.turn.live_turn import _find_live_turn, _rebind_live_turns, _register_live_turn
from trid3nt_server.server.turn.wire import _send_error
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

#: Strong references to fire-and-forget background tasks: ``create_task`` holds
#: only a weak one, so an unreferenced task can be collected mid-flight. Each
#: detached task is added here and self-discards when it finishes.
_BG_TASKS: set[asyncio.Task] = set()

#: Bounded wall-clock budget for the graceful-shutdown drain of ``_BG_TASKS``.
#: A shutdown waits at most this long for outstanding detached tasks, so a
#: pathologically slow one is abandoned rather than hanging the exit.
_BG_DRAIN_TIMEOUT_S: float = float(
    os.environ.get("TRID3NT_BG_DRAIN_TIMEOUT_S", "10")
)

async def _drain_bg_tasks(
    timeout: float | None = None,
) -> None:
    """Flush any outstanding detached background tasks on shutdown, bounded by
    ``timeout`` so a pathologically slow task cannot hang the exit. Best-effort,
    and a no-op when nothing is pending."""
    pending = [t for t in _BG_TASKS if not t.done()]
    if not pending:
        return
    budget = timeout if timeout is not None else _BG_DRAIN_TIMEOUT_S
    logger.info("bg-task drain: flushing %d pending task(s)", len(pending))
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "bg-task drain: timed out after %.1fs with %d task(s) "
            "still pending",
            budget,
            sum(1 for t in pending if not t.done()),
        )
    except Exception:  # noqa: BLE001 - drain is best-effort, never blocks exit
        logger.exception("bg-task drain: unexpected error")

async def _handle_dev_tool_invoke(
    websocket: ServerConnection,
    state: SessionState,
    payload_dict: dict,
) -> None:
    """Run a client-parsed ``!run`` invocation OUTSIDE the model loop, through
    the SAME dispatch seam a model-issued call takes, so the gates, offload,
    layer materialization, card sidecar and turn-complete all ride identically."""
    # Wire-shape validation only, since the client already validated syntax: a
    # non-empty ``name`` and a dict of ``args``. An unknown tool routes through
    # the same not-found envelope a model call would produce.
    name = payload_dict.get("name")
    if not isinstance(name, str) or not name.strip():
        await _send_error(
            websocket,
            state.session_id,
            "TOOL_PARAMS_INVALID",
            "dev-tool-invoke: 'name' must be a non-empty string",
        )
        return
    name = name.strip()
    args = payload_dict.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        await _send_error(
            websocket,
            state.session_id,
            "TOOL_PARAMS_INVALID",
            "dev-tool-invoke: 'args' must be an object",
        )
        return
    raw_text = payload_dict.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raw_text = _reconstruct_run_signature(name, args)
    client_case_id = payload_dict.get("case_id")

    # Reset the per-turn accumulators BEFORE the turn scaffolding, mirroring the
    # user-message dispatch, so this manual turn's CaseChatMessage captures only
    # its own layer/pipeline emissions.
    state.current_turn_layer_ids = []
    state.current_turn_pipeline_id = None
    state.current_turn_map_commands = []

    # Case rebind, sync, turn pin and user-row persist: the ``!run`` line lands
    # as the user bubble so a replay is attributable, and a session with no Case
    # gets one auto-created.
    await _prepare_user_turn(
        websocket, state, raw_text, client_case_id=client_case_id
    )

    # Bind the emitter + rebind any live turns onto this socket, exactly as the
    # user-message path does before dispatching a turn task.
    _ensure_emitter(websocket, state)
    _rebind_live_turns(state.session_id, state.emitter)

    # Stream-scoped supersede: a manual invocation in the SAME stream cancels
    # that stream's in-flight turn, mirroring a re-prompt. Turns in other Cases
    # keep running.
    turn_key = state.current_turn_case_id or _ROOT_STREAM_KEY
    prior = state.inflight_tasks.get(turn_key)
    if prior is None or prior.done():
        prior = _find_live_turn(state.session_id, turn_key)
    if prior is not None and not prior.done():
        prior.cancel()
    for _done_key in [
        k for k, t in state.inflight_tasks.items() if t.done()
    ]:
        state.inflight_tasks.pop(_done_key, None)

    logger.info(
        "dev-tool-invoke dispatch session=%s tool=%s case=%s",
        state.session_id,
        name,
        state.current_turn_case_id,
    )
    task = asyncio.create_task(
        _dispatch_tool_and_persist(websocket, state, name, args, raw_text)
    )
    state.inflight_tasks[turn_key] = task
    _register_live_turn(state.session_id, turn_key, task, state.emitter)



async def _handle_secret_add(
    websocket: ServerConnection,
    state: SessionState,
    envelope: SecretAddEnvelopePayload,
) -> None:
    """Store a plugin-pushed credential VALUE in the in-memory session cache,
    keyed by session and provider; it is NEVER persisted, echoed back or logged.
    The user typing the key into the plugin IS the confirmation."""
    if not envelope.key_value:
        await _send_error(
            websocket,
            state.session_id,
            "TOOL_PARAMS_INVALID",
            "secret-add: key_value is empty",
        )
        return
    set_session_credential(state.session_id, envelope.provider, envelope.key_value)

async def _handle_layer_delete(
    websocket: ServerConnection,
    state: SessionState,
    payload_dict: Any,
) -> None:
    """Process a ``layer-delete`` envelope: drop the layer from the live
    accumulator, emit a refreshed session state, and persist the survivors
    authoritatively. A missing ``layer_id`` is a typed params error."""
    layer_id: str | None = None
    if isinstance(payload_dict, dict):
        lid = payload_dict.get("layer_id")
        if isinstance(lid, str) and lid:
            layer_id = lid
    if not layer_id:
        await _send_error(
            websocket,
            state.session_id,
            "TOOL_PARAMS_INVALID",
            "layer-delete requires a non-empty string layer_id.",
        )
        return

    # Pin the target Case the same way every persistence site does so a
    # mid-turn Case switch never mis-aims the delete.
    target_case = _turn_case_id(state)

    _ensure_emitter(websocket, state)
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return

    # Drop the layer from the live accumulator. reset_loaded_layers also
    # prunes the inline-GeoJSON side-table to the surviving ids.
    survivors: list[dict] = [
        layer.model_dump(mode="json")
        for layer in state.emitter.loaded_layers
        if layer.layer_id != layer_id
    ]
    state.emitter.reset_loaded_layers(survivors)

    # Re-inline surviving vectors BEFORE the emit, so a delete never transiently
    # drops sibling vector layers: only ids already in the inline side-table get
    # an inline payload, and a vector layer with none cannot render. The call is
    # idempotent, so a full side-table makes this a cheap no-op.
    try:
        await state.emitter.reinline_vector_layers()
    except Exception:  # noqa: BLE001 -- re-inline is best-effort
        logger.warning(
            "layer-delete vector re-inline failed session=%s case=%s",
            state.session_id,
            target_case,
        )

    # Emit the refreshed session state: the client replaces rather than
    # reconciles, so the now-absent layer disappears, and the fan-out is
    # session-scoped, so every connection converges on the new list.
    await state.emitter.emit_session_state()

    # Persist authoritatively (replace, not the union merge -- see helper).
    await _delete_case_loaded_layer(state, layer_id, case_id=target_case)

    logger.info(
        "layer-delete session=%s case=%s layer=%s survivors=%d",
        state.session_id,
        target_case,
        layer_id,
        len(survivors),
    )
