"""``compute_ndvi`` - Sentinel-2 NDVI vegetation index.

NDVI = (NIR - Red) / (NIR + Red), clamped to its own -1..1 domain. No scene in
the window under the cloud cap is a typed refusal, never a fabricated layer.
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
from trid3nt_server.tools.fetchers._fetch_common import bbox_pixel_dims
from trid3nt_server.tools.derive import _pc_search
from trid3nt_server.tools.cache import read_through

__all__ = [
    "compute_ndvi",
    "estimate_payload_mb",
    "NDVIError",
    "NDVIBboxError",
    "NDVINoImageryError",
    "NDVIUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_ndvi.compute_ndvi")


# ---------------------------------------------------------------------------
# Error types (typed-error surface).
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
    """No Sentinel-2 scene covers the bbox in the window under the cloud cap; an
    honest miss, never a fabricated layer.
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

#: Cloud-cover ceiling in percent for scene selection, generous so a typical AOI
#: finds a usable scene; the least-cloudy match is then chosen.
_DEFAULT_MAX_CLOUD = 30.0

#: bbox area guardrail in deg^2, not a memory ceiling: the grid is px-clamped to
#: [16,4096] per axis, so an AOI up to this cap coarsens to ~20-24 m/px and the
#: COG stays bounded whatever the area. About a county extent; beyond it, refuse.
_MAX_BBOX_DEG2 = 1.0

#: Native-10m comfort window (deg^2). Below this an AOI fits the 4096px grid at
#: ~native 10 m; between this and _MAX_BBOX_DEG2 the px-clamp coarsens the cell
#: (honest auto-coarsen, logged) rather than rejecting.
_NATIVE_COMFORT_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: NDVI runs -1 to 1 by definition, so the row pins that domain.
_STYLE = {"kind": "continuous", "ramp": "rdylgn", "units": "index",
         "label": "NDVI", "scale": {"policy": "fixed",
         "range": [-1, 1], "transform": "linear"}}


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
# Payload estimator (chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """The emitted COG's size in MB, scaled linearly with bbox area and floored."""
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
    # Between the comfort window and the cap nothing raises: the px-clamp
    # already coarsens the grid, so the note is what makes the resolution trade
    # visible rather than silent.
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
    """``(start_iso, end_iso)``: a trailing 14-month window, wide enough that a
    low-cloud scene is found even outside peak season.
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
    """``signed_href`` warped to EPSG:4326 and windowed to ``bbox`` as a 2-D
    float32 masked array; any read failure raises ``NDVIUpstreamError``.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    vsicurl = "/vsicurl/" + signed_href
    try:
        with rasterio.Env(**_pc_search.VSICURL_ENV_KW):
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
    """Search Sentinel-2, compute NDVI for ``bbox`` and return a single-band
    float32 COG; no scene in the window raises ``NDVINoImageryError``.
    """
    import numpy as np
    import rasterio

    try:
        item = _pc_search.search_least_cloudy_item(
            collection=_COLLECTION,
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
            sort_by_cloud=True,
        )
    except _pc_search.PCStacNoItemsError as exc:
        raise NDVINoImageryError(
            f"no Sentinel-2 imagery for bbox={bbox} in {datetime_range} "
            f"under {max_cloud_cover}% cloud cover: {exc}"
        ) from exc
    except _pc_search.PCStacError as exc:
        raise NDVIUpstreamError(f"Sentinel-2 STAC search failed: {exc}") from exc

    assets = getattr(item, "assets", {}) or {}
    if _RED_BAND not in assets or _NIR_BAND not in assets:
        raise NDVIUpstreamError(
            f"Sentinel-2 item {getattr(item, 'id', '?')} missing "
            f"{_RED_BAND}/{_NIR_BAND} assets (have {sorted(assets)[:8]})"
        )

    red_href = assets[_RED_BAND].href
    nir_href = assets[_NIR_BAND].href

    width_px, height_px = bbox_pixel_dims(bbox, _NATIVE_CELL_M)

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
    # open_world_hint is reserved for fetch_* / web_fetch / catalog_* tools; a
    # compute_* tool never carries it.
    open_world_hint=False,
)
def compute_ndvi(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    max_cloud_cover: float = _DEFAULT_MAX_CLOUD,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute Sentinel-2 NDVI (vegetation vigor) for a bbox + time window.

    Use this, not ``fetch_sentinel2_truecolor``, when you want NDVI values
    rather than the picture: vegetation condition, greenness, canopy vigor, or
    the vegetation input to a composite. Water and bare soil sit near 0, sparse
    vegetation 0.2-0.5, dense canopy 0.6-0.9. Not for land-cover classes
    (``fetch_landcover``) or true-color imagery (``fetch_naip``).

    Params:
        bbox: EPSG:4326, at most 1.0 deg^2. Under about 0.5 deg^2 reads at
            native 10 m; larger auto-coarsens to fit a 4096 px grid.
        start_date/end_date: "YYYY-MM-DD"; default a trailing 14 months.
        max_cloud_cover: scene ceiling in percent, default 30.0; the
            least-cloudy match is chosen.

    Returns a single-band float32 COG in -1..1 from Sentinel-2 L2A B04 and B08.
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
        style=_STYLE,
        role="primary",
        units="NDVI (-1..1)",
    )
