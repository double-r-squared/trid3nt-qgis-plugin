"""Unit tests for the ``fetch_gridmet`` atomic tool (job A8).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata + Wave 1.5
  flags (``supports_global_query=False``, payload-MB estimator name).
- Validation: bad bbox / out-of-CONUS / bad variable / bad date range
  raise typed errors with ``retryable=False``.
- Payload estimator: returns sensible numbers per the audit spec.
- Mocked DAP path: synthetic THREDDS netCDF → time-mean COG roundtrip via
  the fake GCS shim with the expected
  ``cache/static-30d/gridmet/<key>.tif`` path.
- Two distinct variables produce distinct cache keys.
- Cache hit: second identical call returns the same URI without re-fetching.
- DAP failure surfaces as ``GRIDMETUpstreamError`` (retryable).

Live tests (env-gated ``TRID3NT_TEST_LIVE_GRIDMET=1``):
- Riverside County, CA 1° square × 3 days, fm100. Real THREDDS DAP
  subset; evidence written to ``evidence/gridmet_live.txt``.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile
import types
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.climate.fetch_gridmet import (
    GRIDMETEmptyError,
    GRIDMETInputError,
    GRIDMETNotAvailableError,
    GRIDMETUpstreamError,
    _build_dap_url,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_date_range,
    _validate_variable,
    estimate_payload_mb,
    fetch_gridmet,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

# Riverside County, CA — fire-prone 1° square used by mocked + live tests.
_RIVERSIDE_BBOX = (-117.5, 33.5, -116.5, 34.5)

# Fort Myers, FL — small bbox to confirm CONUS-east also works.
_FORT_MYERS_BBOX = (-82.0, 26.0, -81.0, 27.0)

_LIVE_GRIDMET = os.environ.get("TRID3NT_TEST_LIVE_GRIDMET") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_era5_reanalysis pattern).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self.custom_time = None
        self.cache_control = None

    def exists(self):
        return self._path in self._store

    def download_as_bytes(self):
        return self._store[self._path]

    def upload_from_string(self, data, content_type=None):
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store):
        self._store = store

    def blob(self, path):
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self):
        self.store = {}

    def bucket(self, name):
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


def _build_synthetic_gridmet_dataset(variable: str, bbox, n_days: int = 3):
    """Build an xarray Dataset matching the gridMET aggregated-netCDF shape.

    Used by the mocked DAP path: we patch ``xarray.open_dataset`` to return
    this pre-built Dataset so the tool's subset / mean / COG path runs
    end-to-end without touching the network.
    """
    import numpy as np
    import xarray as xr

    west, south, east, north = bbox
    # gridMET native resolution is ~0.0417° (4 km). Use a coarser grid
    # for the synthetic to keep tests fast.
    lats = np.arange(north + 0.5, south - 0.5, -0.1)
    lons = np.arange(west - 0.5, east + 0.5, 0.1)
    days = np.array(
        [
            np.datetime64(_dt.date(2024, 9, 26) + _dt.timedelta(days=d), "D")
            for d in range(n_days)
        ]
    )
    # Variable's "internal" long-name token from the production module.
    from trid3nt_server.tools.fetchers.climate.fetch_gridmet import _VARIABLES
    long_name, units = _VARIABLES[variable]

    arr = np.zeros((len(days), len(lats), len(lons)), dtype=np.float32)
    cy, cx = len(lats) // 2, len(lons) // 2
    for t in range(len(days)):
        for j in range(len(lats)):
            for i in range(len(lons)):
                d2 = (j - cy) ** 2 + (i - cx) ** 2
                arr[t, j, i] = float(15.0 + 0.1 * t * np.exp(-d2 / 4.0))

    da = xr.DataArray(
        arr,
        dims=("day", "lat", "lon"),
        coords={
            "day": days,
            "lat": lats,
            "lon": lons,
        },
        name=long_name,
        attrs={"long_name": long_name.replace("_", " "), "units": units},
    )
    return da.to_dataset()


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    assert "fetch_gridmet" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_gridmet"]
    assert entry.metadata.name == "fetch_gridmet"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "gridmet"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_fr_dc_6_cross_field_consistency():
    md = TOOL_REGISTRY["fetch_gridmet"].metadata
    assert md.cacheable is True
    assert md.ttl_class != "live-no-cache"
    assert md.source_class


# ---------------------------------------------------------------------------
# Validation tests.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(GRIDMETInputError):
        _validate_bbox((-117.5, 33.5, -117.5, 33.5))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(GRIDMETInputError):
        _validate_bbox((-181.0, 33.5, -116.5, 34.5))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(GRIDMETInputError):
        _validate_bbox((-117.5, -91.0, -116.5, 34.5))


def test_non_conus_bbox_raises_input_error():
    # Paris, France — far outside CONUS.
    with pytest.raises(GRIDMETInputError, match="CONUS"):
        _validate_bbox((2.0, 48.0, 3.0, 49.0))


def test_alaska_bbox_raises_input_error():
    # Anchorage — outside CONUS gridMET coverage.
    with pytest.raises(GRIDMETInputError, match="CONUS"):
        _validate_bbox((-150.0, 60.0, -149.0, 61.0))


def test_invalid_variable_raises_input_error():
    with pytest.raises(GRIDMETInputError, match="unsupported gridMET variable"):
        _validate_variable("fm999")


def test_non_iso_start_date_raises_input_error():
    with pytest.raises(GRIDMETInputError):
        _validate_date_range("2024/09/26", "2024-09-26")


def test_inverted_date_range_raises_input_error():
    with pytest.raises(GRIDMETInputError, match="start_date must be <= end_date"):
        _validate_date_range("2024-09-27", "2024-09-26")


def test_date_before_1979_raises_not_available():
    with pytest.raises(GRIDMETNotAvailableError):
        _validate_date_range("1970-01-01", "1970-12-31")


def test_huge_date_range_raises_input_error():
    with pytest.raises(GRIDMETInputError, match="exceeds hard cap"):
        _validate_date_range("2020-01-01", "2024-01-01")


def test_input_error_is_not_retryable():
    """GRIDMETInputError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="not_a_real_var",
            start_date="2024-09-26",
            end_date="2024-09-26",
        )
    except GRIDMETInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected GRIDMETInputError")


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-117.123456789, 33.123456789, -116.987654321, 34.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-117.123457, 33.123457, -116.987654, 34.987654)


def test_build_dap_url_shape():
    """The DAP URL points at the aggregated netCDF for the variable."""
    url = _build_dap_url("fm100")
    assert url.startswith("http://thredds.northwestknowledge.net:8080")
    assert "agg_met_fm100_1979_CurrentYear_CONUS.nc" in url


def test_estimate_payload_mb_returns_small_number_for_metro_bbox():
    mb = estimate_payload_mb(
        bbox=_RIVERSIDE_BBOX,
        variable="fm100",
        start_date="2024-09-26",
        end_date="2024-09-28",
    )
    # 1° square → ~0.01 MB output COG.
    assert 0.005 <= mb <= 0.05


def test_estimate_payload_mb_handles_none_bbox():
    mb = estimate_payload_mb(
        bbox=None,
        variable="fm100",
        start_date="2024-09-26",
        end_date="2024-09-28",
    )
    # CONUS-wide bbox should be larger than a metro one.
    assert mb > 0.1


# ---------------------------------------------------------------------------
# Mocked DAP happy-path tests.
# ---------------------------------------------------------------------------


def _install_fake_xr_open(monkeypatch, dataset_factory):
    """Patch ``xarray.open_dataset`` to return a synthetic Dataset.

    ``dataset_factory(url)`` is called with the DAP URL and must return a
    Dataset; this lets us reject unexpected URLs and forge per-variable
    payloads.
    """
    import xarray as xr

    real_open = xr.open_dataset

    def fake_open(path_or_url, *args, **kwargs):
        if isinstance(path_or_url, str) and path_or_url.startswith(
            "http://thredds.northwestknowledge.net"
        ):
            return dataset_factory(path_or_url)
        return real_open(path_or_url, *args, **kwargs)

    monkeypatch.setattr(xr, "open_dataset", fake_open)


def test_mocked_happy_path_fm100(monkeypatch):
    """Mocked DAP → time-mean → COG roundtrip; output lands in the cache."""
    fake_gcs = FakeStorageClient()

    def _factory(url):
        assert "fm100" in url
        return _build_synthetic_gridmet_dataset("fm100", _RIVERSIDE_BBOX, n_days=3)

    _install_fake_xr_open(monkeypatch, _factory)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="fm100",
            start_date="2024-09-26",
            end_date="2024-09-28",
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "Percent"
    assert "gridmet" in result.layer_id.lower()
    assert "fm100" in result.layer_id.lower()

    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/gridmet/")
    assert path.endswith(".tif")
    # Written COG bytes look like a TIFF.
    assert data[:2] in (b"II", b"MM"), (
        f"COG should start with TIFF magic; got {data[:8]!r}"
    )


def test_two_variables_produce_distinct_cache_keys(monkeypatch):
    """fm100 vs pdsi produce different cache keys."""
    fake_gcs = FakeStorageClient()
    seen_urls = []

    def _factory(url):
        seen_urls.append(url)
        # Pick the variable from the URL.
        for v in ("fm100", "pdsi"):
            if v in url:
                return _build_synthetic_gridmet_dataset(
                    v, _RIVERSIDE_BBOX, n_days=2
                )
        raise AssertionError(f"unexpected DAP URL: {url}")

    _install_fake_xr_open(monkeypatch, _factory)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="fm100",
            start_date="2024-09-26",
            end_date="2024-09-27",
        )
        r2 = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="pdsi",
            start_date="2024-09-26",
            end_date="2024-09-27",
        )

    assert r1.uri != r2.uri
    assert len(fake_gcs.store) == 2


def test_cache_hit_skips_dap(monkeypatch):
    """Second identical call returns the cached URI without re-fetching."""
    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}

    def _factory(url):
        call_count["n"] += 1
        return _build_synthetic_gridmet_dataset(
            "fm100", _RIVERSIDE_BBOX, n_days=2
        )

    _install_fake_xr_open(monkeypatch, _factory)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="fm100",
            start_date="2024-09-26",
            end_date="2024-09-27",
        )
        r2 = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="fm100",
            start_date="2024-09-26",
            end_date="2024-09-27",
        )

    assert call_count["n"] == 1
    assert r1.uri == r2.uri


def test_dap_failure_surfaces_as_upstream_error(monkeypatch):
    """A failing xarray open surfaces as GRIDMETUpstreamError (retryable)."""
    fake_gcs = FakeStorageClient()

    import xarray as xr

    def _fake_open(path_or_url, *args, **kwargs):
        raise RuntimeError("OPeNDAP server unreachable (mocked)")

    monkeypatch.setattr(xr, "open_dataset", _fake_open)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(GRIDMETUpstreamError) as exc_info:
            fetch_gridmet(
                bbox=_RIVERSIDE_BBOX,
                variable="fm100",
                start_date="2024-09-26",
                end_date="2024-09-27",
            )
        assert exc_info.value.retryable is True

    # No artifact should have been written on the failure path.
    assert fake_gcs.store == {}


def test_layer_uri_shape_fields(monkeypatch):
    """The returned LayerURI carries the documented fields."""
    fake_gcs = FakeStorageClient()

    def _factory(url):
        return _build_synthetic_gridmet_dataset(
            "pdsi", _RIVERSIDE_BBOX, n_days=2
        )

    _install_fake_xr_open(monkeypatch, _factory)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="pdsi",
            start_date="2024-09-26",
            end_date="2024-09-27",
        )

    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "unitless"
    assert result.style_preset == "gridmet_pdsi"
    assert "gridmet" in result.layer_id.lower()
    assert "pdsi" in result.layer_id.lower()
    assert "gridMET" in result.name


def test_extra_kwargs_absorbed(monkeypatch):
    """The tool absorbs invented kwargs (Gemini hallucination defense)."""
    fake_gcs = FakeStorageClient()

    def _factory(url):
        return _build_synthetic_gridmet_dataset(
            "fm100", _RIVERSIDE_BBOX, n_days=2
        )

    _install_fake_xr_open(monkeypatch, _factory)

    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        # Call with extra invented kwargs — must not raise TypeError.
        result = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="fm100",
            start_date="2024-09-26",
            end_date="2024-09-27",
            unknown_param="ignored",
            another_invention=42,
        )
    assert result.uri is not None


# ---------------------------------------------------------------------------
# Live test — real THREDDS DAP call (env-gated by default).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_GRIDMET,
    reason="TRID3NT_TEST_LIVE_GRIDMET=1 not set",
)
def test_live_riverside_fm100(tmp_path):
    """LIVE: fetch gridMET fm100 over Riverside County for 3 days."""
    import rasterio

    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetchers.climate.fetch_gridmet.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_gridmet(
            bbox=_RIVERSIDE_BBOX,
            variable="fm100",
            start_date="2024-09-15",
            end_date="2024-09-17",
        )

    assert result.uri is not None
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/gridmet/")
    assert path.endswith(".tif")
    assert len(data) > 0

    # Verify CRS + intersect.
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            assert src.crs is not None
            bounds = src.bounds
            assert bounds.left < bounds.right
            assert bounds.bottom < bounds.top
            arr = src.read(1)
    finally:
        os.unlink(tf_path)

    import numpy as np
    n_finite = int(np.isfinite(arr).sum())
    evidence = [
        "# gridMET live test — Riverside County, CA — fm100",
        f"# bbox: {_RIVERSIDE_BBOX}",
        f"# dates: 2024-09-15 -> 2024-09-17",
        f"# result.uri: {result.uri}",
        f"# COG size: {len(data)} bytes",
        f"# raster shape: {arr.shape}",
        f"# finite pixels: {n_finite}",
        f"# min: {float(np.nanmin(arr)):.4f} %",
        f"# max: {float(np.nanmax(arr)):.4f} %",
        f"# mean: {float(np.nanmean(arr)):.4f} %",
        f"# bounds: {bounds}",
    ]
    evidence_text = "\n".join(evidence)
    print("\n" + evidence_text)

    # Write evidence file.
    evidence_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "reports",
        "inflight",
        "job-A8-engine-20260609",
        "evidence",
    )
    try:
        os.makedirs(evidence_dir, exist_ok=True)
        with open(os.path.join(evidence_dir, "gridmet_live.txt"), "w") as fh:
            fh.write(evidence_text + "\n")
    except OSError:
        pass
