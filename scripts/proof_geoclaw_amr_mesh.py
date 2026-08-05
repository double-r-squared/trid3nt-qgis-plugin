"""Render the GeoClaw amr_regions proofs from ONE live re-smoke run:

  * amr_regions_mesh.png -- the RAW UNIFIED MESH (Clawpack-gallery style): the
    actual AMR cell-edge grid lines from the emitted mesh.geojson, ONE colour
    (black, thin). Refinement is self-evident because the grid gets DENSER where
    the solver refined -- NO per-level colour/weight coding, no abstraction. The
    ONLY overlay is the yellow dashed user AMR window (the residual of the user's
    edit) + the white AOI box, over Esri World Imagery.
  * amr_regions.png -- the peak-inundation depth RASTER from the SAME run, same
    visual family (Esri basemap, white AOI box, yellow window, caption strip).

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

# The live re-smoke run (agent-side postprocess run_id carrying both products).
RUN_PREFIX = "01KZ9P4FG93F0J7TQQSYPHGBA4"
BBOX = (-124.24, 41.73, -124.16, 41.78)  # AOI (white box)
WINDOW = (-124.21, 41.745, -124.18, 41.770)  # user AMR window (yellow dashed)
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


def _overlays(ax):
    # White AOI box.
    ax0, ay0, ax1, ay1 = _box_3857(*(BBOX[0], BBOX[1], BBOX[2], BBOX[3]))
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=5))
    # Yellow dashed user AMR window.
    wx0, wy0, wx1, wy1 = _box_3857(*(WINDOW[0], WINDOW[1], WINDOW[2], WINDOW[3]))
    ax.add_patch(Rectangle((wx0, wy0), wx1 - wx0, wy1 - wy0, fill=False,
                           edgecolor="#ffd000", linewidth=2.2, linestyle=(0, (6, 4)),
                           zorder=6))


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
    _overlays(ax)
    _frame(ax, view)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("peak inundation depth (m)")
    fig.text(
        0.5, 0.02,
        "geoclaw_amr_refinement_regions  |  RASTER: peak inundation depth (same run as "
        "amr_regions_mesh.png)  |  yellow dashed = USER AMR window, white box = AOI  |  "
        "basemap: Esri World Imagery",
        ha="center", fontsize=6.5, color="0.4",
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.07)
    out = os.path.join(OUT_DIR, "amr_regions.png")
    fig.savefig(out)
    plt.close(fig)
    print("wrote", out, "| depth vmax=%.3f" % vmax)


def main():
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
    render_mesh(s3)
    render_depth(s3)


if __name__ == "__main__":
    sys.exit(main())
