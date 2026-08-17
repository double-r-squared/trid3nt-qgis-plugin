#!/usr/bin/env python3
"""Proofs for the HEC-RAS 2025 rain-on-grid landing.

(1) hecras_flood_2d_rog_depth.png  -- max water depth over the Coweeta catchment on
    ESRI World Imagery (EPSG:3857 tiles AND data), catchment boundary overlaid, mesh
    footprint. (2) hecras_flood_2d_rog_compare_chart.png -- dock-exact 6.0x2.2 dpi200
    outlet hydrograph HEC-RAS 2025 vs the TELEMAC-2D reference peak, quantitative axes,
    a caption strip, NO annotation boxes over the plot.

Reads the pipeline run JSON (metrics + prep + hydrograph) + the result HDF. ASCII only.
"""
from __future__ import annotations

import io
import json
import math
import sys
from pathlib import Path

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.patches import Rectangle
from PIL import Image
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

OUT = Path("/home/nate/Documents/trid3nt-local/docs/proof/templates")
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


def _basemap(w, s, e, n, zoom):
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


def depth_map(run_json, result_h5, catchment_geojson):
    d = json.loads(Path(run_json).read_text())
    prep = d["prep"]; m = d["metrics"]
    epsg = prep["utm_epsg"]; ox, oy = prep["origin_x"], prep["origin_y"]
    to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

    base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
    with h5py.File(result_h5, "r") as f:
        depth = f[f"{base}/Cell Depth"][:]
        cxy = f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
    dmax = depth.max(axis=0)                                  # per-cell max depth
    ux = ox + cxy[:, 0]; uy = oy + cxy[:, 1]
    lon, lat = to_ll.transform(ux, uy)
    mx, my = TO3857.transform(lon, lat)

    # catchment boundary (4326 -> 3857) + per-cell in-catchment mask (restrict the
    # depth field to the delineated catchment, matching the metrics).
    g = json.loads(Path(catchment_geojson).read_text())
    geom = shape((g["features"][0] if "features" in g else g)["geometry"])
    geom3857 = shp_transform(lambda X, Y, z=None: TO3857.transform(X, Y), geom)
    from shapely.prepared import prep as _prep
    from shapely.geometry import Point as _P
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    geom_utm = shp_transform(lambda X, Y, z=None: to_utm.transform(X, Y), geom)
    pgm = _prep(geom_utm)
    incatch = np.array([pgm.contains(_P(x, y)) for x, y in zip(ux, uy)])

    lw, le, ls, ln = lon.min(), lon.max(), lat.min(), lat.max()
    pad = 0.12
    padx = (le - lw) * pad; pady = (ln - ls) * pad
    basemap, bm_ext = _basemap(lw - padx, ls - pady, le + padx, ln + pady, 13)

    # per-cell max depth, catchment cells only
    keep = (dmax > 0.02) & incatch
    fig, ax = plt.subplots(figsize=(8, 7), dpi=120)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    vmax = max(float(np.nanpercentile(dmax[keep], 98)) if keep.any() else 1.0, 0.2)
    sc = ax.scatter(mx[keep], my[keep], c=dmax[keep], s=6, marker="s",
                    cmap="YlGnBu", vmin=0, vmax=vmax, alpha=0.85, linewidths=0, zorder=3)
    # catchment boundary
    if geom3857.geom_type == "Polygon":
        xs, ys = geom3857.exterior.xy
        ax.plot(xs, ys, color="#ff3b30", lw=1.8, zorder=5)
    else:
        for poly in geom3857.geoms:
            xs, ys = poly.exterior.xy
            ax.plot(xs, ys, color="#ff3b30", lw=1.8, zorder=5)
    mx0, my0 = TO3857.transform(lw, ls); mx1, my1 = TO3857.transform(le, ln)
    ax.set_xlim(mx0 - (mx1 - mx0) * pad, mx1 + (mx1 - mx0) * pad)
    ax.set_ylim(my0 - (my1 - my0) * pad, my1 + (my1 - my0) * pad)
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(sc, ax=ax, fraction=0.035, pad=0.02)
    cb.set_label("max water depth (m)")
    ax.set_title("HEC-RAS 2025 rain-on-grid: max depth, Coweeta Creek NC\n"
                 f"25 mm/hr x 6 h, {m['n_catchment_cells']} catchment cells "
                 f"({prep['cell_size']:.0f} m); red = delineated catchment", fontsize=10)
    p = OUT / "hecras_flood_2d_rog_depth.png"
    fig.tight_layout(); fig.savefig(p, dpi=150); plt.close(fig)
    return str(p)


def compare_chart(run_json, telemac_ref):
    d = json.loads(Path(run_json).read_text())
    m = d["metrics"]
    t = np.array(m["hydrograph_t_hr"]); q = np.array(m["hydrograph_q_m3s"])
    q = q.copy(); q[:1] = 0.0                                 # drop the t=0 gradient spike

    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    fig.subplots_adjust(left=0.11, right=0.985, top=0.9, bottom=0.42)
    ax.plot(t, q, color="#1f5fbf", lw=1.6, label="HEC-RAS 2025 (DWE, rain-only)")
    ax.axhline(telemac_ref["peak_outlet_q_m3s"], color="#e07b00", lw=1.3, ls="--",
               label=f"TELEMAC-2D peak {telemac_ref['peak_outlet_q_m3s']:.1f} m3/s (AMC II)")
    ax.scatter([m["peak_time_hr"]], [m["peak_outlet_q_m3s"]], color="#1f5fbf", s=18, zorder=5)
    ax.set_xlabel("time (h)", fontsize=8)
    ax.set_ylabel("outlet Q (m3/s)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.2, loc="center left", framealpha=0.85)
    ax.margins(x=0.02)
    ax.grid(True, lw=0.3, alpha=0.4)
    cap = (f"Coweeta Creek NC (28.9 km2), 25 mm/hr x 6 h. HEC-RAS 2025 peak "
           f"{m['peak_outlet_q_m3s']:.0f} m3/s @ {m['peak_time_hr']:.1f} h, runoff coeff "
           f"{m['runoff_coeff']:.2f}, max depth {m['max_depth_m']:.1f} m.\n"
           f"HEC-RAS is RAIN-ONLY (2025 beta has no infiltration layer); TELEMAC is "
           f"AMC II with SCS-CN loss -- the ~4x gap is the infiltration difference.")
    fig.text(0.5, 0.13, cap, ha="center", va="top", fontsize=5.6)
    p = OUT / "hecras_flood_2d_rog_compare_chart.png"
    fig.savefig(p, dpi=200); plt.close(fig)
    return str(p)


def main():
    run_json = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rog2025_coweeta_dwe.json"
    result_h5 = sys.argv[2] if len(sys.argv) > 2 else None
    catchment = "/tmp/rog_coweeta/catchment.geojson"
    if result_h5 is None:
        result_h5 = json.loads(Path(run_json).read_text())["result_h5"]
    telemac_ref = {"engine": "TELEMAC-2D", "peak_outlet_q_m3s": 45.5,
                   "runoff_volume_1e3_m3": 162.0, "max_depth_m": 6.95, "wall_s": 64.0}
    p1 = depth_map(run_json, result_h5, catchment)
    p2 = compare_chart(run_json, telemac_ref)
    print(p1); print(p2)


if __name__ == "__main__":
    raise SystemExit(main())
