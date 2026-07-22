"""Unit tests for the ``fetch_wdpa_protected_areas`` atomic tool (job-0089).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Unknown bbox shape raises ``WDPABboxError``.
- Mocked 100-feature response → FlatGeobuf with 100 polygons.
- designation_filter='National Park' returns subset.
- Empty bbox over open water → 0 features without raising.
- Pagination across 4000 features (two pages).
- Cache miss invokes fetch_fn; second call (HIT) skips fetch_fn.
- Returns ``LayerURI`` with expected shape.

Tests that hit the real WDPA ArcGIS endpoint are gated by
``TRID3NT_TEST_LIVE_WDPA=1``. They verify the Everglades bbox returns the
Everglades National Park polygon — the FR-0086 codified geography-not-just-
bytes lesson applies here (we check the polygon NAME, not just byte count).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch
from typing import Any

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas import (
    WDPABboxError,
    WDPADesignationError,
    WDPAError,
    WDPAUpstreamError,
    _bbox_to_envelope,
    _normalize_designation_filter,
    _normalize_one_designation,
    _round_bbox_to_6dp,
    _validate_bbox,
    fetch_wdpa_protected_areas,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Everglades, FL bbox (kickoff live-test target).
_EVERGLADES_BBOX = (-81.5, 25.0, -80.5, 26.5)

# Open-water bbox far offshore (no WDPA polygons).
_OCEAN_BBOX = (-30.0, 10.0, -25.0, 15.0)

_LIVE_WDPA = os.environ.get("TRID3NT_TEST_LIVE_WDPA") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors existing test pattern).
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


def _polygon_feature(
    name: str,
    designation: str = "National Park",
    iucn: str = "II",
    wdpaid: int = 1,
    *,
    coords: list[list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Build a GeoJSON polygon feature with WDPA-shaped properties.

    Property keys match the live WDPA FeatureServer schema (lowercase).
    """
    if coords is None:
        # Tiny square inside the Everglades bbox.
        coords = [[[-81.4, 25.5], [-81.3, 25.5], [-81.3, 25.6], [-81.4, 25.6], [-81.4, 25.5]]]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {
            "name_eng": name,
            "desig_eng": designation,
            "iucn_cat": iucn,
            "status": "Designated",
            "status_yr": 1947,
            "site_id": wdpaid,
        },
    }


def _wdpa_response(features: list[dict[str, Any]], exceeded: bool = False) -> dict[str, Any]:
    """Build a fake WDPA FeatureServer GeoJSON response."""
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": features,
    }
    if exceeded:
        payload["exceededTransferLimit"] = True
    return payload


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_wdpa_protected_areas appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_wdpa_protected_areas" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_wdpa_protected_areas"]
    assert entry.metadata.name == "fetch_wdpa_protected_areas"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "wdpa"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_rejects_degenerate():
    """A bbox where min == max raises WDPABboxError."""
    with pytest.raises(WDPABboxError):
        _validate_bbox((-81.0, 25.0, -81.0, 25.0))


def test_validate_bbox_rejects_out_of_range():
    """Out-of-CRS-range longitudes raise WDPABboxError."""
    with pytest.raises(WDPABboxError):
        _validate_bbox((-200.0, 25.0, -180.0, 26.0))


def test_validate_bbox_rejects_non_finite():
    """Non-finite values raise WDPABboxError."""
    with pytest.raises(WDPABboxError):
        _validate_bbox((float("nan"), 25.0, -80.0, 26.0))


def test_round_bbox_to_6dp():
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-81.123456789, 25.123456789, -80.987654321, 26.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-81.123457, 25.123457, -80.987654, 26.987654)


def test_bbox_to_envelope_format():
    """_bbox_to_envelope formats as xmin,ymin,xmax,ymax."""
    env = _bbox_to_envelope((-81.5, 25.0, -80.5, 26.5))
    assert env == "-81.5,25.0,-80.5,26.5"


def test_degenerate_bbox_raises_through_public_tool():
    """The public tool surface validates the bbox before any network call."""
    with pytest.raises(WDPABboxError):
        fetch_wdpa_protected_areas(bbox=(-81.0, 25.0, -81.0, 25.0))


# ---------------------------------------------------------------------------
# designation_filter normalization tests (OQ-0089-DESIGNATION-FILTER-SEMANTICS;
# the "goes18 vs goes-18" silent-identifier-mismatch guard).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("National Park", "National Park"),  # exact canonical
        ("national park", "National Park"),  # lowercase
        ("NATIONAL PARK", "National Park"),  # uppercase
        ("  National   Park  ", "National Park"),  # extra whitespace
        ("National Parks", "National Park"),  # plural in alias table
        ("national parks", "National Park"),  # lowercase plural
        ("NP", "National Park"),  # abbreviation
        ("np", "National Park"),  # lowercase abbreviation
        ("N.P.", "National Park"),  # abbreviation with dots
        ("NWR", "National Wildlife Refuge"),  # abbreviation
        ("national wildlife refuges", "National Wildlife Refuge"),  # plural
        ("WMA", "Wildlife Management Area"),  # abbreviation
        ("biosphere reserve", "UNESCO-MAB Biosphere Reserve"),  # phrasing
        ("ramsar", "Ramsar Site, Wetland of International Importance"),
        ("world heritage", "World Heritage Site (natural or mixed)"),
        ("State Forests", "State Forest"),  # generic trailing-s singularize
    ],
)
def test_normalize_one_designation_maps_to_canonical(spelling, expected):
    """Every accepted human/LLM spelling maps to the EXACT live desig_eng token."""
    assert _normalize_one_designation(spelling) == expected


def test_normalize_one_designation_unknown_raises_with_help():
    """An unknown designation raises WDPADesignationError listing accepted forms."""
    with pytest.raises(WDPADesignationError) as exc:
        _normalize_one_designation("Marsupial Sanctuary")
    msg = str(exc.value)
    # The error must be HELPFUL: name the bad token + list accepted designations
    # and aliases so a typo fails LOUD, not into a silent empty layer.
    assert "Marsupial Sanctuary" in msg
    assert "National Park" in msg
    assert "alias" in msg.lower()
    assert exc.value.error_code == "WDPA_DESIGNATION_INVALID"
    assert exc.value.retryable is False


def test_normalize_one_designation_empty_string_raises():
    """An empty / whitespace-only entry raises WDPADesignationError (fail loud)."""
    with pytest.raises(WDPADesignationError):
        _normalize_one_designation("   ")


def test_normalize_one_designation_non_str_raises():
    """A non-str entry raises WDPADesignationError."""
    with pytest.raises(WDPADesignationError):
        _normalize_one_designation(42)  # type: ignore[arg-type]


def test_normalize_designation_filter_dedupes_and_sorts():
    """Mixed spellings of the same designation collapse to one canonical token."""
    out = _normalize_designation_filter(["NP", "national park", "National Parks"])
    assert out == ["National Park"]


def test_normalize_designation_filter_none_and_empty_are_no_filter():
    """None and empty list both mean 'no filter' (return None)."""
    assert _normalize_designation_filter(None) is None
    assert _normalize_designation_filter([]) is None


def test_normalize_designation_filter_multiple_canonical_sorted():
    """Multiple distinct designations normalize, dedupe, and sort."""
    out = _normalize_designation_filter(["NWR", "np"])
    assert out == ["National Park", "National Wildlife Refuge"]


def test_normalize_designation_filter_non_list_raises():
    """A non-list designation_filter raises WDPADesignationError."""
    with pytest.raises(WDPADesignationError):
        _normalize_designation_filter("National Park")  # type: ignore[arg-type]


def test_lowercase_designation_filter_matches_through_public_tool():
    """A lowercase 'national park' filter now matches (was a silent 0-feature dead end).

    Reproduces the confirmed hazard: before the fix, designation_filter=
    ['national park'] returned 0 features even though Everglades NP is present.
    After normalization both sides casefold, so the National Park features
    survive the filter.
    """
    import geopandas as gpd

    features = [
        _polygon_feature("Everglades NP", designation="National Park", wdpaid=1),
        _polygon_feature(
            "Big Cypress National Preserve",
            designation="National Preserve",
            wdpaid=2,
        ),
        _polygon_feature("Biscayne NP", designation="National Park", wdpaid=4),
    ]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_wdpa_protected_areas(
            bbox=_EVERGLADES_BBOX,
            designation_filter=["national park"],  # lowercase / plural / abbrev
        )

    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2, f"expected 2 National Park features, got {len(gdf)}"
        names = sorted(gdf["name_eng"].tolist())
        assert names == ["Biscayne NP", "Everglades NP"]
        # The label flows from the canonical token, not the lowercase input.
        assert "National Park" in result.name
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_abbreviation_filter_matches_through_public_tool():
    """designation_filter=['NP'] resolves to 'National Park' and matches."""
    import geopandas as gpd

    features = [
        _polygon_feature("Everglades NP", designation="National Park", wdpaid=1),
        _polygon_feature(
            "Loxahatchee NWR",
            designation="National Wildlife Refuge",
            wdpaid=3,
        ),
    ]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_wdpa_protected_areas(
            bbox=_EVERGLADES_BBOX,
            designation_filter=["NP"],
        )

    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        assert gdf["name_eng"].tolist() == ["Everglades NP"]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_unknown_designation_filter_raises_through_public_tool():
    """An unknown designation_filter fails LOUD before any network call."""
    with pytest.raises(WDPADesignationError):
        fetch_wdpa_protected_areas(
            bbox=_EVERGLADES_BBOX,
            designation_filter=["Marsupial Sanctuary"],
        )


def test_filter_matching_zero_of_many_raises_with_present_designations():
    """A valid filter that eliminates every fetched feature fails LOUD.

    Honest-degrade (data-source-fallback norm): instead of a silent
    0-feature FlatGeobuf, list the designations actually present so the
    caller can correct the filter.
    """
    features = [
        _polygon_feature("Big Cypress", designation="National Preserve", wdpaid=2),
        _polygon_feature(
            "Loxahatchee NWR",
            designation="National Wildlife Refuge",
            wdpaid=3,
        ),
    ]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(WDPADesignationError) as exc:
            fetch_wdpa_protected_areas(
                bbox=_EVERGLADES_BBOX,
                designation_filter=["National Park"],
            )

    msg = str(exc.value)
    # Must surface the designations actually present in the bbox.
    assert "National Preserve" in msg
    assert "National Wildlife Refuge" in msg


# ---------------------------------------------------------------------------
# Mocked-network tests.
# ---------------------------------------------------------------------------


def test_mocked_100_features_returns_100_polygons():
    """A mocked 100-feature response yields a FlatGeobuf with 100 polygons."""
    import geopandas as gpd

    features = [
        _polygon_feature(f"Park {i}", wdpaid=i)
        for i in range(100)
    ]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)

    # Round-trip the stored bytes through geopandas.
    assert len(fake_gcs.store) == 1
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 100, f"expected 100 features, got {len(gdf)}"
        assert result.layer_type == "vector"
        assert result.role == "context"
        assert result.units is None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_designation_filter_returns_subset():
    """designation_filter='National Park' returns subset matching DESIG_ENG."""
    import geopandas as gpd

    features = [
        _polygon_feature("Everglades NP", designation="National Park", wdpaid=1),
        _polygon_feature(
            "Big Cypress National Preserve",
            designation="National Preserve",
            wdpaid=2,
        ),
        _polygon_feature(
            "Loxahatchee NWR",
            designation="National Wildlife Refuge",
            wdpaid=3,
        ),
        _polygon_feature("Biscayne NP", designation="National Park", wdpaid=4),
    ]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_wdpa_protected_areas(
            bbox=_EVERGLADES_BBOX,
            designation_filter=["National Park"],
        )

    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2, f"expected 2 National Park features, got {len(gdf)}"
        names = sorted(gdf["name_eng"].tolist())
        assert names == ["Biscayne NP", "Everglades NP"]
        # Filter label flows into the LayerURI name.
        assert "National Park" in result.name
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_empty_bbox_returns_zero_features():
    """An empty WDPA response (open water) yields an empty FlatGeobuf without raising."""
    import geopandas as gpd

    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response([], exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_wdpa_protected_areas(bbox=_OCEAN_BBOX)

    # Should not raise. Bytes should be valid empty FlatGeobuf.
    assert result.uri is not None
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0, "Empty FlatGeobuf should still be non-zero bytes"
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 0, f"expected 0 features, got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_pagination_across_4000_features():
    """Two pages of 2000 features each merge into a single 4000-feature FlatGeobuf."""
    import geopandas as gpd

    page1 = [_polygon_feature(f"Park p1-{i}", wdpaid=i) for i in range(2000)]
    page2 = [_polygon_feature(f"Park p2-{i}", wdpaid=2000 + i) for i in range(2000)]

    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}

    def side_effect(bbox, offset):
        call_count["n"] += 1
        if offset == 0:
            return _wdpa_response(page1, exceeded=True)
        elif offset == 2000:
            return _wdpa_response(page2, exceeded=False)
        else:
            raise AssertionError(f"unexpected offset={offset}")

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        side_effect=side_effect,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)

    assert call_count["n"] == 2, f"expected 2 pages fetched, got {call_count['n']}"
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 4000, f"expected 4000 features after merge, got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_cache_miss_then_hit_skips_fetch_fn():
    """Second call with the same bbox + filter is a cache HIT and does not re-fetch."""
    features = [_polygon_feature("Everglades NP", wdpaid=1)]
    fake_gcs = FakeStorageClient()
    page_call_count = {"n": 0}

    def page_side_effect(bbox, offset):
        page_call_count["n"] += 1
        return _wdpa_response(features, exceeded=False)

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        side_effect=page_side_effect,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)
        r2 = fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)

    assert page_call_count["n"] == 1, (
        f"fetch_fn should run once (miss); got {page_call_count['n']} calls"
    )
    assert r1.uri == r2.uri


def test_designation_filter_changes_cache_key():
    """Different designation_filter produces a different cache key (separate URIs)."""
    features = [_polygon_feature("Everglades NP", wdpaid=1)]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_all = fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)
        r_np = fetch_wdpa_protected_areas(
            bbox=_EVERGLADES_BBOX, designation_filter=["National Park"]
        )

    assert r_all.uri != r_np.uri, (
        "Different designation_filter should produce different cache keys"
    )


def test_upstream_error_envelope_raises_wdpa_upstream_error():
    """An ArcGIS error envelope (200 OK with {error}) raises WDPAUpstreamError."""
    error_payload = {"error": {"code": 500, "message": "internal error"}}

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        side_effect=lambda bbox, offset: (_ for _ in ()).throw(
            WDPAUpstreamError("WDPA query returned error envelope")
        ),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(FakeStorageClient()),
    ):
        with pytest.raises(WDPAUpstreamError):
            fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)


def test_upstream_error_is_retryable():
    """WDPAUpstreamError carries retryable=True for FR-AS-11 mapping."""
    err = WDPAUpstreamError("test")
    assert err.retryable is True
    assert err.error_code == "WDPA_UPSTREAM_ERROR"


def test_bbox_error_is_not_retryable():
    """WDPABboxError carries retryable=False (no point in retrying invalid input)."""
    err = WDPABboxError("test")
    assert err.retryable is False
    assert err.error_code == "WDPA_BBOX_INVALID"


def test_layer_uri_shape():
    """The LayerURI shape is correct (vector, context, units=None)."""
    fake_gcs = FakeStorageClient()
    features = [_polygon_feature("Test Park", wdpaid=1)]
    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas._wdpa_query_one_page",
        return_value=_wdpa_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_wdpa_protected_areas(bbox=_EVERGLADES_BBOX)

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "/wdpa/" in result.uri
    assert result.uri.endswith(".fgb")
    assert result.style_preset == "wdpa_protected_areas"
    assert "WDPA" in result.name


# ---------------------------------------------------------------------------
# Live integration tests (TRID3NT_TEST_LIVE_WDPA=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_WDPA,
    reason="Set TRID3NT_TEST_LIVE_WDPA=1 to run live WDPA download tests",
)
def test_live_everglades_returns_everglades_np():
    """LIVE: queries real WDPA and verifies Everglades National Park is in results.

    Codified job-0086 lesson: not just bytes-round-trip; we verify the known
    geography (Everglades NP exists within the Everglades bbox).
    """
    import geopandas as gpd
    from trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas import (
        _fetch_wdpa_bytes,
    )

    fgb_bytes = _fetch_wdpa_bytes(_EVERGLADES_BBOX, designation_filter=None)
    assert len(fgb_bytes) > 0

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1, "expected at least 1 protected area in Everglades bbox"
        name_col = "name_eng" if "name_eng" in gdf.columns else "NAME"
        names = gdf[name_col].dropna().str.lower().tolist() if name_col in gdf.columns else []
        assert any("everglades" in n for n in names), (
            f"expected Everglades NP in {names}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
