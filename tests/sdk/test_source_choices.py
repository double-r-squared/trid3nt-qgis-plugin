"""Explicit source options and the successful requests the SDK exposes."""

import asyncio

import pytest

from trid3nt import case
from trid3nt.inputs.input import Input
from trid3nt.inputs.slots import BED
from trid3nt.inputs.user_input import UserInputError
from trid3nt.sdk.model import _form
from trid3nt.tools.fetchers._router.errors import RouterInputError
from trid3nt.workflows.runtime import fill
from trid3nt_contracts.coverage import SourceChoice, SourceOption


def test_named_source_options_refuse_before_replacing_the_value(tmp_path):
    m = case(tmp_path).modflow_water_table(bed=4.0)
    with pytest.raises(RouterInputError) as refused:
        m.bed = {"source": "fetch_dem", "bbox": [0]}
    assert refused.value.error_code == "DEM_INPUT_ERROR"
    assert m.bed == 4.0


def _selection(monkeypatch):
    async def need(*args):
        return None

    async def ask(*args):
        return {"purpose": "bed"}

    calls = []

    async def run(runner, kwargs, label):
        calls.append((runner, kwargs))
        return runner

    def match(need, candidates):
        return SourceChoice(slot="bed terrain", need="terrain", picked="fetch_dem",
                            rows=[SourceOption(fetcher="fetch_dem"), SourceOption(fetcher="fetch_etopo")])

    monkeypatch.setattr(fill, "_need", need)
    monkeypatch.setattr(fill, "_ask_for", ask)
    monkeypatch.setattr(fill, "_call_runner", run)
    monkeypatch.setattr(fill, "match", match)
    monkeypatch.setattr(fill, "_RANKED", {"takes": lambda choice: True})
    return calls


def test_named_options_reach_request_while_chat_keeps_its_wire(monkeypatch, tmp_path):
    calls = _selection(monkeypatch)
    box = [-93.6729, 41.6625, -93.6369, 41.6895]
    m = case(tmp_path).modflow_water_table(bed={"source": "fetch_dem", "bbox": box})
    row = m._workflow.input("bed")
    for named in (None, m._named()):
        state = fill.Fill(m._workflow, model=named)
        env = fill.production(state)
        fill._stated(env, row, m._wire()["bed"])
        asyncio.run(fill._probe(env, row, "terrain", "bed terrain"))
    assert calls == [("fetch_dem", {"purpose": "bed"}),
                     ("fetch_dem", {"purpose": "bed", "bbox": box})]


def test_form_keeps_the_whole_ordered_bed_walk(monkeypatch, tmp_path):
    calls = _selection(monkeypatch)
    m = case(tmp_path).modflow_water_table()
    state = fill.Fill(m._workflow, model=m._named())
    env = fill.production(state)
    row = Input(BED).need("terrain").steps("merge_surfaces")
    row = fill.dataclasses.replace(row, name="bed")

    async def ingest(laid, **kwargs):
        if isinstance(laid, str):
            raise UserInputError("Isolated coverage shortfall.", code="BED_WET_HOLE")
        return laid

    assert asyncio.run(fill._bed_surface(env, row, ingest)) == ["fetch_dem", "fetch_etopo"]
    choices = _form(state).choices["bed"]
    assert [(c.source, c.rank, c.candidate_count, c.options) for c in choices] == [
        ("fetch_dem", 1, 2, calls[0][1]), ("fetch_etopo", 2, 2, calls[1][1])]
