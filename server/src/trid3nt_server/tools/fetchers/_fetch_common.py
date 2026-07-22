"""Shared core of the data fetchers (split from the original multi-tool
``data_fetch`` module, job-0033): the typed fetch-error hierarchy
(``FetchError`` / ``UpstreamAPIError`` / ``BboxInvalidError``), the Nominatim
usage-policy User-Agent, and the bbox validation / resolution-quantization
helpers every fetcher pre-applies before handing params to the FR-DC-3 cache
shim (OQ-32-QUANTIZATION-LOCATION: engine-side quantize).

Tool-specific error subclasses (``GeocodeNoMatchError``, ``DemPartialCoverageError``,
``PrecipForcingUnavailableError``, ...) live next to their tool module.
"""

from __future__ import annotations

import math

__all__ = [
    "FetchError",
    "UpstreamAPIError",
    "BboxInvalidError",
    "round_bbox_to_resolution",
]


# ---------------------------------------------------------------------------
# Error codes registered by this module (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------
#
# These RuntimeError subclasses carry a stable ``error_code`` for the
# WebSocket A.6 error frame the agent surface emits when a fetch fails. They
# are caught nowhere inside this module — the ``read_through`` contract is
# "re-raise on fetcher failure; no sentinel" — so server-side error handling
# (server.py M1) maps them to A.6 codes via the agent's error surface (job-
# 0035 lands the mapping; for now they bubble up).


class FetchError(RuntimeError):
    """Base class for data-fetch failures. ``error_code`` is the A.6 code."""

    error_code: str = "UPSTREAM_API_ERROR"
    retryable: bool = True

class UpstreamAPIError(FetchError):
    """An upstream public-data API returned an error or timed out."""

    error_code = "UPSTREAM_API_ERROR"
    retryable = True

class BboxInvalidError(FetchError):
    """The bbox failed validation (degenerate, out of CRS range, too large)."""

    error_code = "BBOX_INVALID"
    retryable = False

# Nominatim usage policy requires a descriptive User-Agent identifying the
# application + a contact. We bake the project name + repo URL; override the
# contact email via env var ``TRID3NT_NOMINATIM_USER_AGENT`` for ops.
_DEFAULT_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# ---------------------------------------------------------------------------
# bbox helpers (FR-DC-3 / OQ-32-QUANTIZATION-LOCATION: engine-side quantize).
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``BboxInvalidError`` if ``bbox`` is degenerate or out of WGS84 range.

    A valid bbox is ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326,
    with min < max on both axes, lons in ``[-180, 180]`` and lats in
    ``[-90, 90]``.
    """
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
    """Quantize a WGS84 bbox to a per-source resolution grid before cache-keying.

    Rationale: two callers asking for the same area at the same resolution
    should hit the same cache entry even if their bbox edges differ by a few
    floating-point meters. We snap each corner to the nearest grid line whose
    spacing in degrees matches ``resolution_m`` (using a degrees-per-meter
    conversion at the bbox center latitude — good enough for any sub-state
    bbox; per-source overrides can refine).

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        resolution_m: target grid spacing in meters (e.g. 10 for 3DEP 10m).

    Returns:
        A quantized bbox tuple. Always slightly larger than the input bbox
        (snaps mins down and maxes up) so the requested area is covered.

    Surfaced as the engine-side resolution of OQ-32-QUANTIZATION-LOCATION:
    the cache shim's contract is canonicalize+hash; per-source quantization
    is engine-owned domain knowledge.
    """
    _validate_bbox(bbox)
    if resolution_m <= 0:
        raise BboxInvalidError(f"resolution_m must be positive; got {resolution_m!r}")

    min_lon, min_lat, max_lon, max_lat = bbox
    # Stabilize mid_lat by rounding to 4 decimals (~11m) so two callers whose
    # bbox edges differ by sub-meter floats don't get different
    # m_per_deg_lon factors (which would defeat the dedup-via-quantization
    # property — same grid cell must yield same snap result).
    mid_lat = round(0.5 * (min_lat + max_lat), 4)
    # 1 degree of latitude ~ 111_320 m; 1 degree of longitude ~ 111_320 * cos(lat) m.
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    if m_per_deg_lon < 1e-6:  # near a pole — fall back to deg-lat
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
    """Approximate area of a small WGS84 bbox in square kilometers."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    dlat_km = (max_lat - min_lat) * 111.320
    dlon_km = (max_lon - min_lon) * 111.320 * math.cos(math.radians(mid_lat))
    return abs(dlat_km * dlon_km)
