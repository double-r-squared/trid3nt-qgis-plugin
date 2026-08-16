#!/usr/bin/env python
"""QGIS-true proof render for the REGISTERED coastal_tidal_surge template.

Renders the A/B pair PRODUCED THROUGH THE REGISTERED TOOL (coastal_tidal_surge,
Apalachicola Bay / CO-OPS 8728690, Hurricane Michael window):
  A = observed storm-surge series, B = astronomical prediction (calm-tide control),
both driven through the SAME coastal domain via the LIQUID BOUNDARIES FILE SL(1).
Filled peak-inundation depth cells (mesh-faithful tripcolor, dry cells masked) +
the mesh wireframe over ESRI World Imagery. Persisted to
docs/proof/templates/coastal_tidal_surge.png (named after the workflow file).

Env: SP = the dir holding A/ and B/ (each with res_coastal.slf +
telemac_metrics.json + manifest.json), staged by scratchpad/coastal_composer_live.py.
Run from repo root with venvs/agent active.
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
import matplotlib.tri as mtri
import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from trid3nt_server.agent.workflows.telemac.postprocess_telemac import read_selafin  # noqa: E402

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


def _load_case(run_dir, bbox, utm_epsg, init_wl):
    mesh = read_selafin(os.path.join(run_dir, "res_coastal.slf"))
    x_local = np.asarray(mesh["x"]); y_local = np.asarray(mesh["y"])
    ikle = np.asarray(mesh["ikle"])

    def _var(key):
        name = next(v for v in mesh["varnames"] if v.strip().upper().startswith(key))
        return np.asarray(mesh["data"][name])
    sfc = _var("FREE SURFACE")
    bot = _var("BOTTOM")[-1]
    peak = np.asarray(sfc).max(axis=0)
    depth = peak - bot
    fwd = Transformer.from_crs(4326, utm_epsg, always_xy=True)
    x0m, y0m = fwd.transform(bbox[0], bbox[1])
    back = Transformer.from_crs(utm_epsg, 4326, always_xy=True)
    lon, lat = back.transform(x_local + x0m, y_local + y0m)
    xm, ym = TO3857.transform(lon, lat)
    tri = mtri.Triangulation(xm, ym, ikle)
    cell_depth = depth[ikle].min(axis=1)
    cell_land = (bot[ikle] > init_wl).all(axis=1)
    tri.set_mask(~((cell_depth > 0.02) & cell_land))
    cell_val = depth[ikle].mean(axis=1)
    return tri, depth, cell_val, xm, ym, ikle


def main():
    sp = os.environ["SP"]
    mA = json.load(open(os.path.join(sp, "A", "telemac_metrics.json")))
    mB = json.load(open(os.path.join(sp, "B", "telemac_metrics.json")))
    bbox = json.load(open(os.path.join(sp, "A", "manifest.json")))["coastal"]["bbox"]
    utm_epsg = int(mA["utm_epsg"])

    img, ext = basemap(bbox[0], bbox[1], bbox[2], bbox[3], zoom=13)
    triA, dA, cvA, xm, ym, ikle = _load_case(os.path.join(sp, "A"), bbox, utm_epsg, mA["init_wl_m"])
    triB, dB, cvB, _, _, _ = _load_case(os.path.join(sp, "B"), bbox, utm_epsg, mB["init_wl_m"])
    vmax = float(np.nanpercentile(np.concatenate(
        [cvA[~triA.mask], cvB[~triB.mask]]), 92)) if (~triA.mask).any() else 2.0

    w3, e3 = float(xm.min()), float(xm.max())
    s3, n3 = float(ym.min()), float(ym.max())

    fig, axes = plt.subplots(1, 2, figsize=(16, 8.5), constrained_layout=True)
    cases = [
        (axes[0], triA, mA,
         f"A  observed Hurricane Michael surge (CO-OPS 8728690)\n"
         f"ocean SL(1) peak {mA['sl_max_m']:.2f} m -> flooded LAND "
         f"{mA['flooded_land_km2']:.2f} km^2 ({mA['n_newly_flooded_nodes']} nodes)"),
        (axes[1], triB, mB,
         f"B  astronomical PREDICTION (same domain, control)\n"
         f"ocean SL(1) peak {mB['sl_max_m']:.2f} m -> flooded LAND "
         f"{mB['flooded_land_km2']:.2f} km^2 ({mB['n_newly_flooded_nodes']} nodes)"),
    ]
    im = None
    for ax, tri, m, title in cases:
        ax.imshow(img, extent=[ext[0], ext[1], ext[2], ext[3]], origin="upper")
        ax.triplot(mtri.Triangulation(xm, ym, ikle), color="white",
                   lw=0.15, alpha=0.35)
        cell_val = cvA if m is mA else cvB
        im = ax.tripcolor(tri, facecolors=cell_val, cmap="YlGnBu",
                          vmin=0.0, vmax=vmax, alpha=0.9, edgecolors="none")
        ax.set_xlim(min(w3, e3), max(w3, e3))
        ax.set_ylim(min(s3, n3), max(s3, n3))
        ax.set_title(title, fontsize=10.5)
        ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=axes, shrink=0.72, location="bottom", pad=0.02)
    cb.set_label("Peak inundation depth over land (m)  -- TELEMAC-2D SAINT-VENANT + TIDAL FLATS")
    ratio = mA["flooded_land_km2"] / max(mB["flooded_land_km2"], 1e-6)
    fig.suptitle(
        "coastal_tidal_surge (REGISTERED tool) -- Apalachicola Bay, FL: TELEMAC-2D coastal inundation "
        "(real NOAA DEM_all topobathy,\nocean boundary forced by the LIQUID BOUNDARIES FILE SL(1) from "
        "a REAL NOAA CO-OPS 8728690 series). "
        f"Observed surge floods {ratio:.0f}x more land than the calm tide "
        f"({mA['flooded_land_km2']:.2f} vs {mB['flooded_land_km2']:.3f} km^2); "
        f"domain {mA['npoin']} nodes @ {mA['dx_m']:.0f} m, ocean edge {mA['ocean_edge']}",
        fontsize=10.5)
    out = os.path.join(os.path.dirname(__file__), "..", "docs", "proof", "templates",
                       "coastal_tidal_surge.png")
    out = os.path.abspath(out)
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print("wrote", out)


if __name__ == "__main__":
    main()
