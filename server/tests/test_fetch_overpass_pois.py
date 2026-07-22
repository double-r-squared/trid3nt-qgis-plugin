"""Unit tests for the ``fetch_overpass_pois`` atomic tool.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata + flags.
- Tag resolution: amenity / tag='key=value' / bare-value alias / category /
  value; priority order; unknown bare value -> OverpassInputError.
- Overpass QL builder queries node/way/relation with ``out center``.
- Point extraction: node uses lat/lon, way/relation uses center centroid;
  out-of-bbox centroids dropped; missing coords dropped; tags_json carried.
- Synthetic end-to-end (mocked Overpass + in-memory S3 cache) -> N point
  features, correct extent, cache hit on the second call.
- Honest-empty: zero matched features -> OverpassNoFeaturesError (retryable=False).
- Input validation: bad bbox / missing tag / garbled token -> OverpassInputError.
- Upstream fallback: all mirrors 504 -> OverpassUpstreamError (retryable);
  a non-429 4xx short-circuits to OverpassInputError.
- estimate_payload_mb scales with bbox area, never zero.
- Live verification (``TRID3NT_TEST_LIVE_OSM=1``): SF hospitals -> >=1 point,
  all inside bbox.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois import (
    OVERPASS_ENDPOINTS,
    OverpassInputError,
    OverpassNoFeaturesError,
    OverpassPoiError,
    OverpassUpstreamError,
    _build_overpass_ql,
    _extract_point_records,
    _records_bbox,
    _resolve_tag,
    _validate_bbox,
    estimate_payload_mb,
    fetch_overpass_pois,
)

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# San Francisco core bbox (min_lon, min_lat, max_lon, max_lat).
_SF_BBOX = (-122.45, 37.74, -122.38, 37.80)

_LIVE_OSM = os.environ.get("TRID3NT_TEST_LIVE_OSM") == "1"


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors test_fetch_roads_osm.py).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake) -> Any:
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        ReadThroughResult,
        cache_path,
        compute_cache_key,
        is_cacheable,
    )

    store = fake.store

    def patched(metadata, params, ext, fetch_fn, **kw):  # type: ignore[no-untyped-def]
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


def _node(osm_id: int, lon: float, lat: float, **tags: Any) -> dict[str, Any]:
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags or {}}


def _way(osm_id: int, lon: float, lat: float, **tags: Any) -> dict[str, Any]:
    """A way element with a precomputed centroid (Overpass ``out center``)."""
    return {
        "type": "way",
        "id": osm_id,
        "center": {"lat": lat, "lon": lon},
        "tags": tags or {},
    }


def _payload(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 0.6, "generator": "Overpass API (mock)", "elements": elements}


def _fast_sleep(monkeypatch) -> None:
    monkeypatch.setattr(
        "trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.time.sleep", lambda *_a, **_k: None
    )


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_tool_is_registered_with_expected_metadata():
    assert "fetch_overpass_pois" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_overpass_pois"]
    assert entry.metadata.name == "fetch_overpass_pois"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "overpass_pois"
    assert entry.metadata.cacheable is True
    # Global query disabled (a bbox is required).
    assert getattr(entry.metadata, "supports_global_query", False) is False


# ---------------------------------------------------------------------------
# Tag resolution.
# ---------------------------------------------------------------------------


def test_resolve_tag_amenity_shortcut():
    assert _resolve_tag(None, "hospital", None, None) == ("amenity", "hospital")


def test_resolve_tag_explicit_key_value():
    assert _resolve_tag("emergency=fire_hydrant", None, None, None) == (
        "emergency",
        "fire_hydrant",
    )


def test_resolve_tag_bare_value_alias():
    # 'supermarket' maps to shop, not amenity.
    assert _resolve_tag("supermarket", None, None, None) == ("shop", "supermarket")
    assert _resolve_tag("hospital", None, None, None) == ("amenity", "hospital")


def test_resolve_tag_category_and_value():
    assert _resolve_tag(None, None, "power=substation", None) == ("power", "substation")
    assert _resolve_tag(None, None, None, "school") == ("amenity", "school")


def test_resolve_tag_priority_amenity_wins_over_tag():
    # amenity is checked first.
    assert _resolve_tag("shop=bakery", "hospital", None, None) == ("amenity", "hospital")


def test_resolve_tag_unknown_bare_value_raises():
    with pytest.raises(OverpassInputError):
        _resolve_tag("zzz_not_a_known_value", None, None, None)


def test_resolve_tag_missing_all_raises():
    with pytest.raises(OverpassInputError):
        _resolve_tag(None, None, None, None)


def test_resolve_tag_rejects_garbled_token():
    with pytest.raises(OverpassInputError):
        _resolve_tag('amenity=hosp"ital', None, None, None)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_validate_bbox_degenerate_raises():
    with pytest.raises(OverpassInputError):
        _validate_bbox((-122.45, 37.74, -122.45, 37.74))


def test_validate_bbox_out_of_range_raises():
    with pytest.raises(OverpassInputError):
        _validate_bbox((-200.0, 37.74, -122.38, 37.80))


def test_validate_bbox_wrong_length_raises():
    with pytest.raises(OverpassInputError):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_fetch_bad_bbox_raises_input_error():
    with pytest.raises(OverpassInputError):
        fetch_overpass_pois(bbox=(-122.45, 37.80, -122.38, 37.74), amenity="hospital")


def test_fetch_missing_tag_raises_input_error():
    with pytest.raises(OverpassInputError):
        fetch_overpass_pois(bbox=_SF_BBOX)


# ---------------------------------------------------------------------------
# Overpass QL builder.
# ---------------------------------------------------------------------------


def test_build_ql_queries_all_element_types_with_center():
    ql = _build_overpass_ql(_SF_BBOX, "amenity", "hospital")
    assert 'node["amenity"="hospital"]' in ql
    assert 'way["amenity"="hospital"]' in ql
    assert 'relation["amenity"="hospital"]' in ql
    assert ql.strip().endswith("out center;")
    # Overpass corner order is (south, west, north, east).
    assert "(37.74,-122.45,37.8,-122.38)" in ql


# ---------------------------------------------------------------------------
# Point extraction.
# ---------------------------------------------------------------------------


def test_extract_uses_node_coords_and_way_centroid():
    payload = _payload(
        [
            _node(1, -122.40, 37.76, name="A", amenity="hospital"),
            _way(2, -122.41, 37.77, name="B", amenity="hospital"),
        ]
    )
    recs = _extract_point_records(payload, _SF_BBOX)
    assert len(recs) == 2
    by_id = {r["osm_id"]: r for r in recs}
    assert by_id[1]["osm_type"] == "node"
    assert by_id[2]["osm_type"] == "way"
    assert by_id[1]["name"] == "A"
    # tags_json round-trips the full tag dict.
    assert json.loads(by_id[2]["tags_json"])["amenity"] == "hospital"


def test_extract_drops_out_of_bbox_centroid():
    payload = _payload(
        [
            _node(1, -122.40, 37.76, amenity="hospital"),
            _way(2, -130.0, 37.77, amenity="hospital"),  # centroid west of bbox
        ]
    )
    recs = _extract_point_records(payload, _SF_BBOX)
    assert [r["osm_id"] for r in recs] == [1]


def test_extract_drops_missing_coords():
    payload = _payload(
        [
            {"type": "way", "id": 9, "tags": {"amenity": "hospital"}},  # no center
            _node(1, -122.40, 37.76, amenity="hospital"),
        ]
    )
    recs = _extract_point_records(payload, _SF_BBOX)
    assert [r["osm_id"] for r in recs] == [1]


def test_records_bbox_pads_single_point():
    recs = [{"lon": -122.40, "lat": 37.76}]
    ext = _records_bbox(recs)
    assert ext is not None
    min_lon, min_lat, max_lon, max_lat = ext
    assert min_lon < -122.40 < max_lon
    assert min_lat < 37.76 < max_lat


# ---------------------------------------------------------------------------
# Synthetic end-to-end (mocked Overpass + in-memory S3).
# ---------------------------------------------------------------------------


def test_end_to_end_synthetic_features_and_cache_hit(monkeypatch):
    _fast_sleep(monkeypatch)
    fake = _FakeStore()
    elements = [
        _node(1, -122.40, 37.76, name="UCSF", amenity="hospital"),
        _way(2, -122.41, 37.77, name="SFGH", amenity="hospital"),
        _way(3, -122.43, 37.79, name="CPMC", amenity="hospital"),
    ]

    captured_url: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None):
            captured_url.append(url)
            return _Resp(_payload(elements))

    with patch("trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.httpx.Client", _Client), patch(
        "trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.read_through",
        _make_read_through_injector(fake),
    ):
        layer = fetch_overpass_pois(bbox=_SF_BBOX, amenity="hospital")
        assert layer.layer_type == "vector"
        assert layer.role == "primary"
        assert layer.style_preset == "overpass_pois"
        assert layer.uri is not None and layer.uri.endswith(".fgb")
        # extent fits the 3 points.
        assert layer.bbox is not None
        assert layer.bbox[0] <= -122.43 and layer.bbox[2] >= -122.40

        # Decode the cached FlatGeobuf to confirm 3 features round-trip.
        import geopandas as gpd  # noqa: PLC0415

        path = layer.uri.split("/", 3)[3]
        gdf = gpd.read_file(io_bytes(fake.store[path]))
        assert len(gdf) == 3
        assert set(gdf["value"]) == {"hospital"}

        # Second identical call is a cache HIT -> Overpass POSTed only once.
        n_posts_before = len(captured_url)
        fetch_overpass_pois(bbox=_SF_BBOX, amenity="hospital")
        assert len(captured_url) == n_posts_before  # no new POST


def io_bytes(b: bytes):
    import io

    return io.BytesIO(b)


# ---------------------------------------------------------------------------
# Honest-empty.
# ---------------------------------------------------------------------------


def test_zero_features_raises_no_features_error(monkeypatch):
    _fast_sleep(monkeypatch)
    fake = _FakeStore()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return _payload([])

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None):
            return _Resp()

    with patch("trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.httpx.Client", _Client), patch(
        "trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.read_through",
        _make_read_through_injector(fake),
    ):
        with pytest.raises(OverpassNoFeaturesError) as ei:
            fetch_overpass_pois(bbox=_SF_BBOX, amenity="hospital")
        assert ei.value.retryable is False


# ---------------------------------------------------------------------------
# Upstream fallback behaviour.
# ---------------------------------------------------------------------------


def test_all_mirrors_504_raises_upstream_error(monkeypatch):
    _fast_sleep(monkeypatch)

    def _raise_504(url, data=None):
        req = httpx.Request("POST", url)
        resp = httpx.Response(504, request=req)
        raise httpx.HTTPStatusError("gateway timeout", request=req, response=resp)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        post = staticmethod(_raise_504)

    with patch("trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.httpx.Client", _Client):
        from trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois import _post_overpass

        with pytest.raises(OverpassUpstreamError) as ei:
            _post_overpass("[out:json];out;")
        assert ei.value.retryable is True


def test_non_429_4xx_short_circuits_to_input_error(monkeypatch):
    _fast_sleep(monkeypatch)

    def _raise_400(url, data=None):
        req = httpx.Request("POST", url)
        resp = httpx.Response(400, request=req)
        raise httpx.HTTPStatusError("bad request", request=req, response=resp)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        post = staticmethod(_raise_400)

    with patch("trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.httpx.Client", _Client):
        from trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois import _post_overpass

        with pytest.raises(OverpassInputError):
            _post_overpass("[out:json];out;")


def test_endpoints_list_has_multiple_mirrors():
    # The fallback chain must have at least 2 independent mirrors.
    assert len(OVERPASS_ENDPOINTS) >= 2
    assert all(u.startswith("https://") for u in OVERPASS_ENDPOINTS)


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_scales_with_area_and_never_zero():
    small = estimate_payload_mb(bbox=(-122.45, 37.74, -122.40, 37.78))
    large = estimate_payload_mb(bbox=(-123.0, 37.0, -121.0, 39.0))
    assert small > 0.0
    assert large > small
    # No-bbox path still returns a positive default.
    assert estimate_payload_mb() > 0.0


# ---------------------------------------------------------------------------
# Error hierarchy.
# ---------------------------------------------------------------------------


def test_error_subclassing():
    assert issubclass(OverpassInputError, OverpassPoiError)
    assert issubclass(OverpassUpstreamError, OverpassPoiError)
    assert issubclass(OverpassNoFeaturesError, OverpassPoiError)
    assert OverpassInputError("x").retryable is False
    assert OverpassUpstreamError("x").retryable is True
    assert OverpassNoFeaturesError("x").retryable is False


# ---------------------------------------------------------------------------
# Live verification (opt-in).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_OSM, reason="set TRID3NT_TEST_LIVE_OSM=1 to run live Overpass")
def test_live_sf_hospitals_inside_bbox():
    import geopandas as gpd  # noqa: PLC0415

    layer = fetch_overpass_pois(bbox=_SF_BBOX, amenity="hospital")
    assert layer.uri is not None
    gdf = gpd.read_file(layer.uri)
    assert len(gdf) >= 1
    for geom in gdf.geometry:
        assert _SF_BBOX[0] <= geom.x <= _SF_BBOX[2]
        assert _SF_BBOX[1] <= geom.y <= _SF_BBOX[3]
