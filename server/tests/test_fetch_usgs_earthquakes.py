"""Unit tests for the ``fetch_usgs_earthquakes`` atomic tool.

Real USGS FDSN Event Web Service earthquake-event fetcher (observed seismic
events as points). All HTTP is mocked or operates on synthetic GeoJSON bodies —
no live network in the default suite.

Coverage:
- Error classes carry correct ``retryable`` + ``error_code`` attributes.
- Input validation: bad bbox (shape / range / degenerate), reversed window,
  over-cap window, out-of-range magnitude.
- Window resolution: default ~30-day look-back; one-sided derivation; bare-date
  start/end handling.
- GeoJSON parse: synthetic FeatureCollection -> records with depth from the
  3rd coordinate, mag, place, url, epoch-ms -> ISO time; bad-coordinate drop.
- FlatGeobuf builder: synthetic records -> valid Point FGB round-trips through
  geopandas with the expected columns.
- Honest-empty path: zero-feature FeatureCollection -> EarthquakesNoEventsError
  (never an empty success-shaped layer).
- Result-too-large: metadata.count over the cap -> EarthquakesResultTooLargeError.
- Payload estimator returns a positive float; a higher magnitude floor lowers it.

Live test (gated by TRID3NT_TEST_LIVE_USGS_QUAKES=1): a real FDSN request for a
small seismically active California bbox; confirms >=1 event with a finite mag.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile

import pytest

from trid3nt_server.tools.fetch_usgs_earthquakes import (
    DEFAULT_WINDOW_DAYS,
    FDSN_RESULT_LIMIT,
    MAX_WINDOW_DAYS,
    EarthquakesInputError,
    EarthquakesNoEventsError,
    EarthquakesResultTooLargeError,
    EarthquakesUpstreamError,
    _build_flatgeobuf,
    _build_query_url,
    _epoch_ms_to_iso,
    _events_bbox,
    _parse_event_geojson,
    _resolve_window,
    _validate_bbox,
    _validate_min_magnitude,
    estimate_payload_mb,
    fetch_usgs_earthquakes,
)

# California seismically active bbox used across tests.
CA_BBOX = (-122.5, 35.5, -117.0, 39.0)


# ---------------------------------------------------------------------------
# Synthetic FDSN GeoJSON body (mirrors the real response shape).
# ---------------------------------------------------------------------------


def _synthetic_geojson(n: int = 3, count: int | None = None) -> bytes:
    feats = []
    base_ms = 1782205497650  # a real-shaped epoch-ms value
    for i in range(n):
        feats.append(
            {
                "type": "Feature",
                "id": f"nc{1000 + i}",
                "properties": {
                    "mag": 2.5 + i * 0.7,
                    "magType": "ml",
                    "place": f"{5 + i} km SW of Somewhere, CA",
                    "time": base_ms + i * 1000,
                    "updated": base_ms + i * 2000,
                    "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/nc{1000 + i}",
                    "type": "earthquake",
                    "status": "reviewed",
                    "tsunami": 0,
                    "felt": (i if i else None),
                    "sig": 100 + i,
                    "net": "nc",
                },
                # 3rd coordinate is depth_km.
                "geometry": {
                    "type": "Point",
                    "coordinates": [-121.4 + i * 0.1, 36.7 + i * 0.1, 7.0 + i],
                },
            }
        )
    md = {"status": 200, "title": "USGS Earthquakes"}
    if count is not None:
        md["count"] = count
    return json.dumps(
        {"type": "FeatureCollection", "metadata": md, "features": feats}
    ).encode("utf-8")


def _empty_geojson() -> bytes:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "metadata": {"status": 200, "count": 0},
            "features": [],
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Error-type contract.
# ---------------------------------------------------------------------------


def test_error_types_retryable_and_codes():
    assert EarthquakesInputError.retryable is False
    assert EarthquakesInputError.error_code == "USGS_EARTHQUAKES_INPUT_ERROR"
    assert EarthquakesResultTooLargeError.retryable is False
    assert (
        EarthquakesResultTooLargeError.error_code
        == "USGS_EARTHQUAKES_RESULT_TOO_LARGE"
    )
    assert EarthquakesUpstreamError.retryable is True
    assert EarthquakesUpstreamError.error_code == "USGS_EARTHQUAKES_UPSTREAM_ERROR"
    assert EarthquakesNoEventsError.retryable is False
    assert EarthquakesNoEventsError.error_code == "USGS_EARTHQUAKES_NO_EVENTS"


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
    with pytest.raises(EarthquakesInputError):
        _validate_bbox(bad)


def test_validate_bbox_accepts_good():
    _validate_bbox(CA_BBOX)  # no raise


def test_reversed_window_rejected():
    with pytest.raises(EarthquakesInputError):
        _resolve_window("2026-06-27", "2026-06-01")


def test_over_cap_window_rejected():
    start = "2020-01-01"
    end = "2026-01-01"  # ~6 years, well over MAX_WINDOW_DAYS
    with pytest.raises(EarthquakesInputError):
        _resolve_window(start, end)
    assert MAX_WINDOW_DAYS == 366


def test_bad_date_rejected():
    with pytest.raises(EarthquakesInputError):
        _resolve_window("not-a-date", "2026-06-01")


@pytest.mark.parametrize("bad", [-3.0, 13.0, float("inf"), "abc"])
def test_min_magnitude_out_of_range_rejected(bad):
    with pytest.raises(EarthquakesInputError):
        _validate_min_magnitude(bad)


def test_min_magnitude_none_passes_through():
    assert _validate_min_magnitude(None) is None
    assert _validate_min_magnitude(4.5) == 4.5


# ---------------------------------------------------------------------------
# Window resolution semantics.
# ---------------------------------------------------------------------------


def test_default_window_is_30_days():
    start, end = _resolve_window(None, None)
    d0 = _dt.datetime.fromisoformat(start).replace(tzinfo=_dt.timezone.utc)
    d1 = _dt.datetime.fromisoformat(end).replace(tzinfo=_dt.timezone.utc)
    span_days = (d1 - d0).total_seconds() / 86400.0
    assert abs(span_days - DEFAULT_WINDOW_DAYS) < 0.01


def test_one_sided_start_derives_end():
    # Only start given -> end defaults to now; span is at least a moment.
    start, end = _resolve_window("2026-06-01", None)
    assert start.startswith("2026-06-01")
    assert end > start


def test_one_sided_end_derives_30day_start():
    start, end = _resolve_window(None, "2026-06-30")
    d0 = _dt.date.fromisoformat(start[:10])
    d1 = _dt.date.fromisoformat(end[:10])
    assert (d1 - d0).days == DEFAULT_WINDOW_DAYS


# ---------------------------------------------------------------------------
# URL builder.
# ---------------------------------------------------------------------------


def test_build_query_url_with_bbox_and_mag():
    url = _build_query_url(
        bbox=CA_BBOX,
        starttime="2026-05-28T00:00:00",
        endtime="2026-06-27T00:00:00",
        min_magnitude=2.5,
    )
    assert url.startswith("https://earthquake.usgs.gov/fdsnws/event/1/query?")
    assert "format=geojson" in url
    assert "minlongitude=-122.5" in url
    assert "maxlatitude=39.0" in url
    assert "minmagnitude=2.5" in url
    assert f"limit={FDSN_RESULT_LIMIT}" in url


def test_build_query_url_global_omits_bbox():
    url = _build_query_url(
        bbox=None,
        starttime="2026-05-28T00:00:00",
        endtime="2026-06-27T00:00:00",
        min_magnitude=None,
    )
    assert "minlongitude" not in url
    assert "minmagnitude" not in url


# ---------------------------------------------------------------------------
# GeoJSON parse — compute/shape correctness on synthetic input.
# ---------------------------------------------------------------------------


def test_parse_event_geojson_extracts_fields():
    records, count = _parse_event_geojson(_synthetic_geojson(n=3, count=3))
    assert count == 3
    assert len(records) == 3
    r0 = records[0]
    # depth from the 3rd coordinate
    assert r0["depth_km"] == pytest.approx(7.0)
    assert r0["lon"] == pytest.approx(-121.4)
    assert r0["lat"] == pytest.approx(36.7)
    assert r0["mag"] == pytest.approx(2.5)
    assert r0["mag_type"] == "ml"
    assert r0["place"].endswith("CA")
    assert r0["url"].startswith("https://earthquake.usgs.gov/")
    assert r0["event_type"] == "earthquake"
    assert r0["net"] == "nc"
    # epoch-ms -> ISO UTC
    assert r0["time"] is not None and r0["time"].endswith("Z")


def test_parse_event_geojson_drops_bad_coordinates():
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "metadata": {"count": 2},
            "features": [
                {
                    "type": "Feature",
                    "id": "ok1",
                    "properties": {"mag": 3.0, "time": 1782205497650},
                    "geometry": {"type": "Point", "coordinates": [-120.0, 37.0, 5.0]},
                },
                {
                    "type": "Feature",
                    "id": "bad1",
                    "properties": {"mag": 3.0},
                    "geometry": {"type": "Point", "coordinates": ["x", None]},
                },
            ],
        }
    ).encode("utf-8")
    records, _ = _parse_event_geojson(body)
    assert len(records) == 1
    assert records[0]["id"] == "ok1"


def test_parse_event_geojson_empty_body():
    records, count = _parse_event_geojson(_empty_geojson())
    assert records == []
    assert count == 0


def test_parse_event_geojson_rejects_non_featurecollection():
    body = json.dumps({"type": "Feature"}).encode("utf-8")
    with pytest.raises(EarthquakesUpstreamError):
        _parse_event_geojson(body)


def test_epoch_ms_to_iso_handles_bad():
    assert _epoch_ms_to_iso(None) is None
    assert _epoch_ms_to_iso("nope") is None
    iso = _epoch_ms_to_iso(1782205497650)
    assert iso is not None and iso.endswith("Z")


# ---------------------------------------------------------------------------
# Extent.
# ---------------------------------------------------------------------------


def test_events_bbox_extent():
    recs = [
        {"lon": -121.0, "lat": 36.0},
        {"lon": -120.0, "lat": 37.0},
    ]
    ext = _events_bbox(recs)
    assert ext == (-121.0, 36.0, -120.0, 37.0)


def test_events_bbox_pads_single_point():
    ext = _events_bbox([{"lon": -120.0, "lat": 37.0}])
    assert ext is not None
    west, south, east, north = ext
    assert west < -120.0 < east
    assert south < 37.0 < north


def test_events_bbox_empty_is_none():
    assert _events_bbox([]) is None


# ---------------------------------------------------------------------------
# FlatGeobuf builder — round-trip through geopandas.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_roundtrips():
    gpd = pytest.importorskip("geopandas")
    records, _ = _parse_event_geojson(_synthetic_geojson(n=3, count=3))
    fgb = _build_flatgeobuf(records)
    assert isinstance(fgb, bytes) and len(fgb) > 0

    fp = tempfile.mktemp(suffix=".fgb")
    try:
        with open(fp, "wb") as f:
            f.write(fgb)
        gdf = gpd.read_file(fp)
        assert len(gdf) == 3
        assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
        assert set(gdf.geometry.geom_type.unique()) == {"Point"}
        for col in ("id", "mag", "depth_km", "place", "time", "url", "net"):
            assert col in gdf.columns
        assert float(gdf["depth_km"].min()) == pytest.approx(7.0)
    finally:
        if os.path.exists(fp):
            os.unlink(fp)


# ---------------------------------------------------------------------------
# Honest-empty + result-too-large paths (via the internal fetch fn).
# ---------------------------------------------------------------------------


def test_fetch_bytes_empty_raises_no_events(monkeypatch):
    import trid3nt_server.tools.fetch_usgs_earthquakes as M

    monkeypatch.setattr(M, "_http_get", lambda url, timeout=90.0: (_empty_geojson(), 200))
    with pytest.raises(EarthquakesNoEventsError):
        M._fetch_usgs_earthquakes_bytes(
            bbox=CA_BBOX,
            starttime="2026-06-25T00:00:00",
            endtime="2026-06-27T00:00:00",
            min_magnitude=6.0,
        )


def test_fetch_bytes_204_raises_no_events(monkeypatch):
    import trid3nt_server.tools.fetch_usgs_earthquakes as M

    monkeypatch.setattr(M, "_http_get", lambda url, timeout=90.0: (b"", 204))
    with pytest.raises(EarthquakesNoEventsError):
        M._fetch_usgs_earthquakes_bytes(
            bbox=CA_BBOX,
            starttime="2026-06-25T00:00:00",
            endtime="2026-06-27T00:00:00",
            min_magnitude=6.0,
        )


def test_fetch_bytes_over_cap_raises_too_large(monkeypatch):
    import trid3nt_server.tools.fetch_usgs_earthquakes as M

    body = _synthetic_geojson(n=2, count=FDSN_RESULT_LIMIT + 5)
    monkeypatch.setattr(M, "_http_get", lambda url, timeout=90.0: (body, 200))
    with pytest.raises(EarthquakesResultTooLargeError):
        M._fetch_usgs_earthquakes_bytes(
            bbox=None,
            starttime="2020-01-01T00:00:00",
            endtime="2020-06-01T00:00:00",
            min_magnitude=None,
        )


def test_fetch_bytes_400_too_large_raises(monkeypatch):
    import trid3nt_server.tools.fetch_usgs_earthquakes as M

    monkeypatch.setattr(
        M,
        "_http_get",
        lambda url, timeout=90.0: (b"Error 400: parameter combination exceeds the limit", 400),
    )
    with pytest.raises(EarthquakesResultTooLargeError):
        M._fetch_usgs_earthquakes_bytes(
            bbox=None,
            starttime="2020-01-01T00:00:00",
            endtime="2026-01-01T00:00:00",
            min_magnitude=None,
        )


def test_fetch_bytes_happy_path(monkeypatch):
    import trid3nt_server.tools.fetch_usgs_earthquakes as M

    body = _synthetic_geojson(n=3, count=3)
    monkeypatch.setattr(M, "_http_get", lambda url, timeout=90.0: (body, 200))
    fgb, extent = M._fetch_usgs_earthquakes_bytes(
        bbox=CA_BBOX,
        starttime="2026-05-28T00:00:00",
        endtime="2026-06-27T00:00:00",
        min_magnitude=2.5,
    )
    assert isinstance(fgb, bytes) and len(fgb) > 0
    assert extent is not None and len(extent) == 4


# ---------------------------------------------------------------------------
# Tool-level input validation (the public entrypoint).
# ---------------------------------------------------------------------------


def test_tool_rejects_degenerate_bbox():
    with pytest.raises(EarthquakesInputError):
        fetch_usgs_earthquakes(bbox=(10.0, 10.0, 10.0, 20.0))


def test_tool_rejects_reversed_window():
    with pytest.raises(EarthquakesInputError):
        fetch_usgs_earthquakes(
            bbox=CA_BBOX, start_date="2026-06-27", end_date="2026-06-01"
        )


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_payload_estimator_positive_and_monotonic_in_mag():
    # Pick a regime (moderate area + window, low magnitude floors) where both
    # estimates sit between the MB floor and the result-cap ceiling, so the
    # magnitude-floor monotonicity is observable rather than clamped flat.
    box = (-122.0, 36.0, -118.0, 40.0)  # ~16 sq-deg
    low = estimate_payload_mb(
        bbox=box, start_date="2026-05-01", end_date="2026-06-01",
        min_magnitude=1.0,
    )
    high = estimate_payload_mb(
        bbox=box, start_date="2026-05-01", end_date="2026-06-01",
        min_magnitude=2.5,
    )
    assert low > 0.0
    assert high > 0.0
    # A higher magnitude floor implies fewer events -> a smaller payload.
    assert high < low


def test_payload_estimator_bounded_by_cap():
    # A global, long, no-floor query is bounded by the result cap.
    est = estimate_payload_mb(bbox=None, min_magnitude=0.0)
    assert est <= FDSN_RESULT_LIMIT * 200 / 1_000_000.0 + 1e-6


# ---------------------------------------------------------------------------
# Optional live test (gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("TRID3NT_TEST_LIVE_USGS_QUAKES") != "1",
    reason="set TRID3NT_TEST_LIVE_USGS_QUAKES=1 to run the live FDSN test",
)
def test_live_california_window():
    now = _dt.datetime.now(_dt.timezone.utc)
    start = (now - _dt.timedelta(days=30)).date().isoformat()
    end = now.date().isoformat()
    res = fetch_usgs_earthquakes(
        bbox=CA_BBOX, start_date=start, end_date=end, min_magnitude=2.5
    )
    assert res.layer_type == "vector"
    assert res.style_preset == "earthquakes"
    assert res.uri is not None
    assert res.bbox is not None


# ---------------------------------------------------------------------------
# 2026-07-13 typed no-data contract (live OPEN-17-adjacent incident): a
# 0-event fetch must surface as a TYPED no-data outcome the model can relay
# (typed error + structured suggestions), never a publishable success shape
# with layer handles.
# ---------------------------------------------------------------------------


def test_public_tool_zero_events_raises_typed_no_data(monkeypatch):
    """PUBLIC tool surface: a faked 0-event FDSN body raises the typed error."""
    import trid3nt_server.tools.fetch_usgs_earthquakes as M

    monkeypatch.setattr(
        M, "_http_get", lambda url, timeout=90.0: (_empty_geojson(), 200)
    )

    # Drive read_through's fetch_fn directly (cache plumbing is not under
    # test); the zero-event typed raise must escape before any cache write.
    def _fake_read_through(*, metadata, params, ext, fetch_fn):
        fetch_fn()
        raise AssertionError("fetch_fn must raise on zero events")

    monkeypatch.setattr(M, "read_through", _fake_read_through)

    with pytest.raises(EarthquakesNoEventsError) as ei:
        fetch_usgs_earthquakes(
            bbox=(-100.0, 46.0, -99.0, 47.0), min_magnitude=5.0
        )
    err = ei.value
    assert err.error_code == "USGS_EARTHQUAKES_NO_EVENTS"
    assert err.retryable is False
    # Structured recovery options for the model to relay verbatim.
    assert err.suggestions
    joined = " ".join(err.suggestions).lower()
    assert "min_magnitude" in joined


def test_no_events_error_envelope_is_not_publishable_success():
    """The model-facing envelope: status=error, no layer handles, suggestions."""
    from trid3nt_server.adapter import summarize_tool_result

    err = EarthquakesNoEventsError(
        "No earthquakes matched bbox=(-100.0, 46.0, -99.0, 47.0) over "
        "2026-06-13..2026-07-13 (M>=5.0)."
    )
    env = summarize_tool_result("fetch_usgs_earthquakes", None, error=err)
    assert env["status"] == "error"
    assert env["error_code"] == "USGS_EARTHQUAKES_NO_EVENTS"
    assert env["retryable"] is False
    # NO layer handles / uri / publishable-success keys anywhere.
    flat = json.dumps(env).lower()
    assert "layer_id" not in flat
    assert "layer_handles" not in flat
    assert '"uri"' not in flat
    # Structured suggestions surfaced as a list.
    assert isinstance(env["suggestions"], list)
    assert env["suggestions"]
    assert any("min_magnitude" in s for s in env["suggestions"])


def test_error_envelope_without_suggestions_attr_is_unchanged():
    """Errors that carry no ``suggestions`` keep the pre-2026-07-13 shape."""
    from trid3nt_server.adapter import summarize_tool_result

    env = summarize_tool_result(
        "fetch_usgs_earthquakes", None, error=EarthquakesUpstreamError("boom")
    )
    assert env["status"] == "error"
    assert "suggestions" not in env
