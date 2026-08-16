"""LIVE real-data proof for a mid-run WIND-DIRECTION SHIFT on the committed
Sacramento nr Red Bluff CA showcase reach (ADR 0239 amendment 2 bbox/ignition),
spotting ON. NATE asks (1) whether the fire starts from a point / grows into
its shape in <30 min, and (2)/(3) to "toss in a wind direction change" and see
it turn the fire head.

SCENARIO (LOUDLY labeled synthetic): wind FROM 270 deg (west) for the first
3 h, shifting to FROM 200 deg (SSW) for the last 3 h, same speed (35 mph) --
a real ELMFIRE transient (multi-band, time-interpolated) weather schedule on
the SAME real LANDFIRE fuels + 3DEP DEM warp as the committed river-barrier
pair, spotting ON. A CONSTANT-270 companion run (same everything else) is
solved alongside to quantify the heading shift (burned-centroid azimuth from
ignition), mirroring the synthetic elmfire_transient_wind_schedule_spread
template's constant-vs-transient contrast.

Usage (from the repo root):
  set -a; source .env.local; set +a
  PYTHONPATH=src:contracts/src:. venvs/agent/bin/python scripts/proof_elmfire_windshift.py

Emits to docs/proof/templates/:
  elmfire_windshift_growth_montage.png   (5-min steps h0-h1, 30-min h1-h6, wind arrow/frame)
  elmfire_windshift_growth.gif           (same cadence, animated)
  elmfire_windshift_toa_transient.png    (final ToA, transient run, ESRI basemap)
  elmfire_windshift_toa_constant.png     (final ToA, constant-270 companion)
  elmfire_windshift_heading_chart.png    (constant vs transient burned-centroid heading)
  elmfire_windshift_proof_result.json
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import math
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("proof_windshift")
for noisy in ("urllib3.connection", "botocore", "boto3", "rasterio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

ROOT = Path("/home/nate/Documents/trid3nt-local")
PROOF = ROOT / "docs" / "proof" / "templates"
PROOF.mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location("db", ROOT / "services/workers/elmfire/deck_builder.py")
db = importlib.util.module_from_spec(_spec)
sys.modules["db"] = db
_spec.loader.exec_module(db)

import numpy as np
import rasterio
import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as patheffects
import matplotlib.pyplot as plt
from PIL import Image
from pyproj import Transformer
from rasterio.transform import Affine
from rasterio.warp import calculate_default_transform, reproject, Resampling

from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs
from trid3nt_server.agent.workflows.elmfire.postprocess_elmfire import (
    discover_elmfire_rasters, read_fire_raster,
)
from trid3nt_server.agent.workflows.elmfire.fire_spread.fire_spread import _cleanup_dir
from trid3nt_server.agent.workflows.elmfire.run_elmfire import fetch_elmfire_inputs
from trid3nt_server.agent.workflows.elmfire.spotting.spotting import (
    _RealCase, _solve_real_case, _spotting_namelist, _read_fbfm_grid,
)
from trid3nt_server.agent.workflows.elmfire.sensitivity._sensitivity_common import (
    publish_primary_from_out_dir,
)

_r = importlib.util.spec_from_file_location("rfp", ROOT / "scripts/render_fidelity_proof_generic.py")
rfp = importlib.util.module_from_spec(_r)
sys.modules["rfp"] = rfp
_r.loader.exec_module(rfp)

TO4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

# ---- the COMMITTED showcase reach (ADR 0239 amendment 2 / f6b1b59) -------- #
# docs/proof/templates/elmfire_river_barrier_proof_result.json: chosen_reach
# "sacramento_redbluff_CA" -- SAME bbox + ignition, no re-search.
BBOX = [-122.19893597835888, 40.0977537437604, -122.11237150586113, 40.152662229617306]
IGNITION = (-122.15767135716419, 40.12484241809142)
CELL = 30.0
WIND_SPEED_MPH = 35.0
DURATION_HOURS = 6.0
WIND_DIR_INITIAL = 270.0   # FROM the west
WIND_DIR_SHIFTED = 200.0   # FROM the SSW
SHIFT_FRACTION = 0.5       # shift completes at 3h of 6h, held after
MEAN_SPOTTING_DISTANCE_M = 60.0
NEMBERS = 30
PIGN_PCT = 100.0


def _wd_at(t_s: float, duration_s: float, dt_meteorology_s: float) -> float:
    """The wind direction ELMFIRE's own linear band interpolation would report
    at time ``t_s`` -- band0 at t=0, band1 reached at ``dt_meteorology_s``, held
    after (ITHI clamps to the last band)."""
    frac = min(1.0, max(0.0, t_s / dt_meteorology_s))
    return WIND_DIR_INITIAL + (WIND_DIR_SHIFTED - WIND_DIR_INITIAL) * frac


def _toward_dxdy(wind_from_deg: float) -> tuple[float, float]:
    """Unit (dx, dy) in map (east, north) the wind BLOWS TOWARD, given the
    meteorological FROM-direction (deg, clockwise from north)."""
    toward = math.radians((wind_from_deg + 180.0) % 360.0)
    return math.sin(toward), math.cos(toward)


async def _solve(run_args: ElmfireRunArgs, inputs, *, weather_schedule, dt_meteorology_s, label: str):
    spotting_extra = _spotting_namelist(
        mean_spotting_distance_m=MEAN_SPOTTING_DISTANCE_M, nembers=NEMBERS,
        pign_pct=PIGN_PCT, critical_spotting_intensity_kwm=0.0,
    )
    out_dir, run_id, deck_dir, epsg, out_is_temp = await _solve_real_case(
        run_args, inputs, spotting_extra=spotting_extra,
        weather_schedule=weather_schedule, dt_meteorology_s=dt_meteorology_s,
        compute_class="standard", emitter=None, step_label=f"build_{label}",
    )
    toa_path = discover_elmfire_rasters(out_dir).get("time_of_arrival")
    toa, transform, _crs, cs = read_fire_raster(toa_path, epsg=epsg)
    case = _RealCase(out_dir=out_dir, run_id=run_id, epsg=epsg)
    pub = publish_primary_from_out_dir(
        case, bbox=tuple(run_args.bbox), duration_s=float(run_args.duration_hours) * 3600.0,
        ignition_lonlat=tuple(run_args.ignition_lonlat),
    )
    fbfm = _read_fbfm_grid(deck_dir, epsg)
    manifest = json.loads((Path(deck_dir) / "deck_manifest.json").read_text())
    ixy = (manifest.get("ignitions_domain_xy") or [{}])[0]
    inv = ~transform
    ic, ir = inv * (float(ixy["x"]), float(ixy["y"]))
    ign_rowcol = (int(round(ir)), int(round(ic)))
    return {
        "out_dir": out_dir, "run_id": run_id, "deck_dir": deck_dir, "epsg": epsg,
        "out_is_temp": out_is_temp, "toa": toa, "transform": transform, "cs": cs,
        "fbfm": fbfm, "ign_rowcol": ign_rowcol, "uri": pub.uri,
        "burned_km2": pub.burned_area_km2,
    }


def _burned_heading_deg(toa: np.ndarray, transform: Affine, ign_rowcol: tuple[int, int]) -> float:
    """Azimuth (deg, clockwise from north) of the burned-cell centroid FROM the
    ignition, in real map coordinates -- the quantitative spread-direction proxy."""
    rows, cols = np.where(toa >= 0)
    if rows.size == 0:
        return float("nan")
    ir, ic = ign_rowcol
    dcol = float(cols.mean()) - ic
    drow = float(rows.mean()) - ir
    dx = dcol * abs(transform.a)     # east +
    dy = -drow * abs(transform.e)    # north + (row increases south)
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _reproject_toa_to_3857(toa: np.ndarray, transform: Affine, epsg: int):
    with rasterio.io.MemoryFile() as mf:
        with mf.open(driver="GTiff", height=toa.shape[0], width=toa.shape[1], count=1,
                     dtype="float32", crs=f"EPSG:{epsg}", transform=transform, nodata=np.nan) as ds:
            arr = toa.astype("float32").copy()
            arr[arr < 0] = np.nan
            ds.write(arr, 1)
        with mf.open() as src:
            dtr, w, h = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
            data = np.full((h, w), np.nan, "float32")
            reproject(rasterio.band(src, 1), data, src_transform=src.transform, src_crs=src.crs,
                      dst_transform=dtr, dst_crs="EPSG:3857", resampling=Resampling.nearest,
                      src_nodata=np.nan, dst_nodata=np.nan)
    x0 = dtr.c; y1 = dtr.f; x1 = x0 + dtr.a * w; y0 = y1 + dtr.e * h
    return data, (x0, x1, y0, y1)


def _reproject_water_to_3857(fbfm: np.ndarray, transform: Affine, epsg: int):
    water = (fbfm == 98).astype("float32")
    water[water == 0] = np.nan
    with rasterio.io.MemoryFile() as mf:
        with mf.open(driver="GTiff", height=fbfm.shape[0], width=fbfm.shape[1], count=1,
                     dtype="float32", crs=f"EPSG:{epsg}", transform=transform, nodata=np.nan) as ds:
            ds.write(water, 1)
        with mf.open() as src:
            dtr, w, h = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
            data = np.full((h, w), np.nan, "float32")
            reproject(rasterio.band(src, 1), data, src_transform=src.transform, src_crs=src.crs,
                      dst_transform=dtr, dst_crs="EPSG:3857", resampling=Resampling.nearest,
                      src_nodata=np.nan, dst_nodata=np.nan)
    x0 = dtr.c; y1 = dtr.f; x1 = x0 + dtr.a * w; y0 = y1 + dtr.e * h
    return data, (x0, x1, y0, y1)


def _draw_panel(ax, bm, bm_ext, water3857, water_ext, toa3857, toa_ext, ign_xy_3857,
                 t_cutoff_s, duration_s, dt_meteorology_s, xlim, ylim, label):
    ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
    ax.imshow(water3857, extent=water_ext, origin="upper", cmap="Blues", vmin=0, vmax=1.4,
              zorder=2, alpha=0.85)
    burned = np.where((toa3857 >= 0) & (toa3857 <= t_cutoff_s), 1.0, np.nan)
    ax.imshow(burned, extent=toa_ext, origin="upper", cmap="Reds", vmin=0, vmax=1.3,
              zorder=4, alpha=0.85)
    ix, iy = ign_xy_3857
    ax.plot([ix], [iy], marker="*", color="black", markersize=10, markeredgecolor="white",
            markeredgewidth=0.7, zorder=6)
    wd_now = _wd_at(t_cutoff_s, duration_s, dt_meteorology_s)
    dx, dy = _toward_dxdy(wd_now)
    arrow_len = (xlim[1] - xlim[0]) * 0.14
    ax.annotate("", xy=(ix + dx * arrow_len, iy + dy * arrow_len), xytext=(ix, iy),
                arrowprops=dict(arrowstyle="->", color="yellow", lw=2.2), zorder=7)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
    shifting = " (SHIFTING)" if 0.0 < t_cutoff_s < dt_meteorology_s else ""
    ax.text(0.02, 0.96, f"{label}\nwind FROM {wd_now:.0f}deg{shifting}",
            transform=ax.transAxes, fontsize=8.5, va="top", ha="left", color="white", weight="bold",
            path_effects=[patheffects.withStroke(linewidth=2.2, foreground="black")])


async def main() -> int:
    run_args_base = dict(
        bbox=BBOX, wind_speed_mph=WIND_SPEED_MPH, fuel_moisture="dry",
        duration_hours=DURATION_HOURS, cellsize_m=CELL,
    )
    inputs = await asyncio.to_thread(fetch_elmfire_inputs, tuple(BBOX))
    duration_s = DURATION_HOURS * 3600.0
    dt_meteorology_s = duration_s * SHIFT_FRACTION

    scratch = []
    try:
        constant = await _solve(
            ElmfireRunArgs(ignition_lonlat=list(IGNITION), wind_dir_deg=WIND_DIR_INITIAL, **run_args_base),
            inputs, weather_schedule=None, dt_meteorology_s=3600.0, label="constant",
        )
        scratch.append(constant["deck_dir"])
        if constant["out_is_temp"]:
            scratch.append(constant["out_dir"])

        transient = await _solve(
            ElmfireRunArgs(ignition_lonlat=list(IGNITION), wind_dir_deg=WIND_DIR_INITIAL, **run_args_base),
            inputs,
            weather_schedule=[{"wd": WIND_DIR_INITIAL}, {"wd": WIND_DIR_SHIFTED}],
            dt_meteorology_s=dt_meteorology_s, label="transient",
        )
        scratch.append(transient["deck_dir"])
        if transient["out_is_temp"]:
            scratch.append(transient["out_dir"])

        heading_const = _burned_heading_deg(constant["toa"], constant["transform"], constant["ign_rowcol"])
        heading_trans = _burned_heading_deg(transient["toa"], transient["transform"], transient["ign_rowcol"])
        heading_shift = abs(((heading_trans - heading_const) + 180.0) % 360.0 - 180.0)

        # ---- reproject once (transient run, shared across montage/GIF panels) ----
        toa3857, toa_ext = _reproject_toa_to_3857(transient["toa"], transient["transform"], transient["epsg"])
        water3857, water_ext = _reproject_water_to_3857(transient["fbfm"], transient["transform"], transient["epsg"])
        lw, ls_ = TO4326.transform(toa_ext[0], toa_ext[2]); le, ln = TO4326.transform(toa_ext[1], toa_ext[3])
        padx = (le - lw) * 0.12; pady = (ln - ls_) * 0.12
        bm, bm_ext = rfp.basemap(lw - padx, ls_ - pady, le + padx, ln + pady, 14)
        ix, iy = rfp.TO3857.transform(*IGNITION)
        xlim = (toa_ext[0] - (toa_ext[1] - toa_ext[0]) * 0.05, toa_ext[1] + (toa_ext[1] - toa_ext[0]) * 0.05)
        ylim = (toa_ext[2] - (toa_ext[3] - toa_ext[2]) * 0.05, toa_ext[3] + (toa_ext[3] - toa_ext[2]) * 0.05)

        # ---- fine-cadence steps: NATE ask #1 -- 5-min for the first hour (proves
        # single-cell point ignition + the wind-ellipse stretching are visible),
        # then 30-min to the end. ----
        fine = [i * 300.0 for i in range(1, 13)]            # 5..60 min
        coarse = [3600.0 + i * 1800.0 for i in range(1, 11)]  # 90..360 min
        steps_s = fine + coarse
        assert len(steps_s) == 22

        # ---- 1) montage ----
        nrows_m, ncols_m = 5, 5
        fig, axes = plt.subplots(nrows_m, ncols_m, figsize=(4.0 * ncols_m, 4.0 * nrows_m), dpi=105)
        axes_flat = axes.flatten()
        for i, t_s in enumerate(steps_s):
            t_h = t_s / 3600.0
            _draw_panel(axes_flat[i], bm, bm_ext, water3857, water_ext, toa3857, toa_ext,
                        (ix, iy), t_s, duration_s, dt_meteorology_s, xlim, ylim, f"T+{t_h:.2f}h")
        for j in range(len(steps_s), nrows_m * ncols_m):
            axes_flat[j].axis("off")
        fig.suptitle(
            "ELMFIRE wind-shift proof -- Sacramento nr Red Bluff CA, spotting ON "
            f"(run {transient['run_id']}) -- SCENARIO WIND: FROM {WIND_DIR_INITIAL:.0f}deg "
            f"(west) 0-3h, shifting to FROM {WIND_DIR_SHIFTED:.0f}deg (SSW) 3-6h, "
            f"{WIND_SPEED_MPH:.0f} mph constant", fontsize=11)
        fig.text(0.5, 0.005,
                  "5-min steps T+0.08h..1.0h (single-cell ignition + early wind-ellipse "
                  "stretching), then 30-min steps to T+6.0h. Yellow arrow = current wind "
                  "direction (rotates as the schedule shifts). Blue = river. Red = burned-so-far. "
                  "SYNTHETIC scenario wind on REAL LANDFIRE fuels + 3DEP DEM.",
                  ha="center", va="bottom", fontsize=7.5, wrap=True)
        fig.tight_layout(rect=(0, 0.015, 1, 0.95))
        out_montage = PROOF / "elmfire_windshift_growth_montage.png"
        fig.savefig(out_montage, dpi=140)
        plt.close(fig)
        log.info("wrote %s", out_montage)

        # ---- 2) GIF (same cadence) ----
        frames = []
        for t_s in steps_s:
            t_h = t_s / 3600.0
            fig, ax = plt.subplots(figsize=(6.5, 6.8), dpi=100)
            _draw_panel(ax, bm, bm_ext, water3857, water_ext, toa3857, toa_ext, (ix, iy),
                        t_s, duration_s, dt_meteorology_s, xlim, ylim, f"T+{t_h:.2f}h / 6.0h")
            rfp.add_scale_bar(ax, ax.get_xlim())
            fig.suptitle("ELMFIRE windshift -- Sacramento reach (spotting ON)", fontsize=9)
            fig.tight_layout(rect=(0, 0, 1, 0.95))
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=100)
            plt.close(fig)
            buf.seek(0)
            frames.append(Image.open(buf).convert("RGB"))
        out_gif = PROOF / "elmfire_windshift_growth.gif"
        # hold the last frame longer so the final turned shape reads clearly
        durations = [400] * (len(frames) - 1) + [1800]
        frames[0].save(out_gif, save_all=True, append_images=frames[1:], duration=durations, loop=0)
        log.info("wrote %s", out_gif)

        # ---- 3) final ToA renders (both runs, ESRI basemap, rfp helper) ----
        rfp.render(transient["uri"], str(PROOF / "elmfire_windshift_toa_transient.png"),
                   f"Fire arrival -- WIND SHIFT 270->200deg @ 3h ({transient['run_id'][:10]}..)",
                   f"SCENARIO wind: FROM 270deg (west) 0-3h -> FROM 200deg (SSW) 3-6h, "
                   f"{WIND_SPEED_MPH:.0f} mph, spotting ON. Heading shift vs constant-wind "
                   f"companion: {heading_shift:.0f} deg.",
                   cmap="YlOrRd", units_label="fire arrival (hr)")
        rfp.render(constant["uri"], str(PROOF / "elmfire_windshift_toa_constant.png"),
                   f"Fire arrival -- CONSTANT wind 270deg companion ({constant['run_id'][:10]}..)",
                   f"Same deck/ignition/spotting, wind held FROM 270deg the whole 6h "
                   f"(the control for the heading-shift measurement).",
                   cmap="YlOrRd", units_label="fire arrival (hr)")

        # ---- 4) heading chart ----
        fig, ax = plt.subplots(figsize=(6, 5), dpi=140)
        bars = ax.bar(["constant 270deg", "wind shift 270->200deg"],
                      [heading_const, heading_trans], color=["#4c78a8", "#d1495b"], width=0.6)
        for b, v in zip(bars, [heading_const, heading_trans]):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.0f}deg", ha="center", va="bottom", fontsize=10)
        ax.set_ylabel("burned-centroid heading from ignition (deg, cw from north)")
        ax.set_title(f"Does the wind shift turn the fire head? shift={heading_shift:.0f}deg", fontsize=11)
        ax.spines[["top", "right"]].set_visible(False)
        fig.text(0.5, 0.01,
                  "Heading = azimuth of the burned-cell centroid from the ignition point. "
                  "A constant west wind holds an east-ish heading throughout; the wind-shift "
                  "run's heading rotates toward the post-shift (SSW->NNE) direction.",
                  ha="center", va="bottom", fontsize=7, wrap=True)
        fig.tight_layout(rect=(0, 0.08, 1, 1))
        out_chart = PROOF / "elmfire_windshift_heading_chart.png"
        fig.savefig(out_chart, dpi=150)
        plt.close(fig)
        log.info("wrote %s", out_chart)

        result = {
            "bbox": BBOX, "ignition_lonlat": list(IGNITION),
            "wind_speed_mph": WIND_SPEED_MPH, "duration_hours": DURATION_HOURS,
            "wind_dir_initial_deg": WIND_DIR_INITIAL, "wind_dir_shifted_deg": WIND_DIR_SHIFTED,
            "shift_fraction": SHIFT_FRACTION, "dt_meteorology_s": dt_meteorology_s,
            "mean_spotting_distance_m": MEAN_SPOTTING_DISTANCE_M, "nembers": NEMBERS, "pign_pct": PIGN_PCT,
            "constant_run_id": constant["run_id"], "transient_run_id": transient["run_id"],
            "constant_burned_km2": constant["burned_km2"], "transient_burned_km2": transient["burned_km2"],
            "constant_heading_deg": heading_const, "transient_heading_deg": heading_trans,
            "heading_shift_deg": heading_shift,
            "constant_uri": constant["uri"], "transient_uri": transient["uri"],
        }
        (PROOF / "elmfire_windshift_proof_result.json").write_text(json.dumps(result, indent=2, default=str))
        log.info("PROOF RESULT: %s", json.dumps(result, indent=2, default=str))
        print("\n==== WINDSHIFT PROOF ====")
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        for d in scratch:
            _cleanup_dir(d)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
