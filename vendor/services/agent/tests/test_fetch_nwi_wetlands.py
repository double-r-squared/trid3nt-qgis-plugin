"""Unit tests for ``fetch_nwi_wetlands`` (USFWS NWI wetland polygons).

Coverage:
- Registration + metadata (cacheable, static-30d, supports_global_query=False,
  payload estimator).
- ``_normalize_props`` strips the ``Wetlands.`` table-prefix -> plain
  attribute/wetland_type/acres.
- ``_esri_json_to_features`` converts Esri rings -> GeoJSON polygon features
  (fallback path).
- ``_features_to_flatgeobuf`` round-trips a small fixture to a readable FGB with
  the 3 semantic columns; empty in -> valid empty FGB.
- bbox validation raises typed input errors.
- ``estimate_payload_mb`` returns a sensible clamped envelope.
- Mocked one-page fetch asserts the geojson body is parsed; a WAF/HTML 200 maps
  to a typed upstream error (fall-loud, not silent-empty).

No network — everything is a fixture or a mocked httpx response.
"""

from __future__ import annotations

import json

import geopandas as gpd
import httpx
import pytest
from shapely.geometry import Polygon

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_nwi_wetlands import (
    NWIWetlandsInputError,
    NWIWetlandsUpstreamError,
    _esri_json_to_features,
    _features_to_flatgeobuf,
    _normalize_props,
    _nwi_query_one_page,
    _validate_bbox,
    estimate_payload_mb,
    fetch_nwi_wetlands,
)

_FL_BBOX = (-81.5, 26.0, -81.3, 26.2)


def test_registered_with_expected_metadata():
    entry = TOOL_REGISTRY.get("fetch_nwi_wetlands")
    assert entry is not None
    m = entry.metadata
    assert m.cacheable is True
    assert m.ttl_class == "static-30d"
    assert m.source_class == "nwi_wetlands"
    assert m.supports_global_query is False
    assert m.payload_mb_estimator_name == "estimate_payload_mb"
    assert entry.fn is fetch_nwi_wetlands


def test_normalize_props_strips_table_prefix():
    props = {
        "Wetlands.ATTRIBUTE": "L1UBHx",
        "Wetlands.WETLAND_TYPE": "Lake",
        "Wetlands.ACRES": 29.76,
        "NWI_Wetland_Codes.SYSTEM_NAME": "Lacustrine",  # dropped
    }
    out = _normalize_props(props)
    assert out == {"attribute": "L1UBHx", "wetland_type": "Lake", "acres": 29.76}


def test_normalize_props_unqualified_keys():
    out = _normalize_props({"ATTRIBUTE": "PFO1A", "wetland_type": "Freshwater Forested/Shrub Wetland"})
    assert out["attribute"] == "PFO1A"
    assert out["wetland_type"].startswith("Freshwater")
    assert out["acres"] is None


def test_esri_json_to_features_rings():
    payload = {
        "features": [
            {
                "attributes": {"Wetlands.ATTRIBUTE": "PEM1C", "Wetlands.WETLAND_TYPE": "Freshwater Emergent Wetland"},
                "geometry": {
                    "rings": [
                        [[-81.4, 26.1], [-81.39, 26.1], [-81.39, 26.11], [-81.4, 26.11], [-81.4, 26.1]]
                    ]
                },
            },
            {"attributes": {}, "geometry": {"rings": []}},  # malformed -> skipped
        ]
    }
    feats = _esri_json_to_features(payload)
    assert len(feats) == 1
    assert feats[0]["geometry"]["type"] == "Polygon"
    assert feats[0]["properties"]["Wetlands.WETLAND_TYPE"].startswith("Freshwater")


def _fixture_features():
    poly = [[-81.4, 26.1], [-81.39, 26.1], [-81.39, 26.11], [-81.4, 26.11], [-81.4, 26.1]]
    return [
        {
            "type": "Feature",
            "properties": {"Wetlands.ATTRIBUTE": "L1UBHx", "Wetlands.WETLAND_TYPE": "Lake", "Wetlands.ACRES": 12.5},
            "geometry": {"type": "Polygon", "coordinates": [poly]},
        },
        {
            "type": "Feature",
            "properties": {"Wetlands.ATTRIBUTE": "PFO1A", "Wetlands.WETLAND_TYPE": "Freshwater Forested/Shrub Wetland", "Wetlands.ACRES": 4.0},
            "geometry": {"type": "Polygon", "coordinates": [poly]},
        },
    ]


def test_features_to_flatgeobuf_roundtrip(tmp_path):
    fgb = _features_to_flatgeobuf(_fixture_features())
    assert isinstance(fgb, bytes) and len(fgb) > 0
    p = tmp_path / "nwi.fgb"
    p.write_bytes(fgb)
    gdf = gpd.read_file(p)
    assert len(gdf) == 2
    assert set(["attribute", "wetland_type", "acres"]).issubset(set(gdf.columns))
    assert set(gdf["wetland_type"]) == {"Lake", "Freshwater Forested/Shrub Wetland"}


def test_features_to_flatgeobuf_empty(tmp_path):
    fgb = _features_to_flatgeobuf([])
    p = tmp_path / "empty.fgb"
    p.write_bytes(fgb)
    gdf = gpd.read_file(p)
    assert len(gdf) == 0


def test_bbox_validation():
    with pytest.raises(NWIWetlandsInputError):
        _validate_bbox((-81.3, 26.0, -81.5, 26.2))  # min>=max lon
    with pytest.raises(NWIWetlandsInputError):
        _validate_bbox((-200.0, 26.0, -81.3, 26.2))  # lon out of range
    _validate_bbox(_FL_BBOX)  # ok


def test_estimate_payload_mb_clamped():
    small = estimate_payload_mb(bbox=_FL_BBOX)
    assert 0.05 <= small <= 50.0
    assert estimate_payload_mb(bbox=None) == 50.0
    huge = estimate_payload_mb(bbox=(-100.0, 20.0, -80.0, 40.0))
    assert huge == 50.0


def test_one_page_geojson_parsed(monkeypatch):
    body = {"type": "FeatureCollection", "features": _fixture_features()}

    def fake_get(self, url, params=None, headers=None):
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    out = _nwi_query_one_page(_FL_BBOX, 0, "geojson")
    assert out["type"] == "FeatureCollection"
    assert len(out["features"]) == 2


def test_one_page_waf_html_is_upstream_error(monkeypatch):
    def fake_get(self, url, params=None, headers=None):
        return httpx.Response(200, text="<html>blocked</html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(NWIWetlandsUpstreamError):
        _nwi_query_one_page(_FL_BBOX, 0, "geojson")
