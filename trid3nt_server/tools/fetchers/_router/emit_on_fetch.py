"""Emit-on-fetch: surface a fetched INPUT as a ``role="context"`` layer.

A spec's render declaration is the switch -- there is no boolean -- so a source
returning a renderable LayerURI is published by reference when ``route()`` runs
inside a composer. Best-effort, and a uri is surfaced once per session."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Callable

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.emit_on_fetch"
)

__all__ = ["maybe_emit_input_on_fetch", "input_layer_name"]


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _resolution_label(spec: SourceSpec) -> str | None:
    """A human resolution string for the provenance name (first native hint)."""
    for decl in spec.resolution_declarations:
        if decl.native_hint:
            return decl.native_hint
    return None


def input_layer_name(
    spec: SourceSpec, params: dict[str, Any], purpose: str | None
) -> str:
    """Build ``Input: <what> (<source>[, <resolution>][, <datum>])``, where ``<what>``
    is the caller's ``purpose`` word, else the resolved ``variable`` / ``product``
    param, else the source class. A declared VERTICAL DATUM always rides here."""
    variable = params.get("variable") or params.get("product") or spec.source_class
    if isinstance(purpose, str) and purpose.strip():
        what = purpose.strip()
    else:
        what = str(variable).replace("_", " ")
    parts = [spec.source_class]
    res = _resolution_label(spec)
    if res:
        parts.append(res)
    if spec.vertical_datum:
        parts.append(f"datum {spec.vertical_datum}")
    return f"Input: {what} ({', '.join(parts)})"


def _drive_emit(emitter: Any, coro_factory: Callable[[], Any]) -> None:
    """Drive an async emit coroutine onto the emitter's bound loop from any thread.
    A worker thread schedules and WAITS (ordering and framing hold, because the
    caller is parked); the loop thread fires-and-forgets, since waiting deadlocks."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is not None:
        task = running.create_task(coro_factory())
        pending = getattr(emitter, "_pending_input_emit_tasks", None)
        if pending is None:
            pending = set()
            emitter._pending_input_emit_tasks = pending
        pending.add(task)
        task.add_done_callback(pending.discard)
        return

    loop = getattr(emitter, "_bound_loop", None)
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        fut.result(timeout=60)
        return

    # No bracketing loop (verify/CI/pure-sync direct call): run inline.
    asyncio.run(coro_factory())


def maybe_emit_input_on_fetch(
    spec: SourceSpec,
    params: dict[str, Any],
    layer: LayerURI,
    *,
    visualize: Any,
    purpose: str | None,
) -> None:
    """Surface ``layer`` as a role=context input IFF in composer mode: a no-op that
    NEVER raises unless every gate passes -- an emitter is bound, this is not the
    fetcher's own direct dispatch, output is renderable, and the uri is new."""
    try:
        # visualize=False is the per-CALL suppression, for a PROBE fetch of otherwise
        # visualizable data (an AOI candidate scan); the spec itself carries no flag.
        if visualize is False:
            return
        from trid3nt_server.emission.pipeline_emitter import (
            current_emitter,
            dispatched_tool_name,
        )

        emitter = current_emitter()
        if emitter is None:
            return
        # DIRECT chat dispatch: the tool-wrapper (emit_tool_call) already emits
        # the returned LayerURI as its declared role. Only the IN-COMPOSER nested
        # calling mode is the gap this seam closes.
        if dispatched_tool_name() == spec.name:
            return
        # Render declaration present == a renderable LayerURI was built. A record
        # source returned its dict before this point (no visual form, no attempt).
        if spec.output.layer_type not in ("raster", "vector"):
            return
        uri = (layer.uri or "").strip()
        if not uri:
            return
        seen = getattr(emitter, "_emitted_input_uris", None)
        if seen is None:
            seen = set()
            emitter._emitted_input_uris = seen
        if uri in seen:
            return
        seen.add(uri)

        name = input_layer_name(spec, params, purpose)
        layer_id = f"input-{spec.source_class}-{_short_hash(uri)}"

        from trid3nt_server.emission.layer_uri_emit import (
            publish_input_layer,
            publish_raster_input_cog,
        )

        if spec.output.layer_type == "raster":
            def _coro() -> Any:
                return publish_raster_input_cog(
                    emitter,
                    cog_uri=uri,
                    layer_id=layer_id,
                    name=name,
                    style=layer.style,
                    role="context",
                    fallback_note=layer.fallback_note,
                    fallbacks=layer.fallbacks,
                )
        else:
            input_layer = layer.model_copy(
                update={
                    "layer_id": layer_id,
                    "name": name,
                    "role": "context",
                    "bbox": None,
                }
            )

            def _coro() -> Any:
                return publish_input_layer(emitter, input_layer, role="context")

        _drive_emit(emitter, _coro)
    except Exception as exc:  # noqa: BLE001 -- input surfacing is NEVER fatal
        logger.warning(
            "emit_on_fetch: failed to surface fetched input for %s (non-fatal, "
            "the fetch is unaffected): %s",
            spec.name,
            exc,
        )
