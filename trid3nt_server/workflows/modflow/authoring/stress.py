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
from .griddata import METRES, sample

__all__ = ["cells_of", "edge_cells", "place", "place_streams", "point_cell"]

def place(statement: Mapping[str, Any], *, points: Any, cells: Any,
          utm_epsg: int | None, nlay: int, frame: Any = None,
          active: Any = None, start: Any = None) -> list[tuple]:
    """``statement`` -> ``((layer - 1, cell), *values)`` for every cell the geometry places.

    ``active`` is one row per layer of the cells the run solves (DISV_IDOMAIN not zero); unstated, every cell is.
    The geometry ``{"cells": "edge"}`` is the cells with a face on the mesh's outer boundary, a layer of
    ``"all"`` every solved layer of each placed cell, and a value ``{"cells": "start"}`` the run's
    starting head there; ``start`` is its one row per layer."""
    name = statement["composite"]
    layers = _layers(statement["layer"], name=name, nlay=nlay)
    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            f"{name} places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.",
            error_code="MODFLOW_STRESS_OUTSIDE")
    geometry = statement["geometry"]
    if isinstance(geometry, Mapping) and geometry.get("cells") == "edge":
        placed = edge_cells(cells)
    else:
        placed = cells_of(geometry, name=name, points=points, cells=cells,
                          utm_epsg=int(utm_epsg), active=None if active is None else active[layers[0]])
    rows: list[tuple] = []
    columns = {key: (_start(value, name=name, start=start, placed=placed, layers=layers)
                     if isinstance(value, Mapping) and value.get("cells") == "start"
                     else {layers[0]: _column(value, key, name=name, placed=placed, points=points,
                                              cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                                              dimension=statement["dimensions"][key])})
               for key, value in statement["values"].items()}
    for layer in layers:
        for rank, icell in enumerate(placed):
            if active is not None and active[layer][icell] == 0:
                continue
            rows.append(((layer, icell), *(column.get(layer, column[layers[0]])[rank]
                                           for column in columns.values())))
    return rows


def _layers(layer: Any, *, name: str, nlay: int) -> list[int]:
    """The 0-based layers a statement places on: its one layer, or every layer for ``"all"``."""
    if layer == "all":
        return list(range(nlay))
    if int(layer) > nlay:
        raise ModflowError(
            f"{name} places on layer {layer} and the grid has {nlay} layer(s).",
            error_code="MODFLOW_STRESS_LAYER")
    return [int(layer) - 1]


def _start(value: Mapping[str, Any], *, name: str, start: Any, placed: list[int],
           layers: list[int]) -> dict[int, list[float]]:
    """The starting head at the placed cells, per layer."""
    if start is None:
        raise ModflowError(
            f"{name} head is the starting head, and this run states no IC_STRT.",
            error_code="MODFLOW_STRESS_UNSTATED")
    # IC_STRT is one number, one value per cell, or one row per layer
    import numpy as np

    heads = np.asarray(start, dtype=float)
    if heads.ndim == 0:
        return {layer: [float(heads)] * len(placed) for layer in layers}
    rows = heads[None, :] if heads.ndim == 1 else heads
    return {layer: [float(rows[min(layer, len(rows) - 1)][i]) for i in placed] for layer in layers}


def edge_cells(cells: Any) -> list[int]:
    """The sorted cell ids with a face (two consecutive vertices) no other cell shares: the mesh's outer boundary."""
    faces: dict[frozenset, list[int]] = {}
    for icell, ring in enumerate(cells):
        for a, b in zip(ring, list(ring[1:]) + [ring[0]]):
            faces.setdefault(frozenset((a, b)), []).append(icell)
    return sorted({owners[0] for owners in faces.values() if len(owners) == 1})


def place_streams(statements: list[Mapping[str, Any]], *, points: Any, cells: Any,
                  utm_epsg: int | None, nlay: int, frame: Any = None,
                  active: Any = None) -> tuple[list[list], list[list]]:
    """Streams statements -> ``(packagedata, connectiondata)`` rows, one SFR reach per line piece in a cell.

    Each line is cut by the cells it crosses in digitized order (downstream), a part of it off the mesh is left out, a reach's length is
    the line's length inside its cell on the model's length unit, and its layer is the statement's
    or the cell's top solved layer. Consecutive reaches of a line connect downstream; a line ending in
    a cell that holds one reach of another line (not that line's last) joins it. Reaches are numbered
    from 0, upstream before downstream, ties by statement, line and position along the line."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import shape as geo
    from shapely.ops import transform

    from trid3nt_server.inputs.geometry import flatten_geometries
    from trid3nt_server.inputs.shape import shape

    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            "Streams places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    unit = frame[0] if frame else None
    metres = METRES.get(str(unit or "unknown").strip().lower())
    if metres is None:
        raise ModflowError(
            "Streams cuts a line into reach lengths on the model's length unit, and "
            f"DISV_LENGTH_UNITS is {str(unit or 'unstated')!r}; state it as "
            f"{', '.join(METRES)}.", error_code="MODFLOW_UNITS_UNSTATED")
    to_utm = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform
    polygons = shapely.polygons([[(float(points[v][0]), float(points[v][1])) for v in ring]
                                 for ring in cells])
    tree = shapely.STRtree(polygons)
    reaches: list[dict[str, Any]] = []
    lines = 0
    for statement in statements:
        first = len(reaches)
        for part in flatten_geometries(shape(statement["geometry"], label="Streams").features):
            line = transform(to_utm, geo(part))
            if line.geom_type not in ("LineString", "MultiLineString"):
                raise ModflowError(f"Streams places lines; got a {line.geom_type}.",
                                   error_code="MODFLOW_STRESS_UNSTATED")
            for one in (line.geoms if line.geom_type == "MultiLineString" else [line]):
                reaches += _cut(one, tree, polygons, cells, line_id=lines, metres=metres,
                                layer=statement.get("layer"), nlay=nlay, active=active)
                lines += 1
        placed = [reach["cell"] for reach in reaches[first:]]
        for key, value in statement["values"].items():
            column = _column(value, key, name="Streams", placed=placed, points=points,
                             cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                             dimension=statement["dimensions"][key])
            for reach, item in zip(reaches[first:], column):
                reach[key] = item
    if not reaches:
        raise ModflowError(
            "Streams places by geometry and the geometry touches no cell of the "
            "accepted mesh; state a geometry that lies on it.",
            error_code="MODFLOW_STRESS_OUTSIDE")
    return _rows(reaches)


def _cut(line: Any, tree: Any, polygons: Any, cells: Any, *, line_id: int, metres: float,
         layer: Any, nlay: int, active: Any) -> list[dict[str, Any]]:
    pieces = []
    for cell in tree.query(line, predicate="intersects"):
        cut = line.intersection(polygons[cell])
        for piece in getattr(cut, "geoms", [cut]):
            if piece.geom_type == "LineString" and piece.length > 0.0:
                pieces.append((line.project(piece.interpolate(0.5, normalized=True)),
                               int(cell), piece.length))
    if sum(length for _, _, length in pieces) > line.length * (1.0 + 1e-9):
        raise ModflowError(
            "Streams places a line along a cell edge, which counts it in both cells; "
            "move the line off the edge.", error_code="MODFLOW_STRESS_OUTSIDE")
    pieces.sort()
    for (_, before, _), (_, after, _) in zip(pieces, pieces[1:]):
        if not set(cells[before]) & set(cells[after]):
            raise ModflowError(
                f"Streams: the line leaves the mesh between cells {before} and {after} and "
                "returns; a stream is one connected line on the mesh, so split it there.",
                error_code="MODFLOW_STRESS_OUTSIDE")
    return [{"line": line_id, "last": rank == len(pieces) - 1, "cell": cell,
             "length": length / metres, "layer": _reach_layer(cell, layer, nlay, active)}
            for rank, (_, cell, length) in enumerate(pieces)]


def _reach_layer(cell: int, layer: Any, nlay: int, active: Any) -> int:
    """The 0-based layer a reach lies on: the stated one, else the cell's top solved layer; a cell solved on none refuses."""
    if layer is not None and int(layer) > nlay:
        raise ModflowError(
            f"Streams places on layer {layer} and the grid has {nlay} layer(s).",
            error_code="MODFLOW_STRESS_LAYER")
    layers = [int(layer) - 1] if layer is not None else range(nlay)
    for index in layers:
        if active is None or active[index][cell] != 0:
            return index
    raise ModflowError(
        f"Streams crosses cell {cell}, which the run does not solve "
        f"{'on layer ' + str(layer) if layer is not None else 'on any layer'} (DISV_IDOMAIN 0).",
        error_code="MODFLOW_STRESS_OUTSIDE")


def _rows(reaches: list[dict[str, Any]]) -> tuple[list[list], list[list]]:
    """Number the reaches upstream first, join the lines, and write each reach's packagedata and connection rows."""
    downstream: dict[int, int] = {}
    for rank, reach in enumerate(reaches):
        if rank + 1 < len(reaches) and not reach["last"]:
            downstream[rank] = rank + 1
            continue
        held = [other for other, each in enumerate(reaches)
                if each["line"] != reach["line"] and not each["last"]
                and (each["layer"], each["cell"]) == (reach["layer"], reach["cell"])]
        if len(held) > 1:
            raise ModflowError(
                f"Streams: the line ending in cell {reach['cell']} meets {len(held)} reaches "
                "of other lines there and cannot choose the one it joins; end it in a "
                "cell where one line passes.", error_code="MODFLOW_STRESS_UNSTATED")
        if held and reach["last"]:
            downstream[rank] = held[0]
    upstream: dict[int, list[int]] = {rank: [] for rank in range(len(reaches))}
    for rank, below in downstream.items():
        upstream[below].append(rank)
    number: dict[int, int] = {}
    while len(number) < len(reaches):
        ready = [rank for rank in range(len(reaches)) if rank not in number
                 and all(up in number for up in upstream[rank])]
        if not ready:
            raise ModflowError(
                "Streams: the lines join in a loop, so no reach is upstream of the rest.",
                error_code="MODFLOW_STRESS_UNSTATED")
        number[ready[0]] = len(number)
    order = sorted(number, key=number.get)
    packagedata, connectiondata = [], []
    for rank in order:
        reach = reaches[rank]
        above = sorted(number[up] for up in upstream[rank])
        below = [number[downstream[rank]]] if rank in downstream else []
        packagedata.append([
            number[rank], reach["layer"], reach["cell"], reach["length"], reach["width"],
            reach["slope"], reach["bed_top"], reach["bed_thickness"],
            reach["bed_conductivity"], reach["manning"], len(above) + len(below), 1.0, 0])
        connectiondata.append([number[rank], *above, *[-item for item in below]])
    return packagedata, connectiondata


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
