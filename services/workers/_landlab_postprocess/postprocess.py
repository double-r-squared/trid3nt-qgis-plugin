"""Worker-side Landlab field -> EPSG:4326 COG postprocess.

Byte-faithful port of ``trid3nt_server.workflows.postprocess_landlab`` (probability
and overland-flow paths). Runs inside the Batch worker AFTER the component chain
writes ``landlab_field.tif`` to the scratch directory; rewrites it as an
EPSG:4326 COG and builds the typed ``publish_manifest.json`` dict.

NEVER imports agent code. NEVER itself writes completion.json -- it RETURNS the
manifest dict + the status the entrypoint folds into completion.json.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.workers._raster_postprocess import manifest as _manifest
from services.workers._raster_postprocess.band_stats import compute_band_stats

LOG = logging.getLogger("trid3nt.worker.landlab_postprocess")

#: Worker-side wet-depth floor for the overland chain (agent uses 0.05).
NODATA_DEPTH_M: float = 0.001

#: Worker-side unstable-probability threshold (mirrors agent's UNSTABLE_PROBABILITY_THRESHOLD).
UNSTABLE_PROBABILITY_THRESHOLD: float = 0.75

LANDSLIDE_STYLE_PRESET: str = "continuous_landslide_susceptibility"
OVERLAND_STYLE_PRESET: str = "continuous_flood_depth"

_FIELD_COG_FILENAME: str = "landlab_field.tif"
_LANDSLIDE_COG_FILENAME: str = "landlab_susceptibility_4326.tif"
_OVERLAND_COG_FILENAME: str = "landlab_overland_4326.tif"


@dataclass
class LandlabPostprocessResult:
    """What the entrypoint folds into completion.json + writes as the manifest."""

    status: str  # "ok" | "error"
    manifest: dict[str, Any] | None
    metrics: dict[str, Any] = field(default_factory=dict)
    cog_paths: list[Path] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


def _compute_landlab_metrics(
    field_arr: Any,
    *,
    analysis: str,
    result_block: dict[str, Any] | None,
) -> dict[str, float]:
    """Compute the three narration scalars.

    Prefers the worker result_block (authoritative FoS computed from the full
    field); falls back to recomputing from the reprojected array.
    """
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(field_arr, dtype="float64")
    active = np.isfinite(arr)
    vals = arr[active]
    n_active = int(vals.size)

    if n_active == 0:
        return {
            "unstable_area_fraction": 0.0,
            "min_factor_of_safety": 0.0,
            "mean_probability_of_failure": 0.0,
        }

    if analysis == "overland_flow":
        wet_frac = float(np.count_nonzero(vals >= NODATA_DEPTH_M) / n_active)
        max_depth = float(np.max(vals))
        recomputed = {
            "unstable_area_fraction": wet_frac,
            "min_factor_of_safety": max_depth,
            "mean_probability_of_failure": 0.0,
        }
    else:
        unstable_frac = float(
            np.count_nonzero(vals >= UNSTABLE_PROBABILITY_THRESHOLD) / n_active
        )
        mean_pof = float(np.mean(vals))
        recomputed = {
            "unstable_area_fraction": unstable_frac,
            "min_factor_of_safety": 0.0,
            "mean_probability_of_failure": mean_pof,
        }

    def _pick(key: str) -> float:
        if isinstance(result_block, dict) and result_block.get(key) is not None:
            try:
                return float(result_block[key])
            except (TypeError, ValueError):
                pass
        return float(recomputed[key])

    unstable = max(0.0, min(1.0, _pick("unstable_area_fraction")))
    min_fos = max(0.0, _pick("min_factor_of_safety"))
    mean_pof = max(0.0, min(1.0, _pick("mean_probability_of_failure")))
    return {
        "unstable_area_fraction": unstable,
        "min_factor_of_safety": min_fos,
        "mean_probability_of_failure": mean_pof,
    }


def _warp_to_4326(src_path: Path, out_path: Path) -> None:
    """Reproject a projected-CRS field COG to EPSG:4326, writing a COG at out_path.

    Uses rasterio.warp directly (no cog_io / trid3nt_server imports).
    The src may be any rasterio-readable raster with an embedded CRS.
    Nearest-neighbour resampling preserves NaN boundaries.
    """
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.crs import CRS  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.warp import calculate_default_transform  # noqa: PLC0415
    from rasterio.warp import reproject as _warp_reproject  # noqa: PLC0415

    dst_crs = CRS.from_epsg(4326)

    src_tmp = out_path.with_suffix(".src.tmp.tif")
    try:
        with rasterio.open(src_path) as src:
            src_crs = src.crs
            if src_crs is None:
                src_crs = dst_crs  # fallback: assume already 4326
            arr = src.read(1).astype("float32")
            nodata = src.nodata
            src_transform = src.transform
            src_width = src.width
            src_height = src.height
            src_bounds = src.bounds

        if nodata is not None and not math.isnan(float(nodata)):
            arr = np.where(arr == float(nodata), np.nan, arr).astype("float32")

        transform, out_w, out_h = calculate_default_transform(
            src_crs, dst_crs, src_width, src_height, *src_bounds
        )

        # Write intermediate GTiff for warp source.
        with rasterio.open(
            src_tmp, "w", driver="GTiff",
            width=src_width, height=src_height, count=1, dtype="float32",
            crs=src_crs, transform=src_transform, nodata=float("nan"),
        ) as tmp:
            tmp.write(arr, 1)

        dst_arr = np.full((out_h, out_w), float("nan"), dtype="float32")
        with rasterio.open(src_tmp) as src_fh:
            _warp_reproject(
                source=rasterio.band(src_fh, 1),
                destination=dst_arr,
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,
                src_nodata=float("nan"),
                dst_nodata=float("nan"),
            )

        with rasterio.open(
            out_path, "w", driver="COG",
            width=out_w, height=out_h, count=1, dtype="float32",
            crs=dst_crs, transform=transform, nodata=float("nan"),
            compress="LZW",
        ) as dst:
            dst.write(dst_arr, 1)
    finally:
        try:
            src_tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _cog_bbox(cog_path: Path) -> list[float] | None:
    try:
        import rasterio  # noqa: PLC0415
        with rasterio.open(cog_path) as ds:
            b = ds.bounds
            return [float(b.left), float(b.bottom), float(b.right), float(b.top)]
    except Exception:  # noqa: BLE001
        return None


def run_landlab_postprocess(
    run_id: str,
    scratch: str | Path,
    analysis: str,
    result_block: dict[str, Any] | None,
    runs_uri_for: Any,
) -> LandlabPostprocessResult:
    """Run Landlab postprocess on the LOCAL scratch dir; return the manifest result.

    ``runs_uri_for`` is a callable ``rel -> uri`` (the entrypoint's
    ``lambda rel: _runs_uri(run_id, rel)``). The COG is written into scratch
    under a deterministic key so the entrypoint's output sweep uploads it; the
    manifest's ``cog_uri`` is the resolved runs-bucket URI for that key.

    NEVER raises for an expected-empty result -- returns a status=error result
    with the typed error_code (honesty gate / Invariant 1 / FR-AS-7).
    """
    import numpy as np  # noqa: PLC0415

    scratch = Path(scratch)
    src_tif = scratch / _FIELD_COG_FILENAME

    if not src_tif.exists():
        return LandlabPostprocessResult(
            status="error", manifest=None,
            error_code="LANDLAB_OUTPUT_EMPTY",
            error_message=f"{_FIELD_COG_FILENAME} not found in scratch",
        )

    is_landslide = analysis != "overland_flow"
    cog_filename = _LANDSLIDE_COG_FILENAME if is_landslide else _OVERLAND_COG_FILENAME
    cog_path = scratch / cog_filename

    try:
        _warp_to_4326(src_tif, cog_path)
    except Exception as exc:  # noqa: BLE001
        return LandlabPostprocessResult(
            status="error", manifest=None,
            error_code="LANDLAB_COG_REPROJECT_FAILED",
            error_message=f"warp to 4326 failed: {exc}",
        )

    # Read back the reprojected array to compute metrics.
    try:
        import rasterio as _rio  # noqa: PLC0415
        with _rio.open(cog_path) as ds:
            field_arr = ds.read(1).astype("float64")
    except Exception as exc:  # noqa: BLE001
        return LandlabPostprocessResult(
            status="error", manifest=None,
            error_code="LANDLAB_COG_REPROJECT_FAILED",
            error_message=f"could not read reprojected COG: {exc}",
        )

    # Honesty gate: at least one finite cell required.
    finite_count = int(np.count_nonzero(np.isfinite(field_arr)))
    if finite_count == 0:
        return LandlabPostprocessResult(
            status="error", manifest=None,
            error_code="LANDLAB_OUTPUT_EMPTY",
            error_message="reprojected Landlab COG has no finite cells",
        )

    metrics = _compute_landlab_metrics(
        field_arr, analysis=analysis, result_block=result_block
    )
    bbox = _cog_bbox(cog_path)

    try:
        band_stats = compute_band_stats(str(cog_path))
    except Exception:  # noqa: BLE001
        band_stats = {"min": None, "max": None, "p2": None, "p98": None,
                      "is_categorical": False, "is_rgba": False}

    cog_uri = runs_uri_for(cog_filename)

    if is_landslide:
        layer_name = "Landslide susceptibility"
        style_preset = LANDSLIDE_STYLE_PRESET
        units = "probability"
        layer_id_stem = f"landlab-susceptibility-{run_id}"
    else:
        layer_name = "Peak overland depth"
        style_preset = OVERLAND_STYLE_PRESET
        units = "meters"
        layer_id_stem = f"landlab-overland-{run_id}"

    layer = _manifest.build_layer_entry(
        layer_id_stem=layer_id_stem,
        name=layer_name,
        role="primary",
        style_preset=style_preset,
        units=units,
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=band_stats,
        metrics=metrics,
    )
    mf = _manifest.build_manifest(
        engine="landlab",
        run_id=run_id,
        status="ok",
        frame_count=1,
        metrics=metrics,
        layers=[layer],
    )
    LOG.info(
        "landlab postprocess run_id=%s analysis=%s unstable_frac=%.4f "
        "min_fos=%.4f mean_pof=%.4f cog=%s",
        run_id, analysis,
        metrics["unstable_area_fraction"],
        metrics["min_factor_of_safety"],
        metrics["mean_probability_of_failure"],
        cog_filename,
    )
    return LandlabPostprocessResult(
        status="ok",
        manifest=mf,
        metrics=metrics,
        cog_paths=[cog_path],
    )
