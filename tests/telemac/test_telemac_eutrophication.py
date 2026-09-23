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


def test_the_oxygen_row_is_ranged_over_the_wet_nodes_and_pins_no_bottom():
    """The oxygen is advected as depth times concentration, so the step a node
    dries on carries hundreds of mg/L; those nodes are outside the wet mask the
    legend is taken over. What is left is a field between 8.0 and 8.7 mg/L, and
    a ramp pinned to zero would spend half a percent of its colours on it."""
    import numpy as np

    from trid3nt_server.render import presets
    from trid3nt_server.workflows.telemac.modules.outputs import _drawn

    style = {row.name: row.style for row in APPENDED[EUTRO]}["DISSOLVED O2"]
    assert "range" not in style and "floor" not in style
    # A reach record's own shape: a near-constant saturated field and the one
    # node-step the drying front threw out of the balance.
    record = np.concatenate([np.full(9999, 8.667), [664.697]])
    wet = np.concatenate([np.full(9999, True), [False]])
    lo, hi = presets.measured_range(_drawn(record, wet), style)
    assert hi == pytest.approx(8.667, abs=0.01), hi
    assert lo > 0.0, "a background variable is ranged over what it measured"


def test_a_background_variable_declares_no_visible_edge():
    """A quantity the water already carries everywhere has no boundary to draw:
    masking it below a fraction of its own peak erases the field. A RELEASED
    quantity is the opposite case, and the micropollutant rows say so."""
    from trid3nt_server.workflows.telemac.modules.outputs import _floor

    background = {row.name: row for row in APPENDED[EUTRO]}
    assert background["DISSOLVED O2"].has_edge is False
    assert all(row.has_edge is False for row in APPENDED[EUTRO])
    assert all(row.has_edge is True for row in APPENDED[waqtel._MICROPOL])
    import numpy as np

    record = np.concatenate([np.zeros(200), np.full(9799, 8.667), [664.697]])
    assert _floor(False, record, background["DISSOLVED O2"].style) is None
    # the same record on a row that DOES declare an edge is the ruled contrast:
    # an edge above the whole field, and every frame masked to nothing.
    assert _floor(True, record, {"floor": 0}) == pytest.approx(33.23, abs=0.01)


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
    from trid3nt_server.workflows.telemac.templates.eutrophication import (
        eutrophication,
    )

    return eutrophication


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
    """Every OUTPUTS read has a caption; a row's own caption, keyed by its
    name rather than a published variable, is additive beside it."""
    template = _template()
    placed = {p.variable for p in template.OUTPUTS}
    assert placed <= set(template.CAPTIONS)
    assert set(template.CAPTIONS) - placed == {"discharge", "level", "observe"}


def test_the_window_is_one_pass_rather_than_a_season():
    """A reach flushes in hours; the deck's own DURATION is two days of passes,
    and a user who wants another window sets that keyword by its name."""
    assert _template().STEERING.ASSERTED["DURATION"] == 172800.0


def test_a_clarity_the_deck_has_no_opinion_about_is_left_unwritten():
    """An unstated keyword is the engine's own default, which is what the sheet
    reports; a param that carried only optionality stated nothing at all."""
    slots = _template().STEERING.ASSERTED["coupling"][0]["slots"]
    assert "SECCHI_DEPTH" not in slots


def test_each_longitudinal_chart_draws_the_water_it_entered_on():
    """The water the reach ARRIVED at is drawn as a reference line on the chart
    that plots that tracer, off the value the deck states for it - one number,
    not a second surface in front of it."""
    from trid3nt_server.workflows.telemac.modules.outputs import Line, Profile

    template = _template()
    entering = template.STEERING.ASSERTED["INITIAL_VALUES_OF_TRACERS"]
    drawn = {}
    for placed in template.OUTPUTS:
        if placed.kind != "profile" or placed.reference is None:
            continue
        read = Profile(name=placed.variable, units="mg/L",
                       distance_m=[0.0, 100.0], values=[1.0, 2.0],
                       along="downstream distance")
        lines = placed.reference(read, {}, {"do_standard_mgl": 5.0})
        drawn[placed.variable] = lines[0] if lines else None
    assert drawn["T1"].values == [entering[0], entering[0]]
    assert drawn["T2"].values == [entering[1], entering[1]]
    assert drawn["T4"].values == [entering[3], entering[3]]
    assert drawn["T8"] == Line(label="5 mg/L standard", x=[0.0, 100.0],
                               values=[5.0, 5.0])


def test_the_charts_plot_the_biomass_the_nutrients_and_the_oxygen():
    """The longitudinal change is the question, so every tracer this template
    judges is charted ALONG the reach rather than watched at one node."""
    charted = [p.variable for p in _template().OUTPUTS if p.kind == "profile"]
    assert charted == ["T1", "T2", "T4", "T8"]


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


# -- the domain wave: three slots, seated levers, no restated plan ------------ #


def _plan():
    return _template().telemac_eutrophication.workflow


def test_the_workflow_owns_the_stages_and_the_template_adds_its_own_two():
    """The template states no mesh recipe, no file names and no settle step: the
    domain, the bed and the runs are what the plan is built from. What it DOES
    add is the one thing a question with a CURRENT derives on top of an
    engine-neutral domain: the uniform-flow opening the deck is written at."""
    assert [step.label for step in _plan().plan.steps] == [
        "stated", "mesh", "channel", "settled", "sheet", "solve", "outputs"]


def test_the_mesh_is_built_over_the_domain_slot_at_the_runtimes_lever():
    from trid3nt_server.workflows.mesh.tool import recipe_from_plan_value
    from trid3nt_server.workflows.runtime import DataRef

    recipe = recipe_from_plan_value(
        next(s for s in _plan().plan.steps
             if s.name == "mesh").kwargs["mesh"])
    assert recipe.extent == DataRef("domain")
    assert recipe.resolution_m.name == "mesh_resolution_m"
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}
    # No runs row: the edge rides on the domain's own producer, which measured
    # it where it cut the polygon.
    runs = next(op for op in recipe.ops if op.fn == "set_boundary_roles")
    assert runs.kwargs == {"runs": DataRef("domain")}


def test_the_bed_is_a_class_the_match_composes():
    """set_bed takes ONE source. A survey covers the channel and the terrain
    covers the banks; which of each reaches this domain, and the merge between
    them, is the match's and the runtime's, not a row this template states."""
    rows = {row.name: row for row in _plan().data}
    assert rows["bed"].role == "bed"
    assert rows["bed"].data_class == "bathymetry"
    assert [name for name, row in rows.items() if row.role == "bed"] == ["bed"]
    assert {"survey", "surveyed_bed", "terrain"} & set(rows) == set()


def test_the_water_temperature_record_is_one_reading_off_the_nearest_site():
    """The engine reads ONE water temperature, and the portal returns sample
    SITES. Which of them the run is judged against is the observation slot's
    choice, ranked from inside the domain, and the journal carries the site and
    the date because a sample is a moment."""
    row = {r.name: r for r in _plan().data}["observe"]
    assert row.role == "observe"
    assert row.data_class == "water quality sample"
    # This run publishes no temperature variable, so the record is read in
    # the unit it was measured in and the row states none.
    assert not row.observes


def test_water_nobody_sampled_is_a_sentence_rather_than_a_refusal():
    """A place with no sample is a fact about the place, not a reason to refuse
    a run whose temperature is stated anyway."""
    row = {r.name: r for r in _plan().data}["observe"]
    assert row.is_context
    assert "the stated value stands" in row.context_sentence


def test_the_stated_temperature_is_the_decks_own_keyword():
    """The row is what the stated number is READ AGAINST; it never fills the
    keyword. A domain with no sample would otherwise leave the growth, the
    mortality and the oxygen ceiling with no temperature at all."""
    assert _template().STEERING.ASSERTED["coupling"][0]["slots"][
        "WATER_TEMPERATURE"] == 22.0


def test_the_three_slots_reach_the_wire_so_a_drawn_body_supersedes_the_fetch():
    filled = {row.name for row in _plan().data if row.fills_from_user}
    assert {"domain", "bed"} <= filled


def test_the_runtime_levers_are_seated_and_no_keyword_twin_is_declared():
    """A template declares no param the dictionary or the runtime already
    describes; mesh_resolution_m is restated only because this question's
    default differs from the runtime's."""
    declared = {prm.name for prm in _plan().params}
    assert {"event_time", "cores", "mesh_resolution_m"} <= declared
    assert not declared & {"location", "bbox", "river_geometry_uri",
                           "reach_length_km", "friction_coefficient",
                           "friction_law", "output_interval_min",
                           "sim_duration_s", "water_temp_c", "sunshine_w_m2",
                           "secchi_depth_m", "do_saturation_mgl",
                           "initial_phyto_ug_l", "initial_do_mgl"}


def test_the_carrier_flow_is_one_reading_the_open_channel_step_can_read():
    """The discharge the inflow prescribes is whichever source's coverage row
    matches this domain, read as ONE value - the step that opens the channel
    refuses a record nobody chose a site from - and its absence is legal."""
    row = {r.name: r for r in _plan().data}["discharge"]
    assert row.role == "discharge"
    assert row.data_class == "discharge series"
    assert row.is_optional


def test_the_longitudinal_reads_are_taken_along_the_line_slot():
    """The producer measured the line and it rides on the domain artifact, so
    the LINE slot is filled from it rather than the network being walked twice.
    A body of water whose producer measured none is asked for the line."""
    from trid3nt_server.workflows.runtime import Ref

    template = _template()
    along = {p.along for p in template.OUTPUTS if p.kind == "profile"}
    assert along == {Ref("line")}
    assert "centerline" not in {row.name for row in _plan().data}
    assert {row.name for row in _plan().data if row.role == "line"} == {"line"}


def test_the_stretch_the_producer_walks_is_the_templates_own_number():
    """Reach length is a domain twin: the runtime and the domain slot describe
    how far the water runs, so the template states its opinion of the value
    instead of declaring a param for it. A user who wants another stretch
    supplies the domain polygon."""
    from trid3nt_server.workflows.telemac.templates.eutrophication import (
        eutrophication,
    )

    assert "reach_length_km" not in {prm.name for prm in _plan().params}
    domain = {r.name: r for r in _plan().data}["domain"]
    assert domain.span_km == eutrophication._REACH_LENGTH_KM
