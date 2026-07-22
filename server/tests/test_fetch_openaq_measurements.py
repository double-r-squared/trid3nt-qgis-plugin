"""Unit tests for the ``fetch_openaq_measurements`` atomic tool.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: bad bbox / unknown parameter raise typed OpenAQInputError.
- Parameter normalization: default set, single, list-dedup, case-insensitive.
- Key resolution priority: explicit api_key > secret_ref > env var; NO key
  resolved raises OpenAQMissingKeyError (OPENAQ_KEY_REQUIRED) BEFORE any
  network call — OpenAQ has no public mirror, so the honest degrade IS the
  typed error.
- sensor->parameter cross-reference map from a station's sensors array.
- Assembly: latest values joined to parameter/units, parameter filter, bbox
  geographic-correctness filter, station-coordinate fallback for null record
  coords, unknown-sensor skip.
- FlatGeobuf serialization: documented column schema; honest-empty FGB.
- Mocked happy path (real read_through injector + mocked HTTP): two-call
  fan-out (locations + per-location latest) -> FlatGeobuf -> s3:// LayerURI.
- 401/403 -> OpenAQAuthError (not retryable).
- 5xx / 422 -> OpenAQUpstreamError (retryable).
- Cache hit: second identical call reuses the cached FlatGeobuf.
- Cache key omits the api_key.
- LayerURI shape: layer_type, role, style_preset, units, bbox verified.

Live tests (gated by ``TRID3NT_TEST_LIVE_OPENAQ=1`` + ``TRID3NT_OPENAQ_API_KEY``):
- A real bbox -> >=0 features; evidence captured.
- A bogus key against the real endpoint -> OpenAQAuthError (proves the live
  auth gate without needing a valid key).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements import (
    DEFAULT_PARAMETERS,
    OpenAQAuthError,
    OpenAQInputError,
    OpenAQMissingKeyError,
    OpenAQUpstreamError,
    _assemble_measurement_rows,
    _build_sensor_param_map,
    _resolve_api_key,
    _round_bbox_to_6dp,
    _rows_to_flatgeobuf_bytes,
    _validate_bbox,
    _validate_parameters,
    estimate_payload_mb,
    fetch_openaq_measurements,
    set_persistence_for_secrets,
)

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)

# Delhi NCR bbox (min_lon, min_lat, max_lon, max_lat) — same axis order OpenAQ
# v3 expects (no flip).
_DELHI_BBOX = (76.8, 28.4, 77.4, 28.9)

# Live test gates.
_LIVE_OPENAQ = os.environ.get("TRID3NT_TEST_LIVE_OPENAQ") == "1"
_LIVE_OPENAQ_KEY = os.environ.get("TRID3NT_OPENAQ_API_KEY")

# Test API key (mock-only; never sent to real OpenAQ unless the live test runs).
_MOCK_API_KEY = "test-mock-key-abc123"


def _station(
    *,
    loc_id: int,
    name: str = "Delhi - ITO",
    country_code: str = "IN",
    lat: float = 28.628,
    lon: float = 77.241,
    sensors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an OpenAQ /v3/locations result-shaped station dict."""
    if sensors is None:
        sensors = [
            {
                "id": 9001,
                "name": "pm25",
                "parameter": {
                    "id": 2,
                    "name": "pm25",
                    "units": "ug/m3",
                    "displayName": "PM2.5",
                },
            },
            {
                "id": 9002,
                "name": "no2",
                "parameter": {
                    "id": 5,
                    "name": "no2",
                    "units": "ppm",
                    "displayName": "NO2 mass",
                },
            },
        ]
    return {
        "id": loc_id,
        "name": name,
        "country": {"code": country_code, "name": "India"},
        "coordinates": {"latitude": lat, "longitude": lon},
        "sensors": sensors,
    }


def _latest(
    *,
    sensor_id: int,
    value: float,
    lat: float | None = 28.628,
    lon: float | None = 77.241,
    utc: str = "2026-06-27T05:00:00Z",
    local: str = "2026-06-27T10:30:00+05:30",
) -> dict[str, Any]:
    """Build an OpenAQ /v3/locations/{id}/latest result-shaped record."""
    coords = None
    if lat is not None and lon is not None:
        coords = {"latitude": lat, "longitude": lon}
    return {
        "sensorsId": sensor_id,
        "value": value,
        "datetime": {"utc": utc, "local": local},
        "coordinates": coords,
    }


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors test_fetch_ebird_observations).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake: _FakeStore):
    """S3-only in-memory read-through injector (drives off ``fake.store``)."""
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        ReadThroughResult,
        cache_path,
        compute_cache_key,
        is_cacheable,
    )

    store = fake.store

    def patched(metadata, params, ext, fetch_fn, **kw):
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

    return patched


# ---------------------------------------------------------------------------
# Mock httpx.Client routing locations vs per-location latest by URL.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class _MockHTTPClient:
    """Routes GETs: /locations -> a (possibly paged) list; /latest -> per-id.

    ``locations_pages`` is a list of result-lists (one per page); ``latest_by_id``
    maps locations_id -> list of latest records. ``status_override`` lets a test
    force an error status on the FIRST locations call.
    """

    def __init__(
        self,
        locations_pages: list[list[dict[str, Any]]],
        latest_by_id: dict[int, list[dict[str, Any]]],
        *,
        status_override: int | None = None,
        error_text: str = "",
    ) -> None:
        self._locations_pages = list(locations_pages)
        self._latest_by_id = latest_by_id
        self._status_override = status_override
        self._error_text = error_text
        self.get_calls: list[dict[str, Any]] = []
        self._loc_call_idx = 0

    def __enter__(self) -> "_MockHTTPClient":
        return self

    def __exit__(self, *a) -> None:
        return None

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResp:
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        if url.endswith("/locations"):
            if self._status_override is not None:
                return _FakeResp(self._status_override, None, self._error_text)
            if self._loc_call_idx < len(self._locations_pages):
                page = self._locations_pages[self._loc_call_idx]
            else:
                page = []
            self._loc_call_idx += 1
            return _FakeResp(200, {"meta": {"found": len(page)}, "results": page})
        # /locations/{id}/latest
        loc_id = int(url.rstrip("/").split("/")[-2])
        return _FakeResp(200, {"results": self._latest_by_id.get(loc_id, [])})


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    assert "fetch_openaq_measurements" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_openaq_measurements"]
    assert entry.metadata.name == "fetch_openaq_measurements"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "openaq"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Validation / typed errors.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_bbox((77.0, 28.0, 77.0, 28.0))


def test_inverted_bbox_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_bbox((77.4, 28.9, 76.8, 28.4))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_bbox((-181.0, 28.0, -120.0, 29.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_bbox((76.0, 28.0, 77.0, 91.0))


def test_bbox_wrong_length_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_bbox((76.0, 28.0, 77.0))  # type: ignore[arg-type]


def test_bbox_nonnumeric_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_bbox(("a", 28.0, 77.0, 29.0))  # type: ignore[arg-type]


def test_unknown_parameter_raises_input_error():
    with pytest.raises(OpenAQInputError):
        _validate_parameters("not_a_pollutant")


def test_parameter_default_set():
    assert _validate_parameters(None) == list(DEFAULT_PARAMETERS)


def test_parameter_single_case_insensitive():
    assert _validate_parameters("PM25") == ["pm25"]


def test_parameter_list_dedup():
    assert _validate_parameters(["no2", "o3", "no2"]) == ["no2", "o3"]


def test_parameter_non_str_entry_raises():
    with pytest.raises(OpenAQInputError):
        _validate_parameters([123])  # type: ignore[list-item]


def test_input_error_is_not_retryable():
    assert OpenAQInputError("bad").retryable is False


def test_upstream_error_is_retryable():
    assert OpenAQUpstreamError("5xx").retryable is True


def test_auth_error_is_not_retryable():
    err = OpenAQAuthError("401")
    assert err.retryable is False
    assert err.error_code == "OPENAQ_AUTH_ERROR"


def test_missing_key_error_code_and_not_retryable():
    err = OpenAQMissingKeyError("no key")
    assert err.retryable is False
    assert err.error_code == "OPENAQ_KEY_REQUIRED"


def test_round_bbox_to_6dp():
    raw = (76.123456789, 28.123456789, 77.987654321, 28.987654321)
    assert _round_bbox_to_6dp(raw) == (76.123457, 28.123457, 77.987654, 28.987654)


# ---------------------------------------------------------------------------
# Key resolution (canonical 3-path secret loader).
# ---------------------------------------------------------------------------


def test_resolve_api_key_explicit_kwarg_wins():
    with patch.dict(os.environ, {"TRID3NT_OPENAQ_API_KEY": "env-value"}):
        assert _resolve_api_key(api_key="explicit", secret_ref=None) == "explicit"


def test_resolve_api_key_env_var_fallback():
    with patch.dict(os.environ, {"TRID3NT_OPENAQ_API_KEY": "env-value"}):
        assert _resolve_api_key(api_key=None, secret_ref=None) == "env-value"


def test_resolve_api_key_secret_ref_str_shortcut():
    with patch.dict(os.environ, {}, clear=True):
        assert _resolve_api_key(api_key=None, secret_ref="vault-direct") == "vault-direct"


def test_resolve_api_key_no_path_raises_missing_key():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(OpenAQMissingKeyError) as exc:
            _resolve_api_key(api_key=None, secret_ref=None)
        assert exc.value.error_code == "OPENAQ_KEY_REQUIRED"


def test_resolve_api_key_via_persistence_secret_ref():
    class FakePersistence:
        async def get_secret_value(self, secret_ref):
            return "vault-resolved-key"

    set_persistence_for_secrets(FakePersistence())
    try:
        class FakeRecord:
            secret_id = "S01"
            provider = "openaq"

        with patch.dict(os.environ, {}, clear=True):
            out = _resolve_api_key(api_key=None, secret_ref=FakeRecord())
        assert out == "vault-resolved-key"
    finally:
        set_persistence_for_secrets(None)


def test_no_key_raises_before_any_network_call():
    """The full tool body raises OPENAQ_KEY_REQUIRED with NO HTTP attempted."""
    with patch.dict(os.environ, {}, clear=True), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client"
    ) as mock_client:
        with pytest.raises(OpenAQMissingKeyError) as exc:
            fetch_openaq_measurements(bbox=_DELHI_BBOX)
        assert exc.value.error_code == "OPENAQ_KEY_REQUIRED"
    # No httpx.Client was ever constructed.
    mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# sensor->parameter map + assembly logic.
# ---------------------------------------------------------------------------


def test_build_sensor_param_map():
    m = _build_sensor_param_map(_station(loc_id=100))
    assert m[9001] == {"parameter": "pm25", "display_name": "PM2.5", "unit": "ug/m3"}
    assert m[9002]["parameter"] == "no2"


def test_assemble_joins_value_to_parameter_and_units():
    stations = [_station(loc_id=100)]
    latest = {100: [_latest(sensor_id=9001, value=152.3)]}
    rows, geoms = _assemble_measurement_rows(
        stations, latest, bbox=_DELHI_BBOX, parameters=list(DEFAULT_PARAMETERS)
    )
    assert len(rows) == 1
    assert rows[0]["parameter"] == "pm25"
    assert rows[0]["value"] == 152.3
    assert rows[0]["unit"] == "ug/m3"
    assert rows[0]["display_name"] == "PM2.5"
    assert rows[0]["country"] == "IN"
    assert rows[0]["datetime_utc"] == "2026-06-27T05:00:00Z"
    assert geoms == [(77.241, 28.628)]


def test_assemble_filters_by_parameter_set():
    """A co2 reading is dropped when not in the requested parameter set."""
    sensors = [
        {"id": 9003, "name": "co2", "parameter": {"name": "co2", "units": "ppm", "displayName": "CO2"}},
        {"id": 9001, "name": "pm25", "parameter": {"name": "pm25", "units": "ug/m3", "displayName": "PM2.5"}},
    ]
    stations = [_station(loc_id=100, sensors=sensors)]
    latest = {100: [_latest(sensor_id=9003, value=410.0), _latest(sensor_id=9001, value=10.0)]}
    rows, _ = _assemble_measurement_rows(
        stations, latest, bbox=_DELHI_BBOX, parameters=["pm25"]
    )
    assert len(rows) == 1
    assert rows[0]["parameter"] == "pm25"


def test_assemble_geographic_correctness_filters_outside_bbox():
    """A latest record whose coords fall outside the bbox is dropped."""
    stations = [_station(loc_id=100, lat=28.628, lon=77.241)]
    latest = {
        100: [
            _latest(sensor_id=9001, value=1.0, lat=28.628, lon=77.241),  # inside
            _latest(sensor_id=9002, value=2.0, lat=10.0, lon=10.0),  # outside
        ]
    }
    rows, geoms = _assemble_measurement_rows(
        stations, latest, bbox=_DELHI_BBOX, parameters=list(DEFAULT_PARAMETERS)
    )
    assert len(rows) == 1
    for lon, lat in geoms:
        assert _DELHI_BBOX[0] <= lon <= _DELHI_BBOX[2]
        assert _DELHI_BBOX[1] <= lat <= _DELHI_BBOX[3]


def test_assemble_falls_back_to_station_coords_when_record_coords_null():
    stations = [_station(loc_id=100, lat=28.628, lon=77.241)]
    latest = {100: [_latest(sensor_id=9001, value=5.0, lat=None, lon=None)]}
    rows, geoms = _assemble_measurement_rows(
        stations, latest, bbox=_DELHI_BBOX, parameters=list(DEFAULT_PARAMETERS)
    )
    assert len(rows) == 1
    assert geoms == [(77.241, 28.628)]


def test_assemble_skips_unknown_sensor():
    stations = [_station(loc_id=100)]
    latest = {100: [_latest(sensor_id=9999, value=1.0)]}  # not in sensors array
    rows, _ = _assemble_measurement_rows(
        stations, latest, bbox=_DELHI_BBOX, parameters=list(DEFAULT_PARAMETERS)
    )
    assert rows == []


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def test_rows_to_flatgeobuf_serializes_schema():
    rows = [
        {
            "location_id": 100,
            "location_name": "Delhi - ITO",
            "country": "IN",
            "parameter": "pm25",
            "display_name": "PM2.5",
            "value": 152.3,
            "unit": "ug/m3",
            "datetime_utc": "2026-06-27T05:00:00Z",
            "datetime_local": "2026-06-27T10:30:00+05:30",
            "sensor_id": 9001,
        }
    ]
    geoms = [(77.241, 28.628)]
    fgb = _rows_to_flatgeobuf_bytes(rows, geoms)
    assert fgb.startswith(b"fgb")

    import tempfile

    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb)
        path = tf.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        assert set(gdf.columns) >= {
            "location_id",
            "location_name",
            "country",
            "parameter",
            "display_name",
            "value",
            "unit",
            "datetime_utc",
            "datetime_local",
            "sensor_id",
            "geometry",
        }
        assert gdf.iloc[0]["parameter"] == "pm25"
        assert gdf.iloc[0]["value"] == 152.3
        assert gdf.geometry.iloc[0].geom_type == "Point"
        assert gdf.crs.to_epsg() == 4326
    finally:
        os.unlink(path)


def test_rows_to_flatgeobuf_honest_empty():
    """Zero rows -> a header-only FGB carrying the documented column schema."""
    fgb = _rows_to_flatgeobuf_bytes([], [])
    assert fgb.startswith(b"fgb")

    import tempfile

    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb)
        path = tf.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 0
        assert "parameter" in gdf.columns
        assert "value" in gdf.columns
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_scales_with_area():
    small = estimate_payload_mb(bbox=_DELHI_BBOX)
    big = estimate_payload_mb(bbox=(-10.0, 30.0, 40.0, 60.0))
    assert big > small >= 0.0


def test_estimate_payload_mb_handles_missing_bbox():
    assert estimate_payload_mb() >= 0.0
    assert estimate_payload_mb(bbox="garbage") >= 0.0


# ---------------------------------------------------------------------------
# End-to-end mocked-HTTP happy path (real read_through injector).
# ---------------------------------------------------------------------------


def test_mocked_happy_path_two_call_fanout():
    fake = _FakeStore()
    stations = [_station(loc_id=100)]
    latest_by_id = {
        100: [
            _latest(sensor_id=9001, value=152.3),
            _latest(sensor_id=9002, value=0.018),
        ]
    }
    mock_client = _MockHTTPClient([stations], latest_by_id)

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client,
    ):
        result = fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert result.style_preset == "openaq_measurements"
    assert result.bbox == _round_bbox_to_6dp(_DELHI_BBOX)

    [(path, data)] = list(fake.store.items())
    assert path.startswith("cache/dynamic-1h/openaq/")
    assert path.endswith(".fgb")

    import tempfile

    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 2  # pm25 + no2
        assert set(gdf["parameter"]) == {"pm25", "no2"}
    finally:
        os.unlink(tf_path)

    # First call is /locations; subsequent are /latest. X-API-Key header set.
    assert mock_client.get_calls[0]["url"].endswith("/locations")
    assert mock_client.get_calls[0]["headers"]["X-API-Key"] == _MOCK_API_KEY
    assert mock_client.get_calls[0]["params"]["bbox"] == "76.8,28.4,77.4,28.9"
    assert any(c["url"].endswith("/latest") for c in mock_client.get_calls)


def test_mocked_empty_bbox_yields_empty_layer_no_error():
    """A bbox with no stations -> a valid empty FGB, not an error (honest-empty)."""
    fake = _FakeStore()
    mock_client = _MockHTTPClient([[]], {})

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client,
    ):
        result = fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)

    assert result.uri is not None
    [(_, data)] = list(fake.store.items())

    import tempfile

    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 0
    finally:
        os.unlink(tf_path)


def test_401_on_locations_raises_auth_error():
    fake = _FakeStore()
    mock_client = _MockHTTPClient(
        [[]], {}, status_override=401, error_text='{"detail":"Invalid credentials"}'
    )
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(OpenAQAuthError) as exc:
            fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)
    assert exc.value.error_code == "OPENAQ_AUTH_ERROR"


def test_422_on_locations_raises_upstream_error():
    fake = _FakeStore()
    mock_client = _MockHTTPClient(
        [[]], {}, status_override=422, error_text='{"detail":"bad bbox"}'
    )
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(OpenAQUpstreamError):
            fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)


def test_500_on_locations_raises_upstream_error():
    fake = _FakeStore()
    mock_client = _MockHTTPClient([[]], {}, status_override=503, error_text="oops")
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(OpenAQUpstreamError):
            fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)


def test_cache_hit_skips_fetch_fn():
    """A second identical call reuses the cached FGB (one fetch fan-out only)."""
    fake = _FakeStore()
    stations = [_station(loc_id=100)]
    latest_by_id = {100: [_latest(sensor_id=9001, value=152.3)]}

    mock_client_1 = _MockHTTPClient([stations], latest_by_id)
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client_1,
    ):
        r1 = fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)

    # Second call: a fresh mock client that would RAISE if .get is called.
    class _ExplodingClient(_MockHTTPClient):
        def get(self, *a, **k):  # noqa: D401
            raise AssertionError("cache hit must not re-fetch")

    mock_client_2 = _ExplodingClient([stations], latest_by_id)
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
        side_effect=_make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
        return_value=mock_client_2,
    ):
        r2 = fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_MOCK_API_KEY)

    assert r1.uri == r2.uri
    assert len(fake.store) == 1


def test_cache_key_omits_api_key():
    """Two calls with different keys but identical (bbox, params) share a path."""
    fake = _FakeStore()
    stations = [_station(loc_id=100)]
    latest_by_id = {100: [_latest(sensor_id=9001, value=152.3)]}

    for key in ("key-AAA", "key-BBB"):
        mock_client = _MockHTTPClient([stations], latest_by_id)
        with patch(
            "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.read_through",
            side_effect=_make_read_through_injector(fake),
        ), patch(
            "trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.httpx.Client",
            return_value=mock_client,
        ):
            fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=key)

    # Both calls wrote to the SAME cache path (api_key not in the key).
    assert len(fake.store) == 1


# ---------------------------------------------------------------------------
# Live tests (gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_LIVE_OPENAQ and _LIVE_OPENAQ_KEY),
    reason="set TRID3NT_TEST_LIVE_OPENAQ=1 + TRID3NT_OPENAQ_API_KEY to run",
)
def test_live_openaq_delhi():
    result = fetch_openaq_measurements(bbox=_DELHI_BBOX, api_key=_LIVE_OPENAQ_KEY)
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "vector"


@pytest.mark.skipif(
    not _LIVE_OPENAQ,
    reason="set TRID3NT_TEST_LIVE_OPENAQ=1 to run the live auth-gate probe",
)
def test_live_openaq_bogus_key_raises_auth_error():
    """A bogus key against the REAL endpoint proves the live auth gate."""
    with pytest.raises(OpenAQAuthError) as exc:
        fetch_openaq_measurements(
            bbox=_DELHI_BBOX, api_key="deadbeef-not-a-real-key-000"
        )
    assert exc.value.error_code == "OPENAQ_AUTH_ERROR"
