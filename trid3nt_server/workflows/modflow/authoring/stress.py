"""One stress composite's statement as the rows its package's period list takes.

A statement carries a geometry in lon/lat, the layer and one value per column; the
geometry is intersected with the accepted mesh's cells and every value is a number
(stated in the model's units) or a raster read at each placed cell's centre onto the
model's frame, by the dimension its column states.
"""

from __future__ import annotations

import tempfile
from typing import Any, Mapping

from trid3nt_server.inputs.geometry import flatten_features
from trid3nt_server.inputs.point import Point, require_viable

from ..errors import ModflowError
from .griddata import METRES, field_name, field_value, sample, surface_of

__all__ = ["GIVERS", "at_cell", "cells_of", "edge_cells", "hold_lakes", "lake_statements", "layer_rows", "maw_statements",
           "pick", "place", "place_interbeds", "place_lakes", "place_maw", "place_streams", "place_uzf", "point_cell", "uzf_statements"]

# The boundary packages that give water to the mover: each row a statement of theirs places is a feature.
GIVERS = ("WEL", "DRN", "GHB", "RIV")


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
    owners = (_owners(geometry, placed, name=name, points=points, cells=cells, utm_epsg=int(utm_epsg))
              if any(field_name(value, label=f"{name} {key}") for key, value in statement["values"].items()) else None)
    columns = {key: (_start(value, name=name, start=start, placed=placed, layers=layers)
                     if isinstance(value, Mapping) and value.get("cells") == "start"
                     else {layers[0]: _column(value, key, name=name, placed=placed, points=points,
                                              cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                                              dimension=statement["dimensions"][key], owners=owners)})
               for key, value in statement["values"].items()}
    for layer in layers:
        for rank, icell in enumerate(placed):
            if active is not None and active[layer][icell] == 0:
                continue
            rows.append(((layer, icell), *(column.get(layer, column[layers[0]])[rank]
                                           for column in columns.values())))
    return rows


def _owners(geometry: Any, placed: list[int], *, name: str, points: Any, cells: Any,
            utm_epsg: int) -> list[list[tuple[str, Mapping[str, Any]]]]:
    """For each placed cell the ``(where, properties)`` of every feature of the geometry that places it."""
    from trid3nt_server.inputs.shape import shape

    if isinstance(geometry, Mapping) and geometry.get("cells"):
        raise ModflowError(f"{name} reads a feature's field, and the geometry {geometry!r} is no feature collection.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    held: dict[int, list[tuple[str, Mapping[str, Any]]]] = {cell: [] for cell in placed}
    for number, (part, properties) in enumerate(flatten_features(shape(geometry, label=name).features), start=1):
        for cell in cells_of(part, name=name, points=points, cells=cells, utm_epsg=utm_epsg):
            held[cell].append((f"feature {number} of the geometry", properties))
    return [held[cell] for cell in placed]


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


# The period settings of each package in mf6's order: a keyword and the fields its row carries after it.
_MAW_SETTINGS = (("status", ("status",)), ("flowing_well", ("fwelev", "fwcond", "fwrlen")), ("rate", ("rate",)),
                 ("well_head", ("well_head",)), ("head_limit", ("head_limit",)),
                 ("shut_off", ("minrate", "maxrate")), ("rate_scaling", ("pump_elevation", "scaling_length")))
_SFR_SETTINGS = tuple((name, (name,)) for name in ("status", "bedk", "manning", "stage", "inflow", "rainfall",
                                                   "evaporation", "runoff", "upstream_fraction"))
_LAK_SETTINGS = tuple((name, (name,)) for name in ("status", "stage", "rainfall", "evaporation", "runoff",
                                                   "inflow", "withdrawal"))
_OUTLET_SETTINGS = tuple((name, (name,)) for name in ("rate", "invert", "width", "slope", "rough"))
# The values a package's table also holds: stated as one number a value is the table's alone, keyed by period every entry is a row.
_SFR_TABLED, _LAK_TABLED = frozenset({"manning"}), frozenset({"stage"})
_OUTLET_TABLED = frozenset({"invert", "width", "slope", "rough"})


def _timed(value: Any) -> dict[int, Any]:
    """A period setting as ``{period index: value}``: a plain value is period 1's."""
    return dict(value) if isinstance(value, Mapping) else {0: value}


def _is_timed(value: Any) -> bool:
    """Whether a value is keyed by period (a mapping of period indices) rather than a raster's mapping."""
    return isinstance(value, Mapping) and bool(value) and all(isinstance(key, int) for key in value)


def _setting_rows(number: int, values: Mapping[str, Any], spec: tuple, *, label: str,
                  tabled: frozenset = frozenset()) -> list[tuple[int, list]]:
    """``(period index, row)`` for every setting of ``spec`` the values state, in spec order; a record is stated whole, in the same periods."""
    found: list[tuple[int, list]] = []
    for keyword, fields in spec:
        stated = [name for name in fields if values.get(name) is not None]
        if not stated:
            continue
        if len(stated) != len(fields):
            raise ModflowError(
                f"{label} states {', '.join(stated)} without {', '.join(f for f in fields if f not in stated)}; "
                f"{keyword} takes {', '.join(fields)} together.", error_code="MODFLOW_STRESS_UNSTATED")
        timed = [_timed(values[name]) for name in fields]
        if any(item.keys() != timed[0].keys() for item in timed):
            raise ModflowError(
                f"{label} states {', '.join(fields)} for different periods; {keyword} takes them together.",
                error_code="MODFLOW_STRESS_UNSTATED")
        if keyword in tabled and not isinstance(values[fields[0]], Mapping):
            continue
        found += [(period, [number, keyword, *[item[period] for item in timed]]) for period in sorted(timed[0])]
    return found


def _auxiliary(stated: Mapping[str, Any] | None, names: tuple, *, label: str, option: str) -> dict[str, dict[int, Any]]:
    """The auxiliary values by the declared names as ``{period index: value}``, each stated from period 1."""
    declared = {str(name).lower(): str(name) for name in names}
    stated = stated or {}
    unknown = sorted(name for name in stated if name.lower() not in declared)
    if unknown:
        raise ModflowError(
            f"{label} states auxiliary {', '.join(unknown)}, which {option} does not name"
            + (f" (it names {', '.join(declared.values())})" if declared else "") + ".",
            error_code="MODFLOW_STRESS_UNSTATED")
    timed = {declared[name.lower()]: _timed(value) for name, value in stated.items()}
    absent = [name for name in declared.values() if 0 not in timed.get(name, {})]
    if absent:
        raise ModflowError(
            f"{label} states no auxiliary {', '.join(absent)} from period 1, which {option} names; "
            "the engine has no value for it, so state it from a measurement or a named source.",
            error_code="MODFLOW_STRESS_UNSTATED")
    return {name: timed[name] for name in declared.values()}


def _aux_table(number: int, stated: Mapping[str, Any] | None, names: tuple, *, label: str, option: str
               ) -> tuple[list, list[tuple[int, list]]]:
    """The table's auxiliary values (period 1's) and the ``(period index, row)`` of those keyed by period."""
    timed = _auxiliary(stated, names, label=label, option=option)
    keyed = {name.lower() for name, value in (stated or {}).items() if isinstance(value, Mapping)}
    return ([item[0] for item in timed.values()],
            [(period, [number, "auxiliary", name, value]) for name, item in timed.items()
             if name.lower() in keyed for period, value in sorted(item.items())])


def _by_period(found: list[tuple[int, list]]) -> dict[int, list[list]]:
    """The rows grouped under their period index, in the order found within a period."""
    grouped: dict[int, list[list]] = {}
    for period, row in sorted(found, key=lambda item: item[0]):
        grouped.setdefault(period, []).append(row)
    return grouped


def place_streams(statements: list[Mapping[str, Any]], *, points: Any, cells: Any,
                  utm_epsg: int | None, nlay: int, frame: Any = None, active: Any = None,
                  auxiliary: tuple = (), diversions: list[Mapping[str, Any]] = ()
                  ) -> tuple[list[list], list[list], dict[int, list[list]], list[list], list[dict[str, Any]]]:
    """Streams statements -> ``(packagedata, connectiondata, perioddata, diversions, features)``, one SFR reach per line piece in a cell.

    Each line is cut by the cells it crosses in digitized order (downstream), a part of it off the mesh is left out, a reach's length is
    the line's length inside its cell on the model's length unit, and its layer is the statement's
    or the cell's top solved layer. Consecutive reaches of a line connect downstream; a line ending in
    a cell that holds one reach of another line (not that line's last) joins it. Reaches are numbered
    from 0, upstream before downstream, ties by statement, line and position along the line. A statement's
    period settings are written to each of its reaches, its inflow to each line's upstream reach; ``auxiliary``
    names the SFR_AUXILIARY columns. A diversion statement moves water from one reach to another: the target
    becomes a downstream connection of the source (numbered after it), receives only the diverted water (USTRF 0.0),
    and the source counts it in NDV. ``features`` is each reach as ``{package, name, number, cells, table, properties, first, last}``, its
    properties those of its line's feature and first and last whether it is its line's upstream or downstream reach."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import shape as geo
    from shapely.ops import transform

    from trid3nt_server.inputs.shape import shape

    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            "Streams places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    metres = _metres(frame, "Streams cuts a line into reach lengths")
    to_utm = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform
    polygons = shapely.polygons([[(float(points[v][0]), float(points[v][1])) for v in ring]
                                 for ring in cells])
    tree = shapely.STRtree(polygons)
    reaches: list[dict[str, Any]] = []
    lines = 0
    for statement in statements:
        first = len(reaches)
        for number, (part, properties) in enumerate(
                flatten_features(shape(statement["geometry"], label="Streams").features), start=1):
            line = transform(to_utm, geo(part))
            if line.geom_type not in ("LineString", "MultiLineString"):
                raise ModflowError(f"Streams places lines; got a {line.geom_type}.",
                                   error_code="MODFLOW_STRESS_UNSTATED")
            for one in (line.geoms if line.geom_type == "MultiLineString" else [line]):
                cut = _cut(one, tree, polygons, cells, line_id=lines, metres=metres,
                           layer=statement.get("layer"), nlay=nlay, active=active)
                for reach in cut:
                    reach["owner"] = (f"feature {number} of the geometry", properties)
                reaches += cut
                lines += 1
        placed = [reach["cell"] for reach in reaches[first:]]
        for reach in reaches[first:]:
            reach["settings"] = {key: value for key, value in (statement.get("settings") or {}).items()
                                 if key != "inflow" or reach["first"]}
            reach["name"], reach["table"] = statement.get("name"), statement.get("cross_section")
        for key, value in statement["values"].items():
            column = _column(value, key, name="Streams", placed=placed, points=points,
                             cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                             dimension=statement["dimensions"][key],
                             owners=[[reach["owner"]] for reach in reaches[first:]])
            for reach, item in zip(reaches[first:], column):
                reach[key] = item
    if not reaches:
        raise ModflowError(
            "Streams places by geometry and the geometry touches no cell of the "
            "accepted mesh; state a geometry that lies on it.",
            error_code="MODFLOW_STRESS_OUTSIDE")
    mesh = {"points": points, "cells": cells, "utm_epsg": utm_epsg}
    found = _reaches(reaches)
    chosen = [(pick(item.get("from"), item.get("from_at"), found, role="Diversions from", **mesh)["number"],
               pick(item.get("to"), item.get("to_at"), found, role="Diversions to", **mesh)["number"], item)
              for item in diversions]
    return _rows(reaches, auxiliary, chosen)


def _reaches(reaches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"package": "SFR", "name": reach.get("name"), "number": rank, "cells": {reach["cell"]}}
            for rank, reach in enumerate(reaches)]


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
    return [{"line": line_id, "first": rank == 0, "last": rank == len(pieces) - 1, "cell": cell,
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


def _rows(reaches: list[dict[str, Any]], auxiliary: tuple, diversions: list[tuple[int, int, Mapping[str, Any]]]
          ) -> tuple[list[list], list[list], dict[int, list[list]], list[list], list[dict[str, Any]]]:
    """Number the reaches upstream first, join the lines, and write each reach's packagedata, connection, diversion and period rows."""
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
    diverted: dict[int, list[tuple[int, Mapping[str, Any]]]] = {}
    for source, target, item in diversions:
        if source == target or downstream.get(source) == target or upstream[target]:
            raise ModflowError(
                f"Diversions: reach {target + 1} of the placed reaches cannot take water diverted from reach "
                f"{source + 1}; a diversion's target is a reach that has no upstream reach and is not the source's "
                "own downstream reach, so its USTRF is the diverted water alone.", error_code="MODFLOW_STRESS_UNSTATED")
        diverted.setdefault(source, []).append((target, item))
        upstream[target].append(source)
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
    packagedata, connectiondata, found, divisions, features = [], [], [], [], []
    targets = {target for items in diverted.values() for target, _ in items}
    for rank in order:
        reach = reaches[rank]
        above = sorted(number[up] for up in upstream[rank])
        below = [number[downstream[rank]]] if rank in downstream else []
        below += [number[target] for target, _ in diverted.get(rank, ())]
        settings = reach.get("settings") or {}
        label = f"Streams reach {number[rank] + 1}"
        columns, timed = _aux_table(number[rank], settings.get("auxiliary"), auxiliary, label=label, option="SFR_AUXILIARY")
        packagedata.append([
            number[rank], reach["layer"], reach["cell"], reach["length"], reach["width"],
            reach["slope"], reach["bed_top"], reach["bed_thickness"],
            reach["bed_conductivity"], reach["manning"], len(above) + len(below), 0.0 if rank in targets else 1.0,
            len(diverted.get(rank, ())), *columns])
        connectiondata.append([number[rank], *above, *[-item for item in below]])
        found += _setting_rows(number[rank], settings, _SFR_SETTINGS, label=label, tabled=_SFR_TABLED) + timed
        for idv, (target, item) in enumerate(diverted.get(rank, ())):
            divisions.append([number[rank], idv, number[target], item["type"]])
            found += [(period, [number[rank], "diversion", idv, value])
                      for period, value in sorted(_timed(item["value"]).items())]
        features.append({"package": "SFR", "name": reach.get("name"), "number": number[rank],
                         "cells": {reach["cell"]}, "table": reach.get("table"), "properties": reach["owner"][1],
                         "first": reach["first"], "last": reach["last"]})
    return packagedata, connectiondata, _by_period(found), divisions, features


def lake_statements(sheet: Any) -> list[Mapping[str, Any]]:
    """The Lakes statements a sheet states (its LAK_PACKAGEDATA as the composite wrote it), or none."""
    value = getattr(sheet.filled.get("LAK_PACKAGEDATA"), "value", None)
    return list(value["lakes"]) if isinstance(value, Mapping) and "lakes" in value else []


def maw_statements(sheet: Any) -> list[Mapping[str, Any]]:
    """The MultiAquiferWells statements a sheet states (its MAW_PACKAGEDATA as the composite wrote it), or none."""
    value = getattr(sheet.filled.get("MAW_PACKAGEDATA"), "value", None)
    return list(value["wells"]) if isinstance(value, Mapping) and "wells" in value else []


def uzf_statements(sheet: Any) -> list[Mapping[str, Any]]:
    """The UnsaturatedZone statements a sheet states (its UZF_PACKAGEDATA as the composite wrote it), or none."""
    value = getattr(sheet.filled.get("UZF_PACKAGEDATA"), "value", None)
    return list(value["zones"]) if isinstance(value, Mapping) and "zones" in value else []


def layer_rows(idomain: Any, *, nlay: int, ncpl: int) -> list[list[Any]]:
    """A placed DISV_IDOMAIN (a number, one number per layer, or rows) as one row per layer of every cell; unstated, every cell is 1."""
    if idomain is None:
        idomain = 1
    if isinstance(idomain, (int, float)):
        return [[idomain] * ncpl for _ in range(nlay)]
    return [list(row) if isinstance(row, (list, tuple)) else [row] * ncpl for row in idomain]


def hold_lakes(statements: list[Mapping[str, Any]], idomain: Any, *, points: Any, cells: Any,
               utm_epsg: int | None, nlay: int, frame: Any, top: Any, botm: Any) -> list[list[Any]]:
    """``idomain`` as one row per layer with every lake cell zero: a lake's water stands where the aquifer's cell is not solved."""
    rows = layer_rows(idomain, nlay=nlay, ncpl=len(cells))
    for lake in _lakes(statements, points=points, cells=cells, utm_epsg=utm_epsg, nlay=nlay,
                       frame=frame, active=rows, top=top, botm=botm)[0]:
        for layer, cell in lake["held"]:
            rows[layer][cell] = 0
    return rows


def place_lakes(statements: list[Mapping[str, Any]], *, points: Any, cells: Any,
                utm_epsg: int | None, nlay: int, frame: Any = None, active: Any = None,
                top: Any, botm: Any, auxiliary: tuple = ()
                ) -> tuple[list[list], list[list], dict[int, list[list]], list[list], list[dict[str, Any]]]:
    """Lakes statements -> ``(packagedata, connectiondata, perioddata, outlets, features)`` rows, one LAK lake per polygon.

    A lake holds the cells whose centres its polygon contains, in every solved layer whose top lies
    above the lake's bottom; each is connected to the solved cells that are not lake cells beside it
    (HORIZONTAL, over the face the two cells share, the neighbour's whole thickness) and to the one below it
    (VERTICAL). A stated period setting is a row of its period; one not stated has none. An outlet leaves
    the statement's one lake for the lake number ``to`` (from 1) or outside, numbered in statement order, its
    values the columns its type reads and mf6's unread value elsewhere.
    ``active`` is one row per layer of the cells the run solves, ``top`` the grid's top and ``botm`` its bottoms;
    ``auxiliary`` names the LAK_AUXILIARY columns. ``features`` is each lake as ``{package, name, number, cells,
    outlets, table, properties}``, its outlets the numbers they were given."""
    import numpy as np
    import shapely

    metres = _metres(frame, "Lakes writes connection lengths")
    lakes, skipped = _lakes(statements, points=points, cells=cells, utm_epsg=utm_epsg, nlay=nlay,
                            frame=frame, active=active, top=top, botm=botm)
    if lakes or skipped:
        from trid3nt_server.inputs.run_notes import journal_note

        journal_note(
            f"Lakes: {len(lakes)} lake(s) placed" + (
                f"; {len(skipped)} waterbod{'y' if len(skipped) == 1 else 'ies'} skipped, not lakes of this mesh: "
                + "; ".join(f"{each['label']} ({each['why']}, {each['cells']} cell centre(s)"
                            + (f", spread {each['spread']:.3f} m" if each["spread"] is not None else "")
                            + f", {each['area']:.0f} m2)" for each in skipped) if skipped else "")
            + "; the lakes exchange water with the aquifer only (no outlets, no inflow).")
    tops, bottoms = _levels(top, botm, nlay=nlay, ncpl=len(cells))
    centres = shapely.centroid(shapely.polygons(
        [[(float(points[v][0]), float(points[v][1])) for v in ring] for ring in cells]))
    faces: dict[frozenset, list[int]] = {}
    for icell, ring in enumerate(cells):
        for a, b in zip(ring, list(ring[1:]) + [ring[0]]):
            faces.setdefault(frozenset((a, b)), []).append(icell)
    held = {item for lake in lakes for item in lake["held"]}

    def solved(layer: int, cell: int) -> bool:
        return (layer, cell) not in held and (active is None or active[layer][cell] > 0)

    packagedata, connectiondata, found, outlets = [], [], [], []
    for number, lake in enumerate(lakes):
        rows = []
        for layer, cell in lake["held"]:
            beside = []
            for a, b in zip(cells[cell], list(cells[cell][1:]) + [cells[cell][0]]):
                for other in faces[frozenset((a, b))]:
                    if other != cell and solved(layer, other):
                        (ax, ay), (bx, by) = points[a][:2], points[b][:2]
                        width = float(np.hypot(bx - ax, by - ay))
                        gap = abs((bx - ax) * (centres[other].y - ay)
                                  - (by - ay) * (centres[other].x - ax)) / width
                        beside.append((other, gap / metres, width / metres))
            for other, length, width in sorted(beside):
                rows.append([number, 0, (layer, other), "horizontal", lake["leakance"],
                             float(bottoms[layer][other]), float(tops[layer][other]), length, width])
            if layer + 1 < nlay and solved(layer + 1, cell):
                rows.append([number, 0, (layer + 1, cell), "vertical", lake["leakance"],
                             0.0, 0.0, 0.0, 0.0])
        if not rows:
            raise ModflowError(
                f"Lakes: {lake['name']} has no solved cell beside or below it to connect to; "
                "a lake with no connection exchanges no water.", error_code="MODFLOW_STRESS_OUTSIDE")
        for count, row in enumerate(rows):
            row[1] = count
        label = f"Lakes {lake['name']}"
        columns, timed = _aux_table(number, lake["values"].get("auxiliary"), auxiliary, label=label, option="LAK_AUXILIARY")
        packagedata.append([number, _timed(lake["values"]["stage"])[0], len(rows), *columns])
        connectiondata += rows
        found += _setting_rows(number, lake["values"], _LAK_SETTINGS, label=label, tabled=_LAK_TABLED) + timed
    features = []
    for number, lake in enumerate(lakes):
        features.append({"package": "LAK", "name": lake["given"], "number": number,
                         "cells": {cell for _, cell in lake["held"]}, "table": lake["table"], "properties": lake["properties"],
                         "outlets": list(range(len(outlets), len(outlets) + len(lake["outlets"])))})
        for outlet in lake["outlets"]:
            outletno = len(outlets)
            out = outlet["to"] - 1
            if out >= len(lakes) or out == number:
                raise ModflowError(
                    f"Lakes: {lake['name']} outlet {outletno + 1} discharges to lake {outlet['to']}; "
                    + ("it leaves that lake" if out == number else f"the run has {len(lakes)} lake(s)")
                    + ", so state another lake or \"outside\".", error_code="MODFLOW_STRESS_OUTSIDE")
            read = {name: _timed(outlet[name])[0] if outlet.get(name) is not None else _UNREAD
                    for name in ("invert", "width", "rough", "slope")}
            outlets.append([outletno, number, out, outlet["couttype"], *read.values()])
            found += _setting_rows(outletno, outlet, _OUTLET_SETTINGS,
                                   label=f"Lakes outlet {outletno + 1}", tabled=_OUTLET_TABLED)
    return packagedata, connectiondata, _by_period(found), outlets, features


def place_maw(statements: list[Mapping[str, Any]], *, points: Any, cells: Any, utm_epsg: int | None,
              nlay: int, active: Any = None, top: Any, botm: Any, auxiliary: tuple = (),
              flowing_wells: bool = False
              ) -> tuple[list[list], list[list], dict[int, list[list]], list[dict[str, Any]]]:
    """MultiAquiferWells statements -> ``(packagedata, connectiondata, perioddata, features)`` rows, one MAW well per point.

    A well sits in the cell holding its point and has one connection to every solved layer whose cell its
    screen crosses (the screen's bottom under the layer's top and its top over the layer's bottom); a
    well is numbered in statement order and a point's order, and each period setting it states is a row of its
    period. ``active`` is one row per layer of the cells the run solves, ``top`` the grid's top and ``botm`` its
    bottoms; ``auxiliary`` names the MAW_AUXILIARY columns; ``flowing_wells`` is the MAW_FLOWING_WELLS option,
    which mf6 reads the flowing-well values (fwelev, fwcond, fwrlen) only under."""
    import numpy as np

    from trid3nt_server.inputs.geometry import flatten_geometries
    from trid3nt_server.inputs.shape import shape

    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            "MultiAquiferWells places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    tops, bottoms = _levels(top, botm, nlay=nlay, ncpl=len(cells))
    solved = np.ones((nlay, len(cells)), dtype=bool) if active is None else np.asarray(active) > 0
    packagedata, connectiondata, found, features = [], [], [], []
    for statement in statements:
        values = statement["values"]
        flowing = [name for name in ("fwelev", "fwcond", "fwrlen") if values.get(name) is not None]
        if flowing and not flowing_wells:
            raise ModflowError(
                f"MultiAquiferWells states {', '.join(flowing)}, which mf6 reads only with MAW_FLOWING_WELLS; "
                "state MAW_FLOWING_WELLS true or remove them.", error_code="MODFLOW_STRESS_UNSTATED")
        parts = flatten_geometries(shape(statement["geometry"], label="MultiAquiferWells").features)
        for part in parts:
            if part.get("type") not in ("Point", "MultiPoint"):
                raise ModflowError(f"MultiAquiferWells places points; got a {part.get('type')}.",
                                   error_code="MODFLOW_STRESS_UNSTATED")
        for lon, lat, *_ in (xy for part in parts for xy in (
                part["coordinates"] if part["type"] == "MultiPoint" else [part["coordinates"]])):
            number = len(packagedata)
            cell = point_cell(Point(float(lon), float(lat)), label="MultiAquiferWells", points=points,
                              cells=cells, utm_epsg=int(utm_epsg), active=solved.any(axis=0))
            screened = [layer for layer in range(nlay) if solved[layer][cell]
                        and values["screen_bottom"] < tops[layer][cell]
                        and values["screen_top"] > bottoms[layer][cell]]
            if not screened:
                raise ModflowError(
                    f"MultiAquiferWells: well {number + 1} in cell {cell} has a screen "
                    f"({values['screen_bottom']} to {values['screen_top']}) crossing no solved layer there; "
                    "state a screen over a layer the run solves.", error_code="MODFLOW_STRESS_OUTSIDE")
            label = f"MultiAquiferWells well {number + 1}"
            columns, timed = _aux_table(number, values.get("auxiliary"), auxiliary, label=label, option="MAW_AUXILIARY")
            features.append({"package": "MAW", "name": statement.get("name"), "number": number, "cells": {cell}})
            packagedata.append([number, values["radius"], values["bottom"], values["start_head"],
                                values["condeqn"], len(screened), *columns])
            connectiondata += [[number, count, (layer, cell), values["screen_top"], values["screen_bottom"],
                                values["hk_skin"], values["radius_skin"]]
                               for count, layer in enumerate(screened)]
            found += _setting_rows(number, values, _MAW_SETTINGS, label=label) + timed
    if not packagedata:
        raise ModflowError("MultiAquiferWells places by geometry and the geometry holds no point.",
                           error_code="MODFLOW_STRESS_OUTSIDE")
    return packagedata, connectiondata, _by_period(found), features


def place_interbeds(statements: list[Mapping[str, Any]], *, points: Any, cells: Any, utm_epsg: int | None, nlay: int,
                    frame: Any = None, active: Any = None, boundnames: bool = False) -> list[list]:
    """Interbeds statements -> CSUB packagedata rows ``[icsubno, (layer, cell), cdelay, pcs0, thick_frac, rnb, ssv_cc, sse_cr, theta, kv, h0[, boundname]]``.

    Each cell a geometry places on a layer is one interbed, numbered in statement order, then layer, then cell;
    ``active`` is one row per layer of the cells the run solves. A NODELAY interbed's rnb, kv and h0 are the
    value mf6 takes any value in. ``boundnames`` is the CSUB_BOUNDNAMES option: every row then carries a name."""
    if points is None or cells is None or not utm_epsg:
        raise ModflowError("Interbeds places by geometry, and this run has no accepted mesh with a projected zone "
                           "to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    rows: list[list] = []
    for statement in statements:
        layers = _layers(statement["layer"], name="Interbeds", nlay=nlay)
        placed = cells_of(statement["geometry"], name="Interbeds", points=points, cells=cells, utm_epsg=int(utm_epsg),
                          active=None if active is None else active[layers[0]])
        if boundnames and not statement.get("name"):
            raise ModflowError("Interbeds states no name, and CSUB_BOUNDNAMES is true: every interbed then carries a "
                               "boundary name.", error_code="MODFLOW_STRESS_UNSTATED")
        if not boundnames and statement.get("name"):
            raise ModflowError("Interbeds states a name, and CSUB_BOUNDNAMES is not true: the name is the boundary "
                               "name column mf6 reads only under that option.", error_code="MODFLOW_STRESS_UNSTATED")
        columns = {key: (_column(held, key, name="Interbeds", placed=placed, points=points, cells=cells,
                                 utm_epsg=int(utm_epsg), frame=frame, dimension=statement["dimensions"][key])
                         if key in statement["dimensions"] else [held] * len(placed))
                   for key, held in statement["values"].items()}
        unread = [_UNREAD] * len(placed)
        for layer in layers:
            for rank, icell in enumerate(placed):
                if active is not None and active[layer][icell] == 0:
                    continue
                at = {key: columns.get(key, unread)[rank] for key in ("pcs0", "thick_frac", "rnb", "ssv_cc", "sse_cr",
                                                                      "theta", "kv", "h0")}
                rows.append([len(rows), (layer, icell), statement["cdelay"], *at.values(),
                             *([statement["name"]] if boundnames else [])])
    if not rows:
        raise ModflowError("Interbeds places by geometry and the geometry holds no solved cell.",
                           error_code="MODFLOW_STRESS_OUTSIDE")
    return rows


# The value mf6 takes in a UZF period column its stated options do not read.
_UNREAD = -999.0
# The period columns after the infiltration, with the options that read them (None: always when ET is simulated).
_ET_READ = {"pet": None, "extdp": None, "extwc": "unsat_etwc",
            "ha": "unsat_etae", "hroot": "unsat_etae", "rootact": "unsat_etae"}


def place_uzf(statements: list[Mapping[str, Any]], *, points: Any, cells: Any, utm_epsg: int | None,
              nlay: int, frame: Any = None, active: Any = None, options: Mapping[str, Any] = {},
              auxiliary: tuple = ()) -> tuple[list[list], dict[int, list[list]], list[dict[str, Any]]]:
    """UnsaturatedZone statements -> ``(packagedata, perioddata, features)`` rows, one UZF cell per solved layer of a held column.

    A polygon holds the columns whose centres it contains; the top solved cell of a column is the land-surface
    cell (landflag 1) and each solved layer under it, down to the first layer the run does not solve, a cell
    connected to the one above (ivertcon, the last one 0-based -1). Cells are numbered by layer then cell, the
    land cells carry the period-1 row. ``options`` names the UZF options stated (simulate_et, unsat_etwc,
    unsat_etae): an ET value is read, and required, only by the options that read it, and stated for none it refuses;
    a column no option reads is written as mf6's unread value. finf, an ET value or an auxiliary value keyed by
    period is a land cell's row of each period it names, every column of the row the value in force then.
    ``active`` is one row per layer of the cells the run solves; ``auxiliary`` names the UZF_AUXILIARY columns."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import shape as geo
    from shapely.ops import transform

    from trid3nt_server.inputs.geometry import flatten_geometries
    from trid3nt_server.inputs.shape import shape

    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            "UnsaturatedZone places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    to_utm = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform
    centres = shapely.centroid(shapely.polygons(
        [[(float(points[v][0]), float(points[v][1])) for v in ring] for ring in cells]))
    tree = shapely.STRtree(centres)
    solved = [[True] * len(cells) for _ in range(nlay)] if active is None else [
        [item > 0 for item in row] for row in active]
    columns: dict[int, dict[str, Any]] = {}
    for index, statement in enumerate(statements):
        values = statement["values"]
        reads = {name for name, option in _ET_READ.items()
                 if options.get("simulate_et") and (option is None or options.get(option))}
        stated = {name for name in _ET_READ if values.get(name) is not None}
        if stated - reads:
            raise ModflowError(
                f"UnsaturatedZone states {', '.join(sorted(stated - reads))}, which the UZF options stated "
                "read nothing of; state UZF_SIMULATE_ET (and UZF_UNSAT_ETWC or UZF_UNSAT_ETAE for "
                "extwc, ha, hroot, rootact) or remove it.", error_code="MODFLOW_STRESS_UNSTATED")
        if reads - stated:
            raise ModflowError(
                f"UnsaturatedZone states no {', '.join(sorted(reads - stated))}, which the UZF options "
                "stated read; the engine has no value for it, so state it from a measurement or a named source.",
                error_code="MODFLOW_STRESS_UNSTATED")
        late = sorted(key for key in ("finf", *reads) if _is_timed(values[key]) and 0 not in values[key])
        if late:
            raise ModflowError(
                f"UnsaturatedZone {', '.join(late)} keyed by period states period 1; the first period's row "
                "needs a value, and the engine has none.", error_code="MODFLOW_STRESS_UNSTATED")
        aux = _auxiliary(values.get("auxiliary"), auxiliary, label="UnsaturatedZone", option="UZF_AUXILIARY")
        for part in flatten_geometries(shape(statement["geometry"], label="UnsaturatedZone").features):
            area = transform(to_utm, geo(part))
            if area.geom_type not in ("Polygon", "MultiPolygon"):
                raise ModflowError(f"UnsaturatedZone places polygons; got a {area.geom_type}.",
                                   error_code="MODFLOW_STRESS_UNSTATED")
            for one in (area.geoms if area.geom_type == "MultiPolygon" else [area]):
                centred = sorted(int(c) for c in tree.query(one, predicate="contains"))
                if not centred:
                    raise ModflowError(
                        "UnsaturatedZone: a polygon holds no cell centre of the accepted mesh; "
                        "state a polygon over cells of it.", error_code="MODFLOW_STRESS_OUTSIDE")
                held = [cell for cell in centred if any(row[cell] for row in solved)]
                if not held:
                    raise ModflowError(
                        f"UnsaturatedZone: a polygon holds {len(centred)} cell centre(s), and the run solves "
                        "none of them on any layer (DISV_IDOMAIN 0).", error_code="MODFLOW_STRESS_OUTSIDE")
                found = {key: [dict(values[key])] * len(held) if _is_timed(values[key])
                         else _column(values[key], key, name="UnsaturatedZone", placed=held, points=points,
                                      cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                                      dimension=_UZF_DIMENSION[key])
                         for key in (*_UZF_CELL, "finf", *reads)}
                for rank, cell in enumerate(held):
                    if cell in columns:
                        raise ModflowError(
                            f"UnsaturatedZone: statements {columns[cell]['statement'] + 1} and "
                            f"{index + 1} both hold cell {cell}; a column belongs "
                            "to one statement.", error_code="MODFLOW_STRESS_OUTSIDE")
                    columns[cell] = {"statement": index, "auxiliary": aux,
                                     **{key: column[rank] for key, column in found.items()}}
    top = {cell: next(layer for layer in range(nlay) if solved[layer][cell]) for cell in columns}
    stacks = {}
    for cell, layer in top.items():
        stack = [layer]
        while stack[-1] + 1 < nlay and solved[stack[-1] + 1][cell]:
            stack.append(stack[-1] + 1)
        stacks[cell] = stack
    order = sorted((layer, cell) for cell, stack in stacks.items() for layer in stack)
    number = {item: rank for rank, item in enumerate(order)}
    packagedata, period_rows, features = [], [], []
    for layer, cell in order:
        column = columns[cell]
        land = layer == top[cell]
        below = number.get((layer + 1, cell), -1)
        packagedata.append([number[(layer, cell)], (layer, cell), int(land), below,
                            *[column[key] for key in _UZF_CELL]])
        if land:
            features.append({"package": "UZF", "name": statements[column["statement"]].get("name"),
                             "number": number[(layer, cell)], "cells": {cell}})
            timed = {key: _timed(column[key]) for key in ("finf", *_ET_READ) if column.get(key) is not None}
            series = [*timed.values(), *column["auxiliary"].values()]
            for period in sorted({period for item in series for period in item}):
                period_rows.append((period, [number[(layer, cell)], _in_force(timed["finf"], period),
                                             *[_in_force(timed[key], period) if key in timed else _UNREAD
                                               for key in _ET_READ],
                                             *[_in_force(item, period) for item in column["auxiliary"].values()]]))
    return packagedata, _by_period(period_rows), features


def _in_force(item: Mapping[int, Any], period: int) -> Any:
    """The value of a period-keyed setting in force in ``period``: the latest one stated at or before it."""
    return item[max(key for key in item if key <= period)]


_UZF_CELL = ("surfdep", "vks", "thtr", "thts", "thti", "eps")
_UZF_DIMENSION = {"surfdep": "L", "vks": "L/T", "thtr": "1", "thts": "1", "thti": "1", "eps": "1",
                  "finf": "L/T", "pet": "L/T", "extdp": "L", "extwc": "1", "ha": "L", "hroot": "L",
                  "rootact": "1"}


def _levels(top: Any, botm: Any, *, nlay: int, ncpl: int) -> tuple[Any, Any]:
    """The top of each layer's cells and its bottom, each ``(nlay, ncpl)``, off the grid's top and bottoms as placed."""
    import numpy as np

    low = np.asarray(botm, dtype=float)
    bottoms = np.broadcast_to(low.reshape(-1, 1) if low.ndim < 2 else low, (nlay, ncpl))
    first = np.broadcast_to(np.asarray(top, dtype=float), (ncpl,))
    return np.vstack([first[None, :], bottoms[:-1]]), bottoms


def _lakes(statements: list[Mapping[str, Any]], *, points: Any, cells: Any, utm_epsg: int | None,
           nlay: int, frame: Any, active: Any, top: Any, botm: Any) -> tuple[list[dict[str, Any]], list[float]]:
    """Every polygon of every statement as a lake - its stated values and the ``(layer, cell)`` pairs it holds - and the areas (m2) of the polygons holding no cell centre, which are no lake of this mesh."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import shape as geo
    from shapely.ops import transform

    from trid3nt_server.inputs.shape import shape

    if points is None or cells is None or not utm_epsg:
        raise ModflowError(
            "Lakes places by geometry, and this run has no accepted mesh "
            "with a projected zone to place it on.", error_code="MODFLOW_STRESS_OUTSIDE")
    tops, _ = _levels(top, botm, nlay=nlay, ncpl=len(cells))
    to_utm = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform
    centres = shapely.centroid(shapely.polygons(
        [[(float(points[v][0]), float(points[v][1])) for v in ring] for ring in cells]))
    tree = shapely.STRtree(centres)
    lakes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    owner: dict[tuple[int, int], str] = {}
    for statement in statements:
        first = len(lakes)
        for number, (part, properties) in enumerate(
                flatten_features(shape(statement["geometry"], label="Lakes").features), start=1):
            values = {key: (field_value(field, properties, label=f"Lakes {key}", where=f"feature {number} of the geometry")
                            if (field := field_name(held, label=f"Lakes {key}")) else held)
                      for key, held in statement["values"].items()}
            area = transform(to_utm, geo(part))
            if area.geom_type not in ("Polygon", "MultiPolygon"):
                raise ModflowError(f"Lakes places polygons; got a {area.geom_type}.",
                                   error_code="MODFLOW_STRESS_UNSTATED")
            for one in (area.geoms if area.geom_type == "MultiPolygon" else [area]):
                name = f"lake {len(lakes) + 1}"
                label = str((properties or {}).get("permanent_identifier") or (properties or {}).get("gnis_name") or name)
                centred = sorted(int(c) for c in tree.query(one, predicate="contains"))
                if not centred:
                    skipped.append({"label": label, "why": "no cell centre", "cells": 0, "spread": None,
                                    "area": float(one.area)})
                    continue
                if (surface := surface_of(values["stage"], label="Lakes stage")) is not None:
                    stage, spread = _flat_stage(surface, placed=centred, points=points, cells=cells,
                                                utm_epsg=int(utm_epsg), frame=frame)
                    if stage is None:
                        skipped.append({"label": label, "why": "no majority stage", "cells": len(centred),
                                        "spread": spread, "area": float(one.area)})
                        continue
                    values = {**values, "stage": stage}
                bottoms = _column(values["bottom"], "bottom", name="Lakes", placed=centred,
                                  points=points, cells=cells, utm_epsg=int(utm_epsg), frame=frame,
                                  dimension="L")
                held = [(layer, cell) for cell, low in zip(centred, bottoms) for layer in range(nlay)
                        if (active is None or active[layer][cell] > 0) and tops[layer][cell] > low]
                if not held:
                    raise ModflowError(
                        f"Lakes: {name} holds {len(centred)} cell centre(s), and none is solved "
                        "in a layer whose top lies above the lake's bottom.",
                        error_code="MODFLOW_STRESS_OUTSIDE")
                for item in held:
                    if item in owner:
                        raise ModflowError(
                            f"Lakes: {owner[item]} and {name} both hold cell {item[1]} on layer "
                            f"{item[0] + 1}; a cell belongs to one lake.",
                            error_code="MODFLOW_STRESS_OUTSIDE")
                    owner[item] = name
                lakes.append({"name": name, "held": sorted(held), "stage": values["stage"], "properties": properties,
                              "leakance": values["leakance"], "values": values, "given": statement.get("name"),
                              "outlets": statement.get("outlets") or [], "table": statement.get("table")})
        for stated in ("outlets", "table"):
            if statement.get(stated) and len(lakes) - first != 1:
                raise ModflowError(
                    f"Lakes: {stated} belong to one lake, and this statement's geometry places {len(lakes) - first}; "
                    f"state the {stated} on a statement holding one polygon.", error_code="MODFLOW_STRESS_UNSTATED")
    return lakes, skipped


# USGS Elevation-Derived Hydrography Data Acquisition Specifications, Vertical Alignment of lake/pond polygons:
# a water body's z-values match its hydro-flattened surface within 0.001 m; a centre at the mode is within it.
_FLAT_WITHIN_M = 0.001


def _flat_stage(surface: Any, *, placed: list[int], points: Any, cells: Any, utm_epsg: int,
                frame: Any) -> tuple[float | None, float]:
    """``(the elevation more than half of a lake's cell centres share, spread)``; the stage is None where no elevation is shared by a majority, and the spread is the surface's range over the centres in metres."""
    import numpy as np

    metres = _metres(frame, "Lakes stage reads a surface")
    found = np.asarray(_column(surface, "stage", name="Lakes", placed=placed, points=points,
                               cells=cells, utm_epsg=utm_epsg, frame=frame, dimension="L"), dtype=float)
    # Islands and banks inside a polygon sit above the flattened water, so the water is the mode, not every centre.
    near = np.abs(found[:, None] - found[None, :]) * metres <= _FLAT_WITHIN_M
    counts = near.sum(axis=1)
    best = int(np.argmax(counts))
    spread = float((found.max() - found.min()) * metres)
    return (None if counts[best] * 2 <= len(found) else float(np.median(found[near[best]]))), spread


def _metres(frame: Any, what: str) -> float:
    """The metres in the model's length unit, or the refusal that names DISV_LENGTH_UNITS."""
    unit = frame[0] if frame else None
    metres = METRES.get(str(unit or "unknown").strip().lower())
    if metres is None:
        raise ModflowError(
            f"{what} on the model's length unit, and "
            f"DISV_LENGTH_UNITS is {str(unit or 'unstated')!r}; state it as "
            f"{', '.join(METRES)}.", error_code="MODFLOW_UNITS_UNSTATED")
    return metres


def _column(value: Any, key: str, *, name: str, placed: list[int], points: Any,
            cells: Any, utm_epsg: int, frame: Any,
            dimension: str, owners: Any = None) -> list[float]:
    """A value at each placed cell; ``owners`` is each placed cell's ``(where, properties)`` list, which a ``{"field": NAME}`` value reads."""
    field = field_name(value, label=f"{name} {key}")
    if field is not None:
        if owners is None:
            raise ModflowError(f"{name} {key} reads the field {field!r}, and its geometry places no feature.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        column = []
        for held in owners:
            found = {field_value(field, properties, label=f"{name} {key}", where=where) for where, properties in held}
            if len(found) != 1:
                raise ModflowError(
                    f"{name} {key} reads the field {field!r}, and a placed cell lies under {len(held)} features "
                    f"holding {sorted(found)}; a cell takes one value.", error_code="MODFLOW_STRESS_UNSTATED")
            column.append(found.pop())
        return column
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value] * len(placed)
    if isinstance(value, Mapping) and set(value) == {"series"}:
        if not isinstance(value["series"], str) or not value["series"].strip():
            raise ModflowError(f"{name} {key} names a series as {{\"series\": NAME}}; got {value!r}.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        return [value["series"].strip()] * len(placed)
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


def at_cell(at: Any, *, role: str, points: Any, cells: Any, utm_epsg: int | None) -> int:
    """The cell the one point a statement places by lies in; a point that misses refuses with the nearest-cell hint."""
    from trid3nt_server.inputs.geometry import flatten_geometries
    from trid3nt_server.inputs.shape import shape

    if points is None or cells is None or not utm_epsg:
        raise ModflowError(f"{role} places by a point, and this run has no accepted mesh with a projected zone.",
                           error_code="MODFLOW_STRESS_OUTSIDE")
    parts = flatten_geometries(shape(at, label=role).features)
    if len(parts) != 1 or parts[0].get("type") != "Point":
        raise ModflowError(f"{role} places by one point; got {len(parts)} geometries.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    lon, lat = parts[0]["coordinates"][:2]
    return point_cell(Point(float(lon), float(lat)), label=role, points=points, cells=cells, utm_epsg=int(utm_epsg))


def pick(name: Any, at: Any, features: list[Mapping[str, Any]], *, role: str, points: Any, cells: Any,
         utm_epsg: int | None) -> Mapping[str, Any]:
    """The one feature a statement's end names, by its name, the cell of the point it lies at, or both; none or several refuse."""
    if name is None and at is None:
        raise ModflowError(f"{role} states neither a name nor a point; state the feature it is.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    found = [item for item in features if name is None or item["name"] == name]
    if name is not None and not found:
        named = sorted({str(item["name"]) for item in features if item["name"] is not None})
        raise ModflowError(
            f"{role} names {name!r}, and no placed feature carries that name"
            + (f" (named: {', '.join(named)})" if named else "") + ".", error_code="MODFLOW_STRESS_UNSTATED")
    if at is not None:
        cell = at_cell(at, role=role, points=points, cells=cells, utm_epsg=utm_epsg)
        found = [item for item in found if cell in item["cells"]]
    if len(found) != 1:
        raise ModflowError(
            f"{role} names {len(found)} features"
            + (f" ({', '.join(f'{item['package']} {item['number'] + 1}' for item in found)})" if found else "")
            + "; state the one it is by a name, a point in its cell, or both.", error_code="MODFLOW_STRESS_UNSTATED")
    return found[0]


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
