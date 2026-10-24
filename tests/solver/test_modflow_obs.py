"""Observations and time series stated through the composites: the OBS files they write, the id of each line, the chart a run's CSV becomes.

The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the south-west, one layer, three
periods. Offline except the validate cases, which run mf6's own --mode validate through the image.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.inputs.point import PointNotViableError
from trid3nt_server.inputs.temporal import Series
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow import workflow as W
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.observations import observation_files, place_observations
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF, GWT
from trid3nt_server.workflows.modflow.modules.outputs import Solved
from trid3nt_server.workflows.runtime import primitives as P
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 3
_N = _SIDE * _SIDE
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE)
          for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_MESH = dict(points=_POINTS, cells=_CELLS, utm_epsg=32617)
_STREAM = {"width": 5.0, "slope": 0.001, "bed_top": 9.0, "bed_thickness": 1.0, "bed_conductivity": 1.0e-5,
           "manning": 0.03}


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


_PRODUCED = dict(
    stream=_line((2.0, 15.0), (28.0, 15.0)), p_centre=_point(15.0, 15.0), p_corner=_point(5.0, 5.0),
    p_east=_point(25.0, 15.0), p_stream=_point(5.0, 15.0), p_off=_point(500.0, 500.0))


def _fill(_produced=None, **stated):
    return fill(GWF, produced={**_PRODUCED, **(_produced or {})}, **{
        "DISV_NLAY": 1, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": [[0.0] * _N], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
        "TDIS_NPER": 3, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 1, 1.0]] * 3,
        "CHD_MAXBOUND": 1, "CHD_STRESS_PERIOD_DATA": {0: [[0, 0, 5.0]]}, **stated})


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


def _written(sheet, tmp_path, *packages):
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    written = serialize(sheet, rundir, mesh)["written"]
    for package in packages:
        assert package in written, written
    return rundir, {name: _Flat((rundir / name).read_text()) for name in packages}


def _refused(sheet, tmp_path):
    mesh = _mesh(tmp_path)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    with pytest.raises((ModflowError, PointNotViableError)) as caught:
        out = MF._mesh_files(sheet, mesh)
        shutil.copy(out["disv"], rundir / "mesh.disv")
        serialize(sheet, rundir, mesh)
    return str(caught.value)


def _statements(sheet, identifier):
    return sheet.filled[identifier].value["observations"]


# ---- the composite: a line of an OBS file, validated where it is stated

def test_a_statement_with_no_package_is_a_line_of_the_models_obs_file_and_with_one_that_packages():
    sheet = _fill(Observations=[{"type": "head", "name": "h1", "at": "p_centre", "layer": 1},
                                {"type": "wel", "name": "q1", "package": "wel", "on": "w"},
                                {"type": "flow-ja-face", "name": "f1", "at": "p_centre", "layer": 1, "to_at": "p_east",
                                 "to_layer": 1}])
    assert [row["name"] for row in _statements(sheet, "OBS_CONTINUOUS")] == ["h1", "f1"]
    assert _statements(sheet, "WEL_OBS_FILERECORD")[0]["package"] == "WEL"


@pytest.mark.parametrize("change,fragment", [
    ({"type": "he ad"}, "mf6's own observation type"),
    ({"type": None}, "mf6's own observation type"),
    ({"name": "two words"}, "without a space"),
    ({"name": "n" * 41}, "1 to 40"),
    ({"layer": 0}, "layer is an integer from 1"),
    ({"layer": True}, "layer is an integer from 1"),
    ({"connection": 0}, "connection is an integer from 1"),
    ({"digits": -1}, "digits is an integer from 0"),
    ({"bogus": 1}, "bogus is none of them"),
    ({"package": " "}, "package is the prefix"),
])
def test_an_observation_that_is_not_a_line_refuses_by_name(change, fragment):
    with pytest.raises(ModflowError) as caught:
        _fill(Observations={**{"type": "head", "name": "h1", "at": "p_centre", "layer": 1}, **change})
    assert fragment in str(caught.value)


# ---- the id of each line, by the point contract

def test_a_cell_is_a_point_on_a_layer_and_is_written_with_its_file(staged_beside, tmp_path):
    sheet = _fill(Observations=[{"type": "head", "name": "h1", "at": "p_centre", "layer": 1, "digits": 8},
                                {"type": "drawdown", "name": "d1", "at": "p_corner", "layer": 1, "digits": 8},
                                {"type": "flow-ja-face", "name": "f1", "at": "p_centre", "layer": 1, "to_at": "p_east",
                                 "to_layer": 1, "digits": 8}])
    rundir, text = _written(sheet, tmp_path, "gwf.obs")
    obs = text["gwf.obs"]
    assert "DIGITS 8" in obs
    assert "BEGIN continuous FILEOUT gwf.obs.head.csv" in obs and "h1 head 1 5" in obs
    assert "BEGIN continuous FILEOUT gwf.obs.drawdown.csv" in obs and "d1 drawdown 1 1" in obs
    assert "f1 flow-ja-face 1 5 1 6" in obs
    assert "OBS6  gwf.obs" in (rundir / "gwf.nam").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_named_well_is_its_cell_for_a_head_and_a_point_in_its_cell_names_it_too(staged_beside, tmp_path):
    sheet = _fill(
        WEL_MAXBOUND=1,
        Wells=[{"geometry": "p_centre", "layer": 1, "rate": -1.0, "name": "w"},
               {"geometry": "p_corner", "layer": 1, "rate": -2.0, "name": "v"}],
        Observations=[{"type": "head", "name": "hw", "on": "w"},
                      {"type": "head", "name": "hv", "at": "p_corner", "on": "v"},
                      {"type": "wel", "name": "qw", "package": "WEL", "on": "w"},
                      {"type": "wel", "name": "qv", "package": "WEL", "at": "p_corner", "layer": 1}])
    rundir, text = _written(sheet, tmp_path, "gwf.obs", "gwf.wel.obs")
    assert "hw head 1 5" in text["gwf.obs"] and "hv head 1 1" in text["gwf.obs"]
    assert "qw wel 1 5" in text["gwf.wel.obs"] and "qv wel 1 1" in text["gwf.wel.obs"]
    assert "BEGIN continuous FILEOUT gwf.wel.obs.wel.csv" in text["gwf.wel.obs"]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_placed_feature_is_its_number_and_a_connection_is_the_second_id(staged_beside, tmp_path):
    sheet = _fill(
        STO_ICONVERT=0, STO_SS=0.0, STO_SY=0.0,
        Streams={"geometry": "stream", "name": "creek", **_STREAM},
        Observations=[{"package": "SFR", "type": "stage", "name": "s2", "at": "p_stream"},
                      {"package": "SFR", "type": "downstream-flow", "name": "d3", "at": "p_east"}])
    rundir, text = _written(sheet, tmp_path, "gwf.sfr.obs")
    obs = text["gwf.sfr.obs"]
    assert "s2 stage 1" in obs and "d3 downstream-flow 3" in obs
    assert "BEGIN continuous FILEOUT gwf.sfr.obs.stage.csv" in obs
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_connection_is_one_based_and_written_as_the_second_id():
    found, _digits = place_observations(
        [{"type": "maw", "name": "q1", "package": "MAW", "on": "w", "connection": 2}], module="gwf", package="MAW",
        features=[{"package": "MAW", "name": "w", "number": 0, "cells": {4}}], nlay=1, **_MESH)
    assert found == {"gwf.maw.obs.maw.csv": [["q1", "maw", (0,), (1,)]]}


@pytest.mark.parametrize("statement,fragment", [
    ({"type": "head", "name": "h", "at": "p_off", "layer": 1}, "nearest"),
    ({"type": "head", "name": "h", "at": "p_centre", "layer": 2}, "grid has 1 layer"),
    ({"type": "head", "name": "h", "at": "p_centre"}, "state both"),
    ({"type": "head", "name": "h", "layer": 1}, "state both"),
    ({"type": "head", "name": "h", "on": "nobody"}, "no placed feature carries that name"),
    ({"type": "flow-ja-face", "name": "h", "at": "p_centre", "layer": 1, "to_at": "p_east"}, "state both"),
    ({"type": "x", "name": "h", "at": "p_centre", "layer": 1, "connection": 1, "to_at": "p_east", "to_layer": 1},
     "id2 is one of them"),
    ({"package": "SFR", "type": "stage", "name": "s", "at": "p_stream"}, "names 0 features"),
])
def test_an_id_that_cannot_be_resolved_refuses_and_a_point_is_never_moved(staged_beside, tmp_path, statement, fragment):
    sheet = _fill(Observations=statement, WEL_MAXBOUND=1,
                  Wells={"geometry": "p_corner", "layer": 1, "rate": -1.0, "name": "w"})
    assert fragment in _refused(sheet, tmp_path)


def test_a_layer_on_a_numbered_package_refuses(staged_beside, tmp_path):
    sheet = _fill(STO_ICONVERT=0, STO_SS=0.0, STO_SY=0.0, Streams={"geometry": "stream", **_STREAM},
                  Observations={"package": "SFR", "type": "stage", "name": "s", "at": "p_stream", "layer": 1})
    assert "has no layer" in _refused(sheet, tmp_path)


def test_digits_are_the_files_one_option(staged_beside, tmp_path):
    sheet = _fill(Observations=[{"type": "head", "name": "a", "at": "p_centre", "layer": 1, "digits": 5},
                                {"type": "head", "name": "b", "at": "p_corner", "layer": 1, "digits": 6}])
    assert "digits" in _refused(sheet, tmp_path)
    sheet = _fill(OBS_DIGITS=4, Observations={"type": "head", "name": "a", "at": "p_centre", "layer": 1, "digits": 5})
    assert "OBS_DIGITS" in _refused(sheet, tmp_path)


# ---- the keyword path: stated whole, or refused

def test_observations_stated_whole_are_written_as_stated(staged_beside, tmp_path):
    sheet = _fill(OBS_CONTINUOUS={"gwf.h.csv": [["a", "head", (0, 4)]]}, OBS_DIGITS=9,
                  WEL_MAXBOUND=1, WEL_STRESS_PERIOD_DATA={0: [[(0, 1), -1.0]]},
                  WEL_OBS_FILERECORD={"digits": 7, "continuous": {"gwf.w.csv": [["q", "wel", (0, 1)]]}})
    rundir, text = _written(sheet, tmp_path, "gwf.obs", "gwf.wel.obs")
    assert "a head 1 5" in text["gwf.obs"] and "DIGITS 9" in text["gwf.obs"]
    assert "q wel 1 2" in text["gwf.wel.obs"] and "DIGITS 7" in text["gwf.wel.obs"]
    assert validate_authored_simulation(rundir) == {"rc": 0}
    assert observation_files(sheet.filled, "gwf") == [("gwf.h.csv", "gwf.h.csv", None), ("gwf.w.csv", "gwf.w.csv", None)]


def test_a_package_observation_file_named_only_is_refused_not_dropped(staged_beside, tmp_path):
    sheet = _fill(WEL_MAXBOUND=1, WEL_STRESS_PERIOD_DATA={0: [[(0, 1), -1.0]]}, WEL_OBS_FILERECORD="wel.obs")
    assert "WEL_OBS_FILERECORD names an observation file this authoring would not write" in _refused(sheet, tmp_path)


def test_a_transport_body_writes_its_series_and_its_observations(staged_beside, tmp_path):
    sheet = _fill(
        OC_HEAD_FILERECORD="gwf.hds",
        Transport={"slots": {
            "IC_STRT": 0.0, "MST_POROSITY": 0.25, "ADV_SCHEME": "upstream", "IMS_LINEAR_ACCELERATION": "BICGSTAB",
            "SSM_SAVE_FLOWS": True, "CNC_MAXBOUND": 1, "CNC_STRESS_PERIOD_DATA": {0: [[(0, 0), "CIN"]]},
            "CNC_TS_FILERECORD": {"names": "CIN", "rows": [[0.0, 1.0], [30.0, 2.0]], "interpolation": "linear"},
            "OBS_CONTINUOUS": {"type": "concentration", "name": "c1", "at": "p_centre", "layer": 1}}})
    rundir, text = _written(sheet, tmp_path, "gwt.obs", "cnc.ts", "gwt.cnc")
    assert "c1 concentration 1 5" in text["gwt.obs"]
    assert "BEGIN continuous FILEOUT gwt.obs.concentration.csv" in text["gwt.obs"]
    assert "1 1 CIN" in text["gwt.cnc"]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_the_csvs_a_sheet_writes_are_what_the_worker_must_find():
    sheet = _fill(Observations=[{"type": "head", "name": "h", "at": "p_centre", "layer": 1},
                                {"type": "wel", "name": "q", "package": "WEL", "on": "w"}],
                  OC_HEAD_FILERECORD="gwf.hds")
    assert observation_files(sheet.filled, "gwf") == [("gwf.obs.head.csv", "head", "head"),
                                                     ("gwf.wel.obs.wel.csv", "WEL wel", "wel")]
    assert W._results(sheet) == ["gwf.hds", "gwf.obs.head.csv", "gwf.wel.obs.wel.csv"]


def test_a_coupled_body_states_its_observations_through_the_carrier_s_composite():
    sheet = _fill(Transport={"slots": {"OBS_CONTINUOUS": {"type": "concentration", "name": "c1", "at": "p_centre", "layer": 1}}})
    body = sheet.coupled[0]
    assert body["module"] == "gwt"
    assert observation_files(body["slots"], "gwt") == [("gwt.obs.concentration.csv", "concentration", "concentration")]
    assert GWT.COMPOSITES["Observations"] is not None


# ---- time series on every package, from a record or by name

def test_a_run_input_record_is_a_series_on_the_run_s_clock_in_the_model_s_time_units(staged_beside, tmp_path):
    record = Series([0.0, 86400.0 * 10, 86400.0 * 30], [1.0, 3.0, 2.0], units="m")
    sheet = _fill({"record": record}, TimeSeries={"package": "CHD", "names": "STAGE", "rows": "record",
                                                  "interpolation": "linear"},
                  CHD_STRESS_PERIOD_DATA={0: [[(0, 0), "STAGE"]]})
    rundir, text = _written(sheet, tmp_path, "chd.ts", "gwf.chd")
    assert all(row in text["chd.ts"] for row in ("0.00000000 1.00000000", "10.00000000 3.00000000", "30.00000000 2.00000000"))
    assert "TS6  FILEIN  chd.ts" in (rundir / "gwf.chd").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_record_in_other_units_is_converted_and_a_clock_without_units_refuses():
    record = Series([0.0, 3600.0], [100.0, 200.0], units="cm")
    held = GWF.COMPOSITES["TimeSeries"].apply(
        {"package": "CHD", "names": "S", "rows": "r", "units": "m", "interpolation": "linear"},
        {"r": record, "TDIS_TIME_UNITS": "hours"})[0]
    assert held["CHD_TS_FILERECORD"]["series"]["rows"] == [[0.0, 1.0], [1.0, 2.0]]
    with pytest.raises(ModflowError) as caught:
        GWF.COMPOSITES["TimeSeries"].apply({"package": "CHD", "names": "S", "rows": "r", "interpolation": "linear"},
                                           {"r": record})
    assert "TDIS_TIME_UNITS" in str(caught.value)
    with pytest.raises(ModflowError) as caught:
        GWF.COMPOSITES["TimeSeries"].apply(
            {"package": "CHD", "names": ["S", "T"], "rows": "r", "interpolation": "linear"},
            {"r": record, "TDIS_TIME_UNITS": "hours"})
    assert "one series" in str(caught.value)


def test_a_stress_value_names_a_series_of_its_package_and_every_package_takes_one(staged_beside, tmp_path):
    sheet = _fill(
        WEL_MAXBOUND=1, DRN_MAXBOUND=1,
        Wells={"geometry": "p_centre", "layer": 1, "rate": {"series": "PUMP"}},
        Drains={"geometry": "p_corner", "layer": 1, "elevation": {"series": "LEVEL"}, "conductance": 1.0},
        TimeSeries=[{"package": "WEL", "names": "PUMP", "rows": [[0.0, -1.0], [30.0, -2.0]], "interpolation": "stepwise"},
                    {"package": "DRN", "names": "LEVEL", "rows": [[0.0, 8.0], [30.0, 9.0]], "interpolation": "linear"}])
    rundir, text = _written(sheet, tmp_path, "gwf.wel", "wel.ts", "gwf.drn", "drn.ts")
    assert "1 5 PUMP" in text["gwf.wel"] and "1 1 LEVEL 1.0" in text["gwf.drn"]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_series_a_package_does_not_state_refuses_naming_both(staged_beside, tmp_path):
    sheet = _fill(WEL_MAXBOUND=1, Wells={"geometry": "p_centre", "layer": 1, "rate": {"series": "PUMP"}},
                  TimeSeries={"package": "WEL", "names": "OTHER", "rows": [[0.0, -1.0], [30.0, -2.0]],
                              "interpolation": "linear"})
    message = _refused(sheet, tmp_path)
    assert "PUMP" in message and "OTHER" in message
    assert "states none" in _refused(_fill(WEL_MAXBOUND=1, Wells={"geometry": "p_centre", "layer": 1,
                                                                   "rate": {"series": "PUMP"}}), tmp_path)


def test_one_file_feeds_one_package():
    with pytest.raises(ModflowError) as caught:
        _fill(TimeSeries=[{"package": "CHD", "names": "A", "rows": [[0.0, 1.0]], "interpolation": "linear"},
                          {"package": "CHD", "names": "B", "rows": [[0.0, 1.0]], "interpolation": "linear"}])
    assert "CHD twice" in str(caught.value)


# ---- the chart a run's CSV becomes

_CSV = "time,H1,H2\n10.0,1.5,2.5\n20.0,2.5,1e30\n30.0,3.5,3.5\n"


@pytest.fixture
def solved(monkeypatch, tmp_path):
    def download(run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        dest.write_text(_CSV)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)
    sheet = _fill(Observations=[{"type": "head", "name": "h1", "at": "p_centre", "layer": 1},
                                {"type": "head", "name": "h2", "at": "p_corner", "layer": 1}])
    return Solved({"run_id": "R1", "utm_epsg": 32617, "sheet": sheet, "mesh": {}}, GWF)


def test_a_file_is_one_series_with_a_line_per_observation_and_a_dry_value_is_left_out(solved):
    read = P.Primitive("observations", variable="gwf.obs.head.csv")
    series = GWF.READS["observations"](read, solved)
    assert series.name == "head observations" and series.units == "meters" and series.label == "H1"
    assert list(series.times) == [10.0 * 86400, 20.0 * 86400, 30.0 * 86400] and list(series.values) == [1.5, 2.5, 3.5]
    (line,) = series.lines
    assert line.label == "H2" and list(line.values) == [2.5, 3.5] and list(line.x) == [10.0 * 86400, 30.0 * 86400]
    assert series.measures == {"observations": 2, "max": 3.5, "min": 1.5}
    payload = P._chart(series, caption="head observations", where="Lattice")
    names = {row["series"] for row in payload.vega_lite_spec["data"]["values"]} if hasattr(payload, "vega_lite_spec") \
        else {row["series"] for row in payload["vega_lite_spec"]["data"]["values"]}
    assert names == {"H1", "H2"}


def test_a_file_the_run_does_not_state_refuses(solved):
    with pytest.raises(ModflowError) as caught:
        GWF.READS["observations"](P.Primitive("observations", variable="other.csv"), solved)
    assert "no OBS file this run states" in str(caught.value)


def test_the_module_table_publishes_each_stated_file_as_a_chart(solved):
    rows, _account = W._table(lambda module: solved, "gwf", [])
    (row,) = [p for p in rows if p.kind == "observations"]
    assert row.variable == "gwf.obs.head.csv" and row.publish == "chart" and row.module is None
