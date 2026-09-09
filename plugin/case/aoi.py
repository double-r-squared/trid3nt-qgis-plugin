"""AOI helpers -- PURE PYTHON (no PyQGIS / PyQt imports).

CRS math and the guard for the dock's explicit AOI. An extent wider than
``AOI_MAX_DEG`` per side is NOT attached: a whole-country box is not a usable
simulation AOI, and sending it silently would invite a giant fetch."""

from __future__ import annotations

import math
from typing import Optional, Tuple

__all__ = [
    "AOI_MAX_DEG",
    "aoi_status_text",
    "bbox_span_deg",
    "bbox_within_guard",
    "choose_aoi",
    "extent_to_bbox4326",
    "format_bbox",
    "merc_to_lonlat",
]

#: Guard: maximum extent side (degrees) still attached as an AOI.
AOI_MAX_DEG = 2.0

# Spherical-mercator constants (EPSG:3857).
_EARTH_RADIUS_M = 6378137.0
_MERC_MAX = math.pi * _EARTH_RADIUS_M  # ~20037508.34


def merc_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    """EPSG:3857 metres -> (lon, lat) degrees (spherical mercator inverse)."""
    lon = math.degrees(x / _EARTH_RADIUS_M)
    lat = math.degrees(2.0 * math.atan(math.exp(y / _EARTH_RADIUS_M)) - math.pi / 2.0)
    return lon, lat


def extent_to_bbox4326(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    crs_authid: str,
) -> Optional[Tuple[float, float, float, float]]:
    """Canvas extent in ``crs_authid`` -> ``(lon_min, lat_min, lon_max, lat_max)``.
    Only EPSG:4326 and EPSG:3857 are handled; any other CRS, a degenerate extent
    or a non-finite value returns None rather than a fabricated bbox."""
    values = (xmin, ymin, xmax, ymax)
    if any(not math.isfinite(v) for v in values):
        return None
    if xmax <= xmin or ymax <= ymin:
        return None
    authid = (crs_authid or "").strip().upper()
    if authid in ("EPSG:4326", "OGC:CRS84"):
        lon_min, lat_min, lon_max, lat_max = xmin, ymin, xmax, ymax
    elif authid == "EPSG:3857":
        # Clamp to the mercator world square before inverting.
        cx0 = max(-_MERC_MAX, min(_MERC_MAX, xmin))
        cx1 = max(-_MERC_MAX, min(_MERC_MAX, xmax))
        cy0 = max(-_MERC_MAX, min(_MERC_MAX, ymin))
        cy1 = max(-_MERC_MAX, min(_MERC_MAX, ymax))
        lon_min, lat_min = merc_to_lonlat(cx0, cy0)
        lon_max, lat_max = merc_to_lonlat(cx1, cy1)
    else:
        return None
    lon_min = max(-180.0, min(180.0, lon_min))
    lon_max = max(-180.0, min(180.0, lon_max))
    lat_min = max(-90.0, min(90.0, lat_min))
    lat_max = max(-90.0, min(90.0, lat_max))
    if lon_max <= lon_min or lat_max <= lat_min:
        return None
    return (lon_min, lat_min, lon_max, lat_max)


def bbox_span_deg(bbox: Tuple[float, float, float, float]) -> Tuple[float, float]:
    """(lon span, lat span) of a ``[lon_min, lat_min, lon_max, lat_max]`` bbox."""
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def bbox_within_guard(
    bbox: Tuple[float, float, float, float], max_deg: float = AOI_MAX_DEG
) -> bool:
    """True when BOTH sides are within the guard (attachable as an AOI)."""
    dlon, dlat = bbox_span_deg(bbox)
    return dlon <= max_deg and dlat <= max_deg


def choose_aoi(
    selection_bbox: Optional[Tuple[float, float, float, float]],
    canvas_bbox: Optional[Tuple[float, float, float, float]],
    prefer_selection: bool,
) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[str]]:
    """Pick which AOI rides this send: ``(bbox, source)``, ``source`` being
    ``"selection"``, ``"canvas"`` or None so the status line can name which
    extent went out."""
    # The guard is deliberately NOT applied here: a too-large selection must
    # surface as "selection ... too large", never fall back to the canvas the
    # user explicitly overrode.
    if prefer_selection and selection_bbox is not None:
        return selection_bbox, "selection"
    if canvas_bbox is not None:
        return canvas_bbox, "canvas"
    return None, None


def format_bbox(bbox: Tuple[float, float, float, float], precision: int = 6) -> str:
    """``[lon_min, lat_min, lon_max, lat_max]`` with fixed precision -- the
    element order every bbox carrier on the wire expects."""
    return "[" + ", ".join(f"{v:.{precision}f}" for v in bbox) + "]"


def aoi_status_text(
    bbox: Optional[Tuple[float, float, float, float]],
    enabled: bool,
    max_deg: float = AOI_MAX_DEG,
    source: str = "canvas",
) -> str:
    """The dock's one-line AOI status: off, unresolved, the span, or a
    too-large note that states the AOI was NOT sent."""
    label = "selection" if source == "selection" else "canvas"
    if not enabled:
        return "AOI: off"
    if bbox is None:
        return f"AOI: {label} extent unavailable (CRS not resolved) -- sent without AOI"
    dlon, dlat = bbox_span_deg(bbox)
    if not bbox_within_guard(bbox, max_deg):
        return (
            f"AOI: {label} {dlon:.2f} x {dlat:.2f} deg -- too large "
            f"(> {max_deg:g} deg/side), sent without AOI"
        )
    return f"AOI: {label} {dlon:.2f} x {dlat:.2f} deg"
