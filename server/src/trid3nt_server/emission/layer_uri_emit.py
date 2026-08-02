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

``SIGNED_URLS`` (Decision 11) is a dormant placeholder env var for a future
direct-fetch feature; when set, this seam does NOTHING beyond logging a loud
WARNING -- emissions are byte-identical to ``SIGNED_URLS`` absent. Default is
``false`` and production ships with the flag absent/false.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from trid3nt_contracts.execution import LayerURI

if TYPE_CHECKING:  # pragma: no cover - typing-only import (no runtime cycle)
    from .pipeline_emitter import PipelineEmitter

logger = logging.getLogger("trid3nt_server.emission.layer_uri_emit")

__all__ = ["emit_layer_uri", "publish_input_layer", "signed_urls_enabled"]

# Env var name for the dormant direct-fetch / signed-URL scaffold (Decision 11).
SIGNED_URLS_ENV = "SIGNED_URLS"


def signed_urls_enabled() -> bool:
    """Read the dormant ``SIGNED_URLS`` flag (default ``False``).

    Accepts ``"true"`` / ``"1"`` / ``"yes"`` (case-insensitive) as truthy. The
    flag is DORMANT in v0.1: even when truthy it changes no emission behavior --
    see the module docstring and Decision 11.
    """
    raw = os.environ.get(SIGNED_URLS_ENV, "")
    return raw.strip().lower() in {"true", "1", "yes"}


def emit_layer_uri(layer: LayerURI) -> LayerURI | None:
    """Validate a client-bound ``LayerURI`` at the single emission seam.

    Returns the ``LayerURI`` unchanged when it is safe to deliver to the client,
    or ``None`` when it must be DROPPED (kept off the map). Callers MUST treat a
    ``None`` return as "do not call ``add_loaded_layer``"; the tool result the LLM
    sees is unaffected, so the failure is narrated honestly and the
    retry-on-failure loop can act.

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

    ``SIGNED_URLS`` (dormant): when set truthy, a WARNING is logged and behavior
    is otherwise UNCHANGED (byte-identical emission). See the module docstring
    and Decision 11 -- the natural consumer is a future direct-fetch feature whose
    signing lives in the client (scout Architecture A), not here.
    """
    if signed_urls_enabled():
        # DORMANT: no direct-fetch surface exists to sign for (Decision 11).
        # Log loudly and fall through to identity behavior so emissions stay
        # byte-identical to the flag-absent case.
        logger.warning(
            "%s=true but no direct-fetch surface exists to sign for — no-op "
            "(see Decision 11 in reports/sprints/sprint-13-5-decisions.md). "
            "layer_id=%s passes through unchanged.",
            SIGNED_URLS_ENV,
            layer.layer_id,
        )

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

    return layer


async def publish_input_layer(
    emitter: "PipelineEmitter | None",
    layer_uri: LayerURI | None,
    *,
    role: str = "input",
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
        safe = emit_layer_uri(layer_uri)
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
