"""``fetch_noaa_slr_confidence`` -- NOAA SLR mapping-CONFIDENCE raster fetcher.

The ``conf_*`` sibling of ``fetch_noaa_slr_scenarios`` (named at that module's
docstring L77-78). Where the scenarios tool returns the inundation FOOTPRINT
polygons, this returns NOAA OCM's CONFIDENCE-of-mapping raster for one whole-foot
SLR level: a symbolized overlay showing where the bathtub inundation mapping is
high-confidence (blue) vs low-confidence (orange) given the underlying lidar DEM
vertical uncertainty. CONUS coastal only.

Reads NOAA OCM ``dc_slr/conf_<N>ft`` MapServer (anonymous / no key) via the shared
``_noaa_slr_raster`` export path -> a 4-band RGBA COG with the symbology baked in,
so publish_layer's RGBA passthrough renders it directly.

ASCII only.
"""

from __future__ import annotations

import logging
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through
from ._noaa_slr_raster import (
    NOAASLRRasterInputError,
    _DEFAULT_RES_DEG,
    estimate_payload_mb_for,
    export_slr_raster_cog_bytes,
    resolve_res_deg,
    round_bbox,
    validate_bbox,
)

__all__ = ["fetch_noaa_slr_confidence", "estimate_payload_mb", "VALID_CONF_FT"]

logger = logging.getLogger("grace2_agent.tools.fetch_noaa_slr_confidence")

#: NOAA publishes confidence rasters at WHOLE-foot levels 0..10 (11 services).
VALID_CONF_FT: frozenset[float] = frozenset(float(n) for n in range(0, 11))


def _conf_service_name(slr_ft: float) -> str:
    if slr_ft not in VALID_CONF_FT:
        raise NOAASLRRasterInputError(
            f"slr_ft={slr_ft!r} is not a valid NOAA SLR confidence level; valid "
            f"values are the whole feet {sorted(VALID_CONF_FT)}"
        )
    return f"conf_{int(slr_ft)}ft"


_METADATA = AtomicToolMetadata(
    name="fetch_noaa_slr_confidence",
    ttl_class="static-30d",
    source_class="noaa_slr_confidence",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    res_deg: float | None = None,
    **_kw: Any,
) -> float:
    return estimate_payload_mb_for(bbox, res_deg)


@register_tool(_METADATA, open_world_hint=True)
def fetch_noaa_slr_confidence(
    bbox: tuple[float, float, float, float],
    slr_ft: float = 3.0,
    res_deg: float | None = None,
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch the NOAA Sea-Level-Rise mapping-CONFIDENCE raster for one SLR level.

    **What it does:** Reads NOAA OCM's SLR Viewer confidence-of-mapping service for
    one whole-foot SLR level and returns it as a transparent RGBA raster overlay
    (the NOAA symbology baked in: blue = HIGH confidence the area is inundated at
    this level, orange = LOW confidence, transparent = not inundated). This is the
    mapping-uncertainty companion to ``fetch_noaa_slr_scenarios`` (which returns
    the inundation-footprint polygons).

    **When to use:**
    - "How confident is the sea-level-rise inundation mapping at 3 ft here?"
    - "Show the NOAA SLR mapping confidence / uncertainty for this coast."
    - As an overlay on ``fetch_noaa_slr_scenarios`` to flag low-confidence
      inundation areas before reporting exposure.

    **When NOT to use:**
    - For the inundation FOOTPRINT itself -> ``fetch_noaa_slr_scenarios``.
    - For marsh-migration projections -> ``fetch_noaa_slr_marsh``.
    - For event-driven storm surge -> ``run_model_flood_scenario`` / ``fetch_gtsm_tide_surge``.
    - Outside CONUS -> not covered (CONUS coastal product only).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required.
      Example coastal Lee County FL: ``(-82.2, 26.2, -81.5, 26.9)``.
    - ``slr_ft`` (float, default ``3.0``): one WHOLE foot 0..10 (confidence is
      published at whole-foot levels only).
    - ``res_deg`` (float, optional): output cell size in degrees (finer = opt-in,
      payload-warning-coupled). Defaults to ~50 m (the symbology is coarse).

    **Returns:** a 4-band transparent RGBA COG ``LayerURI`` (``layer_type="raster"``,
    ``role="context"``, ``style_preset="noaa_slr_confidence"``). A bbox with no SLR
    coverage at this level returns a valid transparent (empty) overlay -- never
    fabricated content.

    **Cross-tool dependencies:** pairs with ``fetch_noaa_slr_scenarios`` (footprint)
    and ``fetch_noaa_slr_marsh`` (marsh migration) -- the three NOAA OCM SLR Viewer products.
    """
    q_bbox = round_bbox(validate_bbox(bbox))
    rd = resolve_res_deg(res_deg if res_deg is not None else _DEFAULT_RES_DEG)
    service = _conf_service_name(float(slr_ft))

    params = {
        "bbox": list(q_bbox),
        "product": "slr_confidence",
        "slr_ft": float(slr_ft),
        "res_deg": round(rd, 7),
    }
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: export_slr_raster_cog_bytes(service, q_bbox, rd),
    )
    assert result.uri is not None, "fetch_noaa_slr_confidence is cacheable; uri must be set"
    return LayerURI(
        layer_id=f"noaa-slr-confidence-{int(slr_ft)}ft-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"NOAA SLR Mapping Confidence ({slr_ft:.0f} ft)",
        layer_type="raster",
        uri=result.uri,
        style_preset="noaa_slr_confidence",
        role="context",
        units=None,
        bbox=q_bbox,
    )
