"""Water moved between packages, SFR diversions, reach cross sections and lake tables, stated through the composites.

A mover or diversion end is a name a placement statement gave its features and/or a point read as the cell it lies
in; the numbers mf6 reads are computed where the mesh is. Offline except the validate cases, which run mf6's own
--mode validate through the image. The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row
from the south-west, three periods.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.inputs.point import PointNotViableError
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.movers import mover_rows
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_lakes, place_streams
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
_LAKE = {"stage": 12.0, "bottom": 0.0, "leakance": 0.1}
_STREAM = {"width": 5.0, "slope": 0.001, "bed_top": 9.0, "bed_thickness": 1.0, "bed_conductivity": 1.0e-5,
           "manning": 0.03}


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


def _fill(stated: dict, **produced):
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": 1, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": [[0.0] * _N], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
        "TDIS_NPER": 3, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 1, 1.0]] * 3, **stated})


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


@pytest.fixture
def staged_beside(monkeypatch, tmp_path):
    out = tmp_path / "staged"
    out.mkdir()

    def _copy(mesh_id, local):
        dest = out / local.name
        shutil.copy(local, dest)
        return str(dest)

    monkeypatch.setattr(MF, "_stage", _copy)
    return out


class _Flat(str):
    """A file's text whose membership test ignores the column padding flopy writes."""

    def __contains__(self, needle):
        return " ".join(needle.split()) in " ".join(self.split())


def _written(sheet, tmp_path, package):
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    assert package in serialize(sheet, rundir, mesh)["written"]
    return rundir, _Flat((rundir / package).read_text())


_CENTRE = _box(11.0, 11.0, 19.0, 19.0)

# Two parallel lines on the lattice's south and north rows, west to east: reaches 0-2 (cells 0-2) and 3-5 (cells 6-8).
_SOUTH = _line((2.0, 5.0), (28.0, 5.0))
_NORTH = _line((2.0, 25.0), (28.0, 25.0))
_POND = {"to": "outside", "couttype": "SPECIFIED", "rate": -0.5}
_TABLE = [[0.0, 1.0], [0.5, 0.0], [1.0, 1.0]]
_AT = dict(s1=_point(15.0, 5.0), s2=_point(25.0, 5.0), n0=_point(5.0, 25.0), n1=_point(15.0, 25.0),
           centre=_point(15.0, 15.0))
_PRODUCED = dict(south=_SOUTH, north=_NORTH, pond=_CENTRE, s1=_point(15.0, 5.0), n0=_point(5.0, 25.0),
                 n1=_point(15.0, 25.0), centre=_point(15.0, 15.0))


def _deck(*stated, **produced):
    keywords = {"Streams": [{"geometry": "south", **_STREAM, "name": "creek"},
                            {"geometry": "north", **_STREAM, "name": "ditch"}],
                "Lakes": {"geometry": "pond", **_LAKE, "name": "pond", "outlets": [_POND]}}
    for item in stated:
        keywords.update(item)
    return _fill(keywords, **{**_PRODUCED, **produced})


def _mvr(sheet, tmp_path):
    return _written(sheet, tmp_path, "gwf.mvr")


def _refused(sheet, tmp_path):
    mesh = _mesh(tmp_path)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    with pytest.raises((ModflowError, PointNotViableError)) as caught:
        out = MF._mesh_files(sheet, mesh)
        shutil.copy(out["disv"], rundir / "mesh.disv")
        serialize(sheet, rundir, mesh)
    return str(caught.value)


# ---- MVR: a mover's ends resolve to the reach, outlet, lake, well or UZF cell mf6 numbers

def test_a_stream_into_a_lake_and_the_lake_s_outlet_into_a_stream_write_the_numbers_mf6_reads(staged_beside, tmp_path):
    sheet = _deck({"WaterMover": [{"from": "creek", "from_at": "s1", "to": "pond", "type": "factor", "value": 1.0},
                              {"from": "pond", "to": "ditch", "to_at": "n1", "type": "UPTO", "value": 0.2}]})
    rundir, text = _mvr(sheet, tmp_path)
    assert "MAXMVR  2" in text and "MAXPACKAGES  2" in text
    assert "sfr_0  2  lak_0  1  FACTOR       1.00000000" in text
    assert "lak_0  1  sfr_0  5  UPTO         0.20000000" in text
    assert "MOVER" in (rundir / "gwf.sfr").read_text() and "MOVER" in (rundir / "gwf.lak").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_value_keyed_by_period_restates_every_mover_in_each_period_it_names(staged_beside, tmp_path):
    sheet = _deck({"WaterMover": [{"from": "creek", "from_at": "s1", "to": "pond", "type": "FACTOR", "value": 1.0},
                              {"from": "pond", "to": "ditch", "to_at": "n1", "type": "FACTOR", "value": {1: 0.5, 3: 0.25}}]})
    _, text = _mvr(sheet, tmp_path)
    assert text.count("BEGIN period") == 2
    assert "BEGIN period  1\n  sfr_0  2  lak_0  1  FACTOR       1.00000000\n  lak_0  1  sfr_0  5  FACTOR       0.50000000" in text
    assert "BEGIN period  3\n  sfr_0  2  lak_0  1  FACTOR       1.00000000\n  lak_0  1  sfr_0  5  FACTOR       0.25000000" in text


def test_an_end_is_a_name_a_point_or_both_and_resolves_to_exactly_one_feature(staged_beside, tmp_path):
    ends = [{"from": "creek", "from_at": "s1"}, {"from_at": "s1"}]
    for end in ends:
        _, text = _mvr(_deck({"WaterMover": {**end, "to": "pond", "type": "FACTOR", "value": 1.0}}), tmp_path)
        assert "sfr_0  2  lak_0  1" in text
    assert "lak_0  1  sfr_0  5" in _mvr(_deck({"WaterMover": {"from": "pond", "to_at": "n1", "type": "FACTOR", "value": 1.0}}),
                                        tmp_path)[1]


@pytest.mark.parametrize("move,named", [
    ({"from": "creek", "to": "pond"}, "names 3 features"),
    ({"from": "river", "to": "pond"}, "no placed feature carries that name"),
    ({"from": "creek", "from_at": "n1", "to": "pond"}, "names 0 features"),
    ({"from_at": "off", "to": "pond"}, "It was not moved"),
    ({"from": "pond", "from_outlet": 2, "to": "ditch", "to_at": "n1"}, "1 outlet"),
    ({"from": "creek", "from_at": "s1", "from_outlet": 1, "to": "pond"}, "from_outlet"),
])
def test_an_end_naming_no_feature_or_several_refuses_by_what_it_matched(staged_beside, tmp_path, move, named):
    sheet = _deck({"WaterMover": {"type": "FACTOR", "value": 1.0, **move}}, off=_point(500.0, 500.0))
    assert named in _refused(sheet, tmp_path)


def test_a_point_in_a_lake_s_cell_names_the_lake(staged_beside, tmp_path):
    _, text = _mvr(_deck({"WaterMover": {"from": "pond", "to_at": "n1", "type": "FACTOR", "value": 1.0}}), tmp_path)
    assert "lak_0  1  sfr_0  5" in text
    _, text = _mvr(_deck({"WaterMover": {"from_at": "s1", "to_at": "centre", "type": "EXCESS", "value": 0.0}}), tmp_path)
    assert "sfr_0  2  lak_0  1  EXCESS" in text


def test_a_lake_with_several_outlets_gives_water_through_the_one_from_outlet_names(staged_beside, tmp_path):
    lake = {"Lakes": {"geometry": "pond", **_LAKE, "name": "pond", "outlets": [_POND, {**_POND, "rate": -0.25}]}}
    move = {"from": "pond", "to": "ditch", "to_at": "n1", "type": "UPTO", "value": 0.2}
    assert "outlet" in _refused(_deck(lake, {"WaterMover": move}), tmp_path)
    _, text = _mvr(_deck(lake, {"WaterMover": {**move, "from_outlet": 2}}), tmp_path)
    assert "lak_0  2  sfr_0  5  UPTO" in text


def test_a_lake_with_no_outlet_gives_no_water_to_the_mover(staged_beside, tmp_path):
    lake = {"Lakes": {"geometry": "pond", **_LAKE, "name": "pond"}}
    assert "0 outlet" in _refused(_deck(lake, {"WaterMover": {"from": "pond", "to": "ditch", "to_at": "n1",
                                                          "type": "FACTOR", "value": 1.0}}), tmp_path)


@pytest.mark.parametrize("move,named", [
    ({"to": None}, "neither to nor to_at"), ({"type": "FACTOR"}, "value"), ({"type": "FACTOR", "value": 1.5}, "fraction"),
    ({"type": "UPTO", "value": -1.0}, "rate"), ({"type": "SPILL", "value": 1.0}, "SPILL"),
    ({"type": "UPTO", "value": {2: 1.0}}, "period 1"), ({"type": "UPTO", "value": {0: 1.0}}, "keyed by period"),
    ({"type": "UPTO", "value": 1.0, "from_outlet": 0}, "from_outlet"), ({"type": "UPTO", "value": True}, "value"),
    ({"type": "UPTO", "value": 1.0, "speed": 3}, "speed")])
def test_a_mover_statement_missing_its_value_or_stating_a_wrong_one_refuses_by_name(move, named):
    with pytest.raises(ModflowError) as caught:
        _deck({"WaterMover": {"from": "creek", "from_at": "s1", "to": "pond", **move}})
    assert named in str(caught.value)


def test_the_mover_keywords_beside_movers_and_a_false_mover_option_refuse(staged_beside, tmp_path):
    move = {"WaterMover": {"from": "creek", "from_at": "s1", "to": "pond", "type": "FACTOR", "value": 1.0}}
    assert "MVR_MAXMVR" in _refused(_deck(move, {"MVR_MAXMVR": 3}), tmp_path)
    assert "SFR_MOVER" in _refused(_deck(move, {"SFR_MOVER": False}), tmp_path)


def test_a_well_and_a_uzf_land_cell_give_and_take_water_by_their_numbers():
    streams = place_streams(_streams_of([(_SOUTH, "creek")]), nlay=1, frame=("meters", "days"), **_MESH)[4]
    wells = [{"package": "MAW", "name": "supply", "number": 0, "cells": {4}}]
    zones = [{"package": "UZF", "name": "field", "number": 7, "cells": {8}}]
    used, periods = mover_rows(
        [{"from": "supply", "to": "creek", "to_at": _point(15.0, 5.0), "type": "UPTO", "value": 1.0},
         {"from": "field", "to": "creek", "to_at": _point(25.0, 5.0), "type": "EXCESS", "value": {0: 0.0, 1: 2.0}}],
        [*streams, *wells, *zones], **_MESH)
    assert used == ["MAW", "SFR", "UZF"]
    assert periods == {0: [["MAW", 0, "SFR", 1, "UPTO", 1.0], ["UZF", 7, "SFR", 2, "EXCESS", 0.0]],
                       1: [["MAW", 0, "SFR", 1, "UPTO", 1.0], ["UZF", 7, "SFR", 2, "EXCESS", 2.0]]}


def _streams_of(lines):
    sheet = _fill({"Streams": [{"geometry": f"line{n}", **_STREAM, "name": name} for n, (_, name) in enumerate(lines)]},
                  **{f"line{n}": line for n, (line, _) in enumerate(lines)})
    return sheet.filled["SFR_PACKAGEDATA"].value["streams"]


# ---- SFR diversions

def test_a_diversion_connects_the_target_downstream_of_its_source_and_counts_it(staged_beside, tmp_path):
    divert = {"Diversions": {"from": "creek", "from_at": "s1", "to": "ditch", "to_at": "n0", "type": "upto",
                             "value": {1: 2.0, 2: 1.0}}}
    rundir, text = _written(_deck(divert), tmp_path, "gwf.sfr")
    assert "  2  1 2      10.00000000       5.00000000       0.00100000       9.00000000       1.00000000" \
           "  1.00000000E-05  0.03  3       1.00000000  1\n" in text
    assert "  4  1 7       8.00000000       5.00000000       0.00100000       9.00000000       1.00000000" \
           "  1.00000000E-05  0.03  2       0.00000000  0\n" in text
    assert "  2  1  -3  -4\n" in text and "  4  2  -5\n" in text
    assert "BEGIN diversions\n  2  1  4  UPTO\nEND diversions" in text
    assert "BEGIN period  1\n  2  diversion  1       2.00000000\nEND period  1" in text
    assert "BEGIN period  2\n  2  diversion  1       1.00000000\nEND period  2" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_reach_with_two_diversions_numbers_them_from_the_first_in_statement_order():
    sheet = _fill({"Streams": [{"geometry": "a", **_STREAM, "name": "a"}, {"geometry": "b", **_STREAM, "name": "b"},
                               {"geometry": "c", **_STREAM, "name": "c"}],
                   "Diversions": [{"from": "a", "from_at": "p", "to": "b", "to_at": "q", "type": "UPTO", "value": 1.0},
                                  {"from": "a", "from_at": "p", "to": "c", "to_at": "r", "type": "FRACTION", "value": 0.5}]},
                  a=_SOUTH, b=_line((2.0, 15.0), (28.0, 15.0)), c=_NORTH, p=_point(5.0, 5.0), q=_point(5.0, 15.0),
                  r=_point(5.0, 25.0))
    packagedata, connectiondata, perioddata, diversions, _ = place_streams(
        sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=("meters", "days"),
        diversions=sheet.filled["SFR_DIVERSIONS"].value["diversions"], **_MESH)
    assert diversions == [[0, 0, 3, "UPTO"], [0, 1, 6, "FRACTION"]]
    assert packagedata[0][-3:] == [3, 1.0, 2] and connectiondata[0] == [0, -1, -3, -6]
    assert perioddata == {0: [[0, "diversion", 0, 1.0], [0, "diversion", 1, 0.5]]}


@pytest.mark.parametrize("move,named", [
    ({"from": "creek", "from_at": "s1", "to": "creek", "to_at": "s1"}, "cannot take water"),
    ({"from": "creek", "from_at": "s1", "to": "creek", "to_at": "s2"}, "cannot take water"),
    ({"from": "creek", "from_at": "s1", "to": "ditch", "to_at": "n1"}, "cannot take water"),
    ({"from": "creek", "from_at": "s1", "to": "creek", "to_at": "centre"}, "names 0 features"),
    ({"from": "creek", "from_at": "s1", "to": "pond"}, "no placed feature carries"),
    ({"from": "creek", "to": "ditch", "to_at": "n0"}, "names 3 features"),
])
def test_a_diversion_to_a_reach_with_an_upstream_reach_or_to_itself_or_to_nothing_refuses(move, named):
    sheet = _deck()
    with pytest.raises(ModflowError) as caught:
        place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=("meters", "days"),
                      diversions=[{"type": "UPTO", "value": 1.0, **{k: _AT.get(v, v) if k.endswith("_at") else v
                                                                      for k, v in move.items()}}], **_MESH)
    assert named in str(caught.value)


def test_a_fraction_diversion_is_a_fraction_and_a_diversion_without_streams_refuses(staged_beside, tmp_path):
    with pytest.raises(ModflowError) as caught:
        _deck({"Diversions": {"from": "creek", "to": "ditch", "type": "FRACTION", "value": 2.0}})
    assert "fraction" in str(caught.value)
    sheet = _fill({"Diversions": {"from": "a", "to": "b", "type": "UPTO", "value": 1.0},
                   "SFR_NREACHES": 1, "SFR_PACKAGEDATA": [[0, (0, 0), 1.0, 1.0, 0.1, 0.0, 1.0, 1.0, 0.03, 0, 1.0, 0]],
                   "SFR_CONNECTIONDATA": [[0]]})
    assert "Streams" in _refused(sheet, tmp_path)


def test_the_diversion_and_cross_section_keywords_beside_the_statements_refuse(staged_beside, tmp_path):
    divert = {"Diversions": {"from": "creek", "from_at": "s1", "to": "ditch", "to_at": "n0", "type": "UPTO", "value": 1.0}}
    assert "SFR_CROSSSECTIONS" in _refused(_deck(divert, {"SFR_CROSSSECTIONS": [[0, "x.tab"]]}), tmp_path)
    assert "SFR_DIVERSIONS" in _refused(_deck({"SFR_DIVERSIONS": [[0, 0, 1, "UPTO"]]}), tmp_path)


# ---- SFR cross sections and LAK tables: the TAB6 files authoring writes

def test_a_cross_section_is_one_file_per_distinct_table_read_by_every_reach_of_the_line(staged_beside, tmp_path):
    other = [[0.0, 2.0], [0.5, 0.0], [1.0, 2.0]]
    deck = _deck({"Streams": [{"geometry": "south", **_STREAM, "name": "creek", "cross_section": _TABLE},
                              {"geometry": "north", **_STREAM, "name": "ditch", "cross_section": other}]})
    rundir, text = _written(deck, tmp_path, "gwf.sfr")
    assert "  1  TAB6  FILEIN  sfr_xs1.tab\n  2  TAB6  FILEIN  sfr_xs1.tab\n  3  TAB6  FILEIN  sfr_xs1.tab\n" \
           "  4  TAB6  FILEIN  sfr_xs2.tab\n" in text
    assert "NROW  3\n  NCOL  2" in (rundir / "sfr_xs1.tab").read_text()
    assert "       0.50000000       0.00000000\n" in (rundir / "sfr_xs1.tab").read_text()
    assert "       0.00000000       2.00000000\n" in (rundir / "sfr_xs2.tab").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_three_column_cross_section_carries_the_roughness_fraction(staged_beside, tmp_path):
    deck = _deck({"Streams": {"geometry": "south", **_STREAM, "cross_section": [[0.0, 1.0, 1.0], [0.5, 0.0, 0.5],
                                                                              [1.0, 1.0, 1.0]]}})
    rundir, _ = _written(deck, tmp_path, "gwf.sfr")
    assert "NCOL  3" in (rundir / "sfr_xs1.tab").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


@pytest.mark.parametrize("table", [[], [[0.0]], [[0.0, 1.0], [0.5]], [[0.0, "a"]], [[0.0, 1.0, 1.0, 1.0]], True])
def test_a_cross_section_that_is_no_table_of_numbers_refuses(table):
    with pytest.raises(ModflowError) as caught:
        _deck({"Streams": {"geometry": "south", **_STREAM, "cross_section": table}})
    assert "cross_section" in str(caught.value)


def test_a_lake_table_is_the_lake_s_file_and_counts_in_ntables(staged_beside, tmp_path):
    deck = _deck({"Lakes": {"geometry": "pond", **_LAKE, "name": "pond",
                            "table": [[0.0, 0.0, 0.0], [12.0, 10.0, 64.0], [14.0, 30.0, 80.0]]}})
    rundir, text = _written(deck, tmp_path, "gwf.lak")
    assert "NTABLES  1" in text and "BEGIN tables\n  1  TAB6  FILEIN  lak_tab1.tab\nEND tables" in text
    assert "12.0 10.0 64.0" in _Flat((rundir / "lak_tab1.tab").read_text().replace("00000000", "0"))
    assert validate_authored_simulation(rundir) == {"rc": 0}


@pytest.mark.parametrize("table", [[[0.0, 1.0]], [[0.0, 1.0, 2.0, 3.0]], [], [[0.0, 1.0, "a"]], 5])
def test_a_lake_table_without_three_numbers_a_row_refuses(table):
    with pytest.raises(ModflowError) as caught:
        _deck({"Lakes": {"geometry": "pond", **_LAKE, "table": table}})
    assert "table" in str(caught.value)


def test_a_lake_table_on_a_statement_placing_two_lakes_refuses(staged_beside, tmp_path):
    two = {"type": "MultiPolygon", "coordinates": [_CENTRE["coordinates"], _box(1.0, 11.0, 9.0, 19.0)["coordinates"]]}
    sheet = _fill({"Lakes": {"geometry": "two", **_LAKE, "table": [[0.0, 0.0, 0.0], [12.0, 1.0, 2.0]]}}, two=two)
    assert "table" in _refused(sheet, tmp_path)


def test_the_lake_table_keywords_beside_a_table_refuse(staged_beside, tmp_path):
    deck = _deck({"Lakes": {"geometry": "pond", **_LAKE, "table": [[0.0, 0.0, 0.0], [12.0, 1.0, 2.0]]}},
                 {"LAK_NTABLES": 1})
    assert "LAK_NTABLES" in _refused(deck, tmp_path)


def test_a_name_that_is_not_a_string_refuses():
    with pytest.raises(ModflowError) as caught:
        _deck({"Streams": {"geometry": "south", **_STREAM, "name": 3}})
    assert "name" in str(caught.value)
    with pytest.raises(ModflowError):
        _deck({"Lakes": {"geometry": "pond", **_LAKE, "name": " "}})


def test_lake_features_carry_their_outlets_numbered_across_lakes():
    sheet = _fill({"Lakes": [{"geometry": "pond", **_LAKE, "name": "a", "outlets": [_POND, {**_POND, "rate": -1.0}]},
                             {"geometry": "left", **_LAKE, "name": "b", "outlets": [_POND]}]},
                  pond=_CENTRE, left=_box(1.0, 11.0, 9.0, 19.0))
    *_, features = place_lakes(sheet.filled["LAK_PACKAGEDATA"].value["lakes"], nlay=1, frame=("meters", "days"),
                               top=[10.0] * 9, botm=[[0.0] * 9], **_MESH)
    assert [(f["name"], f["number"], f["outlets"]) for f in features] == [("a", 0, [0, 1]), ("b", 1, [2])]
