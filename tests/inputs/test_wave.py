"""Unit tests for the WAVE typed input: a buoy's record -> the sea state it measured.

Covered: the three columns found by the units the source states them in, the
reading taken at the moment the run opens at rather than at the record's last
sample, the record's own period and from-bearing kept as reported (what a deck
makes of them is its engine's), the nearest buoy when the source was not narrowed,
and the refusals - a record that measures none of the three, one that states two
columns in a quantity's unit, and a period of zero.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.observation import ObservationError
from trid3nt_server.inputs.slots import ingest_slot
from trid3nt_server.inputs.wave import wave
from trid3nt_server.inputs.slots import WAVE

#: What the sea-state source states about its own columns: three readings and
#: the window behind each, every one of them in the unit it is measured in.
_UNITS = {"wave_height_m": "m", "wave_height_series_csv": "m",
          "peak_period_s": "s", "peak_period_series_csv": "s",
          "wave_direction_deg_true": "degT",
          "wave_direction_series_csv": "degT"}

_WINDOW = ("2026-02-10T00:00:00Z", "2026-02-10T01:00:00Z",
           "2026-02-10T02:00:00Z")


def _series(*values: float) -> str:
    return "\n".join(f"{stamp},{value}" for stamp, value in zip(_WINDOW, values))


def _buoy(station: str, lon: float, lat: float, *, height=(1.0, 2.0, 3.0),
          period=(8.0, 10.0, 12.0), heading=(90.0, 95.0, 100.0)) -> dict:
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "station_id": station, "station_name": f"{station} buoy",
                "wave_height_m": height[-1], "peak_period_s": period[-1],
                "wave_direction_deg_true": heading[-1],
                "wave_height_series_csv": _series(*height),
                "peak_period_series_csv": _series(*period),
                "wave_direction_series_csv": _series(*heading)}}


def _fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def test_each_quantity_is_read_off_the_column_stated_in_its_own_unit():
    """A slot that states a need names no column of a source it did not choose,
    so what says which column carries the period is the unit the source states
    that column in."""
    found = wave(_fc(_buoy("44056", -75.71, 36.20)), column_units=_UNITS)
    assert found.height_m == pytest.approx(3.0)
    assert found.peak_period_s == pytest.approx(12.0)
    assert found.from_direction_deg == pytest.approx(100.0)


def test_the_sea_state_read_is_the_one_measured_when_the_run_opens():
    """A record carries a window, and a boundary forced at whatever the buoy
    last held would be a sea state from another hour."""
    found = wave(_fc(_buoy("44056", -75.71, 36.20)), column_units=_UNITS,
                 at="2026-02-10T01:00:00Z")
    assert found.height_m == pytest.approx(2.0)
    assert found.peak_period_s == pytest.approx(10.0)
    assert found.from_direction_deg == pytest.approx(95.0)
    assert found.sampled == "2026-02-10T01:00:00Z"


def test_the_sea_state_carries_no_engine_s_reading_of_it():
    """A frequency and a bearing run TOWARD are what one engine's boundary
    states; the type holds what the buoy measured and nothing derived from it."""
    import dataclasses

    found = wave(_fc(_buoy("44056", -75.71, 36.20)), column_units=_UNITS,
                 at="2026-02-10T00:00:00Z")
    assert {field.name for field in dataclasses.fields(found)} == {
        "height_m", "peak_period_s", "from_direction_deg", "site_id",
        "site_name", "sampled", "distance_km"}
    assert (found.peak_period_s, found.from_direction_deg) == (8.0, 90.0)


def test_the_nearest_buoy_answers_when_the_source_was_not_narrowed():
    layer = _fc(_buoy("offshore", -75.59, 36.19, height=(4.0,) * 3),
                _buoy("inshore", -75.71, 36.18, height=(1.5,) * 3))
    found = wave(layer, near=[-75.72, 36.18], column_units=_UNITS)
    assert found.site_id == "inshore"
    assert found.height_m == pytest.approx(1.5)


def test_a_record_that_measures_no_period_refuses_by_the_quantity_it_lacks():
    """A source publishing a height and nothing else measures no sea state, and
    the refusal says which of the three it could not read."""
    columns = {k: v for k, v in _UNITS.items() if "period" not in k}
    with pytest.raises(ObservationError) as refused:
        wave(_fc(_buoy("44056", -75.71, 36.20)), column_units=columns)
    assert "a wave period" in str(refused.value)


def test_two_columns_in_one_unit_refuse_rather_than_the_first_being_taken():
    """A quantity read off whichever column came first is not a measurement
    anybody took."""
    columns = dict(_UNITS, swell_period_s="s")
    with pytest.raises(ObservationError) as refused:
        wave(_fc(_buoy("44056", -75.71, 36.20)), column_units=columns)
    assert "2 columns in that unit" in str(refused.value)


def test_a_period_of_zero_is_not_a_sea_state_a_boundary_can_be_forced_at():
    with pytest.raises(ObservationError) as refused:
        wave(_fc(_buoy("44056", -75.71, 36.20, period=(0.0,) * 3)),
             column_units=_UNITS)
    assert "zero or less is not a sea state" in str(refused.value)


def test_the_slot_reserves_the_name_and_reads_the_class_the_buoys_publish():
    """A row's NAME is its role: a row called wave is a sea state, and the one
    class that fills it is the one a buoy network states coverage of."""
    assert WAVE.classes == frozenset({"wave series"})
    assert WAVE.ingest is wave
    assert WAVE.geometry == ""
    found = ingest_slot(WAVE, _fc(_buoy("44056", -75.71, 36.20)),
                        label="wave", column_units=_UNITS)
    assert found.height_m == pytest.approx(3.0)


def test_the_wave_row_is_told_the_record_s_own_columns_and_the_run_s_window():
    from types import SimpleNamespace

    from trid3nt_server.workflows.runtime.fill import _what_the_record_reports
    from trid3nt_server.inputs.input import Input

    row = SimpleNamespace(units=_UNITS, value_column="wave_height_m",
                          series_column="wave_height_series_csv",
                          above_column="", data_class="wave series")
    told = _what_the_record_reports(SimpleNamespace(window_s=7200.0, run={}, moment=""),
                                    Input(name="wave", slot=WAVE), row)
    assert told["column_units"] == _UNITS
    assert told["window_s"] == 7200.0
