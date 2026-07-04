"""Shared internals for the NOAA OCM SLR Viewer RASTER siblings.

Backs ``fetch_noaa_slr_confidence`` (the ``conf_*`` mapping-confidence services)
and ``fetch_noaa_slr_marsh`` (the ``marsh_*`` marsh-migration services). NOT a
registered tool (no ``@register_tool``) -- it is the common data path the two
thin tool modules call.

Both products are NOAA OCM ``dc_slr`` ArcGIS MapServer services that publish a
SYMBOLIZED raster layer (a baked color scheme, not raw values). The fetch path:
  ``MapServer/export`` -> a rendered RGBA PNG over the bbox in EPSG:4326
  -> georeference to a 4-band RGBA COG (the bbox transform)
so publish_layer's RGBA/multiband passthrough renders the baked symbology
directly (no colormap, no new style-registry row needed) -- same as the GLM /
GOES transparent-overlay rasters.

Honesty floor: a successful-but-fully-transparent export (a bbox with no SLR
coverage at that level) returns a valid transparent COG (the layer appears,
renders nothing) and logs -- it never fabricates content. HTTP/parse failures
raise a typed upstream error.

ASCII only.
"""

from __future__ import annotations

import io
import logging
import math
import os
import tempfile
from typing import Any

import httpx

#: NOAA OCM SLR Viewer ArcGIS REST MapServer folder (conf_* + marsh_* live here).
SLR_BASE_URL = "https://coast.noaa.gov/arcgis/rest/services/dc_slr"

_HTTP_TIMEOUT_S = 60.0
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: Default output cell size (deg). The SLR confidence / marsh symbology is coarse
#: (a planning-level overlay), so ~50 m is plenty; finer is opt-in via res_deg.
_DEFAULT_RES_DEG = 0.0005
#: MapServer/export pixel-dimension cap (NOAA rejects very large requests).
_MAX_PX = 2048
_MIN_PX = 16

logger = logging.getLogger("grace2_agent.tools._noaa_slr_raster")


# ---------------------------------------------------------------------------
# Shared typed errors (FR-AS-11 surface).
# ---------------------------------------------------------------------------
class NOAASLRRasterError(RuntimeError):
    error_code: str = "NOAA_SLR_RASTER_ERROR"
    retryable: bool = True


class NOAASLRRasterInputError(NOAASLRRasterError):
    error_code = "NOAA_SLR_RASTER_INPUT_INVALID"
    retryable = False


class NOAASLRRasterUpstreamError(NOAASLRRasterError):
    error_code = "NOAA_SLR_RASTER_UPSTREAM_ERROR"
    retryable = True


class NOAASLRRasterEmptyError(NOAASLRRasterError):
    error_code = "NOAA_SLR_RASTER_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------
def validate_bbox(
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    """Validate + normalize the bbox. Raises a typed error BEFORE any network call."""
    if bbox is None:
        raise NOAASLRRasterInputError(
            "bbox is required (min_lon, min_lat, max_lon, max_lat) in EPSG:4326; "
            "the NOAA SLR Viewer is a CONUS coastal product, not global."
        )
    if not (isinstance(bbox, (tuple, list)) and len(bbox) == 4):
        raise NOAASLRRasterInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise NOAASLRRasterInputError(f"bbox values must be numeric; got {bbox!r}") from exc
    vals = (min_lon, min_lat, max_lon, max_lat)
    if not all(math.isfinite(v) for v in vals):
        raise NOAASLRRasterInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0
            and -90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NOAASLRRasterInputError(f"bbox out of EPSG:4326 range: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NOAASLRRasterInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    return (min_lon, min_lat, max_lon, max_lat)


def round_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def resolve_res_deg(res_deg: float | None) -> float:
    if res_deg is None:
        return _DEFAULT_RES_DEG
    try:
        rd = float(res_deg)
    except (TypeError, ValueError) as exc:
        raise NOAASLRRasterInputError(f"res_deg must be numeric; got {res_deg!r}") from exc
    if not (math.isfinite(rd) and rd > 0):
        raise NOAASLRRasterInputError(f"res_deg must be a positive number; got {res_deg!r}")
    return rd


def grid_size(bbox: tuple[float, float, float, float], res_deg: float) -> tuple[int, int]:
    min_lon, min_lat, max_lon, max_lat = bbox
    w = max(_MIN_PX, min(_MAX_PX, int(math.ceil((max_lon - min_lon) / res_deg))))
    h = max(_MIN_PX, min(_MAX_PX, int(math.ceil((max_lat - min_lat) / res_deg))))
    return w, h


def estimate_payload_mb_for(
    bbox: tuple[float, float, float, float] | None,
    res_deg: float | None = None,
) -> float:
    """Estimate the emitted RGBA COG size in MB (DEFLATE-compressed symbology)."""
    if bbox is None:
        return 3.0
    try:
        b = round_bbox(validate_bbox(bbox))
        w, h = grid_size(b, resolve_res_deg(res_deg))
    except NOAASLRRasterError:
        return 3.0
    # 4-band uint8, ~0.25 compression on flat symbology.
    return max(0.1, min(60.0, w * h * 4 * 0.25 / 1e6))


# ---------------------------------------------------------------------------
# Core: MapServer/export -> georeferenced RGBA COG bytes (the cache fetch_fn).
# ---------------------------------------------------------------------------
def export_slr_raster_cog_bytes(
    service_name: str,
    bbox: tuple[float, float, float, float],
    res_deg: float = _DEFAULT_RES_DEG,
) -> bytes:
    """``MapServer/export`` the symbolized raster over ``bbox`` -> 4-band RGBA COG bytes.

    ``service_name`` is e.g. ``"conf_3ft"`` or ``"marsh_300"``. Raises
    ``NOAASLRRasterUpstreamError`` on HTTP / decode failure.
    """
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.enums import ColorInterp
    from rasterio.transform import from_bounds

    width, height = grid_size(bbox, res_deg)
    url = f"{SLR_BASE_URL}/{service_name}/MapServer/export"
    params = {
        "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": f"{width},{height}",
        "format": "png32",
        "transparent": "true",
        "f": "image",
    }
    logger.info("NOAA SLR export: GET %s size=%dx%d bbox=%s", url, width, height, bbox)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise NOAASLRRasterUpstreamError(f"NOAA SLR export failed url={url}: {exc}") from exc
    ct = resp.headers.get("content-type", "")
    if resp.status_code >= 400 or "image" not in ct:
        raise NOAASLRRasterUpstreamError(
            f"NOAA SLR export HTTP {resp.status_code} content-type={ct!r} url={url}: "
            f"{resp.text[:300]!r}"
        )
    try:
        im = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001 -- undecodable upstream payload
        raise NOAASLRRasterUpstreamError(
            f"NOAA SLR export returned an undecodable image url={url}: {exc}"
        ) from exc

    arr = np.asarray(im, dtype=np.uint8)  # (H, W, 4)
    out_h, out_w = arr.shape[0], arr.shape[1]
    chw = np.transpose(arr, (2, 0, 1))  # (4, H, W)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], out_w, out_h)

    fd, path = tempfile.mkstemp(suffix=".tif", prefix="grace2_noaa_slr_")
    os.close(fd)
    try:
        profile = {
            "driver": "COG",
            "dtype": "uint8",
            "count": 4,
            "height": out_h,
            "width": out_w,
            "crs": "EPSG:4326",
            "transform": transform,
            "compress": "DEFLATE",
        }
        try:
            with rasterio.open(path, "w", **profile) as dst:
                dst.write(chw)
                dst.colorinterp = (
                    ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha,
                )
        except Exception as exc:  # noqa: BLE001 -- COG driver may be unavailable
            logger.warning("NOAA SLR RGBA COG write failed (%s); GTiff fallback", exc)
            profile["driver"] = "GTiff"
            profile["tiled"] = True
            with rasterio.open(path, "w", **profile) as dst:
                dst.write(chw)
                dst.colorinterp = (
                    ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha,
                )
        lit = float((arr[:, :, 3] > 0).mean())
        if lit == 0.0:
            logger.warning(
                "NOAA SLR %s: export is fully transparent for bbox=%s (no coverage at "
                "this level) -- returning an empty (transparent) overlay", service_name, bbox,
            )
        else:
            logger.info("NOAA SLR %s: %dx%d, %.1f%% non-transparent", service_name, out_w, out_h, 100 * lit)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
