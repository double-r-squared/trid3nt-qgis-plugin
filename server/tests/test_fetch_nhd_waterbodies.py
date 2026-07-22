"""Unit tests for ``fetch_nhd_waterbodies`` (USGS NHD waterbody polygons).

Coverage:
- Registration + metadata.
- ``_normalize_props`` is case-insensitive (HR lowercase vs medium-res
  UPPERCASE) and derives ``ftype_label`` from the numeric ``ftype``.
- ``_features_to_flatgeobuf`` round-trips a small fixture; empty -> empty FGB.
- Primary -> fallback: a primary-endpoint failure transparently falls back to
  the medium-resolution NHD waterbody layer.
- Both-fail -> honest typed upstream error.
- bbox validation.

No network — fixtures + a mocked httpx that keys on the request URL.
"""

from __future__ import annotations

import geopandas as gpd
import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_nhd_waterbodies import (
    NHD_WATERBODY_URL_FALLBACK,
    NHD_WATERBODY_URL_PRIMARY,
    NHDWaterbodiesInputError,
    NHDWaterbodiesUpstreamError,
    _features_to_flatgeobuf,
    _fetch_nhd_features,
    _normalize_props,
    _validate_bbox,
    fetch_nhd_waterbodies,
)

_FL_BBOX = (-81.5, 26.0, -81.3, 26.2)


def test_registered_with_expected_metadata():
    entry = TOOL_REGISTRY.get("fetch_nhd_waterbodies")
    assert entry is not None
    m = entry.metadata
    assert m.cacheable is True
    assert m.ttl_class == "static-30d"
    assert m.source_class == "nhd_waterbodies"
    assert m.supports_global_query is False
    assert entry.fn is fetch_nhd_waterbodies


def test_normalize_props_lowercase_hr():
    out = _normalize_props(
        {"gnis_name": "Lake Trafford", "ftype": 390, "areasqkm": 6.1, "fcode": 39004}
    )
    assert out["gnis_name"] == "Lake Trafford"
    assert out["ftype"] == 390
    assert out["ftype_label"] == "LakePond"
    assert out["areasqkm"] == 6.1


def test_normalize_props_uppercase_fallback_schema():
    out = _normalize_props(
        {"GNIS_NAME": None, "FTYPE": 436, "AREASQKM": 0.9, "PERMANENT_IDENTIFIER": "abc"}
    )
    assert out["ftype"] == 436
    assert out["ftype_label"] == "Reservoir"
    assert out["permanent_identifier"] == "abc"


def _fixture_features():
    poly = [[-81.4, 26.1], [-81.39, 26.1], [-81.39, 26.11], [-81.4, 26.11], [-81.4, 26.1]]
    return [
        {
            "type": "Feature",
            "properties": {"gnis_name": "Big Lake", "ftype": 390, "areasqkm": 2.0},
            "geometry": {"type": "Polygon", "coordinates": [poly]},
        },
        {
            "type": "Feature",
            "properties": {"gnis_name": None, "ftype": 466, "areasqkm": 0.03},
            "geometry": {"type": "Polygon", "coordinates": [poly]},
        },
    ]


def test_features_to_flatgeobuf_roundtrip(tmp_path):
    fgb = _features_to_flatgeobuf(_fixture_features())
    p = tmp_path / "nhd.fgb"
    p.write_bytes(fgb)
    gdf = gpd.read_file(p)
    assert len(gdf) == 2
    assert {"permanent_identifier", "gnis_name", "ftype", "ftype_label", "areasqkm"}.issubset(
        set(gdf.columns)
    )
    assert set(gdf["ftype_label"]) == {"LakePond", "SwampMarsh"}


def test_features_to_flatgeobuf_empty(tmp_path):
    fgb = _features_to_flatgeobuf([])
    p = tmp_path / "e.fgb"
    p.write_bytes(fgb)
    assert len(gpd.read_file(p)) == 0


def test_bbox_validation():
    with pytest.raises(NHDWaterbodiesInputError):
        _validate_bbox((0.0, 0.0, 0.0, 0.0))
    _validate_bbox(_FL_BBOX)


def test_primary_to_fallback(monkeypatch):
    body = {"type": "FeatureCollection", "features": _fixture_features()}

    def fake_get(self, url, params=None, headers=None):
        req = httpx.Request("GET", url)
        if url == NHD_WATERBODY_URL_PRIMARY:
            return httpx.Response(500, text="primary down", request=req)
        assert url == NHD_WATERBODY_URL_FALLBACK
        return httpx.Response(200, json=body, request=req)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    feats = _fetch_nhd_features(_FL_BBOX)
    assert len(feats) == 2


def test_both_endpoints_fail(monkeypatch):
    def fake_get(self, url, params=None, headers=None):
        return httpx.Response(503, text="down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(NHDWaterbodiesUpstreamError):
        _fetch_nhd_features(_FL_BBOX)
