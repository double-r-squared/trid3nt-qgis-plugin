"""The reach-temperature question: what it reads from the world, and what it states.

Offline: the two records this template is driven by arrive as DATA rows, so what
is proved here is the deck the template states over them - that the carrier
declares its own TEMPERATURE tracer and the thermic process attaches to it rather
than appending a second one, that the atmospheric file is written from the
fetched station record, and that the reach opens at a MEASURED temperature or
refuses."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.inputs import geometry as geometry_reader
from trid3nt_server.workflows.telemac.authoring.atmosphere import ATMOSPHERE_FILENAME
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import fill
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.templates.river_temperature import (
    river_temperature as template,
)

_HOUR = 3600.0
_SEED = {"lon": -124.1, "lat": 40.48}


# -- what the reach opens at -------------------------------------------------- #

def _site(properties, lon=-124.2, lat=40.5):
    return {"type": "Feature", "properties": properties,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _opening(monkeypatch, rows, supplied=None):
    monkeypatch.setattr(reach, "_read_vector_features", lambda uri: rows)
    return asyncio.run(reach.resolve_water_temperature(
        sample="s3://cache/wqp.fgb", supplied=supplied, seed=_SEED))


def test_a_stated_opening_temperature_stands_and_reads_no_record(monkeypatch):
    def _refuse(_uri):
        raise AssertionError("a stated value reaches no record")

    monkeypatch.setattr(reach, "_read_vector_features", _refuse)
    out = asyncio.run(reach.resolve_water_temperature(
        sample="s3://cache/wqp.fgb", supplied=14.5, seed=_SEED))
    assert out["water_temp_c"] == 14.5 and out["site"] is None


def test_the_opening_temperature_is_a_sample_with_its_date_on_the_note(monkeypatch):
    out = _opening(monkeypatch, [_site(
        {"site_id": "USGS-11477000", "site_name": "EEL R A SCOTIA CA",
         "value": 16.4, "unit": "deg C", "result_date": "2026-08-28"})])
    assert out["water_temp_c"] == 16.4
    assert "2026-08-28" in out["note"] and "USGS-11477000" in out["note"]


def test_the_nearest_site_that_reports_a_value_is_the_one_taken(monkeypatch):
    out = _opening(monkeypatch, [
        _site({"site_id": "FAR", "value": 9.0, "unit": "deg C"}, lon=-124.9),
        _site({"site_id": "NEAR", "value": 16.4, "unit": "deg C"}, lon=-124.12),
        _site({"site_id": "NEAREST_BUT_EMPTY", "value": None}, lon=-124.1)])
    assert out["site"] == "NEAR"


def test_a_fahrenheit_row_is_converted_rather_than_read_as_celsius(monkeypatch):
    out = _opening(monkeypatch, [_site(
        {"site_id": "S1", "value": 68.0, "unit": "deg F",
         "result_date": "2026-08-28"})])
    assert out["water_temp_c"] == pytest.approx(20.0, abs=0.01)


def test_the_site_the_reach_opens_at_reaches_the_run_journal(monkeypatch):
    """The opening temperature is settled against a fetched record, so where it
    came from belongs on the record that outlives the session rather than on an
    answer row that can only say the value was derived."""
    from trid3nt_server.workflows.runtime import journal

    token = journal.bind_notes()
    try:
        _opening(monkeypatch, [_site(
            {"site_id": "USGS-11477000", "site_name": "EEL R A SCOTIA CA",
             "value": 16.4, "unit": "deg C", "result_date": "2026-08-28"})])
        notes = journal.drain_notes(token)
    except BaseException:
        journal.drain_notes(token)
        raise
    assert len(notes) == 1
    assert "USGS-11477000" in notes[0] and "2026-08-28" in notes[0]
    assert "km from the reach" in notes[0]


def test_the_answer_no_longer_promises_a_provenance_row_for_the_opening():
    """A Step-derived value has no resolved param row, so a declared provenance
    name for it reads "derived" and carries no note at all."""
    declared = {name for name, _ in
                template.telemac_river_temperature.workflow.answer_provenance}
    assert "initial_water_temp_c" not in declared
    assert "discharge_m3s" in declared, "the fetched carrier still rides the answer"


def test_no_measured_water_temperature_refuses_rather_than_opening_at_a_guess(
        monkeypatch):
    with pytest.raises(TelemacError, match="not measured anywhere near it"):
        _opening(monkeypatch, [_site({"site_id": "S1", "value": None})])


# -- the deck the template states --------------------------------------------- #

def _observation(hour: int):
    return {"type": "Feature",
            "properties": {"station": "SCOC1",
                           "utc_valid": f"2026-09-01T{hour:02d}:00:00Z",
                           "tmpf": 68.0, "dwpf": 50.0, "relh": 51.0,
                           "sknt": 5.0, "drct": 270.0, "solar_rad": 400.0,
                           "precip_in": 0.0},
            "geometry": {"type": "Point", "coordinates": [-124.2, 40.5]}}


def _sheet(monkeypatch):
    """The template's own steering body, filled with what its refs resolve to."""
    monkeypatch.setattr(
        geometry_reader, "read_geometry_doc",
        lambda layer: {"features": [_observation(hour) for hour in range(24)]})
    return fill(template.STEERING, produced={
        "settled": {"title": "REACH", "time_step_s": 5.0, "graphic_period": 120,
                    "depth_m": 1.4, "friction_law": 3,
                    "friction_coefficient": 33.0, "inflow_q_m3s": 22.0,
                    "outflow_stage_m": 3.1,
                    "liquid_boundary_order": ["inflow", "outflow"],
                    "liquid_boundary_prescribes": ["flowrate", "elevation"],
                    "seed_lon": _SEED["lon"], "seed_lat": _SEED["lat"]},
        "seed": _SEED,
        "opening": {"water_temp_c": 16.4},
        "weather": "s3://cache/raws.fgb"},
        params={"sim_duration_s": 20.0 * _HOUR})


def test_the_carrier_declares_the_temperature_tracer_and_the_process_adopts_it(
        monkeypatch):
    sheet = _sheet(monkeypatch)
    (row,) = sheet.tracers
    # The carrier's own name and unit, not the row the process would append.
    assert (row.name, row.unit) == ("TEMPERATURE", "DEG")
    assert dict(sheet.resolved())["WATER QUALITY PROCESS"] == 11


def test_the_reach_opens_and_flows_in_at_the_measured_temperature(monkeypatch):
    assert dict(_sheet(monkeypatch).resolved())["INITIAL VALUES OF TRACERS"] == [16.4]


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


def test_the_question_places_a_series_and_publishes_no_field_of_its_own():
    assert {p.kind for p in template.OUTPUTS} == {"series"}
    assert all(p.style is None for p in template.OUTPUTS)
    assert set(template.CAPTIONS) == {"T1"}


def test_the_answer_measures_the_point_the_question_placed():
    at_point = {name for name, measure in template.ANSWER.items()
                if getattr(measure.primitive, "kind", None) == "series"}
    assert at_point == {"peak_temperature_c", "peak_temperature_time_s",
                        "final_temperature_c", "diurnal_range_c"}


def test_the_series_is_read_at_a_point_settled_against_the_accepted_mesh():
    named = [getattr(step, "name", None)
             for step in template.telemac_river_temperature.workflow.plan.declared()]
    # The point the chart is anchored at is placed BEFORE the sheet is filled,
    # so the series reads a node of the mesh the run actually solved on, and the
    # temperature the reach opens at is measured before the deck states it.
    assert named.index("station") < named.index("sheet")
    assert named.index("opening") < named.index("sheet")
