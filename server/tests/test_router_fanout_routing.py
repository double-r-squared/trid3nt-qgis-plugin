"""Offline tests for the wave-6 router modes (fan-out + per-enum routing).

Exercises the phase-2 wave-6 (ADR 0052) additions against LOCAL synthetic
fixtures, NO live calls:

- ``float_list`` param validation (scalar / list / default / empty->default /
  bad value / non-numeric) -- the fan-out driver's param type.
- fan_out.execute: per-value merge + slr_ft/scenario_label/dissolve stamp +
  honest-empty header-only FGB; per-value endpoint templating; forced upstream.
- endpoint_by_param: enum -> sub-layer endpoint selection (usace_levees layer).
- properties_by_param: per-enum column projection + json_coerce of list fields +
  honest-empty header carrying the per-value column set.
- edge matrix: forced HTTP 404 / 403 / 429, ArcGIS error-envelope, unparseable
  body, honest-empty -- each asserting the typed ``*_UPSTREAM_ERROR`` class +
  retryable flag (actionability of the upstream class).
"""

from __future__ import annotations

import os
import tempfile

import geopandas as gpd
import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.agent.tools.fetchers._router import router
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.agent.tools.fetchers._router.executors import vector_fgb
from trid3nt_server.agent.tools.fetchers._router.transforms import fan_out


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #

_POLY = {
    "type": "Polygon",
    "coordinates": [[[-82.0, 26.0], [-81.9, 26.0], [-81.9, 26.1], [-82.0, 26.1], [-82.0, 26.0]]],
}


def _fgb_gdf(b: bytes) -> gpd.GeoDataFrame:
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(b)
    try:
        return gpd.read_file(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _fanout_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_fanout",
        "source_class": "demo_fanout",
        "error_prefix": "DEMO_FANOUT",
        "input_error_suffix": "INPUT_INVALID",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url_template": "http://example.test/{service}/MapServer/0/query"}},
        "params": {
            "bbox": {"type": "bbox", "required": True},
            "scenario_ft": {"type": "float_list", "required": False,
                            "default": [1.0, 2.0, 3.0], "values": [0.5, 1.0, 2.0, 3.0]},
        },
        "ingest": {
            "query_template": {"f": "geojson"},
            "pagination": {"page_size": 1000},
            "properties": ["slr_ft", "scenario_label", "dissolve"],
            "fan_out": {
                "param": "scenario_ft",
                "endpoint": "data",
                "value_map": {"0.5": "s0_5", "1.0": "s1", "2.0": "s2", "3.0": "s3"},
                "stamp": {
                    "slr_ft": {"source": "value"},
                    "scenario_label": {"source": "value_template", "template": "{value:.1f} ft SLR"},
                    "dissolve": {"source": "prop", "from": "Dissolve", "kind": "int", "default": 1},
                },
            },
        },
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "demo"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.3},
    })


def _levees_like_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_routed",
        "source_class": "demo_routed",
        "shape": "vector-fgb",
        "supports_global_query": True,
        "endpoints": {
            "areas": {"url": "http://example.test/16/query"},
            "routes": {"url": "http://example.test/14/query"},
        },
        "params": {
            "bbox": {"type": "bbox", "required": False, "schema_optional": True},
            "layer": {"type": "enum", "required": False, "default": "leveed_areas",
                      "values": ["leveed_areas", "system_routes"]},
        },
        "ingest": {
            "query_template": {"out_fields": "*", "f": "geojson"},
            "pagination": {"page_size": 1000},
            "endpoint_by_param": {"param": "layer", "map": {"leveed_areas": "areas", "system_routes": "routes"}},
            "json_coerce_nested": True,
            "properties_by_param": {"param": "layer", "map": {
                "leveed_areas": ["SYSTEM_ID", "STATES"],
                "system_routes": ["SYSTEM_ID", "MAX_HEIGHT"],
            }},
        },
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "demo", "emit_bbox": False},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 20.0},
    })


# --------------------------------------------------------------------------- #
# float_list validation.
# --------------------------------------------------------------------------- #


def test_float_list_scalar_list_default():
    spec = _fanout_spec()
    base = {"bbox": [-82.0, 26.0, -81.9, 26.1]}
    assert router.validate_params(spec, base)["scenario_ft"] == [1.0, 2.0, 3.0]  # default
    assert router.validate_params(spec, {**base, "scenario_ft": 2.0})["scenario_ft"] == [2.0]  # scalar
    assert router.validate_params(spec, {**base, "scenario_ft": [3.0, 1.0, 1.0]})["scenario_ft"] == [1.0, 3.0]  # sort+dedup
    assert router.validate_params(spec, {**base, "scenario_ft": []})["scenario_ft"] == [1.0, 2.0, 3.0]  # empty->default


def test_float_list_bad_value_and_type():
    spec = _fanout_spec()
    base = {"bbox": [-82.0, 26.0, -81.9, 26.1]}
    with pytest.raises(RouterInputError) as ei:
        router.validate_params(spec, {**base, "scenario_ft": 9.0})   # not in values
    assert ei.value.error_code == "DEMO_FANOUT_INPUT_INVALID"
    with pytest.raises(RouterInputError):
        router.validate_params(spec, {**base, "scenario_ft": ["a"]})  # non-numeric
    with pytest.raises(RouterInputError):
        router.validate_params(spec, {**base, "scenario_ft": True})   # bool rejected


# --------------------------------------------------------------------------- #
# fan_out.execute.
# --------------------------------------------------------------------------- #


def test_fanout_merge_stamp_and_order(monkeypatch):
    spec = _fanout_spec()
    seen_urls = []

    def _fake_page(spec_, url, params):
        seen_urls.append(url)
        return [{"type": "Feature", "geometry": _POLY, "properties": {"Dissolve": 1}}]

    monkeypatch.setattr(vector_fgb, "_fetch_one_page", _fake_page)
    params = router.validate_params(spec, {"bbox": [-82.0, 26.0, -81.9, 26.1]})
    gdf = _fgb_gdf(fan_out.execute(spec, params))

    assert len(gdf) == 3
    assert [c for c in gdf.columns if c != "geometry"] == ["slr_ft", "scenario_label", "dissolve"]
    assert sorted(float(v) for v in gdf["slr_ft"]) == [1.0, 2.0, 3.0]
    assert set(gdf["scenario_label"]) == {"1.0 ft SLR", "2.0 ft SLR", "3.0 ft SLR"}
    assert set(int(v) for v in gdf["dissolve"]) == {1}
    # per-value endpoint templating hit each service name
    assert any("/s1/" in u for u in seen_urls) and any("/s3/" in u for u in seen_urls)


def test_fanout_honest_empty_header(monkeypatch):
    spec = _fanout_spec()
    monkeypatch.setattr(vector_fgb, "_fetch_one_page", lambda s, u, p: [])
    params = router.validate_params(spec, {"bbox": [-82.0, 26.0, -81.9, 26.1], "scenario_ft": 1.0})
    gdf = _fgb_gdf(fan_out.execute(spec, params))
    assert len(gdf) == 0
    assert [c for c in gdf.columns if c != "geometry"] == ["slr_ft", "scenario_label", "dissolve"]


def test_fanout_forced_upstream(monkeypatch):
    spec = _fanout_spec()

    def _boom(s, u, p):
        raise RouterUpstreamError("forced")

    monkeypatch.setattr(vector_fgb, "_fetch_one_page", _boom)
    params = router.validate_params(spec, {"bbox": [-82.0, 26.0, -81.9, 26.1], "scenario_ft": 1.0})
    with pytest.raises(RouterUpstreamError):
        fan_out.execute(spec, params)


# --------------------------------------------------------------------------- #
# endpoint_by_param + properties_by_param.
# --------------------------------------------------------------------------- #


def test_endpoint_by_param_selects_sublayer():
    spec = _levees_like_spec()
    p_areas = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "leveed_areas"})
    p_routes = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "system_routes"})
    assert vector_fgb.resolve_endpoints(spec, p_areas)[0].url.endswith("/16/query")
    assert vector_fgb.resolve_endpoints(spec, p_routes)[0].url.endswith("/14/query")


def test_properties_by_param_projection_and_json_coerce(monkeypatch):
    spec = _levees_like_spec()
    feats = [{"type": "Feature", "geometry": _POLY,
              "properties": {"SYSTEM_ID": "S1", "STATES": ["LA", "MS"], "EXTRA": "drop"}}]
    monkeypatch.setattr(vector_fgb, "_fetch_one_page", lambda s, u, p: feats)
    params = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "leveed_areas"})
    gdf = _fgb_gdf(vector_fgb.execute(spec, params))
    cols = [c for c in gdf.columns if c != "geometry"]
    assert cols == ["SYSTEM_ID", "STATES"]              # projected to per-layer set, EXTRA dropped
    assert gdf["STATES"].iloc[0] == '["LA", "MS"]'      # list -> JSON string (json_coerce_nested)


def test_properties_by_param_honest_empty_header(monkeypatch):
    spec = _levees_like_spec()
    monkeypatch.setattr(vector_fgb, "_fetch_one_page", lambda s, u, p: [])
    params = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "system_routes"})
    gdf = _fgb_gdf(vector_fgb.execute(spec, params))
    assert len(gdf) == 0
    assert [c for c in gdf.columns if c != "geometry"] == ["SYSTEM_ID", "MAX_HEIGHT"]


# --------------------------------------------------------------------------- #
# Edge matrix: forced HTTP statuses + error envelope + unparseable + empty.
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.mark.parametrize("status", [404, 403, 429, 500])
def test_edge_http_status_typed_upstream(monkeypatch, status):
    spec = _levees_like_spec()
    monkeypatch.setattr("httpx.Client.get", lambda self, *a, **k: _FakeResp(status_code=status, text="boom"))
    params = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "leveed_areas"})
    with pytest.raises(RouterUpstreamError) as ei:
        vector_fgb.execute(spec, params)
    assert ei.value.error_code == "DEMO_ROUTED_UPSTREAM_ERROR"
    assert ei.value.retryable is True
    assert ei.value.actionability == "agent"


def test_edge_arcgis_error_envelope(monkeypatch):
    spec = _levees_like_spec()
    body = '{"error": {"code": 400, "message": "Invalid query"}}'
    monkeypatch.setattr("httpx.Client.get", lambda self, *a, **k: _FakeResp(status_code=200, text=body))
    params = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "leveed_areas"})
    with pytest.raises(RouterUpstreamError) as ei:
        vector_fgb.execute(spec, params)
    assert "Invalid query" in str(ei.value)


def test_edge_unparseable_body(monkeypatch):
    spec = _levees_like_spec()
    monkeypatch.setattr("httpx.Client.get", lambda self, *a, **k: _FakeResp(status_code=200, text="<html>nope"))
    params = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "leveed_areas"})
    with pytest.raises(RouterUpstreamError):
        vector_fgb.execute(spec, params)


def test_edge_empty_is_header_not_error(monkeypatch):
    spec = _levees_like_spec()
    monkeypatch.setattr("httpx.Client.get",
                        lambda self, *a, **k: _FakeResp(status_code=200, text='{"type":"FeatureCollection","features":[]}'))
    params = router.validate_params(spec, {"bbox": [-90.1, 29.9, -90.0, 30.0], "layer": "leveed_areas"})
    gdf = _fgb_gdf(vector_fgb.execute(spec, params))   # honest-empty, NEVER an error
    assert len(gdf) == 0


# --------------------------------------------------------------------------- #
# esri-json ingest mode + percentile/fraction/raw column kinds (ejscreen).
# --------------------------------------------------------------------------- #


def _ejscreen_like_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_esri",
        "source_class": "demo_esri",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url": "http://example.test/FeatureServer/0/query"}},
        "params": {
            "bbox": {"type": "bbox", "required": True},
            "indicator": {"type": "enum", "required": False, "default": "pm25",
                          "lowercase": True, "values": ["pm25", "ozone"]},
        },
        "ingest": {
            "esri_json": True, "geometry_envelope": "json",
            "query_template": {"out_fields": "*", "f": "json"},
            "pagination": {"mode": "result_offset", "page_size": 2000},
            "column_map": {
                "bg_id": {"from": "ID"},
                "indicator": {"kind": "param", "param": "indicator"},
                "value": {"kind": "percentile", "from_param": {"param": "indicator", "map": {"pm25": "P_PM25", "ozone": "P_OZONE"}}},
                "minority_pct": {"from": "MINORPCT", "kind": "fraction"},
                "pm25_raw": {"from": "PM25", "kind": "raw"},
                "total_pop": {"from": "ACSTOTPOP", "kind": "int", "null_below": -999.0},
            },
        },
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "demo"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 4.0},
    })


def test_esri_geometry_decode_variants():
    poly = vector_fgb._esri_geometry_to_geojson({"rings": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})
    assert poly["type"] == "Polygon"
    assert vector_fgb._esri_geometry_to_geojson({"rings": [[[0, 0], [1, 0]]]}) is None  # degenerate
    assert vector_fgb._esri_geometry_to_geojson({"x": -95.0, "y": 29.0})["type"] == "Point"
    assert vector_fgb._esri_geometry_to_geojson({"paths": [[[0, 0], [1, 1]]]})["type"] == "LineString"
    assert vector_fgb._esri_geometry_to_geojson({"paths": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]})["type"] == "MultiLineString"


@pytest.mark.parametrize("kind,val,expect", [
    ("percentile", 83.4, 83.4), ("percentile", -999, None), ("percentile", 150.0, None),
    ("fraction", 0.62, 0.62), ("fraction", -999, None), ("fraction", 1.5, None),
    ("raw", 9.1, 9.1), ("raw", -999, None), ("raw", None, None),
])
def test_norm_env_sentinels(kind, val, expect):
    assert vector_fgb._norm_env(val, kind) == expect


def test_esri_json_projection_from_param(monkeypatch):
    spec = _ejscreen_like_spec()
    esri = [{"attributes": {"ID": "48", "P_PM25": 83.4, "P_OZONE": -999, "MINORPCT": 0.62,
                            "PM25": 9.1, "ACSTOTPOP": 1500},
             "geometry": {"rings": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}]
    monkeypatch.setattr("httpx.Client.get",
                        lambda self, *a, **k: _FakeResp(status_code=200, text=__import__("json").dumps({"features": esri})))
    params = router.validate_params(spec, {"bbox": [-95.3, 29.7, -95.2, 29.8], "indicator": "PM25"})
    assert params["indicator"] == "pm25"                 # enum lowercase
    gdf = _fgb_gdf(vector_fgb.execute(spec, params))
    assert [c for c in gdf.columns if c != "geometry"] == ["bg_id", "indicator", "value", "minority_pct", "pm25_raw", "total_pop"]
    row = gdf.iloc[0]
    assert row["indicator"] == "pm25"                    # kind=param echo
    assert float(row["value"]) == 83.4                   # from_param -> P_PM25
    assert int(row["total_pop"]) == 1500


def test_esri_json_geometry_envelope_is_json():
    spec = _ejscreen_like_spec()
    _url, qp = vector_fgb.build_query_params(spec, (-95.3, 29.7, -95.2, 29.8),
                                             endpoint=spec.endpoints["data"])
    assert qp["f"] == "json" and qp["returnGeometry"] == "true"
    assert qp["geometry"].startswith("{") and '"xmin"' in qp["geometry"]   # JSON envelope, not comma
