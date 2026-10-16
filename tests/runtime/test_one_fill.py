"""The one fill: every input answers on arrival, and READY is what launch reads."""

import asyncio
import sys
import types

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime.fill import (
    ACCEPTED,
    DEFAULTED,
    MISSING,
    REJECTED,
    Fill,
    fill,
)
from trid3nt_server.workflows.runtime.module import (
    ENGINE_UNAVAILABLE,
    SlotRefused,
    accept,
)
from trid3nt_server.workflows.telemac.modules import (
    module as module_mod,
)
from trid3nt_server.workflows.telemac.modules import wrapper_for


def _wf():
    return TOOL_REGISTRY["telemac_dye_release"].fn.workflow


#: What a dye run states: the moment it opens at, and the one value its template
#: cannot stand without - the release.
_DYE = {"release": [0.5, 0.5], "event_time": "2024-06-01T00:00:00Z"}


def _filled(values):
    return asyncio.run(fill(Fill(workflow=_wf()), values))


def _skeleton(*inputs):
    from trid3nt_server.workflows.runtime import Workflow

    return Workflow(metadata=types.SimpleNamespace(name="probe", engine=""),
                    inputs=inputs, template=types.SimpleNamespace())


def test_one_loop_answers_a_value_a_keyword_and_a_row_with_one_origin_stamp(
        monkeypatch):
    """Every kind of input comes off the workflow's one lookup and leaves one
    verdict: the value taken, who stated it and the source that names it."""
    from trid3nt_contracts.common import Origin

    async def _produce(env, decl):
        env.sources[decl.name] = "fetch_stub"
        return "held"

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    monkeypatch.delenv("HOMETEL", raising=False)
    wf = _wf()
    row = next(d for d in wf.inputs if d.data_class)
    state = asyncio.run(fill(Fill(workflow=wf), {
        "spill_fraction": 0.4, row.name: {"source": "fetch_stub"},
        "keywords": {"LAW OF BOTTOM FRICTION": 4}}))
    stated, keyword, sourced = (state.inputs[n] for n in (
        "spill_fraction", "LAW OF BOTTOM FRICTION", row.name))
    assert (stated.value, stated.origin) == (0.4, Origin.USER)
    assert (keyword.value, keyword.origin) == (4, Origin.USER)
    assert (sourced.origin, sourced.source) == (Origin.PRODUCER, "fetch_stub")
    assert state.keywords == {"LAW OF BOTTOM FRICTION": 4}
    defaulted = next(v for v in state.inputs.values() if v.state == DEFAULTED)
    assert defaulted.origin is Origin.TEMPLATE
    for gone in ("_seat", "_keyword"):
        assert not hasattr(fill_mod, gone)


def test_a_keyword_is_answered_as_an_input_is_through_the_one_lookup():
    """The lookup hands back the engine's own keyword after the body's inputs: its
    name, the one range that bounds it and the module's accept rule."""
    held = _wf().input("LAW OF BOTTOM FRICTION")
    assert held.name == "LAW_OF_BOTTOM_FRICTION" and not held.is_row
    assert held.bounds == held.body.MODULE_INPUT[held.name].whole_bounds
    assert held.carried and held.accept(4) == 4
    assert _wf().input("spill_fraction").bounds == (0.05, 0.9)


@pytest.mark.parametrize("name", ["wind", "sources", "LAW OF BOTTOM FRICTION",
                                  "LAW_OF_BOTTOM_FRICTION"])
def test_beside_keywords_only_a_declared_input_is_taken(name):
    """A composite, a keyword or its identifier stated as an argument is not an
    input of the tool: it refuses by name and says where a keyword is stated."""
    state = _filled({name: 1})
    verdict = state.inputs[name]
    assert (verdict.state, verdict.code) == (REJECTED, "INPUT_UNNAMED")
    assert f"{name!r} is not an input of telemac_dye_release" in verdict.reason
    assert "inside keywords" in verdict.reason and not state.ready


def test_inside_keywords_a_declared_input_refuses_naming_where_it_belongs():
    state = _filled({"keywords": {"spill_fraction": 0.4}})
    verdict = state.inputs["spill_fraction"]
    assert (verdict.state, verdict.code) == (REJECTED, "KEYWORD_REFUSED")
    assert "beside keywords" in verdict.reason and not state.ready


def test_a_misspelled_keyword_names_the_nearest_spelling():
    verdict = _filled({"keywords": {"LAW OF BOTTOM FRICTON": 4}}).inputs[
        "LAW OF BOTTOM FRICTON"]
    assert verdict.code == "KEYWORD_REFUSED"
    assert "LAW OF BOTTOM FRICTION" in verdict.reason


def test_a_failed_launch_does_not_say_the_engine_check_was_out_of_reach(monkeypatch):
    """The note rides a run that launches; a launch that raises reads its own
    error and nothing about the fill's check."""
    monkeypatch.delenv("HOMETEL", raising=False)

    async def _launch(self, state):
        raise SlotRefused("the deck refused")

    async def _produce(env, decl):
        return decl.name

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    monkeypatch.setattr(type(wf), "launch", _launch)
    out = asyncio.run(wf.run({**_DYE, "input_mode": "auto",
                              "keywords": {"LAW OF BOTTOM FRICTION": 4}}))
    assert out["status"] == "error" and "the deck refused" in out["error_message"]
    assert ENGINE_UNAVAILABLE not in out["error_message"]


def test_a_stated_value_is_accepted_and_the_rest_default():
    state = _filled({"spill_fraction": 0.4})
    assert state.inputs["spill_fraction"].state == ACCEPTED
    assert state.inputs["spill_fraction"].value == 0.4
    assert any(v.state == DEFAULTED for v in state.inputs.values())


def test_a_value_outside_its_range_is_rejected_with_its_remedy():
    state = _filled({"spill_fraction": 9.0})
    verdict = state.inputs["spill_fraction"]
    assert verdict.state == REJECTED and "spill_fraction" in verdict.reason
    assert verdict.remedies and not state.ready


def test_a_required_input_nothing_filled_is_missing_and_not_ready():
    from trid3nt_server.inputs.input import Input

    wf = _skeleton(Input(name="depth", desc="d", type=float))
    state = asyncio.run(fill(Fill(workflow=wf), {}))
    assert state.inputs["depth"].state == MISSING and not state.ready
    state = asyncio.run(fill(state, {"depth": 3.0}))
    assert state.inputs["depth"].state == ACCEPTED and state.ready


@pytest.mark.parametrize("mode", ["auto", "user_gated"])
def test_a_required_param_left_unstated_refuses_in_every_mode(mode):
    """No default and not optional is the whole floor: the fill refuses before
    any launch, and no mode shows the input to anybody to approve instead."""
    from trid3nt_server.inputs.input import Input

    wf = _skeleton(Input(name="wave_height_m", desc="d", bounds=(0.01, 10.0)),
                   Input(name="grade", desc="d", default=0.2, bounds=(0.05, 0.5)))
    state = asyncio.run(fill(Fill(workflow=wf), {"input_mode": mode}))
    assert state.inputs["wave_height_m"].state == MISSING and not state.ready
    code, sentence = state.refusal()
    assert code == "GATE_REFUSED"
    assert "wave_height_m was not supplied and has no default" in sentence


def test_a_sourced_input_is_fetched_before_it_answers(monkeypatch):
    asked = []

    async def _produce(env, decl):
        asked.append((decl.name, env.picks.get(decl.name)))
        return {"fetched": env.picks.get(decl.name)}

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    decl = next(d for d in wf.inputs if d.data_class)
    state = asyncio.run(fill(Fill(workflow=wf), {decl.name: {"source": "stub"}}))
    assert (decl.name, "stub") in asked
    assert state.inputs[decl.name].state == ACCEPTED


def test_a_stated_row_the_template_names_is_produced_once_and_a_refused_one_stops_the_rest(
        monkeypatch):
    """A row the caller states AND the template names is read once, and a stated
    row that is refused leaves the rows the template names unasked."""
    asked = []

    async def _produce(env, decl):
        asked.append(decl.name)
        if decl.name == refused:
            raise fill_mod.StepFailedError("held nothing", error_code="STUB")
        return "held"

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = TOOL_REGISTRY["telemac_channel_dredging"].fn.workflow
    assert wf.named_rows() == ("domain", "dredge_area", "dump_area", "bed", "level",
                               "discharge", "line")
    refused = None
    stated = {"dredge_area": "L1", "bed": 3.0, "input_mode": "auto",
              "seed_point": [0.5, 0.5], "event_time": "2024-06-01T00:00:00Z"}
    state = asyncio.run(fill(Fill(workflow=wf), stated))
    assert state.ready and sorted(asked) == [
        "bed", "discharge", "domain", "dredge_area", "dump_area", "level", "line"]
    asked.clear()
    refused = "bed"
    state = asyncio.run(fill(Fill(workflow=wf), stated))
    assert state.inputs["bed"].code == "STUB" and "dump_area" not in asked
    assert asked.count("dredge_area") == 1


def test_a_user_gated_engine_run_with_no_session_is_refused_at_the_fill_and_fetches_nothing(
        monkeypatch):
    asked = []

    async def _produce(env, decl):
        asked.append(decl.name)
        return "held"

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    state = asyncio.run(fill(Fill(workflow=_wf()), {}))
    refused = state.inputs["input_mode"]
    assert refused.code == "NO_SESSION" and "input_mode='auto'" in refused.reason
    assert not state.ready and asked == []
    state = asyncio.run(fill(Fill(workflow=_wf()),
                             {**_DYE, "input_mode": "auto"}))
    assert "input_mode" not in state.inputs and "domain" in asked


def test_a_layer_by_id_is_supplied_and_ingested_before_it_answers(monkeypatch):
    seen = []

    async def _produce(env, decl):
        seen.append(env.supplied.get(decl.name))
        return "ingested"

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    decl = next(d for d in wf.inputs if d.data_class)
    state = asyncio.run(fill(Fill(workflow=wf), {decl.name: {"layer": "L1"}}))
    assert "L1" in seen and state.inputs[decl.name].value == "ingested"


def test_a_keyword_is_held_to_the_engine_class_where_it_imports(monkeypatch):
    calls = []
    monkeypatch.setattr(module_mod, "engine_check",
                        lambda module: lambda k, v: calls.append(k) or
                        ("not among the choices" if v == 99 else ""))
    t2d = wrapper_for("telemac2d")
    assert accept(t2d, "LAW OF BOTTOM FRICTION", 4)[2] == ""
    with pytest.raises(SlotRefused, match="refused by the engine"):
        accept(t2d, "LAW OF BOTTOM FRICTION", 99)
    assert calls


def test_an_absent_engine_tree_is_stated_and_ours_judges(monkeypatch):
    monkeypatch.delenv("HOMETEL", raising=False)
    t2d = wrapper_for("telemac2d")
    assert accept(t2d, "LAW OF BOTTOM FRICTION", 4)[2] == ENGINE_UNAVAILABLE
    state = _filled({"keywords": {"LAW OF BOTTOM FRICTION": 4}})
    assert state.notes == [ENGINE_UNAVAILABLE]
    assert ENGINE_UNAVAILABLE not in state.said[0]


def test_a_keyword_the_module_refuses_is_rejected_by_name():
    state = _filled({"keywords": {"LAW OF BOTTOM FRICTON": 4}})
    assert state.inputs["LAW OF BOTTOM FRICTON"].state == REJECTED
    assert not state.ready


def _journal(monkeypatch, line):
    from trid3nt_server.workflows.runtime import journal

    monkeypatch.setattr(journal, "read_records", lambda: [line])


def test_a_card_edit_refills_through_the_modules_accept_rule(monkeypatch):
    seen = []
    real = accept

    def _spy(body, name, value):
        seen.append(name)
        return real(body, name, value)

    from trid3nt_server.workflows.runtime import review
    from trid3nt_server.workflows.telemac import workflow as tw

    monkeypatch.setattr(review, "accept", _spy)
    monkeypatch.setattr(review, "card_rows", lambda sheet: [])
    steering = _wf().steering

    async def _fill(values, edits):
        return types.SimpleNamespace(values=dict(values), edits=dict(edits),
                                     filled={}, files={})

    async def _gate(*, apply_revision, present, **_):
        await apply_revision({"LAW OF BOTTOM FRICTION": 4})
        return types.SimpleNamespace(proceed=True)

    monkeypatch.setitem(sys.modules["trid3nt_server.inputs.gate.input_review"].__dict__,
                        "gate_input_review", _gate)
    sheet = asyncio.run(tw._review(_fill, {}, steering=steering, workflow="w",
                                   title="", input_mode="user_gated", spent=()))
    assert seen == ["LAW OF BOTTOM FRICTION"]
    assert sheet.edits == {"LAW OF BOTTOM FRICTION": 4}


def _place_registers_a_row(monkeypatch):
    from trid3nt_server.inputs.slots import DOMAIN

    async def _produce(env, decl):
        if decl.slot == DOMAIN:
            fill_mod._runtime_row(env, f"{decl.name}_datum_offset", "x", {})
        return "held"

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    decl = next(d for d in wf.inputs if d.data_class and d.slot != DOMAIN)
    assert any(d.slot == DOMAIN for d in wf.inputs)
    return wf, decl


def test_a_place_that_registers_a_row_while_it_runs_still_answers(monkeypatch):
    wf, decl = _place_registers_a_row(monkeypatch)
    state = asyncio.run(fill(Fill(workflow=wf), {decl.name: {"source": "stub"}}))
    assert state.inputs[decl.name].state == ACCEPTED, state.inputs[decl.name]


def test_an_untyped_fault_is_refused_and_its_trace_logged(monkeypatch, caplog):
    async def _produce(env, decl):
        raise KeyError("fault")

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = _wf()
    decl = next(d for d in wf.inputs if d.data_class)
    with caplog.at_level("WARNING", logger=fill_mod.logger.name):
        state = asyncio.run(fill(Fill(workflow=wf), {
            decl.name: {"layer": "L1"}}))
    assert state.inputs[decl.name].code == "INPUT_REFUSED"
    assert any(r.exc_info and r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize("cancel_code,code", [
    ("no_session", "NO_SESSION"),
    ("declined", "USER_INPUT_CANCELLED"),
])
def test_a_refusal_keeps_the_code_of_its_reason_through_the_review(
        monkeypatch, cancel_code, code):
    from trid3nt_server.errors import DeclarativeError
    from trid3nt_server.inputs.gate import input_review
    from trid3nt_server.inputs.gate.input_review import ReviewOutcome
    from trid3nt_server.workflows.runtime import review
    from trid3nt_server.workflows.telemac import workflow as tw

    monkeypatch.setattr(review, "card_rows", lambda sheet: [])

    async def _fill(values, edits):
        return types.SimpleNamespace(values=dict(values), edits=dict(edits),
                                     filled={}, files={})

    async def _gate(**kwargs):
        return ReviewOutcome(proceed=False, entries=[], params={}, cancelled=True,
                             cancel_reason="declined", cancel_code=cancel_code)

    monkeypatch.setitem(input_review.__dict__, "gate_input_review", _gate)
    with pytest.raises(DeclarativeError) as caught:
        asyncio.run(tw._review(_fill, {}, steering=_wf().steering, workflow="w",
                               title="", input_mode="auto", spent=()))
    assert caught.value.error_code == code


def test_what_producing_said_at_the_fill_is_restated_on_the_run(monkeypatch):
    """The fill runs before the run's channels open, so a station a row chose
    and what a cut covers reach the run only because the launch restates them."""
    from trid3nt_server.inputs import run_notes
    from trid3nt_server.workflows.runtime import journal
    from trid3nt_server.workflows.runtime.workflow import RunResult

    async def _produce(env, decl):
        run_notes.cut_coverage(f"the {decl.name} covers the cut")
        run_notes.journal_note(f"the {decl.name} came from a stub")
        journal.slot_choice(decl.name)
        return decl.name

    async def _launch(self, state):
        covered.extend(run_notes.run_coverage())
        return RunResult(value=None)

    covered = []
    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = TOOL_REGISTRY["telemac_ice_cover"].fn.workflow
    monkeypatch.setattr(type(wf), "launch", _launch)
    state = asyncio.run(fill(Fill(workflow=wf), {
        "input_mode": "auto", "seed": [0.5, 0.5], "station": [0.5, 0.5],
        "event_time": "2024-06-01T00:00:00Z"}))
    run = asyncio.run(wf._launched(state))
    rows = ["domain", "bed", "weather", "level", "discharge"]
    assert run.choices == rows
    assert covered == [f"the {name} covers the cut" for name in rows]
    assert run.notes[:10] == [said for name in rows for said in (
        f"the {name} covers the cut", f"the {name} came from a stub")]


_RING = [[-122.6769, 45.5153], [-122.6615, 45.5153],
         [-122.6615, 45.5198], [-122.6769, 45.5198]]
_STATED = {"structure": [[-122.6700, 45.5160], [-122.6700, 45.5190]],
           "domain": _RING, "bed": 8.0, "wave_height_m": 1.0,
           "reflection_coef": 0.5, "mesh_resolution_m": 35.0,
           "event_time": "2024-06-01T00:00:00Z"}


@pytest.mark.parametrize("order", [("structure", "domain"), ("domain", "structure")])
def test_the_order_inputs_are_stated_in_does_not_decide_the_fill(order):
    wf = TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow
    values = {name: _STATED[name] for name in order}
    values.update(_STATED, input_mode="auto")
    state = asyncio.run(fill(Fill(workflow=wf), values))
    assert state.ready, {n: v.reason for n, v in state.inputs.items()
                         if v.state == REJECTED}
