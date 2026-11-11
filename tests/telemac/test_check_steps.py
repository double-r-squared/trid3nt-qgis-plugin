"""check_steps stops a TELEMAC solve after N engine steps through its own DURATION, marks the run a check run, and refuses where an engine does not honour it."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime.fill import Fill
from trid3nt_server.workflows.runtime import journal
from seated import resolve_params, value_inputs
from trid3nt_server.workflows.telemac import workflow as tw
from trid3nt_server.workflows.telemac.authoring import mesh_files, opening, release_point
from trid3nt_server.tools.mesh import step as mesh_step


def _launched(monkeypatch, *, carried, settled=None):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    seen: dict = {}

    def fake(name, value=None):
        async def run(*args, **kwargs):
            seen[name] = kwargs
            return {"name": name, **(value or {})}
        return run

    async def _row(env, decl):
        return f"row:{decl.name}"

    monkeypatch.setattr(fill_mod, "_produce", _row)
    for module, attr, name in ((mesh_step, "build_declared_mesh", "mesh"), (mesh_step, "keep_mesh", "keep"),
                               (mesh_files, "telemac_mesh_files", "mesh_files"), (opening, "open_channel", "channel"),
                               (release_point, "settle_release", "source")):
        monkeypatch.setattr(module, attr, fake(name))
    monkeypatch.setattr(opening, "open_water", fake("settled", {"server_facts": {"duration_s": 3600.0}, "keywords": settled or {"TIME_STEP": 0.7, "DURATION": 3600.0}}))
    monkeypatch.setattr(tw, "fill_sheet", fake("sheet"))
    monkeypatch.setattr(tw, "run_sheet", fake("solve", {"run_id": "RUN7"}))
    monkeypatch.setattr(tw, "publish_outputs", fake("outputs"))
    state = Fill(workflow=workflow, carried=carried,
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    from trid3nt_contracts.common import Origin
    from trid3nt_server.inputs.point import Point
    from trid3nt_server.workflows.runtime.fill import ACCEPTED, Verdict

    state.inputs["release"] = Verdict(ACCEPTED, Point(-122.65, 45.53), origin=Origin.USER)
    token = bind_notes()
    run = asyncio.run(workflow.launch(state))
    return run, seen, drain_notes(token)


def test_check_steps_shortens_the_duration_to_that_many_steps_of_the_step_in_force(monkeypatch):
    run, seen, notes = _launched(monkeypatch, carried={"check_steps": 200})
    assert seen["sheet"]["keywords"]["DURATION"] == pytest.approx(140.0)
    assert run.results["check_steps"] == 200
    assert seen["outputs"]["partial"] == "check run, stops at step 200"
    assert any("stops after 200 engine steps" in note for note in notes)


def test_a_stated_time_step_beats_the_settled_one(monkeypatch):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    held = workflow._checked({tw._SETTLED: {"server_facts": {"duration_s": 3600.0}, "keywords": {"TIME_STEP": 0.7, "DURATION": 3600.0}}}, {"TIME_STEP": 2.0}, 100)
    assert held["DURATION"] == pytest.approx(200.0)


def test_a_check_longer_than_the_window_keeps_the_window(monkeypatch):
    _run, seen, _notes = _launched(monkeypatch, carried={"check_steps": 100000})
    assert seen["sheet"]["keywords"]["DURATION"] == pytest.approx(3600.0)


def test_the_printout_period_is_held_to_the_steps_taken(monkeypatch):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    cadence = workflow.steering.CADENCE
    held = workflow._checked({tw._SETTLED: {"server_facts": {"duration_s": 3600.0}, "keywords": {"TIME_STEP": 1.0, "DURATION": 600.0}}}, {cadence: 500}, 100)
    assert held[cadence] == 100


def test_unset_is_a_normal_run(monkeypatch):
    run, seen, notes = _launched(monkeypatch, carried={})
    assert "DURATION" not in seen["sheet"]["keywords"]
    assert "check_steps" not in run.results
    assert seen["outputs"]["partial"] is None
    assert not any("check run" in note for note in notes)


def test_a_variable_time_step_refuses_naming_the_control():
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    from trid3nt_server.workflows.telemac.errors import TelemacError

    with pytest.raises(TelemacError, match="check_steps"):
        workflow._checked({tw._SETTLED: {"server_facts": {"duration_s": 3600.0}, "keywords": {"TIME_STEP": 1.0}}}, {"VARIABLE TIME-STEP": True}, 5)


@pytest.mark.parametrize("name", ["modflow_water_table", "geoclaw_tsunami_inundation"])
def test_an_engine_without_it_refuses_naming_the_control(name):
    if name not in TOOL_REGISTRY:
        pytest.skip(f"{name} is not registered")
    workflow = TOOL_REGISTRY[name].fn.workflow
    refused = workflow.judged(Fill(workflow=workflow, carried={"check_steps": 5}))
    assert refused["check_steps"].code == "CHECK_STEPS_UNSUPPORTED"
    assert "check_steps" in refused["check_steps"].reason


@pytest.mark.parametrize("bad", [0, -3, True, 2.5, "7"])
def test_a_count_that_is_not_a_whole_number_of_steps_refuses(bad):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    refused = workflow.judged(Fill(workflow=workflow, carried={"check_steps": bad}))
    assert refused["check_steps"].code == "CHECK_STEPS_INVALID"


def test_the_control_is_on_the_wire_and_a_telemac_template_accepts_it():
    fn = TOOL_REGISTRY["telemac_dye_release"].fn
    assert "check_steps" in fn.param_descriptions
    workflow = fn.workflow
    assert "check_steps" not in workflow.judged(Fill(workflow=workflow, carried={"check_steps": 5}))


def test_the_journal_record_says_it_is_a_check_run():
    record = journal.build_record(run_id="R", engine="telemac", module="telemac2d", inputs={}, result=None,
                                  wall_seconds=1.0, origin="test", notes=[], check_steps=20)
    assert record["check_steps"] == 20
    assert journal.build_record(run_id="R", engine="telemac", module="telemac2d", inputs={}, result=None,
                                wall_seconds=1.0, origin="test", notes=[])["check_steps"] is None


def test_a_check_shorter_than_the_duration_range_refuses_naming_the_steps_it_needs():
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    from trid3nt_server.workflows.telemac.errors import TelemacError

    with pytest.raises(TelemacError, match=r"check_steps of 86 or more") as raised:
        workflow._checked({tw._SETTLED: {"server_facts": {"duration_s": 3600.0}, "keywords": {"TIME_STEP": 0.7, "DURATION": 3600.0}}}, {}, 40)
    assert raised.value.error_code == "TELEMAC_CHECK_STEPS_TOO_FEW"


def test_the_fill_window_the_staged_check_reads_is_the_shortened_one():
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    run = {tw._SETTLED: {"server_facts": {"duration_s": 3600.0, "name": "n"}, "keywords": {"TIME_STEP": 1.0, "DURATION": 3600.0}}}
    workflow._checked(run, {}, 100)
    assert run[tw._SETTLED]["server_facts"] == {"duration_s": 100.0, "name": "n"}
