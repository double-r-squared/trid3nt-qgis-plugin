"""Regular-grid geometry: a geographic bbox + a metre resolution -> the canonical
origin / spans / cell-size / row-col mesh counts a regular deck runs on.

A screening geometry, not a projected mesh."""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "RegularGrid",
    "regular_grid_from_bbox",
    "M_PER_DEG_LAT",
]

#: Metres per degree of latitude (WGS84 mean).
M_PER_DEG_LAT: float = 111_320.0


@dataclass(frozen=True)
class RegularGrid:
    """Regular-grid geometry derived from a geographic bbox + a metre resolution."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    resolution_m: float
    #: cell (mesh) counts across the bbox, floored at 1
    ncol: int
    nrow: int
    #: cell size in DEGREES (span / count) -- what a spherical deck writes
    dlon: float
    dlat: float
    #: physical spans in metres at the centre latitude
    span_x_m: float
    span_y_m: float
    #: centre-latitude deg->m scales used for the conversion (provenance)
    m_per_deg_lon: float
    m_per_deg_lat: float

    @property
    def centre_lat(self) -> float:
        return 0.5 * (self.min_lat + self.max_lat)

    @property
    def n_cells(self) -> int:
        return self.ncol * self.nrow


def regular_grid_from_bbox(
    bbox: tuple[float, float, float, float],
    resolution_m: float,
) -> RegularGrid:
    """Derive a :class:`RegularGrid` from a lon/lat ``bbox`` + a metre resolution.

    ``bbox`` is EPSG:4326 (min_lon, min_lat, max_lon, max_lat)."""
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if not (max_lon > min_lon and max_lat > min_lat):
        raise ValueError(
            f"regular_grid_from_bbox: degenerate bbox {bbox!r} "
            "(need max_lon > min_lon and max_lat > min_lat)"
        )
    if not (resolution_m > 0) or not math.isfinite(resolution_m):
        raise ValueError(
            f"regular_grid_from_bbox: resolution_m must be positive/finite, got {resolution_m!r}"
        )

    centre_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = M_PER_DEG_LAT
    # Angular <-> metre conversion is a local equirectangular scale taken at the
    # bbox centre latitude: 111_320 m per degree of latitude, times cos(lat) for
    # longitude. The 0.01 floor keeps the longitude scale finite near the poles.
    m_per_deg_lon = M_PER_DEG_LAT * max(0.01, math.cos(math.radians(centre_lat)))

    span_x_m = (max_lon - min_lon) * m_per_deg_lon
    span_y_m = (max_lat - min_lat) * m_per_deg_lat

    ncol = max(1, int(round(span_x_m / resolution_m)))
    nrow = max(1, int(round(span_y_m / resolution_m)))

    dlon = (max_lon - min_lon) / ncol
    dlat = (max_lat - min_lat) / nrow

    return RegularGrid(
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        resolution_m=float(resolution_m),
        ncol=ncol,
        nrow=nrow,
        dlon=dlon,
        dlat=dlat,
        span_x_m=span_x_m,
        span_y_m=span_y_m,
        m_per_deg_lon=m_per_deg_lon,
        m_per_deg_lat=m_per_deg_lat,
    )
