"""Declarative library v1: value types, doors, validator, interpreter, ledger.

Offline only - every runner here is a local stub; no solve, no network.
"""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import importlib
import warnings
from types import MappingProxyType, SimpleNamespace

import pytest
from pydantic import BaseModel

from trid3nt_server.workflows.lib import (
    Build,
    CoversAOI,
    D,
    Data,
    DataRef,
    Domain,
    DrawGate,
    Fetch,
    FormGate,
    LeakScanTruncated,
    ModifierIllegalError,
    P,
    Param,
    ParamNotResolved,
    ParamRef,
    ParamRefLeakedError,
    Physics,
    PlanValidationError,
    Ref,
    RenderSourceMissingError,
    ResolvedParams,
    RunMode,
    StepFailedError,
    SuppliedGeometryError,
    Step,
    When,
    Plan,
    Workflow,
    deep_freeze,
    doors,
    interpret,
    invocation_key,
    merge_provenance,
    provenance_entries,
    register_workflow,
    render_docstring,
    resolve_params,
    validate_plan,
)

_HERE = "tests.test_declarative_library"


@contextlib.contextmanager
def _patched(target, name, value):
    """Patch and restore by hand.

    NOT monkeypatch: the fixture and the test share one monkeypatch instance, so a
    mid-test ``undo()`` would also revert the fixture's persistence-dir env and
    send the rest of the test at the user's real store.
    """
    original = target.__dict__[name]
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


async def _raw_ledger_doc(key: str) -> dict | None:
    """The ledger document as it sits on disk - a tombstone is invisible above."""
    from trid3nt_server.workflows.lib.ledger import _COLLECTION
    from trid3nt_server.persistence import DEFAULT_DATABASE, FileMCPClient

    result = await FileMCPClient().call_tool("find-one", {
        "database": DEFAULT_DATABASE, "collection": _COLLECTION,
        "filter": {"_id": key}})
    return result.get("document")


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


class _StubLayer:
    """Stands in for a published LayerURI: a real object-store raster to style."""

    uri = "s3://b/real.tif"
    layer_id = "stub-layer"


async def stub_layer(**kwargs):
    _CALLS.append("stub_layer")
    return _StubLayer()


async def stub_self_gating(**kwargs):
    _CALLS.append("stub_self_gating")
    return {"uri": "s3://b/sg.tif", "seen": kwargs}


async def stub_gate_raiser(**kwargs):
    _CALLS.append("stub_gate_raiser")
    raise _RetryableGate()


def stub_chart(*, result, params):
    _CALLS.append("stub_chart")
    if "stub_chart" in _FAIL_AT:
        raise RuntimeError("chart-boom")
    return {"chart_id": "c1", "title": "t"}


def stub_chart_empty(*, result, params):
    """A builder with nothing to plot - the curve the result should carry is absent."""
    _CALLS.append("stub_chart_empty")
    return None


def stub_chart_leaks(*, result, params):
    """A builder that puts a DESCRIPTION in the title - the f-string leak, one call
    later, where only the emitted payload can catch it."""
    _CALLS.append("stub_chart_leaks")
    return {"chart_id": "c1", "title": ParamRef("base")}


class _StubProduct(BaseModel):
    """A pydantic result, because the skeleton's publish stage ``model_copy``s one."""

    uri: str = "s3://b/product.tif"
    layer_id: str = "stub-product"
    run_id: str | None = None
    synthetic_inputs: list = []
    fallback_note: str | None = None


async def stub_product(**kwargs):
    _CALLS.append("stub_product")
    return _StubProduct()


async def stub_deep(**kwargs):
    """A clean result deep enough to exhaust a shrunken scan budget."""
    _CALLS.append("stub_deep")
    node = {"leaf": 1.0}
    for i in range(40):
        node = {"n": node, "i": i}
    return {"uri": "s3://b/k.tif", "deep": node}


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
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "_artifact_state",
                        lambda uri: "absent" if uri in _MISSING_ARTIFACTS else "live")
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
        [Param("a", desc="d", door=doors.SCENARIO, default=1.0, type=float)],
        {"a": 7.0}, question={"a": 3.0},
    )
    assert p.value_of("a") == 7.0 and p.row("a").door == doors.USER


@pytest.mark.asyncio
async def test_question_beats_the_labeled_default():
    p = await resolve_params(
        [Param("a", desc="d", door=doors.SCENARIO, default=1.0, type=float)], {},
        question={"a": 3.0},
    )
    assert p.value_of("a") == 3.0 and p.row("a").basis == "prompt_interpreted"


@pytest.mark.asyncio
async def test_bounds_clamp_leaves_a_provenance_note():
    p = await resolve_params(
        [Param("a", desc="d", door=doors.SCENARIO, default=1.0, bounds=(0.0, 5.0),
               units="m")],
        {"a": 99.0},
    )
    assert p.value_of("a") == 5.0
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
        Param("base", desc="d", door=doors.SCENARIO, default=4.0, type=float),
    ], {})
    assert p.value_of("out") == 8.0 and p.row("out").basis == "derived"


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
    assert p.value_of("a") is None


@pytest.mark.asyncio
async def test_a_user_door_default_is_stamped_as_a_default_not_as_the_user():
    decl = [Param("a", desc="d", door=doors.USER, default=3.0, type=float)]
    p = await resolve_params(decl, {})
    assert p.value_of("a") == 3.0 and p.row("a").basis == "default_demo"
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
            Param("base", desc="d", door=doors.SCENARIO, default=4.0, type=float)]
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
def test_reference_producer_cannot_be_supplied():
    assert not hasattr(Fetch.tool("fetch_dem"), "supplied")
    assert hasattr(Build.tool("build_mesh"), "supplied")


def test_named_applies_once():
    step = Step(runner=f"{_HERE}.stub_step").named("a")
    with pytest.raises(ModifierIllegalError):
        step.named("b")


def test_gate_rejects_step_modifiers():
    for call in (lambda g: g.named("x"), lambda g: g.overrides_domain(),
                 lambda g: g.style(preset="p"), lambda g: g.chart("c", builder=stub_chart)):
        with pytest.raises(ModifierIllegalError):
            call(FormGate())


# --- the plan validator ------------------------------------------------------ #
def _params():
    return [Param("base", desc="d", door=doors.SCENARIO, default=1.0, type=float),
            Param("pt", desc="d", door=doors.USER, optional=True)]


def test_validator_refuses_a_ref_to_nothing():
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_step", kwargs={"x": Ref("nope")}),))
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_refuses_a_forward_ref():
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step", kwargs={"x": Ref("later")}),
        Step(runner=f"{_HERE}.stub_second").named("later"),
    ))
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_accepts_a_backward_ref():
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("first.uri")}),
    ))
    validate_plan(plan, _params())


def test_validator_refuses_two_steps_with_one_name():
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step").named("dup"),
        Step(runner=f"{_HERE}.stub_second").named("dup"),
    ))
    with pytest.raises(PlanValidationError, match="two steps"):
        validate_plan(plan, _params())


def test_validator_refuses_a_gate_after_the_consequential_step():
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step", consequential=True),
        FormGate(),
    ))
    with pytest.raises(PlanValidationError, match="dead gate"):
        validate_plan(plan, _params())


def test_validator_refuses_a_draw_gate_on_a_non_user_param():
    plan = Plan("w", None, (DrawGate(param="base", geometry="point"),))
    with pytest.raises(PlanValidationError, match="USER door"):
        validate_plan(plan, _params())


def test_validator_refuses_a_draw_gate_on_an_undeclared_param():
    plan = Plan("w", None, (DrawGate(param="ghost", geometry="point"),))
    with pytest.raises(PlanValidationError, match="undeclared param"):
        validate_plan(plan, _params())


def test_validator_refuses_a_second_form_gate():
    plan = Plan("w", None, (FormGate(), FormGate(),))
    with pytest.raises(PlanValidationError, match="more than one FormGate"):
        validate_plan(plan, _params())


def test_validator_checks_data_producer_refs():
    with pytest.raises(PlanValidationError, match="neither"):
        validate_plan(Plan("w", None, (Step(runner=f"{_HERE}.stub_step"),)), _params(),
                      [Data("m", Build.tool("b", size=Ref("ghost")))])


def test_validator_refuses_a_ref_into_a_guarded_branch():
    """A conditional step's name is not visible outside its branch - the branch
    may not fire, and a runtime REF_UNRESOLVED is not a contract."""
    plan = Plan("w", None, (
        When(P.base, Step(runner=f"{_HERE}.stub_step").named("maybe")),
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("maybe.uri")}),
    ))
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_validator_accepts_a_ref_inside_the_same_branch():
    plan = Plan("w", None, (
        When(P.base,
             Step(runner=f"{_HERE}.stub_step").named("here"),
             Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("here.uri")})),
    ))
    validate_plan(plan, _params())


def test_when_refuses_a_concrete_condition():
    """A concrete condition would decide the branch while the plan VALUE is being
    built - which is before any gate, so before anything the user could approve."""
    for concrete in (True, False, "yes", 1, 0.0, None):
        with pytest.raises(PlanValidationError, match="not a late-bound condition"):
            When(concrete, Step(runner=f"{_HERE}.stub_step"))


def test_validator_refuses_a_when_on_an_undeclared_param():
    """A branch condition must NAME something that resolves when it is reached."""
    plan = Plan("w", None, (When(P.ghost, Step(runner=f"{_HERE}.stub_step")),))
    with pytest.raises(PlanValidationError, match="not a declared param"):
        validate_plan(plan, _params())


def test_validator_refuses_a_when_on_a_step_that_is_not_visible_yet():
    plan = Plan("w", None, (
        When(Ref("later.flag"), Step(runner=f"{_HERE}.stub_step")),
        Step(runner=f"{_HERE}.stub_second").named("later"),
    ))
    with pytest.raises(PlanValidationError, match="resolves to nothing"):
        validate_plan(plan, _params())


def test_a_form_gate_over_a_branch_on_a_revisable_param_is_legal():
    """The old refusal is GONE, and its absence is the point: the interpreter
    decides the branch after the gate, so a revision of what it reads reaches it."""
    decl = [Param("flag", desc="run the extra step", door=doors.SCENARIO,
                  default=False)]
    plan = Plan("branchy", None, (
        FormGate(),
        When(P.flag, Step(runner=f"{_HERE}.stub_second").named("extra")),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    validate_plan(plan, decl)


def test_validator_refuses_a_param_declared_twice():
    decl = [Param("base", desc="d", door=doors.SCENARIO, default=1.0, type=float),
            Param("base", desc="other", door=doors.SCENARIO, default=2.0,
                  type=float)]
    with pytest.raises(PlanValidationError, match="declared twice"):
        validate_plan(Plan("w", None, (Step(runner=f"{_HERE}.stub_step"),)), decl)


@pytest.mark.asyncio
async def test_resolver_refuses_a_param_declared_twice():
    decl = [Param("base", desc="d", door=doors.SCENARIO, default=1.0, type=float),
            Param("base", desc="other", door=doors.SCENARIO, default=2.0,
                  type=float)]
    with pytest.raises(PlanValidationError, match="declared twice"):
        await resolve_params(decl, {})


# --- plan construction is pure ------------------------------------------------ #
def test_plan_construction_executes_nothing():
    Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step").named("a"),
        When(P.base, Step(runner=f"{_HERE}.stub_second")),
    ))
    assert _CALLS == []


def test_declared_lists_a_guarded_step_whichever_way_the_branch_falls():
    """The plan says what is DECLARED; which steps RUN is the interpreter's answer."""
    guarded = Step(runner=f"{_HERE}.stub_second").named("maybe")
    plan = Plan("w", None, (When(P.base, guarded),))
    assert plan.declared() == (guarded,)
    assert "when:base" in " ".join(plan.describe())


# --- the interpreter ---------------------------------------------------------- #
async def _run(plan, params_decl, wire, data=(), **kw):
    p = await resolve_params(params_decl, wire)
    return await interpret(plan, p, params_decl, data, **kw)


@pytest.mark.asyncio
async def test_interpreter_runs_steps_and_binds_refs():
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": Ref("first.uri")}),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert _CALLS == ["stub_step", "stub_second"]
    assert out.value["seen"]["x"] == "s3://b/k.tif"


@pytest.mark.asyncio
async def test_chart_modifier_runs_as_its_own_node():
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert _CALLS == ["stub_step", "stub_chart"]
    assert out.executed == ["a", "a.chart:c"]


@pytest.mark.asyncio
async def test_step_failure_is_typed_and_keeps_the_cause():
    _FAIL_AT.add("stub_step")
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    with pytest.raises(StepFailedError) as exc:
        await _run(plan, _params(), {}, resume=False)
    assert exc.value.step == "a" and isinstance(exc.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_draw_gate_refuses_typed_in_auto_when_the_param_is_required():
    decl = [Param("pt", desc="where it enters", door=doors.USER)]
    plan = Plan("w", None, (
        DrawGate(param="pt", geometry="point", prompt="click it"),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    with pytest.raises(Exception, match="never invented"):
        await _run(plan, decl, {"pt": None}, resume=False)


@pytest.mark.asyncio
async def test_draw_gate_refuses_typed_when_there_is_no_session_to_draw_on():
    """user_gated with no live map is the headless direct call: the caller asked
    for a drawing and there is nowhere to draw. That is not a licence to invent."""
    decl = [Param("pt", desc="where it enters", door=doors.USER)]
    plan = Plan("w", None, (
        DrawGate(param="pt", geometry="point"),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    with pytest.raises(Exception, match="no live map session"):
        await _run(plan, decl, {"pt": None}, input_mode="user_gated", resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_required_param_with_no_gate_refuses_at_the_consequential_step():
    decl = [Param("needed", desc="a real value", door=doors.USER)]
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_step", consequential=True),))
    with pytest.raises(Exception, match="never invented"):
        await _run(plan, decl, {}, resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_data_producer_runs_lazily_on_first_ref():
    decl = _params()
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}),
    ))
    out = await _run(plan, decl, {}, data, resume=False)
    assert _CALLS == ["stub_producer", "stub_second"]
    assert out.value["seen"]["m"] == "s3://b/produced.tif"


@pytest.mark.asyncio
async def test_byo_artifact_short_circuits_the_producer():
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer").supplied("s3://mine/m.slf",
                                                                  validate=None))]
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}),))
    out = await _run(plan, _params(), {}, data, resume=False)
    assert _CALLS == ["stub_second"]
    assert out.value["seen"]["m"] == "s3://mine/m.slf"


@pytest.mark.asyncio
async def test_byo_coverage_validation_refuses_without_a_domain():
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer").supplied("s3://mine/m.slf",
                                                                  validate=CoversAOI))]
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}),))
    with pytest.raises(Exception, match="cannot be checked against the modelled domain"):
        await _run(plan, _params(), {}, data, resume=False)


@pytest.mark.asyncio
async def test_run_mode_binds_the_runs_input_gate_mode_into_a_step():
    plan = Plan("mode_w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"input_mode": RunMode}),
    ))
    out = await _run(plan, _params(), {}, input_mode="user_gated", resume=False)
    assert out.value["seen"]["input_mode"] == "user_gated"


# --- the branch the INTERPRETER decides --------------------------------------- #
def _flagged(**overrides):
    return [Param("flag", desc="run the extra step", door=doors.SCENARIO,
                  default=False, **overrides)]


@pytest.mark.asyncio
async def test_a_guarded_step_runs_only_when_its_branch_fires():
    """``Plan.declared()`` lists it either way; ``RunResult.executed`` is the answer."""
    decl = _flagged()
    guarded = Step(runner=f"{_HERE}.stub_second").named("maybe")
    plan = Plan("guard_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("always"),
        When(P.flag, guarded),
    ))
    assert [s.label for s in plan.declared()] == ["always", "maybe"]

    out = await _run(plan, decl, {}, resume=False)
    assert out.executed == ["always"] and _CALLS == ["stub_step"]
    assert [s.label for s in plan.declared()] == ["always", "maybe"]

    _CALLS.clear()
    out = await _run(plan, decl, {"flag": True}, resume=False)
    assert out.executed == ["always", "maybe"]
    assert _CALLS == ["stub_step", "stub_second"]


@pytest.mark.asyncio
async def test_a_nested_branch_is_decided_by_its_own_condition():
    """A When inside a fired When is still guarded by its OWN condition."""
    decl = [Param("outer", desc="d", door=doors.SCENARIO, default=True),
            Param("inner", desc="d", door=doors.SCENARIO, default=False)]
    deep = Step(runner=f"{_HERE}.stub_second").named("deep")
    plan = Plan("nest_w", None, (When(P.outer, When(P.inner, deep)),))

    out = await _run(plan, decl, {}, resume=False)
    assert out.executed == [] and _CALLS == []
    assert plan.declared() == (deep,)          # declared, simply not reached

    out = await _run(plan, decl, {"inner": True}, resume=False)
    assert out.executed == ["deep"] and _CALLS == ["stub_second"]

    _CALLS.clear()
    out = await _run(plan, decl, {"outer": False, "inner": True}, resume=False)
    assert out.executed == [] and _CALLS == []


@pytest.mark.asyncio
async def test_an_unfired_branch_never_pulls_the_data_behind_it():
    """The point of demand-pulled producers: a branch that does not fire costs no
    fetch, because the only thing that would have pulled the artifact is the step
    the branch skipped."""
    decl = _flagged()
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Plan("lazy_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("always"),
        When(P.flag, Step(runner=f"{_HERE}.stub_second",
                          kwargs={"m": Ref("mesh")}).named("maybe")),
    ))
    await _run(plan, decl, {}, data, resume=False)
    assert _CALLS == ["stub_step"]              # stub_producer never ran

    _CALLS.clear()
    out = await _run(plan, decl, {"flag": True}, data, resume=False)
    assert _CALLS == ["stub_step", "stub_producer", "stub_second"]
    assert out.value["seen"]["m"] == "s3://b/produced.tif"


@pytest.mark.asyncio
async def test_a_branch_after_the_form_gate_reads_the_approved_revision(monkeypatch):
    """The whole point of deciding the branch in the interpreter: the plan value is
    built before any gate, so only a late-decided When can honour the approval."""
    decl = _flagged()
    plan = Plan("gate_branch", None, (
        FormGate(),
        When(P.flag, Step(runner=f"{_HERE}.stub_second").named("extra")),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))

    # Approved as declared: the branch stays shut.
    _review({}, monkeypatch)
    p = await resolve_params(decl, {})
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    assert out.executed == ["solve"]

    # The user flipped it at the card: the branch that runs is the APPROVED one.
    _CALLS.clear()
    _review({"flag": True}, monkeypatch)
    p = await resolve_params(decl, {})
    assert p.value_of("flag") is False               # the pre-review sheet says no
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    assert out.executed == ["extra", "solve"]


@pytest.mark.asyncio
async def test_a_data_producer_runs_when_its_consumer_does_hence_after_the_gate(
        monkeypatch):
    """Producers are demand-pulled, so the fetch happens at the step that Refs the
    artifact - which puts it after any gate declared in front of that step. A
    producer that fetched earlier fetched against params the gate exists to change."""
    _recording_review({}, monkeypatch)
    decl = _params()
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Plan("gated_data", None, (
        Step(runner=f"{_HERE}.stub_step").named("pre"),
        FormGate(),
        Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}).named("post"),
    ))
    out = await _run(plan, decl, {}, data, resume=False, input_mode="user_gated",
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert _CALLS == ["stub_step", "form_gate", "stub_producer", "stub_second"]
    assert out.value["seen"]["m"] == "s3://b/produced.tif"


# --- a producer-less Data slot: context handed in, or labelled absence -------- #
@pytest.mark.asyncio
async def test_a_context_slot_is_satisfied_by_the_artifact_handed_in():
    data = [Data("clip_zone")]
    plan = Plan("slot_supplied", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"z": Ref("clip_zone")}).named("a"),
    ))
    out = await _run(plan, _params(), {}, data, resume=False,
                     supplied={"clip_zone": "s3://mine/zone.gpkg"},
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert out.value["seen"]["z"] == "s3://mine/zone.gpkg"


@pytest.mark.asyncio
async def test_an_unsatisfied_required_context_slot_refuses_typed():
    """Naming a default fetcher for a slot the template deliberately left open
    would be the library inventing the source."""
    data = [Data("clip_zone")]
    plan = Plan("slot_req", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"z": Ref("clip_zone")}).named("a"),
    ))
    with pytest.raises(StepFailedError) as exc:
        await _run(plan, _params(), {}, data, resume=False)
    assert exc.value.error_code == "DATA_SLOT_UNSATISFIED"
    assert exc.value.step == "data:clip_zone"
    assert _CALLS == []


@pytest.mark.asyncio
async def test_an_unsatisfied_optional_context_slot_binds_none_and_says_so():
    """Absence is legal AND LABELLED: the run answered a slightly different
    question than one that had the layer, and only the reader can weigh that."""
    data = [Data("clip_zone").optional()]
    plan = Plan("slot_opt", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"z": Ref("clip_zone")}).named("a"),
    ))
    out = await _run(plan, _params(), {}, data, resume=False)
    assert out.value["seen"]["z"] is None
    assert len(out.notes) == 1 and "clip_zone" in out.notes[0]


def test_a_producer_backed_data_may_not_be_optional():
    """A producer either produces or fails typed, so there is no absence to describe."""
    with pytest.raises(PlanValidationError, match="optional"):
        Data("mesh", Build.tool(f"{_HERE}.stub_producer")).optional()


def test_a_context_slot_declares_the_shape_it_accepts():
    """A slot names no source - the SHAPE is the only thing it can honestly say."""
    slot = Data("structure").supplied(geometry="polyline").optional()
    assert slot.producer is None
    assert slot.geometry == "polyline" and slot.is_optional is True


def test_a_context_slot_refuses_a_shape_nobody_declares():
    with pytest.raises(PlanValidationError, match="not a declared shape"):
        Data("structure").supplied(geometry="squiggle")


def test_supplied_on_a_slot_and_supplied_on_a_producer_are_different_asks():
    """A producer that can be SUPERSEDED says so on the producer, not on the slot."""
    with pytest.raises(PlanValidationError, match="declares a producer AND"):
        Data("mesh", Build.tool(f"{_HERE}.stub_producer")).supplied(geometry="mesh")
    producer = Build.tool(f"{_HERE}.stub_producer").supplied("s3://mine/m.slf")
    assert Data("mesh", producer).is_supplied is True


@pytest.mark.asyncio
async def test_a_resumed_run_does_not_refetch_produced_data():
    decl = _params()
    data = [Data("mesh", Build.tool(f"{_HERE}.stub_producer"))]
    plan = Plan("data_led", None, (
        Step(runner=f"{_HERE}.stub_step", kwargs={"m": Ref("mesh")}).named("a"),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ))
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
    plan = Plan("w", None, (
        Step(runner=f"{_HERE}.stub_refine").named("clip").overrides_domain(),
    ))
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
    plan = Plan("dom_w", None, (
        Step(runner=f"{_HERE}.stub_refine").named("clip").overrides_domain(),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ))
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
    plan = Plan("resume_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("expensive"),
        Step(runner=f"{_HERE}.stub_second").named("cheap"),
    ))
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
    plan = Plan("done_w", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    first = await _run(plan, _params(), {"base": 5.0})
    assert first.executed == ["a"] and first.replayed == []

    _CALLS.clear()
    second = await _run(plan, _params(), {"base": 5.0})
    assert _CALLS == ["stub_step"]
    assert second.executed == ["a"] and second.replayed == []


@pytest.mark.asyncio
async def test_a_completed_run_leaves_a_tombstone_not_a_replayable_ledger():
    from trid3nt_server.workflows.lib import StepLedger, invocation_key as _key

    plan = Plan("reaped_w", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    p = await resolve_params(_params(), {"base": 6.0})
    await interpret(plan, p, _params())
    key = _key("reaped_w", p.values_dict())
    ledger = await StepLedger.load(key, "reaped_w")
    assert ledger.records == []
    assert (await _raw_ledger_doc(key))["complete"] is True


@pytest.mark.asyncio
async def test_replay_re_executes_when_the_cached_artifact_is_gone():
    """A dead URI is never handed back: the artifact probe forces a re-run."""
    plan = Plan("dead_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("expensive"),
        Step(runner=f"{_HERE}.stub_second").named("cheap"),
    ))
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
    plan = Plan("clean_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("a"),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ))
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
    plan = Plan("gate_w", None, (Step(runner=f"{_HERE}.stub_gate_raiser").named("g"),))
    with pytest.raises(_RetryableGate) as exc:
        await _run(plan, _params(), {}, resume=False)
    assert exc.value.suggestions


@pytest.mark.asyncio
async def test_an_auxiliary_chart_failure_does_not_kill_the_run():
    """failure retracts nothing: the primary result stands, the miss is narrated."""
    _FAIL_AT.add("stub_chart")
    plan = Plan("aux_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert out.value["uri"] == "s3://b/k.tif"
    assert out.executed == ["a"]
    assert len(out.notes) == 1 and "chart-boom" in out.notes[0]


@pytest.mark.asyncio
async def test_a_step_that_produced_no_layer_to_style_is_FATAL():
    """The honesty floor: a declared style whose source is not a layer means the
    step did not make the map layer it promised - that is a primary defect, not a
    styling note."""
    plan = Plan("aux_r", None, (
        Step(runner=f"{_HERE}.stub_producer").named("a").style(preset="p"),
    ))
    with pytest.raises(RenderSourceMissingError, match="no object-store layer"):
        await _run(plan, _params(), {}, resume=False)


@pytest.mark.asyncio
async def test_a_styling_failure_over_a_real_layer_is_only_a_note(monkeypatch):
    """The other half of the split: there IS a layer, the re-painting of it failed."""
    import trid3nt_server.emission.restyle as _rs

    def _boom(**kwargs):
        raise RuntimeError("restyle-boom")

    monkeypatch.setattr(_rs, "apply_style", _boom)
    plan = Plan("style_r", None, (
        Step(runner=f"{_HERE}.stub_layer").named("a").style(preset="p"),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert out.value.uri == "s3://b/real.tif"
    assert len(out.notes) == 1 and "restyle-boom" in out.notes[0]


@pytest.mark.asyncio
async def test_a_failed_auxiliary_node_re_executes_on_the_next_run():
    _FAIL_AT.add("stub_chart")
    plan = Plan("aux_led", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart),
    ))
    await _run(plan, _params(), {"base": 8.0})
    _CALLS.clear()
    _FAIL_AT.clear()
    out = await _run(plan, _params(), {"base": 8.0})
    assert _CALLS == ["stub_step", "stub_chart"] and out.notes == []


def test_invocation_key_is_stable_and_param_sensitive():
    assert invocation_key("w", {"a": 1}) == invocation_key("w", {"a": 1})
    assert invocation_key("w", {"a": 1}) != invocation_key("w", {"a": 2})


def test_invocation_key_separates_the_two_input_modes():
    """A failed AUTO attempt must not seed a user_gated replay: the gated run may
    revise the very params the auto attempt cached."""
    auto = invocation_key("w", {"a": 1}, input_mode="auto")
    gated = invocation_key("w", {"a": 1}, input_mode="user_gated")
    assert auto != gated
    assert invocation_key("w", {"a": 1}, input_mode=None) == auto


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


def test_the_routing_view_stops_before_the_param_sheet():
    """Two views, one declaration: a surface that only helps someone CHOOSE the
    tool takes the routing block; the model filling the params takes the sheet."""
    kwargs = dict(summary="S.", routing="R.", params=_params(), returns="a layer",
                  not_for="something else")
    routing = render_docstring(**kwargs, view="routing")
    full = render_docstring(**kwargs)
    assert "Params:" not in routing and "Params:" in full
    assert routing.startswith("S.") and "Do NOT use this for" in routing
    assert "Returns: a layer" in routing
    assert len(routing) < len(full)


def test_do_sag_publishes_both_docstring_views():
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag

    assert "Params:" in (telemac_do_sag.__doc__ or "")
    assert "Params:" not in telemac_do_sag.routing_doc
    assert len(telemac_do_sag.routing_doc) < 1400


def test_docstring_reports_bounds_units_and_labeled_defaults():
    doc = render_docstring(
        summary="S.", routing="R.",
        params=[Param("a", desc="the knob", door=doors.SCENARIO, default=2.0,
                      bounds=(1.0, 3.0), units="mg/L")],
        returns="a layer")
    assert "mg/L" in doc and "range 1-3" in doc
    assert "labeled scenario default" in doc


# --- late binding: a plan DESCRIBES, the interpreter SUBSTITUTES -------------- #
@pytest.mark.asyncio
async def test_a_plan_reads_params_as_late_bound_refs_not_baked_values():
    p = await resolve_params(_params(), {"base": 4.0})
    assert isinstance(p.base, ParamRef) and p.base.name == "base"
    step = Step(runner=f"{_HERE}.stub_step", kwargs={"x": p.base})
    baked = step.kwargs["x"]                        # the VALUE 4.0 is nowhere in it
    assert isinstance(baked, ParamRef) and baked.name == "base"


@pytest.mark.asyncio
async def test_a_param_ref_has_no_truth_value_at_construction_time():
    """A construction-time ``if`` on a ref must refuse, and the refusal has to name
    the one conditional the language has: a When the interpreter decides."""
    p = await resolve_params(_params(), {"base": 4.0})
    with pytest.raises(PlanValidationError, match=r"When\(P\.base, \.\.\.\)"):
        bool(p.base)
    with pytest.raises(PlanValidationError, match=r"When\(P\.base, \.\.\.\)"):
        bool(P.base)


@pytest.mark.asyncio
async def test_an_undeclared_param_read_refuses_at_construction():
    p = await resolve_params(_params(), {})
    with pytest.raises(ParamNotResolved):
        _ = p.ghost


def test_validator_refuses_a_param_ref_to_an_undeclared_param():
    plan = Plan("w", None, (Step(runner=f"{_HERE}.stub_step",
                              kwargs={"x": ParamRef("ghost")}),))
    with pytest.raises(PlanValidationError, match="P.ghost names no declared param"):
        validate_plan(plan, _params())


@pytest.mark.asyncio
async def test_late_binding_reaches_the_runner_with_the_resolved_value():
    p = await resolve_params(_params(), {"base": 4.0})
    plan = Plan("late_w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"x": p.base}).named("a"),))
    out = await interpret(plan, p, _params(), resume=False)
    assert out.value["seen"]["x"] == 4.0


# --- the form gate's revision REACHES the run -------------------------------- #
def _review(revised, monkeypatch):
    """Patch the review spine so it approves, carrying ``revised`` back."""
    from trid3nt_server.gates.input_review import ReviewOutcome, _apply_revision

    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")

    async def _fake(*, tool_name, mode, entries, params, **kw):
        merged_e, merged_p = _apply_revision(list(entries), dict(params), revised)
        return ReviewOutcome(proceed=True, entries=merged_e, params=merged_p,
                             mode="user_gated", rounds_used=1)

    monkeypatch.setattr(_interp, "gate_input_review", _fake)


def _recording_review(revised, monkeypatch):
    """``_review``, plus a mark in ``_CALLS`` so gate ORDER is assertable."""
    _review(revised, monkeypatch)
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    approved = _interp.gate_input_review

    async def _marked(**kw):
        _CALLS.append("form_gate")
        return await approved(**kw)

    monkeypatch.setattr(_interp, "gate_input_review", _marked)


@pytest.mark.asyncio
async def test_a_revision_approved_at_the_form_gate_is_what_actually_runs(monkeypatch):
    """what-was-approved == what-ran. The plan is built BEFORE the gate, so the
    only way a revision reaches the step is late binding."""
    _review({"base": 99.0}, monkeypatch)
    decl = _params()
    p = await resolve_params(decl, {"base": 2.0})
    plan = Plan("rev_w", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_second", kwargs={"q": p.base},
             consequential=True).named("solve"),
    ))
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    assert out.value["seen"]["q"] == 99.0


@pytest.mark.asyncio
async def test_a_revised_row_is_re_stamped_user_in_the_runs_provenance(monkeypatch):
    _review({"base": 7.5}, monkeypatch)
    decl = _params()
    p = await resolve_params(decl, {})
    plan = Plan("rev_prov", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_second", kwargs={"q": p.base}).named("solve"),
    ))
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    row = next(e for e in out.entries if e.param == "base")
    assert row.value == 7.5 and row.basis == "user"
    assert "revised at input review" in (row.note or "")


@pytest.mark.asyncio
async def test_a_revision_still_obeys_the_declared_bounds(monkeypatch):
    """The form is an edit surface, not a bypass of the declaration."""
    _review({"b": 900.0}, monkeypatch)
    decl = [Param("b", desc="d", door=doors.SCENARIO, default=2.0, bounds=(0.0, 10.0))]
    p = await resolve_params(decl, {})
    plan = Plan("rev_bounds", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_second", kwargs={"q": p.b}).named("solve"),
    ))
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    assert out.value["seen"]["q"] == 10.0
    assert "CLAMPED" in (next(e for e in out.entries if e.param == "b").note or "")


@pytest.mark.asyncio
async def test_a_revision_re_keys_the_ledger(monkeypatch):
    """The approved sheet is a different invocation; a replay may only come from an
    attempt at THESE values."""
    _review({"base": 3.5}, monkeypatch)
    decl = _params()
    p = await resolve_params(decl, {"base": 1.0})
    plan = Plan("rev_key", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_step").named("solve"),
    ))
    await interpret(plan, p, decl, input_mode="user_gated")
    revised_key = invocation_key("rev_key", {**p.values_dict(), "base": 3.5},
                                 input_mode="user_gated")
    assert (await _raw_ledger_doc(revised_key))["complete"] is True
    assert await _raw_ledger_doc(
        invocation_key("rev_key", p.values_dict(), input_mode="user_gated")) is None


# --- a self-gating composite takes no second review card --------------------- #
def test_validator_refuses_a_form_gate_in_front_of_a_self_gating_step():
    plan = Plan("w", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_self_gating", consequential=True,
             self_gating=True).named("composite"),
    ))
    with pytest.raises(PlanValidationError, match="reviews its own inputs"):
        validate_plan(plan, _params())


@pytest.mark.asyncio
async def test_do_sag_declares_no_form_gate_in_front_of_its_self_gating_review():
    from trid3nt_server.tools import TOOL_REGISTRY

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    validate_plan(wf.plan, wf.params, wf.data)
    assert [s.kind for s in wf.plan.declared() if hasattr(s, "kind")] == ["draw"]


# --- the completion tombstone: three ghost paths ----------------------------- #
@pytest.mark.asyncio
async def test_a_finished_run_whose_reap_would_fail_is_still_marked_complete(monkeypatch):
    """The wave-1b reap was a DELETE whose failure only warned, so a finished run
    stayed replayable. The tombstone is a positive marker on the same write path."""
    from trid3nt_server.persistence import FileMCPClient

    real = FileMCPClient.call_tool

    async def _no_deletes(self, name, arguments=None):
        if name == "delete-one":
            raise OSError("simulated persistence failure on delete")
        return await real(self, name, arguments)

    monkeypatch.setattr(FileMCPClient, "call_tool", _no_deletes)
    plan = Plan("ghost1", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    await _run(plan, _params(), {"base": 21.0})
    _CALLS.clear()
    out = await _run(plan, _params(), {"base": 21.0})
    assert _CALLS == ["stub_step"]
    assert out.executed == ["a"] and out.replayed == []


@pytest.mark.asyncio
async def test_a_crash_between_the_last_record_and_completion_leaves_no_ghost():
    from trid3nt_server.workflows.lib import StepLedger

    async def _die(self):
        raise KeyboardInterrupt("SIGINT before the completion call")

    plan = Plan("ghost2", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    with _patched(StepLedger, "complete", _die):
        with pytest.raises(KeyboardInterrupt):
            await _run(plan, _params(), {"base": 22.0})

    _CALLS.clear()
    out = await _run(plan, _params(), {"base": 22.0})
    assert _CALLS == ["stub_step"]
    assert out.executed == ["a"] and out.replayed == []


@pytest.mark.asyncio
async def test_a_cancel_after_the_last_record_leaves_no_ghost():
    import asyncio

    from trid3nt_server.workflows.lib import StepLedger

    async def _cancel(self):
        raise asyncio.CancelledError()

    plan = Plan("ghost3", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    with _patched(StepLedger, "complete", _cancel):
        with pytest.raises(asyncio.CancelledError):
            await _run(plan, _params(), {"base": 23.0})

    _CALLS.clear()
    out = await _run(plan, _params(), {"base": 23.0})
    assert _CALLS == ["stub_step"] and out.replayed == []


@pytest.mark.asyncio
async def test_a_cancel_MID_plan_stays_resumable_that_is_resume_working():
    """A cancelled run is not a finished run: its completed steps must survive."""
    import asyncio

    async def _cancel_step(**kwargs):
        raise asyncio.CancelledError()

    globals()["stub_cancel"] = _cancel_step
    plan = Plan("cancel_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("expensive"),
        Step(runner=f"{_HERE}.stub_cancel").named("interrupted"),
    ))
    with pytest.raises(asyncio.CancelledError):
        await _run(plan, _params(), {"base": 24.0})
    _CALLS.clear()

    plan2 = Plan("cancel_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("expensive"),
        Step(runner=f"{_HERE}.stub_second").named("interrupted"),
    ))
    out = await _run(plan2, _params(), {"base": 24.0})
    assert out.replayed == ["expensive"] and _CALLS == ["stub_second"]


@pytest.mark.asyncio
async def test_the_sweep_reaps_tombstones_past_the_ttl(monkeypatch):
    """Tombstones are bounded: they are reaped on AGE, not kept forever."""
    from trid3nt_server.workflows.lib import StepLedger, invocation_key as _key
    import trid3nt_server.workflows.lib.ledger as _led

    plan = Plan("ttl_w", None, (Step(runner=f"{_HERE}.stub_step").named("a"),))
    p = await resolve_params(_params(), {"base": 25.0})
    await interpret(plan, p, _params())
    key = _key("ttl_w", p.values_dict())
    assert (await _raw_ledger_doc(key))["complete"] is True

    monkeypatch.setattr(_led, "_TTL", _led.timedelta(seconds=-1))
    await StepLedger.load("some-other-key", "ttl_w")     # the sweep runs on load
    assert await _raw_ledger_doc(key) is None


# --- aux notes survive a later failure ---------------------------------------- #
@pytest.mark.asyncio
async def test_an_aux_note_is_carried_into_the_failure_that_ends_the_run():
    _FAIL_AT.add("stub_chart")
    _FAIL_AT.add("stub_second")
    plan = Plan("notes_w", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart),
        Step(runner=f"{_HERE}.stub_second").named("b"),
    ))
    with pytest.raises(StepFailedError) as exc:
        await _run(plan, _params(), {}, resume=False)
    assert any("chart-boom" in n for n in getattr(exc.value, "__notes__", ()))


# --- eager Data producer errors are typed too --------------------------------- #
@pytest.mark.asyncio
async def test_an_eager_data_producer_failure_is_typed_with_its_own_error_code():
    async def _bad_producer(**kwargs):
        _CALLS.append("bad_producer")
        raise _DataDown()

    globals()["bad_producer"] = _bad_producer
    data = [Data("mesh", Build.tool(f"{_HERE}.bad_producer"))]
    plan = Plan("eager_w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"m": Ref("mesh")}).named("a"),
    ))
    with pytest.raises(StepFailedError) as exc:
        await _run(plan, _params(), {}, data, resume=False,
                   domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert exc.value.error_code == "MESH_SOURCE_DOWN"
    assert exc.value.step == "data:mesh"


class _DataDown(RuntimeError):
    error_code = "MESH_SOURCE_DOWN"

    def __init__(self):
        super().__init__("the mesh source is down")


# --- the ledger under concurrency --------------------------------------------- #
@pytest.mark.asyncio
async def test_two_ledgers_over_one_store_share_a_lock():
    from trid3nt_server.workflows.lib import StepLedger
    from trid3nt_server.workflows.lib.ledger import _COLLECTION
    from trid3nt_server.persistence import DEFAULT_DATABASE

    a = await StepLedger.load("KEY_A", "wf")
    b = await StepLedger.load("KEY_B", "wf")
    path = a._client._collection_path(DEFAULT_DATABASE, _COLLECTION)
    assert a._client is not b._client
    assert a._client._lock_for(path) is b._client._lock_for(path)


@pytest.mark.asyncio
async def test_a_concurrent_ledger_cannot_resurrect_a_completed_one():
    """A whole-store write computed from a stale snapshot would put the reaped
    records back and make a FINISHED run replayable again."""
    import asyncio
    import time

    from trid3nt_server.workflows.lib import StepLedger
    from trid3nt_server.workflows.lib.ledger import LedgerRecord
    from trid3nt_server.persistence import FileMCPClient

    real_read = FileMCPClient._read_store

    def _slow_read(path):
        time.sleep(0.05)                 # force the two cycles to overlap
        return real_read(path)

    def _rec(node, uri):
        return LedgerRecord(index=0, node=node, runner="r",
                            completed_at="2026-08-23T00:00:00+00:00",
                            result_kind="json", result={"uri": uri},
                            artifact_uris=(uri,))

    a = await StepLedger.load("KEY_A", "wf")
    b = await StepLedger.load("KEY_B", "wf")
    await a.record(_rec("a0", "s3://x/a0"))
    await b.record(_rec("b0", "s3://x/b0"))

    with _patched(FileMCPClient, "_read_store", staticmethod(_slow_read)):
        await asyncio.gather(a.complete(), b.record(_rec("b1", "s3://x/b1")))

    assert (await _raw_ledger_doc("KEY_A"))["complete"] is True
    assert (await _raw_ledger_doc("KEY_A"))["records"] == []
    assert [r["node"] for r in (await _raw_ledger_doc("KEY_B"))["records"]] == ["b1"]


def test_the_store_cycle_is_locked_across_processes(tmp_path):
    """The flock + read-inside-the-lock rule, exercised by real parallel writers:
    every writer's document survives, because none writes a stale whole store."""
    import threading

    from trid3nt_server.persistence import FileMCPClient

    client = FileMCPClient(base_dir=tmp_path)
    path = client._collection_path("db", "coll")

    def _writer(i):
        def _apply(store):
            store[f"doc{i}"] = {"_id": f"doc{i}"}
            return None, True
        client._cycle(path, _apply)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(FileMCPClient._read_store(path)) == sorted(
        f"doc{i}" for i in range(12))


# --- law 9 without a form card ------------------------------------------------ #
@pytest.mark.asyncio
async def test_auto_mode_refuses_an_invented_physics_default_with_no_form_gate():
    """Removing the plan's FormGate must not remove the honesty floor with it: a
    physics value that fell back to an invented default has nobody to approve it."""
    decl = [Param("aquifer_k_ms", desc="hydraulic conductivity", door=doors.SCENARIO,
                  default=1e-4, type=float, units="m/s", consequence="physics")]
    plan = Plan("law9_w", None, (
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    with pytest.raises(Exception, match="PHYSICS_INPUT_REQUIRED"):
        await _run(plan, decl, {}, resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_a_user_supplied_physics_value_is_not_refused():
    decl = [Param("aquifer_k_ms", desc="hydraulic conductivity", door=doors.SCENARIO,
                  default=1e-4, type=float, units="m/s", consequence="physics")]
    plan = Plan("law9_ok", None, (
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    await _run(plan, decl, {"aquifer_k_ms": 9.1e-6}, resume=False)
    assert _CALLS == ["stub_step"]


def test_validator_refuses_a_param_ref_in_a_data_producer():
    """A producer consumes params too - the dataflow crosses the Param/Data line."""
    data = [Data("mesh", Build.tool("b", size=ParamRef("ghost")))]
    with pytest.raises(PlanValidationError, match="P.ghost names no declared param"):
        validate_plan(Plan("w", None, (Step(runner=f"{_HERE}.stub_step"),)), _params(), data)


# ============================================================================ #
# wave 1d - revision coherence: leaks, re-derivation, eviction, the law-9 floor
# ============================================================================ #

# --- R3-1: a ParamRef may not leak past the late-binding seam ----------------- #
def test_a_param_ref_refuses_every_silent_leak_path():
    """Each of these used to answer QUIETLY: an f-string baked ``ParamRef(...)``
    into a layer title, ``==`` answered False against the value the author meant,
    and hashing let a ref sit in a set the binder did not walk."""
    ref = ParamRef("reach_km")
    with pytest.raises(PlanValidationError, match="str"):
        str(ref)
    with pytest.raises(PlanValidationError, match="f-string"):
        _ = f"DO sag over {ref} km"
    with pytest.raises(PlanValidationError, match="comparison"):
        _ = ref == 12.0
    with pytest.raises(PlanValidationError, match="comparison"):
        _ = ref != 12.0
    with pytest.raises(PlanValidationError, match="hashing"):
        _ = {ref}
    assert repr(ref) == "ParamRef('reach_km')"     # naming it is what repr is for


@pytest.mark.asyncio
async def test_the_binder_walks_sets_and_frozensets_like_the_validator_does():
    """The validator has always walked sets for declared reads; a binder that did
    not would hand the runner a set of DESCRIPTIONS."""
    plan = Plan("setbind", None, (
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second", kwargs={
            "s": {Ref("first.uri")},
            "f": frozenset({Ref("first.uri")}),
        }).named("second"),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert out.value["seen"]["s"] == {"s3://b/k.tif"}
    assert out.value["seen"]["f"] == frozenset({"s3://b/k.tif"})


@dataclasses.dataclass(frozen=True, slots=True)
class _Holder:
    """A plan author's own value type in the HOUSE idiom: frozen + slots, so it has
    no ``__dict__`` at all. The binder does not walk into it - the LEAK GUARD is
    what refuses the ref it is hiding, and it has to read slots to see one."""

    value: object


class _DictHolder:
    """The plain ``__dict__`` object, kept so the slots arm does not cost this one."""

    def __init__(self, value):
        self.value = value


class _SlotsNoDataclass:
    """``__slots__`` without the dataclass decorator - the other half of that arm."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


async def stub_returns_a_leaked_ref(**kwargs):
    _CALLS.append("stub_returns_a_leaked_ref")
    # A set arm and a slotted-object arm, each hiding a ref: the scan walks both.
    # The set holds a _DictHolder, which hashes by identity - a frozen dataclass
    # would hash its fields and ParamRef refuses hashing.
    return {"uri": "s3://b/k.tif", "bag": {_DictHolder(ParamRef("base"))},
            "held": _Holder(ParamRef("base"))}


@pytest.mark.asyncio
async def test_a_ref_hidden_on_a_slotted_object_never_reaches_a_runner():
    """frozen+slots has no __dict__; a guard that read only __dict__ handed the
    runner a description and json.dumps(default=str) put it on the wire as text."""
    plan = Plan("leak_args", None, (
        Step(runner=f"{_HERE}.stub_step",
             kwargs={"held": _Holder(ParamRef("base"))}).named("s"),
    ))
    with pytest.raises(ParamRefLeakedError, match="arguments"):
        await _run(plan, _params(), {}, resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_a_ref_hidden_on_a_dict_object_never_reaches_a_runner():
    plan = Plan("leak_args_dict", None, (
        Step(runner=f"{_HERE}.stub_step",
             kwargs={"held": _DictHolder(ParamRef("base"))}).named("s"),
    ))
    with pytest.raises(ParamRefLeakedError, match="arguments"):
        await _run(plan, _params(), {}, resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_a_ref_in_a_result_never_reaches_the_ledger_or_the_caller():
    """A ref on disk is always a bug, never data - so the record is refused rather
    than written, sequence and slotted-object arms included."""
    plan = Plan("leak_result", None, (
        Step(runner=f"{_HERE}.stub_returns_a_leaked_ref").named("s"),
    ))
    with pytest.raises(ParamRefLeakedError, match="ledger record"):
        await _run(plan, _params(), {}, resume=False)


def test_the_guard_reads_slots_dataclass_fields_and_dict_alike():
    """The three attribute shapes an author can hand the guard, at the seam itself."""
    from trid3nt_server.workflows.lib.interpreter import _refuse_leaked_param_refs

    for holder in (_Holder(ParamRef("base")), _DictHolder(ParamRef("base")),
                   _SlotsNoDataclass(ParamRef("base"))):
        with pytest.raises(ParamRefLeakedError, match=r"ParamRef\('base'\)") as exc:
            _refuse_leaked_param_refs({"result": {"deep": [holder]}}, "a test surface")
        assert "['result']['deep'][0].value" in str(exc.value)


# --- R3-2: a revision re-derives what depends on it -------------------------- #
def derive_saturation(params):
    """The classic derived row: saturation from temperature."""
    return 2.0 * float(params.water_temp_c)


def _wq_params():
    return [
        Param("water_temp_c", desc="water temperature", door=doors.SCENARIO,
              default=20.0, bounds=(0.0, 40.0), units="C"),
        Param("sat_mgl", desc="DO saturation", door=doors.DERIVED,
              resolve=f"{_HERE}.derive_saturation", bounds=(0.0, 200.0),
              units="mg/L"),
    ]


@pytest.mark.asyncio
async def test_a_revision_re_derives_the_rows_that_consume_it(monkeypatch):
    """20 C derived 40 mg/L. The user approved 30 C, so the sheet that RUNS must
    say 60 - a derived row left on its pre-revision value contradicts the very
    value it is derived from."""
    _review({"water_temp_c": 30.0}, monkeypatch)
    decl = _wq_params()
    p = await resolve_params(decl, {})
    assert p.row("sat_mgl").value == 40.0
    plan = Plan("rederive", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"t": p.water_temp_c, "sat": p.sat_mgl}).named("solve"),
    ))
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    assert out.value["seen"] == {"t": 30.0, "sat": 60.0}
    row = next(e for e in out.entries if e.param == "sat_mgl")
    assert row.value == 60.0 and row.basis == "derived"
    assert "re-derived" in (row.note or "") and "water_temp_c" in (row.note or "")


@pytest.mark.asyncio
async def test_a_user_pinned_derived_row_beats_the_re_derivation(monkeypatch):
    """User wins. The re-derivation is REPORTED on the row, never applied over an
    explicit value."""
    _review({"water_temp_c": 30.0}, monkeypatch)
    decl = _wq_params()
    p = await resolve_params(decl, {"sat_mgl": 7.5})
    plan = Plan("rederive_pin", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"t": p.water_temp_c, "sat": p.sat_mgl}).named("solve"),
    ))
    out = await interpret(plan, p, decl, input_mode="user_gated", resume=False)
    assert out.value["seen"] == {"t": 30.0, "sat": 7.5}
    row = next(e for e in out.entries if e.param == "sat_mgl")
    assert row.value == 7.5 and row.basis == "user"
    assert "stands" in (row.note or "") and "60" in (row.note or "")


# --- R3-3: a revision invalidates the data produced from the old values ------ #
async def stub_dem(**kwargs):
    _CALLS.append("stub_dem")
    return {"uri": f"s3://b/dem-{kwargs['res']}.tif", "dem": f"dem@{kwargs['res']}"}


@pytest.mark.asyncio
async def test_a_revision_evicts_the_data_produced_from_the_old_values(monkeypatch):
    """A producer's kwargs carry its reads, so "did this artifact consume a revised
    param" is answerable. The pre-gate fetch at 30 m may not survive an approved
    3 m."""
    _review({"res_m": 3.0}, monkeypatch)
    decl = [Param("res_m", desc="target resolution", door=doors.SCENARIO,
                  default=30.0, bounds=(1.0, 100.0), units="m")]
    p = await resolve_params(decl, {})
    data = [Data("terrain", Fetch.tool(f"{_HERE}.stub_dem", res=p.res_m))]
    plan = Plan("evict", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"dem": Ref("terrain.dem")}).named("pre"),
        FormGate(),
        Step(runner=f"{_HERE}.stub_step", consequential=True,
             kwargs={"dem": Ref("terrain.dem")}).named("solve"),
    ))
    out = await interpret(plan, p, decl, data, input_mode="user_gated", resume=False)
    assert _CALLS.count("stub_dem") == 2                     # refetched, not reused
    assert out.results["solve"]["value"]["dem"] == "dem@3.0"
    assert out.results["pre"]["seen"]["dem"] == "dem@30.0"


@pytest.mark.asyncio
async def test_data_that_reads_no_revised_param_is_not_evicted(monkeypatch):
    """Eviction is targeted, not a blanket refetch: an artifact the revision
    cannot have changed keeps its resume value."""
    _review({"other": 9.0}, monkeypatch)
    decl = [Param("res_m", desc="resolution", door=doors.CONSTANT, default=30.0, type=float),
            Param("other", desc="unrelated", door=doors.SCENARIO, default=1.0, type=float)]
    p = await resolve_params(decl, {})
    data = [Data("terrain", Fetch.tool(f"{_HERE}.stub_dem", res=p.res_m))]
    plan = Plan("evict_none", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"dem": Ref("terrain.dem")}).named("pre"),
        FormGate(),
        Step(runner=f"{_HERE}.stub_step", consequential=True,
             kwargs={"dem": Ref("terrain.dem")}).named("solve"),
    ))
    await interpret(plan, p, decl, data, input_mode="user_gated", resume=False)
    assert _CALLS.count("stub_dem") == 1


# --- R3-4 / R4-3: the law-9 floor needs a review SURFACE, not just a session -- #
class _FakeEmitter:
    """Just enough emitter for the interpreter: a live session to pause on."""

    session_id = "sess-1d"

    def begin_substeps(self, total):
        return None

    @contextlib.asynccontextmanager
    async def substep(self, raw_name):
        yield "child"


def _physics_only():
    return [Param("aquifer_k_ms", desc="hydraulic conductivity",
                  door=doors.SCENARIO, default=1e-4, type=float, units="m/s",
                  consequence="physics")]


def _gateless_plan(*, consequential, self_gating=False):
    return Plan("law9_mode", None, (
        Step(runner=f"{_HERE}.stub_step", consequential=consequential,
             self_gating=self_gating).named("solve"),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", None, "user_gated"])
async def test_the_law9_floor_refuses_in_every_mode_without_a_live_session(mode):
    """user_gated with NO emitter is the headless direct call: the caller asked for
    review and there is nobody to review. That is not a licence to invent."""
    with pytest.raises(Exception) as exc:
        await _run(_gateless_plan(consequential=True), _physics_only(), {},
                   input_mode=mode, resume=False)
    assert exc.value.error_code == "PHYSICS_INPUT_REQUIRED"
    assert _CALLS == []


@pytest.mark.asyncio
async def test_a_self_gating_step_owns_the_approval_instead(monkeypatch):
    """The exemption needs a REVIEW SURFACE, and a self-gating step is one: it puts
    its own card in front of the live session. Then the floor steps aside."""
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "current_emitter", lambda: _FakeEmitter())
    await _run(_gateless_plan(consequential=True, self_gating=True), _physics_only(),
               {}, input_mode="user_gated", resume=False)
    assert _CALLS == ["stub_step"]


@pytest.mark.asyncio
async def test_a_live_session_with_no_card_anywhere_still_refuses(monkeypatch):
    """An emitter is where a card COULD be shown, never evidence that one was. A
    gateless plan whose step does not review its own inputs has no review surface
    at all, so a live user_gated session must not be the softer path."""
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "current_emitter", lambda: _FakeEmitter())
    with pytest.raises(Exception) as exc:
        await _run(_gateless_plan(consequential=True), _physics_only(), {},
                   input_mode="user_gated", resume=False)
    assert exc.value.error_code == "PHYSICS_INPUT_REQUIRED"
    assert "nothing in this workflow reviews these values" in str(exc.value)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_the_law9_floor_does_not_wait_for_a_consequential_step():
    """An invented physics value poisons the prep as surely as the solve, and a
    plan that tags nothing consequential would otherwise skip the floor."""
    with pytest.raises(Exception, match="PHYSICS_INPUT_REQUIRED"):
        await _run(_gateless_plan(consequential=False), _physics_only(), {},
                   resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_no_step_runs_before_the_law9_refusal():
    plan = Plan("law9_prefix", None, (
        Step(runner=f"{_HERE}.stub_second").named("prep"),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    with pytest.raises(Exception, match="PHYSICS_INPUT_REQUIRED"):
        await _run(plan, _physics_only(), {}, resume=False)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_law9_refuses_under_one_code_whether_or_not_a_form_gate_declares_it():
    """Callers route on the REASON. A gate refusal and the gateless floor are the
    same refusal, so they carry the same code."""
    decl = _physics_only()
    gated = Plan("law9_gated", None, (
        FormGate(),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    with pytest.raises(Exception) as via_gate:
        await _run(gated, decl, {}, resume=False)
    with pytest.raises(Exception) as via_floor:
        await _run(_gateless_plan(consequential=True), decl, {}, resume=False)
    assert via_gate.value.error_code == via_floor.value.error_code \
        == "PHYSICS_INPUT_REQUIRED"


# --- R3-5: a re-key REAPS the key it moved away from ------------------------- #
@pytest.mark.asyncio
async def test_a_revision_reaps_the_ledger_it_re_keyed_away_from(monkeypatch):
    """The pre-gate step recorded under the ORIGINAL key, then the run continued
    under the approved one. Leaving that document behind orphans records nobody
    can resume from, computed from the values the review replaced."""
    _review({"base": 3.5}, monkeypatch)
    decl = _params()
    p = await resolve_params(decl, {"base": 1.0})
    original = invocation_key("rekey_reap", p.values_dict(), input_mode="user_gated")
    plan = Plan("rekey_reap", None, (
        Step(runner=f"{_HERE}.stub_second").named("pre"),
        FormGate(),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    await interpret(plan, p, decl, input_mode="user_gated")
    revised = invocation_key("rekey_reap", {**p.values_dict(), "base": 3.5},
                             input_mode="user_gated")
    assert await _raw_ledger_doc(original) is None
    assert (await _raw_ledger_doc(revised))["complete"] is True


# --- observations 1 and 2: plan shapes that cannot honor a revision ---------- #
def test_validator_refuses_a_gate_as_the_last_node():
    plan = Plan("tail_gate", None, (
        Step(runner=f"{_HERE}.stub_step").named("s"),
        FormGate(),
    ))
    with pytest.raises(PlanValidationError, match="LAST node"):
        validate_plan(plan, _params())


# --- observation 6: a binding fault is a typed plan error -------------------- #
_Point = collections.namedtuple("_Point", "lon lat")


class _HostileTuple(tuple):
    """A tuple subclass whose constructor is NOT ``type(x)(iterable)``."""

    def __new__(cls, a, b):
        return super().__new__(cls, (a, b))


@pytest.mark.asyncio
async def test_a_namedtuple_kwarg_keeps_its_shape_through_binding():
    plan = Plan("nt", None, (
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"pt": _Point(Ref("first.uri"), 2.0)}).named("second"),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert out.value["seen"]["pt"] == _Point("s3://b/k.tif", 2.0)


@pytest.mark.asyncio
async def test_a_binding_fault_arrives_typed_not_raw():
    plan = Plan("bindfail", None, (
        Step(runner=f"{_HERE}.stub_step").named("first"),
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"t": _HostileTuple(Ref("first.uri"), 1)}).named("second"),
    ))
    with pytest.raises(StepFailedError) as exc:
        await _run(plan, _params(), {}, resume=False)
    assert exc.value.error_code == "STEP_ARGS_UNBINDABLE"
    assert exc.value.step == "second"


# --- R4-1: the leak guard never passes on an exhausted budget ---------------- #
def _deep_clean(depth):
    """A clean nested structure whose node count the scan budget can be set under."""
    node = {"leaf": 1.0}
    for i in range(depth):
        node = {"n": node, "i": i}
    return node


def test_a_budget_exhausted_leak_scan_warns_and_names_the_surface(monkeypatch):
    """A scan that stopped looking has NOT found the surface clean. Silence there
    let a leak behind a large value pass as verified."""
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "_LEAK_SCAN_BUDGET", 8)
    with pytest.warns(LeakScanTruncated, match="'value'"):
        _interp._refuse_leaked_param_refs(
            {"value": _deep_clean(50)}, "a test surface")


def test_a_clean_scan_inside_the_budget_warns_about_nothing(monkeypatch):
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "_LEAK_SCAN_BUDGET", 500)
    with warnings.catch_warnings():
        warnings.simplefilter("error", LeakScanTruncated)
        _interp._refuse_leaked_param_refs({"value": _deep_clean(50)}, "a test surface")


def test_a_large_value_cannot_starve_the_entries_scan(monkeypatch):
    """Per-surface budgets: one shared budget let a 60k-node value spend it all and
    leave the entries - where the leak actually was - never looked at."""
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "_LEAK_SCAN_BUDGET", 20)
    with pytest.warns(LeakScanTruncated, match="'value'"):
        with pytest.raises(ParamRefLeakedError, match=r"ParamRef\('base'\)"):
            _interp._refuse_leaked_param_refs(
                {"value": _deep_clean(200), "entries": [{"param": ParamRef("base")}]},
                "a test surface")


@pytest.mark.asyncio
async def test_the_run_warns_rather_than_silently_passing_a_truncated_scan(monkeypatch):
    """The guard is a floor, not a gate: an over-budget surface still runs, but the
    partial check is said out loud rather than reported as clean."""
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "_LEAK_SCAN_BUDGET", 4)
    plan = Plan("truncated", None, (Step(runner=f"{_HERE}.stub_deep").named("a"),))
    with pytest.warns(LeakScanTruncated):
        out = await _run(plan, _params(), {}, resume=False)
    assert out.value["uri"] == "s3://b/k.tif"


# --- the read-recording machinery is GONE: a read is just a read ------------- #
@pytest.mark.asyncio
async def test_a_concrete_read_is_value_of_and_nothing_watches_it():
    """The plan is STATIC - it reads no concrete value - so there is no
    plan-construction read left to record, and the machinery that recorded one is
    gone with the branch check it fed."""
    for gone in ("get", "concrete_reads", "freeze_reads"):
        assert not hasattr(ResolvedParams, gone)
    p = await resolve_params(_params(), {})
    assert p.value_of("base") == 1.0
    assert p.value_of("ghost", "fallback") == "fallback"
    assert p.row("base").value == 1.0
    assert p.values_dict()["base"] == 1.0
    assert [r.name for r in p.rows()] == ["base", "pt"]
    assert p.values_view().base == 1.0
    assert p.values_view().get("base") == 1.0
    # ...and the LATE-bound attribute read still describes rather than resolves.
    assert isinstance(p.base, ParamRef) and p.base.name == "base"


# --- R4-5: a re-key leaves a TOMBSTONE, not a replayable orphan ------------- #
@pytest.mark.asyncio
async def test_a_failed_reap_at_re_key_leaves_a_non_replayable_marker(monkeypatch):
    """Deleting was enough only when the delete SUCCEEDED. A swallowed failure left
    the abandoned key's records complete:false and replayable for the whole TTL -
    the wave-1b replay ghost, back through the re-key door."""
    _review({"base": 3.5}, monkeypatch)
    _ledger = importlib.import_module("trid3nt_server.workflows.lib.ledger")

    async def _boom(client, key):
        raise RuntimeError("delete-one is down")

    decl = _params()
    p = await resolve_params(decl, {"base": 1.0})
    original = invocation_key("rekey_fail", p.values_dict(), input_mode="user_gated")
    plan = Plan("rekey_fail", None, (
        Step(runner=f"{_HERE}.stub_second").named("pre"),
        FormGate(),
        Step(runner=f"{_HERE}.stub_step", consequential=True).named("solve"),
    ))
    monkeypatch.setattr(_ledger, "_reap", _boom)
    await interpret(plan, p, decl, input_mode="user_gated")
    orphan = await _raw_ledger_doc(original)
    assert orphan is not None, "the reap failed, so the document is still there"
    assert orphan["complete"] is True
    assert orphan["records"] == [] and orphan["data_records"] == []


@pytest.mark.asyncio
async def test_a_tombstoned_orphan_key_cannot_be_replayed(monkeypatch):
    """What the marker BUYS: the abandoned key hands nothing back to a rerun."""
    _ledger = importlib.import_module("trid3nt_server.workflows.lib.ledger")

    async def _boom(client, key):
        raise RuntimeError("delete-one is down")

    ledger = await _ledger.StepLedger.load("orphan_key", "w")
    await ledger.record(_ledger.LedgerRecord(index=0, node="a", runner="r",
                                             completed_at="2026-08-23T00:00:00+00:00"))
    monkeypatch.setattr(_ledger, "_reap", _boom)
    await ledger.clear()
    reloaded = await _ledger.StepLedger.load("orphan_key", "w")
    assert reloaded.replay_for(0, "a") is None


# --- the chart PAYLOAD is the surface, not the node's return dict ------------ #
@pytest.mark.asyncio
async def test_a_leaked_ref_in_a_chart_payload_never_reaches_the_wire(monkeypatch):
    """The node returns a small marker dict; the PAYLOAD is what goes over the WS.
    Guarding only the marker let a ref in a chart title through."""
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    emitted = []

    async def _emit(payload):
        emitted.append(payload)

    monkeypatch.setattr(_interp, "emit_chart_payloads", _emit)
    plan = Plan("chart_leak", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart_leaks),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert emitted == []
    assert out.value["uri"] == "s3://b/k.tif"       # the primary result stands
    assert len(out.notes) == 1 and "ParamRef('base')" in out.notes[0]


@pytest.mark.asyncio
async def test_a_clean_chart_payload_still_reaches_the_wire(monkeypatch):
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    emitted = []

    async def _emit(payload):
        emitted.append(payload)

    monkeypatch.setattr(_interp, "emit_chart_payloads", _emit)
    plan = Plan("chart_ok", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert emitted == [{"chart_id": "c1", "title": "t"}]
    assert out.notes == []
    # The SPEC is the product: the run carries its own chart out so the caller can
    # persist it, rather than leaving a reader to rebuild one from the scalars.
    assert out.charts == {"c": {"chart_id": "c1", "title": "t"}}


@pytest.mark.asyncio
async def test_a_chart_that_failed_leaves_no_spec_to_persist(monkeypatch):
    _interp = importlib.import_module("trid3nt_server.workflows.lib.interpreter")
    monkeypatch.setattr(_interp, "emit_chart_payloads", lambda payload: _noop())
    plan = Plan("chart_none", None, (
        Step(runner=f"{_HERE}.stub_step").named("a")
        .chart("c", builder=stub_chart_empty),
    ))
    out = await _run(plan, _params(), {}, resume=False)
    assert out.charts == {} and len(out.notes) == 1


async def _noop():
    return None


# ============================================================================ #
# The plan is a STATIC value: built once, at declaration, checked there
# ============================================================================ #
class _StubFacade(Workflow):
    """A facade realizing the EngineOps five, so registration gets past the hole
    check and reaches the thing under test: the plan built in ``__init__``."""

    engine = "stub"

    def acquire_domain(self, **slots):
        return (Step(runner=f"{_HERE}.stub_step").named("aoi"),)

    def build_mesh(self, domain, policy, **slots):
        return Step(runner=f"{_HERE}.stub_producer").named("mesh")

    def author(self, *, mesh, physics, forcing):
        return Step(runner=f"{_HERE}.stub_step").named("deck")

    def solver_spec(self, **slots):
        return Step(runner=f"{_HERE}.stub_step").named("solve")

    def read_results(self, run, **slots):
        return Step(runner=f"{_HERE}.stub_product").named("out")


def _declare(params, plan_decl, data=(), name="declared_w"):
    """Declare a workflow the way ``register_workflow`` does: the plan is built and
    validated inside ``__init__``, so an authoring defect raises HERE."""
    return _StubFacade(metadata=SimpleNamespace(name=name, engine="stub"),
                       params=params, plan=plan_decl, data=data)


def test_a_mistyped_param_read_is_refused_at_declaration_with_its_write_site():
    """The refusal fires an import away from the line that wrote the ref, and the
    sheet it is checked against runs to dozens of names - so the site and the
    nearest declared spelling are the whole value of the message."""
    def _plan(ops):
        return (Step(runner=f"{_HERE}.stub_step", kwargs={"x": P.bse}).named("s"),)

    with pytest.raises(PlanValidationError) as exc:
        _declare(_params(), _plan)
    message = str(exc.value)
    assert "P.bse names no declared param" in message
    assert "written at test_declarative_library.py:" in message
    assert "Closest declared: base" in message


def test_a_mistyped_data_read_is_refused_and_says_it_is_a_data_name():
    """``D.terain`` is a Data typo, not a step nobody named - which namespace the
    bad name came from is the difference between two very different hunts."""
    def _plan(ops):
        return (Step(runner=f"{_HERE}.stub_second",
                     kwargs={"m": D.terain}).named("s"),)

    data = [Data("terrain", Build.tool(f"{_HERE}.stub_producer"))]
    with pytest.raises(PlanValidationError) as exc:
        _declare(_params(), _plan, data)
    message = str(exc.value)
    assert "D.terain names no declared Data" in message
    assert "written at test_declarative_library.py:" in message
    assert "Declared Data: ['terrain']" in message


def test_a_bad_plan_is_refused_at_register_workflow_not_at_run_time():
    """Registration is the last moment an authoring defect is still an authoring
    defect; after it, the same hole surfaces mid-run as an engine failure."""
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    def _plan(ops):
        return (Step(runner=f"{_HERE}.stub_step", kwargs={"x": P.ghost}).named("s"),)

    metadata = AtomicToolMetadata(name="never_registered_w", ttl_class="live-no-cache",
                                  source_class="workflow_dispatch", cacheable=False,
                                  engine="stub", tier="template")
    with pytest.raises(PlanValidationError, match="P.ghost names no declared param"):
        register_workflow(_StubFacade, metadata, _params(), _plan)

    from trid3nt_server.tools import TOOL_REGISTRY
    assert "never_registered_w" not in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_the_plan_is_built_once_at_declaration_not_per_run():
    """A STATIC plan reads no concrete value, so rebuilding it per run would buy
    nothing and cost the one guarantee it does buy: what was validated is what runs."""
    built = []

    def _plan(ops):
        built.append(ops)
        return (Step(runner=f"{_HERE}.stub_product").named("a"),)

    wf = _declare(_params(), _plan, name="built_once_w")
    assert len(built) == 1 and built[0] is wf
    declared = wf.plan
    assert wf.plan is declared

    await wf.run({"base": 2.0})
    await wf.run({"base": 3.0})
    assert _CALLS == ["stub_product", "stub_product"]
    assert len(built) == 1                      # two runs, one plan construction
    assert wf.plan is declared


def test_build_plan_takes_no_sheet():
    """The signature IS the contract: a sheet argument is what a plan that read
    concrete values needed, and there is no such plan any more."""
    import inspect

    def _plan(ops):
        return (Step(runner=f"{_HERE}.stub_step").named("a"),)

    wf = _declare(_params(), _plan, name="nosheet_w")
    assert list(inspect.signature(wf.build_plan).parameters) == []
    rebuilt = wf.build_plan()
    assert [s.label for s in rebuilt.declared()] == [s.label for s in wf.plan.declared()]


# --- a binding block is a process-lifetime value, so it is frozen DEEP -------- #
def test_a_binding_block_is_frozen_all_the_way_down():
    """A module-level block is shared by every run of the template, so a mutable
    container inside one is a cross-run channel: a step that pops a key out of a
    declared dict changes what the NEXT run declares."""
    block = Physics("tracer", cfg={"scheme": "upwind", "rungs": [1, 2]},
                    decay=P.base)

    assert isinstance(block.cfg, MappingProxyType)
    assert block.cfg["rungs"] == (1, 2)          # the nested list became a tuple
    assert isinstance(block.values, MappingProxyType)
    assert block.process == "tracer"
    with pytest.raises(TypeError):
        block.cfg["scheme"] = "central"
    with pytest.raises(PlanValidationError):
        block.cfg = {}
    # a declared read passes through untouched - it is already a value
    assert isinstance(block.decay, ParamRef) and block.decay.name == "base"


# --- what the validator walks, the binder must bind --------------------------- #
def test_a_ref_inside_a_frozen_mapping_is_not_invisible_to_the_validator():
    """A binding block is deep-frozen into MappingProxyType, which is a Mapping and
    not a dict - so a walk that descended dicts alone would pass a plan carrying a
    ref to nothing straight through to the run."""
    block = deep_freeze({"cfg": {"decay": ParamRef("ghost")}})
    plan = Plan("frozen_param_w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"physics": block}),))
    assert isinstance(block["cfg"], MappingProxyType)
    with pytest.raises(PlanValidationError, match="P.ghost names no declared param"):
        validate_plan(plan, _params())


def test_a_data_ref_inside_a_frozen_mapping_is_not_invisible_to_the_validator():
    block = deep_freeze({"cfg": {"zone": DataRef("ghost_zone")}})
    plan = Plan("frozen_data_w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"physics": block}),))
    with pytest.raises(PlanValidationError, match="D.ghost_zone names no declared Data"):
        validate_plan(plan, _params(), [Data("clip_zone")])


@pytest.mark.asyncio
async def test_a_ref_inside_a_frozen_mapping_reaches_the_runner_bound():
    """The binder honors every read the validator counted; a frozen mapping arrives
    as a plain dict because a read-only proxy has no constructor to rebuild it."""
    block = deep_freeze({"cfg": {"decay": ParamRef("base"), "zone": DataRef("clip_zone")}})
    plan = Plan("frozen_bind_w", None, (
        Step(runner=f"{_HERE}.stub_second", kwargs={"physics": block}).named("a"),))
    out = await _run(plan, _params(), {"base": 4.0}, [Data("clip_zone")], resume=False,
                     supplied={"clip_zone": "s3://mine/zone.gpkg"},
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    bound = out.value["seen"]["physics"]
    assert isinstance(bound, dict) and not isinstance(bound, MappingProxyType)
    assert bound["cfg"] == {"decay": 4.0, "zone": "s3://mine/zone.gpkg"}


def test_a_data_ref_refuses_every_read_that_would_turn_it_into_data():
    """``D.<name>`` describes an artifact the interpreter has not produced yet, so
    the three silent conversions - a branch, str(), an f-string - all refuse."""
    ref = DataRef("mesh")
    with pytest.raises(PlanValidationError, match="truth-value testing"):
        bool(ref)
    with pytest.raises(PlanValidationError, match=r"str\(\)"):
        str(ref)
    with pytest.raises(PlanValidationError, match="f-string"):
        f"{ref}"
    with pytest.raises(PlanValidationError, match="f-string"):
        f"{D.mesh}"
    assert repr(ref) == "DataRef('mesh')"       # naming it is what a diagnostic does


# --- a context slot's declared SHAPE is checked at the front door ------------- #
@pytest.mark.asyncio
async def test_a_supplied_artifact_of_the_wrong_shape_is_refused_typed():
    data = [Data("structure").supplied(geometry="polyline").optional()]
    plan = Plan("shape_w", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"s": Ref("structure")}).named("a"),))
    with pytest.raises(SuppliedGeometryError) as exc:
        await _run(plan, _params(), {}, data, resume=False,
                   supplied={"structure": "s3://mine/terrain.tif"},
                   domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert exc.value.error_code == "SUPPLIED_GEOMETRY_MISMATCH"
    assert "raster" in str(exc.value) and _CALLS == []


@pytest.mark.asyncio
async def test_a_supplied_artifact_of_the_declared_shape_is_adopted():
    """Suffix-deep and no deeper: a vector satisfies a polyline slot here, and
    whether its features are lines is the consumer's species reader's answer."""
    data = [Data("structure").supplied(geometry="polyline").optional()]
    plan = Plan("shape_ok_w", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"s": Ref("structure")}).named("a"),))
    out = await _run(plan, _params(), {}, data, resume=False,
                     supplied={"structure": "s3://mine/breakwater.fgb"},
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert out.value["seen"]["s"] == "s3://mine/breakwater.fgb"


@pytest.mark.asyncio
async def test_an_unclassifiable_supplied_artifact_is_adopted_not_guessed_at():
    """A layer name carries no suffix, and a refusal must never rest on a guess."""
    data = [Data("structure").supplied(geometry="polyline").optional()]
    plan = Plan("shape_unknown_w", None, (
        Step(runner=f"{_HERE}.stub_second",
             kwargs={"s": Ref("structure")}).named("a"),))
    out = await _run(plan, _params(), {}, data, resume=False,
                     supplied={"structure": "harbor-breakwater-layer"},
                     domain=Domain(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert out.value["seen"]["s"] == "harbor-breakwater-layer"
