"""Model exchanges and the unstructured grid: a LocalGridRefinement child joined to its parent, WaterMover across the exchange, a DISU stated whole.

Offline over a 4 x 4 lattice of 10 m squares in UTM 32617, numbered row by row from the south-west, two layers, the central 2 x 2 cells
refined twice over. The validate cases run mf6's own --mode validate through the image.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow import workflow as WF
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring import refinement as RF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.staged_check import check_staged_run
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.modflow.modules.outputs import Solved
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 4
_N = _SIDE * _SIDE
_STEP = 10.0
_POINTS = [(x * _STEP, y * _STEP) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE) for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_STREAM = {"width": 5.0, "slope": 0.001, "bed_top": 9.0, "bed_thickness": 1.0, "bed_conductivity": 1.0e-5,
           "manning": 0.03}


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


# The central 2 x 2 cells: parent cells 5, 6, 9 and 10, rows 1 and 2 from the south.
_CENTRE = _box(10.0, 10.0, 30.0, 30.0)
_GWF = {"NAM_SAVE_FLOWS": True, "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_SAVERECORD": [("HEAD", "LAST")]}
_CHILD = {"NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0, "OC_HEAD_FILERECORD": "gwf-child.hds",
          "OC_SAVERECORD": [("HEAD", "LAST")]}
_IMS = {"IMS_OUTER_DVCLOSE": 1.0e-6, "IMS_OUTER_MAXIMUM": 100, "IMS_INNER_MAXIMUM": 100, "IMS_INNER_DVCLOSE": 1.0e-6,
        "IMS_RCLOSERECORD": "1e-06"}


def _fill(stated: dict, produced: dict | None = None):
    return fill(GWF, produced={"centre": _CENTRE, **(produced or {})}, **{
        "DISV_NLAY": 2, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N, "DISV_BOTM": [[5.0] * _N, [0.0] * _N],
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 1, 1.0]], **_GWF, **_IMS, **stated})


def _refining(**over):
    return {"geometry": "centre", "refinement": 2, "slots": dict(_CHILD), **over}


def _deck(refinement=None, produced=None, **stated):
    return _fill({"LocalGridRefinement": _refining() if refinement is None else refinement, **stated}, produced)


def _mesh(tmp_path: Path, cells=_CELLS, points=_POINTS) -> dict:
    rows = ["MESH2D"]
    tag = "E4Q" if len(cells[0]) == 4 else "E3T"
    for cell, ring in enumerate(cells, start=1):
        rows.append(f"{tag} {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(points, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d", display_uri=str(path), crs_authid="EPSG:32617",
        has_bathymetry=True, node_count=len(points), element_count=len(cells),
        bbox=(0.0, 0.0, 40.0, 40.0), utm_epsg=32617)
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


def _written(sheet, tmp_path):
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    return rundir, serialize(sheet, rundir, mesh)["written"], out


def _text(rundir, name):
    return " ".join((rundir / name).read_text().split())


def _rows(rundir, name, block):
    """The data lines of one block of one file, each as its tokens."""
    rows, on = [], False
    for line in (rundir / name).read_text().splitlines():
        words = line.split()
        if words[:2] == ["BEGIN", block]:
            on = True
        elif words[:1] == ["END"]:
            on = False
        elif on and words:
            rows.append(words)
    return rows


def _zeros(path):
    """The count of inactive cells the staged DISV's idomain holds."""
    body = path.read_text().split("idomain")[1].split("END")[0]
    return sum(1 for token in body.split() if token == "0")


def _idomain(path):
    """The staged DISV's idomain, one list of cell values per layer."""
    layers: list[list[int]] = []
    for line in path.read_text().split("idomain")[1].split("END")[0].splitlines():
        if "INTERNAL" in line:
            layers.append([])
        elif layers and line.split():
            layers[-1] += [int(token) for token in line.split()]
    return layers


def _refused(sheet, tmp_path, error=ModflowError):
    with pytest.raises(error) as caught:
        _written(sheet, tmp_path)
    return caught.value


# --- the statement -------------------------------------------------------------------------------


def test_a_refinement_is_a_gwf_body_named_beside_the_carrier_and_keyed_by_its_name():
    sheet = _deck()

    body = sheet.files["gwf-child"]

    assert [item["name"] for item in sheet.coupled] == ["gwf-child"]
    assert body["module"] == "gwf" and body["refine"]["refinement"] == 2 and body["refine"]["layers"] is None


def test_the_input_names_a_refinement_reads_include_those_its_own_statements_read():
    class Steering(GWF):
        LocalGridRefinement = _refining(slots={"Wells": {"geometry": "well", "layer": 1, "rate": -1.0}})

    assert Steering.named() == ["centre", "well"]


def test_a_refinement_with_no_geometry_states_nothing():
    assert _deck(_refining(geometry="nowhere")).coupled == ()


@pytest.mark.parametrize("over, said", [
    ({"refinement": None}, "state it"),
    ({"refinement": 1}, "from 2"),
    ({"refinement": True}, "from 2"),
    ({"name": "gwt"}, "no other model"),
    ({"name": "a name"}, "1 to 16"),
    ({"name": "x" * 17}, "1 to 16"),
    ({"layers": "3"}, "list of integers"),
    ({"exchange": {"mover": True}}, "xt3d"),
    ({"surprise": 1}, "none of them"),
])
def test_a_refinement_statement_that_is_wrong_refuses_by_what_it_states(over, said):
    with pytest.raises(SlotRefused) as caught:
        _deck(_refining(**over))

    assert said in str(caught.value)


def test_two_refinements_take_positional_names_and_never_the_same_name():
    both = _deck([_refining(), _refining(geometry="centre")])

    assert [item["name"] for item in both.coupled] == ["gwf-child", "gwf-child2"]
    with pytest.raises(SlotRefused, match="no other model"):
        _deck([_refining(name="same"), _refining(name="same")])


def test_a_refinement_cannot_state_the_simulation_s_clock_or_grid():
    with pytest.raises(SlotRefused, match="the simulation's own"):
        _deck(_refining(slots={**_CHILD, "DISV_NLAY": 3}))
    with pytest.raises(SlotRefused, match="the simulation's own"):
        _deck(_refining(slots={**_CHILD, "Aquifer": {"thickness": 5.0, "water_table": 8.0, "conductivity": 1.0}}))


# --- the parent as a lattice ---------------------------------------------------------------------


def test_the_mesh_is_read_as_a_lattice_with_row_zero_the_northern_row():
    grid = RF.lattice(_POINTS, _CELLS)

    assert (grid.nrow, grid.ncol) == (4, 4)
    assert list(grid.delr) == [10.0] * 4 and list(grid.delc) == [10.0] * 4
    assert (grid.xll, grid.yll) == (0.0, 0.0)
    assert list(grid.cell_at[0]) == [12, 13, 14, 15] and list(grid.cell_at[3]) == [0, 1, 2, 3]


def test_a_mesh_that_is_not_a_lattice_refuses_naming_what_the_refinement_needs():
    triangles = [(0, 1, 6), (0, 6, 5)]
    with pytest.raises(ModflowError, match="not four-cornered") as caught:
        RF.lattice(_POINTS, triangles)
    sheared = [list(ring) for ring in _CELLS]
    points = [(x + 0.4 * y, y) for x, y in _POINTS]
    with pytest.raises(ModflowError, match="rectilinear parent; this mesh has 16 cells"):
        RF.lattice(points, sheared)

    assert caught.value.error_code == "MODFLOW_REFINE_PARENT"


# --- the parent and the child as written ---------------------------------------------------------


def test_the_parent_solves_no_cell_the_child_replaces_in_the_layers_it_refines(tmp_path, staged_beside):
    sheet = _deck()

    out = MF._mesh_files(sheet, _mesh(tmp_path))

    assert _zeros(Path(out["disv"])) == 4 * 2


def test_the_cells_a_polygon_holds_are_the_ones_the_parent_gives_up_by_their_rows_from_the_south(tmp_path, staged_beside):
    sheet = _fill({"LocalGridRefinement": _refining(geometry="strip")}, {"strip": _box(10.0, 10.0, 30.0, 20.0)})

    out = MF._mesh_files(sheet, _mesh(tmp_path))
    layers = _idomain(Path(out["disv"]))

    # Row 1 from the south, columns 1 and 2: cells 5 and 6.
    assert [[cell for cell, value in enumerate(layer) if value == 0] for layer in layers] == [[5, 6], [5, 6]]


def test_layers_the_child_does_not_refine_stay_solved_in_the_parent(tmp_path, staged_beside):
    sheet = _deck(_refining(layers=[2, 0]))

    out = MF._mesh_files(sheet, _mesh(tmp_path))

    assert _zeros(Path(out["disv"])) == 4


def test_a_run_writes_both_models_one_solution_and_the_exchange(tmp_path, staged_beside):
    rundir, written, _out = _written(_deck(), tmp_path)

    assert {"gwf.nam", "gwf-child.nam", "gwf-gwf-child.exg", "gwf.ims", "mfsim.nam", "gwf-child.disv"} <= set(written) | {"gwf-child.disv"}
    assert (rundir / "gwf-child.disv").is_file() and "gwf-child.ims" not in written
    nam = _text(rundir, "mfsim.nam")
    assert "gwf6 gwf.nam gwf" in nam and "gwf6 gwf-child.nam gwf-child" in nam
    assert "GWF6-GWF6 gwf-gwf-child.exg gwf gwf-child" in nam
    assert "ims6 gwf.ims gwf gwf-child" in nam
    assert "DISV6 gwf-child.disv" in _text(rundir, "gwf-child.nam")


def test_the_child_grid_is_the_refined_block_with_the_parent_s_top_and_bottoms(tmp_path, staged_beside):
    rundir, _written_, _out = _written(_deck(), tmp_path)

    dims = _text(rundir, "gwf-child.disv")

    assert "NLAY 2 NCPL 16" in dims and "NVERT 25" in dims
    xs = sorted({float(row[1]) for row in _rows(rundir, "gwf-child.disv", "vertices")})
    ys = sorted({float(row[2]) for row in _rows(rundir, "gwf-child.disv", "vertices")})
    assert xs == [10.0, 15.0, 20.0, 25.0, 30.0] and ys == xs


def test_the_exchange_cells_are_the_ones_lgr_lists_numbered_as_each_grid_numbers_its_cells(tmp_path, staged_beside):
    from flopy.utils.lgrutil import Lgr

    rundir, _written_, _out = _written(_deck(), tmp_path)

    # Lgr counts rows from the north; the mesh numbers cells from the south-west.
    mask = np.ones((2, _SIDE, _SIDE), dtype=int)
    mask[:, 1:3, 1:3] = 0
    lgr = Lgr(2, _SIDE, _SIDE, [_STEP] * _SIDE, [_STEP] * _SIDE, np.full((_SIDE, _SIDE), 10.0),
              np.stack([np.full((_SIDE, _SIDE), 5.0), np.full((_SIDE, _SIDE), 0.0)]), mask, ncpp=2, ncppl=[1, 1],
              xllp=0.0, yllp=0.0)
    expected = [((kp, (_SIDE - 1 - ip) * _SIDE + jp), (kc, ic * 4 + jc), ihc)
                for (kp, ip, jp), (kc, ic, jc), ihc, *_ in lgr.get_exchange_data()]
    written = [((int(a) - 1, int(b) - 1), (int(c) - 1, int(d) - 1), int(e))
               for a, b, c, d, e, *_ in _rows(rundir, "gwf-gwf-child.exg", "exchangedata")]

    assert len(written) == len(expected) > 0
    assert written == [((k, p), (kc, c), ihc) for (k, p), (kc, c), ihc in expected]
    assert {p for (_k, p), _c, _i in written} <= {1, 2, 4, 7, 8, 11, 13, 14} | {0, 3, 12, 15, 5 - 5}


def test_a_statement_in_the_child_is_placed_on_the_child_s_cells(tmp_path, staged_beside):
    sheet = _deck(_refining(slots={**_CHILD, "Wells": {"geometry": "well", "layer": 1, "rate": -1.0}}),
                  produced={"well": _point(12.0, 12.0)})
    rundir, written, _out = _written(sheet, tmp_path)

    # The well lies in the south-west cell of the 4 x 4 child, whose cells count from the north-west: row 3, column 0.
    rows = _rows(rundir, "gwf-child.wel", "period")

    assert "gwf-child.wel" in written and [row[:2] for row in rows] == [["1", "13"]] and float(rows[0][2]) == -1.0


def test_a_child_that_states_its_own_solver_refuses(tmp_path, staged_beside):
    error = _refused(_deck(_refining(slots={**_CHILD, **_IMS})), tmp_path)

    assert error.error_code == "MODFLOW_KEYWORD_SHAPE" and "carrier's solution" in str(error)


def test_two_refinements_over_one_cell_refuse_and_so_does_one_over_a_cell_the_run_does_not_solve(tmp_path, staged_beside):
    error = _refused(_deck(_refining(), DISV_IDOMAIN=[[1] * 5 + [0] + [1] * 10, [1] * _N]), tmp_path)

    assert error.error_code == "MODFLOW_REFINE_PARENT" and "DISV_IDOMAIN 0" in str(error)


@pytest.mark.parametrize("layers", [[1, 1, 1], [0, 1], [2, 0, 1], [-1, 1]])
def test_layers_that_are_not_the_refined_layers_from_the_top_refuse(layers, tmp_path, staged_beside):
    error = _refused(_deck(_refining(layers=layers)), tmp_path)

    assert error.error_code == "MODFLOW_REFINE_LAYERS"


def test_a_refinement_on_a_mesh_of_triangles_refuses_at_the_lattice(tmp_path, staged_beside):
    mesh = _mesh(tmp_path, cells=[(0, 1, 6), (0, 6, 5)])
    sheet = _deck(DISV_TOP=10.0, DISV_BOTM=[5.0, 0.0])

    with pytest.raises(ModflowError, match="not four-cornered"):
        MF._mesh_files(sheet, mesh)


def test_the_xt3d_exchange_carries_the_angle_and_the_distance(tmp_path, staged_beside):
    rundir, _written_, _out = _written(_deck(_refining(exchange={"xt3d": True})), tmp_path)

    text = _text(rundir, "gwf-gwf-child.exg")

    assert "XT3D" in text and "ANGLDEGX CDIST" in text
    assert len(_rows(rundir, "gwf-gwf-child.exg", "exchangedata")[0]) == 8 + 2


def test_the_written_run_is_accepted_by_mf6_and_the_staged_check(tmp_path, staged_beside):
    sheet = _deck()
    rundir, written, out = _written(sheet, tmp_path)

    read = check_staged_run(rundir, steering="mfsim.nam", inputs=[{"gs_uri": out["disv"], "dest": "mesh.disv"}],
                            written_by_the_engine=written, duration_s=10.0 * 86400.0, cores=1)

    assert read["name_files"] == ["gwf-child.nam", "gwf.nam", "mfsim.nam"]
    assert validate_authored_simulation(rundir) == {"rc": 0}


# --- water across the exchange -------------------------------------------------------------------

_SOUTH = _line((2.0, 5.0), (38.0, 5.0))
_NORTH = _line((2.0, 35.0), (38.0, 35.0))
_INSIDE = _line((12.0, 20.0), (28.0, 20.0))


def _moving(*movers, **over):
    """Streams in the parent's north and south rows and in the child, with the movers stated beside."""
    child = {**_CHILD, "Streams": {"geometry": "inside", **_STREAM, "name": "inner"}}
    produced = dict(north=_NORTH, south=_SOUTH, inside=_INSIDE, head=_point(37.0, 35.0), first=_point(12.5, 20.0),
                    last=_point(27.5, 20.0), tail=_point(3.0, 5.0))
    return _deck(_refining(slots=child), produced, Streams=[{"geometry": "north", **_STREAM, "name": "upper"},
                                                            {"geometry": "south", **_STREAM, "name": "lower"}],
                 WaterMover=list(movers), **over)


def test_a_mover_with_its_ends_in_two_bodies_is_the_exchange_s_own_mvr(tmp_path, staged_beside):
    sheet = _moving(
        {"from": "upper", "from_at": "head", "to": "inner", "to_at": "first", "type": "FACTOR", "value": 1.0},
        {"from": "inner", "from_at": "last", "to": "lower", "to_at": "tail", "type": "FACTOR", "value": 0.5})
    rundir, written, _out = _written(sheet, tmp_path)

    mvr = _rows(rundir, "gwf-gwf-child.mvr", "period")
    names = _text(rundir, "gwf-gwf-child.mvr")

    assert "gwf-gwf-child.mvr" in written and "gwf.mvr" not in written and "gwf-child.mvr" not in written
    assert "MODELNAMES" in names and "gwf sfr_0" in names and "gwf-child sfr_0" in names
    assert [row[0] for row in mvr] == ["gwf", "gwf-child"] and [row[6] for row in mvr] == ["FACTOR", "FACTOR"]
    assert mvr[0][3] == "gwf-child" and mvr[1][3] == "gwf"
    assert "MOVER" in _text(rundir, "gwf.sfr") and "MOVER" in _text(rundir, "gwf-child.sfr")
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_mover_between_two_features_of_the_child_is_the_child_s_own_mvr_though_the_carrier_states_it(tmp_path, staged_beside):
    child = {**_CHILD, "Streams": [{"geometry": "inside", **_STREAM, "name": "inner"},
                                   {"geometry": "other", **_STREAM, "name": "outer"}]}
    sheet = _deck(_refining(slots=child), dict(north=_NORTH, inside=_line((12.0, 12.0), (28.0, 12.0)),
                                               other=_line((12.0, 28.0), (28.0, 28.0)), last=_point(27.5, 12.0),
                                               first=_point(12.5, 28.0)),
                  Streams={"geometry": "north", **_STREAM, "name": "upper"},
                  WaterMover={"from": "inner", "from_at": "last", "to": "outer", "to_at": "first", "type": "FACTOR",
                              "value": 1.0})
    rundir, written, _out = _written(sheet, tmp_path)

    assert "gwf-child.mvr" in written and "gwf.mvr" not in written
    assert "gwf-gwf-child.mvr" not in written


def test_a_mover_stated_in_the_child_resolves_its_ends_across_bodies(tmp_path, staged_beside):
    child = {**_CHILD, "Streams": {"geometry": "inside", **_STREAM, "name": "inner"},
             "WaterMover": {"from": "inner", "from_at": "last", "to": "lower", "to_at": "tail", "type": "FACTOR",
                            "value": 1.0}}
    sheet = _deck(_refining(slots=child), dict(south=_SOUTH, inside=_INSIDE, last=_point(27.5, 20.0), tail=_point(3.0, 5.0)),
                  Streams={"geometry": "south", **_STREAM, "name": "lower"})
    rundir, written, _out = _written(sheet, tmp_path)

    assert "gwf-gwf-child.mvr" in written and "gwf-child.mvr" not in written


def test_an_end_naming_no_feature_of_any_body_refuses_by_what_every_body_holds(tmp_path, staged_beside):
    sheet = _moving({"from": "nowhere", "to": "inner", "to_at": "first", "type": "FACTOR", "value": 1.0})

    error = _refused(sheet, tmp_path)

    assert "nowhere" in str(error) and "any model" in str(error)


def test_stating_the_mover_keywords_beside_a_cross_body_mover_refuses(tmp_path, staged_beside):
    sheet = _moving({"from": "upper", "from_at": "head", "to": "inner", "to_at": "first", "type": "FACTOR", "value": 1.0},
                    MVR_MAXMVR=1)

    error = _refused(sheet, tmp_path)

    assert error.error_code == "MODFLOW_KEYWORD_SHAPE" and "MVR_MAXMVR" in str(error)


# --- the refinement's transport -----------------------------------------------------------------

_GWT_CHILD = {"NAM_SAVE_FLOWS": True, "MST_POROSITY": 0.3, "ADV_SCHEME": "upstream", "IC_STRT": 0.0,
              "OC_CONCENTRATION_FILERECORD": "gwt-child.ucn", "OC_SAVERECORD": [("CONCENTRATION", "LAST")]}
_GWT = {**_GWT_CHILD, "OC_CONCENTRATION_FILERECORD": "gwt.ucn", **_IMS, "IMS_LINEAR_ACCELERATION": "BICGSTAB"}


def test_a_refinement_with_transport_writes_the_four_exchanges_and_shares_the_transport_solution(tmp_path, staged_beside):
    sheet = _deck(_refining(transport={"slots": dict(_GWT_CHILD)}), Transport={"slots": dict(_GWT)})
    rundir, written, _out = _written(sheet, tmp_path)

    nam = _text(rundir, "mfsim.nam")

    for line in ("GWF6-GWF6 gwf-gwf-child.exg gwf gwf-child", "GWF6-GWT6 gwf.gwfgwt gwf gwt",
                 "GWF6-GWT6 gwf-child-gwt-child.exg gwf-child gwt-child", "GWT6-GWT6 gwt-gwt-child.exg gwt gwt-child"):
        assert line in nam
    assert "ims6 gwf.ims gwf gwf-child" in nam and "ims6 gwt.ims gwt gwt-child" in nam
    assert "gwt-child.ims" not in written
    assert "ANGLDEGX CDIST" in _text(rundir, "gwt-gwt-child.exg") and "gwfmodelname1 gwf" in _text(rundir, "gwt-gwt-child.exg").lower().replace("gwfmodelname1  gwf", "gwfmodelname1 gwf")
    assert "DISV6 gwf-child.disv" in _text(rundir, "gwt-child.nam")
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_refinement_s_transport_without_a_transport_body_refuses(tmp_path, staged_beside):
    sheet = _deck(_refining(transport={"slots": dict(_GWT_CHILD)}))

    error = _refused(sheet, tmp_path)

    assert error.error_code == "MODFLOW_EXCHANGE_UNKNOWN" and "Transport" in str(error)


def test_a_refinement_s_transport_that_states_a_solver_refuses(tmp_path, staged_beside):
    sheet = _deck(_refining(transport={"slots": dict(_GWT)}), Transport={"slots": dict(_GWT)})

    error = _refused(sheet, tmp_path)

    assert error.error_code == "MODFLOW_KEYWORD_SHAPE" and "state the solver once" in str(error)


# --- the unstructured grid -----------------------------------------------------------------------

# Three cells in a row on one layer, 10 m each; the diagonal's ihc, cl12 and hwva are placeholders mf6 does not read.
_DISU = {
    "DISU_NODES": 3, "DISU_NJA": 7, "DISU_TOP": [10.0] * 3, "DISU_BOT": [0.0] * 3, "DISU_AREA": [100.0] * 3,
    "DISU_IAC": [2, 3, 2], "DISU_JA": [0, 1, 1, 0, 2, 2, 1], "DISU_IHC": [1, 1, 2, 1, 1, 3, 1],
    "DISU_CL12": [1.0, 5.0, 2.0, 5.0, 5.0, 3.0, 5.0], "DISU_HWVA": [1.0, 10.0, 2.0, 10.0, 10.0, 3.0, 10.0]}


def _unstructured(**over):
    stated = {"NAM_SAVE_FLOWS": True, "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
              "CHD_MAXBOUND": 2, "CHD_STRESS_PERIOD_DATA": {0: [[[0], 5.0], [[2], 4.0]]},
              "OC_HEAD_FILERECORD": "gwf.hds", "OC_SAVERECORD": [("HEAD", "LAST")],
              "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[1.0, 1, 1.0]], **_IMS, **_DISU}
    return fill(GWF, **{**stated, **over})


def test_the_dictionary_types_a_disu_s_connection_rows_as_arrays():
    assert {GWF.slot(f"DISU_{name}").type for name in ("JA", "IHC", "CL12", "HWVA", "ANGLDEGX", "IAC")} == {"ARRAY"}


def test_a_sheet_stating_a_disu_writes_it_as_the_model_s_grid_and_no_dis_v(tmp_path, staged_beside):
    sheet = _unstructured()
    mesh = _mesh(tmp_path)
    assert MF._mesh_files(sheet, mesh)["disv"] is None
    rundir = tmp_path / "run"
    rundir.mkdir()

    written = serialize(sheet, rundir, mesh)["written"]

    assert "gwf.disu" in written and "DISU6 gwf.disu" in _text(rundir, "gwf.nam") and "DISV6" not in _text(rundir, "gwf.nam")
    assert [row[0] for row in _rows(rundir, "gwf.chd", "period")] == ["1", "3"]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_node_number_past_the_disu_s_nodes_refuses_in_the_staged_check(tmp_path):
    sheet = _unstructured(CHD_STRESS_PERIOD_DATA={0: [[[0], 5.0], [[3], 4.0]]})
    rundir = tmp_path / "run"
    rundir.mkdir()
    written = serialize(sheet, rundir, None)["written"]

    with pytest.raises(ModflowError) as caught:
        check_staged_run(rundir, steering="mfsim.nam", inputs=[], written_by_the_engine=written, duration_s=86400.0, cores=1)

    assert caught.value.error_code == "MODFLOW_STAGED_CELLID_OUTSIDE" and "node 4" in str(caught.value)


def test_a_disu_beside_the_mesh_s_grid_or_a_refinement_refuses(tmp_path):
    for over in ({"DISV_NLAY": 1}, {"LocalGridRefinement": _refining()}):
        with pytest.raises(ModflowError) as caught:
            serialize(_unstructured(**over), tmp_path, None) if "LocalGridRefinement" not in over else \
                serialize(fill(GWF, produced={"centre": _CENTRE}, **{**dict(_unstructured().stated()), **over}), tmp_path, None)

        assert caught.value.error_code == "MODFLOW_WRONG_GRID"


def test_a_statement_that_places_on_the_mesh_s_cells_refuses_on_a_disu(tmp_path):
    sheet = _unstructured(Wells={"geometry": {"type": "Point", "coordinates": [0.0, 0.0]}, "layer": 1, "rate": -1.0})

    with pytest.raises(ModflowError) as caught:
        serialize(sheet, tmp_path, None)

    assert caught.value.error_code == "MODFLOW_WRONG_GRID" and "node numbers" in str(caught.value)


# --- the outputs of a model of the run -----------------------------------------------------------


def _handle(sheet, tmp_path):
    mesh = _mesh(tmp_path)
    return {"run_id": "R1", "utm_epsg": 32617, "mesh": mesh, "sheet": sheet, "module": "gwf", "name": "lattice"}


def test_a_refinement_s_results_are_read_on_its_own_grid_and_named_for_its_model(tmp_path):
    sheet = _deck()
    child = RF.refined_grids(sheet, _mesh(tmp_path))["gwf-child"]
    run = _handle(sheet, tmp_path)

    carrier = Solved(run, GWF)
    refined = Solved(run, GWF, model="gwf-child", grid=child)

    assert (carrier.nlay, len(carrier.nodes[1]), carrier.qualifier, carrier.listing_name) == (2, 16, "", "gwf.lst")
    assert (refined.nlay, len(refined.nodes[1]), refined.qualifier, refined.tag) == (2, 16, " (gwf-child)", "_gwf-child")
    assert refined.listing_name == "gwf-child.lst"
    assert carrier.nodes[0] is not refined.nodes[0] and refined.bbox[0] > carrier.bbox[0]


def test_a_refinement_with_more_layers_than_its_parent_is_read_on_all_of_them(tmp_path):
    sheet = _deck(_refining(layers=[2, 1]))
    grids = RF.refined_grids(sheet, _mesh(tmp_path))
    run = _handle(sheet, tmp_path)

    refined = Solved(run, GWF, model="gwf-child", grid=grids["gwf-child"])

    assert (Solved(run, GWF).nlay, refined.nlay, len(refined.layering[1])) == (2, 3, 3)


def test_the_output_table_reads_every_model_and_a_disu_charts_only_its_observations(tmp_path):
    sheet = _deck()
    child = RF.refined_grids(sheet, _mesh(tmp_path))["gwf-child"]
    run = _handle(sheet, tmp_path)
    solved = {"gwf": Solved(run, GWF), "gwf-child": Solved(run, GWF, model="gwf-child", grid=child)}

    rows, _account = WF._table(solved.__getitem__, "gwf", ["gwf-child"])

    assert {row.module for row in rows if row.variable == "HEAD"} == {None, "gwf-child"}
    assert [row.plane for row in rows if row.module == "gwf-child" and row.variable == "HEAD"] == [1, 2]
    assert WF._table(solved.__getitem__, "gwf", ["gwf-child"], mapped=False)[0] == []


def test_a_refinement_s_files_ride_the_run_s_result_and_listing_lists_under_its_name():
    sheet = _deck(_refining(slots={**_CHILD, "OBS_CONTINUOUS": {"type": "head", "name": "h1", "at": "centre_point", "layer": 1}}),
                  {"centre_point": _point(20.0, 20.0)})

    assert "gwf-child.obs.head.csv" in WF._results(sheet)
    assert WF._listings(sheet) == ["gwf.lst", "gwf-child.lst"]
