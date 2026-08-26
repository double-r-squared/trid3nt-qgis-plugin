"""The workflow SKELETON itself: the template method, the facade, the factory.

Offline. Nothing here solves; these pin the contract in
``docs/design/declarative-workflows.md`` "The Workflow Skeleton" (ADR 0312):

  1. the five EngineOps are ABSTRACT - an unrealized one refuses by name;
  2. hooks have SILENT defaults, and a FILLED hook reaches the result;
  3. the registration factory synthesizes the wire signature from the declared
     params (``wire=False`` and every CONSTANT-door param stay off it, aliases and
     controls join it) and renders the docstring from that same narrowed set;
  4. a chart builder is the FUNCTION - a dotted string is refused, no fallback;
  5. the slot/signature check runs BOTH ways: a member the deck writer does not
     accept, and a required member no slot covers, are both refused while the
     plan value is being BUILT;
  6. the facade's five are MUST-FILL - a hole refuses at registration, so a
     NotImplementedError never reaches a caller as an engine internal error;
  7. a coercion's failure is triaged, not flattened: retryable propagates, typed
     keeps its code, a bug in our own coercion reads as INTERNAL_ERROR;
  8. a declaration whose wire type would be silently guessed wrong refuses.
"""

from __future__ import annotations

import inspect
from types import MappingProxyType
from typing import Any

import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.lib import (
    EngineOps,
    Forcing,
    MeshPolicy,
    Param,
    Physics,
    PlanValidationError,
    Ref,
    Step,
    Workflow,
    doors,
)


# --- (1) the five operations are abstract ----------------------------------- #
@pytest.mark.parametrize("call", [
    lambda ops: ops.acquire_domain(),
    lambda ops: ops.build_mesh(None, MeshPolicy()),
    lambda ops: ops.author(mesh=None, physics=None, forcing=None),
    lambda ops: ops.solver_spec(),
    lambda ops: ops.read_results(None),
])
def test_an_unrealized_engine_operation_refuses_by_name(call):
    class BareEngine(EngineOps):
        pass

    with pytest.raises(NotImplementedError) as ei:
        call(BareEngine())
    assert "BareEngine" in str(ei.value)


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


def _workflow(cls=Workflow, **kw):
    return cls(metadata=_metadata("skeleton_probe"), params=(), plan=lambda o: (),
               answer=("depth_max_m",), **kw)


def test_the_hooks_are_silent_by_default():
    wf = _workflow()
    assert wf.checks(_Layer(), None) == ()


def test_the_skeleton_emits_no_input_layer_of_its_own():
    """The steps that fetch inputs emit through the ONE seam; a skeleton-level
    second emitter would be the double-emission the ADR 0244 guard catches."""
    import inspect as _inspect

    from trid3nt_server.workflows.lib import workflow as mod

    src = _inspect.getsource(mod)
    assert "publish_input_layer" not in src
    assert "publish_raster_input_cog" not in src


@pytest.mark.asyncio
async def test_a_filled_check_hook_reaches_the_result_as_a_note(monkeypatch):
    from trid3nt_server.workflows.lib import RunResult
    from trid3nt_server.workflows.shared import run_products

    class Checked(Workflow):
        def checks(self, result, run):
            return (f"depth {result.depth_max_m} m is a screening figure",)

    async def _no_persist(run_id, *, charts, metrics):
        return []

    monkeypatch.setattr(run_products, "persist_run_products", _no_persist)
    wf = _workflow(Checked)
    out = await wf._publish(RunResult(value=_Layer()))
    assert "NOTE: depth 1.5 m is a screening figure" in out.fallback_note
    # the ANSWER is the declared fields plus the layer's uri
    assert wf.answer(out) == {"depth_max_m": 1.5, "layer_uri": "s3://b/k.tif"}


# --- (3) the registration factory synthesizes the wire ---------------------- #
def test_the_generated_signature_is_the_declaration_plus_aliases_and_controls():
    from trid3nt_server.workflows.lib.workflow import _wire_signature

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
                                    "input_mode", "restart_clean", "_extra_ignored"]
    assert annotations["depth_m"] == (float | None)      # bounded -> float
    assert annotations["armed"] == (bool | None)         # declared type wins
    assert annotations["location"] == (str | None)       # inferred
    assert sig.parameters["restart_clean"].default is False
    assert all(sig.parameters[n].default is None
               for n in ("location", "depth_m", "armed", "alias", "input_mode"))
    assert sig.parameters["_extra_ignored"].kind is inspect.Parameter.VAR_KEYWORD


def test_a_declared_param_the_wire_does_not_expose_stays_off_the_real_tool():
    from trid3nt_server.tools import TOOL_REGISTRY

    wf = TOOL_REGISTRY["telemac_river_dye"].fn.workflow
    declared = {p.name for p in wf.params}
    constants = {p.name for p in wf.params if p.door == doors.CONSTANT}
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_river_dye"].fn).parameters)
    assert "reach_seed_coords" in declared and "reach_seed_coords" not in wire
    assert declared - {"reach_seed_coords"} - constants <= wire


def test_constant_door_params_are_off_the_model_facing_wire_and_docstring():
    """The door is a BINDING AUTHORITY contract: a constant is nobody's question.

    Both cohort templates carry real constants (bank source, channel width, the
    solve sizing class, the simulated window), and none of them is an argument the
    model may fill or a row the prose sheet advertises. They keep their whole life
    on the ``ParamSheet`` - the form card's advanced fold is where a user changes
    one - which is what makes this an authority contract rather than a deletion.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in ("telemac_do_sag", "telemac_river_dye"):
        fn = TOOL_REGISTRY[name].fn
        constants = {p.name for p in fn.workflow.params if p.door == doors.CONSTANT}
        assert constants, f"{name} declares no constants; the check is vacuous"
        wire = set(inspect.signature(fn).parameters)
        assert not (constants & wire), f"{name} puts {constants & wire} on the wire"
        listed = {c for c in constants if f"    {c}:" in (fn.__doc__ or "")}
        assert not listed, f"{name} documents {listed}, which its schema refuses"


def test_a_constant_supplied_off_the_model_wire_still_reaches_the_sheet():
    """The Tier-A / form-edit lane keeps its lever. The exclusion is the SCHEMA's.

    ``!run`` with every param supplied is the mechanical contract check, and its
    whole point is that nothing is left to ask - which includes pinning the
    constants a canary run has to shrink (a 600 s window instead of three hours).
    That lane hands the workflow a sheet, not a model tool call, so the value is
    seated through the USER door and the row reads ``basis=user``.
    """
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.resolver import resolve_params

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    supplied, err = wf._normalize({"location": "x", "sim_duration_s": 600.0,
                                   "mesh_resolution": "coarse"})
    assert err is None
    assert supplied["sim_duration_s"] == 600.0
    assert supplied["mesh_resolution"] == "coarse"
    sheet = asyncio.run(resolve_params(wf.params, supplied))
    assert sheet.value_of("sim_duration_s") == 600.0
    assert sheet.row("sim_duration_s").basis == "user"


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
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    return TelemacWorkflow(metadata=_metadata("telemac_probe"), params=(),
                           plan=lambda o: ())


def test_an_unknown_physics_member_is_refused_while_the_plan_is_built():
    ops = _telemac()
    mesh = ops.build_mesh(Ref("reach"), MeshPolicy())
    with pytest.raises(PlanValidationError) as ei:
        ops.author(mesh=mesh, physics=Physics("tracer", not_a_deck_field=1.0),
                   forcing=Forcing(carrier=Ref("carrier_discharge")))
    assert "not_a_deck_field" in str(ei.value)


def test_an_unknown_physics_PROCESS_is_refused_rather_than_authored():
    ops = _telemac()
    mesh = ops.build_mesh(Ref("reach"), MeshPolicy())
    with pytest.raises(PlanValidationError) as ei:
        ops.author(mesh=mesh, physics=Physics("magnetohydrodynamics"),
                   forcing=Forcing(carrier=Ref("carrier_discharge")))
    assert "magnetohydrodynamics" in str(ei.value)


def test_the_mesh_policy_reaches_the_deck_under_the_engine_s_own_names():
    from trid3nt_server.workflows.telemac.workflow import CorridorPolicy

    ops = _telemac()
    mesh = ops.build_mesh(
        Ref("reach"), MeshPolicy(resolution="coarse", target_edge_m=100.0),
        corridor=CorridorPolicy(extent_km=0.5, width_m=60.0,
                                boundary_source="nhd_area"))
    deck = ops.author(mesh=mesh, physics=Physics("tracer", substance="dye"),
                      forcing=Forcing(carrier=Ref("carrier_discharge"), rain=None))
    assert deck.name == "deck" and deck.stage == "author"
    assert deck.kwargs["mesh_resolution"] == "coarse"
    assert deck.kwargs["mesh_resolution_m"] == 100.0
    assert deck.kwargs["reach_length_km"] == 0.5
    assert deck.kwargs["channel_width_m"] == 60.0
    assert deck.kwargs["bank_source"] == "nhd_area"
    assert deck.kwargs["carrier_discharge"] == Ref("carrier_discharge")


def test_the_plan_reads_a_data_name_through_the_declaration_namespace():
    """``D`` carries no sheet and no workflow: a name is checked against the
    template's own DATA at registration, which is what lets a binding block sit at
    module level above the plan it feeds."""
    from trid3nt_server.workflows.lib import D, DataRef

    ref = D.rivers
    assert isinstance(ref, DataRef) and ref.path == "rivers"
    assert ref.origin.startswith("test_workflow_skeleton.py:")


def test_the_skeleton_names_and_engines_the_plan_the_template_does_not():
    ops = _telemac()
    ops.plan_decl = lambda o: (Step(runner="pkg.mod.fn"),)
    plan = ops.build_plan()
    assert plan.name == "telemac_probe"      # from the metadata
    assert plan.engine == "telemac2d"        # from the facade


def test_an_undeclared_data_name_refuses_at_registration_naming_its_write_site():
    """``D`` cannot refuse at the attribute - it has no workflow to check against -
    so the refusal moves to the VALIDATOR, which has the declared Data and can say
    which namespace the bad name came from and where it was written."""
    from trid3nt_server.workflows.lib import D, Data, Fetch

    def _plan(ops):
        return (Step(runner="pkg.mod.fn", kwargs={"r": D.terain}).named("s"),)

    with pytest.raises(PlanValidationError) as ei:
        Workflow(metadata=_metadata("data_probe"), params=(), plan=_plan,
                 data=(Data("terrain", Fetch.tool("pkg.mod.fetch")),))
    message = str(ei.value)
    assert "D.terain names no declared Data" in message
    assert "written at test_workflow_skeleton.py:" in message
    assert "Declared Data: ['terrain']" in message


# --- (5b) a REQUIRED deck field no slot covers is refused at construction ---- #
def test_a_required_deck_field_no_slot_covers_refuses_at_plan_construction():
    """The mirror of the unknown-member check, and the more expensive half.

    A Forcing with no ``carrier`` leaves ``carrier_discharge`` unfilled. Without
    this the plan builds, the geocode + flowline + discharge fetches all run, and
    only then does write_reach_deck die on a TypeError - minutes and three network
    round-trips after the declaration that was already wrong.
    """
    ops = _telemac()
    mesh = ops.build_mesh(Ref("reach"), MeshPolicy())
    with pytest.raises(PlanValidationError) as ei:
        ops.author(mesh=mesh, physics=Physics("tracer", substance="dye"),
                   forcing=Forcing())
    message = str(ei.value)
    assert "carrier_discharge" in message
    assert "requires" in message


def test_the_covered_declaration_still_authors():
    """The guard refuses a HOLE, not every plan: the cohort shape still passes."""
    ops = _telemac()
    mesh = ops.build_mesh(Ref("reach"), MeshPolicy())
    deck = ops.author(mesh=mesh, physics=Physics("tracer", substance="dye"),
                      forcing=Forcing(carrier=Ref("carrier_discharge")))
    assert deck.kwargs["carrier_discharge"] == Ref("carrier_discharge")


# --- (6) the EngineOps five are must-fill at REGISTRATION -------------------- #
def test_registration_refuses_a_facade_with_an_unrealized_operation():
    """The design doc promises the library refuses to register a template that
    leaves a must-fill slot empty. A hole that reaches run time surfaces as a bare
    NotImplementedError flattened into <ENGINE>_INTERNAL_ERROR - a declaration
    defect wearing a runtime failure's clothes."""
    from trid3nt_server.workflows.lib import FacadeIncompleteError, register_workflow

    class HalfEngine(Workflow):
        engine = "half"

        def acquire_domain(self, **slots):
            return ()

        def build_mesh(self, domain, policy, **slots):
            return None

        def author(self, *, mesh, physics, forcing):
            return Step(runner="pkg.mod.fn")

    with pytest.raises(FacadeIncompleteError) as ei:
        register_workflow(HalfEngine, _metadata("half_probe"), (),
                          lambda o: ())
    assert "HalfEngine" in str(ei.value)
    assert "solver_spec" in str(ei.value) and "read_results" in str(ei.value)


def test_registration_refuses_something_that_is_not_a_facade_at_all():
    from trid3nt_server.workflows.lib import FacadeIncompleteError, register_workflow

    with pytest.raises(FacadeIncompleteError):
        register_workflow(object, _metadata("not_a_facade"), (), lambda o: ())


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
    from trid3nt_server.workflows.lib import WireArgsError

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


# --- provenance rows are declared as (param, note_key), exactly -------------- #
@pytest.mark.parametrize("row", [("a", "b", "c"), ("a",), (1, 2)])
def test_a_malformed_provenance_row_refuses_at_declaration(row):
    with pytest.raises(PlanValidationError) as ei:
        _workflow(provenance=(row,))
    assert "provenance row" in str(ei.value)


def test_the_run_prefix_comes_from_the_step_the_facade_NAMES():
    """A literal "solve" would silently lose the prefix for a facade that named
    its solve step anything else - and the chart spec would persist nowhere."""
    from trid3nt_server.workflows.lib import RunResult

    class Renamed(Workflow):
        solve_step = "telemac_solve"

    run = RunResult(value=_Layer())
    run.results["telemac_solve"] = {"run_id": "RUN123"}
    assert _workflow(Renamed)._run_id(_Layer(), run) == "RUN123"
    # a workflow that declares no solve step simply has no prefix to find
    assert _workflow()._run_id(_Layer(), run) is None


# --- a context slot's declared SHAPE reaches the wire and the prose ---------- #
def test_a_context_slot_puts_its_declared_shape_on_the_wire_and_in_the_prose():
    """A slot names no source, so the SHAPE it accepts is the only thing the
    schema and the prose can say about it - and both have to say it, or a caller
    fills a polyline slot with a raster because nothing told them otherwise."""
    import typing

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.data import SuppliedGeometry

    fn = TOOL_REGISTRY["artemis_harbor_agitation"].fn
    annotation = inspect.signature(fn).parameters["structure"].annotation
    assert typing.get_args(annotation)[1:] == (SuppliedGeometry("polyline"),)
    # the metadata is documentation, not schema: the hint a declaration builder
    # resolves is the plain string type the argument really takes
    assert typing.get_type_hints(fn)["structure"] == (str | None)

    doc = fn.__doc__ or ""
    context = doc.split("Context layers:")[1].split("Run controls:")[0]
    assert "structure: a polyline layer you supply" in context
    # one argument, one description: the template's own prose joins the slot's
    # line rather than repeating it as a run control
    assert "    structure:" not in doc.split("Run controls:")[1]


def test_a_slot_that_declares_no_shape_still_reaches_the_wire_as_a_string():
    from trid3nt_server.workflows.lib import Data
    from trid3nt_server.workflows.lib.workflow import _wire_signature

    sig, annotations = _wire_signature((), (), (Data("clip_zone"),))
    assert annotations["clip_zone"] == (str | None)
    assert sig.parameters["clip_zone"].default is None


# --- a corridor policy is a binding block, so it is frozen DEEP -------------- #
def test_a_corridor_policy_is_frozen_all_the_way_down():
    """A template writes CORRIDOR at module level and every run reads that same
    object, so a mutable container inside one is a channel from one run to the
    next: a step that edits the declared list changes what the NEXT run declares."""
    from trid3nt_server.workflows.telemac.workflow import CorridorPolicy

    banks = ["nhd_area", "assumed_ribbon"]
    policy = CorridorPolicy(extent_km=0.5, width_m={"left": 30.0, "right": [1, 2]},
                            boundary_source=banks)

    assert policy.boundary_source == ("nhd_area", "assumed_ribbon")
    assert isinstance(policy.width_m, MappingProxyType)
    assert policy.width_m["right"] == (1, 2)     # the nested list became a tuple
    with pytest.raises(TypeError):
        policy.width_m["left"] = 99.0
    banks.append("invented_later")               # the caller's list is not the policy's
    assert policy.boundary_source == ("nhd_area", "assumed_ribbon")
