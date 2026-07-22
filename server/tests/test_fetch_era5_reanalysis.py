"""Unit tests for the ``fetch_era5_reanalysis`` atomic tool (job-0131).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata (incl.
  Wave 1.5 flags: ``supports_global_query=True`` and the payload-MB
  estimator name).
- Validation: bad bbox / bad variable / bad date range raise typed errors.
- API-key resolution: explicit api_key kwarg returned verbatim; missing
  in all three paths returns None (cdsapi falls back to ~/.cdsapirc).
- Mocked CDS retrieve happy path: a fake ``cdsapi.Client`` writes a small
  synthetic NetCDF; the tool converts it to a COG and routes through the
  fake GCS shim with the expected ``cache/static-30d/era5/<key>.tif`` path.
- Two distinct variables produce two distinct cache keys (FR-DC-3
  variable separation).
- Cache hit: a second call with identical params returns the same URI
  without re-invoking cdsapi.
- CDS retrieve timeout surfaces as ``ERA5UpstreamError`` (retryable).
- Cross-field FR-DC-6 consistency: the registered metadata is cacheable
  + static-30d + non-empty source_class.
- payload-MB estimator returns sensible numbers for the audit.md spec
  (0.5 MB / variable / day / 1° square).

Live tests (env-gated ``TRID3NT_TEST_LIVE_ERA5=1`` + a real CDS key via
``~/.cdsapirc`` or ``TRID3NT_COPERNICUS_CDS_API_KEY``):
- Fort Myers 1° square × 1 day, total_precipitation. Evidence emitted
  to ``evidence/era5_live.txt`` per the kickoff.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis import (
    ERA5AuthError,
    ERA5EmptyError,
    ERA5InputError,
    ERA5MissingKeyError,
    ERA5UpstreamError,
    _build_cds_request,
    _cds_retrieve_with_timeout,
    _resolve_api_key,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_date_range,
    _validate_variable,
    estimate_payload_mb,
    fetch_era5_reanalysis,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers / Lee County, FL — small 1° square used by mocked + live tests.
_FORT_MYERS_BBOX = (-82.0, 26.0, -81.0, 27.0)

_LIVE_ERA5 = os.environ.get("TRID3NT_TEST_LIVE_ERA5") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_gbif_occurrences pattern).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
        self.cache_control: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

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


def _write_synthetic_era5_netcdf(
    out_path: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    n_hours: int = 24,
) -> None:
    """Write a tiny ERA5-shaped NetCDF to ``out_path`` for mocked tests.

    Schema mimics what cdsapi returns: a 4D variable with dims
    ``(time, latitude, longitude)`` on a 0.25° grid; coords are EPSG:4326
    decimal degrees with latitude descending (90 → -90 ERA5 convention).
    """
    import numpy as np
    import xarray as xr

    west, south, east, north = bbox
    # 0.25° native ERA5 resolution.
    lats = np.arange(north, south - 0.01, -0.25)
    lons = np.arange(west, east + 0.01, 0.25)
    # Use naive UTC datetimes so xarray's netCDF encoder picks a stable
    # numeric encoding (encoding tz-aware datetimes triggers an "unable to
    # infer dtype" error from xarray).
    times = np.array(
        [
            np.datetime64(_dt.datetime(2024, 9, 26, h, 0), "ns")
            for h in range(n_hours)
        ]
    )

    # Synthetic data: a smooth bump centered in the bbox so the time-mean
    # is non-trivial.
    arr = np.zeros((len(times), len(lats), len(lons)), dtype=np.float32)
    cy, cx = len(lats) // 2, len(lons) // 2
    for t in range(len(times)):
        for j in range(len(lats)):
            for i in range(len(lons)):
                d2 = (j - cy) ** 2 + (i - cx) ** 2
                arr[t, j, i] = float(0.01 * (1 + t) * np.exp(-d2 / 4.0))

    short_name_map = {
        "total_precipitation": "tp",
        "runoff": "ro",
        "2m_temperature": "t2m",
        "10m_u_component_of_wind": "u10",
        "10m_v_component_of_wind": "v10",
        "significant_height_of_combined_wind_waves_and_swell": "swh",
    }
    var_short = short_name_map.get(variable, variable[:8])

    da = xr.DataArray(
        arr,
        dims=("time", "latitude", "longitude"),
        coords={
            "time": times,
            "latitude": lats,
            "longitude": lons,
        },
        name=var_short,
        attrs={
            "long_name": variable.replace("_", " "),
            "units": "m",
        },
    )
    ds = da.to_dataset()
    ds.to_netcdf(out_path)


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_era5_reanalysis appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_era5_reanalysis" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_era5_reanalysis"]
    assert entry.metadata.name == "fetch_era5_reanalysis"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "era5"
    assert entry.metadata.cacheable is True
    # Wave 1.5 flags (audit.md scope).
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_fr_dc_6_cross_field_consistency():
    """Registered metadata satisfies FR-DC-6 (cacheable ⇒ ttl != live, src non-empty)."""
    md = TOOL_REGISTRY["fetch_era5_reanalysis"].metadata
    assert md.cacheable is True
    assert md.ttl_class != "live-no-cache"
    assert md.source_class


# ---------------------------------------------------------------------------
# Validation tests.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(ERA5InputError):
        _validate_bbox((-82.0, 26.0, -82.0, 26.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(ERA5InputError):
        _validate_bbox((-181.0, 26.0, -81.0, 27.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(ERA5InputError):
        _validate_bbox((-82.0, 26.0, -81.0, 91.0))


def test_invalid_variable_raises_input_error():
    with pytest.raises(ERA5InputError, match="unsupported ERA5 variable"):
        _validate_variable("100m_u_component_of_wind")  # not on the allowed list


def test_non_iso_start_date_raises_input_error():
    with pytest.raises(ERA5InputError):
        _validate_date_range("2024/09/26", "2024-09-26")


def test_inverted_date_range_raises_input_error():
    with pytest.raises(ERA5InputError, match="start_date must be <= end_date"):
        _validate_date_range("2024-09-27", "2024-09-26")


def test_huge_date_range_raises_input_error():
    with pytest.raises(ERA5InputError, match="exceeds hard cap"):
        _validate_date_range("2020-01-01", "2024-01-01")


def test_input_error_is_not_retryable():
    """ERA5InputError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_era5_reanalysis(
            bbox=(-82.0, 26.0, -81.0, 27.0),
            variable="not_a_real_var",
            start_date="2024-09-26",
            end_date="2024-09-26",
        )
    except ERA5InputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected ERA5InputError")


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-82.123456789, 26.123456789, -81.987654321, 27.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-82.123457, 26.123457, -81.987654, 27.987654)


def test_build_cds_request_shape():
    """The CDS request dict has the documented shape (area=[N,W,S,E], explicit ymd)."""
    d0 = _dt.date(2024, 9, 26)
    d1 = _dt.date(2024, 9, 27)
    req = _build_cds_request(
        "total_precipitation", (-82.0, 26.0, -81.0, 27.0), d0, d1
    )
    assert req["variable"] == "total_precipitation"
    assert req["product_type"] == "reanalysis"
    assert req["format"] == "netcdf"
    # area = [N, W, S, E] — CDS convention.
    assert req["area"] == [27.0, -82.0, 26.0, -81.0]
    # ymd are explicit lists with zero-padded strings.
    assert req["year"] == ["2024"]
    assert req["month"] == ["09"]
    assert req["day"] == ["26", "27"]
    # All 24 hourly slots.
    assert len(req["time"]) == 24
    assert req["time"][0] == "00:00"
    assert req["time"][-1] == "23:00"


def test_estimate_payload_mb_matches_audit_md_spec():
    """0.5 MB / variable / day / 1° square per audit.md."""
    # 1° square × 1 day → ~0.5 MB.
    one_day_one_deg = estimate_payload_mb(
        bbox=(-82.0, 26.0, -81.0, 27.0),
        variable="total_precipitation",
        start_date="2024-09-26",
        end_date="2024-09-26",
    )
    assert 0.4 <= one_day_one_deg <= 0.6

    # 2 days × 4 sq deg → ~4 MB.
    two_days_four_deg = estimate_payload_mb(
        bbox=(-82.0, 26.0, -80.0, 28.0),  # 2°×2° = 4 sq deg
        variable="total_precipitation",
        start_date="2024-09-26",
        end_date="2024-09-27",
    )
    assert 3.5 <= two_days_four_deg <= 4.5

    # Global bbox is huge.
    global_mb = estimate_payload_mb(
        bbox=None,
        variable="total_precipitation",
        start_date="2024-09-26",
        end_date="2024-09-26",
    )
    assert global_mb > 1000


# ---------------------------------------------------------------------------
# API-key resolution tests.
# ---------------------------------------------------------------------------


def test_resolve_explicit_key_takes_priority():
    """Explicit api_key kwarg is returned verbatim regardless of other paths."""
    monkeypatch_env = os.environ.copy()
    try:
        os.environ["TRID3NT_COPERNICUS_CDS_API_KEY"] = "env-key"
        assert _resolve_api_key(api_key="explicit", secret_ref=None) == "explicit"
    finally:
        os.environ.clear()
        os.environ.update(monkeypatch_env)


def test_resolve_env_fallback_when_no_kwarg():
    """If no kwarg + no secret_ref, the env var wins."""
    monkeypatch_env = os.environ.copy()
    try:
        os.environ["TRID3NT_COPERNICUS_CDS_API_KEY"] = "env-key"
        assert _resolve_api_key(api_key=None, secret_ref=None) == "env-key"
    finally:
        os.environ.clear()
        os.environ.update(monkeypatch_env)


def test_resolve_returns_none_when_all_paths_miss():
    """None of the 4 paths → return None (cdsapi falls back to ~/.cdsapirc)."""
    monkeypatch_env = os.environ.copy()
    try:
        os.environ.pop("TRID3NT_COPERNICUS_CDS_API_KEY", None)
        assert _resolve_api_key(api_key=None, secret_ref=None) is None
    finally:
        os.environ.clear()
        os.environ.update(monkeypatch_env)


def test_resolve_secret_ref_string_shortcut():
    """A string secret_ref (test-mock convenience) returns the string verbatim."""
    assert _resolve_api_key(api_key=None, secret_ref="mocked-key") == "mocked-key"


# ---------------------------------------------------------------------------
# Mocked CDS retrieve happy-path tests.
# ---------------------------------------------------------------------------


def _install_fake_cdsapi(monkeypatch, retrieve_side_effect):
    """Inject a fake ``cdsapi`` module so the tool's lazy import resolves to our stub.

    ``retrieve_side_effect`` is a callable ``(dataset, request, out_path) -> None``.
    """
    fake_mod = types.ModuleType("cdsapi")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def retrieve(self, dataset: str, request: dict, out_path: str) -> None:
            retrieve_side_effect(dataset, request, out_path)

    fake_mod.Client = FakeClient
    monkeypatch.setitem(sys.modules, "cdsapi", fake_mod)


def test_mocked_happy_path_total_precipitation(monkeypatch):
    """Mocked cdsapi → NetCDF → COG roundtrip; output lands in the cache."""
    fake_gcs = FakeStorageClient()

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        assert dataset == "reanalysis-era5-single-levels"
        assert request["variable"] == "total_precipitation"
        _write_synthetic_era5_netcdf(
            out_path, "total_precipitation", _FORT_MYERS_BBOX, n_hours=24
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="total_precipitation",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy-test-key",
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "m"  # total_precipitation native units

    # Cache path layout.
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/era5/")
    assert path.endswith(".tif")
    # The written COG bytes look like a TIFF (II*\x00 or MM\x00*).
    assert data[:2] in (b"II", b"MM"), (
        f"COG should start with TIFF magic; got {data[:8]!r}"
    )


def test_two_variables_produce_distinct_cache_keys(monkeypatch):
    """variable='total_precipitation' vs 'runoff' produce different cache keys."""
    fake_gcs = FakeStorageClient()
    seen_variables: list[str] = []

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        seen_variables.append(request["variable"])
        _write_synthetic_era5_netcdf(
            out_path, request["variable"], _FORT_MYERS_BBOX, n_hours=24
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="total_precipitation",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )
        r2 = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="runoff",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    assert r1.uri != r2.uri
    assert seen_variables == ["total_precipitation", "runoff"]
    assert len(fake_gcs.store) == 2


def test_cache_hit_skips_cdsapi(monkeypatch):
    """Second call with identical params returns the cached URI without re-fetching."""
    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        call_count["n"] += 1
        _write_synthetic_era5_netcdf(
            out_path, "total_precipitation", _FORT_MYERS_BBOX, n_hours=24
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="total_precipitation",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )
        r2 = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="total_precipitation",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    assert call_count["n"] == 1
    assert r1.uri == r2.uri


def test_cdsapi_failure_surfaces_as_upstream_error(monkeypatch):
    """A cdsapi.Client.retrieve raising surfaces as ERA5UpstreamError (retryable)."""
    fake_gcs = FakeStorageClient()

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        raise RuntimeError("CDS queue stalled — please retry later")

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(ERA5UpstreamError) as exc_info:
            fetch_era5_reanalysis(
                bbox=_FORT_MYERS_BBOX,
                variable="total_precipitation",
                start_date="2024-09-26",
                end_date="2024-09-26",
                api_key="dummy",
            )
        assert exc_info.value.retryable is True

    # No artifact should have been written on the failure path.
    assert fake_gcs.store == {}


def test_cdsapi_auth_error_surfaces_as_auth_error(monkeypatch):
    """A cdsapi 401 surfaces as ERA5AuthError (retryable=False)."""
    fake_gcs = FakeStorageClient()

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        raise RuntimeError(
            "401 Unauthorized: User not authenticated (invalid API key)"
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(ERA5AuthError) as exc_info:
            fetch_era5_reanalysis(
                bbox=_FORT_MYERS_BBOX,
                variable="total_precipitation",
                start_date="2024-09-26",
                end_date="2024-09-26",
                api_key="bad-key",
            )
        assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# Missing-credential classification (LIVE BUG NATE 2026-06-18).
#
# A Mexico Beach North Star run failed because the no-key CDS path raised the
# cdsapi constructor error "Missing/incomplete configuration file:
# /root/.cdsapirc" classified as ERA5UpstreamError — a code whose message
# matched NO credential phrase, so the credential pipeline never fired and NO
# secret-entry card surfaced. These tests pin the fix: the missing-.cdsapirc /
# no-config family now classifies as ERA5MissingKeyError (ERA5_MISSING_KEY), so
# is_credential_error → credential-request fires the registered ecmwf_cds card,
# while a GENUINE transient/queue/timeout upstream failure stays
# ERA5UpstreamError (a real outage is NOT misread as a missing key).
# ---------------------------------------------------------------------------

_REQ = {
    "product_type": "reanalysis",
    "variable": "total_precipitation",
    "year": ["2024"],
    "month": ["09"],
    "day": ["26"],
    "time": ["00:00"],
    "area": [27.0, -82.0, 26.0, -81.0],
    "format": "netcdf",
}


def _fake_cdsapi_raising(monkeypatch, *, on_construct=None, on_retrieve=None):
    """Install a fake cdsapi whose Client constructor / retrieve raises."""
    fake_mod = types.ModuleType("cdsapi")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            if on_construct is not None:
                on_construct()

        def retrieve(self, dataset, request, out_path):
            if on_retrieve is not None:
                on_retrieve()

    fake_mod.Client = FakeClient
    monkeypatch.setitem(sys.modules, "cdsapi", fake_mod)


def test_missing_cdsapirc_classifies_as_missing_key(monkeypatch):
    """The cdsapi 'Missing/incomplete configuration file: ...cdsapirc' error
    (no key + no rc file) → ERA5MissingKeyError (ERA5_MISSING_KEY), NOT
    ERA5UpstreamError. This is the exact live failure NATE hit."""
    def _construct():
        raise Exception(
            "Missing/incomplete configuration file: /root/.cdsapirc"
        )

    _fake_cdsapi_raising(monkeypatch, on_construct=_construct)
    with pytest.raises(ERA5MissingKeyError) as exc_info:
        _cds_retrieve_with_timeout(
            api_url="https://x", api_key=None, request=_REQ, out_path="/tmp/x.nc"
        )
    assert exc_info.value.error_code == "ERA5_MISSING_KEY"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "msg",
    [
        "Missing/incomplete configuration file: /root/.cdsapirc",
        "Missing or incomplete configuration file: ~/.cdsapirc",
        "no api key configured",
        "credentials not configured for this client",
    ],
)
def test_no_credentials_family_classifies_as_missing_key(monkeypatch, msg):
    """Every member of the no-credentials family → ERA5MissingKeyError."""
    def _retrieve():
        raise RuntimeError(msg)

    _fake_cdsapi_raising(monkeypatch, on_retrieve=_retrieve)
    with pytest.raises(ERA5MissingKeyError):
        _cds_retrieve_with_timeout(
            api_url="https://x", api_key=None, request=_REQ, out_path="/tmp/x.nc"
        )


@pytest.mark.parametrize(
    "msg",
    [
        "CDS queue stalled — please retry later",
        "503 Service Unavailable",
        "Connection reset by peer",
        "Internal Server Error",
    ],
)
def test_genuine_upstream_failure_stays_upstream(monkeypatch, msg):
    """A real transient/queue/network failure (a key IS present) stays
    ERA5UpstreamError — it is NOT misclassified as a missing key."""
    def _retrieve():
        raise RuntimeError(msg)

    _fake_cdsapi_raising(monkeypatch, on_retrieve=_retrieve)
    with pytest.raises(ERA5UpstreamError) as exc_info:
        _cds_retrieve_with_timeout(
            api_url="https://x", api_key="present-key", request=_REQ,
            out_path="/tmp/x.nc",
        )
    assert exc_info.value.retryable is True


def test_present_but_rejected_key_stays_auth_error(monkeypatch):
    """A present-but-rejected key (401/403) stays ERA5AuthError, not missing."""
    def _retrieve():
        raise RuntimeError("401 Unauthorized: User not authenticated")

    _fake_cdsapi_raising(monkeypatch, on_retrieve=_retrieve)
    with pytest.raises(ERA5AuthError):
        _cds_retrieve_with_timeout(
            api_url="https://x", api_key="bad-key", request=_REQ,
            out_path="/tmp/x.nc",
        )


def test_layer_uri_shape_fields(monkeypatch):
    """The returned LayerURI carries the documented fields."""
    fake_gcs = FakeStorageClient()

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        _write_synthetic_era5_netcdf(
            out_path, "2m_temperature", _FORT_MYERS_BBOX, n_hours=24
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="2m_temperature",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "K"
    assert result.style_preset == "era5_2m_temperature"
    assert "era5" in result.layer_id.lower()
    assert "ERA5" in result.name


# ---------------------------------------------------------------------------
# Derived 10m_wind_speed variable (mocked cdsapi: two retrieves combined).
# ---------------------------------------------------------------------------


def _write_constant_era5_netcdf(
    out_path: str,
    var_short: str,
    bbox: tuple[float, float, float, float],
    value,  # float or 2D array matching the grid
    long_name: str,
    n_hours: int = 3,
) -> None:
    """Write a tiny ERA5-shaped NetCDF with a known constant/grid value per cell.

    Time-invariant so the tool's time-mean recovers ``value`` exactly. Lets the
    wind-speed math assert sqrt(u^2+v^2) on a deterministic grid.
    """
    import numpy as np
    import xarray as xr

    west, south, east, north = bbox
    lats = np.arange(north, south - 0.01, -0.25)
    lons = np.arange(west, east + 0.01, 0.25)
    times = np.array(
        [np.datetime64(_dt.datetime(2024, 9, 26, h, 0), "ns") for h in range(n_hours)]
    )

    grid = np.broadcast_to(
        np.asarray(value, dtype=np.float32), (len(lats), len(lons))
    ).astype(np.float32)
    arr = np.repeat(grid[None, :, :], len(times), axis=0)

    da = xr.DataArray(
        arr,
        dims=("time", "latitude", "longitude"),
        coords={"time": times, "latitude": lats, "longitude": lons},
        name=var_short,
        attrs={"long_name": long_name, "units": "m s-1"},
    )
    da.to_dataset().to_netcdf(out_path)


def test_wind_speed_validates():
    """The derived '10m_wind_speed' variable is on the allowed list."""
    _validate_variable("10m_wind_speed")


def test_wind_speed_issues_two_retrieves_and_writes_magnitude(monkeypatch):
    """variable='10m_wind_speed' fetches BOTH components, writes sqrt(u^2+v^2)."""
    import numpy as np

    fake_gcs = FakeStorageClient()
    seen: list[str] = []

    # u = 3 everywhere, v = 4 everywhere -> speed = 5 everywhere.
    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        var = request["variable"]
        seen.append(var)
        if var == "10m_u_component_of_wind":
            _write_constant_era5_netcdf(
                out_path, "u10", _FORT_MYERS_BBOX, 3.0, "10m u component of wind"
            )
        elif var == "10m_v_component_of_wind":
            _write_constant_era5_netcdf(
                out_path, "v10", _FORT_MYERS_BBOX, 4.0, "10m v component of wind"
            )
        else:
            raise AssertionError(f"unexpected CDS variable {var!r}")

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="10m_wind_speed",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    # BOTH components retrieved, in order.
    assert seen == ["10m_u_component_of_wind", "10m_v_component_of_wind"]

    # LayerURI shape: derived preset + m s-1 units.
    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "m s-1"
    assert result.style_preset == "wind_speed"

    # One cache artifact landed.
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/era5/")
    assert path.endswith(".tif")

    # Decode the single band and confirm sqrt(3^2 + 4^2) == 5 everywhere finite.
    import rasterio

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            assert src.count == 1
            band = src.read(1)
    finally:
        os.unlink(tf_path)

    finite = band[np.isfinite(band)]
    assert finite.size > 0
    np.testing.assert_allclose(finite, 5.0, rtol=0, atol=1e-3)


def test_wind_speed_preserves_nan_nodata(monkeypatch):
    """A NaN cell in either wind component yields NaN in the magnitude band."""
    import numpy as np

    fake_gcs = FakeStorageClient()

    # Build a u-grid with a NaN in one cell; v all 4. The combined cell -> NaN.
    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        var = request["variable"]
        if var == "10m_u_component_of_wind":
            # Grid value 3 with a single NaN; broadcast handles scalar, so build
            # an explicit 2D grid by reading bbox shape.
            west, south, east, north = _FORT_MYERS_BBOX
            ny = len(np.arange(north, south - 0.01, -0.25))
            nx = len(np.arange(west, east + 0.01, 0.25))
            grid = np.full((ny, nx), 3.0, dtype=np.float32)
            grid[0, 0] = np.nan
            _write_constant_era5_netcdf(
                out_path, "u10", _FORT_MYERS_BBOX, grid, "10m u component of wind"
            )
        else:
            _write_constant_era5_netcdf(
                out_path, "v10", _FORT_MYERS_BBOX, 4.0, "10m v component of wind"
            )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="10m_wind_speed",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    [(_path, data)] = list(fake_gcs.store.items())
    import rasterio

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            band = src.read(1)
    finally:
        os.unlink(tf_path)

    # Exactly the one NaN component cell stays NaN (lat-sort flips row order, so
    # don't assume an index); the rest are sqrt(9+16)=5.
    assert int(np.isnan(band).sum()) == 1
    finite = band[np.isfinite(band)]
    assert finite.size == band.size - 1
    np.testing.assert_allclose(finite, 5.0, rtol=0, atol=1e-3)
    assert result.units == "m s-1"


def test_wind_speed_distinct_cache_key_from_components(monkeypatch):
    """10m_wind_speed has its own cache key, distinct from a lone component."""
    fake_gcs = FakeStorageClient()

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        var = request["variable"]
        short = {
            "10m_u_component_of_wind": "u10",
            "10m_v_component_of_wind": "v10",
        }[var]
        _write_constant_era5_netcdf(
            out_path, short, _FORT_MYERS_BBOX, 3.0, var.replace("_", " ")
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_speed = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="10m_wind_speed",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )
        r_u = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="10m_u_component_of_wind",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    assert r_speed.uri != r_u.uri
    # Two distinct cache entries.
    assert len(fake_gcs.store) == 2


def test_component_variable_unchanged(monkeypatch):
    """A lone 10m_u_component_of_wind still issues ONE retrieve, keeps its preset."""
    fake_gcs = FakeStorageClient()
    seen: list[str] = []

    def _retrieve(dataset: str, request: dict, out_path: str) -> None:
        seen.append(request["variable"])
        _write_constant_era5_netcdf(
            out_path, "u10", _FORT_MYERS_BBOX, 7.0, "10m u component of wind"
        )

    _install_fake_cdsapi(monkeypatch, _retrieve)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="10m_u_component_of_wind",
            start_date="2024-09-26",
            end_date="2024-09-26",
            api_key="dummy",
        )

    assert seen == ["10m_u_component_of_wind"]
    assert result.style_preset == "era5_10m_u_component_of_wind"
    assert result.units == "m s-1"


# ---------------------------------------------------------------------------
# Live test — real CDS API call (env-gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_ERA5,
    reason="TRID3NT_TEST_LIVE_ERA5=1 not set (CDS API key required)",
)
def test_live_fort_myers_total_precipitation(tmp_path):
    """LIVE: fetch ERA5 total_precipitation over Fort Myers for one day (Hurricane Ian landfall).

    Calls the real Copernicus CDS API. Captures evidence to
    ``evidence/era5_live.txt`` per the kickoff.
    Asserts the resulting COG is non-empty and tagged with the right CRS.
    """
    import rasterio

    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_era5_reanalysis.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_era5_reanalysis(
            bbox=_FORT_MYERS_BBOX,
            variable="total_precipitation",
            start_date="2024-09-26",
            end_date="2024-09-26",
        )

    assert result.uri is not None
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/era5/")
    assert path.endswith(".tif")
    assert len(data) > 0

    # Open the COG and verify CRS + non-empty bounds intersect bbox.
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            assert src.crs is not None
            bounds = src.bounds
            assert bounds.left < bounds.right
            assert bounds.bottom < bounds.top
            # ERA5 grid is 0.25°; bbox should intersect the read window.
            assert bounds.left < _FORT_MYERS_BBOX[2]
            assert bounds.right > _FORT_MYERS_BBOX[0]
            assert bounds.bottom < _FORT_MYERS_BBOX[3]
            assert bounds.top > _FORT_MYERS_BBOX[1]
            arr = src.read(1)
    finally:
        os.unlink(tf_path)

    # Evidence capture.
    import numpy as np

    n_finite = int(np.isfinite(arr).sum())
    evidence = [
        "# ERA5 live test — Fort Myers total_precipitation",
        f"# bbox: {_FORT_MYERS_BBOX}",
        f"# date: 2024-09-26 (Hurricane Ian landfall)",
        f"# result.uri: {result.uri}",
        f"# COG size: {len(data)} bytes",
        f"# raster shape: {arr.shape}",
        f"# finite pixels: {n_finite}",
        f"# min: {float(np.nanmin(arr)):.6f} m",
        f"# max: {float(np.nanmax(arr)):.6f} m",
        f"# mean: {float(np.nanmean(arr)):.6f} m",
        f"# bounds: {bounds}",
    ]
    evidence_text = "\n".join(evidence)
    print("\n" + evidence_text)

    # Write to the per-job evidence path.
    evidence_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "reports",
        "inflight",
        "job-0131-engine-20260608",
        "evidence",
    )
    try:
        os.makedirs(evidence_dir, exist_ok=True)
        with open(os.path.join(evidence_dir, "era5_live.txt"), "w") as fh:
            fh.write(evidence_text + "\n")
    except OSError:
        pass
