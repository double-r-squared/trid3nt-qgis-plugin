"""Tests for ``fetch_airnow_air_quality`` (EPA AirNow current AQI observations).

Covers (per the tools-session GATE):
- registration in TOOL_REGISTRY + metadata invariants
- input validation (None bbox = input error, degenerate bbox, out-of-range,
  unknown parameter, parameter aliases/defaults)
- the SECRET-loader 3-path resolution + the honest missing-key degrade
  (NO public mirror -> AirNowMissingKeyError, credential-shaped)
- the auth-rejection path (HTTP 401 / WebServiceError -> AirNowAuthError)
- synthetic correctness of the FGB conversion (dedup latest-per-monitor,
  derived columns, geometry/crs) + honest-empty
- the end-to-end fetch via a patched httpx.Client (success + empty + upstream)

No real AirNow key is required: the missing-key + auth paths are the gated
behaviours, and the success path is exercised with a patched HTTP client.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import geopandas as gpd
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_airnow_air_quality import (
    AQI_CATEGORY_NAMES,
    PRESERVED_PROPERTIES,
    VALID_PARAMETERS,
    AirNowAuthError,
    AirNowInputError,
    AirNowMissingKeyError,
    AirNowUpstreamError,
    _aqi_category_name,
    _build_airnow_url,
    _current_hour_window,
    _fetch_airnow_json,
    _records_to_fgb,
    _resolve_api_key,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_parameters,
    estimate_payload_mb,
    fetch_airnow_air_quality,
    set_persistence_for_secrets,
)

_LA_BBOX = (-118.7, 33.7, -117.6, 34.3)


# ---------------------------------------------------------------------------
# httpx.Client fakes (mirror test_fetch_ebird_observations).
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class _MockHTTPClient:
    def __init__(self, response: _FakeHTTPResponse) -> None:
        self._response = response
        self.get_calls: list[dict[str, Any]] = []

    def __enter__(self) -> "_MockHTTPClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeHTTPResponse:
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        return self._response


def _patch_httpx(response: _FakeHTTPResponse):
    client = _MockHTTPClient(response)
    return patch("httpx.Client", return_value=client), client


def _obs(
    lat: float,
    lon: float,
    utc: str,
    parameter: str = "PM25",
    aqi: int = 51,
    category: int = 2,
    value: float = 12.3,
) -> dict[str, Any]:
    return {
        "Latitude": lat,
        "Longitude": lon,
        "UTC": utc,
        "Parameter": parameter,
        "Unit": "UG/M3" if parameter in ("PM25", "PM10") else "PPB",
        "Value": value,
        "RawConcentration": value - 0.5,
        "AQI": aqi,
        "Category": category,
        "SiteName": "Test Site",
        "AgencyName": "Test AQMD",
        "FullAQSCode": "060371103",
        "IntlAQSCode": "840060371103",
    }


# ---------------------------------------------------------------------------
# Registration + metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_airnow_air_quality" in TOOL_REGISTRY


def test_metadata_invariants():
    from trid3nt_server.tools.fetch_airnow_air_quality import _METADATA

    assert _METADATA.name == "fetch_airnow_air_quality"
    assert _METADATA.ttl_class == "dynamic-1h"
    assert _METADATA.source_class == "airnow_air_quality"
    assert _METADATA.cacheable is True
    # AirNow requires a bbox -> NOT a global-sweep tool.
    assert _METADATA.supports_global_query is False
    assert _METADATA.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Typed-error retryability contract.
# ---------------------------------------------------------------------------


def test_input_error_not_retryable():
    assert AirNowInputError("x").retryable is False
    assert AirNowInputError("x").error_code == "AIRNOW_INPUT_ERROR"


def test_upstream_error_retryable():
    assert AirNowUpstreamError("x").retryable is True
    assert AirNowUpstreamError("x").error_code == "AIRNOW_UPSTREAM_ERROR"


def test_missing_key_error_not_retryable():
    assert AirNowMissingKeyError("x").retryable is False
    assert AirNowMissingKeyError("x").error_code == "AIRNOW_MISSING_KEY"


def test_auth_error_not_retryable():
    assert AirNowAuthError("x").retryable is False
    assert AirNowAuthError("x").error_code == "AIRNOW_AUTH_ERROR"


def test_missing_key_error_is_credential_shaped():
    """The generic credential pipeline must recognise the missing-key error
    by suffix (no per-provider registry entry required)."""
    from trid3nt_server import credential_registry as cr

    assert cr.is_credential_shaped_error(
        "fetch_airnow_air_quality", AirNowMissingKeyError("x")
    )
    assert cr.is_credential_shaped_error(
        "fetch_airnow_air_quality", AirNowAuthError("x")
    )


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_none_bbox_is_input_error_not_global_sweep():
    with pytest.raises(AirNowInputError):
        _validate_bbox(None)


def test_degenerate_bbox_raises():
    with pytest.raises(AirNowInputError):
        _validate_bbox((-118.0, 34.0, -118.0, 34.0))


def test_bbox_min_ge_max_raises():
    with pytest.raises(AirNowInputError):
        _validate_bbox((-117.0, 34.0, -118.0, 35.0))


def test_bbox_lon_out_of_range_raises():
    with pytest.raises(AirNowInputError):
        _validate_bbox((-181.0, 33.0, -117.0, 34.0))


def test_bbox_lat_out_of_range_raises():
    with pytest.raises(AirNowInputError):
        _validate_bbox((-118.0, -91.0, -117.0, 34.0))


def test_bbox_wrong_arity_raises():
    with pytest.raises(AirNowInputError):
        _validate_bbox((-118.0, 33.0, -117.0))


def test_bbox_non_numeric_raises():
    with pytest.raises(AirNowInputError):
        _validate_bbox((-118.0, "south", -117.0, 34.0))


def test_valid_bbox_returns_floats():
    out = _validate_bbox(_LA_BBOX)
    assert out == _LA_BBOX
    assert all(isinstance(v, float) for v in out)


def test_round_bbox_to_6dp():
    assert _round_bbox_to_6dp((-118.1234567, 33.7654321, -117.6, 34.3)) == (
        -118.123457,
        33.765432,
        -117.6,
        34.3,
    )


# ---------------------------------------------------------------------------
# Parameter normalization.
# ---------------------------------------------------------------------------


def test_parameters_default_to_primaries():
    assert _validate_parameters(None) == ["PM25", "OZONE", "PM10"]


def test_parameters_aliases_and_dedup():
    assert _validate_parameters(["pm2.5", "o3", "PM10", "pm25"]) == [
        "PM25",
        "OZONE",
        "PM10",
    ]


def test_parameters_single_string():
    assert _validate_parameters("PM25") == ["PM25"]


def test_parameters_unknown_raises():
    with pytest.raises(AirNowInputError):
        _validate_parameters("plutonium")


def test_parameters_non_str_entry_raises():
    with pytest.raises(AirNowInputError):
        _validate_parameters([123])


def test_parameters_bad_type_raises():
    with pytest.raises(AirNowInputError):
        _validate_parameters(42)


def test_all_valid_parameter_tokens_resolvable():
    for token in set(VALID_PARAMETERS.values()):
        assert _validate_parameters(token) == [token]


# ---------------------------------------------------------------------------
# AQI category mapping.
# ---------------------------------------------------------------------------


def test_aqi_category_names():
    assert _aqi_category_name(1) == "Good"
    assert _aqi_category_name(2) == "Moderate"
    assert _aqi_category_name(4) == "Unhealthy"
    assert _aqi_category_name(6) == "Hazardous"
    # unknown / garbage -> Unavailable (honest, never crashes)
    assert _aqi_category_name(99) == "Unavailable"
    assert _aqi_category_name(None) == "Unavailable"
    assert _aqi_category_name("x") == "Unavailable"


def test_all_aqi_categories_have_names():
    for cat in (1, 2, 3, 4, 5, 6, 7):
        assert cat in AQI_CATEGORY_NAMES


# ---------------------------------------------------------------------------
# Secret-loader 3-path resolution.
# ---------------------------------------------------------------------------


def test_resolve_api_key_explicit_kwarg_wins(monkeypatch):
    monkeypatch.setenv("TRID3NT_AIRNOW_API_KEY", "ENVKEY")
    assert _resolve_api_key("KWARGKEY", None) == "KWARGKEY"


def test_resolve_api_key_secret_ref_str_shortcut():
    assert _resolve_api_key(None, "SECRETKEY") == "SECRETKEY"


def test_resolve_api_key_env_fallback(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)
    monkeypatch.setenv("TRID3NT_AIRNOW_API_KEY", "ENVKEY")
    assert _resolve_api_key(None, None) == "ENVKEY"


def test_resolve_api_key_no_path_raises_missing_key(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)
    with pytest.raises(AirNowMissingKeyError):
        _resolve_api_key(None, None)


def test_resolve_api_key_via_persistence_secret_ref(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)

    class FakePersistence:
        async def get_secret_value(self, ref: Any) -> str:
            return "KEY-FROM-VAULT"

    set_persistence_for_secrets(FakePersistence())
    try:

        class Ref:
            pass

        assert _resolve_api_key(None, Ref()) == "KEY-FROM-VAULT"
    finally:
        set_persistence_for_secrets(None)


def test_resolve_api_key_persistence_failure_raises_missing_key(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)

    class BoomPersistence:
        async def get_secret_value(self, ref: Any) -> str:
            raise RuntimeError("vault down")

    set_persistence_for_secrets(BoomPersistence())
    try:

        class Ref:
            pass

        with pytest.raises(AirNowMissingKeyError):
            _resolve_api_key(None, Ref())
    finally:
        set_persistence_for_secrets(None)


# ---------------------------------------------------------------------------
# Honest no-key degrade through the public entrypoint (NO public mirror).
# ---------------------------------------------------------------------------


def test_no_key_raises_missing_key_pre_network(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)
    # No httpx patch -> if it tried the network the test would still pass via
    # the pre-network raise; the point is it NEVER fabricates a layer.
    with pytest.raises(AirNowMissingKeyError):
        fetch_airnow_air_quality(bbox=_LA_BBOX, parameters="PM25")


def test_none_bbox_raises_input_error_even_with_key():
    with pytest.raises(AirNowInputError):
        fetch_airnow_air_quality(bbox=None, api_key="dummy")


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def test_build_url_shape():
    url, params = _build_airnow_url(
        _LA_BBOX,
        ["PM25", "OZONE"],
        "MYKEY",
        start_date="2026-06-27T20",
        end_date="2026-06-27T23",
        monitor_type=0,
    )
    assert url.endswith("/aq/data/")
    assert params["BBOX"] == "-118.7,33.7,-117.6,34.3"
    assert params["parameters"] == "PM25,OZONE"
    assert params["dataType"] == "B"
    assert params["format"] == "application/json"
    assert params["verbose"] == "1"
    assert params["includerawconcentrations"] == "1"
    assert params["API_KEY"] == "MYKEY"
    assert params["startDate"] == "2026-06-27T20"
    assert params["endDate"] == "2026-06-27T23"


def test_current_hour_window_is_floored_and_ordered():
    now = datetime(2026, 6, 27, 23, 47, 12, tzinfo=timezone.utc)
    start, end = _current_hour_window(now)
    assert end == "2026-06-27T23"
    # start is _WINDOW_HOURS before end, floored to the hour
    assert start == "2026-06-27T20"


# ---------------------------------------------------------------------------
# HTTP fetch: auth, upstream, success/empty.
# ---------------------------------------------------------------------------


def test_fetch_json_http_401_raises_auth_error():
    resp = _FakeHTTPResponse(
        401,
        text='{"WebServiceError":[{"Message":"Request not authenticated."}]}',
    )
    p, _ = _patch_httpx(resp)
    with p:
        with pytest.raises(AirNowAuthError) as ei:
            _fetch_airnow_json("http://x/aq/data/", {"API_KEY": "bad"})
    assert ei.value.error_code == "AIRNOW_AUTH_ERROR"


def test_fetch_json_webservice_error_auth_envelope_raises_auth():
    resp = _FakeHTTPResponse(
        200,
        payload={"WebServiceError": [{"Message": "Invalid API_KEY"}]},
    )
    p, _ = _patch_httpx(resp)
    with p:
        with pytest.raises(AirNowAuthError):
            _fetch_airnow_json("http://x/aq/data/", {"API_KEY": "bad"})


def test_fetch_json_http_500_raises_upstream():
    resp = _FakeHTTPResponse(500, text="server boom")
    p, _ = _patch_httpx(resp)
    with p:
        with pytest.raises(AirNowUpstreamError):
            _fetch_airnow_json("http://x/aq/data/", {"API_KEY": "k"})


def test_fetch_json_non_list_body_raises_upstream():
    resp = _FakeHTTPResponse(200, payload={"unexpected": "object"})
    p, _ = _patch_httpx(resp)
    with p:
        with pytest.raises(AirNowUpstreamError):
            _fetch_airnow_json("http://x/aq/data/", {"API_KEY": "k"})


def test_fetch_json_success_returns_records():
    payload = [_obs(34.1, -118.2, "2026-06-27T23:00")]
    resp = _FakeHTTPResponse(200, payload=payload)
    p, client = _patch_httpx(resp)
    with p:
        out = _fetch_airnow_json("http://x/aq/data/", {"API_KEY": "k"})
    assert out == payload
    # API_KEY must NOT be logged in cleartext is handled in code; here we just
    # confirm the call went through with our params.
    assert client.get_calls[0]["params"]["API_KEY"] == "k"


def test_fetch_json_empty_list_is_legitimate():
    resp = _FakeHTTPResponse(200, payload=[])
    p, _ = _patch_httpx(resp)
    with p:
        out = _fetch_airnow_json("http://x/aq/data/", {"API_KEY": "k"})
    assert out == []


# ---------------------------------------------------------------------------
# FGB conversion: dedup, derived columns, geometry, honest-empty.
# ---------------------------------------------------------------------------


def _read_fgb(fgb: bytes) -> gpd.GeoDataFrame:
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        path = f.name
    try:
        return gpd.read_file(path)
    finally:
        os.unlink(path)


def test_records_to_fgb_dedup_keeps_latest_per_monitor_param():
    recs = [
        _obs(34.10, -118.20, "2026-06-27T22:00", "PM25", aqi=51, value=12.3),
        _obs(34.10, -118.20, "2026-06-27T23:00", "PM25", aqi=64, value=18.0),
        _obs(34.05, -118.45, "2026-06-27T23:00", "OZONE", aqi=37, category=1),
    ]
    gdf = _read_fgb(_records_to_fgb(recs))
    # 2 distinct (monitor, parameter) rows after dedup
    assert len(gdf) == 2
    pm = gdf[gdf["Parameter"] == "PM25"].iloc[0]
    assert pm["UTC"] == "2026-06-27T23:00"
    assert int(pm["AQI"]) == 64


def test_records_to_fgb_derived_columns():
    recs = [_obs(34.1, -118.2, "2026-06-27T23:00", "PM25", category=2)]
    gdf = _read_fgb(_records_to_fgb(recs))
    row = gdf.iloc[0]
    assert row["ParameterName"] == "PM2.5 (fine particulate matter)"
    assert row["AQICategoryName"] == "Moderate"
    for col in PRESERVED_PROPERTIES:
        assert col in gdf.columns


def test_records_to_fgb_geometry_and_crs():
    recs = [_obs(34.1, -118.2, "2026-06-27T23:00")]
    gdf = _read_fgb(_records_to_fgb(recs))
    assert gdf.crs is not None
    assert gdf.crs.to_epsg() == 4326
    assert set(gdf.geometry.geom_type.unique()) == {"Point"}
    # lon/lat order is correct (Point(lon, lat))
    pt = gdf.geometry.iloc[0]
    assert abs(pt.x - (-118.2)) < 1e-6
    assert abs(pt.y - 34.1) < 1e-6


def test_records_to_fgb_drops_rows_without_coords():
    recs = [
        _obs(34.1, -118.2, "2026-06-27T23:00"),
        {"Latitude": None, "Longitude": -118.0, "UTC": "x", "Parameter": "PM25"},
        {"Latitude": "bad", "Longitude": -118.0, "UTC": "x", "Parameter": "PM25"},
    ]
    gdf = _read_fgb(_records_to_fgb(recs))
    assert len(gdf) == 1


def test_records_to_fgb_empty_is_valid_header_only():
    gdf = _read_fgb(_records_to_fgb([]))
    assert len(gdf) == 0


# ---------------------------------------------------------------------------
# Payload estimator (advisory, never raises).
# ---------------------------------------------------------------------------


def test_estimate_payload_scales_with_bbox():
    small = estimate_payload_mb(bbox=(-118.3, 34.0, -118.1, 34.2))
    big = estimate_payload_mb(bbox=(-125.0, 24.0, -65.0, 50.0))
    assert big > small > 0


def test_estimate_payload_none_bbox_nominal():
    assert estimate_payload_mb(bbox=None) >= 0.0
    assert estimate_payload_mb() >= 0.0


# ---------------------------------------------------------------------------
# End-to-end success through the public entrypoint (patched HTTP + cache).
# ---------------------------------------------------------------------------


def test_end_to_end_success_returns_layer_uri(monkeypatch):
    """Full path: key -> live(patched) fetch -> FGB -> LayerURI.

    The read_through cache write is best-effort (degrades to uncached on S3
    failure), so this works without AWS creds: it returns the fresh bytes +
    an s3:// uri either way.
    """
    monkeypatch.setenv("TRID3NT_AIRNOW_API_KEY", "TESTKEY")
    payload = [
        _obs(34.10, -118.20, "2026-06-27T23:00", "PM25", aqi=64),
        _obs(34.05, -118.45, "2026-06-27T23:00", "OZONE", aqi=37, category=1),
    ]
    resp = _FakeHTTPResponse(200, payload=payload)
    p, _ = _patch_httpx(resp)
    with p:
        layer = fetch_airnow_air_quality(bbox=_LA_BBOX, parameters=["PM25", "OZONE"])
    assert layer.layer_type == "vector"
    assert layer.role == "primary"
    assert layer.units == "AQI"
    assert layer.style_preset == "airnow_air_quality"
    assert layer.uri.startswith("s3://")
    assert "airnow-aq" in layer.layer_id


def test_end_to_end_auth_failure_surfaces_credential_error(monkeypatch):
    monkeypatch.setenv("TRID3NT_AIRNOW_API_KEY", "BADKEY")
    resp = _FakeHTTPResponse(
        401,
        text='{"WebServiceError":[{"Message":"Request not authenticated."}]}',
    )
    p, _ = _patch_httpx(resp)
    with p:
        with pytest.raises(AirNowAuthError):
            fetch_airnow_air_quality(bbox=_LA_BBOX, parameters="PM25")
