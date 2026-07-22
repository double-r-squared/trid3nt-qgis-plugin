"""Unit tests for the ``fetch_cama_flood_discharge`` atomic tool (job-0133).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata (incl. Wave 1.5
  flags: ``supports_global_query=False`` and the payload-MB estimator name).
- Validation: bad bbox / bad version / bad date range raise typed errors.
- Base-URL resolution: explicit kwarg > env var > legacy default.
- Candidate-filename helper produces the expected probe order.
- Mocked happy-path: a fake HTTP responder serves a synthetic CaMa-Flood
  netCDF; the tool clips it, mean-aggregates across time, writes a COG,
  routes through the fake GCS shim with the expected
  ``cache/static-30d/cama_flood/<key>.tif`` path.
- Two distinct date ranges produce two distinct cache keys.
- Cache hit: identical params skip the HTTP fetch.
- HTML migration sentinel: an HTTP body that begins with ``<`` surfaces
  as ``CaMaFloodUnreachableError`` (retryable=False).
- Geographic-correctness gate (job-0086): an output COG's bounds intersect
  the requested bbox; longitude-360 input is normalized to -180..180
  before clip.
- payload-MB estimator matches the audit.md spec (1 MB/day/1° square).

Live test (env-gated ``TRID3NT_TEST_LIVE_CAMA=1`` + a mirror URL via
``TRID3NT_CAMA_FLOOD_BASE_URL``):
- Mississippi-basin bbox + recent date → real discharge raster.
- Evidence emitted to ``evidence/cama_live.txt`` per the kickoff.

Note: as of 2026-02-12 the kickoff-named U.Tokyo Hydra URL returns an HTML
migration page (OQ-0133-CAMA-DATA-SOURCE-MIGRATION). Without a mirror env
var set, the live test is skipped + reported in the report's Verification
section as ``qualified``.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_cama_flood_discharge import (
    CaMaFloodEmptyError,
    CaMaFloodInputError,
    CaMaFloodUnreachableError,
    CaMaFloodUpstreamError,
    _candidate_filenames,
    _resolve_base_url,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_date_range,
    _validate_version,
    estimate_payload_mb,
    fetch_cama_flood_discharge,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Mississippi basin (~Vicksburg, MS) — small live-test bbox per the kickoff.
_MISSISSIPPI_BBOX = (-92.0, 30.0, -89.0, 32.0)

# Smaller 1° square inside the Mississippi basin for synthetic-data tests.
_SMALL_BBOX = (-91.0, 31.0, -90.0, 32.0)

_LIVE_CAMA = os.environ.get("TRID3NT_TEST_LIVE_CAMA") == "1"
_LIVE_BASE_URL = os.environ.get("TRID3NT_CAMA_FLOOD_BASE_URL")


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_era5_reanalysis pattern).
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


def _write_synthetic_cama_netcdf(
    out_path: str,
    bbox: tuple[float, float, float, float],
    *,
    n_days: int = 1,
    lon_360: bool = False,
    lat_descending: bool = False,
    var_name: str = "rivout",
) -> None:
    """Write a tiny CaMa-Flood-shaped NetCDF to ``out_path``.

    Variable name defaults to ``rivout`` (CaMa-Flood's standard river-outflow
    variable). Dims (time, lat, lon) at 0.1° native CaMa-Flood resolution.

    When ``lon_360=True`` the longitudes are written in 0..360 space so the
    converter's normalization path is exercised. When ``lat_descending=True``
    the latitudes are reversed (north → south).
    """
    import numpy as np
    import xarray as xr

    west, south, east, north = bbox
    res = 0.1
    lats = np.arange(south, north + 0.001, res)
    lons = np.arange(west, east + 0.001, res)

    if lon_360:
        # Convert lons to 0..360 (shift negatives).
        lons = np.where(lons < 0, lons + 360, lons)
        lons = np.sort(lons)
    if lat_descending:
        lats = lats[::-1]

    times = np.array(
        [
            np.datetime64(_dt.datetime(2024, 9, 1) + _dt.timedelta(days=d), "ns")
            for d in range(n_days)
        ]
    )

    rng = np.random.default_rng(seed=42)
    # Discharge in m^3/s — Mississippi basin ranges ~10^3 — 10^4 typical.
    arr = (rng.random((len(times), len(lats), len(lons))).astype(np.float32)
           * 5000.0 + 100.0)

    da = xr.DataArray(
        arr,
        dims=("time", "latitude", "longitude"),
        coords={
            "time": times,
            "latitude": lats,
            "longitude": lons,
        },
        name=var_name,
        attrs={"long_name": "river discharge", "units": "m^3/s"},
    )
    ds = da.to_dataset()
    ds.to_netcdf(out_path)


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_cama_flood_discharge appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_cama_flood_discharge" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_cama_flood_discharge"]
    assert entry.metadata.name == "fetch_cama_flood_discharge"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "cama_flood"
    assert entry.metadata.cacheable is True
    # supports_global_query=False per audit.md (global = ~500MB / day).
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_fr_dc_6_cross_field_consistency():
    """Registered metadata satisfies FR-DC-6 (cacheable ⇒ ttl != live, src non-empty)."""
    md = TOOL_REGISTRY["fetch_cama_flood_discharge"].metadata
    assert md.cacheable is True
    assert md.ttl_class != "live-no-cache"
    assert md.source_class


# ---------------------------------------------------------------------------
# Validation / typed-error tests
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(CaMaFloodInputError):
        _validate_bbox((-92.0, 30.0, -92.0, 30.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(CaMaFloodInputError):
        _validate_bbox((-181.0, 30.0, -89.0, 32.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(CaMaFloodInputError):
        _validate_bbox((-92.0, 30.0, -89.0, 91.0))


def test_non_iso_date_raises_input_error():
    with pytest.raises(CaMaFloodInputError):
        _validate_date_range("2024/09/01", "2024-09-01")


def test_inverted_date_range_raises_input_error():
    with pytest.raises(CaMaFloodInputError, match="start_date must be <= end_date"):
        _validate_date_range("2024-09-02", "2024-09-01")


def test_huge_date_range_raises_input_error():
    with pytest.raises(CaMaFloodInputError, match="exceeds hard cap"):
        _validate_date_range("2020-01-01", "2024-01-01")


def test_unknown_version_raises_input_error():
    with pytest.raises(CaMaFloodInputError, match="unsupported CaMa-Flood version"):
        _validate_version("v9.9.9")


def test_input_error_is_not_retryable():
    """CaMaFloodInputError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_cama_flood_discharge(
            bbox=(-92.0, 30.0, -89.0, 32.0),
            start_date="invalid",
            end_date="2024-09-01",
        )
    except CaMaFloodInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected CaMaFloodInputError")


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-92.123456789, 30.123456789, -89.987654321, 32.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-92.123457, 30.123457, -89.987654, 32.987654)


def test_candidate_filenames_known_year_version():
    """Candidate-probe list includes the canonical operational naming."""
    names = _candidate_filenames(2024, "v4.0.1")
    assert "discharge_v4.0.1_2024.nc" == names[0]
    assert "runoff_v4.0.1_2024.nc" in names
    assert "runoff_VIC_BC_2024.nc" in names
    # All entries end with .nc and contain the year.
    assert all(n.endswith(".nc") for n in names)
    assert all("2024" in n for n in names)


def test_resolve_base_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", "https://env-mirror.example.com/cama/")
    url = _resolve_base_url("https://explicit-mirror.example.com/cama/")
    assert url.startswith("https://explicit-mirror.example.com/")
    assert url.endswith("/")


def test_resolve_base_url_env_fallback(monkeypatch):
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", "https://env-mirror.example.com/cama/")
    url = _resolve_base_url(None)
    assert "env-mirror.example.com" in url
    assert url.endswith("/")


def test_resolve_base_url_legacy_default(monkeypatch):
    monkeypatch.delenv("TRID3NT_CAMA_FLOOD_BASE_URL", raising=False)
    url = _resolve_base_url(None)
    assert "hydro.iis.u-tokyo.ac.jp" in url
    assert url.endswith("/")


def test_estimate_payload_mb_matches_audit_md_spec():
    """1 MB / day / 1° square per audit.md."""
    one_day_one_deg = estimate_payload_mb(
        bbox=(-92.0, 30.0, -91.0, 31.0),
        start_date="2024-09-01",
        end_date="2024-09-01",
    )
    assert 0.8 <= one_day_one_deg <= 1.2

    two_days_four_deg = estimate_payload_mb(
        bbox=(-92.0, 30.0, -90.0, 32.0),  # 2°×2° = 4 sq deg
        start_date="2024-09-01",
        end_date="2024-09-02",
    )
    assert 7.0 <= two_days_four_deg <= 9.0

    global_mb = estimate_payload_mb(
        bbox=None,
        start_date="2024-09-01",
        end_date="2024-09-01",
    )
    assert global_mb > 10_000


# ---------------------------------------------------------------------------
# Mocked-HTTP happy-path tests
# ---------------------------------------------------------------------------


class _FakeHTTPClient:
    """Minimal httpx.Client replacement for tests.

    Behavior:
    - On ``stream("GET", url)``: looks up ``url`` in a routing map. The map
      can return either bytes (served as a netCDF) or a string (served as
      the HTML migration sentinel) or None (404).
    """

    def __init__(self, routing: dict[str, bytes | str | None]) -> None:
        self._routing = routing
        self.urls_called: list[str] = []
        self.closed = False

    def __enter__(self) -> "_FakeHTTPClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def stream(self, method: str, url: str):
        return _FakeStreamCtx(self, method, url)


class _FakeStreamCtx:
    def __init__(self, client: _FakeHTTPClient, method: str, url: str) -> None:
        self.client = client
        self.method = method
        self.url = url

    def __enter__(self) -> "_FakeStreamResponse":
        self.client.urls_called.append(self.url)
        payload = self.client._routing.get(self.url)
        if payload is None:
            return _FakeStreamResponse(404, b"")
        if isinstance(payload, str):
            # HTML sentinel.
            return _FakeStreamResponse(200, payload.encode("utf-8"))
        return _FakeStreamResponse(200, payload)

    def __exit__(self, *args) -> None:
        return None


class _FakeStreamResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def iter_bytes(self, chunk_size: int = 1024 * 1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def _route_for(synthetic_nc_bytes: bytes, base_url: str, year: int) -> dict[str, bytes | str | None]:
    """Build a routing map where the FIRST candidate URL serves the netCDF."""
    from trid3nt_server.tools.fetch_cama_flood_discharge import _candidate_filenames
    names = _candidate_filenames(year, "v4.0.1")
    return {base_url + names[0]: synthetic_nc_bytes}


def _make_synthetic_nc_bytes(bbox: tuple[float, float, float, float], **kw) -> bytes:
    """Build a synthetic CaMa-Flood netCDF in-memory and return its bytes."""
    fd, path = tempfile.mkstemp(suffix=".nc", prefix="trid3nt_cama_test_")
    os.close(fd)
    try:
        _write_synthetic_cama_netcdf(path, bbox, **kw)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_happy_path_writes_cog(monkeypatch):
    """Fake HTTP serves a synthetic CaMa-Flood netCDF; tool writes a COG to GCS."""
    fake_gcs = FakeStorageClient()
    base_url = "https://mirror.example.com/cama/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    nc_bytes = _make_synthetic_nc_bytes(_SMALL_BBOX, n_days=2)
    routing = _route_for(nc_bytes, base_url, 2024)

    def fake_client_factory(*args, **kwargs):
        return _FakeHTTPClient(routing)

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=fake_client_factory,
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-02",
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "m^3/s"

    # Cache path layout.
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/cama_flood/")
    assert path.endswith(".tif")
    # The written COG bytes look like a TIFF.
    assert data[:2] in (b"II", b"MM")


def test_distinct_dates_produce_distinct_cache_keys(monkeypatch):
    """Two different date ranges should write to two different cache paths."""
    fake_gcs = FakeStorageClient()
    base_url = "https://mirror.example.com/cama/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    nc_bytes = _make_synthetic_nc_bytes(_SMALL_BBOX, n_days=2)
    routing = _route_for(nc_bytes, base_url, 2024)

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=lambda *a, **kw: _FakeHTTPClient(routing),
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )
        r2 = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-02",
            end_date="2024-09-02",
        )

    assert r1.uri != r2.uri
    assert len(fake_gcs.store) == 2


def test_cache_hit_skips_http(monkeypatch):
    """Second call with identical params returns the cached URI without HTTP."""
    fake_gcs = FakeStorageClient()
    base_url = "https://mirror.example.com/cama/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    nc_bytes = _make_synthetic_nc_bytes(_SMALL_BBOX, n_days=1)
    routing = _route_for(nc_bytes, base_url, 2024)

    http_calls = {"n": 0}

    def factory(*args, **kwargs):
        http_calls["n"] += 1
        return _FakeHTTPClient(routing)

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=factory,
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )
        r2 = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )

    assert r1.uri == r2.uri
    assert http_calls["n"] == 1


# ---------------------------------------------------------------------------
# HTML migration sentinel: kickoff-named URL returns HTML now.
# ---------------------------------------------------------------------------


def test_html_migration_sentinel_surfaces_as_unreachable(monkeypatch):
    """When every candidate returns HTML, the tool reports CaMaFloodUnreachableError.

    This exercises OQ-0133-CAMA-DATA-SOURCE-MIGRATION: the legacy U.Tokyo
    Hydra path now returns a Japanese-text HTML redirect for every URL
    under ``~yamadai/*``. The tool should detect that case and emit a
    typed, non-retryable error rather than a downstream NetCDF-parse
    failure.
    """
    fake_gcs = FakeStorageClient()
    base_url = "https://hydro.iis.u-tokyo.ac.jp/legacy/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    html_sentinel = (
        "<!DOCTYPE html><html><body>"
        "Webページ移行のお知らせ / Website has moved"
        "</body></html>"
    )

    # Every candidate URL returns the HTML sentinel.
    from trid3nt_server.tools.fetch_cama_flood_discharge import _candidate_filenames
    names = _candidate_filenames(2024, "v4.0.1")
    routing: dict[str, bytes | str | None] = {
        base_url + n: html_sentinel for n in names
    }

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=lambda *a, **kw: _FakeHTTPClient(routing),
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(CaMaFloodUnreachableError) as exc_info:
            fetch_cama_flood_discharge(
                bbox=_SMALL_BBOX,
                start_date="2024-09-01",
                end_date="2024-09-01",
            )

    # Verify the typed error carries the migration explanation.
    assert exc_info.value.retryable is False
    assert "migrated" in str(exc_info.value).lower()
    # No artifact should have been written.
    assert fake_gcs.store == {}


# ---------------------------------------------------------------------------
# Geographic-correctness gate (job-0086).
# ---------------------------------------------------------------------------


def test_geographic_correctness_gate_lon_360_normalization(monkeypatch):
    """A netCDF on the 0..360 grid is correctly clipped to the -180..180 bbox."""
    fake_gcs = FakeStorageClient()
    base_url = "https://mirror.example.com/cama/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    # Synthetic netCDF on the 0..360 grid (CaMa-Flood "western hemisphere"
    # longitudes become 268..271 in 0-360 space for our Mississippi bbox).
    nc_bytes = _make_synthetic_nc_bytes(_SMALL_BBOX, n_days=1, lon_360=True)
    routing = _route_for(nc_bytes, base_url, 2024)

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=lambda *a, **kw: _FakeHTTPClient(routing),
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )

    # Read back the COG and verify its bounds intersect the requested bbox.
    import rasterio
    [(_, data)] = list(fake_gcs.store.items())
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            assert src.crs is not None
            b = src.bounds
            # bounds intersect -91..-90, 31..32.
            assert b.left < _SMALL_BBOX[2]
            assert b.right > _SMALL_BBOX[0]
            assert b.bottom < _SMALL_BBOX[3]
            assert b.top > _SMALL_BBOX[1]
            # Bounds should NOT be in 0..360 space — they should be in
            # -180..180 (negative numbers for the western hemisphere).
            assert b.left < 0 or b.left < 180.0
    finally:
        os.unlink(tf_path)


def test_geographic_correctness_gate_lat_descending(monkeypatch):
    """A netCDF with latitude descending (north → south) gets sorted before clip."""
    fake_gcs = FakeStorageClient()
    base_url = "https://mirror.example.com/cama/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    nc_bytes = _make_synthetic_nc_bytes(_SMALL_BBOX, n_days=1, lat_descending=True)
    routing = _route_for(nc_bytes, base_url, 2024)

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=lambda *a, **kw: _FakeHTTPClient(routing),
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )

    import rasterio
    [(_, data)] = list(fake_gcs.store.items())
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            b = src.bounds
            # bottom < top is the rasterio convention; an unsorted lat axis
            # would have flipped this.
            assert b.bottom < b.top
            assert b.left < b.right
    finally:
        os.unlink(tf_path)


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape_fields(monkeypatch):
    """The returned LayerURI carries the documented fields."""
    fake_gcs = FakeStorageClient()
    base_url = "https://mirror.example.com/cama/"
    monkeypatch.setenv("TRID3NT_CAMA_FLOOD_BASE_URL", base_url)

    nc_bytes = _make_synthetic_nc_bytes(_SMALL_BBOX, n_days=1)
    routing = _route_for(nc_bytes, base_url, 2024)

    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.httpx.Client",
        side_effect=lambda *a, **kw: _FakeHTTPClient(routing),
    ), patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_cama_flood_discharge(
            bbox=_SMALL_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )

    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "m^3/s"
    assert result.style_preset == "cama_flood_discharge"
    assert "cama-flood" in result.layer_id.lower()
    assert "CaMa-Flood" in result.name


# ---------------------------------------------------------------------------
# Live test — real CaMa-Flood mirror (env-gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_LIVE_CAMA and _LIVE_BASE_URL),
    reason=(
        "TRID3NT_TEST_LIVE_CAMA=1 not set OR no TRID3NT_CAMA_FLOOD_BASE_URL mirror "
        "configured. The kickoff-named U.Tokyo Hydra URL returns an HTML "
        "migration page as of 2026-02-12 (OQ-0133-CAMA-DATA-SOURCE-MIGRATION); "
        "a mirror or Dropbox-link wire-up is required to exercise the live path."
    ),
)
def test_live_mississippi_basin_discharge(tmp_path):
    """LIVE: fetch CaMa-Flood discharge over the Mississippi basin for one day.

    Requires a configured mirror via ``TRID3NT_CAMA_FLOOD_BASE_URL``. Captures
    evidence to ``evidence/cama_live.txt`` per the kickoff.
    """
    import rasterio

    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_cama_flood_discharge.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_cama_flood_discharge(
            bbox=_MISSISSIPPI_BBOX,
            start_date="2024-09-01",
            end_date="2024-09-01",
        )

    assert result.uri is not None
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/cama_flood/")
    assert path.endswith(".tif")
    assert len(data) > 0

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        with rasterio.open(tf_path) as src:
            assert src.crs is not None
            b = src.bounds
            assert b.left < b.right
            assert b.bottom < b.top
            assert b.left < _MISSISSIPPI_BBOX[2]
            assert b.right > _MISSISSIPPI_BBOX[0]
            assert b.bottom < _MISSISSIPPI_BBOX[3]
            assert b.top > _MISSISSIPPI_BBOX[1]
            arr = src.read(1)
    finally:
        os.unlink(tf_path)

    import numpy as np
    n_finite = int(np.isfinite(arr).sum())
    evidence = [
        "# CaMa-Flood live test — Mississippi basin discharge",
        f"# bbox: {_MISSISSIPPI_BBOX}",
        f"# date: 2024-09-01",
        f"# mirror: {_LIVE_BASE_URL}",
        f"# result.uri: {result.uri}",
        f"# COG size: {len(data)} bytes",
        f"# raster shape: {arr.shape}",
        f"# finite pixels: {n_finite}",
        f"# min: {float(np.nanmin(arr)):.4f} m^3/s",
        f"# max: {float(np.nanmax(arr)):.4f} m^3/s",
        f"# mean: {float(np.nanmean(arr)):.4f} m^3/s",
        f"# bounds: {b}",
    ]
    evidence_text = "\n".join(evidence)
    print("\n" + evidence_text)

    evidence_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "reports",
        "inflight",
        "job-0133-engine-20260608",
        "evidence",
    )
    try:
        os.makedirs(evidence_dir, exist_ok=True)
        with open(os.path.join(evidence_dir, "cama_live.txt"), "w") as fh:
            fh.write(evidence_text + "\n")
    except OSError:
        pass
