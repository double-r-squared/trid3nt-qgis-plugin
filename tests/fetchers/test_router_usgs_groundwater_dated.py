"""``fetch_usgs_groundwater_levels`` asked a window: the dated field measurements inside it, or each well's mean.

Read off a recorded real answer of the field-measurements collection over the Des Moines River ring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.tools.fetchers._router.hooks import resolve_hook

_SPEC = reg.get_spec("fetch_usgs_groundwater_levels")
_BODY = (Path(__file__).resolve().parents[1] / "fixtures" / "usgs_groundwater_levels"
         / "dm_field_measurements_2022q2.json").read_bytes()
_BOX = [-93.6729, 41.6625, -93.6369, 41.6895]


def _params(**over):
    return router.validate_params(_SPEC, {"bbox": _BOX, **over})


def _build(**over):
    return resolve_hook("usgs_groundwater_levels.build_request")(_SPEC, _params(**over))[0]


def _parse(body=_BODY, **over):
    return resolve_hook("usgs_groundwater_levels.parse_response")(_SPEC, _params(**over), [body])


def test_unasked_a_window_the_row_reads_the_latest_reading_per_well():
    plan = _build()
    assert plan.url.endswith("/latest-field-measurements/items") and "datetime" not in plan.params


def test_a_moment_reads_the_dated_measurements_reaching_back_the_observation_window(user_settings):
    user_settings(OBSERVATION_WINDOW_DAYS=30.0)
    plan = _build(start_date="2022-05-01", end_date="2022-05-03")
    assert plan.url.endswith("/field-measurements/items")
    assert plan.params["datetime"] == "2022-04-01T00:00:00Z/2022-05-03T23:59:59Z"
    readings = _parse(start_date="2022-05-01", end_date="2022-05-03")
    assert len(readings) == 18
    assert {r["properties"]["datetime"][:10] for r in readings} == {"2022-04-25", "2022-05-02"}
    assert all(r["properties"].get("reading_count") is None for r in readings)


def test_a_span_averages_each_wells_readings_of_one_parameter_over_it():
    plan = _build(start_date="2022-04-01", end_date="2022-06-30", period_average=True)
    assert plan.params["datetime"] == "2022-04-01T00:00:00Z/2022-06-30T23:59:59Z"
    averaged = _parse(start_date="2022-04-01", end_date="2022-06-30", period_average=True)
    assert len(averaged) == 10
    raw = [f["properties"] for f in json.loads(_BODY)["features"]
           if f["properties"]["monitoring_location_id"] == "USGS-414035093391801"
           and f["properties"]["parameter_code"] == "62611"]
    one = next(f["properties"] for f in averaged
               if f["properties"]["site_no"] == "414035093391801" and f["properties"]["parameter_code"] == "62611")
    assert one["water_level"] == pytest.approx(sum(float(r["value"]) for r in raw) / len(raw))
    assert (one["reading_count"], one["datetime"], one["period_end"]) == (2, "2022-04-01", "2022-06-30")


def test_a_window_with_one_end_or_backwards_refuses():
    with pytest.raises(RouterInputError):
        _build(start_date="2022-05-01")
    with pytest.raises(RouterInputError):
        _build(start_date="2022-06-01", end_date="2022-05-01")


def test_a_full_page_refuses_rather_than_return_part_of_the_readings():
    page = json.loads(_BODY)
    page["links"].append({"rel": "next", "href": "https://api.waterdata.usgs.gov/next"})
    with pytest.raises(RouterUpstreamError):
        _parse(json.dumps(page).encode(), start_date="2022-04-01", end_date="2022-06-30")
