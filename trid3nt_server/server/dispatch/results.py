"""Post-tool result handling: code-exec/chart emission, tool-dispatch persistence.

Auto-publishing a raster rides the ONE emission seam
(``emission/layer_uri_emit.publish_for_emission``). Nothing in the dispatch layer
decides whether a layer is visible: a second call site here would make visibility
depend on which path a result happened to take.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from trid3nt_server.tools.meta.code_exec_tool.code_exec_tool import CODE_EXEC_RESULT_KEY
from trid3nt_server.server.dispatch.persist import _persist_chart_record
from trid3nt_server.server.session.state import SessionState
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

async def _run_to_completion_shielded(coro: Awaitable[Any]) -> None:
    """Await ``coro`` so it COMPLETES even if the surrounding task is cancelled.

    DURABILITY (layer-publish-survives-disconnect): the per-tool dispatch
    ``finally`` persists the completed layer accumulator to the persistence backend. That
    ``finally`` runs on EVERY exit path -- including ``asyncio.CancelledError``
    (a same-stream re-prompt supersede, the stop button, or any cancel that
    reaches the detached turn). A bare ``await persist(...)`` in a ``finally``
    is NOT safe under cancellation: the first real suspension point inside the
    persist re-raises the pending ``CancelledError``, so the persistence write is
    SKIPPED and a fully-computed layer is lost -- a transient WS drop mid-solve
    would otherwise persist 0 layers despite a completed run that already wrote
    its COGs.

    The fix wraps the persist in a real task + ``asyncio.shield`` so a cancel
    of the parent does NOT cancel the write; if a ``CancelledError`` does arrive
    while we wait, we keep awaiting the shielded task to completion, THEN re-raise
    the cancellation (Invariant 8: the cancel still propagates, the write still
    lands). The persist coroutines swallow their own errors (never raise), so the
    only thing that can interrupt them is the parent cancel this guard absorbs.
    """
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
        # Invariant 8: the write landed; now honor the parent cancellation.
        raise asyncio.CancelledError

async def _maybe_emit_code_exec_result(
    websocket: ServerConnection,
    state: SessionState,
    code_exec_result: dict,
) -> None:
    """Emit a ``code-exec-result`` WS envelope.

    Called when ``code_exec_request`` returns a result carrying the full
    code-exec-result payload under ``_code_exec_result``
    (``is_code_exec_result(result)`` is True). Fires IN ADDITION to the
    standard ``function_response``:

    - ``code-exec-result`` -> the FULL result payload (status + stdout/stderr
      tails + the structured result descriptor + truncated flag + duration)
      for the client to render the result card. The function_response the model
      reads is the COMPACT summary (stripped by
      ``adapter.summarize_tool_result`` via the ``_code_exec_result`` key) so
      narration sources the structured ``result``, not the raw logs.

    Wire shape mirrors ``chart-emission``::

        {
          "type": "code-exec-result",
          "session_id": str,
          "payload": { ...full CodeExecResultPayload dict... }
        }

    Best-effort: never raised on a serialization/wire failure. Code-exec
    results are ephemeral (not persisted to the session ``charts`` array) --
    a re-opened Case replays chat + charts, not transient computations.
    """
    import json as _json

    payload = code_exec_result.get(CODE_EXEC_RESULT_KEY)
    if not isinstance(payload, dict):
        return
    try:
        await websocket.send(
            _json.dumps(
                {
                    "type": "code-exec-result",
                    "session_id": state.session_id,
                    "payload": payload,
                }
            )
        )
        logger.info(
            "code-exec-result emitted session=%s code_exec_id=%s status=%s truncated=%s",
            state.session_id,
            payload.get("code_exec_id"),
            payload.get("status"),
            payload.get("truncated"),
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.exception(
            "code-exec-result emission failed session=%s", state.session_id
        )

async def _maybe_emit_chart(
    websocket: ServerConnection,
    state: SessionState,
    chart_result: dict,
) -> None:
    """Emit a ``chart-emission`` WS envelope + persist the chart.

    Called when the generic chart tool (``generate_chart``) or an engine
    postprocessor returns a ChartEmissionPayload-shaped dict
    (``is_chart_emission_result(result)`` is True). Fires IN ADDITION to
    the standard ``function_response``:

    - ``chart-emission`` -> the FULL Vega-Lite spec for the client to render
      via vega-embed (inline stacked preview + gallery). The function_response
      the model reads is a COMPACT summary with the spec stripped
      (``adapter.summarize_tool_result``) so narration sources the numbers,
      not the inline rows.
    - ``SessionChartRecord`` persisted to the ``sessions`` collection so the
      chart replays on Case rehydration.

    ``created_turn_id`` is stamped here (from the per-turn pipeline id) when
    the tool did not set one, so the client groups charts from the same turn
    into one UI stack.

    Wire shape::

        {
          "type": "chart-emission",
          "session_id": str,
          "payload": { ...full ChartEmissionPayload dict... }
        }

    Best-effort: a serialization / wire / persistence failure is logged but
    never raised -- the ``function_response`` path must not be interrupted by
    a side-channel emission failure.
    """
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
    client sent no ``raw_text`` (older client / programmatic driver). The exact
    composer text is preferred (carries the user's literal syntax); this is the
    honest fallback so a Case reopen still shows an attributable invocation."""
    import json as _json

    if not args:
        return f"!run {name}"
    try:
        return f"!run {name} {_json.dumps(args, default=str)}"
    except Exception:  # noqa: BLE001
        return f"!run {name}"
