"""Each engine's launch splits at its deck: the first half writes and checks the deck and solves nothing, the
second solves the deck the first wrote without meshing or writing again, and the daemon runs both with the review
card between them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from trid3nt.inputs.point import Point
from trid3nt.inputs.review import ReviewOutcome
from trid3nt.sdk.script_run import load_deck, save_deck
from trid3nt.tools import TOOL_REGISTRY
from trid3nt.tools.mesh import step as mesh_step
from trid3nt.workflows.geoclaw import workflow as gw
from trid3nt.workflows.modflow import workflow as mw
from trid3nt.workflows.runtime import fill as fill_mod
from trid3nt.workflows.runtime.fill import ACCEPTED, Fill, Verdict
from trid3nt.workflows.telemac import workflow as tw
from trid3nt.workflows.telemac.authoring import mesh_files, opening, release_point
from seated import resolve_params, value_inputs

_ENGINES = {"telemac_dye_release": tw, "modflow_water_table": mw, "geoclaw_tsunami_inundation": gw}


def _staged(calls: list):
    async def stage_sheet(*, sheet, run_id, prefix="", **_kwargs):
        calls.append("write")
        return {"run_id": run_id, "rundir": f"/staged/{run_id}", "inputs": [], "sheet": {"of": sheet}}
    return stage_sheet


def _stubbed(monkeypatch, template: str, calls: list) -> Fill:
    """The engine's stages answered by stubs that say what they were asked; the sheet is drawn by a card that writes."""
    module = _ENGINES[template]

    async def _row(env, decl):
        return f"row:{decl.name}"

    def said(name, value=None):
        async def run(*_args, **kwargs):
            calls.append(name)
            return {"name": name, **(value(kwargs) if callable(value) else value or {})}
        return run

    async def sheet(*, write, **_kwargs):
        calls.append("sheet")
        await write("the sheet")
        return {"name": "sheet"}

    art = SimpleNamespace(utm_epsg=32610, name="domain", mesh_id="M1", probes={}, engine_files={})
    monkeypatch.setattr(fill_mod, "_produce", _row)
    monkeypatch.setattr(mesh_step, "build_declared_mesh", said("mesh", {"artifact": art, "key": "K1"}))
    monkeypatch.setattr(mesh_step, "keep_mesh", said("keep"))
    monkeypatch.setattr(module, "fill_sheet", sheet)
    monkeypatch.setattr(module, "stage_sheet", _staged(calls))
    monkeypatch.setattr(module, "run_sheet", said("solve", lambda kwargs: {"run_id": kwargs["staged"]["run_id"],
                                                                           "staged": kwargs["staged"]}))
    monkeypatch.setattr(module, "publish_outputs", said("outputs"))
    if module is tw:
        monkeypatch.setattr(mesh_files, "telemac_mesh_files", said("mesh_files"))
        monkeypatch.setattr(opening, "open_channel", said("channel"))
        monkeypatch.setattr(opening, "open_water", said("settled", {"mesh_inputs": [], "server_facts": {}}))
        monkeypatch.setattr(release_point, "settle_release", said("source"))
    if module is gw:
        monkeypatch.setattr(gw, "publish_grid", said("grid"))
        monkeypatch.setattr(gw, "publish_rupture", said("rupture"))
        monkeypatch.setattr(gw, "grid_keywords", lambda *_a, **_k: {})
        monkeypatch.setattr(gw.GeoclawWorkflow, "_data", lambda self, *_a: said("data")())
        monkeypatch.setattr(gw.GeoclawWorkflow, "_catalogue", lambda self, *_a: said("catalogue", None)())
    workflow = TOOL_REGISTRY[template].fn.workflow
    state = Fill(workflow=workflow, carried={"restart_clean": False},
                 inputs=asyncio.run(resolve_params(value_inputs(workflow), {})))
    if module is tw:
        # The release is stated: a point the run would place by itself is never invented.
        state.inputs["release"] = Verdict(ACCEPTED, Point(-122.65, 45.53))
    return state


@pytest.mark.parametrize("template", sorted(_ENGINES))
def test_the_first_half_writes_the_deck_and_solves_nothing_and_the_second_solves_that_deck(monkeypatch, template):
    calls: list = []
    state = _stubbed(monkeypatch, template, calls)
    workflow = state.workflow
    deck = asyncio.run(workflow.prepared(state))
    assert "write" in calls and "mesh" in calls and "solve" not in calls and "outputs" not in calls
    assert deck.staged["run_id"] == deck.run_id and deck.state is state
    written = list(calls)
    calls.clear()
    reviewed = load_deck(save_deck(deck), workflow)
    assert reviewed.state.workflow is workflow
    assert reviewed.state.env.inputs is reviewed.state.inputs
    assert reviewed.staged == deck.staged and reviewed.held == deck.held
    # The solve only reads the prepared deck after the handoff.
    run = asyncio.run(workflow._solved(reviewed))
    assert "solve" in calls and "outputs" in calls
    assert not {"mesh", "sheet", "write"} & set(calls), (written, calls)
    assert run.results["solve"]["staged"] is reviewed.staged


def test_the_daemon_shows_the_card_between_the_written_deck_and_the_solve(monkeypatch):
    """The card is the real review over the written sheet: its deck is written before it is drawn, and a proceed
    is followed by the solve alone."""
    from trid3nt.inputs import review as gate
    from trid3nt.workflows.runtime import review

    calls: list = []
    state = _stubbed(monkeypatch, "telemac_dye_release", calls)
    sheet = SimpleNamespace(filled={}, engine_rows=lambda: ([], set()), required=lambda: [], open=lambda: [],
                            body=SimpleNamespace(MODULE_INPUT={}, __name__="T2D"))

    async def card(**_kwargs):
        calls.append("card")
        return ReviewOutcome(proceed=True, entries=[], params={})

    monkeypatch.setattr(review, "fill_slots", lambda *_a, **_k: sheet)
    monkeypatch.setattr(gate, "gate_input_review", card)
    monkeypatch.setattr(tw, "fill_sheet", tw.partial(review.fill_sheet, engine="telemac", floor=tw._floor))
    asyncio.run(state.workflow._launched(state))
    assert [call for call in calls if call in ("write", "card", "solve")] == ["write", "card", "solve"]
