"""WAQTEL micropol: the rows the process appends, and the deck sized to them.

Offline. What is proved here is the tracer bookkeeping the whole deck hangs off -
five rows, in the engine's order, with the declared one adopted rather than
doubled - the keywords the coupled body carries by their own names, and the
refusal that keeps the second sorption site out of a five-row process."""

from __future__ import annotations

import asyncio

import pytest

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
    assert [row.name for row in ROWS] == _ENGINE_ORDER
    assert {row.unit for row in ROWS} == {"mg/L"}


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
    from trid3nt_server.workflows.telemac.templates.river_micropollutant import (
        river_micropollutant as template,
    )

    return template


def test_every_tracer_sized_array_on_the_deck_carries_one_value_per_row():
    steering = _template().STEERING
    assert steering.NUMBER_OF_TRACERS == 1
    assert len(steering.NAMES_OF_TRACERS) == 1
    assert len(steering.INITIAL_VALUES_OF_TRACERS) == len(ROWS)
    assert len(steering.boundaries["tracers"]) == len(ROWS)
    (release,) = steering.releases
    assert len(release["tracers"]) == len(ROWS)


def test_the_deck_releases_the_substance_dissolved_and_carries_the_sediment_in():
    steering = _template().STEERING
    # The declared row is the dissolved substance, so it is position one and the
    # sediment the process appends is position two.
    (release,) = steering.releases
    assert release["tracers"][0].name == "source_concentration_mgl"
    assert release["tracers"][1:] == [0.0, 0.0, 0.0, 0.0]
    assert steering.INITIAL_VALUES_OF_TRACERS[0] == 0.0
    assert steering.INITIAL_VALUES_OF_TRACERS[1].name == "ambient_spm_mgl"
    assert steering.INITIAL_VALUES_OF_TRACERS[2:] == [0.0, 0.0, 0.0]


def test_the_deck_states_the_partition_as_waqtel_s_own_keyword_names():
    (body,) = _template().STEERING.coupling
    assert body["process"] == PROCESS
    assert {name: value.name for name, value in body["slots"].items()} == {
        "SEDIMENT_SETTLING_VELOCITY": "settling_velocity_mps",
        "COEFFICIENT_OF_DISTRIBUTION": "distribution_coefficient_m3kg",
        "CONSTANT_OF_DESORPTION_KINETIC": "desorption_constant_per_s",
        "EXPONENTIAL_DESINTEGRATION_CONSTANT": "decay_constant_per_s"}


def test_the_question_is_read_where_the_user_put_the_monitoring_point():
    template = _template()
    (placed,) = template.OUTPUTS
    assert (placed.kind, placed.variable, placed.publish) == (
        "series", "T1", "chart")
    assert placed.variable in template.CAPTIONS
    # The peak and its instant are measures of the SAME read the chart shows.
    for name in ("dissolved_cmax_mgl", "dissolved_peak_time_s"):
        assert template.ANSWER[name].primitive == placed.key


def test_the_answer_reads_all_three_phases_at_the_last_instant():
    answer = _template().ANSWER
    assert [answer[name].primitive.variable for name in (
        "dissolved_final_mean_mgl", "suspended_sorbed_final_mean_mgl",
        "bed_sorbed_final_mean_mgl")] == ["T1", "T4", "T5"]
    assert {answer[name].stat for name in (
        "dissolved_final_mean_mgl", "suspended_sorbed_final_mean_mgl",
        "bed_sorbed_final_mean_mgl")} == {"mean"}


def test_the_declared_params_and_the_plan_validate():
    from trid3nt_server.workflows.runtime import resolve_params, validate_plan

    workflow = _template().telemac_river_micropollutant.workflow
    resolved = asyncio.run(resolve_params(
        workflow.params, {"location": "Eel River near Scotia, California"}))
    validate_plan(workflow.plan, workflow.params, workflow.data)
    # Nothing fetches suspended sediment, so the sorbent is a STATED condition.
    assert resolved.value_of("ambient_spm_mgl") == 30.0
    # Every constant the engine itself defaults is left unstated by the deck.
    for name in ("settling_velocity_mps", "distribution_coefficient_m3kg",
                 "desorption_constant_per_s", "decay_constant_per_s"):
        assert resolved.value_of(name) is None


def test_the_plan_reads_as_the_universal_stage_sequence():
    plan = _template().telemac_river_micropollutant.workflow.plan
    assert [step.stage for step in plan.declared() if step.stage] == [
        "acquire", "acquire", "acquire", "mesh", "mesh",
        "author", "author", "author", "author", "solve", "publish"]
    assert [step.name for step in plan.declared()][-1] == "outputs"


@pytest.mark.asyncio
async def test_the_template_requires_a_location_or_a_bbox():
    out = await _template().telemac_river_micropollutant()
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"
