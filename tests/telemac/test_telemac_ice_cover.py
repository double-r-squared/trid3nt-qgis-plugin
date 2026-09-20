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
from trid3nt_server.workflows.runtime import Ref, data_rows, validate_plan
from trid3nt_server.workflows.telemac.authoring.atmosphere import ATMOSPHERE_FILENAME
from trid3nt_server.workflows.telemac.modules import fill
from trid3nt_server.workflows.telemac.modules.khione import (
    RESULT_FILENAME,
    STEERING_FILENAME,
)
from trid3nt_server.workflows.telemac.templates.ice_cover import ice_cover as template

#: The week the deck states as DURATION, as the hourly record that has to span
#: it: the engine stops at an instant outside the table, so a week-long run is
#: only authorable against a week of observations.
_WEEK_HOURS = 7 * 24 + 1

#: The moment the run opens at, which is the weather table's t = 0: the record
#: has to hold an observation at or before it and one at or after the close.
_OPENS = "2024-01-11T00:00:00Z"
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


def _rows():
    return {row.name: row for row in data_rows(template.DATA)}


# -- what the run stands on ---------------------------------------------------- #

def test_every_slot_this_run_stands_on_reaches_the_wire_as_the_slot_it_is():
    """What the user hands in supersedes the match the template needs, so a pond
    outline, a stated depth, a stated opening temperature and a weather record
    the user already holds run this question with no fetch at all."""
    from trid3nt_server.tools import TOOL_REGISTRY

    wire = set(inspect.signature(TOOL_REGISTRY["telemac_ice_cover"].fn).parameters)
    assert {"domain", "bed", "discharge", "observe", "weather"} <= wire
    assert {"survey", "terrain", "surveyed_bed", "carrier",
            "water_temperature"}.isdisjoint(wire)


def test_the_run_writes_the_ice_modules_own_result_beside_the_hosts():
    """Every measure this question answers is read off KHIONE's own file, so the
    run has to publish it: a run that wrote only the host's would come back with
    the ice it made still in the box."""
    solve = next(step for step in _workflow().plan.steps if step.name == "solve")
    assert solve.kwargs["results"] == [template.STEERING.RESULTS_FILE,
                                       RESULT_FILENAME]


def test_the_water_opens_on_one_reading_ranked_from_where_it_is_read():
    """Which site the run opens on is the observation slot's choice, ranked
    against the point the series is read at. The row is LOAD-BEARING: the run
    has to lose that much heat before it makes any ice at all, and it names
    the published TEMPERATURE variable it observes - the unit KHIONE's own
    tracer carries, not a stated one."""
    row = _rows()["observe"]
    assert row.role == "observe"
    assert row.data_class == "water quality sample"
    assert row.observes == "TEMPERATURE"
    assert row.producer is None
    assert row.coercion["near"] == Ref("station")
    assert row.is_context
    assert "stated value stands" in row.context_sentence


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
    seated = {p.name for p in _workflow().params}
    assert {"event_time", "compute_class"} <= seated - own


def test_the_only_params_are_the_questions_own_inputs():
    """The place the cover is read, what counts as frozen there, and the water
    the domain is cut from with which kind of water that is. The days it happens
    under are the run's own moment; everything else is a keyword, a slot or a
    lever."""
    from trid3nt_server.workflows.runtime import param_rows

    assert {p.name for p in param_rows(template.PARAMS)} == {
        "seed", "body", "station", "cover_threshold", "mesh_resolution_m"}


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
    return fill(template.STEERING, **floor,
                params={"event_time": _OPENS}, produced={
        "settled": {"title": "DOMAIN", "time_step_s": 5.0,
                    "liquid_boundary_order": ["inflow", "outflow"],
                    "liquid_boundary_prescribes": ["flowrate", "elevation"],
                    "opening": "CONSTANT DEPTH", "depth_m": 1.4,
                    "level_m": 3.1, "inflow_q_m3s": 22.0,
                    "outflow_stage_m": 3.1},
        "station": _STATION,
        "observe": Observation(value=3.2, units="degC"),
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
    assert set(template.CAPTIONS) == {"DYNCOVC", "DYNCOVT", "discharge",
                                      "level", "observe"}


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
        ("DYNCOVC", "t_above"), ("COV_THT", "max"), ("DYNCOVC", "last")}


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
