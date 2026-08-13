"""Emit-on-fetch: surface a fetched INPUT as a role=context layer (ADR 0244).

The single seam that closes the IN-COMPOSER visualization gap. A spec's RENDER
DECLARATION (it returns a renderable ``LayerURI`` -- a raster COG or a vector
FGB, not a ``record`` dict) IS the intent to visualize: presence means the data
has a visual form and WILL be surfaced wherever it is fetched. The DIRECT chat
path already honours this (``emit_tool_call`` emits the returned LayerURI as the
tool's declared role); this hook adds the missing half -- when the SAME router
``route()`` runs as a bare function nested inside a COMPOSER, the fetched data is
published BY REFERENCE as a ``role="context"`` input row beneath the primary
result, so an engine's terrain / rivers / land cover are never invisible.

Design facts (settled semantics, docs/IDEAS.md 2026-08-13):
  * NO boolean flag on the spec -- the render declaration is the switch. A
    ``record`` source (``layer_type=record``) has no visual form and never emits.
  * ``visualize=False`` is a per-CALL belt-and-suspenders reserved for PROBE
    fetches of visualizable data (AOI candidate scans); it suppresses this hook.
  * ``purpose=`` lets a composer contribute ONE word to the layer name (a label,
    never a pathway) -- e.g. ``purpose="mesh bed"``.
  * BEST-EFFORT: a surfacing failure NEVER fails the fetch (logged once).
  * SESSION dedup by uri: a fetched input is surfaced once per session.

The actual emit rides the existing ``layer_uri_emit`` machinery
(``publish_raster_input_cog`` for a raster COG, ``publish_input_layer`` for a
vector), reused not duplicated. Because ``route()`` is synchronous (and a fetcher
is frequently off-loaded to a worker thread), the async emit coroutine is driven
back onto the emitter's bound loop via ``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Callable

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.emit_on_fetch"
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
    """Build the provenance name ``Input: <what> (<source>[, <resolution>])``.

    ``<what>`` is the composer-supplied ``purpose`` word when present, else the
    resolved ``variable`` / ``product`` param, else the source class.
    """
    variable = params.get("variable") or params.get("product") or spec.source_class
    if isinstance(purpose, str) and purpose.strip():
        what = purpose.strip()
    else:
        what = str(variable).replace("_", " ")
    parts = [spec.source_class]
    res = _resolution_label(spec)
    if res:
        parts.append(res)
    return f"Input: {what} ({', '.join(parts)})"


def _drive_emit(emitter: Any, coro_factory: Callable[[], Any]) -> None:
    """Drive an async emit coroutine to the emitter's bound loop from any thread.

    * On a WORKER thread (the off-loaded sync fetch, no running loop here): the
      loop the emitter was bracketed on IS free (it is awaiting the ``to_thread``
      future), so schedule the coroutine on it and WAIT -- ordering + WS framing
      are preserved because the composer task is parked on the thread meanwhile.
    * On the LOOP thread (a composer that called the fetch WITHOUT off-loading):
      a blocking wait would deadlock, so fire-and-forget as a task; it runs the
      instant the sync fetch stack unwinds back to the loop (still before the
      long solve). A strong ref is kept so it is not GC'd mid-flight.
    * No loop at all (a pure direct/CI call): run it to completion inline.
    """
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
    """Surface ``layer`` as a role=context input IFF in composer mode (ADR 0244).

    Called from ``route()`` right after a successful LayerURI build. No-op (and
    NEVER raises) unless every gate passes: an emitter is bound, this is NOT the
    direct-dispatch of the fetcher itself, ``visualize`` is not ``False``, the
    spec declares a renderable output, and the uri has not already been surfaced.
    """
    try:
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
                    style_preset=layer.style_preset,
                    role="context",
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
