"""Unit tests for the OBSERVATION typed input.

Covered: one value read off a sample layer, the nearest of several when the
source was not already narrowed, a row reporting nothing skipped, a Fahrenheit
row converted by name, an unconvertible pair refusing, a station series read at
its LAST sample, a distance the fetch already measured preferred over one
re-derived here, nothing that reports refusing, and the note a run says."""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.observation import (
    ObservationError,
    convert,
    note,
    observation,
)


def _site(site_id: str, lon: float, lat: float, value: object,
          unit: str = "deg C", **extra: object) -> dict:
    props = {"site_id": site_id, "site_name": f"{site_id} site", "value": value,
             "unit": unit, "result_date": "2026-03-04"}
    props.update(extra)
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props}


def _fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def test_one_value_off_a_narrowed_layer() -> None:
    found = observation(_fc(_site("A", -122.68, 45.51, 9.5)))
    assert found.value == pytest.approx(9.5)
    assert found.site_id == "A"
    assert found.sampled == "2026-03-04"


def test_the_nearest_of_several_when_the_source_was_not_narrowed() -> None:
    layer = _fc(_site("far", -123.2, 45.5, 3.0), _site("near", -122.68, 45.51, 9.5))
    assert observation(layer, near=[-122.67, 45.51]).site_id == "near"


def test_a_row_reporting_nothing_is_not_a_candidate() -> None:
    layer = _fc(_site("near", -122.68, 45.51, None), _site("mid", -122.9, 45.5, 4.0))
    assert observation(layer, near=[-122.67, 45.51]).site_id == "mid"


def test_a_fahrenheit_row_is_converted_by_name() -> None:
    found = observation(_fc(_site("A", -122.68, 45.51, 50.0, unit="deg F")),
                        to_units="deg C")
    assert found.value == pytest.approx(10.0)
    assert found.units == "deg C"


def test_an_unconvertible_pair_refuses_rather_than_passing_the_number() -> None:
    with pytest.raises(ObservationError) as caught:
        observation(_fc(_site("A", -122.68, 45.51, 5.0, unit="mg/L")),
                    to_units="deg C")
    assert caught.value.error_code == "OBSERVATION_UNIT_UNCONVERTIBLE"


def test_a_station_series_is_read_at_its_last_sample() -> None:
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"station_id": "9014", "station_name": "Port Huron",
                              "time_series_csv":
                                  "2026-09-05T00:00Z,175.10\n"
                                  "2026-09-05T01:00Z,175.14\nbad row\n"}}
    found = observation(_fc(station), measures="a water level")
    assert found.value == pytest.approx(175.14)
    assert found.sampled == "2026-09-05T01:00Z"
    assert found.site_id == "9014"


def test_a_distance_the_fetch_measured_is_preferred() -> None:
    layer = _fc(_site("A", -122.68, 45.51, 9.5, distance_km=1.25))
    assert observation(layer, near=[-100.0, 30.0]).distance_km == pytest.approx(1.25)


def test_nothing_that_reports_refuses_typed() -> None:
    with pytest.raises(ObservationError) as caught:
        observation(_fc(_site("A", -122.68, 45.51, None)),
                    measures="a water temperature", label="the sample layer",
                    code="TELEMAC_WATER_TEMPERATURE_UNMEASURED")
    assert caught.value.error_code == "TELEMAC_WATER_TEMPERATURE_UNMEASURED"
    assert "a water temperature" in str(caught.value)


def test_the_note_names_the_site_the_distance_and_the_moment() -> None:
    found = observation(_fc(_site("A", -122.68, 45.51, 9.5)), near=[-122.67, 45.51])
    said = note(found, opens="the reach opens at")
    assert "A site" in said and "2026-03-04" in said and "SAMPLE" in said


@pytest.mark.parametrize("value,have,want,expected", [
    (212.0, "deg F", "deg C", 100.0),
    (100.0, "deg C", "deg F", 212.0),
    (7.5, "mg/L", "mg/L", 7.5),
    (7.5, "mg/L", None, 7.5),
])
def test_convert_only_where_a_conversion_is_stated(
        value: float, have: object, want: object, expected: float) -> None:
    assert convert(value, have, want) == pytest.approx(expected)
