"""The WEATHER typed input: a station record -> one station's series per quantity.

Covered: the station taken is the nearest whose record brackets the run's window,
every quantity its network reports is kept in the unit its name states on a clock
that reads zero where the run opens, a quantity the station is short of is named
rather than dropped, and the refusals - no station at all, and none bracketing.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.observation import ObservationError
from trid3nt_server.inputs.slots import WEATHER, ingest_slot
from trid3nt_server.inputs.weather import UNITS, weather

_HOUR = 3600.0
_SEED = {"lon": -124.1, "lat": 40.48}


def _raws(hour: int, station: str = "SCOC1", **overrides):
    return {"station": station, "utc_valid": f"2026-09-01T{hour:02d}:00:00Z",
            "tmpf": 68.0, "dwpf": 50.0, "relh": 51.0, "sknt": 5.0, "drct": 270.0,
            "solar_rad": 400.0, "precip_in": 0.0, **overrides}


def _fc(rows, lon=-124.2, lat=40.5):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": row,
         "geometry": {"type": "Point", "coordinates": [lon, lat]}}
        for row in rows]}


def _both(near_rows, far_rows):
    doc = _fc(near_rows, lon=-124.11, lat=40.48)
    doc["features"] += _fc(far_rows, lon=-124.5, lat=40.8)["features"]
    return doc


def test_every_quantity_the_network_reports_is_kept_in_the_unit_its_name_states():
    found = weather(_fc([_raws(hour) for hour in range(24)]), near=_SEED)
    assert set(found.columns) == {
        "air_temp_c", "wind_speed_mps", "wind_from_deg", "solar_radiation_wm2",
        "vapour_pressure_pa", "relative_humidity_pct", "dew_point_c"}
    assert found.short == {}
    assert {name: series.units for name, series in found.columns.items()} == {
        name: UNITS[name] for name in found.columns}
    assert found.columns["air_temp_c"].values[0] == pytest.approx(20.0, abs=1e-3)
    assert found.columns["wind_speed_mps"].values[0] == pytest.approx(
        5.0 * 0.514444, abs=1e-3)
    assert found.site_id == "SCOC1" and found.distance_km < 20.0


def test_every_series_reads_zero_at_the_moment_the_run_opens_at():
    found = weather(_fc([_raws(hour) for hour in range(11, 20)]), near=_SEED,
                    at="2026-09-01T12:00:00Z", window_s=6.0 * _HOUR)
    assert found.opens.isoformat() == "2026-09-01T12:00:00+00:00"
    assert found.columns["air_temp_c"].times_s == tuple(
        hour * _HOUR for hour in range(8))


def test_the_nearest_station_bracketing_the_window_is_the_one_taken():
    """A nearer station whose record stops inside the run speaks for none of it."""
    found = weather(_both([_raws(hour, "NEAR") for hour in range(0, 6)],
                          [_raws(hour, "FAR") for hour in range(0, 24)]),
                    near=_SEED, window_s=20.0 * _HOUR)
    assert found.site_id == "FAR"


def test_a_bracketing_station_short_of_one_quantity_is_still_the_station():
    """No station further off is tried for the quantity the nearest is short of:
    it is named with why, and whoever reads it decides what that costs."""
    near = [_raws(hour, "NEAR", solar_rad=None if hour >= 6 else 400.0)
            for hour in range(0, 24)]
    found = weather(_both(near, [_raws(hour, "FAR") for hour in range(0, 24)]),
                    near=_SEED, window_s=20.0 * _HOUR)
    assert found.site_id == "NEAR"
    assert "solar_radiation_wm2" not in found.columns
    assert "after the close" in found.short["solar_radiation_wm2"]
    assert len(found.columns["air_temp_c"]) == 22


def test_a_reading_one_instrument_missed_leaves_the_other_series_whole():
    rows = [_raws(hour, tmpf=None if hour == 3 else 68.0) for hour in range(24)]
    found = weather(_fc(rows), near=_SEED)
    assert len(found.columns["air_temp_c"]) == 23
    assert len(found.columns["wind_speed_mps"]) == 24


def test_a_record_opening_after_the_run_does_refuses_naming_the_opening():
    with pytest.raises(ObservationError, match="no observation at or before the opening"):
        weather(_fc([_raws(hour) for hour in range(13, 20)]), near=_SEED,
                at="2026-09-01T12:00:00Z", window_s=6.0 * _HOUR)


def test_a_record_shorter_than_the_run_refuses_rather_than_extrapolating():
    with pytest.raises(ObservationError, match="the record runs") as refused:
        weather(_fc([_raws(hour) for hour in range(0, 12)]), near=_SEED,
                window_s=20.0 * _HOUR)
    assert refused.value.error_code == "WEATHER_INVALID"


def test_a_record_ending_on_the_close_refuses_naming_the_close():
    """A solve can step just past its close, so the closing reading is strictly after it."""
    with pytest.raises(ObservationError, match="no observation after the close"):
        weather(_fc([_raws(hour) for hour in range(12, 19)]), near=_SEED,
                at="2026-09-01T12:00:00Z", window_s=6.0 * _HOUR)


def test_a_record_holding_no_station_refuses():
    with pytest.raises(ObservationError, match="no station observation"):
        weather(_fc([]), near=_SEED)


def test_the_slot_reads_the_class_the_station_networks_publish():
    assert WEATHER.classes == frozenset({"weather forcing"})
    assert WEATHER.ingest is weather
    found = ingest_slot(WEATHER, _fc([_raws(hour) for hour in range(24)]),
                        label="weather", near=_SEED, window_s=20.0 * _HOUR,
                        column_units={"tmpf": "F"})
    assert found.site_id == "SCOC1"


def test_a_row_naming_no_point_ranks_from_the_middle_of_the_run_s_domain():
    from trid3nt_server.inputs.bound_domain import Domain, bind_domain, reset_domain

    doc = _both([_raws(hour, "NEAR") for hour in range(24)],
                [_raws(hour, "FAR") for hour in range(24)])
    token = bind_domain(Domain(bbox=(-124.55, 40.75, -124.45, 40.85)))
    try:
        assert weather(doc).site_id == "FAR"
    finally:
        reset_domain(token)


def test_the_weather_row_is_told_the_run_s_window_and_the_moment_it_opens_at():
    from types import SimpleNamespace

    from trid3nt_server.inputs.input import Input
    from trid3nt_server.workflows.runtime.fill import _what_the_record_reports

    told = _what_the_record_reports(
        SimpleNamespace(window_s=7200.0,
                        run={"event_time": "2026-09-01T12:00:00Z"},
                        moment="event_time"),
        Input(name="weather", slot=WEATHER), None)
    assert (told["window_s"], told["at"]) == (7200.0, "2026-09-01T12:00:00Z")
