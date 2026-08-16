"""Regular-grid geometry: a geographic bbox + a metre resolution -> the canonical
origin / spans / cell-size / row-col mesh counts a regular deck runs on.

This is the shared DOMAIN math for the ``regular_grid`` paradigm (SFINCS/SWAN/
MODFLOW-DIS families). It is deliberately pure and dependency-light (stdlib
``math`` only) so it is unit-testable in isolation and reusable by any composer
or preview.

Server/worker split (mesh layer M1): the three engine deck AUTHORS that emit a
regular grid -- SFINCS (hydromt ``setup_grid_from_region``), SWAN
(``deck_builder._grid_geometry``), and MODFLOW (``gwt_adapter`` DIS delr/delc) --
run inside their own GPL/build-isolated worker images (each Dockerfile COPYs
only ``workers/*``, never ``server/``), so they cannot import this
server-side component and keep their own paradigm-native derivation (SWAN spans
in degrees, MODFLOW delr/delc in metres, SFINCS via hydromt). The reusable value
this component delivers on the SERVER side is the regular-grid OUTLINE PREVIEW
(:mod:`trid3nt_server.mesh.mesh_preview`), which gives those three engines
their first-ever visible mesh -- a published grid-extent + cell-size layer.

Angular <-> metre conversion uses a local equirectangular scale at the bbox
centre latitude (``111_320 m/deg`` lat, ``* cos(lat)`` lon) -- the same
small-AOI approximation the SFINCS/SWMM autoscalers use (sfincs_builder
``autoscale_grid_resolution``). It is a screening geometry, not a projected mesh.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "RegularGrid",
    "regular_grid_from_bbox",
    "M_PER_DEG_LAT",
]

#: Metres per degree of latitude (WGS84 mean) -- matches the SFINCS/SWMM
#: autoscaler convention (sfincs_builder ``autoscale_grid_resolution``).
M_PER_DEG_LAT: float = 111_320.0


@dataclass(frozen=True)
class RegularGrid:
    """Regular-grid geometry derived from a geographic bbox + a metre resolution.

    Fields are the canonical regular-grid quantities every DIS / CGRID / SFINCS
    regular deck needs: an origin (the SW corner), physical spans, a target cell
    size, and the row/column mesh counts. ``ncol``/``nrow`` are the cell counts
    (mesh count), floored at 1 so a degenerate AOI never yields an empty grid.
    """

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    resolution_m: float
    #: cell (mesh) counts across the bbox
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

    ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326;
    ``resolution_m`` is the target uniform cell size in metres. The origin is the
    SW corner; spans are the physical bbox width/height at the centre latitude;
    cell counts are ``round(span / resolution)`` floored at 1.

    Raises ``ValueError`` on a degenerate bbox (non-increasing extent) or a
    non-positive resolution -- the caller vouches for a real AOI.
    """
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
