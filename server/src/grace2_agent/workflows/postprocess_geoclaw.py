"""GeoClaw (Clawpack) run-output postprocessing (sprint-17).

``postprocess_geoclaw(out_dir, run_args, *, run_id, ...) -> (layers, metrics)``
reads the GeoClaw ``fort.q`` AMR ASCII frames from a solved run's ``_output/``
directory, rasterizes each frame's water DEPTH (``q[0] = h``) onto a regular
EPSG:4326 grid over the AOI, masks dry/sub-threshold cells to NaN, and emits the
SAME ``(layers, metrics)`` shape as ``postprocess_flood`` / ``postprocess_swmm``
so the Phase-1 flood-animation scrubber path consumes it UNCHANGED:

  - ``layers[0]`` = the PEAK overland-depth COG, role ``"primary"``, name
    ``"Peak flood depth"``, style preset ``continuous_flood_depth``. It is a
    :class:`~grace2_contracts.geoclaw_contracts.GeoClawDepthLayerURI` carrying
    the three narration scalars (``max_depth_m`` / ``flooded_area_km2`` /
    ``max_inundation_m``) + the echoed scenario.
  - ``layers[1:]`` = up to ``MAX_FLOOD_FRAMES`` per-frame depth COGs, role
    ``"context"``, names ``"Flood depth step N"`` — the EXACT web
    ``parseFrameToken`` / ``detectSequentialGroups`` token so the LayerPanel
    collapses them into one bottom-center-scrubber temporal group. Each frame
    lands at a DISTINCT runs-bucket key (distinct TiTiler url) -> no dedup
    collapse.

This is the GeoClaw analogue of ``postprocess_swmm``. The defining difference:
GeoClaw emits AMR-patch ASCII frames (one or more rectangular grid patches per
frame, at different refinement levels), NOT a single regular raster. We READ each
``fort.qNNNN`` (with its ``fort.tNNNN`` header for the frame time), rasterize the
finest-available depth onto a regular AOI grid ourselves (a higher AMR level
overwrites a coarser one where they overlap), then reuse the shared COG-write +
frame-selection + upload helpers.

Reuse (do NOT reinvent): the even-subsample frame selector
``_select_frame_time_indices`` (MAX_FLOOD_FRAMES=24), the ``NODATA_DEPTH_M=0.05``
wet threshold, the ``continuous_flood_depth`` style preset, and the
``RUNS_BUCKET_DEFAULT`` from ``postprocess_flood``. The honesty floor
(Invariant 1 / FR-AS-7): the depth scalars are computed with plain arithmetic
from the depth grid — no LLM anywhere; the agent narrates the typed fields, never
invents them.

Tier separation (Invariant 5): the COG lands in the runs bucket (scheme-aware
via ``cache.storage_scheme()``); the agent does not re-render — ``publish_layer``
/ TiTiler serves the tiles from the URI on the envelope.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from grace2_contracts.geoclaw_contracts import (
    GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M,
    GEOCLAW_DEPTH_STYLE_PRESET,
    GeoClawDepthLayerURI,
)

from . import cog_io
from .cog_io import CogIoError

# Reuse the SFINCS postprocess constants/helpers (single source of truth so the
# GeoClaw + SFINCS + SWMM animation paths stay byte-compatible on the web side).
from .postprocess_flood import (
    MAX_FLOOD_FRAMES,
    NODATA_DEPTH_M,
    RUNS_BUCKET_DEFAULT,
    _select_frame_time_indices,
)

__all__ = [
    "PostprocessGeoClawError",
    "postprocess_geoclaw",
    "parse_fort_q_frame",
    "rasterize_frame_to_grid",
    "compute_geoclaw_grid_shape",
    "compute_geoclaw_depth_metrics",
    "read_fgmax_output",
    "GEOCLAW_DEPTH_STYLE_PRESET",
    "GEOCLAW_TARGET_GROUND_RES_M",
    "NODATA_DEPTH_M",
    "MAX_FLOOD_FRAMES",
    "RUNS_BUCKET_DEFAULT",
]

#: Target GROUND resolution (metres/pixel) for the adaptive GeoClaw output
#: raster. ~25 m matches the finest CoNED/level-5 AMR nest at the AOI so the
#: overland run-up band rasterizes as a smooth, dense sheet (SFINCS parity, whose
#: quadtree raster defaults to ~30 m) instead of the legacy fixed 256x256 grid
#: (~33-53 m over an ~8 km AOI -> chunky specks).
GEOCLAW_TARGET_GROUND_RES_M: float = 25.0

#: Floor: never coarser than the legacy fixed 256x256 grid (a tiny AOI rasterizes
#: FINER than the target res rather than exploding cell size).
GEOCLAW_MIN_PX_PER_SIDE: int = 256

#: Caps so a huge AOI can never produce a monster COG: at most this many pixels
#: per side AND at most this many total cells (aspect-preserving downscale when
#: the total-cell cap bites).
GEOCLAW_MAX_PX_PER_SIDE: int = 2500
GEOCLAW_MAX_TOTAL_CELLS: int = 5_000_000

#: fgmax time-column sentinel: GeoClaw writes an extreme value (|t| > 1e8) at a
#: point the wave never reached. The reader maps these (and any negative time)
#: to NaN so the earliest-arrival nanmin is honest.
_FGMAX_SENTINEL_ABS: float = 1e8

logger = logging.getLogger("grace2_agent.workflows.postprocess_geoclaw")


class PostprocessGeoClawError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders
    a typed error frame. Codes used here:

    - ``GEOCLAW_OUTPUT_READ_FAILED`` — could not read a ``fort.q`` frame.
    - ``GEOCLAW_OUTPUT_EMPTY`` — no ``fort.q`` frames found / no wet cells.
    - ``GEOCLAW_DEPENDENCY_MISSING`` — numpy / rasterio not importable.
    - ``GEOCLAW_COG_WRITE_FAILED`` — rasterio could not write the depth COG.
    - ``GEOCLAW_CRS_TAG_MISMATCH`` — the COG CRS tag did not round-trip.
    - ``GEOCLAW_COG_UPLOAD_FAILED`` — the runs-bucket upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_GEOCLAW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# fort.q AMR ASCII frame parsing (pure numpy — unit-testable on a synthetic frame).
# --------------------------------------------------------------------------- #
#: A single AMR patch within a fort.q frame.
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
    """A GeoClaw fort.q header line is ``<value>    <field_name>``; return value."""
    m = _HEADER_VAL_RE.match(line)
    return m.group(1) if m else None


def parse_fort_q_frame(text: str) -> list[_Patch]:
    """Parse one GeoClaw ``fort.qNNNN`` frame's text into a list of AMR patches.

    GeoClaw fort.q ASCII format (per patch):
        <grid_number>    grid_number
        <AMR_level>      AMR_level
        <mx>             mx
        <my>             my
        <xlow>           xlow
        <ylow>           ylow
        <dx>             dx
        <dy>             dy
        <blank>
        q[0] q[1] q[2]    (mx*my rows, column-major: i fastest? -> GeoClaw writes
                           i (x) inner, j (y) outer; a blank line separates j rows)

    GeoClaw writes the patch data with the x-index (i) varying fastest within a
    y-row, y-rows separated by a blank line, ascending j (south->north). We read
    q[0] (depth h) into an ``(my, mx)`` array with row 0 = ylow. Multiple patches
    (one per AMR grid) may appear; we return them all. Pure numpy.
    """
    import numpy as np

    patches: list[_Patch] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        # Skip blank lines between patches.
        if not lines[i].strip():
            i += 1
            continue
        # Read the 8-field header (grid_number, AMR_level, mx, my, xlow, ylow,
        # dx, dy). Each is "<value>  <field_name>".
        header_vals: list[str] = []
        hdr_start = i
        while i < n and len(header_vals) < 8:
            v = _header_value(lines[i])
            if v is None:
                break
            header_vals.append(v)
            i += 1
        if len(header_vals) < 8:
            # Not a valid header start; advance to avoid an infinite loop.
            i = hdr_start + 1
            continue
        _grid_no = int(float(header_vals[0]))
        level = int(float(header_vals[1]))
        mx = int(float(header_vals[2]))
        my = int(float(header_vals[3]))
        xlow = float(header_vals[4])
        ylow = float(header_vals[5])
        dx = float(header_vals[6])
        dy = float(header_vals[7])

        h = np.full((my, mx), np.nan, dtype="float64")
        # Read mx*my data rows. GeoClaw writes i (x) inner loop, j (y) outer,
        # ascending j; rows of a single j are contiguous, j-blocks separated by a
        # blank line. We read row-by-row, filling (j, i) = h-value.
        count = 0
        j = 0
        col = 0
        while i < n and count < mx * my:
            ln = lines[i].strip()
            i += 1
            if not ln:
                # Blank line = end of a j-row block (GeoClaw separates y-rows).
                if col != 0:
                    j += 1
                    col = 0
                continue
            parts = ln.split()
            if not parts:
                continue
            try:
                hv = float(parts[0])  # q[0] = water depth h
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


def _frame_time_from_t_header(text: str) -> float | None:
    """Read the frame time from a ``fort.tNNNN`` header (first field = time)."""
    for line in text.splitlines():
        v = _header_value(line)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def compute_geoclaw_grid_shape(
    bbox: tuple[float, float, float, float],
    *,
    target_res_m: float = GEOCLAW_TARGET_GROUND_RES_M,
    min_px_per_side: int = GEOCLAW_MIN_PX_PER_SIDE,
    max_px_per_side: int = GEOCLAW_MAX_PX_PER_SIDE,
    max_total_cells: int = GEOCLAW_MAX_TOTAL_CELLS,
) -> tuple[int, int]:
    """Adaptive output raster ``(H, W)`` for an AOI at a target ground resolution.

    Sizes the GeoClaw depth raster from the AOI's REAL ground extent so the
    overland run-up rasterizes at ~``target_res_m`` (matching the finest AMR /
    CoNED nearshore, SFINCS-parity) instead of a fixed 256x256 grid that made
    cells 33-53 m over an ~8 km AOI (chunky specks). ``H`` from the latitude span,
    ``W`` from the longitude span with a ``cos(mean_lat)`` correction so the metric
    aspect ratio is honest.

    Bounded on both ends:
      - FLOOR ``min_px_per_side`` — never coarser than the legacy 256; a tiny AOI
        gets a FINER-than-target grid, never a coarser one.
      - CAP ``max_px_per_side`` per side AND ``max_total_cells`` overall
        (aspect-preserving downscale) so a huge AOI can't produce a monster COG.

    Pure arithmetic — unit-testable.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon <= min_lon or max_lat <= min_lat:
        return (min_px_per_side, min_px_per_side)

    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    aoi_h_m = (max_lat - min_lat) * m_per_deg_lat
    aoi_w_m = (max_lon - min_lon) * m_per_deg_lon

    res = max(float(target_res_m), 1e-6)
    nrows = int(round(aoi_h_m / res))
    ncols = int(round(aoi_w_m / res))

    # Floor to the legacy minimum, then cap per side.
    nrows = min(max(nrows, min_px_per_side), max_px_per_side)
    ncols = min(max(ncols, min_px_per_side), max_px_per_side)

    # Cap total cells (aspect-preserving); re-apply the floor afterwards so an
    # extreme aspect ratio can't drop a side below the legacy minimum.
    if nrows * ncols > max_total_cells:
        scale = math.sqrt(max_total_cells / float(nrows * ncols))
        nrows = max(min_px_per_side, int(nrows * scale))
        ncols = max(min_px_per_side, int(ncols * scale))

    return (nrows, ncols)


def rasterize_frame_to_grid(
    patches: list[_Patch],
    bbox: tuple[float, float, float, float],
    out_shape: tuple[int, int],
) -> Any:
    """Rasterize a frame's AMR patches onto a regular AOI grid (finest wins).

    Builds an ``(H, W)`` depth grid over ``bbox`` (EPSG:4326), row 0 = NORTH (the
    standard COG orientation). Each AMR patch cell PAINTS its full footprint —
    every output cell whose centre falls inside that patch cell's ``dx``/``dy``
    extent takes its depth (area/coverage fill), NOT a single nearest-cell
    scatter. That is what keeps the field GAP-FREE when the output grid is FINER
    than a coarse AMR patch: at the old resolution a coarse-patch cell mapped to
    one output cell and left its neighbours NaN (a speckled, holey grid); the
    coverage fill spans every output cell the patch cell covers instead.

    Patches are sorted by AMR level ASCENDING so a finer (higher-level) patch is
    painted LAST and OVERWRITES a coarser one where they overlap (the existing
    finest-wins semantics). Only wet (>= ``NODATA_DEPTH_M``) patch cells write, so
    a finer patch's dry cells never erase a coarser patch's wet value (unchanged
    from the scatter version). Dry / sub-threshold / uncovered cells stay NaN.
    Fully vectorized per patch (inverse sampling: each output cell -> the patch
    cell that contains its centre) — unit-testable on a synthetic patch list.
    """
    import numpy as np

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon <= min_lon or max_lat <= min_lat:
        return grid
    gdx = (max_lon - min_lon) / ncols
    gdy = (max_lat - min_lat) / nrows

    # Output cell-centre coordinates (row 0 = north -> descending latitude).
    xcen = min_lon + (np.arange(ncols) + 0.5) * gdx  # lon centres, west->east
    ycen = max_lat - (np.arange(nrows) + 0.5) * gdy  # lat centres, north->south

    for patch in sorted(patches, key=lambda p: p.level):
        if patch.mx <= 0 or patch.my <= 0 or patch.dx <= 0 or patch.dy <= 0:
            continue
        p_xmin = patch.xlow
        p_xmax = patch.xlow + patch.mx * patch.dx
        p_ymin = patch.ylow
        p_ymax = patch.ylow + patch.my * patch.dy
        # Output columns / rows whose centres fall inside the patch footprint.
        cols = np.nonzero((xcen >= p_xmin) & (xcen < p_xmax))[0]
        rows = np.nonzero((ycen >= p_ymin) & (ycen < p_ymax))[0]
        if cols.size == 0 or rows.size == 0:
            continue
        # Containing-patch-cell index for each covered output col / row (paint the
        # full dx/dy footprint: every output cell in the span maps to one patch
        # cell, so there are no interior gaps at a finer output resolution).
        pi = ((xcen[cols] - patch.xlow) / patch.dx).astype(np.intp)
        pj = ((ycen[rows] - patch.ylow) / patch.dy).astype(np.intp)
        np.clip(pi, 0, patch.mx - 1, out=pi)
        np.clip(pj, 0, patch.my - 1, out=pj)
        # Gather the (rows x cols) sub-block of patch depths (row 0 of `patch.h`
        # is ylow=south; `rows` is north->south, so pj already indexes correctly).
        sub = patch.h[np.ix_(pj, pi)]
        wet = np.isfinite(sub) & (sub >= NODATA_DEPTH_M)
        if not wet.any():
            continue
        block = grid[np.ix_(rows, cols)]
        block[wet] = sub[wet]
        grid[np.ix_(rows, cols)] = block
    return grid


# --------------------------------------------------------------------------- #
# Pure metric math (unit-testable on a synthetic peak grid).
# --------------------------------------------------------------------------- #
def compute_geoclaw_depth_metrics(
    peak_grid: Any,
    *,
    bbox: tuple[float, float, float, float],
    topo_grid: Any = None,
) -> dict[str, Any]:
    """Compute the three narration scalars from the PEAK depth grid.

    Pure arithmetic over the masked peak grid (sub-threshold + dry already NaN):

      - ``max_depth_m``       global max over the wet cells (0.0 if all dry).
      - ``flooded_area_km2``  (#wet cells) * mean-cell-area (km^2). The cell area
        is computed from the AOI extent + grid shape with a cos(lat) correction.
      - ``max_inundation_m``  max overland depth on DRY-LAND cells (cells whose
        topography > 0, i.e. above the still-water datum) — the run-up signal.
        When ``topo_grid`` is None we fall back to ``max_depth_m`` (honest: we
        cannot separate ocean depth from land run-up without topo).

    Also returns ``mean_depth_m`` / ``p95_depth_m`` / ``flooded_cell_count`` for
    parity with the SFINCS/SWMM ``peak_metrics`` dict.
    """
    import math

    import numpy as np

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
        return {
            "max_depth_m": 0.0,
            "mean_depth_m": 0.0,
            "p95_depth_m": 0.0,
            "flooded_cell_count": 0,
            "flooded_area_km2": 0.0,
            "max_inundation_m": 0.0,
            "arrival_time_s": None,
        }

    flooded_cell_count = int(wet.size)
    max_inundation = float(np.nanmax(wet))
    if topo_grid is not None:
        try:
            topo = np.asarray(topo_grid, dtype="float64")
            if topo.shape == arr.shape:
                land = topo > 0.0
                land_wet = arr[wet_mask & land]
                max_inundation = (
                    float(np.nanmax(land_wet)) if land_wet.size else 0.0
                )
        except Exception:  # noqa: BLE001 — metric is best-effort
            pass

    return {
        "max_depth_m": float(np.nanmax(wet)),
        "mean_depth_m": float(np.nanmean(wet)),
        "p95_depth_m": float(np.nanpercentile(wet, 95)),
        "flooded_cell_count": flooded_cell_count,
        "flooded_area_km2": flooded_cell_count * cell_area_m2 / 1_000_000.0,
        "max_inundation_m": max_inundation,
        # arrival_time_s comes ONLY from a real fgmax run (read_fgmax_output);
        # the between-frame fort.q metrics cannot supply a wave-arrival time, so
        # this is None here (the honesty floor: never narrate a fabricated time).
        "arrival_time_s": None,
    }


# --------------------------------------------------------------------------- #
# fgmax (fixed-grid maximum) reader (GAP1 - hand-rolled, NO clawpack import).
# --------------------------------------------------------------------------- #
def read_fgmax_output(
    out_dir: str | Path,
    *,
    fgno: int = 1,
    arrival_tol_m: float = GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M,
) -> dict[str, Any] | None:
    """Read a GeoClaw fgmax (fixed-grid maximum) output into the depth scalars.

    GeoClaw's fgmax monitor records, per fixed-grid point, the TRUE between-frame
    peak (max depth + max speed) and the wave arrival time - quantities the
    discrete fort.q frame snapshots cannot recover (the peak can fall between two
    output frames). This is a HAND-ROLLED reader (mirroring the hand-rolled fort.q
    reader above): it keeps the agent venv clawpack-free - there is NO
    ``clawpack``/``geoclaw`` import here, only ``numpy.loadtxt``.

    Expected layout (a real GeoClaw 5.14.0 run with ``num_fgmax_val=2``):
        ``<out_dir>/_output/fgmax{fgno:04d}.txt`` - 9 space-separated columns:
            col0 x (lon)            col4 h     (max water depth, m)
            col1 y (lat)            col5 s     (max speed, m/s)
            col2 amr_level (int)    col6 t_hmax (time of max depth, s)
            col3 B (topo, m; <0 offshore)  col7 t_smax (time of max speed, s)
                                    col8 arrival_time (s)
        ``<out_dir>/_output/fgmax_grids.data`` - the grid geometry header.

    Sentinels: GeoClaw writes an EXTREME value (|t| > 1e8) in a time column for a
    point the wave NEVER reached; the reader maps those (and any negative time) to
    NaN so the earliest-arrival ``nanmin`` is honest.

    Returns ``None`` (NOT an error) when the fgmax file OR its grids header is
    absent - a dam_break / surge run (or a tsunami run with fgmax disabled) simply
    did not produce fgmax output, which is not fatal: the caller keeps the fort.q
    metrics and reports ``arrival_time_s=None``.

    Returns (when present):
        ``{"max_depth_m", "max_inundation_m", "arrival_time_s",
           "grid": {"x", "y", "h", "B", "arrival_time"}}`` where:
          - ``max_depth_m``      = nanmax(h) over all fgmax points.
          - ``max_inundation_m`` = nanmax(h) over ON-LAND points (B > 0) - the
            overland run-up signal (0.0 when no land point is wet).
          - ``arrival_time_s``   = earliest arrival over points whose recorded max
            depth exceeds ``arrival_tol_m`` (nan-safe); ``None`` when no such point
            arrived (all-NaN).
    """
    import numpy as np

    out = Path(out_dir)
    base = out / "_output"
    if not base.is_dir():
        base = out
    fgmax_path = base / f"fgmax{fgno:04d}.txt"
    grids_path = base / "fgmax_grids.data"
    if not fgmax_path.exists() or not grids_path.exists():
        return None

    try:
        arr = np.loadtxt(fgmax_path, comments="#")
    except Exception as exc:  # noqa: BLE001 - fgmax is best-effort overlay
        logger.warning(
            "read_fgmax_output: could not parse %s (%s); ignoring fgmax",
            fgmax_path,
            exc,
        )
        return None

    arr = np.atleast_2d(np.asarray(arr, dtype="float64"))
    # Require EXACTLY 9 columns (num_fgmax_val=2, the layout our deck pins). A
    # 15-column file (num_fgmax_val=5) would put arrival_time at col14 and a
    # depth-minimum at col8, so a loose ">= 9" guard would silently read the WRONG
    # arrival column. We pin 9 and otherwise degrade to the fort.q metrics.
    if arr.size == 0 or arr.shape[1] != 9:
        logger.warning(
            "read_fgmax_output: %s has %d columns (expected exactly 9 for "
            "num_fgmax_val=2); ignoring fgmax",
            fgmax_path,
            arr.shape[1] if arr.ndim == 2 else 0,
        )
        return None

    x = arr[:, 0]
    y = arr[:, 1]
    B = arr[:, 3].copy()
    h = arr[:, 4].copy()
    arrival = arr[:, 8].copy()

    # NEVER-SET sentinel -> NaN. GeoClaw initializes EVERY fgmax valuemax (h, B,
    # tmax, arrival) to FG_NOTSET = -0.99999e99 and only overwrites updated points
    # (fgmax_module.f90). FG_NOTSET is FINITE, so without this mask an all-never-set
    # grid (a weak run, or an fgmax grid entirely on high ground) would make
    # nanmax(h) ~ -9.9999e98 -> a NEGATIVE max_depth_m that crashes the
    # GeoClawDepthLayerURI(ge=0.0) validator. Mirror the canonical reader's
    # `h < -1e50` mask (fgmax_tools.py): never-set points become NaN -> an honest
    # max_depth_m=0.0 / arrival=None degrade.
    notset = h < -1e50
    h[notset] = np.nan
    B[notset] = np.nan

    # Sentinel -> NaN: a never-arrived point carries |t| > 1e8 (or t < 0).
    sentinel = (np.abs(arrival) > _FGMAX_SENTINEL_ABS) | (arrival < 0.0)
    arrival[sentinel] = np.nan

    # max depth over all points (NaN-safe; empty -> 0.0).
    finite_h = h[np.isfinite(h)]
    max_depth_m = float(np.nanmax(h)) if finite_h.size else 0.0

    # inundation = max depth on land (B > 0).
    land = B > 0.0
    land_h = h[land & np.isfinite(h)]
    max_inundation_m = float(np.nanmax(land_h)) if land_h.size else 0.0

    # earliest on-land-ish arrival: points whose recorded peak depth is wet.
    wet = np.isfinite(h) & (h > arrival_tol_m)
    wet_arrival = arrival[wet]
    if wet_arrival.size and np.isfinite(wet_arrival).any():
        arrival_time_s: float | None = float(np.nanmin(wet_arrival))
    else:
        arrival_time_s = None

    return {
        "max_depth_m": max_depth_m,
        "max_inundation_m": max_inundation_m,
        "arrival_time_s": arrival_time_s,
        "grid": {
            "x": x,
            "y": y,
            "h": h,
            "B": B,
            "arrival_time": arrival,
        },
    }


# --------------------------------------------------------------------------- #
# COG write (EPSG:4326 grid) + CRS round-trip guard.
# --------------------------------------------------------------------------- #
#: stage -> (GeoClaw error_code) map (STEP 1 dedupe; byte-identical codes).
_GEOCLAW_STAGE_CODES: dict[str, str] = {
    "DEPENDENCY": "GEOCLAW_DEPENDENCY_MISSING",
    "WRITE": "GEOCLAW_COG_WRITE_FAILED",
    "REPROJECT": "GEOCLAW_COG_WRITE_FAILED",
    "CRS_MISMATCH": "GEOCLAW_CRS_TAG_MISMATCH",
    "UPLOAD": "GEOCLAW_COG_UPLOAD_FAILED",
}


def _reraise_cogio(
    exc: CogIoError, *, bbox: tuple[float, float, float, float] | None = None
) -> "PostprocessGeoClawError":
    """Map a cog_io ``CogIoError`` onto the GeoClaw typed error (preserves codes)."""
    code = _GEOCLAW_STAGE_CODES.get(exc.stage, "POSTPROCESS_GEOCLAW_FAILED")
    details = dict(exc.details)
    if bbox is not None and "bbox" not in details:
        details["bbox"] = list(bbox)
    return PostprocessGeoClawError(code, message=exc.message, details=details)


def _write_depth_cog_4326(
    grid: Any,
    bbox: tuple[float, float, float, float],
) -> Path:
    """Write a masked ``(H, W)`` EPSG:4326 depth grid (row 0 = north) to a COG.

    The grid is already in EPSG:4326 over ``bbox`` (rasterize_frame_to_grid builds
    it north-up), so no reprojection is needed. Thin shim over
    ``cog_io.write_cog_4326_from_grid`` (STEP 1 dedupe; ``reproject=False``): build
    the affine from the bbox + shape, write the COG directly, run the CRS
    round-trip guard. Byte-identical to the pre-dedupe writer.
    """
    import numpy as np
    from rasterio.transform import from_bounds

    arr = np.asarray(grid, dtype="float32")
    nrows, ncols = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)

    try:
        return cog_io.write_cog_4326_from_grid(
            arr,
            src_crs="EPSG:4326",
            src_transform=transform,
            reproject=False,
            crs_roundtrip_guard=True,
            dst_suffix="_geoclaw_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc, bbox=bbox) from exc


def _safe_unlink(p: Path) -> None:
    cog_io.safe_unlink(p)


# --------------------------------------------------------------------------- #
# Upload (scheme-aware) — mirrors postprocess_swmm._upload_cog_to_runs_bucket.
# --------------------------------------------------------------------------- #
def _upload_cog_to_runs_bucket(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None = None,
    *,
    dest_filename: str = "geoclaw_depth_peak.tif",
) -> str:
    """Upload the staged COG to ``{scheme}://<runs_bucket>/<run_id>/<dest_filename>``.

    Thin shim over ``cog_io.upload_cog`` (STEP 1 dedupe; byte-identical):
    scheme-aware via ``cache.storage_scheme()`` - ``s3`` via boto3
    (``ContentType=image/tiff``), ``gs`` via fsspec (default bucket
    ``RUNS_BUCKET_DEFAULT``, RAISES on failure). Per-frame callers pass a DISTINCT
    ``dest_filename`` so each frame lands at its own object key (no dedup collapse).
    """
    try:
        return cog_io.upload_cog(
            local_cog,
            run_id,
            runs_bucket,
            dest_filename=dest_filename,
            content_type="image/tiff",
            gs_backend="fsspec",
            gs_fallback_to_file=False,
            runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="GeoClaw depth COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


# --------------------------------------------------------------------------- #
# fort.q frame discovery + read.
# --------------------------------------------------------------------------- #
def _discover_frames(out_dir: Path) -> list[tuple[int, Path, Path | None]]:
    """List ``(frame_no, fort.qNNNN, fort.tNNNN | None)`` ascending by frame_no.

    GeoClaw writes ``fort.q0000``, ``fort.q0001``, ... under ``_output/`` (or the
    given dir directly). The matching ``fort.tNNNN`` carries the frame time.
    """
    q_re = re.compile(r"^fort\.q(\d{4,})$")
    found: list[tuple[int, Path, Path | None]] = []
    search_dirs = [out_dir]
    sub = out_dir / "_output"
    if sub.is_dir():
        search_dirs.insert(0, sub)
    seen: set[int] = set()
    for d in search_dirs:
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
            t_path = p.with_name(p.name.replace("fort.q", "fort.t", 1))
            found.append((no, p, t_path if t_path.exists() else None))
    found.sort(key=lambda x: x[0])
    return found


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_geoclaw(
    out_dir: str | Path,
    bbox: tuple[float, float, float, float],
    *,
    run_id: str,
    scenario: str = "dam_break",
    grid_shape: tuple[int, int] | None = None,
    target_ground_res_m: float = GEOCLAW_TARGET_GROUND_RES_M,
    runs_bucket: str | None = None,
    topo_grid: Any = None,
    mask_ocean: bool = False,
    fgmax_arrival_tol_m: float = GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M,
) -> tuple[list[GeoClawDepthLayerURI], dict[str, Any]]:
    """Rasterize a solved GeoClaw run into a peak + per-frame depth-COG layer set.

    Reads the ``fort.q`` AMR frames from ``out_dir`` (the downloaded ``_output/``),
    rasterizes each frame's depth onto a regular ``grid_shape`` EPSG:4326 grid over
    ``bbox`` (finer AMR patches win), selects the PEAK frame (largest total wet
    depth), writes the PEAK + up to ``MAX_FLOOD_FRAMES`` per-frame depth COGs,
    uploads them, and returns the EXACT ``(layers, metrics)`` shape
    ``postprocess_flood`` returns so the Phase-1 scrubber path consumes it
    unchanged.

    When ``grid_shape`` is ``None`` (the live default) the output raster is sized
    ADAPTIVELY from the AOI at ``target_ground_res_m`` metres/pixel via
    ``compute_geoclaw_grid_shape`` (floor 256, capped for huge AOIs) so the run-up
    band is a smooth, dense sheet rather than chunky ~256x256 specks. The peak
    grid, every frame grid, AND ``topo_grid`` share this one shape (they are
    compared cell-for-cell for ``max_inundation_m``).

    Args:
        out_dir: directory containing the GeoClaw fort.q frames (or an ``_output/``
            subdir).
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326 — the raster
            extent + zoom-to bbox.
        run_id: the run identifier the COGs are keyed under in the runs bucket.
        scenario: the GeoClaw driver family (echoed onto the layers).
        grid_shape: the regular output raster ``(H, W)`` to rasterize the AMR
            frames onto. ``None`` (default) -> adaptive from ``bbox`` +
            ``target_ground_res_m``.
        target_ground_res_m: target ground resolution (m/px) for the adaptive
            shape when ``grid_shape`` is None (ignored when a shape is passed).
        runs_bucket: optional override for the runs bucket name.
        topo_grid: optional ``(H, W)`` topography grid (same shape) for the
            ``max_inundation_m`` land/ocean split AND (with ``mask_ocean``) the
            belt-and-suspenders ``topo < 0`` OR-term of the overland depth mask.
        mask_ocean: when True, mask the published depth (peak + every frame +
            metrics) to OVERLAND inundation only — set depth to NaN wherever the
            cell is PERMANENT WATER (ocean). Permanent water is detected by the
            SIMULATION'S OWN INITIAL WATER SURFACE (robust on any coast): any cell
            WET at ``t=0`` (the earliest fort.q frame ``grids[0]`` — GeoClaw's
            still-water initial condition ``h = max(0, sea_level - B)``) is ocean,
            using a small wet epsilon (``NODATA_DEPTH_M``) so only genuinely-wet sea
            is caught even if an Okada ``dtopo`` perturbs the ``t=0`` surface. This
            replaces the old ``topo < 0`` criterion, which failed on ETOPO coasts
            (no CUDEM) where the nearshore bathymetry reads ~0 m (not negative) and
            so caught only far-offshore deep cells — the nearshore sea stayed in the
            published COG. When a shape-matching ``topo_grid`` is supplied, ``topo <
            0`` is OR-ed in as a belt-and-suspenders term (a cell that is either
            initially-wet OR below the still-water datum is ocean) so nothing
            regresses on CUDEM coasts. A strict NO-OP when NO cell is initially wet
            AND (no topo cell is < 0), so it can never erase a legitimate inland
            flood. The composer gates this to the OFFSHORE/COASTAL scenario families
            (tsunami / surge); inland ``dam_break`` stays unmasked.
        fgmax_arrival_tol_m: the fgmax wet-cell threshold (m) backing
            ``arrival_time_s`` when an fgmax monitor was run.

    Returns:
        ``(layers, metrics)``: ``layers[0]`` peak ``GeoClawDepthLayerURI`` +
        ``layers[1:]`` per-frame; ``metrics`` the peak aggregates dict.

    Raises:
        PostprocessGeoClawError: any read / rasterize / COG-write / upload failure.
    """
    try:
        import numpy as np  # noqa: F401 — vouch the import path
    except Exception as exc:  # noqa: BLE001
        raise PostprocessGeoClawError(
            "GEOCLAW_DEPENDENCY_MISSING",
            message=f"numpy unavailable for GeoClaw postprocess: {exc}",
        ) from exc

    out = Path(out_dir)
    frame_files = _discover_frames(out)
    if not frame_files:
        raise PostprocessGeoClawError(
            "GEOCLAW_OUTPUT_EMPTY",
            message=f"no fort.q frames found under {out}",
            details={"out_dir": str(out)},
        )

    import numpy as np

    # Adaptive output raster (None -> size from the AOI at the target ground
    # resolution; floor 256, capped for huge AOIs). Peak + every frame + topo_grid
    # all share this ONE shape (cell-for-cell comparison for max_inundation_m).
    if grid_shape is None:
        grid_shape = compute_geoclaw_grid_shape(
            bbox, target_res_m=target_ground_res_m
        )
        logger.info(
            "postprocess_geoclaw run_id=%s adaptive output grid H=%d W=%d "
            "(~%.0f m/px target) over bbox=%s",
            run_id,
            grid_shape[0],
            grid_shape[1],
            target_ground_res_m,
            tuple(bbox),
        )

    grids: list[Any] = []
    for _no, q_path, _t_path in frame_files:
        try:
            patches = parse_fort_q_frame(q_path.read_text(errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise PostprocessGeoClawError(
                "GEOCLAW_OUTPUT_READ_FAILED",
                message=f"could not read {q_path.name}: {exc}",
                details={"frame": q_path.name},
            ) from exc
        grids.append(rasterize_frame_to_grid(patches, bbox, grid_shape))

    # --- Overland (initial-wet ocean-masked) inundation -------------------- #
    # For an OFFSHORE / COASTAL scenario (tsunami / surge) whose domain reaches the
    # open sea, GeoClaw's water DEPTH (q[0]=h) is the FULL water column, so the
    # ocean portion of the AOI renders as a sheet of sea rather than the coastal
    # flood. OVERLAND INUNDATION = dry-land cells that got wet, i.e. cells DRY at
    # t=0 that are wet in a later frame. So the ocean (PERMANENT WATER) is exactly
    # the set of cells WET AT t=0: the earliest fort.q frame (grids[0], sorted by
    # frame number) is GeoClaw's still-water initial condition h=max(0,sea_level-B).
    # This initial-wet criterion is robust on ANY coast — it replaces the old
    # `topo<0` test that FAILED on ETOPO coasts (no CUDEM) where nearshore bathy
    # reads ~0 m (not negative), leaving the nearshore sea in the published COG.
    # A small wet epsilon (NODATA_DEPTH_M) means only genuinely-wet sea is caught,
    # robust even if an Okada dtopo perturbs the t=0 surface offshore.
    # `topo<0` (when an aligned topo_grid is supplied) is OR-ed in as a
    # belt-and-suspenders term (initially-wet OR below-datum = ocean) so nothing
    # regresses on CUDEM coasts. Applied to EVERY frame so PEAK, per-frame COGs, and
    # all derived metrics are consistently overland. Guarded so a legitimate inland
    # flood is never erased: (1) the composer only sets mask_ocean for tsunami/surge
    # (inland dam_break stays unmasked), (2) a strict no-op when NO cell is wet at
    # t=0 AND no topo cell is < 0.
    if mask_ocean:
        try:
            # PRIMARY: any cell wet at t=0 is permanent water (the ocean).
            init = np.asarray(grids[0], dtype="float64")
            ocean = np.isfinite(init) & (init > NODATA_DEPTH_M)
            n_initwet = int(ocean.sum())
            # ADDITIONAL OR (CUDEM belt-and-suspenders): below the still-water datum.
            n_topo = 0
            if topo_grid is not None:
                topo = np.asarray(topo_grid, dtype="float64")
                if topo.shape == tuple(grid_shape):
                    topo_ocean = np.isfinite(topo) & (topo < 0.0)
                    n_topo = int(topo_ocean.sum())
                    ocean = ocean | topo_ocean
                else:
                    logger.warning(
                        "postprocess_geoclaw run_id=%s topo_grid shape %s != output "
                        "grid %s; ocean mask uses initial-wet only (no topo<0 OR)",
                        run_id,
                        tuple(topo.shape),
                        tuple(grid_shape),
                    )
            n_ocean = int(ocean.sum())
            if n_ocean:
                for _i in range(len(grids)):
                    gi = np.asarray(grids[_i], dtype="float64").copy()
                    gi[ocean] = np.nan
                    grids[_i] = gi
                logger.info(
                    "postprocess_geoclaw run_id=%s masked %d/%d ocean cells "
                    "(initial-wet=%d, topo<0=%d) -> overland inundation (was total "
                    "water column)",
                    run_id,
                    n_ocean,
                    int(ocean.size),
                    n_initwet,
                    n_topo,
                )
            else:
                logger.info(
                    "postprocess_geoclaw run_id=%s mask_ocean requested but no "
                    "initial-wet or topo<0 cells (no permanent water) — no-op",
                    run_id,
                )
        except Exception as exc:  # noqa: BLE001 — mask is best-effort; never sink the run
            logger.warning(
                "postprocess_geoclaw run_id=%s ocean mask failed (%s); publishing "
                "unmasked total-depth",
                run_id,
                exc,
            )

    n_steps = len(grids)

    # --- PEAK grid (max-total-depth step) ---
    best_grid = None
    best_sum = -1.0
    for g in grids:
        s = float(np.nansum(g))
        if s > best_sum:
            best_sum = s
            best_grid = g
    peak_grid = best_grid if best_grid is not None else np.full(grid_shape, np.nan)

    metrics = compute_geoclaw_depth_metrics(
        peak_grid, bbox=bbox, topo_grid=topo_grid
    )
    metrics["crs"] = "EPSG:4326"

    # --- fgmax override (GAP1) ----------------------------------------------
    # fort.q snapshots can MISS the true between-frame peak; when an fgmax monitor
    # ran (tsunami/surge run-up), its fixed-grid maximum is the authoritative peak
    # + the only source of a wave-arrival time. Override the depth/inundation
    # scalars with the fgmax values and set arrival_time_s. When fgmax is absent
    # (dam_break / surge / fgmax disabled) read_fgmax_output returns None and we
    # KEEP the fort.q metrics with arrival_time_s=None (honesty floor: no
    # fabricated arrival).
    metrics.setdefault("arrival_time_s", None)
    fgmax = read_fgmax_output(out, arrival_tol_m=fgmax_arrival_tol_m)
    if fgmax is not None:
        metrics["max_depth_m"] = float(fgmax["max_depth_m"])
        metrics["max_inundation_m"] = float(fgmax["max_inundation_m"])
        metrics["arrival_time_s"] = fgmax["arrival_time_s"]
        metrics["fgmax_used"] = True

    # When the depth is masked to overland, the narrated PEAK depth must be the
    # land run-up too — otherwise the fort.q peak grid is ocean-masked but an fgmax
    # override could re-inject the deep-ocean max (fgmax's max_depth_m is over ALL
    # points, sea included). Pin max_depth_m to the on-land inundation max so the
    # scalar matches the published overland COG (honest: it is the max depth on dry
    # land = the run-up depth). The unmasked (no ocean) case is untouched because
    # there max_inundation_m already equals max_depth_m. Applies whenever the ocean
    # mask ran (initial-wet works without a topo_grid; when topo_grid is None the
    # metric's max_inundation already falls back to max_depth so this is a no-op).
    if mask_ocean:
        metrics["max_depth_m"] = float(metrics.get("max_inundation_m", 0.0))

    logger.info(
        "postprocess_geoclaw run_id=%s scenario=%s n_steps=%d max_depth_m=%.4g "
        "flooded_area_km2=%.6g max_inundation_m=%.4g fgmax_used=%s "
        "arrival_time_s=%s",
        run_id,
        scenario,
        n_steps,
        metrics["max_depth_m"],
        metrics["flooded_area_km2"],
        metrics["max_inundation_m"],
        bool(fgmax is not None),
        metrics.get("arrival_time_s"),
    )

    # --- PEAK layer (always layers[0]) ---
    peak_cog = _write_depth_cog_4326(peak_grid, bbox)
    try:
        peak_uri = _upload_cog_to_runs_bucket(
            peak_cog, run_id, runs_bucket, dest_filename="geoclaw_depth_peak.tif"
        )
    finally:
        _safe_unlink(peak_cog)

    layers: list[GeoClawDepthLayerURI] = [
        GeoClawDepthLayerURI(
            layer_id=f"geoclaw-depth-peak-{run_id}",
            name="Peak flood depth",
            layer_type="raster",
            uri=peak_uri,
            style_preset=GEOCLAW_DEPTH_STYLE_PRESET,
            role="primary",
            units="meters",
            bbox=tuple(bbox),
            max_depth_m=float(metrics["max_depth_m"]),
            flooded_area_km2=float(metrics["flooded_area_km2"]),
            max_inundation_m=float(metrics["max_inundation_m"]),
            arrival_time_s=metrics.get("arrival_time_s"),
            scenario=scenario,  # type: ignore[arg-type]
        )
    ]

    # --- per-frame layers (engine-agnostic flood animation, Phase 1) ---
    if n_steps > 1:
        frame_indices = _select_frame_time_indices(n_steps)
        frame_layers = _emit_frame_layers(
            grids,
            frame_indices,
            bbox=bbox,
            run_id=run_id,
            runs_bucket=runs_bucket,
            scenario=scenario,
        )
        if len(frame_layers) >= 2:
            layers.extend(frame_layers)
        else:
            logger.info(
                "postprocess_geoclaw: < 2 frame layers (%d) — emitting peak only "
                "(no animation group) for run_id=%s",
                len(frame_layers),
                run_id,
            )

    if len(layers) > 1:
        logger.info(
            "postprocess_geoclaw: emitted peak layer + %d time-step frames "
            "(animation group) for run_id=%s",
            len(layers) - 1,
            run_id,
        )
    return layers, metrics


def _emit_frame_layers(
    grids: list[Any],
    frame_indices: list[int],
    *,
    bbox: tuple[float, float, float, float],
    run_id: str,
    runs_bucket: str | None,
    scenario: str,
) -> list[GeoClawDepthLayerURI]:
    """Write + upload the per-frame depth COGs as contiguous ``step N`` layers.

    A single corrupt frame must NOT sink the whole animation OR the peak layer:
    on a frame write/upload failure we clean up the partial frames and return
    ``[]`` (the caller degrades to peak-only). Mirrors postprocess_swmm.
    """
    import numpy as np

    frame_layers: list[GeoClawDepthLayerURI] = []
    written_cogs: list[Path] = []
    try:
        for frame_no, t_idx in enumerate(frame_indices, start=1):
            grid_t = grids[t_idx]
            frame_cog = _write_depth_cog_4326(grid_t, bbox)
            written_cogs.append(frame_cog)
            wet = np.asarray(grid_t, dtype="float64")
            wet = wet[np.isfinite(wet)]
            fm = compute_geoclaw_depth_metrics(grids[t_idx], bbox=bbox)
            frame_uri = _upload_cog_to_runs_bucket(
                frame_cog,
                run_id,
                runs_bucket,
                dest_filename=f"geoclaw_depth_frame_{frame_no:02d}.tif",
            )
            _safe_unlink(frame_cog)
            written_cogs.pop()
            frame_layers.append(
                GeoClawDepthLayerURI(
                    layer_id=f"geoclaw-depth-frame-{frame_no:02d}-{run_id}",
                    name=f"Flood depth step {frame_no}",
                    layer_type="raster",
                    uri=frame_uri,
                    style_preset=GEOCLAW_DEPTH_STYLE_PRESET,
                    role="context",
                    units="meters",
                    bbox=tuple(bbox),
                    max_depth_m=float(fm["max_depth_m"]),
                    flooded_area_km2=float(fm["flooded_area_km2"]),
                    max_inundation_m=float(fm["max_inundation_m"]),
                    scenario=scenario,  # type: ignore[arg-type]
                )
            )
    except PostprocessGeoClawError as exc:
        logger.warning(
            "postprocess_geoclaw: a frame COG write/upload failed (%s); degrading "
            "to peak-only (no animation group).",
            exc,
        )
        for p in written_cogs:
            _safe_unlink(p)
        return []
    return frame_layers
