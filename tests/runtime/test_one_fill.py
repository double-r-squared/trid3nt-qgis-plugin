"""The one fill: every input answers on arrival, and READY is what launch reads."""

import asyncio
import sys
import types

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime.fill import (
    ACCEPTED, DEFAULTED, MISSING, REJECTED, Fill, fill, refuse_other_mesh)
from trid3nt_server.workflows.runtime.errors import ContinuationRefused
from trid3nt_server.workflows.telemac.modules import module as module_mod
from trid3nt_server.workflows.telemac.modules import wrapper_for


def _wf():
    return TOOL_REGISTRY["telemac_dye_release"].fn.workflow


def _filled(values):
    return asyncio.run(fill(Fill(workflow=_wf()), values))


def test_a_stated_value_is_accepted_and_the_rest_default():
    state = _filled({"location": "the Wabash River", "cores": 2})
    assert state.inputs["cores"].state == ACCEPTED
    assert state.inputs["cores"].value == 2
    assert any(v.state == DEFAULTED for v in state.inputs.values())


def test_a_value_outside_its_range_is_rejected_with_its_remedy():
    state = _filled({"location": "the Wabash River", "cores": 999})
    verdict = state.inputs["cores"]
    assert verdict.state == REJECTED and "cores" in verdict.reason
    assert verdict.remedies and not state.ready


def test_a_required_input_nothing_filled_is_missing_and_not_ready():
    from trid3nt_server.workflows.runtime import Param

    wf = types.SimpleNamespace(params=(Param(name="depth", desc="d", door="user", type=float),),
                               coercions=(), data=())
    state = asyncio.run(fill(Fill(workflow=wf), {}))
    assert state.inputs["depth"].state == MISSING and not state.ready
    state = asyncio.run(fill(state, {"depth": 3.0}))
    assert state.inputs["depth"].state == ACCEPTED and state.ready


def test_a_sourced_input_is_fetched_before_it_answers(monkeypatch):
    asked = []

    async def _produce(env, decl):
        asked.append((decl.name, env.picks.get(decl.name)))
        return {"fetched": env.picks.get(decl.name)}

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    decl = next(d for d in wf.data if d.data_class)
    state = asyncio.run(fill(Fill(workflow=wf), {"location": "the Wabash River",
                                                  decl.name: {"source": "stub"}}))
    assert (decl.name, "stub") in asked
    assert state.inputs[decl.name].state == ACCEPTED
    assert state.inputs[decl.name].origin == "source:stub"


def test_a_layer_by_id_is_supplied_and_ingested_before_it_answers(monkeypatch):
    seen = []

    async def _produce(env, decl):
        seen.append(env.supplied.get(decl.name))
        return "ingested"

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    decl = next(d for d in wf.data if d.data_class)
    state = asyncio.run(fill(Fill(workflow=wf), {"location": "x",
                                                  decl.name: {"layer": "L1"}}))
    assert "L1" in seen and state.inputs[decl.name].value == "ingested"


def test_a_keyword_is_held_to_the_engine_class_where_it_imports(monkeypatch):
    calls = []
    monkeypatch.setattr(module_mod, "engine_check",
                        lambda module: lambda k, v: calls.append(k) or
                        ("not among the choices" if v == 99 else ""))
    t2d = wrapper_for("telemac2d")
    assert module_mod.accept(t2d, "LAW OF BOTTOM FRICTION", 4)[2] == ""
    with pytest.raises(module_mod.SlotRefused, match="refused by the engine"):
        module_mod.accept(t2d, "LAW OF BOTTOM FRICTION", 99)
    assert calls


def test_an_absent_engine_tree_is_stated_and_ours_judges(monkeypatch):
    monkeypatch.delenv("HOMETEL", raising=False)
    t2d = wrapper_for("telemac2d")
    assert module_mod.accept(t2d, "LAW OF BOTTOM FRICTION", 4)[2] == \
        module_mod.ENGINE_UNAVAILABLE
    state = _filled({"location": "x",
                     "keywords": {"LAW OF BOTTOM FRICTION": 4}})
    assert module_mod.ENGINE_UNAVAILABLE in state.notes


def test_a_keyword_the_module_refuses_is_rejected_by_name():
    state = _filled({"location": "x", "keywords": {"LAW OF BOTTOM FRICTON": 4}})
    assert state.inputs["LAW OF BOTTOM FRICTON"].state == REJECTED
    assert not state.ready


def _journal(monkeypatch, line):
    from trid3nt_server.workflows.runtime import journal

    monkeypatch.setattr(journal, "read_records", lambda: [line])


def test_a_continuation_of_the_same_module_is_accepted(monkeypatch):
    _journal(monkeypatch, {"run_id": "R1", "solved": "s3://x/r.slf",
                           "module": _wf().module_name, "mesh": {"key": "K"}})
    state = _filled({"location": "x", "continue_from": "R1"})
    assert state.inputs["continue_from"].state == ACCEPTED
    refuse_other_mesh(state.continued, "K")


def test_a_continuation_of_another_module_is_refused(monkeypatch):
    _journal(monkeypatch, {"run_id": "R1", "solved": "s3://x/r.slf",
                           "module": "tomawac", "mesh": {"key": "K"}})
    state = _filled({"location": "x", "continue_from": "R1"})
    assert state.inputs["continue_from"].state == REJECTED
    assert "tomawac" in state.inputs["continue_from"].reason


def test_a_continuation_on_another_mesh_is_refused():
    with pytest.raises(ContinuationRefused, match="another mesh"):
        refuse_other_mesh({"run_id": "R1", "mesh": {"key": "K"}}, "OTHER")


def test_a_card_edit_refills_through_the_modules_accept_rule(monkeypatch):
    seen = []
    real = module_mod.accept

    def _spy(body, name, value):
        seen.append(name)
        return real(body, name, value)

    from trid3nt_server.workflows.telemac import workflow as tw

    monkeypatch.setattr(tw, "accept", _spy)
    monkeypatch.setattr(tw, "card_rows", lambda sheet: [])
    steering = _wf().steering

    async def _fill(values, edits):
        return types.SimpleNamespace(values=dict(values), edits=dict(edits),
                                     filled={}, files={})

    async def _gate(*, apply_revision, present, **_):
        await apply_revision({"LAW OF BOTTOM FRICTION": 4})
        return types.SimpleNamespace(proceed=True)

    monkeypatch.setitem(sys.modules["trid3nt_server.gates.input_review"].__dict__,
                        "gate_input_review", _gate)
    sheet = asyncio.run(tw._review(_fill, {}, steering=steering, workflow="w",
                                   title="", input_mode="user_gated", spent=()))
    assert seen == ["LAW OF BOTTOM FRICTION"]
    assert sheet.edits == {"LAW OF BOTTOM FRICTION": 4}
