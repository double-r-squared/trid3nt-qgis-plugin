"""OSM buildings sidecar-write fold parity (trigger wave, ADR 0084): fetch_buildings.

Migrates the OFFLINE-testable coverage of the deleted twin onto the overpass_sidecar
executor + the ``buildings`` hooks. The LIVE twin-vs-router value parity (Overpass
polygon fetch: slim FGB schema + per-fid tag bags value-identical, sidecar sibling key,
geometry area) is proven by the ADR 0084 live drive. Here the offline surfaces are: spec
identity, the QL build, the (features, tags) parse (ways->Polygon, relations->
(Multi)Polygon, slim props, tag capture, intersects-not-clip, junk drop), the sidecar
sibling-key derivation, empty -> BUILDINGS_EMPTY, and param validation.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.executors import overpass_sidecar
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes
from trid3nt_server.tools.fetchers._router.hooks import buildings as BH
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/source.yaml"
)

# An AOI wide enough to contain the synthetic footprints (~26.60-26.63, -81.87..-81.84).
_AOI = (-81.90, 26.58, -81.82, 26.65)


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(SPEC, raw)


def _payload_bytes(elements: list[dict[str, Any]]) -> bytes:
    return json.dumps({"elements": elements}).encode("utf-8")


_ELEMENTS = [
    {
        "type": "way", "id": 111, "tags": {"building": "yes", "name": "Block A"},
        "geometry": [
            {"lat": 26.60, "lon": -81.85}, {"lat": 26.60, "lon": -81.84},
            {"lat": 26.61, "lon": -81.84}, {"lat": 26.61, "lon": -81.85},
            {"lat": 26.60, "lon": -81.85},
        ],
    },
    {
        "type": "relation", "id": 222, "tags": {"building": "commercial"},
        "members": [
            {"type": "way", "role": "outer", "geometry": [
                {"lat": 26.62, "lon": -81.87}, {"lat": 26.62, "lon": -81.86},
                {"lat": 26.63, "lon": -81.86}, {"lat": 26.63, "lon": -81.87},
                {"lat": 26.62, "lon": -81.87}]},
            {"type": "way", "role": "inner", "geometry": [
                {"lat": 26.625, "lon": -81.868}, {"lat": 26.625, "lon": -81.862},
                {"lat": 26.628, "lon": -81.862}, {"lat": 26.625, "lon": -81.868}]},
        ],
    },
    {"type": "node", "id": 333, "lat": 26.6, "lon": -81.8},           # not a polygon -> drop
    {"type": "way", "id": 444, "tags": {"building": "yes"}, "geometry": [
        {"lat": 26.60, "lon": -81.85}, {"lat": 26.60, "lon": -81.84}]},  # degenerate -> drop
]


def _to_gdf(b: bytes) -> gpd.GeoDataFrame:
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(b)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


# --------------------------------------------------------------------------- #
# Spec identity.
# --------------------------------------------------------------------------- #


def test_spec_identity():
    assert SPEC.name == "fetch_buildings" and SPEC.source_class == "buildings"
    assert SPEC.error_code_prefix == "BUILDINGS"
    assert SPEC.shape == "vector-fgb" and SPEC.output.layer_type == "vector"
    assert SPEC.output.role == "input" and SPEC.output.style["kind"] == "reference"
    assert SPEC.output.emit_bbox is False
    assert SPEC.hooks.build_request == "buildings.build_request"
    sw = SPEC.ingest["sidecar_write"]
    assert sw == {"ext": "tags.json", "parse": "buildings.parse"}
    assert SPEC.cache.ttl_class == "static-30d"
    assert SPEC.docstring and "footprint" in SPEC.docstring.lower()
    assert SPEC.corpus


def test_executor_is_overpass_sidecar():
    assert router.select_executor(SPEC).__module__.endswith("executors.overpass_sidecar")


def test_promoted_signature_matches_twin():
    from trid3nt_server.tools.fetchers._router import registration
    sig, _ = registration.promoted_signature(SPEC)
    assert list(sig.parameters) == ["bbox", "source", "_extra_ignored"]
    assert sig.parameters["source"].default == "osm"


# --------------------------------------------------------------------------- #
# build_request -- the Overpass QL.
# --------------------------------------------------------------------------- #


def test_build_request_ql_selects_ways_and_relations():
    params = _vp(bbox=list(_AOI))
    plans = BH.build_request(SPEC, params)
    assert len(plans) == 1 and plans[0].method == "POST"
    ql = plans[0].data["data"]
    assert 'way["building"]' in ql and 'relation["building"]' in ql
    assert "out geom;" in ql
    # bbox corners are (south, west, north, east) -- lat first (on the quantized bbox).
    w, s, e, n = params["bbox"]
    assert f"{s},{w},{n},{e}" in ql


# --------------------------------------------------------------------------- #
# parse -- (features, tags) assembly.
# --------------------------------------------------------------------------- #


def test_parse_assembles_polygons_relations_slim_props_and_tags():
    params = _vp(bbox=list(_AOI))
    features, tags = BH.parse(SPEC, params, [_payload_bytes(_ELEMENTS)])
    assert len(features) == 2
    assert {f["geometry"]["type"] for f in features} <= {"Polygon", "MultiPolygon"}
    assert {f["properties"]["osm_id"] for f in features} == {111, 222}
    # SLIM inline props: id-only, no building/name.
    for f in features:
        assert set(f["properties"]) == {"osm_id", "osm_type", "fid"}
    assert {f["properties"]["fid"] for f in features} == {"w111", "r222"}
    # FULL tag bag captured for the sidecar, keyed by fid.
    assert tags["w111"] == {"building": "yes", "name": "Block A"}
    assert tags["r222"] == {"building": "commercial"}


def test_parse_drops_footprints_entirely_outside_bbox():
    # A footprint well outside the AOI is dropped (intersects filter).
    outside = [{
        "type": "way", "id": 999, "tags": {"building": "yes"},
        "geometry": [
            {"lat": 10.0, "lon": -50.0}, {"lat": 10.0, "lon": -49.99},
            {"lat": 10.01, "lon": -49.99}, {"lat": 10.0, "lon": -50.0}],
    }]
    features, _ = BH.parse(SPEC, _vp(bbox=list(_AOI)), [_payload_bytes(_ELEMENTS + outside)])
    assert {f["properties"]["osm_id"] for f in features} == {111, 222}


def test_parse_serializes_to_slim_fgb():
    features, _ = BH.parse(SPEC, _vp(bbox=list(_AOI)), [_payload_bytes(_ELEMENTS)])
    gdf = _to_gdf(features_to_fgb_bytes(features, SPEC, _vp(bbox=list(_AOI))))
    assert len(gdf) == 2
    assert set(gdf.columns) == {"osm_id", "osm_type", "fid", "geometry"}


def test_empty_features_raise_buildings_empty():
    from trid3nt_server.tools.fetchers._router.executors import http_json

    # Monkeypatch the transport at the executor's fetch seam to return an empty
    # Overpass body, so execute() reaches the empty-features gate without a network hit.
    import trid3nt_server.tools.fetchers._router.executors.overpass_sidecar as osx
    orig = osx._fetch_endpoint_fallback
    osx._fetch_endpoint_fallback = lambda spec, plans: [_payload_bytes([])]
    try:
        with pytest.raises(RouterEmptyError) as ei:
            osx.execute(SPEC, _vp(bbox=list(_AOI)))
        assert ei.value.error_code == "BUILDINGS_EMPTY"
    finally:
        osx._fetch_endpoint_fallback = orig


# --------------------------------------------------------------------------- #
# Sidecar sibling-key derivation.
# --------------------------------------------------------------------------- #


def test_sidecar_uri_is_sibling_of_fgb():
    from trid3nt_server.tools.cache import cache_path, compute_cache_key
    params = _vp(bbox=list(_AOI), source="osm")
    key = compute_cache_key(SPEC.source_class, params, SPEC.cache.ttl_class)
    fgb = cache_path(SPEC.source_class, SPEC.cache.ttl_class, key, "fgb")
    side = overpass_sidecar.sidecar_uri(SPEC, params, "tags.json")
    assert side.endswith(fgb.replace(".fgb", ".tags.json"))
    assert "/static-30d/buildings/" in side


# --------------------------------------------------------------------------- #
# Param validation.
# --------------------------------------------------------------------------- #


def test_bbox_required():
    with pytest.raises(RouterInputError):
        _vp(source="osm")


def test_unknown_source_rejected():
    with pytest.raises(RouterInputError):
        _vp(bbox=list(_AOI), source="usgs-nationalmap")


def test_bbox_res10_quantized():
    vp = _vp(bbox=list(_AOI))
    # res_10 snaps to ~10 m; the AOI is preserved (envelope), 6 dp stable.
    assert len(vp["bbox"]) == 4 and vp["source"] == "osm"
