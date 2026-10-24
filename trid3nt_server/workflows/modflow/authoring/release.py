"""The PRT release package's points from the Release statements: each point a position in the mesh's metres, its cell computed here.

A point is a position the statement states or seeds (along a line, over a polygon, in the cells of a placed feature); it is
never moved, and one outside the grid refuses with the nearest-cell hint. The row is ``(number, (layer, cell), x, y, z)``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from trid3nt_server.inputs.point import Point

from ..errors import ModflowError
from .stress import point_cell

__all__ = ["place_release"]


def place_release(statements: list[Mapping[str, Any]], *, points: Any, cells: Any, utm_epsg: int | None, nlay: int,
                  top: Any, botm: Any, active: Any, features: list[Mapping[str, Any]]) -> list[tuple]:
    """Release statements -> ``(number, (layer, cell), x, y, z)`` for every point, numbered from 0 in the order stated."""
    if points is None or cells is None or not utm_epsg or top is None or botm is None:
        raise ModflowError("Release places by geometry on the accepted mesh, and this run has no mesh with a projected zone "
                           "and layering to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    finder = _Cells(points, cells, int(utm_epsg), active)
    rows: list[tuple] = []
    for rank, statement in enumerate(statements, start=1):
        role = f"Release {rank}"
        for x, y, cell, feature_layer in _seeds(statement, role=role, finder=finder, features=features):
            layer, z = _vertical(statement, cell, feature_layer, role=role, nlay=nlay, top=top, botm=botm)
            if active is not None and active[layer][cell] == 0:
                raise ModflowError(f"{role} releases in layer {layer + 1} of cell {cell}, which the run does not solve "
                                   "(DISV_IDOMAIN 0).", error_code="MODFLOW_STRESS_OUTSIDE")
            rows.append((len(rows), (layer, cell), x, y, z))
    return rows


class _Cells:
    """The mesh's cells as polygons in the zone's metres, for the cell a position lies in."""

    def __init__(self, points: Any, cells: Any, utm_epsg: int, active: Any) -> None:
        import shapely

        self.points, self.cells, self.utm_epsg, self.active = points, cells, utm_epsg, active
        self.rings = [[(float(points[v][0]), float(points[v][1])) for v in ring] for ring in cells]
        self.polygons = shapely.polygons(self.rings)
        self.tree = shapely.STRtree(self.polygons)

    def of(self, xy: tuple[float, float], *, role: str) -> int:
        """The lowest cell covering ``xy``, or the shared refusal naming the nearest cell."""
        import shapely
        from pyproj import Transformer

        hits = self.tree.query(shapely.Point(*xy), predicate="covered_by")
        if len(hits):
            return int(sorted(hits)[0])
        lon, lat = Transformer.from_crs(self.utm_epsg, 4326, always_xy=True).transform(*xy)
        return point_cell(Point(float(lon), float(lat)), label=role, points=self.points, cells=self.cells,
                          utm_epsg=self.utm_epsg)


def _seeds(statement: Mapping[str, Any], *, role: str, finder: _Cells, features: list[Mapping[str, Any]]
           ) -> list[tuple[float, float, int, int | None]]:
    if statement.get("on") is not None:
        return _on_feature(statement, role=role, finder=finder, features=features)
    from pyproj import Transformer
    from shapely.geometry import shape as geo
    from shapely.ops import transform

    from trid3nt_server.inputs.geometry import flatten_geometries
    from trid3nt_server.inputs.shape import shape

    to_utm = Transformer.from_crs(4326, finder.utm_epsg, always_xy=True).transform
    found: list[tuple[float, float, int, int | None]] = []
    for part in flatten_geometries(shape(statement["geometry"], label=role).features):
        kind = part.get("type")
        projected = transform(to_utm, geo(part))
        if kind == "Point":
            _unseeded(statement, role)
            found.append((float(projected.x), float(projected.y), finder.of((projected.x, projected.y), role=role), None))
        elif kind == "LineString":
            found += [(x, y, finder.of((x, y), role=role), None) for x, y in _along(projected, statement, role)]
        elif kind == "Polygon":
            found += [(x, y, finder.of((x, y), role=role), None) for x, y in _over(projected, statement, role, finder)]
        else:
            raise ModflowError(f"{role} releases at a point, a line or a polygon; got a {kind}.",
                               error_code="MODFLOW_STRESS_UNSTATED")
    if not found:
        raise ModflowError(f"{role} seeds no release point.", error_code="MODFLOW_STRESS_OUTSIDE")
    return found


def _unseeded(statement: Mapping[str, Any], role: str) -> None:
    if statement.get("spacing") is not None or statement.get("count") is not None:
        raise ModflowError(f"{role} states spacing or count, and its geometry is a point: one release at the point.",
                           error_code="MODFLOW_STRESS_UNSTATED")


def _along(line: Any, statement: Mapping[str, Any], role: str) -> list[tuple[float, float]]:
    """The points along a line: every ``spacing`` from its start, or ``count`` evenly from end to end."""
    spacing, count = statement.get("spacing"), statement.get("count")
    if (spacing is None) == (count is None):
        raise ModflowError(f"{role} releases along a line and states exactly one of spacing (a distance in metres from the "
                           "line's start) and count (points from end to end).", error_code="MODFLOW_STRESS_UNSTATED")
    length = float(line.length)
    if spacing is not None:
        steps = int(math.floor(length / float(spacing) * (1.0 + 1e-12)))
        distances = [rank * float(spacing) for rank in range(steps + 1)]
    else:
        if int(count) < 2:
            raise ModflowError(f"{role} count along a line is from 2 (both ends); got {count}.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        distances = [length * rank / (int(count) - 1) for rank in range(int(count))]
    return [(float(point.x), float(point.y)) for point in (line.interpolate(d) for d in distances)]


def _over(polygon: Any, statement: Mapping[str, Any], role: str, finder: _Cells) -> list[tuple[float, float]]:
    """The points over a polygon: a lattice of ``spacing`` squares anchored at its bounding box, or ``count`` per cell side in each rectangular cell whose centre it holds."""
    import shapely

    spacing, count = statement.get("spacing"), statement.get("count")
    if (spacing is None) == (count is None):
        raise ModflowError(f"{role} releases over a polygon and states exactly one of spacing (a lattice of squares this "
                           "many metres wide) and count (points per cell side, in each cell whose centre it holds).",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if spacing is not None:
        west, south, east, north = polygon.bounds
        s = float(spacing)
        columns, rows = int(math.floor((east - west) / s * (1.0 + 1e-12))), int(math.floor((north - south) / s * (1.0 + 1e-12)))
        seeds = [(west + (i + 0.5) * s, south + (j + 0.5) * s) for j in range(rows + 1) for i in range(columns + 1)]
        return [xy for xy in seeds if polygon.covers(shapely.Point(*xy))]
    held = sorted(int(i) for i in finder.tree.query(polygon, predicate="intersects")
                  if polygon.covers(finder.polygons[i].centroid))
    return [xy for cell in held for xy in _lattice(finder, cell, int(count), role)]


def _lattice(finder: _Cells, cell: int, count: int, role: str) -> list[tuple[float, float]]:
    """``count`` x ``count`` points at the sub-cell centres of a rectangular cell."""
    ring = finder.rings[cell]
    if len(ring) != 4:
        raise ModflowError(f"{role} states count, which subdivides a rectangular cell, and cell {cell} has {len(ring)} "
                           "vertices.", error_code="MODFLOW_STRESS_UNSTATED")
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = ring
    if math.hypot(x0 + x2 - x1 - x3, y0 + y2 - y1 - y3) > 1e-6 * max(math.hypot(x1 - x0, y1 - y0), 1.0):
        raise ModflowError(f"{role} states count, which subdivides a rectangular cell, and cell {cell} is no parallelogram.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    return [(x0 + (i + 0.5) / count * (x1 - x0) + (j + 0.5) / count * (x3 - x0),
             y0 + (i + 0.5) / count * (y1 - y0) + (j + 0.5) / count * (y3 - y0))
            for j in range(count) for i in range(count)]


def _on_feature(statement: Mapping[str, Any], *, role: str, finder: _Cells, features: list[Mapping[str, Any]]
                ) -> list[tuple[float, float, int, int | None]]:
    """``count`` x ``count`` points in each cell of every placed feature the name is."""
    name, count = statement["on"], statement.get("count")
    if statement.get("geometry") is not None or statement.get("spacing") is not None or count is None:
        raise ModflowError(f"{role} releases on the feature {name!r}: state count (points per cell side) and no geometry "
                           "or spacing.", error_code="MODFLOW_STRESS_UNSTATED")
    found = [item for item in features if item.get("name") == name]
    if not found:
        named = sorted({str(item["name"]) for item in features if item.get("name") is not None})
        raise ModflowError(f"{role} names {name!r}, and no placed feature of the flow model carries that name"
                           + (f" (named: {', '.join(named)})" if named else "") + ".", error_code="MODFLOW_STRESS_UNSTATED")
    return [(x, y, cell, item.get("layer")) for item in found for cell in sorted(item["cells"])
            for x, y in _lattice(finder, cell, int(count), role)]


def _level(value: Any, cell: int) -> float:
    return float(value) if isinstance(value, (int, float)) else float(value[cell])


def _vertical(statement: Mapping[str, Any], cell: int, feature_layer: int | None, *, role: str, nlay: int,
              top: Any, botm: Any) -> tuple[int, float]:
    """The ``(layer - 1, z)`` of a point in ``cell``: a layer and the fraction of its thickness above its bottom, or a depth below the grid's top."""
    ceiling = _level(top, cell)
    floors = [_level(botm[layer], cell) for layer in range(nlay)]
    if statement.get("depth") is not None:
        elevation = ceiling - float(statement["depth"])
        for layer, floor in enumerate(floors):
            if elevation >= floor:
                return layer, elevation
        raise ModflowError(f"{role} releases {statement['depth']} below the grid's top ({ceiling}) at cell {cell}, below the "
                           f"model base ({floors[-1]}).", error_code="MODFLOW_STRESS_LAYER")
    layer = statement["layer"] - 1 if statement.get("layer") is not None else feature_layer
    if layer is None:
        raise ModflowError(f"{role} is on a feature with no layer of its own; state layer and z, or depth.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if not 0 <= layer < nlay:
        raise ModflowError(f"{role} releases in layer {layer + 1} and the grid has {nlay} layer(s).",
                           error_code="MODFLOW_STRESS_LAYER")
    upper = ceiling if layer == 0 else floors[layer - 1]
    fraction = float(statement["z"])
    return layer, fraction * upper + (1.0 - fraction) * floors[layer]
