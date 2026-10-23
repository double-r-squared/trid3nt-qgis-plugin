"""UZF: stated by the module's own keywords, and placed by the UnsaturatedZone composite from a polygon.

Offline except the validate cases, which run mf6's own --mode validate through the image.
The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the
south-west, three layers (10 to 7, 7 to 4, 4 to 0); polygons are drawn on it in lon/lat
and every UZF cell and row below is computed by hand.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_uzf
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 3
_N = _SIDE * _SIDE
_NLAY = 3
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE)
          for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_BOTM = [[7.0] * _N, [4.0] * _N, [0.0] * _N]


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


_CENTRE = _box(11.0, 11.0, 19.0, 19.0)
_MIDDLE_ROW = _box(1.0, 11.0, 29.0, 19.0)
_VALUES = {"surfdep": 0.1, "vks": 0.5, "thtr": 0.1, "thts": 0.45, "thti": 0.105, "eps": 4.0, "finf": 0.02}
_ET = {name: None for name in ("pet", "extdp", "extwc", "ha", "hroot", "rootact")}


def _statement(geometry=_CENTRE, **over):
    return {"composite": "UnsaturatedZone", "geometry": geometry, "values": {**_VALUES, **_ET, **over}}


def _place(statements, *, active=None, **options):
    return place_uzf(statements, points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=_NLAY,
                     frame=("meters", "days"), active=active, options=options)[:2]


def _solved(**off):
    rows = [[1] * _N for _ in range(_NLAY)]
    for (layer, cell) in off.get("off", ()):
        rows[layer][cell] = 0
    return rows


_ROW = [0.5, 0.1, 0.45, 0.105, 4.0]   # vks, thtr, thts, thti, eps


# ---- Part B: the UZF cells -------------------------------------------------


def test_a_polygon_over_one_column_is_a_land_cell_and_the_cells_under_it():
    packagedata, perioddata = _place([_statement()])

    assert packagedata == [[0, (0, 4), 1, 1, 0.1, *_ROW],
                           [1, (1, 4), 0, 2, 0.1, *_ROW],
                           [2, (2, 4), 0, -1, 0.1, *_ROW]]
    assert perioddata == {0: [[0, 0.02, -999.0, -999.0, -999.0, -999.0, -999.0, -999.0]]}


def test_cells_are_numbered_by_layer_then_cell_and_each_connects_to_the_one_below():
    packagedata, perioddata = _place([_statement(_MIDDLE_ROW)])

    assert [(row[0], row[1], row[2], row[3]) for row in packagedata] == [
        (0, (0, 3), 1, 3), (1, (0, 4), 1, 4), (2, (0, 5), 1, 5),
        (3, (1, 3), 0, 6), (4, (1, 4), 0, 7), (5, (1, 5), 0, 8),
        (6, (2, 3), 0, -1), (7, (2, 4), 0, -1), (8, (2, 5), 0, -1)]
    assert [row[0] for row in perioddata[0]] == [0, 1, 2]


def test_the_land_cell_is_the_top_solved_cell_of_the_column():
    packagedata, perioddata = _place([_statement()], active=_solved(off=[(0, 4)]))

    assert [(row[1], row[2], row[3]) for row in packagedata] == [((1, 4), 1, 1), ((2, 4), 0, -1)]
    assert perioddata[0][0][0] == 0


def test_the_cells_under_a_layer_the_run_does_not_solve_are_not_uzf_cells():
    packagedata, _ = _place([_statement()], active=_solved(off=[(1, 4)]))
    assert [(row[1], row[2], row[3]) for row in packagedata] == [((0, 4), 1, -1)]


def test_a_column_the_run_solves_on_no_layer_is_left_out():
    packagedata, _ = _place([_statement(_MIDDLE_ROW)], active=_solved(off=[(0, 4), (1, 4), (2, 4)]))
    assert sorted({row[1][1] for row in packagedata}) == [3, 5]


def test_a_polygon_over_columns_the_run_solves_on_no_layer_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement()], active=_solved(off=[(0, 4), (1, 4), (2, 4)]))
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE" and "1 cell centre" in str(caught.value)


def test_a_polygon_holding_no_cell_centre_refuses_naming_it():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_box(11.0, 11.0, 14.0, 14.0))])
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"
    assert "no cell centre" in str(caught.value)


def test_a_line_is_no_unsaturated_zone():
    line = {"type": "LineString", "coordinates": [list(_TO_LONLAT(2.0, 15.0)), list(_TO_LONLAT(28.0, 15.0))]}
    with pytest.raises(ModflowError) as caught:
        _place([_statement(line)])
    assert "polygons" in str(caught.value)


def test_two_statements_over_one_column_refuse():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_CENTRE), _statement(_MIDDLE_ROW)])
    assert "statements 1 and 2" in str(caught.value) and "cell 4" in str(caught.value)


def test_a_list_of_statements_numbers_the_cells_through_all_of_them():
    packagedata, perioddata = _place([_statement(_CENTRE, finf=0.01), _statement(_box(1.0, 1.0, 9.0, 9.0), finf=0.03)])

    assert [row[1] for row in packagedata] == [(0, 0), (0, 4), (1, 0), (1, 4), (2, 0), (2, 4)]
    assert [(row[0], row[1]) for row in perioddata[0]] == [(0, 0.03), (1, 0.01)]


def test_a_property_read_from_a_raster_is_each_column_s_own():
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    import tempfile

    grid = np.full((3, 3), 0.5, dtype="float32")
    grid[1] = [0.2, 0.3, 0.4]       # the middle row of the lattice; the raster's first row is the north
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "vks.tif"
        with rasterio.open(path, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32",
                           crs="EPSG:32617", transform=from_origin(0.0, 30.0, 10.0, 10.0)) as dst:
            dst.write(grid, 1)
        packagedata, _ = _place([_statement(_MIDDLE_ROW, vks={"uri": str(path), "units": "m/d"})])

    assert [round(row[5], 6) for row in packagedata if row[2]] == [0.2, 0.3, 0.4]
    assert [round(row[5], 6) for row in packagedata if row[1][0] == 2] == [0.2, 0.3, 0.4]


# ---- Part B: the ET values -------------------------------------------------


def test_et_values_stated_with_no_et_option_refuse_naming_them():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(pet=0.01)])
    assert "pet" in str(caught.value) and "UZF_SIMULATE_ET" in str(caught.value)


def test_simulate_et_requires_pet_and_extdp_and_writes_them():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(pet=0.01)], simulate_et=True)
    assert "extdp" in str(caught.value) and "no " in str(caught.value)
    _, perioddata = _place([_statement(pet=0.01, extdp=1.5)], simulate_et=True)
    assert perioddata == {0: [[0, 0.02, 0.01, 1.5, -999.0, -999.0, -999.0, -999.0]]}


def test_the_options_that_read_the_other_et_values_require_them():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(pet=0.01, extdp=1.5)], simulate_et=True, unsat_etwc=True)
    assert "extwc" in str(caught.value)
    with pytest.raises(ModflowError) as caught:
        _place([_statement(pet=0.01, extdp=1.5, extwc=0.2)], simulate_et=True, unsat_etae=True)
    assert "ha, hroot, rootact" in str(caught.value)
    _, perioddata = _place([_statement(pet=0.01, extdp=1.5, extwc=0.2, ha=-1.0, hroot=-5.0, rootact=0.5)],
                           simulate_et=True, unsat_etwc=True, unsat_etae=True)
    assert perioddata == {0: [[0, 0.02, 0.01, 1.5, 0.2, -1.0, -5.0, 0.5]]}


def test_an_et_value_no_stated_option_reads_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(pet=0.01, extdp=1.5, extwc=0.2)], simulate_et=True)
    assert "extwc" in str(caught.value)


# ---- Part B: the composite's statement -------------------------------------

_NAMES = list(_VALUES)


def _fill(stated: dict, **produced):
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": _NLAY, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": _BOTM, "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 1, "STO_ICONVERT": 1,
        "STO_SS": 1.0e-5, "STO_SY": 0.2, "IC_STRT": 5.0, "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[10.0, 2, 1.0]], "UZF_NTRAILWAVES": 7, "UZF_NWAVESETS": 40, **stated})


@pytest.mark.parametrize("argument", _NAMES)
def test_a_value_left_unstated_refuses_by_name(argument):
    for dropped in (True, False):
        body = {"geometry": "where", **_VALUES}
        if dropped:
            body.pop(argument)
        else:
            body[argument] = None
        with pytest.raises(ModflowError) as caught:
            _fill({"UnsaturatedZone": body}, where=_CENTRE)
        assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"
        assert "UnsaturatedZone" in str(caught.value) and argument in str(caught.value)


@pytest.mark.parametrize("argument", _NAMES)
def test_a_value_that_is_neither_a_number_nor_a_raster_refuses(argument):
    for held in ("0.5", True):
        with pytest.raises(ModflowError) as caught:
            _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES, argument: held}}, where=_CENTRE)
        assert argument in str(caught.value)


def test_an_argument_the_composite_does_not_take_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES, "depth": 3.0}}, where=_CENTRE)
    assert "depth" in str(caught.value)


def test_a_geometry_reading_nothing_states_nothing():
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES}}, where=None)
    assert "UZF_PACKAGEDATA" not in sheet.filled


def test_a_list_of_statements_is_one_package_s_zones():
    sheet = _fill({"UnsaturatedZone": [{"geometry": "a", **_VALUES}, {"geometry": "b", **_VALUES}]},
                  a=_CENTRE, b=_box(1.0, 1.0, 9.0, 9.0))
    assert len(sheet.filled["UZF_PACKAGEDATA"].value["zones"]) == 2


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


def test_zones_write_the_uzf_package_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES}, "UZF_PRINT_FLOWS": True}, where=_MIDDLE_ROW)
    rundir = _rundir(sheet, mesh, tmp_path)

    written = serialize(sheet, rundir, mesh)["written"]

    assert "gwf.uzf" in written
    text = (rundir / "gwf.uzf").read_text()
    assert "NUZFCELLS  9" in text and "NTRAILWAVES  7" in text
    assert "  1  1 4  1  4       0.10000000" in text
    assert "  9  3 6  0  0       0.10000000" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_et_stated_for_simulate_et_writes_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES, "pet": 0.01, "extdp": 1.5},
                   "UZF_SIMULATE_ET": True}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    serialize(sheet, rundir, mesh)
    assert "SIMULATE_ET" in (rundir / "gwf.uzf").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


@pytest.mark.parametrize("keyword,value", [
    ("UZF_NUZFCELLS", 2),
    ("UZF_PERIODDATA", {0: [[0, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]})])
def test_stating_the_uzf_keywords_beside_the_polygon_refuses(staged_beside, tmp_path, keyword, value):
    mesh = _mesh(tmp_path)
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES}, keyword: value}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert keyword in str(caught.value)


def test_recharge_on_a_zone_s_column_refuses_naming_both(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES},
                   "Recharge": {"geometry": "where", "layer": 1, "rate": 1.0e-4}}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "UnsaturatedZone" in str(caught.value) and "Recharge" in str(caught.value)
    assert "cell 4" in str(caught.value)


def test_recharge_beside_a_zone_on_other_columns_is_allowed(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES},
                   "Recharge": {"geometry": "elsewhere", "layer": 1, "rate": 1.0e-4}},
                  where=_CENTRE, elsewhere=_box(1.0, 1.0, 9.0, 9.0))
    rundir = _rundir(sheet, mesh, tmp_path)
    serialize(sheet, rundir, mesh)
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_recharge_stated_by_its_own_keywords_on_a_zone_s_column_refuses(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_VALUES},
                   "RCH_MAXBOUND": 1, "RCH_STRESS_PERIOD_DATA": {0: [[(0, 4), 1.0e-4]]}}, where=_CENTRE)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "Recharge" in str(caught.value) and "cell 4" in str(caught.value)


# ---- Part A: UZF by the module's own keywords ------------------------------


def test_uzf_stated_by_its_own_keywords_writes_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    cell = [0.5, 0.1, 0.45, 0.105, 4.0]
    sheet = _fill({
        "UZF_NUZFCELLS": 3,
        "UZF_PACKAGEDATA": [[0, (0, 4), 1, 1, 0.1, *cell], [1, (1, 4), 0, 2, 0.001, *cell],
                            [2, (2, 4), 0, -1, 0.001, *cell]],
        "UZF_PERIODDATA": {0: [[0, 0.02, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]}})
    rundir = _rundir(sheet, mesh, tmp_path)

    assert "gwf.uzf" in serialize(sheet, rundir, mesh)["written"]
    text = (rundir / "gwf.uzf").read_text()
    assert "NUZFCELLS  3" in text and "  3  3 5  0  0" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}
