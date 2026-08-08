#!/usr/bin/env python
"""ADR 0190 row 1 proof renders: TELEMAC distributed rainfall vs no-rain.

Two figures into docs/proof/templates/, named after the workflow file
(telemac_river_dye):
  * telemac_river_dye_rainfall_diffmap.png -- final-frame WATER DEPTH change
    (rain minus no-rain), scattered mesh nodes filled + mesh wireframe over
    ESRI World Imagery; white box = AOI.
  * telemac_river_dye_rainfall_timing_chart.png -- domain-mean wet depth vs
    time, with-rain vs without (the timing divergence), plugin render_spec
    Figure(6.0,2.2) dpi=100 savefig dpi=200.

Env (MinIO): set -a; source .env.local; set +a
"""
from __future__ import annotations

import io
import json
import math
import os

import boto3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from matplotlib.tri import Triangulation
from PIL import Image
from pyproj import Transformer

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
ARR = os.path.join(OUT, "telemac_river_dye_rain_forcing_arrays.npz")
SUMMARY = os.path.join(OUT, "telemac_river_dye_rain_forcing_summary.json")
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


def _utm_epsg(run_id):
    s3 = boto3.client("s3")
    m = json.loads(s3.get_object(Bucket=os.environ["TRID3NT_RUNS_BUCKET"],
                                 Key=f"{run_id}/telemac_metrics.json")["Body"].read())
    return int(m.get("utm_epsg") or 32611)


def main():
    data = np.load(ARR)
    summ = json.loads(open(SUMMARY).read())
    x, y = data["x"], data["y"]
    ddiff = data["ddiff"] * 1000.0  # m -> mm
    epsg = int(data["utm_epsg"]) if "utm_epsg" in data else _utm_epsg(summ["base_run_id"])
    rate = summ["rain_mm_per_day"]
    dur_h = summ["duration_s"] / 3600.0

    # UTM mesh nodes -> lon/lat -> 3857
    to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(x, y)
    mx, my = TO3857.transform(lon, lat)
    # Triangulate on the REAL SELAFIN element connectivity (IKLE), NOT an
    # unconstrained Delaunay of the node cloud. A Delaunay of a sinuous channel's
    # nodes bridges every river bend with long triangles across the convex hull,
    # painting a chaotic fan OUTSIDE the actual water body (the pre-fix defect).
    # The true elements follow the meshed banks, so the fill + wireframe trace
    # the real river and never leave the channel.
    ikle = data["ikle"] if "ikle" in data else None
    tri = (Triangulation(mx, my, triangles=ikle) if ikle is not None
           else Triangulation(mx, my))

    lw, le, ls, ln = lon.min(), lon.max(), lat.min(), lat.max()
    pad_x = (le - lw) * 0.35 + 1e-4
    pad_y = (ln - ls) * 0.35 + 1e-4
    basemap, bm_ext = _basemap(lw - pad_x, ls - pad_y, le + pad_x, ln + pad_y, 14)

    # ---- (1) difference map -------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 8), dpi=110)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    vmax = max(float(np.nanpercentile(ddiff, 99)), 1.0)
    tcf = ax.tricontourf(tri, ddiff, levels=np.linspace(0, vmax, 12),
                         cmap="YlGnBu", alpha=0.82, zorder=3)
    ax.triplot(tri, color="white", linewidth=0.15, alpha=0.35, zorder=4)  # mesh wireframe
    ax0, ay0 = TO3857.transform(lw, ls)
    ax1, ay1 = TO3857.transform(le, ln)
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=5))
    wx0, _ = TO3857.transform(lw - pad_x, ls)
    wx1, _ = TO3857.transform(le + pad_x, ls)
    _, wy0 = TO3857.transform(lw, ls - pad_y)
    _, wy1 = TO3857.transform(lw, ln + pad_y)
    ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(f"TELEMAC-2D distributed rainfall: water-depth rise (rain minus no-rain)\n"
                 f"{rate:.0f} mm/day over {dur_h:.1f} h, same inflow hydrograph",
                 fontsize=11)
    cb = fig.colorbar(tcf, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("Water-depth increase (mm)")
    fig.text(0.01, 0.01,
             f"Snake River reach nr Twin Falls ID (UTM {epsg}); mesh wireframe white. "
             f"Final frame t={summ['duration_s']:.0f}s. Mean rise "
             f"{summ['final_depth_diff_mean_m']*1000:.1f} mm, max "
             f"{summ['final_depth_diff_max_m']*1000:.1f} mm. Fixed-stage outflow "
             f"drains most rain volume (steady rise concentrates upstream). "
             f"telemac_river_dye rain_or_evap_mm_per_day knob (ADR 0190 row 1).",
             fontsize=7, color="0.35", wrap=True)
    fig.tight_layout()
    p1 = os.path.join(OUT, "telemac_river_dye_rainfall_diffmap.png")
    fig.savefig(p1, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p1, "vmax_mm=%.2f" % vmax)

    # ---- (2) timing chart (plugin render_spec Figure(6.0,2.2)) --------------
    tr = data["times_rain"] / 60.0
    mdb, mdr = data["mean_depth_base"], data["mean_depth_rain"]
    n = min(mdb.size, mdr.size, tr.size)
    delta_mm = (mdr[:n] - mdb[:n]) * 1000.0  # rain-induced mean-depth rise (mm)
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    ax.plot(tr[:n], delta_mm, color="#1a9850", lw=1.8)
    ax.fill_between(tr[:n], 0, delta_mm, color="#1a9850", alpha=0.15)
    ax.set_xlabel("time (min)", fontsize=8)
    ax.set_ylabel("mean-depth rise\nfrom rain (mm)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=0.25)
    ax.margins(x=0.01)
    ax.set_ylim(bottom=0)
    ax.set_title(f"{rate:.0f} mm/day distributed rain, same inflow hydrograph",
                 fontsize=8)
    fig.text(0.5, 0.005,
             "telemac_river_dye distributed rainfall forcing: rain-driven mean-depth "
             "rise accumulates over the inflow-only run (ADR 0190 row 1)",
             ha="center", fontsize=6, color="0.4")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    p2 = os.path.join(OUT, "telemac_river_dye_rainfall_timing_chart.png")
    fig.savefig(p2, dpi=200)
    plt.close(fig)
    print("wrote", p2)


if __name__ == "__main__":
    main()
