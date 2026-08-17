"""LIVE real-data proof for the ELMFIRE river-barrier ember-spotting demo (
real mode). Picks a REAL river reach in grass/shrub fire country, runs the spotting
OFF-vs-ON pair over REAL LANDFIRE fuels + a real 3DEP DEM, measures the river width +
far-side burned area off the ToA grid, and renders the honest pair over ESRI imagery.

Usage (from the repo root):
  set -a; source .env.local; set +a
  PYTHONPATH=.:contracts:. venvs/agent/bin/python scripts/proof_elmfire_river_barrier.py

Emits to docs/proof/templates/:
  elmfire_spot_fire_barrier_crossing_river_context.png   (river + ignition on the real land)
  elmfire_spot_fire_barrier_crossing_toa_spotting_off.png
  elmfire_spot_fire_barrier_crossing_toa_spotting_on.png
  elmfire_spot_fire_barrier_crossing_off_vs_on_chart.png
  elmfire_river_barrier_proof_result.json
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("proof_river_barrier")
for noisy in ("urllib3.connection", "botocore", "boto3", "rasterio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

ROOT = Path("/home/nate/Documents/trid3nt-local")
PROOF = ROOT / "docs" / "proof" / "templates"
PROOF.mkdir(parents=True, exist_ok=True)

# deck builder (by path, as run_elmfire does)
_spec = importlib.util.spec_from_file_location("db", ROOT / "workers/elmfire/deck_builder.py")
db = importlib.util.module_from_spec(_spec)
sys.modules["db"] = db
_spec.loader.exec_module(db)

import numpy as np
import rasterio

from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs
from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
    discover_elmfire_rasters, read_fire_raster,
)
from trid3nt_server.workflows.elmfire.fire_spread.fire_spread import (
    _cleanup_dir, _publish_primary_layer,
)
from trid3nt_server.workflows.elmfire.run_elmfire import fetch_elmfire_inputs
from trid3nt_server.workflows.elmfire.spotting.spotting import (
    _RealCase, _solve_real_case, _spotting_namelist,
    check_river_separates_domain, measure_river_split, river_barrier_captions,
)
from trid3nt_server.workflows.elmfire.sensitivity._sensitivity_common import (
    publish_primary_from_out_dir,
)

# ESRI-basemap renderer (reused)
_r = importlib.util.spec_from_file_location(
    "rfp", ROOT / "scripts/render_fidelity_proof_generic.py")
rfp = importlib.util.module_from_spec(_r)
sys.modules["rfp"] = rfp
_r.loader.exec_module(rfp)

CELL = 30.0
WIND_DIR = 270.0  # from the west -> head fire runs east into the river

# WIDE search regions (real, grass/shrub fire country, a river running roughly
# N-S over the region at large). Iteration-2 finding: even a hand-picked ~10 km
# "straight-looking" box (the iteration-1 candidates) wraps the land into ONE
# component - a real river meanders enough that a single fixed small bbox rarely
# lands cleanly on a fully-separating sub-reach. So each region here is WIDE in
# the N-S (cross-wind / row) direction; a sliding narrower window is searched for
# an actual straight two-component sub-reach (see _find_straight_subreach) rather
# than guessing one fixed box.
SEARCH_REGIONS: dict[str, list[float]] = {
    "deschutes_maupin_OR":    [-121.20, 45.05, -120.98, 45.35],
    "deschutes_lower_OR":     [-120.98, 45.15, -120.75, 45.60],
    "john_day_cottonwood_OR": [-120.62, 45.25, -120.34, 45.65],
    "sacramento_redbluff_CA": [-122.30, 39.80, -122.06, 40.25],
    "snake_twin_falls_ID":    [-114.50, 42.35, -114.18, 42.70],
}

#: Sliding-window search: the cross-wind (N-S, row) extent tried per window, the
#: step between windows, the HALF-WIDTH (E-W, col) window centered on the LOCAL
#: river position within that row band, and the floors a window must clear to be
#: a viable candidate reach (mirrors the live composer's realism floors).
#:
#: A full-region-width window (the first cut of this search) let a WIDE region's
#: far east/west "wings" of land reconnect around the window's top/bottom edge -
#: the very gooseneck-equivalent failure this refinement targets, just moved from
#: "the river bends" to "the box is wider than the river cares about". Centering
#: a MODEST-width window on the local river column (found from the water cells in
#: that row band) keeps the window's aspect close to square, which is what an
#: actual live user's bbox would look like too.
_WINDOW_ROWS: int = 220           # ~6.6 km of river frontage per window try
_WINDOW_STEP_ROWS: int = 30       # ~0.9 km slide between tries
_WINDOW_HALF_COLS: int = 200      # ~6 km each side of the local river column
_MIN_WIDTH_M: float = 60.0
_MIN_BURNABLE_FRAC: float = 0.25
_MIN_CROSS_WIND_COVERAGE: float = 0.5


def _warp_fbfm(bbox: list[float]) -> tuple[np.ndarray, dict]:
    """Fetch + warp LANDFIRE fbfm40 for bbox onto the 5070 30 m deck grid."""
    fn = TOOL_REGISTRY["fetch_landfire_fuels"].fn
    lyr = fn(bbox=bbox, layer="fbfm40")
    uri = getattr(lyr, "uri", None) or lyr.get("uri")
    src = uri[len("file://"):] if str(uri).startswith("file://") else uri
    grid = db.compute_target_grid([float(v) for v in bbox], target_epsg=5070, cellsize_m=CELL)
    dest = Path(tempfile.mkdtemp()) / "fbfm40.tif"
    db.warp_to_grid("fbfm40", db._localize_input("fbfm40", src, Path(tempfile.mkdtemp())), grid, dest)
    with rasterio.open(dest) as ds:
        arr = ds.read(1)
    return np.asarray(arr), grid


def _subwindow_bbox(
    region_bbox: list[float], ny: int, nx: int,
    row0: int, rows: int, col0: int, cols: int,
) -> list[float]:
    """EPSG:4326 bbox of grid rows ``[row0, row0+rows)`` / cols ``[col0, col0+cols)``
    of a region - LINEAR fraction-of-region interpolation on the ORIGINAL request
    bbox (row 0 is the north edge, col 0 is the west edge), NOT a re-derivation
    off the region's projected (EPSG:5070) grid dimensions.

    ``compute_target_grid`` axis-aligns its bounding box in EPSG:5070 meters; for
    a bbox with any real extent far from the Albers central meridian, that box
    measurably grows past the true request in EITHER axis (meridian/parallel
    convergence - verified up to 1.6x wider for a 0.45 deg / 50 km tall region,
    and independently up to ~3x taller for a wide-but-short one, at this
    longitude). Deriving a window's bbox from those inflated grid dimensions
    propagates the inflation into the window's reported footprint, so a live
    re-fetch at the reported (wrong, oversized) bbox pulls in landscape the
    window never actually covered - exactly the regression this avoids. Both
    axes are therefore a plain fraction of row0/ny and col0/nx against the KNOWN
    request bbox - no reprojection needed."""
    lon_lo, lat_lo, lon_hi, lat_hi = [float(v) for v in region_bbox]
    left_frac = col0 / float(nx)
    right_frac = (col0 + cols) / float(nx)
    top_frac = row0 / float(ny)
    bot_frac = (row0 + rows) / float(ny)
    win_lon_lo = lon_lo + left_frac * (lon_hi - lon_lo)
    win_lon_hi = lon_lo + right_frac * (lon_hi - lon_lo)
    win_lat_hi = lat_hi - top_frac * (lat_hi - lat_lo)
    win_lat_lo = lat_hi - bot_frac * (lat_hi - lat_lo)
    return [win_lon_lo, win_lat_lo, win_lon_hi, win_lat_hi]


def _find_straight_subreach(region_bbox: list[float]) -> list[dict]:
    """Fetch ONE wide region, slide a cross-wind row band down it, and - for each
    band - center a MODEST-width window on the LOCAL river column found in that
    band (not the region's full width). Return every window that clears the
    two-component connectivity gate + the realism floors, as scored dicts
    carrying the window's precise sub-bbox.

    Meander-robust by construction: a fixed guess at a "straight" reach missed on
    every iteration-1 candidate, and a full-region-width window let the region's
    far east/west land "wings" reconnect around the window's top/bottom edge (the
    gooseneck failure mode again, just relocated); centering a modest window on
    the local river position and gating each on ``check_river_separates_domain``
    finds an ACTUAL straight sub-reach (or honestly finds none) instead of
    trusting a hand-picked or over-wide box."""
    arr, grid = _warp_fbfm(region_bbox)
    ny, nx = arr.shape
    water = arr == 98
    hits: list[dict] = []
    for row0 in range(0, max(1, ny - _WINDOW_ROWS + 1), _WINDOW_STEP_ROWS):
        band_water = water[row0 : row0 + _WINDOW_ROWS, :]
        if band_water.shape[0] < _WINDOW_ROWS or not band_water.any():
            continue
        col_center = int(np.median(np.where(band_water)[1]))
        col0 = max(0, col_center - _WINDOW_HALF_COLS)
        col1 = min(nx, col_center + _WINDOW_HALF_COLS)
        sub = arr[row0 : row0 + _WINDOW_ROWS, col0:col1]
        sep = check_river_separates_domain(sub)
        if not sep["two_component"]:
            continue
        s = _score_reach(sub)
        if (s["median_river_width_m"] < _MIN_WIDTH_M
                or s["burnable_frac"] < _MIN_BURNABLE_FRAC
                or s["cross_wind_coverage"] < _MIN_CROSS_WIND_COVERAGE):
            continue
        s["_sub_bbox"] = _subwindow_bbox(region_bbox, ny, nx, row0, _WINDOW_ROWS, col0, col1 - col0)
        s["_row0"] = row0
        hits.append(s)
    return hits


def _score_reach(arr: np.ndarray) -> dict:
    """River metrics: median crossing width, burnable fraction, cross-wind coverage,
    and the MEANDER-ROBUST two-component connectivity gate (does the river fully
    separate the AOI's land into exactly two 8-connected components? - a gooseneck
    that threads the fire between meanders, or a land-bridge gap, fails this)."""
    ny, nx = arr.shape
    water = arr == 98
    burn = np.isin(arr, list(range(100, 210)))
    widths = []
    for r in range(ny):
        row = water[r]
        if row.any():
            best = cur = 0
            for v in row:
                cur = cur + 1 if v else 0
                best = max(best, cur)
            if best >= 2:
                widths.append(best)
    med_w = float(np.median(widths)) * CELL if widths else 0.0
    sep = check_river_separates_domain(arr)
    return {
        "median_river_width_m": med_w,
        "rows_with_river": len(widths),
        "cross_wind_coverage": len(widths) / ny,
        "burnable_frac": float(burn.sum()) / arr.size,
        "water_frac": float(water.sum()) / arr.size,
        "grid": f"{nx}x{ny}",
        "two_component": sep["two_component"],
        "num_land_components": sep["num_components"],
        "significant_land_component_sizes_cells": sep["significant_component_sizes_cells"],
    }


def _auto_ignition(arr: np.ndarray, grid: dict) -> tuple[float, float]:
    """Place the ignition UPWIND (west) of the river median column, at mid-height.
    Returns (lon, lat) in EPSG:4326."""
    from rasterio.transform import Affine
    from rasterio.warp import transform as warp_xy

    water = arr == 98
    ny, nx = arr.shape
    river_cols = np.where(water.any(axis=0), np.arange(nx), 0)
    c_river = int(np.median(np.where(water)[1]))  # median water column
    ign_col = max(3, c_river - 30)                # ~900 m west of the river median
    ign_row = ny // 2
    tr = Affine(*grid["transform"])
    x, y = tr * (ign_col + 0.5, ign_row + 0.5)
    lon, lat = warp_xy(f"EPSG:{grid['epsg']}", "EPSG:4326", [x], [y])
    return float(lon[0]), float(lat[0])


async def _solve_and_publish(run_args, inputs, *, spotting_extra, label):
    out_dir, run_id, deck_dir, epsg, out_is_temp = await _solve_real_case(
        run_args, inputs, spotting_extra=spotting_extra,
        compute_class="standard", emitter=None, step_label=f"build_{label}",
    )
    toa_path = discover_elmfire_rasters(out_dir).get("time_of_arrival")
    toa, transform, _crs, cs = read_fire_raster(toa_path, epsg=epsg)
    case = _RealCase(out_dir=out_dir, run_id=run_id, epsg=epsg)
    pub = publish_primary_from_out_dir(
        case, bbox=tuple(run_args.bbox),
        duration_s=float(run_args.duration_hours) * 3600.0,
        ignition_lonlat=tuple(run_args.ignition_lonlat),
    )
    return {"out_dir": out_dir, "run_id": run_id, "deck_dir": deck_dir, "epsg": epsg,
            "out_is_temp": out_is_temp, "toa": toa, "transform": transform, "cs": cs,
            "uri": pub.uri, "burned_km2": pub.burned_area_km2}


def _render_river_context(arr, grid, ign_lonlat, out_png, title, caption):
    """River (water class) + ignition over ESRI imagery (input-layer proof)."""
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from pyproj import Transformer
    from rasterio.transform import Affine
    from rasterio.warp import reproject, Resampling, calculate_default_transform

    water = (arr == 98).astype("float32")
    water[water == 0] = np.nan
    tr = Affine(*grid["transform"])
    src_crs = f"EPSG:{grid['epsg']}"
    with rasterio.io.MemoryFile() as mf:
        with mf.open(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                     dtype="float32", crs=src_crs, transform=tr, nodata=np.nan) as ds:
            ds.write(water, 1)
        with mf.open() as src:
            dtr, w, h = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
            data = np.full((h, w), np.nan, "float32")
            reproject(rasterio.band(src, 1), data, src_transform=src.transform, src_crs=src.crs,
                      dst_transform=dtr, dst_crs="EPSG:3857", resampling=Resampling.nearest,
                      src_nodata=np.nan, dst_nodata=np.nan)
    x0 = dtr.c; y1 = dtr.f; x1 = x0 + dtr.a * w; y0 = y1 + dtr.e * h
    to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lw, ls = to4326.transform(x0, y0); le, ln = to4326.transform(x1, y1)
    padx = (le - lw) * 0.15; pady = (ln - ls) * 0.15
    bm, bm_ext = rfp.basemap(lw - padx, ls - pady, le + padx, ln + pady, 14)
    fig, ax = plt.subplots(figsize=(8, 7), dpi=130)
    ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
    ax.imshow(np.ma.masked_invalid(data), extent=(x0, x1, y0, y1), origin="upper",
              cmap="cool", vmin=0, vmax=1, alpha=0.9, zorder=3)
    ix, iy = rfp.TO3857.transform(ign_lonlat[0], ign_lonlat[1])
    ax.plot([ix], [iy], marker="*", color="red", markersize=20, markeredgecolor="white",
            markeredgewidth=1.2, zorder=6, label="ignition (upwind)")
    ax.annotate("", xy=(ix + (x1 - x0) * 0.18, iy), xytext=(ix, iy),
                arrowprops=dict(arrowstyle="->", color="yellow", lw=2.5), zorder=6)
    ax.text(ix, iy + (y1 - y0) * 0.03, "wind ->", color="yellow", fontsize=9, zorder=7)
    mx0, my0 = rfp.TO3857.transform(lw, ls); mx1, my1 = rfp.TO3857.transform(le, ln)
    ax.set_xlim(mx0 - (mx1 - mx0) * 0.15, mx1 + (mx1 - mx0) * 0.15)
    ax.set_ylim(my0 - (my1 - my0) * 0.15, my1 + (my1 - my0) * 0.15)
    ax.set_xticks([]); ax.set_yticks([])
    rfp.add_scale_bar(ax, ax.get_xlim())
    ax.legend(loc="upper right", fontsize=8)
    ax.set_title(title, fontsize=10)
    fig.text(0.5, 0.015, caption, ha="center", va="bottom", fontsize=7, wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


def _render_chart(off_km2, on_km2, width_m, verdict, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 5), dpi=140)
    bars = ax.bar(["spotting OFF", "spotting ON"], [off_km2, on_km2],
                  color=["#4c78a8", "#d1495b"], width=0.6)
    for b, v in zip(bars, [off_km2, on_km2]):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.3g}", ha="center", va="bottom", fontsize=10)
    ax.set_ylabel("burned area on the far land component (km2)")
    ax.set_title(f"Does the fire jump the river? (~{width_m:.0f} m wide) [{verdict}]", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    caps = river_barrier_captions(
        off_far_km2=off_km2, on_far_km2=on_km2, river_width_m=width_m, verdict=verdict,
    )
    fig.text(0.5, 0.01, f"{caps['off']} {caps['on']}", ha="center", va="bottom", fontsize=7, wrap=True)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


async def main() -> int:
    # 1) per WIDE region, slide a narrower cross-wind window and keep every window
    # that clears the two-component connectivity gate + realism floors. A region
    # with zero surviving windows is REJECTED and reported with why (iteration-1
    # fixed candidates all failed this gate - see the ADR amendment).
    region_hits: dict[str, list[dict]] = {}
    rejected: dict[str, str] = {}
    for name, region_bbox in SEARCH_REGIONS.items():
        try:
            hits = _find_straight_subreach(region_bbox)
        except Exception as e:
            log.warning("region %s fetch/search failed: %s", name, e)
            rejected[name] = f"fetch/search failed: {e}"
            continue
        log.info("region %s: %d passing window(s) found", name, len(hits))
        if not hits:
            rejected[name] = (
                "no sliding window in this region cleared the two-component gate "
                "+ realism floors (every window's land either fails to separate, "
                "or is too narrow/sparse/short a river crossing)"
            )
            continue
        region_hits[name] = hits

    for name, reason in rejected.items():
        log.warning("REJECTED region %s: %s", name, reason)
    for name, hits in region_hits.items():
        best = max(hits, key=lambda s: (s["median_river_width_m"], s["cross_wind_coverage"]))
        log.info("region %s best window: bbox=%s width_m=%.0f coverage=%.2f",
                  name, best["_sub_bbox"], best["median_river_width_m"], best["cross_wind_coverage"])

    if not region_hits:
        log.error(
            "no region produced a passing straight sub-reach - rejected: %s", rejected,
        )
        return 2

    # Rank EVERY passing window across every region, best first, and take the
    # first one that SURVIVES an independent re-fetch/re-warp of its precise
    # bbox. The sliding-window's own check is exact (it labels the wide region's
    # OWN pixels), but an independent live re-fetch at that footprint can still
    # regress at a resampling-fragile boundary - so this is a ranked fallback,
    # not a single hard gate, honestly logging every attempt that fails live.
    ranked: list[tuple[str, dict]] = [
        (name, s) for name, hits in region_hits.items() for s in hits
    ]
    ranked.sort(key=lambda ns: (ns[1]["median_river_width_m"], ns[1]["cross_wind_coverage"]), reverse=True)

    pick = arr = grid = chosen = ign = bbox = None
    live_rewarp_rejects: dict[str, str] = {}
    for name, window in ranked:
        cand_bbox = window["_sub_bbox"]
        try:
            cand_arr, cand_grid = _warp_fbfm(cand_bbox)
        except Exception as e:
            live_rewarp_rejects[f"{name}@{cand_bbox}"] = f"re-fetch failed: {e}"
            continue
        sep = check_river_separates_domain(cand_arr)
        if not sep["two_component"]:
            live_rewarp_rejects[f"{name}@{cand_bbox}"] = (
                f"re-warp regressed on the two-component gate: {sep}"
            )
            continue
        pick, arr, grid, bbox = name, cand_arr, cand_grid, cand_bbox
        chosen = _score_reach(arr)
        chosen["_arr"] = arr; chosen["_grid"] = grid; chosen["_bbox"] = bbox
        ign = _auto_ignition(arr, grid)
        break

    for key, reason in live_rewarp_rejects.items():
        log.warning("REJECTED window %s: %s", key, reason)
    if pick is None:
        log.error(
            "every window's independent re-fetch regressed on the two-component "
            "gate - rejected: %s", live_rewarp_rejects,
        )
        return 2
    log.info("CHOSEN region=%s bbox=%s ignition=%s metrics=%s (tried %d window(s) total, "
             "%d rejected on re-warp)",
             pick, bbox, ign, {k: v for k, v in chosen.items() if not k.startswith("_")},
             len(ranked), len(live_rewarp_rejects))

    run_args = ElmfireRunArgs(
        bbox=bbox, ignition_lonlat=list(ign), wind_speed_mph=35.0, wind_dir_deg=WIND_DIR,
        fuel_moisture="dry", duration_hours=6.0, cellsize_m=CELL,
    )
    inputs = await asyncio.to_thread(fetch_elmfire_inputs, tuple(bbox))
    spotting_extra = _spotting_namelist(
        mean_spotting_distance_m=60.0, nembers=30, pign_pct=100.0,
        critical_spotting_intensity_kwm=0.0,
    )

    scratch = []
    try:
        off = await _solve_and_publish(run_args, inputs, spotting_extra=None, label="off")
        scratch.append(off["deck_dir"])
        if off["out_is_temp"]:
            scratch.append(off["out_dir"])
        on = await _solve_and_publish(run_args, inputs, spotting_extra=spotting_extra, label="on")
        scratch.append(on["deck_dir"])
        if on["out_is_temp"]:
            scratch.append(on["out_dir"])

        # measure the river split off the ON deck's warped fbfm grid
        with rasterio.open(Path(on["deck_dir"]) / "inputs" / "fbfm40.tif") as ds:
            fbfm = ds.read(1)
        manifest = json.loads((Path(on["deck_dir"]) / "deck_manifest.json").read_text())
        ixy = (manifest.get("ignitions_domain_xy") or [{}])[0]
        inv = ~on["transform"]
        ic, ir = inv * (float(ixy["x"]), float(ixy["y"]))
        ign_rc = (int(round(ir)), int(round(ic)))
        off_split = measure_river_split(off["toa"], fbfm, ign_rowcol=ign_rc,
                                        wind_dir_deg=WIND_DIR, cellsize_m=on["cs"])
        on_split = measure_river_split(on["toa"], fbfm, ign_rowcol=ign_rc,
                                       wind_dir_deg=WIND_DIR, cellsize_m=on["cs"])
        off_far = off_split["far_area_km2"]; on_far = on_split["far_area_km2"]
        width = on_split["river_width_m"]
        # Same exhaustive, mutually-exclusive three-way verdict as the composer
        # (spotting.py::model_elmfire_river_barrier_crossing) - "leaked" replaces
        # the old ambiguous "inconclusive" bucket now that the connectivity split
        # makes an OFF-run far-side burn structurally diagnostic of a bad reach.
        off_leaks = off_far > 1e-3
        jumped = bool(on_far > 1e-3 and not off_leaks)
        verdict = "leaked" if off_leaks else ("jumped" if jumped else "held")
        caps = river_barrier_captions(
            off_far_km2=off_far, on_far_km2=on_far, river_width_m=width, verdict=verdict,
        )

        # 4) renders
        base = "elmfire_spot_fire_barrier_crossing"
        _render_river_context(
            arr, grid, ign, str(PROOF / f"{base}_river_context.png"),
            f"Real river barrier + ignition -- {pick} (LANDFIRE water class, non-burnable)",
            f"River ~{width:.0f} m wide crosses the E-W wind axis, fully separating the AOI "
            "into two land components (the connectivity gate); ignition (star) is UPWIND in "
            "grass/shrub. Real LANDFIRE fuels; head fire runs downwind (yellow) into the river.")
        rfp.render(off["uri"], str(PROOF / f"{base}_toa_spotting_off.png"),
                   f"Fire arrival -- spotting OFF ({pick}) [{verdict}]",
                   f"{caps['off']} Real LANDFIRE fuels + 3DEP DEM, 35 mph W wind, 6 h.",
                   cmap="YlOrRd", units_label="fire arrival (hr)")
        rfp.render(on["uri"], str(PROOF / f"{base}_toa_spotting_on.png"),
                   f"Fire arrival -- spotting ON ({pick}) [{verdict}]",
                   f"{caps['on']} Same warp as OFF.",
                   cmap="YlOrRd", units_label="fire arrival (hr)")
        _render_chart(off_far, on_far, width, verdict, str(PROOF / f"{base}_off_vs_on_chart.png"))

        result = {
            "chosen_reach": pick, "bbox": bbox, "ignition_lonlat": list(ign),
            "river_width_m": width, "river_width_min_m": on_split["river_width_min_m"],
            "river_band_rows": on_split["river_band_rows"],
            "river_band_coverage": on_split["river_band_coverage"],
            "num_land_components": on_split["num_land_components"],
            "reach_metrics": {k: v for k, v in chosen.items() if not k.startswith("_")},
            "rejected_regions": rejected,
            "rejected_windows_on_rewarp": live_rewarp_rejects,
            "windows_tried_total": len(ranked),
            "wind_mph": 35.0, "wind_dir_deg": WIND_DIR, "duration_hours": 6.0,
            "mean_spotting_distance_m": 60.0, "nembers": 30, "pign_pct": 100.0,
            "far_side_area_spotting_off_km2": off_far,
            "far_side_area_spotting_on_km2": on_far,
            "head_fire_area_km2": on_split["head_area_km2"],
            "off_side_leaks": bool(off_leaks),
            "verdict": verdict,
            "off_run_id": off["run_id"], "on_run_id": on["run_id"],
            "off_uri": off["uri"], "on_uri": on["uri"],
        }
        (PROOF / "elmfire_river_barrier_proof_result.json").write_text(json.dumps(result, indent=2, default=str))
        log.info("PROOF RESULT: %s", json.dumps(result, indent=2, default=str))
        print("\n==== RIVER BARRIER PROOF ====")
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        for d in scratch:
            _cleanup_dir(d)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
