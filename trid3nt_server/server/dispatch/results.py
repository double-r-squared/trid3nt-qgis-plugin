"""Post-tool result handling: auto-publish rasters, code-exec/chart emission, tool-dispatch persistence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from trid3nt_contracts.execution import LayerURI
from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.meta.code_exec_tool.code_exec_tool import CODE_EXEC_RESULT_KEY
from trid3nt_server.emission.layer_uri_emit import emit_layer_uri
from trid3nt_server.server.dispatch.persist import _persist_chart_record
from trid3nt_server.server.session.case_state import _persist_case_loaded_layers
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.styles import _resolve_publish_wrap_style_preset
from trid3nt_server.server.turn.wire import _send_error
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

async def _auto_publish_droppable_raster(
    websocket: ServerConnection,
    state: SessionState,
    *,
    layer: LayerURI,
    case_id: str | None,
) -> None:
    """Deterministically publish + render a droppable object-store raster.

    ``layer`` is exactly the class ``emit_layer_uri`` DROPS -- a renderable
    raster carrying a raw ``s3://``/``gs://`` uri MapLibre cannot fetch. Calls
    ``publish_layer`` server-side, off the asyncio loop (no-sync-blocking
    norm), and feeds the resulting published uri (an http(s) face, or the raw
    ``s3://`` COG on the QGIS-native path) through the SAME
    ``emit_layer_uri`` -> ``add_loaded_layer`` -> persist machinery the
    publish_layer wrap-site uses, so dedup/z-index/snapshot/manifest behave
    identically (an LLM publish of the SAME COG merges by COG identity -- no
    double-add).

    Honesty floor: on FAILURE (raises, or returns neither an http(s) URL nor
    an s3:// COG uri) surfaces a typed ``LAYER_AUTO_PUBLISH_FAILED`` error --
    never a silent green. The raw ``s3://`` COG uri is a SUCCESS shape for
    rasters (the plugin reads it via /vsicurl/), accepted alongside http(s).
    The LLM-visible tool result is left UNCHANGED so retry-on-failure
    narration can act. Best-effort: never raises, so it cannot break the
    dispatch.
    """
    publish_entry = TOOL_REGISTRY.get("publish_layer")
    if publish_entry is None:  # pragma: no cover - publish_layer always present
        logger.warning(
            "auto-publish: publish_layer not in registry; cannot render "
            "raster layer_id=%s uri=%s",
            layer.layer_id,
            layer.uri,
        )
        return

    style_preset = _resolve_publish_wrap_style_preset(
        style_preset=layer.style_preset,
        layer_uri=layer.uri,
        layer_id=layer.layer_id,
    )

    try:
        # publish_layer is synchronous (polls PyQGIS); run it OFF the
        # event loop so it cannot stall the WS heartbeat. The server wrapper
        # normally resolves the case-scoped .qgs for publish_layer; here we pass
        # case_id straight through so the same per-Case routing applies inside
        # the tool body.
        published_url = await asyncio.to_thread(
            publish_entry.fn,
            layer_uri=layer.uri,
            layer_id=layer.layer_id,
            style_preset=style_preset or None,
            case_id=case_id,
        )
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - classify into honesty floor
        logger.exception(
            "auto-publish: publish_layer FAILED layer_id=%s uri=%s",
            layer.layer_id,
            layer.uri,
        )
        await _emit_auto_publish_failure(
            websocket, state, layer=layer, reason=str(exc) or exc.__class__.__name__
        )
        return

    # Honesty floor: publish_layer's SUCCESS shapes are an http(s) URL (a
    # WMS/durable-GeoJSON face) or the raw s3:// COG uri (QGIS-native raster
    # publish; the plugin reads it via /vsicurl/). Anything else -- empty/None,
    # an error string, gs://, file:// -- is NOT a renderable layer: never add
    # it + narrate success.
    if not (
        isinstance(published_url, str)
        and published_url.startswith(("http://", "https://", "s3://"))
    ):
        logger.warning(
            "auto-publish: publish_layer returned a non-renderable value for "
            "layer_id=%s uri=%s -> %r; treating as render failure",
            layer.layer_id,
            layer.uri,
            published_url,
        )
        await _emit_auto_publish_failure(
            websocket,
            state,
            layer=layer,
            reason=(
                "publish_layer did not return a renderable http(s) URL or "
                "s3:// COG uri"
            ),
        )
        return

    # Success: route the published uri (http(s) face or raw s3:// COG) through
    # the SINGLE emission seam (it passes both through untouched) and the
    # existing add_loaded_layer machinery. The published layer keeps the
    # producing layer's id/name so the COG-identity dedup collapses a later LLM
    # re-publish of the same COG into this same row.
    try:
        _emit_layer = emit_layer_uri(
            LayerURI(
                layer_id=layer.layer_id,
                name=layer.name,
                layer_type="raster",
                uri=published_url,
                style_preset=style_preset,
                role=layer.role,
                units=layer.units,
                bbox=layer.bbox,
            )
        )
        if _emit_layer is None:  # pragma: no cover - http/s3 never drops
            return
        await state.emitter.add_loaded_layer(_emit_layer)
        # Track the layer on the active turn so the closing CaseChatMessage
        # captures it (mirrors the publish_layer wrap-site).
        if layer.layer_id:
            state.current_turn_layer_ids.append(layer.layer_id)
        # Re-persist AFTER this add: the dispatch finally-persist ran BEFORE this
        # auto-publish, so without re-persisting the rendered layer would live
        # only in memory and a Case reopen would rehydrate without it (the exact
        # publish_layer-wrap-site durability concern). Shielded so a parent cancel
        # cannot interrupt the write; each persist swallows its own errors.
        if case_id:
            await _run_to_completion_shielded(
                _persist_case_loaded_layers(state, case_id=case_id)
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - emission/persist is best-effort
        logger.exception(
            "auto-publish: rendered-layer emission failed layer_id=%s",
            layer.layer_id,
        )

async def _emit_auto_publish_failure(
    websocket: ServerConnection,
    state: SessionState,
    *,
    layer: LayerURI,
    reason: str,
) -> None:
    """Surface a typed 'computed but not displayable' state (honesty floor).

    When the deterministic auto-publish cannot produce a renderable http(s) URL,
    we MUST NOT silently drop the layer and narrate success. Emit a typed
    ``LAYER_AUTO_PUBLISH_FAILED`` error envelope so the failure is visible to the
    user (a degraded card / honest error) and the LLM-visible retry loop can act.
    Best-effort: never raises.
    """
    try:
        # The A.6 ErrorCode literal is a closed set; INTERNAL_ERROR is the right
        # wire code for an unexpected server-side render failure. The typed
        # ``[LAYER_AUTO_PUBLISH_FAILED]`` marker leads the human-readable message
        # so the surface is unambiguous + greppable (and the web can special-case
        # a degraded layer card off it) without widening the contract enum.
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            (
                f"[LAYER_AUTO_PUBLISH_FAILED] Computed layer {layer.name!r} "
                f"({layer.layer_id}) could not be displayed: {reason}. The result "
                f"was produced but is not renderable on the map."
            ),
            retryable=True,
        )
    except Exception:  # noqa: BLE001 - the honesty surface must never break dispatch
        logger.debug(
            "auto-publish failure-envelope emit failed layer_id=%s",
            layer.layer_id,
            exc_info=True,
        )

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
