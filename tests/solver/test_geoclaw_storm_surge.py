"""The geoclaw_storm_surge template: the sheet its statements fill, the storm the run fetches and the clock it counts from.

Offline. The fixes are rows of the IBTrACS table for Hurricane Ike (2008); the level-1 box is the example's and the
domain a window on the Texas coast, so what is pinned is the plumbing: the sheet writes surge.data and the storm file
and passes the staged check, the surge refinement lists survive their file, the workflow fetches the named storm over
the level-1 box and counts the run from its last landfall unless an event_time is stated, and the template reads only
inputs it declares.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.tools.mesh.session import MeshSession
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.geoclaw.authoring import grid_keywords, serialize
from trid3nt_server.workflows.geoclaw.authoring.staged_check import check_staged_run
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.geoclaw.templates.storm_surge import storm_surge as template
from trid3nt_server.workflows.runtime.sheet import fill

_STORM = Path(__file__).with_name("test_geoclaw_storm.py")
_spec = importlib.util.spec_from_file_location("geoclaw_storm_fixes", _STORM)
_fixes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixes)

_BOX = (-99.0, 8.0, -70.0, 32.0)
_COAST = (-95.2, 29.0, -94.4, 29.7)


def _grid(tmp_path):
    ops = [mesh_op("flagregion", name="Region_domain", minlevel=1, maxlevel=4, t1=-1.0e9, t2=1.0e9, extent=_BOX),
           mesh_op("flagregion", name="Region_coast", minlevel=5, maxlevel=5, t1=-1.0e9, t2=1.0e9, extent=_COAST)]
    return MeshSession(tool.build_mesh(mesher="claw_grid", extent=_BOX, resolution_m=template._LEVEL1_M,
                                       build={"refinement_ratios": template._RATIOS}, ops=ops), workdir=tmp_path).accept()


def _sheet(tmp_path, **over):
    art = _grid(tmp_path)
    run = {"mesh": {"artifact": art}, "storm_tracks": _fixes._doc(), "storm_name": "IKE", "season": 2008,
           "event_time": _fixes._LANDFALL, "domain": SimpleNamespace(bbox=_COAST), "vertical_frame": "NAVD88"}
    stated = {k: v for k, v in template.STEERING.ASSERTED.items() if k != "Topography"}
    stated.update(CLAW_T0=-3600.0, CLAW_TFINAL=7200.0, FixedGrids={**stated["FixedGrids"], "tstart_max": -3600.0}, **over)
    return fill(GEO, produced=run, **stated, **grid_keywords(art, set(stated)))


def test_the_sheet_writes_the_surge_data_and_the_storm_file_and_passes_the_staged_check(tmp_path):
    rundir = tmp_path / "run"
    sheet = _sheet(tmp_path)
    written = serialize(sheet, rundir)
    checked = check_staged_run(rundir, sheet=sheet, written=written["written"])["checked"]
    surge = (rundir / "surge.data").read_text()

    assert {"SURGE_R_REFINE", "SURGE_WIND_REFINE", "SURGE_STORM_FILE", "GEO_MANNING_BREAK"} <= set(checked)
    assert "60000.0 40000.0 20000.0 =: R_refine" in surge and "1                    =: storm_specification_type" in surge
    assert "'storm_01.storm'" in surge and (rundir / "storm_01.storm").read_text().splitlines()[1] == "0.0"
    assert sheet.filled["AMR_AMR_LEVELS_MAX"].value == 5 and sheet.filled["CLAW_NUM_AUX"].value == 7


def test_a_storm_file_the_run_directory_lacks_refuses_at_the_staged_check(tmp_path):
    from trid3nt_server.workflows.geoclaw.errors import GeoclawError

    rundir = tmp_path / "run"
    sheet = _sheet(tmp_path)
    written = serialize(sheet, rundir)
    (rundir / "storm_01.storm").unlink()

    with pytest.raises(GeoclawError) as caught:
        check_staged_run(rundir, sheet=sheet, written=written["written"])
    assert caught.value.error_code == "GEOCLAW_STAGED_FILE_MISSING"


def test_every_model_the_engine_reads_a_track_for_is_a_keyword_value_of_the_body_not_of_the_composite(tmp_path):
    for number in (1, 2, 4, 5, 6, 7, 8, 9):
        sheet = _sheet(tmp_path / str(number), SURGE_STORM_SPECIFICATION_TYPE=number)
        rundir = tmp_path / f"run{number}"
        written = serialize(sheet, rundir)
        check_staged_run(rundir, sheet=sheet, written=written["written"])
        assert f"{number}                    =: storm_specification_type" in (rundir / "surge.data").read_text()


def test_the_template_reads_only_inputs_it_declares_and_states_no_sea_level():
    steering = template.STEERING
    declared = {name for name in vars(template.INPUTS) if not name.startswith("_")}
    read = set(steering.named()) - {"deep_bed_offset", "storm_tracks"}

    assert read <= declared, read - declared
    assert {"storm_name", "season"} <= declared and "GEO_SEA_LEVEL" not in steering.ASSERTED
    assert steering.ASSERTED["SURGE_STORM_SPECIFICATION_TYPE"] == 1


def _data(monkeypatch, run, own=None):
    """The rows `_data` asks the world for, over a stand-in environment and a recording fetch."""
    from trid3nt_server.workflows.geoclaw import workflow as gw

    asked = []

    async def _fetch(env, row, runner, **kwargs):
        asked.append((row, runner, kwargs))
        return _fixes._doc()

    monkeypatch.setattr(gw, "_fetch", _fetch)
    monkeypatch.setattr("trid3nt_server.inputs.geometry.read_geometry_doc", lambda source: source)
    standin = SimpleNamespace(steering=template.STEERING, template=template)
    standin._storm = lambda env, statement: gw.GeoclawWorkflow._storm(standin, env, statement)
    asyncio.run(gw.GeoclawWorkflow._data(standin, SimpleNamespace(run=run), {"GEO_SEA_LEVEL": 0.0, **(own or {})}, template.STEERING))
    return asked


def _held(tmp_path, **over):
    return {"mesh": {"artifact": _grid(tmp_path)}, "deep_bed": object(), "deep_bed_offset": None,
            "storm_name": "IKE", "season": 2008, **over}


def test_the_run_fetches_the_named_storm_over_the_level_1_box_and_counts_from_its_landfall(monkeypatch, tmp_path):
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    run = _held(tmp_path)
    token = bind_notes()
    asked = _data(monkeypatch, run)
    notes = drain_notes(token)

    assert asked == [("storm_tracks_fetch", "fetch_storm_tracks", {
        "bbox": [-99.0, 8.0, -70.0, 32.0], "storm_name": "IKE", "start_year": 2008, "end_year": 2008,
        "geometry": "points", "purpose": "storm track"})]
    assert run["event_time"] == _fixes._LANDFALL and run["storm_tracks"]["features"]
    assert len(notes) == 1 and "last landfall" in notes[0]


def test_a_stated_event_time_stands_and_a_supplied_track_is_not_fetched(monkeypatch, tmp_path):
    stated = _held(tmp_path, event_time="2008-09-13T06:00:00+00:00")
    _data(monkeypatch, stated)
    supplied = _held(tmp_path, storm_tracks=_fixes._doc())

    assert stated["event_time"] == "2008-09-13T06:00:00+00:00"
    assert _data(monkeypatch, supplied) == []


def test_a_run_naming_no_storm_refuses_before_any_fetch(monkeypatch, tmp_path):
    from trid3nt_server.workflows.geoclaw.errors import GeoclawError

    with pytest.raises(GeoclawError) as caught:
        _data(monkeypatch, _held(tmp_path, season=None))

    assert caught.value.error_code == "GEOCLAW_STORM_UNSTATED"


def test_the_workflow_answers_for_the_storm_row_and_names_its_product():
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.geoclaw import workflow as gw

    workflow = TOOL_REGISTRY["geoclaw_storm_surge"].fn.workflow

    assert workflow.input("storm_tracks_fetch").producer.runner == "fetch_storm_tracks"
    assert "storm_tracks" in gw._PRODUCED and workflow.unnamed() == ()
