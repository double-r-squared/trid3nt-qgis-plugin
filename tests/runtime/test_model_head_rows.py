"""The plume template's water table and thickness: one source per run, the model's only where it holds the whole domain.

Offline. The match, the probe and the either-or run as the fill runs them; only the place, the ask and the fetchers
are stubbed, so what is proved is which source answers each row: a model grid's elevation and thickness together,
Zell-Sanford's depth and thickness elsewhere, a stated value over either."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router.errors import router_empty_error
from trid3nt_server.tools.search.match import Need
from trid3nt_server.workflows.runtime import fill

_CAPE_COD = (-70.56, 41.635)
_NOWHERE = (-100.0, 35.0)
_NAMES = ("water_table", "water_table_depth", "saturated_thickness")


def _rows(template="modflow_plume"):
    workflow = TOOL_REGISTRY[template].fn.workflow
    return {row.name: row for row in workflow.inputs if row.name in _NAMES}


@pytest.fixture
def world(monkeypatch):
    """The place of the run and what each fetcher does; ``called`` is every fetcher asked, in order."""
    state = {"at": _CAPE_COD, "empty": set(), "called": []}

    async def need(env, decl, data_class, label):
        return Need(slot=label, data_class=data_class, of=decl.observes, lon=state["at"][0], lat=state["at"][1],
                    frame="NAVD88", mesh_m=100.0)

    async def ask(env, choice, decl):
        return {"bbox": [state["at"][0] - 0.01, state["at"][1] - 0.01, state["at"][0] + 0.01, state["at"][1] + 0.01]}

    async def run(runner, kwargs, label):
        state["called"].append(runner)
        if runner in state["empty"]:
            raise router_empty_error("USGS_MODEL", "the model measures part of the box", "EMPTY")
        return {"from": runner}

    async def nowhere(env):
        return None

    async def beside(env, decl):
        return {}

    monkeypatch.setattr(fill, "_need", need)
    monkeypatch.setattr(fill, "_ask_for", ask)
    monkeypatch.setattr(fill, "_call_runner", run)
    monkeypatch.setattr(fill, "_somewhere_to_ask", nowhere)
    monkeypatch.setattr(fill, "_beside", beside)
    return state


def _read(env, *names):
    token = bind_notes()
    held = {name: asyncio.run(fill.read(env, name)) for name in names}
    return held, drain_notes(token)


def _source(held, name):
    return held[name]["from"] if held[name] is not None else None


def test_inside_a_model_grid_the_water_table_elevation_and_the_model_thickness_answer_and_no_depth(world):
    held, said = _read(fill._Env(data=_rows()), "water_table", "saturated_thickness", "water_table_depth")
    assert _source(held, "water_table") == "fetch_usgs_model_water_table"
    assert _source(held, "saturated_thickness") == "fetch_usgs_model_thickness"
    assert held["water_table_depth"] is None
    assert "fetch_water_table_depth" not in world["called"]
    assert any("water_table_depth is not asked for" in line and "water_table fills" in line for line in said)


def test_outside_every_grid_the_zell_sanford_depth_and_thickness_answer_as_before(world):
    world["at"] = _NOWHERE
    held, _ = _read(fill._Env(data=_rows()), "water_table", "saturated_thickness", "water_table_depth")
    assert held["water_table"] is None
    assert _source(held, "water_table_depth") == "fetch_water_table_depth"
    assert _source(held, "saturated_thickness") == "fetch_aquifer_thickness"
    assert not any(name.startswith("fetch_usgs_model") for name in world["called"])


def test_a_box_the_model_does_not_hold_whole_falls_back_to_zell_sanford_for_both(world):
    """The centre lies in the grid, the model rows answer EMPTY (the box straddles its edge, or holds an unmeasured cell)."""
    world["empty"] = {"fetch_usgs_model_water_table", "fetch_usgs_model_thickness"}
    held, said = _read(fill._Env(data=_rows()), "water_table", "saturated_thickness", "water_table_depth")
    assert held["water_table"] is None
    assert _source(held, "water_table_depth") == "fetch_water_table_depth"
    assert _source(held, "saturated_thickness") == "fetch_aquifer_thickness"
    assert any("fetch_usgs_model_thickness held nothing, so fetch_aquifer_thickness fills" in line for line in said)
    assert any("fetch_usgs_model_water_table held nothing" in line for line in said)


def test_a_stated_depth_wins_and_the_model_water_table_is_not_read(world):
    env = fill._Env(data=_rows())
    env.supplied["water_table_depth"] = 4.0
    held, said = _read(env, "water_table", "water_table_depth", "saturated_thickness")
    assert held["water_table"] is None and held["water_table_depth"] == 4.0
    assert "fetch_usgs_model_water_table" not in world["called"]
    assert any("water_table is not read" in line and "water_table_depth" in line for line in said)


def test_a_stated_water_table_elevation_wins_and_the_depth_is_not_asked_for(world):
    env = fill._Env(data=_rows())
    env.supplied["water_table"] = 12.5
    held, _ = _read(env, "water_table_depth", "water_table")
    assert held["water_table"] == 12.5 and held["water_table_depth"] is None
    assert world["called"] == []


@pytest.mark.parametrize("template", ["modflow_plume", "modflow_water_table"])
def test_both_templates_declare_the_either_or(template):
    rows = _rows(template)
    water_table = rows["water_table"]
    assert water_table.optional and water_table.instead_of == "water_table_depth"
    assert water_table.observes == "water_table_elevation" and water_table.data_class == "groundwater level"
    assert not rows["water_table_depth"].optional
    assert rows["water_table_depth"].observes == "water_table_depth"
