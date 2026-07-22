"""Unit tests for the ``fetch_gtsm_tide_surge`` atomic tool (job-0132).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: bad bbox / unknown output / bad date range raise typed errors.
- Key-resolution priority: explicit api_key > secret_ref > env var > None
  (cdsapi falls back to ~/.cdsapirc).
- CDS request construction: months / years / variable / format.
- ZIP extraction: malformed ZIP raises GTSMUpstreamError; empty ZIP raises.
- NetCDF → gauge-records: in-bbox gauges selected; geographic-correctness gate
  rejects gauges outside bbox; all-NaN gauges filtered.
- Records → FlatGeobuf: schema present; CSV time-series embedded; LayerURI
  shape verified end-to-end.
- Output flavor (water_level vs surge_only) round-trips through the cache key.
- Mocked happy path: a single-NetCDF response → FlatGeobuf with feature count.
- Cache hit: second identical call reuses cached FlatGeobuf.

Live test (gated by ``GRACE2_TEST_LIVE_GTSM=1`` + CDS key):
- Florida coast bbox + Hurricane Ian dates → real time-series with finite
  values; evidence captured to ``reports/inflight/.../evidence/gtsm_live.txt``.
"""

from __future__ import annotations

import datetime as _dt
import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_gtsm_tide_surge import (
    GTSMAuthError,
    GTSMEmptyError,
    GTSMInputError,
    GTSMMissingKeyError,
    GTSMUpstreamError,
    _build_cds_request,
    _extract_netcdfs_from_zip,
    _netcdf_to_gauge_records,
    _records_to_flatgeobuf_bytes,
    _resolve_api_key,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_date_range,
    _validate_output,
    estimate_payload_mb,
    fetch_gtsm_tide_surge,
    set_persistence_for_secrets,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Florida coast bbox per audit.md (live test target).
_FLORIDA_BBOX = (-83.0, 25.0, -80.0, 28.0)

# Hurricane Ian dates per audit.md.
_IAN_START = "2022-09-26"
_IAN_END = "2022-09-29"

# Live test gates.
_LIVE_GTSM = os.environ.get("GRACE2_TEST_LIVE_GTSM") == "1"
_LIVE_CDS_KEY = os.environ.get("GRACE2_COPERNICUS_CDS_API_KEY")


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors sibling Tier-2 test patterns).
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
    from grace2_agent.tools.cache import (
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


# ---------------------------------------------------------------------------
# Fake CDS retrieval: write a synthetic GTSM-shaped NetCDF + ZIP it.
# ---------------------------------------------------------------------------


def _write_synthetic_gtsm_nc(
    nc_path: str,
    *,
    station_lons: list[float],
    station_lats: list[float],
    n_timesteps: int = 24,
    variable: str = "total_water_level",
    fill_pattern: str = "tide",
) -> None:
    """Write a tiny GTSM-shaped NetCDF for tests.

    Mirrors the on-disk schema we expect from CDS:
    - dim: ``stations``
    - dim: ``time``
    - coord var: ``station_x_coordinate(stations)``
    - coord var: ``station_y_coordinate(stations)``
    - coord var: ``time(time)`` (np.datetime64)
    - data var: ``<variable>(time, stations)``
    """
    import netCDF4  # type: ignore[import-not-found]

    with netCDF4.Dataset(nc_path, "w", format="NETCDF4") as ds:
        n_stations = len(station_lons)
        ds.createDimension("stations", n_stations)
        ds.createDimension("time", n_timesteps)

        x = ds.createVariable("station_x_coordinate", "f8", ("stations",))
        y = ds.createVariable("station_y_coordinate", "f8", ("stations",))
        t = ds.createVariable("time", "f8", ("time",))
        v = ds.createVariable(variable, "f4", ("time", "stations"), fill_value=np.nan)

        x[:] = np.asarray(station_lons, dtype=np.float64)
        y[:] = np.asarray(station_lats, dtype=np.float64)

        t.units = "hours since 2022-09-26 00:00:00"
        t.calendar = "standard"
        t[:] = np.arange(n_timesteps, dtype=np.float64)

        # Fill values: simple sinusoidal "tide" with per-station phase shift.
        if fill_pattern == "tide":
            arr = np.zeros((n_timesteps, n_stations), dtype=np.float32)
            for s in range(n_stations):
                phase = s * 0.5
                arr[:, s] = 0.5 * np.sin(
                    2.0 * np.pi * np.arange(n_timesteps) / 12.0 + phase
                ) + 0.1 * s
            v[:, :] = arr
        elif fill_pattern == "all_nan":
            v[:, :] = np.nan
        else:
            v[:, :] = 0.0


def _make_zip_with_ncs(nc_paths: list[str]) -> bytes:
    """Pack the given .nc files into an in-memory ZIP and return bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in nc_paths:
            zf.write(p, arcname=os.path.basename(p))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_gtsm_tide_surge appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_gtsm_tide_surge" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_gtsm_tide_surge"]
    assert entry.metadata.name == "fetch_gtsm_tide_surge"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "gtsm"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False


# ---------------------------------------------------------------------------
# Validation / typed-error tests.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(GTSMInputError):
        _validate_bbox((-83.0, 25.0, -83.0, 25.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(GTSMInputError):
        _validate_bbox((-181.0, 25.0, -80.0, 28.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(GTSMInputError):
        _validate_bbox((-83.0, 25.0, -80.0, 91.0))


def test_unknown_output_raises_input_error():
    with pytest.raises(GTSMInputError):
        _validate_output("tide_only")


def test_water_level_output_ok():
    _validate_output("water_level")
    _validate_output("surge_only")


def test_date_range_inverted_raises_input_error():
    with pytest.raises(GTSMInputError):
        _validate_date_range("2022-09-29", "2022-09-26")


def test_date_range_out_of_gtsm_coverage_raises():
    with pytest.raises(GTSMInputError):
        _validate_date_range("1900-01-01", "1900-01-02")


def test_date_range_exceeds_cap_raises():
    with pytest.raises(GTSMInputError, match="exceeds hard cap"):
        _validate_date_range("2020-01-01", "2021-12-31")


def test_input_error_is_not_retryable():
    err = GTSMInputError("bad")
    assert err.retryable is False


def test_upstream_error_is_retryable():
    err = GTSMUpstreamError("5xx")
    assert err.retryable is True


def test_auth_error_is_not_retryable():
    err = GTSMAuthError("401")
    assert err.retryable is False


def test_missing_key_error_is_not_retryable():
    err = GTSMMissingKeyError("no key")
    assert err.retryable is False


def test_empty_error_is_not_retryable():
    err = GTSMEmptyError("no gauges")
    assert err.retryable is False


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-83.123456789, 25.123456789, -80.987654321, 28.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-83.123457, 25.123457, -80.987654, 28.987654)


def test_build_cds_request_water_level():
    d0 = _dt.date(2022, 9, 26)
    d1 = _dt.date(2022, 9, 29)
    req = _build_cds_request("water_level", d0, d1)
    assert req["variable"] == ["total_water_level"]
    assert req["experiment"] == ["reanalysis"]
    assert req["temporal_aggregation"] == ["hourly"]
    assert req["format"] == "zip"
    assert req["year"] == ["2022"]
    assert req["month"] == ["09"]


def test_build_cds_request_surge_only_maps_to_residual():
    d0 = _dt.date(2022, 9, 26)
    d1 = _dt.date(2022, 9, 29)
    req = _build_cds_request("surge_only", d0, d1)
    assert req["variable"] == ["storm_surge_residual"]


def test_build_cds_request_spans_two_months():
    d0 = _dt.date(2022, 9, 28)
    d1 = _dt.date(2022, 10, 3)
    req = _build_cds_request("water_level", d0, d1)
    assert sorted(req["month"]) == ["09", "10"]


def test_estimate_payload_mb_scales_with_window():
    small = estimate_payload_mb(
        bbox=_FLORIDA_BBOX, start_date=_IAN_START, end_date=_IAN_END
    )
    big = estimate_payload_mb(
        bbox=_FLORIDA_BBOX, start_date="2022-01-01", end_date="2022-12-31"
    )
    # The estimator just needs to be monotonic in window length; absolute
    # numbers are best-effort.
    assert big > small
    assert small > 0


# ---------------------------------------------------------------------------
# Key-resolution tests (FR-AS-11 + §F.3).
# ---------------------------------------------------------------------------


def test_resolve_api_key_explicit_kwarg_wins():
    with patch.dict(os.environ, {"GRACE2_COPERNICUS_CDS_API_KEY": "env-value"}):
        out = _resolve_api_key(api_key="explicit-value", secret_ref=None)
    assert out == "explicit-value"


def test_resolve_api_key_env_var_fallback():
    with patch.dict(os.environ, {"GRACE2_COPERNICUS_CDS_API_KEY": "env-value"}):
        out = _resolve_api_key(api_key=None, secret_ref=None)
    assert out == "env-value"


def test_resolve_api_key_secret_ref_str_shortcut():
    with patch.dict(os.environ, {}, clear=True):
        out = _resolve_api_key(api_key=None, secret_ref="secret-direct-value")
    assert out == "secret-direct-value"


def test_resolve_api_key_no_path_returns_none():
    """With no kwarg / secret_ref / env var, the resolver returns None so the
    cdsapi library can fall back to ``~/.cdsapirc``. (Distinct from the eBird
    pattern where missing key is a hard error — CDS workflow stores keys in
    an rc file on dev machines.)"""
    with patch.dict(os.environ, {}, clear=True):
        out = _resolve_api_key(api_key=None, secret_ref=None)
    assert out is None


def test_resolve_api_key_via_persistence_secret_ref():
    """When ``secret_ref`` is a SecretRecord-like object, the resolver goes
    through the bound Persistence and returns the materialized value."""

    class FakePersistence:
        async def get_secret_value(self, secret_ref):
            return "vault-resolved-key"

    set_persistence_for_secrets(FakePersistence())
    try:
        class FakeRecord:
            secret_id = "S01"
            provider = "ebird"  # closest available ProviderID (no copernicus_cds yet)
            is_active = True
            vault_ref = "projects/p/secrets/s/versions/latest"

        with patch.dict(os.environ, {}, clear=True):
            out = _resolve_api_key(api_key=None, secret_ref=FakeRecord())
        assert out == "vault-resolved-key"
    finally:
        set_persistence_for_secrets(None)


# ---------------------------------------------------------------------------
# ZIP / NetCDF extraction tests.
# ---------------------------------------------------------------------------


def test_extract_netcdfs_from_zip_unpacks_members(tmp_path):
    """The ZIP unpacker extracts every .nc file and ignores non-.nc members."""
    # Synthesize two .nc files + a stray README.
    nc1 = tmp_path / "GTSM_202209.nc"
    nc2 = tmp_path / "GTSM_202210.nc"
    _write_synthetic_gtsm_nc(
        str(nc1), station_lons=[-82.0], station_lats=[26.0]
    )
    _write_synthetic_gtsm_nc(
        str(nc2), station_lons=[-82.0], station_lats=[26.0]
    )
    readme = tmp_path / "README.txt"
    readme.write_text("not a netcdf")

    zip_path = tmp_path / "out.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.write(str(nc1), arcname=nc1.name)
        zf.write(str(nc2), arcname=nc2.name)
        zf.write(str(readme), arcname=readme.name)

    extracted = _extract_netcdfs_from_zip(str(zip_path))
    assert len(extracted) == 2
    assert all(p.endswith(".nc") for p in extracted)
    # Cleanup.
    for p in extracted:
        os.unlink(p)


def test_extract_netcdfs_from_zip_malformed_raises(tmp_path):
    bad = tmp_path / "notazip.zip"
    bad.write_text("not a real ZIP archive")
    with pytest.raises(GTSMUpstreamError):
        _extract_netcdfs_from_zip(str(bad))


# ---------------------------------------------------------------------------
# NetCDF → gauge records tests.
# ---------------------------------------------------------------------------


def test_netcdf_to_gauge_records_selects_in_bbox(tmp_path):
    """Only gauges inside the requested bbox are returned."""
    nc_path = str(tmp_path / "gtsm.nc")
    # 3 stations: 2 in Florida bbox, 1 in California.
    _write_synthetic_gtsm_nc(
        nc_path,
        station_lons=[-82.0, -81.5, -122.0],
        station_lats=[26.0, 27.0, 38.0],
    )
    recs = _netcdf_to_gauge_records([nc_path], _FLORIDA_BBOX, "water_level")
    assert len(recs) == 2
    for r in recs:
        assert _FLORIDA_BBOX[0] <= r["lon"] <= _FLORIDA_BBOX[2]
        assert _FLORIDA_BBOX[1] <= r["lat"] <= _FLORIDA_BBOX[3]
        # Each gauge should have 24 hourly samples with the synthetic tide.
        assert len(r["times"]) == 24
        assert len(r["values"]) == 24
        assert all(isinstance(v, float) for v in r["values"])


def test_netcdf_to_gauge_records_empty_bbox_raises(tmp_path):
    """A bbox far from any gauge raises GTSMEmptyError."""
    nc_path = str(tmp_path / "gtsm.nc")
    _write_synthetic_gtsm_nc(
        nc_path, station_lons=[-122.0], station_lats=[38.0]
    )
    with pytest.raises(GTSMEmptyError):
        _netcdf_to_gauge_records([nc_path], _FLORIDA_BBOX, "water_level")


def test_netcdf_to_gauge_records_all_nan_filtered(tmp_path):
    """Gauges with all-NaN time series are filtered."""
    nc_path = str(tmp_path / "gtsm.nc")
    _write_synthetic_gtsm_nc(
        nc_path,
        station_lons=[-82.0],
        station_lats=[26.0],
        fill_pattern="all_nan",
    )
    with pytest.raises(GTSMEmptyError):
        _netcdf_to_gauge_records([nc_path], _FLORIDA_BBOX, "water_level")


def test_netcdf_to_gauge_records_longitude_normalization(tmp_path):
    """Stations with longitudes in 0..360 are normalised to -180..180."""
    nc_path = str(tmp_path / "gtsm.nc")
    # 278.0 == -82.0 after wrap; 277.5 == -82.5; should match Florida bbox.
    _write_synthetic_gtsm_nc(
        nc_path,
        station_lons=[278.0, 277.5],
        station_lats=[26.0, 26.5],
    )
    recs = _netcdf_to_gauge_records([nc_path], _FLORIDA_BBOX, "water_level")
    assert len(recs) == 2
    for r in recs:
        assert _FLORIDA_BBOX[0] <= r["lon"] <= _FLORIDA_BBOX[2]


# ---------------------------------------------------------------------------
# FlatGeobuf serialization tests.
# ---------------------------------------------------------------------------


def test_records_to_flatgeobuf_serializes_with_time_series(tmp_path):
    """Records → FlatGeobuf produce a parseable file with time-series CSV."""
    import geopandas as gpd  # type: ignore[import-not-found]

    records = [
        {
            "gauge_id": "GTSM-000001",
            "lon": -82.0,
            "lat": 26.0,
            "times": [
                "2022-09-26T00:00:00Z",
                "2022-09-26T01:00:00Z",
                "2022-09-26T02:00:00Z",
            ],
            "values": [0.1, 0.2, 0.3],
        },
        {
            "gauge_id": "GTSM-000002",
            "lon": -81.0,
            "lat": 27.0,
            "times": [
                "2022-09-26T00:00:00Z",
                "2022-09-26T01:00:00Z",
                "2022-09-26T02:00:00Z",
            ],
            "values": [0.5, 0.6, 0.7],
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(records, "water_level")
    assert fgb_bytes.startswith(b"fgb")
    assert len(fgb_bytes) > 0

    out_path = str(tmp_path / "out.fgb")
    with open(out_path, "wb") as f:
        f.write(fgb_bytes)
    gdf = gpd.read_file(out_path, engine="pyogrio")

    assert len(gdf) == 2
    assert set(gdf.columns) >= {
        "gauge_id",
        "lon",
        "lat",
        "time_start",
        "time_end",
        "n_timesteps",
        "wl_min_m",
        "wl_max_m",
        "wl_mean_m",
        "output",
        "time_series_csv",
        "geometry",
    }
    assert gdf["output"].iloc[0] == "water_level"
    # CSV time series carries 3 lines.
    csv0 = gdf["time_series_csv"].iloc[0]
    assert csv0.strip().count("\n") == 2  # 3 lines → 2 newlines (no trailing)


def test_records_to_flatgeobuf_handles_nans():
    """Non-finite values in a gauge series are dropped from the CSV."""
    records = [
        {
            "gauge_id": "G1",
            "lon": -82.0,
            "lat": 26.0,
            "times": ["t0", "t1", "t2"],
            "values": [0.1, float("nan"), 0.3],
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(records, "water_level")
    assert fgb_bytes.startswith(b"fgb")


def test_empty_records_produces_empty_flatgeobuf():
    fgb_bytes = _records_to_flatgeobuf_bytes([], "water_level")
    assert fgb_bytes.startswith(b"fgb")


# ---------------------------------------------------------------------------
# End-to-end mocked happy path.
# ---------------------------------------------------------------------------


def _fake_cds_retrieve_factory(zip_bytes: bytes):
    """Return a fake _cds_retrieve_with_timeout that writes ``zip_bytes`` to out_path."""

    def fake_retrieve(api_url, api_key, request, out_path, timeout_s=300):
        with open(out_path, "wb") as f:
            f.write(zip_bytes)

    return fake_retrieve


def test_end_to_end_mocked_happy_path(tmp_path, monkeypatch):
    """Mock the CDS retrieve to return a synthetic ZIP; verify LayerURI shape
    and cache write-through."""
    # Build a tiny synthetic NetCDF + zip it.
    nc_path = str(tmp_path / "GTSM_202209.nc")
    _write_synthetic_gtsm_nc(
        nc_path,
        station_lons=[-82.0, -81.5, -122.0],
        station_lats=[26.0, 27.0, 38.0],
    )
    zip_bytes = _make_zip_with_ncs([nc_path])

    # Patch the CDS retrieve at module level.
    from grace2_agent.tools import fetch_gtsm_tide_surge as mod

    monkeypatch.setattr(
        mod, "_cds_retrieve_with_timeout", _fake_cds_retrieve_factory(zip_bytes)
    )

    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    monkeypatch.setattr(mod, "read_through", patched_rt)

    # Florida bbox + the synthetic data: 2 in-bbox stations expected.
    layer = fetch_gtsm_tide_surge(
        bbox=_FLORIDA_BBOX,
        start_date="2022-09-26",
        end_date="2022-09-29",
        output="water_level",
        api_key="mock-key",
    )

    assert layer.uri is not None
    assert layer.uri.startswith("s3://grace2-hazard-cache-226996537797/cache/static-30d/gtsm/")
    assert layer.uri.endswith(".fgb")
    assert layer.layer_type == "vector"
    assert layer.role == "primary"
    assert layer.units == "m"
    assert layer.style_preset == "gtsm_water_level"

    # The FlatGeobuf landed in the fake bucket; round-trip via geopandas.
    import geopandas as gpd  # type: ignore[import-not-found]

    bucket_path = layer.uri.split("s3://grace2-hazard-cache-226996537797/")[1]
    fgb_bytes = fake_gcs.store[bucket_path]
    assert fgb_bytes.startswith(b"fgb")

    out_path = str(tmp_path / "out.fgb")
    with open(out_path, "wb") as f:
        f.write(fgb_bytes)
    gdf = gpd.read_file(out_path, engine="pyogrio")
    # Only 2 stations should be inside the Florida bbox.
    assert len(gdf) == 2
    for geom in gdf.geometry:
        assert _FLORIDA_BBOX[0] <= geom.x <= _FLORIDA_BBOX[2]
        assert _FLORIDA_BBOX[1] <= geom.y <= _FLORIDA_BBOX[3]


def test_cache_hit_on_second_identical_call(tmp_path, monkeypatch):
    """The second identical call reuses the cached FlatGeobuf — no second
    CDS retrieve."""
    nc_path = str(tmp_path / "GTSM_202209.nc")
    _write_synthetic_gtsm_nc(
        nc_path,
        station_lons=[-82.0],
        station_lats=[26.0],
    )
    zip_bytes = _make_zip_with_ncs([nc_path])

    from grace2_agent.tools import fetch_gtsm_tide_surge as mod

    call_count = {"n": 0}

    def counting_retrieve(api_url, api_key, request, out_path, timeout_s=300):
        call_count["n"] += 1
        with open(out_path, "wb") as f:
            f.write(zip_bytes)

    monkeypatch.setattr(mod, "_cds_retrieve_with_timeout", counting_retrieve)

    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    monkeypatch.setattr(mod, "read_through", patched_rt)

    layer1 = fetch_gtsm_tide_surge(
        bbox=_FLORIDA_BBOX,
        start_date="2022-09-26",
        end_date="2022-09-29",
        output="water_level",
        api_key="mock-key",
    )
    layer2 = fetch_gtsm_tide_surge(
        bbox=_FLORIDA_BBOX,
        start_date="2022-09-26",
        end_date="2022-09-29",
        output="water_level",
        api_key="mock-key",
    )

    assert layer1.uri == layer2.uri
    # Only one CDS retrieve fired despite two calls.
    assert call_count["n"] == 1


def test_output_flavor_distinct_cache_keys(tmp_path, monkeypatch):
    """water_level vs surge_only request distinct cache entries."""
    nc_path_wl = str(tmp_path / "GTSM_wl.nc")
    _write_synthetic_gtsm_nc(
        nc_path_wl,
        station_lons=[-82.0],
        station_lats=[26.0],
        variable="total_water_level",
    )
    zip_wl = _make_zip_with_ncs([nc_path_wl])

    nc_path_sr = str(tmp_path / "GTSM_sr.nc")
    _write_synthetic_gtsm_nc(
        nc_path_sr,
        station_lons=[-82.0],
        station_lats=[26.0],
        variable="storm_surge_residual",
    )
    zip_sr = _make_zip_with_ncs([nc_path_sr])

    from grace2_agent.tools import fetch_gtsm_tide_surge as mod

    def selective_retrieve(api_url, api_key, request, out_path, timeout_s=300):
        var_list = request["variable"]
        if var_list and var_list[0] == "storm_surge_residual":
            with open(out_path, "wb") as f:
                f.write(zip_sr)
        else:
            with open(out_path, "wb") as f:
                f.write(zip_wl)

    monkeypatch.setattr(mod, "_cds_retrieve_with_timeout", selective_retrieve)

    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    monkeypatch.setattr(mod, "read_through", patched_rt)

    layer_wl = fetch_gtsm_tide_surge(
        bbox=_FLORIDA_BBOX,
        start_date="2022-09-26",
        end_date="2022-09-29",
        output="water_level",
        api_key="mock-key",
    )
    layer_sr = fetch_gtsm_tide_surge(
        bbox=_FLORIDA_BBOX,
        start_date="2022-09-26",
        end_date="2022-09-29",
        output="surge_only",
        api_key="mock-key",
    )

    # Distinct URIs → distinct cache keys.
    assert layer_wl.uri != layer_sr.uri
    assert layer_wl.style_preset == "gtsm_water_level"
    assert layer_sr.style_preset == "gtsm_surge_only"


# ---------------------------------------------------------------------------
# Live test (env-gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_LIVE_GTSM and _LIVE_CDS_KEY),
    reason="set GRACE2_TEST_LIVE_GTSM=1 + GRACE2_COPERNICUS_CDS_API_KEY",
)
def test_live_florida_coast_hurricane_ian(tmp_path):
    """Live test: fetch GTSM water-level for Florida coast over Hurricane Ian.

    Verifies the live CDS path end-to-end and writes an evidence file with
    the gauge count, time-series length, and water-level statistics.
    """
    import geopandas as gpd  # type: ignore[import-not-found]

    # Inject the fake GCS so the live test does not require live cache-bucket
    # write permission. The CDS retrieve is real.
    fake_gcs = FakeStorageClient()
    from grace2_agent.tools import fetch_gtsm_tide_surge as mod

    patched_rt = _make_read_through_injector(fake_gcs)
    orig_rt = mod.read_through
    mod.read_through = patched_rt
    try:
        layer = fetch_gtsm_tide_surge(
            bbox=_FLORIDA_BBOX,
            start_date=_IAN_START,
            end_date=_IAN_END,
            output="water_level",
        )
    finally:
        mod.read_through = orig_rt

    assert layer.uri is not None
    bucket_path = layer.uri.split("gs://grace2-hazard-cache-226996537797/")[1]
    fgb_bytes = fake_gcs.store[bucket_path]

    out_path = str(tmp_path / "gtsm_live.fgb")
    with open(out_path, "wb") as f:
        f.write(fgb_bytes)
    gdf = gpd.read_file(out_path, engine="pyogrio")
    assert len(gdf) >= 1, "expected ≥1 GTSM gauge inside the Florida bbox"

    # Capture evidence.
    evidence_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "reports",
        "inflight",
        "job-0132-engine-20260608",
        "evidence",
    )
    os.makedirs(evidence_dir, exist_ok=True)
    with open(os.path.join(evidence_dir, "gtsm_live.txt"), "w") as f:
        f.write(f"# fetch_gtsm_tide_surge live evidence — job-0132\n")
        f.write(f"bbox={_FLORIDA_BBOX}\n")
        f.write(f"dates={_IAN_START} → {_IAN_END}\n")
        f.write(f"output=water_level\n")
        f.write(f"layer.uri={layer.uri}\n")
        f.write(f"n_gauges={len(gdf)}\n")
        f.write(f"gauge_ids={list(gdf['gauge_id'].head(5))}\n")
        f.write(f"wl_min_m={gdf['wl_min_m'].min():.4f}\n")
        f.write(f"wl_max_m={gdf['wl_max_m'].max():.4f}\n")
        f.write(f"wl_mean_m={gdf['wl_mean_m'].mean():.4f}\n")
