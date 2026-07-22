"""Worker-side GeoClaw fort.q AMR frames -> EPSG:4326 COG postprocess.

Byte-faithful port of ``grace2_agent.workflows.postprocess_geoclaw``. Runs
inside the Batch worker AFTER ``xgeoclaw`` / ``python setrun.py`` has produced
its ``_output/fort.q*`` frames; rasterizes each frame's water depth onto a
regular EPSG:4326 grid (finest-wins AMR coverage), writes peak + per-frame
COGs into the scratch dir, and builds the typed ``publish_manifest.json`` dict.

NEVER imports agent code. NEVER itself writes completion.json.

Key differences from the agent version:
  - COGs written to scratch (not temp); entrypoint output sweep uploads them.
  - ``runs_uri_for`` callable for manifest cog_uri fields.
  - ``NODATA_DEPTH_M = 0.001`` (worker-side floor; agent uses 0.05).
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.workers._raster_postprocess import manifest as _manifest
from services.workers._raster_postprocess.band_stats import compute_band_stats

LOG = logging.getLogger("grace2.worker.geoclaw_postprocess")

#: Worker-side wet-depth floor (agent uses 0.05).
NODATA_DEPTH_M: float = 0.001

GEOCLAW_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"

#: Target ground resolution (m/px) for adaptive raster sizing.
GEOCLAW_TARGET_GROUND_RES_M: float = 25.0
GEOCLAW_MIN_PX_PER_SIDE: int = 256
GEOCLAW_MAX_PX_PER_SIDE: int = 2500
GEOCLAW_MAX_TOTAL_CELLS: int = 5_000_000

_FGMAX_SENTINEL_ABS: float = 1e8

#: Upper bound on emitted animation frames.
MAX_FRAMES: int = int(os.environ.get("GRACE2_MAX_FLOOD_FRAMES", "144"))

_PEAK_COG: str = "geoclaw_depth_peak.tif"
_FRAME_COG_TMPL: str = "geoclaw_depth_frame_{n:02d}.tif"


@dataclass
class GeoClawPostprocessResult:
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
# fort.q AMR ASCII frame parsing (byte-faithful port from agent).
# --------------------------------------------------------------------------- #
class _Patch:
    __slots__ = ("level", "mx", "my", "xlow", "ylow", "dx", "dy", "h")

    def __init__(self, level, mx, my, xlow, ylow, dx, dy, h):
        self.level = level
        self.mx = mx
        self.my = my
        self.xlow = xlow
        self.ylow = ylow
        self.dx = dx
        self.dy = dy
        self.h = h  # (my, mx) depth array, row 0 = ylow (south)


_HEADER_VAL_RE = re.compile(r"^\s*([-+0-9.eE]+)\s+(\w+)")


def _header_value(line: str) -> str | None:
    m = _HEADER_VAL_RE.match(line)
    return m.group(1) if m else None


def parse_fort_q_frame(text: str) -> list[_Patch]:
    """Parse one GeoClaw ``fort.qNNNN`` frame into a list of AMR patches."""
    import numpy as np  # noqa: PLC0415

    patches: list[_Patch] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        header_vals: list[str] = []
        hdr_start = i
        while i < n and len(header_vals) < 8:
            v = _header_value(lines[i])
            if v is None:
                break
            header_vals.append(v)
            i += 1
        if len(header_vals) < 8:
            i = hdr_start + 1
            continue
        level = int(float(header_vals[1]))
        mx = int(float(header_vals[2]))
        my = int(float(header_vals[3]))
        xlow = float(header_vals[4])
        ylow = float(header_vals[5])
        dx = float(header_vals[6])
        dy = float(header_vals[7])
        h = np.full((my, mx), np.nan, dtype="float64")
        count = 0
        j = 0
        col = 0
        while i < n and count < mx * my:
            ln = lines[i].strip()
            i += 1
            if not ln:
                if col != 0:
                    j += 1
                    col = 0
                continue
            parts = ln.split()
            if not parts:
                continue
            try:
                hv = float(parts[0])
            except ValueError:
                continue
            if j < my and col < mx:
                h[j, col] = hv
            col += 1
            count += 1
            if col >= mx:
                j += 1
                col = 0
        patches.append(_Patch(level, mx, my, xlow, ylow, dx, dy, h))
    return patches


def compute_geoclaw_grid_shape(
    bbox: tuple[float, float, float, float],
    *,
    target_res_m: float = GEOCLAW_TARGET_GROUND_RES_M,
) -> tuple[int, int]:
    """Adaptive output raster ``(H, W)`` from AOI at a target ground resolution."""
    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon <= min_lon or max_lat <= min_lat:
        return (GEOCLAW_MIN_PX_PER_SIDE, GEOCLAW_MIN_PX_PER_SIDE)
    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    aoi_h_m = (max_lat - min_lat) * m_per_deg_lat
    aoi_w_m = (max_lon - min_lon) * m_per_deg_lon
    res = max(float(target_res_m), 1e-6)
    nrows = int(round(aoi_h_m / res))
    ncols = int(round(aoi_w_m / res))
    nrows = min(max(nrows, GEOCLAW_MIN_PX_PER_SIDE), GEOCLAW_MAX_PX_PER_SIDE)
    ncols = min(max(ncols, GEOCLAW_MIN_PX_PER_SIDE), GEOCLAW_MAX_PX_PER_SIDE)
    if nrows * ncols > GEOCLAW_MAX_TOTAL_CELLS:
        scale = math.sqrt(GEOCLAW_MAX_TOTAL_CELLS / float(nrows * ncols))
        nrows = max(GEOCLAW_MIN_PX_PER_SIDE, int(nrows * scale))
        ncols = max(GEOCLAW_MIN_PX_PER_SIDE, int(ncols * scale))
    return (nrows, ncols)


def rasterize_frame_to_grid(
    patches: list[_Patch],
    bbox: tuple[float, float, float, float],
    out_shape: tuple[int, int],
) -> Any:
    """Rasterize a frame's AMR patches onto a regular AOI grid (finest wins).

    Coverage fill: each AMR patch cell PAINTS its full footprint so the output
    grid is gap-free at finer resolutions. Patches sorted by AMR level ascending
    so finer levels overwrite coarser ones where they overlap.
    """
    import numpy as np  # noqa: PLC0415

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon <= min_lon or max_lat <= min_lat:
        return grid
    gdx = (max_lon - min_lon) / ncols
    gdy = (max_lat - min_lat) / nrows
    xcen = min_lon + (np.arange(ncols) + 0.5) * gdx
    ycen = max_lat - (np.arange(nrows) + 0.5) * gdy
    for patch in sorted(patches, key=lambda p: p.level):
        if patch.mx <= 0 or patch.my <= 0 or patch.dx <= 0 or patch.dy <= 0:
            continue
        p_xmin = patch.xlow
        p_xmax = patch.xlow + patch.mx * patch.dx
        p_ymin = patch.ylow
        p_ymax = patch.ylow + patch.my * patch.dy
        cols = np.nonzero((xcen >= p_xmin) & (xcen < p_xmax))[0]
        rows = np.nonzero((ycen >= p_ymin) & (ycen < p_ymax))[0]
        if cols.size == 0 or rows.size == 0:
            continue
        pi = ((xcen[cols] - patch.xlow) / patch.dx).astype(np.intp)
        pj = ((ycen[rows] - patch.ylow) / patch.dy).astype(np.intp)
        np.clip(pi, 0, patch.mx - 1, out=pi)
        np.clip(pj, 0, patch.my - 1, out=pj)
        sub = patch.h[np.ix_(pj, pi)]
        wet = np.isfinite(sub) & (sub >= NODATA_DEPTH_M)
        if not wet.any():
            continue
        block = grid[np.ix_(rows, cols)]
        block[wet] = sub[wet]
        grid[np.ix_(rows, cols)] = block
    return grid


def compute_geoclaw_depth_metrics(
    peak_grid: Any,
    *,
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Compute depth narration scalars from the PEAK depth grid."""
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(peak_grid, dtype="float64")
    wet_mask = np.isfinite(arr)
    wet = arr[wet_mask]
    nrows, ncols = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    cell_w_m = ((max_lon - min_lon) / max(ncols, 1)) * m_per_deg_lon
    cell_h_m = ((max_lat - min_lat) / max(nrows, 1)) * m_per_deg_lat
    cell_area_m2 = abs(cell_w_m * cell_h_m)
    if wet.size == 0:
        return {"max_depth_m": 0.0, "mean_depth_m": 0.0, "p95_depth_m": 0.0,
                "flooded_cell_count": 0, "flooded_area_km2": 0.0,
                "max_inundation_m": 0.0, "arrival_time_s": None}
    flooded_cell_count = int(wet.size)
    return {
        "max_depth_m": float(np.nanmax(wet)),
        "mean_depth_m": float(np.nanmean(wet)),
        "p95_depth_m": float(np.nanpercentile(wet, 95)),
        "flooded_cell_count": flooded_cell_count,
        "flooded_area_km2": flooded_cell_count * cell_area_m2 / 1_000_000.0,
        "max_inundation_m": float(np.nanmax(wet)),
        "arrival_time_s": None,
    }


def read_fgmax_output(out_dir: Path) -> dict[str, Any] | None:
    """Read GeoClaw fgmax output; return None when absent."""
    import numpy as np  # noqa: PLC0415

    base = out_dir / "_output"
    if not base.is_dir():
        base = out_dir
    fgmax_path = base / "fgmax0001.txt"
    grids_path = base / "fgmax_grids.data"
    if not fgmax_path.exists() or not grids_path.exists():
        return None
    try:
        arr = np.atleast_2d(np.asarray(np.loadtxt(fgmax_path, comments="#"), dtype="float64"))
    except Exception:  # noqa: BLE001
        return None
    if arr.size == 0 or arr.shape[1] != 9:
        return None
    h = arr[:, 4].copy()
    B = arr[:, 3].copy()
    arrival = arr[:, 8].copy()
    notset = h < -1e50
    h[notset] = np.nan
    B[notset] = np.nan
    sentinel = (np.abs(arrival) > _FGMAX_SENTINEL_ABS) | (arrival < 0.0)
    arrival[sentinel] = np.nan
    finite_h = h[np.isfinite(h)]
    max_depth_m = float(np.nanmax(h)) if finite_h.size else 0.0
    land = B > 0.0
    land_h = h[land & np.isfinite(h)]
    max_inundation_m = float(np.nanmax(land_h)) if land_h.size else 0.0
    wet = np.isfinite(h) & (h > 0.001)
    wet_arrival = arrival[wet]
    arrival_time_s = (
        float(np.nanmin(wet_arrival))
        if wet_arrival.size and np.isfinite(wet_arrival).any()
        else None
    )
    return {
        "max_depth_m": max_depth_m,
        "max_inundation_m": max_inundation_m,
        "arrival_time_s": arrival_time_s,
    }


# --------------------------------------------------------------------------- #
# COG write.
# --------------------------------------------------------------------------- #
def _write_depth_cog(
    grid: Any,
    bbox: tuple[float, float, float, float],
    out_path: Path,
) -> None:
    """Write masked depth grid (already EPSG:4326, row-0=north) to a COG."""
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.transform import from_bounds  # noqa: PLC0415

    arr = np.asarray(grid, dtype="float32")
    nrows, ncols = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
    with rasterio.open(
        out_path, "w", driver="COG",
        width=ncols, height=nrows, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=float("nan"),
        compress="LZW",
    ) as dst:
        dst.write(arr, 1)


# --------------------------------------------------------------------------- #
# Frame discovery.
# --------------------------------------------------------------------------- #
def _discover_frames(scratch: Path) -> list[tuple[int, Path]]:
    """List ``(frame_no, fort.qNNNN)`` ascending by frame_no."""
    q_re = re.compile(r"^fort\.q(\d{4,})$")
    found: list[tuple[int, Path]] = []
    seen: set[int] = set()
    for d in [scratch / "_output", scratch]:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            m = q_re.match(p.name)
            if not m:
                continue
            no = int(m.group(1))
            if no in seen:
                continue
            seen.add(no)
            found.append((no, p))
    found.sort(key=lambda x: x[0])
    return found


# --------------------------------------------------------------------------- #
# Top-level.
# --------------------------------------------------------------------------- #
def run_geoclaw_postprocess(
    run_id: str,
    scratch: str | Path,
    build_spec: dict[str, Any],
    runs_uri_for: Any,
) -> GeoClawPostprocessResult:
    """Run GeoClaw fort.q -> COG postprocess in the LOCAL scratch dir.

    NEVER raises for an expected-empty result (honesty gate).
    """
    import numpy as np  # noqa: PLC0415

    scratch = Path(scratch)

    frame_files = _discover_frames(scratch)
    if not frame_files:
        return GeoClawPostprocessResult(
            status="error", manifest=None,
            error_code="GEOCLAW_OUTPUT_EMPTY",
            error_message=f"no fort.q frames found under {scratch}",
        )

    bbox_raw = build_spec.get("bbox")
    if not bbox_raw or len(bbox_raw) != 4:
        return GeoClawPostprocessResult(
            status="error", manifest=None,
            error_code="GEOCLAW_OUTPUT_EMPTY",
            error_message="build_spec missing bbox (min_lon, min_lat, max_lon, max_lat)",
        )
    bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[arg-type]
    scenario = str(build_spec.get("scenario", "dam_break"))
    mask_ocean = bool(build_spec.get("mask_ocean", False))

    grid_shape = compute_geoclaw_grid_shape(bbox)  # type: ignore[arg-type]

    grids: list[Any] = []
    for _no, q_path in frame_files:
        try:
            patches = parse_fort_q_frame(q_path.read_text(errors="replace"))
        except Exception as exc:  # noqa: BLE001
            return GeoClawPostprocessResult(
                status="error", manifest=None,
                error_code="GEOCLAW_OUTPUT_READ_FAILED",
                error_message=f"could not read {q_path.name}: {exc}",
            )
        grids.append(rasterize_frame_to_grid(patches, bbox, grid_shape))  # type: ignore[arg-type]

    # Ocean mask (initial-wet criterion for coastal/tsunami scenarios).
    if mask_ocean and grids:
        try:
            init = np.asarray(grids[0], dtype="float64")
            ocean = np.isfinite(init) & (init > NODATA_DEPTH_M)
            if ocean.any():
                for _i in range(len(grids)):
                    gi = np.asarray(grids[_i], dtype="float64").copy()
                    gi[ocean] = np.nan
                    grids[_i] = gi
        except Exception:  # noqa: BLE001
            pass

    # --- PEAK grid ---
    best_grid = None
    best_sum = -1.0
    for g in grids:
        s = float(np.nansum(g))
        if s > best_sum:
            best_sum = s
            best_grid = g
    peak_grid = best_grid if best_grid is not None else np.full(grid_shape, np.nan)

    metrics = compute_geoclaw_depth_metrics(peak_grid, bbox=bbox)  # type: ignore[arg-type]

    # fgmax override (arrival_time + inundation from fixed-grid maxima).
    fgmax = read_fgmax_output(scratch)
    if fgmax is not None:
        metrics["max_depth_m"] = float(fgmax["max_depth_m"])
        metrics["max_inundation_m"] = float(fgmax["max_inundation_m"])
        metrics["arrival_time_s"] = fgmax["arrival_time_s"]

    if mask_ocean:
        metrics["max_depth_m"] = float(metrics.get("max_inundation_m", 0.0))

    # Honesty gate.
    if int(metrics["flooded_cell_count"]) == 0:
        return GeoClawPostprocessResult(
            status="error", manifest=None,
            error_code="GEOCLAW_OUTPUT_EMPTY",
            error_message="GeoClaw solve produced no wet cells",
        )

    metrics["crs"] = "EPSG:4326"

    # --- Write COGs ---
    cog_paths: list[Path] = []
    layers: list[dict[str, Any]] = []

    peak_cog_path = scratch / _PEAK_COG
    try:
        _write_depth_cog(peak_grid, bbox, peak_cog_path)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        return GeoClawPostprocessResult(
            status="error", manifest=None,
            error_code="GEOCLAW_COG_WRITE_FAILED",
            error_message=f"peak COG write failed: {exc}",
        )
    cog_paths.append(peak_cog_path)

    try:
        peak_bs = compute_band_stats(str(peak_cog_path))
    except Exception:  # noqa: BLE001
        peak_bs = {"min": None, "max": None, "p2": None, "p98": None,
                   "is_categorical": False, "is_rgba": False}

    layers.append(_manifest.build_layer_entry(
        layer_id_stem=f"geoclaw-depth-peak-{run_id}",
        name="Peak flood depth",
        role="primary",
        style_preset=GEOCLAW_DEPTH_STYLE_PRESET,
        units="meters",
        cog_uri=runs_uri_for(_PEAK_COG),
        frame_no=None,
        bbox=list(bbox),
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
                _write_depth_cog(grids[t_idx], bbox, frame_cog_path)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                LOG.warning("geoclaw postprocess: frame %d COG write failed; dropping frames", frame_no)
                for extra in cog_paths[1:]:
                    extra.unlink(missing_ok=True)
                cog_paths = cog_paths[:1]
                layers = layers[:1]
                break
            cog_paths.append(frame_cog_path)
            fm = compute_geoclaw_depth_metrics(grids[t_idx], bbox=bbox)  # type: ignore[arg-type]
            try:
                fbs = compute_band_stats(str(frame_cog_path))
            except Exception:  # noqa: BLE001
                fbs = {"min": None, "max": None, "p2": None, "p98": None,
                       "is_categorical": False, "is_rgba": False}
            layers.append(_manifest.build_layer_entry(
                layer_id_stem=f"geoclaw-depth-frame-{frame_no:02d}-{run_id}",
                name=f"Flood depth step {frame_no}",
                role="context",
                style_preset=GEOCLAW_DEPTH_STYLE_PRESET,
                units="meters",
                cog_uri=runs_uri_for(fname),
                frame_no=frame_no,
                bbox=list(bbox),
                band_stats=fbs,
                metrics=fm,
            ))
        if len(layers) < 3:
            for extra in cog_paths[1:]:
                extra.unlink(missing_ok=True)
            cog_paths = cog_paths[:1]
            layers = layers[:1]

    mf = _manifest.build_manifest(
        engine="geoclaw",
        run_id=run_id,
        status="ok",
        frame_count=len(layers),
        metrics=metrics,
        layers=layers,
    )
    LOG.info(
        "geoclaw postprocess run_id=%s scenario=%s n_frames=%d "
        "max_depth_m=%.4g flooded_area_km2=%.4g",
        run_id, scenario, len(layers),
        metrics["max_depth_m"], metrics["flooded_area_km2"],
    )
    return GeoClawPostprocessResult(
        status="ok",
        manifest=mf,
        metrics=metrics,
        cog_paths=cog_paths,
    )
