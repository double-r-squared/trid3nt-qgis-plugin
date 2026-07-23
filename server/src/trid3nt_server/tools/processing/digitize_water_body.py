"""``digitize_water_body`` atomic tool  --  NDWI surface-water polygons (land cover).

Digitizes open surface-water bodies (lakes, reservoirs, ponds, wide river
reaches) for a bbox + recent time window from Sentinel-2 L2A surface
reflectance, via the Microsoft Planetary Computer (PC) STAC catalog. It is the
CPU / spectral-index counterpart to a GPU SAM-style segmentation: no model
weights, no GPU  --  just the Normalized Difference Water Index

    NDWI = (Green - NIR) / (Green + NIR)      (McFeeters 1996)

thresholded to a water mask, then vectorized to water polygons (FlatGeobuf).

Why NDWI works
==============

Open water absorbs strongly in the near-infrared (B08) while reflecting more in
the green (B03), so liquid water lands at NDWI > 0 and land / vegetation /
bare soil land at NDWI < 0. A simple ``NDWI > 0`` threshold (McFeeters' original
cutoff) cleanly separates the water body from its surroundings for a clear
reservoir / lake AOI; the threshold is exposed as a tunable lever
(``ndwi_threshold``) for turbid water or for tightening / loosening the mask.

Data source
===========

PC collection ``sentinel-2-l2a`` (Sentinel-2 Level-2A, 10 m surface reflectance):

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    bands:   B03 (Green, 10 m), B08 (NIR, 10 m)
    select:  the LEAST-cloudy scene (``eo:cloud_cover``) intersecting the bbox
             inside the requested datetime window (clouds masquerade as nodata
             / spurious NDWI, so the cleanest scene gives the sharpest mask).

Assets are Azure-Blob COGs behind SAS tokens; this tool signs each asset href
via the PC SAS REST endpoint (``_pc_stac.sas_sign_href``) and reads a
bbox-windowed, EPSG:4326-warped array per band through GDAL ``/vsicurl/``  --  the
same fetch path ``compute_ndvi`` uses. The water mask is vectorized with
``rasterio.features.shapes`` and the polygons are re-emitted as a FlatGeobuf
(EPSG:4326) with a ``water_bodies`` style preset.

Honesty (data-source fallback norm)
===================================

- If NO Sentinel-2 scene intersects the bbox in the window under the cloud cap:
  a typed ``WaterBodyNoImageryError`` (never a fabricated layer).
- If a scene is read but contains NO water above the threshold (a dry inland
  AOI legitimately has none): a typed ``WaterBodyNoWaterError``  --  the agent
  surface narrates an honest "no open water detected" rather than emitting an
  empty layer that reads as success.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, window, threshold, cloud, min_area)`` calls reuse the cached water FGB
in the ``static-30d`` / ``digitize_water_body`` cache prefix.

Tier-1 free (no API key). Heavy emit-free sync raster + vector work  --  belongs
in ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` (server.py) so it runs via
``asyncio.to_thread`` and never stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.fetchers.imagery import _pc_stac
from trid3nt_server.tools.cache import read_through

__all__ = [
    "digitize_water_body",
    "estimate_payload_mb",
    "WaterBodyError",
    "WaterBodyBboxError",
    "WaterBodyNoImageryError",
    "WaterBodyNoWaterError",
    "WaterBodyUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.digitize_water_body")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class WaterBodyError(RuntimeError):
    """Base class for digitize_water_body failures."""

    error_code = "WATER_BODY_ERROR"
    retryable = True


class WaterBodyBboxError(WaterBodyError):
    """Malformed / out-of-range / degenerate / too-large bbox or bad threshold."""

    error_code = "WATER_BODY_INPUT_INVALID"
    retryable = False


class WaterBodyNoImageryError(WaterBodyError):
    """No Sentinel-2 scene covers the bbox in the window under the cloud cap.

    Honest no-imagery signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "WATER_BODY_NO_IMAGERY"
    retryable = False


class WaterBodyNoWaterError(WaterBodyError):
    """A scene was read but no open water sits above the NDWI threshold.

    Honest no-water signal for a dry inland AOI  --  the agent narrates "no
    surface water detected" rather than emitting an empty layer that reads as a
    successful digitization. Non-retryable (the data genuinely shows no water);
    the caller may widen the window, lower ``ndwi_threshold``, or broaden bbox.
    """

    error_code = "WATER_BODY_NO_WATER"
    retryable = False


class WaterBodyUpstreamError(WaterBodyError):
    """A PC STAC search / asset read / vectorization / FGB write failed."""

    error_code = "WATER_BODY_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "sentinel-2-l2a"
_GREEN_BAND = "B03"
_NIR_BAND = "B08"

#: Sentinel-2 native 10 m grid; used to size the bbox-windowed read.
_NATIVE_CELL_M = 10.0

#: Default cloud-cover ceiling (percent). Tighter than NDVI's 30% because a
#: cloud edge fakes high-green/low-NIR pixels that masquerade as water; the
#: least-cloudy match is then chosen.
_DEFAULT_MAX_CLOUD = 20.0

#: Default NDWI threshold (McFeeters 1996): NDWI > 0 == open water.
_DEFAULT_NDWI_THRESHOLD = 0.0

#: Default minimum mapped water-polygon area (m^2). Drops single-pixel /
#: few-pixel NDWI specks (mixed shoreline pixels, wet roofs) so the output is
#: clean water bodies, not noise. One Sentinel-2 pixel is ~100 m^2; 1000 m^2 is
#: ~10 pixels.
_DEFAULT_MIN_AREA_M2 = 1000.0

#: bbox area guardrail (deg^2)  --  matches compute_ndvi. A Sentinel-2 read over
#: a huge AOI spans many MGRS tiles + materializes an enormous grid; the
#: atomic-tool surface is AOI-scoped. ~0.5 deg^2 ~ a county-ish extent.
_MAX_BBOX_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Water-polygon style preset (blue fill). Consumed by publish_layer's
#: vector style registry; falls back gracefully if unregistered.
_STYLE_PRESET = "water_bodies"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="digitize_water_body",
    ttl_class="static-30d",
    source_class="digitize_water_body",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate the emitted water-polygon FlatGeobuf size in MB.

    Water-body polygons are sparse (a handful of polygons even for a big lake);
    a clear reservoir AOI emits well under 1 MB. Scale loosely with bbox area
    (more shoreline complexity = more vertices), floored and capped.
    """
    if bbox is None:
        return 0.5
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 0.5
    return max(0.05, min(20.0, sq_deg * 8.0))


# ---------------------------------------------------------------------------
# bbox / threshold helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise WaterBodyBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise WaterBodyBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise WaterBodyBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise WaterBodyBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise WaterBodyBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise WaterBodyBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for digitize_water_body (Sentinel-2 NDWI is AOI-scoped; narrow the bbox)."
        )


def _validate_threshold(ndwi_threshold: float) -> float:
    try:
        thr = float(ndwi_threshold)
    except (TypeError, ValueError) as exc:
        raise WaterBodyBboxError(
            f"ndwi_threshold must be numeric; got {ndwi_threshold!r}"
        ) from exc
    if not math.isfinite(thr) or not (-1.0 <= thr <= 1.0):
        raise WaterBodyBboxError(
            f"ndwi_threshold must be a finite value in [-1, 1]; got {thr!r}"
        )
    return thr


def _validate_min_area(min_area_m2: float) -> float:
    try:
        a = float(min_area_m2)
    except (TypeError, ValueError) as exc:
        raise WaterBodyBboxError(
            f"min_area_m2 must be numeric; got {min_area_m2!r}"
        ) from exc
    if not math.isfinite(a) or a < 0.0:
        raise WaterBodyBboxError(
            f"min_area_m2 must be a finite value >= 0; got {a!r}"
        )
    return a


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _default_window() -> tuple[str, str]:
    """Default datetime window: a trailing ~14 month window ending today.

    Returns ``(start_iso, end_iso)`` as ``YYYY-MM-DD`` strings. A generous
    window so a recent low-cloud scene is reliably found.
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=425)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Core: search -> per-band windowed read -> NDWI -> mask -> polygons -> FGB.
# ---------------------------------------------------------------------------


def _read_band_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Read ``signed_href`` warped to EPSG:4326 and windowed to ``bbox``.

    Returns a 2-D float32 numpy masked array at ``(height_px, width_px)``.
    Raises ``WaterBodyUpstreamError`` on any read failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    vsicurl = "/vsicurl/" + signed_href
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                dst_transform = rasterio.transform.from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
                )
                dst = np.zeros((height_px, width_px), dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=src.nodata if src.nodata is not None else 0,
                    dst_nodata=0,
                )
        return np.ma.masked_equal(dst.astype("float32"), 0.0)
    except WaterBodyError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise WaterBodyUpstreamError(
            f"Sentinel-2 band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _digitize_water_fgb_bytes(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_cloud_cover: float,
    ndwi_threshold: float,
    min_area_m2: float,
) -> bytes:
    """Search S2, compute NDWI for ``bbox``, threshold + vectorize to water FGB.

    Raises:
        ``WaterBodyNoImageryError``: no scene in the window (honest no-imagery).
        ``WaterBodyNoWaterError``: scene read but no water above threshold.
        ``WaterBodyUpstreamError``: search / read / vectorize / write failure.
    """
    import numpy as np
    import rasterio
    from rasterio import features

    # 1. Pick the least-cloudy intersecting scene in the window.
    try:
        item = _pc_stac.search_least_cloudy_item(
            collection=_COLLECTION,
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
            sort_by_cloud=True,
        )
    except _pc_stac.PCStacNoItemsError as exc:
        raise WaterBodyNoImageryError(
            f"no Sentinel-2 imagery for bbox={bbox} in {datetime_range} "
            f"under {max_cloud_cover}% cloud cover: {exc}"
        ) from exc
    except _pc_stac.PCStacError as exc:
        raise WaterBodyUpstreamError(
            f"Sentinel-2 STAC search failed: {exc}"
        ) from exc

    assets = getattr(item, "assets", {}) or {}
    if _GREEN_BAND not in assets or _NIR_BAND not in assets:
        raise WaterBodyUpstreamError(
            f"Sentinel-2 item {getattr(item, 'id', '?')} missing "
            f"{_GREEN_BAND}/{_NIR_BAND} assets (have {sorted(assets)[:8]})"
        )

    green_href = _pc_stac.sas_sign_href(assets[_GREEN_BAND].href, _COLLECTION)
    nir_href = _pc_stac.sas_sign_href(assets[_NIR_BAND].href, _COLLECTION)

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)

    # 2. Read both bands warped+windowed to the bbox, compute NDWI.
    green = _read_band_window(green_href, bbox, width_px, height_px)
    nir = _read_band_window(nir_href, bbox, width_px, height_px)

    green_f = green.astype("float32")
    nir_f = nir.astype("float32")
    denom = green_f + nir_f
    with np.errstate(divide="ignore", invalid="ignore"):
        ndwi = (green_f - nir_f) / denom
    ndwi = np.ma.masked_invalid(ndwi)
    ndwi = np.ma.masked_where(
        np.ma.getmaskarray(green) | np.ma.getmaskarray(nir), ndwi
    )
    ndwi = np.ma.masked_where(np.abs(denom) < 1e-6, ndwi)

    if ndwi.count() == 0:
        raise WaterBodyNoImageryError(
            f"Sentinel-2 scene {getattr(item, 'id', '?')} produced an all-nodata "
            f"NDWI over bbox={bbox} (scene does not actually cover the AOI)."
        )

    # 3. Threshold to a water mask (NDWI > threshold). Masked / nodata pixels
    #    are NOT water (filled False).
    water_mask = np.ma.filled(ndwi > ndwi_threshold, False).astype(np.uint8)
    water_px = int(water_mask.sum())
    if water_px == 0:
        raise WaterBodyNoWaterError(
            f"no open water above NDWI > {ndwi_threshold} in Sentinel-2 scene "
            f"{getattr(item, 'id', '?')} over bbox={bbox} "
            f"(valid_px={int(ndwi.count())}). The AOI shows no surface water in "
            "this window; try a wider window, a lower ndwi_threshold, or a "
            "broader bbox."
        )

    # 4. Vectorize the mask -> water polygons (rasterio.features.shapes).
    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    try:
        from shapely.geometry import shape

        raw_geoms = [
            shape(geom)
            for geom, val in features.shapes(
                water_mask, mask=water_mask.astype(bool), transform=transform
            )
            if val == 1
        ]
    except Exception as exc:  # noqa: BLE001
        raise WaterBodyUpstreamError(
            f"water-mask vectorization failed for bbox={bbox}: {exc}"
        ) from exc

    if not raw_geoms:
        # Mask had water pixels but no closed polygon emerged  --  treat as
        # honest no-water (defensive; shapes() over a non-empty mask yields
        # at least one polygon in practice).
        raise WaterBodyNoWaterError(
            f"water mask over bbox={bbox} produced no polygons "
            f"(water_px={water_px}); no mappable surface water."
        )

    # 5. Area-filter (drop specks) + emit FlatGeobuf.
    try:
        import geopandas as gpd

        gdf = gpd.GeoDataFrame(
            {"value": [1] * len(raw_geoms)},
            geometry=raw_geoms,
            crs="EPSG:4326",
        )
        # Area in m^2 via Web-Mercator (adequate for a speck filter at AOI scale).
        gdf["area_m2"] = gdf.to_crs(3857).area
        if min_area_m2 > 0.0:
            gdf = gdf[gdf["area_m2"] >= min_area_m2].copy()
    except Exception as exc:  # noqa: BLE001
        raise WaterBodyUpstreamError(
            f"water-polygon area filtering failed for bbox={bbox}: {exc}"
        ) from exc

    if len(gdf) == 0:
        raise WaterBodyNoWaterError(
            f"all detected water polygons over bbox={bbox} were smaller than "
            f"min_area_m2={min_area_m2} m^2 (only NDWI specks; no mappable water "
            "body). Lower min_area_m2 to keep small ponds."
        )

    total_area_m2 = float(gdf["area_m2"].sum())
    # Annotate for narration / downstream zonal use.
    gdf["water_index"] = "NDWI"
    gdf["ndwi_threshold"] = float(ndwi_threshold)

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_water_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as fh:
            fgb_bytes = fh.read()
    except Exception as exc:  # noqa: BLE001
        raise WaterBodyUpstreamError(
            f"water FlatGeobuf write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass

    logger.info(
        "digitize_water_body: scene=%s cc=%.3f bbox=%s -> %d polygon(s) "
        "(%.1f m^2 total, water_px=%d/%d) -> %d-byte FGB",
        getattr(item, "id", "?"),
        float(getattr(item, "properties", {}).get("eo:cloud_cover", -1.0)),
        bbox,
        len(gdf),
        total_area_m2,
        water_px,
        int(ndwi.count()),
        len(fgb_bytes),
    )
    return fgb_bytes


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True, openWorldHint=True (reads an external
    # public STAC API + Azure blobs  --  like fetch_* / the other PC-STAC tools;
    # the open-world test asserts a tool that hits an external API carries it),
    # destructiveHint=False, idempotentHint=True (cache-deduped).
    open_world_hint=True,
)
def digitize_water_body(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    ndwi_threshold: float = _DEFAULT_NDWI_THRESHOLD,
    max_cloud_cover: float = _DEFAULT_MAX_CLOUD,
    min_area_m2: float = _DEFAULT_MIN_AREA_M2,
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Digitize open surface-water bodies (NDWI) for a bbox + recent window.

    Use this when: outlining/digitizing/mapping a lake, reservoir, pond,
    or wide river reach ("draw the boundary of Lake X", "where is the open
    water here?") -- CPU spectral-index path, no GPU/SAM. Also useful for
    a vector water footprint to intersect with other layers, or comparing
    extent between two dates. Do NOT use for: vegetation vigor
    (``compute_ndvi``); land-cover classes (``fetch_landcover``/
    ``extract_landcover_class``); regulatory floodplains
    (``fetch_fema_nfhl_zones``) or modeled inundation
    (``run_model_flood_scenario``); SLR bathtub footprints
    (``fetch_noaa_slr_scenarios``).

    Params:
        bbox: EPSG:4326, <= 0.5 deg^2.
        start_date/end_date: "YYYY-MM-DD" window; default trailing ~14mo.
        ndwi_threshold: NDWI cutoff for water (default 0.0, McFeeters
            1996); lower (e.g. -0.1) for turbid water; range [-1, 1].
        max_cloud_cover: scene cloud ceiling percent (default 20.0).
        min_area_m2: drop polygons smaller than this (default 1000,
            removes few-pixel specks).

    Returns:
        ``LayerURI`` (vector, ``role="primary"``, ``units="m^2"``,
        ``style_preset="water_bodies"``) for a FlatGeobuf of water
        polygons (EPSG:4326) with ``area_m2``, ``water_index``,
        ``ndwi_threshold``. Source: Sentinel-2 L2A via Microsoft Planetary
        Computer STAC (bands B03+B08).

    Raises:
        WaterBodyNoImageryError: no scene in the window/cloud cap.
        WaterBodyNoWaterError: honest no-water result (dry AOI) -- never
            an empty layer read as success.
        WaterBodyBboxError: bad bbox/threshold/min_area.
        WaterBodyUpstreamError: STAC/read/vectorize/write failure.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)
    thr = _validate_threshold(ndwi_threshold)
    min_area = _validate_min_area(min_area_m2)

    if start_date and end_date:
        dt_range = f"{start_date}/{end_date}"
    else:
        s, e = _default_window()
        dt_range = f"{s}/{e}"

    try:
        max_cc = float(max_cloud_cover)
    except (TypeError, ValueError):
        max_cc = _DEFAULT_MAX_CLOUD

    params = {
        "bbox": list(q_bbox),
        "datetime_range": dt_range,
        "ndwi_threshold": thr,
        "max_cloud_cover": max_cc,
        "min_area_m2": min_area,
        "collection": _COLLECTION,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _digitize_water_fgb_bytes(
            q_bbox, dt_range, max_cc, thr, min_area
        ),
    )
    assert result.uri is not None, (
        "digitize_water_body is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"water-bodies-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name="Surface Water Bodies (NDWI)",
        layer_type="vector",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="m^2",
        bbox=q_bbox,
    )
