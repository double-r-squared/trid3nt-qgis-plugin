"""``fetch_river_geometry``: the OSM waterway network in an AOI.

The spec-driven surface: the PURE hooks - class-vocabulary resolution, the tag the
library is asked for, the AOI clip and the honest empty - the area guardrail, and
the end-to-end layer with a stable cache key. Offline, over frames shaped as the
library returns them plus an in-memory cache injector."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry import hooks as river
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

RIVER_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[2]
    / "trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/source.yaml"
)

# Kansas -- outside every old v0.1 HUC4 envelope (the exact case that used to
# dead-end before OSM became the primary; now the only path).
_KANSAS = (-97.4, 37.6, -97.2, 37.8)
_FORT_MYERS = (-81.92, 26.55, -81.80, 26.68)
_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _frame(rows: list[tuple[int, list[tuple[float, float]], dict]]) -> gpd.GeoDataFrame:
    """A frame shaped as the library returns one: a (element, id) index plus tags."""
    index = pd.MultiIndex.from_tuples(
        [("way", oid) for oid, _c, _t in rows], names=["element", "id"])
    keys: list[str] = []
    for _oid, _c, tags in rows:
        keys.extend(k for k in tags if k not in keys)
    cols = {k: [tags.get(k) for _oid, _c, tags in rows] for k in keys}
    return gpd.GeoDataFrame(
        cols, geometry=[LineString(c) for _oid, c, _t in rows], index=index,
        crs="EPSG:4326")


def _serve(monkeypatch, rows):
    monkeypatch.setattr(river, "overpass_features",
                        lambda spec, params, tags, *, timeout_s: _frame(rows))


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


def _validated(**raw: Any) -> dict[str, Any]:
    return router.validate_params(RIVER_SPEC, raw)


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #


def test_river_promoted_as_router_spec():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_river_geometry"]
    assert entry.metadata.source_class == "river_geometry"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.cacheable is True
    assert entry.fn.__module__.endswith("_promoted.fetch_river_geometry")


def test_river_docstring_describes_osm_primary_and_dropped_leg():
    from trid3nt_server.tools import TOOL_REGISTRY

    doc = TOOL_REGISTRY["fetch_river_geometry"].fn.__doc__ or ""
    assert "Overpass" in doc
    assert "NHDPlus HR HUC4 region-download fallback leg was removed" in doc


# --------------------------------------------------------------------------- #
# Waterway class-vocabulary resolution (the PURE hook).
# --------------------------------------------------------------------------- #


def test_resolve_waterway_classes_default_and_aliases():
    r = river._resolve_waterway_classes
    assert r("P", "S", None) == ["river", "stream", "canal"]
    assert r("P", "S", "") == ["river", "stream", "canal"]
    assert r("P", "S", "   ") == ["river", "stream", "canal"]
    assert r("P", "S", "all") == ["river", "stream", "canal", "ditch", "drain"]
    assert r("P", "S", "drainage") == ["ditch", "drain"]
    assert r("P", "S", "ditches") == ["ditch", "drain"]
    assert r("P", "S", "  Ditch ") == ["ditch"]
    assert r("P", "S", "ditch,drain") == ["ditch", "drain"]
    assert r("P", "S", "river+ditch") == ["river", "ditch"]
    assert r("P", "S", ["ditch", "drain", "ditch"]) == ["ditch", "drain"]


def test_resolve_waterway_classes_rejects_unknown_tokens():
    r = river._resolve_waterway_classes
    for bad in ("sewer", "river,sewer"):
        with pytest.raises(RouterInputError):
            r("RIVER_GEOMETRY", "INPUT_INVALID", bad)
    with pytest.raises(RouterInputError):
        r("RIVER_GEOMETRY", "INPUT_INVALID", ["ditch", 5])  # type: ignore[list-item]
    with pytest.raises(RouterInputError):
        r("RIVER_GEOMETRY", "INPUT_INVALID", 42)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The tag the library is asked for, and the source label kept for back-compat.
# --------------------------------------------------------------------------- #


def test_river_asks_the_library_for_the_resolved_waterway_set(monkeypatch):
    asked: list[dict] = []

    def spy(spec, params, tags, *, timeout_s):
        asked.append(tags)
        return _frame([])

    monkeypatch.setattr(river, "overpass_features", spy)
    river.delegate(RIVER_SPEC, _validated(bbox=list(_KANSAS)), timeout_s=1)
    river.delegate(RIVER_SPEC, _validated(bbox=list(_KANSAS), waterway_type="drainage"),
                   timeout_s=1)
    river.delegate(RIVER_SPEC, _validated(bbox=list(_KANSAS), waterway_type="all"),
                   timeout_s=1)
    assert asked[0] == {"waterway": ["river", "stream", "canal"]}
    assert asked[1] == {"waterway": ["ditch", "drain"]}
    assert asked[2] == {"waterway": ["river", "stream", "canal", "ditch", "drain"]}


def test_river_rejects_unknown_source():
    with pytest.raises(RouterInputError):
        river.validate(RIVER_SPEC, _validated(bbox=list(_KANSAS), source="merit_hydro"))


def test_river_source_aliases_resolve():
    for alias in ("nhdplus", "nhd"):
        assert _validated(bbox=list(_KANSAS), source=alias)["source"] == "nhdplus_hr"


# --------------------------------------------------------------------------- #
# Parse hook: LineString extraction, bbox clip (fills + no spill), honest empty.
# --------------------------------------------------------------------------- #


def test_river_parse_fills_bbox_and_clips_spill(monkeypatch):
    min_lon, min_lat, max_lon, max_lat = _KANSAS
    mid = 0.5 * (min_lat + max_lat)
    _serve(monkeypatch, [
        # spans the full bbox width
        (1001, [(min_lon, mid), (0.5 * (min_lon + max_lon), mid), (max_lon, mid)],
         {"waterway": "river", "name": "Big River"}),
        # starts inside, runs well off the right edge -> the clip must trim it
        (1002, [(max_lon - 0.01, min_lat + 0.01), (max_lon + 0.5, min_lat + 0.01)],
         {"waterway": "stream", "name": "Edge Creek"}),
    ])
    feats = river.delegate(RIVER_SPEC, {"bbox": list(_KANSAS)}, timeout_s=1)
    assert len(feats) >= 1
    assert sorted(feats[0]["properties"]) == ["name", "osm_id", "waterway"]
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, RIVER_SPEC, {"bbox": list(_KANSAS)}))
    minx, miny, maxx, maxy = gdf.total_bounds
    eps = 1e-6
    assert (maxx - minx) >= 0.5 * (max_lon - min_lon)  # fills the bbox width
    assert minx >= min_lon - eps and maxx <= max_lon + eps
    assert miny >= min_lat - eps and maxy <= max_lat + eps


def test_river_parse_empty_yields_header_only_fgb(monkeypatch):
    _serve(monkeypatch, [])
    feats = river.delegate(RIVER_SPEC, {"bbox": list(_KANSAS)}, timeout_s=1)
    assert feats == []
    # An empty result is a valid 0-feature layer, never a typed error (twin contract).
    fgb = features_to_fgb_bytes(feats, RIVER_SPEC, {"bbox": list(_KANSAS)})
    assert isinstance(fgb, bytes) and len(fgb) > 0


# --------------------------------------------------------------------------- #
# Guardrail: the 5000 km^2 area gate (max_bbox_km2).
# --------------------------------------------------------------------------- #


def test_river_oversized_bbox_rejected_km2():
    oversized = (-81.9, 25.5, -80.1, 27.0)  # ~25,000 km^2
    with pytest.raises(RouterInputError) as ei:
        _validated(bbox=list(oversized))
    assert "km^2" in str(ei.value)


# --------------------------------------------------------------------------- #
# End-to-end LayerURI + cache-key stability.
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


def test_river_end_to_end_layer_uri(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    min_lon, min_lat, max_lon, max_lat = _KANSAS
    mid = 0.5 * (min_lat + max_lat)
    _serve(monkeypatch, [(1, [(min_lon, mid), (max_lon, mid)],
                          {"waterway": "river", "name": "Big River"})])

    layer = router.route(RIVER_SPEC, {"bbox": list(_KANSAS)})
    assert layer.layer_type == "vector"
    assert layer.role == "input"
    assert layer.style["kind"] == "reference"
    assert layer.uri.startswith("s3://") and "/river_geometry/" in layer.uri
    assert layer.uri.endswith(".fgb")


def test_river_cache_key_distinct_per_bbox(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    _serve(monkeypatch, [(1, [(-100.0, 40.0), (-99.0, 40.0)], {"waterway": "river"})])
    fl = router.route(RIVER_SPEC, {"bbox": list(_FORT_MYERS)})
    ca = router.route(RIVER_SPEC, {"bbox": [-118.4, 33.8, -118.2, 34.0]})
    assert fl.uri != ca.uri


def test_river_default_cache_key_stable_across_none_and_absent(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    calls = {"n": 0}

    def fake(spec, params, tags, *, timeout_s):
        calls["n"] += 1
        return _frame([(1, [(-97.35, 37.7), (-97.25, 37.7)], {"waterway": "river"})])

    monkeypatch.setattr(river, "overpass_features", fake)
    no_arg = router.route(RIVER_SPEC, {"bbox": list(_KANSAS)})
    explicit_none = router.route(RIVER_SPEC, {"bbox": list(_KANSAS), "waterway_type": None})
    assert no_arg.uri == explicit_none.uri
    assert calls["n"] == 1  # the None call was a cache hit (same key)


def test_river_waterway_type_distinct_cache_key(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    _serve(monkeypatch, [(1, [(-97.35, 37.7), (-97.25, 37.7)], {"waterway": "river"})])
    default = router.route(RIVER_SPEC, {"bbox": list(_KANSAS)})
    drainage = router.route(RIVER_SPEC, {"bbox": list(_KANSAS), "waterway_type": "drainage"})
    all_ = router.route(RIVER_SPEC, {"bbox": list(_KANSAS), "waterway_type": "all"})
    assert default.uri != drainage.uri != all_.uri
    assert default.uri != all_.uri
