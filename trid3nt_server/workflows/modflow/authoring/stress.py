"""One stress composite's statement as the rows its package's period list takes.

A statement carries a geometry in lon/lat, the layer and one value per column; the
geometry is intersected with the accepted mesh's cells and every value is a number
or the name of a run input whose raster is read at each placed cell's centre.
"""

from __future__ import annotations

import tempfile
from typing import Any, Mapping

from ..errors import ModflowError
from .griddata import sample

__all__ = ["place"]

def place(statement: Mapping[str, Any], *, points: Any, cells: Any,
          utm_epsg: int | None, nlay: int) -> list[tuple]:
    """``statement`` -> ``((layer - 1, cell), *values)`` for every cell the geometry places."""
    name = statement["composite"]
    layer = int(statement["layer"])
    if layer > nlay:
        raise ModflowError(
            f"{name} places on layer {layer} and the grid has {nlay} layer(s).",
            error_code="MODFLOW_STRESS_LAYER")
    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            f"{name} places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.",
            error_code="MODFLOW_STRESS_OUTSIDE")
    placed = _cells(statement["geometry"], name=name, points=points,
                    cells=cells, utm_epsg=int(utm_epsg))
    columns = [_column(value, key, name=name, placed=placed, points=points,
                       cells=cells, utm_epsg=int(utm_epsg))
               for key, value in statement["values"].items()]
    return [((layer - 1, icell), *(column[rank] for column in columns))
            for rank, icell in enumerate(placed)]


def _column(value: Any, key: str, *, name: str, placed: list[int], points: Any,
            cells: Any, utm_epsg: int) -> list[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value] * len(placed)
    with tempfile.TemporaryDirectory(prefix="trid3nt_stress_") as work:
        return sample(value, label=f"{name} {key}", name=key, scratch=work,
                      points=points, cells=[cells[i] for i in placed],
                      utm_epsg=utm_epsg)


def _cells(geometry: Any, *, name: str, points: Any, cells: Any,
           utm_epsg: int) -> list[int]:
    """The sorted cell ids the geometry touches: a polygon holds a cell by its centre,
    a point the one cell GridIntersect returns for it, even on a shared vertex."""
    from flopy.discretization import VertexGrid
    from flopy.utils.gridintersect import GridIntersect
    from pyproj import Transformer
    from shapely.geometry import shape as geo
    from shapely.ops import transform

    from trid3nt_server.inputs.geometry import flatten_geometries
    from trid3nt_server.inputs.shape import shape

    shp = shape(geometry, label=name)
    to_utm = Transformer.from_crs(4326, utm_epsg, always_xy=True).transform
    vertices = [[iv, float(x), float(y)] for iv, (x, y) in enumerate(points)]
    cell2d = [[icell, sum(float(points[v][0]) for v in ring) / len(ring),
               sum(float(points[v][1]) for v in ring) / len(ring), len(ring),
               *[int(v) for v in ring]] for icell, ring in enumerate(cells)]
    grid = VertexGrid(vertices=vertices, cell2d=cell2d, ncpl=len(cells), nlay=1)
    finder = GridIntersect(grid)
    found: set[int] = set()
    for part in flatten_geometries(shp.features):
        projected = transform(to_utm, geo(part))
        areal = projected.geom_type in ("Polygon", "MultiPolygon")
        for row in finder.intersect(projected, contains_centroid=areal, geo_dataframe=False):
            cellid = row["cellids"]
            found.add(int(cellid[0] if hasattr(cellid, "__len__") else cellid))
    if not found:
        raise ModflowError(
            f"{name} places by geometry and the geometry touches no cell of the "
            "accepted mesh; state a geometry that lies on it.",
            error_code="MODFLOW_STRESS_OUTSIDE")
    return sorted(found)
