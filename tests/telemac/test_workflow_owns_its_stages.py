"""The stages the WORKFLOW builds from a template module's declared slots.

Offline: nothing is built and nothing is solved. What is proved is the PLAN a
template gets when it states only what differs from the base.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.workflows.runtime import (
    Continued,
    Data,
    DataRef,
    ParamRef,
    PlanValidationError,
    Ref,
    tool,
)
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES, lever
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

_METADATA = AtomicToolMetadata(
    name="telemac_water_temperature", ttl_class="live-no-cache",
    source_class="workflow_dispatch", cacheable=False, engine="telemac",
    tier="template")


class STEERING(T2D):
    """The deck: a body of water under a week of weather."""

    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
    RESULTS_FILE = "r2d_domain.slf"
    TITLE = Ref("settled.title")
    # The clock is a KEYWORD the module carries, so the deck states it and the
    # settle reads it off the deck rather than off a param beside it.
    DURATION = 604800.0


class PARAMS:
    mesh_resolution_m = lever("mesh_resolution_m", default=25.0)


class DATA:
    domain = Data.need("hydrography", at=Ref("seed"))
    bed = Data.need("bathymetry")


def _template(data: type = DATA, params: type = PARAMS) -> SimpleNamespace:
    """A template module, as the workflow reads one: its own names and nothing
    around them."""
    return SimpleNamespace(STEERING=STEERING, PARAMS=params, DATA=data)


def _workflow(data: type = DATA, params: type = PARAMS) -> TelemacWorkflow:
    return TelemacWorkflow(metadata=_METADATA, params=params,
                           template=_template(data, params), data=data,
                           levers=TelemacWorkflow.levers())


def _steps(workflow: TelemacWorkflow) -> list[str]:
    return [step.label for step in workflow.plan.steps]


def _step(workflow: TelemacWorkflow, name: str):
    return next(s for s in workflow.plan.steps if s.name == name)


def _mesh(workflow: TelemacWorkflow):
    return _step(workflow, "mesh")


def test_a_template_that_states_only_what_differs_gets_the_whole_plan():
    """No domain steps, no mesh recipe, no settle, no file names: the workflow
    builds its stages from the slots and the deck's own statements."""
    workflow = _workflow()
    assert _steps(workflow) == ["stated", "mesh", "mesh_files", "settled",
                                "sheet", "solve", "outputs"]
    assert [step.stage for step in workflow.plan.steps] == [
        "prep", "mesh", "author", "author", "author", "solve", "publish"]


def test_the_mesh_is_built_over_the_domain_slot_at_the_runtimes_own_lever():
    from trid3nt_server.mesh.tool import recipe_from_plan_value

    workflow = _workflow()
    recipe = recipe_from_plan_value(_mesh(workflow).kwargs["mesh"])
    assert recipe.mesher == "om2d" and recipe.kind == "unstructured_tri"
    assert recipe.extent == DataRef("domain")
    assert recipe.resolution_m.name == "mesh_resolution_m"
    assert [op.fn for op in recipe.ops][-2:] == ["set_bed", "set_boundary_roles"]


def test_the_rim_is_sized_because_nothing_else_in_the_library_sizes_it():
    """Every domain is cut from a shoreline now, and no sizing function measures
    the domain's own outline: an undeclared rim meshes an order of magnitude past
    the size word, and the granularity lever is the user's."""
    from trid3nt_server.mesh.tool import recipe_from_plan_value

    workflow = _workflow()
    recipe = recipe_from_plan_value(_mesh(workflow).kwargs["mesh"])
    rim = recipe.ops[0]
    assert (rim.fn, dict(rim.kwargs)) == ("set_rim_size", {})


def test_the_bed_op_takes_the_one_row_the_merge_derive_produced():
    """A survey where it has data and the surface elsewhere is ONE bed, composed
    in the DATA body; the op takes that row and nothing beside it."""
    from trid3nt_server.mesh.tool import recipe_from_plan_value

    workflow = _workflow()
    recipe = recipe_from_plan_value(_mesh(workflow).kwargs["mesh"])
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}


def test_the_boundary_runs_ride_on_the_polygon_the_domain_arrived_as():
    """The named stretches of the edge are NO ROW: a reach fetcher returns the
    section and the two faces it was cut between, and a drawn outline carries
    whatever it was drawn with, so the op reads the domain itself."""
    from trid3nt_server.mesh.tool import recipe_from_plan_value

    recipe = recipe_from_plan_value(_mesh(_workflow()).kwargs["mesh"])
    runs = next(op for op in recipe.ops if op.fn == "set_boundary_roles")
    assert runs.kwargs == {"runs": DataRef("domain")}


def test_the_settle_step_reads_the_files_the_deck_itself_names():
    workflow = _workflow()
    settle = _step(workflow, "settled")
    assert settle.runner.endswith("opening.open_water")
    assert settle.kwargs["geometry"] == "domain.slf"
    assert settle.kwargs["boundary"] == "domain.cli"
    assert settle.kwargs["result"] == "r2d_domain.slf"
    # THE CLOCK IS THE RESOLVED FLOOR'S, not the class attribute's: the settle
    # runs before the sheet exists, so it reads what the deck will write.
    assert settle.kwargs["duration_s"] == Ref("stated.DURATION")
    # WHICH RUN THIS ONE CONTINUES is the run's own continue_from, so every settle reads
    # it and no template declares a param that twins it.
    assert settle.kwargs["continue_from"] is Continued


def test_the_floor_is_resolved_once_before_any_stage_runs():
    """The first step of every plan resolves the deck's assertions under the
    run's own keywords, so a stated DURATION is the one the settle reads."""
    from trid3nt_server.workflows.telemac.workflow import stated

    workflow = _workflow()
    first = workflow.plan.steps[0]
    assert first.name == "stated" and first.stage == "prep"
    assert stated(steering=STEERING, keywords={})["DURATION"] == 604800.0
    assert stated(steering=STEERING,
                  keywords={"DURATION": 3600.0})["DURATION"] == 3600.0


def test_the_runtime_levers_are_seated_so_the_template_states_none_of_them():
    workflow = _workflow()
    # The template's own row for a lever keeps its place; every lever it does
    # not state is seated after it, in the order the runtime declares them.
    assert [prm.name for prm in workflow.params] == [
        "mesh_resolution_m", *(n for n in LEVER_NAMES if n != "mesh_resolution_m")]


def test_the_domain_and_the_bed_reach_the_wire_as_the_slots_they_are():
    """What the user hands in supersedes the producer the template preferred, so
    both slots are arguments even though both name a source."""
    supplied = workflow_wire(_workflow())
    assert {"domain", "bed"} <= supplied


def test_a_row_the_deck_never_reads_is_demand_pulled_not_fetched():
    """EVERY FETCHED ROW HAS A SUPPLIED TWIN, which only holds if supplying the
    twin stops the fetch. The fill is handed the rows the deck NAMES; a row that
    exists only to feed another row's producer is produced when that producer
    runs, and never when the row it feeds was handed in."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    rows = {row.name for row in workflow.data}
    assert {"bed", "discharge", "domain"} <= rows
    sheet = next(step for step in workflow.plan.declared()
                 if step.label == "sheet")
    named = set(sheet.kwargs["produced"])
    # the bed is read by the MESH's own set_bed op, never by the deck
    assert not named & {"bed", "discharge", "domain"}
    assert "settled" in named


def workflow_wire(workflow: TelemacWorkflow) -> set[str]:
    return {decl.name for decl in workflow.data if decl.fills_from_user}


def test_a_template_with_no_domain_refuses_at_import():
    class NO_DOMAIN:
        bed = Data.need("bathymetry")

    with pytest.raises(PlanValidationError, match="declares no domain"):
        _workflow(data=NO_DOMAIN)


def test_a_template_with_no_bed_refuses_at_import():
    class NO_BED:
        domain = Data.need("hydrography")

    with pytest.raises(PlanValidationError, match="declares no bed"):
        _workflow(data=NO_BED)
