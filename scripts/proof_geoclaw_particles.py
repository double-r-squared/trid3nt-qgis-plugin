"""Proof render for the GeoClaw Lagrangian particle-track fold.

Two artifacts to docs/proof/templates/:
  - geoclaw_lagrangian_particles.png: the peak-depth raster + the particle DRIFT
    TRACKS (LineStrings, one colour per drifter, start dot) over Esri World
    Imagery, white box = AOI. The tracks ARE a product layer (the wake path).
  - geoclaw_lagrangian_particles_chart.png: the cumulative-drift-vs-time chart,
    rendered through the plugin chart-dock's OWN render_spec interpreter.

Auto-discovers the most recent MinIO run carrying a particles.geojson (+ the
sibling geoclaw_depth_peak.tif under the same run_id).

Run (from repo root):
  set -a; source .env.local; set +a
  PYTHONPATH=.:contracts venvs/agent/bin/python scripts/proof_geoclaw_particles.py
"""

from __future__ import annotations

import importlib.util
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
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from pyproj import Transformer
from rasterio.warp import Resampling, calculate_default_transform, reproject

REPO = "/home/nate/Documents/trid3nt-local"
OUT_DIR = REPO + "/docs/proof/templates"
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
BBOX = (-124.24, 41.73, -124.16, 41.78)
ZOOM = 13
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
# distinct colours per drifter track (colour-blind-safe qualitative).
TRACK_COLORS = ["#f5c518", "#20b2aa", "#ff6f3c"]

bucket = os.environ["TRID3NT_RUNS_BUCKET"]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)


def _tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def _tile_bounds_3857(x, y, z):
    n = 2 ** z

    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)

    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def fetch_basemap(w, s, e, n, zoom):
    from PIL import Image
    x0f, y1f = _tile_xy(w, s, zoom)
    x1f, y0f = _tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _discover_run() -> str:
    """Most recent run_id carrying particles.geojson."""
    pag = s3.get_paginator("list_objects_v2")
    hits = []
    for page in pag.paginate(Bucket=bucket):
        for o in page.get("Contents", []) or []:
            if o["Key"].endswith("/particles.geojson"):
                hits.append((o["LastModified"], o["Key"].split("/", 1)[0]))
    if not hits:
        raise SystemExit("no particles.geojson found in the runs bucket")
    hits.sort()
    return hits[-1][1]


def _read_depth_3857(run_id: str):
    """Download geoclaw_depth_peak.tif, warp to EPSG:3857, return (arr, extent)."""
    key = f"{run_id}/geoclaw_depth_peak.tif"
    local = f"/tmp/claude-1000/geoclaw_depth_{run_id}.tif"
    s3.download_file(bucket, key, local)
    with rasterio.open(local) as src:
        dst_crs = "EPSG:3857"
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        dst = np.full((height, width), np.nan, dtype="float32")
        reproject(source=rasterio.band(src, 1), destination=dst,
                  src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs=dst_crs,
                  resampling=Resampling.bilinear, dst_nodata=np.nan)
        w = transform.c
        n = transform.f
        e = transform.c + transform.a * width
        s_ = transform.f + transform.e * height
    return dst, (w, e, s_, n)


def _box_3857(w, s, e, n):
    x0, y0 = TO_3857.transform(w, s)
    x1, y1 = TO_3857.transform(e, n)
    return x0, y0, x1, y1


def render_map(run_id: str, fc: dict):
    depth, (dw, de, ds, dn) = _read_depth_3857(run_id)
    finite = depth[np.isfinite(depth)]
    vmax = float(np.nanpercentile(finite, 98)) if finite.size else 1.0
    view = BBOX
    pad_x = (view[2] - view[0]) * 0.12
    pad_y = (view[3] - view[1]) * 0.12
    basemap, bm_extent = fetch_basemap(view[0] - pad_x, view[1] - pad_y,
                                       view[2] + pad_x, view[3] + pad_y, ZOOM)
    fig, ax = plt.subplots(figsize=(10, 9), dpi=120)
    ax.imshow(basemap, extent=bm_extent, origin="upper")
    im = ax.imshow(depth, extent=(dw, de, ds, dn), origin="upper", cmap="Blues",
                   norm=Normalize(0.0, vmax), alpha=0.72, zorder=3)
    # particle tracks.
    for i, f in enumerate(fc["features"]):
        coords = f["geometry"]["coordinates"]
        xs = [TO_3857.transform(c[0], c[1])[0] for c in coords]
        ys = [TO_3857.transform(c[0], c[1])[1] for c in coords]
        col = TRACK_COLORS[i % len(TRACK_COLORS)]
        p = f["properties"]
        ax.plot(xs, ys, "-", color=col, linewidth=2.4, zorder=6,
                label=f"drifter {p['gauge_id']}: {p['track_length_m']:.0f} m")
        ax.plot([xs[0]], [ys[0]], "o", color=col, markersize=8,
                markeredgecolor="white", markeredgewidth=1.2, zorder=7)
        ax.plot([xs[-1]], [ys[-1]], "^", color=col, markersize=9,
                markeredgecolor="white", markeredgewidth=1.0, zorder=7)
    # white AOI box.
    ax0, ay0, ax1, ay1 = _box_3857(*BBOX)
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=5))
    wx0, _ = TO_3857.transform(view[0] - pad_x, view[1])
    wx1, _ = TO_3857.transform(view[2] + pad_x, view[1])
    _, wy0 = TO_3857.transform(view[0], view[1] - pad_y)
    _, wy1 = TO_3857.transform(view[0], view[3] + pad_y)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(loc="upper right", fontsize=8, framealpha=0.85)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02, extend="max")
    cb.set_label("peak overland depth (m)")
    fig.text(0.5, 0.02,
             "geoclaw_inundation lagrangian_particles  |  RASTER: peak overland "
             "depth (0..p98)  |  LINES: Lagrangian drift tracks (o=seed, ^=end)  |  "
             "white box = AOI  |  basemap: Esri World Imagery",
             ha="center", fontsize=6.5, color="0.4")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.06)
    out = os.path.join(OUT_DIR, "geoclaw_lagrangian_particles.png")
    fig.savefig(out)
    plt.close(fig)
    print("wrote", out)


def _load_charts():
    spec = importlib.util.spec_from_file_location(
        "trid3nt_charts", REPO + "/plugin/ui/charts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if getattr(mod, "Figure", None) is None:
        mod.Figure = Figure
        mod._MATPLOTLIB_ERROR = None
    return mod


def render_chart(fc: dict):
    # rebuild the tracks list shape build_particle_track_chart_spec expects.
    from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
        build_particle_track_chart_spec,
    )
    tracks = []
    for f in fc["features"]:
        coords = f["geometry"]["coordinates"]
        p = f["properties"]
        n = len(coords)
        dur = float(p["duration_s"])
        # reconstruct evenly-spaced times across the recorded window (the geojson
        # dropped per-vertex t; the chart's x-axis is time, evenly sampled here).
        t = [dur * k / (n - 1) for k in range(n)] if n > 1 else [0.0]
        tracks.append({"gauge_id": int(p["gauge_id"]), "coords": coords, "t": t,
                       "length_m": float(p["track_length_m"]), "duration_s": dur})
    spec = build_particle_track_chart_spec(tracks)
    charts = _load_charts()
    fig = Figure(figsize=(6.0, 2.2), dpi=100)
    summary = charts.render_spec(fig, spec)
    out = os.path.join(OUT_DIR, "geoclaw_lagrangian_particles_chart.png")
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print("wrote", out, "| render summary:", summary)


def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else _discover_run()
    print("run_id:", run_id)
    fc = json.loads(s3.get_object(Bucket=bucket, Key=f"{run_id}/particles.geojson")["Body"].read())
    print("tracks:", len(fc["features"]))
    render_map(run_id, fc)
    render_chart(fc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
