#!/usr/bin/env python3
"""ESRI-basemap proof render for the REAL Marquette breakwater ARTEMIS pair.

Reads the two single-frame agit_field.slf the sandbox produced (present /
removed), georeferences the LOCAL-frame mesh with the SAME offset-reconstruction
the latent-#7 fix uses (add the AOI SW-corner UTM origin, then UTM->4326), maps
Kd = Hs/H0, rasterizes onto an EPSG:3857 grid and overlays it on ESRI World
Imagery with the surveyed breakwater geometry (OSM) drawn on top. Emits a
side-by-side PNG (structure present vs removed) proving the sheltering the real
breakwater provides its real marina. ASCII only.
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
import matplotlib.patheffects as pe
import numpy as np
import requests
from PIL import Image
from pyproj import Transformer
from scipy.interpolate import griddata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "src"))
from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin  # noqa: E402

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
            r = sess.get(TILE.format(z=zoom, y=ty, x=tx),
                         headers={"User-Agent": "trid3nt-proof"}, timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"),
                         (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _georef_local_mesh(slf_path, bbox, utm_epsg):
    """Read local-frame Hs mesh -> true (lon, lat) via the latent-#7 offset fix."""
    mesh = read_selafin(slf_path)
    hs_var = next(v for v in mesh["varnames"]
                  if "WAVE HEIGHT" in v.strip().upper())
    hs = np.asarray(mesh["data"][hs_var])[-1]
    x_local = np.asarray(mesh["x"])
    y_local = np.asarray(mesh["y"])
    fwd = Transformer.from_crs(4326, utm_epsg, always_xy=True)
    x0m, y0m = fwd.transform(bbox[0], bbox[1])          # AOI SW corner
    back = Transformer.from_crs(utm_epsg, 4326, always_xy=True)
    lon, lat = back.transform(x_local + x0m, y_local + y0m)
    return np.asarray(lon), np.asarray(lat), hs


def _kd_grid_3857(lon, lat, kd, extent3857, nx=600):
    xm, ym = TO3857.transform(lon, lat)
    w, e, s, n = extent3857
    gx = np.linspace(w, e, nx)
    gy = np.linspace(s, n, int(nx * (n - s) / (e - w)))
    GX, GY = np.meshgrid(gx, gy)
    grid = griddata((xm, ym), kd, (GX, GY), method="linear")
    return grid


def _draw(ax, img, ext3857, grid, vmax, bw_polylines, aoi, title):
    ax.imshow(img, extent=[ext3857[0], ext3857[1], ext3857[2], ext3857[3]],
              origin="upper")
    im = ax.imshow(grid, extent=[ext3857[0], ext3857[1], ext3857[2], ext3857[3]],
                   origin="lower", cmap="viridis", vmin=0.0, vmax=vmax, alpha=0.68)
    for pl in bw_polylines:
        xs, ys = TO3857.transform([p[0] for p in pl], [p[1] for p in pl])
        ax.plot(xs, ys, "-", color="red", lw=1.6,
                path_effects=[pe.withStroke(linewidth=3.0, foreground="white")])
    w, s, e, n = aoi
    xw, ys = TO3857.transform(w, s)
    xe, yn = TO3857.transform(e, n)
    ax.set_xlim(xw, xe)
    ax.set_ylim(ys, yn)
    ax.set_title(title, fontsize=11)
    ax.set_xticks([]); ax.set_yticks([])
    return im


def main():
    sp = os.environ["SP"]
    run = os.path.join(sp, "real_bw_run")
    raw = json.load(open(os.path.join(sp, "manifest_raw.json")))
    pm = json.load(open(os.path.join(run, "pair_metrics.json")))
    bbox = raw["bbox"]
    utm_epsg = int(pm["present"]["utm_epsg"])
    H0 = 2.0
    bw = raw["breakwater_polylines"]

    aoi = (bbox[0], bbox[1], bbox[2], bbox[3])
    img, ext3857 = basemap(bbox[0], bbox[1], bbox[2], bbox[3], zoom=14)

    cases = {}
    for label in ("present", "removed"):
        lon, lat, hs = _georef_local_mesh(
            os.path.join(run, label, "agit_field.slf"), bbox, utm_epsg)
        cases[label] = (lon, lat, hs / H0)
        print(f"{label}: lon[{lon.min():.4f},{lon.max():.4f}] "
              f"lat[{lat.min():.4f},{lat.max():.4f}] kd_max={ (hs/H0).max():.2f}")
        assert bbox[0] - 0.01 <= lon.min() and lon.max() <= bbox[2] + 0.01, \
            "georef escaped AOI -- latent #7 not fixed"

    allkd = np.concatenate([cases[l][2] for l in cases])
    vmax = float(np.nanpercentile(allkd, 98))

    fig, axes = plt.subplots(1, 2, figsize=(15, 8), constrained_layout=True)
    titles = {
        "present": (f"Breakwater PRESENT (as surveyed, OSM {len(bw)} ways)\n"
                    f"marina lee Kd={pm['present']['kd_sheltered']:.3f}"),
        "removed": (f"Breakwater REMOVED (proof-norm-#9 control)\n"
                    f"same lee Kd={pm['removed']['kd_sheltered']:.3f}"),
    }
    im = None
    for ax, label in zip(axes, ("present", "removed")):
        lon, lat, kd = cases[label]
        grid = _kd_grid_3857(lon, lat, kd, ext3857)
        im = _draw(ax, img, ext3857, grid, vmax, bw, aoi, titles[label])
    cb = fig.colorbar(im, ax=axes, shrink=0.7, location="bottom", pad=0.02)
    cb.set_label("Agitation coefficient Kd = Hs / H0  (phase-resolving ARTEMIS)")
    shel_p = pm["present"]["kd_sheltered"]; shel_r = pm["removed"]["kd_sheltered"]
    red = 100.0 * (shel_r - shel_p) / shel_r if shel_r else 0.0
    fig.suptitle(
        "Marquette Lower Harbor (Cinder Pond Marina), Lake Superior -- REAL surveyed "
        "breakwater, real NOAA lake bathymetry\n"
        f"labeled incident swell Hs={H0:.1f} m T=8 s from the open lake "
        f"(dir {raw['wave_dir_deg']:.0f} deg trig, rubble-mound RP=0.5); "
        f"the real breakwater cuts marina-lee agitation {red:.0f}% "
        f"({shel_r:.3f} -> {shel_p:.3f})",
        fontsize=11)
    out = os.path.join(run, "artemis_real_breakwater_pair.png")
    fig.savefig(out, dpi=115, bbox_inches="tight")
    print("wrote", out)


if __name__ == "__main__":
    main()
