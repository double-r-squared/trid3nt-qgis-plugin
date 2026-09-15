"""WAQTEL's algal processes: the rows, the process choice and the template's deck.

No solve, no network. What the engine's own Fortran appends and reads is pinned
here against the two upstream examples' decks, so a row renamed or a keyword
dropped is caught without a solver. The live agreement with those examples'
reference files is the driver's job, not this file's."""

import pytest

from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, fill, waqtel
from trid3nt_server.workflows.telemac.modules.module import SlotRefused
from trid3nt_server.workflows.telemac.modules.outputs import (
    OutputEmpty,
    Solved,
)
#: The two algal processes, their rows and the body, read off the wrapper that
#: carries them.
APPENDED = waqtel._APPENDED
BIOMASS = waqtel._BIOMASS
EUTRO = waqtel._EUTRO
eutrophication = WAQTEL.eutrophication

#: The eight names the engine's ``nametrac_waqtel.F`` appends under EUTRO, in the
#: order it appends them, and the five it appends under BIOMASS.
_EUTRO_NAMES = ("PHYTO BIOMASS", "DISSOLVED PO4", "POR NON ASSIMIL",
                "DISSOLVED NO3", "NOR NON ASSIM", "NH4 LOAD", "ORGANIC LOAD",
                "DISSOLVED O2")
_BIOMASS_NAMES = ("PHYTO BIOMASS", "DISSOLVED PO4", "POR NON ASSIM",
                  "DISSOLVED NO3", "NOR NON ASSIM")

#: Every keyword the eutrophication source term reads and the biomass one does
#: not, spelled as WAQTEL's dictionary identifies it.
_OXYGEN_KEYWORDS = ("CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K120",
                    "CONSTANT_FOR_THE_NITRIFICATION_KINETIC_K520",
                    "OXYGEN_PRODUCED_BY_PHOTOSYNTHESIS",
                    "CONSUMED_OXYGEN_BY_NITRIFICATION", "BENTHIC_DEMAND",
                    "K2_REAERATION_COEFFICIENT", "FORMULA_FOR_COMPUTING_K2",
                    "O2_SATURATION_DENSITY_OF_WATER__CS_",
                    "FORMULA_FOR_COMPUTING_CS",
                    "SEDIMENTATION_VELOCITY_OF_ORGANIC_LOAD", "WATER_SALINITY")

#: The nineteen the growth term reads, which both processes run.
_GROWTH_KEYWORDS = ("MAXIMUM_ALGAL_GROWTH_RATE_AT_20C",
                    "ALGAL_TOXICITY_COEFFICIENTS",
                    "PARAMETER_OF_CALIBRATION_OF_SMITH_FORMULA", "SECCHI_DEPTH",
                    "SUNSHINE_FLUX_DENSITY_ON_WATER_SURFACE",
                    "METHOD_OF_COMPUTATION_OF_RAY_EXTINCTION_COEFFICIENT",
                    "VEGETAL_TURBIDITY_COEFFICIENT_WITHOUT_PHYTO",
                    "CONSTANT_OF_HALF_SATURATION_WITH_PHOSPHATE",
                    "CONSTANT_OF_HALF_SATURATION_WITH_NITROGEN",
                    "COEFFICIENTS_OF_ALGAL_MORTALITY_AT_20C",
                    "RESPIRATION_RATE_OF_ALGAL_BIOMASS",
                    "PROPORTION_OF_PHOSPHORUS_WITHIN_PHYTO_CELLS",
                    "PERCENTAGE_OF_PHOSPHORUS_ASSIMILABLE_IN_DEAD_PHYTO",
                    "PROPORTION_OF_NITROGEN_WITHIN_PHYTO_CELLS",
                    "PERCENTAGE_OF_NITROGEN_ASSIMILABLE_IN_DEAD_PHYTO",
                    "RATE_OF_TRANSFORMATION_OF_POR_TO_PO4",
                    "RATE_OF_TRANSFORMATION_OF_NOR_TO_NO3",
                    "SEDIMENTATION_VELOCITY_OF_ORGANIC_PHOSPHORUS",
                    "SEDIMENTATION_VELOCITY_OF_NON_ALGAL_NITROGEN")

#: A value each listed keyword takes, so a whole set can be stated at once
#: without a slot refusing on type.
_VALUE = {"METHOD_OF_COMPUTATION_OF_RAY_EXTINCTION_COEFFICIENT": 1,
          "FORMULA_FOR_COMPUTING_K2": 0, "FORMULA_FOR_COMPUTING_CS": 0,
          "ALGAL_TOXICITY_COEFFICIENTS": [1.0, 0.0],
          "COEFFICIENTS_OF_ALGAL_MORTALITY_AT_20C": [0.1, 0.0]}


def _stated(*keywords):
    return {name: _VALUE.get(name, 0.5) for name in keywords}


def test_the_process_numbers_are_the_engines_primes():
    assert (BIOMASS, EUTRO) == (3, 5)


def test_eutro_appends_the_eight_tracers_in_the_engines_order():
    assert tuple(row.name for row in APPENDED[EUTRO]) == _EUTRO_NAMES


def test_biomass_appends_the_first_five():
    assert tuple(row.name for row in APPENDED[BIOMASS]) == _BIOMASS_NAMES


def test_the_third_tracer_is_spelled_differently_by_each_process():
    """The engine writes ``POR NON ASSIMIL`` under EUTRO and ``POR NON ASSIM``
    under BIOMASS for the same quantity, so one row set cannot serve both."""
    assert APPENDED[EUTRO][2].name != APPENDED[BIOMASS][2].name


def test_every_appended_row_draws_and_names_its_unit():
    for number, rows in APPENDED.items():
        for row in rows:
            assert row.style["kind"] == "mesh", (number, row.name)
            assert row.style["ramp"] and row.style["units"], (number, row.name)


def test_the_rows_of_one_process_are_told_apart_by_their_ramps():
    ramps = [row.style["ramp"] for row in APPENDED[EUTRO]]
    assert len(set(ramps)) == len(ramps)


def test_the_oxygen_row_caps_its_legend_off_the_drying_node():
    """The oxygen is advected as depth times concentration, so the step a node
    dries on carries hundreds of mg/L and the water it borders carries 8.7."""
    import numpy as np

    from trid3nt_server.render import presets

    style = {row.name: row.style for row in APPENDED[EUTRO]}["DISSOLVED O2"]
    assert style["range"] == "p99.9"
    # A reach record's own shape: a near-constant saturated field and the one
    # node-step the drying front threw out of the balance.
    record = np.concatenate([np.full(9999, 8.667), [664.697]])
    lo, hi = presets.measured_range(record, style)
    assert lo == 0.0, "the declared floor still pins the bottom"
    assert hi == pytest.approx(8.667, abs=0.01), hi


def test_the_oxygen_mask_sits_under_the_field_the_legend_ranges():
    """A tracer's visible edge is a fraction of the magnitude its row declares.

    Read off the record maximum instead, the oxygen's edge lands at 33 mg/L over
    an 8.7 mg/L field and every frame is masked to nothing."""
    import numpy as np

    from trid3nt_server.render import presets
    from trid3nt_server.workflows.telemac.modules import outputs

    style = {row.name: row.style for row in APPENDED[EUTRO]}["DISSOLVED O2"]
    # A reach record's own shape: dry nodes at zero, the saturated water, and
    # the one node-step the drying front threw out of the balance.
    record = np.concatenate([np.zeros(200), np.full(9799, 8.667), [664.697]])
    floor = outputs._floor("T8", record, style)
    assert floor == pytest.approx(0.05 * 8.667, abs=0.01), floor
    assert floor < 8.667, "the saturated field is drawn, not masked"
    assert floor <= presets.measured_range(record, style)[1]
    # The same record on a row that declares no cap is what the ruling names:
    # an edge above the whole field, and every frame masked to nothing.
    uncapped = outputs._floor("T8", record, {"floor": 0})
    assert uncapped == pytest.approx(33.23, abs=0.01)
    assert uncapped > 8.667


def test_the_three_oxygen_rows_draw_as_the_oxygen_process_already_draws_them():
    """One variable, one style, whichever process wrote it."""
    shared = {row.name: row.style for row in waqtel._APPENDED[2]}
    for name in shared:
        theirs = [row.style for rows in waqtel._APPENDED.values()
                  for row in rows if row.name == name]
        assert len(theirs) > 1 and all(s == shared[name] for s in theirs), name


def test_the_oxygen_flag_is_the_whole_of_the_process_choice():
    assert eutrophication()["process"] == BIOMASS
    assert eutrophication(oxygen=True)["process"] == EUTRO


def test_the_growth_constants_alone_run_under_biomass():
    body = eutrophication(**_stated(*_GROWTH_KEYWORDS))
    assert body["process"] == BIOMASS


def test_the_growth_constants_run_under_eutro_too():
    """The eutrophication source term is the biomass one plus the oxygen half,
    so every growth constant is read under both."""
    body = eutrophication(oxygen=True, **_stated(*_GROWTH_KEYWORDS))
    assert body["process"] == EUTRO


@pytest.mark.parametrize("keyword", _OXYGEN_KEYWORDS)
def test_an_oxygen_keyword_under_biomass_refuses_rather_than_being_dropped(keyword):
    """Process 3's source term never reads these, so a body stating one under it
    would carry a number the engine silently ignores."""
    with pytest.raises(SlotRefused) as refusal:
        eutrophication(**_stated(keyword))
    assert keyword in str(refusal.value)


@pytest.mark.parametrize("keyword", _OXYGEN_KEYWORDS)
def test_every_oxygen_keyword_is_stated_under_eutro(keyword):
    body = eutrophication(oxygen=True, **_stated(keyword))
    assert keyword in body["slots"]


def test_a_keyword_waqtel_does_not_have_refuses_by_name():
    with pytest.raises(SlotRefused) as refusal:
        eutrophication(oxygen=True, WATER_TEMPERATURE_C=20.0)
    assert "WATER_TEMPERATURE_C" in str(refusal.value)


def test_a_body_states_only_what_it_was_handed():
    body = eutrophication(oxygen=True, WATER_TEMPERATURE=20.0,
                          FORMULA_FOR_COMPUTING_CS=1)
    assert dict(body["slots"]) == {"WATER_TEMPERATURE": 20.0,
                                   "FORMULA_FOR_COMPUTING_CS": 1}


def test_every_keyword_either_process_reads_fills_against_the_dictionary():
    stated = _stated(*_GROWTH_KEYWORDS, *_OXYGEN_KEYWORDS, "WATER_TEMPERATURE")
    sheet = fill(WAQTEL, **dict(eutrophication(oxygen=True, **stated)["slots"]))
    assert len(dict(sheet.resolved())) == len(stated)


def test_the_carrier_states_the_process_and_the_steering_file():
    sheet = fill(T2D, coupling=[eutrophication(oxygen=True,
                                               WATER_TEMPERATURE=22.0)])
    resolved = dict(sheet.resolved())
    assert resolved["COUPLING WITH"] == "WAQTEL"
    assert resolved["WATER QUALITY PROCESS"] == EUTRO
    assert resolved["WAQTEL STEERING FILE"] == waqtel.STEERING_FILENAME


def _coupled_sheet(**given):
    return fill(T2D, GEOMETRY_FILE="g.slf", BOUNDARY_CONDITIONS_FILE="g.cli",
                RESULTS_FILE="r.slf", coupling=[eutrophication(**given)])


def test_a_carrier_with_no_tracer_of_its_own_carries_the_processs_eight():
    sheet = _coupled_sheet(oxygen=True, WATER_TEMPERATURE=22.0)
    assert tuple(row.name for row in sheet.tracers) == _EUTRO_NAMES


def test_the_printouts_keyword_names_every_appended_tracer():
    sheet = _coupled_sheet(oxygen=True, WATER_TEMPERATURE=22.0)
    written = sheet.printouts()["VARIABLES FOR GRAPHIC PRINTOUTS"].split(",")
    assert [t for t in written if t.startswith("T")] == [
        f"T{n}" for n in range(1, 9)]


def test_a_biomass_carrier_carries_five():
    sheet = _coupled_sheet(WATER_TEMPERATURE=22.0)
    assert tuple(row.name for row in sheet.tracers) == _BIOMASS_NAMES


def _template():
    from trid3nt_server.workflows.telemac.templates.river_eutrophication import (
        river_eutrophication,
    )

    return river_eutrophication


def test_the_template_asks_for_the_oxygen_half():
    body = _template().STEERING.ASSERTED["coupling"][0]
    assert body["process"] == EUTRO


def test_the_template_states_its_constants_by_the_engines_own_keyword_names():
    body = _template().STEERING.ASSERTED["coupling"][0]
    for name in body["slots"]:
        assert WAQTEL.slot(name).keyword


def test_the_template_states_one_value_per_appended_tracer():
    steering = _template().STEERING.ASSERTED
    assert len(steering["INITIAL_VALUES_OF_TRACERS"]) == len(APPENDED[EUTRO])
    assert len(steering["boundaries"]["tracers"]) == len(APPENDED[EUTRO])


def test_the_template_opens_holding_the_water_its_boundaries_carry():
    """The question is what the reach does to water of one composition, so a
    boundary value different from the initial one would answer a step change.

    Compared by REPR: a late-bound read refuses to be compared for equality at
    plan-construction time, and what is pinned here is which read each is."""
    steering = _template().STEERING.ASSERTED
    assert ([repr(v) for v in steering["INITIAL_VALUES_OF_TRACERS"]]
            == [repr(v) for v in steering["boundaries"]["tracers"]])


def test_the_template_declares_no_tracer_of_its_own():
    assert "NUMBER_OF_TRACERS" not in _template().STEERING.ASSERTED


def test_the_template_publishes_nothing_itself():
    """Every read it places is a chart; the modules' tables publish the rest."""
    assert {p.publish for p in _template().OUTPUTS} == {"chart"}


def test_the_template_captions_exactly_what_it_placed():
    template = _template()
    placed = {p.variable for p in template.OUTPUTS}
    assert set(template.CAPTIONS) == placed


def test_the_window_is_one_pass_rather_than_a_season():
    """A reach flushes in hours; the default window is two days of passes."""
    from trid3nt_server.workflows.runtime import param_rows

    from trid3nt_server.workflows.telemac.templates.river_eutrophication import (
        declarations,
    )

    rows = {row.name: row for row in param_rows(declarations.PARAMS)}
    assert rows["sim_duration_s"].default == 172800.0


def test_the_answer_is_the_change_between_the_water_in_and_the_water_out():
    answer = _template().ANSWER
    assert answer["phyto_growth_ratio"].held_to == "initial_phyto_ug_l"
    assert answer["no3_remaining_ratio"].held_to == "initial_no3_mgl"
    assert answer["po4_remaining_ratio"].held_to == "initial_po4_mgl"
    assert answer["do_below_standard"].held_to == "do_standard_mgl"


def test_every_answer_is_read_along_the_reach():
    """The longitudinal change is the question, and a series' measures are the
    whole domain's at each instant - so no answer rides one."""
    for name, measure in _template().ANSWER.items():
        assert measure.primitive.kind in ("profile", "mesh"), name


def test_the_answer_reads_the_biomass_the_nutrients_and_the_oxygen():
    reads = {name: m.primitive.variable for name, m in _template().ANSWER.items()}
    assert reads["phyto_max_ug_l"] == "T1"
    assert reads["po4_remaining_ratio"] == "T2"
    assert reads["no3_remaining_ratio"] == "T4"
    assert reads["do_min_mgl"] == "T8"


#: What each primitive this template reads actually computes. A stat outside its
#: own row answers None, and a delivery REFUSES a null declared answer, so the
#: names are pinned here rather than discovered on a live run.
_MEASURES = {
    "series": {"max", "t_max", "frames", "truncated", "active_frames"},
    "profile": {"min", "x_min_m", "max", "x_max_m", "t", "stations",
                "velocity_mps"},
    "mesh": {"nodes", "elements", "planes", "epsg", "size_m",
             "resolution_label"},
}


def test_every_answer_names_a_measure_its_own_read_carries():
    """A stat the read does not compute answers None, which a delivery refuses."""
    for name, measure in _template().ANSWER.items():
        assert measure.stat in _MEASURES[measure.primitive.kind], name


#: A result carrying a carrier's own variables and then the process's eight, as
#: the engine writes them: name in the first 16 characters, unit after.
_VARNAMES = ["VELOCITY U      ", "VELOCITY V      ", "WATER DEPTH     "] + [
    f"{name:<16}" for name in _EUTRO_NAMES]
_VARUNITS = ["M/S", "M/S", "M", " MICRO_G/L", "MG/L", "MG/L", "MG/L", "MG/L",
             "MGNH4/L", "MGO2/L", "MGO2/L"]


class _Read(Solved):
    """A solved run whose result is stated rather than downloaded."""

    @property
    def result(self):
        return {"varnames": _VARNAMES, "varunits": _VARUNITS}


def _solved(tracer_names):
    return _Read({"run_id": "r", "utm_epsg": 32616, "result_basename": "r.slf",
                  "tracer_names": tracer_names}, T2D)


@pytest.mark.parametrize("token,name", [("T1", "PHYTO BIOMASS"),
                                        ("T6", "NH4 LOAD"),
                                        ("T8", "DISSOLVED O2")])
def test_the_tokens_the_template_reads_name_the_engines_own_variables(token, name):
    """The run record carries every tracer the sheet counted, appended ones
    included, so a read names the variable by position through its name."""
    sheet = _coupled_sheet(oxygen=True, WATER_TEMPERATURE=22.0)
    record = [f"{row.name:<16}{row.unit:<16}" for row in sheet.tracers]
    assert _solved(record).variable(token)[0].strip() == name


def test_a_carrier_declaring_no_tracer_counts_them_from_its_own_table():
    """Nothing is released into this reach, so the carrier declares no tracer
    and there is no declared name to count the appended ones from: they begin
    where the HOST's own table ends, which is the anchor such a record carries.
    Past the last one the read still refuses."""
    read = _solved(None)
    assert read.variable("T1")[0].strip() == "PHYTO BIOMASS"
    assert read.variable("T8")[0].strip() == "DISSOLVED O2"
    with pytest.raises(OutputEmpty):
        read.variable("T9")
