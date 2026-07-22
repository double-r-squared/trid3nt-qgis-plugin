"""Unit tests for ``fetch_fault_sources`` (task #199 — real-fault OpenQuake).

Asserts the GEM Global Active Faults parse + bbox filter + honest empty-AOI
degrade against a small fixture GeoJSON shaped like the real harmonized file
(``'(best,min,max)'`` string properties + a MultiLineString geometry). No
network: the upstream download is monkeypatched and the cache read-through is
swapped for the in-memory S3 injector.

Run:
    services/agent/.venv/bin/python -m pytest \
        services/agent/tests/test_fetch_fault_sources.py -v
"""

from __future__ import annotations

import json

import pytest

from grace2_contracts.execution import LayerURI

from grace2_agent.tools import fetch_fault_sources as ffs
from grace2_agent.tools.fetch_fault_sources import (
    FAULT_LINE_STYLE_PRESET,
    FaultSourcesInputError,
    FaultSourcesResult,
    faults_to_feature_collection,
    fetch_fault_sources,
    first_num,
    trace_coords,
)


# ---------------------------------------------------------------------------
# Fixture: a few real-shaped GEM GAF features.
#   - San Andreas-like dextral LineString through the SF AOI (slip set).
#   - Mount Diablo-like reverse fault with a MultiLineString trace + blank
#     min/max in the '(best,,)' triples.
#   - A zero-slip fault inside the AOI (must be skipped).
#   - A 1-point degenerate trace inside the AOI (must be skipped).
#   - A well-formed fault FAR outside the AOI (must be filtered out).
# ---------------------------------------------------------------------------
_SF_BBOX = [-122.55, 37.45, -122.15, 37.90]

_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "name": "San Andreas (Peninsula)",
                "net_slip_rate": "(17.0,12.0,22.0)",
                "average_dip": "(90,,)",
                "average_rake": "(180.0,,)",
                "upper_seis_depth": "(0.0,,)",
                "lower_seis_depth": "(12.0,,)",
                "slip_type": "Dextral",
                "catalog_name": "UCERF3",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [-122.50, 37.50],
                    [-122.40, 37.65],
                    [-122.30, 37.80],
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "name": "Mount Diablo Thrust",
                "net_slip_rate": "(1.55,0.8,2.22)",
                "average_dip": "(38,,)",
                "average_rake": "(90.0,,)",
                "upper_seis_depth": "(8.0,,)",
                "lower_seis_depth": "(16.0,,)",
                "slip_type": "Reverse",
                "catalog_name": "UCERF3",
            },
            # MultiLineString trace that dips into the AOI.
            "geometry": {
                "type": "MultiLineString",
                "coordinates": [
                    [[-122.45, 37.55, 0.0], [-122.35, 37.70, 0.0]],
                    [[-122.35, 37.70, 0.0], [-122.25, 37.85, 0.0]],
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "name": "Zero-slip creep segment",
                "net_slip_rate": "(0.0,,)",
                "average_dip": "(90,,)",
                "average_rake": "(180.0,,)",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[-122.40, 37.60], [-122.38, 37.62]],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "name": "Degenerate one-point",
                "net_slip_rate": "(5.0,,)",
                "average_dip": "(90,,)",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[-122.40, 37.60]],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "name": "New Madrid (far away)",
                "net_slip_rate": "(2.0,1.0,3.0)",
                "average_dip": "(90,,)",
                "average_rake": "(180.0,,)",
                "upper_seis_depth": "(0.0,,)",
                "lower_seis_depth": "(15.0,,)",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[-89.50, 36.50], [-89.40, 36.60]],
            },
        },
    ],
}


def _make_read_through_injector(store: dict):
    """Self-contained in-memory ``read_through`` replacement (no network/S3).

    Mirrors ``test_fetch_roads_osm``: honors cache-key/path/hit-miss/write
    semantics against ``store`` and short-circuits ``live-no-cache`` exactly
    like the real shim, but never touches boto3.
    """
    from grace2_agent.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    def _patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        now = kw.get("now")
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return _patched


@pytest.fixture()
def _patch_upstream(monkeypatch: pytest.MonkeyPatch):
    """Swap the GEM download for the fixture bytes + the cache for in-memory S3."""
    payload = json.dumps(_FIXTURE).encode("utf-8")
    monkeypatch.setattr(ffs, "_fetch_gem_gaf_bytes", lambda: payload)
    monkeypatch.setattr(ffs, "read_through", _make_read_through_injector({}))


# ===========================================================================
# Property-parse helpers
# ===========================================================================
def test_first_num_parses_triple_strings():
    assert first_num("(15.15,10.49,19.18)") == pytest.approx(15.15)
    assert first_num("(38,,)") == pytest.approx(38.0)
    assert first_num("(0.0,,)") == pytest.approx(0.0)
    # plain number / list / missing
    assert first_num(7) == 7.0
    assert first_num([3.2, 1.0]) == pytest.approx(3.2)
    assert first_num(None, 90.0) == 90.0
    assert first_num("garbage", 90.0) == 90.0


def test_trace_coords_handles_linestring_and_multilinestring():
    ls = {"type": "LineString", "coordinates": [[-122.5, 37.5], [-122.4, 37.6]]}
    assert trace_coords(ls) == [[-122.5, 37.5], [-122.4, 37.6]]
    mls = {
        "type": "MultiLineString",
        "coordinates": [[[-1.0, 2.0, 9.0], [-1.1, 2.1, 9.0]], [[-1.1, 2.1], [-1.2, 2.2]]],
    }
    # Flattened in order, z dropped.
    assert trace_coords(mls) == [[-1.0, 2.0], [-1.1, 2.1], [-1.1, 2.1], [-1.2, 2.2]]
    assert trace_coords({"type": "Point", "coordinates": [0, 0]}) == []


# ===========================================================================
# Parse + bbox-filter
# ===========================================================================
def test_fetch_fault_sources_parses_and_filters(_patch_upstream):
    out = fetch_fault_sources(_SF_BBOX)
    # task #207: a non-empty fetch now returns a renderable LayerURI subclass
    # (the emit_tool_call add_loaded_layer gate fires) that ALSO carries the
    # kinematic source records on its .faults field.
    assert isinstance(out, FaultSourcesResult)
    assert isinstance(out, LayerURI)
    assert out.catalog == "gem"
    # Only the 2 in-AOI, slip>0, >=2-point faults survive (San Andreas + Diablo);
    # zero-slip, one-point, and far-away are all dropped.
    assert out.fault_count == 2
    names = {f["name"] for f in out.faults}
    assert names == {"San Andreas (Peninsula)", "Mount Diablo Thrust"}
    assert out.note is None

    sa = next(f for f in out.faults if f["name"].startswith("San Andreas"))
    # Best-estimate '(best,min,max)' parse.
    assert sa["net_slip_rate_mm_yr"] == pytest.approx(17.0)
    assert sa["dip_deg"] == pytest.approx(90.0)
    assert sa["rake_deg"] == pytest.approx(180.0)
    assert sa["upper_seis_depth_km"] == pytest.approx(0.0)
    assert sa["lower_seis_depth_km"] == pytest.approx(12.0)
    assert sa["slip_type"] == "Dextral"
    assert sa["catalog_name"] == "UCERF3"
    # Geometry preserved as lon/lat trace.
    assert sa["geometry"][0] == [-122.50, 37.50]
    assert len(sa["geometry"]) == 3

    diablo = next(f for f in out.faults if f["name"] == "Mount Diablo Thrust")
    # MultiLineString flattened to a single ordered trace.
    assert len(diablo["geometry"]) == 4
    assert diablo["dip_deg"] == pytest.approx(38.0)


# ===========================================================================
# task #207: the non-empty fetch AUTO-RENDERS the fault traces as a vector
# layer -- the LayerURI return is what the emit_tool_call gate auto-loads, and
# what grounds the narration (the hallucination fix).
# ===========================================================================
def test_fetch_fault_sources_returns_renderable_vector_layer(_patch_upstream):
    out = fetch_fault_sources(_SF_BBOX)
    # A LayerURI return is the ONLY thing the add_loaded_layer gate honours.
    assert isinstance(out, LayerURI)
    assert out.layer_type == "vector"
    assert out.style_preset == FAULT_LINE_STYLE_PRESET
    assert out.role == "context"
    # A renderable, content-addressed uri (the in-memory injector mints s3://).
    assert out.uri and out.uri.startswith("s3://")
    assert out.uri.endswith(".geojson")
    # bbox set so the map zooms to the fault traces.
    assert out.bbox == tuple(_SF_BBOX)
    # Categorical fault-line legend swatch.
    assert out.legend is not None
    assert out.legend.kind == "categorical"
    assert out.name == "Active fault traces (2)"


def test_faults_to_feature_collection_shape(_patch_upstream):
    out = fetch_fault_sources(_SF_BBOX)
    fc = faults_to_feature_collection(out.faults)
    assert fc["type"] == "FeatureCollection"
    # One LineString feature per fault, coordinates = the fault trace.
    assert len(fc["features"]) == 2
    for feat in fc["features"]:
        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "LineString"
        assert len(feat["geometry"]["coordinates"]) >= 2
        assert "name" in feat["properties"]
        assert "net_slip_rate_mm_yr" in feat["properties"]
    # A degenerate (<2-vertex) trace is skipped -- never a fabricated line.
    assert faults_to_feature_collection(
        [{"name": "stub", "geometry": [[-122.5, 37.5]]}]
    )["features"] == []


# ===========================================================================
# Honest empty-AOI degrade (NOT an error, NOT fabricated)
# ===========================================================================
def test_fetch_fault_sources_empty_aoi_degrades(_patch_upstream):
    # An AOI in the open ocean — no fixture fault intersects it.
    out = fetch_fault_sources([-150.0, 10.0, -149.0, 11.0])
    assert out["fault_count"] == 0
    assert out["faults"] == []
    assert out["note"] is not None
    assert "no gem active faults" in out["note"].lower()


# ===========================================================================
# Input validation
# ===========================================================================
def test_fetch_fault_sources_rejects_bad_bbox(_patch_upstream):
    with pytest.raises(FaultSourcesInputError):
        fetch_fault_sources([1, 2, 3])  # not 4 elements
    with pytest.raises(FaultSourcesInputError):
        fetch_fault_sources([10, 0, 5, 1])  # min_lon > max_lon


def test_fetch_fault_sources_rejects_unknown_catalog(_patch_upstream):
    with pytest.raises(FaultSourcesInputError):
        fetch_fault_sources(_SF_BBOX, catalog="usgs_made_up")


# ===========================================================================
# Registration (the tool is a registered @register_tool atomic tool)
# ===========================================================================
def test_fetch_fault_sources_is_registered():
    from grace2_agent.tools import TOOL_REGISTRY

    assert "fetch_fault_sources" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_fault_sources"].metadata
    assert meta.source_class == "gem_active_faults"
    assert meta.cacheable is True
