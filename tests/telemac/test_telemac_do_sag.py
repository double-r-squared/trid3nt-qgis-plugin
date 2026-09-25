"""WAQTEL O2 dissolved-oxygen sag: offline V and V plus the template's own tests.

No solve, no network. A live solve's O2 profile is a committed fixture, re-checked
here against the Streeter-Phelps closed form deterministically, so a regression in
the O2 machinery is caught without re-solving. Beside it: the engine-neutral slots
the converted template stands on, the stages the workflow builds from them, and
the deck those stages fill."""
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.helpers.oxygen_sag import (
    critical_point,
    do_profile,
)
from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.workflow import stated
from trid3nt_server.workflows.telemac.modules.outputs import Line, Profile
from trid3nt_server.workflows.telemac.templates.do_sag.streeter_phelps import overlay

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "telemac_o2_sp_idealized_profile.json"

#: The Willamette at Portland (USGS 14211720) - the canary reach this question's
#: domain producer is walked from.
_PORTLAND = (-122.6735, 45.5175)


# --- COMMITTED live V&V: WAQTEL O2 solve vs Streeter-Phelps ------------------ #
def test_waqtel_o2_reproduces_streeter_phelps():
    """The landed worker's WAQTEL O2 solve (committed profile) matches the S-P
    closed form to well under 0.05 mg/L at the sag minimum - the machinery V&V."""
    d = json.loads(_FIXTURE.read_text())
    p = d["params"]
    x = np.asarray(d["x"]); o2 = np.asarray(d["o2"])
    U = float(np.mean(d["U"]))
    D0 = p["Cs"] - p["up_do"]
    sp, _ = do_profile(list(x), U, p["Cs"], p["L0"], D0, p["k1_day"], p["k2_day"])
    sp = np.asarray(sp)
    crit = critical_point(U, p["Cs"], p["L0"], D0, p["k1_day"], p["k2_day"])
    i = int(o2.argmin())
    # sag minimum matches the analytic sag minimum
    assert abs(o2[i] - crit["min_do_mgl"]) < 0.05
    # sag LOCATION matches within one mesh cell-ish (< 1% of the reach)
    assert abs(x[i] - crit["xc_m"]) < 0.01 * p["L"]
    # whole-profile agreement (numerical diffusion only)
    assert np.sqrt(np.mean((o2 - sp) ** 2)) < 0.05
    # and the modeled sag violates the 5 mg/L standard (the permit answer)
    assert o2[i] < p["standard"]


# --- the declaration --------------------------------------------------------- #
def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac_do_sag"].fn.workflow


def _template():
    from trid3nt_server.workflows.telemac.templates.do_sag import do_sag

    return do_sag


def _steps():
    return list(_workflow().plan.declared())


def _resolve(**supplied):
    from trid3nt_server.workflows.runtime import resolve_params

    return asyncio.run(resolve_params(_workflow().params, dict(supplied)))


def test_declared_params_and_plan_validate():
    from trid3nt_server.workflows.runtime import validate_plan

    wf = _workflow()
    validate_plan(wf.plan, wf.params, wf.data)
    assert [p.name for p in wf.params if p.name not in LEVER_NAMES] == [
        "outfall_coords", "do_standard_mgl"]
    assert _resolve().value_of("do_standard_mgl") == 5.0


def test_the_params_are_the_questions_own_plus_the_runtimes_levers():
    """No keyword twin, no domain twin, no lever restated: the dictionary
    describes the clock, the roughness, the cadence and the O2 kinetics, the
    slots describe the water, and the runtime declares the granularity, the
    moment and the sizing class."""
    declared = {p.name for p in _workflow().params}
    assert not declared & {"friction_law", "friction_coefficient",
                           "output_interval_min"}
    assert not declared & {"location", "bbox", "river_geometry_uri",
                           "reach_length_km"}
    # The keywords this deck has an opinion about: stated in STEERING, set by
    # the user under the dictionary's own name, and never a second time here.
    assert not declared & {"sim_duration_s", "water_temp_c", "k1_per_day",
                           "k2_per_day", "do_saturation_mgl", "upstream_do_mgl"}
    # Every lever is on the sheet; the ones this question does not state for
    # itself are seated at the end, in the order the runtime declares them.
    from trid3nt_server.workflows.runtime import param_rows

    own = {p.name for p in param_rows(_template().PARAMS)}
    seated = [p.name for p in _workflow().params]
    assert set(LEVER_NAMES) <= set(seated)
    appended = [name for name in LEVER_NAMES if name not in own]
    assert seated[-len(appended):] == appended
    # The roughness the outflow stage is derived at is the roughness the deck is
    # written at - one number, stated once on the body. The clock is the deck's
    # too: the settle reads the DURATION it states.
    steering = _template().STEERING
    assert steering.LAW_OF_BOTTOM_FRICTION == 3
    assert steering.FRICTION_COEFFICIENT == 33.0
    assert steering.ASSERTED["DURATION"] == 172800.0
    settled = next(s for s in _steps() if s.name == "settled")
    assert settled.kwargs["duration_s"] == Ref("stated.DURATION")
    assert stated(steering=steering, keywords={})["DURATION"] == 172800.0


def test_the_data_rows_are_the_engine_neutral_slots_this_run_stands_on():
    """One domain needing a hydrography class, one bed the match composes, and
    the line the domain's own producer measures beside it - no ladder, no
    fetcher named on a row."""
    from trid3nt_server.workflows.runtime import data_rows
    from trid3nt_server.workflows.runtime.data import BED, DOMAIN, LINE

    rows = data_rows(_template().DATA)
    assert [d.name for d in rows] == ["domain", "line", "bed", "discharge",
                                      "level"]
    by_name = {d.name: d for d in rows}
    assert by_name["domain"].role == DOMAIN
    assert by_name["domain"].data_class == "hydrography"
    assert by_name["domain"].producer is None
    assert by_name["domain"].span_km == 12.0
    assert by_name["line"].role == LINE
    assert by_name["bed"].role == BED
    assert by_name["bed"].data_class == "bathymetry"
    assert by_name["bed"].producer is None
    # Both slots reach the wire: what the user supplies supersedes the match.
    assert by_name["domain"].fills_from_user and by_name["bed"].fills_from_user


def test_the_carrier_is_one_reading_ranked_against_the_domain():
    """The dilution the whole sag rests on is a NUMBER, so the row is the
    discharge slot: whichever record the match ranks nearest the water, with a
    stated number standing over any record."""
    from trid3nt_server.workflows.runtime import data_rows
    from trid3nt_server.workflows.runtime.data import DISCHARGE

    carrier = {d.name: d for d in data_rows(_template().DATA)}["discharge"]
    assert carrier.role == DISCHARGE
    assert carrier.data_class == "discharge series"
    assert carrier.producer is None
    assert carrier.is_context
    assert "National Water Model" in carrier.context_sentence


def test_the_outfall_seeds_the_domain_match():
    """The outfall names which stretch to model, and the wire carries no second
    spelling of the same point."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime import Ref, data_rows

    domain = data_rows(_template().DATA)[0]
    assert domain.coercion["near"] == Ref("outfall_coords")
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_do_sag"].fn).parameters)
    assert "outfall_coords" in wire
    assert {"domain", "bed"} <= wire
    assert not {"location", "bbox", "river_geometry_uri", "reach_length_km",
                "outfall_lat", "outfall_lon"} & wire


def test_the_workflow_owns_the_stages_and_the_template_states_what_differs():
    steps = _steps()
    assert [s.label for s in steps] == ["stated", "mesh", "mesh_files", "channel", "outfall",
                                        "settled", "sheet", "solve", "outputs"]
    # The review is the door's VIEW of the sheet it just filled, so the run is
    # held on the fill itself rather than in front of a step that has not run.
    assert [s.label for s in steps if s.self_gating] == ["sheet"]
    assert steps[-2].consequential
    channel = steps[3]
    assert channel.runner.endswith("opening.open_channel")
    assert channel.kwargs["friction_law"] == Ref("stated.LAW_OF_BOTTOM_FRICTION")
    assert channel.kwargs["friction_coefficient"] == Ref(
        "stated.FRICTION_COEFFICIENT")
    # An unplaced outfall sits along the domain's OWN centerline companion, so
    # the step is handed the domain rather than a line of its own.
    outfall = steps[4]
    assert outfall.runner.endswith("release_point.settle_release")
    assert set(outfall.kwargs) == {"point", "mesh", "domain", "fraction", "label"}


def test_the_mesh_is_built_over_the_domain_slot_at_the_runtimes_own_lever():
    from trid3nt_server.workflows.mesh.tool import recipe_from_plan_value
    from trid3nt_server.workflows.runtime import DataRef

    recipe = recipe_from_plan_value(
        next(s for s in _steps() if s.name == "mesh").kwargs["mesh"])
    assert recipe.mesher == "om2d" and recipe.kind == "unstructured_tri"
    assert recipe.extent == DataRef("domain")
    assert recipe.resolution_m.name == "mesh_resolution_m"
    # The rim is sized FIRST: no sizing function measures the domain's own edge.
    assert recipe.ops[0].fn == "set_rim_size"
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}
    runs = next(op for op in recipe.ops if op.fn == "set_boundary_roles")
    # No runs row: the boundary runs ride on the domain's own producer.
    assert runs.kwargs == {"runs": DataRef("domain")}


def test_the_settle_step_reads_the_files_the_deck_itself_names():
    settle = next(s for s in _steps() if s.name == "settled")
    assert settle.runner.endswith("opening.open_water")
    assert settle.kwargs["geometry"] == "domain.slf"
    assert settle.kwargs["boundary"] == "domain.cli"
    assert settle.kwargs["result"] == "r2d_domain.slf"
    # A coupled run drives the module's own launcher whole, so it is asked for
    # the carrier's result and nothing else.
    assert list(next(s for s in _steps() if s.name == "solve").kwargs[
        "results"]) == ["r2d_domain.slf"]


def test_no_step_names_a_template_module_as_a_tool():
    """The reach chain is dissolved: nothing this template runs is reached by
    module path into the templates tree."""
    assert not [s.runner for s in _steps() if ".templates." in s.runner]


def test_the_ex_release_params_are_gone_from_the_declared_wire():
    """The discharge and its concentrations were only ever a twin inside the
    release composite; they leave with it, off both the declared params and the
    wire the model is offered - a caller naming the old name gets no such slot."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    declared = {p.name for p in _workflow().params}
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_do_sag"].fn).parameters)
    gone = {"effluent_q_m3s", "effluent_do_mgl", "effluent_bod_mgl"}
    assert not (declared & gone) and not (wire & gone)


def test_a_keyword_outside_the_modules_own_bounds_refuses_by_its_name():
    """The clock left the Params and its plausibility range went WITH it, onto
    the module's keyword table: a user setting DURATION by name is bounded
    there, and the refusal names the keyword rather than a param."""
    from trid3nt_server.workflows.telemac.modules.sheet import SlotRefused

    with pytest.raises(SlotRefused, match="DURATION is taken between"):
        _filled(keywords={"DURATION": 1.0})
    assert dict(_filled(keywords={"DURATION": 600.0}).resolved())["DURATION"] \
        == pytest.approx(600.0)


# --- the outfall: absent DERIVES, malformed REFUSES -------------------------- #
@pytest.mark.parametrize("bad", [[1.0], [1.0, 2.0, 3.0], {"lon": 1},
                                 [200.0, 10.0], ["a", "b"]])
@pytest.mark.asyncio
async def test_malformed_outfall_coords_refuse_they_never_fall_back(bad):
    """A garbage discharge location must not silently become the domain seed.

    A bare word is not garbage: it is a place name the geocoder is asked about."""
    out = await _template().telemac_do_sag(outfall_coords=bad)
    assert isinstance(out, dict) and out["error_code"] == "TELEMAC_PARAMS_INVALID"
    assert "outfall_coords" in out["error_message"]


def test_an_absent_outfall_leaves_a_derived_provenance_row():
    """The user has to see what the sag distance is measured FROM."""
    from trid3nt_server.workflows.runtime import provenance_entries

    row = next(r for r in provenance_entries(_resolve(), _workflow().params)
               if r.param == "outfall_coords")
    assert row.basis == "derived"
    assert "centerline" in (row.note or "")


def test_a_supplied_outfall_is_carried_as_a_user_row():
    from trid3nt_server.inputs import Point
    from trid3nt_server.workflows.runtime import provenance_entries

    supplied, err = asyncio.run(
        _workflow()._normalize({"outfall_coords": ["-122.6735", "45.5175"]}))
    assert err is None and supplied["outfall_coords"] == Point(*_PORTLAND)
    row = next(r for r in provenance_entries(_resolve(**supplied),
                                             _workflow().params)
               if r.param == "outfall_coords")
    assert row.basis == "user"


# --- the gate-mode lever reaches the resolved-input review ------------------- #
def test_the_door_declares_the_run_mode_read_for_the_sheet_review():
    """input_mode is the gate lever, not a Param: without this the user_gated
    review of the filled sheet is silently lost."""
    from trid3nt_server.workflows.runtime import RunMode

    review = next(s for s in _steps() if s.name == "sheet")
    assert review.kwargs["input_mode"] is RunMode
    assert review.self_gating is True    # so no gate may be declared in front


# --- the outputs list: the oxygen profile along what the domain offers -------- #
def test_the_outputs_list_charts_the_oxygen_along_the_domains_centerline():
    """The oxygen field is the module's to publish; what this template lists is
    the read the user gives a line - and the line is the CENTERLINE COMPANION the
    domain's own producer measured beside its polygon."""
    from trid3nt_server.workflows.runtime import Ref

    do_sag = _template()
    assert [(p.kind, p.variable, p.t, p.publish) for p in do_sag.OUTPUTS] == [
        ("profile", "T2", -1, "chart")]
    assert callable(do_sag.OUTPUTS[0].reference)
    assert do_sag.OUTPUTS[0].along == Ref("line")
    assert do_sag.CAPTIONS == {"T2": "dissolved oxygen", "discharge": "a streamflow",
                               "level": "a water level"}
    # the standard the sag is judged by is a LINE on the chart that plots the
    # oxygen, drawn at the param the sheet declares
    from trid3nt_server.workflows.telemac.modules.outputs import Line, Profile

    drawn = do_sag.OUTPUTS[0].reference(
        Profile(name="DO", units="mg/L", distance_m=[0.0, 100.0],
                values=[8.0, 6.0], along="downstream distance"),
        {}, {"do_standard_mgl": 5.0})
    assert drawn == [Line(label="5 mg/L standard", x=[0.0, 100.0],
                          values=[5.0, 5.0])]


# --- the deck those stages fill ---------------------------------------------- #
_SETTLED = {"title": "willamette DOMAIN", "time_step_s": 1.0,
            "until_s": 3700.0,
            "liquid_boundary_order": ["inflow", "outflow"],
            "liquid_boundary_prescribes": ["flowrate", "elevation"],
            "opening": "CONSTANT DEPTH", "depth_m": 1.2, "level_m": 1.0,
            "inflow_q_m3s": 2.0, "outflow_stage_m": 1.0}


def _filled(*, keywords=None, **supplied):
    from trid3nt_server.workflows.telemac.workflow import fill_sheet

    resolved = _resolve(**supplied)
    return asyncio.run(fill_sheet(
        steering=_template().STEERING,
        produced={"settled": _SETTLED,
                  "outfall": {"at": [0.0, 0.0], "name": None}},
        params={row.name: resolved.value_of(row.name)
                for row in _workflow().params},
        workflow="telemac_do_sag", title="",
        keywords=dict(keywords or {}), input_mode="auto"))


def test_the_deck_opens_at_the_derived_depth_and_the_declared_oxygen():
    """The run opens at the normal depth the OPEN CHANNEL derived, and the water
    the reach carries in is CLEAN and saturated - one number reaching both the
    initial state and every liquid boundary."""
    sheet = _filled()
    filled = dict(sheet.resolved())
    assert filled["INITIAL DEPTH"] == pytest.approx(1.2)
    assert filled["INITIAL VALUES OF TRACERS"] == [0.0, 9.022, 0.0, 0.0]
    assert filled["PRESCRIBED TRACERS VALUES"][1] == pytest.approx(9.022)
    assert filled["PRESCRIBED FLOWRATES"] == [pytest.approx(2.0), 0.0]
    assert filled["PRESCRIBED ELEVATIONS"] == [0.0, pytest.approx(1.0)]
    assert filled["LAW OF BOTTOM FRICTION"] == 3
    assert filled["FRICTION COEFFICIENT"] == pytest.approx(33.0)


def test_the_outfall_writes_the_four_source_arrays_and_the_sources_file():
    """The settled point reaches the sheet by position, the deck's own fixed
    discharge and tracer values reach it by name, and the SOURCES FILE holds the
    series - the same numbers the ``releases`` composite wrote for one source."""
    sheet = _filled()
    filled = dict(sheet.resolved())
    assert filled["ABSCISSAE OF SOURCES"] == [0.0]
    assert filled["ORDINATES OF SOURCES"] == [0.0]
    assert filled["WATER DISCHARGE OF SOURCES"] == [pytest.approx(1.0)]
    assert filled["VALUES OF THE TRACERS AT THE SOURCES"] == [
        0.0, pytest.approx(2.0), pytest.approx(250.0), 0.0]
    name = filled["SOURCES FILE"]
    assert name in sheet.files
    series = sheet.files[name]
    assert series.splitlines()[0] == "#"
    assert "TR(1,4)" in series.splitlines()[1]
    row = series.splitlines()[3].split()
    assert row == ["0.000", "1", "0", "2", "250", "0"]


def test_the_o2_process_is_the_coupled_bodys_own_sheet():
    """The rates and the saturation the template declared, and a CONSTANT
    reaeration formula - the one the closed form holds under."""
    sheet = _filled()
    o2 = dict(sheet.files["t2d_river.waqtel"]["slots"])
    assert (o2["CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1"],
            o2["K2_REAERATION_COEFFICIENT"], o2["FORMULA_FOR_COMPUTING_K2"],
            o2["O2_SATURATION_DENSITY_OF_WATER__CS_"]) == (
        0.3, 0.9, 0, pytest.approx(9.022, abs=1e-3))
    # Nitrification, benthic demand and photosynthesis have no term in the
    # closed form the answer is graded against, so this run zeroes all three.
    assert (o2["CONSTANT_OF_NITRIFICATION_KINETIC_K4"], o2["BENTHIC_DEMAND"],
            o2["PHOTOSYNTHESIS_P"], o2["VEGETAL_RESPIRATION_R"]) == (0.0,) * 4


# --- the analytical overlay: anchored, and honest about absence ------------- #
def _profile(x, values, *, velocity=0.5, units="mg/L") -> Profile:
    return Profile(name="DISSOLVED O2", units=units, distance_m=np.asarray(x),
                   values=np.asarray(values), along="downstream distance",
                   measures={"min": min(values), "velocity_mps": velocity})


def _load(x, values) -> dict:
    from trid3nt_server.workflows.telemac.modules import field
    from trid3nt_server.workflows.telemac.modules.outputs import profile

    return {profile("T3", along="line"): _profile(x, values),
            field("T2"): None}


_PARAMS = {"do_standard_mgl": 5.0}
#: The reference is closed over the kinetics the DECK states; these are a
#: reading's worth, not the deck's.
_REFERENCE = overlay(saturation_mgl=9.0, k1_per_day=2.0, k2_per_day=6.0)


def test_the_overlay_is_anchored_at_the_modeled_mix_point():
    """The closed form starts where the solve says the load entered - the CBOD
    peak - not at the top of whatever stretch happened to be wet; the standard
    and the load ride beside it under their own labels."""
    xs = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
    bod = [0.0, 0.0, 20.0, 18.0, 16.0, 14.0]
    do = [9.0, 9.0, 8.5, 8.2, 8.0, 7.9]
    lines = _REFERENCE(_profile(xs, do), _load(xs, bod), _PARAMS)
    assert [line.label for line in lines] == [
        "5 mg/L standard", "organic load", "Streeter-Phelps closed form"]
    closed = lines[2]
    assert list(closed.x) == xs[2:] and len(closed.values) == 4
    assert closed.values[0] == pytest.approx(do[2])   # anchored ON the modeled state
    assert lines[0].values == [5.0, 5.0] and list(lines[1].values) == bod
    assert all(isinstance(line, Line) for line in lines)


@pytest.mark.parametrize("velocity,bod", [
    (0.0, [0.0, 0.0, 20.0, 18.0, 16.0, 14.0]),
    (0.5, [0.0] * 6),
])
def test_the_overlay_draws_no_closed_form_it_cannot_anchor(velocity, bod):
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    lines = _REFERENCE(_profile(xs, [9.0] * 6, velocity=velocity),
                       _load(xs, bod), _PARAMS)
    assert "Streeter-Phelps closed form" not in [line.label for line in lines]


def test_the_overlay_reproduces_the_closed_form_it_is_graded_against():
    """Deterministic grading: fed a profile that IS the closed form, the overlay
    returns that same profile - so a stated deviation is the solve's, not the
    overlay's."""
    xs = [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0]
    do_exact, _ = do_profile(xs, 0.4, 9.0, 20.0, 0.5, 2.0, 6.0)
    bod = [20.0] + [0.0] * 5          # the mix point is bin 0
    lines = _REFERENCE(_profile(xs, do_exact, velocity=0.4), _load(xs, bod),
                       _PARAMS)
    assert lines[-1].values == pytest.approx(do_exact, abs=1e-9)


def test_the_reference_is_drawn_at_the_kinetics_the_deck_states():
    """A closed form computed at rates the run was not solved at grades nothing,
    so the chart's reference reads the deck's own O2 keywords."""
    from trid3nt_server.workflows.telemac.templates.do_sag import do_sag

    xs = [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0]
    o2 = dict(_filled().files["t2d_river.waqtel"]["slots"])
    exact, _ = do_profile(
        xs, 0.4, o2["O2_SATURATION_DENSITY_OF_WATER__CS_"], 20.0, 0.5,
        o2["CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1"],
        o2["K2_REAERATION_COEFFICIENT"])
    lines = do_sag.OUTPUTS[0].reference(
        _profile(xs, exact, velocity=0.4), _load(xs, [20.0] + [0.0] * 5),
        _PARAMS)
    assert lines[-1].values == pytest.approx(exact, abs=1e-9)


def test_a_coupled_keyword_is_stated_by_its_own_body_name():
    """A WAQTEL constant has no user surface through the carrier's dictionary, so
    the floor names the body it belongs to and the value lands on that deck."""
    sheet = _filled(keywords={"waqtel: K2 REAERATION COEFFICIENT": 6.0})
    o2 = sheet.files["t2d_river.waqtel"]["slots"]
    assert o2["K2_REAERATION_COEFFICIENT"] == pytest.approx(6.0)
    assert dict(sheet.resolved()).get("K2 REAERATION COEFFICIENT") is None


def test_a_bare_coupled_keyword_only_one_body_spells_resolves_there():
    """Nothing on TELEMAC-2D spells the reaeration coefficient, so the bare name
    is unambiguous and reaches WAQTEL's deck."""
    sheet = _filled(keywords={"K2 REAERATION COEFFICIENT": 6.0})
    assert sheet.files["t2d_river.waqtel"]["slots"][
        "K2_REAERATION_COEFFICIENT"] == pytest.approx(6.0)


def test_a_name_two_bodies_spell_refuses_naming_both_qualified_spellings():
    """MASS-BALANCE is a keyword of the carrier AND of the coupled deck, and the
    two are read apart, so a bare name is not an answer."""
    from trid3nt_server.workflows.telemac.modules.module import SlotRefused

    with pytest.raises(SlotRefused) as caught:
        _filled(keywords={"MASS-BALANCE": True})
    said = str(caught.value)
    assert "telemac2d: MASS-BALANCE" in said
    assert "waqtel: MASS-BALANCE" in said


def test_a_value_the_coupled_dictionary_refuses_refuses_at_the_fill():
    """The coupled slot is checked against WAQTEL's own dictionary, so a wrong
    type is named here rather than by the Fortran."""
    from trid3nt_server.workflows.telemac.modules.module import SlotRefused

    with pytest.raises(SlotRefused) as caught:
        _filled(keywords={"waqtel: K2 REAERATION COEFFICIENT": "fast"})
    assert "K2 REAERATION COEFFICIENT" in str(caught.value)


def test_the_card_groups_the_coupled_deck_under_its_own_body():
    """One group per coupled deck, every row showing its body, its unit and the
    range it is taken inside."""
    from trid3nt_server.workflows.telemac.workflow import card_rows

    sheet = _filled(keywords={"waqtel: K2 REAERATION COEFFICIENT": 6.0})
    rows = {row.name: row for row in card_rows(sheet)}
    row = rows["waqtel.K2_REAERATION_COEFFICIENT"]
    assert row.value == pytest.approx(6.0)
    assert row.group.startswith("waqtel:")
    assert row.basis == "user"
    assert rows["waqtel.WATER_SALINITY"].basis == "derived"
