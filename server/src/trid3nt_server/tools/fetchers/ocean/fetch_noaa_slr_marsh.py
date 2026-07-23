"""``fetch_noaa_slr_marsh`` -- NOAA SLR MARSH-MIGRATION raster fetcher.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers.ocean._noaa_slr_raster import (
    NOAASLRRasterInputError,
    _DEFAULT_RES_DEG,
    estimate_payload_mb_for,
    export_slr_raster_cog_bytes,
    resolve_res_deg,
    round_bbox,
    validate_bbox,
)

__all__ = ["fetch_noaa_slr_marsh", "estimate_payload_mb", "VALID_MARSH_FT"]

logger = logging.getLogger("trid3nt_server.tools.fetchers.ocean.fetch_noaa_slr_marsh")

#: NOAA publishes marsh-migration rasters at 0..10 ft in 0.5-ft steps (21 services:
#: marsh_000, marsh_050, marsh_100, ... marsh_1000).
VALID_MARSH_FT: frozenset[float] = frozenset(n / 2.0 for n in range(0, 21))


def _marsh_service_name(slr_ft: float) -> str:
    if slr_ft not in VALID_MARSH_FT:
        raise NOAASLRRasterInputError(
            f"slr_ft={slr_ft!r} is not a valid NOAA SLR marsh-migration level; valid "
            f"values are 0..10 ft in 0.5-ft steps {sorted(VALID_MARSH_FT)}"
        )
    # SLR ft x 100, zero-padded to 3 (10.0 -> "1000"): 0.0->"000", 0.5->"050", 3.0->"300".
    return f"marsh_{int(round(slr_ft * 100)):03d}"


_METADATA = AtomicToolMetadata(
    name="fetch_noaa_slr_marsh",
    ttl_class="static-30d",
    source_class="noaa_slr_marsh",
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
def fetch_noaa_slr_marsh(
    bbox: tuple[float, float, float, float],
    slr_ft: float = 3.0,
    res_deg: float | None = None,
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch the NOAA Sea-Level-Rise MARSH-MIGRATION raster for one SLR level.

    **What it does:** Reads NOAA OCM's SLR Viewer marsh-migration projection for one
    SLR level (0..10 ft, 0.5-ft steps) and returns it as a transparent RGBA raster
    overlay (the NOAA marsh-class symbology baked in: existing marsh, migrated marsh,
    open water / drowned, etc.). Shows how coastal wetlands shift under sea-level rise
    -- the habitat-transition companion to ``fetch_noaa_slr_scenarios`` (footprint)
    and ``fetch_noaa_slr_confidence`` (mapping confidence).

    **When to use:**
    - "How does the marsh / coastal wetland migrate under 3 ft of sea-level rise?"
    - "Show the NOAA marsh-migration projection for this estuary."
    - As a habitat-transition overlay for conservation / wetland exposure analysis.

    **When NOT to use:**
    - For the inundation FOOTPRINT -> ``fetch_noaa_slr_scenarios``.
    - For mapping CONFIDENCE -> ``fetch_noaa_slr_confidence``.
    - For event-driven storm surge -> ``run_model_flood_scenario`` / ``fetch_gtsm_tide_surge``.
    - Outside CONUS -> not covered (CONUS coastal product only).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required.
      Example coastal Lee County FL: ``(-82.2, 26.2, -81.5, 26.9)``.
    - ``slr_ft`` (float, default ``3.0``): 0..10 ft in 0.5-ft steps.
    - ``res_deg`` (float, optional): output cell size in degrees (finer = opt-in,
      payload-warning-coupled). Defaults to ~50 m.

    **Returns:** a 4-band transparent RGBA COG ``LayerURI`` (``layer_type="raster"``,
    ``role="context"``, ``style_preset="noaa_slr_marsh"``). A bbox with no marsh
    coverage at this level returns a valid transparent (empty) overlay -- never
    fabricated content.

    **Cross-tool dependencies:** pairs with ``fetch_noaa_slr_scenarios`` +
    ``fetch_noaa_slr_confidence`` (the three NOAA OCM SLR Viewer products); feeds
    conservation / wetland overlays with ``fetch_wdpa_protected_areas``.
    """
    q_bbox = round_bbox(validate_bbox(bbox))
    rd = resolve_res_deg(res_deg if res_deg is not None else _DEFAULT_RES_DEG)
    service = _marsh_service_name(float(slr_ft))

    params = {
        "bbox": list(q_bbox),
        "product": "slr_marsh",
        "slr_ft": float(slr_ft),
        "res_deg": round(rd, 7),
    }
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: export_slr_raster_cog_bytes(service, q_bbox, rd),
    )
    assert result.uri is not None, "fetch_noaa_slr_marsh is cacheable; uri must be set"
    tag = f"{slr_ft:.1f}ft".replace(".0ft", "ft")
    return LayerURI(
        layer_id=f"noaa-slr-marsh-{int(round(slr_ft * 100)):03d}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"NOAA SLR Marsh Migration ({tag})",
        layer_type="raster",
        uri=result.uri,
        style_preset="noaa_slr_marsh",
        role="context",
        units=None,
        bbox=q_bbox,
    )
