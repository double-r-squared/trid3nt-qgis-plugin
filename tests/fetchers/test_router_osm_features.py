"""``fetch_osm_features``: one Overpass interpreter, four feature classes.

Each class owns its own tag vocabulary, clip-or-whole rule and property shape,
which is what these OFFLINE tests exercise against synthetic frames shaped
exactly as the library returns them. The mirror chain and the silent-error hook
run against a stand-in for its request seam."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers.socioeconomic.fetch_osm_features import hooks as osm
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[2]
    / "trid3nt_server/tools/fetchers/socioeconomic/fetch_osm_features/source.yaml"
)

_FORT_MYERS = (-82.0, 26.5, -81.8, 26.7)


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(SPEC, raw)


def _frame(rows: list[tuple[str, int, object, dict]]) -> gpd.GeoDataFrame:
    """A frame shaped as the library returns one: a (element, id) index plus tags."""
    index = pd.MultiIndex.from_tuples(
        [(et, oid) for et, oid, _g, _t in rows], names=["element", "id"])
    cols: dict[str, list] = {}
    for _et, _oid, _g, tags in rows:
        for k in tags:
            cols.setdefault(k, [])
    for k in cols:
        cols[k] = [tags.get(k) for _et, _oid, _g, tags in rows]
    return gpd.GeoDataFrame(
        cols, geometry=[g for _et, _oid, g, _t in rows], index=index, crs="EPSG:4326")


def _fgb_gdf(fgb: bytes):
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


def _serve(monkeypatch, frame):
    monkeypatch.setattr(osm, "overpass_features",
                        lambda spec, params, tags, *, timeout_s: frame)


def test_promoted_as_router_spec():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_osm_features"]
    assert entry.metadata.source_class == "osm_features"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.cacheable is True
    assert entry.fn.__module__.endswith("_promoted.fetch_osm_features")


# --- roads ------------------------------------------------------------------


def test_roads_default_classes_are_the_major_tier_sorted():
    assert osm._resolve_road_classes("OSM_FEATURES", "INPUT_INVALID", None) == sorted(
        osm._DEFAULT_ROAD_CLASSES)


def test_roads_unknown_class_raises_input_error():
    with pytest.raises(RouterInputError) as ei:
        osm.validate(SPEC, {"feature": "roads", "road_classes": ["bogus_class"]})
    assert ei.value.error_code == "OSM_FEATURES_INPUT_INVALID"
    assert ei.value.retryable is False


def test_roads_empty_classes_raises_input_error():
    with pytest.raises(RouterInputError):
        osm.validate(SPEC, {"feature": "roads", "road_classes": []})


def test_roads_clip_keeps_only_the_in_aoi_run(monkeypatch):
    _serve(monkeypatch, _frame([
        ("way", 1, LineString([(-82.3, 26.6), (-81.9, 26.6)]),
         {"name": "W spill", "highway": "motorway"}),
        ("way", 2, LineString([(-81.95, 26.55), (-81.85, 26.65)]),
         {"name": "inside", "highway": "primary"}),
        ("way", 3, LineString([(-83.0, 26.6), (-82.5, 26.6)]),
         {"name": "gone", "highway": "primary"}),
    ]))
    feats = osm.delegate(SPEC, {"feature": "roads", "bbox": list(_FORT_MYERS)}, timeout_s=1)
    ids = sorted(f["properties"]["osm_id"] for f in feats)
    assert 3 not in ids and {1, 2} <= set(ids)
    for f in feats:
        for lon, lat in f["geometry"]["coordinates"]:
            assert -82.0 - 1e-9 <= lon <= -81.8 + 1e-9
            assert 26.5 - 1e-9 <= lat <= 26.7 + 1e-9


def test_roads_serializes_with_the_declared_columns(monkeypatch):
    _serve(monkeypatch, _frame([
        ("way", 100 + i,
         LineString([(-82.0 + 0.001 * i, 26.5 + 0.001 * i),
                     (-82.0 + 0.001 * (i + 1), 26.5 + 0.001 * (i + 1))]),
         {"name": f"Rd {i}", "highway": "primary"})
        for i in range(50)
    ]))
    params = {"feature": "roads", "bbox": list(_FORT_MYERS)}
    feats = osm.delegate(SPEC, params, timeout_s=1)
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, SPEC, params))
    assert len(gdf) == 50
    assert (gdf.geometry.geom_type == "LineString").all()
    for col in ("osm_id", "name", "highway", "lanes", "maxspeed"):
        assert col in gdf.columns


def test_roads_empty_yields_header_only_fgb():
    params = {"feature": "roads", "bbox": list(_FORT_MYERS)}
    gdf = _fgb_gdf(features_to_fgb_bytes([], SPEC, params))
    assert len(gdf) == 0
    for col in ("osm_id", "name", "highway", "lanes", "maxspeed"):
        assert col in gdf.columns


# --- pois ---------------------------------------------------------------


@pytest.mark.parametrize("params,expected", [
    ({"amenity": "hospital"}, ("amenity", "hospital")),
    ({"tag": "emergency=fire_hydrant"}, ("emergency", "fire_hydrant")),
    ({"tag": "hospital"}, ("amenity", "hospital")),         # bare value aliased
    ({"category": "shop=supermarket"}, ("shop", "supermarket")),
    ({"value": "school"}, ("amenity", "school")),
])
def test_pois_tag_resolution(params, expected):
    assert osm._resolve_tag("OSM_FEATURES", "INPUT_INVALID", params) == expected


def test_pois_amenity_wins_priority():
    assert osm._resolve_tag("OSM_FEATURES", "INPUT_INVALID",
                            {"amenity": "hospital", "tag": "shop=supermarket"}) == (
        "amenity", "hospital")


def test_pois_no_selector_raises_input_error():
    with pytest.raises(RouterInputError) as ei:
        osm.validate(SPEC, {"feature": "pois"})
    assert ei.value.error_code == "OSM_FEATURES_INPUT_INVALID"
    assert ei.value.retryable is False


def test_pois_unmappable_bare_value_raises():
    with pytest.raises(RouterInputError):
        osm._resolve_tag("OSM_FEATURES", "INPUT_INVALID", {"value": "unknownthing"})


def test_pois_dirty_token_rejected():
    with pytest.raises(RouterInputError):
        osm._resolve_tag("OSM_FEATURES", "INPUT_INVALID", {"tag": "amenity=hos pital"})


def test_pois_a_node_is_its_own_point_and_an_area_is_its_bbox_centre(monkeypatch):
    _serve(monkeypatch, _frame([
        ("node", 1, Point(-81.9, 26.6), {"amenity": "hospital", "name": "H1"}),
        ("way", 2, Polygon([(-81.90, 26.60), (-81.86, 26.60), (-81.86, 26.64),
                            (-81.90, 26.64)]), {"amenity": "hospital"}),
        ("node", 3, Point(-81.9, 27.9), {"amenity": "hospital"}),   # outside the bbox
    ]))
    feats = osm.delegate(
        SPEC, {"feature": "pois", "bbox": list(_FORT_MYERS), "amenity": "hospital"},
        timeout_s=1)
    assert sorted(f["properties"]["osm_id"] for f in feats) == [1, 2]
    area = next(f for f in feats if f["properties"]["osm_id"] == 2)
    assert area["geometry"]["coordinates"] == pytest.approx([-81.88, 26.62])
    assert all(f["geometry"]["type"] == "Point" for f in feats)
    assert all(f["properties"]["key"] == "amenity" for f in feats)


def test_pois_carries_the_element_type_and_the_whole_tag_bag(monkeypatch):
    _serve(monkeypatch, _frame([
        ("node", 1, Point(-81.9, 26.6),
         {"amenity": "hospital", "name": "H1", "emergency": "yes"}),
    ]))
    params = {"feature": "pois", "bbox": list(_FORT_MYERS), "amenity": "hospital"}
    feats = osm.delegate(SPEC, params, timeout_s=1)
    props = feats[0]["properties"]
    assert props["osm_type"] == "node"
    assert props["tags_json"] == (
        '{"amenity":"hospital","emergency":"yes","name":"H1"}')
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, SPEC, params))
    for col in ("osm_id", "osm_type", "name", "key", "value", "tags_json"):
        assert col in gdf.columns


def test_pois_zero_features_raises_no_features(monkeypatch):
    _serve(monkeypatch, gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))
    with pytest.raises(RouterEmptyError) as ei:
        osm.delegate(
            SPEC, {"feature": "pois", "bbox": list(_FORT_MYERS), "amenity": "hospital"},
            timeout_s=1)
    assert ei.value.error_code == "OSM_FEATURES_NO_FEATURES"
    assert ei.value.retryable is False


# --- coastline ------------------------------------------------------------


def test_coastline_keeps_ways_whole_and_unclipped(monkeypatch):
    _serve(monkeypatch, _frame([
        ("way", 5, LineString([(-83.0, 26.6), (-82.9, 26.65), (-82.7, 26.7)]),
         {"natural": "coastline", "name": "Gulf edge"}),
    ]))
    feats = osm.delegate(SPEC, {"feature": "coastline", "bbox": list(_FORT_MYERS)},
                         timeout_s=1)
    assert len(feats) == 1
    assert feats[0]["geometry"]["coordinates"] == [
        [-83.0, 26.6], [-82.9, 26.65], [-82.7, 26.7]]


def test_coastline_empty_is_not_an_error(monkeypatch):
    _serve(monkeypatch, gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))
    feats = osm.delegate(SPEC, {"feature": "coastline", "bbox": list(_FORT_MYERS)},
                         timeout_s=1)
    assert feats == []


# --- breakwaters -----------------------------------------------------------


def test_breakwaters_unknown_structure_type_raises():
    with pytest.raises(RouterInputError):
        osm.validate(SPEC, {"feature": "breakwaters", "structure_type": "pier"})


def test_breakwaters_kept_whole_not_clipped(monkeypatch):
    _serve(monkeypatch, _frame([
        ("way", 9, LineString([(-83.0, 26.6), (-82.5, 26.6)]), {"man_made": "breakwater"}),
    ]))
    feats = osm.delegate(SPEC, {"feature": "breakwaters", "bbox": list(_FORT_MYERS)},
                         timeout_s=1)
    assert len(feats) == 1
    assert feats[0]["geometry"]["coordinates"] == [[-83.0, 26.6], [-82.5, 26.6]]


# --- end to end -------------------------------------------------------------


def _inject_read_through(monkeypatch, store: dict[str, bytes]):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path, compute_cache_key, is_cacheable,
    )

    def patched(metadata, params, ext, fetch_fn, **kw):
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        source_id = metadata.source_class or metadata.name
        key = compute_cache_key(source_id, params, metadata.ttl_class)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{CACHE_BUCKET}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(router, "read_through", patched)


def test_roads_end_to_end_layer_uri(monkeypatch):
    _inject_read_through(monkeypatch, {})
    _serve(monkeypatch, _frame([
        ("way", 1, LineString([(-81.95, 26.55), (-81.9, 26.6)]),
         {"name": "I-75", "highway": "motorway"}),
    ]))
    layer = router.route(SPEC, {"feature": "roads", "bbox": list(_FORT_MYERS),
                                "road_classes": ["motorway"]})
    assert layer.layer_type == "vector"
    assert layer.role == "context"
    assert layer.style["kind"] == "reference"
    assert "osm_features" in layer.uri
