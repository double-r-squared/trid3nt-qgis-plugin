"""``compute_ndvi`` atomic tool  --  Sentinel-2 NDVI vegetation index (conservation).

Computes the Normalized Difference Vegetation Index

    NDVI = (NIR - Red) / (NIR + Red)

for a bbox + time window from Sentinel-2 L2A surface reflectance, via the
Microsoft Planetary Computer (PC) STAC catalog. NDVI is the canonical
vegetation-vigor / green-biomass index (range -1..1; bare soil / water near 0,
dense healthy vegetation 0.6-0.9). It is the vegetation layer in the SC-DNR-style
conservation-priority stack (``model_conservation_priority``).

Data source
===========

PC collection ``sentinel-2-l2a`` (Sentinel-2 Level-2A, 10 m surface reflectance):

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    bands:   B04 (Red, 10 m), B08 (NIR, 10 m)
    select:  the LEAST-cloudy scene (``eo:cloud_cover``) intersecting the bbox
             inside the requested datetime window.

Assets are Azure-Blob COGs behind SAS tokens; this tool signs each asset href
via the PC SAS REST endpoint (see ``_pc_stac.sas_sign_href``) and reads a
bbox-windowed, EPSG:4326-warped array per band through GDAL ``/vsicurl/``. NDVI
is computed in-memory and re-emitted as a single-band float32 COG (-1..1) with a
green vegetation colormap (``ndvi`` style preset -> RdYlGn ramp).

Honesty (data-source fallback norm): if NO Sentinel-2 scene intersects the bbox
in the window (or none under the cloud threshold), a typed
``NDVINoImageryError`` is raised  --  never a fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, start, end, max_cloud_cover)`` calls reuse the cached NDVI COG in the
``static-30d`` / ``ndvi`` cache prefix.

Tier-1 free (no API key). Heavy emit-free sync raster work  --  registered in
``_ALWAYS_OFFLOAD_SYNC_TOOLS`` so it runs via ``asyncio.to_thread`` and never
stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from . import _pc_stac
from .cache import read_through

__all__ = [
    "compute_ndvi",
    "estimate_payload_mb",
    "NDVIError",
    "NDVIBboxError",
    "NDVINoImageryError",
    "NDVIUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.compute_ndvi")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NDVIError(RuntimeError):
    """Base class for compute_ndvi failures."""

    error_code = "NDVI_ERROR"
    retryable = True


class NDVIBboxError(NDVIError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "NDVI_BBOX_INVALID"
    retryable = False


class NDVINoImageryError(NDVIError):
    """No Sentinel-2 scene covers the bbox in the window under the cloud cap.

    Honest no-imagery signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "NDVI_NO_IMAGERY"
    retryable = False


class NDVIUpstreamError(NDVIError):
    """A PC STAC search / asset read / COG write failed."""

    error_code = "NDVI_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "sentinel-2-l2a"
_RED_BAND = "B04"
_NIR_BAND = "B08"

#: Sentinel-2 native 10 m grid; used to size the bbox-windowed read.
_NATIVE_CELL_M = 10.0

#: Default cloud-cover ceiling (percent) for scene selection. Generous so a
#: typical AOI finds a usable scene; the least-cloudy match is then chosen.
_DEFAULT_MAX_CLOUD = 30.0

#: bbox area guardrail (deg^2). NATE 2026-06-26: raised 0.5 -> 1.0. This is NOT a
#: memory ceiling: the emitted grid is already px-clamped to [16,4096]/axis by
#: _pc_stac.bbox_pixel_dims, so a ~0.77-1.0 deg^2 AOI clamps to 4096x4096 and
#: auto-coarsens to ~20-24 m/px -- COG byte size stays bounded regardless of
#: bbox area. The old 0.5 cap rejected legitimate county-ish AOIs (~0.77 deg^2)
#: with no recourse. ~1.0 deg^2 ~ a county-ish extent; beyond it we still raise.
_MAX_BBOX_DEG2 = 1.0

#: Native-10m comfort window (deg^2). Below this an AOI fits the 4096px grid at
#: ~native 10 m; between this and _MAX_BBOX_DEG2 the px-clamp coarsens the cell
#: (honest auto-coarsen, logged) rather than rejecting. NATE 2026-06-26.
_NATIVE_COMFORT_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: NDVI colormap style preset  --  green vegetation ramp (RdYlGn rescaled -1..1).
#: Registered in publish_layer._TITILER_STYLE_REGISTRY this sprint.
_STYLE_PRESET = "ndvi"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compute_ndvi",
    ttl_class="static-30d",
    source_class="ndvi",
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
    """Estimate emitted NDVI COG size in MB.

    A single-band float32 LZW-COG at 10 m runs ~150 MB / sq-deg uncompressed;
    NDVI compresses well (smooth ramp). Scale linearly with bbox area, floored.
    """
    if bbox is None:
        return 5.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 5.0
    return max(0.5, sq_deg * 60.0)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise NDVIBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NDVIBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NDVIBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NDVIBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NDVIBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise NDVIBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for compute_ndvi. AOIs up to ~1.0 deg^2 are auto-coarsened to fit "
            "the 4096px grid (effective cell ~= bbox_m/4096); narrow the bbox for "
            "native 10 m."
        )
    # NATE 2026-06-26: between the native-10m comfort window and the cap we do
    # NOT raise -- the px-clamp ([16,4096]/axis in _pc_stac.bbox_pixel_dims)
    # already coarsens the grid so the COG stays bounded. Log an honest note so
    # the user understands the resolution trade (native 10 m -> ~20-24 m/px).
    if area > _NATIVE_COMFORT_DEG2:
        logger.info(
            "compute_ndvi: bbox area %.3f deg^2 exceeds the ~%.2f deg^2 native-10m "
            "comfort window; the 4096px grid clamp auto-coarsens this AOI to an "
            "effective cell ~= bbox_m/4096 (~20-24 m/px). Narrow the bbox for "
            "native 10 m resolution.",
            area,
            _NATIVE_COMFORT_DEG2,
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _default_window() -> tuple[str, str]:
    """Default datetime window: the most recent full growing season-ish year.

    Returns ``(start_iso, end_iso)`` as ``YYYY-MM-DD`` strings. Defaults to a
    trailing ~14 month window so a recent low-cloud scene is reliably found
    even outside peak season.
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=425)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Core: search -> per-band windowed read -> NDVI -> COG bytes.
# ---------------------------------------------------------------------------


def _read_band_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Read ``signed_href`` warped to EPSG:4326 and windowed to ``bbox``.

    Returns a 2-D float32 numpy masked array at ``(height_px, width_px)``.
    Raises ``NDVIUpstreamError`` on any read failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    vsicurl = "/vsicurl/" + signed_href
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                # Destination grid: the requested bbox at the requested size in
                # EPSG:4326. reproject() resamples the source (UTM) into it.
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
        masked = np.ma.masked_equal(dst.astype("float32"), 0.0)
        return masked
    except NDVIError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise NDVIUpstreamError(
            f"Sentinel-2 band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _compute_ndvi_cog_bytes(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_cloud_cover: float,
) -> bytes:
    """Search S2, compute NDVI for ``bbox``, return a single-band float32 COG.

    Raises:
        ``NDVINoImageryError``: no scene in the window (honest no-imagery).
        ``NDVIUpstreamError``: search / read / write failure.
    """
    import numpy as np
    import rasterio

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
        raise NDVINoImageryError(
            f"no Sentinel-2 imagery for bbox={bbox} in {datetime_range} "
            f"under {max_cloud_cover}% cloud cover: {exc}"
        ) from exc
    except _pc_stac.PCStacError as exc:
        raise NDVIUpstreamError(f"Sentinel-2 STAC search failed: {exc}") from exc

    assets = getattr(item, "assets", {}) or {}
    if _RED_BAND not in assets or _NIR_BAND not in assets:
        raise NDVIUpstreamError(
            f"Sentinel-2 item {getattr(item, 'id', '?')} missing "
            f"{_RED_BAND}/{_NIR_BAND} assets (have {sorted(assets)[:8]})"
        )

    red_href = _pc_stac.sas_sign_href(assets[_RED_BAND].href, _COLLECTION)
    nir_href = _pc_stac.sas_sign_href(assets[_NIR_BAND].href, _COLLECTION)

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)

    # 2. Read each band warped+windowed to the bbox, compute NDVI.
    red = _read_band_window(red_href, bbox, width_px, height_px)
    nir = _read_band_window(nir_href, bbox, width_px, height_px)

    red_f = red.astype("float32")
    nir_f = nir.astype("float32")
    denom = nir_f + red_f
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir_f - red_f) / denom
    # Mask where either band was nodata OR the denominator is ~0.
    ndvi = np.ma.masked_invalid(ndvi)
    ndvi = np.ma.masked_where(np.ma.getmaskarray(red) | np.ma.getmaskarray(nir), ndvi)
    ndvi = np.ma.masked_where(np.abs(denom) < 1e-6, ndvi)
    # Clamp to the physical NDVI range.
    ndvi = np.ma.clip(ndvi, -1.0, 1.0)

    if ndvi.count() == 0:
        raise NDVINoImageryError(
            f"Sentinel-2 scene {getattr(item, 'id', '?')} produced an all-nodata "
            f"NDVI over bbox={bbox} (scene does not actually cover the AOI)."
        )

    filled = ndvi.filled(np.nan).astype("float32")

    # 3. Write a single-band float32 COG (LZW, NaN nodata) in EPSG:4326.
    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_ndvi_"
        ) as f:
            tmp_path = f.name
        profile = dict(
            driver="COG",
            dtype="float32",
            count=1,
            height=height_px,
            width=width_px,
            crs="EPSG:4326",
            transform=transform,
            nodata=float("nan"),
            compress="LZW",
        )
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(filled, 1)
        with open(tmp_path, "rb") as fh:
            cog_bytes = fh.read()
    except Exception as exc:  # noqa: BLE001
        raise NDVIUpstreamError(f"NDVI COG write failed for bbox={bbox}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    logger.info(
        "compute_ndvi: scene=%s cc=%.2f bbox=%s -> %d-byte COG (%dx%d, valid=%d)",
        getattr(item, "id", "?"),
        float(getattr(item, "properties", {}).get("eo:cloud_cover", -1.0)),
        bbox,
        len(cog_bytes),
        width_px,
        height_px,
        int(ndvi.count()),
    )
    return cog_bytes


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=False (a local raster compute
    # -- like the other compute_* tools; the annotation contract reserves
    # open_world_hint for fetch_* / web_fetch / catalog_* external-API tools, and
    # test_open_world_tools_are_fetchers_or_external forbids a compute_* tool from
    # carrying it), destructiveHint=False, idempotentHint=True (cache-deduped).
    open_world_hint=False,
)
def compute_ndvi(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    max_cloud_cover: float = _DEFAULT_MAX_CLOUD,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute Sentinel-2 NDVI (vegetation vigor) for a bbox + time window.

    Use this (not fetch_sentinel2_truecolor) when you want the NDVI vegetation-index values, not the raw true-color picture.

    **What it does:** Searches the Microsoft Planetary Computer for the
    least-cloudy Sentinel-2 L2A scene intersecting ``bbox`` inside the time
    window, reads the Red (B04) and NIR (B08) 10 m bands clipped to the bbox,
    computes ``NDVI = (NIR - Red) / (NIR + Red)`` per pixel, and returns a
    single-band float32 NDVI COG (range -1..1) with a green vegetation colormap.

    NDVI is the standard greenness / live-biomass index: water and bare soil
    sit near 0 (or negative), sparse vegetation 0.2-0.5, dense healthy canopy
    0.6-0.9. It is the vegetation layer in the conservation-priority stack.

    **When to use:**
    - User wants vegetation condition, greenness, canopy vigor, or a
      "where is the healthy vegetation" map for an area.
    - As the vegetation input to ``model_conservation_priority`` (SC-DNR-style
      species + vegetation + biodiversity stack).
    - Comparing vegetation between two dates (call twice with different windows).

    **When NOT to use:**
    - Land-cover CLASSES (forest / crop / urban)  --  use ``fetch_landcover`` /
      ``extract_landcover_class``; NDVI is a continuous index, not a classifier.
    - High-res true-color aerial imagery  --  use ``fetch_naip``.
    - Areas with persistent cloud cover in the window (raise the window or the
      ``max_cloud_cover`` ceiling); a no-imagery result is an honest typed error.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 1.0 deg^2). AOIs under ~0.5 deg^2 read at native
      10 m; larger AOIs (up to ~1.0 deg^2) are auto-coarsened to fit the 4096px
      grid (effective cell ~= bbox_m/4096, ~20-24 m/px); narrow the bbox for
      native 10 m.
    - ``start_date`` / ``end_date`` (str, optional): ``"YYYY-MM-DD"`` window
      bounds. Default: a trailing ~14-month window ending today.
    - ``max_cloud_cover`` (float, default 30.0): only scenes below this
      ``eo:cloud_cover`` percent are considered; the least-cloudy is chosen.

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="primary"``,
    ``units="NDVI (-1..1)"``) pointing at a single-band float32 COG in the
    ``static-30d``/``ndvi`` cache prefix. ``style_preset="ndvi"`` (RdYlGn ramp).

    **Data source:** Sentinel-2 Level-2A via the Microsoft Planetary Computer
    STAC (``sentinel-2-l2a`` collection; bands B04 + B08).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, window,
    cloud)`` calls reuse the cached NDVI COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)

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
        "max_cloud_cover": max_cc,
        "collection": _COLLECTION,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _compute_ndvi_cog_bytes(q_bbox, dt_range, max_cc),
    )
    assert result.uri is not None, (
        "compute_ndvi is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"ndvi-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name="Sentinel-2 NDVI",
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="NDVI (-1..1)",
    )
