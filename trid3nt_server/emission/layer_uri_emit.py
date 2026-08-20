"""Single emission seam for client-bound ``LayerURI`` objects.

THE ONE PLACE a ``LayerURI`` destined for the client passes through before
``PipelineEmitter.add_loaded_layer`` tracks it and a ``session-state`` envelope
carries it to the browser. Every site that hands a ``LayerURI`` to
``add_loaded_layer`` routes it through :func:`emit_layer_uri` first.

No client surface fetches a raw ``gs://`` object today (Decision 11):
rasters reach the client as QGIS Server WMS run.app URLs or, on the local
build, are fetched directly by the QGIS plugin via ``/vsicurl/``; vectors
reach the client as inline GeoJSON (``PipelineEmitter`` reads the ``gs://``
uri server-side and ships the parsed FeatureCollection inline); charts embed
their data inline.

The guardrail
=============
:func:`emit_layer_uri` refuses (logs + DROPS, returning ``None``) any ``LayerURI``
that is a **renderable raster carrying a genuinely un-renderable uri** (``gs://``,
``file://``, or empty) -- the client cannot fetch those, so the only honest
outcome is to keep the layer off the map and let the narration/tool-card carry
the failure (the LLM-visible tool result stays truthful so the
retry-on-failure loop can act). Everything else passes untouched:

  * raster + ``s3://`` (raw COG; the QGIS plugin reads it via /vsicurl/) -> PASS
  * raster + ``http(s)`` (a WMS/tile URL) -> PASS
  * vector + ``gs://`` / ``s3://`` (inline-GeoJSON path) -> PASS
    (do NOT break it)
  * vector + ``http(s)`` -> PASS
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
    "publish_input_layer",
    "publish_raster_input_cog",
    "stamp_fallbacks",
]


def stamp_fallbacks(
    layer: LayerURI, activations: Sequence[Any] | None
) -> LayerURI:
    """Merge fallback-ladder activation rows + their narration onto ``layer``.

    THE ONE place a re-emitted layer regains the rows its source carried: a
    layer rebuilt from a bare uri (``publish_raster_input_cog``, a worker
    manifest row) starts with an empty ``fallbacks`` list, and an empty list
    means "no ladder governs this" -- never "nothing was substituted". Rows
    already on the layer are kept and not duplicated.
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

    Returns the ``LayerURI`` unchanged when it is safe to deliver to the client,
    or ``None`` when it must be DROPPED (kept off the map). Callers MUST treat a
    ``None`` return as "do not call ``add_loaded_layer``"; the tool result the LLM
    sees is unaffected, so the failure is narrated honestly and the
    retry-on-failure loop can act.

    ``fallbacks`` carries the activation rows of the ladder that produced this
    layer's data; they are stamped here so a re-emitted layer never loses them
    (see :func:`stamp_fallbacks`).

    Guardrail (the §1 fix promoted to an invariant -- Decision 11;
    relaxed for ``s3://`` rasters by the TiTiler exit / QGIS-native swap):
        * Renderable RASTER carrying a genuinely un-renderable uri (``gs://``,
          ``file://`` local paths the plugin cannot reach, or EMPTY) -> DROP
          (return ``None``). Emitting one only paints a broken layer row. This
          is exactly the publish-FAILURE degraded path's leak.
        * RASTER carrying a raw ``s3://`` COG uri -> PASS. The QGIS plugin
          loads it via /vsicurl/ (publish_layer's raster SUCCESS shape).
        * VECTOR carrying ``gs://`` / ``s3://`` -> PASS. Vectors are delivered
          as inline GeoJSON; the uri is read server-side by the
          emitter and never fetched by the client. Do NOT break this path.
        * Anything with an ``http(s)`` uri (a WMS/tile URL) -> PASS.
    """
    uri = layer.uri or ""

    # The guardrail: renderable raster + a genuinely un-renderable uri -> drop.
    # This is the publish-failure degraded-path leak (§1) turned into an
    # invariant. Vectors carrying gs:// / s3:// are the inline-GeoJSON path
    # and pass untouched.
    #
    # TiTiler exit / QGIS-native swap: raster s3:// now PASSES --
    # publish_layer returns the raw s3:// COG uri and the QGIS plugin reads it
    # via /vsicurl/, so the browser-era s3 drop is REVERSED. Still
    # dropped (nothing can render them): gs:// (no reachable face on this
    # stack), file:// local paths the plugin cannot reach, and EMPTY uris.
    if layer.layer_type == "raster" and (
        not uri or uri.startswith("gs://") or uri.startswith("file://")
    ):
        logger.warning(
            "layer_uri_emit: DROPPING renderable raster LayerURI with an "
            "un-renderable uri (never reaches the map). layer_id=%s uri=%r. "
            "The renderable forms are an http(s) tile/WMS URL or a raw s3:// "
            "COG (plugin /vsicurl/). (job-0254 guardrail; see Decision 11.)",
            layer.layer_id,
            uri,
        )
        return None

    return stamp_fallbacks(layer, fallbacks)


async def publish_input_layer(
    emitter: "PipelineEmitter | None",
    layer_uri: LayerURI | None,
    *,
    role: str = "input",
    fallbacks: Sequence[Any] | None = None,
) -> bool:
    """BEST-EFFORT: surface an engine INPUT layer on the map (role="input").

    Surface engine inputs: every engine run consumes renderable
    inputs (OpenQuake fault traces, SFINCS DEM / rivers / landcover, SWMM
    building footprints) but historically only the RESULT layer was published.
    This is the ONE reusable seam composers call to also surface those inputs:
    it wraps :func:`emit_layer_uri` (the guardrail) + ``emitter.add_loaded_layer``
    exactly like the SWMM / SFINCS mesh-layer emit, with two hard rules baked in:

      * ``role`` defaults to ``"input"`` and is FORCED onto the LayerURI (a copy is
        made if the incoming role differs) so an input renders non-intrusively
        beneath the primary result, never competing with it for "the answer".
      * ``bbox`` is FORCED to ``None`` so ``add_loaded_layer`` does NOT emit a
        competing ``zoom-to`` map-command -- an input/context layer must never
        fight the AOI / result camera for the view (mirrors the mesh-layer rule).

    BEST-EFFORT CONTRACT (the whole point): a failure to surface an input must
    NEVER fail the solve. This function NEVER raises -- every failure path (no
    emitter bound, a falsy layer, the guardrail dropping a raw-object-store
    raster, an ``add_loaded_layer`` exception) is swallowed with a WARNING and
    returns ``False``. Returns ``True`` only when the layer actually reached the
    emitter. The result-layer publish is untouched; this only ADDS input rows.

    Note: a RASTER input must carry a renderable uri -- an http(s) tile/WMS URL
    or a raw ``s3://`` COG (the QGIS plugin reads it via /vsicurl/; TiTiler
    exit). A ``gs://`` / ``file://`` / empty-uri raster is correctly DROPPED
    here by the ``emit_layer_uri`` guardrail (nothing can render it); VECTORS
    carrying ``s3://`` inline server-side and pass straight through,
    so they need no round-trip.
    """
    if emitter is None or layer_uri is None:
        return False
    try:
        # Force the input invariants: role="input" + bbox=None. Copy only when a
        # field actually differs so the common (already-correct) path is a no-op.
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
            "publish_input_layer: surfaced engine input layer_id=%s type=%s "
            "preset=%s role=%s",
            safe.layer_id,
            safe.layer_type,
            safe.style_preset,
            safe.role,
        )
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


async def publish_raster_input_cog(
    emitter: "PipelineEmitter | None",
    *,
    cog_uri: str | None,
    layer_id: str,
    name: str,
    style_preset: str,
    role: str = "context",
    fallback_note: str | None = None,
    fallbacks: Sequence[Any] | None = None,
) -> bool:
    """BEST-EFFORT: surface an EXISTING ``s3://`` raster COG as an input/context row.

    The raster twin of :func:`publish_input_layer` for a COG that is NOT yet
    registered with the render bridge. Rides the object ALREADY in the runs
    bucket / cache (NO re-upload): rounds the ``s3://`` COG through
    ``publish_layer`` (which registers its ``style_preset`` and returns a
    plugin-renderable uri), builds a ``role`` LayerURI, and hands it to
    :func:`publish_input_layer`. This is the seam the flood-input DEM path
    (model_flood_scenario) established inline; consolidated here so the
    bathymetry-consuming coastal templates surface their fetched topobathy the
    same way.

    Best-effort contract: NEVER raises. Every failure (no emitter, a falsy uri,
    a ``PublishLayerError`` on a non-``s3://`` / unregistered uri, the emit
    guardrail dropping it) is swallowed with a WARNING and returns ``False``; a
    failure to surface an input can NEVER fail the solve. Returns ``True`` only
    when the layer actually reached the emitter.
    """
    if emitter is None or not cog_uri:
        return False
    try:
        # Late import: keep this emission module free of a load-time dependency
        # on the heavy publish_layer tool (rasterio / TiTiler), mirroring how the
        # composers import it inline.
        from trid3nt_server.data.publish_layer.publish_layer import (
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
            style_preset=style_preset,
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
        style_preset=style_preset,
        role=role,
        bbox=None,
        fallback_note=fallback_note,
    )
    return await publish_input_layer(emitter, layer, role=role, fallbacks=fallbacks)
