"""Worker-side SWMM .out binary -> EPSG:4326 COG postprocess.

Byte-faithful port of ``grace2_agent.workflows.postprocess_swmm``. Runs inside
the Batch worker AFTER pyswmm completes, reads the ``.out`` binary via the
pyswmm ``Output`` API, scatters per-timestep node depths onto the mesh-cell
grid (``S_i_j`` convention), reprojects to EPSG:4326, and builds the typed
``publish_manifest.json`` dict.

NEVER imports agent code. NEVER itself writes completion.json.

``postprocess_spec`` (from the worker manifest) must carry:
  grid_shape  -- [nrows, ncols] the mesh grid dimensions
  resolution_m -- cell size in metres (for area computation)
  crs          -- the mesh CRS EPSG string (e.g. "EPSG:32617")
  transform    -- [6-element Affine] as [a, b, c, d, e, f] (rasterio Affine.to_gdal())
  bbox         -- [min_lon, min_lat, max_lon, max_lat] EPSG:4326 AOI
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.workers._raster_postprocess import manifest as _manifest
from services.workers._raster_postprocess.band_stats import compute_band_stats

LOG = logging.getLogger("grace2.worker.swmm_postprocess")

#: Worker-side wet-depth floor (agent uses 0.05).
NODATA_DEPTH_M: float = 0.001

SWMM_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"

#: Upper bound on emitted animation frames.
MAX_FRAMES: int = int(os.environ.get("GRACE2_MAX_FLOOD_FRAMES", "144"))

_PEAK_COG: str = "swmm_depth_peak.tif"
_FRAME_COG_TMPL: str = "swmm_depth_frame_{n:02d}.tif"


@dataclass
class SWMMPostprocessResult:
    """What the entrypoint folds into completion.json + writes as the manifest."""

    status: str  # "ok" | "error"
    manifest: dict[str, Any] | None
    metrics: dict[str, Any] = field(default_factory=dict)
    cog_paths: list[Path] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# --------------------------------------------------------------------------- #
# Frame-index selector (inlined).
# --------------------------------------------------------------------------- #
def _select_frame_indices(n_steps: int) -> list[int]:
    if n_steps <= 0:
        return []
    if n_steps <= MAX_FRAMES:
        return list(range(n_steps))
    import numpy as np  # noqa: PLC0415
    idx = np.linspace(0, n_steps - 1, MAX_FRAMES).round().astype(int)
    return [int(i) for i in np.unique(idx)]


# --------------------------------------------------------------------------- #
# Node-name <-> cell-grid mapping (S_i_j convention from swmm_mesh_builder).
# --------------------------------------------------------------------------- #
def _parse_cell_node(name: str) -> tuple[int, int] | None:
    if not isinstance(name, str) or not name.startswith("S_"):
        return None
    parts = name.split("_")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def scatter_node_depths_to_grid(
    depth_by_node: dict[str, float],
    grid_shape: tuple[int, int],
) -> Any:
    """Scatter ``{node_name: depth_m}`` onto the ``(H, W)`` mesh grid.

    Cells with no node stay NaN. Sub-threshold cells (< NODATA_DEPTH_M) -> NaN.
    """
    import numpy as np  # noqa: PLC0415

    nrows, ncols = int(grid_shape[0]), int(grid_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    for name, depth in depth_by_node.items():
        rc = _parse_cell_node(name)
        if rc is None:
            continue
        i, j = rc
        if not (0 <= i < nrows and 0 <= j < ncols):
            continue
        d = float(depth)
        grid[i, j] = d if d >= NODATA_DEPTH_M else float("nan")
    return grid


# --------------------------------------------------------------------------- #
# Metrics.
# --------------------------------------------------------------------------- #
def compute_swmm_depth_metrics(
    peak_grid: Any,
    *,
    resolution_m: float,
) -> dict[str, Any]:
    """Compute depth narration scalars from the PEAK depth grid.

    ``n_buildings_affected`` always 0 on the worker (no footprint rasterizer here;
    the agent's on-box path handles that if needed).
    """
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(peak_grid, dtype="float64")
    wet = arr[np.isfinite(arr)]
    cell_area_m2 = float(resolution_m) * float(resolution_m)
    if wet.size == 0:
        return {"max_depth_m": 0.0, "mean_depth_m": 0.0, "p95_depth_m": 0.0,
                "flooded_cell_count": 0, "flooded_area_km2": 0.0,
                "n_buildings_affected": 0}
    flooded_cell_count = int(wet.size)
    return {
        "max_depth_m": float(np.nanmax(wet)),
        "mean_depth_m": float(np.nanmean(wet)),
        "p95_depth_m": float(np.nanpercentile(wet, 95)),
        "flooded_cell_count": flooded_cell_count,
        "flooded_area_km2": flooded_cell_count * cell_area_m2 / 1_000_000.0,
        "n_buildings_affected": 0,
    }


# --------------------------------------------------------------------------- #
# COG write with reproject to EPSG:4326.
# --------------------------------------------------------------------------- #
def _write_depth_cog_4326(
    grid: Any,
    *,
    src_crs: str,
    src_transform: Any,
    out_path: Path,
) -> None:
    """Reproject a projected-metres depth grid to EPSG:4326 and write as a COG.

    ``src_transform`` is a rasterio ``Affine`` or a 6-element sequence ``(a, b,
    c, d, e, f)`` in GDAL order (x_off, x_pix, x_rot, y_off, y_rot, y_pix).
    """
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.crs import CRS  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.transform import Affine  # noqa: PLC0415
    from rasterio.warp import calculate_default_transform  # noqa: PLC0415
    from rasterio.warp import reproject as _warp  # noqa: PLC0415

    arr = np.asarray(grid, dtype="float32")
    nrows, ncols = arr.shape

    # Normalise transform to rasterio.Affine.
    if not isinstance(src_transform, Affine):
        t = list(src_transform)
        # GDAL order: (x_off, x_pix, x_rot, y_off, y_rot, y_pix)
        src_transform = Affine(t[1], t[2], t[0], t[4], t[5], t[3])

    src_crs_obj = CRS.from_user_input(src_crs)
    dst_crs_obj = CRS.from_epsg(4326)

    # Write a temp GTiff for the warp source.
    src_tmp = out_path.with_suffix(".src.tmp.tif")
    try:
        with rasterio.open(
            src_tmp, "w", driver="GTiff",
            width=ncols, height=nrows, count=1, dtype="float32",
            crs=src_crs_obj, transform=src_transform, nodata=float("nan"),
        ) as tmp:
            tmp.write(arr, 1)

        with rasterio.open(src_tmp) as src_fh:
            dst_transform, out_w, out_h = calculate_default_transform(
                src_crs_obj, dst_crs_obj, ncols, nrows, *src_fh.bounds
            )
            dst_arr = np.full((out_h, out_w), float("nan"), dtype="float32")
            _warp(
                source=rasterio.band(src_fh, 1),
                destination=dst_arr,
                src_transform=src_transform,
                src_crs=src_crs_obj,
                dst_transform=dst_transform,
                dst_crs=dst_crs_obj,
                resampling=Resampling.nearest,
                src_nodata=float("nan"),
                dst_nodata=float("nan"),
            )

        with rasterio.open(
            out_path, "w", driver="COG",
            width=out_w, height=out_h, count=1, dtype="float32",
            crs=dst_crs_obj, transform=dst_transform, nodata=float("nan"),
            compress="LZW",
        ) as dst:
            dst.write(dst_arr, 1)
    finally:
        src_tmp.unlink(missing_ok=True)


def _cog_bbox(cog_path: Path) -> list[float]:
    try:
        import rasterio  # noqa: PLC0415
        with rasterio.open(cog_path) as ds:
            b = ds.bounds
            return [float(b.left), float(b.bottom), float(b.right), float(b.top)]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# pyswmm Output binary reader.
# --------------------------------------------------------------------------- #
def _read_swmm_out_depths(
    out_path: Path,
    grid_shape: tuple[int, int],
) -> list[dict[str, float]] | None:
    """Read all reporting timesteps from the SWMM ``.out`` binary.

    Returns a list of per-timestep ``{node_name: depth_m}`` dicts. Returns
    None if the file cannot be read or has no timesteps (caller returns
    status=error).

    Uses pyswmm's ``Output`` API (the ``swmm.toolkit`` binary reader). Only
    STORAGE nodes in the mesh grid (``S_i_j`` names) are read.
    """
    try:
        from pyswmm.output import Output  # type: ignore[import-not-found]
        from swmm.toolkit.shared_enum import NodeAttribute  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("swmm postprocess: pyswmm/swmm.toolkit not available: %s", exc)
        return None

    try:
        out = Output(str(out_path))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("swmm postprocess: Output('%s') failed: %s", out_path, exc)
        return None

    try:
        n_periods = out.period_count
        node_ids = list(out.nodes)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("swmm postprocess: could not query Output: %s", exc)
        return None

    if n_periods == 0 or not node_ids:
        return None

    # Filter to mesh-cell storage nodes only.
    mesh_ids = [n for n in node_ids if _parse_cell_node(n) is not None]
    if not mesh_ids:
        LOG.warning("swmm postprocess: no S_i_j mesh nodes found in .out")
        return None

    timesteps: list[dict[str, float]] = []
    try:
        for t in range(n_periods):
            snapshot: dict[str, float] = {}
            for nid in mesh_ids:
                try:
                    depth = out.node_series(nid, NodeAttribute.INVERT_DEPTH, t, t)
                    if depth is not None:
                        vals = list(depth.values())
                        if vals:
                            snapshot[nid] = float(vals[0])
                except Exception:  # noqa: BLE001
                    pass
            timesteps.append(snapshot)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("swmm postprocess: error reading timesteps: %s", exc)
        if not timesteps:
            return None
    return timesteps if timesteps else None


# --------------------------------------------------------------------------- #
# Top-level.
# --------------------------------------------------------------------------- #
def run_swmm_postprocess(
    run_id: str,
    scratch: str | Path,
    postprocess_spec: dict[str, Any],
    runs_uri_for: Any,
) -> SWMMPostprocessResult:
    """Run SWMM .out -> COG postprocess in the LOCAL scratch dir.

    ``postprocess_spec`` must contain:
      - ``grid_shape`` -- [nrows, ncols]
      - ``resolution_m`` -- cell size in metres
      - ``crs`` -- mesh CRS string (e.g. "EPSG:32617")
      - ``transform`` -- 6-element GDAL Affine in order [a,b,c,d,e,f]
        where a=x_off, b=x_pix, c=x_rot, d=y_off, e=y_rot, f=y_pix
      - ``bbox`` -- [min_lon, min_lat, max_lon, max_lat] EPSG:4326

    NEVER raises for an expected-empty result (honesty gate).
    """
    import numpy as np  # noqa: PLC0415

    scratch = Path(scratch)

    # Locate the .out file.
    out_files = sorted(scratch.glob("*.out"))
    if not out_files:
        return SWMMPostprocessResult(
            status="error", manifest=None,
            error_code="SWMM_OUTPUT_EMPTY",
            error_message=f"no .out file found under {scratch}",
        )
    out_path = out_files[0]

    # Validate postprocess_spec.
    grid_shape_raw = postprocess_spec.get("grid_shape")
    resolution_m = postprocess_spec.get("resolution_m")
    src_crs = postprocess_spec.get("crs")
    transform_raw = postprocess_spec.get("transform")
    bbox_raw = postprocess_spec.get("bbox")

    missing = [k for k, v in [
        ("grid_shape", grid_shape_raw), ("resolution_m", resolution_m),
        ("crs", src_crs), ("transform", transform_raw), ("bbox", bbox_raw),
    ] if v is None]
    if missing:
        return SWMMPostprocessResult(
            status="error", manifest=None,
            error_code="SWMM_OUTPUT_EMPTY",
            error_message=f"postprocess_spec missing keys: {missing}",
        )

    grid_shape = (int(grid_shape_raw[0]), int(grid_shape_raw[1]))
    resolution_m_f = float(resolution_m)
    bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[arg-type]

    # Read all timesteps from the .out binary.
    timesteps = _read_swmm_out_depths(out_path, grid_shape)
    if not timesteps:
        return SWMMPostprocessResult(
            status="error", manifest=None,
            error_code="SWMM_OUTPUT_EMPTY",
            error_message=f"could not read node depths from {out_path.name}",
        )

    # Build per-timestep depth grids.
    grids: list[Any] = [scatter_node_depths_to_grid(ts, grid_shape) for ts in timesteps]

    # PEAK grid (max total wet depth).
    best_grid = None
    best_sum = -1.0
    for g in grids:
        s = float(np.nansum(g))
        if s > best_sum:
            best_sum = s
            best_grid = g
    peak_grid = best_grid if best_grid is not None else np.full(grid_shape, np.nan)

    metrics = compute_swmm_depth_metrics(peak_grid, resolution_m=resolution_m_f)

    # Honesty gate.
    if int(metrics["flooded_cell_count"]) == 0:
        return SWMMPostprocessResult(
            status="error", manifest=None,
            error_code="SWMM_OUTPUT_EMPTY",
            error_message="SWMM solve produced no flooded cells",
        )

    # --- Write COGs ---
    cog_paths: list[Path] = []
    layers: list[dict[str, Any]] = []

    peak_cog_path = scratch / _PEAK_COG
    try:
        _write_depth_cog_4326(
            peak_grid, src_crs=src_crs, src_transform=transform_raw, out_path=peak_cog_path
        )
    except Exception as exc:  # noqa: BLE001
        return SWMMPostprocessResult(
            status="error", manifest=None,
            error_code="SWMM_COG_WRITE_FAILED",
            error_message=f"peak COG write failed: {exc}",
        )
    cog_paths.append(peak_cog_path)

    try:
        peak_bs = compute_band_stats(str(peak_cog_path))
    except Exception:  # noqa: BLE001
        peak_bs = {"min": None, "max": None, "p2": None, "p98": None,
                   "is_categorical": False, "is_rgba": False}

    cog_bbox = _cog_bbox(peak_cog_path) or list(bbox)
    metrics["crs"] = "EPSG:4326"

    layers.append(_manifest.build_layer_entry(
        layer_id_stem=f"swmm-depth-peak-{run_id}",
        name="Peak flood depth",
        role="primary",
        style_preset=SWMM_DEPTH_STYLE_PRESET,
        units="meters",
        cog_uri=runs_uri_for(_PEAK_COG),
        frame_no=None,
        bbox=cog_bbox,
        band_stats=peak_bs,
        metrics=metrics,
    ))

    # Per-frame layers.
    n_steps = len(grids)
    if n_steps > 1:
        frame_indices = _select_frame_indices(n_steps)
        for frame_no, t_idx in enumerate(frame_indices, start=1):
            fname = _FRAME_COG_TMPL.format(n=frame_no)
            frame_cog_path = scratch / fname
            try:
                _write_depth_cog_4326(
                    grids[t_idx], src_crs=src_crs, src_transform=transform_raw,
                    out_path=frame_cog_path,
                )
            except Exception:  # noqa: BLE001
                LOG.warning("swmm postprocess: frame %d COG write failed; dropping frames", frame_no)
                for extra in cog_paths[1:]:
                    extra.unlink(missing_ok=True)
                cog_paths = cog_paths[:1]
                layers = layers[:1]
                break
            cog_paths.append(frame_cog_path)
            fm = compute_swmm_depth_metrics(grids[t_idx], resolution_m=resolution_m_f)
            try:
                fbs = compute_band_stats(str(frame_cog_path))
            except Exception:  # noqa: BLE001
                fbs = {"min": None, "max": None, "p2": None, "p98": None,
                       "is_categorical": False, "is_rgba": False}
            frame_bbox = _cog_bbox(frame_cog_path) or cog_bbox
            layers.append(_manifest.build_layer_entry(
                layer_id_stem=f"swmm-depth-frame-{frame_no:02d}-{run_id}",
                name=f"Flood depth step {frame_no}",
                role="context",
                style_preset=SWMM_DEPTH_STYLE_PRESET,
                units="meters",
                cog_uri=runs_uri_for(fname),
                frame_no=frame_no,
                bbox=frame_bbox,
                band_stats=fbs,
                metrics=fm,
            ))
        if len(layers) < 3:
            for extra in cog_paths[1:]:
                extra.unlink(missing_ok=True)
            cog_paths = cog_paths[:1]
            layers = layers[:1]

    mf = _manifest.build_manifest(
        engine="swmm",
        run_id=run_id,
        status="ok",
        frame_count=len(layers),
        metrics=metrics,
        layers=layers,
    )
    LOG.info(
        "swmm postprocess run_id=%s n_frames=%d max_depth_m=%.4g "
        "flooded_area_km2=%.4g cog_count=%d",
        run_id, len(layers), metrics["max_depth_m"],
        metrics["flooded_area_km2"], len(cog_paths),
    )
    return SWMMPostprocessResult(
        status="ok",
        manifest=mf,
        metrics=metrics,
        cog_paths=cog_paths,
    )
