#!/usr/bin/env python3
"""Culvert-through-embankment on the HEC-RAS 2025 managed engine (ADR 0251, Stage 2).

The productionized culvert leg. The seam is PROVEN (ADR 0251 A/B/C: the Culvert is
the ONE 2D structure the 2025 beta wires into the compute -- ``InitializeDriver_Culverts``
copies every barrel/opening field into the solve; a barrel conveys flow a raised
embankment otherwise blocks). This module drives that on a REAL reach:

    DEM (3DEP lidar; the road embankment IS in the surface)                 [host]
      -> reproject to a LOCAL SI grid, oriented so the reach flows DOWN the
         y-axis (downstream at the bottom wall = tailwater, upstream at the top
         wall = inflow -- the StructChannel BC layout the seam proved)       [rasterio]
      -> derive the embankment band + channel thalweg + barrel inverts from the
         local terrain (or take caller overrides)                           [numpy]
      -> author TWO decks via ``synthdrv culvertreach``: A (barrel present) and
         B (barrel ABSENT); OVERWRITE the exported synthetic Terrain.tif with the
         real local DEM in BOTH (the real road embankment is the ridge)      [docker]
      -> ``ras prepare`` + ``ras solve --solver CPU`` on each                [docker]
      -> A/B discriminant: upstream ponding (B, the embankment blocks) vs
         conveyance (A, the barrel passes the reach flow) + peak-depth COG   [host]

The embankment is NOT synthesized: a lidar DEM of a road crossing captures the road
deck as a raised band spanning the valley, while the buried culvert is invisible to
lidar -- so the DEM naturally supplies the blocked (B) case, and A adds the barrel the
engine wires into the solve. A ridge is burned ONLY if the caller asks (an
under-resolved crossing), stated honestly by the caller.

ORIENTATION (screening constraint): the structured grid puts inflow on the top wall
and tailwater on the bottom, so the reach must run roughly along the domain y-axis
(the caller frames the AOI on a reach reach, road crossing it broadside). General
reach rotation is a follow-on. The leg flips the local DEM vertically when the DEM
gradient shows downstream at the top, so either N->S or S->N reaches work.

Run in the agent venv with the MinIO env block (never ambient AWS). ASCII only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from rog2025_pipeline import (
    AUTHORING_IMAGE_DEFAULT,
    PROBE_DIR_DEFAULT,
    Rog2025Error,
    Rog2025Prep,
    prepare_local_terrain,
)

_HERE = Path(__file__).resolve().parent


class CulvertReachError(RuntimeError):
    """A stage failed (prep / geometry-derivation / author / prepare / solve / extract)."""


@dataclass
class CulvertGeometry:
    """The barrel + embankment geometry in the LOCAL SI mesh frame (metres, y up)."""
    us_x: float                # upstream barrel endpoint x (on the channel thalweg, m)
    us_y: float                # upstream barrel endpoint y (high y, m)
    ds_x: float                # downstream barrel endpoint x (on the channel thalweg, m)
    ds_y: float                # downstream barrel endpoint y (low y, m)
    us_invert: float           # upstream invert elevation (m, absolute -- >= cell min)
    ds_invert: float           # downstream invert elevation (m, absolute)
    embankment_y0: float       # embankment band low-y edge (m)
    embankment_y1: float       # embankment band high-y edge (m)
    embankment_crest_m: float  # peak cross-stream-minimum elevation in the band (m)
    bed_us_m: float            # channel bed just upstream of the band (m)
    bed_ds_m: float            # channel bed just downstream of the band (m)
    blocks: bool               # the embankment crest rises above both channel beds
                               # (so the surface path is blocked -- the B case ponds)


def _orient_downstream_to_bottom(prep: Rog2025Prep) -> Rog2025Prep:
    """Flip the local DEM vertically if the DEM gradient shows downstream at the TOP.

    The StructChannel BC layout inflows the TOP wall and tailwaters the BOTTOM. The
    reach must therefore run downhill toward y=0. If the top rows are on average LOWER
    than the bottom rows, the reach drains north -> flip the terrain (and re-anchor the
    UTM origin_y to the new south edge) so downstream lands at the bottom wall."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(prep.local_dem) as src:
        arr = src.read(1)
        nd = src.nodata
        tr = src.transform
        prof = src.profile
    valid = arr != nd if nd is not None else np.isfinite(arr)
    h = arr.shape[0]
    band = max(1, h // 6)
    top = arr[:band][valid[:band]]
    bot = arr[-band:][valid[-band:]]
    if top.size == 0 or bot.size == 0:
        return prep
    if float(top.mean()) >= float(bot.mean()):
        return prep  # already draining toward the bottom (or flat) -- keep as-is
    # flip: downstream (low) currently at top -> move it to the bottom
    flipped = arr[::-1, :].copy()
    # the transform's top-left y (tr.f) stays; row 0 is still the north edge, so the
    # origin math (origin_y = UTM south) is unchanged -- only the elevation field flips.
    with rasterio.open(prep.local_dem, "w", **prof) as dst:
        dst.write(flipped, 1)
        try:
            dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")
        except Exception:  # noqa: BLE001
            pass
    return prep


def derive_culvert_geometry(prep: Rog2025Prep, *, min_embank_m: float = 1.0,
                            invert_margin_m: float = 0.15) -> CulvertGeometry:
    """Find the road embankment band, the channel thalweg, and the barrel inverts.

    Works on the LOCAL terrain raster (fine, absolute elevations, y up). The road is a
    raised band crossing the domain, so the CROSS-STREAM MINIMUM elevation (the lowest
    point on each y-row -- the valley floor) shows a LOCAL positive anomaly along the road.
    A raw peak-find conflates that with the natural longitudinal grade of a valley, so the
    profile is DETRENDED first (a wide rolling-median baseline = the ambient valley floor)
    and the embankment = the largest run of rows whose detrended residual exceeds
    ``min_embank_m`` (a real road fill, not the valley slope). The thalweg is the median
    argmin column away from the band; the inverts are the channel bed just up/downstream."""
    import numpy as np
    import rasterio

    with rasterio.open(prep.local_dem) as src:
        arr = src.read(1).astype("float64")
        nd = src.nodata
        tr = src.transform
    valid = (arr != nd) & np.isfinite(arr) if nd is not None else np.isfinite(arr)
    arr = np.where(valid, arr, np.nan)
    ny_t, nx_t = arr.shape
    # The terrain tif is in the LOCAL SI frame already (crs=None; origin at
    # (-PAD*res, terr_h+PAD*res), y up). Row/col centres map to local metres via the
    # tif transform directly -- NOT via origin_x/origin_y (those are the UTM anchors for
    # georeferencing the COG, a different frame). The mesh occupies [0,W]x[0,H] in the
    # bottom-left of this terrain footprint.
    rows = np.arange(ny_t)
    row_y = tr.f + (rows + 0.5) * tr.e                  # tr.e < 0; local y (m), row 0 = top

    def _col_x(col):
        return tr.c + (col + 0.5) * tr.a                # local x (m)
    cross_min = np.nanmin(arr, axis=1)                  # valley-floor elev per row (m)

    # DETREND: a wide rolling-median baseline approximates the natural longitudinal grade;
    # the road fill is a local bump ABOVE it. Window ~ 1/3 the domain (>> a road width).
    fin = np.isfinite(cross_min)
    cm = np.interp(rows, rows[fin], cross_min[fin]) if fin.sum() < ny_t else cross_min
    win = max(5, (ny_t // 3) | 1)
    pad = win // 2
    padded = np.pad(cm, pad, mode="edge")
    baseline = np.array([np.median(padded[i:i + win]) for i in range(ny_t)])
    resid = cm - baseline                               # local rise above the valley grade
    crest_row = int(np.argmax(resid))
    peak_resid = float(resid[crest_row])
    if peak_resid < min_embank_m:
        raise CulvertReachError(
            f"no road embankment found: the largest local rise above the valley grade is "
            f"{peak_resid:.2f} m (< {min_embank_m:.2f} m). Frame the AOI on a road/levee "
            "crossing (a spanning fill), not open channel.")
    band_mask = resid >= 0.5 * peak_resid               # the fill shoulder-to-shoulder
    lo = crest_row
    while lo - 1 >= 0 and band_mask[lo - 1]:
        lo -= 1
    hi = crest_row
    while hi + 1 < ny_t and band_mask[hi + 1]:
        hi += 1
    y_hi = float(row_y[lo]); y_lo = float(row_y[hi])
    band_y0, band_y1 = min(y_lo, y_hi), max(y_lo, y_hi)
    crest = float(cm[crest_row])

    W, H = prep.width_m, prep.height_m
    margin = 1.5 * prep.cell_size
    half = max(1, int(round(0.35 * prep.cell_size / tr.a)))  # inside the containing cell

    def _channel_col(row_center: int) -> int:
        """The channel column at a row (argmin over a +-few-row band, for stability)."""
        r0 = max(0, row_center - 2); r1 = min(ny_t, row_center + 3)
        prof = np.nanmin(arr[r0:r1, :], axis=0)
        return int(np.nanargmin(prof))

    def _cell_min(local_x: float, local_y: float) -> float:
        """Fine-terrain minimum over the mesh-cell footprint around a local point --
        the value ``ras prepare`` reports as the cell minimum elevation (the invert
        floor: ``Barrel_*InvertBelowCell`` hard-errors when invert < this)."""
        col = int(round((local_x - tr.c) / tr.a - 0.5))
        row = int(round((tr.f - local_y) / (-tr.e) - 0.5))
        sub = arr[max(0, row - half):row + half + 1, max(0, col - half):col + half + 1]
        return float(np.nanmin(sub))

    # barrel endpoints: on the channel thalweg AT each row just outside the band (the
    # creek meanders, so a single fixed column lands off-channel at the far end -- each
    # endpoint follows its own row's thalweg), clamped inside the mesh interior so both
    # openings pair to a cell.
    step = max(1, int(round(2.0 * prep.cell_size / tr.a)))
    us_row = max(0, lo - step); ds_row = min(ny_t - 1, hi + step)
    us_col = _channel_col(us_row); ds_col = _channel_col(ds_row)
    us_x = float(np.clip(_col_x(us_col), margin, W - margin))
    ds_x = float(np.clip(_col_x(ds_col), margin, W - margin))
    us_y = float(np.clip(row_y[us_row], margin, H - margin))
    ds_y = float(np.clip(row_y[ds_row], margin, H - margin))
    bed_us = _cell_min(us_x, us_y); bed_ds = _cell_min(ds_x, ds_y)
    if us_y < ds_y:                                     # keep us at the higher y (top/inflow)
        us_x, us_y, ds_x, ds_y = ds_x, ds_y, us_x, us_y
        bed_us, bed_ds = bed_ds, bed_us
    if us_y - ds_y < prep.cell_size:
        raise CulvertReachError(
            f"barrel endpoints collapsed within the mesh (us_y={us_y:.1f} ds_y={ds_y:.1f}, "
            f"H={H:.1f}); the embankment band maps outside the mesh footprint -- frame the AOI "
            "so the crossing sits mid-domain")

    # inverts sit a small margin ABOVE the endpoint CELL minimum (>= the subgrid cell min
    # ras prepare enforces), so the barrel connects the two channel beds through the fill.
    us_invert = bed_us + invert_margin_m
    ds_invert = bed_ds + invert_margin_m
    blocks = bool(crest > bed_us and crest > bed_ds)

    return CulvertGeometry(
        us_x=us_x, us_y=us_y, ds_x=ds_x, ds_y=ds_y,
        us_invert=us_invert, ds_invert=ds_invert,
        embankment_y0=band_y0, embankment_y1=band_y1, embankment_crest_m=float(crest),
        bed_us_m=bed_us, bed_ds_m=bed_ds, blocks=blocks)


def seal_embankment(prep: Rog2025Prep, geom: CulvertGeometry, *, cap_m: float,
                    widen_cells: float = 1.5) -> float:
    """Raise the embankment band in the LOCAL DEM to a solid sealing crest, IN PLACE.

    The pure-real path leaks when the road (~one lane, ~10 m) is narrower than the mesh
    cell: the 20 m road cell's subgrid captures the buried channel pixel under the deck,
    so the cell never blocks and case B never ponds. This raises the real road band
    (widened ``widen_cells`` mesh cells each side so it spans >= one full cell) to
    ``crest + cap_m``, sealing the mesh road cell. Applied to BOTH A and B (the barrel is
    then the ONLY difference -- the ADR 0251 seam-probe design on the REAL road/terrain).
    Returns the sealing crest elevation (m). HONEST: this is a burned 1-cell cap at the
    REAL road centerline, used because the sub-cell fill under-resolves at screening scale."""
    import numpy as np
    import rasterio

    seal_crest = float(geom.embankment_crest_m + cap_m)
    pad_m = widen_cells * prep.cell_size
    y0 = geom.embankment_y0 - pad_m
    y1 = geom.embankment_y1 + pad_m
    with rasterio.open(prep.local_dem, "r+") as d:
        a = d.read(1)
        nd = d.nodata if d.nodata is not None else -9999.0
        valid = a != nd
        tr = d.transform
        rows = np.arange(d.height)
        ys = tr.f + (rows + 0.5) * tr.e            # local y of each row
        band = (ys >= y0) & (ys <= y1)
        mask = np.zeros_like(a, dtype=bool)
        mask[band, :] = True
        mask &= valid & (a < seal_crest)
        a[mask] = seal_crest
        d.write(a, 1)
    return seal_crest


def _spec(prep: Rog2025Prep, geom: CulvertGeometry, out_dir: str, *, with_culvert: bool,
          inflow_cms: float, tailwater_depth_m: float, dt_s: float, sim_hours: float,
          report_every: int, manning_n: float, rise_span_m: float, shape: str,
          opening_type: str, k_in: float, k_out: float) -> dict:
    """Assemble the ``culvertreach`` spec.json (strict; parser v2)."""
    tailwater_stage = geom.bed_ds_m + float(tailwater_depth_m)
    spec: dict = {
        "out_dir": out_dir, "nx": prep.nx, "ny": prep.ny, "cell_size": prep.cell_size,
        "manning_n": manning_n, "dt_s": dt_s, "sim_seconds": sim_hours * 3600.0,
        "report_every": report_every, "ramp_seconds": 300.0,
        "inflow_cms": float(inflow_cms), "tailwater_stage": float(tailwater_stage),
        "parser_version": 2,
    }
    if with_culvert:
        spec["culvert"] = {
            "barrel": [geom.us_x, geom.us_y, geom.ds_x, geom.ds_y],
            "us_invert": geom.us_invert, "ds_invert": geom.ds_invert,
            "rise": float(rise_span_m), "span": float(rise_span_m),
            "shape": shape, "mannings": 0.013,
            "opening_type": opening_type, "k_in": float(k_in), "k_out": float(k_out),
        }
    return spec


def _author_prepare_solve(prep: Rog2025Prep, geom: CulvertGeometry, workdir, case: str, *,
                          with_culvert: bool, inflow_cms: float, tailwater_depth_m: float,
                          dt_s: float, sim_hours: float, report_every: int, manning_n: float,
                          rise_span_m: float, shape: str, opening_type: str, k_in: float,
                          k_out: float, image: str, probe_dir: str, timeout_s: int = 14400):
    """Author the culvert deck, overwrite the terrain with the real DEM, prepare + solve."""
    probe = Path(probe_dir)
    stage = probe / f"cvr_{Path(workdir).name}_{case}"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "local_dem.tif").write_bytes(Path(prep.local_dem).read_bytes())
    out_dir = f"/probe/{stage.name}/proj"
    spec = _spec(prep, geom, out_dir, with_culvert=with_culvert, inflow_cms=inflow_cms,
                 tailwater_depth_m=tailwater_depth_m, dt_s=dt_s, sim_hours=sim_hours,
                 report_every=report_every, manning_n=manning_n, rise_span_m=rise_span_m,
                 shape=shape, opening_type=opening_type, k_in=k_in, k_out=k_out)
    (stage / "spec.json").write_text(json.dumps(spec, indent=2))

    runner = f"""
set -e
cd /opt/hecras2025/app
cp /probe/synthdrv.dll .
cp ras.runtimeconfig.json synthdrv.runtimeconfig.json
rm -rf {out_dir} {out_dir}_r2r
dotnet synthdrv.dll culvertreach /probe/{stage.name}/spec.json
cp /probe/{stage.name}/local_dem.tif "{out_dir}/Terrains/Terrain.tif"
RAS=$(ls {out_dir}/*.ras | head -1)
mkdir -p {out_dir}_r2r
dotnet ras.dll prepare -s "$RAS" -o {out_dir}_r2r -f
R2R=$(ls {out_dir}_r2r/*.r2r.h5 | head -1)
dotnet ras.dll solve "$R2R" /probe/{stage.name}/result.h5 --solver CPU -f
echo SOLVE_OK
"""
    (stage / "run.sh").write_text(runner)
    argv = ["docker", "run", "--rm", "-v", f"{probe}:/probe",
            "--entrypoint", "/bin/bash", image, f"/probe/{stage.name}/run.sh"]
    t0 = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    wall = time.time() - t0
    (stage / "run.log").write_text(proc.stdout + "\n=== STDERR ===\n" + proc.stderr)
    result = stage / "result.h5"
    if "SOLVE_OK" not in proc.stdout or not result.exists():
        raise CulvertReachError(
            f"author/prepare/solve failed for case {case} (exit {proc.returncode}, "
            f"{wall:.0f}s):\n{proc.stdout[-2500:]}\n{proc.stderr[-1200:]}")
    return result, wall


def _upstream_downstream_masks(result_h5, geom: CulvertGeometry):
    """Boolean per-cell masks: cells upstream (y > band) and downstream (y < band)."""
    import h5py
    import numpy as np

    gbase = "/Geometry/2D Flow Areas/Base Mesh"
    with h5py.File(result_h5, "r") as f:
        cell_xy = f[f"{gbase}/Cell Coordinates"][:]     # (Nc, 2) local m
    us = cell_xy[:, 1] > geom.embankment_y1
    ds = cell_xy[:, 1] < geom.embankment_y0
    return us, ds


def extract_discriminant(result_a, result_b, geom: CulvertGeometry) -> dict:
    """A/B discriminant: upstream ponding (B) vs conveyance (A), + max|A-B| depth delta.

    Mirrors the ADR 0251 seam-probe metric on the real reach. B (no barrel) traps the
    reach inflow upstream of the embankment (storage rises); A (barrel) conveys it under
    the road (upstream storage quasi-steady). The final-step upstream mean depth orders
    A < B, and max|A-B| over the per-cell depth field proves the barrel is hydraulically
    LIVE (the must-move-water gate)."""
    import h5py
    import numpy as np

    base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
    us, ds = _upstream_downstream_masks(result_a, geom)

    def _final_depth(h5):
        with h5py.File(h5, "r") as f:
            d = f[f"{base}/Cell Depth"][:]              # (Nt, Nc)
            cv = f[f"{base}/DEBUG/CellVolume"][:]       # (Nt, Nc)
            t_days = f["/Results/Output Blocks/Base Output/Time"][:]
        return d, cv, (t_days - t_days[0]) * 86400.0

    da, cva, ts = _final_depth(result_a)
    db, cvb, _ = _final_depth(result_b)
    us_mean_a = float(da[-1, us].mean()); us_mean_b = float(db[-1, us].mean())
    us_max_a = float(da[-1, us].max()); us_max_b = float(db[-1, us].max())
    # upstream storage rate over the last third (quasi-steady window)
    k = max(2, len(ts) // 3)
    dvdt_a = float((cva[-1, us].sum() - cva[-k, us].sum()) / (ts[-1] - ts[-k]))
    dvdt_b = float((cvb[-1, us].sum() - cvb[-k, us].sum()) / (ts[-1] - ts[-k]))
    max_abs_delta = float(np.abs(da[-1] - db[-1]).max())

    return {
        "us_mean_depth_a_m": round(us_mean_a, 4),        # barrel present
        "us_mean_depth_b_m": round(us_mean_b, 4),        # barrel absent (ponds)
        "us_max_depth_a_m": round(us_max_a, 4),          # peak upstream depth (channel pool)
        "us_max_depth_b_m": round(us_max_b, 4),
        "us_storage_rate_a_m3s": round(dvdt_a, 4),       # lower -> conveyed
        "us_storage_rate_b_m3s": round(dvdt_b, 4),       # higher -> trapped
        "max_abs_depth_delta_m": round(max_abs_delta, 4),  # >0 -> barrel LIVE
        "ponding_relieved_max_m": round(us_max_b - us_max_a, 4),
        "storage_relieved_m3s": round(dvdt_b - dvdt_a, 4),  # flow the barrel conveys
        "moves_water": bool(max_abs_delta > 0.05 and us_max_b > us_max_a + 0.05
                            and dvdt_b > dvdt_a),
        "n_upstream_cells": int(us.sum()), "n_downstream_cells": int(ds.sum()),
        "sim_hours": round(float(ts[-1] / 3600.0), 3),
    }


def run_culvert_reach(dem_tif, workdir, *, cell_size=25.0, elev_units="m", bbox4326=None,
                      inflow_cms=6.0, tailwater_depth_m=0.5, sim_hours=2.5, dt_s=None,
                      manning_n=0.045, rise_span_m=2.0, shape="Circle",
                      opening_type="ConcretePipeCulvert_SquareEdgeWithHeadwall",
                      k_in=0.5, k_out=1.0, geometry_override=None, min_embank_m=1.0,
                      seal_embankment_m=None,
                      image=AUTHORING_IMAGE_DEFAULT, probe_dir=PROBE_DIR_DEFAULT) -> dict:
    """Full culvert-reach A/B pipeline; returns prep + geometry + discriminant + provenance.

    ``geometry_override``: a ``CulvertGeometry`` (or dict) to bypass DEM auto-derivation
    (the composer passes barrel/invert overrides through the input-review gate)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    prep = prepare_local_terrain(
        dem_tif, workdir, cell_size=cell_size, elev_units=elev_units, bbox4326=bbox4326)
    prep = _orient_downstream_to_bottom(prep)

    if geometry_override is not None:
        geom = (geometry_override if isinstance(geometry_override, CulvertGeometry)
                else CulvertGeometry(**geometry_override))
    else:
        geom = derive_culvert_geometry(prep, min_embank_m=min_embank_m)
    if not geom.blocks:
        raise CulvertReachError(
            f"the embankment crest {geom.embankment_crest_m:.2f} m does not rise above both "
            f"channel beds ({geom.bed_us_m:.2f}/{geom.bed_ds_m:.2f} m) -- no blocked case to "
            "relieve; frame the AOI tighter on the road crossing or burn a ridge")

    seal_crest = None
    if seal_embankment_m is not None:
        seal_crest = seal_embankment(prep, geom, cap_m=float(seal_embankment_m))

    if dt_s is None:
        dt_s = max(1.0, min(6.0, cell_size / 8.0))
    report_every = max(1, int(round(300.0 / dt_s)))

    common = dict(inflow_cms=inflow_cms, tailwater_depth_m=tailwater_depth_m, dt_s=dt_s,
                  sim_hours=sim_hours, report_every=report_every, manning_n=manning_n,
                  rise_span_m=rise_span_m, shape=shape, opening_type=opening_type,
                  k_in=k_in, k_out=k_out, image=image, probe_dir=probe_dir)
    result_a, wall_a = _author_prepare_solve(
        prep, geom, workdir, "A_culvert", with_culvert=True, **common)
    result_b, wall_b = _author_prepare_solve(
        prep, geom, workdir, "B_blocked", with_culvert=False, **common)

    disc = extract_discriminant(result_a, result_b, geom)
    return {
        "result_a_h5": str(result_a), "result_b_h5": str(result_b),
        "wall_s": round(wall_a + wall_b, 1), "prep": asdict(prep), "geometry": asdict(geom),
        "discriminant": disc, "dt_s": dt_s, "inflow_cms": inflow_cms,
        "seal_crest_m": seal_crest,
        "embankment_basis": ("burned 1-cell crest cap at the real road centerline "
                             f"(seal to {seal_crest:.2f} m; sub-cell fill under-resolves "
                             "at screening mesh)" if seal_crest is not None
                             else "real 3DEP terrain (road embankment in the lidar)"),
        "engine": "HEC-RAS 2025 managed (CPU, beta)",
        "structure": "culvert barrel (InitializeDriver_Culverts wired; ADR 0251)",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dem_tif")
    ap.add_argument("workdir")
    ap.add_argument("--cell-size", type=float, default=25.0)
    ap.add_argument("--inflow-cms", type=float, default=6.0)
    ap.add_argument("--sim-hours", type=float, default=2.5)
    ap.add_argument("--rise-span-m", type=float, default=2.0)
    ap.add_argument("--elev-units", default="m")
    args = ap.parse_args()
    out = run_culvert_reach(
        args.dem_tif, args.workdir, cell_size=args.cell_size, inflow_cms=args.inflow_cms,
        sim_hours=args.sim_hours, rise_span_m=args.rise_span_m, elev_units=args.elev_units)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
