"""Unit + live tests for the ``fetch_ghsl_population`` atomic tool.

Coverage:

Unit (no network):
- Tool is registered in TOOL_REGISTRY with expected metadata.
- ``bbox=None`` raises ``GHSLPopBboxRequiredError`` (BBOX_REQUIRED, not-retryable).
- Malformed / degenerate / out-of-range / non-finite bbox raises
  ``GHSLPopInputError``.
- Unsupported epoch raises ``GHSLPopInputError``.
- ``_tiles_for_bbox`` maps known non-US cities to the correct GHSL R/C tiles
  (synthetic correctness against tile-grid bounds measured live).
- ``_bbox_intersects_coverage`` reports a city in / open-ocean-south out.
- ``_round_bbox`` quantizes to 6 dp.
- ``estimate_payload_mb`` scales with area and floors.
- Synthetic COG round-trip: a fake single-tile fetcher feeding ``read_through``
  yields a persons/cell COG; the LayerURI shape carries the bbox + preset.
- Cache miss invokes the inner fetcher once; cache hit skips it.
- Honest-empty: an inner fetcher that raises ``GHSLPopEmptyError`` propagates.

Live (env-guarded by TRID3NT_TEST_LIVE_GHSL=1):
- Lagos (Nigeria) bbox returns a CRS-tagged COG with population sum in a
  plausible range (~10^7 persons; matches the dev probe of ~12.5M);
  output bounds fall inside the requested bbox; CRS is EPSG:4326.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_ghsl_population import (
    GHSLPopBboxRequiredError,
    GHSLPopEmptyError,
    GHSLPopInputError,
    _bbox_intersects_coverage,
    _GHSL_COVERAGE_BBOX,
    _round_bbox,
    _tile_bounds,
    _tiles_for_bbox,
    estimate_payload_mb,
    fetch_ghsl_population,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)

# Lagos, Nigeria — small, dense, well inside coverage; single GHSL tile R9_C19.
_LAGOS_BBOX = (3.10, 6.35, 3.70, 6.75)
# A bbox far south over open ocean (south of coverage) for the empty path.
_DEEP_SOUTH_OCEAN_BBOX = (0.0, -80.0, 5.0, -75.0)

_LIVE_GHSL = os.environ.get("TRID3NT_TEST_LIVE_GHSL") == "1"


# ---------------------------------------------------------------------------
# In-memory read-through injector (S3-only; mirrors sibling fetcher tests).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _fake_cog_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_GHSL_COG_" + tag.encode() + b"\x00" * 16


def _make_read_through_injector(fake: _FakeStore):
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
# Registration
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    assert "fetch_ghsl_population" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_ghsl_population"]
    assert entry.metadata.name == "fetch_ghsl_population"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "ghsl_population"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Typed-error / input-validation (no network)
# ---------------------------------------------------------------------------


def test_bbox_none_raises_bbox_required():
    with pytest.raises(GHSLPopBboxRequiredError) as exc:
        fetch_ghsl_population(bbox=None)  # type: ignore[arg-type]
    assert exc.value.error_code == "BBOX_REQUIRED"
    assert exc.value.retryable is False


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(GHSLPopInputError):
        fetch_ghsl_population(bbox=(3.1, 6.35, 3.1, 6.35))


def test_out_of_range_lon_raises_input_error():
    with pytest.raises(GHSLPopInputError):
        fetch_ghsl_population(bbox=(-200.0, 6.35, 3.7, 6.75))


def test_non_finite_bbox_raises_input_error():
    with pytest.raises(GHSLPopInputError):
        fetch_ghsl_population(bbox=(float("nan"), 6.35, 3.7, 6.75))


def test_wrong_length_bbox_raises_input_error():
    with pytest.raises(GHSLPopInputError):
        fetch_ghsl_population(bbox=(3.1, 6.35, 3.7))  # type: ignore[arg-type]


def test_unsupported_epoch_raises_input_error():
    with pytest.raises(GHSLPopInputError, match="epoch"):
        fetch_ghsl_population(bbox=_LAGOS_BBOX, epoch=1975)


def test_deep_south_ocean_raises_empty_via_fetch():
    """A bbox south of coverage raises GHSLPopEmptyError from the inner fetcher.

    Calls ``_fetch_ghsl_pop_bytes`` directly to bypass the cache short-circuit.
    """
    from trid3nt_server.tools.fetch_ghsl_population import _fetch_ghsl_pop_bytes

    with pytest.raises(GHSLPopEmptyError):
        _fetch_ghsl_pop_bytes(bbox=_DEEP_SOUTH_OCEAN_BBOX)


# ---------------------------------------------------------------------------
# Tile-grid math (synthetic correctness vs live-measured tile bounds)
# ---------------------------------------------------------------------------


def test_tiles_for_bbox_lagos_single_tile():
    """Lagos maps to exactly one tile R9_C19 (measured live during dev)."""
    assert _tiles_for_bbox(_LAGOS_BBOX) == [(9, 19)]


def test_tiles_for_bbox_known_cities():
    """Mexico City -> R7_C9; a London-ish bbox -> R5_C19 (live-measured grid)."""
    assert _tiles_for_bbox((-99.3, 19.2, -98.9, 19.6)) == [(7, 9)]
    assert _tiles_for_bbox((-0.2, 51.3, 0.1, 51.6)) == [(4, 18), (4, 19)]


def test_tiles_for_bbox_cross_tile_boundary():
    """A bbox straddling the lon=10 column boundary spans two columns."""
    tiles = _tiles_for_bbox((9.8, 5.0, 10.2, 5.5))
    assert set(tiles) == {(9, 19), (9, 20)}


def test_tile_bounds_match_measured():
    """Tile-bounds formula reproduces five live-measured tiles to < 0.001 deg."""
    measured = {
        (7, 9): (-100.0079, 19.0996, -90.0079, 29.0996),
        (5, 19): (-0.0079, 39.0996, 9.9921, 49.0996),
        (8, 26): (69.9921, 9.0996, 79.9921, 19.0996),
        (4, 12): (-70.0079, 49.0996, -60.0079, 59.0996),
        (6, 19): (-0.0079, 29.0996, 9.9921, 39.0996),
    }
    for (r, c), exp in measured.items():
        got = _tile_bounds(r, c)
        assert all(abs(a - b) < 0.001 for a, b in zip(got, exp)), (r, c, got, exp)


def test_tile_bounds_contains_its_bbox_for_lagos():
    """The Lagos tile actually contains the Lagos bbox."""
    (r, c) = _tiles_for_bbox(_LAGOS_BBOX)[0]
    w, s, e, n = _tile_bounds(r, c)
    assert w <= _LAGOS_BBOX[0] and e >= _LAGOS_BBOX[2]
    assert s <= _LAGOS_BBOX[1] and n >= _LAGOS_BBOX[3]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_bbox_intersects_coverage_includes_lagos():
    assert _bbox_intersects_coverage(_LAGOS_BBOX) is True


def test_bbox_intersects_coverage_excludes_deep_south():
    assert _bbox_intersects_coverage(_DEEP_SOUTH_OCEAN_BBOX) is False


def test_round_bbox_quantizes_to_6dp():
    raw = (3.123456789, 6.123456789, 3.987654321, 6.987654321)
    assert _round_bbox(raw) == (3.123457, 6.123457, 3.987654, 6.987654)


def test_coverage_bbox_constants_are_sane():
    cmnlon, cmnlat, cmxlon, cmxlat = _GHSL_COVERAGE_BBOX
    assert cmnlon == -180.0 and cmxlon == 180.0
    assert cmnlat < -50.0 and cmxlat > 80.0


def test_estimate_payload_mb_scales_with_area_and_floors():
    big = estimate_payload_mb((0.0, 0.0, 5.0, 5.0))
    small = estimate_payload_mb((0.0, 0.0, 0.1, 0.1))
    assert big > small
    assert estimate_payload_mb(None) > 0.0
    # floor
    assert estimate_payload_mb((0.0, 0.0, 0.0001, 0.0001)) >= 0.2


# ---------------------------------------------------------------------------
# Cache-layer tests (patched fetch + in-memory store)
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_and_writes_store():
    fake = _FakeStore()
    fetch_count = {"n": 0}

    def fake_inner(bbox, epoch=2020):
        fetch_count["n"] += 1
        return _fake_cog_bytes("MISS")

    with patch(
        "trid3nt_server.tools.fetch_ghsl_population._fetch_ghsl_pop_bytes",
        side_effect=fake_inner,
    ), patch(
        "trid3nt_server.tools.fetch_ghsl_population.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        result = fetch_ghsl_population(bbox=_LAGOS_BBOX)

    assert fetch_count["n"] == 1
    assert result.uri is not None and result.uri.startswith("s3://")
    assert "ghsl_population" in result.uri
    assert len(fake.store) == 1


def test_cache_hit_skips_fetch_fn():
    fake = _FakeStore()
    fetch_count = {"n": 0}

    def fake_inner(bbox, epoch=2020):
        fetch_count["n"] += 1
        return _fake_cog_bytes("HIT")

    with patch(
        "trid3nt_server.tools.fetch_ghsl_population._fetch_ghsl_pop_bytes",
        side_effect=fake_inner,
    ), patch(
        "trid3nt_server.tools.fetch_ghsl_population.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        r1 = fetch_ghsl_population(bbox=_LAGOS_BBOX)
        r2 = fetch_ghsl_population(bbox=_LAGOS_BBOX)

    assert fetch_count["n"] == 1
    assert r1.uri == r2.uri


def test_layer_uri_shape_has_persons_units_and_raster_role():
    fake = _FakeStore()
    with patch(
        "trid3nt_server.tools.fetch_ghsl_population._fetch_ghsl_pop_bytes",
        return_value=_fake_cog_bytes("SHAPE"),
    ), patch(
        "trid3nt_server.tools.fetch_ghsl_population.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        result = fetch_ghsl_population(bbox=_LAGOS_BBOX)

    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "persons_per_cell"
    assert result.style_preset == "population_density"
    assert result.bbox == _round_bbox(_LAGOS_BBOX)
    assert "ghsl-pop" in result.layer_id


def test_empty_fetch_propagates():
    """An inner GHSLPopEmptyError propagates (no silent success)."""
    fake = _FakeStore()

    def boom(bbox, epoch=2020):
        raise GHSLPopEmptyError("synthetic empty")

    with patch(
        "trid3nt_server.tools.fetch_ghsl_population._fetch_ghsl_pop_bytes",
        side_effect=boom,
    ), patch(
        "trid3nt_server.tools.fetch_ghsl_population.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        with pytest.raises(GHSLPopEmptyError):
            fetch_ghsl_population(bbox=_LAGOS_BBOX)


# ---------------------------------------------------------------------------
# Synthetic COG round-trip (real rasterio, no network): proves the COG-write
# path + a real single-tile window read produce a valid persons/cell COG.
# ---------------------------------------------------------------------------


def test_synthetic_single_tile_cog_roundtrip(tmp_path):
    """Build a fake GHSL tile, patch the URL template to it, and assert the
    inner fetcher windows + writes a valid persons/cell COG.
    """
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_bounds as transform_from_bounds

    import trid3nt_server.tools.fetch_ghsl_population as mod

    # Synthetic 10-deg tile matching the R9_C19 footprint (Lagos). Use enough
    # pixels that the small Lagos window (0.6x0.4 deg) covers multiple cells.
    w, s, e, n = mod._tile_bounds(9, 19)
    npx = 1000
    # Fill the WHOLE tile with a constant 123 persons/cell so any non-empty
    # window read yields a positive, finite sum regardless of where the bbox
    # lands inside the tile.
    arr = np.full((npx, npx), 123.0, dtype="float64")
    tile_path = tmp_path / "fake_tile_R9_C19.tif"
    with rasterio.open(
        tile_path,
        "w",
        driver="GTiff",
        height=npx,
        width=npx,
        count=1,
        dtype="float64",
        crs="EPSG:4326",
        transform=transform_from_bounds(w, s, e, n, npx, npx),
        nodata=-200.0,
    ) as dst:
        dst.write(arr, 1)

    # Patch the URL template so the single covering tile resolves to our file.
    with patch.object(
        mod, "_TILE_URL_TEMPLATE", str(tile_path).replace("{r}", "{r}").replace("{c}", "{c}")
    ):
        # Our template has no {r}/{c} now (literal path); _tiles_for_bbox still
        # returns one tile, and .format() on a no-placeholder string is a no-op.
        cog = mod._fetch_ghsl_pop_bytes(bbox=_LAGOS_BBOX)

    out = tmp_path / "out.tif"
    out.write_bytes(cog)
    with rasterio.open(out) as src:
        assert src.crs.to_epsg() == 4326
        assert src.dtypes[0] == "float32"
        data = src.read(1)
        # The tile is a constant 123 persons/cell; every windowed cell carries
        # that value, so the sum is strictly positive, finite, and all cells
        # equal 123 (no nodata sentinel leaked through).
        assert np.nansum(data) > 0.0
        assert np.allclose(data[np.isfinite(data)], 123.0)
        # bounds inside requested bbox
        b = src.bounds
        assert b.left >= _LAGOS_BBOX[0] - 0.01 and b.right <= _LAGOS_BBOX[2] + 0.01


# ---------------------------------------------------------------------------
# Live test (env-guarded)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_GHSL, reason="set TRID3NT_TEST_LIVE_GHSL=1 to run")
def test_live_lagos_population_sum_plausible():
    import numpy as np
    import rasterio

    from trid3nt_server.tools.fetch_ghsl_population import _fetch_ghsl_pop_bytes

    cog = _fetch_ghsl_pop_bytes(bbox=_round_bbox(_LAGOS_BBOX))
    import io

    with rasterio.open(io.BytesIO(cog)) as src:
        assert src.crs.to_epsg() == 4326
        arr = src.read(1)
        total = float(np.nansum(arr))
        # Lagos dense core in this 0.6x0.4 deg window ~ 1e7 persons.
        assert 1_000_000 < total < 30_000_000, total
        b = src.bounds
        assert b.left >= _LAGOS_BBOX[0] - 0.01 and b.right <= _LAGOS_BBOX[2] + 0.01
