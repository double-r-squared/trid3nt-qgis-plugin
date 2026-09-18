"""The vertical-structure template as VALUES over the engine-neutral slots.

Offline. The question is what a column does over the depth a 2D model averages
away, and after the domain wave it asks that of ANY body of water: the domain is
a polygon somebody drew or holds, the bed is one source the caller can supersede,
and nothing in the deck names a reach, a place or a bounding box.
"""

from __future__ import annotations

import inspect

import pytest


def _module():
    from trid3nt_server.workflows.telemac.templates.stratified_flow import (
        stratified_flow,
    )

    return stratified_flow


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac3d_stratified_flow"].fn.workflow


# -- what the run stands on --------------------------------------------------- #

def test_the_run_stands_on_the_domain_slot_the_caller_fills():
    """No fetcher maps every pond, so the polygon is a SLOT: drawn on the canvas,
    picked, or a layer the caller already holds. The producer is the preference
    this question states, and anything supplied supersedes it."""
    from trid3nt_server.workflows.runtime.data import DOMAIN

    row = next(d for d in _workflow().data if d.role == DOMAIN)
    assert row.name == "domain"
    assert row.geometry == "polygon"
    assert row.fills_from_user
    assert row.producer.runner == "fetch_nhd_waterbody_at_point"


def test_the_seed_names_which_body_of_water_and_is_a_point_never_a_place():
    """A place name is geocoded to a point before it reaches a fetcher, so what the
    producer reads is a lon/lat off the Point the seed was ingested into."""
    from trid3nt_server.inputs import Point
    from trid3nt_server.workflows.runtime import Ref
    from trid3nt_server.workflows.runtime.data import DOMAIN

    row = next(d for d in _workflow().data if d.role == DOMAIN)
    assert row.producer.kwargs["seed_point"] == [Ref("seed.lon"), Ref("seed.lat")]
    seed = next(p for p in _workflow().params if p.name == "seed")
    assert seed.type is Point and seed.optional


def test_the_bed_is_one_source_the_caller_supersedes():
    """A charted lake floor is what this question is usually asked over, and it is
    a PREFERENCE: the slot takes a survey raster, soundings or a stated depth from
    whoever has one, and the row's producer only answers when nobody did."""
    from trid3nt_server.workflows.runtime.data import BED

    beds = [d for d in _workflow().data if d.role == BED]
    assert [d.name for d in beds] == ["bed"]
    assert beds[0].producer.runner == "fetch_greatlakes_bathymetry"
    assert beds[0].fills_from_user
    # A pond stated as a depth in metres reaches the same slot as a raster does.
    assert beds[0].wire_annotation == (str | float | None)


def test_a_closed_body_declares_no_boundary_runs():
    """A lake IS a closed body: no stretch of its edge prescribes anything, and the
    mesh takes its roles from the domain, which states none."""
    from trid3nt_server.workflows.runtime.data import RUNS

    assert [d.name for d in _workflow().data if d.role == RUNS] == []


def test_the_level_the_free_surface_opens_at_is_one_measured_value():
    """A body of water opens at a LEVEL somebody measured, the way a river opens at
    a carrier discharge: one reading off the nearest gauge that watched this water,
    ranked against a point inside the polygon rather than the mean of its vertices."""
    from trid3nt_server.workflows.runtime import Ref
    from trid3nt_server.workflows.runtime.data import LEVEL

    row = next(d for d in _workflow().data if d.name == "level")
    assert row.role == LEVEL
    assert row.producer.runner == "fetch_greatlakes_water_level"
    assert row.coercion["near"] == Ref("domain.centroid")
    assert row.coercion["opens"]
    # A number stated on the call stands over any record, so the slot takes both.
    assert row.wire_annotation == (str | float | None)


def test_an_ungauged_body_of_water_continues_the_run():
    """Water with no gauge on it is not a refusal: the sheet says the gauge held
    nothing and the column opens at the zero the bed is counted from."""
    row = next(d for d in _workflow().data if d.name == "level")
    assert row.is_context
    assert "opens at the zero" in row.context_sentence


def test_the_gauge_window_is_the_day_the_scenario_is_read_at():
    """A daily gauge record is asked over a calendar day, and the moment the run is
    about is the runtime's own lever - not a second date the template declares."""
    from trid3nt_server.workflows.runtime import ParamRef, Ref

    module = _module()
    workflow = _workflow()
    day = next(step for step in workflow.plan_decl.produce
               if step.name == "reading_day")
    assert day.runner == "trid3nt_server.inputs.instant.day"
    assert isinstance(day.kwargs["value"], ParamRef)
    assert day.kwargs["value"].name == "event_time"
    level = next(d for d in module.DATA.__dict__.values()
                 if getattr(d, "name", "") == "level")
    assert level.producer.kwargs["start_date"] == Ref("reading_day")
    assert level.producer.kwargs["end_date"] == Ref("reading_day")


# -- the plan the workflow owns ------------------------------------------------ #

def test_the_workflow_owns_the_stages_and_the_template_states_no_recipe():
    """The mesh recipe, the file names and the settle step are the runtime's now;
    what the template states is what DIFFERS from every other domain."""
    module = _module()
    door = _workflow().plan_decl

    assert door.settle is None and door.owns_stages
    assert door.mesh is None and door.domain == ()
    assert not hasattr(module, "MESH")
    assert door.levers == ("mesh_resolution_m", "event_time", "compute_class",
                           "vertical_frame")


def test_the_owned_mesh_paints_its_bed_and_takes_its_roles_from_the_domain():
    """One bed row painted at the nodes, and the runs from wherever they were
    stated - here the domain, which measured none."""
    from trid3nt_server.workflows.mesh.tool import recipe_plan_value

    workflow = _workflow()
    owned = workflow.plan_decl._from_slots(workflow)
    ask = recipe_plan_value(owned.mesh)
    assert repr(ask["extent"]) == "DataRef('domain')"
    assert repr(ask["resolution_m"]) == "ParamRef('mesh_resolution_m')"
    ops = {op["op"]: op["kwargs"] for op in ask["ops"]}
    assert repr(ops["set_bed"]["source"]) == "DataRef('bed')"
    assert repr(ops["set_boundary_roles"]["runs"]) == "DataRef('domain')"


def test_the_column_and_the_free_surface_come_from_one_measurement():
    """The vertical grid and the initial column are planned over the SAME deepest
    column under the SAME free surface - the BASE settle's, which measures the
    water any body of water holds, so a grid that holds the thermocline and a
    hook that places it cannot disagree."""
    from trid3nt_server.workflows.runtime import Ref

    asserted = _module().STEERING.ASSERTED
    settled = next(step for step in _workflow().plan.steps
                   if step.name == "settled")
    assert settled.runner.endswith("assembler.open_water")
    assert settled.kwargs["level"].path == "level"
    assert asserted["INITIAL_ELEVATION"] == Ref("settled.level_m")
    for slot in ("vertical_grid", "column"):
        assert asserted[slot]["max_depth_m"] == Ref("settled.max_depth_m")
    assert asserted["column"]["surface_m"] == Ref("settled.level_m")


def test_the_clock_is_the_settled_domains_and_the_duration_the_decks():
    """The step follows the edge the accepted mesh was BUILT at; the window is the
    deck's own DURATION in seconds, so the engine counts the steps and the settle
    reads the same number the deck was written for."""
    from trid3nt_server.workflows.runtime import Ref

    module = _module()
    asserted = module.STEERING.ASSERTED
    assert asserted["TIME_STEP"] == Ref("settled.time_step_s")
    # The CADENCE is the template's own opinion of the module's own keyword: in
    # steps, stated, because the dictionary's default writes every step.
    assert asserted["GRAPHIC_PRINTOUT_PERIOD"] == 360
    assert asserted["DURATION"] == 18000.0
    assert "NUMBER_OF_TIME_STEPS" not in asserted
    settled = next(step for step in _workflow().plan.steps
                   if step.name == "settled")
    assert settled.kwargs["duration_s"] == 18000.0


def test_the_plane_count_is_the_modules_keyword_and_the_planner_reads_it():
    """The plane count is NUMBER OF HORIZONTAL LEVELS, and the grid plan and the
    initial column READ that keyword rather than a second number beside it - so a
    user who states another count is planned on the column they are solved over."""
    from trid3nt_server.workflows.runtime import Ref

    asserted = _module().STEERING.ASSERTED
    assert asserted["NUMBER_OF_HORIZONTAL_LEVELS"] == 13
    for slot in ("vertical_grid", "column"):
        assert asserted[slot]["levels"] == Ref("NUMBER_OF_HORIZONTAL_LEVELS")


def test_a_stated_plane_count_replans_the_grid_and_the_initial_column():
    """The fill binds the composites' read to whatever the sheet holds, so the
    stretch the grid achieves follows the keyword whether the deck stated the
    count or the user did."""
    from trid3nt_server.workflows.telemac.modules.sheet import fill

    produced = {"settled": {"title": "basin", "time_step_s": 1.0,
                            "level_m": 100.0, "max_depth_m": 120.0}}
    params = {"thermocline_depth_m": 8.0, "warm_temp_c": 25.0,
              "cold_temp_c": 15.0}
    body = _module().STEERING
    stated = fill(body, produced=produced, params=params)
    finer = fill(body, produced=produced, params=params,
                 NUMBER_OF_HORIZONTAL_LEVELS=25)
    assert dict(stated.resolved())["NUMBER OF HORIZONTAL LEVELS"] == 13
    assert dict(finer.resolved())["NUMBER OF HORIZONTAL LEVELS"] == 25
    assert (dict(stated.resolved())["MESH STRETCHING COEFFICIENTS"]
            != dict(finer.resolved())["MESH STRETCHING COEFFICIENTS"])


def test_the_deck_is_calm_and_states_no_wind_keyword_at_all():
    """CALM is the half of the pair the thermocline survives in, and a zero speed
    writes nothing: TELEMAC-3D has no combined speed-and-direction keyword, so the
    wind a user asks for is the engine's own components, set by their names."""
    from trid3nt_server.workflows.telemac.modules.sheet import fill

    asserted = _module().STEERING.ASSERTED
    assert asserted["wind"]["speed_mps"] == 0.0
    produced = {"settled": {"title": "basin", "time_step_s": 1.0,
                            "level_m": 100.0, "max_depth_m": 40.0}}
    sheet = fill(_module().STEERING, produced=produced,
                 params={"thermocline_depth_m": 8.0, "warm_temp_c": 25.0,
                         "cold_temp_c": 15.0})
    assert [name for name in dict(sheet.resolved()) if "WIND" in name] == []


# -- what the template no longer declares -------------------------------------- #

#: What this question asks that nothing else answers for it. Everything else is
#: the dictionary's keyword or the runtime's lever.
_OWN_PARAMS = {"seed", "warm_temp_c", "cold_temp_c", "thermocline_depth_m",
               "mesh_resolution_m"}


def test_the_template_declares_only_its_own_question_and_one_redefaulted_lever():
    """A keyword twin, a domain twin and a lever are each declared once elsewhere.
    The one lever restated here is restated for its DEFAULT alone."""
    from trid3nt_server.workflows.runtime.levers import LEVER_NAMES

    workflow = _workflow()
    declared = {p.name for p in workflow.params}
    assert declared == _OWN_PARAMS | set(LEVER_NAMES)
    rows = {p.name: p for p in workflow.params}
    # 14 m over a lake is a mesh this question has no use for: the template's own
    # row wins and keeps the lever's name.
    assert rows["mesh_resolution_m"].default == 120.0


@pytest.mark.parametrize("gone", ["location", "bbox", "mesh_min_edge_m",
                                  "time_step_s", "output_interval_min",
                                  "sim_duration_hours", "sim_duration_s",
                                  "levels", "wind_speed_mps",
                                  "wind_direction_deg",
                                  "tracer_advection_scheme",
                                  "max_advection_iterations"])
def test_the_dissolved_params_are_gone(gone):
    """Each is a keyword the dictionary describes or a slot the runtime declares."""
    assert gone not in {p.name for p in _workflow().params}


def test_the_model_surface_takes_a_body_of_water_and_never_a_place():
    """Nothing on the wire names a place or a box: the domain IS the argument, and
    a place name is geocoded to a shape before it gets here."""
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["telemac3d_stratified_flow"].fn
    args = set(inspect.signature(fn).parameters)
    assert {"domain", "bed", "level", "seed"} <= args
    assert not args & {"location", "bbox"}


def test_the_routing_text_is_about_a_body_of_water():
    """The question class is the vertical structure of WATER; naming a reach or a
    lake alone is what the domain wave took out."""
    from trid3nt_server.workflows.telemac.templates.stratified_flow.declarations import (
        DOC,
    )

    routing = DOC["routing"]
    assert "reach" not in routing.lower()
    assert "`domain`" in routing and "`bed`" in routing and "`seed`" in routing
    for word in ("lake", "reservoir", "pond"):
        assert word in routing.lower()


def test_the_package_holds_the_template_its_declarations_and_its_corpus():
    """A template package carries no module of its own: what was a clip and a gauge
    reader are a slot's ingestion and a fetcher row now."""
    from pathlib import Path

    package = Path(_module().__file__).parent
    assert sorted(p.name for p in package.iterdir()
                  if p.is_file() and not p.name.startswith(".")) == [
        "__init__.py", "corpus.yaml", "declarations.py", "stratified_flow.py"]
