"""Unit tests for ``compute_flood_extent_skill`` (no network).

Hand-built 10x10 (or smaller) rasters on a projected UTM-like grid (10 m
pixels -> exact 100 m2 = 0.0001 km2 pixel area) so the 2x2 confusion counts,
Hit Rate, False Alarm Ratio, and CSI are exact hand-computed values, not
approximations.

Coverage:
1.  ``test_registered`` -- TOOL_REGISTRY entry, cacheable=False /
    live-no-cache, open_world_hint=False (pure compute, no external API).
2.  ``test_confusion_matrix_hand_computed`` -- 4 quadrants (hit/false_alarm/
    miss/correct_dry, 25 cells each) -> exact areas + H=0.5, F=0.5,
    CSI=1/3.
3.  ``test_model_wet_threshold`` -- a continuous depth raster thresholded at
    a non-default value.
4.  ``test_benchmark_vector_polygon`` -- a polygon benchmark rasterized onto
    the model grid reproduces the SAME confusion as the raster-benchmark
    case covering the identical footprint.
5.  ``test_nodata_excluded_reported`` -- model nodata cells are excluded
    from every count/area and reported (count + area).
6.  ``test_no_overlap_raises`` -- benchmark raster with zero spatial overlap
    with the model grid -> typed FloodExtentSkillNoOverlapError.
7.  ``test_all_dry_csi_null`` -- neither model nor benchmark shows any wet
    pixel -> CSI/hit_rate/false_alarm_ratio all null with caveats.
8.  ``test_unreadable_benchmark_raises`` -- garbage bytes, neither raster
    nor vector -> typed FloodExtentSkillInputError.
9.  ``test_published_context_always_present``.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.compute_flood_extent_skill.compute_flood_extent_skill import (
    FloodExtentSkillInputError,
    FloodExtentSkillNoOverlapError,
    compute_flood_extent_skill,
)

N = 10
RES = 10.0  # meters
X0, Y0 = 500000.0, 4000000.0  # UTM-like origin (top-left)
CRS = "EPSG:32611"
PIXEL_AREA_KM2 = (RES * RES) / 1.0e6  # 0.0001


def _write_raster(
    path: str,
    data: np.ndarray,
    nodata: float | None = None,
    crs: str = CRS,
    dtype: str = "float64",
) -> str:
    transform = from_origin(X0, Y0, RES, RES)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data.astype(dtype), 1)
    return path


def _quadrant_model_benchmark() -> tuple[np.ndarray, np.ndarray]:
    """4 quadrants of a 10x10 grid: TL=hit, TR=false_alarm, BL=miss, BR=correct_dry."""
    model = np.zeros((N, N), dtype="float64")
    bench = np.zeros((N, N), dtype="float64")
    half = N // 2
    model[:half, :half] = 1.0  # TL model wet
    bench[:half, :half] = 1.0  # TL benchmark wet -> HIT
    model[:half, half:] = 1.0  # TR model wet
    bench[:half, half:] = 0.0  # TR benchmark dry -> FALSE ALARM
    model[half:, :half] = 0.0  # BL model dry
    bench[half:, :half] = 1.0  # BL benchmark wet -> MISS
    model[half:, half:] = 0.0  # BR model dry
    bench[half:, half:] = 0.0  # BR benchmark dry -> CORRECT DRY
    return model, bench


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_flood_extent_skill"]
    assert entry.fn is compute_flood_extent_skill
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is False


def test_confusion_matrix_hand_computed(tmp_path) -> None:
    model, bench = _quadrant_model_benchmark()
    model_uri = _write_raster(str(tmp_path / "model.tif"), model)
    bench_uri = _write_raster(str(tmp_path / "bench.tif"), bench)

    result = compute_flood_extent_skill(
        model_extent_uri=model_uri, benchmark_extent_uri=bench_uri
    )

    counts = result["confusion_counts"]
    assert counts == {"hit": 25, "false_alarm": 25, "miss": 25, "correct_dry": 25}
    assert result["n_pixels_compared"] == 100

    assert result["hit_area_km2"] == pytest.approx(25 * PIXEL_AREA_KM2)
    assert result["false_alarm_area_km2"] == pytest.approx(25 * PIXEL_AREA_KM2)
    assert result["miss_area_km2"] == pytest.approx(25 * PIXEL_AREA_KM2)
    assert result["correct_dry_area_km2"] == pytest.approx(25 * PIXEL_AREA_KM2)

    # H = hit/(hit+miss) = 25/50 = 0.5; F = false_alarm/(hit+false_alarm) = 0.5
    # CSI = hit/(hit+miss+false_alarm) = 25/75 = 1/3
    assert result["hit_rate"] == pytest.approx(0.5)
    assert result["false_alarm_ratio"] == pytest.approx(0.5)
    assert result["CSI"] == pytest.approx(1.0 / 3.0)

    assert result["resample_method"] == "nearest"
    assert result["benchmark_source_type"] == "raster"
    assert result["nodata_excluded"] == {"count": 0, "area_km2": 0.0}
    assert "CSI" in result["published_context"]


def test_model_wet_threshold(tmp_path) -> None:
    # Continuous depth raster: 0.0 / 0.5 / 2.0 -- threshold at 1.0 so only
    # the 2.0 cells count as wet.
    model = np.zeros((N, N), dtype="float64")
    model[0:2, :] = 0.5  # below threshold -> stays dry
    model[2:4, :] = 2.0  # above threshold -> wet
    bench = np.zeros((N, N), dtype="float64")
    bench[2:4, :] = 1.0  # benchmark agrees on the same rows

    model_uri = _write_raster(str(tmp_path / "model_depth.tif"), model)
    bench_uri = _write_raster(str(tmp_path / "bench.tif"), bench)

    result = compute_flood_extent_skill(
        model_extent_uri=model_uri,
        benchmark_extent_uri=bench_uri,
        model_wet_threshold=1.0,
    )
    # Rows 2-3 (20 cells) are hits; rows 0-1 (0.5, below threshold) are
    # correct_dry against a dry benchmark; the rest is correct_dry too.
    counts = result["confusion_counts"]
    assert counts["hit"] == 20
    assert counts["false_alarm"] == 0
    assert counts["miss"] == 0
    assert counts["correct_dry"] == 80


def test_benchmark_vector_polygon(tmp_path) -> None:
    model, bench_raster = _quadrant_model_benchmark()
    model_uri = _write_raster(str(tmp_path / "model.tif"), model)

    # The TL+BL quadrant (left half, benchmark-wet columns 0..4) as a polygon
    # in the model's CRS, covering exactly the same "wet" cells as bench_raster's
    # left half (columns 0-4, all rows) -- TL rows 0-4 + BL rows 5-9.
    half = N // 2
    minx = X0
    maxx = X0 + half * RES
    miny = Y0 - N * RES
    maxy = Y0
    poly = box(minx, miny, maxx, maxy)
    gdf = gpd.GeoDataFrame({"geometry": [poly]}, crs=CRS)
    poly_path = str(tmp_path / "bench.fgb")
    gdf.to_file(poly_path, driver="FlatGeobuf", engine="pyogrio")

    result = compute_flood_extent_skill(
        model_extent_uri=model_uri, benchmark_extent_uri=poly_path
    )
    assert result["benchmark_source_type"] == "vector_polygon"
    assert result["resample_method"] == "rasterize"
    # Left half wet in benchmark (cols 0-4): TL (model wet) -> hit (25);
    # BL (model dry) -> miss (25). Right half dry benchmark: TR (model wet)
    # -> false_alarm (25); BR (model dry) -> correct_dry (25). Same as the
    # raster-benchmark quadrant case.
    counts = result["confusion_counts"]
    assert counts == {"hit": 25, "false_alarm": 25, "miss": 25, "correct_dry": 25}


def test_nodata_excluded_reported(tmp_path) -> None:
    model, bench = _quadrant_model_benchmark()
    # Blank out 3 model cells in the TL (hit) quadrant as nodata.
    model[0, 0] = -9999.0
    model[0, 1] = -9999.0
    model[1, 0] = -9999.0
    model_uri = _write_raster(str(tmp_path / "model_nd.tif"), model, nodata=-9999.0)
    bench_uri = _write_raster(str(tmp_path / "bench.tif"), bench)

    result = compute_flood_extent_skill(
        model_extent_uri=model_uri, benchmark_extent_uri=bench_uri
    )
    assert result["nodata_excluded"]["count"] == 3
    assert result["nodata_excluded"]["area_km2"] == pytest.approx(3 * PIXEL_AREA_KM2)
    assert result["n_pixels_compared"] == 97
    # The 3 excluded cells would have been hits -- hit count drops by 3.
    assert result["confusion_counts"]["hit"] == 22


def test_no_overlap_raises(tmp_path) -> None:
    model = np.ones((N, N), dtype="float64")
    model_uri = _write_raster(str(tmp_path / "model.tif"), model)

    # Benchmark raster located far away (no bbox overlap with the model grid).
    far_transform = from_origin(X0 + 100_000.0, Y0 + 100_000.0, RES, RES)
    bench_path = str(tmp_path / "bench_far.tif")
    with rasterio.open(
        bench_path,
        "w",
        driver="GTiff",
        height=N,
        width=N,
        count=1,
        dtype="float64",
        crs=CRS,
        transform=far_transform,
    ) as dst:
        dst.write(np.ones((N, N), dtype="float64"), 1)

    with pytest.raises(FloodExtentSkillNoOverlapError):
        compute_flood_extent_skill(model_extent_uri=model_uri, benchmark_extent_uri=bench_path)


def test_all_dry_csi_null(tmp_path) -> None:
    model = np.zeros((N, N), dtype="float64")
    bench = np.zeros((N, N), dtype="float64")
    model_uri = _write_raster(str(tmp_path / "model.tif"), model)
    bench_uri = _write_raster(str(tmp_path / "bench.tif"), bench)

    result = compute_flood_extent_skill(model_extent_uri=model_uri, benchmark_extent_uri=bench_uri)
    assert result["CSI"] is None
    assert result["hit_rate"] is None
    assert result["false_alarm_ratio"] is None
    assert result["confusion_counts"]["correct_dry"] == 100
    caveat_text = " ".join(result["caveats"])
    assert "CSI is null" in caveat_text
    assert "hit_rate is null" in caveat_text
    assert "false_alarm_ratio is null" in caveat_text


def test_unreadable_benchmark_raises(tmp_path) -> None:
    model = np.ones((N, N), dtype="float64")
    model_uri = _write_raster(str(tmp_path / "model.tif"), model)
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"not a raster or vector file at all")

    with pytest.raises(FloodExtentSkillInputError):
        compute_flood_extent_skill(model_extent_uri=model_uri, benchmark_extent_uri=str(garbage))


def test_published_context_always_present(tmp_path) -> None:
    model, bench = _quadrant_model_benchmark()
    model_uri = _write_raster(str(tmp_path / "model.tif"), model)
    bench_uri = _write_raster(str(tmp_path / "bench.tif"), bench)
    result = compute_flood_extent_skill(model_extent_uri=model_uri, benchmark_extent_uri=bench_uri)
    assert result["published_context"]
    assert "seamlesswave" in result["published_context"].lower()
