"""Unit tests for the ``fetch_field_boundaries`` atomic tool (NATE 2026-06-17).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata + category.
- Bad bbox shape / out-of-range / degenerate → ``FieldsInputError``.
- bbox WITH coverage (mocked source) → vector FlatGeobuf with the expected
  field polygons, returned as a vector ``LayerURI``.
- bbox with NO coverage → ``FieldsNoCoverageError`` (error_code
  FIELDS_NO_COVERAGE, retryable=False) — the honest typed error.
- Explicit unknown ``dataset`` → ``FieldsInputError``.
- Explicit ``dataset`` whose coverage misses the bbox → FieldsNoCoverageError.
- Dataset auto-selection picks the covering region; CONUS bbox → US-USDA.
- Cache: first call (MISS) invokes the source read; identical second call
  (HIT) reuses the cache and does NOT re-read the source.
- 0-feature AOI (coverage but no fields) returns a valid empty layer, no error.

The source GeoParquet read (``_read_fields_gdf``) is mocked in every test so
the suite never touches the network. A live end-to-end probe against the real
Source Cooperative endpoints is gated by ``TRID3NT_TEST_LIVE_FIELDS=1``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_field_boundaries import (
    FTW_DATASETS,
    FieldsInputError,
    FieldsNoCoverageError,
    _bbox_intersects,
    _round_bbox_to_6dp,
    _select_dataset,
    _validate_bbox,
    fetch_field_boundaries,
)

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)

# Ames, Iowa — inside the US-USDA CONUS coverage (live-probe target bbox).
_IOWA_BBOX = (-93.70, 42.00, -93.60, 42.08)

# Mid-South-Atlantic open ocean — outside every registered region.
_OCEAN_BBOX = (-40.0, 0.0, -39.0, 1.0)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_FIELDS") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_wdpa_protected_areas).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time = None
        self.cache_control = None

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
# Fixture data — a small in-memory GeoDataFrame of field polygons.
# ---------------------------------------------------------------------------


def _fields_gdf(n: int = 3):
    """Build a WGS84 GeoDataFrame of ``n`` square field polygons in the Iowa AOI."""
    import geopandas as gpd
    from shapely.geometry import box

    polys = []
    crops = []
    base_lon, base_lat = -93.69, 42.01
    for i in range(n):
        lon = base_lon + i * 0.01
        polys.append(box(lon, base_lat, lon + 0.005, base_lat + 0.005))
        crops.append(f"Corn-{i}")
    return gpd.GeoDataFrame({"crop_name": crops}, geometry=polys, crs="EPSG:4326")


def _empty_gdf():
    import geopandas as gpd
    import pandas as pd

    return gpd.GeoDataFrame(
        pd.DataFrame({"crop_name": pd.Series(dtype="object")}),
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
        crs="EPSG:4326",
    )


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_registered_with_expected_metadata():
    entry = TOOL_REGISTRY.get("fetch_field_boundaries")
    assert entry is not None, "fetch_field_boundaries must be registered"
    md = entry.metadata
    assert md.ttl_class == "static-30d"
    assert md.source_class == "ftw_field_boundaries"
    assert md.cacheable is True
    assert md.open_world_hint is True


def test_in_land_cover_development_category():
    from trid3nt_server.categories import tools_for_category

    assert "fetch_field_boundaries" in tools_for_category("land_cover_development")


def test_registry_entries_have_wgs84_coverage_and_keys():
    keys = {d.key for d in FTW_DATASETS}
    assert "us_usda_cropland" in keys
    for d in FTW_DATASETS:
        lo_lon, lo_lat, hi_lon, hi_lat = d.coverage
        assert -180 <= lo_lon < hi_lon <= 180
        assert -90 <= lo_lat < hi_lat <= 90
        assert d.url.startswith("https://data.source.coop/")


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        (1.0, 2.0, 3.0),  # wrong length
        (-200.0, 0.0, -190.0, 1.0),  # lon out of range
        (0.0, -100.0, 1.0, -91.0),  # lat out of range
        (10.0, 5.0, 5.0, 10.0),  # min_lon >= max_lon
        (5.0, 10.0, 10.0, 5.0),  # min_lat >= max_lat
        (float("nan"), 0.0, 1.0, 1.0),  # non-finite
    ],
)
def test_bad_bbox_raises_input_error(bad):
    with pytest.raises(FieldsInputError):
        _validate_bbox(bad)


def test_round_bbox_6dp():
    assert _round_bbox_to_6dp((-93.1234567, 42.7654321, -92.0, 43.0)) == (
        -93.123457,
        42.765432,
        -92.0,
        43.0,
    )


# ---------------------------------------------------------------------------
# Coverage / dataset selection.
# ---------------------------------------------------------------------------


def test_no_coverage_raises_typed_error():
    with pytest.raises(FieldsNoCoverageError) as ei:
        _select_dataset(_OCEAN_BBOX, None)
    exc = ei.value
    assert exc.error_code == "FIELDS_NO_COVERAGE"
    assert exc.retryable is False


def test_conus_bbox_selects_us_usda():
    ds = _select_dataset(_IOWA_BBOX, None)
    assert ds.key == "us_usda_cropland"


def test_unknown_dataset_raises_input_error():
    with pytest.raises(FieldsInputError):
        _select_dataset(_IOWA_BBOX, "atlantis")


def test_explicit_dataset_outside_bbox_raises_no_coverage():
    # Force the Japan dataset but query the Iowa bbox — no intersection.
    with pytest.raises(FieldsNoCoverageError):
        _select_dataset(_IOWA_BBOX, "japan")


def test_bbox_intersects_helper():
    assert _bbox_intersects((0, 0, 2, 2), (1, 1, 3, 3)) is True
    assert _bbox_intersects((0, 0, 1, 1), (2, 2, 3, 3)) is False


# ---------------------------------------------------------------------------
# End-to-end (mocked source read): coverage → polygons inline.
# ---------------------------------------------------------------------------


def test_coverage_returns_vector_layer_with_polygons():
    fake = FakeStorageClient()
    gdf = _fields_gdf(3)
    with patch(
        "trid3nt_server.tools.fetch_field_boundaries.read_through",
        _make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetch_field_boundaries._read_fields_gdf",
        return_value=gdf,
    ) as mock_read:
        layer = fetch_field_boundaries(_IOWA_BBOX)

    assert layer.layer_type == "vector"
    assert layer.role == "context"
    assert layer.units is None
    assert layer.style_preset == "field_boundaries"
    assert "US Cropland" in layer.name
    assert layer.uri is not None and layer.uri.endswith(".fgb")
    assert mock_read.call_count == 1

    # The cached FlatGeobuf must decode back to 3 polygons in the AOI.
    import io
    import geopandas as gpd

    stored = list(fake.store.values())[0]
    rt = gpd.read_file(io.BytesIO(stored))
    assert len(rt) == 3
    assert set(rt.geometry.geom_type) <= {"Polygon", "MultiPolygon"}
    # Geometry falls inside the requested bbox.
    tb = rt.total_bounds
    assert tb[0] >= _IOWA_BBOX[0] - 1e-6 and tb[2] <= _IOWA_BBOX[2] + 1e-6


def test_empty_aoi_returns_valid_zero_feature_layer():
    fake = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_field_boundaries.read_through",
        _make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetch_field_boundaries._read_fields_gdf",
        return_value=_empty_gdf(),
    ):
        layer = fetch_field_boundaries(_IOWA_BBOX)

    assert layer.layer_type == "vector"
    import io
    import geopandas as gpd

    stored = list(fake.store.values())[0]
    rt = gpd.read_file(io.BytesIO(stored))
    assert len(rt) == 0


def test_no_coverage_bbox_raises_before_any_read():
    # An ocean bbox must raise FieldsNoCoverageError and never invoke the source.
    with patch(
        "trid3nt_server.tools.fetch_field_boundaries._read_fields_gdf"
    ) as mock_read:
        with pytest.raises(FieldsNoCoverageError):
            fetch_field_boundaries(_OCEAN_BBOX)
    mock_read.assert_not_called()


def test_cache_hit_skips_second_source_read():
    fake = FakeStorageClient()
    gdf = _fields_gdf(2)
    with patch(
        "trid3nt_server.tools.fetch_field_boundaries.read_through",
        _make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetch_field_boundaries._read_fields_gdf",
        return_value=gdf,
    ) as mock_read:
        layer1 = fetch_field_boundaries(_IOWA_BBOX)
        layer2 = fetch_field_boundaries(_IOWA_BBOX)

    # Identical (bbox, dataset) → same cache key → one source read, same URI.
    assert mock_read.call_count == 1
    assert layer1.uri == layer2.uri


def test_cache_key_differs_by_dataset_and_bbox():
    # Different bbox in CONUS → different cache key (different stored object).
    fake = FakeStorageClient()
    gdf = _fields_gdf(1)
    other_bbox = (-90.0, 41.0, -89.9, 41.1)
    with patch(
        "trid3nt_server.tools.fetch_field_boundaries.read_through",
        _make_read_through_injector(fake),
    ), patch(
        "trid3nt_server.tools.fetch_field_boundaries._read_fields_gdf",
        return_value=gdf,
    ):
        l1 = fetch_field_boundaries(_IOWA_BBOX)
        l2 = fetch_field_boundaries(other_bbox)
    assert l1.uri != l2.uri
    assert len(fake.store) == 2


# ---------------------------------------------------------------------------
# Live end-to-end (opt-in). Verifies the real Source Cooperative pushdown path:
# a small Iowa bbox returns >0 USDA field polygons that fall inside the AOI.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_FIELDS=1 to run")
def test_live_us_usda_iowa_returns_fields():
    from trid3nt_server.tools.fetch_field_boundaries import _read_fields_gdf, _select_dataset

    ds = _select_dataset(_IOWA_BBOX, None)
    gdf = _read_fields_gdf(ds, _IOWA_BBOX)
    assert len(gdf) > 0, "expected USDA field polygons in the Ames, Iowa AOI"
    tb = gdf.total_bounds
    assert tb[0] >= _IOWA_BBOX[0] - 1e-3 and tb[2] <= _IOWA_BBOX[2] + 1e-3
    assert tb[1] >= _IOWA_BBOX[1] - 1e-3 and tb[3] <= _IOWA_BBOX[3] + 1e-3
