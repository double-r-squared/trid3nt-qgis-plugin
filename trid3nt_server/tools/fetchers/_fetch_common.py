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


#
# These RuntimeError subclasses carry a stable ``error_code`` for the error frame
# the agent surface emits when a fetch fails. Nothing in this module catches them:
# the ``read_through`` contract is "re-raise on fetcher failure; no sentinel".


class FetchError(RuntimeError):
    """Base class for data-fetch failures; ``error_code`` is the stable wire code.
    ``actionability`` is closed over ``{"agent", "user", "operator"}`` and defaults
    to "agent"; a subclass raised for a missing credential overrides it to "user"."""

    error_code: str = "UPSTREAM_API_ERROR"
    retryable: bool = True
    actionability: str = "agent"

class UpstreamAPIError(FetchError):
    """An upstream public-data API returned an error or timed out."""

    error_code = "UPSTREAM_API_ERROR"
    retryable = True

class BboxInvalidError(FetchError):
    """The bbox failed validation (degenerate, out of CRS range, too large)."""

    error_code = "BBOX_INVALID"
    retryable = False

class PixelBudgetExceededError(FetchError):
    """The asked resolution over this bbox needs more pixels per axis than the source
    serves. A fetcher never coarsens on its own, so the request REFUSES naming the
    budget and the resolution that fits, and the caller chooses the grid it gets."""

    error_code = "PIXEL_BUDGET_EXCEEDED"
    retryable = False

# Nominatim usage policy requires a descriptive User-Agent identifying the
# application + a contact. We bake the project name + repo URL; override the
# contact email via env var ``TRID3NT_NOMINATIM_USER_AGENT`` for ops.
_DEFAULT_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)



def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``BboxInvalidError`` unless ``bbox`` is ``(min_lon, min_lat, max_lon,
    max_lat)`` in EPSG:4326 with min < max on both axes, lons in ``[-180, 180]``
    and lats in ``[-90, 90]``."""
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
    """Quantize an EPSG:4326 bbox to a ``resolution_m`` grid before cache-keying:
    mins snap down and maxes up, so the result always covers the input. The
    degrees-per-metre conversion is taken at the bbox centre latitude."""
    _validate_bbox(bbox)
    if resolution_m <= 0:
        raise BboxInvalidError(f"resolution_m must be positive; got {resolution_m!r}")

    min_lon, min_lat, max_lon, max_lat = bbox
    # Stabilize mid_lat by rounding to 4 decimals (~11m) so two callers whose bbox
    # edges differ by sub-meter floats don't get different m_per_deg_lon factors,
    # which would defeat the dedup property: same grid cell, same snap result.
    mid_lat = round(0.5 * (min_lat + max_lat), 4)
    # 1 degree of latitude ~ 111_320 m; 1 degree of longitude ~ 111_320 * cos(lat) m.
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

    # Round to a reasonable number of digits so the JSON canonicalization
    # produces stable strings (float repr quirks otherwise leak into the key).
    return (
        round(snapped_min_lon, 9),
        round(snapped_min_lat, 9),
        round(snapped_max_lon, 9),
        round(snapped_max_lat, 9),
    )

def _bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    """Geodesic area of a WGS84 bbox in square kilometres. The ring is densified
    first: a bbox's top and bottom edges are PARALLELS, and an undensified geodesic
    cuts the corner poleward. Over half the globe is degenerate as a ring here."""
    import shapely
    from pyproj import Geod

    ring = shapely.segmentize(shapely.box(*bbox), 1.0)
    return abs(Geod(ellps="WGS84").geometry_area_perimeter(ring)[0]) / 1.0e6


def _bbox_axes_m(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """Geodesic ``(width_m, height_m)`` of a WGS84 bbox, the width measured along the
    mid-latitude parallel and the height along the western meridian."""
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
    """``(width_px, height_px)`` for ``bbox`` at ``native_cell_m``. Each axis is
    clamped to ``[px_min, px_max]``, so a large AOI never materializes an unbounded
    grid."""
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
    """Raise ``PixelBudgetExceededError`` unless ``bbox`` at ``resolution_m`` fits
    ``budget_px`` pixels on its long axis. The resolution asked is the resolution
    fetched, so a request past the budget refuses rather than coarsening: the message
    names the budget, the asked resolution and the resolution that fits this bbox, and
    the caller re-asks."""
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
