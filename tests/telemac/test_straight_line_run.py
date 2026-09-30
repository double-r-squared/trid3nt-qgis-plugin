"""The TELEMAC run is straight-line code: every stage in its one order, each a
plain call, the mesh reused under its content key and rebuilt on restart_clean."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime.fill import Fill
from trid3nt_server.workflows.runtime.resolver import resolve_params
from trid3nt_server.workflows.telemac import workflow as tw
from trid3nt_server.workflows.telemac.authoring import (mesh_files, opening,
                                                        release_point)
from trid3nt_server.mesh import step as mesh_step


def _faked(monkeypatch, calls: list) -> None:
    def fake(name, value=None):
        async def run(*args, **kwargs):
            calls.append((name, {**kwargs, **({"args": args} if args else {})}))
            return {"name": name, **(value or {})}
        return run

    async def _row(env, decl):
        return f"row:{decl.name}"

    monkeypatch.setattr(fill_mod, "_produce", _row)
    monkeypatch.setattr(mesh_step, "build_declared_mesh", fake("mesh"))
    monkeypatch.setattr(mesh_step, "keep_mesh", fake("keep"))
    monkeypatch.setattr(mesh_files, "telemac_mesh_files", fake("mesh_files"))
    monkeypatch.setattr(opening, "open_channel", fake("channel"))
    monkeypatch.setattr(opening, "open_water", fake("settled"))
    monkeypatch.setattr(release_point, "settle_release", fake("source"))
    monkeypatch.setattr(tw, "fill_sheet", fake("sheet"))
    monkeypatch.setattr(tw, "run_sheet", fake("solve", {"run_id": "RUN7"}))
    monkeypatch.setattr(tw, "publish_outputs", fake("outputs"))


def _launched(monkeypatch, *, keywords=None, restart_clean=False):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)
    state = Fill(workflow=workflow, keywords=dict(keywords or {}),
                 carried={"restart_clean": restart_clean},
                 params=asyncio.run(resolve_params(workflow.params, {})))
    run = asyncio.run(workflow.launch(state))
    return run, calls


def test_the_run_takes_its_stages_in_their_one_order(monkeypatch):
    run, calls = _launched(monkeypatch)
    stages = ["mesh", "mesh_files", "channel", "source", "settled", "sheet",
              "solve", "outputs"]
    assert [name for name in run.results if name in stages] == stages
    kept = [kwargs["args"] for name, kwargs in calls if name == "keep"]
    assert [len(args) for args in kept] == [1, 2] and kept[1][1] == "RUN7"


def test_the_mesh_is_asked_for_over_the_domain_and_bed_rows(monkeypatch):
    _run, calls = _launched(monkeypatch)
    asked = dict(calls)["mesh"]["mesh"]
    assert asked["extent"] == "row:domain"
    beds = [op["kwargs"]["source"] for op in asked["ops"] if op["op"] == "set_bed"]
    assert beds == ["row:bed"]


def test_restart_clean_asks_the_mesh_fresh(monkeypatch):
    _run, calls = _launched(monkeypatch, restart_clean=True)
    assert dict(calls)["mesh"]["fresh"] is True
    _run, calls = _launched(monkeypatch)
    assert dict(calls)["mesh"]["fresh"] is False


def test_the_previous_computation_leaves_the_floor_for_the_settle(monkeypatch):
    _run, calls = _launched(
        monkeypatch, keywords={"PREVIOUS COMPUTATION FILE": "s3://r/prev.slf"})
    assert dict(calls)["settled"]["continue_from"] == "s3://r/prev.slf"
    assert dict(calls)["source"]["continue_from"] == "s3://r/prev.slf"
    assert "PREVIOUS COMPUTATION FILE" not in dict(calls)["sheet"]["keywords"]


def test_the_settle_s_keywords_go_straight_to_the_sheet(monkeypatch):
    def settled(**kwargs):
        async def run(*_args, **_kw):
            return {"name": "settled", "keywords": {"TIME_STEP": 0.7}}
        return run

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)
    monkeypatch.setattr(opening, "open_water", settled())
    state = Fill(workflow=workflow, carried={},
                 params=asyncio.run(resolve_params(workflow.params, {})))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["sheet"]["settled"] == {"TIME_STEP": 0.7}


def test_a_launch_that_raises_carries_its_own_error_and_its_notes(monkeypatch):
    from trid3nt_server.workflows.runtime.errors import StepFailedError
    from trid3nt_server.workflows.runtime.journal import journal_note

    class Refused(Exception):
        """The run's own failure, which nothing may replace."""

    async def refuse(**_kwargs):
        journal_note("the weather row had no source")
        raise Refused("the sheet refused")

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    _faked(monkeypatch, [])
    monkeypatch.setattr(tw, "fill_sheet", refuse)
    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 params=asyncio.run(resolve_params(workflow.params, {})))
    with pytest.raises(StepFailedError) as caught:
        asyncio.run(workflow._launched(state))
    assert isinstance(caught.value.__cause__, Refused)
    assert "also missing from this run: the weather row had no source" \
        in caught.value.__notes__


def test_what_the_card_seats_is_the_value_the_outputs_stage_binds(monkeypatch):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)
    name = next(iter(workflow.params)).name

    async def sheet(*, seat, **_kwargs):
        seat({name: "edited on the card"})
        return {"name": "sheet"}

    monkeypatch.setattr(tw, "fill_sheet", sheet)
    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 params=asyncio.run(resolve_params(workflow.params, {})))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["outputs"]["params"][name] == "edited on the card"
