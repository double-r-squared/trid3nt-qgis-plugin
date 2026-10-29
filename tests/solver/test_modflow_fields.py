"""Values read off the feature a reach, lake or cell belongs to, and water moved between features paired by a property.

A value ``{"field": NAME}`` is the number the property NAME of the feature holds, in the model's units; a mover
statement with ``from_key`` and ``to_key`` pairs the features it names by equal properties. Offline except the validate
case, which runs mf6's own --mode validate through the image. The mesh is a 3x3 lattice of 10 m squares in UTM 32617,
numbered row by row from the south-west.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.inputs.geometry import flatten_features, flatten_geometries
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.movers import mover_rows
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place, place_lakes, place_streams
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 3
_N = _SIDE * _SIDE
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE)
          for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_MESH = dict(points=_POINTS, cells=_CELLS, utm_epsg=32617)
_FRAME = ("meters", "days")


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


def _features(*items):
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": props, "geometry": geometry} for geometry, props in items]}


def _fill(stated: dict, **produced):
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": 1, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": [[0.0] * _N], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 1, 1.0]], **stated})


_SOUTH = _line((2.0, 5.0), (28.0, 5.0))
_NORTH = _line((2.0, 25.0), (28.0, 25.0))
_STREAM = {"width": {"field": "w"}, "slope": {"field": "s"}, "bed_top": 9.0, "bed_thickness": 1.0,
           "bed_conductivity": 1.0e-5, "manning": {"field": "n"}}
_LAKE = {"stage": {"field": "stage"}, "bottom": {"field": "floor"}, "leakance": {"field": "leak"}}
_CREEKS = _features((_SOUTH, {"w": 4.0, "s": 0.001, "n": 0.03, "into": "A", "out_of": None}),
                    (_NORTH, {"w": 9.0, "s": 0.002, "n": 0.05, "into": None, "out_of": "A"}))
_PONDS = _features((_box(11.0, 11.0, 19.0, 19.0), {"id": "A", "stage": 12.0, "floor": 0.0, "leak": 0.1}),
                   (_box(1.0, 11.0, 9.0, 19.0), {"id": "B", "stage": 11.0, "floor": 2.0, "leak": 0.2}))
_OUTLET = {"to": "outside", "couttype": "SPECIFIED", "rate": -0.5}


def _streams(statement):
    sheet = _fill({"Streams": statement}, creeks=_CREEKS)
    return place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=_FRAME, **_MESH)


def _lakes(statement, **produced):
    sheet = _fill({"Lakes": statement}, **{"ponds": _PONDS, **produced})
    return place_lakes(sheet.filled["LAK_PACKAGEDATA"].value["lakes"], nlay=1, frame=_FRAME,
                       top=[10.0] * _N, botm=[[0.0] * _N], **_MESH)


# ---- the property carriers

def test_a_feature_collection_keeps_each_geometry_s_properties_and_a_bare_geometry_has_none():
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"k": 1}, "geometry": {"type": "GeometryCollection", "geometries": [
            _SOUTH, _NORTH]}},
        {"type": "Feature", "properties": None, "geometry": _SOUTH}, _NORTH]}
    assert [props for _, props in flatten_features(doc)] == [{"k": 1}, {"k": 1}, {}, {}]
    assert flatten_geometries(doc) == [geometry for geometry, _ in flatten_features(doc)]


# ---- Streams

def test_each_line_s_reaches_read_the_values_of_the_line_s_own_feature():
    packagedata, *_ = _streams({"geometry": "creeks", **_STREAM})
    by_cell = {row[2]: row for row in packagedata}
    south, north = by_cell[1], by_cell[7]
    assert (south[4], south[5], south[9]) == (4.0, 0.001, 0.03)
    assert (north[4], north[5], north[9]) == (9.0, 0.002, 0.05)


def test_a_reach_feature_carries_its_line_s_properties_and_whether_it_ends_the_line():
    *_, features = _streams({"geometry": "creeks", **_STREAM})
    assert [(f["number"], f["properties"]["w"], f["first"], f["last"]) for f in features] == [
        (0, 4.0, True, False), (1, 4.0, False, False), (2, 4.0, False, True),
        (3, 9.0, True, False), (4, 9.0, False, False), (5, 9.0, False, True)]


def test_a_field_beside_a_stated_number_and_a_raster_form_mixes_per_argument():
    packagedata, *_ = _streams({"geometry": "creeks", **_STREAM, "width": 6.0})
    assert {row[4] for row in packagedata} == {6.0}
    assert {row[5] for row in packagedata} == {0.001, 0.002}


def test_a_feature_lacking_the_field_refuses_naming_the_field_and_the_feature():
    doc = _features((_SOUTH, {"w": 4.0, "s": 0.001, "n": 0.03}), (_NORTH, {"w": 9.0, "n": 0.05}))
    sheet = _fill({"Streams": {"geometry": "creeks", **_STREAM}}, creeks=doc)
    with pytest.raises(ModflowError) as caught:
        place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=_FRAME, **_MESH)
    message = str(caught.value)
    assert "'s'" in message and "feature 2" in message and "slope" in message


@pytest.mark.parametrize("held", ["wide", None, True, float("nan"), float("inf")])
def test_a_property_that_is_no_finite_number_refuses(held):
    doc = _features((_SOUTH, {"w": held, "s": 0.001, "n": 0.03}))
    sheet = _fill({"Streams": {"geometry": "creeks", **_STREAM}}, creeks=doc)
    with pytest.raises(ModflowError) as caught:
        place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=_FRAME, **_MESH)
    assert "'w'" in str(caught.value) and "not a finite number" in str(caught.value)


@pytest.mark.parametrize("value", [{"field": 3}, {"field": " "}, {"field": "w", "extra": 1}])
def test_a_field_form_that_names_no_property_refuses(value):
    with pytest.raises(ModflowError) as caught:
        _streams({"geometry": "creeks", **_STREAM, "width": value})
    assert "NAME" in str(caught.value)


def test_a_field_over_a_line_set_with_no_feature_refuses():
    sheet = _fill({"Streams": {"geometry": "creeks", **_STREAM}}, creeks=_SOUTH)
    with pytest.raises(ModflowError) as caught:
        place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=_FRAME, **_MESH)
    assert "carries no such property" in str(caught.value) and "no properties" in str(caught.value)


# ---- Lakes

def test_each_lake_reads_its_stage_bottom_and_leakance_off_its_own_feature():
    packagedata, connectiondata, *_ = _lakes({"geometry": "ponds", **_LAKE})
    assert [(row[0], row[1]) for row in packagedata] == [(0, 12.0), (1, 11.0)]
    leak = {row[0]: row[4] for row in connectiondata}
    assert leak == {0: 0.1, 1: 0.2}


def test_a_lake_bottom_read_off_a_feature_decides_the_layers_the_lake_holds():
    *_, features = _lakes({"geometry": "ponds", **_LAKE, "bottom": {"field": "floor"}})
    assert [sorted(f["cells"]) for f in features] == [[4], [3]]
    with pytest.raises(ModflowError) as caught:
        _lakes({"geometry": "ponds", **_LAKE}, ponds=_features(
            (_box(1.0, 11.0, 9.0, 19.0), {"id": "B", "stage": 11.0, "floor": 20.0, "leak": 0.2})))
    assert "none is solved" in str(caught.value)


def test_a_lake_feature_carries_its_properties():
    *_, features = _lakes({"geometry": "ponds", **_LAKE})
    assert [f["properties"]["id"] for f in features] == ["A", "B"]


def test_a_lake_missing_the_field_refuses_naming_it():
    doc = _features((_box(11.0, 11.0, 19.0, 19.0), {"id": "A", "stage": 12.0, "floor": 0.0}))
    with pytest.raises(ModflowError) as caught:
        _lakes({"geometry": "ponds", **_LAKE}, ponds=doc)
    assert "'leak'" in str(caught.value) and "feature 1" in str(caught.value)


def test_a_stage_keyed_by_period_still_names_period_1_and_a_field_stage_is_period_1():
    with pytest.raises(ModflowError):
        _lakes({"geometry": "ponds", **_LAKE, "stage": {2: 3.0}})
    packagedata, *_ = _lakes({"geometry": "ponds", **_LAKE})
    assert packagedata[0][1] == 12.0


# ---- the stress composites

def _drain(geometry, values):
    statement = {"stress": "DRN", "composite": "Drains", "geometry": geometry, "layer": 1, "values": values,
                 "dimensions": {"elevation": "L", "conductance": "L2/T"}}
    return place(statement, nlay=1, frame=_FRAME, **_MESH)


def test_a_boundary_cell_reads_the_feature_that_places_it():
    doc = _features((_SOUTH, {"e": 3.0, "c": 1.0}), (_NORTH, {"e": 4.0, "c": 2.0}))
    rows = _drain(doc, {"elevation": {"field": "e"}, "conductance": {"field": "c"}})
    assert rows == [((0, 0), 3.0, 1.0), ((0, 1), 3.0, 1.0), ((0, 2), 3.0, 1.0),
                    ((0, 6), 4.0, 2.0), ((0, 7), 4.0, 2.0), ((0, 8), 4.0, 2.0)]


def test_a_cell_under_two_features_holding_different_values_refuses_and_equal_values_do_not():
    crossing = _line((5.0, 2.0), (5.0, 28.0))
    with pytest.raises(ModflowError) as caught:
        _drain(_features((_SOUTH, {"e": 3.0}), (crossing, {"e": 4.0})), {"elevation": {"field": "e"}, "conductance": 1.0})
    assert "[3.0, 4.0]" in str(caught.value)
    rows = _drain(_features((_SOUTH, {"e": 3.0}), (crossing, {"e": 3.0})), {"elevation": {"field": "e"}, "conductance": 1.0})
    assert {cell for (_, cell), *_ in rows} == {0, 1, 2, 3, 6}


def test_a_field_over_the_edge_cells_refuses():
    with pytest.raises(ModflowError):
        _drain({"cells": "edge"}, {"elevation": {"field": "e"}, "conductance": 1.0})


# ---- movers paired by key

def _pond(identifier, box):
    return _features((box, {"id": identifier, "stage": 12.0, "floor": 0.0, "leak": 0.1}))


_POND_A, _POND_B = _pond("A", _box(11.0, 11.0, 19.0, 19.0)), _pond("B", _box(1.0, 11.0, 9.0, 19.0))


def _placed(creeks=_CREEKS, ponds=(_POND_A, _POND_B)):
    produced = {f"pond{n}": held for n, held in enumerate(ponds)}
    sheet = _fill({"Streams": {"geometry": "creeks", **_STREAM, "name": "creek"},
                   "Lakes": [{"geometry": name, **_LAKE, "name": "pond", "outlets": [_OUTLET]} for name in produced]},
                  creeks=creeks, **produced)
    *_, reaches = place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=_FRAME, **_MESH)
    *_, lakes = place_lakes(sheet.filled["LAK_PACKAGEDATA"].value["lakes"], nlay=1, frame=_FRAME,
                            top=[10.0] * _N, botm=[[0.0] * _N], **_MESH)
    return [*reaches, *lakes]


def _move(statements, features=None):
    return mover_rows(statements, features or _placed(), **_MESH)


def test_a_line_gives_to_the_lake_its_property_names_from_its_last_reach_and_the_lake_gives_to_a_first_reach():
    used, periods = _move([
        {"from": "creek", "to": "pond", "keys": ("into", "id"), "type": "FACTOR", "value": 1.0},
        {"from": "pond", "to": "creek", "keys": ("id", "out_of"), "type": "FACTOR", "value": 0.5}])
    assert used == ["SFR", "LAK"]
    assert periods == {0: [["SFR", 2, "LAK", 0, "FACTOR", 1.0], ["LAK", 0, "SFR", 3, "FACTOR", 0.5]]}


def test_one_statement_makes_a_mover_for_every_pair_it_finds():
    creeks = _features((_SOUTH, {"w": 4.0, "s": 0.001, "n": 0.03, "into": "A"}),
                       (_NORTH, {"w": 9.0, "s": 0.002, "n": 0.05, "into": "B"}))
    _, periods = _move([{"from": "creek", "to": "pond", "keys": ("into", "id"), "type": "FACTOR", "value": 1.0}],
                       _placed(creeks))
    assert periods[0] == [["SFR", 2, "LAK", 0, "FACTOR", 1.0], ["SFR", 5, "LAK", 1, "FACTOR", 1.0]]


def test_several_lines_may_give_to_one_lake():
    creeks = _features((_SOUTH, {"w": 4.0, "s": 0.001, "n": 0.03, "into": "A"}),
                       (_NORTH, {"w": 9.0, "s": 0.002, "n": 0.05, "into": "A"}))
    _, periods = _move([{"from": "creek", "to": "pond", "keys": ("into", "id"), "type": "FACTOR", "value": 1.0}],
                       _placed(creeks))
    assert periods[0] == [["SFR", 2, "LAK", 0, "FACTOR", 1.0], ["SFR", 5, "LAK", 0, "FACTOR", 1.0]]


def test_a_property_the_pairing_names_and_a_feature_lacks_refuses_by_name():
    for statement, needle in (({"from": "creek", "to": "pond", "keys": ("nowhere", "id")}, "'nowhere'"),
                              ({"from": "creek", "to": "pond", "keys": ("into", "nowhere")}, "'nowhere'")):
        with pytest.raises(ModflowError) as caught:
            _move([{**statement, "type": "FACTOR", "value": 1.0}])
        assert needle in str(caught.value) and "carries no such property" in str(caught.value)


def test_a_feature_matching_two_receivers_refuses_rather_than_split_its_water():
    with pytest.raises(ModflowError) as caught:
        _move([{"from": "creek", "to": "pond", "keys": ("into", "id"), "type": "FACTOR", "value": 1.0}],
              _placed(ponds=(_POND_A, _pond("A", _box(1.0, 11.0, 9.0, 19.0)))))
    assert "2 features" in str(caught.value) and "gives its water to one" in str(caught.value)


def test_a_statement_pairing_nothing_refuses_and_a_null_property_pairs_nothing():
    creeks = _features((_SOUTH, {"w": 4.0, "s": 0.001, "n": 0.03, "into": None}),
                       (_NORTH, {"w": 9.0, "s": 0.002, "n": 0.05, "into": "elsewhere"}))
    with pytest.raises(ModflowError) as caught:
        _move([{"from": "creek", "to": "pond", "keys": ("into", "id"), "type": "FACTOR", "value": 1.0}],
              _placed(creeks))
    assert "pairs no feature" in str(caught.value)


def test_a_name_no_feature_carries_refuses():
    with pytest.raises(ModflowError) as caught:
        _move([{"from": "river", "to": "pond", "keys": ("into", "id"), "type": "FACTOR", "value": 1.0}])
    assert "'river'" in str(caught.value)


@pytest.mark.parametrize("move", [
    {"from_key": "into", "to": "pond", "from": "creek"},
    {"from_key": "into", "to_key": "id", "from": "creek"},
    {"from_key": "into", "to_key": "id", "from": "creek", "to": "pond", "to_at": "p"},
    {"from_key": "", "to_key": "id", "from": "creek", "to": "pond"}])
def test_the_keys_are_stated_together_with_both_names_and_no_point(move):
    with pytest.raises(ModflowError):
        _fill({"WaterMover": {**move, "type": "FACTOR", "value": 1.0}},
              p={"type": "Point", "coordinates": list(_TO_LONLAT(15.0, 5.0))})


def test_the_keys_belong_to_water_mover_alone():
    with pytest.raises(ModflowError) as caught:
        _fill({"Diversions": {"from": "a", "to": "b", "from_key": "x", "to_key": "y", "type": "UPTO", "value": 1.0}})
    assert "from_key" in str(caught.value)


def test_the_keyed_movers_write_the_numbers_mf6_reads_and_mf6_validates(tmp_path, monkeypatch):
    out = tmp_path / "staged"
    out.mkdir()

    def _copy(mesh_id, local):
        dest = out / local.name
        shutil.copy(local, dest)
        return str(dest)

    monkeypatch.setattr(MF, "_stage", _copy)
    sheet = _fill({
        "Streams": {"geometry": "creeks", **_STREAM, "name": "creek"},
        "Lakes": {"geometry": "pond", **_LAKE, "name": "pond", "outlets": [_OUTLET]},
        "WaterMover": [{"from": "creek", "to": "pond", "from_key": "into", "to_key": "id", "type": "FACTOR", "value": 1.0},
                       {"from": "pond", "to": "creek", "from_key": "id", "to_key": "out_of", "type": "FACTOR", "value": 0.5}]},
        creeks=_CREEKS, pond=_POND_A)
    mesh = _mesh(tmp_path)
    files = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(files["disv"], rundir / "mesh.disv")
    assert "gwf.mvr" in serialize(sheet, rundir, mesh)["written"]
    text = " ".join((rundir / "gwf.mvr").read_text().split())
    assert "sfr_0 3 lak_0 1 FACTOR 1.00000000" in text and "lak_0 1 sfr_0 4 FACTOR 0.50000000" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


def _mesh(tmp_path: Path) -> dict:
    rows = ["MESH2D"]
    for cell, ring in enumerate(_CELLS, start=1):
        rows.append(f"E4Q {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d", display_uri=str(path), crs_authid="EPSG:32617",
        has_bathymetry=True, node_count=len(_POINTS), element_count=len(_CELLS),
        bbox=(0.0, 0.0, 30.0, 30.0), utm_epsg=32617)
    return {"artifact": art, "display_uri": art.display_uri}
