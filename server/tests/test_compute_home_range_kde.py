"""Unit tests for ``compute_home_range_kde`` atomic tool.

Compute / shape correctness on SYNTHETIC clustered track points (no Movebank key
needed), the honest-empty (too-few-points) path, and input validation. The
``_compute_home_range_bytes`` core is exercised directly so no S3 round-trip is
needed in CI; ``compute_home_range_kde`` (the registered tool) is exercised with
``TRID3NT_CACHE_BUCKET`` pointed at a temp dir? — no: read_through is S3-only, so
the registered-tool path is covered by the prototype's live S3 round-trip and
here we test the deterministic core + validation surface.

Coverage:
 1. ``test_two_cluster_isopleths_monotonic`` — 50% area < 95% area, both > 0;
    the core (50%) sits inside the home range (95%).
 2. ``test_individual_filter`` — filtering to one animal uses only its fixes.
 3. ``test_bandwidth_override_changes_area`` — a larger metre bandwidth smooths
    (enlarges) the home range.
 4. ``test_output_schema_and_crs`` — output FGB is EPSG:4326 (Multi)Polygons
    with the documented attribute columns.
 5. ``test_linestring_input_exploded`` — a LineString (track-line) input is
    accepted (vertices exploded) and yields polygons.
 6. ``test_too_few_points_honest_empty`` — < _MIN_POINTS raises
    TOO_FEW_POINTS (no fabricated polygon).
 7. ``test_degenerate_collinear_honest_empty`` — co-linear fixes raise
    TOO_FEW_POINTS (singular covariance), not a polygon.
 8. ``test_empty_layer_raises`` — a zero-feature layer raises EMPTY_LAYER.
 9. ``test_bad_isopleth_raises`` / ``test_bad_bandwidth_raises`` — input
    validation typed errors.
10. ``test_estimate_payload_mb`` — payload estimator scales with isopleth count
    and is tiny.
11. ``test_registered_metadata`` — registered with cacheable static-30d
    home_range_kde metadata + the payload estimator name.
"""

from __future__ import annotations

import tempfile

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.compute_home_range_kde import (
    HomeRangeKDEError,
    _compute_home_range_bytes,
    _local_utm_epsg,
    _validate_bandwidth,
    _validate_isopleths,
    compute_home_range_kde,
    estimate_payload_mb,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _two_cluster_points(seed: int = 7) -> str:
    """Write a clustered point FGB (two activity centers) and return its path."""
    rng = np.random.default_rng(seed)
    ca = (-100.78, 46.81)
    cb = (-100.70, 46.86)
    a = rng.multivariate_normal(ca, [[2.5e-4, 0], [0, 2.5e-4]], size=300)
    b = rng.multivariate_normal(cb, [[1.0e-4, 0], [0, 1.0e-4]], size=150)
    pts = np.vstack([a, b])
    gdf = gpd.GeoDataFrame(
        {
            "individual_id": ["A"] * 300 + ["B"] * 150,
            "timestamp": ["2026-06-15T12:00:00Z"] * len(pts),
            "sensor_type_id": [653] * len(pts),
            "study_id": [42] * len(pts),
        },
        geometry=[Point(lon, lat) for lon, lat in pts],
        crs="EPSG:4326",
    )
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="hr_test_")
    f.close()
    gdf.to_file(f.name, driver="FlatGeobuf", engine="pyogrio")
    return f.name


def _read_fgb_bytes(data: bytes) -> "gpd.GeoDataFrame":
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    f.write(data)
    f.close()
    return gpd.read_file(f.name)


# ---------------------------------------------------------------------------
# 1. Two-cluster isopleths: monotonic, nested, positive area
# ---------------------------------------------------------------------------


def test_two_cluster_isopleths_monotonic():
    pts = _two_cluster_points()
    fgb, summary = _compute_home_range_bytes(
        pts, isopleths=(50.0, 95.0), bandwidth_m=None, grid_size=180,
        individual_id=None,
    )
    areas = {iso["pct"]: iso["area_km2"] for iso in summary["isopleths"]}
    assert set(areas) == {50.0, 95.0}
    assert areas[50.0] > 0.0
    assert areas[95.0] > 0.0
    # The core area must be smaller than the full home range.
    assert areas[50.0] < areas[95.0]
    assert summary["n_points"] == 450

    gdf = _read_fgb_bytes(fgb)
    assert len(gdf) == 2
    # The 50% isopleth geometry must be (almost) contained by the 95% one.
    g50 = gdf[gdf.isopleth_pct == 50.0].geometry.iloc[0]
    g95 = gdf[gdf.isopleth_pct == 95.0].geometry.iloc[0]
    # Use a tiny buffer to tolerate grid discretization on the boundary.
    assert g95.buffer(1e-4).contains(g50.intersection(g95).buffer(-1e-9)) or \
        g50.intersection(g95).area / g50.area > 0.95


# ---------------------------------------------------------------------------
# 2. Per-individual filter
# ---------------------------------------------------------------------------


def test_individual_filter():
    pts = _two_cluster_points()
    _, summary_all = _compute_home_range_bytes(
        pts, isopleths=(95.0,), bandwidth_m=None, grid_size=150, individual_id=None
    )
    _, summary_a = _compute_home_range_bytes(
        pts, isopleths=(95.0,), bandwidth_m=None, grid_size=150, individual_id="A"
    )
    assert summary_all["n_points"] == 450
    assert summary_a["n_points"] == 300
    assert summary_a["individual_id"] == "A"
    # Single-cluster (A only) home range is smaller than the two-cluster pooled.
    area_all = summary_all["isopleths"][0]["area_km2"]
    area_a = summary_a["isopleths"][0]["area_km2"]
    assert area_a < area_all


# ---------------------------------------------------------------------------
# 3. Bandwidth override changes the area (larger bw -> larger/smoother range)
# ---------------------------------------------------------------------------


def test_bandwidth_override_changes_area():
    pts = _two_cluster_points()
    _, s_small = _compute_home_range_bytes(
        pts, isopleths=(95.0,), bandwidth_m=300.0, grid_size=160, individual_id=None
    )
    _, s_large = _compute_home_range_bytes(
        pts, isopleths=(95.0,), bandwidth_m=2000.0, grid_size=160, individual_id=None
    )
    a_small = s_small["isopleths"][0]["area_km2"]
    a_large = s_large["isopleths"][0]["area_km2"]
    assert a_small > 0 and a_large > 0
    # A wider kernel smooths the UD outward -> a larger home range.
    assert a_large > a_small


# ---------------------------------------------------------------------------
# 4. Output schema + CRS
# ---------------------------------------------------------------------------


def test_output_schema_and_crs():
    pts = _two_cluster_points()
    fgb, _ = _compute_home_range_bytes(
        pts, isopleths=(50.0, 95.0), bandwidth_m=None, grid_size=150,
        individual_id=None,
    )
    gdf = _read_fgb_bytes(fgb)
    assert str(gdf.crs).upper() in {"EPSG:4326", "WGS84"}
    for col in ("isopleth_pct", "area_km2", "n_points", "individual_id"):
        assert col in gdf.columns, f"missing output column {col}"
    assert set(gdf.geom_type).issubset({"Polygon", "MultiPolygon"})


# ---------------------------------------------------------------------------
# 5. LineString (track-line) input is exploded to vertices
# ---------------------------------------------------------------------------


def test_linestring_input_exploded():
    rng = np.random.default_rng(3)
    coords = rng.multivariate_normal(
        (-100.75, 46.83), [[3e-4, 0], [0, 3e-4]], size=120
    )
    gdf = gpd.GeoDataFrame(
        {"individual_id": ["X"]},
        geometry=[LineString([(lon, lat) for lon, lat in coords])],
        crs="EPSG:4326",
    )
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    f.close()
    gdf.to_file(f.name, driver="FlatGeobuf", engine="pyogrio")

    fgb, summary = _compute_home_range_bytes(
        f.name, isopleths=(95.0,), bandwidth_m=None, grid_size=140,
        individual_id=None,
    )
    # 120 vertices exploded from the single LineString feature.
    assert summary["n_points"] == 120
    out = _read_fgb_bytes(fgb)
    assert len(out) == 1
    assert out.iloc[0].area_km2 > 0.0


# ---------------------------------------------------------------------------
# 6. Too-few-points honest empty
# ---------------------------------------------------------------------------


def test_too_few_points_honest_empty():
    gdf = gpd.GeoDataFrame(
        geometry=[Point(-100.78, 46.81), Point(-100.77, 46.82),
                  Point(-100.76, 46.80)],
        crs="EPSG:4326",
    )
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    f.close()
    gdf.to_file(f.name, driver="FlatGeobuf", engine="pyogrio")
    with pytest.raises(HomeRangeKDEError) as ei:
        _compute_home_range_bytes(
            f.name, isopleths=(95.0,), bandwidth_m=None, grid_size=100,
            individual_id=None,
        )
    assert ei.value.error_code == "TOO_FEW_POINTS"


# ---------------------------------------------------------------------------
# 7. Degenerate coincident set -> singular covariance -> honest empty
# ---------------------------------------------------------------------------


def test_degenerate_coincident_honest_empty():
    # All fixes at one location (zero spatial spread) -> singular 2D covariance.
    # gaussian_kde raises LinAlgError, which the tool maps to TOO_FEW_POINTS
    # rather than fabricating a home-range polygon for an animal that never
    # moved.
    pts = [Point(-100.80, 46.80) for _ in range(40)]
    gdf = gpd.GeoDataFrame(geometry=pts, crs="EPSG:4326")
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    f.close()
    gdf.to_file(f.name, driver="FlatGeobuf", engine="pyogrio")
    with pytest.raises(HomeRangeKDEError) as ei:
        _compute_home_range_bytes(
            f.name, isopleths=(95.0,), bandwidth_m=None, grid_size=100,
            individual_id=None,
        )
    assert ei.value.error_code == "TOO_FEW_POINTS"


# ---------------------------------------------------------------------------
# 8. Empty layer
# ---------------------------------------------------------------------------


def test_empty_layer_raises():
    gdf = gpd.GeoDataFrame({"individual_id": []}, geometry=[], crs="EPSG:4326")
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    f.close()
    gdf.to_file(f.name, driver="FlatGeobuf", engine="pyogrio")
    with pytest.raises(HomeRangeKDEError) as ei:
        _compute_home_range_bytes(
            f.name, isopleths=(95.0,), bandwidth_m=None, grid_size=100,
            individual_id=None,
        )
    assert ei.value.error_code in {"EMPTY_LAYER", "NO_POINTS_INPUT"}


# ---------------------------------------------------------------------------
# 9. Input validation
# ---------------------------------------------------------------------------


def test_bad_isopleth_raises():
    pts = _two_cluster_points()
    for bad in ([0], [101], [-5], [50, 150]):
        with pytest.raises(HomeRangeKDEError) as ei:
            compute_home_range_kde(pts, isopleths=bad)
        assert ei.value.error_code == "BAD_ISOPLETH"


def test_bad_bandwidth_raises():
    pts = _two_cluster_points()
    for bad in (0, -10, float("nan")):
        with pytest.raises(HomeRangeKDEError) as ei:
            compute_home_range_kde(pts, bandwidth_m=bad)
        assert ei.value.error_code == "BAD_BANDWIDTH"


def test_no_points_uri_raises():
    with pytest.raises(HomeRangeKDEError) as ei:
        compute_home_range_kde("")
    assert ei.value.error_code == "NO_POINTS_INPUT"


def test_validate_isopleths_defaults_and_sort():
    assert _validate_isopleths(None) == (50.0, 95.0)
    assert _validate_isopleths(90) == (90.0,)
    # de-dup + sort ascending
    assert _validate_isopleths([95, 50, 50]) == (50.0, 95.0)


def test_validate_bandwidth():
    assert _validate_bandwidth(None) is None
    assert _validate_bandwidth(500) == 500.0
    with pytest.raises(HomeRangeKDEError):
        _validate_bandwidth(-1)


def test_local_utm_epsg():
    # Bismarck ND -> UTM 14N (EPSG:32614)
    assert _local_utm_epsg(-100.78, 46.81) == 32614
    # Southern hemisphere -> 327xx
    assert _local_utm_epsg(151.2, -33.9) == 32756


# ---------------------------------------------------------------------------
# 10. Payload estimator
# ---------------------------------------------------------------------------


def test_estimate_payload_mb():
    assert estimate_payload_mb() == pytest.approx(0.06, abs=1e-6)
    assert estimate_payload_mb(isopleths=[50, 95, 99]) == pytest.approx(0.09, abs=1e-6)
    # Always tiny (well under the 25 MB warn threshold).
    assert estimate_payload_mb(isopleths=list(range(1, 100))) < 5.0


# ---------------------------------------------------------------------------
# 11. Registered metadata
# ---------------------------------------------------------------------------


def test_registered_metadata():
    assert "compute_home_range_kde" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["compute_home_range_kde"].metadata
    assert meta.cacheable is True
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "home_range_kde"
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"
