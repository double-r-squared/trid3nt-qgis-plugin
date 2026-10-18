"""The TELEMAC run is straight-line code: every stage in its one order, each a
plain call, the mesh reused under its content key and rebuilt on restart_clean."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime.fill import Fill
from seated import resolve_params
from trid3nt_server.workflows.telemac import workflow as tw
from trid3nt_server.workflows.telemac.authoring import (mesh_files, opening,
                                                        release_point)
from trid3nt_server.tools.mesh import step as mesh_step
from seated import value_inputs


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
    from trid3nt_contracts.common import Origin
    from trid3nt_server.workflows.runtime.fill import ACCEPTED, Verdict

    state = Fill(workflow=workflow, carried={"restart_clean": restart_clean},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    state.inputs.update({name: Verdict(ACCEPTED, value, origin=Origin.USER)
                         for name, value in (keywords or {}).items()})
    # The release is stated: a point the run would place by itself is never invented.
    from trid3nt_server.inputs.point import Point

    state.inputs["release"] = Verdict(ACCEPTED, Point(-122.65, 45.53), origin=Origin.USER)
    run = asyncio.run(workflow.launch(state))
    return run, calls


def test_the_run_takes_its_stages_in_their_one_order(monkeypatch):
    run, calls = _launched(monkeypatch)
    stages = ["mesh", "mesh_files", "channel", "source", "settled", "sheet",
              "solve", "outputs"]
    assert [name for name in run.results if name in stages] == stages
    kept = [kwargs["args"] for name, kwargs in calls if name == "keep"]
    assert [len(args) for args in kept] == [1, 2] and kept[1][1] == "RUN7"


def test_the_solve_is_handed_the_run_the_sheet_stage_staged(monkeypatch):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)

    async def staged(**kwargs):
        calls.append(("staged", kwargs))
        return {"run_tag": "T"}

    async def sheet(*, write, **_kwargs):
        await write("the sheet")
        return {"name": "sheet"}

    monkeypatch.setattr(tw, "stage_sheet", staged)
    monkeypatch.setattr(tw, "fill_sheet", sheet)
    from trid3nt_server.workflows.runtime.fill import Fill

    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["solve"]["staged"] == {"run_tag": "T"}
    assert "results" not in dict(calls)["solve"]
    assert dict(calls)["staged"]["sheet"] == "the sheet"


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
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["sheet"]["settled"] == {"TIME_STEP": 0.7}


def test_a_launch_that_raises_carries_its_own_error_and_its_notes(monkeypatch):
    from trid3nt_server.workflows.runtime.errors import StepFailedError
    from trid3nt_server.inputs.run_notes import journal_note

    class Refused(Exception):
        """The run's own failure, which nothing may replace."""

    async def refuse(**_kwargs):
        journal_note("the weather row had no source")
        raise Refused("the sheet refused")

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    _faked(monkeypatch, [])
    monkeypatch.setattr(tw, "fill_sheet", refuse)
    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    with pytest.raises(StepFailedError) as caught:
        asyncio.run(workflow._launched(state))
    assert isinstance(caught.value.__cause__, Refused)
    assert "also missing from this run: the weather row had no source" \
        in caught.value.__notes__


def test_what_the_card_seats_is_the_value_the_outputs_stage_binds(monkeypatch):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)
    name = next(iter(value_inputs(workflow))).name

    async def sheet(*, seat, **_kwargs):
        seat({name: "edited on the card"})
        return {"name": "sheet"}

    monkeypatch.setattr(tw, "fill_sheet", sheet)
    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["outputs"]["params"][name] == "edited on the card"


def test_a_stated_moment_reaches_the_sheet_with_the_domains_own_point(monkeypatch):
    """UTC, and where on the earth: what a module needs to place the sun. A
    run that states no moment hands the sheet none."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)

    async def _row(env, decl):
        return SimpleNamespace(centroid=(-122.67, 45.52))

    monkeypatch.setattr(fill_mod, "_produce", _row)
    stated = {"event_time": "2024-01-14T12:00:00+00:00"}
    state = Fill(workflow=workflow, carried={},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), stated)))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["sheet"]["produced"]["moment"] == {
        "at": datetime(2024, 1, 14, 12, tzinfo=timezone.utc),
        "lon": -122.67, "lat": 45.52}
    _run, calls = _launched(monkeypatch)
    assert "moment" not in dict(calls)["sheet"]["produced"]


def test_the_published_clock_counts_from_the_moment_the_run_stated(monkeypatch):
    """A run that states its moment publishes every time counted from it; one
    that states none counts from when its solve began."""
    from types import SimpleNamespace

    class _Row(str):
        centroid = (-82.4, 43.0)

    async def _row(env, decl):
        return _Row(f"row:{decl.name}")

    moment = "2019-07-04T12:00:00+00:00"
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    calls: list = []
    _faked(monkeypatch, calls)
    monkeypatch.setattr(fill_mod, "_produce", _row)
    state = Fill(workflow=workflow, carried={}, inputs=asyncio.run(
        resolve_params(value_inputs(workflow), {"event_time": moment})))
    asyncio.run(workflow.launch(state))
    assert dict(calls)["solve"]["moment"] == moment

    async def _box(*_args, **_kwargs):
        return {"run_id": "RUN7", "started_at": "2026-10-12T08:00:00+00:00"}

    monkeypatch.undo()
    monkeypatch.setattr(tw, "run_sheet_", _box)
    sheet = SimpleNamespace(module="telemac2d", files={}, resolved=lambda: {},
                            published=lambda: ())
    settled = {"mesh_inputs": [], "server_facts": {}}

    def solved(**stated):
        return asyncio.run(tw.run_sheet(
            sheet=sheet, staged={}, settled=settled, dispatch="builtins.print", **stated))

    assert solved(moment=moment)["reference_time"] == moment
    assert solved()["reference_time"] == "2026-10-12T08:00:00+00:00"


class _Store:
    """The object store's client surface a staging touches, kept in memory."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body, **_):
        self.objects[Key] = Body

    def get_paginator(self, _name):
        store = self

        class _Pages:
            def paginate(self, *, Bucket, Prefix):
                yield {"Contents": [{"Key": key} for key in store.objects
                                    if key.startswith(Prefix)]}
        return _Pages()

    def delete_objects(self, *, Bucket, Delete):
        for row in Delete["Objects"]:
            del self.objects[row["Key"]]


def _card_edited(monkeypatch, tmp_path, *, cancel):
    """Launch a run whose card is edited once, then cancelled or proceeded on -> the store, the solve's staged run."""
    from trid3nt_server.store import objects
    from trid3nt_server.workflows.solver import staging
    from trid3nt_server.workflows.telemac.errors import TelemacError

    store, solved = _Store(), []
    monkeypatch.setattr(objects, "_CLIENT", store)
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "cache")
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    calls: list = []
    _faked(monkeypatch, calls)

    async def staged(*, sheet, run_tag, prefix, **_kwargs):
        rundir = staging.new_rundir(run_tag, stem="telemac")
        (rundir / f"deck-{sheet}.cas").write_text(sheet)
        return await staging.stage_run(
            rundir, run_tag, module="telemac2d", steering=f"deck-{sheet}.cas",
            results=["r.slf"], outputs=[], mesh_inputs=[], prefix=prefix,
            sheet={}, server_facts={}, result_basename="r.slf",
            check=lambda *_a, **_k: None)

    async def sheet(*, write, **_kwargs):
        await write("first")
        await write("last")
        if cancel:
            raise TelemacError("cancelled at the review",
                               error_code="USER_INPUT_CANCELLED")
        return {"name": "sheet"}

    async def solve(*, staged, **_kwargs):
        solved.append(dict(staged))
        return {"run_id": "RUN7"}

    monkeypatch.setattr(tw, "stage_sheet", staged)
    monkeypatch.setattr(tw, "fill_sheet", sheet)
    monkeypatch.setattr(tw, "run_sheet", solve)
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    try:
        asyncio.run(workflow.launch(state))
    except TelemacError:
        assert cancel
    return store, solved


def test_card_edits_restage_into_the_one_prefix_and_directory(
        monkeypatch, tmp_path):
    store, solved = _card_edited(monkeypatch, tmp_path, cancel=False)
    tag = solved[0]["run_tag"]
    assert [path.name for path in tmp_path.iterdir()] == [f"telemac-{tag}"]
    assert [path.name for path in (tmp_path / f"telemac-{tag}").iterdir()] \
        == ["deck-last.cas"]
    assert sorted(store.objects) == [f"telemac/{tag}/deck-last.cas",
                                     f"telemac/{tag}/manifest.json"]


def test_a_review_cancelled_after_an_edit_leaves_nothing_staged(
        monkeypatch, tmp_path):
    store, solved = _card_edited(monkeypatch, tmp_path, cancel=True)
    assert not solved and not store.objects and not list(tmp_path.iterdir())
