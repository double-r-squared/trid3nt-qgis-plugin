"""The resolution order, pinned: one row, and the keyword sheet.

A row: what the user supplied wins over the row's one source - its producer or
the match its need asks, never both on one row - and only a row with neither is
asked on the canvas; nothing answering refuses by name. An unmatched need
refuses rather than falling to the canvas. The keyword sheet: the engine's
default under the template's statement under the fill's."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.runtime.errors import StepFailedError
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import DOMAIN
from trid3nt_server.workflows.runtime import fill


@pytest.fixture
def rungs(monkeypatch):
    """Every rung records that it was reached and answers with its own name."""
    reached: list[str] = []

    async def _ingested(env, decl, value, *rest):
        return value

    async def _matched(env, decl):
        reached.append("match")
        return "matched"

    async def _produced(env, producer, label):
        reached.append("producer")
        return "produced"

    async def _canvas(slot, **kw):
        reached.append("prompt")
        return reached_answer.get("prompt")

    reached_answer: dict[str, object] = {}
    monkeypatch.setattr(fill, "_ingested", _ingested)
    monkeypatch.setattr(fill, "_validate_supplied", lambda *a: None)
    monkeypatch.setattr(fill, "_matched", _matched)
    monkeypatch.setattr(fill, "_produced", _produced)
    monkeypatch.setattr(fill, "_fetcher_row", lambda *a: None)
    monkeypatch.setattr("trid3nt_server.inputs.slots.ask_on_canvas", _canvas)
    return SimpleNamespace(reached=reached, answer=reached_answer)


def _producer_row():
    return dataclasses.replace(Input(), name="layer",
                               producer=SimpleNamespace(runner="fake"))


def _need_row():
    return dataclasses.replace(Input(DOMAIN).need("hydrography"), name="domain")


def _prompt_row():
    return dataclasses.replace(Input().need(geometry="polyline"), name="line")


def _env(**supplied):
    return fill._Env(data={}, supplied=supplied)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [_producer_row, _need_row, _prompt_row])
async def test_what_the_user_supplied_wins_and_nothing_else_is_asked(rungs, row):
    decl = row()
    assert await fill._produce(_env(**{decl.name: "supplied"}), decl) == "supplied"
    assert rungs.reached == []


@pytest.mark.asyncio
async def test_a_producer_answers_an_unsupplied_row(rungs):
    assert await fill._produce(_env(), _producer_row()) == "produced"
    assert rungs.reached == ["producer"]


@pytest.mark.asyncio
async def test_a_need_is_matched_and_never_drawn(rungs):
    assert await fill._produce(_env(), _need_row()) == "matched"
    assert rungs.reached == ["match"]


@pytest.mark.asyncio
async def test_an_unmatched_need_refuses_rather_than_prompting(rungs, monkeypatch):
    async def _unmatched(env, decl):
        rungs.reached.append("match")
        raise StepFailedError("nothing measures it here",
                              error_code="DATA_NEED_UNMATCHED")

    monkeypatch.setattr(fill, "_matched", _unmatched)
    with pytest.raises(StepFailedError) as caught:
        await fill._produce(_env(), _need_row())
    assert caught.value.error_code == "DATA_NEED_UNMATCHED"
    assert rungs.reached == ["match"]


@pytest.mark.asyncio
async def test_a_row_with_no_source_is_drawn_then_refused(rungs):
    rungs.answer["prompt"] = "drawn"
    assert await fill._produce(_env(), _prompt_row()) == "drawn"
    rungs.answer["prompt"] = None
    with pytest.raises(StepFailedError) as caught:
        await fill._produce(_env(), _prompt_row())
    assert caught.value.error_code == "DATA_SLOT_UNSATISFIED"
    assert rungs.reached == ["prompt", "prompt"]


def test_the_fill_over_the_template_over_the_engine_default():
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import (
        STEERING)
    from trid3nt_server.workflows.telemac.workflow import stated

    engine = STEERING.slot("FRICTION_COEFFICIENT").engine_default
    template = stated(steering=STEERING, keywords={})["FRICTION_COEFFICIENT"]
    filled = stated(steering=STEERING, keywords={"FRICTION COEFFICIENT": 40.0})
    assert template == 33.0 and template != engine
    assert filled["FRICTION_COEFFICIENT"] == 40.0
    # A keyword neither states is absent: the engine's own default stands.
    assert "TIDAL_FLATS" not in stated(steering=STEERING, keywords={})
