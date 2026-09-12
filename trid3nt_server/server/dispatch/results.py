"""Post-tool result handling: chart emission, plus the shielded persistence
await.

Auto-publishing a raster rides the one emission seam; nothing here decides
whether a layer is visible."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from trid3nt_server.server.dispatch.persist import _persist_chart_record
from trid3nt_server.server.session.state import SessionState
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

async def _run_to_completion_shielded(coro: Awaitable[Any]) -> None:
    """Await ``coro`` so it COMPLETES even if the surrounding task is cancelled,
    then re-raise the cancellation."""
    # A bare ``await`` in a ``finally`` is not safe under cancellation: the first
    # suspension point inside it re-raises the pending CancelledError and the
    # write is skipped, losing a fully computed layer on a mid-solve disconnect.
    # Shielding a real task keeps the write running; the cancel still propagates
    # afterwards. The persist coroutines swallow their own errors, so the parent
    # cancel absorbed here is the only thing that can interrupt them.
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                # The inner task itself was cancelled (not just our shield) --
                # nothing more to wait on; propagate.
                raise
            # Parent was cancelled but the shielded write is NOT cancelled.
            # Remember the cancel, and keep waiting on the still-running write
            # (the next loop awaits the same shielded task) so the persistence write
            # COMPLETES before the cancel propagates. If the write already
            # finished, the next ``await shield(task)`` returns immediately.
            cancelled = True
            continue
    if cancelled:
        # The write landed; now honor the parent cancellation.
        raise asyncio.CancelledError

async def _maybe_emit_chart(
    websocket: ServerConnection,
    state: SessionState,
    chart_result: dict,
) -> None:
    """Emit a ``chart-emission`` envelope beside the function response and
    persist the chart so it replays on Case rehydration; a wire or persistence
    failure is logged, never raised into the response path."""
    import json as _json

    payload = dict(chart_result)
    # Stamp the UI stack-grouping key from the current turn if the tool left it
    # unset, so charts from the same turn render as one stack (chart_contracts
    # ``created_turn_id`` semantics).
    if not payload.get("created_turn_id"):
        turn_id = (
            state.current_turn_pipeline_id
            or state.current_pipeline_id
            or state.session_id
        )
        payload["created_turn_id"] = turn_id

    try:
        await websocket.send(
            _json.dumps(
                {
                    "type": "chart-emission",
                    "session_id": state.session_id,
                    "payload": payload,
                }
            )
        )
        logger.info(
            "chart-emission emitted session=%s chart_id=%s title=%r",
            state.session_id,
            payload.get("chart_id"),
            payload.get("title"),
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.exception(
            "chart-emission emission failed session=%s", state.session_id
        )

    # Persist the chart so it replays on Case rehydration (best-effort).
    await _persist_chart_record(state, payload)

def _reconstruct_run_signature(name: str, args: dict) -> str:
    """A human ``!run <name>(...)`` line for the persisted user row when the
    client sent no raw text, so a Case reopen still shows an attributable
    invocation."""
    import json as _json

    if not args:
        return f"!run {name}"
    try:
        return f"!run {name} {_json.dumps(args, default=str)}"
    except Exception:  # noqa: BLE001
        return f"!run {name}"
