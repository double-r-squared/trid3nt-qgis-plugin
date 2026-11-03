"""RIV over a river polygon: a cell's conductance is the stated leakance times the river's area in it, its stage a surface's flat water.

Offline except the validate case, which runs mf6's own --mode validate through the image.
The mesh is the 3x3 lattice of 10 m squares of the lakes suite (UTM 32617, numbered row by row from the south-west);
every area and count below is computed by hand.
"""

from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from tests.solver.test_modflow_lakes import _CELLS, _N, _POINTS, _fill, _mesh, _rundir, staged_beside  # noqa: F401
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place, place_river_areas, river_area
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError

_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_FRAME = ("meters", "days")
_DIMENSIONS = {"stage": "L", "conductance": "L2/T", "rbot": "L"}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


_MIDDLE_ROW = _box(1.0, 11.0, 29.0, 19.0)


def _statement(geometry, **over):
    values = {"stage": 7.5, "conductance": {"leakance": 0.1}, "rbot": 2.0}
    return {"composite": "Rivers", "geometry": geometry, "layer": 1, "values": {**values, **over}, "dimensions": _DIMENSIONS}


def _place(statement, *, active=None):
    return place_river_areas(statement, points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=1, frame=_FRAME, active=active)


def _fine(tmp_path, rows):
    """A 6x6 raster of 5 m pixels over the lattice, ``rows`` north to south."""
    path = tmp_path / "fine.tif"
    with rasterio.open(path, "w", driver="GTiff", height=6, width=6, count=1, dtype="float32", crs="EPSG:32617",
                       transform=from_origin(0.0, 30.0, 5.0, 5.0)) as dst:
        dst.write(np.asarray(rows, dtype="float32"), 1)
    return {"uri": str(path), "units": "m"}


def _levels(tmp_path, middle_north, middle_south):
    """Rows 2 and 3 (y 15-20 and 10-15) are the river's pixels; the rest is a bank at 9 m."""
    grid = np.full((6, 6), 9.0)
    grid[2], grid[3] = middle_north, middle_south
    return _fine(tmp_path, grid)


def test_each_cell_s_conductance_is_the_leakance_times_the_river_s_area_in_it():
    rows, counts = _place(_statement(_MIDDLE_ROW))

    # the river spans x 1-29 and y 11-19: 9 x 8, 10 x 8 and 9 x 8 square metres in cells 3, 4 and 5
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ((0, 3), 7.5, pytest.approx(7.2), 2.0), ((0, 4), 7.5, pytest.approx(8.0), 2.0),
        ((0, 5), 7.5, pytest.approx(7.2), 2.0)]
    assert counts["cells"] == 3 and counts["polygons"] == 1 and counts["area"] == 224


def test_a_river_across_a_cell_edge_gives_each_cell_its_own_share_of_the_area():
    rows, _ = _place(_statement(_box(5.0, 11.0, 15.0, 19.0)))

    assert [(row[0][1], row[2]) for row in rows] == [(3, pytest.approx(4.0)), (4, pytest.approx(4.0))]


def test_a_river_smaller_than_a_cell_holds_no_cell_centre_and_is_still_that_cell_s_river():
    rows, _ = _place(_statement(_box(11.0, 11.0, 14.0, 14.0)))

    assert [(row[0][1], row[2]) for row in rows] == [(4, pytest.approx(0.9))]


def test_two_polygons_in_one_cell_add_their_areas(tmp_path):
    both = {"type": "MultiPolygon", "coordinates": [_box(11.0, 11.0, 14.0, 14.0)["coordinates"],
                                                     _box(16.0, 16.0, 19.0, 19.0)["coordinates"]]}
    rows, counts = _place(_statement(both))

    assert [(row[0][1], row[2]) for row in rows] == [(4, pytest.approx(1.8))] and counts["polygons"] == 1


def test_a_stage_surface_is_each_cell_s_own_flat_level_and_a_split_cell_is_left_out(tmp_path):
    # cell 3 holds 7.5 m, cell 4 holds 8 m, cell 5 holds half 9 m and half 12 m: no elevation is shared by more than half
    surface = _levels(tmp_path, [7.5, 7.5, 8.0, 8.0, 9.0, 9.0], [7.5, 7.5, 8.0, 8.0, 12.0, 12.0])

    rows, counts = _place(_statement(_MIDDLE_ROW, stage={"surface": surface}))

    assert [(row[0][1], row[1]) for row in rows] == [(3, 7.5), (4, 8.0)]
    assert counts["cells"] == 2 and counts["no majority stage"] == 1


def test_a_cell_with_no_pixel_centre_inside_the_river_is_left_out_and_counted(tmp_path):
    surface = _levels(tmp_path, [7.5] * 6, [7.5] * 6)

    rows, counts = _place(_statement(_box(11.0, 11.0, 12.0, 12.0), stage={"surface": surface}))

    assert rows == [] and counts["no pixel centre"] == 1


def test_a_cell_the_run_does_not_solve_is_left_out():
    active = [[1, 1, 1, 1, 0, 1, 1, 1, 1]]

    rows, counts = _place(_statement(_MIDDLE_ROW), active=active)

    assert [row[0][1] for row in rows] == [3, 5] and counts["unsolved"] == 1


def test_a_field_stage_and_bottom_read_the_feature_the_cell_lies_in():
    two = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"stage": 7.0, "bed": 1.0}, "geometry": _box(1.0, 11.0, 9.0, 19.0)},
        {"type": "Feature", "properties": {"stage": 6.0, "bed": 0.5}, "geometry": _box(21.0, 11.0, 29.0, 19.0)}]}

    rows, _ = _place(_statement(two, stage={"field": "stage"}, rbot={"field": "bed"}))

    assert [(row[0][1], row[1], row[3]) for row in rows] == [(3, 7.0, 1.0), (5, 6.0, 0.5)]


@pytest.mark.parametrize("conductance", [{"leakance": 0}, {"leakance": -0.1}, {"leakance": None},
                                          {"leakance": 0.1, "field": "x"}, {"leakance": "0.1"}])
def test_a_leakance_that_is_no_positive_number_refuses(conductance):
    with pytest.raises(ModflowError) as caught:
        _place(_statement(_MIDDLE_ROW, conductance=conductance))
    assert "leakance" in str(caught.value)


def test_a_river_over_a_line_refuses():
    line = {"type": "LineString", "coordinates": [list(_TO_LONLAT(2.0, 15.0)), list(_TO_LONLAT(28.0, 15.0))]}
    with pytest.raises(ModflowError) as caught:
        _place(_statement(line))
    assert "polygons" in str(caught.value)


def test_the_cell_row_placement_leaves_the_area_forms_to_the_river_placement():
    assert river_area(_statement(_MIDDLE_ROW))
    with pytest.raises(ModflowError):
        place(_statement(_MIDDLE_ROW), points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=1, frame=_FRAME)
    with pytest.raises(ModflowError) as caught:
        place(_statement(_MIDDLE_ROW, conductance=5.0, stage={"surface": {"uri": "x.tif"}}),
              points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=1, frame=_FRAME)
    assert "river area" in str(caught.value)


def test_a_run_input_named_in_the_forms_is_read_off_the_run(tmp_path):
    surface = _levels(tmp_path, [7.5] * 6, [7.5] * 6)
    sheet = _fill({"Rivers": {"geometry": "where", "layer": 1, "stage": {"surface": "bed"},
                              "conductance": {"leakance": "leak"}, "rbot": 2.0}},
                  where=_MIDDLE_ROW, bed=surface, leak=0.1)

    statement = sheet.filled["RIV_STRESS_PERIOD_DATA"].value

    assert statement["values"]["stage"] == {"surface": surface}
    assert statement["values"]["conductance"] == {"leakance": 0.1} and river_area(statement)


def _drain_cells(rundir):
    rows = (rundir / "gwf.drn").read_text().split("BEGIN period", 1)[1].split("END period", 1)[0].split("\n")[1:]
    return [row.split()[1] for row in rows if row.strip()]


def test_a_drain_line_inside_the_river_writes_no_drain_where_the_river_is_placed_and_mf6_validates(
        staged_beside, tmp_path, monkeypatch):  # noqa: F811
    notes = []
    monkeypatch.setattr("trid3nt_server.inputs.run_notes.journal_note", notes.append)
    mesh = _mesh(tmp_path)
    surface = _levels(tmp_path, [7.5, 7.5, 8.0, 8.0, 9.0, 9.0], [7.5, 7.5, 8.0, 8.0, 12.0, 12.0])
    line = {"type": "LineString", "coordinates": [list(_TO_LONLAT(1.0, 15.0)), list(_TO_LONLAT(29.0, 15.0))]}
    sheet = _fill({"Rivers": {"geometry": "where", "layer": 1, "stage": {"surface": "bed"},
                              "conductance": {"leakance": "leak"}, "rbot": 2.0},
                   "Drains": {"geometry": "line", "layer": 1, "elevation": 9.0, "conductance": 5.0}},
                  where=_MIDDLE_ROW, line=line, bed=surface, leak=0.1)
    rundir = _rundir(sheet, mesh, tmp_path)

    written = serialize(sheet, rundir, mesh)["written"]

    assert "gwf.riv" in written and "gwf.drn" in written
    riv = (rundir / "gwf.riv").read_text().split("BEGIN period", 1)[1].split("END period", 1)[0].split("\n")[1:]
    assert [(row.split()[1], *map(float, row.split()[2:5])) for row in riv if row.strip()] == [
        ("4", 7.5, pytest.approx(7.2), 2.0), ("5", 8.0, pytest.approx(8.0), 2.0)]
    # cells 3 and 4 (1-based 4 and 5) are the river; cell 5 showed no majority stage and keeps its drain
    assert _drain_cells(rundir) == ["6"]
    assert len(notes) == 1 and "2 river cell(s)" in notes[0] and "1 no majority stage" in notes[0]
    assert "2 drain row(s) in river cells are dropped" in notes[0]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_river_no_cell_of_which_shows_a_stage_writes_no_riv_package(staged_beside, tmp_path):  # noqa: F811
    mesh = _mesh(tmp_path)
    ragged = _levels(tmp_path, [7.5, 9.0, 8.0, 12.0, 9.0, 10.0], [7.0, 7.5, 8.5, 8.0, 12.0, 11.0])
    sheet = _fill({"Rivers": {"geometry": "where", "layer": 1, "stage": {"surface": "bed"},
                              "conductance": {"leakance": 0.1}, "rbot": 2.0}}, where=_MIDDLE_ROW, bed=ragged)
    rundir = _rundir(sheet, mesh, tmp_path)

    assert "gwf.riv" not in serialize(sheet, rundir, mesh)["written"]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_leakance_left_unstated_refuses_by_name_where_a_river_is_found():
    with pytest.raises(ModflowError) as caught:
        _fill({"Rivers": {"geometry": "where", "layer": 1, "stage": 7.5, "conductance": {"leakance": "leak"}, "rbot": 2.0}},
              where=_MIDDLE_ROW, leak=None)
    assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED" and "Rivers states no leakance" in str(caught.value)


def test_no_river_found_states_nothing_and_asks_for_no_leakance():
    sheet = _fill({"Rivers": {"geometry": "where", "layer": 1, "stage": {"surface": "bed"},
                              "conductance": {"leakance": "leak"}, "rbot": "bottom"}}, where=None, bed=None, leak=None)

    assert "RIV_STRESS_PERIOD_DATA" not in sheet.filled
