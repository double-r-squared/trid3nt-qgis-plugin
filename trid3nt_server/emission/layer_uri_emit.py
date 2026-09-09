"""Single emission seam for client-bound ``LayerURI`` objects.

Every ``LayerURI`` bound for the client crosses :func:`emit_layer_uri` before it
is tracked and delivered. One store, one scheme: a raster reaches the client as
the ``s3://`` COG the plugin opens natively through GDAL ``/vsis3``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Sequence

from trid3nt_contracts.common import render_fallback_line
from trid3nt_contracts.execution import LayerURI

if TYPE_CHECKING:  # pragma: no cover - typing-only import (no runtime cycle)
    from .pipeline_emitter import PipelineEmitter

logger = logging.getLogger("trid3nt_server.emission.layer_uri_emit")

__all__ = [
    "emit_layer_uri",
    "publish_for_emission",
    "publish_input_layer",
    "publish_raster_input_cog",
    "stamp_fallbacks",
]


async def publish_for_emission(layer: LayerURI) -> LayerURI:
    """Publish a raster LayerURI on its way to the map. THE auto-emit step.

    Only a raw ``s3://`` COG raster is published; a failed publish degrades to unstyled.
    """
    # Vectors render inline from their producing tool's GeoJSON and an http(s)
    # raster is already a rendered face, so neither has anything to publish.
    uri = layer.uri or ""
    if layer.layer_type != "raster" or not uri.startswith("s3://"):
        return layer

    from .publish import PublishLayerError, publish_layer

    try:
        # OFFLOAD: publish runs rasterio / GDAL over the COG. Keep it off the
        # event loop so the WS keepalive stays responsive.
        published = await asyncio.to_thread(
            publish_layer,
            layer_uri=uri,
            layer_id=layer.layer_id,
            style=layer.style,
            name=layer.name,
        )
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except PublishLayerError as exc:
        logger.warning(
            "publish_for_emission: publish failed for layer_id=%s error_code=%s: "
            "%s. The s3:// COG still reaches the map, unstyled.",
            layer.layer_id, getattr(exc, "error_code", "?"), exc,
        )
        return layer
    except Exception:  # noqa: BLE001 - enrichment is never fatal to the layer
        logger.exception(
            "publish_for_emission: publish RAISED for layer_id=%s. The "
            "s3:// COG still reaches the map, unstyled.",
            layer.layer_id,
        )
        return layer

    if not (isinstance(published, str) and published.startswith(
        ("http://", "https://", "s3://")
    )):
        logger.warning(
            "publish_for_emission: publish returned a non-renderable value for "
            "layer_id=%s -> %r; keeping the original COG uri.",
            layer.layer_id, published,
        )
        return layer

    if published == uri:
        return layer
    return layer.model_copy(update={"uri": published})


def stamp_fallbacks(
    layer: LayerURI, activations: Sequence[Any] | None
) -> LayerURI:
    """Merge fallback-ladder activation rows and their narration onto ``layer``.

    An empty list means no ladder governs the layer, never that nothing was substituted.
    """
    if not activations:
        return layer

    def _rung(a: Any) -> Any:
        return a.get("rung") if isinstance(a, dict) else getattr(a, "rung", None)

    existing = list(layer.fallbacks or [])
    seen = {_rung(a) for a in existing}
    added = [a for a in activations if _rung(a) not in seen]
    if not added:
        return layer
    merged = existing + added
    update: dict[str, Any] = {"fallbacks": merged}
    note = render_fallback_line(merged)
    if note and note not in (layer.fallback_note or ""):
        update["fallback_note"] = (
            f"{layer.fallback_note} {note}" if layer.fallback_note else note
        )
    return layer.model_copy(update=update)


def emit_layer_uri(
    layer: LayerURI, *, fallbacks: Sequence[Any] | None = None
) -> LayerURI | None:
    """Validate a client-bound ``LayerURI`` at the single emission seam.

    ``None`` means DROP: the caller must not hand the layer to ``add_loaded_layer``.
    """
    uri = layer.uri or ""

    # The guardrail: a renderable raster carrying a uri nothing can fetch is
    # dropped - gs:// has no reachable face on this stack, file:// names a path
    # the plugin cannot reach, and an empty uri renders nothing. A raster s3://
    # COG PASSES (the plugin reads it via /vsis3), and so does every vector.
    if layer.layer_type == "raster" and (
        not uri or uri.startswith("gs://") or uri.startswith("file://")
    ):
        logger.warning(
            "layer_uri_emit: DROPPING renderable raster LayerURI with an "
            "un-renderable uri (never reaches the map). layer_id=%s uri=%r. "
            "The renderable form is an s3:// object the plugin reads via "
            "/vsis3.",
            layer.layer_id,
            uri,
        )
        return None

    return stamp_fallbacks(_resolve_non_raster_legend(layer), fallbacks)


#: The preset shape a layer TYPE implies when its row names none. A vector is
#: drawn, not measured; a mesh paints one of its own dataset groups. Neither can
#: be the raster default, whose resolution reads bands the object does not have.
_KIND_BY_LAYER_TYPE = {"vector": "reference", "mesh": "mesh"}


def _resolve_non_raster_legend(layer: LayerURI) -> LayerURI:
    """Resolve a VECTOR or MESH layer's declared row into its render key.
    A vector and a mesh have no band to read, so the row resolves here rather
    than against bytes; a layer that already carries a legend is left untouched.
    """
    kind = _KIND_BY_LAYER_TYPE.get(layer.layer_type)
    if kind is None or layer.legend is not None:
        return layer
    row = {**(layer.style or {})}
    row.setdefault("kind", kind)
    try:
        from .presets import from_row, paints_a_raster
        from .publish import legend_for_published_layer

        if paints_a_raster(from_row(row)):
            return layer
        legend = legend_for_published_layer(
            row, layer.uri or "", units=layer.units)
    except Exception as exc:  # noqa: BLE001 - a style never blocks an emit
        logger.warning(
            "layer_uri_emit: style resolution skipped for layer_id=%s (%s: %s); "
            "the layer reaches the map on QGIS's own default rendering.",
            layer.layer_id, type(exc).__name__, exc)
        return layer
    if legend is None:
        return layer
    return layer.model_copy(update={"legend": legend})


async def publish_input_layer(
    emitter: "PipelineEmitter | None",
    layer_uri: LayerURI | None,
    *,
    role: str = "input",
    fallbacks: Sequence[Any] | None = None,
) -> bool:
    """BEST-EFFORT: surface ONE extra layer on the map beside the step's return.
    ``role`` and ``bbox=None`` are FORCED onto the layer; never raises, returning
    ``False`` for every failure so surfacing an extra row cannot fail a solve.
    """
    if emitter is None or layer_uri is None:
        return False
    try:
        # Force the surfacing invariants: the caller's role, because a product of
        # the solve declared as an input misstates what the run computed; and
        # bbox=None, so this row emits no zoom-to that fights the result camera.
        # Copy only when a field actually differs so the common path is a no-op.
        if layer_uri.role != role or layer_uri.bbox is not None:
            layer_uri = layer_uri.model_copy(update={"role": role, "bbox": None})
        safe = emit_layer_uri(layer_uri, fallbacks=fallbacks)
        if safe is None:
            # The guardrail dropped it (e.g. a raw-object-store raster that never
            # round-tripped through publish_layer). Honest no-surface, not fatal.
            logger.warning(
                "publish_input_layer: emit_layer_uri DROPPED input layer_id=%s "
                "(not surfaced; the solve is unaffected).",
                layer_uri.layer_id,
            )
            return False
        await emitter.add_loaded_layer(safe)
        logger.info(
            "publish_input_layer: surfaced layer_id=%s type=%s role=%s",
            safe.layer_id, safe.layer_type, safe.role)
        return True
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        layer_id = getattr(layer_uri, "layer_id", "<unknown>")
        logger.warning(
            "publish_input_layer: failed to surface input layer_id=%s "
            "(non-fatal, input absent; the solve is unaffected): %s",
            layer_id,
            exc,
        )
        return False


def _cog_object_exists(cog_uri: str) -> bool:
    """True when ``cog_uri`` names an object physically present in the store.
    Any lookup failure reads as absent and never raises, so a fabricated uri is
    only ever registered once the store has confirmed it real.
    """
    from trid3nt_server.workflows.solver.solver import (
        _get_s3_client,
        _split_object_uri,
    )

    try:
        _, bucket, key = _split_object_uri(cog_uri)
        _get_s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 -- absent / unreachable == do not register
        return False


async def publish_raster_input_cog(
    emitter: "PipelineEmitter | None",
    *,
    cog_uri: str | None,
    layer_id: str,
    name: str,
    style: dict[str, Any] | None = None,
    role: str = "context",
    fallback_note: str | None = None,
    fallbacks: Sequence[Any] | None = None,
) -> bool:
    """BEST-EFFORT: surface an EXISTING ``s3://`` raster COG as an input/context row.
    Rides the object already in the store - no re-upload - and never raises,
    returning ``False`` for every failure rather than failing the solve.
    """
    if emitter is None or not cog_uri:
        return False
    if not await asyncio.to_thread(_cog_object_exists, cog_uri):
        logger.warning(
            "publish_raster_input_cog: SKIPPING layer_id=%s -- %s is NOT present "
            "in the object store (dead-COG: a worker/manifest recorded the "
            "filename but never uploaded it). No 404 layer registered; the "
            "input is honestly absent, not surfaced.",
            layer_id, cog_uri,
        )
        return False
    try:
        # Late import: keep this module free of a load-time rasterio dependency.
        from trid3nt_server.emission.publish import (
            PublishLayerError,
            publish_layer,
        )
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning(
            "publish_raster_input_cog: publish_layer import failed (non-fatal, "
            "input absent): %s",
            exc,
        )
        return False
    try:
        # OFFLOAD: publish_layer runs a sync worker-poll / rasterio path -- keep
        # it off the event loop so the WS keepalive stays responsive.
        renderable = await asyncio.to_thread(
            publish_layer,
            layer_uri=cog_uri,
            layer_id=layer_id,
            style=style,
            name=name,
        )
    except PublishLayerError as exc:
        logger.warning(
            "publish_raster_input_cog: publish_layer failed for %s (non-fatal, "
            "input absent) error_code=%s: %s",
            layer_id,
            getattr(exc, "error_code", "?"),
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning(
            "publish_raster_input_cog: publish_layer raised for %s (non-fatal, "
            "input absent): %s",
            layer_id,
            exc,
        )
        return False

    layer = LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=renderable,
        style=style,
        role=role,
        bbox=None,
        fallback_note=fallback_note,
    )
    return await publish_input_layer(emitter, layer, role=role, fallbacks=fallbacks)
