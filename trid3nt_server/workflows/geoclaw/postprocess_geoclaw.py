"""GeoClaw (Clawpack) run-output postprocessing.

``postprocess_geoclaw(out_dir, run_args, *, run_id, ...) -> (layers, metrics)``
reads the GeoClaw ``fort.q`` AMR ASCII frames from a solved run's ``_output/``
directory, rasterizes each frame's water DEPTH (``q[0] = h``) onto a regular
EPSG:4326 grid over the AOI, masks dry/sub-threshold cells to NaN, and emits the
SAME ``(layers, metrics)`` shape as ``postprocess_flood`` / ``postprocess_swmm``
so the Phase-1 flood-animation scrubber path consumes it UNCHANGED:

  - ``layers[0]`` = the PEAK overland-depth COG, role ``"primary"``, name
    ``"Peak flood depth"``, style preset ``continuous_flood_depth``. It is a
    :class:`~trid3nt_contracts.geoclaw_contracts.GeoClawDepthLayerURI` carrying
    the three narration scalars (``max_depth_m`` / ``flooded_area_km2`` /
    ``max_inundation_m``) + the echoed scenario.
  - ``layers[1:]`` = up to ``MAX_FLOOD_FRAMES`` per-frame depth COGs, role
    ``"context"``, names ``"Flood depth step N"`` -- the EXACT web
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
(Invariant 1): the depth scalars are computed with plain arithmetic
from the depth grid -- no LLM anywhere; the agent narrates the typed fields, never
invents them.

Tier separation (Invariant 5): the COG lands in the runs bucket (scheme-aware
via ``cache.storage_scheme()``); the agent does not re-render -- ``publish_layer``
/ TiTiler serves the tiles from the URI on the envelope.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.geoclaw_contracts import (
    GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M,
    GEOCLAW_DEPTH_STYLE_PRESET,
    GeoClawDepthLayerURI,
)

from trid3nt_server.workflows.shared import cog_io
from trid3nt_server.workflows.shared.cog_io import CogIoError

# Reuse the SFINCS postprocess constants/helpers (single source of truth so the
# GeoClaw + SFINCS + SWMM animation paths stay byte-compatible on the web side).
from trid3nt_server.workflows.shared.cog_io import (
    NODATA_DEPTH_M,
    RUNS_BUCKET_DEFAULT,
)
from trid3nt_server.workflows.shared.frames import (
    MAX_FLOOD_FRAMES,
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
    "parse_geoclaw_gauge_series",
    "build_gauge_timeseries_chart_spec",
    "parse_geoclaw_particle_tracks",
    "build_geoclaw_particle_track_geojson",
    "make_geoclaw_particle_track_layer_uri",
    "build_geoclaw_particle_track_layer",
    "build_particle_track_chart_spec",
    "GEOCLAW_PARTICLE_TRACK_STYLE_PRESET",
    "GEOCLAW_MESH_STYLE_PRESET",
    "build_geoclaw_mesh_geojson",
    "make_geoclaw_mesh_layer_uri",
    "build_geoclaw_mesh_layer",
    "GEOCLAW_DEFORMATION_STYLE_PRESET",
    "build_geoclaw_deformation_layer",
    "compute_thacker_vandv",
    "build_thacker_validation_chart_spec",
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

#: AMR mesh (grid-line) emission. The mesh preview is the RAW GRID: every AMR
#: patch's actual cell edges as LineStrings, all levels in ONE FeatureCollection.
#: Refinement is self-evident (a finer patch draws a denser grid), so there is
#: no per-level colour/weight coding -- the plugin styles it ONE colour via the
#: ``mesh_grid`` preset. A patch with at most this many cells emits its FULL
#: cell-edge grid; a larger/finer patch (where every edge would be megabytes)
#: emits its boundary plus interior lines DECIMATED to a sample stride. The
#: decimation is STATED per-feature (``decimated`` + ``sample_stride_x/y``) and
#: in the FeatureCollection ``metadata`` (honesty floor: the preview declares
#: where it is a faithful full grid vs a sampled one).
GEOCLAW_MESH_STYLE_PRESET: str = "mesh_grid"
#: Lagrangian particle-track vector style (the plugin draws the drift paths as
#: LineStrings). The tracks ARE a product layer (the wake / drifter path), not a
#: mesh abstraction, so they carry their own preset.
GEOCLAW_PARTICLE_TRACK_STYLE_PRESET: str = "particle_track"
#: Metres per degree of latitude (spherical mean) for track-length arithmetic.
_M_PER_DEG_LAT: float = 111_320.0
GEOCLAW_MESH_FULL_CELLLINES_MAX_CELLS: int = 2500
GEOCLAW_MESH_SAMPLE_LINES_PER_SIDE: int = 40
GEOCLAW_MESH_COORD_DECIMALS: int = 7
#: Payload guard: warn (never fail) when the serialized mesh preview exceeds this.
GEOCLAW_MESH_PAYLOAD_SOFT_CAP_MB: float = 8.0

logger = logging.getLogger("trid3nt_server.workflows.geoclaw.postprocess_geoclaw")


class PostprocessGeoClawError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders
    a typed error frame. Codes used here:

    - ``GEOCLAW_OUTPUT_READ_FAILED`` -- could not read a ``fort.q`` frame.
    - ``GEOCLAW_OUTPUT_EMPTY`` -- no ``fort.q`` frames found / no wet cells.
    - ``GEOCLAW_DEPENDENCY_MISSING`` -- numpy / rasterio not importable.
    - ``GEOCLAW_COG_WRITE_FAILED`` -- rasterio could not write the depth COG.
    - ``GEOCLAW_CRS_TAG_MISMATCH`` -- the COG CRS tag did not round-trip.
    - ``GEOCLAW_COG_UPLOAD_FAILED`` -- the runs-bucket upload of the COG failed.
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
# fort.q AMR ASCII frame parsing (pure numpy -- unit-testable on a synthetic frame).
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
      - FLOOR ``min_px_per_side`` -- never coarser than the legacy 256; a tiny AOI
        gets a FINER-than-target grid, never a coarser one.
      - CAP ``max_px_per_side`` per side AND ``max_total_cells`` overall
        (aspect-preserving downscale) so a huge AOI can't produce a monster COG.

    Pure arithmetic -- unit-testable.
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
    standard COG orientation). Each AMR patch cell PAINTS its full footprint --
    every output cell whose centre falls inside that patch cell's ``dx``/``dy``
    extent takes its value (area/coverage fill), NOT a single nearest-cell
    scatter. That keeps the field GAP-FREE when the output grid is FINER than a
    coarse AMR patch.

    Finest-available level wins per area, UNCONDITIONALLY. Patches are painted
    coarse-to-fine (level ASCENDING) and a per-cell ``painted_level`` records the
    finest patch that has touched each output cell. A patch OWNS every covered
    cell whose recorded level is ``<=`` its own; on an owned cell it writes its
    depth when wet (``>= NODATA_DEPTH_M``) and NaN when dry. So a finer patch's
    DRY cell ERASES a coarser patch's wet value: the depth over any area is the
    finest patch's solution there, never a coarse patch cell smeared across the
    footprint of a finer patch that resolves the ground as dry. Dry /
    sub-threshold / uncovered cells are NaN. Fully vectorized per patch (inverse
    sampling: each output cell -> the patch cell that contains its centre) --
    unit-testable on a synthetic patch list.
    """
    import numpy as np

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    # Finest AMR level that has painted each output cell (0 = untouched); a patch
    # owns a cell iff its level >= the recorded level.
    painted_level = np.zeros((nrows, ncols), dtype=np.int32)
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
        block = grid[np.ix_(rows, cols)]
        lvl_block = painted_level[np.ix_(rows, cols)]
        own = patch.level >= lvl_block  # finest-or-equal patch owns the cell
        wet = np.isfinite(sub) & (sub >= NODATA_DEPTH_M)
        block[own & wet] = sub[own & wet]  # finest wet depth
        block[own & ~wet] = np.nan  # finest DRY erases a coarser wet value
        grid[np.ix_(rows, cols)] = block
        lvl_block[own] = patch.level
        painted_level[np.ix_(rows, cols)] = lvl_block
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
        topography > 0, i.e. above the still-water datum) -- the run-up signal.
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
        except Exception:  # noqa: BLE001 -- metric is best-effort
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
# Upload (scheme-aware) -- mirrors postprocess_swmm._upload_cog_to_runs_bucket.
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


#: fgout ascii-frame name (``fgout0001.q0007``): a fixed-grid monitor number then
#: the frame number. output_format='ascii' lands each frame in the SAME fort.q
#: uniform-grid layout (a single uniform patch), so ``parse_fort_q_frame`` +
#: ``rasterize_frame_to_grid`` read them with NO AMR flatten and NO clawpack import.
_FGOUT_Q_RE = re.compile(r"^fgout\d+\.q(\d{4,})$")


def _discover_fgout_frames(out_dir: Path) -> list[tuple[int, Path, Path | None]]:
    """List ``(frame_no, fgoutNNNN.qMMMM, .tMMMM | None)`` ascending by frame_no.

    The fgout monitor (setrun ``FGoutGrid``, gated by ``fgout_frames > 0``) writes
    a SMOOTH single-resolution frame series (``fgout0001.q0001``, ``.q0002``, ...)
    at EVENLY-SPACED times, decoupled from the coarse/variable fort.q AMR-patch
    cadence. Discovered separately from ``_discover_frames`` so an fgout run keeps
    the fort.q peak while the fgout frames BECOME the animation series.
    """
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
            m = _FGOUT_Q_RE.match(p.name)
            if not m:
                continue
            no = int(m.group(1))
            if no in seen:
                continue
            seen.add(no)
            t_path = p.with_name(p.name.replace(".q", ".t", 1))
            found.append((no, p, t_path if t_path.exists() else None))
    found.sort(key=lambda x: x[0])
    return found


def _read_frames_to_grids(
    frame_files: list[tuple[int, Path, Path | None]],
    bbox: tuple[float, float, float, float],
    grid_shape: tuple[int, int],
) -> list[Any]:
    """Parse + rasterize each ``fort.q``/``fgout`` frame onto the regular AOI grid.

    One uniform-grid read path for BOTH the AMR fort.q frames and the uniform
    fgout frames (the fgout ascii layout IS the fort.q layout). Raises the typed
    ``GEOCLAW_OUTPUT_READ_FAILED`` on an unreadable frame."""
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
    return grids


# --------------------------------------------------------------------------- #
# AMR mesh (grid-line) preview -- the RAW GRID as a first-class per-run product.
#
# GeoClaw's adaptive mesh lives ONLY in the fort.q per-patch headers (each patch:
# level, mx, my, xlow, ylow, dx, dy). This turns that structure into an emitted
# vector layer of the ACTUAL cell edges, all levels in one FeatureCollection, so
# refinement is visible as grid DENSITY (a finer patch = a denser grid) with no
# per-level abstraction. Pure numpy-free arithmetic -- unit-testable on a
# synthetic patch list.
# --------------------------------------------------------------------------- #
def build_geoclaw_mesh_geojson(
    patches: list[_Patch],
    *,
    frame_no: int | None = None,
    full_max_cells: int = GEOCLAW_MESH_FULL_CELLLINES_MAX_CELLS,
    sample_lines: int = GEOCLAW_MESH_SAMPLE_LINES_PER_SIDE,
    coord_decimals: int = GEOCLAW_MESH_COORD_DECIMALS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the RAW AMR grid-line ``FeatureCollection`` from a frame's patches.

    Each AMR patch becomes ONE ``MultiLineString`` feature holding its cell-edge
    grid lines in EPSG:4326 (``xlow``/``ylow`` are lon/lat under GeoClaw's
    spherical ``coordinate_system=2``). All levels land in ONE collection; the
    finer a patch, the denser its lines -- the honest, self-evident picture of
    where the solver refined.

    Decimation (STATED, never silent): a patch with at most ``full_max_cells``
    cells emits EVERY interior cell edge (a full grid). A larger/finer patch --
    where every edge would be megabytes -- emits its BOUNDARY (i=0, i=mx, j=0,
    j=my always included) plus interior lines sampled at a stride that keeps ~
    ``sample_lines`` per side. Each feature carries ``decimated`` +
    ``sample_stride_x/y``; the FeatureCollection ``metadata`` foreign member
    summarizes the policy + per-level histogram.

    Returns ``(feature_collection, stats)``.
    """
    import math

    def _rd(v: float) -> float:
        return round(float(v), coord_decimals)

    features: list[dict[str, Any]] = []
    total_lines = 0
    total_vertices = 0
    level_hist: dict[int, int] = {}
    decimated_patches = 0

    for gi, p in enumerate(sorted(patches, key=lambda q: q.level), start=1):
        mx, my = int(p.mx), int(p.my)
        if mx <= 0 or my <= 0 or p.dx <= 0 or p.dy <= 0:
            continue
        x0, y0 = float(p.xlow), float(p.ylow)
        x1 = x0 + mx * float(p.dx)
        y1 = y0 + my * float(p.dy)
        n_cells = mx * my
        decimate = n_cells > full_max_cells
        if decimate:
            sx = max(1, math.ceil(mx / max(sample_lines, 1)))
            sy = max(1, math.ceil(my / max(sample_lines, 1)))
            decimated_patches += 1
        else:
            sx = sy = 1
        # Line indices: sampled stride ALWAYS unioned with the boundary (0, mx/my)
        # so a decimated patch still draws its full outline.
        xi = sorted(set(range(0, mx + 1, sx)) | {0, mx})
        yj = sorted(set(range(0, my + 1, sy)) | {0, my})
        segs: list[list[list[float]]] = []
        for i in xi:  # vertical lines (constant lon), south->north
            lon = _rd(x0 + i * float(p.dx))
            segs.append([[lon, _rd(y0)], [lon, _rd(y1)]])
        for j in yj:  # horizontal lines (constant lat), west->east
            lat = _rd(y0 + j * float(p.dy))
            segs.append([[_rd(x0), lat], [_rd(x1), lat]])
        n_lines = len(segs)
        total_lines += n_lines
        total_vertices += 2 * n_lines
        level_hist[int(p.level)] = level_hist.get(int(p.level), 0) + 1
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "MultiLineString", "coordinates": segs},
                "properties": {
                    "grid_number": gi,
                    "level": int(p.level),
                    "mx": mx,
                    "my": my,
                    "cell_dx_deg": _rd(p.dx),
                    "cell_dy_deg": _rd(p.dy),
                    "n_grid_lines": n_lines,
                    "decimated": bool(decimate),
                    "sample_stride_x": int(sx),
                    "sample_stride_y": int(sy),
                },
            }
        )

    max_level = max(level_hist) if level_hist else 0
    level_hist_str = {str(k): v for k, v in sorted(level_hist.items())}
    metadata = {
        "kind": "geoclaw_amr_gridlines",
        "frame_no": frame_no,
        "crs": "EPSG:4326",
        "patch_count": len(features),
        "level_histogram": level_hist_str,
        "max_level": max_level,
        "total_grid_lines": total_lines,
        "total_vertices": total_vertices,
        "decimated_patch_count": decimated_patches,
        "decimation_policy": (
            f"patches with <= {full_max_cells} cells emit every cell edge (full "
            f"grid); larger patches emit their boundary plus interior lines "
            f"sampled to ~{sample_lines} per side (per-feature 'decimated' + "
            f"sample_stride_x/y). Grid density IS the AMR refinement, unabstracted."
        ),
    }
    fc = {"type": "FeatureCollection", "features": features, "metadata": metadata}
    stats = {
        "patch_count": len(features),
        "max_level": max_level,
        "total_grid_lines": total_lines,
        "total_vertices": total_vertices,
        "decimated_patch_count": decimated_patches,
        "level_histogram": level_hist_str,
        "frame_no": frame_no,
    }
    return fc, stats


def make_geoclaw_mesh_layer_uri(
    fc: dict[str, Any],
    mesh_stats: dict[str, Any],
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> LayerURI | None:
    """Upload the AMR grid-line ``FeatureCollection`` to S3, return a LayerURI.

    Mirrors ``make_hecras_mesh_layer_uri``: writes ``mesh.geojson`` to the durable
    runs bucket at ``s3://<runs_bucket>/<run_id>/mesh.geojson`` and returns a
    ``style_preset="mesh_grid"``, ``role="context"``, ``bbox=None`` vector LayerURI
    (the mesh must not fight the flood camera) carrying ``crs_authid="EPSG:4326"``.
    Grid lines are a LineString FeatureCollection, so the renderable
    QGIS type is a VECTOR (QgsVectorLayer draws the raw black grid); the row still
    rides the mesh-preview protocol (mesh_grid preset + context role + crs_authid).

    Best-effort: ``None`` on an empty FC or an S3 fault (a missing mesh preview
    never voids the depth result). SYNC boto3 put -- wrap in ``asyncio.to_thread``.
    """
    import json as _json

    features = fc.get("features") or []
    if not features:
        return None
    body = _json.dumps(fc, separators=(",", ":")).encode("utf-8")
    payload_mb = len(body) / 1_000_000.0
    mesh_stats["payload_mb"] = round(payload_mb, 4)
    if payload_mb > GEOCLAW_MESH_PAYLOAD_SOFT_CAP_MB:
        logger.warning(
            "geoclaw mesh preview is large (%.2f MB, %d grid lines) run_id=%s -- "
            "emitting anyway (decimation already applied per-patch)",
            payload_mb,
            int(mesh_stats.get("total_grid_lines", 0) or 0),
            run_id,
        )
    try:
        from trid3nt_server.data.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
        )

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/mesh.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 -- best-effort mesh preview
        logger.warning(
            "make_geoclaw_mesh_layer_uri: mesh.geojson S3 upload failed (non-fatal, "
            "run_id=%s): %s",
            run_id,
            exc,
        )
        return None

    max_level = int(mesh_stats.get("max_level", 0) or 0)
    n_lines = int(mesh_stats.get("total_grid_lines", 0) or 0)
    return LayerURI(
        layer_id=f"geoclaw-mesh-{run_id}",
        name=f"Computational mesh (AMR L1-L{max_level}, {n_lines} grid lines)",
        layer_type="vector",
        uri=s3_uri,
        style_preset=GEOCLAW_MESH_STYLE_PRESET,
        role="context",
        bbox=None,
        crs_authid="EPSG:4326",
    )


def build_geoclaw_mesh_layer(
    out_dir: str | Path,
    *,
    run_id: str,
    runs_bucket: str | None = None,
    frame_no: int | None = None,
) -> LayerURI | None:
    """Build + upload the AMR grid-line mesh preview from a solved run's fort.q.

    Reads the PEAK-relevant frame -- ``frame_no`` when given, else the LAST/final
    frame (a pinned AMR window persists across every frame, so the final frame
    faithfully shows the user's refinement) -- parses its patch structure, builds
    the grid-line FeatureCollection, and uploads it as ``mesh.geojson``.

    The ONE shared seam every GeoClaw inundation template rides (all templates
    dispatch through ``model_geoclaw_inundation``). Best-effort: returns ``None``
    on ANY failure (a missing mesh preview never voids the depth result).
    """
    try:
        out = Path(out_dir)
        frames = _discover_frames(out)
        if not frames:
            return None
        if frame_no is not None:
            chosen = next((f for f in frames if f[0] == frame_no), frames[-1])
        else:
            chosen = frames[-1]
        _no, q_path, _t = chosen
        patches = parse_fort_q_frame(q_path.read_text(errors="replace"))
        if not patches:
            return None
        fc, stats = build_geoclaw_mesh_geojson(patches, frame_no=_no)
        layer = make_geoclaw_mesh_layer_uri(
            fc, stats, run_id=run_id, runs_bucket=runs_bucket
        )
        if layer is not None:
            logger.info(
                "build_geoclaw_mesh_layer run_id=%s frame_no=%d patches=%d "
                "max_level=%d grid_lines=%d vertices=%d payload_mb=%.3f uri=%s",
                run_id,
                _no,
                stats["patch_count"],
                stats["max_level"],
                stats["total_grid_lines"],
                stats["total_vertices"],
                float(stats.get("payload_mb", 0.0) or 0.0),
                layer.uri,
            )
        return layer
    except Exception as exc:  # noqa: BLE001 -- mesh preview is NEVER fatal
        logger.warning(
            "build_geoclaw_mesh_layer failed (non-fatal, run_id=%s): %s",
            run_id,
            exc,
        )
        return None


# --------------------------------------------------------------------------- #
# Okada seafloor-deformation PRODUCT (the Okada-dtopo front).
# --------------------------------------------------------------------------- #
#: Signed vertical seafloor deformation (m): uplift(+)/subsidence(-) -> a diverging
#: rdbu ramp centered on 0 (publish_layer pins the symmetric rescale so the dipole
#: reads blue=subsidence / white=0 / red=uplift). Registered in
#: publish_layer._QGIS_STYLE_REGISTRY.
GEOCLAW_DEFORMATION_STYLE_PRESET: str = "diverging_seafloor_deformation"


def _read_esri_ascii_grid(
    path: Path,
) -> tuple[Any, tuple[float, float, float, float]]:
    """Parse a bare ESRI-ASCII grid -> ``(north-up float32 array, EPSG:4326 bbox)``.

    The tsunami ``maketopo.py`` writes ``deformation_dz.asc`` NORTH-first (row 0 =
    highest latitude), so the returned array is already row-0=north (the COG-writer
    convention). NODATA -> NaN. Pure (numpy only, no gdal)."""
    import numpy as np

    hdr: dict[str, float] = {}
    rows: list[list[float]] = []
    with path.open("r", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            key = s.split()[0].lower()
            if key in ("ncols", "nrows", "xllcorner", "yllcorner", "xllcenter",
                       "yllcenter", "cellsize", "nodata_value"):
                hdr[key] = float(s.split()[1])
            else:
                rows.append([float(v) for v in s.split()])
    ncols, nrows = int(hdr["ncols"]), int(hdr["nrows"])
    cell = float(hdr["cellsize"])
    xll = float(hdr.get("xllcorner", hdr.get("xllcenter", 0.0) - cell / 2.0))
    yll = float(hdr.get("yllcorner", hdr.get("yllcenter", 0.0) - cell / 2.0))
    nodata = float(hdr.get("nodata_value", -9999.0))
    arr = np.asarray(rows, dtype="float32").reshape(nrows, ncols)
    arr = np.where(arr == nodata, np.nan, arr)
    bbox = (xll, yll, xll + ncols * cell, yll + nrows * cell)
    return arr, bbox


def build_geoclaw_deformation_layer(
    out_dir: str | Path,
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> tuple[LayerURI | None, dict[str, float]]:
    """Rasterize the Okada seafloor-deformation dZ into a SIGNED product COG + layer.

    Reads the tsunami ``deformation_dz.asc`` (final-time vertical dZ the worker's
    ``maketopo.py`` wrote over the Okada source box), writes a signed EPSG:4326 COG,
    uploads it, and returns ``(LayerURI, {"max_uplift_m", "max_subsidence_m"})`` --
    the direct answer to "what seafloor deformation does this earthquake drive"
    (MODELED, not observed). Returns ``(None, {})`` when no deformation grid is
    present (dam_break / surge / a staged dtopo run) or the grid is degenerate.
    Best-effort: NEVER raises (the depth answer stands on its own)."""
    try:
        import numpy as np
        from rasterio.transform import from_bounds

        candidates = sorted(Path(out_dir).rglob("deformation_dz.asc"))
        if not candidates:
            return None, {}
        grid, dbbox = _read_esri_ascii_grid(candidates[0])
        finite = grid[np.isfinite(grid)]
        if finite.size == 0 or float(np.nanmax(np.abs(grid))) == 0.0:
            return None, {}

        nrows, ncols = grid.shape
        transform = from_bounds(dbbox[0], dbbox[1], dbbox[2], dbbox[3], ncols, nrows)
        try:
            local_cog = cog_io.write_cog_4326_from_grid(
                np.asarray(grid, dtype="float32"),
                src_crs="EPSG:4326",
                src_transform=transform,
                reproject=False,
                crs_roundtrip_guard=True,
                dst_suffix="_geoclaw_deformation_4326.tif",
            )
        except CogIoError as exc:
            raise _reraise_cogio(exc, bbox=dbbox) from exc
        try:
            uri = _upload_cog_to_runs_bucket(
                local_cog, run_id, runs_bucket,
                dest_filename="geoclaw_seafloor_deformation.tif",
            )
        finally:
            _safe_unlink(local_cog)

        max_uplift = float(np.nanmax(grid))
        max_subsidence = float(np.nanmin(grid))
        layer = LayerURI(
            layer_id=f"geoclaw-seafloor-deformation-{run_id}",
            name="Seafloor deformation (Okada)",
            layer_type="raster",
            uri=uri,
            style_preset=GEOCLAW_DEFORMATION_STYLE_PRESET,
            role="context",
            units="meters",
            bbox=tuple(dbbox),  # type: ignore[arg-type]
            fallback_note=(
                f"modeled Okada coseismic deformation: max uplift {max_uplift:.3g} m, "
                f"max subsidence {max_subsidence:.3g} m (NOT an observed field)"
            ),
        )
        logger.info(
            "build_geoclaw_deformation_layer run_id=%s uplift=%.4g m "
            "subsidence=%.4g m grid=%dx%d bbox=%s uri=%s",
            run_id, max_uplift, max_subsidence, nrows, ncols, dbbox, uri,
        )
        return layer, {
            "max_uplift_m": max_uplift,
            "max_subsidence_m": max_subsidence,
        }
    except Exception as exc:  # noqa: BLE001 -- the deformation product is NEVER fatal
        logger.warning(
            "build_geoclaw_deformation_layer failed (non-fatal, run_id=%s): %s",
            run_id, exc,
        )
        return None, {}


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
    sea_level_m: float = 0.0,
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
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326 -- the raster
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
            ``topo <= sea_level_m`` water OR-term of the overland depth mask.
        mask_ocean: when True, mask the published depth (peak + every frame +
            metrics) to OVERLAND inundation only -- set depth to NaN on every
            PERMANENT-WATER (ocean) cell so what remains is depth on dry land.
            A cell is water when EITHER: (1) it is WET at ``t=0`` -- the earliest
            fort.q frame ``grids[0]``, GeoClaw's still-water initial condition
            ``h = max(0, sea_level - B)`` -- using a small wet epsilon
            (``NODATA_DEPTH_M``) so only genuinely-wet sea counts even if an Okada
            ``dtopo`` perturbs the ``t=0`` surface (robust on any coast, including
            ETOPO coasts whose nearshore bathymetry reads ~0 m); OR (2) a
            shape-matching ``topo_grid`` puts it AT OR BELOW the still-water datum
            (``topo <= sea_level_m``) -- overland is strictly ``topo > sea_level_m``.
            A strict NO-OP when no cell is initially wet AND no topo cell is at or
            below the datum, so it can never erase a legitimate inland flood. The
            composer gates this to the OFFSHORE/COASTAL scenario families
            (tsunami / surge); inland ``dam_break`` stays unmasked.
        sea_level_m: still-water datum (m) for the overland/water split; a cell is
            water when ``topo <= sea_level_m`` (default 0.0).
        fgmax_arrival_tol_m: the fgmax wet-cell threshold (m) backing
            ``arrival_time_s`` when an fgmax monitor was run.

    Returns:
        ``(layers, metrics)``: ``layers[0]`` peak ``GeoClawDepthLayerURI`` +
        ``layers[1:]`` per-frame; ``metrics`` the peak aggregates dict.

    Raises:
        PostprocessGeoClawError: any read / rasterize / COG-write / upload failure.
    """
    try:
        import numpy as np  # noqa: F401 -- vouch the import path
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

    grids: list[Any] = _read_frames_to_grids(frame_files, bbox, grid_shape)

    # --- Overland (ocean-masked) inundation -------------------------------- #
    # For an OFFSHORE / COASTAL scenario (tsunami / surge) whose domain reaches the
    # open sea, GeoClaw's water DEPTH (q[0]=h) is the FULL water column, so the
    # ocean portion of the AOI renders as a sheet of sea rather than the coastal
    # flood. Published inundation is depth on DRY LAND only. A cell is PERMANENT
    # WATER (masked to NaN) when EITHER:
    #   (1) it is WET at t=0 -- the earliest fort.q frame (grids[0]) is GeoClaw's
    #       still-water initial condition h=max(0,sea_level-B); a small wet epsilon
    #       (NODATA_DEPTH_M) catches only genuinely-wet sea, robust on ANY coast
    #       (including ETOPO coasts whose nearshore bathy reads ~0 m) even if an
    #       Okada dtopo perturbs the t=0 surface offshore; OR
    #   (2) an aligned topo_grid puts it AT OR BELOW the still-water datum
    #       (topo <= sea_level_m) -- overland is strictly topo > sea_level_m.
    # Applied to EVERY frame so PEAK, per-frame COGs, and all derived metrics are
    # consistently overland. Guarded so a legitimate inland flood is never erased:
    # (1) the composer only sets mask_ocean for tsunami/surge (inland dam_break
    # stays unmasked), (2) a strict no-op when no cell is wet at t=0 AND no topo
    # cell is at or below the datum.
    #
    # The resolved ``ocean_mask`` (the permanent-water cells) is retained so the
    # SAME mask is applied to the fgout animation frames below -- the fort.q t=0
    # still-water frame is the authoritative ocean reference for both series.
    ocean_mask: Any = None
    if mask_ocean:
        try:
            # PRIMARY: any cell wet at t=0 is permanent water (the ocean).
            init = np.asarray(grids[0], dtype="float64")
            ocean = np.isfinite(init) & (init > NODATA_DEPTH_M)
            n_initwet = int(ocean.sum())
            # ADDITIONAL OR: a cell AT OR BELOW the still-water datum is water, not
            # overland -- published inundation is depth on dry land (topo >
            # sea_level) only. The `<=` (not `<`) catches the nearshore sea on
            # ETOPO coasts whose bathymetry reads ~0 m at the waterline.
            n_topo = 0
            if topo_grid is not None:
                topo = np.asarray(topo_grid, dtype="float64")
                if topo.shape == tuple(grid_shape):
                    topo_ocean = np.isfinite(topo) & (topo <= sea_level_m)
                    n_topo = int(topo_ocean.sum())
                    ocean = ocean | topo_ocean
                else:
                    logger.warning(
                        "postprocess_geoclaw run_id=%s topo_grid shape %s != output "
                        "grid %s; ocean mask uses initial-wet only (no topo OR)",
                        run_id,
                        tuple(topo.shape),
                        tuple(grid_shape),
                    )
            n_ocean = int(ocean.sum())
            if n_ocean:
                ocean_mask = ocean
                for _i in range(len(grids)):
                    gi = np.asarray(grids[_i], dtype="float64").copy()
                    gi[ocean] = np.nan
                    grids[_i] = gi
                logger.info(
                    "postprocess_geoclaw run_id=%s masked %d/%d ocean cells "
                    "(initial-wet=%d, topo<=datum=%d) -> overland inundation (was "
                    "total water column)",
                    run_id,
                    n_ocean,
                    int(ocean.size),
                    n_initwet,
                    n_topo,
                )
            else:
                logger.info(
                    "postprocess_geoclaw run_id=%s mask_ocean requested but no "
                    "initial-wet or topo<=datum cells (no permanent water) - no-op",
                    run_id,
                )
        except Exception as exc:  # noqa: BLE001 -- mask is best-effort; never sink the run
            logger.warning(
                "postprocess_geoclaw run_id=%s ocean mask failed (%s); publishing "
                "unmasked total-depth",
                run_id,
                exc,
            )

    n_steps = len(grids)

    # --- fgout SMOOTH animation frames (when the run emitted them) ------------
    # The fgout monitor (gated by fgout_frames > 0) dumps a uniform single-
    # resolution grid at EVENLY-SPACED times -- a smooth animation cadence
    # decoupled from the coarse/variable fort.q AMR-patch output. When present the
    # fgout frames BECOME the scrubber animation series; the fort.q peak (+ any
    # fgmax override) still supplies the PEAK layer + narration scalars. The same
    # ocean mask (from the fort.q t=0 still-water frame) is applied so the fgout
    # frames are overland-consistent with the peak. Absent -> the fort.q frames
    # remain the animation source (byte-identical to a pre-fgout run).
    fgout_files = _discover_fgout_frames(out)
    fgout_grids: list[Any] = []
    if fgout_files:
        try:
            fgout_grids = _read_frames_to_grids(fgout_files, bbox, grid_shape)
            if ocean_mask is not None:
                for _i in range(len(fgout_grids)):
                    gi = np.asarray(fgout_grids[_i], dtype="float64").copy()
                    gi[ocean_mask] = np.nan
                    fgout_grids[_i] = gi
            logger.info(
                "postprocess_geoclaw run_id=%s using %d fgout frames as the SMOOTH "
                "animation series (fort.q peak retained; %d fort.q frames)",
                run_id,
                len(fgout_grids),
                n_steps,
            )
        except PostprocessGeoClawError as exc:
            logger.warning(
                "postprocess_geoclaw run_id=%s fgout frame read failed (%s); "
                "falling back to the fort.q animation frames",
                run_id,
                exc,
            )
            fgout_grids = []

    # The animation series: fgout frames when present, else the fort.q frames.
    anim_grids = fgout_grids if fgout_grids else grids

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
    # land run-up too -- otherwise the fort.q peak grid is ocean-masked but an fgmax
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
    # ``anim_grids`` is the fgout smooth series when the run emitted one, else the
    # fort.q frames -- so the scrubber gets an evenly-spaced single-resolution
    # animation when fgout was requested, the coarse AMR cadence otherwise.
    n_anim = len(anim_grids)
    if n_anim > 1:
        frame_indices = _select_frame_time_indices(n_anim)
        frame_layers = _emit_frame_layers(
            anim_grids,
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


# --------------------------------------------------------------------------- #
# Coastal gauge time series (GAP4) -- the tsunami gauge-timeseries template.
#
# The GeoClaw worker always writes one coastal gauge (gaugeNNNNN.txt) under
# _output/. The standard GeoClaw gauge file has a "#"-commented header then
# numeric rows [level, t, q[0]=h, q[1]=hu, q[2]=hv, eta] (eta = water-surface
# elevation, the last column). We parse the surface-elevation time series so the
# composer can chart the wave (and any co-seismic subsidence, visible as the
# initial post-quake surface offset at the gauge).
# --------------------------------------------------------------------------- #
def parse_geoclaw_gauge_series(
    output_dir: str | Path,
) -> tuple[dict[str, Any] | None, dict[str, float]]:
    """Parse the coastal gauge time series from a solved run's ``_output/``.

    Finds the first ``gauge*.txt`` under ``output_dir`` (recursively -- the
    composer downloads it to ``<tmp>/_output/gauge00001.txt``), skips the
    ``#``-commented header, and reads the numeric rows. Columns follow the
    standard GeoClaw layout ``[level, t, h, hu, hv, eta]``; the surface elevation
    ``eta`` is the LAST column and the depth ``h`` is column index 2. Degrades
    honestly: a 3-column ``[level, t, eta]`` file uses eta=last, h=None.

    Returns ``(series, scalars)`` where ``series`` is
    ``{"t": [...], "eta": [...], "depth": [...]}`` (or ``None`` when no gauge
    file / no rows), and ``scalars`` carries the typed narration numbers:
    ``gauge_max_surface_elevation_m`` / ``gauge_min_surface_elevation_m`` /
    ``gauge_max_amplitude_m`` / ``gauge_coseismic_offset_m`` / ``gauge_max_depth_m``.
    Pure (unit-testable on a fixture gauge file)."""
    root = Path(output_dir)
    candidates = sorted(root.rglob("gauge*.txt"))
    if not candidates:
        return None, {}
    gauge_path = candidates[0]

    times: list[float] = []
    etas: list[float] = []
    depths: list[float] = []
    try:
        with gauge_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split()
                if len(parts) < 3:
                    continue
                try:
                    t = float(parts[1])
                    eta = float(parts[-1])
                except (ValueError, IndexError):
                    continue
                h = None
                if len(parts) >= 4:
                    try:
                        h = float(parts[2])
                    except ValueError:
                        h = None
                times.append(t)
                etas.append(eta)
                depths.append(h if h is not None else float("nan"))
    except OSError:
        return None, {}

    if not times:
        return None, {}

    import math

    finite_depths = [d for d in depths if math.isfinite(d)]
    eta0 = etas[0]
    max_eta = max(etas)
    min_eta = min(etas)
    scalars: dict[str, float] = {
        "gauge_max_surface_elevation_m": float(max_eta),
        "gauge_min_surface_elevation_m": float(min_eta),
        "gauge_max_amplitude_m": float(max_eta - min_eta),
        "gauge_coseismic_offset_m": float(eta0),
        "gauge_max_depth_m": float(max(finite_depths)) if finite_depths else 0.0,
    }
    series = {"t": times, "eta": etas, "depth": depths}
    return series, scalars


def build_gauge_timeseries_chart_spec(
    series: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Vega-Lite gauge surface-elevation time-series chart.

    A line of water-surface elevation (m) vs time (s) at the coastal gauge -- the
    tsunami waveform (leading depression / run-up peaks) and any co-seismic
    subsidence (the initial post-quake surface offset). Returns ``None`` when the
    series is empty. Pure (unit-testable on a synthetic series)."""
    if not series or not series.get("t"):
        return None
    values = [
        {"t_s": float(t), "eta_m": float(e)}
        for t, e in zip(series["t"], series["eta"])
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "line", "color": "#1f5fbf"},
        "encoding": {
            "x": {
                "field": "t_s",
                "type": "quantitative",
                "title": "time (s)",
            },
            "y": {
                "field": "eta_m",
                "type": "quantitative",
                "title": "surface elevation (m)",
            },
            "tooltip": [
                {"field": "t_s", "type": "quantitative", "format": ".0f"},
                {"field": "eta_m", "type": "quantitative", "format": ".3f"},
            ],
        },
    }


# --------------------------------------------------------------------------- #
# Lagrangian particle tracks -- the wake-tracking fold.
#
# A Lagrangian particle gauge is a gauge advected BY THE FLOW: GeoClaw replaces
# its q[2,3] output columns with the particle position (x(t), y(t)). The gauge
# file header carries "# Lagrangian particle, q[2,3] replaced by (x(t),y(t))"
# and the data rows are [level, t, h, xg, yg, eta]. We parse each such file into
# a drift TRACK (the sequence of (xg, yg) positions) and emit it as a LineString
# vector product + a cumulative-drift-distance chart. Pure python -- no clawpack.
# --------------------------------------------------------------------------- #
def _lonlat_step_m(
    lon0: float, lat0: float, lon1: float, lat1: float
) -> float:
    """Planar great-circle-approx distance (m) between two lon/lat points.

    Metres-per-degree with a ``cos(mean_lat)`` longitude correction -- the same
    convention the depth-metric + grid-shape helpers use (consistent, not WGS84
    geodesic-exact; a drift track spans metres, so the flat approx is negligible)."""
    import math

    mean_lat = 0.5 * (lat0 + lat1)
    m_per_deg_lon = _M_PER_DEG_LAT * max(math.cos(math.radians(mean_lat)), 1e-6)
    dx_m = (lon1 - lon0) * m_per_deg_lon
    dy_m = (lat1 - lat0) * _M_PER_DEG_LAT
    return math.hypot(dx_m, dy_m)


def parse_geoclaw_particle_tracks(
    output_dir: str | Path,
) -> list[dict[str, Any]]:
    """Parse Lagrangian particle-gauge drift tracks from a solved run's ``_output/``.

    Scans every ``gauge*.txt`` under ``output_dir`` (recursively), keeps only the
    LAGRANGIAN gauges (header line ``# Lagrangian particle``), and reads each into
    a drift track. A Lagrangian gauge row is ``[level, t, h, xg, yg, eta]`` where
    ``xg, yg`` are the advected particle position (lon, lat) that replaced hu, hv.

    Returns a list (ascending by gauge id) of
    ``{"gauge_id", "t": [...], "coords": [[lon, lat], ...], "length_m",
       "duration_s", "start", "end"}``; empty when no Lagrangian gauge is present
    (the plain inundation / Eulerian-gauge path). Pure -- unit-testable on a
    fixture gauge file (no clawpack, no numpy)."""
    root = Path(output_dir)
    tracks: list[dict[str, Any]] = []
    for gauge_path in sorted(root.rglob("gauge*.txt")):
        try:
            text = gauge_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        is_lagrangian = any(
            l.startswith("#") and "lagrangian particle" in l.lower() for l in lines
        )
        if not is_lagrangian:
            continue
        gauge_id = None
        for l in lines:
            if l.startswith("#") and "gauge_id=" in l:
                try:
                    gauge_id = int(l.split("gauge_id=")[1].split()[0])
                except (ValueError, IndexError):
                    gauge_id = None
                break
        times: list[float] = []
        coords: list[list[float]] = []
        for l in lines:
            s = l.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            if len(parts) < 5:
                continue
            try:
                t = float(parts[1])
                xg = float(parts[3])  # q[1] slot holds x(t) for a Lagrangian gauge
                yg = float(parts[4])  # q[2] slot holds y(t)
            except (ValueError, IndexError):
                continue
            times.append(t)
            coords.append([xg, yg])
        if len(coords) < 2:
            continue
        length_m = 0.0
        for a, b in zip(coords[:-1], coords[1:]):
            length_m += _lonlat_step_m(a[0], a[1], b[0], b[1])
        tracks.append(
            {
                "gauge_id": gauge_id if gauge_id is not None else len(tracks) + 1,
                "t": times,
                "coords": coords,
                "length_m": float(length_m),
                "duration_s": float(times[-1] - times[0]),
                "start": list(coords[0]),
                "end": list(coords[-1]),
            }
        )
    tracks.sort(key=lambda tr: int(tr["gauge_id"]))
    return tracks


def build_geoclaw_particle_track_geojson(
    tracks: list[dict[str, Any]],
    *,
    coord_decimals: int = GEOCLAW_MESH_COORD_DECIMALS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the particle-track ``FeatureCollection`` (one LineString per track).

    Each track becomes a ``LineString`` of its (lon, lat) drift positions in
    EPSG:4326, carrying ``gauge_id`` / ``track_length_m`` / ``duration_s`` /
    ``n_points`` properties. Returns ``(feature_collection, stats)``. Pure."""

    def _rd(v: float) -> float:
        return round(float(v), coord_decimals)

    features: list[dict[str, Any]] = []
    max_len = 0.0
    max_dur = 0.0
    for tr in tracks:
        coords = [[_rd(c[0]), _rd(c[1])] for c in tr["coords"]]
        if len(coords) < 2:
            continue
        max_len = max(max_len, float(tr["length_m"]))
        max_dur = max(max_dur, float(tr["duration_s"]))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "gauge_id": int(tr["gauge_id"]),
                    "n_points": len(coords),
                    "track_length_m": round(float(tr["length_m"]), 3),
                    "duration_s": round(float(tr["duration_s"]), 2),
                },
            }
        )
    metadata = {
        "kind": "geoclaw_lagrangian_particle_tracks",
        "crs": "EPSG:4326",
        "track_count": len(features),
        "max_track_length_m": round(max_len, 3),
        "max_duration_s": round(max_dur, 2),
    }
    fc = {"type": "FeatureCollection", "features": features, "metadata": metadata}
    stats = {
        "track_count": len(features),
        "max_track_length_m": max_len,
        "max_duration_s": max_dur,
    }
    return fc, stats


def make_geoclaw_particle_track_layer_uri(
    fc: dict[str, Any],
    stats: dict[str, Any],
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> LayerURI | None:
    """Upload the particle-track ``FeatureCollection`` to S3, return a LayerURI.

    Writes ``particles.geojson`` to the durable runs bucket and returns a
    ``particle_track`` vector LayerURI (role ``"context"``, ``bbox=None`` so the
    tracks never fight the flood camera) carrying ``crs_authid="EPSG:4326"``.
    Best-effort: ``None`` on an empty FC or an S3 fault. SYNC boto3 put -- the
    caller wraps it in ``asyncio.to_thread``."""
    import json as _json

    features = fc.get("features") or []
    if not features:
        return None
    body = _json.dumps(fc, separators=(",", ":")).encode("utf-8")
    try:
        from trid3nt_server.data.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
        )

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/particles.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 -- best-effort product layer
        logger.warning(
            "make_geoclaw_particle_track_layer_uri: particles.geojson upload "
            "failed (non-fatal, run_id=%s): %s",
            run_id,
            exc,
        )
        return None

    n_tracks = int(stats.get("track_count", 0) or 0)
    return LayerURI(
        layer_id=f"geoclaw-particles-{run_id}",
        name=f"Lagrangian particle tracks ({n_tracks} drifters)",
        layer_type="vector",
        uri=s3_uri,
        style_preset=GEOCLAW_PARTICLE_TRACK_STYLE_PRESET,
        role="context",
        bbox=None,
        crs_authid="EPSG:4326",
    )


def build_geoclaw_particle_track_layer(
    out_dir: str | Path,
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> tuple[LayerURI | None, list[dict[str, Any]]]:
    """Parse + upload the Lagrangian particle tracks from a solved run.

    Returns ``(layer, tracks)``: the ``particle_track`` vector LayerURI (or
    ``None`` when no Lagrangian gauge ran / an S3 fault) plus the parsed track
    dicts (for the chart + narration scalars). NEVER raises (best-effort)."""
    try:
        tracks = parse_geoclaw_particle_tracks(out_dir)
        if not tracks:
            return None, []
        fc, stats = build_geoclaw_particle_track_geojson(tracks)
        layer = make_geoclaw_particle_track_layer_uri(
            fc, stats, run_id=run_id, runs_bucket=runs_bucket
        )
        if layer is not None:
            logger.info(
                "build_geoclaw_particle_track_layer run_id=%s tracks=%d "
                "max_length_m=%.1f max_duration_s=%.0f uri=%s",
                run_id,
                stats["track_count"],
                float(stats["max_track_length_m"]),
                float(stats["max_duration_s"]),
                layer.uri,
            )
        return layer, tracks
    except Exception as exc:  # noqa: BLE001 -- particle tracks are NEVER fatal
        logger.warning(
            "build_geoclaw_particle_track_layer failed (non-fatal, run_id=%s): %s",
            run_id,
            exc,
        )
        return None, []


def build_particle_track_chart_spec(
    tracks: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Build the Vega-Lite cumulative-drift-distance chart for the particle tracks.

    A multi-series line of cumulative drift distance (m) vs time (s), ONE series
    per particle (colour + legend by gauge id) -- so each drifter's total travel
    and its rate are read off a quantitative axis (the spatial path itself is the
    map overlay). Returns ``None`` when there are no tracks. Pure."""
    if not tracks:
        return None
    values: list[dict[str, Any]] = []
    for tr in tracks:
        coords = tr["coords"]
        times = tr["t"]
        if len(coords) < 2:
            continue
        gid = int(tr["gauge_id"])
        cum = 0.0
        label = f"particle {gid}"
        values.append({"t_s": float(times[0]), "dist_m": 0.0, "particle": label})
        for k in range(1, len(coords)):
            cum += _lonlat_step_m(
                coords[k - 1][0], coords[k - 1][1], coords[k][0], coords[k][1]
            )
            values.append(
                {"t_s": float(times[k]), "dist_m": round(cum, 3), "particle": label}
            )
    if not values:
        return None
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "t_s", "type": "quantitative", "title": "time (s)"},
            "y": {
                "field": "dist_m",
                "type": "quantitative",
                "title": "cumulative drift (m)",
            },
            "color": {
                "field": "particle",
                "type": "nominal",
                "title": "particle",
            },
            "tooltip": [
                {"field": "particle", "type": "nominal"},
                {"field": "t_s", "type": "quantitative", "format": ".0f"},
                {"field": "dist_m", "type": "quantitative", "format": ".1f"},
            ],
        },
    }


# --------------------------------------------------------------------------- #
# Thacker (1981) paraboloid-basin V&V (scenario="thacker").
#
# Grades a solved bowl run against the closed-form radially-symmetric Thacker
# solution (trid3nt_contracts.geoclaw_thacker): the CENTER gauge (id 1) supplies
# the central surface elevation eta(0,t) -> numerical PERIOD (autocorrelation) +
# central AMPLITUDE; the dense +x-axis gauge line (ids 100+, radii 0..1.5a)
# supplies the moving SHORELINE (largest wet radius over time) + a closed-wall
# MASS-conservation proxy (ring-integrated volume drift). Pure numpy over the
# gauge files -- no clawpack; the deck + this grader share the analytic module so
# they agree by construction.
# --------------------------------------------------------------------------- #
def _parse_geoclaw_gauge_file(path: Path) -> tuple[list[float], list[float], list[float]]:
    """Parse ``(t, h, eta)`` columns from one ``gaugeNNNNN.txt`` (h=col2, eta=last)."""
    ts: list[float] = []
    hs: list[float] = []
    es: list[float] = []
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        p = s.split()
        if len(p) < 4:
            continue
        try:
            ts.append(float(p[1]))
            hs.append(float(p[2]))
            es.append(float(p[-1]))
        except ValueError:
            continue
    return ts, hs, es


def compute_thacker_vandv(
    out_dir: str | Path,
    a_m: float,
    h0_m: float,
    amp_A: float,
    *,
    dry_tol_m: float = 5.0e-3,
    n_axis_gauges: int = 31,
    axis_r_max_factor: float = 1.5,
) -> dict[str, Any]:
    """Grade a solved Thacker run against the closed form; return the V&V scalars.

    Reads the center gauge (id 1) + the +x-axis gauge line (ids 100..100+N-1 at
    radii ``0 .. axis_r_max_factor*a`` in ``n_axis_gauges`` steps, matching the
    deck) from ``out_dir/_output``. Computes:

      - ``period_s_numerical`` (autocorrelation of the detrended center eta) vs
        ``period_s_analytic`` (``2*pi*a/sqrt(8 g h0)``);
      - ``eta_center_max/min/amplitude`` numerical vs analytic;
      - ``r_shore_min/max`` (the shoreline's closest / furthest wet radius over the
        run) numerical vs analytic;
      - ``mass_drift_pct`` -- ring-integrated water volume peak-to-peak drift over
        the run (a closed-wall conservation proxy; ~0 for perfect conservation);
      - ``rms_eta_m`` -- RMS of (numerical - analytic) center elevation;
      - ``series`` -- ``{t, eta_numerical, eta_analytic}`` for the overlay chart.

    Pure numpy; raises ``PostprocessGeoClawError('GEOCLAW_OUTPUT_EMPTY')`` when the
    center gauge is missing / empty (an un-narratable run)."""
    import numpy as np

    from trid3nt_contracts.geoclaw_thacker import thacker_eta, thacker_reference

    out = Path(out_dir)
    base = out / "_output"
    if not base.is_dir():
        base = out
    ref = thacker_reference(a_m, h0_m, amp_A)

    center = base / "gauge00001.txt"
    if not center.exists():
        raise PostprocessGeoClawError(
            "GEOCLAW_OUTPUT_EMPTY",
            message=f"thacker center gauge not found under {base}",
            details={"out_dir": str(base)},
        )
    ts, _hc, es = _parse_geoclaw_gauge_file(center)
    if len(ts) < 8:
        raise PostprocessGeoClawError(
            "GEOCLAW_OUTPUT_EMPTY",
            message=f"thacker center gauge has too few samples ({len(ts)})",
        )
    t = np.asarray(ts, dtype="float64")
    eta_num = np.asarray(es, dtype="float64")
    eta_ana = np.asarray(
        [thacker_eta(0.0, 0.0, float(tt), a_m, h0_m, amp_A) for tt in ts],
        dtype="float64",
    )

    # Period via autocorrelation of the uniformly-resampled, detrended signal:
    # the first autocorrelation maximum AFTER the correlation first goes negative
    # is the fundamental period (robust to the wetting/dry-front wiggles a naive
    # peak-picker trips on).
    tu = np.linspace(t[0], t[-1], 4000)
    eu = np.interp(tu, t, eta_num)
    eu = eu - eu.mean()
    ac = np.correlate(eu, eu, mode="full")[len(eu) - 1:]
    dtu = float(tu[1] - tu[0])
    below = np.nonzero(ac < 0)[0]
    period_num = float("nan")
    if below.size:
        seg = ac[below[0]:]
        if seg.size:
            period_num = float((below[0] + int(np.argmax(seg))) * dtu)

    rms_eta = float(np.sqrt(np.mean((eta_num - eta_ana) ** 2)))

    # Shoreline: per sampled time, the largest axis-gauge radius that is wet
    # (h > dry_tol); its max / min over the run are r_shore_max / r_shore_min.
    radii = [axis_r_max_factor * a_m * (k / (n_axis_gauges - 1)) for k in range(n_axis_gauges)]
    axis: dict[float, tuple[np.ndarray, np.ndarray]] = {}
    for k, r in enumerate(radii):
        gp = base / f"gauge{100 + k:05d}.txt"
        if not gp.exists():
            continue
        gts, ghs, _ge = _parse_geoclaw_gauge_file(gp)
        if gts:
            axis[r] = (np.asarray(gts, "float64"), np.asarray(ghs, "float64"))

    r_shore_max_num = 0.0
    r_shore_min_num = float(axis_r_max_factor * a_m)
    if axis:
        sample_ts = np.linspace(max(t[0], 0.05), t[-1], 200)
        wet_r = []
        for tt in sample_ts:
            best_r = 0.0
            for r, (gt, gh) in axis.items():
                idx = int(np.argmin(np.abs(gt - tt)))
                if float(gh[idx]) > dry_tol_m and r > best_r:
                    best_r = r
            wet_r.append(best_r)
        r_shore_max_num = float(max(wet_r))
        r_shore_min_num = float(min(wet_r))

    # Mass conservation: total water volume per fort.q frame, integrated over the
    # LEVEL-1 (base) patch that uniformly covers the whole domain -- sum(max(h,0))
    # * dx * dy, threshold-free (no dry cutoff) and overlap-free (a single level, so
    # no AMR double counting). A closed-wall frictionless basin conserves mass, so
    # the peak-to-peak drift is the honest conservation gate. (A finest-wins
    # rasterization with a wet threshold would falsely "lose" the thin sheet the
    # bowl spreads at mid-period; the base-level integral avoids that.) NaN when the
    # fort.q frames are absent.
    mass_drift_pct = float("nan")
    total_volume_m3_first = float("nan")
    try:
        frames = _discover_frames(out)
        vols: list[float] = []
        for _no, q_path, _t in frames:
            patches = parse_fort_q_frame(q_path.read_text(errors="replace"))
            base = [p for p in patches if p.level == min(p2.level for p2 in patches)] if patches else []
            v = 0.0
            for p in base:
                harr = np.asarray(p.h, dtype="float64")
                v += float(np.nansum(np.clip(harr, 0.0, None))) * abs(p.dx * p.dy)
            vols.append(v)
        if vols:
            total_volume_m3_first = vols[0]
            vmean = float(np.mean(vols))
            mass_drift_pct = (
                (max(vols) - min(vols)) / vmean * 100.0 if vmean > 0 else float("nan")
            )
    except Exception as exc:  # noqa: BLE001 -- mass proxy is best-effort
        logger.warning("compute_thacker_vandv: fort.q mass integral failed: %s", exc)

    def _err(num: float, ana: float) -> float:
        return abs(num - ana) / abs(ana) * 100.0 if ana else float("nan")

    return {
        "bowl_a_m": float(a_m),
        "bowl_h0_m": float(h0_m),
        "bowl_eta_amp": float(amp_A),
        "gravity": ref["gravity"],
        "period_s_numerical": period_num,
        "period_s_analytic": ref["period_s"],
        "period_error_pct": _err(period_num, ref["period_s"]),
        "eta_center_max_numerical_m": float(np.nanmax(eta_num)),
        "eta_center_max_analytic_m": ref["eta_center_max_m"],
        "eta_center_min_numerical_m": float(np.nanmin(eta_num)),
        "eta_center_min_analytic_m": ref["eta_center_min_m"],
        "eta_center_amplitude_numerical_m": float(np.nanmax(eta_num) - np.nanmin(eta_num)),
        "eta_center_amplitude_analytic_m": ref["eta_center_amplitude_m"],
        "eta_amplitude_error_pct": _err(
            float(np.nanmax(eta_num) - np.nanmin(eta_num)), ref["eta_center_amplitude_m"]
        ),
        "r_shore_max_numerical_m": r_shore_max_num,
        "r_shore_max_analytic_m": ref["r_shore_max_m"],
        "r_shore_max_error_pct": _err(r_shore_max_num, ref["r_shore_max_m"]),
        "r_shore_min_numerical_m": r_shore_min_num,
        "r_shore_min_analytic_m": ref["r_shore_min_m"],
        "r_shore_min_error_pct": _err(r_shore_min_num, ref["r_shore_min_m"]),
        "mass_drift_pct": mass_drift_pct,
        "total_volume_m3": total_volume_m3_first,
        "rms_eta_m": rms_eta,
        "series": {
            "t": [float(x) for x in ts],
            "eta_numerical": [float(x) for x in eta_num],
            "eta_analytic": [float(x) for x in eta_ana],
        },
    }


def build_thacker_validation_chart_spec(
    vandv: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Vega-Lite overlay: numerical vs analytic center elevation eta(0,t).

    ONE figure layering the GeoClaw center-gauge series over the Thacker closed
    form -- the visual heart of the V&V. Returns ``None`` when the series is empty."""
    if not vandv:
        return None
    series = vandv.get("series") or {}
    ts = series.get("t") or []
    if not ts:
        return None
    en_all = series.get("eta_numerical", [])
    ea_all = series.get("eta_analytic", [])
    # Downsample so BOTH lines fit well under the chart-payload inline-row cap
    # (~2000 rows) WITHOUT truncating the time axis: stride to <= 500 samples per
    # line (1000 rows total), preserving the full [0, tfinal] range.
    stride = max(1, len(ts) // 500)
    values = []
    for i in range(0, len(ts), stride):
        tt = float(ts[i])
        if i < len(en_all):
            values.append({"t_s": tt, "eta_m": float(en_all[i]), "solution": "GeoClaw (numerical)"})
        if i < len(ea_all):
            values.append({"t_s": tt, "eta_m": float(ea_all[i]), "solution": "Thacker 1981 (analytic)"})
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "t_s", "type": "quantitative", "title": "time (s)"},
            "y": {
                "field": "eta_m",
                "type": "quantitative",
                "title": "center surface elevation eta(0,t) [m]",
            },
            "color": {
                "field": "solution",
                "type": "nominal",
                "title": None,
                "scale": {
                    "domain": ["GeoClaw (numerical)", "Thacker 1981 (analytic)"],
                    "range": ["#1f5fbf", "#d1495b"],
                },
            },
            "strokeDash": {
                "field": "solution",
                "type": "nominal",
                "scale": {
                    "domain": ["GeoClaw (numerical)", "Thacker 1981 (analytic)"],
                    "range": [[1, 0], [6, 3]],
                },
                "legend": None,
            },
            "tooltip": [
                {"field": "solution", "type": "nominal"},
                {"field": "t_s", "type": "quantitative", "format": ".2f"},
                {"field": "eta_m", "type": "quantitative", "format": ".4f"},
            ],
        },
    }
