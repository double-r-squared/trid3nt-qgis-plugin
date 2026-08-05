"""Render the GeoClaw amr_regions proofs from ONE live re-smoke run:

  * amr_regions_mesh.png -- the RAW UNIFIED MESH (Clawpack-gallery style): the
    actual AMR cell-edge grid lines from the emitted mesh.geojson, ONE colour
    (black, thin). Refinement is self-evident because the grid gets DENSER where
    the solver refined -- NO per-level colour/weight coding, no abstraction. The
    ONLY overlay is the yellow dashed user AMR window (the residual of the user's
    edit) + the white AOI box, over Esri World Imagery.
  * amr_regions.png -- the MID-RUN SEA-SURFACE ANOMALY (eta) snapshot (the
    approved ADR 0148 raster style): a full-AOI wave field on the diverging
    blue-white-red ramp (Clawpack-gallery), from a mid-run fort.q frame with the
    wave inside the domain, symmetric vmin/vmax.
  * amr_regions_depth.png -- the peak-inundation depth product map from the SAME
    run.

Captions live in the bottom strip; no annotation boxes over the plot, no
suptitle, no _relief variants.

Run (from repo root, env loaded):
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/proof_geoclaw_amr_mesh.py
"""
from __future__ import annotations

import io
import json
import math
import os
import sys

import boto3
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
TMP = "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad"
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

# The live re-smoke run (agent-side postprocess run_id carrying the depth COG +
# mesh.geojson) and the SOLVER run_id carrying the raw fort.q AMR frames.
RUN_PREFIX = "01KZ9P4FG93F0J7TQQSYPHGBA4"
SOLVER_PREFIX = "01KZ9P4FGQPKPCZV4W7C4KR9EK"
BBOX = (-124.24, 41.73, -124.16, 41.78)  # AOI (white box)
WINDOW = (-124.21, 41.745, -124.18, 41.770)  # user AMR window (yellow dashed)
GAUGE = (-124.20, 41.7325)  # coastal gauge (red dot)
ETA_FRAME = 4  # mid-run fort.q frame (t=720 s) -- wave inside the domain
ZOOM = 13


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
    pad_x = (le - lw) * 0.12
    pad_y = (ln - ls) * 0.12
    wx0, _ = TO_3857.transform(lw - pad_x, ls)
    wx1, _ = TO_3857.transform(le + pad_x, ls)
    _, wy0 = TO_3857.transform(lw, ls - pad_y)
    _, wy1 = TO_3857.transform(lw, ln + pad_y)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    ax.set_xticks([])
    ax.set_yticks([])
    return pad_x, pad_y


def _overlays(ax, gauge=False):
    # White AOI box.
    ax0, ay0, ax1, ay1 = _box_3857(*(BBOX[0], BBOX[1], BBOX[2], BBOX[3]))
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=5))
    # Yellow dashed user AMR window.
    wx0, wy0, wx1, wy1 = _box_3857(*(WINDOW[0], WINDOW[1], WINDOW[2], WINDOW[3]))
    ax.add_patch(Rectangle((wx0, wy0), wx1 - wx0, wy1 - wy0, fill=False,
                           edgecolor="#ffd000", linewidth=2.2, linestyle=(0, (6, 4)),
                           zorder=6))
    if gauge:
        gx, gy = TO_3857.transform(GAUGE[0], GAUGE[1])
        ax.plot([gx], [gy], marker="o", markersize=8, markerfacecolor="red",
                markeredgecolor="white", markeredgewidth=1.2, zorder=7)


# --------------------------------------------------------------------------- #
# fort.q AMR frame parsing (h + eta) + finest-wins eta rasterize.
# --------------------------------------------------------------------------- #
import re as _re

_HV = _re.compile(r"^\s*([-+0-9.eE]+)\s+(\w+)")


def parse_fort_q_h_eta(text):
    """Parse one fort.q frame -> list of (level, mx, my, xlow, ylow, dx, dy, H, E)
    where H = depth (col0) and E = surface elevation eta (col3)."""
    patches = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        hv, s = [], i
        while i < n and len(hv) < 8:
            m = _HV.match(lines[i])
            if not m:
                break
            hv.append(m.group(1))
            i += 1
        if len(hv) < 8:
            i = s + 1
            continue
        lvl = int(float(hv[1]))
        mx, my = int(float(hv[2])), int(float(hv[3]))
        xlow, ylow, dx, dy = (float(hv[4]), float(hv[5]), float(hv[6]), float(hv[7]))
        H = np.full((my, mx), np.nan)
        E = np.full((my, mx), np.nan)
        cnt, j, c = 0, 0, 0
        while i < n and cnt < mx * my:
            ln = lines[i].strip()
            i += 1
            if not ln:
                if c != 0:
                    j += 1
                    c = 0
                continue
            p = ln.split()
            try:
                h = float(p[0])
                eta = float(p[3]) if len(p) > 3 else float(p[0])
            except (ValueError, IndexError):
                continue
            if j < my and c < mx:
                H[j, c] = h
                E[j, c] = eta
            c += 1
            cnt += 1
            if c >= mx:
                j += 1
                c = 0
        patches.append((lvl, mx, my, xlow, ylow, dx, dy, H, E))
    return patches


def rasterize_eta(patches, bbox, shape):
    """Finest-wins eta over the AOI grid, masked to NaN where dry (h <= 0.01)."""
    nrows, ncols = shape
    grid = np.full((nrows, ncols), np.nan)
    lvlg = np.zeros((nrows, ncols), dtype=int)
    mnlon, mnlat, mxlon, mxlat = bbox
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
        sub_e = E[np.ix_(pj, pi)]
        sub_h = H[np.ix_(pj, pi)]
        block = grid[np.ix_(rows, cols)]
        lb = lvlg[np.ix_(rows, cols)]
        own = lvl >= lb
        wet = np.isfinite(sub_h) & (sub_h > 0.01)
        block[own & wet] = sub_e[own & wet]
        block[own & ~wet] = np.nan
        grid[np.ix_(rows, cols)] = block
        lb[own] = lvl
        lvlg[np.ix_(rows, cols)] = lb
    return grid


def _grid_to_3857(grid, bbox):
    """Reproject an EPSG:4326 AOI grid to 3857; return (arr, (w,e,s,n))."""
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    nrows, ncols = grid.shape
    src_t = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], ncols, nrows)
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


def render_eta(s3):
    txt = s3.get_object(Bucket=os.environ["TRID3NT_RUNS_BUCKET"],
                        Key=f"{SOLVER_PREFIX}/_output/fort.q{ETA_FRAME:04d}")["Body"].read()
    tsec = s3.get_object(Bucket=os.environ["TRID3NT_RUNS_BUCKET"],
                         Key=f"{SOLVER_PREFIX}/_output/fort.t{ETA_FRAME:04d}")["Body"].read()
    t_s = float(tsec.decode("utf-8", "replace").split()[0])
    patches = parse_fort_q_h_eta(txt.decode("utf-8", "replace"))
    eta = rasterize_eta(patches, BBOX, (360, 560))
    eta3857, (w, e, s_, n) = _grid_to_3857(eta, BBOX)

    finite = eta3857[np.isfinite(eta3857)]
    vlim = max(0.3, round(float(np.nanpercentile(np.abs(finite), 98)), 1)) if finite.size else 0.5

    view = (BBOX[0], BBOX[1], BBOX[2], BBOX[3])
    pad_x = (view[2] - view[0]) * 0.12
    pad_y = (view[3] - view[1]) * 0.12
    basemap, bm_extent = fetch_basemap(view[0] - pad_x, view[1] - pad_y,
                                       view[2] + pad_x, view[3] + pad_y, ZOOM)

    fig, ax = plt.subplots(figsize=(10, 9), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    im = ax.imshow(eta3857, extent=(w, e, s_, n), origin="upper", cmap="bwr",
                   norm=Normalize(-vlim, vlim), alpha=0.82, zorder=3)
    _overlays(ax, gauge=True)
    _frame(ax, view)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("sea-surface anomaly eta (m)")
    fig.text(
        0.5, 0.02,
        f"geoclaw_amr_refinement_regions  |  RASTER: mid-run sea-surface anomaly eta "
        f"(t={t_s:.0f} s, diverging +-{vlim:.1f} m, same run as amr_regions_mesh.png)  |  "
        f"yellow dashed = USER AMR window, white box = AOI, red dot = gauge  |  "
        f"basemap: Esri World Imagery",
        ha="center", fontsize=6.5, color="0.4",
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.07)
    out = os.path.join(OUT_DIR, "amr_regions.png")
    fig.savefig(out)
    plt.close(fig)
    print("wrote", out, f"| eta frame t={t_s:.0f}s vlim=+-{vlim:.1f} wetcells={finite.size}")


def render_mesh(s3):
    body = s3.get_object(Bucket=os.environ["TRID3NT_RUNS_BUCKET"],
                         Key=f"{RUN_PREFIX}/mesh.geojson")["Body"].read()
    fc = json.loads(body)
    meta = fc.get("metadata", {})

    segs_3857 = []
    for f in fc["features"]:
        for seg in f["geometry"]["coordinates"]:
            (lo0, la0), (lo1, la1) = seg[0], seg[1]
            x0, y0 = TO_3857.transform(lo0, la0)
            x1, y1 = TO_3857.transform(lo1, la1)
            segs_3857.append([(x0, y0), (x1, y1)])

    view = (BBOX[0], BBOX[1], BBOX[2], BBOX[3])
    pad_x = (view[2] - view[0]) * 0.12
    pad_y = (view[3] - view[1]) * 0.12
    basemap, bm_extent = fetch_basemap(view[0] - pad_x, view[1] - pad_y,
                                       view[2] + pad_x, view[3] + pad_y, ZOOM)

    fig, ax = plt.subplots(figsize=(10, 9), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    # THE RAW MESH: one colour (black), thin. Density == refinement.
    ax.add_collection(LineCollection(segs_3857, colors="black", linewidths=0.4,
                                     zorder=3, alpha=0.9))
    _overlays(ax)
    _frame(ax, view)
    n_lines = int(meta.get("total_grid_lines", len(segs_3857)))
    hist = meta.get("level_histogram", {})
    fig.text(
        0.5, 0.045,
        "yellow dashed = your AMR window; the mesh is the solver's response to it",
        ha="center", fontsize=10, color="0.15",
    )
    fig.text(
        0.5, 0.018,
        f"geoclaw_amr_refinement_regions  |  RAW AMR mesh: actual cell-edge grid lines, "
        f"all levels one colour -- density IS the refinement  |  L1-L{meta.get('max_level')} "
        f"patches={meta.get('patch_count')} (per-level {hist})  {n_lines} grid lines, frame "
        f"{meta.get('frame_no')}  |  white box = AOI  |  basemap: Esri World Imagery",
        ha="center", fontsize=6.5, color="0.4",
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.09)
    out = os.path.join(OUT_DIR, "amr_regions_mesh.png")
    fig.savefig(out)
    plt.close(fig)
    print("wrote", out, f"| {len(segs_3857)} grid lines, L1-L{meta.get('max_level')}")


def render_depth(s3):
    key = f"{RUN_PREFIX}/geoclaw_depth_peak.tif"
    tif = os.path.join(TMP, "amr_regions_peak.tif")
    s3.download_file(os.environ["TRID3NT_RUNS_BUCKET"], key, tif)
    with rasterio.open(tif) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        dst = np.full((height, width), np.nan, dtype="float32")
        reproject(source=rasterio.band(src, 1), destination=dst,
                  src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs="EPSG:3857",
                  src_nodata=src.nodata, dst_nodata=np.nan, resampling=Resampling.bilinear)
        w, s_, e, n = rasterio.transform.array_bounds(height, width, transform)
        transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    depth = np.where(dst > 0.01, dst, np.nan)

    view = (BBOX[0], BBOX[1], BBOX[2], BBOX[3])
    pad_x = (view[2] - view[0]) * 0.12
    pad_y = (view[3] - view[1]) * 0.12
    basemap, bm_extent = fetch_basemap(view[0] - pad_x, view[1] - pad_y,
                                       view[2] + pad_x, view[3] + pad_y, ZOOM)

    fig, ax = plt.subplots(figsize=(10, 9), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    vmax = float(np.nanpercentile(depth, 99)) if np.isfinite(depth).any() else 1.0
    im = ax.imshow(depth, extent=(w, e, s_, n), origin="upper", cmap="YlGnBu",
                   norm=Normalize(0, vmax), alpha=0.85, zorder=3)
    _overlays(ax, gauge=True)
    _frame(ax, view)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("peak inundation depth (m)")
    fig.text(
        0.5, 0.02,
        "geoclaw_amr_refinement_regions  |  RASTER: peak inundation depth (same run as "
        "amr_regions_mesh.png)  |  yellow dashed = USER AMR window, white box = AOI, "
        "red dot = gauge  |  basemap: Esri World Imagery",
        ha="center", fontsize=6.5, color="0.4",
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.07)
    out = os.path.join(OUT_DIR, "amr_regions_depth.png")
    fig.savefig(out)
    plt.close(fig)
    print("wrote", out, "| depth vmax=%.3f" % vmax)


def main():
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
    render_mesh(s3)
    render_eta(s3)
    render_depth(s3)


if __name__ == "__main__":
    sys.exit(main())
