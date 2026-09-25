"""WAQTEL micropol: the rows the process appends, and the deck sized to them.

Offline. What is proved here is the tracer bookkeeping the whole deck hangs off -
five rows, in the engine's order, with the declared one adopted rather than
doubled - the keywords the coupled body carries by their own names, the refusal
that keeps the second sorption site out of a five-row process, and the three
engine-neutral slots the converted template now stands on."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.workflows.runtime import DataRef, Ref
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, fill, waqtel
from trid3nt_server.workflows.telemac.modules.module import SlotRefused

#: The process, its rows and its body, read off the wrapper that carries them.
PROCESS = 7
ROWS = waqtel._APPENDED[PROCESS]
micropollutant = WAQTEL.micropollutant

#: The names the engine's own nametrac adds for process 7, in its order.
_ENGINE_ORDER = ["SUSPENDED LOAD", "BED SEDIMENTS", "MICRO POLLUTANT",
                 "ABS. SUSP. LOAD.", "ABSORB. BED SED."]

#: The example's own three MICROPOL statements, which is what a body carries.
_EXAMPLE = {"SEDIMENT_SETTLING_VELOCITY": 4.0e-7,
            "COEFFICIENT_OF_DISTRIBUTION": 1.0,
            "EXPONENTIAL_DESINTEGRATION_CONSTANT": 0.0}


def test_the_rows_are_the_engine_s_own_names_in_the_order_it_appends_them():
    """The units are the ones MICROPOL's own source terms are written in: the
    suspended sediment is the concentration the distribution coefficient's m3/kg
    is read against, and what settled onto the bed is per square metre."""
    assert [row.name for row in ROWS] == _ENGINE_ORDER
    assert [row.unit for row in ROWS] == ["kg/m3", "kg/m2", "mg/L", "mg/L",
                                          "g/m2"]


def test_every_row_draws_under_a_style_of_its_own_floored_at_nothing():
    assert len({row.style["ramp"] for row in ROWS}) == len(ROWS)
    for row in ROWS:
        assert row.style["kind"] == "mesh" and row.style["floor"] == 0
        assert row.varies


def test_the_body_carries_the_keywords_it_was_given_and_no_others():
    assert dict(micropollutant(**_EXAMPLE)["slots"]) == _EXAMPLE
    assert dict(micropollutant()["slots"]) == {}


def test_a_keyword_waqtel_does_not_spell_refuses_by_name():
    """At the BODY, which is import time for a template that states it: a typo
    never reaches a fill, and the refusal names the keyword it nearly was."""
    with pytest.raises(SlotRefused, match="SEDIMENT_SETTLING_VELOCITY"):
        micropollutant(SEDIMENT_SETTLING_VELCOITY=4.0e-7)


def test_the_second_sorption_site_refuses_because_it_moves_the_rows():
    with pytest.raises(SlotRefused, match="seven tracers"):
        micropollutant(KINETIC_EXCHANGE_MODEL=2,
                       COEFFICIENT_OF_DISTRIBUTION_2=2.0)


def test_the_body_names_the_process_and_the_carrier_states_it():
    sheet = fill(T2D, coupling=[micropollutant(**_EXAMPLE)])
    (body,) = sheet.coupled
    assert body["module"] == "waqtel" and body["process"] == PROCESS
    assert dict(sheet.resolved())["WATER QUALITY PROCESS"] == PROCESS
    assert dict(sheet.resolved())["COUPLING WITH"] == "WAQTEL"


def test_the_steering_file_carries_the_keywords_and_nothing_else():
    sheet = fill(T2D, coupling=[micropollutant(**_EXAMPLE)])
    (body,) = sheet.coupled
    assert dict(fill(WAQTEL, **dict(body["slots"])).resolved()) == {
        "SEDIMENT SETTLING VELOCITY": 4.0e-7,
        "COEFFICIENT OF DISTRIBUTION": 1.0,
        "EXPONENTIAL DESINTEGRATION CONSTANT": 0.0}


def test_a_constant_nobody_states_is_left_to_the_engine():
    sheet = fill(T2D, coupling=[micropollutant(
        SEDIMENT_SETTLING_VELOCITY=None, COEFFICIENT_OF_DISTRIBUTION=1.0)])
    (body,) = sheet.coupled
    assert dict(fill(WAQTEL, **dict(body["slots"])).resolved()) == {
        "COEFFICIENT OF DISTRIBUTION": 1.0}


def test_the_process_appends_its_five_to_a_carrier_that_declares_none():
    sheet = fill(T2D, coupling=[micropollutant()])
    assert [row.name for row in sheet.tracers] == _ENGINE_ORDER
    assert sheet.printouts()["VARIABLES FOR GRAPHIC PRINTOUTS"].endswith(
        ",T1,T2,T3,T4,T5")


def test_the_dissolved_row_a_carrier_declares_is_ADOPTED_never_appended():
    # ADDTRACER matches on the first sixteen characters, so a carrier that
    # spells the process's own name keeps its row and the count stays five.
    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["MICRO POLLUTANT MG/L"],
                 coupling=[micropollutant()])
    rows = list(sheet.tracers)
    assert len(rows) == 5
    assert (rows[0].name, rows[0].unit) == ("MICRO POLLUTANT", "MG/L")
    assert [row.name for row in rows[1:]] == [
        name for name in _ENGINE_ORDER if name != "MICRO POLLUTANT"]


def test_the_rows_are_declared_data_on_the_wrapper():
    rows = dict(WAQTEL.APPENDABLE)[f"process {PROCESS}"]
    assert [row.name for row in rows] == _ENGINE_ORDER


def _template():
    from trid3nt_server.workflows.telemac.templates.micropollutant_release import (
        micropollutant_release as template,
    )

    return template


def test_every_tracer_sized_array_on_the_deck_carries_one_value_per_row():
    steering = _template().STEERING
    assert steering.NUMBER_OF_TRACERS == 1
    assert len(steering.NAMES_OF_TRACERS) == 1
    assert len(steering.INITIAL_VALUES_OF_TRACERS) == len(ROWS)
    assert len(steering.boundaries["tracers"]) == len(ROWS)
    assert len(steering.VALUES_OF_THE_TRACERS_AT_THE_SOURCES) == len(ROWS)


def test_the_marker_is_stated_as_the_four_source_keywords_by_name():
    """No ``releases`` composite: the deck states WHERE, HOW MUCH and AT WHAT
    CONCENTRATION as the engine's own keywords, one element per source."""
    asserted = _template().STEERING.ASSERTED
    assert asserted["ABSCISSAE_OF_SOURCES"] == [Ref("source.at.0")]
    assert asserted["ORDINATES_OF_SOURCES"] == [Ref("source.at.1")]
    assert asserted["WATER_DISCHARGE_OF_SOURCES"] == [1.0]
    assert asserted["VALUES_OF_THE_TRACERS_AT_THE_SOURCES"] == [
        100.0, 0.0, 0.0, 0.0, 0.0]
    assert "releases" not in asserted


def test_the_spill_window_is_the_questions_own_input_and_stays_a_param():
    asserted = _template().STEERING.ASSERTED
    assert asserted["sources"]["window_s"].name == "release_duration_s"
    assert asserted["sources"]["until_s"].path == "settled.until_s"


def test_the_deck_releases_the_substance_dissolved_and_carries_the_sediment_in():
    steering = _template().STEERING
    # The declared row is the dissolved substance, so it is position one and the
    # sediment the process appends is position two. Nothing fetches suspended
    # sediment, so the ambient class is a STATED condition, not a Param -
    # 30 mg/L as kg/m3, the water's own typical suspended load.
    assert steering.VALUES_OF_THE_TRACERS_AT_THE_SOURCES[0] == 100.0
    assert steering.VALUES_OF_THE_TRACERS_AT_THE_SOURCES[1:] == [0.0, 0.0, 0.0, 0.0]
    assert steering.INITIAL_VALUES_OF_TRACERS[0] == 0.0
    assert steering.INITIAL_VALUES_OF_TRACERS[1] == 0.03
    assert steering.INITIAL_VALUES_OF_TRACERS[2:] == [0.0, 0.0, 0.0]


def test_the_deck_states_the_process_and_leaves_its_constants_to_the_engine():
    """The settling, the sorption equilibrium, the desorption kinetic and the
    decay are WAQTEL's own keywords. This question is asked of a substance it is
    never told the name of, so it states none of them and each engine default
    stands."""
    (body,) = _template().STEERING.coupling
    assert body["process"] == PROCESS
    assert dict(body["slots"]) == {}


def test_the_question_is_read_where_the_user_put_the_monitoring_point():
    template = _template()
    (placed,) = template.OUTPUTS
    assert (placed.kind, placed.variable, placed.publish) == (
        "series", "T1", "chart")
    assert placed.variable in template.CAPTIONS


def test_the_declared_params_resolve():
    from trid3nt_server.workflows.runtime import resolve_params

    workflow = _template().telemac_micropollutant_release.workflow
    asyncio.run(resolve_params(workflow.params, {}))


def test_the_template_declares_none_of_the_runtime_s_own_rows():
    """The domain, the bed, the granularity, the moment and the sizing class are
    the runtime's; the deck's clock, roughness and cadence are keywords the
    dictionary describes. What is left is the question's own."""
    from trid3nt_server.workflows.runtime.levers import LEVER_NAMES

    from trid3nt_server.workflows.runtime import param_rows

    workflow = _template().telemac_micropollutant_release.workflow
    declared = [prm.name for prm in workflow.params]
    # Every lever is on the sheet; the ones this question does not state for
    # itself are seated at the end, in the order the runtime declares them, and
    # one it differs on keeps its own place and its own opinion of the value.
    own = {prm.name for prm in param_rows(_template().PARAMS)}
    assert set(LEVER_NAMES) <= set(declared)
    seated = [name for name in LEVER_NAMES if name not in own]
    assert declared[-len(seated):] == seated
    for gone in ("location", "bbox", "river_geometry_uri", "reach_length_km",
                 "friction_law", "friction_coefficient", "output_interval_min",
                 "sim_duration_s"):
        assert gone not in declared
    steering = _template().STEERING
    assert steering.LAW_OF_BOTTOM_FRICTION == 3
    assert steering.FRICTION_COEFFICIENT == 33.0
    assert steering.DURATION == 172800.0


def test_the_four_slots_are_the_world_this_run_stands_on():
    """One domain, one bed, both stated as the CLASS the match fills them from -
    no ladder, no producer, no separate runs row: the boundary runs ride on
    whichever source answers the domain."""
    from trid3nt_server.workflows.runtime.data import BED, DOMAIN

    data = {decl.name: decl for decl in
            _template().telemac_micropollutant_release.workflow.data}
    assert data["domain"].role == DOMAIN
    assert data["domain"].data_class == "hydrography"
    assert data["bed"].role == BED
    assert data["bed"].data_class == "bathymetry"
    assert "runs" not in data and "survey" not in data
    assert "surveyed_bed" not in data and "terrain" not in data


def test_the_discharge_reaches_the_channel_as_ONE_reading_never_the_record():
    """The step that opens the channel refuses a record nobody chose a site
    from, so the flow is an OBSERVATION ranked against the domain's own point."""
    from trid3nt_server.workflows.runtime.data import DISCHARGE

    data = {decl.name: decl for decl in
            _template().telemac_micropollutant_release.workflow.data}
    discharge = data["discharge"]
    assert discharge.role == DISCHARGE and discharge.is_context
    assert discharge.data_class == "discharge series"


def test_both_placed_points_carry_the_domain_they_are_placed_along():
    """Neither point is required, and an unplaced one sits its fraction along
    the domain's centerline companion - which only the domain carries."""
    from trid3nt_server.workflows.runtime import DataRef, Ref

    plan = _template().telemac_micropollutant_release.workflow.plan
    placed = [step for step in plan.declared()
              if step.name in ("source", "monitoring")]
    assert [step.kwargs["domain"] for step in placed] == [DataRef("domain")] * 2


def test_an_unsurveyed_domain_still_runs_on_the_terrain_alone():
    """The bed is ONE need row now; the survey-then-terrain composition is the
    matched bed's own affair, not a second CONTEXT row on this template."""
    data = {decl.name: decl for decl in
            _template().telemac_micropollutant_release.workflow.data}
    assert data["bed"].is_context is False


def test_the_mesh_the_workflow_builds_paints_the_one_bed_row():
    from trid3nt_server.mesh.tool import recipe_from_plan_value
    from trid3nt_server.workflows.runtime import DataRef, Ref

    plan = _template().telemac_micropollutant_release.workflow.plan
    recipe = recipe_from_plan_value(
        next(s for s in plan.steps if s.name == "mesh").kwargs["mesh"])
    assert recipe.extent == DataRef("domain")
    assert recipe.resolution_m.name == "mesh_resolution_m"
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}
    # No runs row: the boundary runs ride on the domain's own producer.
    runs = next(op for op in recipe.ops if op.fn == "set_boundary_roles")
    assert runs.kwargs == {"runs": DataRef("domain")}


def test_the_plan_reads_as_the_universal_stage_sequence():
    plan = _template().telemac_micropollutant_release.workflow.plan
    assert [step.name for step in plan.declared()] == [
        "stated", "mesh", "mesh_files", "channel", "source", "monitoring",
        "settled", "sheet", "solve", "outputs"]
    assert [step.stage for step in plan.declared() if step.stage] == [
        "prep", "mesh", "author", "author", "author", "author", "author",
        "author", "solve", "publish"]


def test_the_ambient_sediment_is_a_stated_deck_opinion_not_a_param():
    """Nothing fetches suspended sediment; ambient_spm_kg_m3 was a straight twin
    of INITIAL VALUES OF TRACERS position 2, so it is the keyword's own stated
    value now, like FRICTION_COEFFICIENT above it, and carries no Param."""
    from trid3nt_server.workflows.runtime import param_rows

    declared = {prm.name for prm in param_rows(_template().PARAMS)}
    assert "ambient_spm_kg_m3" not in declared
    assert "source_q_m3s" not in declared
    assert "source_concentration_mgl" not in declared
