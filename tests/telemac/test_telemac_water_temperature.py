"""The water-temperature question: what it stands on, and what it states.

Offline: the domain, the bed, the flow and what the water opens at are slots, and
what is proved here is the deck the template states over them - that the deck
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
from trid3nt_server.workflows.runtime import Ref, data_rows, validate_plan
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
#: is here too: it is the observation ROW's own supplied number, not a Param;
#: and the two weather dates are twins of the run's own window, which opens at
#: event_time and closes on the deck's DURATION.
_NOT_DECLARED = {"location", "bbox", "river_geometry_uri", "reach_length_km",
                 "friction_coefficient", "friction_law", "output_interval_min",
                 "event_time", "compute_class", "initial_water_temp_c",
                 "sim_duration_s", "weather_start", "weather_end"}


def _workflow():
    return template.telemac_water_temperature.workflow


def _rows():
    return {row.name: row for row in data_rows(template.DATA)}


# -- what the run stands on ---------------------------------------------------- #

def test_every_slot_this_run_stands_on_reaches_the_wire_as_the_slot_it_is():
    """What the user hands in supersedes the match the template needs, so a pond
    outline, a stated depth, a stated opening temperature and a weather record
    the user already holds run this question with no fetch at all."""
    from trid3nt_server.tools import TOOL_REGISTRY

    wire = set(inspect.signature(
        TOOL_REGISTRY["telemac_water_temperature"].fn).parameters)
    assert {"domain", "bed", "discharge", "observe", "weather"} <= wire
    assert {"survey", "terrain", "surveyed_bed", "carrier",
            "water_temperature"}.isdisjoint(wire)


def test_the_water_opens_on_one_reading_ranked_from_where_it_is_read():
    """Which site the run opens on is the observation slot's choice, ranked
    against the point the series is read at. The row is LOAD-BEARING: the run
    spends days on water that entered carrying this value, and it names the
    published TEMPERATURE variable it observes - the unit its own tracer
    text carries, not a stated one."""
    row = _rows()["observe"]
    assert row.role == "observe"
    assert row.data_class == "water quality sample"
    assert row.observes == "TEMPERATURE"
    assert row.producer is None
    assert row.coercion["near"] == Ref("station")
    assert not row.is_context


def test_the_discharge_is_one_reading_the_channel_step_can_open_on():
    """The step that opens the channel refuses a record nobody chose from, so
    the streamflow row is ranked from inside the domain before it gets there.
    Its ROLE is what the workflow reads: a discharge is what makes this an open
    channel, and the step that measures one is listed off that."""
    row = _rows()["discharge"]
    assert row.role == "discharge"
    assert row.data_class == "discharge series"
    assert row.producer is None
    assert row.is_context
    assert "National Water Model" in row.context_sentence


def test_the_bed_is_one_need_row_the_match_composes():
    """One row, one class: the measurement where it measured, the terrain under
    the rest, composed by the match's own bed rule - never a producer or a merge
    stated on the template."""
    rows = _rows()
    assert [name for name, row in rows.items() if row.role == "bed"] == ["bed"]
    assert rows["bed"].data_class == "bathymetry"
    assert rows["bed"].producer is None


def test_the_boundary_roles_ride_on_the_domain_since_no_runs_row_exists():
    """There is no runs row: the boundary walk the match returned rides on the
    domain's own producer, and the mesh recipe reads that row directly."""
    from trid3nt_server.workflows.mesh.tool import recipe_from_plan_value
    from trid3nt_server.workflows.runtime import DataRef

    recipe = recipe_from_plan_value(
        next(s for s in _workflow().plan.steps if s.name == "mesh").kwargs["mesh"])
    runs = next(op for op in recipe.ops if op.fn == "set_boundary_roles")
    assert runs.kwargs == {"runs": DataRef("domain")}
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}
    assert "runs" not in _rows()


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
        "observe": Observation(value=16.4, units="degC"),
        "weather": "s3://cache/raws.fgb"})


def test_the_deck_declares_the_temperature_tracer_and_the_process_adopts_it(
        monkeypatch):
    sheet = _sheet(monkeypatch)
    (row,) = sheet.tracers
    # The deck's own name and unit, not the row the process would append.
    assert (row.name, row.unit) == ("TEMPERATURE", "DEGC")
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


def test_the_weather_is_asked_over_the_window_the_deck_closes():
    """No date Param twins the run's window: the record is asked for from the
    moment the run opens at to the instant the deck's own DURATION closes it,
    in the two params the matched source spells that window under."""
    from trid3nt_server.tools.search.match import (
        Need, base_ask, match, sources_with_coverage)
    from trid3nt_server.workflows.runtime.interpreter import _closes

    window_s = _workflow().run_window_s({})
    assert window_s == 604800.0
    opens = "2026-09-10T00:00:00Z"
    need = Need(slot="weather", data_class="weather forcing", lon=-122.72,
                lat=45.57, opens=opens, until=_closes(opens, window_s))
    picked = match(need, sources_with_coverage())
    ask = base_ask(picked.picked, "weather", None, need.lon, need.lat,
                   need.opens, need.until)
    assert (ask["start_time"], ask["end_time"]) == ("2026-09-10", "2026-09-17")


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
    # WHAT THIS RUN'S READERS READ and nothing beside it: the thermal budget's
    # air, vapour, wind and shortwave. No cloud and no pressure column - the
    # network this run is driven by reports neither, and the engine reads its
    # own constant for each; no dew point, which only a coupled ice module
    # reads; no wind direction and no rain, which only the host's own terms
    # read and this deck arms neither of.
    assert header == ["T", "TAIR", "PVAP", "WINDS", "RAY3"]


def test_the_heat_budget_states_none_of_the_engines_own_constants(monkeypatch):
    (body,) = _sheet(monkeypatch).coupled
    assert body["process"] == 11 and dict(body["slots"]) == {}


# -- what the question reads --------------------------------------------------- #

def test_the_question_places_a_series_and_captions_every_data_row_that_states_one():
    assert {p.kind for p in template.OUTPUTS} == {"series"}
    assert all(p.style is None for p in template.OUTPUTS)
    assert set(template.CAPTIONS) == {"T1", "discharge", "level", "observe"}


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
