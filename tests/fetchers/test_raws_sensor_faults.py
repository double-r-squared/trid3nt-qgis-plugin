"""``fetch_raws_weather`` drops a reading no atmosphere could have produced.

A relative humidity over saturation and a dew point above the air it was
measured in are instrument faults, and a record that carries them drives a heat
budget to a number nobody measured. Offline: the station-day bodies are stated,
and what is proved is which rows survive the ingestion and what it says it did.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.weather.fetch_raws_weather import hooks as raws


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_raws_weather"]


def _station():
    return [{"type": "Feature", "geometry": {"type": "Point",
                                             "coordinates": [-122.1, 45.6]},
             "properties": {"sid": "TRKW1", "sname": "Three Corner Rock",
                            "lon": -122.1, "lat": 45.6, "elevation": 885.0,
                            "state": "WA"}}]


def _observation(hour: int, **over):
    return {"utc_valid": f"2024-01-14T{hour:02d}:00:00Z", "tmpf": 20.0,
            "dwpf": 14.0, "URHRGZZ": 78.0, "sknt": 4.0, "drct": 90.0,
            "VBIRGZZ": 8.0, "XRIRGZZ": 0.0, "PCIRGZZ": 863.0, **over}


def _merged(spec, observations):
    body = json.dumps({"data": observations}).encode()
    return raws.enrich_merge(
        spec, {"start_date": "2024-01-14", "end_date": "2024-01-14"},
        _station(), {"TRKW1:2024-01-14": SimpleNamespace(body=body)})


def test_a_humidity_over_saturation_is_a_sensor_fault_rather_than_an_instant(spec):
    kept = _merged(spec, [_observation(0), _observation(1, URHRGZZ=332.0)])
    assert [row["properties"]["utc_valid"] for row in kept] == [
        "2024-01-14T00:00:00Z"]


def test_a_dew_point_above_the_air_it_was_measured_in_is_dropped(spec):
    kept = _merged(spec, [_observation(0), _observation(1, dwpf=26.0)])
    assert len(kept) == 1


def test_what_the_record_lost_to_faults_is_said_on_the_run_journal(spec, caplog):
    with caplog.at_level("WARNING"):
        _merged(spec, [_observation(0), _observation(1, URHRGZZ=332.0),
                       _observation(2, dwpf=26.0)])
    assert "2 readings were dropped as sensor faults" in caplog.text


def test_a_record_that_is_all_faults_refuses_rather_than_returning_nothing(spec):
    from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError

    with pytest.raises(RouterEmptyError):
        _merged(spec, [_observation(0, URHRGZZ=332.0)])
