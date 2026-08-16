"""Proofs for the ADR 0161 ELMFIRE transient-weather + crown-fire fronts.

Solves the representative constant/transient/canopied decks directly through the
rebuilt trid3nt/elmfire:dev image (build_constant_flat_deck agent-side -> docker
run binary), reads the time-of-arrival + crown-fire rasters, and renders:

  - ToA maps over Esri World Imagery (white box = AOI, red dot = ignition);
  - the constant-vs-shifting-wind ToA contour comparison in ONE figure;
  - the crown initiation-boundary + Cruz-rate-ceiling comparison in ONE figure;
  - the dead-fuel interpolation-cadence accuracy-vs-cost chart.

Readability laws: quantitative axes, legends, no annotation box over the plot,
no suptitle; pinned frames/scales live in the caption strip.
"""
from __future__ import annotations

import glob
import io
import math
import os
import subprocess
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from matplotlib.colors import Normalize
from PIL import Image
from pyproj import Transformer
from rasterio.warp import (
    Resampling, calculate_default_transform, reproject, transform_bounds,
)

REPO = "/home/nate/Documents/trid3nt-local"
sys.path.insert(0, REPO)
sys.path.insert(0, REPO + "/contracts/src")
os.environ.setdefault("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")

from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs  # noqa: E402
from trid3nt_server.agent.workflows.elmfire.run_elmfire import (  # noqa: E402
    build_constant_flat_deck,
)

OUT = REPO + "/docs/proof/templates"
IMAGE = "trid3nt/elmfire:dev"
BIN = "elmfire_2025.0526"
CENTER = (-98.5, 38.5)
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_FROM_3857 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def _to_lonlat(x, y):
    return _FROM_3857.transform(x, y)
CANOPY = {"cc": 60, "ch": 375, "cbh": 10, "cbd": 18}


def bbox(domain_km):
    hl = (domain_km * 1000 / 2) / 111320.0
    ho = hl / max(math.cos(math.radians(CENTER[1])), 1e-6)
    return (CENTER[0] - ho, CENTER[1] - hl, CENTER[0] + ho, CENTER[1] + hl)


def run_deck(d):
    cmd = ["docker", "run", "--rm", "--entrypoint", "/bin/bash", "--cpus", "2",
           "-v", f"{d}:/deck", "-w", "/deck", IMAGE,
           "-c", f"mkdir -p outputs scratch && {BIN} ./inputs/elmfire.data"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=400)


def solve(domain_km=8.0, **kw):
    """Build + solve a deck; return (toa_hours_5070_path, crown_path|None, deck_dir)."""
    d = tempfile.mkdtemp(prefix="elm-proof-")
    args = ElmfireRunArgs(
        bbox=bbox(domain_km), ignition_lonlat=CENTER,
        wind_speed_mph=kw.pop("wind_speed_mph", 22.0),
        wind_dir_deg=kw.pop("wind_dir_deg", 270.0),
        fuel_moisture=kw.pop("fuel_moisture", "dry"),
        duration_hours=kw.pop("duration_hours", 0.5),
        cellsize_m=kw.pop("cellsize_m", 60.0),
    )
    build_constant_flat_deck(args, d, **kw)
    run_deck(d)
    tf = sorted(glob.glob(f"{d}/outputs/time_of_arrival_*.bil"))
    cf = sorted(glob.glob(f"{d}/outputs/crown_fire_[0-9]*.bil"))
    return (tf[-1] if tf else None), (cf[-1] if cf else None), d


def read_hr(path):
    with rasterio.open(path) as ds:
        a = ds.read(1).astype("float64")
        a[a == -9999.0] = np.nan
    return a / 3600.0  # seconds -> hours


def tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def tile_bounds_3857(x, y, z):
    n = 2 ** z
    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)
    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def fetch_basemap(w, s, e, n, zoom):
    x0f, y1f = tile_xy(w, s, zoom)
    x1f, y0f = tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, _, _, _ = tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def to_3857(path):
    # The ELMFIRE BIL outputs carry the geotransform but NO CRS; the deck grid is
    # EPSG:5070 (the deck-builder canon), stamped on the read.
    src_crs = "EPSG:5070"
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        arr[arr == -9999.0] = np.nan
        transform, width, height = calculate_default_transform(
            src_crs, "EPSG:3857", src.width, src.height, *src.bounds)
        dst = np.full((height, width), np.nan, dtype="float32")
        reproject(source=(arr / 3600.0).astype("float32"), destination=dst,
                  src_transform=src.transform, src_crs=src_crs,
                  dst_transform=transform, dst_crs="EPSG:3857",
                  src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.nearest)
        w, s_, e, n = rasterio.transform.array_bounds(height, width, transform)
        lb = transform_bounds(src_crs, "EPSG:4326", *src.bounds)
    return dst, (w, e, s_, n), lb


def toa_map(toa_path, out_name, title, caption, dom_km):
    toa, (w, e, s_, n), (lw, ls, le, ln) = to_3857(toa_path)
    # Window on the FIRE extent (not the whole AOI) so the burn fills the frame.
    ny, nx = toa.shape
    ys_i, xs_i = np.where(np.isfinite(toa))
    xr = np.linspace(w, e, nx); yr = np.linspace(n, s_, ny)  # 3857, origin upper
    fx0, fx1 = xr[xs_i.min()], xr[xs_i.max()]
    fy0, fy1 = yr[ys_i.max()], yr[ys_i.min()]
    mx = max((fx1 - fx0), (fy1 - fy0)) * 0.9 + 400.0
    cx = (fx0 + fx1) / 2.0; cy = (fy0 + fy1) / 2.0
    win_w, win_s = _to_lonlat(cx - mx, cy - mx)
    win_e, win_n = _to_lonlat(cx + mx, cy + mx)
    basemap, bm_ext = fetch_basemap(win_w, win_s, win_e, win_n, 13)
    fig, ax = plt.subplots(figsize=(8.4, 8.0), dpi=115)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    vmax = float(np.nanpercentile(toa, 99)) if np.isfinite(toa).any() else 1.0
    im = ax.imshow(toa, extent=(w, e, s_, n), origin="upper",
                   cmap="inferno", norm=Normalize(0, vmax), alpha=0.85, zorder=3)
    # white AOI box (may sit at/beyond the frame edge - the fire window is tighter)
    bx0, by0 = TO_3857.transform(lw, ls)
    bx1, by1 = TO_3857.transform(le, ln)
    ax.plot([bx0, bx1, bx1, bx0, bx0], [by0, by0, by1, by1, by0],
            color="white", lw=1.6, zorder=4)
    ix, iy = TO_3857.transform(CENTER[0], CENTER[1])
    ax.plot(ix, iy, marker="o", color="#00e5ff", ms=8, mec="black", zorder=5)
    ax.set_xlim(cx - mx, cx + mx); ax.set_ylim(cy - mx, cy + mx)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title, fontsize=12, pad=8)
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
    cb.set_label("time of arrival (hours from ignition)")
    fig.text(0.5, 0.02, caption, ha="center", fontsize=8.5, wrap=True)
    fig.subplots_adjust(bottom=0.11, top=0.95)
    fig.savefig(f"{OUT}/{out_name}", dpi=115)
    plt.close(fig)
    print("wrote", out_name)


def compare_map(items, out_name, title, caption, aoi_km, zoom=12):
    """Overlay several scenarios' ToA contours (hour-labeled) on the Esri basemap.

    ``items`` = [(toa_path, color, linestyle, label), ...]. White box = AOI, cyan
    dot = ignition; the emitted raster's data (arrival-time contours) reads ON THE
    MAP. Frame = the AOI (+ small margin) so the white AOI box is visible."""
    from matplotlib.lines import Line2D

    lw_a, ls_a, le_a, ln_a = bbox(aoi_km)
    mlon = (le_a - lw_a) * 0.06
    mlat = (ln_a - ls_a) * 0.06
    basemap, bm_ext = fetch_basemap(lw_a - mlon, ls_a - mlat, le_a + mlon, ln_a + mlat, zoom)
    fig, ax = plt.subplots(figsize=(8.6, 8.2), dpi=115)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    grids = []
    all_max = 0.0
    for path, color, ls, label in items:
        toa, (w, e, s_, n), _ = to_3857(path)
        nyi, nxi = toa.shape
        X = np.linspace(w, e, nxi)
        Y = np.linspace(n, s_, nyi)
        grids.append((X, Y, toa, color, ls, label))
        if np.isfinite(toa).any():
            all_max = max(all_max, float(np.nanmax(toa)))
    levels = np.linspace(all_max / 6.0, all_max * 0.98, 5)
    handles = []
    for X, Y, toa, color, ls, label in grids:
        cs = ax.contour(X, Y, toa, levels=levels, colors=color, linewidths=1.6,
                        linestyles=ls, zorder=3)
        ax.clabel(cs, inline=True, fontsize=7, fmt="%.1f")
        handles.append(Line2D([0], [0], color=color, lw=1.8, ls=ls))
    bx0, by0 = TO_3857.transform(lw_a, ls_a)
    bx1, by1 = TO_3857.transform(le_a, ln_a)
    ax.plot([bx0, bx1, bx1, bx0, bx0], [by0, by0, by1, by1, by0],
            color="white", lw=1.6, zorder=4)
    ix, iy = TO_3857.transform(CENTER[0], CENTER[1])
    ax.plot(ix, iy, marker="o", color="#00e5ff", ms=8, mec="black", zorder=5)
    ax.legend(handles, [it[3] for it in items], loc="upper left", framealpha=0.85)
    ax.set_xlim(bm_ext[0], bm_ext[1]); ax.set_ylim(bm_ext[2], bm_ext[3])
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title, fontsize=12, pad=8)
    fig.text(0.5, 0.02, caption, ha="center", fontsize=8.5, wrap=True)
    fig.subplots_adjust(bottom=0.10, top=0.95)
    fig.savefig(f"{OUT}/{out_name}", dpi=115)
    plt.close(fig)
    print("wrote", out_name)


def frame_grid(toa_hr, cellsize_m):
    """(x_m, y_m) meshgrid centred on ignition (domain centre) for a ToA grid."""
    ny, nx = toa_hr.shape
    xs = (np.arange(nx) - (nx - 1) / 2.0) * cellsize_m
    ys = -(np.arange(ny) - (ny - 1) / 2.0) * cellsize_m  # north positive
    return np.meshgrid(xs, ys)


# --------------------------------------------------------------------------- #
def proof_transient_wind():
    print("== transient wind ==")
    dom = 12.0
    tf_c, _, dc = solve(domain_km=dom, wind_dir_deg=270.0, duration_hours=2.0,
                        wind_speed_mph=25.0, cellsize_m=60.0, fuel_model=102)
    tf_t, _, dt = solve(domain_km=dom, wind_dir_deg=270.0, duration_hours=2.0,
                        wind_speed_mph=25.0, cellsize_m=60.0, fuel_model=102,
                        weather_schedule=[{"wd": 270.0}, {"wd": 180.0}],
                        dt_meteorology_s=3600.0)
    # Map of the redirected (transient) fire over Esri.
    toa_map(tf_t, "elmfire_transient_wind_schedule_spread.png",
            "Fire arrival time - mid-run wind shift (FROM 270 deg -> 180 deg)",
            "Frame: EPSG:5070 60 m, GR2 grass, 25 mph, 2 h burn; wind FROM 270 deg then shifts "
            "to 180 deg at 50% of the run (windowed on the burn). Cyan dot = ignition.",
            dom)
    # Measured heading shift of the burned-area centroid (constant -> transient).
    c = read_hr(tf_c); t = read_hr(tf_t)
    Xc, Yc = frame_grid(c, 60.0)
    def _az(grid, X, Y):
        m = np.isfinite(grid)
        return math.degrees(math.atan2(float(Y[m].mean()), float(X[m].mean())))
    shift = abs(_az(t, Xc, Yc) - _az(c, Xc, Yc))
    # Comparison AS A MAP: both scenarios' ToA contours over Esri (data on the map).
    compare_map(
        [(tf_c, "#1f5fbf", "-", "constant wind (FROM 270)"),
         (tf_t, "#d1495b", "--", "mid-run shift (270 -> 180)")],
        "elmfire_transient_wind_schedule_spread_chart.png",
        "Time-of-arrival contours over the AOI: constant wind vs a mid-run wind shift",
        "Contours = hours from ignition on Esri World Imagery. Constant wind (blue) drives the "
        f"fire due east; the mid-run shift (red dashed) bends the spread axis northward "
        f"(measured centroid heading shift {shift:.0f} deg). White box = AOI, cyan dot = ignition.",
        dom, zoom=12)
    import shutil
    for d in (dc, dt):
        shutil.rmtree(d, ignore_errors=True)


def _crown_area(crown_path, cellsize_m):
    with rasterio.open(crown_path) as ds:
        a = ds.read(1).astype("float64")
    return int((a >= 1.5).sum()) * (cellsize_m ** 2) / 1e6


def _burned_area(toa_path, cellsize_m):
    a = read_hr(toa_path)
    return int(np.isfinite(a).sum()) * (cellsize_m ** 2) / 1e6


def proof_crown():
    print("== crown ==")
    def crown_solve(ccc=None, limit=None):
        se = {"CROWN_FIRE_MODEL": "1", "BANDTHICKNESS": "3"}
        if ccc is not None:
            se["CRITICAL_CANOPY_COVER"] = f"{ccc:.4f}"
        if limit is not None:
            se["CROWN_FIRE_SPREAD_RATE_LIMIT"] = f"{limit:.1f}"
        return solve(domain_km=8.0, wind_speed_mph=25.0, duration_hours=0.5,
                     cellsize_m=60.0, fuel_model=147, canopy=dict(CANOPY),
                     target_cfl=0.3, simulator_extra=se,
                     outputs_extra={"DUMP_CROWN_FIRE": ".TRUE."})
    # Initiation sweep (3 thresholds), ceiling contrast (capped vs uncapped).
    init = [(v, crown_solve(ccc=v)) for v in (0.30, 0.525, 0.75)]
    cap_tf, _, dcap = crown_solve(limit=120.0)
    unc_tf, _, dunc = crown_solve(limit=99999.0)
    cap_a = _burned_area(cap_tf, 60.0); unc_a = _burned_area(unc_tf, 60.0)

    # Ceiling EXTENT CONTRAST as a MAP: capped vs uncapped ToA over Esri.
    compare_map(
        [(unc_tf, "#d1495b", "-", f"uncapped Cruz ({unc_a:.2f} km2)"),
         (cap_tf, "#1f5fbf", "-", f"capped 120 ft/min ({cap_a:.2f} km2)")],
        "elmfire_crown_fire_initiation_threshold_sweep.png",
        "Crown-fire extent: Cruz active-crown rate ceiling capped vs uncapped",
        "Time-of-arrival contours (hours) over Esri World Imagery, SH7 shrub + canopy "
        "(cc=60%, cbh=1.0 m, cbd=0.18 kg/m3, ch=37.5 m), 25 mph, 0.5 h burn. Lifting "
        f"CROWN_FIRE_SPREAD_RATE_LIMIT from 120 ft/min to uncapped grows the burn {unc_a/cap_a:.1f}x. "
        "White box = AOI, cyan dot = ignition.",
        8.0, zoom=13)

    # Initiation boundary as a PURE dock chart (active-crown area vs threshold).
    fig, ax = plt.subplots(figsize=(7.4, 5.4), dpi=115)
    xs = [v for v, _ in init]
    ys = [_crown_area(r[1], 60.0) for _, r in init]
    ax.plot(xs, ys, "-o", color="#d1495b", lw=1.8)
    ax.axvline(0.60, color="#3a3a3a", ls=":", lw=1.3)
    ax.text(0.60, max(ys) * 0.5, " deck canopy cover 0.60", rotation=90,
            va="center", fontsize=8, color="#3a3a3a")
    ax.set_xlabel("CRITICAL_CANOPY_COVER threshold (fraction)")
    ax.set_ylabel("active-crown area (km2)")
    ax.set_title("Crown-fire initiation boundary vs critical canopy cover", fontsize=11)
    ax.grid(alpha=0.25)
    fig.text(0.5, 0.015,
             "Active-crown area collapses to zero once CRITICAL_CANOPY_COVER rises past the deck's "
             "0.60 canopy cover (dotted) - the surface-to-crown initiation boundary. SH7 shrub + "
             "canopy, 25 mph, 0.5 h burn.",
             ha="center", fontsize=8.5, wrap=True)
    fig.subplots_adjust(bottom=0.17)
    fig.savefig(f"{OUT}/elmfire_crown_fire_initiation_threshold_sweep_chart.png", dpi=115)
    plt.close(fig)
    print("wrote elmfire_crown_fire_initiation_threshold_sweep_chart.png")
    import shutil
    for _, r in init:
        shutil.rmtree(r[2], ignore_errors=True)
    shutil.rmtree(dcap, ignore_errors=True); shutil.rmtree(dunc, ignore_errors=True)


def proof_dead_fuel():
    print("== dead fuel interp ==")
    sched = [{"m1": 3.0, "m10": 4.0, "m100": 5.0},
             {"m1": 3.0 + 7.0 / 3, "m10": 4.0 + 7.0 / 3, "m100": 5.0 + 7.0 / 3},
             {"m1": 3.0 + 14.0 / 3, "m10": 4.0 + 14.0 / 3, "m100": 5.0 + 14.0 / 3},
             {"m1": 10.0, "m10": 11.0, "m100": 12.0}]
    dtmet = 3600.0 / (len(sched) - 1)
    cads = [60.0, 660.0, 1260.0, 1800.0]
    pts = []
    ref_tf = None
    dirs = []
    for cad in cads:
        tf, _, d = solve(domain_km=8.0, wind_speed_mph=18.0, duration_hours=1.0,
                         cellsize_m=60.0, fuel_model=102, weather_schedule=sched,
                         dt_meteorology_s=dtmet,
                         time_control_extra={
                             "DT_INTERPOLATE_M1": f"{cad:.1f}",
                             "DT_INTERPOLATE_M10": f"{cad*10:.1f}",
                             "DT_INTERPOLATE_M100": f"{cad*100:.1f}"})
        pts.append((cad, _burned_area(tf, 60.0)))
        if cad == cads[0]:
            ref_tf = tf; ref_dir = d
        else:
            dirs.append(d)
    ref = pts[0][1]
    fig, ax = plt.subplots(figsize=(7.4, 5.4), dpi=115)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    ax.plot(xs, ys, "-o", color="#d1495b", lw=1.8)
    ax.axhline(ref, color="#3a3a3a", ls=":", lw=1.3)
    ax.text(xs[-1], ref, "  reference (finest cadence)", va="bottom", ha="right",
            fontsize=8, color="#3a3a3a")
    ax.set_xlabel("dead-fuel moisture interpolation cadence DT_INTERPOLATE_M1 (s)")
    ax.set_ylabel("burned area (km2)")
    ax.set_title("Accuracy vs cost: dead-fuel moisture interpolation cadence", fontsize=11)
    ax.grid(alpha=0.25)
    fig.text(0.5, 0.015,
             "Transient moisture-recovery deck (1-hr dead-fuel moisture 3%->10% over 1 h, 4 met bands). "
             f"Coarsening the interpolation cadence (cheaper) lags the recovering moisture; burned area drifts "
             f"{(ys[-1]-ref)/ref*100:.0f}% above the finest-cadence reference.",
             ha="center", fontsize=8.5, wrap=True)
    fig.subplots_adjust(bottom=0.17)
    fig.savefig(f"{OUT}/elmfire_dead_fuel_moisture_interpolation_frequency_control_chart.png", dpi=115)
    plt.close(fig)
    print("wrote elmfire_dead_fuel_moisture_interpolation_frequency_control_chart.png")
    toa_map(ref_tf, "elmfire_dead_fuel_moisture_interpolation_frequency_control.png",
            "Fire arrival time - dead-fuel interpolation reference (finest cadence)",
            "Frame: EPSG:5070 60 m, 8 km domain, GR2 grass, 18 mph, 1 h burn, moisture-recovery "
            "schedule, DT_INTERPOLATE_M1=60 s (reference). White box = AOI, cyan dot = ignition.",
            8.0)
    import shutil
    shutil.rmtree(ref_dir, ignore_errors=True)
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(OUT, exist_ok=True)
    if which in ("all", "wind"):
        proof_transient_wind()
    if which in ("all", "crown"):
        proof_crown()
    if which in ("all", "dead"):
        proof_dead_fuel()
    print("PROOFS DONE")
