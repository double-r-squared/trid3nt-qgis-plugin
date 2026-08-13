"""Render the SWAN CAND-S template Hs COGs over the Esri World Imagery basemap --
the docs/proof/templates/ standard. Reads the peak-COG s3 URIs the live smoke
recorded, downloads each from MinIO, and paints the significant-wave-height field
in a cyan->blue ramp over the imagery.

Visual vocabulary: the white rectangle is the AOI boundary ONLY. Open water /
land / nodata cells are left transparent so the Esri imagery reads through
(never painted white). SWAN runs on a REGULAR computational grid (no unstructured
mesh artifact), so no wireframe overlay.
"""
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
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
SCR = ("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
       "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/")
SWEEP_JSON = SCR + "sweep_final.json"
BATCH_JSON = SCR + "swan_sweep_smoke.json"
TMP = os.path.dirname(os.path.abspath(__file__))
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


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


def reproject_to_3857(path):
    with rasterio.open(path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        dst = np.full((height, width), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1), destination=dst,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=transform, dst_crs="EPSG:3857",
            src_nodata=src.nodata, dst_nodata=np.nan, resampling=Resampling.bilinear)
        w, s_, e, n = rasterio.transform.array_bounds(height, width, transform)
        lb = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    return dst, (w, e, s_, n), lb


def render(s3, run_uri, out_name, title, caption):
    # run_uri = s3://trid3nt-runs/<run_id>/swan_wave_height_peak.tif
    assert run_uri.startswith("s3://"), run_uri
    _, _, rest = run_uri.partition("s3://")
    bucket, _, key = rest.partition("/")
    tif = os.path.join(TMP, out_name.replace(".png", ".tif"))
    s3.download_file(bucket, key, tif)

    hs, (w, e, s_, n), (lw, ls, le, ln) = reproject_to_3857(tif)
    hs = np.where(hs > 0.01, hs, np.nan)  # drop calm/nodata -> transparent

    pad_x = (le - lw) * 0.15
    pad_y = (ln - ls) * 0.15
    basemap, bm_extent = fetch_basemap(lw - pad_x, ls - pad_y, le + pad_x, ln + pad_y, 12)

    fig, ax = plt.subplots(figsize=(10, 9), dpi=110)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    vmax = float(np.nanpercentile(hs, 99)) if np.isfinite(hs).any() else 1.0
    im = ax.imshow(hs, extent=(w, e, s_, n), origin="upper",
                   cmap="cool", norm=Normalize(0, vmax), alpha=0.80, zorder=3)
    # AOI boundary = white rectangle ONLY.
    ax0, ay0 = TO_3857.transform(lw, ls)
    ax1, ay1 = TO_3857.transform(le, ln)
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=4))
    wx0, _ = TO_3857.transform(lw - pad_x, ls)
    wx1, _ = TO_3857.transform(le + pad_x, ls)
    _, wy0 = TO_3857.transform(lw, ls - pad_y)
    _, wy1 = TO_3857.transform(lw, ln + pad_y)
    ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title, fontsize=12)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("Significant wave height Hs (m)")
    fig.text(0.01, 0.01, caption, fontsize=7, color="0.35")
    fig.tight_layout()
    out = os.path.join(OUT_DIR, out_name)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out, "| Hs vmax=%.3f" % vmax)


def main():
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])

    # Sweep: render the lowest-friction scheme's Hs field (most wave energy retained).
    sweep = json.load(open(SWEEP_JSON))["schemes"]
    render(s3, sweep[0]["uri"], "swan_physics_sensitivity_sweep.png",
           f"swan_physics_sensitivity_sweep -- SWAN Hs, JONSWAP cfjon={sweep[0]['scheme']} "
           "(Apalachee Bay shelf, FL)",
           "Basemap: Esri World Imagery | white box = AOI | SWAN regular grid, "
           "cyan->blue = significant wave height Hs (m)")

    # Snapshot batch: render the peak snapshot's Hs field.
    snaps = json.load(open(BATCH_JSON))["batch"]["snapshots"]
    peak = max(snaps, key=lambda s: s["max_hs_m"])
    render(s3, peak["uri"], "swan_stationary_snapshot_batch.png",
           f"swan_stationary_snapshot_batch -- SWAN Hs, peak snapshot {peak['label']} "
           f"(offshore Hs={peak['hs_boundary_m']} m, Huntington Beach CA)",
           "Basemap: Esri World Imagery | white box = AOI | SWAN regular grid, "
           "cyan->blue = significant wave height Hs (m)")


if __name__ == "__main__":
    sys.exit(main())
