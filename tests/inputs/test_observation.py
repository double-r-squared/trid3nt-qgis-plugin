"""Unit tests for the OBSERVATION typed input.

Covered: one value read off a sample layer, the nearest of several when the
source was not already narrowed, a row reporting nothing skipped, a Fahrenheit
row converted by name, an unconvertible pair refusing, a station series read at
its LAST sample, a distance the fetch already measured preferred over one
re-derived here, nothing that reports refusing, the note a run says, and the
WINDOW a run's own moment closes: a sample from another decade is not ranked,
a sample inside the window is, and nothing inside it refuses - and the WINDOW
itself: the whole series kept beside the reading, in the slot's unit, on the
slot's datum, and absent where the record reported one moment."""

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


def test_a_sample_from_another_decade_is_not_this_runs_water() -> None:
    """The nearest site is the one that stopped reporting in 1974: it is nearer
    than the station that sampled this winter, and it is not the reading."""
    layer = _fc(_site("retired", -122.6698, 45.5185, 13.5,
                      result_date="1974-05-10"),
                _site("gauge", -122.6692, 45.5175, 7.2,
                      result_date="2023-12-18"))
    found = observation(layer, near=[-122.6698, 45.5185],
                        at="2024-01-14T12:00:00Z")
    assert found.site_id == "gauge"
    assert found.value == pytest.approx(7.2)
    assert found.sampled == "2023-12-18"


def test_nothing_sampled_in_the_window_refuses_rather_than_reaching_back() -> None:
    layer = _fc(_site("retired", -122.6698, 45.5185, 13.5,
                      result_date="1974-05-10"))
    with pytest.raises(ObservationError) as caught:
        observation(layer, near=[-122.6698, 45.5185], at="2024-01-14T12:00:00Z",
                    measures="a water temperature", label="the sample layer",
                    code="TELEMAC_WATER_TEMPERATURE_UNMEASURED")
    assert caught.value.error_code == "TELEMAC_WATER_TEMPERATURE_UNMEASURED"
    assert "within 30 days" in str(caught.value)


def test_a_row_that_states_no_moment_ranks_every_sample_it_fetched() -> None:
    layer = _fc(_site("retired", -122.6698, 45.5185, 13.5,
                      result_date="1974-05-10"))
    assert observation(layer, near=[-122.6698, 45.5185]).site_id == "retired"


def test_the_note_names_the_site_the_distance_and_the_moment() -> None:
    found = observation(_fc(_site("A", -122.68, 45.51, 9.5)), near=[-122.67, 45.51])
    said = note(found, opens="the reach opens at")
    assert "A site" in said and "2026-03-04" in said and "SAMPLE" in said


@pytest.mark.parametrize("value,have,want,expected", [
    (212.0, "deg F", "deg C", 100.0),
    (100.0, "deg C", "deg F", 212.0),
    (7.5, "mg/L", "mg/L", 7.5),
    (7.5, "mg/L", None, 7.5),
    # one unit, as each source spells it: the Water Quality Portal writes
    # "deg C" where a keyword reads "degC"
    (8.0, "deg C", "degC", 8.0),
    (8.0, "\u00b0C", "degC", 8.0),
])
def test_convert_only_where_a_conversion_is_stated(
        value: float, have: object, want: object, expected: float) -> None:
    assert convert(value, have, want) == pytest.approx(expected)


def test_the_whole_window_is_kept_beside_the_reading() -> None:
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"site_id": "14211720", "discharge_cfs": 300.0,
                              "unit": "ft3/s",
                              "time_series_csv":
                                  "2026-09-05T00:00Z,100\n"
                                  "2026-09-05T01:00Z,200\n"
                                  "2026-09-05T02:00Z,300\n"}}
    found = observation(_fc(station), field="discharge_cfs", to_units="m3/s",
                        measures="a streamflow")
    assert str(found.series) == "series, 3 points over the window"
    assert found.series.units == "m3/s"
    assert found.series.times_s == (0.0, 3600.0, 7200.0)
    assert found.series.values[0] == pytest.approx(2.8316846592)
    assert found.value == pytest.approx(found.series.values[-1])


def test_the_window_is_read_in_the_unit_the_row_states_for_it() -> None:
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"site_id": "14211720", "discharge_cfs": 300.0,
                              "time_series_csv":
                                  "2026-09-05T00:00Z,100\n"
                                  "2026-09-05T01:00Z,200\n"}}
    found = observation(_fc(station), field="discharge_cfs", to_units="m3/s",
                        record_units="ft3/s", measures="a streamflow")
    assert found.series.units == "m3/s"
    assert found.series.values[0] == pytest.approx(2.8316846592)


def test_a_record_that_reported_one_moment_carries_no_window() -> None:
    assert observation(_fc(_site("A", -122.68, 45.51, 9.5))).series is None


def test_the_run_opens_at_its_own_moment_inside_the_record() -> None:
    """t=0 is the instant the run asks at, not the record's first sample."""
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"site_id": "14211720", "discharge_cfs": 300.0,
                              "unit": "ft3/s",
                              "reading_dt": "2026-09-13T23:00Z",
                              "time_series_csv":
                                  "2026-09-13T21:00Z,100\n"
                                  "2026-09-13T22:00Z,200\n"
                                  "2026-09-13T23:00Z,300\n"}}
    found = observation(_fc(station), field="discharge_cfs", to_units="m3/s",
                        at="2026-09-13T22:00:00Z", window_s=3600.0,
                        measures="a streamflow")
    assert found.series.times_s == (-3600.0, 0.0, 3600.0)


def test_a_record_that_stops_before_the_run_does_refuses_naming_the_nearest() -> None:
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"site_id": "14211720", "discharge_cfs": 300.0,
                              "unit": "ft3/s",
                              "reading_dt": "2026-09-13T22:00Z",
                              "time_series_csv":
                                  "2026-09-13T21:00Z,100\n"
                                  "2026-09-13T22:00Z,200\n"}}
    with pytest.raises(ObservationError) as caught:
        observation(_fc(station), field="discharge_cfs", to_units="m3/s",
                    at="2026-09-13T21:30:00Z", window_s=172800.0,
                    measures="a streamflow", label="the gauge")
    assert caught.value.error_code == "OBSERVATION_WINDOW_UNCOVERED"
    assert "2026-09-13T22:00" in str(caught.value)
    assert "window loosened" in str(caught.value)


def test_a_window_in_no_stated_unit_refuses_rather_than_reading_the_slot_s() -> None:
    """The cfs defect: 2000 ft3/s read as 2000 m3/s is a different river."""
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"site_id": "14211720", "discharge_cfs": 2000.0,
                              "time_series_csv":
                                  "2026-09-05T00:00Z,1000\n"
                                  "2026-09-05T01:00Z,2000\n"}}
    with pytest.raises(ObservationError) as caught:
        observation(_fc(station), field="discharge_cfs", to_units="m3/s",
                    measures="a streamflow")
    assert caught.value.error_code == "OBSERVATION_UNIT_UNSTATED"


def test_the_coverage_row_s_column_unit_is_what_a_bare_record_is_read_in() -> None:
    station = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
               "properties": {"site_id": "14211720", "discharge_cfs": 2000.0,
                              "time_series_csv":
                                  "2026-09-05T00:00Z,1000\n"
                                  "2026-09-05T01:00Z,2000\n"}}
    found = observation(_fc(station), field="discharge_cfs", to_units="m3/s",
                        column_units={"discharge_cfs": "ft3/s",
                                      "time_series_csv": "ft3/s"},
                        measures="a streamflow")
    assert found.value == pytest.approx(56.633693184)
    assert found.series.units == "m3/s"
    assert found.series.values[-1] == pytest.approx(56.633693184)


def _gauge(**over):
    props = {"site_id": "14211720", "gage_height_ft": 3.4,
             "gauge_datum_ft": 37.2, "vertical_datum": "NAVD88",
             "reading_dt": "2026-09-13T22:00Z",
             "stage_series_csv": "2026-09-13T21:00Z,3.1\n"
                                 "2026-09-13T22:00Z,3.4\n"}
    props.update(over)
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.68, 45.51]},
            "properties": props}


def test_a_gage_height_reaches_the_run_s_frame_over_the_gauge_s_own_zero() -> None:
    """3.4 ft above a zero that stands 37.2 ft up is 40.6 ft, not 3.4."""
    found = observation(_fc(_gauge()), field="gage_height_ft",
                        series_field="stage_series_csv", above_field="gauge_datum_ft",
                        column_units={"gage_height_ft": "ft",
                                      "stage_series_csv": "ft",
                                      "gauge_datum_ft": "ft"},
                        to_units="m", to_datum="NAVD88",
                        measures="a water-surface elevation")
    assert found.value == pytest.approx(40.6 * 0.3048)
    assert found.datum == "NAVD88"
    assert found.series.values[0] == pytest.approx((3.1 + 37.2) * 0.3048)


def test_a_gauge_that_publishes_no_zero_refuses_rather_than_reading_a_height() -> None:
    with pytest.raises(ObservationError) as caught:
        observation(_fc(_gauge(gauge_datum_ft=None)), field="gage_height_ft",
                    above_field="gauge_datum_ft",
                    column_units={"gage_height_ft": "ft"},
                    to_units="m", to_datum="NAVD88",
                    measures="a water-surface elevation")
    assert caught.value.error_code == "OBSERVATION_GAUGE_ZERO_UNSTATED"
