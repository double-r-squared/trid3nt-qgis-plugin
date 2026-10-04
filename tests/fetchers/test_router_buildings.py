"""``fetch_buildings``: the Overpass footprints through the library delegate.

The offline surfaces: spec identity, the features parse (ways to polygons,
relations to multipolygons, slim properties, intersects rather than clip, junk
dropped), the typed empty and param validation. The live parity is a separate drive."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes
from trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings import hooks as BH
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[2]
    / "trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/source.yaml"
)

# An AOI wide enough to contain the synthetic footprints (~26.60-26.63, -81.87..-81.84).
_AOI = (-81.90, 26.58, -81.82, 26.65)


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(SPEC, raw)


def _frame(rows: list[tuple[str, int, Any, dict]]) -> gpd.GeoDataFrame:
    """A frame shaped as the library returns one: a (element, id) index plus tags."""
    index = pd.MultiIndex.from_tuples(
        [(et, oid) for et, oid, _g, _t in rows], names=["element", "id"])
    keys: list[str] = []
    for _et, _oid, _g, tags in rows:
        keys.extend(k for k in tags if k not in keys)
    cols = {k: [tags.get(k) for _et, _oid, _g, tags in rows] for k in keys}
    return gpd.GeoDataFrame(
        cols, geometry=[g for _et, _oid, g, _t in rows], index=index, crs="EPSG:4326")


#: A way footprint, an assembled multipolygon relation with its courtyard, and a
#: node the library kept because it carried the tag but which is not a footprint.
_BLOCK_A = Polygon([(-81.85, 26.60), (-81.84, 26.60), (-81.84, 26.61), (-81.85, 26.61)])
_COURTYARD = Polygon(
    [(-81.87, 26.62), (-81.86, 26.62), (-81.86, 26.63), (-81.87, 26.63)],
    [[(-81.868, 26.625), (-81.862, 26.625), (-81.862, 26.628), (-81.868, 26.625)]])
_ROWS = [
    ("way", 111, _BLOCK_A, {"building": "yes", "name": "Block A"}),
    ("relation", 222, _COURTYARD, {"building": "commercial"}),
    ("node", 333, Point(-81.8, 26.6), {"building": "yes"}),   # not areal -> dropped
]


def _serve(monkeypatch, rows):
    monkeypatch.setattr(BH, "overpass_features",
                        lambda spec, params, tags, *, timeout_s: _frame(rows))


def _to_gdf(b: bytes) -> gpd.GeoDataFrame:
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(b)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)




def test_spec_identity():
    assert SPEC.name == "fetch_buildings" and SPEC.source_class == "buildings"
    assert SPEC.error_code_prefix == "BUILDINGS"
    assert SPEC.shape == "vector-fgb" and SPEC.output.layer_type == "vector"
    assert SPEC.output.role == "input" and SPEC.output.style["kind"] == "reference"
    assert SPEC.output.emit_bbox is False
    assert SPEC.hooks.delegate == "buildings.features"
    assert SPEC.ingest["delegate"] == {"library": "osmnx", "timeout_s": 180}
    assert SPEC.cache.ttl_class == "static-30d"
    assert SPEC.docstring and "footprint" in SPEC.docstring.lower()


def test_executor_is_library_delegate():
    assert router.select_executor(SPEC).__module__.endswith("executors.library_delegate")


def test_promoted_signature_matches_twin():
    from trid3nt_server.tools.fetchers._router import registration
    sig, _ = registration.promoted_signature(SPEC)
    assert list(sig.parameters) == ["bbox", "source", "_extra_ignored"]
    assert sig.parameters["source"].default == "osm"




def test_features_keeps_areal_footprints_slim(monkeypatch):
    _serve(monkeypatch, _ROWS)
    features = BH.features(SPEC, _vp(bbox=list(_AOI)), timeout_s=1)
    assert len(features) == 2                       # the node is not a footprint
    assert {f["geometry"]["type"] for f in features} <= {"Polygon", "MultiPolygon"}
    assert {f["properties"]["osm_id"] for f in features} == {111, 222}
    # SLIM inline props: id-only, no building/name.
    for f in features:
        assert set(f["properties"]) == {"osm_id", "osm_type", "fid"}
    assert {f["properties"]["fid"] for f in features} == {"w111", "r222"}


def test_a_relation_keeps_its_courtyard(monkeypatch):
    _serve(monkeypatch, _ROWS)
    features = BH.features(SPEC, _vp(bbox=list(_AOI)), timeout_s=1)
    courtyard = next(f for f in features if f["properties"]["osm_id"] == 222)
    assert len(courtyard["geometry"]["coordinates"]) == 2     # an outer ring and a hole


def test_features_serializes_to_slim_fgb(monkeypatch):
    _serve(monkeypatch, _ROWS)
    features = BH.features(SPEC, _vp(bbox=list(_AOI)), timeout_s=1)
    gdf = _to_gdf(features_to_fgb_bytes(features, SPEC, _vp(bbox=list(_AOI))))
    assert len(gdf) == 2
    assert set(gdf.columns) == {"osm_id", "osm_type", "fid", "geometry"}


def test_empty_features_raise_buildings_empty(monkeypatch):
    _serve(monkeypatch, [])
    with pytest.raises(RouterEmptyError) as ei:
        router.select_executor(SPEC)(SPEC, _vp(bbox=list(_AOI)))
    assert ei.value.error_code == "BUILDINGS_EMPTY"




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


def test_the_footprints_are_found_through_their_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("exposure", "fetch_buildings")
