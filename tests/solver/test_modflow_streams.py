"""SFR: stated by the module's own keywords, and placed by the Streams composite from a line.

Offline except the validate cases, which run mf6's own --mode validate through the image.
The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the
south-west; lines are drawn on it in lon/lat and every length below is computed by hand.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_streams
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 3
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE)
          for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_VALUES = {"width": 5.0, "slope": 0.001, "bed_top": 9.0, "bed_thickness": 1.0,
           "bed_conductivity": 1.0e-5, "manning": 0.03}
_DIMENSIONS = {"width": "L", "slope": "1", "bed_top": "L", "bed_thickness": "L",
               "bed_conductivity": "L/T", "manning": "1"}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


_MAIN = _line((2.0, 15.0), (28.0, 15.0))
_TRIBUTARY = _line((15.0, 28.0), (15.0, 13.0))


def _statement(geometry, **over):
    return {"geometry": geometry, "values": dict(_VALUES), "dimensions": dict(_DIMENSIONS),
            **over}


def _place(statements, *, frame=("meters", "days"), active=None, nlay=1):
    return place_streams(statements, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                         nlay=nlay, frame=frame, active=active)[:2]


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


def _fill(stated: dict, **produced):
    cells = len(_CELLS)
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": 1, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * cells,
        "DISV_BOTM": [[0.0] * cells], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0,
        "IC_STRT": 5.0, "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[10.0, 1, 1.0]], **stated})


# ---- Part B: the line cut --------------------------------------------------


def test_a_straight_line_across_the_grid_is_three_reaches_of_hand_computed_length():
    packagedata, connectiondata = _place([_statement(_MAIN)])

    assert [(row[0], row[1], row[2], row[3]) for row in packagedata] \
        == [(0, 0, 3, pytest.approx(8.0)), (1, 0, 4, pytest.approx(10.0)),
            (2, 0, 5, pytest.approx(8.0))]
    assert connectiondata == [[0, -1], [1, 0, -2], [2, 1]]
    assert [row[10] for row in packagedata] == [1, 2, 1]
    assert packagedata[0][4:10] == [5.0, 0.001, 9.0, 1.0, 1.0e-5, 0.03]
    assert [row[11:] for row in packagedata] == [[1.0, 0]] * 3


def test_a_reach_s_length_is_on_the_model_s_length_unit():
    packagedata, _ = _place([_statement(_MAIN)], frame=("feet", "days"))
    assert [row[3] for row in packagedata] \
        == [pytest.approx(8.0 / 0.3048), pytest.approx(10.0 / 0.3048),
            pytest.approx(8.0 / 0.3048)]


def test_a_line_runs_downstream_in_the_order_it_is_digitized():
    packagedata, connectiondata = _place([_statement(_line((28.0, 15.0), (2.0, 15.0)))])
    assert [row[2] for row in packagedata] == [5, 4, 3]
    assert connectiondata == [[0, -1], [1, 0, -2], [2, 1]]


def test_a_line_ending_on_another_line_s_reach_joins_it():
    packagedata, connectiondata = _place([_statement(_MAIN), _statement(_TRIBUTARY)])

    # Upstream first: the main stem's first reach, the tributary's two, then the main stem on.
    assert [(row[2], round(row[3], 6)) for row in packagedata] \
        == [(3, 8.0), (7, 8.0), (4, 7.0), (4, 10.0), (5, 8.0)]
    assert connectiondata == [[0, -3], [1, -2], [2, 1, -3], [3, 0, 2, -4], [4, 3]]
    assert [row[10] for row in packagedata] == [1, 1, 2, 3, 1]


def test_two_lines_meeting_where_neither_passes_do_not_join_each_other():
    _, connectiondata = _place([_statement(_line((2.0, 5.0), (8.0, 5.0))),
                                _statement(_line((8.0, 2.0), (5.0, 2.0)))])
    assert connectiondata == [[0], [1]]


def test_a_reach_lies_on_the_cell_s_top_solved_layer_unless_one_is_stated():
    active = [[1] * 9, [1] * 9]
    active[0][4] = 0
    packagedata, _ = _place([_statement(_MAIN)], active=active, nlay=2)
    assert [row[1] for row in packagedata] == [0, 1, 0]
    stated, _ = _place([_statement(_MAIN, layer=2)], active=active, nlay=2)
    assert [row[1] for row in stated] == [1, 1, 1]


def test_a_cell_solved_on_no_layer_refuses_naming_it():
    active = [[1] * 9]
    active[0][4] = 0
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_MAIN)], active=active)
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"
    assert "cell 4" in str(caught.value)


def test_a_layer_the_grid_does_not_have_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_MAIN, layer=2)])
    assert caught.value.error_code == "MODFLOW_STRESS_LAYER"


def test_a_line_off_the_mesh_refuses_as_the_other_stresses_do():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_line((500.0, 500.0), (600.0, 600.0)))])
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"


def test_the_part_of_a_line_off_the_mesh_is_left_out():
    packagedata, _ = _place([_statement(_line((2.0, 15.0), (40.0, 15.0)))])
    assert [(row[2], round(row[3], 6)) for row in packagedata] == [(3, 8.0), (4, 10.0), (5, 10.0)]


def test_a_line_leaving_the_mesh_and_returning_refuses_naming_the_cells():
    holed = [ring for cell, ring in enumerate(_CELLS) if cell != 4]
    with pytest.raises(ModflowError) as caught:
        place_streams([_statement(_MAIN)], points=_POINTS, cells=holed, utm_epsg=32617,
                      nlay=1, frame=("meters", "days"))
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"
    assert "leaves the mesh between cells 3 and 4" in str(caught.value)


def test_a_line_counted_in_two_cells_refuses():
    with pytest.raises(ModflowError) as caught:
        place_streams([_statement(_MAIN)], points=_POINTS, cells=[*_CELLS, _CELLS[4]],
                      utm_epsg=32617, nlay=1, frame=("meters", "days"))
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"
    assert "edge" in str(caught.value)


def test_a_polygon_is_no_stream():
    ring = {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in
            [(1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 1.0)]]]}
    with pytest.raises(ModflowError) as caught:
        _place([_statement(ring)])
    assert "Polygon" in str(caught.value)


def test_a_length_unit_the_model_does_not_state_refuses():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_MAIN)], frame=(None, "days"))
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"


def test_lines_joining_in_a_loop_refuse():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_line((5.0, 5.0), (15.0, 5.0))),
                _statement(_line((15.0, 6.0), (5.0, 6.0)))])
    assert "loop" in str(caught.value)


def test_a_stated_raster_width_is_read_at_each_reach_cell(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "width.tif"
    with rasterio.open(path, "w", driver="GTiff", height=3, width=3, count=1,
                       dtype="float32", crs="EPSG:32617",
                       transform=from_origin(0.0, 30.0, 10.0, 10.0)) as dst:
        dst.write(np.arange(9, dtype="float32").reshape(3, 3), 1)
    values = {**_VALUES, "width": {"uri": str(path), "units": "m"}}
    packagedata, _ = _place([_statement(_MAIN, values=values)])
    # The raster's first row is the north: row 1 of the lattice reads values 3, 4, 5.
    assert [row[4] for row in packagedata] == [3.0, 4.0, 5.0]


# ---- Part B: the composite's statement ------------------------------------


@pytest.mark.parametrize("argument", ["width", "slope", "bed_top", "bed_thickness",
                                      "bed_conductivity", "manning"])
def test_a_value_left_unstated_refuses_by_name(argument):
    for dropped in (True, False):
        body = {"geometry": "where", **_VALUES}
        if dropped:
            body.pop(argument)
        else:
            body[argument] = None
        with pytest.raises(ModflowError) as caught:
            _fill({"Streams": body}, where=_MAIN)
        assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"
        assert "Streams" in str(caught.value) and argument in str(caught.value)


def test_a_manning_that_is_no_number_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"Streams": {"geometry": "where", **_VALUES, "manning": "n_raster"}},
              where=_MAIN, n_raster={"uri": "x.tif", "units": "1"})
    assert "manning" in str(caught.value)


@pytest.mark.parametrize("layer", [0, -1, 1.5, "1", True])
def test_a_layer_that_is_not_an_integer_from_one_refuses(layer):
    with pytest.raises(ModflowError):
        _fill({"Streams": {"geometry": "where", **_VALUES, "layer": layer}}, where=_MAIN)


def test_a_geometry_reading_nothing_states_nothing():
    sheet = _fill({"Streams": {"geometry": "where", **_VALUES}}, where=None)
    assert "SFR_PACKAGEDATA" not in sheet.filled


def test_a_list_of_statements_is_one_package_s_lines():
    sheet = _fill({"Streams": [{"geometry": "main", **_VALUES},
                               {"geometry": "branch", **_VALUES}]},
                  main=_MAIN, branch=_TRIBUTARY)
    assert len(sheet.filled["SFR_PACKAGEDATA"].value["streams"]) == 2
    assert GWF.COMPOSITES["Streams"].names(
        [{"geometry": "main"}, {"geometry": "branch"}]) == ["main", "branch"]


# ---- Part B: the write -----------------------------------------------------


def _rundir(sheet, mesh, tmp_path):
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    return rundir


def test_streams_write_the_sfr_package_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Streams": [{"geometry": "main", **_VALUES},
                               {"geometry": "branch", **_VALUES}],
                   "SFR_SAVE_FLOWS": True}, main=_MAIN, branch=_TRIBUTARY)
    rundir = _rundir(sheet, mesh, tmp_path)

    written = serialize(sheet, rundir, mesh)["written"]

    assert "gwf.sfr" in written
    text = (rundir / "gwf.sfr").read_text()
    assert "NREACHES  5" in text
    assert text.count("\n") > 10
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_stating_the_reach_keywords_beside_the_lines_refuses(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Streams": {"geometry": "main", **_VALUES}, "SFR_NREACHES": 3},
                  main=_MAIN)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "SFR_NREACHES" in str(caught.value)


# ---- Part A: SFR by the module's own keywords -------------------------------

_REACHES = [  # ifno, cell, rlen, rwid, rgrd, rtp, rbth, rhk, man, ncon, ustrf, ndv
    [0, 3, 8.0, 5.0, 0.001, 9.0, 1.0, 1.0e-5, 0.03, 1, 1.0, 0],
    [1, 4, 10.0, 5.0, 0.001, 8.0, 1.0, 1.0e-5, 0.03, 2, 1.0, 0],
    [2, 5, 8.0, 5.0, 0.001, 7.0, 1.0, 1.0e-5, 0.03, 1, 1.0, 0],
]


def test_sfr_stated_by_its_own_keywords_writes_and_mf6_validates(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({
        "SFR_NREACHES": 3, "SFR_SAVE_FLOWS": True, "SFR_STAGE_FILERECORD": "gwf.sfr.stage.bin",
        "SFR_PACKAGEDATA": [[row[0], 0, *row[1:]] for row in _REACHES],
        "SFR_CONNECTIONDATA": [[0, -1], [1, 0, -2], [2, 1]],
        "SFR_PERIODDATA": {0: [[0, "inflow", 1.0]]}})
    rundir = _rundir(sheet, mesh, tmp_path)

    assert "gwf.sfr" in serialize(sheet, rundir, mesh)["written"]
    assert "STAGE  FILEOUT  gwf.sfr.stage.bin" in (rundir / "gwf.sfr").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


# ---- the period-keyed logical a keyword takes -------------------------------


def test_a_logical_takes_a_period_keyed_statement():
    slot = GWF.slot("STO_STEADY_STATE")
    assert slot.check({0: True}) == {0: True}
    assert slot.check({0: True, 1: False}) == {0: True, 1: False}


@pytest.mark.parametrize("value", [{"a": True}, {0: 1}, {0: "true"}])
def test_a_period_keyed_logical_that_is_not_one_refuses(value):
    with pytest.raises(SlotRefused):
        GWF.slot("STO_STEADY_STATE").check(value)
