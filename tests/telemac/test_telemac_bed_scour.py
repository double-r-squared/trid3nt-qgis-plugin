"""The mobile-bed template as VALUES: the slots it stands on and the plan the
workflow builds from them.

Offline: nothing is fetched, meshed or solved. What is proved is the
DECLARATION - one domain row, one composed bed row, the runtime levers it no
longer restates, the keyword opinions it states as keywords, and the two stages
it still owns because open-channel hydraulics and a release point are measured
against the accepted mesh.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trid3nt_server.workflows.runtime import DataRef, Ref
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.templates.bed_scour import bed_scour

_MODULE = bed_scour
#: The registered tool, found by what it CARRIES rather than by its name, so the
#: question-class rename moves the module and this test moves with it.
_WORKFLOW = next(value.workflow for value in vars(_MODULE).values()
                 if getattr(value, "workflow", None) is not None)
_ANSWER = _MODULE.ANSWER
#: The package the corpus sits in, read off the module so a rename moves both.
_PACKAGE = Path(_MODULE.__file__).parent

#: What the domain wave took off every template: the keyword twins the module's
#: dictionary already describes, the domain twins the slots replaced, and the
#: levers the runtime declares once.
_DISSOLVED = {"location", "bbox", "river_geometry_uri", "reach_length_km",
              "friction_coefficient", "friction_law", "output_interval_min",
              "evaporation_mm_per_day", "rainfall_gridmet_window"}


def _rows() -> dict:
    return {row.name: row for row in _WORKFLOW.data}


def _recipe():
    from trid3nt_server.workflows.mesh.tool import recipe_from_plan_value

    return recipe_from_plan_value(_WORKFLOW.plan.steps[0].kwargs["mesh"])


def test_the_domain_is_one_row_the_reach_fetcher_produces():
    """The five chained rows are one slot with a producer: the release point is
    the seed, and the polygon comes back with its two end faces."""
    domain = _rows()["domain"]
    assert domain.role == "domain" and domain.geometry == "polygon"
    assert domain.producer.runner == "fetch_river_reach"
    assert set(domain.producer.kwargs) == {"seed_point", "distance_km"}
    assert [ref.path for ref in domain.producer.kwargs["seed_point"]] == [
        "release.lon", "release.lat"]


def test_the_bed_is_one_row_the_slot_composed_from_the_survey_and_the_terrain():
    """set_bed takes ONE source: the soundings are gridded by the derive the row
    names, and the slot lays that measurement OVER the terrain beside it."""
    rows = _rows()
    bed = rows["bed"]
    assert bed.role == "bed"
    assert bed.producer.runner == "derive_survey_surface"
    assert bed.producer.kwargs["points"] == DataRef("survey")
    assert bed.coercion["over"] == DataRef("terrain")
    assert rows["terrain"].producer.runner == "fetch_dem"
    assert [row.name for row in _WORKFLOW.data if row.role == "bed"] == ["bed"]


def test_an_unsurveyed_domain_still_runs_and_the_sheet_says_which_bed_it_got():
    """A body of water with no federal navigation project has no published
    survey. The survey row is CONTEXT, the grid of nothing is nothing, and the
    terrain the slot composes over is the whole bed."""
    rows = _rows()
    assert rows["survey"].is_context
    assert "terrain surface stands" in rows["survey"].context_sentence


def test_the_carrier_discharge_is_an_observation_row_not_a_step():
    """The flow that moves the bed is ONE measured value the run opens on, not a
    layer: an OBSERVATION row ranked against the domain, absent-is-context, and
    the user's own number wins over any record."""
    carrier = _rows()["carrier"]
    assert carrier.producer.runner == "fetch_noaa_nwm_streamflow"
    assert carrier.is_context and carrier.role == "discharge"
    assert carrier.coercion["field"] == "streamflow_cms"
    assert carrier.coercion["near"] == Ref("domain.centroid")
    assert carrier.producer.kwargs["valid_time"].name == "event_time"


def test_the_workflow_owns_every_stage_but_the_two_measured_on_the_mesh():
    """No domain steps, no mesh recipe, no settle, no restated file names. The
    open-channel hydraulics and the release point are measured against the
    ACCEPTED mesh, so those two stay the template's."""
    assert _WORKFLOW.plan_decl.owns_stages
    assert [step.label for step in _WORKFLOW.plan.steps] == [
        "mesh", "channel", "source", "settled", "sheet", "solve", "outputs"]
    settle = next(step for step in _WORKFLOW.plan.steps if step.label == "settled")
    assert settle.runner.endswith("assembler.open_water")
    assert (settle.kwargs["geometry"], settle.kwargs["boundary"],
            settle.kwargs["result"]) == ("domain.slf", "domain.cli",
                                         "r2d_domain.slf")


def test_the_deck_names_its_own_files_and_the_door_does_not_restate_them():
    """The run directory's names ARE the deck's own statements, so the settle
    step above reads them off the body rather than being told them twice."""
    asserted = _MODULE.STEERING.ASSERTED
    assert asserted["GEOMETRY_FILE"] == "domain.slf"
    assert asserted["BOUNDARY_CONDITIONS_FILE"] == "domain.cli"
    assert asserted["RESULTS_FILE"] == "r2d_domain.slf"


def test_the_release_step_is_handed_the_domain_the_centerline_rides_on():
    """An unplaced marker sits at spill_fraction along the domain's centerline
    companion, and a placed one is held on that same line - both of which need
    the domain artifact, not just the mesh cut from it."""
    source = next(step for step in _WORKFLOW.plan.steps if step.label == "source")
    assert source.kwargs["domain"] == Ref("domain")
    assert source.kwargs["fraction"].name == "spill_fraction"


def test_no_step_of_this_plan_calls_a_template_by_module_path():
    """Every runner is a framework home; nothing is reached at
    ``templates.<name>``."""
    assert not [step.runner for step in _WORKFLOW.plan.steps
                if ".templates." in step.runner]


def test_the_mesh_is_built_over_the_domain_slot_at_the_runtime_lever():
    recipe = _recipe()
    assert recipe.extent == DataRef("domain")
    assert recipe.resolution_m.name == "mesh_resolution_m"
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}


def test_the_boundary_roles_come_from_the_runs_slot():
    """The reach fetcher returns the section and the two faces it was cut
    between, and they ride on the domain into the runs slot; a domain that
    carries none is asked for them, and an edge that names none is closed."""
    runs = next(op for op in _recipe().ops if op.fn == "set_boundary_roles")
    assert runs.kwargs == {"runs": DataRef("runs")}
    row = next(row for row in _WORKFLOW.data if row.role == "runs")
    assert (row.name, row.producer, row.is_optional) == ("runs", None, True)


def test_the_runtime_levers_are_seated_and_no_baseline_param_is_restated():
    declared = [prm.name for prm in _WORKFLOW.params]
    # Every lever is on the sheet; the ones this question does not state for
    # itself are seated at the end, in the order the runtime declares them, and
    # one it differs on keeps its own place and its own opinion of the value.
    from trid3nt_server.workflows.runtime import param_rows

    own = [prm.name for prm in param_rows(_MODULE.PARAMS)]
    assert set(LEVER_NAMES) <= set(declared)
    seated = [name for name in LEVER_NAMES if name not in own]
    assert declared[-len(seated):] == seated
    assert _DISSOLVED.isdisjoint(declared)


def test_the_friction_this_deck_is_solved_at_is_a_keyword_not_a_param():
    """The dictionary already describes the law and its coefficient. The
    template states its OPINION of the value, and the stage the run opens at is
    derived at that same number."""
    asserted = _MODULE.STEERING.ASSERTED
    assert asserted["LAW_OF_BOTTOM_FRICTION"] == 3
    assert asserted["FRICTION_COEFFICIENT"] == 33.0
    channel = next(step for step in _WORKFLOW.plan.steps if step.label == "channel")
    assert channel.kwargs["friction_law"] == asserted["LAW_OF_BOTTOM_FRICTION"]
    assert channel.kwargs["friction_coefficient"] == asserted["FRICTION_COEFFICIENT"]


def test_the_boundary_values_read_the_measured_walk_and_the_open_channel_step():
    """WHICH list carries a value is the mesh's own walk; what the two liquid
    faces carry is the flow and the level the open-channel step measured."""
    measured = _MODULE.STEERING.ASSERTED["boundaries"]["measured"]
    assert measured["liquid_boundary_order"].path == "settled.liquid_boundary_order"
    assert measured["inflow_q_m3s"].path == "settled.inflow_q_m3s"
    assert measured["outflow_stage_m"].path == "settled.outflow_stage_m"
    assert _MODULE.STEERING.ASSERTED["INITIAL_DEPTH"].path == "settled.depth_m"
    assert _MODULE.STEERING.ASSERTED["INITIAL_ELEVATION"].path == "settled.level_m"


def test_every_slot_a_user_can_fill_reaches_the_wire():
    """A drawn estuary supersedes the fetched reach and a surveyed raster
    supersedes the merge, so both slots are arguments though both name a
    source."""
    assert {row.name for row in _WORKFLOW.data if row.fills_from_user} == {
        "domain", "runs", "bed", "carrier", "stage"}


def test_the_only_published_read_is_the_placed_one_and_it_has_its_caption():
    """What the run WRITES is the module's own table. This template publishes
    the marker's history and captions that, and nothing else."""
    assert [primitive.variable for primitive in _MODULE.OUTPUTS] == ["T1"]
    assert set(_MODULE.CAPTIONS) == {"T1"}


def test_the_answer_is_the_bed_change_the_balance_and_the_sorting_signature():
    assert set(_WORKFLOW.answer_fields) == set(_ANSWER)
    assert _ANSWER["bed_evolution_max_m"].primitive.module == "gaia"
    assert _ANSWER["surface_d50_spread_m"].primitive.variable == "D50"


def test_the_corpus_key_is_the_registered_name():
    corpus = yaml.safe_load((_PACKAGE / "corpus.yaml").read_text(encoding="utf-8"))
    assert list(corpus) == [_WORKFLOW.name]
    assert all(phrase == phrase.strip() and phrase.isascii()
               for phrase in corpus[_WORKFLOW.name])


def test_the_routing_text_names_the_question_class_not_one_body_of_water():
    """A bed scours under an estuary as readily as under a river, so the first
    sentence of the routing text says the question, never the body."""
    routing = _MODULE.DOC["routing"]
    assert "reach" not in routing.split(".")[0]
    assert "supply `domain`" in routing
