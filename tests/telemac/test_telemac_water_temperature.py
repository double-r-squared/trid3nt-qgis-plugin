"""The water-temperature question: what it stands on, and what it states.

Offline: the domain, the bed, the flow and what the water opens at are slots, and
what is proved here is the deck the template states over them - that the carrier
declares its own TEMPERATURE tracer and the thermic process attaches to it rather
than appending a second one, that the atmospheric file is written from the fetched
station record, and that a pond outline with a stated depth and a stated opening
temperature runs this question with no fetch at all."""

from __future__ import annotations

import datetime as _dt
import inspect

import pytest

from trid3nt_server.inputs import geometry as geometry_reader
from trid3nt_server.inputs.observation import Observation
from trid3nt_server.workflows.runtime import validate_plan
from trid3nt_server.workflows.telemac.authoring.atmosphere import ATMOSPHERE_FILENAME
from trid3nt_server.workflows.telemac.modules import fill
from trid3nt_server.workflows.telemac.templates.water_temperature import (
    water_temperature as template,
)

#: The week the deck states as DURATION, as the hourly record that has to span
#: it: the engine stops at an instant outside the table, so a week-long run is
#: only authorable against a week of observations.
_WEEK_HOURS = 7 * 24 + 1
_STATION = {"lon": -122.72, "lat": 45.57, "name": "Temperature station"}

#: The baseline Params the domain slot, the module dictionary and the runtime
#: levers now carry. A template that declared one of them would be a second
#: statement of a value somebody else already describes. The opening temperature
#: is here too: it is the observation ROW's own supplied number, not a Param.
_NOT_DECLARED = {"location", "bbox", "river_geometry_uri", "reach_length_km",
                 "friction_coefficient", "friction_law", "output_interval_min",
                 "event_time", "compute_class", "initial_water_temp_c",
                 "sim_duration_s"}


def _workflow():
    return template.telemac_water_temperature.workflow


# -- what the run stands on ---------------------------------------------------- #

def test_every_slot_this_run_stands_on_reaches_the_wire_as_the_slot_it_is():
    """What the user hands in supersedes the producer the template preferred, so
    a pond outline, a stated depth and a stated opening temperature run this
    question with no fetch at all. The rows that are nobody's slot stay off it."""
    from trid3nt_server.tools import TOOL_REGISTRY

    wire = set(inspect.signature(
        TOOL_REGISTRY["telemac_water_temperature"].fn).parameters)
    assert {"domain", "bed", "carrier", "water_temperature"} <= wire
    assert {"survey", "terrain", "surveyed_bed", "weather"}.isdisjoint(wire)


def test_the_water_opens_on_one_reading_ranked_from_where_it_is_read():
    """The portal returns sample SITES and the engine reads ONE temperature.
    Which site the run opens on is the observation slot's choice, ranked against
    the point the series is read at, and the journal carries the site and the
    date because a sample is a moment. The row is LOAD-BEARING: the run spends
    days on water that entered carrying this value."""
    from trid3nt_server.workflows.runtime import Ref

    row = {r.name: r for r in _workflow().data}["water_temperature"]
    assert row.role == "observation"
    assert row.producer.runner == "fetch_usgs_water_quality"
    assert row.coercion["near"] == [Ref("station.lon"), Ref("station.lat")]
    assert row.coercion["to_units"] == "degC"
    assert row.coercion["opens"]
    assert not row.is_context


def test_the_carrier_is_one_reading_the_channel_step_can_open_on():
    """The step that opens the channel refuses a record nobody chose from, so
    the streamflow row is ranked from inside the domain before it gets there.
    Its ROLE is what the workflow reads: a discharge is what makes this an open
    channel, and the step that measures one is listed off that."""
    row = {r.name: r for r in _workflow().data}["carrier"]
    assert row.role == "discharge"
    assert row.coercion["field"] == "streamflow_cms"
    assert row.is_context


def test_the_bed_is_one_row_the_merge_derive_made_of_two_rows():
    rows = {row.name: row for row in _workflow().data}
    assert [name for name, row in rows.items() if row.role == "bed"] == ["bed"]
    assert rows["bed"].producer.runner == "derive_merge_rasters"
    assert rows["bed"].producer.kwargs["primary"].path == "surveyed_bed"
    assert rows["bed"].producer.kwargs["fallback"].path == "terrain"
    # The survey is CONTEXT: water with no federal navigation project has no
    # published sounding, the grid of nothing is nothing, and the terrain the
    # merge passes through is the whole bed rather than a refusal.
    assert rows["survey"].is_context
    assert "terrain surface stands" in rows["survey"].context_sentence


def test_the_domain_producer_hands_over_the_faces_the_run_is_prescribed_on():
    """The runs slot is what the roles are set from, and the two end transects
    the reach producer cut its polygon between are what fills it."""
    recipe = next(s for s in _workflow().plan.steps
                  if s.name == "mesh").kwargs["mesh"]
    roles = next(op for op in recipe["ops"] if op["op"] == "set_boundary_roles")
    assert roles["kwargs"]["runs"].path == "runs"
    bed = next(op for op in recipe["ops"] if op["op"] == "set_bed")
    assert bed["kwargs"]["source"].path == "bed"


def test_no_keyword_twin_and_no_domain_twin_is_declared_here():
    from trid3nt_server.workflows.runtime import param_rows

    own = {p.name for p in param_rows(template.PARAMS)}
    assert _NOT_DECLARED.isdisjoint(own)
    # The two levers the template does not declare are SEATED on it by the
    # runtime, so the card still carries them.
    seated = {p.name for p in _workflow().params}
    assert {"event_time", "compute_class"} <= seated - own


def test_the_granularity_row_is_restated_only_for_its_own_default():
    """The runtime's 14 m spends the compute budget on a planform a surface heat
    budget does not read, so this question states its own number and keeps the
    lever's bounds."""
    resolution = next(p for p in _workflow().params if p.name == "mesh_resolution_m")
    assert (resolution.default, resolution.bounds) == (20.0, (3.0, 5000.0))


# -- the deck the template states ---------------------------------------------- #

def _observation(hour: int):
    stamp = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(hours=hour)
    return {"type": "Feature",
            "properties": {"station": "SCOC1",
                           "utc_valid": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "tmpf": 68.0, "dwpf": 50.0, "relh": 51.0,
                           "sknt": 5.0, "drct": 270.0, "solar_rad": 400.0,
                           "precip_in": 0.0},
            "geometry": {"type": "Point", "coordinates": [-122.9, 45.6]}}


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
        "water_temperature": Observation(value=16.4, units="degC"),
        "weather": "s3://cache/raws.fgb"})


def test_the_carrier_declares_the_temperature_tracer_and_the_process_adopts_it(
        monkeypatch):
    sheet = _sheet(monkeypatch)
    (row,) = sheet.tracers
    # The carrier's own name and unit, not the row the process would append.
    assert (row.name, row.unit) == ("TEMPERATURE", "DEG")
    assert dict(sheet.resolved())["WATER QUALITY PROCESS"] == 11


def test_the_deck_reads_the_measured_temperature_the_water_opens_and_flows_in_at(
        monkeypatch):
    resolved = dict(_sheet(monkeypatch).resolved())
    assert resolved["INITIAL VALUES OF TRACERS"] == [16.4]
    # One value per tracer per liquid boundary, in the measured order.
    assert resolved["PRESCRIBED TRACERS VALUES"] == [16.4, 16.4]


def test_the_deck_is_solved_at_the_roughness_its_outflow_stage_is_derived_at(
        monkeypatch):
    resolved = dict(_sheet(monkeypatch).resolved())
    assert resolved["LAW OF BOTTOM FRICTION"] == template._FRICTION_LAW
    assert resolved["FRICTION COEFFICIENT"] == template._FRICTION_COEFFICIENT
    assert resolved["INITIAL DEPTH"] == 1.4


def test_the_clock_is_the_decks_own_keyword_and_the_weather_table_spans_it(
        monkeypatch):
    """DURATION is a keyword the module carries, so the week is stated on the
    deck rather than restated as a Param, and the atmospheric table is written
    for that same number - the engine stops at an instant outside it."""
    sheet = _sheet(monkeypatch)
    assert dict(sheet.resolved())["DURATION"] == 604800.0
    last = sheet.files[ATMOSPHERE_FILENAME].splitlines()[-1]
    assert float(last.split()[0]) >= 604800.0


def test_a_stated_duration_sizes_the_weather_record(monkeypatch):
    """The weather table is placed on the DURATION keyword, so a run that states
    a longer window is measured against THAT window: the engine stops at an
    instant outside the table, and the refusal names both numbers."""
    from trid3nt_server.workflows.telemac.errors import TelemacError

    with pytest.raises(TelemacError) as caught:
        _sheet(monkeypatch, DURATION=1209600.0)
    assert "168 h" in str(caught.value) and "336 h" in str(caught.value)


def test_the_deck_names_the_atmospheric_file_and_the_run_directory_holds_it(
        monkeypatch):
    sheet = _sheet(monkeypatch)
    assert dict(sheet.resolved())["ASCII ATMOSPHERIC DATA FILE"] == ATMOSPHERE_FILENAME
    header = sheet.files[ATMOSPHERE_FILENAME].splitlines()[1].split()
    # No cloud and no pressure column: the network this run is driven by does not
    # report them, and the engine reads its own constant for each.
    assert header == ["T", "TAIR", "PVAP", "WINDS", "WINDD", "RAY3", "RAINI"]


def test_the_heat_budget_states_none_of_the_engines_own_constants(monkeypatch):
    (body,) = _sheet(monkeypatch).coupled
    assert body["process"] == 11 and dict(body["slots"]) == {}


# -- what the question reads --------------------------------------------------- #

def test_the_question_places_a_series_and_publishes_no_field_of_its_own():
    assert {p.kind for p in template.OUTPUTS} == {"series"}
    assert all(p.style is None for p in template.OUTPUTS)
    assert set(template.CAPTIONS) == {"T1"}


def test_the_answer_measures_the_point_the_question_placed():
    at_point = {name for name, measure in template.ANSWER.items()
                if measure.primitive.kind == "series"}
    assert at_point == {"peak_temperature_c", "peak_temperature_time_s",
                        "final_temperature_c", "diurnal_range_c"}


def test_the_domain_wide_answers_take_what_any_body_of_water_offers():
    """A lake has no centerline, so the reach-long profile is gone: the spread
    between the warmest and the coolest water is a measure of the field itself."""
    fields = {name: measure for name, measure in template.ANSWER.items()
              if measure.primitive.kind == "field"}
    assert {(name, m.primitive.variable, m.stat) for name, m in fields.items()} == {
        ("temperature_spread_c", "T1", "spread"),
        ("mean_velocity_mps", "M", "mean")}
    assert all(m.primitive.along is None for m in fields.values())


def test_the_workflow_owns_the_stages_and_the_template_states_what_differs():
    wf = _workflow()
    validate_plan(wf.plan, wf.params, wf.data)
    steps = list(wf.plan.declared())
    assert [s.label for s in steps] == ["stated", "mesh", "channel", "station",
                                        "settled", "sheet", "solve", "outputs"]
    # The point the chart is anchored at is placed BEFORE the sheet is filled, so
    # the series reads a node of the mesh the run actually solved on - and the
    # observation row that ranks its sites against that point is produced when
    # the sheet reads it, which is after.
    named = [s.label for s in steps]
    assert named.index("station") < named.index("sheet")
    assert [s.label for s in steps if s.self_gating] == ["sheet"]
    assert steps[-2].consequential
