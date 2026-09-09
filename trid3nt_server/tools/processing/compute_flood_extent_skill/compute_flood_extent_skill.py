"""``compute_flood_extent_skill`` - modeled vs benchmark wet/dry skill.

The MODEL grid is the reference: a raster benchmark resamples onto it NEAREST,
never interpolated, and nodata is excluded from every count, never read as dry.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

import numpy as np

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "compute_flood_extent_skill",
    "FloodExtentSkillError",
    "FloodExtentSkillInputError",
    "FloodExtentSkillNoOverlapError",
    "FloodExtentSkillUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_flood_extent_skill.compute_flood_extent_skill")


# ---------------------------------------------------------------------------
# Error types (typed-error surface).
# ---------------------------------------------------------------------------


class FloodExtentSkillError(RuntimeError):
    """Base class for compute_flood_extent_skill failures."""

    error_code: str = "FLOOD_EXTENT_SKILL_ERROR"
    retryable: bool = True


class FloodExtentSkillInputError(FloodExtentSkillError):
    """Bad inputs -- unreadable raster/vector, missing CRS, bad threshold."""

    error_code = "FLOOD_EXTENT_SKILL_INPUT_INVALID"
    retryable = False


class FloodExtentSkillNoOverlapError(FloodExtentSkillError):
    """Model and benchmark rasters share zero valid (non-nodata) overlap."""

    error_code = "FLOOD_EXTENT_SKILL_NO_OVERLAP"
    retryable = False


class FloodExtentSkillUpstreamError(FloodExtentSkillError):
    """Staging (S3 download), GDAL read, reproject, or rasterize failed."""

    error_code = "FLOOD_EXTENT_SKILL_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_PUBLISHED_CONTEXT = (
    "CSI ~0.5-0.7 is a general 'good agreement' convention, not "
    "agency-codified (SEAMLESS-WAVE: https://www.seamlesswave.com/metrics.html). "
    "Published flood-model-vs-satellite CSI ranges ~0.29 (small/urban basins, "
    "<50 km2) to ~0.75 (basins >1000 km2, or vs. discharge-forced GloFAS) "
    "depending on event/basin scale (EGUsphere 2025 preprint: "
    "https://egusphere.copernicus.org/preprints/2025/egusphere-2025-4387/). "
    "These are empirical reference points, not a pass/fail target."
)

_METADATA = AtomicToolMetadata(
    name="compute_flood_extent_skill",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Staging.
# ---------------------------------------------------------------------------


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise FloodExtentSkillUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise FloodExtentSkillInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise FloodExtentSkillInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri


def _validate_threshold(value: Any, label: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise FloodExtentSkillInputError(
            f"{label} must be numeric; got {value!r}"
        ) from exc
    if not math.isfinite(v):
        raise FloodExtentSkillInputError(f"{label} must be finite; got {value!r}")
    return v


# ---------------------------------------------------------------------------
# Model raster loading.
# ---------------------------------------------------------------------------


def _load_model_raster(
    model_local: str,
) -> tuple[np.ndarray, np.ndarray, Any, Any, Any, tuple[int, int]]:
    """Open the model raster; return (band, valid_mask, transform, crs, bounds, shape)."""
    import rasterio

    try:
        src = rasterio.open(model_local)
    except Exception as exc:  # noqa: BLE001
        raise FloodExtentSkillInputError(
            f"could not open model_extent_uri: {exc}"
        ) from exc
    try:
        if src.crs is None:
            raise FloodExtentSkillInputError("model_extent_uri raster carries no CRS.")
        band = src.read(1).astype(np.float64)
        nodata = src.nodata
        mask = np.ones(band.shape, dtype=bool)
        if nodata is not None:
            if isinstance(nodata, float) and math.isnan(nodata):
                mask = ~np.isnan(band)
            elif math.isfinite(float(nodata)):
                mask = band != float(nodata)
        transform = src.transform
        crs = src.crs
        bounds = src.bounds
        shape = band.shape
    finally:
        src.close()
    return band, mask, transform, crs, bounds, shape


def _is_raster(local_path: str) -> bool:
    """Best-effort raster-vs-vector sniff: try opening as a raster first."""
    import rasterio

    try:
        with rasterio.open(local_path):
            return True
    except Exception:  # noqa: BLE001
        return False


def _load_benchmark_as_mask(
    benchmark_local: str,
    model_transform: Any,
    model_crs: Any,
    model_shape: tuple[int, int],
    benchmark_wet_threshold: float,
    notes: list[str],
) -> tuple[np.ndarray, np.ndarray, str, str]:
    """``(wet_mask, valid_mask, source_type, resample_method)`` on the MODEL grid;
    ``resample_method`` is always populated so the caller can see what was done.
    """
    if _is_raster(benchmark_local):
        import rasterio
        from rasterio.warp import Resampling, reproject

        try:
            with rasterio.open(benchmark_local) as src:
                if src.crs is None:
                    raise FloodExtentSkillInputError(
                        "benchmark_extent_uri raster carries no CRS."
                    )
                src_band = src.read(1).astype(np.float64)
                src_nodata = src.nodata
                dst = np.full(model_shape, np.nan, dtype=np.float64)
                reproject(
                    source=src_band,
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=(
                        float(src_nodata) if src_nodata is not None else None
                    ),
                    dst_transform=model_transform,
                    dst_crs=model_crs,
                    dst_nodata=np.nan,
                    resampling=Resampling.nearest,
                )
        except FloodExtentSkillError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FloodExtentSkillUpstreamError(
                f"benchmark raster reproject/resample onto the model grid "
                f"failed: {exc}"
            ) from exc
        valid = ~np.isnan(dst)
        wet = np.zeros(model_shape, dtype=bool)
        wet[valid] = dst[valid] > benchmark_wet_threshold
        notes.append(
            "Benchmark is a RASTER; reprojected/resampled onto the model "
            "raster's grid with nearest-neighbor resampling (categorical "
            f"wet/dry data -- never interpolated), threshold "
            f"value>{benchmark_wet_threshold} = wet."
        )
        return wet, valid, "raster", "nearest"

    import geopandas as gpd
    from rasterio.features import rasterize

    try:
        gdf = gpd.read_file(benchmark_local)
    except Exception as exc:  # noqa: BLE001
        raise FloodExtentSkillInputError(
            f"benchmark_extent_uri is neither a readable raster nor a "
            f"readable vector layer: {exc}"
        ) from exc
    if len(gdf) == 0:
        raise FloodExtentSkillInputError(
            "benchmark_extent_uri vector layer contains zero features."
        )
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
        notes.append("Benchmark vector carried no CRS; assumed EPSG:4326.")
    try:
        gdf = gdf.to_crs(model_crs)
    except Exception as exc:  # noqa: BLE001
        raise FloodExtentSkillUpstreamError(
            f"benchmark vector reprojection to the model CRS failed: {exc}"
        ) from exc

    try:
        burned = rasterize(
            [(geom, 1) for geom in gdf.geometry if geom is not None and not geom.is_empty],
            out_shape=model_shape,
            transform=model_transform,
            fill=0,
            dtype=np.uint8,
        )
    except Exception as exc:  # noqa: BLE001
        raise FloodExtentSkillUpstreamError(
            f"benchmark polygon rasterize onto the model grid failed: {exc}"
        ) from exc

    wet = burned == 1
    valid = np.ones(model_shape, dtype=bool)  # a polygon covers the full domain (in/out)
    notes.append(
        "Benchmark is a VECTOR polygon layer; rasterized directly onto the "
        "model raster's grid (polygon interior = wet, exterior = dry -- no "
        "resampling/interpolation involved)."
    )
    return wet, valid, "vector_polygon", "rasterize"


# ---------------------------------------------------------------------------
# Pixel-area helper.
# ---------------------------------------------------------------------------


def _pixel_area_km2_grid(transform: Any, crs: Any, shape: tuple[int, int]) -> np.ndarray:
    """Per-pixel area in km2: constant on a projected CRS, a per-row geodesic
    approximation on a geographic one - documented, not exact.
    """
    height, width = shape
    px_w = abs(transform.a)
    px_h = abs(transform.e)
    if crs is not None and crs.is_geographic:
        from pyproj import Geod

        row_idx = np.arange(height, dtype=np.float64)
        lat = transform.f + (row_idx + 0.5) * transform.e
        geod = Geod(ellps="WGS84")
        lon0 = np.full(lat.shape, transform.c)
        cell_w_m = geod.inv(lon0, lat, lon0 + px_w, lat)[2]
        cell_h_m = geod.inv(lon0, lat - 0.5 * px_h, lon0, lat + 0.5 * px_h)[2]
        row_area_km2 = cell_w_m * cell_h_m / 1.0e6
        return np.repeat(row_area_km2[:, np.newaxis], width, axis=1)
    # A projected CRS is assumed to carry linear metre units.
    area_km2 = (px_w * px_h) / 1.0e6
    return np.full(shape, area_km2, dtype=np.float64)


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(_METADATA)
def compute_flood_extent_skill(
    model_extent_uri: str,
    benchmark_extent_uri: str,
    model_wet_threshold: float = 0.0,
    benchmark_wet_threshold: float = 0.0,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Score a modeled flood-extent raster against a benchmark wet/dry extent.

    Use when you want to know whether a flood model's WET/DRY footprint matches
    a reference extent - a satellite (SAR) wet mask, an observed flood-extent
    polygon, any benchmark raster or polygon. Returns the 2x2 categorical
    confusion in km2 plus Hit Rate, False Alarm Ratio and CSI. Not for
    continuous time series (``compute_skill_metrics``) or per-pixel residuals
    (``compute_model_residuals``).

    Params:
        model_extent_uri: the MODELED single-band raster, binarized by
            ``model_wet_threshold``; pass an already-binary 0/1 raster with the
            default 0.0 to use it as-is.
        benchmark_extent_uri: the BENCHMARK, either a single-band raster
            binarized by ``benchmark_wet_threshold`` or a vector polygon whose
            interior is wet. Detected automatically.

    ``published_context`` rides along as literature reference ranges, never as a
    pass/fail. Unreadable inputs and zero valid overlap are typed errors.
    """
    if not isinstance(model_extent_uri, str) or not model_extent_uri.strip():
        raise FloodExtentSkillInputError(
            f"model_extent_uri must be a non-empty URI string; got {model_extent_uri!r}"
        )
    if not isinstance(benchmark_extent_uri, str) or not benchmark_extent_uri.strip():
        raise FloodExtentSkillInputError(
            f"benchmark_extent_uri must be a non-empty URI string; got "
            f"{benchmark_extent_uri!r}"
        )
    model_thr = _validate_threshold(model_wet_threshold, "model_wet_threshold")
    bench_thr = _validate_threshold(benchmark_wet_threshold, "benchmark_wet_threshold")

    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="trid3nt_flood_extent_skill_") as tmpdir:
        model_local = _stage_uri_local(model_extent_uri, tmpdir, "model")
        band, model_valid, transform, crs, _bounds, shape = _load_model_raster(model_local)

        benchmark_local = _stage_uri_local(benchmark_extent_uri, tmpdir, "benchmark")
        bench_wet, bench_valid, source_type, resample_method = _load_benchmark_as_mask(
            benchmark_local, transform, crs, shape, bench_thr, notes
        )

    model_wet = band > model_thr

    compared = model_valid & bench_valid
    n_compared = int(compared.sum())
    n_excluded = int((~compared).sum())

    pixel_area_grid = _pixel_area_km2_grid(transform, crs, shape)

    if n_compared == 0:
        raise FloodExtentSkillNoOverlapError(
            "model and benchmark share zero valid (non-nodata, in-grid) "
            "overlapping pixels -- nothing to compare."
        )

    hit_mask = compared & model_wet & bench_wet
    false_alarm_mask = compared & model_wet & ~bench_wet
    miss_mask = compared & ~model_wet & bench_wet
    correct_dry_mask = compared & ~model_wet & ~bench_wet

    hit_count = int(hit_mask.sum())
    false_alarm_count = int(false_alarm_mask.sum())
    miss_count = int(miss_mask.sum())
    correct_dry_count = int(correct_dry_mask.sum())

    hit_area_km2 = float(pixel_area_grid[hit_mask].sum())
    false_alarm_area_km2 = float(pixel_area_grid[false_alarm_mask].sum())
    miss_area_km2 = float(pixel_area_grid[miss_mask].sum())
    correct_dry_area_km2 = float(pixel_area_grid[correct_dry_mask].sum())
    excluded_area_km2 = float(pixel_area_grid[~compared].sum())

    denom_h = hit_count + miss_count
    denom_f = hit_count + false_alarm_count
    denom_csi = hit_count + miss_count + false_alarm_count

    hit_rate = round(hit_count / denom_h, 6) if denom_h > 0 else None
    false_alarm_ratio = round(false_alarm_count / denom_f, 6) if denom_f > 0 else None
    csi = round(hit_count / denom_csi, 6) if denom_csi > 0 else None

    caveats: list[str] = []
    if denom_h == 0:
        caveats.append(
            "hit_rate is null: no benchmark-wet pixels in the compared "
            "footprint (hit+miss == 0)."
        )
    if denom_f == 0:
        caveats.append(
            "false_alarm_ratio is null: no model-wet pixels in the compared "
            "footprint (hit+false_alarm == 0)."
        )
    if denom_csi == 0:
        caveats.append(
            "CSI is null: neither the model nor the benchmark shows any wet "
            "pixel in the compared footprint (all correct-dry)."
        )

    logger.info(
        "compute_flood_extent_skill: model=%s benchmark=%s (%s) "
        "n_compared=%d hit=%d false_alarm=%d miss=%d CSI=%s",
        model_extent_uri,
        benchmark_extent_uri,
        source_type,
        n_compared,
        hit_count,
        false_alarm_count,
        miss_count,
        csi,
    )

    return {
        "hit_area_km2": round(hit_area_km2, 6),
        "false_alarm_area_km2": round(false_alarm_area_km2, 6),
        "miss_area_km2": round(miss_area_km2, 6),
        "correct_dry_area_km2": round(correct_dry_area_km2, 6),
        "hit_rate": hit_rate,
        "false_alarm_ratio": false_alarm_ratio,
        "CSI": csi,
        "confusion_counts": {
            "hit": hit_count,
            "false_alarm": false_alarm_count,
            "miss": miss_count,
            "correct_dry": correct_dry_count,
        },
        "n_pixels_compared": n_compared,
        "resample_method": resample_method,
        "benchmark_source_type": source_type,
        "model_crs": str(crs) if crs is not None else None,
        "compare_crs": str(crs) if crs is not None else None,
        "nodata_excluded": {
            "count": n_excluded,
            "area_km2": round(excluded_area_km2, 6),
        },
        "published_context": _PUBLISHED_CONTEXT,
        "caveats": caveats,
        "notes": notes,
    }
