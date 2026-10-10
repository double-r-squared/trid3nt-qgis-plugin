"""Shared core of the data fetchers.

The typed fetch-error hierarchy, the Nominatim usage-policy User-Agent, and the
bbox validation and resolution-quantization helpers every fetcher pre-applies
before handing params to the cache shim."""

from __future__ import annotations

import math

__all__ = [
    "FetchError",
    "UpstreamAPIError",
    "BboxInvalidError",
    "PixelBudgetExceededError",
    "bbox_pixel_dims",
    "enforce_pixel_budget",
    "round_bbox_to_resolution",
]


class FetchError(RuntimeError):
    """Base class for data-fetch failures; ``error_code`` is the stable wire code, ``actionability`` defaults to "agent" ("user" for a missing credential)."""

    error_code: str = "UPSTREAM_API_ERROR"
    retryable: bool = True
    actionability: str = "agent"

class UpstreamAPIError(FetchError):
    error_code = "UPSTREAM_API_ERROR"
    retryable = True

class BboxInvalidError(FetchError):
    error_code = "BBOX_INVALID"
    retryable = False

class PixelBudgetExceededError(FetchError):
    """The asked resolution over this bbox needs more pixels per axis than the source serves; a fetcher never coarsens, so the request REFUSES."""

    error_code = "PIXEL_BUDGET_EXCEEDED"
    retryable = False

_DEFAULT_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)



def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``BboxInvalidError`` unless ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 with min < max."""
    if len(bbox) != 4:
        raise BboxInvalidError(
            f"bbox must be a 4-tuple (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not (math.isfinite(min_lon) and math.isfinite(min_lat) and math.isfinite(max_lon) and math.isfinite(max_lat)):
        raise BboxInvalidError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise BboxInvalidError(f"bbox lon out of range [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise BboxInvalidError(f"bbox lat out of range [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise BboxInvalidError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )

def round_bbox_to_resolution(
    bbox: tuple[float, float, float, float],
    resolution_m: int,
) -> tuple[float, float, float, float]:
    """Quantize an EPSG:4326 bbox to a ``resolution_m`` grid before cache-keying: mins snap down, maxes up; degrees-per-metre taken at the centre latitude."""
    _validate_bbox(bbox)
    if resolution_m <= 0:
        raise BboxInvalidError(f"resolution_m must be positive; got {resolution_m!r}")

    min_lon, min_lat, max_lon, max_lat = bbox
    # mid_lat is rounded to 4 decimals (~11 m) so sub-meter edge differences give the same snap result.
    mid_lat = round(0.5 * (min_lat + max_lat), 4)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    if m_per_deg_lon < 1e-6:  # near a pole -- fall back to deg-lat
        m_per_deg_lon = 111_320.0

    deg_lat_per_step = resolution_m / m_per_deg_lat
    deg_lon_per_step = resolution_m / m_per_deg_lon

    snapped_min_lon = math.floor(min_lon / deg_lon_per_step) * deg_lon_per_step
    snapped_max_lon = math.ceil(max_lon / deg_lon_per_step) * deg_lon_per_step
    snapped_min_lat = math.floor(min_lat / deg_lat_per_step) * deg_lat_per_step
    snapped_max_lat = math.ceil(max_lat / deg_lat_per_step) * deg_lat_per_step

    # Rounded so JSON canonicalization yields stable strings.
    return (
        round(snapped_min_lon, 9),
        round(snapped_min_lat, 9),
        round(snapped_max_lon, 9),
        round(snapped_max_lat, 9),
    )

def _bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    """Geodesic area of a WGS84 bbox in km2; the ring is densified because top and bottom edges are PARALLELS."""
    import shapely
    from pyproj import Geod

    ring = shapely.segmentize(shapely.box(*bbox), 1.0)
    return abs(Geod(ellps="WGS84").geometry_area_perimeter(ring)[0]) / 1.0e6


def _bbox_axes_m(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    mid_lat = 0.5 * (min_lat + max_lat)
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    return (
        geod.inv(min_lon, mid_lat, max(min_lon, max_lon), mid_lat)[2],
        geod.inv(min_lon, min_lat, min_lon, max(min_lat, max_lat))[2],
    )


def bbox_pixel_dims(
    bbox: tuple[float, float, float, float],
    native_cell_m: float,
    *,
    px_min: int = 16,
    px_max: int = 4096,
) -> tuple[int, int]:
    """``(width_px, height_px)`` for ``bbox`` at ``native_cell_m``, each axis clamped to ``[px_min, px_max]``."""
    width_m, height_m = _bbox_axes_m(bbox)
    width_px = max(px_min, min(px_max, int(round(width_m / native_cell_m)) or px_min))
    height_px = max(px_min, min(px_max, int(round(height_m / native_cell_m)) or px_min))
    return width_px, height_px


def enforce_pixel_budget(
    bbox: tuple[float, float, float, float],
    resolution_m: float,
    *,
    budget_px: int,
    source: str,
) -> None:
    """Raise ``PixelBudgetExceededError`` unless ``bbox`` at ``resolution_m`` fits ``budget_px`` on its long axis;
    the message names the budget, the asked resolution and the one that fits."""
    long_axis_m = max(_bbox_axes_m(bbox))
    fits_res = int(math.ceil(long_axis_m / budget_px))
    if resolution_m >= fits_res:
        return
    raise PixelBudgetExceededError(
        f"{source}: bbox={tuple(bbox)} at resolution_m={resolution_m:g} needs "
        f"{int(math.ceil(long_axis_m / resolution_m))} px on its long axis, past the "
        f"{budget_px} px/axis budget this source serves. Nothing was coarsened: "
        f"re-ask at resolution_m={fits_res} m (the finest spacing that fits this "
        "bbox) or coarser, or ask for a smaller bbox."
    )
