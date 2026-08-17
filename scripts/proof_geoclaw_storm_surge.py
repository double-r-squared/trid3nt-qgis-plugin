"""GeoClaw storm-surge proofs from the Ike-anchor direct smoke run
(scripts/run_geoclaw_surge_smoke.py ike) + the drag-law A/B run.

Emits to docs/proof/templates/:
  geoclaw_storm_surge.png          -- peak sea-surface-elevation (eta) surge field,
                                      diverging bwr, PINNED frame time + colour scale,
                                      over Esri World Imagery, white AOI box + gauge.
  geoclaw_storm_surge_depth.png    -- onshore inundation depth product map.
  geoclaw_storm_surge_mesh.png     -- the RAW AMR cell-edge grid (density == refine).
  geoclaw_storm_surge_chart.png    -- dock chart: coastal gauge surge waveform +
                                      the Garratt-vs-Powell drag-law A/B overlay
                                      (numeric delta in the caption strip).

The synthetic Gulf shelf is IDEALIZED (modeled shoreline = a straight latitude line
~29.45N, NOT the real Galveston coastline) -- the Esri basemap is geographic
reference only. Stated in every caption strip.

Run (repo root, env loaded):
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/proof_geoclaw_storm_surge.py
"""
from __future__ import annotations

import io
import json
import math
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from PIL import Image
from pyproj import Transformer

sys.path.insert(0, "scripts")
from run_geoclaw_surge_smoke import BBOX, GAUGE, _bathy_z, COAST_LAT  # noqa: E402

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
ART = ("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
       "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/geoclaw_surge")
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
ZOOM = 10
# DETERMINISM: PIN the presentation (frame criterion + colour scale) so a re-smoke
# reads as the same experiment. eta rendered at the frame nearest ETA_T_S; the
# surge field is one-sided (piled water), so a 0..ETA_VMAX sequential-ish diverging
# scale is fixed here, never data-scaled.
ETA_T_S = 6300.0     # near / just after landfall
ETA_VMAX_M = 5.0     # fixed colour scale (0..+5 m surge above datum), clips beyond


def _run_dir(tag: str) -> str:
    for d in sorted(os.listdir(ART)):
        if d.startswith(f"surgesmoke-{tag}"):
            return os.path.join(ART, d)
    raise SystemExit(f"no run dir for tag {tag!r} under {ART} (run the smoke first)")


# --------------------------------------------------------------------------- #
# basemap
# --------------------------------------------------------------------------- #
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


def _box_3857(w, s, e, n):
    x0, y0 = TO_3857.transform(w, s)
    x1, y1 = TO_3857.transform(e, n)
    return x0, y0, x1, y1


def _frame(ax, view):
    lw, ls, le, ln = view
    pad_x = (le - lw) * 0.10
    pad_y = (ln - ls) * 0.10
    wx0, _ = TO_3857.transform(lw - pad_x, ls)
    wx1, _ = TO_3857.transform(le + pad_x, ls)
    _, wy0 = TO_3857.transform(lw, ls - pad_y)
    _, wy1 = TO_3857.transform(lw, ln + pad_y)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    ax.set_xticks([])
    ax.set_yticks([])


def _overlays(ax, gauge=True):
    ax0, ay0, ax1, ay1 = _box_3857(*BBOX)
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=5))
    if gauge:
        gx, gy = TO_3857.transform(GAUGE[0], GAUGE[1])
        ax.plot([gx], [gy], marker="o", markersize=8, markerfacecolor="red",
                markeredgecolor="white", markeredgewidth=1.2, zorder=7)


# --------------------------------------------------------------------------- #
# fort.q parse (h + eta col3) + finest-wins rasterize
# --------------------------------------------------------------------------- #
def parse_fq(text):
    patches = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        hv, s = [], i
        while i < n and len(hv) < 8:
            parts = lines[i].split()
            if len(parts) < 2:
                break
            hv.append(parts[0])
            i += 1
        if len(hv) < 8:
            i = s + 1
            continue
        lvl = int(float(hv[1])); mx, my = int(float(hv[2])), int(float(hv[3]))
        xlow, ylow, dx, dy = float(hv[4]), float(hv[5]), float(hv[6]), float(hv[7])
        H = np.full((my, mx), np.nan); E = np.full((my, mx), np.nan)
        cnt, j, c = 0, 0, 0
        while i < n and cnt < mx * my:
            ln = lines[i].strip(); i += 1
            if not ln:
                if c != 0:
                    j += 1; c = 0
                continue
            p = ln.split()
            try:
                h = float(p[0]); eta = float(p[3]) if len(p) > 3 else float(p[0])
            except (ValueError, IndexError):
                continue
            if j < my and c < mx:
                H[j, c] = h; E[j, c] = eta
            c += 1; cnt += 1
            if c >= mx:
                j += 1; c = 0
        patches.append((lvl, mx, my, xlow, ylow, dx, dy, H, E))
    return patches


def frame_time(path):
    for ln in open(path):
        p = ln.split()
        if p:
            try:
                return float(p[0])
            except ValueError:
                return None
    return None


def rasterize(patches, field, shape, onshore_only=False):
    """Finest-wins raster of 'eta' or 'depth' over the AOI grid."""
    nrows, ncols = shape
    grid = np.full((nrows, ncols), np.nan)
    lvlg = np.zeros((nrows, ncols), dtype=int)
    mnlon, mnlat, mxlon, mxlat = BBOX
    xcen = mnlon + (np.arange(ncols) + 0.5) * (mxlon - mnlon) / ncols
    ycen = mxlat - (np.arange(nrows) + 0.5) * (mxlat - mnlat) / nrows
    for lvl, mx, my, xlow, ylow, dx, dy, H, E in sorted(patches, key=lambda q: q[0]):
        if mx <= 0 or my <= 0 or dx <= 0 or dy <= 0:
            continue
        cols = np.nonzero((xcen >= xlow) & (xcen < xlow + mx * dx))[0]
        rows = np.nonzero((ycen >= ylow) & (ycen < ylow + my * dy))[0]
        if cols.size == 0 or rows.size == 0:
            continue
        pi = np.clip(((xcen[cols] - xlow) / dx).astype(int), 0, mx - 1)
        pj = np.clip(((ycen[rows] - ylow) / dy).astype(int), 0, my - 1)
        sh = H[np.ix_(pj, pi)]; se = E[np.ix_(pj, pi)]
        gx, gy = np.meshgrid(xcen[cols], ycen[rows])
        B = _bathy_z(gx, gy)
        val = se if field == "eta" else sh
        block = grid[np.ix_(rows, cols)]; lb = lvlg[np.ix_(rows, cols)]
        own = lvl >= lb
        wet = np.isfinite(sh) & (sh > 0.02)
        keep = own & wet
        if onshore_only:
            keep = keep & (B > 0.0)
        # surge eta shown only in the coastal band to avoid the far high-land film.
        if field == "eta":
            keep = keep & (B < 4.0)
        block[keep] = val[keep]
        block[own & ~keep] = np.nan
        grid[np.ix_(rows, cols)] = block
        lb[own] = lvl; lvlg[np.ix_(rows, cols)] = lb
    return grid


def grid_to_3857(grid):
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, calculate_default_transform, reproject
    nrows, ncols = grid.shape
    src_t = from_bounds(BBOX[0], BBOX[1], BBOX[2], BBOX[3], ncols, nrows)
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=ncols, height=nrows, count=1,
                     dtype="float32", crs="EPSG:4326", transform=src_t,
                     nodata=float("nan")) as ds:
            ds.write(grid.astype("float32"), 1)
        with mf.open() as src:
            transform, w, h = calculate_default_transform(
                src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
            dst = np.full((h, w), np.nan, dtype="float32")
            reproject(source=rasterio.band(src, 1), destination=dst,
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=transform, dst_crs="EPSG:3857",
                      src_nodata=np.nan, dst_nodata=np.nan,
                      resampling=Resampling.bilinear)
            wb, sb, eb, nb = rasterio.transform.array_bounds(h, w, transform)
    return dst, (wb, eb, sb, nb)


def _frame_nearest(out_dir, t_target):
    best = None
    for fq in sorted(os.listdir(out_dir)):
        if not fq.startswith("fort.q"):
            continue
        ft = os.path.join(out_dir, fq.replace("fort.q", "fort.t"))
        if not os.path.exists(ft):
            continue
        t = frame_time(ft)
        if t is None:
            continue
        d = abs(t - t_target)
        if best is None or d < best[2]:
            best = (fq, t, d)
    return best[:2] if best else (None, None)


def _basemap_for_view():
    view = BBOX
    px = (view[2] - view[0]) * 0.10; py = (view[3] - view[1]) * 0.10
    return fetch_basemap(view[0] - px, view[1] - py, view[2] + px, view[3] + py, ZOOM), view


# --------------------------------------------------------------------------- #
def render_eta(out_dir):
    fq, t_s = _frame_nearest(out_dir, ETA_T_S)
    patches = parse_fq(open(os.path.join(out_dir, fq)).read())
    eta = rasterize(patches, "eta", (400, 500))
    eta3857, (w, e, s_, n) = grid_to_3857(eta)
    (basemap, bm_extent), view = _basemap_for_view()
    fig, ax = plt.subplots(figsize=(10, 8.6), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    im = ax.imshow(eta3857, extent=(w, e, s_, n), origin="upper", cmap="turbo",
                   norm=Normalize(0, ETA_VMAX_M), alpha=0.82, zorder=3)
    _overlays(ax); _frame(ax, view)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02, extend="max")
    cb.set_label("storm-surge sea-surface elevation above datum (m)")
    wet = int(np.isfinite(eta3857).sum())
    fig.text(0.5, 0.02,
             f"geoclaw_storm_surge  |  Hurricane Ike (NHC ATCF bal092008), Garratt drag  |  "
             f"RASTER: coastal surge eta (t={t_s:.0f}s from landfall, fixed 0..{ETA_VMAX_M:.0f} m scale)  |  "
             f"white box = AOI, red dot = gauge  |  IDEALIZED planar Gulf shelf (shoreline ~{COAST_LAT:.2f}N, not the real coast)  |  "
             f"Esri World Imagery basemap (reference)",
             ha="center", fontsize=5.4, color="0.4")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.06)
    out = os.path.join(OUT_DIR, "geoclaw_storm_surge.png")
    fig.savefig(out); plt.close(fig)
    print("wrote", out, f"| eta frame {fq} t={t_s:.0f}s wetcells={wet}")


def render_depth(out_dir):
    # peak onshore inundation depth across all frames (finest-wins per frame, max over t).
    peak = None
    for fq in sorted(os.listdir(out_dir)):
        if not fq.startswith("fort.q"):
            continue
        patches = parse_fq(open(os.path.join(out_dir, fq)).read())
        g = rasterize(patches, "depth", (400, 500), onshore_only=True)
        peak = g if peak is None else np.fmax(peak, np.where(np.isfinite(g), g, np.nan))
    depth3857, (w, e, s_, n) = grid_to_3857(peak)
    (basemap, bm_extent), view = _basemap_for_view()
    fig, ax = plt.subplots(figsize=(10, 8.6), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    vmax = float(np.nanpercentile(depth3857, 99)) if np.isfinite(depth3857).any() else 1.0
    im = ax.imshow(depth3857, extent=(w, e, s_, n), origin="upper", cmap="YlGnBu",
                   norm=Normalize(0, max(vmax, 0.5)), alpha=0.85, zorder=3)
    _overlays(ax); _frame(ax, view)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("peak onshore surge inundation depth (m)")
    fig.text(0.5, 0.02,
             f"geoclaw_storm_surge  |  Hurricane Ike (bal092008), Garratt drag  |  "
             f"RASTER: peak ONSHORE surge inundation depth (max over frames)  |  "
             f"white box = AOI, red dot = gauge  |  IDEALIZED planar Gulf shelf  |  "
             f"Esri World Imagery basemap (reference)",
             ha="center", fontsize=5.4, color="0.4")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.06)
    out = os.path.join(OUT_DIR, "geoclaw_storm_surge_depth.png")
    fig.savefig(out); plt.close(fig)
    print("wrote", out, f"| onshore depth vmax={vmax:.2f}")


def render_mesh(out_dir):
    # RAW AMR mesh: cell-edge grid lines per fort.q patch (density == refinement).
    fq, t_s = _frame_nearest(out_dir, ETA_T_S)
    patches = parse_fq(open(os.path.join(out_dir, fq)).read())
    segs = []
    levels = {}
    for lvl, mx, my, xlow, ylow, dx, dy, H, E in patches:
        levels[lvl] = levels.get(lvl, 0) + 1
        xe = xlow + np.arange(mx + 1) * dx
        ye = ylow + np.arange(my + 1) * dy
        for x in xe:  # vertical lines
            x0, y0 = TO_3857.transform(x, ye[0]); x1, y1 = TO_3857.transform(x, ye[-1])
            segs.append([(x0, y0), (x1, y1)])
        for y in ye:  # horizontal lines
            x0, y0 = TO_3857.transform(xe[0], y); x1, y1 = TO_3857.transform(xe[-1], y)
            segs.append([(x0, y0), (x1, y1)])
    (basemap, bm_extent), view = _basemap_for_view()
    fig, ax = plt.subplots(figsize=(10, 8.6), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    ax.add_collection(LineCollection(segs, colors="black", linewidths=0.35,
                                     zorder=3, alpha=0.9))
    _overlays(ax, gauge=False); _frame(ax, view)
    hist = {f"L{k}": v for k, v in sorted(levels.items())}
    fig.text(0.5, 0.02,
             f"geoclaw_storm_surge  |  RAW AMR mesh: actual cell-edge grid lines, all levels one colour -- "
             f"density IS the refinement  |  patches={len(patches)} (per-level {hist}), frame {fq} t={t_s:.0f}s  |  "
             f"white box = AOI  |  Esri World Imagery basemap (reference)",
             ha="center", fontsize=5.4, color="0.4")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.05)
    out = os.path.join(OUT_DIR, "geoclaw_storm_surge_mesh.png")
    fig.savefig(out); plt.close(fig)
    print("wrote", out, f"| {len(segs)} grid lines patches={len(patches)} {hist}")


def render_chart():
    """Dock chart: coastal gauge surge waveform + Garratt-vs-Powell A/B overlay."""
    ike = json.load(open(os.path.join(_run_dir("ike"), "gauge_waveform.json")))
    ga = json.load(open(os.path.join(_run_dir("ab-garratt"), "gauge_waveform.json")))
    po = json.load(open(os.path.join(_run_dir("ab-powell"), "gauge_waveform.json")))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.0), dpi=130)
    # (a) Ike gauge surge waveform.
    t = np.array(ike["gauge_t_s"]) / 3600.0
    ax1.plot(t, ike["gauge_eta_m"], color="#1f5fbf", lw=1.6)
    ax1.axhline(0, color="0.6", lw=0.7)
    ax1.axvline(0, color="0.6", lw=0.7, ls=(0, (4, 3)))
    ax1.set_xlabel("time from landfall (hours)")
    ax1.set_ylabel("surge surface elevation (m)")
    ax1.set_title("Ike coastal gauge waveform", fontsize=9)
    ax1.grid(True, alpha=0.25)

    # (b) drag-law A/B overlay (gauge eta).
    tg = np.array(ga["gauge_t_s"]) / 3600.0
    tp = np.array(po["gauge_t_s"]) / 3600.0
    ax2.plot(tg, ga["gauge_eta_m"], color="#c1440e", lw=1.6, label="Garratt")
    ax2.plot(tp, po["gauge_eta_m"], color="#1f8f4e", lw=1.6, ls=(0, (5, 2)), label="Powell")
    ax2.axhline(0, color="0.6", lw=0.7)
    ax2.set_xlabel("time from landfall (hours)")
    ax2.set_ylabel("surge surface elevation (m)")
    ax2.set_title("wind-drag-law A/B (synthetic track)", fontsize=9)
    ax2.legend(fontsize=8, loc="upper left")
    ax2.grid(True, alpha=0.25)

    dpk = ga["gauge_peak_eta_m"] - po["gauge_peak_eta_m"]
    fig.text(0.5, 0.005,
             f"geoclaw_storm_surge  |  (a) Ike gauge peak {ike['gauge_peak_eta_m']:.2f} m  "
             f"(b) drag-law A/B gauge peak: Garratt {ga['gauge_peak_eta_m']:.2f} m vs Powell "
             f"{po['gauge_peak_eta_m']:.2f} m  ->  delta {dpk:+.2f} m (the knob measurably moves the surge)",
             ha="center", fontsize=6.4, color="0.35")
    fig.subplots_adjust(left=0.08, right=0.985, top=0.90, bottom=0.20, wspace=0.28)
    out = os.path.join(OUT_DIR, "geoclaw_storm_surge_chart.png")
    fig.savefig(out); plt.close(fig)
    print("wrote", out, f"| Ike peak {ike['gauge_peak_eta_m']:.2f} m, A/B delta {dpk:+.2f} m")


def main():
    ike_out = os.path.join(_run_dir("ike"), "_output")
    render_eta(ike_out)
    render_depth(ike_out)
    render_mesh(ike_out)
    render_chart()
    return 0


if __name__ == "__main__":
    sys.exit(main())
