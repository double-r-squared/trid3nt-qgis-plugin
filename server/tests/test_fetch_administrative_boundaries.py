"""Unit tests for the ``fetch_administrative_boundaries`` atomic tool (job-0084).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Unknown level raises ``AdminBoundaryLevelError`` (typed error, not-retryable).
- bbox over Fort Myers FL returns Lee County polygon (county level).
- bbox over Fort Myers FL returns a Fort Myers place polygon (place level).
- bbox over a single state returns ≥1 feature (state level).
- Cache miss: fetch_fn is invoked and bytes are written to the store.
- Cache hit: second call with same params skips the fetch_fn.
- ``_state_fips_for_bbox`` returns Florida FIPS ("12") for a FL bbox.
- ``_tiger_url`` constructs the expected URLs.
- ``_round_bbox_to_6dp`` rounds to 6 decimal places.

Tests that perform real network downloads are marked ``live`` and gated by the
``GRACE2_TEST_LIVE_TIGER`` environment variable (set to "1" to enable). All
other tests use patched network calls and/or fake GCS and run unconditionally.
"""

from __future__ import annotations

import os
import tempfile
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_administrative_boundaries import (
    AdminBoundaryEmptyError,
    AdminBoundaryError,
    AdminBoundaryLevelError,
    AdminBoundaryUpstreamError,
    _ALASKA_ANTIMERIDIAN_BBOX,
    _ALASKA_FIPS,
    _fetch_admin_boundaries_bytes,
    _round_bbox_to_6dp,
    _state_fips_for_bbox,
    _tiger_url,
    _VALID_LEVELS,
    fetch_administrative_boundaries,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers, FL bounding box (county-level: Lee County envelope)
_FORT_MYERS_BBOX = (-82.3, 26.3, -81.6, 26.8)

# Florida FIPS
_FL_FIPS = "12"

# Marker for live tests
_LIVE_TIGER = os.environ.get("GRACE2_TEST_LIVE_TIGER") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors existing test pattern from test_compute_colored_relief.py).
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


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    """Minimal FlatGeobuf-shaped bytes placeholder for cache tests."""
    return b"FAKE_TIGER_FGB_" + tag.encode() + b"\x00" * 16


# ---------------------------------------------------------------------------
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_administrative_boundaries appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_administrative_boundaries" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_administrative_boundaries"]
    assert entry.metadata.name == "fetch_administrative_boundaries"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "admin_boundaries"
    assert entry.metadata.cacheable is True


def test_four_levels_are_defined():
    """All four required level names are present in _VALID_LEVELS."""
    assert _VALID_LEVELS == {"state", "county", "place", "zcta"}


# ---------------------------------------------------------------------------
# Typed-error tests (no network needed).
# ---------------------------------------------------------------------------


def test_unknown_level_raises_typed_error():
    """Passing an unknown level raises AdminBoundaryLevelError (not generic RuntimeError)."""
    with pytest.raises(AdminBoundaryLevelError, match="unknown level"):
        fetch_administrative_boundaries(
            level="municipality",  # type: ignore[arg-type]
            bbox=_FORT_MYERS_BBOX,
        )


def test_level_error_is_not_retryable():
    """AdminBoundaryLevelError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_administrative_boundaries(
            level="bogus",  # type: ignore[arg-type]
            bbox=_FORT_MYERS_BBOX,
        )
    except AdminBoundaryLevelError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected AdminBoundaryLevelError")


def test_degenerate_bbox_raises_admin_boundary_error():
    """A bbox where min == max raises AdminBoundaryError before any download."""
    with pytest.raises(AdminBoundaryError):
        fetch_administrative_boundaries(level="state", bbox=(-82.0, 26.5, -82.0, 26.5))


# ---------------------------------------------------------------------------
# Helper / URL-builder tests (pure Python, no network).
# ---------------------------------------------------------------------------


def test_state_fips_for_bbox_returns_florida_for_fort_myers():
    """_state_fips_for_bbox returns Florida ('12') for the Fort Myers bbox."""
    fips_list = _state_fips_for_bbox(_FORT_MYERS_BBOX)
    assert _FL_FIPS in fips_list, (
        f"Expected Florida FIPS '12' in {fips_list} for bbox {_FORT_MYERS_BBOX}"
    )


def test_state_fips_for_bbox_returns_empty_for_ocean():
    """_state_fips_for_bbox returns [] for a bbox in the middle of the Atlantic."""
    fips_list = _state_fips_for_bbox((-50.0, 40.0, -45.0, 45.0))
    assert fips_list == [], f"Expected empty list for Atlantic bbox; got {fips_list}"


# ---------------------------------------------------------------------------
# Routing-hazard tests: the AK antimeridian fix + the corrected error category
# for an unroutable place bbox (the "goes18 vs goes-18" identifier/routing
# bug class -- a heuristic-built routing token that must match coverage and
# fail with the RIGHT typed error, never a misleading upstream/silent dead-end).
# ---------------------------------------------------------------------------


def test_state_fips_for_bbox_routes_eastern_aleutians_to_alaska():
    """Adak (negative-lon Aleutians, ~ -176) routes to AK ('02')."""
    fips_list = _state_fips_for_bbox((-176.0, 51.5, -174.0, 52.5))
    assert _ALASKA_FIPS in fips_list, (
        f"Expected AK '02' for the eastern Aleutians; got {fips_list}"
    )


def test_state_fips_for_bbox_routes_western_aleutians_across_antimeridian():
    """Attu (western Aleutians, positive-lon ~ +173) routes to AK ('02').

    This is the antimeridian-crossing case the single AK envelope missed: the
    western Aleutian Islands use eastern-hemisphere (positive) longitudes, so a
    valid US place query there must NOT dead-end.
    """
    fips_list = _state_fips_for_bbox((172.0, 52.5, 173.5, 53.2))
    assert _ALASKA_FIPS in fips_list, (
        f"Expected AK '02' for the trans-antimeridian western Aleutians; got {fips_list}"
    )


def test_state_fips_for_bbox_no_duplicate_alaska_for_mainland():
    """Mainland AK routes to a single '02' (the antimeridian tail must not dup it)."""
    fips_list = _state_fips_for_bbox((-150.0, 60.0, -149.0, 61.5))
    assert fips_list.count(_ALASKA_FIPS) == 1, (
        f"Expected exactly one AK '02' entry; got {fips_list}"
    )


def test_alaska_antimeridian_bbox_is_eastern_hemisphere():
    """The AK antimeridian envelope uses positive (eastern-hemisphere) longitudes."""
    a_min_lon, _a_min_lat, a_max_lon, _a_max_lat = _ALASKA_ANTIMERIDIAN_BBOX
    assert a_min_lon > 0 and a_max_lon > 0, (
        f"AK antimeridian box must be positive-lon; got {_ALASKA_ANTIMERIDIAN_BBOX}"
    )
    assert a_max_lon <= 180.0, "Longitude must not exceed +180"


def test_unroutable_place_bbox_raises_level_error_not_upstream():
    """An ocean place bbox raises AdminBoundaryLevelError (routing), NOT a misleading
    AdminBoundaryUpstreamError.

    Nothing is fetched from census.gov for an unroutable bbox, so labeling it an
    UPSTREAM failure is wrong/misleading. The error must name the real cause
    (not routable to a TIGER state) and the actionable fallback (use county /
    a CONUS-or-territory bbox).
    """
    with pytest.raises(AdminBoundaryLevelError, match="not routable to a TIGER state"):
        _fetch_admin_boundaries_bytes("place", (-50.0, 40.0, -45.0, 45.0))


def test_unroutable_place_error_mentions_county_fallback():
    """The routing error guides the caller to the level='county' fallback."""
    try:
        _fetch_admin_boundaries_bytes("place", (-50.0, 40.0, -45.0, 45.0))
    except AdminBoundaryLevelError as exc:
        assert "county" in str(exc).lower(), (
            f"Expected the error to suggest level='county'; got {exc!r}"
        )
        assert exc.retryable is False
    else:
        pytest.fail("Expected AdminBoundaryLevelError for an unroutable place bbox")


@pytest.mark.parametrize("level,expected_fragment", [
    ("state", "STATE/tl_2024_us_state.zip"),
    ("county", "COUNTY/tl_2024_us_county.zip"),
    ("zcta", "ZCTA520/tl_2024_us_zcta520.zip"),
])
def test_tiger_url_nationwide_levels(level: str, expected_fragment: str):
    """_tiger_url returns the correct census.gov path for nationwide levels."""
    url = _tiger_url(level)
    assert expected_fragment in url, (
        f"Expected {expected_fragment!r} in URL for level={level!r}; got {url!r}"
    )


def test_tiger_url_place_with_fips():
    """_tiger_url for place uses the state FIPS in the filename."""
    url = _tiger_url("place", state_fips="12")
    assert "PLACE/tl_2024_12_place.zip" in url, (
        f"Expected per-state place URL; got {url!r}"
    )


def test_tiger_url_place_without_fips_raises():
    """_tiger_url for place without state_fips raises AdminBoundaryLevelError."""
    with pytest.raises(AdminBoundaryLevelError):
        _tiger_url("place", state_fips=None)


def test_tiger_url_place_for_alaska_matches_exact_census_format():
    """The AK place URL is the EXACT census.gov token (tl_2024_02_place.zip).

    Identifier-format guard (the "goes18 vs goes-18" class): the 2-digit FIPS is
    glued into the filename with single underscores; no extra hyphen/space must
    sneak in for the antimeridian-routed AK case.
    """
    url = _tiger_url("place", state_fips="02")
    assert url == (
        "https://www2.census.gov/geo/tiger/TIGER2024/PLACE/tl_2024_02_place.zip"
    ), f"Unexpected AK place URL: {url!r}"
    # Defensive: the only hyphen must be in the host, never in the filename token.
    assert "tl_2024-02" not in url and "tl_2024_02-place" not in url


def test_round_bbox_to_6dp():
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-82.123456789, 26.123456789, -81.987654321, 26.987654321)
    rounded = _round_bbox_to_6dp(raw)
    for v in rounded:
        # Check no more than 6 decimal places.
        assert round(v, 6) == v, f"Expected 6dp; got {v!r}"
    assert rounded == (-82.123457, 26.123457, -81.987654, 26.987654)


# ---------------------------------------------------------------------------
# Cache-layer tests (pure Python, patched network).
# ---------------------------------------------------------------------------


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


def test_cache_miss_invokes_fetch_fn_and_writes_store():
    """On first call (cache miss), the fetch_fn is invoked and the result is stored."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("COUNTY")

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_administrative_boundaries._fetch_admin_boundaries_bytes",
        side_effect=lambda level, bbox: fake_fetch(),
    ), patch(
        "grace2_agent.tools.fetch_administrative_boundaries.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_administrative_boundaries(level="county", bbox=_FORT_MYERS_BBOX)

    assert fetch_count["n"] == 1, "fetch_fn should have been called once on cache miss"
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert len(fake_gcs.store) == 1, "One artifact should be in the fake GCS store"


def test_cache_hit_skips_fetch_fn():
    """On second call (cache hit), the fetch_fn is NOT invoked."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("COUNTY_HIT")

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_administrative_boundaries._fetch_admin_boundaries_bytes",
        side_effect=lambda level, bbox: fake_fetch(),
    ), patch(
        "grace2_agent.tools.fetch_administrative_boundaries.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_administrative_boundaries(level="county", bbox=_FORT_MYERS_BBOX)
        r2 = fetch_administrative_boundaries(level="county", bbox=_FORT_MYERS_BBOX)

    assert fetch_count["n"] == 1, "fetch_fn should be called only once (second call is a HIT)"
    assert r1.uri == r2.uri, "Both calls should return the same cached URI"


# ---------------------------------------------------------------------------
# LayerURI shape tests (patched network, no GCS).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level,expected_label_fragment", [
    ("state", "States"),
    ("county", "Counties"),
    ("place", "Places"),
    ("zcta", "ZIP"),
])
def test_layer_uri_shape(level: str, expected_label_fragment: str):
    """fetch_administrative_boundaries returns a LayerURI with expected shape."""
    fake_gcs = FakeStorageClient()
    fake_bytes = _fake_fgb_bytes(level.upper())

    with patch(
        "grace2_agent.tools.fetch_administrative_boundaries._fetch_admin_boundaries_bytes",
        return_value=fake_bytes,
    ), patch(
        "grace2_agent.tools.fetch_administrative_boundaries.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_administrative_boundaries(level=level, bbox=_FORT_MYERS_BBOX)

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "admin_boundaries" in result.uri
    assert expected_label_fragment in result.name, (
        f"Expected {expected_label_fragment!r} in name={result.name!r} for level={level!r}"
    )
    assert "TIGER 2024" in result.name


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_TIGER=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_TIGER,
    reason="Set GRACE2_TEST_LIVE_TIGER=1 to run live Census TIGER download tests",
)
def test_live_county_fort_myers_returns_lee_county():
    """LIVE: downloads real TIGER 2024 county file and clips to Fort Myers bbox.

    Verifies Lee County polygon is present in the result FlatGeobuf.
    Writes the actual FlatGeobuf and checks it is non-empty and readable.
    """
    import geopandas as gpd

    from grace2_agent.tools.fetch_administrative_boundaries import (
        _fetch_admin_boundaries_bytes,
    )

    fgb_bytes = _fetch_admin_boundaries_bytes(level="county", bbox=_FORT_MYERS_BBOX)
    assert len(fgb_bytes) > 0, "FlatGeobuf bytes should be non-empty"

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1, "Expected at least 1 feature"

        # Check for Lee County in the NAME or NAMELSAD columns.
        name_col = "NAMELSAD" if "NAMELSAD" in gdf.columns else "NAME"
        names = gdf[name_col].str.lower().tolist() if name_col in gdf.columns else []
        assert any("lee" in n for n in names), (
            f"Expected Lee County in {names}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_TIGER,
    reason="Set GRACE2_TEST_LIVE_TIGER=1 to run live Census TIGER download tests",
)
def test_live_place_fort_myers_returns_fort_myers_cdp():
    """LIVE: downloads real TIGER 2024 place file for FL and clips to Fort Myers bbox.

    Verifies a Fort Myers place polygon is present in the result.
    """
    import geopandas as gpd

    from grace2_agent.tools.fetch_administrative_boundaries import (
        _fetch_admin_boundaries_bytes,
    )

    fgb_bytes = _fetch_admin_boundaries_bytes(level="place", bbox=_FORT_MYERS_BBOX)
    assert len(fgb_bytes) > 0

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1, "Expected at least 1 feature"

        name_col = "NAMELSAD" if "NAMELSAD" in gdf.columns else "NAME"
        names = gdf[name_col].str.lower().tolist() if name_col in gdf.columns else []
        assert any("fort myers" in n for n in names), (
            f"Expected Fort Myers in place names: {names}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_TIGER,
    reason="Set GRACE2_TEST_LIVE_TIGER=1 to run live Census TIGER download tests",
)
def test_live_state_returns_at_least_one_feature():
    """LIVE: bbox over a single state returns ≥1 feature (state level)."""
    import geopandas as gpd

    from grace2_agent.tools.fetch_administrative_boundaries import (
        _fetch_admin_boundaries_bytes,
    )

    # Small bbox entirely within Florida.
    florida_bbox = (-82.5, 26.0, -81.5, 27.0)
    fgb_bytes = _fetch_admin_boundaries_bytes(level="state", bbox=florida_bbox)
    assert len(fgb_bytes) > 0

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1, "Expected at least 1 state feature"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
