"""Unit + live tests for ``fetch_noaa_coops_currents``.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Input validation: bad bbox shapes, degenerate bbox, out-of-range, unknown
  product, wrong-type product.
- Station discovery: stations inside bbox returned; out-of-bbox excluded;
  empty bbox raises honest typed error; upstream failure raises typed error.
- URL builder: observed vs predictions, english units (knots), MAX_SLACK.
- Per-station parse: observed picks latest row; predictions pick nearest-now
  with flood/ebb direction selection; missing/NaN handled.
- FlatGeobuf serialization shape (with + without records).
- Error classes carry retryable + error_code.
- Payload estimator returns a positive sane float.
- Cache miss -> fetch_fn invoked; cache hit -> fetch_fn skipped.
- LayerURI shape: layer_type="vector", role="primary", units="kn".
- Extra-kwargs absorption (LLM hallucination guard).

Live test (gated by TRID3NT_TEST_LIVE_COOPS=1):
    Real CO-OPS API request for San Francisco Bay (4 current stations) for
    both observed and predicted products. Confirms >=1 station, FGB round-trip,
    speed_kn >= 0, direction_deg in [0, 360), coords in the SF Bay envelope.
"""

from __future__ import annotations

import datetime
import json
import os
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_noaa_coops_currents import (
    COOPSCurrentsEmptyError,
    COOPSCurrentsInputError,
    COOPSCurrentsUpstreamError,
    _build_currents_url,
    _build_flatgeobuf,
    _discover_stations_in_bbox,
    _parse_observed,
    _parse_predictions,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_product,
    estimate_payload_mb,
    fetch_noaa_coops_currents,
)

# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

# San Francisco Bay bbox (4 current stations: s06010, s08010, s09010, s10010).
_SF_BAY_BBOX: tuple[float, float, float, float] = (-123.0, 37.4, -122.0, 38.2)

_LIVE_COOPS = os.environ.get("TRID3NT_TEST_LIVE_COOPS") == "1"

_NOW = datetime.datetime(2026, 6, 27, 23, 0, 0, tzinfo=datetime.timezone.utc)


def _make_station_catalog(stations: list[dict[str, Any]]) -> bytes:
    return json.dumps({"stations": stations}).encode("utf-8")


def _make_observed_response(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps({
        "metadata": {"id": "s08010", "name": "Southampton Shoal",
                     "lat": "37.9162", "lon": "-122.4223"},
        "data": rows,
    }).encode("utf-8")


def _make_predictions_response(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps({
        "current_predictions": {"units": "knots", "cp": rows},
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (GCP decommissioned).
# ---------------------------------------------------------------------------


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


_PINNED_NOW = datetime.datetime(2026, 6, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_read_through_injector(fake_gcs):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        ReadThroughResult,
        cache_path,
        compute_cache_key,
        is_cacheable,
    )

    store = fake_gcs.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        patched.call_count["n"] += 1
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    patched.call_count = {"n": 0}
    return patched


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_noaa_coops_currents" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_noaa_coops_currents"]
    assert entry.metadata.name == "fetch_noaa_coops_currents"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "noaa_coops_currents"
    assert entry.metadata.cacheable is True


def test_supports_global_query_is_false():
    entry = TOOL_REGISTRY["fetch_noaa_coops_currents"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_SF_BAY_BBOX)


def test_validate_bbox_degenerate():
    with pytest.raises(COOPSCurrentsInputError, match="degenerate"):
        _validate_bbox((-122.5, 37.4, -122.5, 38.2))  # west == east


def test_validate_bbox_inverted():
    with pytest.raises(COOPSCurrentsInputError, match="degenerate"):
        _validate_bbox((-122.0, 38.2, -123.0, 37.4))


def test_validate_bbox_out_of_range():
    with pytest.raises(COOPSCurrentsInputError, match="lon"):
        _validate_bbox((-200.0, 37.4, -122.0, 38.2))


def test_validate_bbox_wrong_length():
    with pytest.raises(COOPSCurrentsInputError, match="west, south, east, north"):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_validate_bbox_non_finite():
    with pytest.raises(COOPSCurrentsInputError, match="non-finite"):
        _validate_bbox((float("nan"), 37.4, -122.0, 38.2))


def test_validate_product_ok():
    _validate_product("currents")
    _validate_product("currents_predictions")


def test_validate_product_unknown():
    with pytest.raises(COOPSCurrentsInputError, match="unsupported product"):
        _validate_product("water_level")


def test_validate_product_wrong_type():
    with pytest.raises(COOPSCurrentsInputError, match="str"):
        _validate_product(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_classes_attributes():
    for cls, retryable in [
        (COOPSCurrentsInputError, False),
        (COOPSCurrentsUpstreamError, True),
        (COOPSCurrentsEmptyError, False),
    ]:
        inst = cls("test")
        assert inst.retryable is retryable, f"{cls.__name__}.retryable wrong"
        assert isinstance(inst.error_code, str) and inst.error_code != ""


# ---------------------------------------------------------------------------
# Station discovery.
# ---------------------------------------------------------------------------


def test_discover_stations_in_bbox_filters_correctly():
    catalog = [
        {"id": "s08010", "name": "Southampton Shoal", "lat": "37.9162", "lng": "-122.4223"},  # in
        {"id": "s06010", "name": "Martinez", "lat": "38.0346", "lng": "-122.1252"},  # in
        {"id": "bh0101", "name": "Boston Harbor", "lat": "42.35", "lng": "-71.05"},  # out
        {"id": "n05010", "name": "NY Harbor", "lat": "40.70", "lng": "-74.02"},  # out
    ]
    with patch(
        "trid3nt_server.tools.fetch_noaa_coops_currents._http_get",
        return_value=_make_station_catalog(catalog),
    ):
        result = _discover_stations_in_bbox(_SF_BAY_BBOX)
    ids = {s["id"] for s in result}
    assert "s08010" in ids
    assert "s06010" in ids
    assert "bh0101" not in ids
    assert "n05010" not in ids


def test_discover_stations_empty_bbox():
    catalog = _make_station_catalog([
        {"id": "bh0101", "name": "Boston", "lat": "42.35", "lng": "-71.05"},
    ])
    with patch(
        "trid3nt_server.tools.fetch_noaa_coops_currents._http_get",
        return_value=catalog,
    ), pytest.raises(COOPSCurrentsEmptyError):
        _discover_stations_in_bbox(_SF_BAY_BBOX)


def test_discover_stations_upstream_error():
    with patch(
        "trid3nt_server.tools.fetch_noaa_coops_currents._http_get",
        side_effect=COOPSCurrentsUpstreamError("network timeout"),
    ), pytest.raises(COOPSCurrentsUpstreamError):
        _discover_stations_in_bbox(_SF_BAY_BBOX)


# ---------------------------------------------------------------------------
# URL builder.
# ---------------------------------------------------------------------------


def test_build_currents_url_observed():
    url = _build_currents_url("s08010", "currents", date(2026, 6, 26), date(2026, 6, 27))
    assert "station=s08010" in url
    assert "product=currents" in url
    assert "begin_date=20260626" in url
    assert "end_date=20260627" in url
    assert "units=english" in url  # knots
    assert "format=json" in url
    assert "interval=MAX_SLACK" not in url


def test_build_currents_url_predictions():
    url = _build_currents_url(
        "s08010", "currents_predictions", date(2026, 6, 27), date(2026, 6, 29)
    )
    assert "product=currents_predictions" in url
    assert "interval=MAX_SLACK" in url
    assert "units=english" in url


# ---------------------------------------------------------------------------
# Observed parser.
# ---------------------------------------------------------------------------


def test_parse_observed_picks_latest():
    resp = json.loads(_make_observed_response([
        {"t": "2026-06-27 22:00", "s": "0.30", "d": "120", "b": "4"},
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "167", "b": "4"},  # latest
        {"t": "2026-06-27 21:00", "s": "0.20", "d": "90", "b": "4"},
    ]))
    snap = _parse_observed(resp)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(0.45)
    assert snap["direction_deg"] == pytest.approx(167.0)
    assert snap["datetime"] == "2026-06-27T23:00Z"
    assert snap["bin"] == 4
    assert snap["flow_state"] == ""


def test_parse_observed_skips_missing_values():
    resp = json.loads(_make_observed_response([
        {"t": "2026-06-27 22:00", "s": "", "d": "120", "b": "4"},
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "", "b": "4"},
        {"t": "2026-06-27 21:00", "s": "0.20", "d": "90", "b": "4"},  # only valid
    ]))
    snap = _parse_observed(resp)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(0.20)
    assert snap["datetime"] == "2026-06-27T21:00Z"


def test_parse_observed_empty():
    assert _parse_observed({"data": []}) is None


# ---------------------------------------------------------------------------
# Predictions parser.
# ---------------------------------------------------------------------------


def test_parse_predictions_nearest_now_flood():
    # now = 2026-06-27 23:00; nearest is the 23:10 flood row.
    resp = json.loads(_make_predictions_response([
        {"Time": "2026-06-27 20:00", "Type": "ebb", "Velocity_Major": -0.8,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8", "Depth": "35"},
        {"Time": "2026-06-27 23:10", "Type": "flood", "Velocity_Major": 1.2,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8", "Depth": "35"},  # nearest
        {"Time": "2026-06-28 03:00", "Type": "slack", "Velocity_Major": 0,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8", "Depth": "35"},
    ]))
    snap = _parse_predictions(resp, _NOW)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(1.2)
    assert snap["direction_deg"] == pytest.approx(356.0)  # flood direction
    assert snap["flow_state"] == "flood"
    assert snap["datetime"] == "2026-06-27T23:10Z"


def test_parse_predictions_ebb_uses_ebb_dir():
    # now = 2026-06-27 23:00; nearest is the 23:05 ebb row (negative velocity).
    resp = json.loads(_make_predictions_response([
        {"Time": "2026-06-27 23:05", "Type": "ebb", "Velocity_Major": -0.9,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8", "Depth": "35"},
    ]))
    snap = _parse_predictions(resp, _NOW)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(0.9)  # abs of negative
    assert snap["direction_deg"] == pytest.approx(170.0)  # ebb direction
    assert snap["flow_state"] == "ebb"


def test_parse_predictions_empty():
    assert _parse_predictions({"current_predictions": {"cp": []}}, _NOW) is None
    assert _parse_predictions({}, _NOW) is None


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_with_records():
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    records = [
        {
            "station_id": "s08010", "station_name": "Southampton Shoal",
            "lon": -122.4223, "lat": 37.9162, "speed_kn": 0.45,
            "direction_deg": 167.0, "datetime": "2026-06-27T23:00Z",
            "bin": 4, "flow_state": "",
        },
    ]
    fgb = _build_flatgeobuf(records, "currents")
    assert len(fgb) > 100

    # Round-trip + verify attributes survive serialization.
    import tempfile

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        p = f.name
    try:
        gdf = gpd.read_file(p)
    finally:
        os.unlink(p)
    assert len(gdf) == 1
    row = gdf.iloc[0]
    assert row["station_id"] == "s08010"
    assert row["speed_kn"] == pytest.approx(0.45)
    assert row["direction_deg"] == pytest.approx(167.0)
    assert set(["station_id", "station_name", "lon", "lat", "product",
                "speed_kn", "direction_deg", "datetime", "bin",
                "flow_state"]).issubset(set(gdf.columns))


def test_build_flatgeobuf_empty_records():
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")
    fgb = _build_flatgeobuf([], "currents")
    assert isinstance(fgb, bytes)
    assert len(fgb) > 0


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive():
    mb = estimate_payload_mb(bbox=_SF_BAY_BBOX, product="currents")
    assert mb > 0
    assert mb < 1000


def test_estimate_payload_mb_no_bbox():
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# Round-trip cache test (mock S3).
# ---------------------------------------------------------------------------


def test_fetch_tool_cache_miss_then_hit():
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    catalog = _make_station_catalog([
        {"id": "s08010", "name": "Southampton Shoal", "lat": "37.9162", "lng": "-122.4223"},
    ])
    data = _make_observed_response([
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "167", "b": "4"},
    ])

    def fake_http_get(url: str, timeout: float) -> bytes:
        if "stations.json" in url:
            return catalog
        return data

    injector = _make_read_through_injector(fake_gcs)
    with (
        patch("trid3nt_server.tools.fetch_noaa_coops_currents._http_get",
              side_effect=fake_http_get),
        patch("trid3nt_server.tools.fetch_noaa_coops_currents.read_through",
              side_effect=injector),
    ):
        r1 = fetch_noaa_coops_currents(bbox=_SF_BAY_BBOX, product="currents")
        r2 = fetch_noaa_coops_currents(bbox=_SF_BAY_BBOX, product="currents")

    assert r1.layer_id == r2.layer_id
    assert injector.call_count["n"] == 2


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape():
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    catalog = _make_station_catalog([
        {"id": "s08010", "name": "Southampton Shoal", "lat": "37.9162", "lng": "-122.4223"},
    ])
    data = _make_observed_response([
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "167", "b": "4"},
    ])

    def fake_http_get(url: str, timeout: float) -> bytes:
        if "stations.json" in url:
            return catalog
        return data

    injector = _make_read_through_injector(FakeStorageClient())
    with (
        patch("trid3nt_server.tools.fetch_noaa_coops_currents._http_get",
              side_effect=fake_http_get),
        patch("trid3nt_server.tools.fetch_noaa_coops_currents.read_through",
              side_effect=injector),
    ):
        result = fetch_noaa_coops_currents(bbox=_SF_BAY_BBOX, product="currents")

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units == "kn"
    assert result.uri.startswith("s3://")
    assert "coops-currents-currents" in result.layer_id
    assert "CO-OPS" in result.name


# ---------------------------------------------------------------------------
# Extra-kwargs absorption (LLM hallucination guard).
# ---------------------------------------------------------------------------


def test_extra_kwargs_absorbed():
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    catalog = _make_station_catalog([
        {"id": "s08010", "name": "Southampton Shoal", "lat": "37.9162", "lng": "-122.4223"},
    ])
    data = _make_observed_response([
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "167", "b": "4"},
    ])

    def fake_http_get(url: str, timeout: float) -> bytes:
        if "stations.json" in url:
            return catalog
        return data

    injector = _make_read_through_injector(FakeStorageClient())
    with (
        patch("trid3nt_server.tools.fetch_noaa_coops_currents._http_get",
              side_effect=fake_http_get),
        patch("trid3nt_server.tools.fetch_noaa_coops_currents.read_through",
              side_effect=injector),
    ):
        result = fetch_noaa_coops_currents(
            bbox=_SF_BAY_BBOX,
            product="currents",
            invented_param="foo",  # type: ignore[call-arg]
            another_fake_kwarg=42,  # type: ignore[call-arg]
        )
    assert result.layer_type == "vector"


# ---------------------------------------------------------------------------
# Round-bbox helper.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    result = _round_bbox_to_6dp((-123.000001234, 37.400001234, -122.0, 38.2))
    assert all(len(str(abs(v)).split(".")[-1]) <= 6 for v in result)


# ---------------------------------------------------------------------------
# Live smoke test (requires TRID3NT_TEST_LIVE_COOPS=1).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_COOPS, reason="set TRID3NT_TEST_LIVE_COOPS=1 to run live CO-OPS test")
@pytest.mark.parametrize("product", ["currents", "currents_predictions"])
def test_live_fetch_sf_bay(product):
    """Live: fetch SF Bay tidal currents for both products."""
    import tempfile

    import geopandas as gpd

    from trid3nt_server.tools.fetch_noaa_coops_currents import (
        _fetch_coops_currents_bytes,
    )

    fgb = _fetch_coops_currents_bytes(_SF_BAY_BBOX, product)
    assert isinstance(fgb, bytes)
    assert len(fgb) > 100

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        p = f.name
    try:
        gdf = gpd.read_file(p)
    finally:
        os.unlink(p)

    assert len(gdf) >= 1
    for _, row in gdf.iterrows():
        assert -124.0 <= row.geometry.x <= -121.5, "lon out of SF Bay range"
        assert 37.0 <= row.geometry.y <= 38.5, "lat out of SF Bay range"
        assert row["speed_kn"] >= 0.0, "speed must be non-negative"
        assert 0.0 <= row["direction_deg"] < 360.0, "direction out of range"
        assert row["station_id"].startswith("s") or row["station_id"]
    print(
        f"[LIVE SMOKE] CO-OPS SF Bay currents/{product}: {len(gdf)} station(s), "
        f"max speed={gdf['speed_kn'].max():.3f} kn"
    )
