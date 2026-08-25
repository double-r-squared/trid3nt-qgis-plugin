"""The workflow SKELETON itself: the template method, the facade, the factory.

Offline. Nothing here solves; these pin the contract in
``docs/design/declarative-workflows.md`` "The Workflow Skeleton" (ADR 0312):

  1. the five EngineOps are ABSTRACT - an unrealized one refuses by name;
  2. hooks have SILENT defaults, and a FILLED hook reaches the result;
  3. the registration factory synthesizes the wire signature from the declared
     params (``wire=False`` stays off it, aliases and controls join it);
  4. a chart builder is the FUNCTION - a dotted string is refused, no fallback;
  5. a slot member the engine's deck writer does not accept is refused while the
     plan value is being BUILT, never silently dropped.
"""

from __future__ import annotations

import inspect
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
    return cls(metadata=_metadata("skeleton_probe"), params=(), plan=lambda p, d, o: (),
               answer=("depth_max_m",), **kw)


def test_the_hooks_are_silent_by_default():
    wf = _workflow()
    assert wf.checks(_Layer(), None) == ()
    assert wf.context_layers(_Layer(), None) == ()


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
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_river_dye"].fn).parameters)
    assert "reach_seed_coords" in declared and "reach_seed_coords" not in wire
    assert declared - {"reach_seed_coords"} <= wire


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
                           plan=lambda p, d, o: ())


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
    ops = _telemac()
    mesh = ops.build_mesh(Ref("reach"), MeshPolicy(
        resolution="coarse", target_edge_m=100.0, extent_km=0.5, width_m=60.0,
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


def test_the_plan_reads_a_data_name_the_workflow_declares_and_refuses_one_it_does_not():
    from trid3nt_server.workflows.lib import Data, Fetch, WireArgsError
    from trid3nt_server.workflows.lib.workflow import DataRefs

    d = DataRefs((Data("rivers", Fetch.tool("pkg.mod.fetch")),))
    assert d.rivers == Ref("rivers")
    with pytest.raises(WireArgsError) as ei:
        _ = d.terrain
    assert "terrain" in str(ei.value)


def test_the_skeleton_names_and_engines_the_plan_the_template_does_not():
    ops = _telemac()
    ops.plan_decl = lambda p, d, o: (Step(runner="pkg.mod.fn"),)
    plan = ops.build_plan(None)
    assert plan.name == "telemac_probe"      # from the metadata
    assert plan.engine == "telemac2d"        # from the facade
