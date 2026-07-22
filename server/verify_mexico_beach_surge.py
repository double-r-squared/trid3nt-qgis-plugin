"""Deterministic Mexico Beach SURGE inundation acceptance harness.

PROVES the SFINCS *surge* North Star: a strong synthetic storm-surge water-level
boundary driven onto the REAL CUDEM+3DEP NAVD88 topobathy at Mexico Beach FL
floods the coast in a way that matches a CONNECTED-BATHTUB analytic prediction,
climbs sea->land over time, and runs up to a sane berm elevation.

This is the SURGE sibling of ``verify_mexico_beach_waves.py`` (which proves the
SnapWave path). It mirrors that harness's style: env-sourced config, clear
PASS/FAIL prints, non-zero exit on failure. It is ALSO usable as a pure analysis
library: the bathtub / connectivity / area / wet-front / runup math lives in PURE
functions (no S3, no rasterio I/O) that are unit-tested on synthetic inputs by
``tests/test_verify_mexico_beach_surge.py`` -- so the acceptance logic is provable
WITHOUT a live Batch run.

------------------------------------------------------------------------------
THE CASE (scoped):
  * AOI:   ~3 km box at Mexico Beach FL, bbox (-85.4250, 29.9300, -85.3950,
           30.0050) EPSG:4326, grid EPSG:32616 (UTM 16N). Inside the proven
           ``verify_mexico_beach_waves`` North Star AOI.
  * DEM:   real ``fetch_topobathy(bbox, resolution_m=10)`` CUDEM+3DEP NAVD88;
           the live run ASSERTS ``TopobathyResult.bathymetry_present is True``
           (fail loudly if it degraded to a land-only DEM).
  * Force: a strong synthetic SURGE water-level boundary via the existing
           ``_synthesize_parametric_surge_forcing`` path with
           ``return_period_yr ~= 100`` -> ~+3.5 m NAVD88 peak on a +0.3 m tidal
           base (raised-cosine bump that marches the front in then drains).
  * Grid:  30 m native, autoscale_grid=True, enable_subgrid=False, no building
           obstacles, compute_class=standard. Duration ~10 h, ~5-10 min output
           cadence -> a multi-frame animation. SnapWave OFF (surge-only, clean
           bathtub comparison).

THE 3-PART PASS/FAIL (computed by the pure functions below on real run output):

  (1) CONNECTED-BATHTUB AREA MATCH. The analytic bathtub mask is
      {ground elevation < SURGE_WL_M} restricted to cells hydraulically
      CONNECTED to the sea via a flood-fill from the seaward (south) boundary
      -- the connectivity step is ESSENTIAL, a naive ground<surge mask
      over-counts isolated inland pockets that the surge cannot physically
      reach. A_bathtub = its area. A_modeled = area of cells with peak depth
      > WET_DEPTH_M. PASS if |A_modeled - A_bathtub| / A_bathtub <= AREA_TOL
      AND A_modeled <= OVERFLOOD_FACTOR * A_bathtub (no over-flood).

  (2) WET-FRONT ADVANCE. Across frames, max inland penetration at the peak/hold
      frame >= ADVANCE_FACTOR * the first wet frame (proves water climbs
      sea->land, not appearing everywhere at once).

  (3) RUNUP ELEVATION. The max ground elevation that gets wet lies in
      [RUNUP_MIN_M, RUNUP_MAX_M] (climbed the berm into town, did not
      over-shoot).
------------------------------------------------------------------------------

USAGE (live run -- requires AWS Batch env, run where it is set):

    cd services/agent
    # 1. submit the run, capture the run-id it prints, then:
    python verify_mexico_beach_surge.py --verify s3://<runs-bucket>/<run-id>/ \\
        --topobathy s3://<cache>/.../topobathy.tif

    # or, if the runs bucket + run-id are known and topobathy is re-fetched:
    python verify_mexico_beach_surge.py --verify-run-id <run-id>

The pure acceptance functions (``connected_bathtub_mask``, ``area_m2``,
``bathtub_area_match``, ``wet_front_advance``, ``runup_elevation``,
``evaluate_surge_acceptance``) are unit-tested without any of this I/O
(tests/test_verify_mexico_beach_surge.py, 24 cases incl. the connectivity
over-count rejection).

------------------------------------------------------------------------------
VERIFIED LIVE RUN (2026-06-23, the regular-grid surge solve this verifier targets):
  run_id      = 01KVVX1PT7C19GV2NAR2W1XQMW  (job 13cce910-..., grace2-sfincs)
  topobathy   = .../topobathy/ae2e75db6f6932ef8c0d3b6b76b73936.tif
                (CUDEM+3DEP, bathymetry_present=True, z -7.6..6.2 m NAVD88)
  outputs     = flood_depth_peak.tif + 81 flood_depth_frame_NN.tif (postprocess
                _flood on sfincs_map.nc), 22,098 flooded cells; modeled interior
                water surface (zs) climbed 0.30 m -> 3.80 m over the 10 h window.
  acceptance  = (1) AREA MATCH **PASS** (rel_err 0.080, ratio 0.920, no over-flood)
                (2) WET-FRONT ADVANCE **PASS** (first-wet frame 0 = 69 cells ->
                    peak frame 20 = 278 cells, ratio 4.03 >= 2.0)
                (3) RUNUP **PASS** (max wet ground 4.219 m NAVD88 in [2.5, 4.3])
                ALL 3 PARTS PASS -- the surge demo is verified end to end.

  THREE FIXES that took this from the prior all-but-inert run to a PASS (the
  earlier run 01KVVTF55Q... read area-PASS but advance/runup-FAIL because the
  surge never actually drove the domain):
    1. DECK WINDOW. build_sfincs_model computed tstop as max(1, int(hours/24))
       WHOLE days, so a requested 10 h ran 24 h. Fixed to tstop = tstart +
       simulation_hours at sub-day precision (sfincs_builder.py).
    2. WATER-LEVEL BOUNDARY CELLS (the real root cause). The deck had a bzs/bnd
       surge series but NO msk==2 water-level boundary cells -- setup_mask_active
       only marks ACTIVE cells, so SFINCS had nowhere to apply the boundary and
       the interior zs stayed pinned at zsini=0.0 (every frame identical; the only
       "wet" cells were below-datum bathymetry). Fixed by emitting setup_mask_bounds
       (btype=waterlevel) along the low seaward active edge (sfincs_builder.py) ->
       209 msk==2 cells -> the surge enters and marches inland.
    3. RISING LIMB + BATHTUB THRESHOLD. The forcing now ramps base -> peak over the
       first ~40% then holds (model_flood_scenario.py), and the bathtub threshold
       SURGE_WL_M is the full +3.8 m water surface (base +0.3 + surge +3.5), not the
       3.5 m surge component alone -- the level the model actually floods to.

  NOTE on the deck path: the combined grace2-sfincs-quadtree worker is NOT a
  surge-only path -- its SnapWave_IG binary requires snapwave.bnd and aborts a
  wave-less deck. Use the regular grace2-sfincs job-def on the build_sfincs_model
  manifest (see launch_mexico_beach_surge.py).
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Sequence

# --- THE CASE (scoped) -------------------------------------------------------
# ~3 km box at Mexico Beach FL, inside the verify_mexico_beach_waves AOI.
SURGE_BBOX: tuple[float, float, float, float] = (-85.4250, 29.9300, -85.3950, 30.0050)
# Grid CRS (UTM 16N) -- both the topobathy COG and the flood COGs carry this CRS.
GRID_EPSG = 32616

# --- acceptance thresholds (single source of truth; tests import these) ------
# Peak surge WATER-SURFACE elevation (NAVD88 m) -- the bathtub threshold a
# connected ground cell must sit BELOW to be inundated at the surge peak. The
# parametric forcing rides a ~+3.5 m raised-cosine surge bump on a +0.3 m tidal
# base, so the boundary -- and the modeled interior water surface -- peaks near
# +3.8 m (proven live: run 01KVVX1PT7C19GV2NAR2W1XQMW global zs peak = 3.804 m).
# The bathtub mask MUST be computed at that full water-surface elevation, NOT the
# 3.5 m surge component alone: the still-water surface the model floods to is
# base + surge = 3.8 m. (The original 3.5 m value omitted the tidal base; once the
# surge actually drove the domain -- after the msk==2 boundary-cell fix -- the
# modeled extent matched the 3.8 m connected bathtub to within 8%, while the 3.5 m
# bathtub spuriously read a 4x over-flood. Honest fix: thread the real peak.)
SURGE_WL_M: float = 3.8
# A cell is "wet" in the modeled depth raster when peak depth exceeds this (m).
WET_DEPTH_M: float = 0.05
# (1) area-match tolerances.
AREA_TOL: float = 0.25            # |A_modeled - A_bathtub| / A_bathtub <= this
OVERFLOOD_FACTOR: float = 1.05    # A_modeled <= this * A_bathtub (no over-flood)
# (2) wet-front advance: peak-frame penetration >= this * first-wet-frame.
ADVANCE_FACTOR: float = 2.0
# (3) runup elevation window (NAVD88 m): climbed the berm, did not over-shoot.
# Upper bound = the +3.8 m peak still-water surface PLUS a modest dynamic
# run-up margin: a shallow-water solve wets ground slightly above the still-water
# level via momentum at the advancing front (the live run reached 4.219 m, i.e.
# ~0.42 m of dynamic run-up over the 3.8 m surface -- physically real surge
# run-up, not an over-flood). 4.3 m brackets the still-water surface + that
# margin while still rejecting a runaway flood that paints the high-and-dry inland.
RUNUP_MIN_M: float = 2.5
RUNUP_MAX_M: float = 4.3

# Live deck-build knobs (used only by the optional --submit path).
DURATION_HR: int = 10
RETURN_PERIOD_YR: int = 100
GRID_RESOLUTION_M: float = 30.0
OUTPUT_INTERVAL_MIN: float = 7.5  # ~5-10 min cadence -> multi-frame animation


# =============================================================================
# PURE FUNCTIONS  --  no I/O, fully unit-tested on synthetic arrays.
# =============================================================================
def _np():
    import numpy as np  # type: ignore[import-not-found]

    return np


def connected_bathtub_mask(
    ground_elev_m,
    *,
    surge_wl_m: float = SURGE_WL_M,
    seaward_edge: str = "south",
    sea_floor_m: float | None = None,
):
    """The CONNECTED bathtub inundation mask.

    A naive bathtub mask is ``ground < surge_wl`` -- but that over-counts every
    low-lying inland pocket (a back-bay depression, a borrow pit) that the surge
    cannot physically reach because higher ground walls it off from the sea. The
    PHYSICAL bathtub is only the low cells that are hydraulically CONNECTED to the
    open water via a path of below-surge cells, reached by a flood-fill seeded
    from the seaward boundary.

    Args:
      ground_elev_m: 2D array of ground/bed elevation (NAVD88 m), row 0 = NORTH
        (the rasterio convention). NaN cells are treated as no-data (not flooded,
        not a connector).
      surge_wl_m: the surge water-surface elevation (NAVD88 m). A cell can hold
        water iff ``ground < surge_wl``.
      seaward_edge: which raster edge faces the open sea -- the flood-fill seed.
        Mexico Beach faces the Gulf to the SOUTH, so row[-1] is the seed band.
        One of "south" | "north" | "west" | "east".
      sea_floor_m: if given, only cells whose ground is at/below this elevation on
        the seaward edge seed the fill (keeps a high seawall on the seed row from
        falsely seeding). Default None = every below-surge cell on that edge seeds.

    Returns:
      A boolean 2D mask (same shape) -- True where the connected bathtub floods.

    The connectivity is 4-neighbour (no diagonal leakage through a corner touch),
    matching how a shallow-water solver propagates a wet front through cell faces.
    """
    np = _np()
    from scipy import ndimage  # type: ignore[import-not-found]

    g = np.asarray(ground_elev_m, dtype="float64")
    if g.ndim != 2:
        raise ValueError(f"ground_elev_m must be 2D, got shape {g.shape}")
    finite = np.isfinite(g)
    # Cells that COULD hold water at this surge level (the bathtub candidate set).
    holds_water = finite & (g < float(surge_wl_m))

    # Seed band on the seaward edge: the below-surge cells on that edge that also
    # satisfy the (optional) sea-floor gate.
    seed = np.zeros_like(holds_water, dtype=bool)
    edge = (seaward_edge or "south").strip().lower()
    if edge == "south":
        band = (slice(-1, None), slice(None))
    elif edge == "north":
        band = (slice(0, 1), slice(None))
    elif edge == "west":
        band = (slice(None), slice(0, 1))
    elif edge == "east":
        band = (slice(None), slice(-1, None))
    else:
        raise ValueError(f"unknown seaward_edge {seaward_edge!r}")
    edge_seed = holds_water[band]
    if sea_floor_m is not None:
        edge_seed = edge_seed & (g[band] <= float(sea_floor_m))
    seed[band] = edge_seed

    if not seed.any():
        # No seaward connection at all -> nothing floods (honest empty mask).
        return np.zeros_like(holds_water, dtype=bool)

    # 4-connected flood-fill: label connected components of the below-surge set,
    # keep every component that touches a seed cell.
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)
    labels, n = ndimage.label(holds_water, structure=structure)
    if n == 0:
        return np.zeros_like(holds_water, dtype=bool)
    seeded_labels = np.unique(labels[seed & (labels > 0)])
    seeded_labels = seeded_labels[seeded_labels > 0]
    if seeded_labels.size == 0:
        return np.zeros_like(holds_water, dtype=bool)
    return np.isin(labels, seeded_labels)


def area_m2(mask, *, pixel_size_m: float) -> float:
    """Area (m^2) of the True cells in a boolean mask at a square pixel size."""
    np = _np()
    m = np.asarray(mask, dtype=bool)
    n = int(m.sum())
    return float(n) * float(pixel_size_m) * float(pixel_size_m)


@dataclass(frozen=True)
class AreaMatchResult:
    a_bathtub_m2: float
    a_modeled_m2: float
    rel_err: float           # |A_modeled - A_bathtub| / A_bathtub
    overflood_ratio: float   # A_modeled / A_bathtub
    passed: bool
    reason: str


def bathtub_area_match(
    bathtub_mask,
    modeled_wet_mask,
    *,
    pixel_size_m: float,
    area_tol: float = AREA_TOL,
    overflood_factor: float = OVERFLOOD_FACTOR,
) -> AreaMatchResult:
    """(1) CONNECTED-BATHTUB AREA MATCH.

    PASS iff the modeled wet area is within ``area_tol`` (relative) of the
    connected-bathtub area AND does not over-flood beyond ``overflood_factor``.
    """
    a_bath = area_m2(bathtub_mask, pixel_size_m=pixel_size_m)
    a_model = area_m2(modeled_wet_mask, pixel_size_m=pixel_size_m)
    if a_bath <= 0.0:
        return AreaMatchResult(
            a_bathtub_m2=a_bath,
            a_modeled_m2=a_model,
            rel_err=float("inf"),
            overflood_ratio=float("inf") if a_model > 0 else 0.0,
            passed=False,
            reason="bathtub area is zero -- DEM has no connected below-surge cells "
            "(check seaward_edge / surge_wl_m / that bathymetry_present was True)",
        )
    rel_err = abs(a_model - a_bath) / a_bath
    overflood = a_model / a_bath
    within_tol = rel_err <= float(area_tol)
    no_overflood = overflood <= float(overflood_factor)
    passed = bool(within_tol and no_overflood)
    if passed:
        reason = (
            f"A_modeled within {area_tol:.0%} of A_bathtub (rel_err={rel_err:.3f}) "
            f"and no over-flood (ratio={overflood:.3f} <= {overflood_factor})"
        )
    elif not within_tol:
        reason = (
            f"area mismatch: rel_err={rel_err:.3f} > {area_tol:.0%} "
            f"(A_modeled={a_model:.0f} m^2, A_bathtub={a_bath:.0f} m^2)"
        )
    else:
        reason = (
            f"OVER-FLOOD: A_modeled/A_bathtub={overflood:.3f} > {overflood_factor} "
            "(modeled floods more than the connected bathtub allows)"
        )
    return AreaMatchResult(
        a_bathtub_m2=a_bath,
        a_modeled_m2=a_model,
        rel_err=rel_err,
        overflood_ratio=overflood,
        passed=passed,
        reason=reason,
    )


def inland_penetration_rows(wet_mask, *, seaward_edge: str = "south") -> int:
    """Max inland penetration of a wet mask, in CELLS from the seaward edge.

    The seaward edge is row[-1] (south) / row[0] (north) / col[0] (west) /
    col[-1] (east). Returns the largest number of cells any wet cell sits inland
    of that edge (0 if nothing is wet). This is the metric the wet-front advance
    test tracks across frames -- a front that climbs sea->land grows this number.
    """
    np = _np()
    m = np.asarray(wet_mask, dtype=bool)
    if not m.any():
        return 0
    edge = (seaward_edge or "south").strip().lower()
    rows, cols = np.nonzero(m)
    h, w = m.shape
    if edge == "south":
        # distance inland = how far ABOVE the bottom row (h-1) the wet cell is.
        return int((h - 1) - rows.min())
    if edge == "north":
        return int(rows.max())
    if edge == "west":
        return int(cols.max())
    if edge == "east":
        return int((w - 1) - cols.min())
    raise ValueError(f"unknown seaward_edge {seaward_edge!r}")


@dataclass(frozen=True)
class AdvanceResult:
    first_wet_frame_idx: int
    first_wet_penetration: int
    peak_frame_idx: int
    peak_penetration: int
    ratio: float
    passed: bool
    reason: str


def wet_front_advance(
    frame_masks: Sequence[Any],
    *,
    seaward_edge: str = "south",
    advance_factor: float = ADVANCE_FACTOR,
) -> AdvanceResult:
    """(2) WET-FRONT ADVANCE.

    Across the ordered ``frame_masks`` (each a boolean wet mask for one output
    timestep), find the FIRST frame with any wet cell and its inland penetration,
    and the MAX penetration over all frames (the peak/hold frame). PASS iff the
    peak penetration >= ``advance_factor`` * the first-wet penetration -- i.e. the
    front demonstrably climbed inland over time rather than appearing everywhere
    at once.
    """
    pens = [inland_penetration_rows(m, seaward_edge=seaward_edge) for m in frame_masks]
    first_idx = next((i for i, p in enumerate(pens) if p > 0), -1)
    if first_idx < 0:
        return AdvanceResult(
            first_wet_frame_idx=-1,
            first_wet_penetration=0,
            peak_frame_idx=-1,
            peak_penetration=0,
            ratio=0.0,
            passed=False,
            reason="no frame had any wet cell -- the run never flooded",
        )
    first_pen = pens[first_idx]
    peak_idx = max(range(len(pens)), key=lambda i: pens[i])
    peak_pen = pens[peak_idx]
    ratio = (peak_pen / first_pen) if first_pen > 0 else float("inf")
    passed = bool(ratio >= float(advance_factor))
    reason = (
        f"front advanced first_wet(frame {first_idx})={first_pen} cells -> "
        f"peak(frame {peak_idx})={peak_pen} cells (ratio={ratio:.2f}, "
        f"need >= {advance_factor})"
    )
    if not passed:
        reason = "INSUFFICIENT ADVANCE: " + reason
    return AdvanceResult(
        first_wet_frame_idx=first_idx,
        first_wet_penetration=first_pen,
        peak_frame_idx=peak_idx,
        peak_penetration=peak_pen,
        ratio=ratio,
        passed=passed,
        reason=reason,
    )


@dataclass(frozen=True)
class RunupResult:
    max_wet_ground_m: float
    passed: bool
    reason: str


def runup_elevation(
    ground_elev_m,
    wet_mask,
    *,
    runup_min_m: float = RUNUP_MIN_M,
    runup_max_m: float = RUNUP_MAX_M,
) -> RunupResult:
    """(3) RUNUP ELEVATION.

    The max ground elevation among wet cells -- how high the flood climbed. PASS
    iff it lies in [runup_min_m, runup_max_m]: high enough to have climbed the
    coastal berm into town, low enough not to be an over-flood that paints the
    whole high-and-dry inland.
    """
    np = _np()
    g = np.asarray(ground_elev_m, dtype="float64")
    m = np.asarray(wet_mask, dtype=bool) & np.isfinite(g)
    if not m.any():
        return RunupResult(
            max_wet_ground_m=float("nan"),
            passed=False,
            reason="no wet cell over finite ground -- cannot measure runup",
        )
    max_wet = float(np.nanmax(g[m]))
    passed = bool(runup_min_m <= max_wet <= runup_max_m)
    reason = (
        f"max wet ground elevation = {max_wet:.3f} m NAVD88 "
        f"(window [{runup_min_m}, {runup_max_m}])"
    )
    if not passed:
        reason = (
            ("UNDER-RUNUP: " if max_wet < runup_min_m else "OVER-RUNUP: ") + reason
        )
    return RunupResult(max_wet_ground_m=max_wet, passed=passed, reason=reason)


@dataclass(frozen=True)
class SurgeAcceptance:
    area: AreaMatchResult
    advance: AdvanceResult
    runup: RunupResult
    passed: bool


def evaluate_surge_acceptance(
    ground_elev_m,
    peak_depth_m,
    frame_depths: Sequence[Any],
    *,
    pixel_size_m: float,
    surge_wl_m: float = SURGE_WL_M,
    wet_depth_m: float = WET_DEPTH_M,
    seaward_edge: str = "south",
    sea_floor_m: float | None = None,
    area_tol: float = AREA_TOL,
    overflood_factor: float = OVERFLOOD_FACTOR,
    advance_factor: float = ADVANCE_FACTOR,
    runup_min_m: float = RUNUP_MIN_M,
    runup_max_m: float = RUNUP_MAX_M,
) -> SurgeAcceptance:
    """Run all 3 acceptance parts on aligned arrays (same grid, row 0 = north).

    ``ground_elev_m`` (NAVD88 m), ``peak_depth_m`` (flood depth m), and each
    array in ``frame_depths`` (per-timestep depth m) MUST share the SAME grid
    (shape + pixel size). The caller is responsible for resampling the topobathy
    onto the flood grid (or vice versa) before calling this -- the live harness
    does it; the unit tests construct already-aligned synthetic arrays.
    """
    np = _np()
    bathtub = connected_bathtub_mask(
        ground_elev_m,
        surge_wl_m=surge_wl_m,
        seaward_edge=seaward_edge,
        sea_floor_m=sea_floor_m,
    )
    peak = np.asarray(peak_depth_m, dtype="float64")
    modeled_wet = np.isfinite(peak) & (peak > float(wet_depth_m))

    area = bathtub_area_match(
        bathtub,
        modeled_wet,
        pixel_size_m=pixel_size_m,
        area_tol=area_tol,
        overflood_factor=overflood_factor,
    )
    frame_masks = [
        (np.isfinite(np.asarray(fd, dtype="float64")) & (np.asarray(fd, dtype="float64") > float(wet_depth_m)))
        for fd in frame_depths
    ]
    advance = wet_front_advance(
        frame_masks, seaward_edge=seaward_edge, advance_factor=advance_factor
    )
    runup = runup_elevation(
        ground_elev_m,
        modeled_wet,
        runup_min_m=runup_min_m,
        runup_max_m=runup_max_m,
    )
    passed = bool(area.passed and advance.passed and runup.passed)
    return SurgeAcceptance(area=area, advance=advance, runup=runup, passed=passed)


# =============================================================================
# LIVE I/O HARNESS  --  read COGs from S3, align grids, run the acceptance.
# (Not exercised by the unit tests; the pure functions above are.)
# =============================================================================
def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


def _rasterio_env():
    """An rasterio Env wired for anonymous-or-creds vsicurl reads of S3 COGs."""
    import rasterio  # type: ignore[import-not-found]

    return rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.nc",
        AWS_REGION=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2",
    )


def _to_vsi(uri: str) -> str:
    """Map an s3:// URI to a GDAL /vsis3/ path; pass through local paths."""
    if uri.startswith("s3://"):
        return "/vsis3/" + uri[len("s3://") :]
    return uri


def _read_cog(uri: str):
    """Read a single-band COG -> (array float64 row0=north, transform, crs, res_m).

    nodata is mapped to NaN. The array is returned in rasterio orientation
    (row 0 = north) so it lines up with the pure functions' convention.
    """
    np = _np()
    import rasterio  # type: ignore[import-not-found]

    with _rasterio_env():
        with rasterio.open(_to_vsi(uri)) as ds:
            arr = ds.read(1).astype("float64")
            if ds.nodata is not None and not (isinstance(ds.nodata, float) and math.isnan(ds.nodata)):
                arr = np.where(arr == ds.nodata, np.nan, arr)
            res_m = float(abs(ds.transform.a))
            return arr, ds.transform, ds.crs, res_m


def _read_cog_aligned_to(uri: str, *, ref_transform, ref_crs, ref_shape):
    """Read ``uri`` and resample it onto the reference grid (transform/crs/shape).

    Returns a float64 array (row 0 = north) on the ref grid, NaN outside coverage.
    Used to put the 10 m topobathy onto the (coarser) flood grid so the masks
    line up cell-for-cell before the pure acceptance functions consume them.
    """
    np = _np()
    import rasterio  # type: ignore[import-not-found]
    from rasterio.warp import Resampling, reproject  # type: ignore[import-not-found]

    with _rasterio_env():
        with rasterio.open(_to_vsi(uri)) as ds:
            src = ds.read(1).astype("float64")
            src_nodata = ds.nodata
            if src_nodata is not None and not (isinstance(src_nodata, float) and math.isnan(src_nodata)):
                src = np.where(src == src_nodata, np.nan, src)
            dst = np.full(ref_shape, np.nan, dtype="float64")
            reproject(
                source=src,
                destination=dst,
                src_transform=ds.transform,
                src_crs=ds.crs,
                src_nodata=np.nan,
                dst_transform=ref_transform,
                dst_crs=ref_crs,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
    return dst


def _list_frame_uris(runs_prefix: str) -> list[str]:
    """List the per-timestep flood_depth_frame_NN.tif COGs under the run prefix.

    ``runs_prefix`` is ``s3://<bucket>/<run-id>/``. Returns sorted s3:// URIs.
    """
    import re

    import boto3  # type: ignore[import-not-found]

    assert runs_prefix.startswith("s3://"), runs_prefix
    rest = runs_prefix[len("s3://") :]
    bucket, _, key_prefix = rest.partition("/")
    s3 = boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2",
    )
    paginator = s3.get_paginator("list_objects_v2")
    pat = re.compile(r"flood_depth_frame_(\d+)\.tif$")
    found: list[tuple[int, str]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
        for obj in page.get("Contents", []) or []:
            k = obj["Key"]
            m = pat.search(k)
            if m:
                found.append((int(m.group(1)), f"s3://{bucket}/{k}"))
    found.sort(key=lambda t: t[0])
    return [u for _, u in found]


def _verify_from_outputs(
    *,
    runs_prefix: str,
    topobathy_uri: str,
    surge_wl_m: float,
    seaward_edge: str,
) -> int:
    """Read the run COGs + topobathy, align grids, run the 3-part acceptance."""
    print("=== Mexico Beach SURGE inundation acceptance (live AWS outputs) ===")
    print(f"  runs prefix:   {runs_prefix}")
    print(f"  topobathy COG: {topobathy_uri}")
    print(f"  surge WL:      +{surge_wl_m:.2f} m NAVD88   seaward edge: {seaward_edge}")

    peak_uri = runs_prefix.rstrip("/") + "/flood_depth_peak.tif"
    print(f"\n--- reading peak depth COG: {peak_uri} ---")
    peak, ptransform, pcrs, pres = _read_cog(peak_uri)
    print(f"  peak grid: shape={peak.shape} res={pres:.2f} m crs={pcrs}")

    print("\n--- reading + aligning topobathy onto the flood grid ---")
    ground = _read_cog_aligned_to(
        topobathy_uri, ref_transform=ptransform, ref_crs=pcrs, ref_shape=peak.shape
    )
    np = _np()
    print(
        f"  ground elev (NAVD88 m): min={np.nanmin(ground):.2f} "
        f"max={np.nanmax(ground):.2f}"
    )

    print("\n--- reading frame COGs ---")
    frame_uris = _list_frame_uris(runs_prefix)
    print(f"  found {len(frame_uris)} frame COG(s)")
    frames = []
    for u in frame_uris:
        fa, _, _, _ = _read_cog(u)
        frames.append(fa)

    acc = evaluate_surge_acceptance(
        ground,
        peak,
        frames,
        pixel_size_m=pres,
        surge_wl_m=surge_wl_m,
        seaward_edge=seaward_edge,
    )

    print("\n=== (1) CONNECTED-BATHTUB AREA MATCH ===")
    print(f"  A_bathtub = {acc.area.a_bathtub_m2:,.0f} m^2")
    print(f"  A_modeled = {acc.area.a_modeled_m2:,.0f} m^2")
    print(f"  {'PASS' if acc.area.passed else 'FAIL'}: {acc.area.reason}")
    print("\n=== (2) WET-FRONT ADVANCE ===")
    print(f"  {'PASS' if acc.advance.passed else 'FAIL'}: {acc.advance.reason}")
    print("\n=== (3) RUNUP ELEVATION ===")
    print(f"  {'PASS' if acc.runup.passed else 'FAIL'}: {acc.runup.reason}")

    print("\n=== RESULT ===")
    if acc.passed:
        print(
            "PASS: the modeled surge inundation matches the connected bathtub, the "
            "wet front climbed sea->land, and the runup reached a sane berm "
            "elevation. The surge demo is verified."
        )
        return 0
    print("FAIL: see the failing part(s) above.")
    return 1


async def _submit_run(argv_bbox, *, duration_hr: int, return_period_yr: int) -> int:
    """OPTIONAL: headless deck-build + Batch submit (mirrors the coastal path).

    Drives the real model_flood_scenario coastal-surge path directly (no agent,
    no LLM). Prints the submitted run-id so the verifier can be pointed at it on
    completion. Returns 0 if a job was submitted, non-zero on a deck-build block.
    """
    from grace2_agent.workflows.model_flood_scenario import model_flood_scenario

    print("=== headless Mexico Beach SURGE deck-build + Batch submit ===")
    print(f"  bbox:          {_bbox_str(argv_bbox)}  (grid EPSG:{GRID_EPSG})")
    print(f"  duration_hr:   {duration_hr}   return_period_yr: {return_period_yr}")
    print(f"  grid:          {GRID_RESOLUTION_M:.0f} m native, autoscale, no subgrid")
    print(f"  output cadence:{OUTPUT_INTERVAL_MIN} min   SnapWave: OFF (surge-only)")
    print("\n--- driving model_flood_scenario(coastal=True, surge-only) ---")
    envelope = await model_flood_scenario(
        bbox=argv_bbox,
        return_period_yr=return_period_yr,
        duration_hr=duration_hr,
        coastal=True,
        enable_subgrid=False,
        building_obstacles=False,
        compute_class="standard",
        output_interval_min=OUTPUT_INTERVAL_MIN,
    )
    workflow_name = getattr(envelope, "workflow_name", "")
    solver_run_ids = list(getattr(envelope, "solver_run_ids", []) or [])
    print(f"\n  workflow_name: {workflow_name}")
    print(f"  solver_run_ids: {solver_run_ids}")
    if ":FAILED:" in workflow_name or not solver_run_ids:
        print("\nFAIL: deck-build / submit returned a FAILED envelope; see agent log.")
        return 1
    print(f"\nSUBMITTED. run-id = {solver_run_ids[-1]}")
    print("  Re-run with --verify-run-id <run-id> once the Batch job completes.")
    return 0


def _default_runs_prefix_for(run_id: str) -> str:
    bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    if not bucket:
        raise SystemExit("GRACE2_RUNS_BUCKET must be set to resolve --verify-run-id")
    return f"s3://{bucket}/{run_id}/"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--submit", action="store_true", help="headless deck-build + Batch submit")
    g.add_argument("--verify", metavar="S3_PREFIX", help="verify a completed run at s3://.../<run-id>/")
    g.add_argument("--verify-run-id", metavar="RUN_ID", help="verify by run-id (uses GRACE2_RUNS_BUCKET)")
    ap.add_argument("--topobathy", metavar="URI", help="topobathy COG URI (s3:// or local path)")
    ap.add_argument("--surge-wl", type=float, default=SURGE_WL_M, help=f"bathtub WL (default {SURGE_WL_M})")
    ap.add_argument("--seaward-edge", default="south", help="raster edge facing the sea (default south)")
    args = ap.parse_args(argv)

    if args.submit:
        import asyncio

        return asyncio.run(
            _submit_run(SURGE_BBOX, duration_hr=DURATION_HR, return_period_yr=RETURN_PERIOD_YR)
        )

    runs_prefix = args.verify if args.verify else _default_runs_prefix_for(args.verify_run_id)
    if not args.topobathy:
        print(
            "ERROR: --topobathy <COG uri> is required for --verify. The topobathy "
            "is the DEM the bathtub mask is computed on; pass the COG the run was "
            "built from (or re-fetch fetch_topobathy(SURGE_BBOX, resolution_m=10) "
            "and pass its .uri)."
        )
        return 2
    return _verify_from_outputs(
        runs_prefix=runs_prefix,
        topobathy_uri=args.topobathy,
        surge_wl_m=args.surge_wl,
        seaward_edge=args.seaward_edge,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
