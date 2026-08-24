"""Overpass-family fold parity (ADR 0070): roads + pois via the router.

Migrates the value-bearing coverage of the deleted twin tests
(test_fetch_roads_osm.py + test_fetch_overpass_pois.py) onto the spec-driven
surface: the PURE hooks (QL build, geometry/clip decode, tag resolution,
honest-empty), the http_json endpoint_fallback mirror chain (first-success /
4xx-short-circuit / all-fail), and the end-to-end LayerURI + cache-key stability.
Offline: synthetic Overpass JSON bodies + an in-memory read_through injector; the
real 3-mirror network path is proven by the live proof recorded in ADR 0070.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.executors import http_json
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers._router.hooks import RequestPlan, overpass
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path
from trid3nt_server.tools.fetchers._router.transport import (
    TransportNotFound,
    TransportUpstreamError,
)

_SPEC_BASE = (
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/tools/fetchers/socioeconomic"
)
ROADS_SPEC = load_spec_from_path(_SPEC_BASE / "fetch_roads_osm/source.yaml")
POIS_SPEC = load_spec_from_path(_SPEC_BASE / "fetch_overpass_pois/source.yaml")

_FORT_MYERS = (-82.0, 26.5, -81.8, 26.7)
_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _body(elements: list[dict[str, Any]]) -> bytes:
    return json.dumps({"version": 0.6, "elements": elements}).encode("utf-8")


def _way(osm_id: int, coords: list[tuple[float, float]], **tags: Any) -> dict[str, Any]:
    return {
        "type": "way",
        "id": osm_id,
        "geometry": [{"lat": lat, "lon": lon} for lon, lat in coords],
        "tags": tags or {},
    }


def _fgb_gdf(fgb: bytes):
    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb)
    try:
        return gpd.read_file(path, engine="pyogrio")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #


def test_both_promoted_as_router_specs():
    from trid3nt_server.tools import TOOL_REGISTRY

    for name, src in (("fetch_roads_osm", "osm_roads"), ("fetch_overpass_pois", "overpass_pois")):
        entry = TOOL_REGISTRY[name]
        assert entry.metadata.source_class == src
        assert entry.metadata.ttl_class == "static-30d"
        assert entry.metadata.cacheable is True
        assert entry.fn.__module__.endswith(f"_promoted.{name}")


# --------------------------------------------------------------------------- #
# Roads: QL build hook.
# --------------------------------------------------------------------------- #


def test_roads_ql_contains_bbox_and_classes():
    ql = overpass._build_roads_ql(_FORT_MYERS, ("motorway", "primary"))
    assert "(26.5,-82.0,26.7,-81.8)" in ql
    assert "^(motorway|primary)$" in ql
    assert "out geom;" in ql
    assert "[out:json][timeout:60]" in ql


def test_roads_ql_narrowed_to_motorway_only():
    ql = overpass._build_roads_ql(_FORT_MYERS, ("motorway",))
    assert "^(motorway)$" in ql
    assert "primary" not in ql


def test_roads_build_request_default_classes_sorted():
    plans = overpass.build_request_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["motorway", "motorway_link", "primary", "primary_link", "secondary", "tertiary", "trunk", "trunk_link"]})
    assert len(plans) == 3  # one POST plan per mirror
    assert all(p.method == "POST" and "data" in p.data for p in plans)
    # sorted alternation in the QL
    assert "^(motorway|motorway_link|primary|primary_link|secondary|tertiary|trunk|trunk_link)$" in plans[0].data["data"]


def test_roads_unknown_class_raises_input_error():
    with pytest.raises(RouterInputError) as ei:
        overpass.build_request_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["bogus_class"]})
    assert ei.value.error_code == "OSM_ROADS_INPUT_INVALID"
    assert ei.value.retryable is False


def test_roads_empty_classes_raises_input_error():
    with pytest.raises(RouterInputError):
        overpass.build_request_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": []})


# --------------------------------------------------------------------------- #
# Roads: parse + clip.
# --------------------------------------------------------------------------- #


def test_roads_parse_extracts_and_clips_spill():
    bodies = [_body([
        _way(1, [(-82.3, 26.6), (-81.9, 26.6)], name="W spill", highway="motorway"),
        _way(2, [(-81.95, 26.55), (-81.85, 26.65)], name="inside", highway="primary"),
        _way(3, [(-83.0, 26.6), (-82.5, 26.6)], name="gone", highway="primary"),
    ])]
    feats = overpass.parse_response_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, bodies)
    ids = sorted(f["properties"]["osm_id"] for f in feats)
    assert 3 not in ids and {1, 2} <= set(ids)
    for f in feats:
        for lon, lat in f["geometry"]["coordinates"]:
            assert -82.0 - 1e-9 <= lon <= -81.8 + 1e-9
            assert 26.5 - 1e-9 <= lat <= 26.7 + 1e-9


def test_roads_parse_zigzag_yields_multiple_segments():
    bodies = [_body([_way(
        4, [(-81.9, 26.65), (-82.2, 26.65), (-81.9, 26.60), (-82.2, 26.60), (-81.9, 26.55)],
        name="Zigzag", highway="trunk",
    )])]
    feats = overpass.parse_response_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, bodies)
    assert len(feats) >= 2
    assert all(f["properties"]["osm_id"] == 4 for f in feats)


def test_roads_parse_skips_single_point_and_non_way():
    bodies = [_body([
        _way(1, [(-81.95, 26.55), (-81.9, 26.6)], highway="motorway"),
        {"type": "node", "id": 2, "lat": 26.5, "lon": -82.0},
        _way(3, [(-81.9, 26.6)], highway="primary"),  # single point
    ])]
    feats = overpass.parse_response_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, bodies)
    assert [f["properties"]["osm_id"] for f in feats] == [1]


def test_roads_50_ways_serialize_to_50_features():
    ways = [
        _way(100 + i, [(-82.0 + 0.001 * i, 26.5 + 0.001 * i), (-82.0 + 0.001 * (i + 1), 26.5 + 0.001 * (i + 1))],
             name=f"Rd {i}", highway="primary")
        for i in range(50)
    ]
    feats = overpass.parse_response_roads(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, [_body(ways)])
    fgb = features_to_fgb_bytes(feats, ROADS_SPEC, {"bbox": list(_FORT_MYERS)})
    gdf = _fgb_gdf(fgb)
    assert len(gdf) == 50
    assert (gdf.geometry.geom_type == "LineString").all()
    for col in ("osm_id", "name", "highway", "lanes", "maxspeed"):
        assert col in gdf.columns


def test_roads_empty_yields_header_only_fgb():
    fgb = features_to_fgb_bytes([], ROADS_SPEC, {"bbox": list(_FORT_MYERS)})
    gdf = _fgb_gdf(fgb)
    assert len(gdf) == 0
    # honest-empty header carries the declared schema
    for col in ("osm_id", "name", "highway", "lanes", "maxspeed"):
        assert col in gdf.columns


# --------------------------------------------------------------------------- #
# POIs: tag resolution.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("params,expected", [
    ({"amenity": "hospital"}, ("amenity", "hospital")),
    ({"tag": "emergency=fire_hydrant"}, ("emergency", "fire_hydrant")),
    ({"tag": "hospital"}, ("amenity", "hospital")),         # bare value aliased
    ({"category": "shop=supermarket"}, ("shop", "supermarket")),
    ({"value": "school"}, ("amenity", "school")),
])
def test_pois_tag_resolution(params, expected):
    assert overpass._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", params) == expected


def test_pois_amenity_wins_priority():
    assert overpass._resolve_tag("OVERPASS_POIS", "INPUT_INVALID",
                                 {"amenity": "hospital", "tag": "shop=supermarket"}) == ("amenity", "hospital")


def test_pois_no_selector_raises_input_error():
    with pytest.raises(RouterInputError) as ei:
        overpass._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", {})
    assert ei.value.error_code == "OVERPASS_POIS_INPUT_INVALID"
    assert ei.value.retryable is False


def test_pois_unmappable_bare_value_raises():
    with pytest.raises(RouterInputError):
        overpass._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", {"value": "unknownthing"})


def test_pois_dirty_token_rejected():
    with pytest.raises(RouterInputError):
        overpass._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", {"tag": "amenity=hos pital"})


def test_pois_ql_queries_all_element_types():
    ql = overpass._build_pois_ql(_FORT_MYERS, "amenity", "hospital")
    for et in ("node", "way", "relation"):
        assert f'{et}["amenity"="hospital"]' in ql
    assert "out center;" in ql


# --------------------------------------------------------------------------- #
# POIs: parse + honest-empty.
# --------------------------------------------------------------------------- #


def test_pois_parse_node_and_center_inside_bbox():
    bodies = [_body([
        {"type": "node", "id": 1, "lat": 26.6, "lon": -81.9, "tags": {"amenity": "hospital", "name": "H1"}},
        {"type": "way", "id": 2, "center": {"lat": 26.62, "lon": -81.88}, "tags": {"amenity": "hospital"}},
        {"type": "node", "id": 3, "lat": 27.9, "lon": -81.9, "tags": {"amenity": "hospital"}},  # outside bbox
    ])]
    feats = overpass.parse_response_pois(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"}, bodies)
    ids = sorted(f["properties"]["osm_id"] for f in feats)
    assert ids == [1, 2]
    assert all(f["properties"]["key"] == "amenity" and f["properties"]["value"] == "hospital" for f in feats)
    assert all(f["geometry"]["type"] == "Point" for f in feats)


def test_pois_zero_features_raises_no_features():
    with pytest.raises(RouterEmptyError) as ei:
        overpass.parse_response_pois(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"}, [_body([])])
    assert ei.value.error_code == "OVERPASS_POIS_NO_FEATURES"
    assert ei.value.retryable is False


def test_pois_serialize_carries_props():
    bodies = [_body([
        {"type": "node", "id": 1, "lat": 26.6, "lon": -81.9, "tags": {"amenity": "hospital", "name": "H1"}},
    ])]
    feats = overpass.parse_response_pois(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"}, bodies)
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, POIS_SPEC, {"bbox": list(_FORT_MYERS)}))
    assert len(gdf) == 1
    for col in ("osm_id", "osm_type", "name", "key", "value", "tags_json"):
        assert col in gdf.columns


# --------------------------------------------------------------------------- #
# Mirror endpoint_fallback chain (the data-source fallback norm).
# --------------------------------------------------------------------------- #


def _plans(n: int = 3):
    return [RequestPlan(url=f"https://m{i}/api", method="POST", data={"data": "ql"}) for i in range(n)]


def test_fallback_first_success_wins(monkeypatch):
    calls = []

    def fake_get_raw(plan):
        calls.append(plan.url)
        if plan.url == "https://m0/api":
            raise TransportUpstreamError("504", status=504)
        return b'{"elements": []}'

    monkeypatch.setattr(http_json, "_get_raw", fake_get_raw)
    bodies = http_json._fetch_endpoint_fallback(ROADS_SPEC, _plans())
    assert bodies == [b'{"elements": []}']
    assert calls == ["https://m0/api", "https://m1/api"]  # stopped at first success


def test_fallback_all_fail_raises_upstream(monkeypatch):
    monkeypatch.setattr(http_json, "_get_raw",
                        lambda plan: (_ for _ in ()).throw(TransportUpstreamError("504", status=504)))
    with pytest.raises(RouterUpstreamError) as ei:
        http_json._fetch_endpoint_fallback(ROADS_SPEC, _plans())
    assert ei.value.error_code == "OSM_ROADS_UPSTREAM_ERROR"
    assert ei.value.retryable is True


def test_fallback_4xx_short_circuits(monkeypatch):
    calls = []

    def fake_get_raw(plan):
        calls.append(plan.url)
        raise TransportNotFound("404", status=404)

    monkeypatch.setattr(http_json, "_get_raw", fake_get_raw)
    with pytest.raises(RouterUpstreamError):
        http_json._fetch_endpoint_fallback(ROADS_SPEC, _plans())
    assert calls == ["https://m0/api"]  # 4xx did NOT try the other mirrors


# --------------------------------------------------------------------------- #
# End-to-end router.route: LayerURI shape + cache-key stability.
# --------------------------------------------------------------------------- #


def _inject_read_through(monkeypatch, store: dict[str, bytes]):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path, compute_cache_key, is_cacheable,
    )

    def patched(metadata, params, ext, fetch_fn, **kw):
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        source_id = metadata.source_class or metadata.name
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{CACHE_BUCKET}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(router, "read_through", patched)


def test_roads_end_to_end_layer_uri(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    body = _body([_way(1, [(-81.95, 26.55), (-81.9, 26.6)], name="I-75", highway="motorway")])
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: body)

    layer = router.route(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["motorway"]})
    assert layer.layer_type == "vector"
    assert layer.role == "context"
    assert layer.units is None
    assert layer.style_preset == "osm_roads"
    assert "osm_roads" in layer.uri


def test_roads_cache_key_independent_of_class_ordering(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    calls = {"n": 0}
    body = _body([_way(1, [(-81.95, 26.55), (-81.9, 26.6)], highway="motorway")])

    def fake(plan):
        calls["n"] += 1
        return body

    monkeypatch.setattr(http_json, "_get_raw", fake)
    r1 = router.route(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["motorway", "primary"]})
    r2 = router.route(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["primary", "motorway"]})
    assert r1.uri == r2.uri
    assert calls["n"] == 1  # second call was a cache hit (sorted class key)


def test_pois_end_to_end_bbox_from_features(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    body = _body([
        {"type": "node", "id": 1, "lat": 26.6, "lon": -81.9, "tags": {"amenity": "hospital"}},
    ])
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: body)
    layer = router.route(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"})
    assert layer.layer_type == "vector"
    assert layer.role == "primary"
    assert layer.style_preset == "overpass_pois"
    # single-point extent padded by 0.02 (bbox_from_features)
    assert layer.bbox is not None
    w, s, e, n = layer.bbox
    assert e - w == pytest.approx(0.04, abs=1e-6)
    assert n - s == pytest.approx(0.04, abs=1e-6)


def test_pois_no_features_propagates(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: _body([]))
    with pytest.raises(RouterEmptyError) as ei:
        router.route(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"})
    assert ei.value.error_code == "OVERPASS_POIS_NO_FEATURES"
