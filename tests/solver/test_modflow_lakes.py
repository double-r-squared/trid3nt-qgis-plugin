"""LAK: stated by the module's own keywords, and placed by the Lakes composite from a polygon.

Offline except the validate cases, which run mf6's own --mode validate through the image.
The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the
south-west; polygons are drawn on it in lon/lat and every count and length below is computed by hand.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import hold_lakes, place_lakes
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
_ONE_LAYER = {"top": [10.0] * _N, "botm": [[0.0] * _N]}
_TWO_LAYERS = {"top": [10.0] * _N, "botm": [[5.0] * _N, [0.0] * _N]}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


_CENTRE = _box(11.0, 11.0, 19.0, 19.0)
_MIDDLE_ROW = _box(1.0, 11.0, 29.0, 19.0)


def _statement(geometry, **over):
    values = {"stage": 12.0, "bottom": 0.0, "leakance": 0.1, "rainfall": None, "evaporation": None}
    return {"composite": "Lakes", "geometry": geometry, "values": {**values, **over}}


def _place(statements, grid=_ONE_LAYER, *, nlay=1, frame=("meters", "days"), active=None):
    return place_lakes(statements, points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=nlay,
                       frame=frame, active=active, **grid)


# ---- Part B: the lake cells and their connections --------------------------


def test_a_polygon_over_one_cell_is_a_lake_connected_to_its_four_neighbours():
    packagedata, connectiondata, perioddata = _place([_statement(_CENTRE)])

    assert packagedata == [[0, 12.0, 4]]
    assert [(row[0], row[1], row[2], row[3]) for row in connectiondata] \
        == [(0, 0, (0, 1), "horizontal"), (0, 1, (0, 3), "horizontal"),
            (0, 2, (0, 5), "horizontal"), (0, 3, (0, 7), "horizontal")]
    # bed leakance, bottom and top of the neighbour, half its width to the face, the face's width
    assert all(row[4:] == [0.1, 0.0, 10.0, 5.0, 10.0] for row in connectiondata)
    assert perioddata == []


def test_a_lake_one_layer_deep_connects_horizontally_and_to_the_cell_below():
    # the lake bottom (6) lies under layer 1's top (10) and over layer 2's top (5)
    _, connectiondata, _ = _place([_statement(_CENTRE, bottom=6.0)], _TWO_LAYERS, nlay=2)

    assert [(row[2], row[3]) for row in connectiondata] \
        == [((0, 1), "horizontal"), ((0, 3), "horizontal"), ((0, 5), "horizontal"),
            ((0, 7), "horizontal"), ((1, 4), "vertical")]
    assert connectiondata[0][5:7] == [5.0, 10.0]
    assert connectiondata[-1][5:] == [0.0, 0.0, 0.0, 0.0]
    assert [row[1] for row in connectiondata] == [0, 1, 2, 3, 4]


def test_a_lake_reaching_the_second_layer_holds_both_and_connects_each_layer_beside_it():
    packagedata, connectiondata, _ = _place([_statement(_CENTRE, bottom=4.0)], _TWO_LAYERS, nlay=2)

    assert packagedata == [[0, 12.0, 8]]
    assert [row[2] for row in connectiondata] \
        == [(0, 1), (0, 3), (0, 5), (0, 7), (1, 1), (1, 3), (1, 5), (1, 7)]
    assert {row[3] for row in connectiondata} == {"horizontal"}
    assert connectiondata[4][5:7] == [0.0, 5.0]


def test_two_lake_cells_side_by_side_connect_only_to_the_cells_that_are_not_the_lake():
    packagedata, connectiondata, _ = _place([_statement(_box(11.0, 11.0, 29.0, 19.0))])

    assert packagedata == [[0, 12.0, 5]]
    assert [row[2] for row in connectiondata] == [(0, 1), (0, 3), (0, 7), (0, 2), (0, 8)]


def test_the_bottom_read_from_a_raster_sets_each_cell_s_depth(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "bottom.tif"
    grid = np.full((3, 3), 9.0, dtype="float32")
    grid[1] = [4.0, 6.0, 6.0]       # the middle row of the lattice; the raster's first row is the north
    with rasterio.open(path, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32",
                       crs="EPSG:32617", transform=from_origin(0.0, 30.0, 10.0, 10.0)) as dst:
        dst.write(grid, 1)
    bottom = {"uri": str(path), "units": "m"}
    packagedata, connectiondata, _ = _place([_statement(_MIDDLE_ROW, bottom=bottom)],
                                            _TWO_LAYERS, nlay=2)

    # cells 3 (both layers), 4 and 5 (layer 1); a lake cell is never another's neighbour or vertical
    assert [row[2] for row in connectiondata] \
        == [(0, 0), (0, 6), (0, 1), (0, 7), (1, 4), (0, 2), (0, 8), (1, 5),
            (1, 0), (1, 4), (1, 6)]
    assert packagedata == [[0, 12.0, 11]]


def test_a_neighbour_the_run_does_not_solve_is_not_connected():
    active = [[1] * _N, [1] * _N]
    active[0][1] = 0
    _, connectiondata, _ = _place([_statement(_CENTRE, bottom=6.0)], _TWO_LAYERS, nlay=2, active=active)
    assert [row[2] for row in connectiondata] == [(0, 3), (0, 5), (0, 7), (1, 4)]


def test_one_polygon_in_several_parts_is_several_lakes_numbered_in_order():
    two = {"type": "MultiPolygon", "coordinates": [_box(1.0, 1.0, 9.0, 9.0)["coordinates"],
                                                   _box(21.0, 21.0, 29.0, 29.0)["coordinates"]]}
    packagedata, connectiondata, _ = _place([_statement(two)])

    assert packagedata == [[0, 12.0, 2], [1, 12.0, 2]]
    assert [(row[0], row[2]) for row in connectiondata] \
        == [(0, (0, 1)), (0, (0, 3)), (1, (0, 5)), (1, (0, 7))]


def test_a_stated_rainfall_and_evaporation_are_the_first_period_s_rows_and_an_unstated_one_is_absent():
    _, _, both = _place([_statement(_CENTRE, rainfall=0.01, evaporation=0.02)])
    _, _, one = _place([_statement(_CENTRE, evaporation=0.02)])
    assert both == [[0, "rainfall", 0.01], [0, "evaporation", 0.02]]
    assert one == [[0, "evaporation", 0.02]]


def test_a_connection_s_lengths_are_on_the_model_s_length_unit():
    _, connectiondata, _ = _place([_statement(_CENTRE)], frame=("feet", "days"))
    assert connectiondata[0][7:] == [pytest.approx(5.0 / 0.3048), pytest.approx(10.0 / 0.3048)]


def test_a_length_unit_the_run_does_not_state_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_CENTRE)], frame=(None, "days"))
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"


# ---- Part B: what refuses --------------------------------------------------


def test_a_polygon_holding_no_cell_centre_refuses_naming_the_lake():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_box(11.0, 11.0, 14.0, 14.0))])
    assert "lake 1" in str(caught.value) and "no cell centre" in str(caught.value)


def test_a_lake_bottom_above_every_layer_top_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_CENTRE, bottom=20.0)])
    assert "lake 1" in str(caught.value) and "bottom" in str(caught.value)


def test_a_lake_cell_the_run_does_not_solve_is_not_held():
    active = [[1] * _N]
    active[0][4] = 0
    with pytest.raises(ModflowError):
        _place([_statement(_CENTRE)], active=active)


def test_two_lakes_over_one_cell_refuse_naming_both():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_CENTRE), _statement(_box(12.0, 12.0, 18.0, 18.0))])
    assert "lake 1" in str(caught.value) and "lake 2" in str(caught.value)


def test_a_lake_with_nothing_beside_or_below_it_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_box(-1.0, -1.0, 31.0, 31.0))])
    assert "lake 1" in str(caught.value) and "connect" in str(caught.value)


def test_a_line_is_no_lake():
    line = {"type": "LineString", "coordinates": [list(_TO_LONLAT(2.0, 15.0)), list(_TO_LONLAT(28.0, 15.0))]}
    with pytest.raises(ModflowError):
        _place([_statement(line)])


def test_a_lake_off_the_mesh_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_box(100.0, 100.0, 120.0, 120.0))])
    assert "no cell centre" in str(caught.value)


# ---- Part B: the lake cells leave the aquifer ------------------------------


def test_the_lake_cells_are_zero_in_the_idomain_the_rest_keep():
    rows = hold_lakes([_statement(_CENTRE, bottom=4.0)], None, points=_POINTS, cells=_CELLS,
                      utm_epsg=32617, nlay=2, frame=("meters", "days"), **_TWO_LAYERS)
    assert [row[4] for row in rows] == [0, 0]
    assert sum(sum(row) for row in rows) == 2 * _N - 2

    stated = [[2] * _N, [1] * _N]
    rows = hold_lakes([_statement(_CENTRE, bottom=6.0)], stated, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                      nlay=2, frame=("meters", "days"), **_TWO_LAYERS)
    assert rows[0][4] == 0 and rows[0][3] == 2 and rows[1] == [1] * _N


# ---- Part B: the composite's statement -------------------------------------

_VALUES = {"stage": 12.0, "bottom": 0.0, "leakance": 0.1}


def _fill(stated: dict, **produced):
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": 1, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": [[0.0] * _N], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0,
        "IC_STRT": 5.0, "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[10.0, 1, 1.0]], **stated})


@pytest.mark.parametrize("argument", ["stage", "bottom", "leakance"])
def test_a_value_left_unstated_refuses_by_name(argument):
    for dropped in (True, False):
        body = {"geometry": "where", **_VALUES}
        if dropped:
            body.pop(argument)
        else:
            body[argument] = None
        with pytest.raises(ModflowError) as caught:
            _fill({"Lakes": body}, where=_CENTRE)
        assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"
        assert "Lakes" in str(caught.value) and argument in str(caught.value)


@pytest.mark.parametrize("argument", ["stage", "leakance", "rainfall", "evaporation"])
def test_a_value_that_is_not_one_number_refuses(argument):
    with pytest.raises(ModflowError) as caught:
        _fill({"Lakes": {"geometry": "where", **_VALUES, argument: [1.0, 2.0]}}, where=_CENTRE)
    assert argument in str(caught.value)


def test_an_argument_the_composite_does_not_take_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"Lakes": {"geometry": "where", **_VALUES, "depth": 3.0}}, where=_CENTRE)
    assert "depth" in str(caught.value)


def test_a_geometry_reading_nothing_states_nothing():
    sheet = _fill({"Lakes": {"geometry": "where", **_VALUES}}, where=None)
    assert "LAK_PACKAGEDATA" not in sheet.filled


def test_a_list_of_statements_is_one_package_s_lakes():
    sheet = _fill({"Lakes": [{"geometry": "a", **_VALUES}, {"geometry": "b", **_VALUES}]},
                  a=_CENTRE, b=_box(1.0, 1.0, 9.0, 9.0))
    assert len(sheet.filled["LAK_PACKAGEDATA"].value["lakes"]) == 2


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


def test_lakes_write_the_lak_package_the_disv_holds_the_lake_cell_inactive_and_mf6_validates(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": {"geometry": "where", **_VALUES, "evaporation": 0.001},
                   "LAK_PRINT_STAGE": True}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)

    written = serialize(sheet, rundir, mesh)["written"]

    assert "gwf.lak" in written
    text = (rundir / "gwf.lak").read_text()
    assert "NLAKES  1" in text and "evaporation" in text and "rainfall" not in text
    disv = (rundir / "mesh.disv").read_text()
    idomain = disv.split("idomain", 1)[1].split("END", 1)[0].split()
    assert [int(v) for v in idomain if v.lstrip("-").isdigit()][-_N:] == [1, 1, 1, 1, 0, 1, 1, 1, 1]
    assert validate_authored_simulation(rundir) == {"rc": 0}


@pytest.mark.parametrize("keyword,value", [("LAK_NLAKES", 2), ("LAK_CONNECTIONDATA", [[0, 0, (0, 1), "vertical", 0.1, 0.0, 0.0, 0.0, 0.0]])])
def test_stating_the_lake_keywords_beside_the_polygons_refuses(staged_beside, tmp_path, keyword, value):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": {"geometry": "where", **_VALUES}, keyword: value}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert keyword in str(caught.value)


def test_stating_the_period_rows_beside_a_stated_flux_refuses_and_beside_none_does_not(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    rows = {0: [[0, "inflow", 1.0]]}
    sheet = _fill({"Lakes": {"geometry": "where", **_VALUES, "rainfall": 0.01},
                   "LAK_PERIODDATA": rows}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "LAK_PERIODDATA" in str(caught.value)

    sheet = _fill({"Lakes": {"geometry": "where", **_VALUES}, "LAK_PERIODDATA": rows}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    assert "gwf.lak" in serialize(sheet, rundir, mesh)["written"]
    assert "inflow" in (rundir / "gwf.lak").read_text()


# ---- Part A: LAK by the module's own keywords -------------------------------


def test_lak_stated_by_its_own_keywords_writes_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    connections = [[0, n, (0, cell), "horizontal", 0.1, 0.0, 10.0, 5.0, 10.0]
                   for n, cell in enumerate((1, 3, 5, 7))]
    sheet = _fill({
        "DISV_IDOMAIN": [[1, 1, 1, 1, 0, 1, 1, 1, 1]],
        "LAK_NLAKES": 1, "LAK_NOUTLETS": 0, "LAK_NTABLES": 0, "LAK_PRINT_STAGE": True,
        "LAK_STAGE_FILERECORD": "gwf.lak.stage.bin",
        "LAK_PACKAGEDATA": [[0, 12.0, 4]], "LAK_CONNECTIONDATA": connections,
        "LAK_PERIODDATA": {0: [[0, "rainfall", 0.01], [0, "evaporation", 0.02]]}})
    rundir = _rundir(sheet, mesh, tmp_path)

    assert "gwf.lak" in serialize(sheet, rundir, mesh)["written"]
    text = (rundir / "gwf.lak").read_text()
    assert "STAGE  FILEOUT  gwf.lak.stage.bin" in text and "horizontal" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}
