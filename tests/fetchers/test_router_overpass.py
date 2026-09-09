"""Router value coverage for the OSM family, read through OSMnx.

The library owns the query, the socket and the element-to-geometry decode. What
each row owns is its tag vocabulary and its projection - clip or keep whole, a
point or a line, which columns - and that is what these OFFLINE tests exercise,
against synthetic frames shaped exactly as the library returns them. The mirror
chain and the silent-error hook are driven against a stand-in for the library's
own request seam.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers._router.hooks import osm as osm_hooks
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path
from trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois import hooks as pois
from trid3nt_server.tools.fetchers.socioeconomic.fetch_roads_osm import hooks as roads

_SPEC_BASE = (
    Path(__file__).resolve().parents[2]
    / "trid3nt_server/tools/fetchers/socioeconomic"
)
ROADS_SPEC = load_spec_from_path(_SPEC_BASE / "fetch_roads_osm/source.yaml")
POIS_SPEC = load_spec_from_path(_SPEC_BASE / "fetch_overpass_pois/source.yaml")

_FORT_MYERS = (-82.0, 26.5, -81.8, 26.7)
_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


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


def _serve(monkeypatch, module, frame):
    monkeypatch.setattr(module, "overpass_features",
                        lambda spec, params, tags, *, timeout_s: frame)


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
# Roads: the highway vocabulary.
# --------------------------------------------------------------------------- #


def test_roads_default_classes_are_the_major_tier_sorted():
    assert roads._resolve_road_classes("OSM_ROADS", "INPUT_INVALID", None) == sorted(
        roads._DEFAULT_ROAD_CLASSES)


def test_roads_unknown_class_raises_input_error():
    with pytest.raises(RouterInputError) as ei:
        roads.validate(ROADS_SPEC, {"road_classes": ["bogus_class"]})
    assert ei.value.error_code == "OSM_ROADS_INPUT_INVALID"
    assert ei.value.retryable is False


def test_roads_empty_classes_raises_input_error():
    with pytest.raises(RouterInputError):
        roads.validate(ROADS_SPEC, {"road_classes": []})


def test_roads_class_set_is_sorted_and_deduped():
    assert roads._resolve_road_classes(
        "OSM_ROADS", "INPUT_INVALID", ["primary", "motorway", "primary"]) == [
        "motorway", "primary"]


# --------------------------------------------------------------------------- #
# Roads: the clip, which is what makes a network measured INSIDE an area.
# --------------------------------------------------------------------------- #


def test_roads_clip_keeps_only_the_in_aoi_run(monkeypatch):
    _serve(monkeypatch, roads, _frame([
        ("way", 1, LineString([(-82.3, 26.6), (-81.9, 26.6)]),
         {"name": "W spill", "highway": "motorway"}),
        ("way", 2, LineString([(-81.95, 26.55), (-81.85, 26.65)]),
         {"name": "inside", "highway": "primary"}),
        ("way", 3, LineString([(-83.0, 26.6), (-82.5, 26.6)]),
         {"name": "gone", "highway": "primary"}),
    ]))
    feats = roads.delegate(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, timeout_s=1)
    ids = sorted(f["properties"]["osm_id"] for f in feats)
    assert 3 not in ids and {1, 2} <= set(ids)
    for f in feats:
        for lon, lat in f["geometry"]["coordinates"]:
            assert -82.0 - 1e-9 <= lon <= -81.8 + 1e-9
            assert 26.5 - 1e-9 <= lat <= 26.7 + 1e-9


def test_roads_a_way_crossing_the_edge_twice_is_two_segments(monkeypatch):
    _serve(monkeypatch, roads, _frame([
        ("way", 4, LineString([(-81.9, 26.65), (-82.2, 26.65), (-81.9, 26.60),
                               (-82.2, 26.60), (-81.9, 26.55)]),
         {"name": "Zigzag", "highway": "trunk"}),
    ]))
    feats = roads.delegate(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, timeout_s=1)
    assert len(feats) >= 2
    assert all(f["properties"]["osm_id"] == 4 for f in feats)
    assert all(f["properties"]["name"] == "Zigzag" for f in feats)


def test_roads_serializes_with_the_declared_columns(monkeypatch):
    _serve(monkeypatch, roads, _frame([
        ("way", 100 + i,
         LineString([(-82.0 + 0.001 * i, 26.5 + 0.001 * i),
                     (-82.0 + 0.001 * (i + 1), 26.5 + 0.001 * (i + 1))]),
         {"name": f"Rd {i}", "highway": "primary"})
        for i in range(50)
    ]))
    feats = roads.delegate(ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, timeout_s=1)
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, ROADS_SPEC, {"bbox": list(_FORT_MYERS)}))
    assert len(gdf) == 50
    assert (gdf.geometry.geom_type == "LineString").all()
    for col in ("osm_id", "name", "highway", "lanes", "maxspeed"):
        assert col in gdf.columns


def test_roads_empty_yields_header_only_fgb():
    gdf = _fgb_gdf(features_to_fgb_bytes([], ROADS_SPEC, {"bbox": list(_FORT_MYERS)}))
    assert len(gdf) == 0
    for col in ("osm_id", "name", "highway", "lanes", "maxspeed"):
        assert col in gdf.columns


# --------------------------------------------------------------------------- #
# POIs: the five ways of naming one tag.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("params,expected", [
    ({"amenity": "hospital"}, ("amenity", "hospital")),
    ({"tag": "emergency=fire_hydrant"}, ("emergency", "fire_hydrant")),
    ({"tag": "hospital"}, ("amenity", "hospital")),         # bare value aliased
    ({"category": "shop=supermarket"}, ("shop", "supermarket")),
    ({"value": "school"}, ("amenity", "school")),
])
def test_pois_tag_resolution(params, expected):
    assert pois._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", params) == expected


def test_pois_amenity_wins_priority():
    assert pois._resolve_tag("OVERPASS_POIS", "INPUT_INVALID",
                             {"amenity": "hospital", "tag": "shop=supermarket"}) == (
        "amenity", "hospital")


def test_pois_no_selector_raises_input_error():
    with pytest.raises(RouterInputError) as ei:
        pois.validate(POIS_SPEC, {})
    assert ei.value.error_code == "OVERPASS_POIS_INPUT_INVALID"
    assert ei.value.retryable is False


def test_pois_unmappable_bare_value_raises():
    with pytest.raises(RouterInputError):
        pois._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", {"value": "unknownthing"})


def test_pois_dirty_token_rejected():
    with pytest.raises(RouterInputError):
        pois._resolve_tag("OVERPASS_POIS", "INPUT_INVALID", {"tag": "amenity=hos pital"})


# --------------------------------------------------------------------------- #
# POIs: one representative point per element, strictly inside the bbox.
# --------------------------------------------------------------------------- #


def test_pois_a_node_is_its_own_point_and_an_area_is_its_bbox_centre(monkeypatch):
    _serve(monkeypatch, pois, _frame([
        ("node", 1, Point(-81.9, 26.6), {"amenity": "hospital", "name": "H1"}),
        ("way", 2, Polygon([(-81.90, 26.60), (-81.86, 26.60), (-81.86, 26.64),
                            (-81.90, 26.64)]), {"amenity": "hospital"}),
        ("node", 3, Point(-81.9, 27.9), {"amenity": "hospital"}),   # outside the bbox
    ]))
    feats = pois.delegate(
        POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"}, timeout_s=1)
    assert sorted(f["properties"]["osm_id"] for f in feats) == [1, 2]
    area = next(f for f in feats if f["properties"]["osm_id"] == 2)
    assert area["geometry"]["coordinates"] == pytest.approx([-81.88, 26.62])
    assert all(f["geometry"]["type"] == "Point" for f in feats)
    assert all(f["properties"]["key"] == "amenity" for f in feats)


def test_pois_carries_the_element_type_and_the_whole_tag_bag(monkeypatch):
    _serve(monkeypatch, pois, _frame([
        ("node", 1, Point(-81.9, 26.6),
         {"amenity": "hospital", "name": "H1", "emergency": "yes"}),
    ]))
    feats = pois.delegate(
        POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"}, timeout_s=1)
    props = feats[0]["properties"]
    assert props["osm_type"] == "node"
    assert props["tags_json"] == (
        '{"amenity":"hospital","emergency":"yes","name":"H1"}')
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, POIS_SPEC, {"bbox": list(_FORT_MYERS)}))
    for col in ("osm_id", "osm_type", "name", "key", "value", "tags_json"):
        assert col in gdf.columns


def test_pois_zero_features_raises_no_features(monkeypatch):
    _serve(monkeypatch, pois, gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))
    with pytest.raises(RouterEmptyError) as ei:
        pois.delegate(
            POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"}, timeout_s=1)
    assert ei.value.error_code == "OVERPASS_POIS_NO_FEATURES"
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# The mirror chain and the error the library returns as data.
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.ok = status_code < 400


def test_the_row_names_the_interpreter_and_the_library_gets_the_base(monkeypatch):
    """The library appends /interpreter itself, and its timeout is also the QL's
    own [timeout:N] directive, which Overpass parses as an integer."""
    seen: list[tuple[str, object]] = []

    def fake(bbox, tags):
        from osmnx import settings

        seen.append((settings.overpass_url, settings.requests_timeout))
        return gpd.GeoDataFrame(geometry=[Point(-81.9, 26.6)], crs="EPSG:4326")

    import osmnx as ox

    monkeypatch.setattr(ox, "features_from_bbox", fake)
    osm_hooks.overpass_features(
        ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, {"highway": "primary"}, timeout_s=180.0)
    url, timeout = seen[0]
    assert url == "https://overpass-api.de/api"
    assert isinstance(timeout, int) and timeout == 180


def test_a_failed_mirror_is_followed_by_the_next(monkeypatch):
    seen: list[str] = []

    def fake(bbox, tags):
        from osmnx import settings

        seen.append(settings.overpass_url)
        if len(seen) == 1:
            raise RuntimeError("504")
        return gpd.GeoDataFrame(geometry=[Point(-81.9, 26.6)], crs="EPSG:4326")

    import osmnx as ox

    monkeypatch.setattr(ox, "features_from_bbox", fake)
    gdf = osm_hooks.overpass_features(
        ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, {"highway": "primary"}, timeout_s=1)
    assert len(gdf) == 1
    assert len(seen) == 2 and seen[0] != seen[1]


def test_every_mirror_failing_is_a_typed_upstream_error(monkeypatch):
    import osmnx as ox

    monkeypatch.setattr(
        ox, "features_from_bbox",
        lambda bbox, tags: (_ for _ in ()).throw(RuntimeError("504 from the mirror")))
    with pytest.raises(RouterUpstreamError) as ei:
        osm_hooks.overpass_features(
            ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, {"highway": "primary"}, timeout_s=1)
    assert ei.value.error_code == "OSM_ROADS_UPSTREAM_ERROR"
    assert ei.value.retryable is True
    assert "504 from the mirror" in str(ei.value)


def test_no_element_matched_is_an_empty_frame_not_a_failure(monkeypatch):
    import osmnx as ox

    monkeypatch.setattr(
        ox, "features_from_bbox",
        lambda bbox, tags: (_ for _ in ()).throw(
            ox._errors.InsufficientResponseError("no elements")))
    gdf = osm_hooks.overpass_features(
        ROADS_SPEC, {"bbox": list(_FORT_MYERS)}, {"highway": "primary"}, timeout_s=1)
    assert len(gdf) == 0


def test_a_non_ok_response_with_a_json_body_is_raised_not_returned():
    """The library's own parse raises only on a body it cannot read as JSON, so a
    JSON error envelope would reach the router as a zero-element answer."""

    class _Real:
        @staticmethod
        def post(url, *a, **k):
            return _Resp(400, '{"remark": "query timed out"}')

    wrapped = osm_hooks._RaiseOnStatus(_Real())
    with pytest.raises(osm_hooks._OverpassStatus) as ei:
        wrapped.post("https://mirror.test/api")
    assert "query timed out" in str(ei.value)
    assert "400" in str(ei.value)


def test_the_statuses_the_library_retries_itself_pass_through():
    class _Real:
        @staticmethod
        def post(url, *a, **k):
            return _Resp(429, "slow down")

    assert osm_hooks._RaiseOnStatus(_Real()).post("https://mirror.test/api").status_code == 429


def test_a_mirror_that_only_ever_throttles_stops_being_retried():
    """The library retries a 429 by recursion with no ceiling, so the ceiling is here."""

    class _Real:
        @staticmethod
        def post(url, *a, **k):
            return _Resp(429, "slow down")

    wrapped = osm_hooks._RaiseOnStatus(_Real())
    for _ in range(osm_hooks._MAX_THROTTLED):
        assert wrapped.post("https://mirror.test/api").status_code == 429
    with pytest.raises(osm_hooks._OverpassStatus):
        wrapped.post("https://mirror.test/api")


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
    _inject_read_through(monkeypatch, {})
    _serve(monkeypatch, roads, _frame([
        ("way", 1, LineString([(-81.95, 26.55), (-81.9, 26.6)]),
         {"name": "I-75", "highway": "motorway"}),
    ]))
    layer = router.route(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["motorway"]})
    assert layer.layer_type == "vector"
    assert layer.role == "context"
    assert layer.units is None
    assert layer.style["kind"] == "reference"
    assert "osm_roads" in layer.uri


def test_roads_cache_key_independent_of_class_ordering(monkeypatch):
    _inject_read_through(monkeypatch, {})
    calls = {"n": 0}

    def counting(spec, params, tags, *, timeout_s):
        calls["n"] += 1
        return _frame([("way", 1, LineString([(-81.95, 26.55), (-81.9, 26.6)]),
                        {"highway": "motorway"})])

    monkeypatch.setattr(roads, "overpass_features", counting)
    r1 = router.route(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["motorway", "primary"]})
    r2 = router.route(ROADS_SPEC, {"bbox": list(_FORT_MYERS), "road_classes": ["primary", "motorway"]})
    assert r1.uri == r2.uri
    assert calls["n"] == 1  # the second call was a cache hit (sorted class key)


def test_pois_end_to_end_bbox_from_features(monkeypatch):
    _inject_read_through(monkeypatch, {})
    _serve(monkeypatch, pois, _frame([
        ("node", 1, Point(-81.9, 26.6), {"amenity": "hospital"}),
    ]))
    layer = router.route(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"})
    assert layer.layer_type == "vector"
    assert layer.role == "primary"
    assert layer.style["kind"] == "reference"
    # single-point extent padded by 0.02 (bbox_from_features)
    w, s, e, n = layer.bbox
    assert e - w == pytest.approx(0.04, abs=1e-6)
    assert n - s == pytest.approx(0.04, abs=1e-6)


def test_pois_no_features_propagates(monkeypatch):
    _inject_read_through(monkeypatch, {})
    _serve(monkeypatch, pois, gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))
    with pytest.raises(RouterEmptyError) as ei:
        router.route(POIS_SPEC, {"bbox": list(_FORT_MYERS), "amenity": "hospital"})
    assert ei.value.error_code == "OVERPASS_POIS_NO_FEATURES"


def test_every_pois_corpus_phrasing_surfaces_the_row_model_free():
    """The row's phrasings are the questions IT answers, not a neighbour's.

    A curated US critical-infrastructure category is ``fetch_hifld_*``'s question
    and retrieves there; what only this row answers is an arbitrary OSM tag, and
    the same class of feature anywhere on earth.
    """
    import yaml

    import trid3nt_server.main as main
    from trid3nt_server.tools.search.search_tools import search_tools as st
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    main._import_tools_registry()
    st._get_index()
    corpus = yaml.safe_load(
        (Path(pois.__file__).resolve().parent / "corpus.yaml").read_text()
    )["fetch_overpass_pois"]
    missed = [q for q in corpus
              if "fetch_overpass_pois" not in retrieve_visible_tools(q, None, 8)]
    assert not missed, f"phrasings that do not surface the row: {missed}"
