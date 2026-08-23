"""Declarative library v1: value types, doors, validator, interpreter, ledger.

Offline only - every runner here is a local stub; no solve, no network.
"""
from __future__ import annotations

import pytest

from trid3nt_server.declarative import (
    Build,
    CoversAOI,
    Data,
    Domain,
    DrawGate,
    Fetch,
    FormGate,
    ModifierIllegalError,
    Param,
    PlanValidationError,
    Ref,
    StepFailedError,
    Step,
    When,
    Within,
    Workflow,
    doors,
    interpret,
    invocation_key,
    render_docstring,
    resolve_params,
    validate_plan,
)

_HERE = "tests.test_declarative_library"


# --- stub runners the plans below name by dotted path ----------------------- #
_CALLS: list[str] = []
_FAIL_AT: set[str] = set()


async def stub_step(**kwargs):
    _CALLS.append("stub_step")
    if "stub_step" in _FAIL_AT:
        raise RuntimeError("boom")
    return {"uri": "s3://b/k.tif", "value": kwargs}


async def stub_second(**kwargs):
    _CALLS.append("stub_second")
    if "stub_second" in _FAIL_AT:
        raise RuntimeError("boom-2")
    return {"uri": "s3://b/k2.tif", "seen": kwargs}


async def stub_producer(**kwargs):
    _CALLS.append("stub_producer")
    return "s3://b/produced.tif"


def stub_chart(*, result, params):
    _CALLS.append("stub_chart")
    return {"chart_id": "c1", "title": "t"}


def derive_double(params):
    return float(params.base) * 2.0


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    # The ledger writes through the real file-persistence store; keep every test's
    # writes inside its own tmp dir so the suite never touches ~/.trid3nt.
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    _CALLS.clear()
    _FAIL_AT.clear()
    yield


# --- Param declarations ------------------------------------------------------ #
def test_derived_param_needs_a_resolve_path():
    with pytest.raises(PlanValidationError):
        Param("x", desc="d", door=doors.DERIVED)


def test_scenario_param_needs_a_labeled_default():
    with pytest.raises(PlanValidationError):
        Param("x", desc="d", door=doors.SCENARIO)


def test_inverted_bounds_refused():
    with pytest.raises(PlanValidationError):
        Param("x", desc="d", default=1.0, bounds=(9.0, 1.0))


# --- the doors --------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_supplied_beats_default_and_question():
    p = await resolve_params(
        [Param("a", desc="d", door=doors.SCENARIO, default=1.0)],
        {"a": 7.0}, question={"a": 3.0},
    )
    assert p.a == 7.0 and p.row("a").door == doors.USER


@pytest.mark.asyncio
async def test_question_beats_the_labeled_default():
    p = await resolve_params(
        [Param("a", desc="d", door=doors.SCENARIO, default=1.0)], {},
        question={"a": 3.0},
    )
    assert p.a == 3.0 and p.row("a").basis == "prompt_interpreted"


@pytest.mark.asyncio
async def test_bounds_clamp_leaves_a_provenance_note():
    p = await resolve_params(
        [Param("a", desc="d", door=doors.SCENARIO, default=1.0, bounds=(0.0, 5.0),
               units="m")],
        {"a": 99.0},
    )
    assert p.a == 5.0
    assert p.row("a").clamped_from == 99.0
    assert "CLAMPED" in p.row("a").note


@pytest.mark.asyncio
async def test_non_numeric_bounded_value_refuses_never_defaults():
    with pytest.raises(Exception) as exc:
        await resolve_params(
            [Param("a", desc="d", door=doors.SCENARIO, default=1.0, bounds=(0.0, 5.0))],
            {"a": "nonsense"},
        )
    assert "not a number" in str(exc.value)


@pytest.mark.asyncio
async def test_derivations_resolve_regardless_of_declaration_order():
    p = await resolve_params([
        Param("out", desc="d", door=doors.DERIVED, resolve=f"{_HERE}.derive_double"),
        Param("base", desc="d", door=doors.SCENARIO, default=4.0),
    ], {})
    assert p.out == 8.0 and p.row("out").basis == "derived"


@pytest.mark.asyncio
async def test_optional_absent_param_resolves_to_none():
    p = await resolve_params([Param("a", desc="d", door=doors.USER, optional=True)], {})
    assert p.a is None


# --- modifier legality ------------------------------------------------------- #
def test_reference_producer_has_no_byo():
    assert not hasattr(Fetch.tool("fetch_dem"), "byo")
    assert hasattr(Build.tool("build_mesh"), "byo")


def test_named_applies_once():
    step = Step(runner=f"{_HERE}.stub_step").named("a")
    with pytest.raises(ModifierIllegalError):
        step.named("b")


def test_gate_rejects_step_modifiers():
    for call in (lambda g: g.named("x"), lambda g: g.overrides_domain(),
                 lambda g: g.render(preset="p"), lambda g: g.chart("c", builder="b")):
        with pytest.raises(ModifierIllegalError):
            call(FormGate())


# --- the plan validator ------------------------------------------------------ #
def _params():
    return [Param("base", desc="d", door=doors.SCENARIO, default=1.0),
            Param("pt", desc="d", door=doors.USER, optional=True)]


def test_validator_refuses_a_ref_to_nothing():
    plan = Workflow("w")[Step(runner=f"{_HERE}.stub_step", kwargs={"x": Ref("nope")})]
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_refuses_a_forward_ref():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_step", kwargs={"x": Ref("later")}),
        Step(runner=f"{_HERE}.stub_second").named("later"),
    ]
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_accepts_a_backward_ref():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("first.uri")}),
    ]
    validate_plan(plan, _params())


def test_validator_refuses_two_steps_with_one_name():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_step").named("dup"),
        Step(runner=f"{_HERE}.stub_second").named("dup"),
    ]
    with pytest.raises(PlanValidationError, match="two steps"):
        validate_plan(plan, _params())


def test_validator_refuses_a_gate_after_the_consequential_step():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_step", consequential=True),
        FormGate(),
    ]
    with pytest.raises(PlanValidationError, match="dead gate"):
        validate_plan(plan, _params())


def test_validator_refuses_a_draw_gate_on_a_non_user_param():
    plan = Workflow("w")[DrawGate(param="base", geometry="point")]
    with pytest.raises(PlanValidationError, match="USER door"):
        validate_plan(plan, _params())


def test_validator_refuses_a_draw_gate_on_an_undeclared_param():
    plan = Workflow("w")[DrawGate(param="ghost", geometry="point")]
    with pytest.raises(PlanValidationError, match="undeclared param"):
        validate_plan(plan, _params())


def test_validator_refuses_a_second_form_gate():
    plan = Workflow("w")[FormGate(), FormGate()]
    with pytest.raises(PlanValidationError, match="more than one FormGate"):
        validate_plan(plan, _params())


def test_validator_checks_within_constraint_refs():
    plan = Workflow("w")[
        DrawGate(param="pt", geometry="point", constrain=Within(Ref("ghost")))
    ]
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_checks_data_producer_refs():
    with pytest.raises(PlanValidationError, match="neither"):
        validate_plan(Workflow("w")[Step(runner=f"{_HERE}.stub_step")], _params(),
                      [Data("m", Build.tool("b", size=Ref("ghost")))])


# --- plan construction is pure ------------------------------------------------ #
def test_plan_construction_executes_nothing():
    Workflow("w")[
        Step(runner=f"{_HERE}.stub_step").named("a"),
        When(False, Step(runner=f"{_HERE}.stub_second")),
    ]
    assert _CALLS == []


def test_untaken_branch_is_dropped_from_execution_but_kept_for_inspection():
    plan = Workflow("w")[When(False, Step(runner=f"{_HERE}.stub_second"))]
    assert plan.flat() == ()
    assert len(plan.declared()) == 1


# --- the interpreter ---------------------------------------------------------- #
async def _run(plan, params_decl, supplied, data=(), **kw):
    p = await resolve_params(params_decl, supplied)
    return await interpret(plan, p, params_decl, data, **kw)


@pytest.mark.asyncio
async def test_interpreter_runs_steps_and_binds_refs():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("first.uri")}),
    ]
    out = await _run(plan, _params(), {}, resume=False)
    assert _CALLS == ["stub_step", "stub_second"]
    assert out.value["seen"]["x"] == "s3://b/k.tif"


@pytest.mark.asyncio
async def test_chart_modifier_runs_as_its_own_node():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=f"{_HERE}.stub_chart"),
    ]
    out = await _run(plan, _params(), {}, resume=False)
    assert _CALLS == ["stub_step", "stub_chart"]
    assert out.executed == ["a", "a.chart:c"]


@pytest.mark.asyncio
async def test_step_failure_is_typed_and_keeps_the_cause():
    _FAIL_AT.add("stub_step")
    plan = Workflow("w")[Step(runner=f"{_HERE}.stub_step").named("a")]
    with pytest.raises(StepFailedError) as exc:
        await _run(plan, _params(), {}, resume=False)
    assert exc.value.step == "a" and isinstance(exc.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_draw_gate_refuses_typed_in_auto_when_the_param_is_required():
    decl = [Param("pt", desc="where it enters", door=doors.USER)]
    plan = Workflow("w")[DrawGate(param="pt", geometry="point", prompt="click it")]
    with pytest.raises(Exception, match="never invented"):
        await _run(plan, decl, {"pt": None}, resume=False)


@pytest.mark.asyncio
async def test_draw_gate_names_wave_two_in_user_gated_mode():
    decl = [Param("pt", desc="where it enters", door=doors.USER)]
    plan = Workflow("w")[DrawGate(param="pt", geometry="point")]
    with pytest.raises(Exception, match="wave 2"):
        await _run(plan, decl, {"pt": None}, input_mode="user_gated", resume=False)


@pytest.mark.asyncio
async def test_required_param_with_no_gate_refuses_at_the_consequential_step():
    decl = [Param("needed", desc="a real value", door=doors.USER)]
    plan = Workflow("w")[Step(runner=f"{_HERE}.stub_step", consequential=True)]
    with pytest.raises(Exception, match="never invented"):
        await _run(plan, decl, {}, resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_data_producer_runs_lazily_on_first_ref():
    decl = _params()
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}),
    ]
    out = await _run(plan, decl, {}, data, resume=False)
    assert _CALLS == ["stub_producer", "stub_second"]
    assert out.value["seen"]["m"] == "s3://b/produced.tif"


@pytest.mark.asyncio
async def test_byo_artifact_short_circuits_the_producer():
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer").byo("s3://mine/m.slf",
                                                                  validate=None))]
    plan = Workflow("w")[Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")})]
    out = await _run(plan, _params(), {}, data, resume=False)
    assert _CALLS == ["stub_second"]
    assert out.value["seen"]["m"] == "s3://mine/m.slf"


@pytest.mark.asyncio
async def test_byo_coverage_validation_refuses_without_a_domain():
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer").byo("s3://mine/m.slf",
                                                                  validate=CoversAOI))]
    plan = Workflow("w")[Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")})]
    with pytest.raises(Exception, match="coverage-validated"):
        await _run(plan, _params(), {}, data, resume=False)


@pytest.mark.asyncio
async def test_overrides_domain_rebinds_the_environment():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_refine").named("clip").overrides_domain(),
    ]
    out = await _run(plan, _params(), {}, resume=False,
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert out.domain is not None and out.domain.bbox == (10.0, 10.0, 11.0, 11.0)


async def stub_refine(**kwargs):
    return {"bbox": [10.0, 10.0, 11.0, 11.0], "name": "refined"}


# --- the ledger + resume ------------------------------------------------------ #
@pytest.mark.asyncio
async def test_rerun_replays_completed_steps_and_resumes_at_the_failure():
    plan = Workflow("resume_w")[
        Step(runner=f"{_HERE}.stub_step").named("expensive"),
        Step(runner=f"{_HERE}.stub_second").named("cheap"),
    ]
    _FAIL_AT.add("stub_second")
    with pytest.raises(StepFailedError):
        await _run(plan, _params(), {"base": 2.0})
    assert _CALLS == ["stub_step", "stub_second"]

    _CALLS.clear()
    _FAIL_AT.clear()
    out = await _run(plan, _params(), {"base": 2.0})
    assert _CALLS == ["stub_second"]              # the expensive step did NOT re-run
    assert out.replayed == ["expensive"]
    assert out.executed == ["cheap"]


@pytest.mark.asyncio
async def test_restart_clean_ignores_the_ledger():
    plan = Workflow("clean_w")[Step(runner=f"{_HERE}.stub_step").named("a")]
    await _run(plan, _params(), {"base": 3.0})
    _CALLS.clear()
    out = await _run(plan, _params(), {"base": 3.0}, resume=False)
    assert _CALLS == ["stub_step"] and out.replayed == []


def test_invocation_key_is_stable_and_param_sensitive():
    assert invocation_key("w", {"a": 1}) == invocation_key("w", {"a": 1})
    assert invocation_key("w", {"a": 1}) != invocation_key("w", {"a": 2})


# --- generated docstring ------------------------------------------------------ #
def test_docstring_front_loads_routing_within_the_truncation_budget():
    doc = render_docstring(
        summary="S.", routing="R " * 100, params=_params(), returns="a layer",
    )
    assert doc.index("\nParams:") < 1000
    assert doc.startswith("S.")


def test_docstring_refuses_a_routing_block_over_the_budget():
    with pytest.raises(ValueError, match="truncation budget"):
        render_docstring(summary="S.", routing="x" * 1200, params=_params(),
                         returns="a layer")


def test_docstring_reports_bounds_units_and_labeled_defaults():
    doc = render_docstring(
        summary="S.", routing="R.",
        params=[Param("a", desc="the knob", door=doors.SCENARIO, default=2.0,
                      bounds=(1.0, 3.0), units="mg/L")],
        returns="a layer")
    assert "mg/L" in doc and "range 1-3" in doc
    assert "labeled scenario default" in doc
