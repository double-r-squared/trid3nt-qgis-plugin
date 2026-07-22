"""Unit tests for the ``fetch_inaturalist_observations`` atomic tool (job-0088).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata (cacheable,
  static-30d, source_class="inaturalist").
- Invalid quality_grade / bad bbox / non-positive days_back / max_records
  → INatInputError (retryable=False).
- Taxon-name resolution path: 'Trichechus manatus' resolves via taxa lookup.
- Single-page happy path: 200-record mock response writes one FGB to cache,
  decodes back to 200 points (all inside bbox — geographic-correctness check
  per codified job-0086 lesson).
- Pagination: 400 records across 2 pages.
- max_records cap respected.
- Cache: miss invokes fetch_fn; second identical call is a hit.
- Cache key collapses str("American alligator") and int(name-resolved-id) onto
  the same path (cache-key uses resolved int per audit.md).
- Empty quality-grade=any vs research distinct cache paths.
- Live test (env TRID3NT_TEST_LIVE_INAT=1) over manatee + FL Gulf bbox returns
  ≥1 feature whose coordinates fall inside the bbox.

Live network tests are gated by ``TRID3NT_TEST_LIVE_INAT=1``.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations import (
    INatError,
    INatInputError,
    INatUpstreamError,
    _coerce_taxon_id,
    _extract_observation_record,
    _resolve_taxon_id,
    _round_bbox_to_6dp,
    _validate_bbox,
    fetch_inaturalist_observations,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Florida Gulf coast bbox spanning Charlotte Harbor + Sanibel area — well-known
# manatee habitat. Used by both mocked and live tests.
_FL_GULF_BBOX = (-82.4, 26.4, -81.7, 26.9)

# Everglades / Big Cypress bbox — alligator habitat for live verification.
_EVERGLADES_BBOX = (-81.5, 25.5, -80.5, 26.5)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_INAT") == "1"


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
# Fake httpx.Client (avoids MagicMock-spec-of-Mock pitfall when patching the
# httpx.Client constructor).
# ---------------------------------------------------------------------------


class _FakeHttpxClient:
    """Plain stand-in for httpx.Client supporting the methods the tool calls.

    Acts as a context manager + has ``.get(url, params=...) -> response``. The
    response builder is supplied at construction so each call can return a
    different payload via an iterator. ``close()`` is a no-op.
    """

    def __init__(self, response_factory):
        # response_factory: zero-arg callable returning an httpx.Response.
        self._response_factory = response_factory
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, **_):
        self.get_calls.append((url, params or {}))
        return self._response_factory()

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


# ---------------------------------------------------------------------------
# Mock iNat response helpers.
# ---------------------------------------------------------------------------


def _mock_observation(obs_id: int, lon: float, lat: float, *, species="Manatee", user="alice") -> dict[str, Any]:
    """Build a single iNat-shaped observation dict for testing."""
    return {
        "id": obs_id,
        "observed_on": "2026-05-15",
        "user": {"login": user},
        "photos": [{"url": f"https://inat.example/photo/{obs_id}.jpg"}],
        "species_guess": species,
        "place_guess": "Fort Myers, FL",
        "geojson": {"type": "Point", "coordinates": [lon, lat]},
    }


def _mock_observations_page(records: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {
        "total_results": total,
        "page": 1,
        "per_page": len(records),
        "results": records,
    }


def _fake_httpx_response(json_data: dict[str, Any], status_code: int = 200) -> httpx.Response:
    """Build a real httpx.Response with the supplied JSON body."""
    return httpx.Response(
        status_code=status_code,
        content=__import__("json").dumps(json_data).encode("utf-8"),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://api.inaturalist.org/v1/observations"),
    )


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_inaturalist_observations appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_inaturalist_observations" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_inaturalist_observations"]
    assert entry.metadata.name == "fetch_inaturalist_observations"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "inaturalist"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Pure-Python helper tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_rejects_degenerate():
    with pytest.raises(INatInputError):
        _validate_bbox((-82.0, 26.5, -82.0, 26.5))


def test_validate_bbox_rejects_out_of_range_lat():
    with pytest.raises(INatInputError):
        _validate_bbox((-82.0, -91.0, -81.0, 27.0))


def test_round_bbox_to_6dp():
    raw = (-82.123456789, 26.123456789, -81.987654321, 26.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-82.123457, 26.123457, -81.987654, 26.987654)


def test_extract_observation_record_handles_missing_geometry():
    obs = {"id": 1, "geojson": None}
    assert _extract_observation_record(obs) is None


def test_extract_observation_record_happy_path():
    obs = _mock_observation(101, -82.0, 26.5, species="manatee")
    rec = _extract_observation_record(obs)
    assert rec is not None
    assert rec["id"] == 101
    assert rec["lon"] == -82.0
    assert rec["lat"] == 26.5
    assert rec["species_guess"] == "manatee"
    assert rec["user_login"] == "alice"
    assert "photo" in (rec["photo_url"] or "")


# ---------------------------------------------------------------------------
# Input-validation tests at the public surface.
# ---------------------------------------------------------------------------


def test_bad_quality_grade_raises_input_error():
    with pytest.raises(INatInputError, match="quality_grade"):
        fetch_inaturalist_observations(
            taxon_id=43616,
            bbox=_FL_GULF_BBOX,
            quality_grade="bogus",
        )


def test_input_error_is_not_retryable():
    try:
        fetch_inaturalist_observations(
            taxon_id=43616,
            bbox=_FL_GULF_BBOX,
            quality_grade="bogus",
        )
    except INatInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected INatInputError")


def test_zero_days_back_raises_input_error():
    with pytest.raises(INatInputError, match="days_back"):
        fetch_inaturalist_observations(
            taxon_id=43616,
            bbox=_FL_GULF_BBOX,
            days_back=0,
        )


def test_zero_max_records_raises_input_error():
    with pytest.raises(INatInputError, match="max_records"):
        fetch_inaturalist_observations(
            taxon_id=43616,
            bbox=_FL_GULF_BBOX,
            max_records=0,
        )


def test_coerce_taxon_id_rejects_bool():
    with pytest.raises(INatInputError):
        _coerce_taxon_id(True)  # type: ignore[arg-type]


def test_coerce_taxon_id_rejects_empty_string():
    with pytest.raises(INatInputError):
        _coerce_taxon_id("   ")


def test_coerce_taxon_id_accepts_digit_string():
    assert _coerce_taxon_id("43616") == 43616


# ---------------------------------------------------------------------------
# Taxon-name resolution path (mocked HTTP).
# ---------------------------------------------------------------------------


def test_resolve_taxon_id_happy_path():
    """A name lookup returning a hit yields the int id."""
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _fake_httpx_response(
        {"results": [{"id": 43616, "name": "Trichechus manatus"}]}
    )
    assert _resolve_taxon_id("Trichechus manatus", client=client) == 43616
    args, kwargs = client.get.call_args
    assert "taxa" in args[0]
    assert kwargs["params"]["q"] == "Trichechus manatus"


def test_resolve_taxon_id_no_results_raises_input_error():
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _fake_httpx_response({"results": []})
    with pytest.raises(INatInputError, match="no results"):
        _resolve_taxon_id("Nonexistent species 12345", client=client)


def test_resolve_taxon_id_http_error_raises_upstream():
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = httpx.ConnectError("network down")
    with pytest.raises(INatUpstreamError):
        _resolve_taxon_id("Trichechus manatus", client=client)


# ---------------------------------------------------------------------------
# Happy-path: 200-record single-page response → FlatGeobuf via fake GCS.
# Verifies geographic-correctness (codified job-0086 lesson): points fall
# inside the requested bbox.
# ---------------------------------------------------------------------------


def _decode_fgb_features(fgb_bytes: bytes):
    """Decode a FlatGeobuf bytes blob to a list of (lon, lat) tuples."""
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
    finally:
        os.unlink(path)
    coords = []
    for geom in gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        coords.append((geom.x, geom.y))
    return gdf, coords


def test_happy_path_200_records_writes_fgb_with_points_inside_bbox():
    """200-record mock response writes one FGB to cache with 200 valid points inside bbox."""
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX
    # Generate 200 mock observations across the bbox interior.
    lon_lo, lat_lo, lon_hi, lat_hi = bbox
    records = []
    for i in range(200):
        # Spread across bbox with a stable deterministic pattern
        t = i / 199.0
        lon = lon_lo + t * (lon_hi - lon_lo)
        lat = lat_lo + ((i % 7) / 6.0) * (lat_hi - lat_lo) * 0.9 + 0.01
        records.append(_mock_observation(1000 + i, lon, lat))
    page1 = _mock_observations_page(records, total=200)

    def make_client(*a, **kw):
        return _FakeHttpxClient(lambda: _fake_httpx_response(page1))

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)

    assert result.uri.startswith("s3://"), f"Unexpected URI: {result.uri}"
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.bbox is not None
    # One artifact in the fake GCS.
    assert len(fake_gcs.store) == 1
    fgb_bytes = next(iter(fake_gcs.store.values()))
    gdf, coords = _decode_fgb_features(fgb_bytes)
    assert len(coords) == 200, f"Expected 200 points; got {len(coords)}"
    # GEOGRAPHIC-CORRECTNESS CHECK (codified job-0086 lesson): every point
    # must fall inside the requested bbox.
    for lon, lat in coords:
        assert lon_lo <= lon <= lon_hi, f"point lon={lon} outside bbox lon=[{lon_lo},{lon_hi}]"
        assert lat_lo <= lat <= lat_hi, f"point lat={lat} outside bbox lat=[{lat_lo},{lat_hi}]"


# ---------------------------------------------------------------------------
# Pagination: 400 records across 2 pages.
# ---------------------------------------------------------------------------


def test_pagination_walks_until_total_results_exhausted():
    """400-record dataset spread across 2 pages of 200 each."""
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX

    page1_records = [
        _mock_observation(2000 + i, bbox[0] + 0.1, bbox[1] + 0.1) for i in range(200)
    ]
    page2_records = [
        _mock_observation(3000 + i, bbox[0] + 0.2, bbox[1] + 0.2) for i in range(200)
    ]
    response_queue = [
        _mock_observations_page(page1_records, total=400),
        _mock_observations_page(page2_records, total=400),
    ]
    captured_client: dict[str, _FakeHttpxClient] = {}

    def next_response():
        if response_queue:
            return _fake_httpx_response(response_queue.pop(0))
        # Defensive: if a third call sneaks in, return an empty page so
        # the loop terminates cleanly rather than hanging the test.
        return _fake_httpx_response(_mock_observations_page([], total=400))

    def make_client(*a, **kw):
        c = _FakeHttpxClient(next_response)
        captured_client["c"] = c
        return c

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)

    client = captured_client["c"]
    assert len(client.get_calls) == 2, (
        f"Expected 2 page calls; got {len(client.get_calls)}"
    )
    # Sanity: page numbers in the params progress 1 then 2.
    assert client.get_calls[0][1].get("page") == 1
    assert client.get_calls[1][1].get("page") == 2
    fgb_bytes = next(iter(fake_gcs.store.values()))
    _, coords = _decode_fgb_features(fgb_bytes)
    assert len(coords) == 400, f"Expected 400 points across 2 pages; got {len(coords)}"


def test_max_records_caps_fetch():
    """max_records=50 stops the walk early even with more upstream data available."""
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX

    page1_records = [
        _mock_observation(4000 + i, bbox[0] + 0.1, bbox[1] + 0.1) for i in range(200)
    ]
    page = _mock_observations_page(page1_records, total=1000)

    def make_client(*a, **kw):
        return _FakeHttpxClient(lambda: _fake_httpx_response(page))

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_inaturalist_observations(
            taxon_id=43616, bbox=bbox, max_records=50
        )

    fgb_bytes = next(iter(fake_gcs.store.values()))
    _, coords = _decode_fgb_features(fgb_bytes)
    assert len(coords) == 50, f"Expected 50 (capped); got {len(coords)}"


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_and_writes_store():
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX
    fetch_calls = {"n": 0}

    page = _mock_observations_page(
        [_mock_observation(9001, bbox[0] + 0.05, bbox[1] + 0.05)], total=1
    )

    def make_client(*a, **kw):
        fetch_calls["n"] += 1
        return _FakeHttpxClient(lambda: _fake_httpx_response(page))

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)

    assert fetch_calls["n"] == 1
    assert result.uri.startswith("s3://")
    assert len(fake_gcs.store) == 1


def test_cache_hit_skips_fetch_fn():
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX
    fetch_calls = {"n": 0}
    page = _mock_observations_page(
        [_mock_observation(9002, bbox[0] + 0.05, bbox[1] + 0.05)], total=1
    )

    def make_client(*a, **kw):
        fetch_calls["n"] += 1
        return _FakeHttpxClient(lambda: _fake_httpx_response(page))

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)
        r2 = fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)

    assert fetch_calls["n"] == 1, "Second call should be a cache hit; no fetch"
    assert r1.uri == r2.uri


def test_cache_key_collapses_name_and_resolved_id():
    """Calling with str-name vs int-id (same taxon) hits the same cache path."""
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX

    obs_page = _mock_observations_page(
        [_mock_observation(9003, bbox[0] + 0.05, bbox[1] + 0.05)], total=1
    )
    taxa_page = {"results": [{"id": 43616, "name": "Trichechus manatus"}]}

    # Build an iterator of responses that any client created in this test will
    # share. First the taxa-resolve response, then the observations response.
    response_queue = [taxa_page, obs_page, obs_page]

    def next_response():
        if response_queue:
            return _fake_httpx_response(response_queue.pop(0))
        return _fake_httpx_response(obs_page)

    def make_client(*a, **kw):
        return _FakeHttpxClient(next_response)

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_name = fetch_inaturalist_observations(
            taxon_id="Trichechus manatus", bbox=bbox
        )
        r_int = fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)

    assert r_name.uri == r_int.uri, (
        "Same taxon expressed as name vs int must collapse onto the same cache path"
    )
    assert len(fake_gcs.store) == 1


def test_quality_grade_changes_cache_path():
    """quality_grade='research' vs 'any' produce distinct cache paths."""
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX
    page = _mock_observations_page(
        [_mock_observation(9004, bbox[0] + 0.05, bbox[1] + 0.05)], total=1
    )

    def make_client(*a, **kw):
        return _FakeHttpxClient(lambda: _fake_httpx_response(page))

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_research = fetch_inaturalist_observations(
            taxon_id=43616, bbox=bbox, quality_grade="research"
        )
        r_any = fetch_inaturalist_observations(
            taxon_id=43616, bbox=bbox, quality_grade="any"
        )

    assert r_research.uri != r_any.uri
    assert len(fake_gcs.store) == 2


# ---------------------------------------------------------------------------
# Upstream-error tests.
# ---------------------------------------------------------------------------


def test_observations_http_error_surfaces_upstream_error():
    """A 503 from the iNat API raises INatUpstreamError (retryable=True)."""
    fake_gcs = FakeStorageClient()
    bbox = _FL_GULF_BBOX

    def make_bad_response():
        return httpx.Response(
            status_code=503,
            content=b"{}",
            request=httpx.Request("GET", "https://api.inaturalist.org/v1/observations"),
        )

    def make_client(*a, **kw):
        return _FakeHttpxClient(make_bad_response)

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.httpx.Client",
        side_effect=make_client,
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), pytest.raises(INatUpstreamError):
        fetch_inaturalist_observations(taxon_id=43616, bbox=bbox)


# ---------------------------------------------------------------------------
# LIVE test — env-guarded; one focused call.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_INAT=1 to enable")
def test_live_alligator_everglades_returns_geographically_valid_points():
    """Live: American alligator over Everglades returns ≥1 feature whose
    coordinates fall inside the requested bbox (geographic-correctness check,
    codified job-0086 lesson).
    """
    fake_gcs = FakeStorageClient()
    bbox = _EVERGLADES_BBOX

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_inaturalist_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_inaturalist_observations(
            taxon_id="American alligator",
            bbox=bbox,
            max_records=50,
        )

    assert result.uri.startswith("s3://")
    fgb_bytes = next(iter(fake_gcs.store.values()))
    gdf, coords = _decode_fgb_features(fgb_bytes)
    assert len(coords) >= 1, (
        f"Expected ≥1 alligator observation in Everglades bbox; got {len(coords)}"
    )
    lon_lo, lat_lo, lon_hi, lat_hi = bbox
    for lon, lat in coords:
        assert lon_lo <= lon <= lon_hi, (
            f"point lon={lon} outside requested bbox lon=[{lon_lo},{lon_hi}]"
        )
        assert lat_lo <= lat <= lat_hi, (
            f"point lat={lat} outside requested bbox lat=[{lat_lo},{lat_hi}]"
        )
