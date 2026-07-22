"""Unit + live tests for the ``fetch_hrsl_population`` atomic tool (job-0112).

Coverage:

Unit (no network):
- Tool is registered in TOOL_REGISTRY with expected metadata.
- ``bbox=None`` raises ``HRSLBboxRequiredError`` (BBOX_REQUIRED, not-retryable).
- Malformed / degenerate / out-of-range bbox raises ``HRSLInputError``.
- bbox over Antarctica (outside HRSL coverage) raises ``HRSLEmptyError``.
- Unknown ``source`` raises ``HRSLInputError``.
- Cache miss invokes ``_fetch_hrsl_bytes`` and writes the fake store.
- Cache hit on second identical call skips ``_fetch_hrsl_bytes``.
- ``LayerURI`` shape (raster, persons_per_cell, bbox carried through).
- ``_bbox_intersects_coverage`` reports CONUS in / Antarctic out.
- ``_round_bbox_to_6dp`` quantizes correctly.

Live (env-guarded by GRACE2_TEST_LIVE_HRSL=1):
- Fort Myers bbox returns a CRS-tagged COG with population sum in a
  plausible range (~10⁵ persons; matches the manual probe of 380,701).
- Geographic-correctness: written COG bounds fall inside the requested bbox;
  CRS is EPSG:4326.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_hrsl_population import (
    HRSLBboxRequiredError,
    HRSLEmptyError,
    HRSLError,
    HRSLInputError,
    _bbox_intersects_coverage,
    _HRSL_COVERAGE_BBOX,
    _round_bbox_to_6dp,
    fetch_hrsl_population,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers, FL bbox (small enough to be fast in live mode; well inside HRSL).
_FORT_MYERS_BBOX = (-82.0, 26.5, -81.8, 26.7)

# Antarctica bbox (south of -56°S — outside HRSL coverage).
_ANTARCTICA_BBOX = (0.0, -80.0, 5.0, -75.0)

_LIVE_HRSL = os.environ.get("GRACE2_TEST_LIVE_HRSL") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors sibling fetcher tests).
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


def _fake_cog_bytes(tag: str = "TEST") -> bytes:
    """Placeholder bytes — these tests don't actually parse them."""
    return b"FAKE_HRSL_COG_" + tag.encode() + b"\x00" * 16


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
# Registration tests
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_hrsl_population appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_hrsl_population" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_hrsl_population"]
    assert entry.metadata.name == "fetch_hrsl_population"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "hrsl_population"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Typed-error tests (no network)
# ---------------------------------------------------------------------------


def test_bbox_none_raises_bbox_required():
    """None bbox raises HRSLBboxRequiredError with BBOX_REQUIRED code."""
    with pytest.raises(HRSLBboxRequiredError) as exc_info:
        fetch_hrsl_population(bbox=None)  # type: ignore[arg-type]
    assert exc_info.value.error_code == "BBOX_REQUIRED"
    assert exc_info.value.retryable is False


def test_degenerate_bbox_raises_input_error():
    """A min==max bbox raises HRSLInputError."""
    with pytest.raises(HRSLInputError):
        fetch_hrsl_population(bbox=(-82.0, 26.5, -82.0, 26.5))


def test_out_of_range_lon_raises_input_error():
    """A longitude > 180° raises HRSLInputError."""
    with pytest.raises(HRSLInputError):
        fetch_hrsl_population(bbox=(-200.0, 26.5, -80.0, 27.0))


def test_unknown_source_raises_input_error():
    """An unknown source raises HRSLInputError before any download."""
    with pytest.raises(HRSLInputError, match="unknown source"):
        fetch_hrsl_population(bbox=_FORT_MYERS_BBOX, source="bogus_source")


def test_antarctica_bbox_raises_empty_via_fetch():
    """bbox over Antarctica (outside HRSL coverage) raises HRSLEmptyError.

    Calls the inner ``_fetch_hrsl_bytes`` directly to avoid the cache short-circuit
    that would otherwise call rasterio for an Antarctica bbox.
    """
    from grace2_agent.tools.fetch_hrsl_population import _fetch_hrsl_bytes

    with pytest.raises(HRSLEmptyError):
        _fetch_hrsl_bytes(bbox=_ANTARCTICA_BBOX)


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------


def test_bbox_intersects_coverage_includes_fort_myers():
    """_bbox_intersects_coverage returns True for a CONUS bbox."""
    assert _bbox_intersects_coverage(_FORT_MYERS_BBOX) is True


def test_bbox_intersects_coverage_excludes_antarctica():
    """_bbox_intersects_coverage returns False for an Antarctica bbox."""
    assert _bbox_intersects_coverage(_ANTARCTICA_BBOX) is False


def test_round_bbox_to_6dp_quantizes_correctly():
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-82.123456789, 26.123456789, -81.987654321, 26.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-82.123457, 26.123457, -81.987654, 26.987654)


def test_coverage_bbox_constants_are_sane():
    """Coverage envelope spans the full longitude range and roughly -56..72° lat."""
    cmin_lon, cmin_lat, cmax_lon, cmax_lat = _HRSL_COVERAGE_BBOX
    assert cmin_lon == -180.0 and cmax_lon == 180.0
    assert -60.0 < cmin_lat < -55.0  # ~ -56°
    assert 70.0 < cmax_lat < 75.0    # ~ 72°


# ---------------------------------------------------------------------------
# Cache-layer tests (patched fetch + fake GCS)
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_and_writes_store():
    """On cache miss, the inner fetcher is called once and the fake store is written."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_cog_bytes("MISS")

    def fake_inner(bbox, year=2020, source="meta_hrsl"):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_hrsl_population._fetch_hrsl_bytes",
        side_effect=fake_inner,
    ), patch(
        "grace2_agent.tools.fetch_hrsl_population.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_hrsl_population(bbox=_FORT_MYERS_BBOX)

    assert fetch_count["n"] == 1, "Inner fetcher should be called once on miss"
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert "hrsl_population" in result.uri
    assert len(fake_gcs.store) == 1


def test_cache_hit_skips_fetch_fn():
    """Second call with the same params hits the cache and does NOT call the inner fetcher again."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_cog_bytes("HIT")

    def fake_inner(bbox, year=2020, source="meta_hrsl"):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_hrsl_population._fetch_hrsl_bytes",
        side_effect=fake_inner,
    ), patch(
        "grace2_agent.tools.fetch_hrsl_population.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_hrsl_population(bbox=_FORT_MYERS_BBOX)
        r2 = fetch_hrsl_population(bbox=_FORT_MYERS_BBOX)

    assert fetch_count["n"] == 1, "Second call should be a HIT (no fetch)"
    assert r1.uri == r2.uri, "Both calls should return the same cached URI"


def test_layer_uri_shape_has_persons_units_and_raster_role():
    """fetch_hrsl_population returns a LayerURI with the expected shape."""
    fake_gcs = FakeStorageClient()

    with patch(
        "grace2_agent.tools.fetch_hrsl_population._fetch_hrsl_bytes",
        return_value=_fake_cog_bytes("SHAPE"),
    ), patch(
        "grace2_agent.tools.fetch_hrsl_population.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_hrsl_population(bbox=_FORT_MYERS_BBOX)

    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "persons_per_cell"
    assert result.style_preset == "population_density"
    assert result.bbox is not None
    # bbox is quantized to 6dp, but should be very close to input.
    assert all(abs(a - b) < 1e-5 for a, b in zip(result.bbox, _FORT_MYERS_BBOX))
    assert "HRSL" in result.name
    assert "persons/cell" in result.name


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_HRSL=1 to run)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_HRSL,
    reason="Set GRACE2_TEST_LIVE_HRSL=1 to run live Meta HRSL download tests",
)
def test_live_fort_myers_returns_population_cog():
    """LIVE: open global HRSL VRT, window-read Fort Myers bbox, verify physical sanity.

    Geographic-correctness gate (job-0086 lesson):
    - The output COG bounds fall strictly inside the requested bbox.
    - CRS is EPSG:4326.
    - Population sum is in [10000, 5_000_000] persons — Fort Myers metro area
      is ~800k people, the 0.2°×0.2° bbox we use covers roughly that footprint
      (manual probe during development showed ~380k for these exact bounds).
    """
    import numpy as np
    import rasterio
    from grace2_agent.tools.fetch_hrsl_population import _fetch_hrsl_bytes

    cog_bytes = _fetch_hrsl_bytes(bbox=_FORT_MYERS_BBOX)
    assert len(cog_bytes) > 0, "COG bytes should be non-empty"

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        cog_path = f.name
        f.write(cog_bytes)

    try:
        with rasterio.open(cog_path) as src:
            # CRS must be EPSG:4326.
            assert src.crs is not None
            assert src.crs.to_epsg() == 4326, f"Expected EPSG:4326, got {src.crs}"

            # Output bounds must lie strictly inside the requested bbox.
            min_lon, min_lat, max_lon, max_lat = _FORT_MYERS_BBOX
            out_left, out_bottom, out_right, out_top = src.bounds
            # Allow a single-pixel slop on each side (~0.0003° = ~30 m).
            slop = 0.001
            assert out_left >= min_lon - slop, (
                f"COG left {out_left} fell outside bbox min_lon {min_lon}"
            )
            assert out_right <= max_lon + slop, (
                f"COG right {out_right} fell outside bbox max_lon {max_lon}"
            )
            assert out_bottom >= min_lat - slop, (
                f"COG bottom {out_bottom} fell outside bbox min_lat {min_lat}"
            )
            assert out_top <= max_lat + slop, (
                f"COG top {out_top} fell outside bbox max_lat {max_lat}"
            )

            # Read pixel data and verify physical sanity.
            arr = src.read(1)
            finite_pixels = int(np.isfinite(arr).sum())
            total_population = float(np.nansum(arr))
            max_pixel = float(np.nanmax(arr))

            assert finite_pixels > 1000, (
                f"Expected >1000 valid pixels in Fort Myers bbox; got {finite_pixels}"
            )
            assert 10_000 < total_population < 5_000_000, (
                f"Fort Myers metro population sum should be in 10k–5M range; "
                f"got {total_population:.1f}"
            )
            # No single 30 m pixel should exceed ~10,000 people.
            assert 0 < max_pixel < 10_000, (
                f"Max pixel {max_pixel} outside plausible HRSL range"
            )

            # Write evidence for the report.
            evidence_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "..",
                "evidence",
            )
            evidence_dir = os.path.abspath(evidence_dir)
            os.makedirs(evidence_dir, exist_ok=True)
            evidence_path = os.path.join(evidence_dir, "hrsl_live.txt")
            with open(evidence_path, "w") as out:
                out.write(
                    f"# fetch_hrsl_population live test (job-0112)\n"
                    f"bbox = {_FORT_MYERS_BBOX}\n"
                    f"cog_bytes = {len(cog_bytes)}\n"
                    f"crs = {src.crs}\n"
                    f"bounds = {src.bounds}\n"
                    f"shape = ({src.height}, {src.width})\n"
                    f"finite_pixels = {finite_pixels}\n"
                    f"sum_population = {total_population:.1f}\n"
                    f"max_pixel = {max_pixel:.2f}\n"
                )
            print(
                f"\nfetch_hrsl_population live evidence written to {evidence_path}"
            )
    finally:
        try:
            os.unlink(cog_path)
        except OSError:
            pass
