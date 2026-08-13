#!/usr/bin/env python3
"""Standalone fire-render agent script (sandbox, no repo files touched besides
docs/proof/templates outputs). Renders the John Day / Cottonwood Creek reach
(john_day_cottonwood_OR, bbox -120.52,45.40,-120.42,45.48) ELMFIRE spotting
OFF-vs-ON pair NATE asked for:

  1) context layers (FBFM40 categorical, DEM hillshade, canopy cover) over ESRI
     imagery
  2) fire-growth diagnostic: 12-panel 0.5h montage of the spotting-ON ToA raster
     + an animated GIF + a spotting-OFF final-extent comparison panel

Reads LOCAL run staging only (no live tool calls, no network fetches except the
ESRI basemap tiles): data/runs/<run_id>/{inputs,outputs}. Reuses the generic
ESRI-tile helper in scripts/render_fidelity_proof_generic.py (shared utility,
not the owned river-barrier/spotting-composer files).

Usage:
  venvs/agent/bin/python scripts/sandbox/fire_render/render_john_day.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as patheffects
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib.colors import LightSource
from pyproj import Transformer
from rasterio.warp import calculate_default_transform, reproject, Resampling
from PIL import Image

ROOT = Path("/home/nate/Documents/trid3nt-local")
PROOF = ROOT / "docs" / "proof" / "templates"
PROOF.mkdir(parents=True, exist_ok=True)

RUN_ON = "01KZXZ7CDV8XR2CA4EB5H2JMMZ"   # spotting ENABLE_SPOTTING = .TRUE.
RUN_OFF = "01KZXZ62YA0EC0S6FJVHBSK70E"  # spotting absent (OFF)

_r = importlib.util.spec_from_file_location(
    "rfp", ROOT / "scripts/render_fidelity_proof_generic.py")
rfp = importlib.util.module_from_spec(_r)
sys.modules["rfp"] = rfp
_r.loader.exec_module(rfp)

TO4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def run_dir(run_id: str) -> Path:
    return ROOT / "data" / "runs" / run_id


def load_manifest(run_id: str) -> dict:
    return json.loads((run_dir(run_id) / "manifest.json").read_text())


def find_toa(run_id: str) -> Path:
    outs = sorted((run_dir(run_id) / "outputs").glob("time_of_arrival*.bil"))
    assert outs, f"no time_of_arrival .bil in {run_id}"
    return outs[0]


def reproject_band(local_tif: Path, resampling=Resampling.nearest):
    """Single-band local raster -> (data float32 w/ nan nodata, ext3857 tuple)."""
    with rasterio.open(local_tif) as src:
        dst_crs = "EPSG:3857"
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        data = np.full((height, width), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1), destination=data,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=transform, dst_crs=dst_crs,
            resampling=resampling, src_nodata=src.nodata, dst_nodata=np.nan,
        )
        x0 = transform.c; y1 = transform.f
        x1 = x0 + transform.a * width; y0 = y1 + transform.e * height
    return data, (x0, x1, y0, y1)


def reproject_bil_toa(bil_path: Path, epsg: int, grid: dict):
    """ELMFIRE .bil ToA output has no embedded CRS -- stamp the build_spec grid."""
    tr = rasterio.transform.Affine(*grid["transform"])
    with rasterio.open(bil_path) as src:
        arr = src.read(1).astype("float32")
        nodata = src.nodata if src.nodata is not None else -9999.0
    arr[arr == nodata] = np.nan
    with rasterio.io.MemoryFile() as mf:
        with mf.open(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                     dtype="float32", crs=f"EPSG:{epsg}", transform=tr, nodata=np.nan) as ds:
            ds.write(arr, 1)
        with mf.open() as src2:
            dst_crs = "EPSG:3857"
            dtr, w, h = calculate_default_transform(src2.crs, dst_crs, src2.width, src2.height, *src2.bounds)
            data = np.full((h, w), np.nan, "float32")
            reproject(rasterio.band(src2, 1), data, src_transform=src2.transform, src_crs=src2.crs,
                      dst_transform=dtr, dst_crs=dst_crs, resampling=Resampling.nearest,
                      src_nodata=np.nan, dst_nodata=np.nan)
    x0 = dtr.c; y1 = dtr.f; x1 = x0 + dtr.a * w; y0 = y1 + dtr.e * h
    return data, (x0, x1, y0, y1)


def hillshade_rgb_3857(dem_tif: Path):
    with rasterio.open(dem_tif) as src:
        dem = src.read(1).astype("float64")
        nodata = src.nodata
        crs, transform = src.crs, src.transform
    mask = (dem == nodata) if nodata is not None else np.zeros_like(dem, dtype=bool)
    dem_f = np.where(mask, np.nan, dem)
    ls = LightSource(azdeg=315, altdeg=45)
    dem_fill = np.nan_to_num(dem_f, nan=np.nanmin(dem_f))
    rgb = ls.shade(dem_fill, cmap=plt.cm.gist_earth, vert_exag=1.5, dx=30.0, dy=30.0,
                    blend_mode="soft", vmin=np.nanmin(dem_f), vmax=np.nanmax(dem_f))
    rgb_u8 = (np.clip(rgb[..., :3], 0, 1) * 255).astype("uint8")
    with rasterio.io.MemoryFile() as mf:
        with mf.open(driver="GTiff", height=rgb_u8.shape[0], width=rgb_u8.shape[1], count=3,
                     dtype="uint8", crs=crs, transform=transform) as ds:
            for b in range(3):
                ds.write(rgb_u8[..., b], b + 1)
        with mf.open() as src2:
            dst_crs = "EPSG:3857"
            dtr, w, h = calculate_default_transform(src2.crs, dst_crs, src2.width, src2.height, *src2.bounds)
            out = np.zeros((3, h, w), "uint8")
            reproject(rasterio.band(src2, [1, 2, 3]), out, src_transform=src2.transform, src_crs=src2.crs,
                      dst_transform=dtr, dst_crs=dst_crs, resampling=Resampling.bilinear)
    x0 = dtr.c; y1 = dtr.f; x1 = x0 + dtr.a * w; y0 = y1 + dtr.e * h
    return np.moveaxis(out, 0, -1), (x0, x1, y0, y1)


def ext3857_to_lonlat_bounds(ext3857, pad=0.15):
    x0, x1, y0, y1 = ext3857
    lw, ls_ = TO4326.transform(x0, y0)
    le, ln = TO4326.transform(x1, y1)
    padx = (le - lw) * pad; pady = (ln - ls_) * pad
    return (lw - padx, ls_ - pady, le + padx, ln + pady), (lw, ls_, le, ln)


# ---- FBFM40 group classification (LANDFIRE codes) --------------------------
FBFM_GROUPS = [
    ("Water (98)",              lambda a: a == 98,                          "#1f6fd6"),
    ("Non-burnable (urban/ag/bare/snow)",
     lambda a: np.isin(a, [91, 92, 93, 99]),                                "#9e9e9e"),
    ("Grass (GR1-9, 101-109)",  lambda a: (a >= 101) & (a <= 109),          "#c5e1a5"),
    ("Grass-Shrub (GS1-4, 121-124)", lambda a: (a >= 121) & (a <= 124),     "#dce775"),
    ("Shrub (SH1-9, 141-149)",  lambda a: (a >= 141) & (a <= 149),          "#ffb74d"),
    ("Timber (TU/TL/SB, 161-204)", lambda a: (a >= 161) & (a <= 204),       "#2e7d32"),
]
NODATA_COLOR = "#e0e0e0"


def fbfm_rgba(arr: np.ndarray) -> np.ndarray:
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype="float32")
    valid = ~np.isnan(arr)
    covered = np.zeros((h, w), dtype=bool)
    for _, pred, hexcolor in FBFM_GROUPS:
        m = valid & pred(arr)
        c = matplotlib.colors.to_rgba(hexcolor)
        rgba[m] = c
        covered |= m
    leftover = valid & ~covered
    rgba[leftover] = matplotlib.colors.to_rgba(NODATA_COLOR)
    rgba[~valid, 3] = 0.0
    rgba[valid, 3] = 0.92
    return rgba


def main():
    man_on = load_manifest(RUN_ON)
    man_off = load_manifest(RUN_OFF)
    bs_on = man_on["build_spec"]
    grid = bs_on["grid"]
    epsg = grid["epsg"]
    ign_lon, ign_lat = bs_on["ignitions_lonlat"][0]["lon"], bs_on["ignitions_lonlat"][0]["lat"]
    duration_s = bs_on["duration_s"]
    assert bs_on["grid"] == man_off["build_spec"]["grid"], "ON/OFF grids differ -- not the paired run"

    fbfm_path = run_dir(RUN_ON) / "inputs" / "fbfm40.tif"
    dem_path = run_dir(RUN_ON) / "inputs" / "dem.tif"
    cc_path = run_dir(RUN_ON) / "inputs" / "cc.tif"
    assert fbfm_path.exists() and dem_path.exists(), "expected input rasters missing"
    have_cc = cc_path.exists()

    fbfm_data, ext = reproject_band(fbfm_path, Resampling.nearest)
    lonlat_pad_bounds, lonlat_bounds = ext3857_to_lonlat_bounds(ext, pad=0.12)
    print("basemap fetch zoom=14 bbox(4326,padded)=", lonlat_pad_bounds)
    bm, bm_ext = rfp.basemap(*lonlat_pad_bounds, 14)
    ix, iy = rfp.TO3857.transform(ign_lon, ign_lat)
    xlim = (ext[0] - (ext[1] - ext[0]) * 0.05, ext[1] + (ext[1] - ext[0]) * 0.05)
    ylim = (ext[2] - (ext[3] - ext[2]) * 0.05, ext[3] + (ext[3] - ext[2]) * 0.05)

    # ---------------- 1) CONTEXT LAYERS -------------------------------------
    ncols = 3 if have_cc else 2
    fig, axes = plt.subplots(1, ncols, figsize=(6.2 * ncols, 6.6), dpi=130)
    if ncols == 1:
        axes = [axes]

    ax = axes[0]
    ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
    ax.imshow(fbfm_rgba(fbfm_data), extent=ext, origin="upper", zorder=3)
    ax.plot([ix], [iy], marker="*", color="red", markersize=16, markeredgecolor="white",
            markeredgewidth=1.0, zorder=6)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
    rfp.add_scale_bar(ax, ax.get_xlim())
    handles = [plt.Rectangle((0, 0), 1, 1, fc=c, ec="k", lw=0.3) for _, _, c in FBFM_GROUPS]
    handles.append(plt.Rectangle((0, 0), 1, 1, fc=NODATA_COLOR, ec="k", lw=0.3))
    labels = [n for n, _, _ in FBFM_GROUPS] + ["other/no-data"]
    ax.legend(handles, labels, loc="upper left", fontsize=6.5, framealpha=0.9)
    ax.set_title("LANDFIRE FBFM40 fuel model (30 m)", fontsize=10)

    ax = axes[1]
    hs_rgb, hs_ext = hillshade_rgb_3857(dem_path)
    with rasterio.open(dem_path) as ds:
        dem_res_m = ds.res[0]
        dem_min, dem_max = float(np.nanmin(np.where(ds.read(1) == ds.nodata, np.nan, ds.read(1)))), \
                            float(np.nanmax(np.where(ds.read(1) == ds.nodata, np.nan, ds.read(1))))
    ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
    ax.imshow(hs_rgb, extent=hs_ext, origin="upper", zorder=3, alpha=0.92)
    ax.plot([ix], [iy], marker="*", color="red", markersize=16, markeredgecolor="white",
            markeredgewidth=1.0, zorder=6)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
    rfp.add_scale_bar(ax, ax.get_xlim())
    ax.set_title(f"3DEP DEM hillshade+hypsometric ({dem_res_m:.0f} m, {dem_min:.0f}-{dem_max:.0f} m elev)",
                 fontsize=10)

    if have_cc:
        ax = axes[2]
        cc_data, cc_ext = reproject_band(cc_path, Resampling.nearest)
        cc_masked = np.ma.masked_invalid(cc_data)
        cc_vmax = max(5.0, float(np.nanmax(cc_data))) if np.any(~np.isnan(cc_data)) else 100.0
        ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
        im = ax.imshow(cc_masked, extent=cc_ext, origin="upper", cmap="Greens", vmin=0, vmax=cc_vmax,
                        zorder=3, alpha=0.88)
        ax.plot([ix], [iy], marker="*", color="red", markersize=16, markeredgecolor="white",
                markeredgewidth=1.0, zorder=6)
        ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
        rfp.add_scale_bar(ax, ax.get_xlim())
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
        cb.set_label(f"canopy cover % (data max {cc_vmax:.0f}%)", fontsize=8)
        ax.set_title("LANDFIRE canopy cover (30 m)", fontsize=10)

    fig.suptitle("John Day / Cottonwood Creek reach, OR -- ELMFIRE input context "
                  f"(runs {RUN_ON[:10]}../{RUN_OFF[:10]}..)", fontsize=11)
    fig.text(0.5, 0.01,
              "Source: LANDFIRE FBFM40 + canopy cover, USGS 3DEP DEM, staged into the ELMFIRE 30 m/5070 "
              "build grid for run " + RUN_ON + ". Red star = ignition point (shared by both runs). "
              "Water (fuel code 98) highlighted -- this is the river barrier the spotting demo tests.",
              ha="center", va="bottom", fontsize=7.5, wrap=True)
    fig.tight_layout(rect=(0, 0.035, 1, 0.94))
    out1 = PROOF / "elmfire_john_day_context_layers.png"
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    print("wrote", out1)

    # ---------------- 2) FIRE GROWTH DIAGNOSTIC ------------------------------
    toa_on, toa_ext = reproject_bil_toa(find_toa(RUN_ON), epsg, grid)
    toa_off, toa_ext_off = reproject_bil_toa(find_toa(RUN_OFF), epsg, grid)
    water_mask = np.isnan(fbfm_data) == False
    water_only = np.where(fbfm_data == 98, 1.0, np.nan)

    steps_h = [round(0.5 * i, 1) for i in range(1, 13)]  # 0.5 .. 6.0h
    n_panels = len(steps_h) + 1  # + OFF final comparison
    ncols_m, nrows_m = 4, 4
    fig, axes = plt.subplots(nrows_m, ncols_m, figsize=(4.2 * ncols_m, 4.2 * nrows_m), dpi=110)
    axes_flat = axes.flatten()

    def draw_panel(ax, toa_field, t_cutoff_s, label, final=False):
        ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
        ax.imshow(water_only, extent=ext, origin="upper", cmap="Blues", vmin=0, vmax=1.4,
                  zorder=2, alpha=0.85)
        burned = np.where((toa_field >= 0) & (toa_field <= t_cutoff_s), 1.0, np.nan)
        cmap = "Reds" if not final else "Oranges"
        ax.imshow(burned, extent=toa_ext, origin="upper", cmap=cmap, vmin=0, vmax=1.3,
                  zorder=4, alpha=0.82)
        ax.plot([ix], [iy], marker="*", color="black", markersize=9, markeredgecolor="white",
                markeredgewidth=0.6, zorder=6)
        ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
        ax.text(0.02, 0.96, label, transform=ax.transAxes, fontsize=10, va="top", ha="left",
                color="white", weight="bold",
                path_effects=[patheffects.withStroke(linewidth=2.5, foreground="black")])

    for i, t_h in enumerate(steps_h):
        draw_panel(axes_flat[i], toa_on, t_h * 3600.0, f"ON (spotting)  T+{t_h:.1f}h")
    draw_panel(axes_flat[len(steps_h)], toa_off, duration_s,
               f"OFF final (T+{duration_s/3600:.1f}h)", final=True)
    for j in range(n_panels, nrows_m * ncols_m):
        axes_flat[j].axis("off")

    fig.suptitle("ELMFIRE fire-growth diagnostic -- spotting ON, 0.5h steps, 6h duration "
                 f"(run {RUN_ON}) + OFF final extent (run {RUN_OFF})", fontsize=12)
    fig.text(0.5, 0.005,
              "Blue = river (fuel code 98, the barrier under test). Red = ON-run burned-so-far at each "
              "30-min mark. Orange (last panel) = OFF-run final burned extent for comparison. "
              "Black star = shared ignition point.",
              ha="center", va="bottom", fontsize=8, wrap=True)
    fig.tight_layout(rect=(0, 0.015, 1, 0.96))
    out2 = PROOF / "elmfire_john_day_growth_montage.png"
    fig.savefig(out2, dpi=140)
    plt.close(fig)
    print("wrote", out2)

    # ---------------- animated GIF -------------------------------------------
    frames = []
    for t_h in steps_h:
        fig, ax = plt.subplots(figsize=(6.5, 6.8), dpi=100)
        draw_panel(ax, toa_on, t_h * 3600.0, f"spotting ON -- T+{t_h:.1f}h / 6.0h")
        rfp.add_scale_bar(ax, ax.get_xlim())
        fig.suptitle("ELMFIRE John Day reach -- fire growth (run " + RUN_ON[:12] + "..)", fontsize=9)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        buf.seek(0)
        frames.append(Image.open(buf).convert("RGB"))
    out3 = PROOF / "elmfire_john_day_growth.gif"
    frames[0].save(out3, save_all=True, append_images=frames[1:], duration=650, loop=0)
    print("wrote", out3)

    # ---------------- diagnostic stats ----------------------------------------
    def far_side_stats(toa_field):
        c_river = np.where(np.nanmax(np.where(fbfm_data == 98, 1, 0), axis=0) > 0)[0]
        return {
            "burned_cells": int(np.sum(toa_field >= 0)),
            "max_toa_s": float(np.nanmax(np.where(toa_field >= 0, toa_field, np.nan))) if np.any(toa_field >= 0) else None,
        }

    stats = {"ON": far_side_stats(toa_on), "OFF": far_side_stats(toa_off)}
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
