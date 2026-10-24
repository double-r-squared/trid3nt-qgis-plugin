"""Particle tracking: a PRT model in the flow model's simulation, particles released from the map, and the pathlines a solved run reads.

Offline over a 4 x 4 lattice of 10 m squares in UTM 32617, numbered row by row from the south-west, two layers (tops 10, 5 and 0).
The validate cases run mf6's own --mode validate through the image.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.inputs.point import PointNotViableError
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow import workflow as WF
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.release import place_release
from trid3nt_server.workflows.modflow.authoring.simulation import coupled_sheet, serialize
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF, PRT
from trid3nt_server.workflows.modflow.modules.outputs import TRACKED, Solved
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.primitives import OutputEmpty, Primitive, Series, Track
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 4
_N = _SIDE * _SIDE
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE) for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_TOP = [10.0] * _N
_BOTM = [[5.0] * _N, [0.0] * _N]
_ACTIVE = [[1] * _N, [1] * _N]


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


_MESH = dict(points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=2, top=_TOP, botm=_BOTM, active=_ACTIVE)
_PRODUCED = dict(p_in=_point(15.0, 25.0), p_off=_point(500.0, 500.0), run=_line((5.0, 5.0), (35.0, 5.0)),
                 strip=_box(0.0, 0.0, 40.0, 10.0), area=_box(0.0, 0.0, 40.0, 40.0))
_STATED = dict(
    DISV_NLAY=2, DISV_LENGTH_UNITS="meters", DISV_TOP=_TOP, DISV_BOTM=_BOTM, NPF_K=1.0e-4, NPF_ICELLTYPE=0,
    IC_STRT=5.0, TDIS_NPER=1, TDIS_TIME_UNITS="days", TDIS_PERIODDATA=[[10.0, 1, 1.0]], NAM_SAVE_FLOWS=True,
    CHD_MAXBOUND=8, CHD_STRESS_PERIOD_DATA={0: [[(layer, cell), 5.0 + (cell % 4 == 0)] for layer in (0, 1) for cell in (0, 3, 12, 15)]},
    OC_HEAD_FILERECORD="gwf.hds", OC_BUDGET_FILERECORD="gwf.bud", OC_SAVERECORD=[("HEAD", "ALL"), ("BUDGET", "ALL")],
    IMS_OUTER_DVCLOSE=1.0e-6, IMS_OUTER_MAXIMUM=100, IMS_INNER_MAXIMUM=100, IMS_INNER_DVCLOSE=1.0e-6, IMS_RCLOSERECORD="1e-06")
_RELEASE = {"geometry": "p_in", "layer": 1, "z": 0.5}
_TRACKED_FILES = {"OC_TRACKCSV_FILERECORD": "prt.trk.csv", "OC_TRACK_FILERECORD": "prt.trk"}


def _prt(release=_RELEASE, **over):
    slots = {"MIP_POROSITY": 0.3, **_TRACKED_FILES, **over}
    if release is not None:
        slots["Release"] = release
    return {"slots": slots}


def _fill(tracking=None, **stated):
    return fill(GWF, produced=_PRODUCED, **{**_STATED, "ParticleTracking": _prt() if tracking is None else tracking, **stated})


def _mesh(tmp_path: Path) -> dict:
    rows = ["MESH2D"]
    for cell, ring in enumerate(_CELLS, start=1):
        rows.append(f"E4Q {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    art = MeshArtifact(mesh_id="M01", name="lattice", mode="om2d", display_uri=str(path), crs_authid="EPSG:32617",
                       has_bathymetry=True, node_count=len(_POINTS), element_count=len(_CELLS),
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
    return rundir, serialize(sheet, rundir, mesh)["written"]


def _refused(sheet, tmp_path):
    with pytest.raises((ModflowError, PointNotViableError)) as caught:
        _written(sheet, tmp_path)
    return str(caught.value)


def _place(*statements, features=(), **over):
    return place_release(list(statements), features=list(features), **{**_MESH, **over})


# ---- the dictionary: what the extractor reads of the PRT definition files

def test_the_prt_dictionary_asks_porosity_and_the_release_count_and_drops_what_mf6_removed():
    needs = {identifier for identifier, slot in PRT.MODULE_INPUT.items() if slot.is_required}
    assert {"MIP_POROSITY", "PRP_NRELEASEPTS", "PRP_PACKAGEDATA"} <= needs
    assert not {"PRP_PERIODDATA", "PRP_NRELEASETIMES", "PRP_DEV_FORCETERNARY", "MIP_RETFACTOR"} & needs
    assert not {"PRP_RELEASE_TIMES", "PRP_FRACTION", "OC_TRACK_TIMES", "OC_TIMESFILE"} & set(PRT.MODULE_INPUT)
    assert not any(identifier.startswith(("IMS_", "OBS_")) for identifier in PRT.MODULE_INPUT)


# ---- the Release statement: validated where it is stated

def _release(**over):
    return {**_RELEASE, **over}


@pytest.mark.parametrize("statement,fragment", [
    (_release(z=None), "states no z and no depth"),
    (_release(layer=None), "states z and no layer"),
    (_release(depth=3.0), "states a depth and a z"),
    (_release(z=1.5), "z is a number from 0 to 1"),
    (_release(layer=0), "layer is an integer from 1"),
    (_release(layer=True), "layer is an integer from 1"),
    (_release(count=0), "count is an integer from 1"),
    (_release(spacing=0), "spacing is a distance above zero"),
    (_release(spacing=-1.0), "spacing is a number from 0"),
    (_release(on="w"), "a geometry and a feature"),
    (_release(bogus=1), "bogus is none of them"),
    ("not a mapping", "takes a mapping"),
])
def test_a_release_that_is_not_a_statement_refuses_by_name(statement, fragment):
    with pytest.raises(ModflowError) as caught:
        PRT.COMPOSITES["Release"].apply(statement, _PRODUCED)
    assert fragment in str(caught.value)


def test_a_release_reading_nothing_states_nothing_and_a_list_concatenates():
    assert PRT.COMPOSITES["Release"].apply({"geometry": None, "layer": 1, "z": 0.5}, _PRODUCED)[0] == {}
    assert PRT.COMPOSITES["Release"].apply({"geometry": "unstated", "layer": 1, "z": 0.5}, _PRODUCED)[0] == {}
    stated = PRT.COMPOSITES["Release"].apply([_release(geometry=_point(1.0, 1.0)), _release(geometry=_point(2.0, 2.0), layer=2)], _PRODUCED)[0]
    assert [item["layer"] for item in stated["PRP_PACKAGEDATA"]["release"]] == [1, 2]


def test_a_release_names_an_input_for_its_geometry_and_its_numbers():
    stated = PRT.COMPOSITES["Release"].apply({"geometry": "run", "layer": 1, "z": "height", "count": 2}, {"run": 7, "height": 0.25})[0]
    assert stated["PRP_PACKAGEDATA"]["release"] == [{"geometry": 7, "layer": 1, "z": 0.25, "count": 2}]


def test_a_prt_body_takes_a_release_only_where_it_is_stated_and_checks_its_keywords():
    with pytest.raises(SlotRefused):
        _fill(tracking=_prt(PRP_NOT_A_KEYWORD=1))
    with pytest.raises(SlotRefused) as caught:
        _fill(tracking=_prt(TDIS_NPER=2))
    assert "TDIS_NPER" in str(caught.value)


# ---- the points a statement places, each in its cell, never moved

def test_a_point_is_one_particle_at_the_point_in_its_cell_and_its_height_is_a_fraction_of_the_layer():
    (row,) = _place({"geometry": _point(15.0, 25.0), "layer": 1, "z": 0.5})
    number, (layer, cell), x, y, z = row
    assert (number, layer, cell) == (0, 0, 9)
    assert (x, y) == pytest.approx((15.0, 25.0), abs=1e-6)
    assert z == 7.5
    assert _place({"geometry": _point(15.0, 25.0), "layer": 2, "z": 1.0})[0][4] == 5.0
    assert _place({"geometry": _point(15.0, 25.0), "layer": 1, "z": 0.0})[0][4] == 5.0
    assert _place({"geometry": _point(15.0, 25.0), "layer": 1, "z": 1.0})[0][4] == 10.0


def test_a_depth_is_below_the_top_and_picks_the_layer_it_falls_in():
    assert _place({"geometry": _point(15.0, 25.0), "depth": 5.0})[0][1:2] == ((0, 9),)
    assert _place({"geometry": _point(15.0, 25.0), "depth": 5.0})[0][4] == 5.0
    assert _place({"geometry": _point(15.0, 25.0), "depth": 2.0})[0][1:2] == ((0, 9),)
    assert _place({"geometry": _point(15.0, 25.0), "depth": 2.0})[0][4] == 8.0
    (row,) = _place({"geometry": _point(15.0, 25.0), "depth": 7.0})
    assert row[1] == (1, 9) and row[4] == 3.0
    with pytest.raises(ModflowError) as caught:
        _place({"geometry": _point(15.0, 25.0), "depth": 12.0})
    assert "below the model base" in str(caught.value)


def test_a_line_seeds_every_spacing_from_its_start_or_a_count_from_end_to_end():
    spaced = _place({"geometry": _line((5.0, 5.0), (35.0, 5.0)), "layer": 1, "z": 1.0, "spacing": 10.0})
    assert [(row[1][1], round(row[2], 6)) for row in spaced] == [(0, 5.0), (1, 15.0), (2, 25.0), (3, 35.0)]
    counted = _place({"geometry": _line((5.0, 5.0), (35.0, 5.0)), "layer": 1, "z": 1.0, "count": 4})
    assert [round(row[2], 6) for row in counted] == [5.0, 15.0, 25.0, 35.0]
    assert [row[0] for row in counted] == [0, 1, 2, 3]
    assert len(_place({"geometry": _line((5.0, 5.0), (35.0, 5.0)), "layer": 1, "z": 1.0, "spacing": 12.0})) == 3


def test_a_polygon_seeds_a_lattice_from_its_corner_or_a_count_per_cell_side():
    lattice = _place({"geometry": _box(0.0, 0.0, 40.0, 10.0), "layer": 1, "z": 1.0, "spacing": 10.0})
    assert [(round(row[2], 6), round(row[3], 6), row[1][1]) for row in lattice] == [(5.0, 5.0, 0), (15.0, 5.0, 1), (25.0, 5.0, 2), (35.0, 5.0, 3)]
    cells = _place({"geometry": _box(0.0, 0.0, 40.0, 10.0), "layer": 1, "z": 1.0, "count": 2})
    assert len(cells) == 16 and {row[1][1] for row in cells} == {0, 1, 2, 3}
    assert sorted({round(row[2], 6) for row in cells}) == [2.5, 7.5, 12.5, 17.5, 22.5, 27.5, 32.5, 37.5]
    assert sorted({round(row[3], 6) for row in cells}) == [2.5, 7.5]
    assert [row[0] for row in cells] == list(range(16))


def test_a_polygon_lattice_keeps_the_squares_whose_centre_it_holds():
    triangle = {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ((0.0, 0.0), (40.0, 0.0), (0.0, 40.0), (0.0, 0.0))]]}
    held = _place({"geometry": triangle, "layer": 1, "z": 1.0, "spacing": 10.0})
    assert len(held) == 10
    assert all(row[2] + row[3] < 40.0 for row in held)


def test_a_feature_releases_n_by_n_in_each_of_its_cells_at_its_own_layer():
    features = [{"package": "WEL", "name": "w", "number": 1, "cells": {5}, "layer": 1},
                {"package": "WEL", "name": "w", "number": 2, "cells": {6}, "layer": 1}]
    rows = _place({"on": "w", "z": 0.5, "count": 2}, features=features)
    assert len(rows) == 8 and {row[1] for row in rows} == {(1, 5), (1, 6)}
    assert {row[4] for row in rows} == {2.5}
    assert sorted({round(row[2], 6) for row in rows if row[1][1] == 5}) == [12.5, 17.5]


@pytest.mark.parametrize("statement,fragment", [
    ({"on": "w", "z": 0.5}, "state count"),
    ({"on": "w", "z": 0.5, "count": 2, "spacing": 5.0}, "no geometry or spacing"),
    ({"on": "nobody", "z": 0.5, "count": 2}, "no placed feature of the flow model carries that name"),
    ({"geometry": _point(15.0, 25.0), "layer": 1, "z": 0.5, "count": 2}, "geometry is a point"),
    ({"geometry": _line((5.0, 5.0), (35.0, 5.0)), "layer": 1, "z": 1.0}, "exactly one of spacing"),
    ({"geometry": _line((5.0, 5.0), (35.0, 5.0)), "layer": 1, "z": 1.0, "spacing": 5.0, "count": 3}, "exactly one of spacing"),
    ({"geometry": _line((5.0, 5.0), (35.0, 5.0)), "layer": 1, "z": 1.0, "count": 1}, "from 2"),
    ({"geometry": _box(0.0, 0.0, 40.0, 10.0), "layer": 1, "z": 1.0}, "exactly one of spacing"),
    ({"geometry": _point(15.0, 25.0), "layer": 3, "z": 0.5}, "the grid has 2 layer"),
])
def test_a_release_the_geometry_cannot_seed_refuses_by_name(statement, fragment):
    features = [{"package": "WEL", "name": "w", "number": 1, "cells": {5}, "layer": 1}]
    with pytest.raises(ModflowError) as caught:
        _place(statement, features=features)
    assert fragment in str(caught.value)


def test_a_feature_with_no_layer_asks_for_one_and_a_release_outside_the_grid_refuses_with_the_nearest_cell():
    with pytest.raises(ModflowError) as caught:
        _place({"on": "lake", "z": 0.5, "count": 1}, features=[{"package": "LAK", "name": "lake", "number": 1, "cells": {5}}])
    assert "no layer of its own" in str(caught.value)
    with pytest.raises(PointNotViableError) as outside:
        _place({"geometry": _point(500.0, 500.0), "layer": 1, "z": 0.5})
    assert outside.value.hint is not None and outside.value.hint["distance_m"] > 400
    with pytest.raises(PointNotViableError):
        _place({"geometry": _line((5.0, 5.0), (95.0, 5.0)), "layer": 1, "z": 1.0, "spacing": 30.0})


def test_a_release_in_a_cell_the_run_does_not_solve_refuses():
    active = [[1] * 9 + [0] + [1] * 6, [1] * _N]
    with pytest.raises(ModflowError) as caught:
        _place({"geometry": _point(15.0, 25.0), "layer": 1, "z": 0.5}, active=active)
    assert "does not solve" in str(caught.value)


def test_count_subdivides_a_rectangular_cell_only():
    triangles = [(0, 1, 6), (0, 6, 5)]
    with pytest.raises(ModflowError) as caught:
        place_release([{"geometry": _box(0.0, 0.0, 10.0, 10.0), "layer": 1, "z": 1.0, "count": 2}], points=_POINTS, cells=triangles,
                      utm_epsg=32617, nlay=2, top=_TOP, botm=_BOTM, active=None, features=[])
    assert "3 vertices" in str(caught.value) or "4 vertices" in str(caught.value) or "rectangular" in str(caught.value)


# ---- the simulation it writes

def test_the_prt_model_is_written_in_the_flows_simulation_with_its_own_solution(staged_beside, tmp_path):
    rundir, written = _written(_fill(), tmp_path)
    assert {"prt.nam", "prt.prp", "prt.mip", "prt.oc", "prt.ems", "gwf.gwfprt"} <= set(written), written
    nam = (rundir / "mfsim.nam").read_text()
    assert "prt6" in nam.lower() and "ems6" in nam.lower() and "gwf6-prt6" in nam.lower()
    assert "ims6" in nam.lower()
    prp = " ".join((rundir / "prt.prp").read_text().split())
    assert "NRELEASEPTS 1" in prp and "1 1 10 15.00000000 25.00000000 7.50000000" in prp
    mip = " ".join((rundir / "prt.mip").read_text().split())
    assert "porosity" in mip.lower() and "0.3" in mip
    assert "trackcsv" in (rundir / "prt.oc").read_text().lower()
    assert "prt6" in (rundir / "prt.nam").read_text().lower() or "dis" in (rundir / "prt.nam").read_text().lower()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_release_options_and_times_are_the_prp_keywords_stated_by_name(staged_beside, tmp_path):
    sheet = _fill(tracking=_prt(PRP_EXIT_SOLVE_TOLERANCE=1.0e-8, PRP_EXTEND_TRACKING=True, PRP_STOPTRAVELTIME=5.0,
                                PRP_PERIODDATA={0: ["FIRST"]}, MIP_RETFACTOR=2.0))
    rundir, _ = _written(sheet, tmp_path)
    prp = " ".join((rundir / "prt.prp").read_text().split())
    assert "EXIT_SOLVE_TOLERANCE 1.00000000E-08" in prp and "EXTEND_TRACKING" in prp and "STOPTRAVELTIME 5" in prp
    assert "FIRST" in prp
    assert "2.0" in " ".join((rundir / "prt.mip").read_text().split())
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_list_of_releases_is_one_package_numbered_from_one(staged_beside, tmp_path):
    sheet = _fill(tracking=_prt([_RELEASE, {"geometry": "run", "layer": 2, "z": 1.0, "count": 2}]))
    rundir, _ = _written(sheet, tmp_path)
    prp = " ".join((rundir / "prt.prp").read_text().split())
    assert "NRELEASEPTS 3" in prp
    assert "1 1 10 15.00000000 25.00000000 7.50000000" in prp
    assert "2 2 1 5.00000000 5.00000000 5.00000000" in prp and "3 2 4 35.00000000 5.00000000 5.00000000" in prp
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_release_on_a_well_by_its_name_and_by_keywords(staged_beside, tmp_path):
    sheet = _fill(tracking=_prt({"on": "pump", "z": 0.5, "count": 2}), WEL_MAXBOUND=1,
                  Wells={"geometry": "p_in", "layer": 2, "rate": -1.0e-3, "name": "pump"})
    rundir, _ = _written(sheet, tmp_path)
    prp = " ".join((rundir / "prt.prp").read_text().split())
    assert "NRELEASEPTS 4" in prp and "1 2 10" in prp
    assert validate_authored_simulation(rundir) == {"rc": 0}
    keywords = _fill(tracking=_prt(None, PRP_NRELEASEPTS=1, PRP_PACKAGEDATA=[(0, (0, 9), 15.0, 25.0, 7.5)]))
    rundir2 = tmp_path / "again"
    rundir2.mkdir()
    out = MF._mesh_files(keywords, _mesh(tmp_path))
    shutil.copy(out["disv"], rundir2 / "mesh.disv")
    serialize(keywords, rundir2, _mesh(tmp_path))
    assert "1 1 10 15" in " ".join((rundir2 / "prt.prp").read_text().split())


def test_porosity_unstated_refuses_by_name_and_so_does_a_release_nothing_states(staged_beside, tmp_path):
    sheet = _fill(tracking={"slots": {**_TRACKED_FILES, "Release": _RELEASE}})
    assert "MIP_POROSITY" in _refused(sheet, tmp_path)
    sheet = _fill(tracking=_prt(None))
    assert "PRP_NRELEASEPTS" in _refused(sheet, tmp_path)


def test_a_refinement_beside_particle_tracking_refuses(staged_beside, tmp_path):
    sheet = _fill(LocalGridRefinement={"geometry": "area", "refinement": 2,
                                       "slots": {"NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0}})
    assert "LocalGridRefinement" in _refused(sheet, tmp_path)


def test_the_manifest_word_is_the_flow_or_its_transport_never_the_particles():
    assert WF.answering(_fill()) == "gwf"
    both = _fill(Transport={"slots": {"MST_POROSITY": 0.3, "IC_STRT": 0.0}})
    assert WF.answering(both) == "gwt"


def test_a_qualified_keyword_reaches_the_prt_body_and_an_unqualified_one_the_carrier():
    sheet = _fill(tracking=_prt())
    assert [each.MODULE for each in WF.run_bodies(GWF, ["prt: PRP_STOPTIME"])] == ["gwf", "prt"]
    assert [each.MODULE for each in WF.run_bodies(GWF, ["PRP_STOPTIME"])] == ["gwf"]
    own, coupled = WF._floor(GWF, {"prt: PRP_STOPTIME": 5.0, "IC_STRT": 1.0})
    assert own == {"IC_STRT": 1.0} and coupled == {"prt": {"PRP_STOPTIME": 5.0}}
    assert [body["module"] for body in sheet.coupled] == ["prt"]
    assert coupled_sheet(sheet, sheet.coupled[0]).filled["MIP_POROSITY"].value == 0.3


# ---- what a solved run writes: the CSV track file read as pathlines, endpoints and a distribution

_HEADER = "kper,kstp,imdl,iprp,irpt,ilay,icell,izone,istatus,ireason,trelease,t,x,y,z,name"
_RECORDS = [
    # particle 1: released, drops, crosses a cell, ends at a boundary (the exit and the end are one position)
    "1,1,2,1,1,1,10,0,0,0,0.0,0.0,5.0,5.0,10.0,",
    "1,1,2,1,1,1,10,0,1,6,0.0,0.0,5.0,5.0,8.0,",
    "1,1,2,1,1,1,11,0,1,1,0.0,2.0,10.0,5.0,7.0,",
    "1,1,2,1,1,1,11,0,2,1,0.0,4.0,20.0,5.0,6.0,",
    "1,1,2,1,1,1,11,0,2,3,0.0,4.0,20.0,5.0,6.0,",
    # particle 2: refused its release
    "1,1,2,1,2,1,12,0,8,0,0.0,0.0,25.0,5.0,6.0,",
    "1,1,2,1,2,1,12,0,8,3,0.0,0.0,25.0,5.0,6.0,",
    # particle 3: released at 1 day, still being tracked at the end
    "1,1,2,1,3,1,13,0,0,0,1.0,1.0,35.0,5.0,6.0,",
    "1,1,2,1,3,1,13,0,1,5,1.0,3.0,36.0,5.0,6.0,",
]


@pytest.fixture
def solved(monkeypatch, tmp_path):
    (tmp_path / "prt.trk.csv").write_text("\n".join([_HEADER, *_RECORDS]) + "\n")

    def download(run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        shutil.copy(tmp_path / basename, dest)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)

    def build(tracking=None) -> Solved:
        sheet = _fill(tracking=tracking)
        run = {"run_id": "R1", "utm_epsg": 32617, "sheet": sheet, "mesh": _mesh(tmp_path)}
        return Solved(run, PRT, filled=coupled_sheet(sheet, sheet.coupled[0]).filled, model="prt")

    return build


_DAY = 86400.0


def test_a_pathline_is_a_line_per_particle_with_the_travel_time_at_each_vertex(solved):
    read = PRT.READS["pathlines"](Primitive("pathlines"), solved())
    assert isinstance(read, Track) and read.name == "pathlines"
    features = read.features["features"]
    assert [f["properties"]["release_point"] for f in features] == [1, 3]
    first = features[0]
    # The repeated record at the end and the second record at the release (same time, position and height... the drop changes z) differ.
    assert [c[2] for c in first["geometry"]["coordinates"]] == [10.0, 8.0, 7.0, 6.0]
    assert first["properties"]["travel_times_s"] == [0.0, 0.0, 2 * _DAY, 4 * _DAY]
    assert first["properties"]["travel_time_s"] == 4 * _DAY and first["properties"]["end"] == "reached a boundary face"
    lon, lat = _TO_LONLAT(20.0, 5.0)
    assert first["geometry"]["coordinates"][-1][:2] == pytest.approx([lon, lat], abs=1e-7)
    third = features[1]["properties"]
    assert third["released_at_s"] == _DAY and third["travel_time_s"] == 2 * _DAY and third["end"] == "still being tracked"
    assert read.measures["particles"] == 3 and read.measures["unmoved"] == 1 and read.measures["pathlines"] == 2
    assert read.measures["longest_s"] == 4 * _DAY and read.measures["shortest_s"] == 2 * _DAY


def test_an_endpoint_is_the_last_record_with_how_it_ended(solved):
    read = PRT.READS["endpoints"](Primitive("endpoints"), solved())
    ended = {f["properties"]["release_point"]: f for f in read.features["features"]}
    assert ended[1]["properties"]["end"] == "reached a boundary face"
    assert ended[2]["properties"]["end"] == "never released: its cell is inactive"
    assert ended[3]["properties"]["end"] == "still being tracked"
    assert ended[1]["geometry"]["type"] == "Point" and ended[1]["properties"]["layer"] == 1
    assert read.measures["ended"] == {"never released: its cell is inactive": 1, "reached a boundary face": 1,
                                      "still being tracked": 1}


def test_the_travel_times_count_the_particles_that_ended(solved):
    read = PRT.READS["travel_time"](Primitive("travel_time"), solved())
    assert isinstance(read, Series) and read.name == "particles ended" and read.units == "particles"
    assert read.times.tolist() == [0.0, 4 * _DAY] and read.values.tolist() == [1.0, 2.0]
    assert read.measures["ended"] == 2 and read.measures["particles"] == 3


def test_an_output_the_run_did_not_save_refuses_by_the_keyword_that_would_have_saved_it(solved):
    sheet_less = {"slots": {"MIP_POROSITY": 0.3, "Release": _RELEASE}}
    run = solved(tracking=sheet_less)
    for token in TRACKED:
        refused = run.unsaved(token)
        assert refused is not None and "OC_TRACKCSV_FILERECORD" in str(refused)
    assert solved().unsaved("PATHLINES") is None
    with pytest.raises(ModflowError):
        PRT.READS["pathlines"](Primitive("pathlines"), run)


def test_a_track_file_with_no_record_is_empty_and_a_particle_that_never_moved_has_no_line(solved, tmp_path):
    (tmp_path / "prt.trk.csv").write_text(_HEADER + "\n")
    with pytest.raises(OutputEmpty):
        PRT.READS["pathlines"](Primitive("pathlines"), solved())
    (tmp_path / "prt.trk.csv").write_text("\n".join([_HEADER, _RECORDS[5], _RECORDS[6]]) + "\n")
    with pytest.raises(OutputEmpty) as caught:
        PRT.READS["pathlines"](Primitive("pathlines"), solved())
    assert "no particle moved" in str(caught.value)
    (tmp_path / "prt.trk.csv").write_text("\n".join([_HEADER, _RECORDS[7], _RECORDS[8]]) + "\n")
    with pytest.raises(OutputEmpty) as none_ended:
        PRT.READS["travel_time"](Primitive("travel_time"), solved())
    assert "no particle ended" in str(none_ended.value)


def test_a_track_file_lacking_a_column_refuses(solved, tmp_path):
    (tmp_path / "prt.trk.csv").write_text("kper,kstp\n1,1\n")
    with pytest.raises(ModflowError) as caught:
        PRT.READS["pathlines"](Primitive("pathlines"), solved())
    assert "lacks the column" in str(caught.value)


def test_the_table_publishes_a_layer_a_layer_and_a_chart_for_a_prt_body(solved):
    body = solved()
    carrier = Solved({"run_id": "R1", "utm_epsg": 32617, "sheet": body.sheet, "mesh": body.mesh}, GWF)
    read = {"gwf": carrier, "prt": body}
    rows, _account = WF._table(read.__getitem__, "gwf", ["prt"], mapped=True)
    mine = {row.kind: row for row in rows if row.module == "prt"}
    assert {kind: row.publish for kind, row in mine.items()} == {"pathlines": "layer", "endpoints": "layer", "travel_time": "chart"}
    assert mine["pathlines"].style == {"kind": "reference", "geometry": "line"}
    assert mine["endpoints"].style == {"kind": "reference", "geometry": "point"}
    assert not [row for row in rows if row.module == "prt" and row.kind not in mine]


def test_a_run_that_saved_no_track_file_journals_the_keyword_and_publishes_no_row(solved):
    bare = solved(tracking={"slots": {"MIP_POROSITY": 0.3, "Release": _RELEASE}})
    carrier = Solved({"run_id": "R1", "utm_epsg": 32617, "sheet": bare.sheet, "mesh": bare.mesh}, GWF)
    read = {"gwf": carrier, "prt": bare}
    rows, _account = WF._table(read.__getitem__, "gwf", ["prt"], mapped=True)
    assert not [row for row in rows if row.module == "prt"]
