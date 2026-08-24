"""Overpass river fold parity (ADR 0074): fetch_river_geometry via the router.

Migrates the value-bearing coverage of the deleted twin's tests (the
fetch_river_geometry block of test_data_fetch.py) onto the spec-driven surface:
the PURE overpass_river hooks (waterway QL build, class-vocabulary resolution,
LineString clip decode, honest-empty), the max_bbox_km2 guardrail, and the
end-to-end LayerURI + cache-key stability. The vestigial NHDPlus HR HUC4 leg was
DROPPED (NATE-decided), so its fallback-ordering tests are intentionally absent.
Offline: synthetic Overpass JSON bodies + an in-memory read_through injector; the
real 3-mirror network path is proven by the live proof recorded in ADR 0074.
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
from trid3nt_server.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.tools.fetchers._router.executors import http_json
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers._router.hooks import overpass
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

RIVER_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/source.yaml"
)

# Kansas -- outside every old v0.1 HUC4 envelope (the exact case that used to
# dead-end before OSM became the primary; now the only path).
_KANSAS = (-97.4, 37.6, -97.2, 37.8)
_FORT_MYERS = (-81.92, 26.55, -81.80, 26.68)
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
    r = overpass._resolve_waterway_classes
    assert r("P", "S", None) == ("river", "stream", "canal")
    assert r("P", "S", "") == ("river", "stream", "canal")
    assert r("P", "S", "   ") == ("river", "stream", "canal")
    assert r("P", "S", "all") == ("river", "stream", "canal", "ditch", "drain")
    assert r("P", "S", "drainage") == ("ditch", "drain")
    assert r("P", "S", "ditches") == ("ditch", "drain")
    assert r("P", "S", "  Ditch ") == ("ditch",)
    assert r("P", "S", "ditch,drain") == ("ditch", "drain")
    assert r("P", "S", "river+ditch") == ("river", "ditch")
    assert r("P", "S", ["ditch", "drain", "ditch"]) == ("ditch", "drain")


def test_resolve_waterway_classes_rejects_unknown_tokens():
    r = overpass._resolve_waterway_classes
    for bad in ("sewer", "river,sewer"):
        with pytest.raises(RouterInputError):
            r("RIVER_GEOMETRY", "INPUT_INVALID", bad)
    with pytest.raises(RouterInputError):
        r("RIVER_GEOMETRY", "INPUT_INVALID", ["ditch", 5])  # type: ignore[list-item]
    with pytest.raises(RouterInputError):
        r("RIVER_GEOMETRY", "INPUT_INVALID", 42)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# QL build hook: waterway regex + selectable classes + mirrors.
# --------------------------------------------------------------------------- #


def test_river_ql_default_classes_and_mirrors():
    plans = overpass.build_request_river(RIVER_SPEC, _validated(bbox=list(_KANSAS)))
    assert len(plans) == 3  # 3-mirror fallback chain
    assert all(p.method == "POST" for p in plans)
    ql = plans[0].data["data"]
    assert 'waterway"~"^(river|stream|canal)$"' in ql
    assert "ditch" not in ql


def test_river_ql_drainage_and_all_widen():
    ql_drain = overpass.build_request_river(
        RIVER_SPEC, _validated(bbox=list(_KANSAS), waterway_type="drainage")
    )[0].data["data"]
    assert 'waterway"~"^(ditch|drain)$"' in ql_drain
    assert "river|stream|canal" not in ql_drain
    ql_all = overpass.build_request_river(
        RIVER_SPEC, _validated(bbox=list(_KANSAS), waterway_type="all")
    )[0].data["data"]
    assert "ditch|drain" in ql_all and "river|stream|canal" in ql_all


def test_river_rejects_unknown_source():
    with pytest.raises(RouterInputError):
        overpass.build_request_river(
            RIVER_SPEC, _validated(bbox=list(_KANSAS), source="merit_hydro")
        )


def test_river_source_aliases_resolve():
    for alias in ("nhdplus", "nhd"):
        assert _validated(bbox=list(_KANSAS), source=alias)["source"] == "nhdplus_hr"


# --------------------------------------------------------------------------- #
# Parse hook: LineString extraction, bbox clip (fills + no spill), honest empty.
# --------------------------------------------------------------------------- #


def test_river_parse_fills_bbox_and_clips_spill():
    min_lon, min_lat, max_lon, max_lat = _KANSAS
    mid = 0.5 * (min_lat + max_lat)
    body = _body([
        # spans the full bbox width
        _way(1001, [(min_lon, mid), (0.5 * (min_lon + max_lon), mid), (max_lon, mid)],
             waterway="river", name="Big River"),
        # starts inside, runs well off the right edge -> clip must trim it
        _way(1002, [(max_lon - 0.01, min_lat + 0.01), (max_lon + 0.5, min_lat + 0.01)],
             waterway="stream", name="Edge Creek"),
    ])
    feats = overpass.parse_response_river(RIVER_SPEC, {"bbox": list(_KANSAS)}, [body])
    assert len(feats) >= 1
    assert sorted(feats[0]["properties"]) == ["name", "osm_id", "waterway"]
    gdf = _fgb_gdf(features_to_fgb_bytes(feats, RIVER_SPEC, {"bbox": list(_KANSAS)}))
    minx, miny, maxx, maxy = gdf.total_bounds
    eps = 1e-6
    assert (maxx - minx) >= 0.5 * (max_lon - min_lon)  # fills the bbox width
    assert minx >= min_lon - eps and maxx <= max_lon + eps
    assert miny >= min_lat - eps and maxy <= max_lat + eps


def test_river_parse_empty_yields_header_only_fgb():
    feats = overpass.parse_response_river(RIVER_SPEC, {"bbox": list(_KANSAS)}, [_body([])])
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
    body = _body([_way(1, [(min_lon, mid), (max_lon, mid)], waterway="river", name="Big River")])
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: body)

    layer = router.route(RIVER_SPEC, {"bbox": list(_KANSAS)})
    assert layer.layer_type == "vector"
    assert layer.role == "input"
    assert layer.style_preset == "osm_waterways"
    assert layer.uri.startswith("s3://") and "/river_geometry/" in layer.uri
    assert layer.uri.endswith(".fgb")


def test_river_cache_key_distinct_per_bbox(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: _body([
        _way(1, [(-100.0, 40.0), (-99.0, 40.0)], waterway="river"),
    ]))
    fl = router.route(RIVER_SPEC, {"bbox": list(_FORT_MYERS)})
    ca = router.route(RIVER_SPEC, {"bbox": [-118.4, 33.8, -118.2, 34.0]})
    assert fl.uri != ca.uri


def test_river_default_cache_key_stable_across_none_and_absent(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    calls = {"n": 0}

    def fake(plan):
        calls["n"] += 1
        return _body([_way(1, [(-97.35, 37.7), (-97.25, 37.7)], waterway="river")])

    monkeypatch.setattr(http_json, "_get_raw", fake)
    no_arg = router.route(RIVER_SPEC, {"bbox": list(_KANSAS)})
    explicit_none = router.route(RIVER_SPEC, {"bbox": list(_KANSAS), "waterway_type": None})
    assert no_arg.uri == explicit_none.uri
    assert calls["n"] == 1  # the None call was a cache hit (same key)


def test_river_waterway_type_distinct_cache_key(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: _body([
        _way(1, [(-97.35, 37.7), (-97.25, 37.7)], waterway="river"),
    ]))
    default = router.route(RIVER_SPEC, {"bbox": list(_KANSAS)})
    drainage = router.route(RIVER_SPEC, {"bbox": list(_KANSAS), "waterway_type": "drainage"})
    all_ = router.route(RIVER_SPEC, {"bbox": list(_KANSAS), "waterway_type": "all"})
    assert default.uri != drainage.uri != all_.uri
    assert default.uri != all_.uri
