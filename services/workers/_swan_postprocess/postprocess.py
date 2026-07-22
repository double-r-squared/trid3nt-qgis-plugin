"""Worker-side SWAN wave-field mat -> EPSG:4326 COG postprocess.

Byte-faithful port of ``trid3nt_server.workflows.postprocess_swan``. Runs inside
the Batch worker AFTER ``swan.exe`` has written ``swan_out.mat``; rasterizes the
Hs (significant wave height) field onto a regular EPSG:4326 COG and builds the
typed ``publish_manifest.json`` dict. Handles stationary (single-frame) and
nonstationary (multi-frame) SWAN runs identically.

NEVER imports agent code. NEVER itself writes completion.json.

Key differences from the agent version:
  - COGs are written into the SCRATCH directory (not a tempfile); the
    entrypoint's output-glob sweep uploads them.
  - ``runs_uri_for`` is a callable ``rel -> uri`` that maps a filename to its
    runs-bucket URI (used for manifest cog_uri fields only).
  - ``NODATA_WAVE_M = 0.001`` (worker-side floor; agent uses 0.05).
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.workers._raster_postprocess import manifest as _manifest
from services.workers._raster_postprocess.band_stats import compute_band_stats

LOG = logging.getLogger("trid3nt.worker.swan_postprocess")

#: Worker-side calm-threshold (different from agent's 0.05).
NODATA_WAVE_M: float = 0.001

SWAN_WAVE_HEIGHT_STYLE_PRESET: str = "continuous_wave_height"

_SWAN_EXCEPTION_VALUE: float = -999.0

#: Upsample coarse SWAN grids to this minimum side before writing the COG so
#: the GDAL COG driver builds internal overviews (mirrors agent logic).
_COG_MIN_DIM_PX: int = 768

#: MATLAB variable-name candidates SWAN writes per quantity.
_HS_PREFIXES: tuple[str, ...] = ("Hsig", "Hsign", "HSIGN", "Hs")
_TP_PREFIXES: tuple[str, ...] = ("RTp", "RTpeak", "Tps", "Tp", "Period", "TPS", "RTP")
_DIR_PREFIXES: tuple[str, ...] = ("Dir", "PkDir", "Pdir", "DIR", "Theta")

#: Upper bound on emitted animation frames (mirrors agent frames.MAX_FLOOD_FRAMES).
MAX_FRAMES: int = int(os.environ.get("TRID3NT_MAX_FLOOD_FRAMES", "144"))

_PEAK_COG: str = "swan_wave_height_peak.tif"
_FRAME_COG_TMPL: str = "swan_wave_height_frame_{n:02d}.tif"


@dataclass
class SwanPostprocessResult:
    """What the entrypoint folds into completion.json + writes as the manifest."""

    status: str  # "ok" | "error"
    manifest: dict[str, Any] | None
    metrics: dict[str, Any] = field(default_factory=dict)
    cog_paths: list[Path] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# --------------------------------------------------------------------------- #
# Frame-index selector (inlined; mirrors frames._select_frame_time_indices).
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
# SWAN .mat read (scipy.io.loadmat) -> per-frame Hs / Tp / Dir grids.
# --------------------------------------------------------------------------- #
def _match_frame_vars(keys: list[str], prefixes: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for k in keys:
        if k.startswith("__"):
            continue
        for pre in prefixes:
            if k == pre or k.startswith(pre + "_") or k.startswith(pre):
                out.append(k)
                break
    return sorted(set(out))


def _read_mat_fields(mat_path: Path) -> dict[str, list[Any]]:
    """Read ``swan_out.mat`` -> ``{"hs": [...], "tp": [...], "dir": [...]}}``."""
    try:
        import numpy as np  # noqa: PLC0415
        from scipy.io import loadmat  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"numpy/scipy unavailable: {exc}") from exc

    try:
        mat = loadmat(str(mat_path))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"scipy could not read {mat_path}: {exc}") from exc

    keys = list(mat.keys())
    hs_vars = _match_frame_vars(keys, _HS_PREFIXES)
    tp_vars = _match_frame_vars(keys, _TP_PREFIXES)
    dir_vars = _match_frame_vars(keys, _DIR_PREFIXES)

    def _grid(name: str) -> Any:
        arr = np.asarray(mat[name], dtype="float64")
        return np.where(np.isclose(arr, _SWAN_EXCEPTION_VALUE), np.nan, arr)

    return {
        "hs": [_grid(n) for n in hs_vars],
        "tp": [_grid(n) for n in tp_vars],
        "dir": [_grid(n) for n in dir_vars],
    }


def _discover_mat(scratch: Path) -> Path | None:
    """Find ``swan_out.mat`` in scratch (or a ``_output`` subdir)."""
    for d in [scratch / "_output", scratch]:
        p = d / "swan_out.mat"
        if p.is_file():
            return p
    # Fallback: any .mat in tree.
    for d in [scratch / "_output", scratch]:
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and p.suffix.lower() == ".mat":
                    return p
    return None


# --------------------------------------------------------------------------- #
# Metrics.
# --------------------------------------------------------------------------- #
def _compute_wave_metrics(
    peak_hs: Any,
    *,
    bbox: tuple[float, float, float, float],
    tp_grid: Any = None,
    dir_grid: Any = None,
) -> dict[str, Any]:
    import numpy as np  # noqa: PLC0415

    hs = np.asarray(peak_hs, dtype="float64")
    wet_mask = np.isfinite(hs) & (hs >= NODATA_WAVE_M)
    wet = hs[wet_mask]
    nrows, ncols = hs.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    cell_w_m = ((max_lon - min_lon) / max(ncols, 1)) * m_per_deg_lon
    cell_h_m = ((max_lat - min_lat) / max(nrows, 1)) * m_per_deg_lat
    cell_area_m2 = abs(cell_w_m * cell_h_m)

    if wet.size == 0:
        return {"max_hs_m": 0.0, "mean_hs_m": 0.0, "p95_hs_m": 0.0,
                "wave_cell_count": 0, "wave_area_km2": 0.0,
                "mean_tp_s": 0.0, "mean_dir_deg": 0.0}

    mean_tp = 0.0
    if tp_grid is not None:
        try:
            tp = np.asarray(tp_grid, dtype="float64")
            if tp.shape == hs.shape:
                tp_wet = tp[wet_mask & np.isfinite(tp)]
                if tp_wet.size:
                    mean_tp = float(np.nanmean(tp_wet))
        except Exception:  # noqa: BLE001
            pass

    mean_dir = 0.0
    if dir_grid is not None:
        try:
            d = np.asarray(dir_grid, dtype="float64")
            if d.shape == hs.shape:
                d_wet = d[wet_mask & np.isfinite(d)]
                if d_wet.size:
                    rad = np.radians(d_wet)
                    mean_dir = float(
                        math.degrees(
                            math.atan2(float(np.mean(np.sin(rad))),
                                       float(np.mean(np.cos(rad))))
                        ) % 360.0
                    )
        except Exception:  # noqa: BLE001
            pass

    return {
        "max_hs_m": float(np.nanmax(wet)),
        "mean_hs_m": float(np.nanmean(wet)),
        "p95_hs_m": float(np.nanpercentile(wet, 95)),
        "wave_cell_count": int(wet.size),
        "wave_area_km2": wet.size * cell_area_m2 / 1_000_000.0,
        "mean_tp_s": mean_tp,
        "mean_dir_deg": mean_dir,
    }


# --------------------------------------------------------------------------- #
# COG write helpers.
# --------------------------------------------------------------------------- #
def _upsample(arr: Any, min_dim: int = _COG_MIN_DIM_PX) -> Any:
    import numpy as np  # noqa: PLC0415
    a = np.asarray(arr)
    if a.ndim != 2 or a.size == 0 or max(a.shape) >= min_dim:
        return a
    factor = int(np.ceil(min_dim / max(a.shape)))
    return np.repeat(np.repeat(a, factor, axis=0), factor, axis=1) if factor > 1 else a


def _write_hs_cog(
    hs_grid: Any,
    bbox: tuple[float, float, float, float],
    out_path: Path,
) -> None:
    """Write masked Hs grid (flipud + upsample) to an EPSG:4326 COG at out_path."""
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.transform import from_bounds  # noqa: PLC0415

    arr = np.asarray(hs_grid, dtype="float32")
    arr = np.flipud(arr)  # SWAN row-0=south -> COG row-0=north
    arr = np.where(np.isfinite(arr) & (arr >= NODATA_WAVE_M), arr, np.float32("nan"))
    arr = _upsample(arr).astype("float32")
    nrows, ncols = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
    with rasterio.open(
        out_path, "w", driver="COG",
        width=ncols, height=nrows, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=float("nan"),
        compress="LZW", overview_resampling="nearest",
    ) as dst:
        dst.write(arr, 1)


# --------------------------------------------------------------------------- #
# Top-level.
# --------------------------------------------------------------------------- #
def run_swan_postprocess(
    run_id: str,
    scratch: str | Path,
    build_spec: dict[str, Any],
    runs_uri_for: Any,
) -> SwanPostprocessResult:
    """Run SWAN mat -> COG postprocess in the LOCAL scratch dir.

    ``runs_uri_for`` is a callable ``rel -> uri``. COGs are written to scratch
    (deterministic names); the entrypoint's output sweep uploads them; the
    manifest's ``cog_uri`` values point at the resolved runs-bucket URIs.

    NEVER raises for an expected-empty result (honesty gate: returns status=error
    with typed error_code instead).
    """
    import numpy as np  # noqa: PLC0415

    scratch = Path(scratch)

    mat_path = _discover_mat(scratch)
    if mat_path is None:
        return SwanPostprocessResult(
            status="error", manifest=None,
            error_code="SWAN_OUTPUT_EMPTY",
            error_message=f"no swan_out.mat found under {scratch}",
        )

    try:
        fields = _read_mat_fields(mat_path)
    except RuntimeError as exc:
        return SwanPostprocessResult(
            status="error", manifest=None,
            error_code="SWAN_OUTPUT_READ_FAILED",
            error_message=str(exc),
        )

    hs_frames: list[Any] = fields["hs"]
    tp_frames: list[Any] = fields["tp"]
    dir_frames: list[Any] = fields["dir"]

    if not hs_frames:
        return SwanPostprocessResult(
            status="error", manifest=None,
            error_code="SWAN_OUTPUT_EMPTY",
            error_message=f"{mat_path.name} carries no Hsig (HSIGN) wave field",
        )

    bbox_raw = build_spec.get("bbox") or build_spec.get("domain_bbox")
    if not bbox_raw or len(bbox_raw) != 4:
        return SwanPostprocessResult(
            status="error", manifest=None,
            error_code="SWAN_OUTPUT_EMPTY",
            error_message="build_spec missing bbox (min_lon, min_lat, max_lon, max_lat)",
        )
    bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[arg-type]
    mode = str(build_spec.get("mode", "stationary"))

    # --- PEAK frame (largest total wave energy) ---
    best_idx = 0
    best_sum = -1.0
    for i, g in enumerate(hs_frames):
        arr = np.asarray(g, dtype="float64")
        s = float(np.nansum(np.where(np.isfinite(arr) & (arr >= NODATA_WAVE_M), arr, 0.0)))
        if s > best_sum:
            best_sum = s
            best_idx = i

    peak_hs = hs_frames[best_idx]
    peak_tp = tp_frames[best_idx] if best_idx < len(tp_frames) else None
    peak_dir = dir_frames[best_idx] if best_idx < len(dir_frames) else None

    metrics = _compute_wave_metrics(peak_hs, bbox=bbox, tp_grid=peak_tp, dir_grid=peak_dir)

    # Honesty gate.
    if int(metrics["wave_cell_count"]) == 0:
        return SwanPostprocessResult(
            status="error", manifest=None,
            error_code="SWAN_OUTPUT_EMPTY",
            error_message=(
                f"SWAN solve produced no wave-bearing cells "
                f"(Hs everywhere below {NODATA_WAVE_M} m calm threshold)"
            ),
        )

    # --- Write COGs to scratch ---
    cog_paths: list[Path] = []
    layers: list[dict[str, Any]] = []

    peak_cog_path = scratch / _PEAK_COG
    try:
        _write_hs_cog(peak_hs, bbox, peak_cog_path)
    except Exception as exc:  # noqa: BLE001
        return SwanPostprocessResult(
            status="error", manifest=None,
            error_code="SWAN_COG_WRITE_FAILED",
            error_message=f"peak COG write failed: {exc}",
        )
    cog_paths.append(peak_cog_path)

    try:
        peak_bs = compute_band_stats(str(peak_cog_path))
    except Exception:  # noqa: BLE001
        peak_bs = {"min": None, "max": None, "p2": None, "p98": None,
                   "is_categorical": False, "is_rgba": False}

    layers.append(_manifest.build_layer_entry(
        layer_id_stem=f"swan-wave-height-peak-{run_id}",
        name="Peak wave height",
        role="primary",
        style_preset=SWAN_WAVE_HEIGHT_STYLE_PRESET,
        units="meters",
        cog_uri=runs_uri_for(_PEAK_COG),
        frame_no=None,
        bbox=list(bbox),
        band_stats=peak_bs,
        metrics=metrics,
    ))

    # --- Per-frame layers (animation) ---
    n_steps = len(hs_frames)
    if n_steps > 1:
        frame_indices = _select_frame_indices(n_steps)
        for frame_no, t_idx in enumerate(frame_indices, start=1):
            fname = _FRAME_COG_TMPL.format(n=frame_no)
            frame_cog_path = scratch / fname
            try:
                _write_hs_cog(hs_frames[t_idx], bbox, frame_cog_path)
            except Exception:  # noqa: BLE001 -- degrade to peak-only on frame failure
                LOG.warning("swan postprocess: frame %d COG write failed; dropping frames", frame_no)
                # Remove any frames already added.
                for extra in cog_paths[1:]:
                    extra.unlink(missing_ok=True)
                cog_paths = cog_paths[:1]
                layers = layers[:1]
                break
            cog_paths.append(frame_cog_path)
            t_idx_tp = tp_frames[t_idx] if t_idx < len(tp_frames) else None
            t_idx_dir = dir_frames[t_idx] if t_idx < len(dir_frames) else None
            fm = _compute_wave_metrics(
                hs_frames[t_idx], bbox=bbox, tp_grid=t_idx_tp, dir_grid=t_idx_dir
            )
            try:
                fbs = compute_band_stats(str(frame_cog_path))
            except Exception:  # noqa: BLE001
                fbs = {"min": None, "max": None, "p2": None, "p98": None,
                       "is_categorical": False, "is_rgba": False}
            layers.append(_manifest.build_layer_entry(
                layer_id_stem=f"swan-wave-height-frame-{frame_no:02d}-{run_id}",
                name=f"Wave height step {frame_no}",
                role="context",
                style_preset=SWAN_WAVE_HEIGHT_STYLE_PRESET,
                units="meters",
                cog_uri=runs_uri_for(fname),
                frame_no=frame_no,
                bbox=list(bbox),
                band_stats=fbs,
                metrics=fm,
            ))
        # Require >= 2 frame layers to form a real animation group.
        if len(layers) < 3:  # peak + at least 2 frames
            for extra in cog_paths[1:]:
                extra.unlink(missing_ok=True)
            cog_paths = cog_paths[:1]
            layers = layers[:1]

    metrics["crs"] = "EPSG:4326"
    metrics["mode"] = mode

    mf = _manifest.build_manifest(
        engine="swan",
        run_id=run_id,
        status="ok",
        frame_count=len(layers),
        metrics=metrics,
        layers=layers,
    )
    LOG.info(
        "swan postprocess run_id=%s mode=%s n_frames=%d max_hs_m=%.4g "
        "wave_area_km2=%.4g cog_count=%d",
        run_id, mode, len(layers), metrics["max_hs_m"],
        metrics["wave_area_km2"], len(cog_paths),
    )
    return SwanPostprocessResult(
        status="ok",
        manifest=mf,
        metrics=metrics,
        cog_paths=cog_paths,
    )
