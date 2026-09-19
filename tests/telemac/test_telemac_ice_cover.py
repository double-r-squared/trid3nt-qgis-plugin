"""The ice-cover question: what it stands on, and what it states.

Offline: the domain, the bed, the flow and what the water opens at are slots, and
what is proved here is the deck the template states over them - the five ice
keywords it has an opinion about and no constant beside them, the atmospheric
file written in the columns the ice module reads and no solar column at all, the
water and the open water above it opening at a measured temperature, and the
threshold crossing the answer is measured by."""

from __future__ import annotations

import datetime as _dt
import inspect

import pytest

from trid3nt_server.inputs import geometry as geometry_reader
from trid3nt_server.inputs.observation import Observation
from trid3nt_server.workflows.runtime import validate_plan
from trid3nt_server.workflows.telemac.authoring.atmosphere import ATMOSPHERE_FILENAME
from trid3nt_server.workflows.telemac.modules import fill
from trid3nt_server.workflows.telemac.modules.khione import STEERING_FILENAME
from trid3nt_server.workflows.telemac.templates.ice_cover import ice_cover as template

#: The week the deck states as DURATION, as the hourly record that has to span
#: it: the engine stops at an instant outside the table, so a week-long run is
#: only authorable against a week of observations.
_WEEK_HOURS = 7 * 24 + 1
_STATION = {"lon": -122.67, "lat": 45.52, "name": "Ice station"}

#: The baseline Params the domain slot, the module dictionaries and the runtime
#: levers already carry. A template that declared one of them would be a second
#: statement of a value somebody else already describes.
_NOT_DECLARED = {"location", "bbox", "river_geometry_uri", "reach_length_km",
                 "friction_coefficient", "friction_law", "output_interval_min",
                 "event_time", "compute_class", "initial_water_temp_c",
                 "sim_duration_s", "dynamic_ice_cover", "border_ice_cover"}


def _workflow():
    return template.telemac_ice_cover.workflow


# -- what the run stands on ---------------------------------------------------- #

def test_every_slot_this_run_stands_on_reaches_the_wire_as_the_slot_it_is():
    """What the user hands in supersedes the producer the template preferred, so
    a pond outline, a stated depth and a stated opening temperature run this
    question with no fetch at all. The rows that are nobody's slot stay off it."""
    from trid3nt_server.tools import TOOL_REGISTRY

    wire = set(inspect.signature(TOOL_REGISTRY["telemac_ice_cover"].fn).parameters)
    assert {"domain", "bed", "carrier", "water_temperature"} <= wire
    assert {"survey", "terrain", "surveyed_bed", "weather"}.isdisjoint(wire)


def test_the_weather_is_the_record_that_measures_what_the_ice_module_reads():
    """KHIONE takes the dew point and the sky cover out of the file and computes
    its own shortwave, so the airport network - which measures both - is the row
    rather than the fire-weather network the thermal budget needs a solar from."""
    row = {r.name: r for r in _workflow().data}["weather"]
    assert row.producer.runner == "fetch_asos_metar"


def test_the_water_opens_on_one_reading_taken_at_the_runs_own_moment():
    """A freeze-up asked about a past winter opens at what the water carried
    then: the window closes at the run's instant, the way the carrier's does."""
    from trid3nt_server.workflows.runtime import Ref

    rows = {r.name: r for r in _workflow().data}
    opening = rows["water_temperature"]
    assert opening.role == "observation"
    assert opening.producer.runner == "fetch_usgs_water_quality"
    assert opening.producer.kwargs["valid_time"].name == "event_time"
    assert opening.coercion["near"] == [Ref("station.lon"), Ref("station.lat")]
    assert opening.coercion["to_units"] == "degC"
    assert rows["carrier"].producer.kwargs["valid_time"].name == "event_time"


def test_the_bed_is_one_row_the_merge_derive_made_of_two_rows():
    rows = {row.name: row for row in _workflow().data}
    assert [name for name, row in rows.items() if row.role == "bed"] == ["bed"]
    assert rows["bed"].producer.runner == "derive_merge_rasters"
    assert rows["bed"].producer.kwargs["primary"].path == "surveyed_bed"
    assert rows["bed"].producer.kwargs["fallback"].path == "terrain"
    assert rows["survey"].is_context


def test_the_domain_producer_hands_over_the_faces_the_run_is_prescribed_on():
    """A river reach cuts with its two end transects; a lake is the rung below
    it on the same ladder and names no runs at all."""
    rows = {row.name: row for row in _workflow().data}
    assert rows["domain"].producer.runner == "fetch_river_reach"
    assert [rung.runner for rung in rows["domain"].producer.ladder_rungs] == [
        "fetch_nhd_waterbody_at_point"]
    recipe = next(s for s in _workflow().plan.steps
                  if s.name == "mesh").kwargs["mesh"]
    roles = next(op for op in recipe["ops"] if op["op"] == "set_boundary_roles")
    assert roles["kwargs"]["runs"].path == "runs"


def test_no_keyword_twin_and_no_domain_twin_is_declared_here():
    from trid3nt_server.workflows.runtime import param_rows

    own = {p.name for p in param_rows(template.PARAMS)}
    assert _NOT_DECLARED.isdisjoint(own)
    seated = {p.name for p in _workflow().params}
    assert {"event_time", "compute_class"} <= seated - own


def test_the_only_params_are_the_questions_own_inputs():
    """The place the cover is read, what counts as frozen there, the water the
    domain is cut from and the days of weather it happens under. Everything else
    is a keyword, a slot or a lever."""
    from trid3nt_server.workflows.runtime import param_rows

    assert {p.name for p in param_rows(template.PARAMS)} == {
        "seed", "station", "cover_threshold", "weather_start", "weather_end",
        "mesh_resolution_m"}


def test_the_granularity_row_is_restated_only_for_its_own_default():
    resolution = next(p for p in _workflow().params if p.name == "mesh_resolution_m")
    assert (resolution.default, resolution.bounds) == (20.0, (3.0, 5000.0))


# -- the deck the template states ---------------------------------------------- #

def _observation(hour: int):
    stamp = _dt.datetime(2024, 1, 11, tzinfo=_dt.timezone.utc) + _dt.timedelta(hours=hour)
    return {"type": "Feature",
            "properties": {"station": "PDX",
                           "valid": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "tmpf": 21.0, "dwpf": 12.0, "sknt": 9.0,
                           "drct": 80.0, "mslp": 1024.3, "skyc1": "OVC",
                           "p01i": 0.0},
            "geometry": {"type": "Point", "coordinates": [-122.6, 45.59]}}


def _sheet(monkeypatch, **floor):
    """The template's own steering body, filled with what its refs resolve to."""
    monkeypatch.setattr(
        geometry_reader, "read_geometry_doc",
        lambda layer: {"features": [_observation(hour)
                                    for hour in range(_WEEK_HOURS)]})
    return fill(template.STEERING, **floor, produced={
        "settled": {"title": "DOMAIN", "time_step_s": 5.0,
                    "liquid_boundary_order": ["inflow", "outflow"],
                    "liquid_boundary_prescribes": ["flowrate", "elevation"],
                    "opening": "CONSTANT DEPTH", "depth_m": 1.4,
                    "level_m": 3.1, "inflow_q_m3s": 22.0,
                    "outflow_stage_m": 3.1},
        "station": _STATION,
        "water_temperature": Observation(value=3.2, units="degC"),
        "weather": "s3://cache/asos.fgb"})


def test_the_ice_deck_states_the_five_keywords_this_question_has_an_opinion_on(
        monkeypatch):
    """Everything else the module carries - the budget itself, the frazil class
    count and its seeding, the velocity and temperature border ice forms under,
    the cover's own friction - stands at the engine's published default."""
    (body,) = _sheet(monkeypatch).coupled
    assert body["module"] == "khione"
    stated = dict(body["slots"])
    for name in ("GEOMETRY_FILE", "BOUNDARY_CONDITIONS_FILE", "RESULTS_FILE"):
        stated.pop(name)
    assert stated == {"ATMOSPHERE_WATER_EXCHANGE_MODEL": 1,
                      "DYNAMIC_ICE_COVER": True,
                      "MODEL_FOR_MASS_EXCHANGE_BETWEEN_FRAZIL_AND_ICE_COVER": 1,
                      "BORDER_ICE_COVER": True,
                      "GRAPHIC_PRINTOUT_PERIOD": 12000,
                      "LISTING_PRINTOUT_PERIOD": 500}


def test_the_ice_writes_on_the_hosts_own_cadence_so_one_clock_reads_both(
        monkeypatch):
    sheet = _sheet(monkeypatch)
    (body,) = sheet.coupled
    assert (body["slots"]["GRAPHIC_PRINTOUT_PERIOD"]
            == dict(sheet.resolved())["GRAPHIC PRINTOUT PERIOD"])


def test_the_module_declares_the_tracers_and_the_deck_declares_none(monkeypatch):
    """KHIONE ADDTRACERs what it carries, so a deck that numbered a tracer of
    its own would be counting the module's work twice."""
    sheet = _sheet(monkeypatch)
    resolved = dict(sheet.resolved())
    assert "NUMBER OF TRACERS" not in resolved
    assert "NAMES OF TRACERS" not in resolved
    assert [row.name for row in sheet.tracers] == [
        "TEMPERATURE", "FRAZIL", "ICE COVER FRAC.", "ICE COVER THICK."]


def test_the_water_and_the_water_above_it_open_at_the_measured_temperature(
        monkeypatch):
    """The domain opens at the sample the portal published and the water coming
    in is the OPEN water upstream: that temperature and no ice at all, one value
    per appended tracer per liquid boundary."""
    resolved = dict(_sheet(monkeypatch).resolved())
    assert resolved["INITIAL VALUES OF TRACERS"] == [3.2]
    assert resolved["PRESCRIBED TRACERS VALUES"] == [3.2, 0.0, 0.0, 0.0] * 2


def test_the_deck_is_solved_at_the_roughness_its_outflow_stage_is_derived_at(
        monkeypatch):
    resolved = dict(_sheet(monkeypatch).resolved())
    assert resolved["LAW OF BOTTOM FRICTION"] == template._FRICTION_LAW
    assert resolved["FRICTION COEFFICIENT"] == template._FRICTION_COEFFICIENT
    assert resolved["INITIAL DEPTH"] == 1.4


def test_the_clock_is_the_decks_own_keyword_and_the_weather_table_spans_it(
        monkeypatch):
    sheet = _sheet(monkeypatch)
    assert dict(sheet.resolved())["DURATION"] == 604800.0
    last = sheet.files[ATMOSPHERE_FILENAME].splitlines()[-1]
    assert float(last.split()[0]) >= 604800.0


def test_a_stated_duration_sizes_the_weather_record(monkeypatch):
    from trid3nt_server.workflows.telemac.errors import TelemacError

    with pytest.raises(TelemacError) as caught:
        _sheet(monkeypatch, DURATION=1209600.0)
    assert "168 h" in str(caught.value) and "336 h" in str(caught.value)


def test_the_weather_file_carries_what_the_ice_reads_and_no_solar_column(
        monkeypatch):
    """The module computes its own shortwave from the cloud and the local
    longitude, and the cloud is written in the TENTHS it reads rather than the
    octas the observer coded."""
    sheet = _sheet(monkeypatch)
    assert dict(sheet.resolved())["ASCII ATMOSPHERIC DATA FILE"] == (
        ATMOSPHERE_FILENAME)
    lines = sheet.files[ATMOSPHERE_FILENAME].splitlines()
    assert lines[1].split() == ["T", "TAIR", "TDEW", "WINDS", "CLDC", "RAINI"]
    assert lines[2].split() == ["s", "degC", "degC", "m/s", "tenth", "mm/h"]
    assert float(lines[3].split()[4]) == 10.0
    assert STEERING_FILENAME in sheet.files


# -- what the question reads --------------------------------------------------- #

def test_the_question_places_the_two_series_the_cover_is_read_as():
    assert {p.kind for p in template.OUTPUTS} == {"series"}
    assert all(p.style is None for p in template.OUTPUTS)
    assert {p.module for p in template.OUTPUTS} == {"khione"}
    assert set(template.CAPTIONS) == {"DYNCOVC", "DYNCOVT"}


def test_the_answer_is_measured_by_the_threshold_the_ask_states():
    """WHEN the water froze is a crossing of the whole series rather than any
    statistic of it, so the threshold rides the read and is a declared param."""
    crossings = {name: m for name, m in template.ANSWER.items()
                 if m.stat == "t_above"}
    assert set(crossings) == {"freeze_time_s", "domain_freeze_time_s"}
    assert all(m.primitive.above.name == "cover_threshold"
               for m in crossings.values())
    # The point the ask placed, and the whole domain beside it: the unplaced
    # read is the domain maximum at each instant, so it answers "anywhere".
    assert crossings["freeze_time_s"].primitive.at is not None
    assert crossings["domain_freeze_time_s"].primitive.at is None


def test_every_answer_is_read_off_the_ice_modules_own_result():
    reads = [m.primitive for m in template.ANSWER.values()
             if m.primitive.kind == "series"]
    assert {p.module for p in reads} == {"khione"}
    assert {(p.variable, m.stat) for p, m in
            zip(reads, [m for m in template.ANSWER.values()
                        if m.primitive.kind == "series"])} == {
        ("DYNCOVC", "t_above"), ("DYNCOVT", "max"), ("DYNCOVC", "last")}


def test_the_workflow_owns_the_stages_and_the_template_states_what_differs():
    wf = _workflow()
    validate_plan(wf.plan, wf.params, wf.data)
    steps = list(wf.plan.declared())
    assert [s.label for s in steps] == ["stated", "mesh", "channel", "station",
                                        "settled", "sheet", "solve", "outputs"]
    named = [s.label for s in steps]
    assert named.index("station") < named.index("sheet")
    assert [s.label for s in steps if s.self_gating] == ["sheet"]
    assert steps[-2].consequential
