"""One stress composite's statement as the rows its package's period list takes.

A statement carries a geometry in lon/lat, the layer and one value per column; the
geometry is intersected with the accepted mesh's cells and every value is a number
(stated in the model's units) or a raster read at each placed cell's centre onto the
model's frame, by the dimension its column states.
"""

from __future__ import annotations

import tempfile
from typing import Any, Mapping

from trid3nt_server.inputs.point import Point, require_viable

from ..errors import ModflowError
from .griddata import sample

__all__ = ["cells_of", "place", "point_cell"]

def place(statement: Mapping[str, Any], *, points: Any, cells: Any,
          utm_epsg: int | None, nlay: int, frame: Any = None,
          active: Any = None) -> list[tuple]:
    """``statement`` -> ``((layer - 1, cell), *values)`` for every cell the geometry places.

    ``active`` is one row per layer of the cells the run solves (DISV_IDOMAIN not zero); unstated, every cell is."""
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
    placed = cells_of(statement["geometry"], name=name, points=points,
                      cells=cells, utm_epsg=int(utm_epsg),
                      active=None if active is None else active[layer - 1])
    columns = [_column(value, key, name=name, placed=placed, points=points,
                       cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                       dimension=statement["dimensions"][key])
               for key, value in statement["values"].items()]
    return [((layer - 1, icell), *(column[rank] for column in columns))
            for rank, icell in enumerate(placed)]


def _column(value: Any, key: str, *, name: str, placed: list[int], points: Any,
            cells: Any, utm_epsg: int, frame: Any,
            dimension: str) -> list[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value] * len(placed)
    with tempfile.TemporaryDirectory(prefix="trid3nt_stress_") as work:
        return sample(value, label=f"{name} {key}", name=key, scratch=work,
                      points=points, cells=[cells[i] for i in placed],
                      utm_epsg=utm_epsg, dimension=dimension, frame=frame)


def point_cell(pt: Any, *, label: str, points: Any, cells: Any, utm_epsg: int,
               active: Any = None) -> int:
    """The cell a Point lies in, or the shared refusal naming the nearest active cell; the point is never moved."""
    import numpy as np
    import shapely

    rings = [[(float(points[v][0]), float(points[v][1])) for v in ring] for ring in cells]
    polygons = shapely.polygons(rings)
    tree = shapely.STRtree(polygons)
    centres = np.asarray([np.mean(ring, axis=0) for ring in rings])

    def resolve(xy: tuple[float, float]) -> int | None:
        hits = tree.query(shapely.Point(*xy), predicate="covered_by")
        return int(sorted(hits)[0]) if len(hits) else None

    held = np.ones(len(cells), dtype=bool) if active is None else np.asarray(active) != 0
    return require_viable(pt, label=label, utm_epsg=int(utm_epsg), sites_xy=centres,
                          viable=held, resolve=resolve,
                          missed="in a cell the run does not solve (DISV_IDOMAIN 0)")


def cells_of(geometry: Any, *, name: str, points: Any, cells: Any,
           utm_epsg: int, active: Any = None) -> list[int]:
    """The sorted cell ids the geometry touches: a polygon holds a cell by its centre,
    a line every cell it crosses, a point the one cell it lies in (or the shared refusal)."""
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
        if part.get("type") == "Point":
            lon, lat = part["coordinates"][:2]
            found.add(point_cell(Point(float(lon), float(lat)), label=name, points=points,
                                 cells=cells, utm_epsg=utm_epsg, active=active))
            continue
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
