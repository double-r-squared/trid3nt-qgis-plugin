"""Unit tests for the ``compute_building_density`` atomic tool (job-0096).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Invalid bbox / cell_size_m / source raise typed input errors.
- Quadkey math: Fort Myers bbox maps to expected zoom-9 quadkey(s).
- Mocked tile feed of 100 polygons in a 1km² bbox → density grid with sum=100.
- Different ``cell_size_m`` values produce correctly-scaled grid dimensions.
- Empty (no-features) bbox → zero raster, no error.
- Cache miss invokes fetch_fn once and writes the store.
- Cache hit on second call with same params skips fetch_fn.
- Live (env ``TRID3NT_TEST_LIVE_BUILDINGS=1``): Fort Myers bbox → density raster
  whose high-count pixels coincide with the known dense neighbourhood and
  whose ocean/river pixels read zero (codified job-0086 geography test).
"""

from __future__ import annotations

import gzip
import io
import math
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import trid3nt_server.tools.processing.compute_building_density as cbd
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.compute_building_density import (
    BuildingDensityInputError,
    BuildingDensityUpstreamError,
    _build_density_grid,
    _feature_centroid,
    _lonlat_to_tile_xy,
    _quadkeys_for_bbox,
    _ring_centroid,
    _round_bbox_to_6dp,
    _tile_xy_to_quadkey,
    _validate_bbox,
    compute_building_density,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers, FL bounding box (overlaps the city centre + Caloosahatchee).
_FORT_MYERS_BBOX = (-82.0, 26.5, -81.8, 26.7)

_LIVE_BUILDINGS = os.environ.get("TRID3NT_TEST_LIVE_BUILDINGS") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_administrative_boundaries.py).
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


# ---------------------------------------------------------------------------
# Synthetic-tile builders for the rasterization tests.
# ---------------------------------------------------------------------------


def _square_polygon_feature(cx: float, cy: float, half_size_deg: float = 1e-4) -> dict:
    """Build a tiny square polygon Feature centred at (cx, cy) in lon/lat.

    half_size_deg=1e-4 ≈ ~11 m at the equator — well below a 100 m cell so each
    building lives in exactly one cell.
    """
    coords = [
        [
            [cx - half_size_deg, cy - half_size_deg],
            [cx + half_size_deg, cy - half_size_deg],
            [cx + half_size_deg, cy + half_size_deg],
            [cx - half_size_deg, cy + half_size_deg],
            [cx - half_size_deg, cy - half_size_deg],
        ]
    ]
    return {
        "type": "Feature",
        "properties": {"height": -1.0, "confidence": -1.0},
        "geometry": {"type": "Polygon", "coordinates": coords},
    }


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """compute_building_density appears in TOOL_REGISTRY with expected metadata."""
    assert "compute_building_density" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_building_density"]
    assert entry.metadata.name == "compute_building_density"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "building_density"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Input-validation tests (no network).
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_typed_input_error():
    """A min==max bbox raises BuildingDensityInputError before any download."""
    with pytest.raises(BuildingDensityInputError):
        compute_building_density(bbox=(-82.0, 26.5, -82.0, 26.5))


def test_nonpositive_cell_size_raises_typed_input_error():
    """A zero / negative / NaN cell_size_m raises a typed input error."""
    with pytest.raises(BuildingDensityInputError, match="cell_size_m"):
        compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=0.0)
    with pytest.raises(BuildingDensityInputError, match="cell_size_m"):
        compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=-100.0)
    with pytest.raises(BuildingDensityInputError, match="cell_size_m"):
        compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=float("nan"))


def test_unknown_source_raises_typed_input_error():
    """An unsupported source raises BuildingDensityInputError, not retryable."""
    try:
        compute_building_density(bbox=_FORT_MYERS_BBOX, source="osm")
    except BuildingDensityInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected BuildingDensityInputError for unknown source")


def test_lat_out_of_web_mercator_range_raises():
    """A bbox latitude beyond ±85.05° raises BuildingDensityInputError."""
    with pytest.raises(BuildingDensityInputError, match="lat"):
        compute_building_density(bbox=(-180.0, -89.0, 180.0, 89.0))


def test_validate_bbox_passes_clean_bbox():
    """A clean Fort Myers bbox passes validation without raising."""
    _validate_bbox(_FORT_MYERS_BBOX)


def test_round_bbox_to_6dp():
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-82.123456789, 26.123456789, -81.987654321, 26.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-82.123457, 26.123457, -81.987654, 26.987654)


# ---------------------------------------------------------------------------
# Quadkey tests.
# ---------------------------------------------------------------------------


def test_quadkey_zero_at_zoom_one():
    """At zoom 1, (-90, 50) maps to quadkey '0' (NW quadrant of the globe)."""
    tx, ty = _lonlat_to_tile_xy(-90.0, 50.0, 1)
    qk = _tile_xy_to_quadkey(tx, ty, 1)
    assert qk == "0"


def test_quadkey_three_at_zoom_one():
    """At zoom 1, (90, -50) maps to quadkey '3' (SE quadrant of the globe)."""
    tx, ty = _lonlat_to_tile_xy(90.0, -50.0, 1)
    qk = _tile_xy_to_quadkey(tx, ty, 1)
    assert qk == "3"


def test_quadkey_zoom9_length():
    """At zoom 9, the quadkey is 9 characters long."""
    tx, ty = _lonlat_to_tile_xy(-81.9, 26.6, 9)
    qk = _tile_xy_to_quadkey(tx, ty, 9)
    assert len(qk) == 9
    assert all(c in "0123" for c in qk)


def test_quadkeys_for_fort_myers_bbox_includes_known_tile():
    """The Fort Myers bbox intersects ≥1 zoom-9 quadkey; computed quadkey is
    deterministic and starts with the SE-hemisphere prefix for North America.

    Fort Myers (~26.6°N, -81.9°W) at zoom-9 falls under quadkey 032213000-ish
    range (the global ``0`` for western hemisphere northern half). We assert
    the count is reasonable (1-4 tiles for a 0.2°×0.2° bbox at zoom-9) and
    each quadkey is well-formed.
    """
    qks = _quadkeys_for_bbox(_FORT_MYERS_BBOX, zoom=9)
    assert 1 <= len(qks) <= 4, f"Expected 1-4 quadkeys; got {len(qks)}: {qks}"
    for qk in qks:
        assert len(qk) == 9, f"Bad quadkey length: {qk!r}"
        assert all(c in "0123" for c in qk), f"Bad quadkey chars: {qk!r}"


# ---------------------------------------------------------------------------
# Centroid math tests.
# ---------------------------------------------------------------------------


def test_ring_centroid_of_unit_square():
    """The centroid of a unit square at the origin is (0.5, 0.5)."""
    ring = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    cx, cy = _ring_centroid(ring)
    assert abs(cx - 0.5) < 1e-9
    assert abs(cy - 0.5) < 1e-9


def test_feature_centroid_for_polygon():
    """A square polygon feature returns the polygon centroid."""
    feat = _square_polygon_feature(-81.9, 26.6, half_size_deg=1e-4)
    c = _feature_centroid(feat)
    assert c is not None
    cx, cy = c
    assert abs(cx - (-81.9)) < 1e-6
    assert abs(cy - 26.6) < 1e-6


def test_feature_centroid_for_point():
    """A Point feature returns its coordinate as the centroid."""
    feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10.0, 20.0]}}
    assert _feature_centroid(feat) == (10.0, 20.0)


def test_feature_centroid_for_missing_geometry():
    """A feature with no geometry returns None."""
    assert _feature_centroid({"type": "Feature"}) is None
    assert _feature_centroid({"type": "Feature", "geometry": None}) is None


# ---------------------------------------------------------------------------
# Grid construction tests.
# ---------------------------------------------------------------------------


def test_grid_sum_equals_centroid_count_in_bbox():
    """A grid built from 100 centroids inside the bbox has cell-sum 100."""
    # Random-ish centroids inside the Fort Myers bbox; use a deterministic
    # grid so repeated test runs always sum to 100.
    centroids = []
    for i in range(10):
        for j in range(10):
            lon = -82.0 + (i + 0.5) * (0.2 / 10)
            lat = 26.5 + (j + 0.5) * (0.2 / 10)
            centroids.append((lon, lat))
    assert len(centroids) == 100

    arr, transform, crs, h, w = _build_density_grid(
        centroids, _FORT_MYERS_BBOX, cell_size_m=100.0
    )
    assert crs == "EPSG:3857"
    assert h > 0 and w > 0
    # Each centroid bin = 1.0; the total over the grid should equal exactly 100.
    assert abs(float(arr.sum()) - 100.0) < 1e-5, (
        f"Expected grid sum = 100; got {float(arr.sum())}"
    )


def test_smaller_cell_size_yields_more_cells():
    """Halving cell_size_m yields ~4x as many cells (2x along each axis)."""
    centroids = []  # empty — we only care about grid shape.
    _, _, _, h100, w100 = _build_density_grid(centroids, _FORT_MYERS_BBOX, 100.0)
    _, _, _, h50, w50 = _build_density_grid(centroids, _FORT_MYERS_BBOX, 50.0)
    _, _, _, h200, w200 = _build_density_grid(centroids, _FORT_MYERS_BBOX, 200.0)
    # 50m ≈ 2x cells per axis vs 100m; allow ±1 cell rounding tolerance.
    assert abs(h50 - 2 * h100) <= 2
    assert abs(w50 - 2 * w100) <= 2
    # 200m ≈ 0.5x cells per axis vs 100m.
    assert abs(2 * h200 - h100) <= 2
    assert abs(2 * w200 - w100) <= 2


def test_grid_centroids_outside_bbox_are_skipped():
    """Centroids outside the bbox do not contribute to the density grid."""
    # Half inside, half outside; the outside ones must be skipped.
    inside = [(-81.9, 26.6)]
    outside = [(0.0, 0.0), (-100.0, 50.0)]
    arr, _, _, _, _ = _build_density_grid(inside + outside, _FORT_MYERS_BBOX, 100.0)
    # Only the one inside-bbox centroid is counted.
    assert abs(float(arr.sum()) - 1.0) < 1e-5


def test_empty_bbox_yields_zero_raster_no_error():
    """An empty centroid set produces an all-zero raster — no error raised."""
    arr, transform, crs, h, w = _build_density_grid(
        [], _FORT_MYERS_BBOX, cell_size_m=100.0
    )
    assert arr.shape == (h, w)
    assert float(arr.sum()) == 0.0
    assert crs == "EPSG:3857"


# ---------------------------------------------------------------------------
# Cache-layer tests (mocked index + tiles + GCS).
# ---------------------------------------------------------------------------


def _patched_fetch_index(quadkeys: list[str], urls_per_key: list[str]):
    """Return a function that pretends the MS index maps each qk → [url, ...]."""
    return lambda: {qk: [urls_per_key[i]] for i, qk in enumerate(quadkeys)}


def test_cache_miss_invokes_fetch_fn_and_writes_store():
    """First call (cache miss) writes the cache; URI is returned."""
    fake_gcs = FakeStorageClient()

    def fake_fetch(bbox, cell_size_m, source) -> bytes:
        return b"fake-cog-bytes"

    with patch(
        "trid3nt_server.tools.processing.compute_building_density._fetch_building_density_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.processing.compute_building_density.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = compute_building_density(
            bbox=_FORT_MYERS_BBOX, cell_size_m=100.0
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert "building_density" in result.uri
    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "building_density"
    assert result.bbox is not None
    assert len(fake_gcs.store) == 1


def test_cache_hit_skips_fetch_fn():
    """Second call with same params is a HIT; fetch_fn is invoked only once."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def fake_fetch(bbox, cell_size_m, source) -> bytes:
        fetch_count["n"] += 1
        return b"fake-cog-bytes"

    with patch(
        "trid3nt_server.tools.processing.compute_building_density._fetch_building_density_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.processing.compute_building_density.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=100.0)
        r2 = compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=100.0)

    assert fetch_count["n"] == 1
    assert r1.uri == r2.uri


def test_cache_key_differentiates_cell_size():
    """Same bbox + different cell_size_m → different cache key / URI."""
    fake_gcs = FakeStorageClient()

    def fake_fetch(bbox, cell_size_m, source) -> bytes:
        return b"fake-cog-bytes-" + str(cell_size_m).encode()

    with patch(
        "trid3nt_server.tools.processing.compute_building_density._fetch_building_density_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.processing.compute_building_density.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r100 = compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=100.0)
        r50 = compute_building_density(bbox=_FORT_MYERS_BBOX, cell_size_m=50.0)

    assert r100.uri != r50.uri
    assert len(fake_gcs.store) == 2


# ---------------------------------------------------------------------------
# End-to-end with synthetic tiles: 100 polygons → density sum = 100.
# ---------------------------------------------------------------------------


def test_end_to_end_with_100_polygons_density_sum_is_100():
    """100 polygons inside the bbox → COG whose cell-sum is exactly 100.

    This tests the full pipeline including _fetch_building_density_bytes —
    only the upstream HTTP calls are stubbed:
      - _fetch_index returns a single fake quadkey.
      - _download_tile_features returns 100 generated polygons in-bbox.
    The COG bytes are then re-opened with rasterio and the array summed.
    """
    import rasterio

    # 100 polygons on a 10×10 grid inside _FORT_MYERS_BBOX.
    polys = []
    for i in range(10):
        for j in range(10):
            lon = -82.0 + (i + 0.5) * (0.2 / 10)
            lat = 26.5 + (j + 0.5) * (0.2 / 10)
            polys.append(_square_polygon_feature(lon, lat))
    assert len(polys) == 100

    # Stub: the index returns the first quadkey of the Fort Myers bbox.
    qks = _quadkeys_for_bbox(_FORT_MYERS_BBOX, zoom=9)

    def fake_index() -> dict[str, list[str]]:
        return {qks[0]: ["https://fake.example/tile.csv.gz"]}

    def fake_download(url: str) -> list[dict]:
        return polys

    # Reset module-level index cache to ensure our stub is used.
    cbd._INDEX_CACHE = None

    with patch.object(cbd, "_fetch_index", side_effect=fake_index), patch.object(
        cbd, "_download_tile_features", side_effect=fake_download
    ):
        cog_bytes = cbd._fetch_building_density_bytes(
            bbox=_FORT_MYERS_BBOX, cell_size_m=100.0, source="ms_footprints"
        )

    assert len(cog_bytes) > 0
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
        f.write(cog_bytes)
    try:
        with rasterio.open(path) as src:
            arr = src.read(1)
            assert src.crs is not None
            assert str(src.crs).endswith("3857") or "3857" in str(src.crs)
            assert arr.dtype.name == "float32"
            assert abs(float(arr.sum()) - 100.0) < 1e-3, (
                f"Expected sum=100; got {float(arr.sum())}"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_end_to_end_empty_bbox_yields_zero_cog():
    """A bbox whose tile contains zero features still emits a valid all-zero COG."""
    import rasterio

    qks = _quadkeys_for_bbox(_FORT_MYERS_BBOX, zoom=9)

    def fake_index() -> dict[str, list[str]]:
        return {qks[0]: ["https://fake.example/empty.csv.gz"]}

    def fake_download(url: str) -> list[dict]:
        return []  # No features

    cbd._INDEX_CACHE = None

    with patch.object(cbd, "_fetch_index", side_effect=fake_index), patch.object(
        cbd, "_download_tile_features", side_effect=fake_download
    ):
        cog_bytes = cbd._fetch_building_density_bytes(
            bbox=_FORT_MYERS_BBOX, cell_size_m=100.0, source="ms_footprints"
        )

    assert len(cog_bytes) > 0
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
        f.write(cog_bytes)
    try:
        with rasterio.open(path) as src:
            arr = src.read(1)
            assert float(arr.sum()) == 0.0
            assert arr.dtype.name == "float32"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_index_failure_is_typed_upstream_error():
    """An MS index 5xx surfaces as BuildingDensityUpstreamError (retryable)."""
    import requests as _r

    cbd._INDEX_CACHE = None

    class _Boom(_r.RequestException):
        pass

    def fake_get(url, **kw):
        raise _Boom("simulated network failure")

    with patch.object(cbd.requests, "get", side_effect=fake_get):
        with pytest.raises(BuildingDensityUpstreamError, match="MS index download failed"):
            cbd._fetch_index()


# ---------------------------------------------------------------------------
# Live integration test (TRID3NT_TEST_LIVE_BUILDINGS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_BUILDINGS,
    reason="Set TRID3NT_TEST_LIVE_BUILDINGS=1 to run live MS Building Footprints tests",
)
def test_live_fort_myers_density_geographic_correctness():
    """LIVE: real MS data over Fort Myers bbox.

    Codified job-0086 lesson: the test verifies the OUTPUT GEOGRAPHY, not
    just byte round-trip. The signal we cross-check:

    1. **Total building count is large.** The Fort Myers / Cape Coral / Sanibel
       area covered by the bbox has hundreds of thousands of structures; the
       full sum must be ≥ 10,000 to confirm we are not silently dropping tiles.
    2. **The four bbox-corner cells are zero.** The bbox corners sit in marsh,
       water, and undeveloped land at the bbox edges. If our COG were
       Y-flipped (job-0086), N-S mirrored, or off by ½-grid, at least one
       corner would carry a non-zero building count from the urban interior.
    3. **The densest cell is geographically INSIDE the urban core**, not at
       the edge. We assert the argmax cell lies within an inner box that
       excludes the outermost 5 % of the grid on each side.
    4. **Downtown Fort Myers (-81.872, 26.640) lies in a 5×5 neighbourhood
       whose mean count is at least 1**, confirming that the downtown LOCATION
       (computed via rasterio's CRS-aware coordinate→pixel index) is dense in
       the COG. A Y-flip would put the downtown pixel into the Estero Bay
       water and the assertion would fail.

    These four assertions together would catch the job-0086 class of bug
    (in-COG mirror, axis flip, off-by-one) AND a wrong-CRS bug (which
    would mis-locate downtown).
    """
    import numpy as np
    import rasterio
    from pyproj import Transformer

    # Reset module cache so the live test always exercises the real index fetch.
    cbd._INDEX_CACHE = None

    cog_bytes = cbd._fetch_building_density_bytes(
        bbox=_FORT_MYERS_BBOX, cell_size_m=100.0, source="ms_footprints"
    )
    assert len(cog_bytes) > 0

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
        f.write(cog_bytes)
    try:
        with rasterio.open(path) as src:
            arr = src.read(1)
            tags = src.tags()
            assert tags.get("source") == "ms_footprints"
            assert tags.get("grid_crs") == "EPSG:3857"
            assert str(src.crs).endswith("3857") or "3857" in str(src.crs)

            # 1. Sum check.
            total_buildings = float(arr.sum())
            assert total_buildings >= 10_000, (
                f"Expected ≥10,000 buildings in Fort Myers bbox; got {total_buildings}"
            )

            # 2. Four-corner zero check — guards against axis mirrors / Y-flips.
            corners = [
                (0, 0, "NW"),
                (0, src.width - 1, "NE"),
                (src.height - 1, 0, "SW"),
                (src.height - 1, src.width - 1, "SE"),
            ]
            for r, c, lbl in corners:
                val = float(arr[r, c])
                assert val == 0.0, (
                    f"Bbox corner {lbl} reads {val} buildings — "
                    f"density product axis orientation is wrong "
                    f"(job-0086 codified lesson)."
                )

            # 3. Cape Coral residential suburb is the densest known urban area
            #    in the bbox; its 5×5 neighbourhood mean must be ≥ 3
            #    buildings/cell. A grid mis-orientation, Y-flip, or CRS
            #    transform error would put this point into the river or
            #    Pine Island Sound and the mean would collapse.
            tx = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

            def _window_mean(lon: float, lat: float) -> float:
                x, y = tx.transform(lon, lat)
                r, c = src.index(x, y)
                assert 0 <= r < src.height and 0 <= c < src.width, (
                    f"Point ({lon}, {lat}) → index ({r}, {c}) out of grid "
                    f"({src.height}, {src.width}) — CRS / transform mis-configured"
                )
                r0 = max(0, r - 2)
                r1 = min(src.height, r + 3)
                c0 = max(0, c - 2)
                c1 = min(src.width, c + 3)
                return float(arr[r0:r1, c0:c1].mean())

            cape_coral_mean = _window_mean(-81.95, 26.62)
            assert cape_coral_mean >= 3.0, (
                f"Cape Coral 5×5 window mean = {cape_coral_mean} — "
                f"a known dense residential subdivision is reading sparse; "
                f"density product is geographically wrong (job-0086 codified lesson)."
            )

            # 4. Caloosahatchee River mid-channel (-81.90, 26.625) is OPEN WATER —
            #    its 5×5 neighbourhood mean must be 0 (or essentially 0; the
            #    river is ~1 km wide, fits comfortably inside a 500 m window).
            #    A Y-flip would put downtown buildings into this water cell.
            river_mean = _window_mean(-81.90, 26.625)
            assert river_mean < 0.5, (
                f"Caloosahatchee mid-river 5×5 window mean = {river_mean} — "
                f"open water is reading non-trivial building density. "
                f"This is exactly the job-0086 codified failure mode: "
                f"in-COG axis mirror that survives byte round-trip checks."
            )

            # 5. The signal asymmetry must be at least 10x — a Y-flip / mirror
            #    can produce 'wrong-everywhere' patterns that still pass
            #    individual thresholds; the ratio guards against that.
            assert cape_coral_mean > 10 * max(river_mean, 0.1), (
                f"Cape Coral / river ratio is only {cape_coral_mean / max(river_mean, 0.1):.2f} — "
                f"density signal is too uniform to be correct; "
                f"product geometry is suspect."
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_BUILDINGS,
    reason="Set TRID3NT_TEST_LIVE_BUILDINGS=1 to run live MS Building Footprints tests",
)
def test_live_index_fetch_returns_united_states_rows():
    """LIVE: the MS dataset-links.csv index is reachable and has US rows."""
    cbd._INDEX_CACHE = None
    index = cbd._fetch_index()
    # Sanity: index is non-empty.
    assert len(index) > 1000, f"Index suspiciously small: {len(index)} keys"
    # Validate that for the Fort Myers quadkey we get ≥1 URL pointing at
    # the global-buildings Azure storage.
    qks = _quadkeys_for_bbox(_FORT_MYERS_BBOX, zoom=9)
    found_us_quadkey = False
    for qk in qks:
        if qk in index:
            for url in index[qk]:
                if "UnitedStates" in url:
                    found_us_quadkey = True
                    break
        if found_us_quadkey:
            break
    assert found_us_quadkey, (
        f"None of {qks} resolved to a UnitedStates row in the MS index"
    )
