#!/usr/bin/env python3
"""Generic ESRI-basemap raster proof renderer, reused across the two 2026-08-11
fidelity-first drives (hecras_flood_2d rain-on-grid @ 20 m solver floor,
landlab_groundwater_water_table @ 10 m 3DEP-native). Reads the published COG
straight from the MinIO run bucket via boto3 (matches the pattern in
scripts/run_l2_malpasset.py -- GDAL /vsis3/ does not see the boto3 MinIO env),
overlays it on ESRI World Imagery in EPSG:3857, adds a pinned scale bar and a
caption with resolution basis + run numbers. ASCII only.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
from pathlib import Path

import boto3
import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as patheffects
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from PIL import Image
from pyproj import Transformer
from rasterio.warp import calculate_default_transform, reproject, Resampling

TILE = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def _tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def _tile_bounds_3857(x, y, z):
    n = 2 ** z
    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO3857.transform(lon, lat)
    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def basemap(w, s, e, n, zoom):
    x0f, y1f = _tile_xy(w, s, zoom)
    x1f, y0f = _tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def download_s3(uri: str) -> str:
    assert uri.startswith("s3://")
    bucket, key = uri[5:].split("/", 1)
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    fd, path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    s3.download_file(bucket, key, path)
    return path


def reproject_to_3857(local_tif: str) -> tuple[np.ndarray, tuple, str]:
    with rasterio.open(local_tif) as src:
        dst_crs = "EPSG:3857"
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        data = np.full((height, width), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=data,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=transform, dst_crs=dst_crs,
            resampling=Resampling.nearest,
            src_nodata=src.nodata, dst_nodata=np.nan,
        )
        x0 = transform.c
        y1 = transform.f
        x1 = x0 + transform.a * width
        y0 = y1 + transform.e * height
        ext = (x0, x1, y0, y1)
        units = (src.tags().get("units") or "")
    return data, ext, units


def add_scale_bar(ax, xlim, m_per_bar=None):
    span_m = xlim[1] - xlim[0]
    if m_per_bar is None:
        for cand in [50, 100, 200, 250, 500, 1000, 2000, 5000]:
            if cand <= span_m * 0.3:
                m_per_bar = cand
    if m_per_bar is None:
        m_per_bar = 100
    x0 = xlim[0] + span_m * 0.04
    y0 = ax.get_ylim()[0] + (ax.get_ylim()[1] - ax.get_ylim()[0]) * 0.05
    ax.plot([x0, x0 + m_per_bar], [y0, y0], color="white", lw=3, solid_capstyle="butt", zorder=10)
    ax.plot([x0, x0 + m_per_bar], [y0, y0], color="black", lw=1, solid_capstyle="butt", zorder=11)
    ax.text(x0 + m_per_bar / 2, y0, f"{m_per_bar} m", color="white", fontsize=7,
            ha="center", va="bottom", zorder=12,
            path_effects=[patheffects.withStroke(linewidth=2, foreground="black")])


def render(uri: str, out_png: str, title: str, caption: str, cmap: str, units_label: str,
           vmin=None, vmax=None, pad=0.15, zoom=15):
    local = download_s3(uri)
    data, ext3857, _ = reproject_to_3857(local)
    x0, x1, y0, y1 = ext3857
    to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lw, ls = to4326.transform(x0, y0)
    le, ln = to4326.transform(x1, y1)
    padx = (le - lw) * pad
    pady = (ln - ls) * pad
    bm, bm_ext = basemap(lw - padx, ls - pady, le + padx, ln + pady, zoom)

    finite = data[np.isfinite(data)]
    if vmin is None:
        vmin = 0.0
    if vmax is None:
        vmax = float(np.nanpercentile(finite, 98)) if finite.size else 1.0

    fig, ax = plt.subplots(figsize=(8, 7), dpi=130)
    ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
    masked = np.ma.masked_invalid(data)
    im = ax.imshow(masked, extent=(x0, x1, y0, y1), origin="upper", cmap=cmap,
                   vmin=vmin, vmax=vmax, alpha=0.82, zorder=3)
    mx0, my0 = TO3857.transform(lw, ls)
    mx1, my1 = TO3857.transform(le, ln)
    xlim = (mx0 - (mx1 - mx0) * pad, mx1 + (mx1 - mx0) * pad)
    ylim = (my0 - (my1 - my0) * pad, my1 + (my1 - my0) * pad)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim)
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cb.set_label(units_label)
    add_scale_bar(ax, xlim)
    ax.set_title(title, fontsize=10)
    fig.text(0.5, 0.015, caption, ha="center", va="bottom", fontsize=7, wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    os.unlink(local)
    return out_png
