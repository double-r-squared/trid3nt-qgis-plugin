"""Tests for analytical Q&A tools (job-0224, sprint-13 Stage 1).

All tests use synthetic in-memory/temp-file data — no network calls.

Coverage:
- summarize_layer_statistics: raster path + vector path + missing-file error.
- count_features_above_threshold: basic counting + property-not-found error.
- aggregate_property_within_zone: spatial join agg (sum/mean/max) + property-not-found.
- Registration: all three tools appear in TOOL_REGISTRY with correct metadata.
- Category membership: all three tools are in PRIMARY_CATEGORY (geographic_primitives).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers to create synthetic fixtures
# ---------------------------------------------------------------------------


def _make_raster(tmp_path: Path, values: np.ndarray, nodata: float | None = None) -> str:
    """Write a single-band GeoTIFF to tmp_path and return its local path."""
    import rasterio
    from rasterio.transform import from_bounds

    path = str(tmp_path / "test_raster.tif")
    height, width = values.shape
    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": values.dtype,
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
    return path


def _make_geojson_points(tmp_path: Path, records: list[dict]) -> str:
    """Write a GeoJSON FeatureCollection of Point features.

    Each record in ``records`` must have "x", "y", and any other attribute keys.
    """
    features = []
    for r in records:
        x = r.pop("x")
        y = r.pop("y")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": r,
        })
    fc = {"type": "FeatureCollection", "features": features}
    path = str(tmp_path / "test_points.geojson")
    with open(path, "w") as f:
        json.dump(fc, f)
    return path


def _make_geojson_polygons(tmp_path: Path, polys: list[list[list[float]]], name_suffix: str = "") -> str:
    """Write a GeoJSON FeatureCollection of Polygon features.

    ``polys`` is a list of coordinate rings (each ring is a list of [x, y] pairs).
    """
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [ring + [ring[0]]],  # close the ring
            },
            "properties": {"zone_id": i},
        }
        for i, ring in enumerate(polys)
    ]
    fc = {"type": "FeatureCollection", "features": features}
    path = str(tmp_path / f"test_polys{name_suffix}.geojson")
    with open(path, "w") as f:
        json.dump(fc, f)
    return path


# ---------------------------------------------------------------------------
# Cache bypass fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_gcs_cache(monkeypatch):
    """Bypass GCS cache writes/reads for unit tests.

    Patches read_through to call fetch_fn directly and wrap the result as a
    ReadThroughResult so all tool code works without a GCS bucket.
    """
    from grace2_agent.tools import cache as cache_module

    class _FakeResult:
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.cache_hit = False

    def _fake_read_through(*, metadata, params, ext, fetch_fn, bucket, storage_client, source_id, **_kw):
        data = fetch_fn()
        return _FakeResult(data)

    monkeypatch.setattr(cache_module, "read_through", _fake_read_through)


# ---------------------------------------------------------------------------
# summarize_layer_statistics — raster
# ---------------------------------------------------------------------------


class TestSummarizeRaster:
    def test_basic_stats(self, tmp_path):
        """Known 3x3 raster yields correct min/max/mean/sum/count."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        arr = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]], dtype=np.float32)
        path = _make_raster(tmp_path, arr)

        result = summarize_layer_statistics(layer_uri=path)

        assert result["layer_type"] == "raster"
        assert result["count"] == 9
        assert result["min"] == pytest.approx(1.0)
        assert result["max"] == pytest.approx(9.0)
        assert result["mean"] == pytest.approx(5.0)
        assert result["sum"] == pytest.approx(45.0)
        assert "distribution" in result
        assert len(result["distribution"]) == 10
        assert result["layer_uri"] == path
        assert "computed_at" in result

    def test_nodata_excluded(self, tmp_path):
        """Nodata pixels are excluded from all statistics."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        arr = np.array([[1.0, -9999.0, 3.0]], dtype=np.float32)
        path = _make_raster(tmp_path, arr, nodata=-9999.0)

        result = summarize_layer_statistics(layer_uri=path)

        assert result["count"] == 2
        assert result["min"] == pytest.approx(1.0)
        assert result["max"] == pytest.approx(3.0)

    def test_all_nodata_returns_zero_count(self, tmp_path):
        """All-nodata raster returns count=0 and None statistics."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        arr = np.full((3, 3), -9999.0, dtype=np.float32)
        path = _make_raster(tmp_path, arr, nodata=-9999.0)

        result = summarize_layer_statistics(layer_uri=path)

        assert result["count"] == 0
        assert result["min"] is None
        assert result["max"] is None
        assert result["mean"] is None
        assert result["sum"] is None
        assert result["distribution"] == []

    def test_distribution_10_bins(self, tmp_path):
        """Histogram always has exactly 10 bins."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        arr = np.linspace(0, 100, 100, dtype=np.float32).reshape(10, 10)
        path = _make_raster(tmp_path, arr)

        result = summarize_layer_statistics(layer_uri=path)

        bins = result["distribution"]
        assert len(bins) == 10
        for b in bins:
            assert "bin_start" in b
            assert "bin_end" in b
            assert "count" in b
            assert b["count"] >= 0

    def test_provenance_fields(self, tmp_path):
        """Result always carries layer_uri and computed_at."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        arr = np.ones((2, 2), dtype=np.float32)
        path = _make_raster(tmp_path, arr)

        result = summarize_layer_statistics(layer_uri=path)

        assert result["layer_uri"] == path
        assert isinstance(result["computed_at"], str)
        assert "T" in result["computed_at"]  # ISO 8601


# ---------------------------------------------------------------------------
# summarize_layer_statistics — vector
# ---------------------------------------------------------------------------


class TestSummarizeVector:
    def test_basic_vector_stats(self, tmp_path):
        """Feature count and per-attribute summary for a simple GeoJSON."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        records = [
            {"x": 0.1, "y": 0.1, "value": 10.0, "label": "a"},
            {"x": 0.5, "y": 0.5, "value": 20.0, "label": "b"},
            {"x": 0.9, "y": 0.9, "value": 30.0, "label": "c"},
        ]
        path = _make_geojson_points(tmp_path, records)

        result = summarize_layer_statistics(layer_uri=path)

        assert result["layer_type"] == "vector"
        assert result["feature_count"] == 3
        assert "attribute_summary" in result
        assert "value" in result["attribute_summary"]
        vs = result["attribute_summary"]["value"]
        assert vs["count"] == 3
        assert vs["min"] == pytest.approx(10.0)
        assert vs["max"] == pytest.approx(30.0)
        assert vs["mean"] == pytest.approx(20.0)
        assert vs["sum"] == pytest.approx(60.0)
        # Non-numeric column should NOT appear in attribute_summary.
        assert "label" not in result["attribute_summary"]

    def test_empty_vector_returns_zero_count(self, tmp_path):
        """Empty GeoJSON feature collection returns feature_count=0."""
        from grace2_agent.tools.analytical_qa import summarize_layer_statistics

        fc = {"type": "FeatureCollection", "features": []}
        path = str(tmp_path / "empty.geojson")
        with open(path, "w") as f:
            json.dump(fc, f)

        result = summarize_layer_statistics(layer_uri=path)

        assert result["layer_type"] == "vector"
        assert result["feature_count"] == 0
        assert result["attribute_summary"] == {}


# ---------------------------------------------------------------------------
# count_features_above_threshold
# ---------------------------------------------------------------------------


class TestCountFeaturesAboveThreshold:
    def test_basic_count(self, tmp_path):
        """Count features where value >= 15 in a 3-feature layer."""
        from grace2_agent.tools.analytical_qa import count_features_above_threshold

        records = [
            {"x": 0.1, "y": 0.1, "damage": 5.0},
            {"x": 0.5, "y": 0.5, "damage": 20.0},
            {"x": 0.9, "y": 0.9, "damage": 30.0},
        ]
        path = _make_geojson_points(tmp_path, records)

        result = count_features_above_threshold(
            layer_uri=path, property="damage", threshold=15.0
        )

        assert result["count"] == 2
        assert result["total"] == 3
        assert result["property"] == "damage"
        assert result["threshold"] == pytest.approx(15.0)
        assert result["layer_uri"] == path
        assert "computed_at" in result

    def test_threshold_inclusive(self, tmp_path):
        """Threshold comparison is >= (inclusive)."""
        from grace2_agent.tools.analytical_qa import count_features_above_threshold

        records = [
            {"x": 0.1, "y": 0.1, "v": 10.0},
            {"x": 0.5, "y": 0.5, "v": 10.0},
            {"x": 0.9, "y": 0.9, "v": 9.9},
        ]
        path = _make_geojson_points(tmp_path, records)

        result = count_features_above_threshold(
            layer_uri=path, property="v", threshold=10.0
        )

        assert result["count"] == 2

    def test_zero_count_not_an_error(self, tmp_path):
        """No features above threshold returns count=0, total=N."""
        from grace2_agent.tools.analytical_qa import count_features_above_threshold

        records = [
            {"x": 0.1, "y": 0.1, "v": 1.0},
            {"x": 0.5, "y": 0.5, "v": 2.0},
        ]
        path = _make_geojson_points(tmp_path, records)

        result = count_features_above_threshold(
            layer_uri=path, property="v", threshold=100.0
        )

        assert result["count"] == 0
        assert result["total"] == 2

    def test_property_not_found_raises(self, tmp_path):
        """Missing property raises AnalyticalQAError with PROPERTY_NOT_FOUND."""
        from grace2_agent.tools.analytical_qa import (
            count_features_above_threshold,
            AnalyticalQAError,
        )

        records = [{"x": 0.1, "y": 0.1, "v": 1.0}]
        path = _make_geojson_points(tmp_path, records)

        with pytest.raises(AnalyticalQAError) as exc:
            count_features_above_threshold(
                layer_uri=path, property="nonexistent_col", threshold=0.0
            )

        assert exc.value.error_code == "PROPERTY_NOT_FOUND"
        assert "nonexistent_col" in str(exc.value)

    def test_all_features_above_threshold(self, tmp_path):
        """count == total when all features are above threshold."""
        from grace2_agent.tools.analytical_qa import count_features_above_threshold

        records = [
            {"x": 0.1, "y": 0.1, "v": 100.0},
            {"x": 0.5, "y": 0.5, "v": 200.0},
        ]
        path = _make_geojson_points(tmp_path, records)

        result = count_features_above_threshold(
            layer_uri=path, property="v", threshold=0.0
        )

        assert result["count"] == result["total"] == 2


# ---------------------------------------------------------------------------
# aggregate_property_within_zone
# ---------------------------------------------------------------------------


class TestAggregatePropertyWithinZone:
    def _make_zone_and_points(self, tmp_path):
        """Build a zone polygon covering the unit square [0,1]x[0,1] and 4 points:
        3 inside the zone + 1 outside."""
        # Zone: unit square polygon.
        zone_ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        zone_path = _make_geojson_polygons(tmp_path, [zone_ring])

        # Points: 3 inside [0,1]^2, 1 outside at [2, 2].
        records = [
            {"x": 0.2, "y": 0.2, "cost": 10.0},
            {"x": 0.5, "y": 0.5, "cost": 20.0},
            {"x": 0.8, "y": 0.8, "cost": 30.0},
            {"x": 2.0, "y": 2.0, "cost": 999.0},  # outside zone
        ]
        value_path = _make_geojson_points(tmp_path, records)
        return value_path, zone_path

    def test_sum_aggregation(self, tmp_path):
        """sum aggregation includes only in-zone features."""
        from grace2_agent.tools.analytical_qa import aggregate_property_within_zone

        value_path, zone_path = self._make_zone_and_points(tmp_path)

        result = aggregate_property_within_zone(
            value_layer_uri=value_path,
            zone_layer_uri=zone_path,
            property="cost",
            agg="sum",
        )

        assert result["agg"] == "sum"
        assert result["n_features"] == 3
        assert result["total_features"] == 4
        assert result["value"] == pytest.approx(60.0)
        assert result["property"] == "cost"
        assert "computed_at" in result

    def test_mean_aggregation(self, tmp_path):
        """mean aggregation returns the average of in-zone feature values."""
        from grace2_agent.tools.analytical_qa import aggregate_property_within_zone

        value_path, zone_path = self._make_zone_and_points(tmp_path)

        result = aggregate_property_within_zone(
            value_layer_uri=value_path,
            zone_layer_uri=zone_path,
            property="cost",
            agg="mean",
        )

        assert result["agg"] == "mean"
        assert result["value"] == pytest.approx(20.0)  # (10+20+30)/3

    def test_max_aggregation(self, tmp_path):
        """max aggregation returns the maximum of in-zone feature values."""
        from grace2_agent.tools.analytical_qa import aggregate_property_within_zone

        value_path, zone_path = self._make_zone_and_points(tmp_path)

        result = aggregate_property_within_zone(
            value_layer_uri=value_path,
            zone_layer_uri=zone_path,
            property="cost",
            agg="max",
        )

        assert result["agg"] == "max"
        assert result["value"] == pytest.approx(30.0)

    def test_no_features_in_zone_returns_zero_sum(self, tmp_path):
        """When no features fall inside the zone, sum returns 0."""
        from grace2_agent.tools.analytical_qa import aggregate_property_within_zone

        # Zone covers [10, 11] x [10, 11] — none of our points are there.
        zone_ring = [[10.0, 10.0], [11.0, 10.0], [11.0, 11.0], [10.0, 11.0]]
        zone_path = _make_geojson_polygons(tmp_path, [zone_ring], name_suffix="_far")

        records = [
            {"x": 0.2, "y": 0.2, "cost": 10.0},
            {"x": 0.5, "y": 0.5, "cost": 20.0},
        ]
        value_path = _make_geojson_points(tmp_path, records)

        result = aggregate_property_within_zone(
            value_layer_uri=value_path,
            zone_layer_uri=zone_path,
            property="cost",
            agg="sum",
        )

        assert result["n_features"] == 0
        assert result["value"] == pytest.approx(0.0)

    def test_property_not_found_raises(self, tmp_path):
        """Missing property raises AnalyticalQAError with PROPERTY_NOT_FOUND."""
        from grace2_agent.tools.analytical_qa import (
            aggregate_property_within_zone,
            AnalyticalQAError,
        )

        zone_ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        zone_path = _make_geojson_polygons(tmp_path, [zone_ring])
        records = [{"x": 0.5, "y": 0.5, "cost": 10.0}]
        value_path = _make_geojson_points(tmp_path, records)

        with pytest.raises(AnalyticalQAError) as exc:
            aggregate_property_within_zone(
                value_layer_uri=value_path,
                zone_layer_uri=zone_path,
                property="wrong_col",
                agg="sum",
            )

        assert exc.value.error_code == "PROPERTY_NOT_FOUND"

    def test_provenance_fields(self, tmp_path):
        """Result carries value_layer_uri, zone_layer_uri, computed_at."""
        from grace2_agent.tools.analytical_qa import aggregate_property_within_zone

        value_path, zone_path = self._make_zone_and_points(tmp_path)

        result = aggregate_property_within_zone(
            value_layer_uri=value_path,
            zone_layer_uri=zone_path,
            property="cost",
            agg="sum",
        )

        assert result["value_layer_uri"] == value_path
        assert result["zone_layer_uri"] == zone_path
        assert isinstance(result["computed_at"], str)


# ---------------------------------------------------------------------------
# Registration & metadata
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_all_three_tools_in_tool_registry(self):
        """All three analytical Q&A tools must appear in TOOL_REGISTRY."""
        from grace2_agent.tools import TOOL_REGISTRY

        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            assert name in TOOL_REGISTRY, f"{name} not in TOOL_REGISTRY"

    def test_metadata_ttl_class(self):
        """All three tools have ttl_class='dynamic-1h' and cacheable=True."""
        from grace2_agent.tools import TOOL_REGISTRY

        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            entry = TOOL_REGISTRY[name]
            assert entry.metadata.ttl_class == "dynamic-1h", (
                f"{name} has ttl_class={entry.metadata.ttl_class!r}"
            )
            assert entry.metadata.cacheable is True, (
                f"{name} has cacheable={entry.metadata.cacheable!r}"
            )

    def test_read_only_hint(self):
        """All three tools declare read_only_hint=True."""
        from grace2_agent.tools import TOOL_REGISTRY

        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            entry = TOOL_REGISTRY[name]
            assert entry.metadata.read_only_hint is True, (
                f"{name} has read_only_hint={entry.metadata.read_only_hint!r}"
            )

    def test_all_three_in_primary_category(self):
        """All three tools are in PRIMARY_CATEGORY under geographic_primitives."""
        from grace2_agent.categories import PRIMARY_CATEGORY

        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            assert name in PRIMARY_CATEGORY, (
                f"{name} not in PRIMARY_CATEGORY"
            )
            assert PRIMARY_CATEGORY[name] == "geographic_primitives", (
                f"{name} has category {PRIMARY_CATEGORY[name]!r}, expected 'geographic_primitives'"
            )

    def test_source_class(self):
        """All three tools share source_class='analytical_qa'."""
        from grace2_agent.tools import TOOL_REGISTRY

        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            entry = TOOL_REGISTRY[name]
            assert entry.metadata.source_class == "analytical_qa", (
                f"{name} has source_class={entry.metadata.source_class!r}"
            )
