"""The TELEMAC run is straight-line code: every stage in its one order, each a
plain call, the mesh reused under its content key and rebuilt on restart_clean."""

from __future__ import annotations

import asyncio

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
    assert list(run.results) == ["stated", "mesh", "mesh_files", "channel",
                                 "source", "settled", "sheet", "solve",
                                 "outputs"]
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
