"""Single emission seam for client-bound ``LayerURI`` objects.

THE ONE PLACE a ``LayerURI`` destined for the client passes through before
``PipelineEmitter.add_loaded_layer`` tracks it and a ``session-state`` envelope
carries it to the browser. Every site that hands a ``LayerURI`` to
``add_loaded_layer`` routes it through :func:`emit_layer_uri` first.

Why this exists (job-0254, Decision 11 — ``reports/sprints/sprint-13-5-decisions.md``)
======================================================================================
The original sprint-13.5 plan ("sign every client-bound LayerURI via
``mint_signed_url``") assumed the browser fetches GCS objects directly. The
job-0254 design scout (2026-06-11, file:line inventory) proved no such surface
exists today:

  * Rasters reach the client as QGIS Server **WMS run.app URLs** (locked down by
    job-0255's invoker-only QGIS + the agent ``/qgis-proxy`` route). A WMS URL is
    not a ``gs://`` object, so ``mint_signed_url`` structurally cannot sign it
    (``parse_layer_uri`` rejects non-``gs://``).
  * Vectors reach the client as **inline GeoJSON** (job-0175): the ``LayerURI.uri``
    legitimately stays ``gs://`` while ``PipelineEmitter`` reads it server-side
    (``pipeline_emitter.py`` ``_read_vector_uri_as_geojson``) and ships the parsed
    FeatureCollection inline in ``session-state.loaded_layers[].inline_geojson``.
    The browser never fetches the ``gs://`` uri for a vector — so it must pass
    through this seam UNTOUCHED.
  * Charts embed their data inline; ImpactPanel shows ``gs://`` as text only.

The single client-reaching raw ``gs://`` is the publish-FAILURE degraded path in
``workflows/model_flood_scenario.py`` (job-0254 §1): when ``publish_layer`` fails,
the composer used to fall back to emitting the raw ``gs://`` COG in
``LayerURI.uri`` — which never renders (MapLibre cannot fetch ``gs://``); it only
paints a broken, dead layer row in the LayerPanel. §1 drops that emission at the
source; this seam turns the drop into an **invariant** so no future site can
re-introduce a renderable raw-``gs://`` raster.

The guardrail
=============
:func:`emit_layer_uri` refuses (logs + DROPS, returning ``None``) any ``LayerURI``
that is a **renderable raster carrying a genuinely un-renderable uri** (``gs://``,
``file://``, or empty) -- the client cannot fetch those, so the only honest
outcome is to keep the layer off the map and let the narration/tool-card carry
the failure (the LLM-visible tool result stays truthful so the job-0177
retry-on-failure loop can act). Everything else passes untouched:

  * raster + ``s3://`` (raw COG; the QGIS plugin reads it via /vsicurl/) -> PASS
    (TiTiler exit / QGIS-native swap 2026-07: this REVERSES the job-0290c
    browser-era drop -- on the local build the plugin, not MapLibre, renders
    rasters, and it fetches the COG directly)
  * raster + ``http(s)`` (a WMS/tile URL) -> PASS
  * vector + ``gs://`` / ``s3://`` (inline-GeoJSON path, job-0175) -> PASS
    (do NOT break it)
  * vector + ``http(s)`` -> PASS

``SIGNED_URLS`` — dormant scaffold (Decision 11)
===============================================
The ``SIGNED_URLS`` env var is the placeholder for a FUTURE direct-fetch feature
(signed-COG rendering or signed large-vector delivery past the inline ceiling).
Per the scout's Architecture A, that feature's signing belongs in the **web
client** (it mints per-object signed URLs over its own authenticated channel,
respecting Decision F wire isolation), NOT here in the agent — so today this seam
deliberately does NOTHING when the flag is set beyond logging a loud WARNING. The
default is ``false`` and production ships with the flag absent/false (manifest
Correction 2). When ``SIGNED_URLS=true`` is set, emissions are byte-identical to
``SIGNED_URLS`` absent; only a WARNING is logged.

When the direct-fetch feature lands, the implementer extends this seam (or, per
Architecture A, the client) to mint signed URLs for ``gs://`` rasters here —
and at that point the guardrail's "drop renderable raw gs://" rule is relaxed for
the signed case. Until then: dormant.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from trid3nt_contracts.execution import LayerURI

if TYPE_CHECKING:  # pragma: no cover - typing-only import (no runtime cycle)
    from .pipeline_emitter import PipelineEmitter

logger = logging.getLogger("trid3nt_server.layer_uri_emit")

__all__ = ["emit_layer_uri", "publish_input_layer", "signed_urls_enabled"]

# Env var name for the dormant direct-fetch / signed-URL scaffold (Decision 11).
SIGNED_URLS_ENV = "SIGNED_URLS"


def signed_urls_enabled() -> bool:
    """Read the dormant ``SIGNED_URLS`` flag (default ``False``).

    Accepts ``"true"`` / ``"1"`` / ``"yes"`` (case-insensitive) as truthy. The
    flag is DORMANT in v0.1: even when truthy it changes no emission behavior —
    see the module docstring and Decision 11.
    """
    raw = os.environ.get(SIGNED_URLS_ENV, "")
    return raw.strip().lower() in {"true", "1", "yes"}


def emit_layer_uri(layer: LayerURI) -> LayerURI | None:
    """Validate a client-bound ``LayerURI`` at the single emission seam.

    Returns the ``LayerURI`` unchanged when it is safe to deliver to the client,
    or ``None`` when it must be DROPPED (kept off the map). Callers MUST treat a
    ``None`` return as "do not call ``add_loaded_layer``"; the tool result the LLM
    sees is unaffected, so the failure is narrated honestly and the job-0177
    retry-on-failure loop can act.

    Guardrail (the §1 fix promoted to an invariant -- job-0254, Decision 11;
    relaxed for ``s3://`` rasters by the TiTiler exit / QGIS-native swap):
        * Renderable RASTER carrying a genuinely un-renderable uri (``gs://``,
          ``file://`` local paths the plugin cannot reach, or EMPTY) -> DROP
          (return ``None``). Emitting one only paints a broken layer row. This
          is exactly the publish-FAILURE degraded path's leak.
        * RASTER carrying a raw ``s3://`` COG uri -> PASS. The QGIS plugin
          loads it via /vsicurl/ (publish_layer's raster SUCCESS shape).
        * VECTOR carrying ``gs://`` / ``s3://`` -> PASS. Vectors are delivered
          as inline GeoJSON (job-0175); the uri is read server-side by the
          emitter and never fetched by the client. Do NOT break this path.
        * Anything with an ``http(s)`` uri (a WMS/tile URL) -> PASS.

    ``SIGNED_URLS`` (dormant): when set truthy, a WARNING is logged and behavior
    is otherwise UNCHANGED (byte-identical emission). See the module docstring
    and Decision 11 — the natural consumer is a future direct-fetch feature whose
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
    # (job-0175) and pass untouched.
    #
    # TiTiler exit / QGIS-native swap (2026-07): raster s3:// now PASSES --
    # publish_layer returns the raw s3:// COG uri and the QGIS plugin reads it
    # via /vsicurl/, so the job-0290c browser-era s3 drop is REVERSED. Still
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

    NATE task #207 (surface engine inputs): every engine run consumes renderable
    inputs (OpenQuake fault traces, SFINCS DEM / rivers / landcover, SWMM
    building footprints) but historically only the RESULT layer was published.
    This is the ONE reusable seam composers call to also surface those inputs:
    it wraps :func:`emit_layer_uri` (the guardrail) + ``emitter.add_loaded_layer``
    exactly like the SWMM / SFINCS mesh-layer emit, with two hard rules baked in:

      * ``role`` defaults to ``"input"`` and is FORCED onto the LayerURI (a copy is
        made if the incoming role differs) so an input renders non-intrusively
        beneath the primary result, never competing with it for "the answer".
      * ``bbox`` is FORCED to ``None`` so ``add_loaded_layer`` does NOT emit a
        competing ``zoom-to`` map-command — an input/context layer must never
        fight the AOI / result camera for the view (mirrors the mesh-layer rule).

    BEST-EFFORT CONTRACT (the whole point): a failure to surface an input must
    NEVER fail the solve. This function NEVER raises — every failure path (no
    emitter bound, a falsy layer, the guardrail dropping a raw-object-store
    raster, an ``add_loaded_layer`` exception) is swallowed with a WARNING and
    returns ``False``. Returns ``True`` only when the layer actually reached the
    emitter. The result-layer publish is untouched; this only ADDS input rows.

    Note: a RASTER input must carry a renderable uri -- an http(s) tile/WMS URL
    or a raw ``s3://`` COG (the QGIS plugin reads it via /vsicurl/; TiTiler
    exit). A ``gs://`` / ``file://`` / empty-uri raster is correctly DROPPED
    here by the ``emit_layer_uri`` guardrail (nothing can render it); VECTORS
    carrying ``s3://`` inline server-side and pass straight through (job-0175),
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
