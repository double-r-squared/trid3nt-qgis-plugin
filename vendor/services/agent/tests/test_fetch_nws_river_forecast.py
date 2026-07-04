"""Unit tests for the ``fetch_nws_river_forecast`` atomic tool.

Fetches NWS / National Water Prediction Service (AHPS/NWPS) river-forecast
gauges within a bbox and emits one Point feature per gauge carrying observed +
forecast river stage and the NWS flood category (no_flood/action/minor/moderate/
major). All HTTP is mocked - no live network - except the opt-in live test.

Coverage:
- Error classes carry correct retryable + error_code attributes.
- Input validation: missing bbox, malformed bbox, degenerate bbox, too-large bbox.
- Flood-category normalization (no_flooding -> no_flood; others verbatim).
- URL builders (bbox list includes srid=EPSG_4326; detail url quotes the lid).
- Gauges-JSON parser: flattens observed+forecast status into point records;
  drops coordinate-less / lid-less gauges; maps -9999 sentinels to None.
- Threshold parser: pulls action/minor/moderate/major stages; None on miss.
- Happy path (mocked HTTP + read_through stub): one Point feature per gauge,
  FlatGeobuf round-trips with the expected columns and values.
- include_thresholds enrichment joins per-gauge threshold stages.
- Honest empty -> NwsRiverForecastNoGaugesError (never an empty success layer).
- LayerURI shape: layer_type="vector", role="primary", style_preset, bbox set.
- Payload estimator returns a positive float.

Live test (gated by GRACE2_TEST_LIVE_NWPS=1): real NWPS request for the
Cedar Rapids, IA bbox; confirms >=1 gauge with a flood_category.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from grace2_agent.tools.fetch_nws_river_forecast import (
    GAUGE_DETAIL_URL,
    GAUGES_URL,
    NwsRiverForecastBboxTooLargeError,
    NwsRiverForecastError,
    NwsRiverForecastInputError,
    NwsRiverForecastNoGaugesError,
    NwsRiverForecastUpstreamError,
    _build_flatgeobuf,
    _build_gauge_detail_url,
    _build_gauges_url,
    _normalize_flood_category,
    _parse_gauge_thresholds,
    _parse_gauges_json,
    _records_bbox,
    _validate_bbox,
    estimate_payload_mb,
    fetch_nws_river_forecast,
)


# ---------------------------------------------------------------------------
# Fixtures - synthetic NWPS bodies.
# ---------------------------------------------------------------------------

# Two valid gauges (one with forecast, one without) + one coordinate-less gauge
# (must be dropped) + one lid-less gauge (must be dropped).
_GAUGES_BODY = json.dumps(
    {
        "gauges": [
            {
                "lid": "CIDI4",
                "usgsId": "05464500",
                "name": "Cedar River at Cedar Rapids",
                "latitude": 41.9719,
                "longitude": -91.6669,
                "rfc": {"abbreviation": "NCRFC"},
                "wfo": {"abbreviation": "DVN"},
                "state": {"abbreviation": "IA"},
                "status": {
                    "observed": {
                        "primary": 4.73,
                        "primaryUnit": "ft",
                        "secondary": 4.56,
                        "secondaryUnit": "kcfs",
                        "floodCategory": "minor",
                        "validTime": "2026-06-27T22:00:00Z",
                    },
                    "forecast": {
                        "primary": 12.4,
                        "primaryUnit": "ft",
                        "secondary": 9.9,
                        "floodCategory": "moderate",
                        "validTime": "2026-06-28T00:00:00Z",
                    },
                },
            },
            {
                "lid": "OXFI4",
                "usgsId": "",
                "name": "Clear Creek near Oxford",
                "latitude": 41.7183,
                "longitude": -91.7401,
                "rfc": {"abbreviation": "NCRFC"},
                "wfo": {"abbreviation": "DVN"},
                "state": {"abbreviation": "IA"},
                "status": {
                    "observed": {
                        "primary": 3.11,
                        "primaryUnit": "ft",
                        "secondary": -9999.0,
                        "floodCategory": "no_flooding",
                        "validTime": "2026-06-27T22:00:00Z",
                    },
                    "forecast": {
                        "primary": -999.0,
                        "floodCategory": "fcst_not_current",
                        "validTime": "0001-01-01T00:00:00Z",
                    },
                },
            },
            {  # no coordinate -> dropped
                "lid": "NOGEO1",
                "name": "Ghost gauge",
                "latitude": None,
                "longitude": None,
                "status": {},
            },
            {  # no lid -> dropped
                "lid": "",
                "name": "Nameless",
                "latitude": 40.0,
                "longitude": -90.0,
                "status": {},
            },
        ]
    }
).encode("utf-8")

_EMPTY_BODY = json.dumps({"gauges": []}).encode("utf-8")

_DETAIL_BODY_CIDI4 = json.dumps(
    {
        "lid": "CIDI4",
        "flood": {
            "stageUnits": "ft",
            "flowUnits": "cfs",
            "categories": {
                "action": {"stage": 10, "flow": -9999},
                "minor": {"stage": 12, "flow": -9999},
                "moderate": {"stage": 14, "flow": -9999},
                "major": {"stage": 16, "flow": -9999},
            },
        },
    }
).encode("utf-8")


# ---------------------------------------------------------------------------
# Error-class attributes.
# ---------------------------------------------------------------------------


def test_error_classes_have_codes_and_retryable() -> None:
    assert NwsRiverForecastInputError.error_code == "NWS_RIVER_FORECAST_INPUT_ERROR"
    assert NwsRiverForecastInputError.retryable is False
    assert (
        NwsRiverForecastBboxTooLargeError.error_code
        == "NWS_RIVER_FORECAST_BBOX_TOO_LARGE"
    )
    assert NwsRiverForecastBboxTooLargeError.retryable is False
    assert NwsRiverForecastNoGaugesError.error_code == "NWS_RIVER_FORECAST_NO_GAUGES"
    assert NwsRiverForecastNoGaugesError.retryable is False
    assert NwsRiverForecastUpstreamError.retryable is True
    # Subclass hierarchy: input/too-large/no-gauges/upstream all derive from base.
    for cls in (
        NwsRiverForecastInputError,
        NwsRiverForecastBboxTooLargeError,
        NwsRiverForecastNoGaugesError,
        NwsRiverForecastUpstreamError,
    ):
        assert issubclass(cls, NwsRiverForecastError)
    assert issubclass(NwsRiverForecastBboxTooLargeError, NwsRiverForecastInputError)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_validate_bbox_rejects_bad_shapes() -> None:
    with pytest.raises(NwsRiverForecastInputError):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]
    with pytest.raises(NwsRiverForecastInputError):
        _validate_bbox((float("nan"), 2.0, 3.0, 4.0))
    with pytest.raises(NwsRiverForecastInputError):
        _validate_bbox((10.0, 10.0, 5.0, 20.0))  # west >= east
    with pytest.raises(NwsRiverForecastInputError):
        _validate_bbox((-200.0, 10.0, -100.0, 20.0))  # lon out of range


def test_fetch_requires_bbox() -> None:
    with pytest.raises(NwsRiverForecastInputError):
        fetch_nws_river_forecast(bbox=None)


def test_fetch_rejects_non_numeric_bbox() -> None:
    with pytest.raises(NwsRiverForecastInputError):
        fetch_nws_river_forecast(bbox=("a", "b", "c", "d"))  # type: ignore[arg-type]


def test_fetch_rejects_too_large_bbox() -> None:
    with pytest.raises(NwsRiverForecastBboxTooLargeError):
        fetch_nws_river_forecast(bbox=(-179.0, -89.0, 179.0, 89.0))


# ---------------------------------------------------------------------------
# Flood-category normalization.
# ---------------------------------------------------------------------------


def test_normalize_flood_category() -> None:
    assert _normalize_flood_category("no_flooding") == "no_flood"
    # action/minor/moderate/major are already canonical.
    for c in ("action", "minor", "moderate", "major"):
        assert _normalize_flood_category(c) == c
    # operational states pass through verbatim.
    assert _normalize_flood_category("out_of_service") == "out_of_service"
    assert _normalize_flood_category("not_defined") == "not_defined"
    assert _normalize_flood_category(None) == ""


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def test_build_gauges_url_includes_srid_and_bbox() -> None:
    url = _build_gauges_url((-92.0, 41.7, -91.4, 42.1))
    assert url.startswith(GAUGES_URL + "?")
    assert "srid=EPSG_4326" in url
    assert "bbox.xmin=-92.0" in url
    assert "bbox.ymin=41.7" in url
    assert "bbox.xmax=-91.4" in url
    assert "bbox.ymax=42.1" in url


def test_build_gauge_detail_url_quotes_lid() -> None:
    url = _build_gauge_detail_url("CIDI4")
    assert url == GAUGE_DETAIL_URL + "CIDI4"


# ---------------------------------------------------------------------------
# Parsers.
# ---------------------------------------------------------------------------


def test_parse_gauges_json_flattens_status_and_drops_invalid() -> None:
    recs = _parse_gauges_json(_GAUGES_BODY)
    # 2 valid; the no-geo + no-lid gauges are dropped.
    assert len(recs) == 2
    by_lid = {r["lid"]: r for r in recs}
    assert set(by_lid) == {"CIDI4", "OXFI4"}

    cid = by_lid["CIDI4"]
    assert cid["usgs_id"] == "05464500"
    assert cid["name"] == "Cedar River at Cedar Rapids"
    assert cid["rfc"] == "NCRFC"
    assert cid["wfo"] == "DVN"
    assert cid["state"] == "IA"
    assert cid["flood_category"] == "minor"
    assert cid["obs_stage_ft"] == pytest.approx(4.73)
    assert cid["obs_flow_kcfs"] == pytest.approx(4.56)
    assert cid["fcst_flood_category"] == "moderate"
    assert cid["fcst_stage_ft"] == pytest.approx(12.4)
    assert cid["fcst_valid_time"] == "2026-06-28T00:00:00Z"
    # thresholds default to None (no enrichment in list mode)
    assert cid["action_stage_ft"] is None

    oxf = by_lid["OXFI4"]
    # no_flooding normalized -> no_flood
    assert oxf["flood_category"] == "no_flood"
    # -9999 secondary -> None
    assert oxf["obs_flow_kcfs"] is None
    # -999 forecast primary -> None
    assert oxf["fcst_stage_ft"] is None
    # operational forecast state passes through verbatim
    assert oxf["fcst_flood_category"] == "fcst_not_current"


def test_parse_gauges_json_empty_body() -> None:
    assert _parse_gauges_json(b"") == []
    assert _parse_gauges_json(_EMPTY_BODY) == []


def test_parse_gauges_json_bad_json_raises_upstream() -> None:
    with pytest.raises(NwsRiverForecastUpstreamError):
        _parse_gauges_json(b"not json{{{")


def test_parse_gauge_thresholds() -> None:
    out = _parse_gauge_thresholds(_DETAIL_BODY_CIDI4)
    assert out["action_stage_ft"] == pytest.approx(10.0)
    assert out["minor_stage_ft"] == pytest.approx(12.0)
    assert out["moderate_stage_ft"] == pytest.approx(14.0)
    assert out["major_stage_ft"] == pytest.approx(16.0)
    # empty body -> all None
    none_out = _parse_gauge_thresholds(b"")
    assert all(v is None for v in none_out.values())


# ---------------------------------------------------------------------------
# FlatGeobuf builder round-trip.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_roundtrip() -> None:
    gpd = pytest.importorskip("geopandas")
    recs = _parse_gauges_json(_GAUGES_BODY)
    fgb = _build_flatgeobuf(recs)
    assert isinstance(fgb, bytes) and len(fgb) > 0

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
    try:
        with open(path, "wb") as f:
            f.write(fgb)
        gdf = gpd.read_file(path)
        assert len(gdf) == 2
        for col in (
            "lid",
            "name",
            "flood_category",
            "fcst_flood_category",
            "obs_stage_ft",
            "fcst_stage_ft",
            "action_stage_ft",
        ):
            assert col in gdf.columns
        # geometry is points
        assert set(gdf.geometry.geom_type) == {"Point"}
        cid = gdf[gdf["lid"] == "CIDI4"].iloc[0]
        assert cid["flood_category"] == "minor"
        assert float(cid["obs_stage_ft"]) == pytest.approx(4.73)
    finally:
        os.unlink(path)


def test_records_bbox_pads_single_point() -> None:
    bb = _records_bbox([{"lon": -91.0, "lat": 42.0}])
    assert bb is not None
    w, s, e, n = bb
    assert w < -91.0 < e and s < 42.0 < n
    assert _records_bbox([]) is None


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive() -> None:
    assert estimate_payload_mb(bbox=(-92.0, 41.7, -91.4, 42.1)) > 0.0
    assert estimate_payload_mb(bbox=None) > 0.0


# ---------------------------------------------------------------------------
# End-to-end happy path with HTTP + read_through stubbed.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.data = b""
        self.hit = False


def _stub_read_through(*, metadata: Any, params: Any, ext: str, fetch_fn: Any) -> Any:
    # Invoke the real fetch_fn so the capture/extent path is exercised, but
    # return a synthetic S3 uri instead of touching S3.
    fetch_fn()
    return _FakeResult(f"s3://test-bucket/cache/{ext}/stub.fgb")


def test_fetch_happy_path_layeruri_shape() -> None:
    pytest.importorskip("geopandas")
    with patch(
        "grace2_agent.tools.fetch_nws_river_forecast._http_get",
        return_value=_GAUGES_BODY,
    ), patch(
        "grace2_agent.tools.fetch_nws_river_forecast.read_through",
        _stub_read_through,
    ):
        uri = fetch_nws_river_forecast(bbox=(-92.0, 41.7, -91.4, 42.1))
    assert uri.layer_type == "vector"
    assert uri.role == "primary"
    assert uri.style_preset == "nws_river_gauges"
    assert uri.uri == "s3://test-bucket/cache/fgb/stub.fgb"
    assert uri.bbox is not None
    # extent is inside the requested bbox (derived from the 2 returned gauges)
    w, s, e, n = uri.bbox
    assert -92.0 <= w <= e <= -91.4 + 1e-6
    assert 41.7 <= s <= n <= 42.1 + 1e-6


def test_fetch_include_thresholds_enriches() -> None:
    pytest.importorskip("geopandas")
    captured_records: dict[str, Any] = {}

    def _http_get_router(url: str, timeout: float = 60.0) -> bytes:
        if url.startswith(GAUGE_DETAIL_URL):
            return _DETAIL_BODY_CIDI4 if url.endswith("CIDI4") else b""
        return _GAUGES_BODY

    # Spy on _build_flatgeobuf to capture the enriched records.
    import grace2_agent.tools.fetch_nws_river_forecast as mod

    orig_build = mod._build_flatgeobuf

    def _spy_build(records: Any) -> bytes:
        captured_records["recs"] = records
        return orig_build(records)

    with patch.object(mod, "_http_get", side_effect=_http_get_router), patch.object(
        mod, "read_through", _stub_read_through
    ), patch.object(mod, "_build_flatgeobuf", _spy_build):
        fetch_nws_river_forecast(
            bbox=(-92.0, 41.7, -91.4, 42.1), include_thresholds=True
        )

    recs = {r["lid"]: r for r in captured_records["recs"]}
    assert recs["CIDI4"]["action_stage_ft"] == pytest.approx(10.0)
    assert recs["CIDI4"]["major_stage_ft"] == pytest.approx(16.0)
    # OXFI4 detail returns empty body -> thresholds stay None
    assert recs["OXFI4"]["action_stage_ft"] is None


def test_fetch_honest_empty_raises_no_gauges() -> None:
    with patch(
        "grace2_agent.tools.fetch_nws_river_forecast._http_get",
        return_value=_EMPTY_BODY,
    ), patch(
        "grace2_agent.tools.fetch_nws_river_forecast.read_through",
        _stub_read_through,
    ):
        with pytest.raises(NwsRiverForecastNoGaugesError):
            fetch_nws_river_forecast(bbox=(-40.0, -20.0, -39.0, -19.0))


# ---------------------------------------------------------------------------
# Opt-in live test.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("GRACE2_TEST_LIVE_NWPS") != "1",
    reason="set GRACE2_TEST_LIVE_NWPS=1 to run the live NWPS request",
)
def test_live_nwps_cedar_rapids() -> None:
    pytest.importorskip("geopandas")
    recs = _parse_gauges_json(
        __import__(
            "grace2_agent.tools.fetch_nws_river_forecast", fromlist=["_http_get"]
        )._http_get(_build_gauges_url((-92.0, 41.7, -91.4, 42.1)))
    )
    assert len(recs) >= 1
    assert any(r.get("flood_category") for r in recs)
