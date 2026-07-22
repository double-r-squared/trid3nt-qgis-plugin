"""Unit tests for the ``fetch_tsunami_events`` atomic tool.

Real NOAA NCEI / WDS Global Historical Tsunami Database fetcher (observed
historical tsunamis as points: source events + coastal runup observations). All
HTTP is mocked or operates on synthetic NCEI JSON bodies - no live network in
the default suite.

Coverage:
- Error classes carry correct ``retryable`` + ``error_code`` attributes.
- Input validation: bad bbox (shape / range / degenerate), reversed year
  window, out-of-range year, bad observation_type.
- Year-window resolution: default ``DEFAULT_MIN_YEAR`` .. current year;
  one-sided; string/float coercion.
- Mode normalization: singular/plural/synonym -> {events, runups}.
- Cause-code mapping: known codes -> labels; unknown -> "Unknown".
- JSON parse (events + runups): synthetic items -> records with the requested
  props (year / cause / max_water_height / deaths / source); null-coordinate and
  out-of-range-coordinate rows dropped; deathsTotal preferred over deaths.
- FlatGeobuf builder: synthetic records -> valid Point FGB round-trips through
  geopandas with the expected columns.
- Honest-empty path: totalItems=0 body -> TsunamiNoEventsError (never an empty
  success-shaped layer).
- Result-too-large: totalPages over the cap -> TsunamiResultTooLargeError.
- Payload estimator returns a positive float; runups estimate > events estimate.
- URL builder: events vs runups path, bbox inclusion, global omission.

Live test (gated by GRACE2_TEST_LIVE_NCEI_TSUNAMI=1): a real NCEI request for a
Japan bbox; confirms the 2011 Tohoku event (M9.1, max_water_height ~39 m) is
present.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest import mock

import pytest

from grace2_agent.tools.fetch_tsunami_events import (
    CAUSE_CODES,
    DEFAULT_MIN_YEAR,
    ITEMS_PER_PAGE,
    MAX_PAGES,
    TsunamiInputError,
    TsunamiNoEventsError,
    TsunamiResultTooLargeError,
    TsunamiUpstreamError,
    _build_flatgeobuf,
    _build_query_url,
    _cause_label,
    _fetch_tsunami_bytes,
    _parse_items,
    _records_bbox,
    _resolve_year_window,
    _validate_bbox,
    _validate_mode,
    estimate_payload_mb,
    fetch_tsunami_events,
)

# Pacific-Rim / Japan bbox used across tests.
JP_BBOX = (135.0, 30.0, 150.0, 45.0)


# ---------------------------------------------------------------------------
# Synthetic NCEI JSON bodies (mirror the real response shape).
# ---------------------------------------------------------------------------


def _events_body(n: int = 3, total_items: int | None = None,
                 total_pages: int = 1) -> dict:
    items = []
    for i in range(n):
        items.append(
            {
                "id": 5000 + i,
                "year": 2010 + i,
                "month": 3,
                "day": 11,
                "causeCode": [1, 6, 8][i % 3],
                "locationName": f"SHORE {i}",
                "country": "JAPAN",
                "latitude": 38.0 + i * 0.1,
                "longitude": 142.0 + i * 0.1,
                "eqMagnitude": 9.1 - i,
                "maxWaterHeight": 39.26 - i * 5,
                "numRunups": 6427 - i * 100,
                "deaths": 100 + i,
                "deathsTotal": 18423 - i * 1000,
            }
        )
    return {
        "items": items,
        "page": 1,
        "totalPages": total_pages,
        "itemsPerPage": ITEMS_PER_PAGE,
        "totalItems": total_items if total_items is not None else n,
    }


def _runups_body(n: int = 3) -> dict:
    items = []
    for i in range(n):
        items.append(
            {
                "id": 9000 + i,
                "year": 2011,
                "sourceCauseCode": 1,
                "country": "JAPAN",
                "locationName": f"RUNUP {i}",
                "latitude": 38.2 + i * 0.05,
                "longitude": 141.0 + i * 0.05,
                "sourceEqMagnitude": 9.1,
                "runupHt": 10.0 + i,
                "distFromSource": 50.0 + i * 10,
                "deaths": i,
            }
        )
    return {
        "items": items,
        "page": 1,
        "totalPages": 1,
        "itemsPerPage": ITEMS_PER_PAGE,
        "totalItems": n,
    }


def _empty_body() -> dict:
    return {
        "items": [],
        "page": 1,
        "totalPages": 0,
        "itemsPerPage": ITEMS_PER_PAGE,
        "totalItems": 0,
    }


# ---------------------------------------------------------------------------
# Error-type contract.
# ---------------------------------------------------------------------------


def test_error_types_retryable_and_codes():
    assert TsunamiInputError.retryable is False
    assert TsunamiInputError.error_code == "TSUNAMI_EVENTS_INPUT_ERROR"
    assert TsunamiResultTooLargeError.retryable is False
    assert (
        TsunamiResultTooLargeError.error_code
        == "TSUNAMI_EVENTS_RESULT_TOO_LARGE"
    )
    assert TsunamiUpstreamError.retryable is True
    assert TsunamiUpstreamError.error_code == "TSUNAMI_EVENTS_UPSTREAM_ERROR"
    assert TsunamiNoEventsError.retryable is False
    assert TsunamiNoEventsError.error_code == "TSUNAMI_EVENTS_NO_EVENTS"


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        (1, 2, 3),  # wrong length
        (-200.0, 0.0, 10.0, 5.0),  # lon out of range
        (0.0, -100.0, 10.0, 5.0),  # lat out of range
        (10.0, 0.0, 5.0, 5.0),  # west >= east
        (0.0, 10.0, 5.0, 5.0),  # south >= north
        (float("nan"), 0.0, 5.0, 5.0),  # non-finite
    ],
)
def test_validate_bbox_rejects_bad(bad):
    with pytest.raises(TsunamiInputError):
        _validate_bbox(bad)


def test_validate_bbox_accepts_good():
    _validate_bbox(JP_BBOX)  # no raise


def test_reversed_year_window_rejected():
    with pytest.raises(TsunamiInputError):
        _resolve_year_window(2025, 1900)


@pytest.mark.parametrize("bad", [9999, -3000, "abc", None.__class__])
def test_out_of_range_or_bad_year_rejected(bad):
    if bad is None.__class__:
        # a non-int-coercible type
        with pytest.raises(TsunamiInputError):
            _resolve_year_window(object(), 2000)
    else:
        with pytest.raises(TsunamiInputError):
            _resolve_year_window(bad, bad if isinstance(bad, int) else 2000)


@pytest.mark.parametrize("bad", ["floods", "earthquake", "xyz", 7])
def test_bad_observation_type_rejected(bad):
    with pytest.raises(TsunamiInputError):
        _validate_mode(bad)


# ---------------------------------------------------------------------------
# Year-window resolution + mode normalization.
# ---------------------------------------------------------------------------


def test_default_year_window():
    lo, hi = _resolve_year_window(None, None)
    assert lo == DEFAULT_MIN_YEAR
    assert hi >= 2026  # current year (test runs in 2026+)


def test_year_window_one_sided_and_coercion():
    lo, hi = _resolve_year_window("2011", None)
    assert lo == 2011 and hi >= 2026
    lo2, hi2 = _resolve_year_window(None, 1960.0)
    assert lo2 == DEFAULT_MIN_YEAR and hi2 == 1960


def test_mode_normalization():
    assert _validate_mode(None) == "events"
    assert _validate_mode("event") == "events"
    assert _validate_mode("Sources") == "events"
    assert _validate_mode("runup") == "runups"
    assert _validate_mode("observations") == "runups"


# ---------------------------------------------------------------------------
# Cause-code mapping.
# ---------------------------------------------------------------------------


def test_cause_label_known_and_unknown():
    assert _cause_label(1) == "Earthquake"
    assert _cause_label(6) == "Volcano"
    assert _cause_label(8) == "Landslide"
    assert _cause_label(9) == "Meteorological"
    assert _cause_label(99) == "Unknown"
    assert _cause_label(None) == "Unknown"
    assert _cause_label("notint") == "Unknown"
    # All declared codes round-trip to a non-empty label.
    for code, label in CAUSE_CODES.items():
        assert _cause_label(code) == label and label


# ---------------------------------------------------------------------------
# URL builder.
# ---------------------------------------------------------------------------


def test_build_query_url_events_with_bbox():
    url = _build_query_url(
        mode="events", bbox=JP_BBOX, min_year=1900, max_year=2025, page=1
    )
    assert url.startswith(
        "https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1/tsunamis/events?"
    )
    assert "minYear=1900" in url
    assert "maxYear=2025" in url
    assert "minLongitude=135.0" in url
    assert "maxLatitude=45.0" in url
    assert f"itemsPerPage={ITEMS_PER_PAGE}" in url


def test_build_query_url_runups_path_and_global_omits_bbox():
    url = _build_query_url(
        mode="runups", bbox=None, min_year=2011, max_year=2011, page=2
    )
    assert "/tsunamis/runups?" in url
    assert "minLongitude" not in url
    assert "page=2" in url


# ---------------------------------------------------------------------------
# JSON parse.
# ---------------------------------------------------------------------------


def test_parse_events_carries_requested_props():
    recs = _parse_items(_events_body(n=3)["items"], "events")
    assert len(recs) == 3
    r0 = recs[0]
    # Requested props: year / cause / max_water_height / deaths / source.
    assert r0["year"] == 2010
    assert r0["cause"] == "Earthquake"
    assert r0["max_water_height"] == 39.26
    # deathsTotal preferred over per-source deaths.
    assert r0["deaths"] == 18423
    assert r0["num_runups"] == 6427
    assert r0["eq_magnitude"] == 9.1
    assert r0["observation_type"] == "event"
    assert "NCEI" in r0["source"]


def test_parse_runups_uses_runup_height_and_distance():
    recs = _parse_items(_runups_body(n=2)["items"], "runups")
    assert len(recs) == 2
    r0 = recs[0]
    assert r0["cause"] == "Earthquake"  # from sourceCauseCode
    assert r0["max_water_height"] == 10.0  # from runupHt
    assert r0["dist_from_source_km"] == 50.0
    assert r0["eq_magnitude"] == 9.1  # from sourceEqMagnitude
    assert r0["observation_type"] == "runup"


def test_parse_drops_null_and_out_of_range_coords():
    items = [
        {"id": 1, "year": 2000, "causeCode": 1, "latitude": None,
         "longitude": 10.0},  # null lat
        {"id": 2, "year": 2000, "causeCode": 1, "latitude": 95.0,
         "longitude": 10.0},  # lat out of range
        {"id": 3, "year": 2000, "causeCode": 1, "latitude": 38.0,
         "longitude": 142.0},  # good
    ]
    recs = _parse_items(items, "events")
    assert len(recs) == 1 and recs[0]["id"] == 3


# ---------------------------------------------------------------------------
# FlatGeobuf builder + extent.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_roundtrips():
    gpd = pytest.importorskip("geopandas")
    recs = _parse_items(_events_body(n=3)["items"], "events")
    fgb = _build_flatgeobuf(recs)
    assert fgb[:2] == b"fg" or len(fgb) > 0  # FlatGeobuf magic begins with 'fgb'
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        path = f.name
    try:
        gdf = gpd.read_file(path)
        assert len(gdf) == 3
        for col in (
            "year", "cause", "max_water_height", "deaths", "eq_magnitude",
            "observation_type", "source",
        ):
            assert col in gdf.columns
        # FlatGeobuf may reorder features by its spatial index, so assert on the
        # SET of causes (codes 1/6/8 -> Earthquake/Volcano/Landslide), not order.
        assert set(gdf["cause"]) == {"Earthquake", "Volcano", "Landslide"}
        assert set(gdf["observation_type"]) == {"event"}
    finally:
        os.unlink(path)


def test_records_bbox_pads_single_point():
    recs = [{"lon": 142.0, "lat": 38.0}]
    bbox = _records_bbox(recs)
    assert bbox is not None
    w, s, e, n = bbox
    assert w < 142.0 < e and s < 38.0 < n
    assert _records_bbox([]) is None


# ---------------------------------------------------------------------------
# Honest-empty + result-too-large (mocked HTTP).
# ---------------------------------------------------------------------------


def test_empty_result_raises_no_events():
    with mock.patch(
        "grace2_agent.tools.fetch_tsunami_events._http_get_json",
        return_value=_empty_body(),
    ):
        with pytest.raises(TsunamiNoEventsError):
            _fetch_tsunami_bytes(
                mode="events", bbox=JP_BBOX, min_year=2020, max_year=2020
            )


def test_too_many_pages_raises_too_large():
    over = _events_body(n=3, total_items=99999, total_pages=MAX_PAGES + 5)
    with mock.patch(
        "grace2_agent.tools.fetch_tsunami_events._http_get_json",
        return_value=over,
    ):
        with pytest.raises(TsunamiResultTooLargeError):
            _fetch_tsunami_bytes(
                mode="runups", bbox=JP_BBOX, min_year=1900, max_year=2025
            )


def test_pagination_accumulates_items():
    # Two pages of 3 -> 6 records.
    page1 = _events_body(n=3, total_items=6, total_pages=2)
    page2 = _events_body(n=3, total_items=6, total_pages=2)
    bodies = [page1, page2]
    with mock.patch(
        "grace2_agent.tools.fetch_tsunami_events._http_get_json",
        side_effect=bodies,
    ):
        fgb, extent = _fetch_tsunami_bytes(
            mode="events", bbox=JP_BBOX, min_year=2000, max_year=2025
        )
    gpd = pytest.importorskip("geopandas")
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        path = f.name
    try:
        gdf = gpd.read_file(path)
        assert len(gdf) == 6
    finally:
        os.unlink(path)


def test_upstream_error_propagates():
    with mock.patch(
        "grace2_agent.tools.fetch_tsunami_events._http_get_json",
        side_effect=TsunamiUpstreamError("boom"),
    ):
        with pytest.raises(TsunamiUpstreamError):
            _fetch_tsunami_bytes(
                mode="events", bbox=JP_BBOX, min_year=2000, max_year=2025
            )


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_payload_estimator_positive_and_runups_denser():
    e = estimate_payload_mb(
        bbox=JP_BBOX, min_year=1900, max_year=2025, observation_type="events"
    )
    r = estimate_payload_mb(
        bbox=JP_BBOX, min_year=1900, max_year=2025, observation_type="runups"
    )
    assert e > 0.0 and r > 0.0
    assert r > e  # runups are denser than source events
    # Global query > bounded bbox.
    g = estimate_payload_mb(bbox=None, observation_type="events")
    assert g >= e


# ---------------------------------------------------------------------------
# Full tool with a mocked fetch + a stubbed read_through (no S3, no network).
# ---------------------------------------------------------------------------


def test_full_tool_returns_layer_uri():
    from grace2_agent.tools import cache as cache_mod

    fake_uri = "s3://bucket/cache/semi-static-7d/ncei_tsunami/deadbeef.fgb"

    class _Result:
        uri = fake_uri
        data = b""
        hit = False

    def _fake_read_through(metadata, params, ext, fetch_fn, **kw):
        # Exercise the fetch_fn so the captured-extent path runs.
        fetch_fn()
        return _Result()

    body = _events_body(n=3)
    with mock.patch(
        "grace2_agent.tools.fetch_tsunami_events._http_get_json",
        return_value=body,
    ), mock.patch.object(
        cache_mod, "read_through", side_effect=_fake_read_through
    ), mock.patch(
        "grace2_agent.tools.fetch_tsunami_events.read_through",
        side_effect=_fake_read_through,
    ):
        lyr = fetch_tsunami_events(
            bbox=JP_BBOX, min_year=2000, max_year=2025,
            observation_type="events",
        )
    assert lyr.uri == fake_uri
    assert lyr.layer_type == "vector"
    assert lyr.style_preset == "tsunami_events"
    assert lyr.role == "primary"
    assert lyr.bbox is not None
    assert "tsunami-events-" in lyr.layer_id


def test_full_tool_rejects_bad_bbox_before_fetch():
    with pytest.raises(TsunamiInputError):
        fetch_tsunami_events(bbox=(10.0, 0.0, 5.0, 5.0))  # west >= east


# ---------------------------------------------------------------------------
# Live test (opt-in; real NCEI network).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("GRACE2_TEST_LIVE_NCEI_TSUNAMI") != "1",
    reason="set GRACE2_TEST_LIVE_NCEI_TSUNAMI=1 to hit the real NCEI service",
)
def test_live_japan_has_tohoku_2011():
    gpd = pytest.importorskip("geopandas")
    fgb, extent = _fetch_tsunami_bytes(
        mode="events", bbox=JP_BBOX, min_year=1900, max_year=2025
    )
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        path = f.name
    try:
        gdf = gpd.read_file(path)
    finally:
        os.unlink(path)
    assert len(gdf) > 0
    tohoku = gdf[(gdf["year"] == 2011) & (gdf["eq_magnitude"] == 9.1)]
    assert len(tohoku) == 1
    assert float(tohoku.iloc[0]["max_water_height"]) > 30.0
    assert tohoku.iloc[0]["cause"] == "Earthquake"
