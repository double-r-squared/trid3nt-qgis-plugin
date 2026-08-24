"""Router value coverage for the fetch_fault_sources fold (ADR 0081).

The GEM active-faults twin folded to a source.yaml + fault_sources hooks: the
``constant_cache`` two-tier cache (whole-world 10.6 MB GeoJSON downloaded once,
AOI-filtered in the parse hook), the ``variant_by_emptiness`` output switch
(zero-fault AOI -> record dict, non-empty -> FaultSourcesResult), and the
post-emit envelope hook. These tests carry the value-bearing surface the deleted
twin's tests carried: the '(best,min,max)' triple parse, the >=2-distinct-vertex
+ slip>0 gate, the bbox filter, the kinematic-record reconstruction, the honest
empty degrade, and the two-tier cache (no per-AOI re-download).
"""

from __future__ import annotations

import json
import os
import tempfile

import geopandas as gpd
import pytest

from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers._router.hooks import fault_sources as fsh
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_SF_BBOX = [-122.55, 37.45, -122.15, 37.90]

_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"name": "San Andreas (Peninsula)",
            "net_slip_rate": "(17.0,12.0,22.0)", "average_dip": "(90,,)",
            "average_rake": "(180.0,,)", "upper_seis_depth": "(0.0,,)",
            "lower_seis_depth": "(12.0,,)", "slip_type": "Dextral", "catalog_name": "UCERF3"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-122.50, 37.50], [-122.40, 37.65], [-122.30, 37.80]]}},
        {"type": "Feature", "properties": {"name": "Mount Diablo Thrust",
            "net_slip_rate": "(1.55,0.8,2.22)", "average_dip": "(38,,)",
            "average_rake": "(90.0,,)", "upper_seis_depth": "(8.0,,)",
            "lower_seis_depth": "(16.0,,)", "slip_type": "Reverse", "catalog_name": "UCERF3"},
         "geometry": {"type": "MultiLineString", "coordinates": [
             [[-122.45, 37.55, 0.0], [-122.35, 37.70, 0.0]],
             [[-122.35, 37.70, 0.0], [-122.25, 37.85, 0.0]]]}},
        {"type": "Feature", "properties": {"name": "Zero-slip creep segment",
            "net_slip_rate": "(0.0,,)", "average_dip": "(90,,)", "average_rake": "(180.0,,)"},
         "geometry": {"type": "LineString", "coordinates": [[-122.40, 37.60], [-122.38, 37.62]]}},
        {"type": "Feature", "properties": {"name": "Degenerate one-point",
            "net_slip_rate": "(5.0,,)", "average_dip": "(90,,)"},
         "geometry": {"type": "LineString", "coordinates": [[-122.40, 37.60]]}},
        {"type": "Feature", "properties": {"name": "New Madrid (far away)",
            "net_slip_rate": "(2.0,1.0,3.0)", "average_dip": "(90,,)", "average_rake": "(180.0,,)",
            "upper_seis_depth": "(0.0,,)", "lower_seis_depth": "(15.0,,)"},
         "geometry": {"type": "LineString", "coordinates": [[-89.50, 36.50], [-89.40, 36.60]]}},
    ],
}
_PAYLOAD = json.dumps(_FIXTURE).encode()


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_fault_sources"]


def _read(fgb: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


# --------------------------------------------------------------------------- #
# Spec identity (SPEC-IDENTITY rule).
# --------------------------------------------------------------------------- #
def test_spec_identity(spec):
    assert spec.name == "fetch_fault_sources"
    assert spec.shape == "vector-fgb"
    assert spec.error_code_prefix == "FAULT_SOURCES"
    assert spec.source_class == "gem_active_faults"
    assert spec.supports_global_query is True
    assert spec.output.result_model == "FaultSourcesResult"
    assert spec.output.variant_by_emptiness == "fault_sources.empty_record"
    assert spec.output.style_preset == "fault_line"
    assert spec.output.role == "context"
    assert spec.cache.ttl_class == "static-30d"
    assert (spec.ingest or {}).get("constant_cache", {}).get("file_id") == "gem_active_faults_harmonized"


# --------------------------------------------------------------------------- #
# Property-parse helpers (verbatim from the twin).
# --------------------------------------------------------------------------- #
def test_first_num_parses_triple_strings():
    assert fsh.first_num("(15.15,10.49,19.18)") == pytest.approx(15.15)
    assert fsh.first_num("(38,,)") == pytest.approx(38.0)
    assert fsh.first_num("(0.0,,)") == pytest.approx(0.0)
    assert fsh.first_num(7) == 7.0
    assert fsh.first_num([3.2, 1.0]) == pytest.approx(3.2)
    assert fsh.first_num(None, 90.0) == 90.0
    assert fsh.first_num("garbage", 90.0) == 90.0


def test_trace_coords_linestring_and_multilinestring():
    ls = {"type": "LineString", "coordinates": [[-122.5, 37.5], [-122.4, 37.6]]}
    assert fsh.trace_coords(ls) == [[-122.5, 37.5], [-122.4, 37.6]]
    mls = {"type": "MultiLineString",
           "coordinates": [[[-1.0, 2.0, 9.0], [-1.1, 2.1, 9.0]], [[-1.1, 2.1], [-1.2, 2.2]]]}
    assert fsh.trace_coords(mls) == [[-1.0, 2.0], [-1.1, 2.1], [-1.1, 2.1], [-1.2, 2.2]]
    assert fsh.trace_coords({"type": "Point", "coordinates": [0, 0]}) == []


# --------------------------------------------------------------------------- #
# parse_response: bbox filter + kinematic parse + honest-empty.
# --------------------------------------------------------------------------- #
def test_parse_filters_and_kinematic_parse(spec):
    feats = fsh.parse_response(spec, {"bbox": _SF_BBOX}, [_PAYLOAD])
    # Only the 2 in-AOI, slip>0, >=2-distinct faults survive.
    assert len(feats) == 2
    names = {f["properties"]["name"] for f in feats}
    assert names == {"San Andreas (Peninsula)", "Mount Diablo Thrust"}
    sa = next(f for f in feats if f["properties"]["name"].startswith("San Andreas"))
    p = sa["properties"]
    assert p["net_slip_rate_mm_yr"] == pytest.approx(17.0)
    assert p["dip_deg"] == pytest.approx(90.0)
    assert p["rake_deg"] == pytest.approx(180.0)
    assert p["upper_seis_depth_km"] == pytest.approx(0.0)
    assert p["lower_seis_depth_km"] == pytest.approx(12.0)
    assert p["slip_type"] == "Dextral"
    assert p["catalog_name"] == "UCERF3"
    assert sa["geometry"]["type"] == "LineString"
    assert sa["geometry"]["coordinates"][0] == [-122.50, 37.50]
    diablo = next(f for f in feats if f["properties"]["name"] == "Mount Diablo Thrust")
    # MultiLineString flattened to a single ordered 4-vertex trace.
    assert len(diablo["geometry"]["coordinates"]) == 4
    assert diablo["properties"]["dip_deg"] == pytest.approx(38.0)


def test_parse_empty_aoi_is_no_features(spec):
    feats = fsh.parse_response(spec, {"bbox": [-150.0, 10.0, -149.0, 11.0]}, [_PAYLOAD])
    assert feats == []


def test_parse_bad_body_raises_upstream(spec):
    with pytest.raises(Exception) as ei:
        fsh.parse_response(spec, {"bbox": _SF_BBOX}, [b"not json"])
    assert getattr(ei.value, "error_code", "") == "FAULT_SOURCES_UPSTREAM_ERROR"


# --------------------------------------------------------------------------- #
# envelope: kinematic-record reconstruction from the produced FGB.
# --------------------------------------------------------------------------- #
def test_envelope_reconstructs_records(spec):
    feats = fsh.parse_response(spec, {"bbox": _SF_BBOX}, [_PAYLOAD])
    fgb = features_to_fgb_bytes(feats, spec, {"bbox": _SF_BBOX})
    env = fsh.envelope(spec, {"bbox": _SF_BBOX, "catalog": "gem"}, None, fgb)
    assert env["fault_count"] == 2
    assert env["name"] == "Active fault traces (2)"
    assert env["catalog"] == "gem"
    assert env["note"] is None
    assert env["legend"].kind == "categorical"
    names = {r["name"] for r in env["faults"]}
    assert names == {"San Andreas (Peninsula)", "Mount Diablo Thrust"}
    sa = next(r for r in env["faults"] if r["name"].startswith("San Andreas"))
    assert sa["net_slip_rate_mm_yr"] == pytest.approx(17.0)
    assert sa["geometry"][0] == [-122.50, 37.50]
    assert len(sa["geometry"]) == 3


def test_empty_record_shape(spec):
    rec = fsh.empty_record(spec, {"bbox": [-150.0, 10.0, -149.0, 11.0], "catalog": "gem"})
    assert rec["fault_count"] == 0
    assert rec["faults"] == []
    assert rec["catalog"] == "gem"
    assert rec["bbox"] == [-150.0, 10.0, -149.0, 11.0]
    assert "no gem active faults" in rec["note"].lower()
    assert rec["source"] == "GEM Global Active Faults (harmonized)"


# --------------------------------------------------------------------------- #
# build_request: ONE GET of the constant whole-world file (no AOI in URL).
# --------------------------------------------------------------------------- #
def test_build_request_constant_file(spec):
    plans = fsh.build_request(spec, {"bbox": _SF_BBOX})
    assert len(plans) == 1
    assert plans[0].url.endswith("gem_active_faults_harmonized.geojson")
    assert "User-Agent" in plans[0].headers
