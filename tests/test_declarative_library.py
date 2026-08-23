"""Declarative library v1: value types, doors, validator, interpreter, ledger.

Offline only - every runner here is a local stub; no solve, no network.
"""
from __future__ import annotations

import importlib

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
    RunMode,
    StepFailedError,
    Step,
    When,
    Within,
    Workflow,
    doors,
    interpret,
    invocation_key,
    merge_provenance,
    provenance_entries,
    render_docstring,
    resolve_params,
    validate_plan,
)

_HERE = "tests.test_declarative_library"


# --- stub runners the plans below name by dotted path ----------------------- #
_CALLS: list[str] = []
_FAIL_AT: set[str] = set()
#: artifact URIs the fake object store does NOT hold (the dead-URI probe).
_MISSING_ARTIFACTS: set[str] = set()


class _RetryableGate(RuntimeError):
    """Stands in for the engines' retryable typed gates (banks/reach)."""

    retryable = True
    error_code = "STUB_GATE"

    def __init__(self, message="retry me"):
        super().__init__(message)
        self.suggestions = ["widen the reach", "pass banks explicitly"]


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


async def stub_gate_raiser(**kwargs):
    _CALLS.append("stub_gate_raiser")
    raise _RetryableGate()


def stub_chart(*, result, params):
    _CALLS.append("stub_chart")
    if "stub_chart" in _FAIL_AT:
        raise RuntimeError("chart-boom")
    return {"chart_id": "c1", "title": "t"}


def derive_double(params):
    return float(params.base) * 2.0


def derive_broken(params):
    _ = params.base
    return "nope".missing_attribute        # a real bug, not a dependency wait


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    # The ledger writes through the real file-persistence store; keep every test's
    # writes inside its own tmp dir so the suite never touches ~/.trid3nt.
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    # Offline: the stub runners' s3:// URIs have no object store behind them, so
    # the replay probe is answered from _MISSING_ARTIFACTS instead of boto3.
    # import_module, not `import ... as`: the package re-exports `interpret` the
    # FUNCTION, which shadows the submodule attribute of the same name.
    _interp = importlib.import_module("trid3nt_server.declarative.interpret")
    monkeypatch.setattr(_interp, "_artifact_exists",
                        lambda uri: uri not in _MISSING_ARTIFACTS)
    _CALLS.clear()
    _FAIL_AT.clear()
    _MISSING_ARTIFACTS.clear()
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
async def test_a_bool_is_refused_for_a_bounded_param():
    """bool IS an int, so True would coerce to 1.0 - a flag is not a measurement."""
    for flag in (True, False):
        with pytest.raises(Exception, match="not a number"):
            await resolve_params(
                [Param("a", desc="d", door=doors.SCENARIO, default=1.0,
                       bounds=(0.0, 5.0))],
                {"a": flag},
            )


@pytest.mark.asyncio
async def test_optional_absent_param_resolves_to_none():
    p = await resolve_params([Param("a", desc="d", door=doors.USER, optional=True)], {})
    assert p.a is None


@pytest.mark.asyncio
async def test_a_user_door_default_is_stamped_as_a_default_not_as_the_user():
    decl = [Param("a", desc="d", door=doors.USER, default=3.0)]
    p = await resolve_params(decl, {})
    assert p.a == 3.0 and p.row("a").basis == "default_demo"
    p2 = await resolve_params(decl, {"a": 4.0})
    assert p2.row("a").basis == "user"


@pytest.mark.asyncio
async def test_an_absent_param_with_a_derived_stand_in_still_leaves_a_row():
    decl = [Param("outfall", desc="where it enters", door=doors.USER, optional=True,
                  consequence="scenario",
                  derived_when_absent="seeded at the derived reach point")]
    p = await resolve_params(decl, {})
    rows = provenance_entries(p, decl)
    assert [r.param for r in rows] == ["outfall"]
    assert rows[0].basis == "derived"
    assert "derived reach point" in rows[0].note


@pytest.mark.asyncio
async def test_a_bug_inside_a_derivation_is_not_swallowed_as_a_dependency_wait():
    decl = [Param("out", desc="d", door=doors.DERIVED, resolve=f"{_HERE}.derive_broken"),
            Param("base", desc="d", door=doors.SCENARIO, default=4.0)]
    with pytest.raises(AttributeError, match="missing_attribute"):
        await resolve_params(decl, {})


def test_the_composites_own_provenance_row_wins():
    from trid3nt_contracts.common import SyntheticInput

    own = SyntheticInput(param="bank_source", value="nhd_area", basis="fetched",
                         consequence="physics", note="real NHDArea banks")
    declared = SyntheticInput(param="bank_source", value="nhd_area",
                              basis="default_demo", consequence="scenario",
                              note="declared constant default")
    other = SyntheticInput(param="k1", value=0.3, basis="default_demo",
                           consequence="numerical")
    merged = merge_provenance([own], [declared, other])
    assert [r.param for r in merged] == ["bank_source", "k1"]
    assert merged[0].basis == "fetched"


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


def test_validator_refuses_a_ref_into_an_untaken_branch():
    """A conditional step's name is not visible outside its branch - the branch
    may not be taken, and a runtime REF_UNRESOLVED is not a contract."""
    plan = Workflow("w")[
        When(False, Step(runner=f"{_HERE}.stub_step").named("maybe")),
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("maybe.uri")}),
    ]
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_accepts_a_ref_inside_the_same_branch():
    plan = Workflow("w")[
        When(True,
             Step(runner=f"{_HERE}.stub_step").named("here"),
             Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("here.uri")})),
    ]
    validate_plan(plan, _params())


def test_validator_refuses_a_param_declared_twice():
    decl = [Param("base", desc="d", door=doors.SCENARIO, default=1.0),
            Param("base", desc="other", door=doors.SCENARIO, default=2.0)]
    with pytest.raises(PlanValidationError, match="declared twice"):
        validate_plan(Workflow("w")[Step(runner=f"{_HERE}.stub_step")], decl)


@pytest.mark.asyncio
async def test_resolver_refuses_a_param_declared_twice():
    decl = [Param("base", desc="d", door=doors.SCENARIO, default=1.0),
            Param("base", desc="other", door=doors.SCENARIO, default=2.0)]
    with pytest.raises(PlanValidationError, match="declared twice"):
        await resolve_params(decl, {})


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


def test_a_nested_untaken_branch_is_dropped_too():
    """A When inside a taken When is still guarded by its OWN condition."""
    inner = Step(runner=f"{_HERE}.stub_second").named("inner")
    plan = Workflow("w")[When(True, When(False, inner))]
    assert plan.flat() == ()
    assert plan.declared() == (inner,)


def test_a_nested_taken_branch_still_runs():
    inner = Step(runner=f"{_HERE}.stub_second").named("inner")
    plan = Workflow("w")[When(True, When(True, inner))]
    assert plan.flat() == (inner,)


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
async def test_run_mode_binds_the_runs_input_gate_mode_into_a_step():
    plan = Workflow("mode_w")[
        Step(runner=f"{_HERE}.stub_second", kwargs={"input_mode": RunMode}),
    ]
    out = await _run(plan, _params(), {}, input_mode="user_gated", resume=False)
    assert out.value["seen"]["input_mode"] == "user_gated"


@pytest.mark.asyncio
async def test_data_producers_run_after_the_plans_gates():
    """A producer that fetched BEFORE the gate fetched against params the gate
    exists to change."""
    decl = [Param("pt", desc="d", door=doors.USER, optional=True)]
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Workflow("gated_data")[
        DrawGate(param="pt", geometry="point"),
        FormGate(),
        Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}),
    ]
    out = await _run(plan, decl, {}, data, resume=False,
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert _CALLS == ["stub_producer", "stub_second"]
    assert out.value["seen"]["m"] == "s3://b/produced.tif"


@pytest.mark.asyncio
async def test_a_resumed_run_does_not_refetch_produced_data():
    decl = _params()
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Workflow("data_led")[
        Step(runner=f"{_HERE}.stub_step", kwargs={"m": Ref("mesh")}).named("a"),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ]
    _FAIL_AT.add("stub_second")
    with pytest.raises(StepFailedError):
        await _run(plan, decl, {"base": 9.0}, data)
    assert _CALLS == ["stub_producer", "stub_step", "stub_second"]

    _CALLS.clear()
    _FAIL_AT.clear()
    await _run(plan, decl, {"base": 9.0}, data)
    assert _CALLS == ["stub_second"]     # neither the producer nor the step re-ran


@pytest.mark.asyncio
async def test_overrides_domain_rebinds_the_environment():
    plan = Workflow("w")[
        Step(runner=f"{_HERE}.stub_refine").named("clip").overrides_domain(),
    ]
    out = await _run(plan, _params(), {}, resume=False,
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert out.domain is not None and out.domain.bbox == (10.0, 10.0, 11.0, 11.0)


async def stub_refine(**kwargs):
    _CALLS.append("stub_refine")
    return {"bbox": [10.0, 10.0, 11.0, 11.0], "name": "refined"}


@pytest.mark.asyncio
async def test_a_replayed_domain_step_restores_the_domain_it_recorded():
    """The RECORDED domain is what the step actually left behind - correct by
    construction, not by re-reading the result and hoping."""
    plan = Workflow("dom_w")[
        Step(runner=f"{_HERE}.stub_refine").named("clip").overrides_domain(),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ]
    _FAIL_AT.add("stub_second")
    with pytest.raises(StepFailedError):
        await _run(plan, _params(), {"base": 11.0},
                   domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))

    _CALLS.clear()
    _FAIL_AT.clear()
    out = await _run(plan, _params(), {"base": 11.0},
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert _CALLS == ["stub_second"]                       # the clip REPLAYED
    assert out.domain is not None and out.domain.bbox == (10.0, 10.0, 11.0, 11.0)


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
async def test_a_completed_invocation_re_executes_it_is_not_a_cache():
    """The ledger resumes a FAILED attempt; it never becomes a permanent cache for
    a live-no-cache tool."""
    plan = Workflow("done_w")[Step(runner=f"{_HERE}.stub_step").named("a")]
    first = await _run(plan, _params(), {"base": 5.0})
    assert first.executed == ["a"] and first.replayed == []

    _CALLS.clear()
    second = await _run(plan, _params(), {"base": 5.0})
    assert _CALLS == ["stub_step"]
    assert second.executed == ["a"] and second.replayed == []


@pytest.mark.asyncio
async def test_a_completed_run_leaves_no_ledger_behind():
    from trid3nt_server.declarative import StepLedger, invocation_key as _key

    plan = Workflow("reaped_w")[Step(runner=f"{_HERE}.stub_step").named("a")]
    p = await resolve_params(_params(), {"base": 6.0})
    await interpret(plan, p, _params())
    ledger = await StepLedger.load(_key("reaped_w", p.values_dict()), "reaped_w")
    assert ledger.records == []


@pytest.mark.asyncio
async def test_replay_re_executes_when_the_cached_artifact_is_gone():
    """A dead URI is never handed back: the artifact probe forces a re-run."""
    plan = Workflow("dead_w")[
        Step(runner=f"{_HERE}.stub_step").named("expensive"),
        Step(runner=f"{_HERE}.stub_second").named("cheap"),
    ]
    _FAIL_AT.add("stub_second")
    with pytest.raises(StepFailedError):
        await _run(plan, _params(), {"base": 7.0})

    _CALLS.clear()
    _FAIL_AT.clear()
    _MISSING_ARTIFACTS.add("s3://b/k.tif")          # the cached COG was pruned
    out = await _run(plan, _params(), {"base": 7.0})
    assert _CALLS == ["stub_step", "stub_second"]
    assert out.replayed == [] and out.executed == ["expensive", "cheap"]


@pytest.mark.asyncio
async def test_restart_clean_ignores_a_failed_attempts_ledger():
    plan = Workflow("clean_w")[
        Step(runner=f"{_HERE}.stub_step").named("a"),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ]
    _FAIL_AT.add("stub_second")
    with pytest.raises(StepFailedError):
        await _run(plan, _params(), {"base": 3.0})
    _CALLS.clear()
    _FAIL_AT.clear()
    out = await _run(plan, _params(), {"base": 3.0}, resume=False)
    assert _CALLS == ["stub_step", "stub_second"] and out.replayed == []


@pytest.mark.asyncio
async def test_a_retryable_typed_gate_is_RAISED_not_flattened():
    """The adapter harvests .suggestions off the RAISED exception; wrapping it in a
    StepFailedError envelope destroys the retry channel."""
    plan = Workflow("gate_w")[Step(runner=f"{_HERE}.stub_gate_raiser").named("g")]
    with pytest.raises(_RetryableGate) as exc:
        await _run(plan, _params(), {}, resume=False)
    assert exc.value.suggestions


@pytest.mark.asyncio
async def test_an_auxiliary_chart_failure_does_not_kill_the_run():
    """failure retracts nothing: the primary result stands, the miss is narrated."""
    _FAIL_AT.add("stub_chart")
    plan = Workflow("aux_w")[
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=f"{_HERE}.stub_chart"),
    ]
    out = await _run(plan, _params(), {}, resume=False)
    assert out.value["uri"] == "s3://b/k.tif"
    assert out.executed == ["a"]
    assert len(out.notes) == 1 and "chart-boom" in out.notes[0]


@pytest.mark.asyncio
async def test_an_unrenderable_render_node_is_loud_and_non_fatal():
    plan = Workflow("aux_r")[
        Step(runner=f"{_HERE}.stub_producer").named("a").render(preset="p"),
    ]
    out = await _run(plan, _params(), {}, resume=False)
    assert out.value == "s3://b/produced.tif"
    assert len(out.notes) == 1 and "no object-store raster" in out.notes[0]


@pytest.mark.asyncio
async def test_a_failed_auxiliary_node_re_executes_on_the_next_run():
    _FAIL_AT.add("stub_chart")
    plan = Workflow("aux_led")[
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=f"{_HERE}.stub_chart"),
    ]
    await _run(plan, _params(), {"base": 8.0})
    _CALLS.clear()
    _FAIL_AT.clear()
    out = await _run(plan, _params(), {"base": 8.0})
    assert _CALLS == ["stub_step", "stub_chart"] and out.notes == []


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
