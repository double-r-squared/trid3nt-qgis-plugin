"""MAW: stated by the module's own keywords, and placed by the MultiAquiferWells composite from a point.

Offline except the validate cases, which run mf6's own --mode validate through the image.
The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the
south-west, two layers (10 to 5, 5 to 0); points are drawn on it in lon/lat and every
connection and row below is computed by hand.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.inputs.point import PointNotViableError
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_maw
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
_GRID = {"top": [10.0] * _N, "botm": [[5.0] * _N, [0.0] * _N]}


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


_CENTRE = _point(15.0, 15.0)
_VALUES = {"radius": 0.15, "bottom": 0.0, "start_head": 8.0, "condeqn": "THIEM",
           "screen_top": 10.0, "screen_bottom": 0.0, "rate": -3.0,
           "hk_skin": -999.0, "radius_skin": -999.0}


def _statement(geometry=_CENTRE, **over):
    return {"composite": "MultiAquiferWells", "geometry": geometry, "values": {**_VALUES, **over}}


def _place(statements, *, active=None):
    return place_maw(statements, points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=2,
                     active=active, **_GRID)


# ---- Part B: the wells and their connections -------------------------------


def test_a_point_is_a_well_in_its_cell_connected_to_each_layer_its_screen_crosses():
    packagedata, connectiondata, perioddata = _place([_statement()])

    assert packagedata == [[0, 0.15, 0.0, 8.0, "THIEM", 2]]
    assert connectiondata == [[0, 0, (0, 4), 10.0, 0.0, -999.0, -999.0],
                              [0, 1, (1, 4), 10.0, 0.0, -999.0, -999.0]]
    assert perioddata == {0: [[0, "rate", -3.0]]}


def test_a_screen_in_the_lower_layer_alone_connects_that_layer_only():
    _, connectiondata, _ = _place([_statement(screen_top=4.0, screen_bottom=1.0)])
    assert [(row[1], row[2]) for row in connectiondata] == [(0, (1, 4))]


def test_a_screen_in_the_upper_layer_alone_connects_that_layer_only():
    packagedata, connectiondata, _ = _place([_statement(screen_top=9.0, screen_bottom=6.0)])
    assert [(row[1], row[2]) for row in connectiondata] == [(0, (0, 4))]
    assert packagedata[0][5] == 1


def test_a_screen_ending_on_a_layer_boundary_does_not_connect_the_layer_it_only_touches():
    # the screen's top (5) is the upper layer's bottom: it crosses no thickness of it
    _, connectiondata, _ = _place([_statement(screen_top=5.0, screen_bottom=1.0)])
    assert [row[2] for row in connectiondata] == [(1, 4)]
    # and its bottom (5) is the lower layer's top
    _, connectiondata, _ = _place([_statement(screen_top=9.0, screen_bottom=5.0)])
    assert [row[2] for row in connectiondata] == [(0, 4)]


def test_a_layer_the_run_does_not_solve_at_the_cell_is_not_connected():
    active = [[1, 1, 1, 1, 0, 1, 1, 1, 1], [1] * _N]
    packagedata, connectiondata, _ = _place([_statement()], active=active)
    assert [row[2] for row in connectiondata] == [(1, 4)]
    assert packagedata[0][5] == 1


def test_a_screen_over_no_solved_layer_refuses_naming_the_well():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(screen_top=30.0, screen_bottom=20.0)])
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"
    assert "well 1" in str(caught.value) and "cell 4" in str(caught.value)


def test_a_screen_over_layers_the_cell_does_not_solve_refuses():
    active = [[1, 1, 1, 1, 0, 1, 1, 1, 1], [1, 1, 1, 1, 0, 1, 1, 1, 1]]
    with pytest.raises(PointNotViableError) as caught:
        _place([_statement()], active=active)
    assert caught.value.hint is not None


def test_a_point_off_the_mesh_refuses_with_the_shared_code_and_a_hint():
    with pytest.raises(PointNotViableError) as caught:
        _place([_statement(_point(500.0, 500.0))])
    assert caught.value.error_code == "POINT_NOT_VIABLE"
    assert "MultiAquiferWells" in str(caught.value) and caught.value.hint is not None


def test_several_points_are_wells_numbered_in_order_with_their_own_connections():
    many = {"type": "MultiPoint", "coordinates": [_point(5.0, 5.0)["coordinates"],
                                                  _point(25.0, 25.0)["coordinates"]]}
    packagedata, connectiondata, perioddata = _place([_statement(many)])

    assert [row[0] for row in packagedata] == [0, 1]
    assert [(row[0], row[1], row[2]) for row in connectiondata] \
        == [(0, 0, (0, 0)), (0, 1, (1, 0)), (1, 0, (0, 8)), (1, 1, (1, 8))]
    assert perioddata == {0: [[0, "rate", -3.0], [1, "rate", -3.0]]}


def test_a_list_of_statements_numbers_the_wells_through_all_of_them():
    packagedata, connectiondata, perioddata = _place([
        _statement(_point(5.0, 5.0), rate=-1.0), _statement(_point(25.0, 5.0), rate=-2.0, screen_top=4.0)])

    assert [row[0] for row in packagedata] == [0, 1]
    assert [(row[0], row[2]) for row in connectiondata] == [(0, (0, 0)), (0, (1, 0)), (1, (1, 2))]
    assert perioddata == {0: [[0, "rate", -1.0], [1, "rate", -2.0]]}


def test_two_wells_in_one_cell_are_two_wells():
    packagedata, connectiondata, _ = _place([_statement(), _statement(_point(16.0, 16.0))])
    assert [row[0] for row in packagedata] == [0, 1]
    assert {row[2] for row in connectiondata} == {(0, 4), (1, 4)}


def test_a_line_is_no_well():
    line = {"type": "LineString", "coordinates": [list(_TO_LONLAT(2.0, 5.0)), list(_TO_LONLAT(28.0, 5.0))]}
    with pytest.raises(ModflowError) as caught:
        _place([_statement(line)])
    assert "points" in str(caught.value)


def test_the_connection_carries_the_stated_skin_values():
    _, connectiondata, _ = _place([_statement(condeqn="SKIN", hk_skin=0.5, radius_skin=0.4)])
    assert all(row[5:] == [0.5, 0.4] for row in connectiondata)


# ---- Part B: the composite's statement -------------------------------------

_STATED = {"radius": 0.15, "bottom": 0.0, "start_head": 8.0, "condeqn": "THIEM",
           "screen_top": 10.0, "screen_bottom": 0.0, "rate": -3.0}


def _fill(stated: dict, **produced):
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": 2, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": [[5.0] * _N, [0.0] * _N], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0,
        "IC_STRT": 8.0, "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[10.0, 1, 1.0]], **stated})


@pytest.mark.parametrize("argument", list(_STATED))
def test_a_value_left_unstated_refuses_by_name(argument):
    for dropped in (True, False):
        body = {"geometry": "where", **_STATED}
        if dropped:
            body.pop(argument)
        else:
            body[argument] = None
        with pytest.raises(ModflowError) as caught:
            _fill({"MultiAquiferWells": body}, where=_CENTRE)
        assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"
        assert "MultiAquiferWells" in str(caught.value) and argument in str(caught.value)


@pytest.mark.parametrize("argument", [name for name in _STATED if name != "condeqn"])
def test_a_value_that_is_not_one_number_refuses(argument):
    for held in ([1.0, 2.0], True):
        with pytest.raises(ModflowError) as caught:
            _fill({"MultiAquiferWells": {"geometry": "where", **_STATED, argument: held}}, where=_CENTRE)
        assert argument in str(caught.value)


def test_an_unknown_conductance_equation_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"MultiAquiferWells": {"geometry": "where", **_STATED, "condeqn": "DARCY"}}, where=_CENTRE)
    assert "DARCY" in str(caught.value) and "THIEM" in str(caught.value)


def test_a_screen_whose_top_is_not_above_its_bottom_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"MultiAquiferWells": {"geometry": "where", **_STATED, "screen_top": 0.0}}, where=_CENTRE)
    assert "screen" in str(caught.value)


def test_an_argument_the_composite_does_not_take_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"MultiAquiferWells": {"geometry": "where", **_STATED, "depth": 3.0}}, where=_CENTRE)
    assert "depth" in str(caught.value)


def test_the_skin_values_are_those_the_conductance_equation_reads():
    def wells(**body):
        sheet = _fill({"MultiAquiferWells": {"geometry": "where", **_STATED, **body}}, where=_CENTRE)
        return sheet.filled["MAW_PACKAGEDATA"].value["wells"][0]["values"]

    assert (wells()["hk_skin"], wells()["radius_skin"]) == (-999.0, -999.0)
    assert (wells(condeqn="skin", hk_skin=0.5, radius_skin=0.4)["hk_skin"],
            wells(condeqn="skin", hk_skin=0.5, radius_skin=0.4)["radius_skin"]) == (0.5, 0.4)
    specified = wells(condeqn="SPECIFIED", hk_skin=20.0)
    assert (specified["hk_skin"], specified["radius_skin"]) == (20.0, -999.0)
    for equation, missing in (("SKIN", {"radius_skin": 0.4}), ("CUMULATIVE", {"hk_skin": 0.5}),
                              ("MEAN", {}), ("SPECIFIED", {})):
        with pytest.raises(ModflowError) as caught:
            wells(condeqn=equation, **missing)
        assert equation in str(caught.value)
    for equation, extra in (("THIEM", "hk_skin"), ("SPECIFIED", "radius_skin")):
        with pytest.raises(ModflowError) as caught:
            wells(condeqn=equation, hk_skin=1.0, radius_skin=1.0)
        assert extra in str(caught.value)


def test_a_geometry_reading_nothing_states_nothing():
    sheet = _fill({"MultiAquiferWells": {"geometry": "where", **_STATED}}, where=None)
    assert "MAW_PACKAGEDATA" not in sheet.filled


def test_a_list_of_statements_is_one_package_s_wells():
    sheet = _fill({"MultiAquiferWells": [{"geometry": "a", **_STATED}, {"geometry": "b", **_STATED}]},
                  a=_CENTRE, b=_point(5.0, 5.0))
    assert len(sheet.filled["MAW_PACKAGEDATA"].value["wells"]) == 2


# ---- Part B: the write -----------------------------------------------------


def _lattice(tmp_path: Path) -> Path:
    rows = ["MESH2D"]
    for cell, ring in enumerate(_CELLS, start=1):
        rows.append(f"E4Q {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    return path


def _mesh(tmp_path: Path) -> dict:
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d",
        display_uri=str(_lattice(tmp_path)), crs_authid="EPSG:32617",
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


def _rundir(sheet, mesh, tmp_path):
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    return rundir


def test_wells_write_the_maw_package_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"MultiAquiferWells": [{"geometry": "a", **_STATED},
                                         {"geometry": "b", **_STATED, "condeqn": "mean",
                                          "hk_skin": 0.5, "radius_skin": 0.4}],
                   "MAW_NO_WELL_STORAGE": True}, a=_CENTRE, b=_point(5.0, 25.0))
    rundir = _rundir(sheet, mesh, tmp_path)

    written = serialize(sheet, rundir, mesh)["written"]

    assert "gwf.maw" in written
    text = (rundir / "gwf.maw").read_text()
    assert "NMAWWELLS  2" in text and "NO_WELL_STORAGE" in text and "MEAN" in text
    assert "  1  rate      -3.0" in text and "  2  rate      -3.0" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_two_wells_in_one_cell_validate_in_mf6(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"MultiAquiferWells": [{"geometry": "a", **_STATED},
                                         {"geometry": "b", **_STATED}]}, a=_CENTRE, b=_point(16.0, 16.0))
    rundir = _rundir(sheet, mesh, tmp_path)
    serialize(sheet, rundir, mesh)
    assert validate_authored_simulation(rundir) == {"rc": 0}


@pytest.mark.parametrize("keyword,value", [
    ("MAW_NMAWWELLS", 2),
    ("MAW_CONNECTIONDATA", [[0, 0, (0, 4), 10.0, 0.0, -999.0, -999.0]]),
    ("MAW_PERIODDATA", {0: [[0, "rate", 1.0]]})])
def test_stating_the_well_keywords_beside_the_points_refuses(staged_beside, tmp_path, keyword, value):
    mesh = _mesh(tmp_path)
    sheet = _fill({"MultiAquiferWells": {"geometry": "where", **_STATED}, keyword: value}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert keyword in str(caught.value)


# ---- Part A: MAW by the module's own keywords ------------------------------


def test_maw_stated_by_its_own_keywords_writes_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({
        "MAW_NO_WELL_STORAGE": True, "MAW_NMAWWELLS": 1,
        "MAW_PACKAGEDATA": [[0, 0.15, 0.0, 8.0, "THIEM", 2]],
        "MAW_CONNECTIONDATA": [[0, 0, (0, 4), 10.0, 0.0, -999.0, -999.0],
                               [0, 1, (1, 4), 10.0, 0.0, -999.0, -999.0]],
        "MAW_PERIODDATA": {0: [[0, "rate", -3.0]]}})
    rundir = _rundir(sheet, mesh, tmp_path)

    assert "gwf.maw" in serialize(sheet, rundir, mesh)["written"]
    text = (rundir / "gwf.maw").read_text()
    assert "THIEM" in text and "-999" in text and "rate" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}
