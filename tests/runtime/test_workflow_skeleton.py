"""The workflow SKELETON itself: the template method, the facade, the factory.

Offline, nothing here solves. Hooks default silently; the factory synthesizes the
wire signature from the declared params and renders the docstring from that same
narrowed set; slot and signature are checked both ways; the facade's four MUST be
filled; a coercion failure is triaged; a guessable-wrong wire type refuses."""

from __future__ import annotations

import inspect
from types import MappingProxyType
from typing import Any

import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Param,
    PlanValidationError,
    Ref,
    Step,
    Workflow,
    doors,
)


# --- (2) hooks: silent by default, effective when filled --------------------- #
def _metadata(name: str) -> AtomicToolMetadata:
    return AtomicToolMetadata(name=name, ttl_class="live-no-cache",
                              source_class="workflow_dispatch", cacheable=False,
                              engine="telemac", tier="template")


class _Layer:
    """The minimum a published result has to look like to the publish stage."""

    def __init__(self) -> None:
        self.uri = "s3://b/k.tif"
        self.layer_id = "L"
        self.fallback_note = None
        self.synthetic_inputs: list[Any] = []
        self.depth_max_m = 1.5

    def model_copy(self, *, update: dict[str, Any]) -> "_Layer":
        for key, value in update.items():
            setattr(self, key, value)
        return self


class _Stub(Workflow):
    """A workflow whose template states its steps under STEPS."""

    def steps(self):
        return self.template.STEPS(self)


def _module(plan, **names):
    from types import SimpleNamespace

    return SimpleNamespace(STEPS=plan, **names)


def _workflow(cls=_Stub, **kw):
    return cls(metadata=_metadata("skeleton_probe"), params=(),
               template=_module(lambda o: ()), **kw)


def test_the_hooks_are_silent_by_default():
    wf = _workflow()
    from trid3nt_server.workflows.runtime.workflow import RunResult

    assert wf.checks(RunResult(value=_Layer())) == ()


def test_the_skeleton_emits_no_input_layer_of_its_own():
    """The steps that fetch inputs emit through the ONE seam; a skeleton-level
    second emitter would be the double-emission the single-path guard catches."""
    import inspect as _inspect

    from trid3nt_server.workflows.runtime import workflow as mod

    src = _inspect.getsource(mod)
    assert "publish_input_layer" not in src
    assert "publish_raster_input_cog" not in src


@pytest.mark.asyncio
async def test_a_filled_check_hook_reaches_the_result_as_a_note(monkeypatch):
    from trid3nt_server.workflows.runtime import RunResult
    from trid3nt_server.workflows.runtime import run_products

    class Checked(_Stub):
        def checks(self, run):
            return (f"depth {run.value.depth_max_m} m is a screening figure",)

    async def _no_persist(run_id, *, charts):
        return []

    monkeypatch.setattr(run_products, "persist_run_products", _no_persist)
    wf = _workflow(Checked)
    out = await wf._publish(RunResult(value=_Layer()))
    assert "NOTE: depth 1.5 m is a screening figure" in out.fallback_note


# --- (3) the registration factory synthesizes the wire ---------------------- #
def test_the_generated_signature_is_the_declaration_plus_aliases_and_controls():
    from trid3nt_server.workflows.runtime.workflow import _wire_signature

    params = (
        Param("location", door=doors.QUESTION, optional=True, desc="a place"),
        Param("depth_m", door=doors.SCENARIO, default=1.0, bounds=(0.0, 9.0),
              desc="a bounded value"),
        Param("armed", door=doors.USER, optional=True, type=bool, desc="a flag"),
        Param("derived_only", door=doors.USER, optional=True, wire=False,
              desc="resolved by a coercion, never sent"),
        Param("solver_dt_s", door=doors.CONSTANT, default=1.0, bounds=(0.1, 60.0),
              desc="non-question numerics the model is never asked for"),
    )
    sig, annotations = _wire_signature(params, (("alias", str | None),))
    assert list(sig.parameters) == ["location", "depth_m", "armed", "alias",
                                    "input_mode", "restart_clean", "keywords",
                                    "picks", "ops", "_extra_ignored"]
    assert annotations["depth_m"] == (float | None)      # bounded -> float
    assert annotations["armed"] == (bool | None)         # declared type wins
    assert annotations["location"] == (str | None)       # inferred
    assert sig.parameters["restart_clean"].default is False
    assert all(sig.parameters[n].default is None
               for n in ("location", "depth_m", "armed", "alias", "input_mode",
                         "keywords"))
    assert sig.parameters["_extra_ignored"].kind is inspect.Parameter.VAR_KEYWORD


def test_a_declared_param_the_wire_does_not_expose_stays_off_the_real_tool():
    from trid3nt_server.tools import TOOL_REGISTRY

    wf = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    declared = {p.name for p in wf.params}
    constants = {p.name for p in wf.params if p.door == doors.CONSTANT}
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_dye_release"].fn).parameters)
    assert declared - constants <= wire


def test_constant_door_params_are_off_the_model_facing_wire_and_docstring():
    """The door is a BINDING AUTHORITY contract: a constant is nobody's question.

    A constant is neither an argument the model may fill nor a row the prose sheet
    advertises; it lives its whole life on the ``ParamSheet``."""
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in ("telemac_do_sag", "telemac_dye_release"):
        fn = TOOL_REGISTRY[name].fn
        constants = {p.name for p in fn.workflow.params if p.door == doors.CONSTANT}
        assert constants, f"{name} declares no constants; the check is vacuous"
        wire = set(inspect.signature(fn).parameters)
        assert not (constants & wire), f"{name} puts {constants & wire} on the wire"
        listed = {c for c in constants if f"    {c}:" in (fn.__doc__ or "")}
        assert not listed, f"{name} documents {listed}, which its schema refuses"


def test_a_constant_supplied_off_the_model_wire_still_reaches_the_sheet():
    """The exclusion is the SCHEMA's, so the form-edit lane keeps its lever.

    That lane hands the workflow a sheet rather than a model tool call, so the value
    is seated through the USER door and the row reads ``basis=user``."""
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.resolver import resolve_params

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    supplied, err = asyncio.run(wf._normalize(
        {"location": "x", "cores": 2, "mesh_resolution_m": 30.0}))
    assert err is None
    assert supplied["cores"] == 2
    assert supplied["mesh_resolution_m"] == 30.0
    sheet = asyncio.run(resolve_params(wf.params, supplied))
    assert sheet.value_of("cores") == 2
    assert sheet.row("cores").basis == "user"


# --- (4) a chart builder is the function, with no string fallback ----------- #
def test_a_dotted_string_chart_builder_is_refused_with_the_fix_in_the_message():
    step = Step(runner="pkg.mod.fn")
    with pytest.raises(PlanValidationError) as ei:
        step.chart("c", builder="pkg.mod.build_chart")
    assert "function object" in str(ei.value)
    assert "pkg.mod.build_chart" in str(ei.value)


def test_a_chart_records_where_its_builder_lives():
    def build(*, result, params):
        return {}

    step = Step(runner="pkg.mod.fn").chart("c", builder=build)
    assert step.charts[0].builder_path.endswith(
        "test_workflow_skeleton.test_a_chart_records_where_its_builder_lives"
        ".<locals>.build")


# --- (5) an unknown slot member is refused at plan construction ------------- #
def _telemac():
    """A telemac-engined workflow whose template states its steps outright: what
    the NAME and the ENGINE on the plan come from is what is under test, not the
    stages a real TELEMAC template's slots build."""
    class Probe(_Stub):
        engine = "telemac"

    return Probe(metadata=_metadata("telemac_probe"), params=(),
                 template=_module(lambda o: (Step(runner="pkg.mod.fn"),)))


def _reach_mesh(**params):
    """The MESH recipe a reach template writes, with test values for its ask."""
    from trid3nt_server.mesh.tool import tool

    params.setdefault("extent", Ref("reach_polygon"))
    return tool.build_mesh(mesher="om2d", kind="unstructured_tri", **params)


def test_the_plan_reads_a_data_name_off_the_templates_own_body():
    """A read is attribute access on the template's own DATA, which is what lets a
    binding block sit at module level above the plan it feeds - and what makes a
    name the template does not declare unwritable."""
    from trid3nt_server.workflows.runtime import DataRef, tool

    class DATA:
        rivers = tool("fetch_river_geometry")

    assert DATA.rivers == DataRef("rivers")
    with pytest.raises(AttributeError):
        DATA.riverz


def test_the_skeleton_names_and_engines_the_plan_the_template_does_not():
    plan = _telemac().build_plan()
    assert plan.name == "telemac_probe"      # from the metadata
    assert plan.engine == "telemac"          # from the facade


def test_an_undeclared_data_name_refuses_at_registration_saying_it_is_a_data_name():
    """A ref built from a STRING has no body to refuse it, so the refusal is the
    VALIDATOR's - and it says which body the bad name claimed to come from."""
    from trid3nt_server.workflows.runtime import DataDecl, DataRef, tool

    def _plan(ops):
        return (Step(runner="pkg.mod.fn", kwargs={"r": DataRef("terain")}).named("s"),)

    with pytest.raises(PlanValidationError) as ei:
        _Stub(metadata=_metadata("data_probe"), params=(),
              template=_module(_plan),
              data=(DataDecl("terrain", tool("pkg.mod.fetch")),))
    message = str(ei.value)
    assert "DataRef('terain') names no declared Data" in message
    assert "Declared Data: ['terrain']" in message


# --- (7) a coercion's failure is triaged, never flattened ------------------- #
class _Retryable(Exception):
    """What a gate raises: the adapter harvests .suggestions off the RAISED object."""

    retryable = True
    suggestions = ("send location='Eel River, California'",)


@pytest.mark.asyncio
async def test_a_retryable_coercion_failure_propagates_with_its_suggestions():
    def _gate(args):
        raise _Retryable("pick one")

    wf = _workflow(coerce=(_gate,))
    with pytest.raises(_Retryable) as ei:
        await wf.run({})
    assert ei.value.suggestions  # the channel survived; nothing flattened it


@pytest.mark.asyncio
async def test_a_typed_coercion_refusal_keeps_its_own_error_code():
    from trid3nt_server.workflows.runtime import WireArgsError

    def _typed(args):
        raise WireArgsError("needs a location", error_code="TELEMAC_PARAMS_INCOMPLETE")

    out = await _workflow(coerce=(_typed,)).run({})
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


@pytest.mark.asyncio
async def test_a_BUG_in_a_coercion_reads_as_internal_never_as_the_caller_s_fault():
    """PARAMS_INVALID blames the caller for our own crash and sends the model off
    to 'fix' arguments that were never wrong."""
    def _buggy(args):
        return {"x": 1 / 0}

    out = await _workflow(coerce=(_buggy,)).run({})
    assert out["error_code"] == "TELEMAC_INTERNAL_ERROR"
    assert out["status"] == "error"


# --- (8) a declaration whose wire type would be guessed wrong refuses -------- #
def test_an_unbounded_numeric_default_refuses_rather_than_advertising_a_string():
    with pytest.raises(PlanValidationError) as ei:
        Param("k_per_day", door=doors.SCENARIO, default=0.3, desc="a rate")
    assert "STRING" in str(ei.value)
    # ... and both offered fixes are accepted
    assert Param("k_per_day", door=doors.SCENARIO, default=0.3,
                 bounds=(0.01, 20.0), desc="a rate").wire_type is float
    assert Param("k_per_day", door=doors.SCENARIO, default=0.3,
                 type=float, desc="a rate").wire_type is float


def test_a_bool_default_is_not_a_numeric_default():
    """bool IS an int in Python; a flag infers bool correctly and must not refuse."""
    assert Param("armed", door=doors.SCENARIO, default=False,
                 desc="a flag").wire_type is bool
