"""Unit + live tests for ``fetch_gcn250_curve_numbers`` (job-0113).

Coverage (no network needed):
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Invalid bbox (None / degenerate / out-of-range) raises typed error.
- Invalid antecedent_moisture raises typed error.
- Bbox outside coverage (Antarctica) raises ``GCN250EmptyError``.
- Mocked rasterio + ``/vsicurl/`` open → bbox-clipped GeoTIFF, AMC-I vs AMC-III
  produce different CN values at the same location (geographic-correctness
  flavor: AMC-III >= AMC-II >= AMC-I per SCS curve-number theory).
- All-nodata window → ``GCN250EmptyError``.
- Cache miss invokes fetch_fn + writes bytes.
- Cache hit skips fetch_fn (second call returns same URI).

Live tests (network-gated by ``TRID3NT_TEST_LIVE_GCN=1`` — downloads ~640 MB
GCN250 ARCII GeoTIFF on first invocation, so disabled by default):
- Fort Myers FL bbox returns valid CN GeoTIFF with mean in [60, 95] range
  (urban-coastal Florida); evidence written to evidence/gcn250_live.txt.
- AMC-I vs AMC-III at same location: AMC-III mean > AMC-II mean > AMC-I mean
  (SCS curve-number theory — wetter conditions → higher runoff potential →
  higher CN). Geographic-correctness gate per job-0086.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_gcn250_curve_numbers import (
    GCN250BboxRequiredError,
    GCN250EmptyError,
    GCN250InputError,
    GCN250UpstreamError,
    _AMC_TO_FILE_URL,
    _fetch_gcn250_bytes,
    fetch_gcn250_curve_numbers,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------


_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
_LIVE_GCN = os.environ.get("TRID3NT_TEST_LIVE_GCN") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing — mirrors test_fetch_hrsl_population / test_fetch_firms_active_fire pattern.
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

    def upload_from_string(
        self, data: bytes, content_type: str | None = None
    ) -> None:
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


# ---------------------------------------------------------------------------
# Synthetic GeoTIFF helpers for non-network unit tests.
# ---------------------------------------------------------------------------


def _synth_gcn250_tif_bytes(
    cn_value: int = 75,
    bbox: tuple[float, float, float, float] = (-82.0, 26.0, -81.0, 27.0),
    width: int = 32,
    height: int = 32,
    nodata: int = 255,
) -> bytes:
    """Build a tiny synthetic GeoTIFF with a uniform curve-number raster.

    Used to seed the local "source" for tests that bypass network I/O.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    arr = np.full((height, width), cn_value, dtype=np.int16)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_gcn250_synth_")
    os.close(fd)
    try:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            dtype="int16",
            count=1,
            height=height,
            width=width,
            crs="EPSG:4326",
            transform=transform,
            nodata=nodata,
        ) as dst:
            dst.write(arr, 1)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Registration tests (no network).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_gcn250_curve_numbers appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_gcn250_curve_numbers" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_gcn250_curve_numbers"]
    assert entry.metadata.name == "fetch_gcn250_curve_numbers"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "gcn250_curve_numbers"
    assert entry.metadata.cacheable is True


def test_amc_to_file_url_has_three_entries():
    """All three AMC levels map to figshare URLs."""
    assert set(_AMC_TO_FILE_URL.keys()) == {"dry", "average", "wet"}
    for url in _AMC_TO_FILE_URL.values():
        assert url.startswith("https://ndownloader.figshare.com/files/"), (
            f"unexpected url scheme: {url}"
        )


# ---------------------------------------------------------------------------
# Typed-error tests (no network).
# ---------------------------------------------------------------------------


def test_none_bbox_raises_bbox_required():
    """bbox=None raises GCN250BboxRequiredError (not retryable)."""
    with pytest.raises(GCN250BboxRequiredError, match="bbox is required"):
        fetch_gcn250_curve_numbers(bbox=None)  # type: ignore[arg-type]


def test_invalid_bbox_raises_typed_error():
    """Degenerate or out-of-range bbox raises GCN250InputError."""
    # Degenerate (min == max).
    with pytest.raises(GCN250InputError, match="degenerate"):
        fetch_gcn250_curve_numbers(bbox=(-82.0, 26.0, -82.0, 27.0))
    # Lon out of range.
    with pytest.raises(GCN250InputError, match="lon"):
        fetch_gcn250_curve_numbers(bbox=(-200.0, 26.0, -81.0, 27.0))
    # Lat out of range.
    with pytest.raises(GCN250InputError, match="lat"):
        fetch_gcn250_curve_numbers(bbox=(-82.0, -100.0, -81.0, 27.0))
    # Wrong number of elements.
    with pytest.raises(GCN250InputError, match="bbox must be"):
        fetch_gcn250_curve_numbers(bbox=(-82.0, 26.0, -81.0))  # type: ignore[arg-type]


def test_unknown_antecedent_moisture_raises_typed_error():
    """Unknown antecedent_moisture value raises GCN250InputError (not retryable)."""
    with pytest.raises(GCN250InputError, match="antecedent_moisture"):
        fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0),
            antecedent_moisture="saturated",  # type: ignore[arg-type]
        )


def test_input_errors_are_not_retryable():
    """All input errors expose retryable=False per FR-AS-11."""
    try:
        fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0),
            antecedent_moisture="bogus",  # type: ignore[arg-type]
        )
    except GCN250InputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected GCN250InputError")


def test_bbox_outside_coverage_raises_empty_error():
    """Antarctic bbox (below -57° lat) is outside GCN250 coverage."""
    # The _fetch_gcn250_bytes layer is what enforces this; the outer
    # signature only validates bbox bounds (not coverage).
    with pytest.raises(GCN250EmptyError, match="outside GCN250"):
        _fetch_gcn250_bytes(
            bbox=(-60.0, -80.0, -55.0, -70.0),
            antecedent_moisture="average",
        )


# ---------------------------------------------------------------------------
# Mocked rasterio open — verify window-read + clip + write semantics.
# ---------------------------------------------------------------------------


def test_mocked_vsicurl_open_returns_clipped_geotiff(tmp_path):
    """Mocked rasterio.open returning a synthetic CN raster yields a clipped GeoTIFF.

    Verifies the core path: open via /vsicurl/, window-read, write GeoTIFF.
    The synthetic raster carries CN=75 everywhere; the bbox is centered inside
    the synthetic source extent so the window should be fully covered.
    """
    src_path = tmp_path / "synth_src.tif"
    src_path.write_bytes(_synth_gcn250_tif_bytes(cn_value=75))

    # Patch the /vsicurl/ open so it reads from our local synthetic file.
    import rasterio
    real_open = rasterio.open
    open_calls: list[str] = []

    def patched_open(path, *args, **kwargs):
        open_calls.append(str(path))
        if str(path).startswith("/vsicurl/"):
            return real_open(str(src_path))
        return real_open(path, *args, **kwargs)

    with patch("rasterio.open", side_effect=patched_open):
        # Bbox inside synthetic source: source is (-82, 26, -81, 27);
        # request inside.
        result = _fetch_gcn250_bytes(
            bbox=(-81.9, 26.1, -81.5, 26.5),
            antecedent_moisture="average",
        )

    assert open_calls, "expected at least one rasterio.open call"
    assert any("/vsicurl/" in c for c in open_calls), (
        "expected a /vsicurl/-prefixed URL"
    )

    # Result is a valid GeoTIFF — open it back and verify CN values.
    out_path = tmp_path / "out.tif"
    out_path.write_bytes(result)
    import rasterio as rio
    with rio.open(out_path) as out:
        assert out.crs.to_epsg() == 4326, "output must be EPSG:4326"
        arr = out.read(1)
        # All cells should be CN=75 (uniform synthetic source).
        assert arr.min() == 75
        assert arr.max() == 75
        # Bounds should overlap the requested bbox.
        b = out.bounds
        assert b.left >= -82.0 and b.right <= -81.0, (
            f"output bounds {b} not within source extent"
        )
        # Tags carry the AMC + source labels.
        tags = out.tags()
        assert tags.get("antecedent_moisture") == "average"
        assert tags.get("units") == "curve_number"


def test_amc_dry_vs_wet_give_different_cn_at_same_location(tmp_path):
    """AMC-III (wet) and AMC-I (dry) GCN250 files should return different CN values.

    Per SCS curve-number theory and the Jaafar et al. 2019 dataset: at any
    pixel, CN_AMC_III >= CN_AMC_II >= CN_AMC_I (drier antecedent → lower
    runoff potential → lower CN). We seed the mocked source with that
    inequality and verify it round-trips end-to-end (CN field semantics
    preserved, not just bytes round-tripped — geographic-correctness flavor).
    """
    dry_src = tmp_path / "dry.tif"
    avg_src = tmp_path / "avg.tif"
    wet_src = tmp_path / "wet.tif"
    dry_src.write_bytes(_synth_gcn250_tif_bytes(cn_value=55))   # AMC-I
    avg_src.write_bytes(_synth_gcn250_tif_bytes(cn_value=75))   # AMC-II
    wet_src.write_bytes(_synth_gcn250_tif_bytes(cn_value=90))   # AMC-III

    import rasterio
    real_open = rasterio.open

    def patched_open(path, *args, **kwargs):
        if "/vsicurl/" in str(path):
            url = str(path).replace("/vsicurl/", "")
            if url == _AMC_TO_FILE_URL["dry"]:
                return real_open(str(dry_src))
            if url == _AMC_TO_FILE_URL["average"]:
                return real_open(str(avg_src))
            if url == _AMC_TO_FILE_URL["wet"]:
                return real_open(str(wet_src))
            raise AssertionError(f"unexpected vsicurl url {url}")
        return real_open(path, *args, **kwargs)

    bbox = (-81.9, 26.1, -81.5, 26.5)
    with patch("rasterio.open", side_effect=patched_open):
        dry_bytes = _fetch_gcn250_bytes(bbox=bbox, antecedent_moisture="dry")
        avg_bytes = _fetch_gcn250_bytes(bbox=bbox, antecedent_moisture="average")
        wet_bytes = _fetch_gcn250_bytes(bbox=bbox, antecedent_moisture="wet")

    out_dry = tmp_path / "out_dry.tif"
    out_avg = tmp_path / "out_avg.tif"
    out_wet = tmp_path / "out_wet.tif"
    out_dry.write_bytes(dry_bytes)
    out_avg.write_bytes(avg_bytes)
    out_wet.write_bytes(wet_bytes)

    with rasterio.open(out_dry) as src:
        dry_mean = float(src.read(1).mean())
    with rasterio.open(out_avg) as src:
        avg_mean = float(src.read(1).mean())
    with rasterio.open(out_wet) as src:
        wet_mean = float(src.read(1).mean())

    # SCS curve-number monotonicity preserved through the read/clip/write path.
    assert dry_mean < avg_mean < wet_mean, (
        f"expected AMC-I (dry, {dry_mean}) < AMC-II (avg, {avg_mean}) "
        f"< AMC-III (wet, {wet_mean})"
    )
    assert dry_mean == pytest.approx(55.0)
    assert avg_mean == pytest.approx(75.0)
    assert wet_mean == pytest.approx(90.0)


def test_all_nodata_window_raises_empty_error(tmp_path):
    """A synthetic source where all pixels are nodata raises GCN250EmptyError."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    src_path = tmp_path / "all_nodata.tif"
    arr = np.full((32, 32), 255, dtype=np.int16)  # nodata sentinel
    transform = from_bounds(-82.0, 26.0, -81.0, 27.0, 32, 32)
    with rasterio.open(
        src_path, "w",
        driver="GTiff", dtype="int16", count=1,
        height=32, width=32, crs="EPSG:4326",
        transform=transform, nodata=255,
    ) as dst:
        dst.write(arr, 1)

    real_open = rasterio.open

    def patched_open(path, *args, **kwargs):
        if "/vsicurl/" in str(path):
            return real_open(str(src_path))
        return real_open(path, *args, **kwargs)

    with patch("rasterio.open", side_effect=patched_open):
        with pytest.raises(GCN250EmptyError, match="no valid GCN250 pixels"):
            _fetch_gcn250_bytes(
                bbox=(-81.9, 26.1, -81.5, 26.5),
                antecedent_moisture="average",
            )


def test_vsicurl_open_failure_raises_upstream_error(tmp_path):
    """If rasterio.open(/vsicurl/...) raises, we surface a GCN250UpstreamError."""
    import rasterio
    real_open = rasterio.open

    def patched_open(path, *args, **kwargs):
        if "/vsicurl/" in str(path):
            raise RuntimeError("simulated 404 from upstream")
        return real_open(path, *args, **kwargs)

    with patch("rasterio.open", side_effect=patched_open):
        with pytest.raises(GCN250UpstreamError, match="could not open GCN250"):
            _fetch_gcn250_bytes(
                bbox=(-81.9, 26.1, -81.5, 26.5),
                antecedent_moisture="average",
            )


# ---------------------------------------------------------------------------
# Cache miss/hit tests (mocked GCS + mocked fetch).
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_and_writes():
    """On cache miss, _fetch_gcn250_bytes is invoked and bytes are stored in GCS."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_tif = b"II*\x00" + b"\x00" * 64 + b"_FAKE_GCN250"

    def fake_fetch(*_args, **_kwargs) -> bytes:
        fetch_count["n"] += 1
        return fake_tif

    with patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers._fetch_gcn250_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0),
            antecedent_moisture="average",
        )

    assert fetch_count["n"] == 1, "fetch_fn should fire once on cache miss"
    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "curve_number"
    assert result.uri.startswith("s3://")
    assert "gcn250_curve_numbers" in result.uri
    assert "static-30d" in result.uri  # ttl-class nested in path per cache layout
    assert len(fake_gcs.store) == 1, "one artifact written to fake GCS"
    # bbox echoed for zoom-to wiring.
    assert result.bbox == (-82.0, 26.0, -81.0, 27.0)


def test_cache_hit_skips_fetch():
    """Second call with same params hits the cache; fetch_fn fires only once."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def fake_fetch(*_args, **_kwargs) -> bytes:
        fetch_count["n"] += 1
        return b"II*\x00" + b"\x00" * 64 + b"_FAKE_GCN250_CACHED"

    with patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers._fetch_gcn250_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0), antecedent_moisture="average"
        )
        assert fetch_count["n"] == 1
        r2 = fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0), antecedent_moisture="average"
        )
        assert fetch_count["n"] == 1, "cache hit must skip fetch_fn"
        assert r1.uri == r2.uri


def test_different_amc_gives_different_cache_key():
    """AMC=dry and AMC=average produce different cache paths (cache key includes AMC)."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def fake_fetch(*_args, **_kwargs) -> bytes:
        fetch_count["n"] += 1
        return b"II*\x00" + b"\x00" * 64 + bytes([fetch_count["n"]])

    with patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers._fetch_gcn250_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        dry_result = fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0), antecedent_moisture="dry"
        )
        avg_result = fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0), antecedent_moisture="average"
        )
        wet_result = fetch_gcn250_curve_numbers(
            bbox=(-82.0, 26.0, -81.0, 27.0), antecedent_moisture="wet"
        )

    # All three URIs should be distinct (different cache keys).
    assert dry_result.uri != avg_result.uri != wet_result.uri
    assert dry_result.uri != wet_result.uri
    assert fetch_count["n"] == 3, "three distinct misses"
    assert len(fake_gcs.store) == 3


# ---------------------------------------------------------------------------
# Live test — only runs with TRID3NT_TEST_LIVE_GCN=1 (downloads ~640 MB).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_GCN,
    reason="set TRID3NT_TEST_LIVE_GCN=1 to run live GCN250 test (downloads ~640 MB)",
)
def test_live_fetch_fort_myers_florida(tmp_path):
    """Live GCN250 round-trip — Fort Myers FL bbox returns urban-coastal CN raster.

    Geographic-correctness gate (job-0086): Fort Myers FL is a coastal urban
    area; SCS curve numbers for the mixed urban + wetland + impervious
    surface land cover typically fall in [60, 95] for AMC-II. We assert:
    - The output bounds lie inside the requested bbox.
    - Mean CN in [60, 95] (verifies the bytes carry real-Earth CN values,
      not zeros or 255 nodata).
    - Output GeoTIFF carries CRS=EPSG:4326 and units="curve_number" tags.
    - AMC-III mean > AMC-I mean (the wet vs dry inequality holds end-to-end).

    Writes evidence to ``evidence/gcn250_live.txt`` per kickoff.
    """
    fake_gcs = FakeStorageClient()

    # Small Fort Myers FL bbox — kickoff: (-82, 26, -81, 27) is too large
    # at ~1° (≈ 5 MB). Use a tighter ~0.4° square for live-test speed.
    bbox = (-81.95, 26.55, -81.55, 26.85)

    with patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result_avg = fetch_gcn250_curve_numbers(
            bbox=bbox, antecedent_moisture="average"
        )

    assert result_avg.layer_type == "raster"
    assert result_avg.units == "curve_number"
    assert len(fake_gcs.store) == 1
    tif_bytes = next(iter(fake_gcs.store.values()))

    out_path = tmp_path / "live_gcn250_avg.tif"
    out_path.write_bytes(tif_bytes)

    import rasterio
    with rasterio.open(out_path) as src:
        assert src.crs.to_epsg() == 4326
        b = src.bounds
        # Bounds must overlap the requested bbox (allow small tolerance for
        # pixel-snap on outward round).
        assert b.left >= bbox[0] - 0.01 and b.right <= bbox[2] + 0.01
        assert b.bottom >= bbox[1] - 0.01 and b.top <= bbox[3] + 0.01
        arr = src.read(1)
        nodata = src.nodata if src.nodata is not None else 255
        valid = arr[arr != nodata]
        assert len(valid) > 0, "no valid CN pixels"
        avg_mean = float(valid.mean())
        # Urban-coastal Florida expected range [60, 95] for AMC-II.
        assert 60.0 <= avg_mean <= 95.0, (
            f"AMC-II mean CN {avg_mean} outside expected [60, 95] for Fort Myers"
        )
        avg_min = int(valid.min())
        avg_max = int(valid.max())
        tags = src.tags()
        assert tags.get("units") == "curve_number"
        assert tags.get("antecedent_moisture") == "average"

    # AMC-I vs AMC-III sanity at the same bbox: wet should be > dry.
    fake_gcs2 = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_gcn250_curve_numbers.read_through",
        side_effect=_make_read_through_injector(fake_gcs2),
    ):
        fetch_gcn250_curve_numbers(bbox=bbox, antecedent_moisture="dry")
        fetch_gcn250_curve_numbers(bbox=bbox, antecedent_moisture="wet")

    dry_bytes, wet_bytes = list(fake_gcs2.store.values())
    dry_path = tmp_path / "live_gcn250_dry.tif"
    wet_path = tmp_path / "live_gcn250_wet.tif"
    dry_path.write_bytes(dry_bytes)
    wet_path.write_bytes(wet_bytes)
    with rasterio.open(dry_path) as src:
        arr_dry = src.read(1)
        nodata_dry = src.nodata if src.nodata is not None else 255
        dry_mean = float(arr_dry[arr_dry != nodata_dry].mean())
    with rasterio.open(wet_path) as src:
        arr_wet = src.read(1)
        nodata_wet = src.nodata if src.nodata is not None else 255
        wet_mean = float(arr_wet[arr_wet != nodata_wet].mean())

    assert wet_mean > dry_mean, (
        f"AMC-III mean ({wet_mean}) should exceed AMC-I mean ({dry_mean}) "
        "per SCS curve-number theory"
    )

    # Persist evidence per kickoff.
    evidence_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
        "reports", "inflight", "job-0113-engine-20260608", "evidence",
    )
    evidence_dir = os.path.abspath(evidence_dir)
    os.makedirs(evidence_dir, exist_ok=True)
    evidence_path = os.path.join(evidence_dir, "gcn250_live.txt")
    with open(evidence_path, "w") as f:
        f.write(
            f"fetch_gcn250_curve_numbers live test — Fort Myers FL\n"
            f"bbox: {bbox}\n"
            f"AMC-II (avg): mean={avg_mean:.2f}, min={avg_min}, max={avg_max}, "
            f"size={len(tif_bytes)} bytes\n"
            f"AMC-I  (dry): mean={dry_mean:.2f}\n"
            f"AMC-III (wet): mean={wet_mean:.2f}\n"
            f"AMC-III > AMC-I: {wet_mean > dry_mean}\n"
            f"source: Figshare DOI 10.6084/m9.figshare.7756202\n"
        )
    print(
        f"\nlive_test: Fort Myers AMC-II mean CN = {avg_mean:.1f}, "
        f"AMC-I = {dry_mean:.1f}, AMC-III = {wet_mean:.1f}; "
        f"evidence written to {evidence_path}"
    )
