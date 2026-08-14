"""Proof for elmfire_crown_fire_active_ros_verification (ADR 0256): the Cruz
(2005) active crown-fire ROS exact-solution gate.

Runs the live in-image verification, downloads the PUBLISHED time-of-arrival COG,
and renders it as FILLED cells (ADR 0251: never cell-center scatter) over the
Esri World Imagery basemap with the grid overlay, plus a numerical-vs-Cruz ROS
panel. The verification deck is a SYNTHETIC all-constant canopied deck at a
neutral mid-CONUS point (the geography is immaterial on constant fuel); the
basemap is shown for the standard QGIS-true framing.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
from pathlib import Path

os.environ.setdefault("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from matplotlib.colors import Normalize
from PIL import Image
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

REPO = str(Path(__file__).parent.parent)
OUT = REPO + "/docs/proof/templates"
Path(OUT).mkdir(parents=True, exist_ok=True)
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
TO_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


def _tile_xy(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _tile_bounds_3857(x, y, z):
    n = 2 ** z
    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)
    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def _fetch_basemap(w, s, e, n, zoom):
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
    wm0, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


async def _run():
    from trid3nt_server.agent.workflows.elmfire.verification.crown_ros import (
        model_elmfire_crown_ros_verification,
    )
    return await model_elmfire_crown_ros_verification(
        wind_speed_mph=20.0, duration_hours=0.4, cellsize_m=30.0,
        domain_km=12.0, fuel_moisture="dry", compute_class="small",
    )


res = asyncio.run(_run())
print(f"numerical={res.numerical_ros_m_min:.2f} cruz={res.cruz_ros_m_min:.2f} "
      f"rel_err={res.rel_error*100:.2f}% passed={res.passed} uri={res.uri}")

from trid3nt_server.agent.tools.cache import read_object_bytes_s3  # noqa: E402

with MemoryFile(read_object_bytes_s3(res.uri)) as mf, mf.open() as src:
    arr = src.read(1).astype("float64")
    if src.nodata is not None:
        arr[arr == src.nodata] = np.nan
    src_crs = src.crs
    dtrans, dw, dh = calculate_default_transform(src_crs, "EPSG:3857", src.width, src.height, *src.bounds)
    dst = np.full((dh, dw), np.nan, dtype="float32")
    reproject(source=arr.astype("float32"), destination=dst, src_transform=src.transform,
              src_crs=src_crs, dst_transform=dtrans, dst_crs="EPSG:3857",
              src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.nearest)
    w, s_, e, n = rasterio.transform.array_bounds(dh, dw, dtrans)
    lb = transform_bounds(src_crs, "EPSG:4326", *src.bounds)

ny, nx = dst.shape
ys_i, xs_i = np.where(np.isfinite(dst))
xr = np.linspace(w, e, nx); yr = np.linspace(n, s_, ny)
fx0, fx1 = xr[xs_i.min()], xr[xs_i.max()]
fy0, fy1 = yr[ys_i.max()], yr[ys_i.min()]
mx = max((fx1 - fx0), (fy1 - fy0)) * 0.75 + 300.0
cx = (fx0 + fx1) / 2.0; cy = (fy0 + fy1) / 2.0
win_w, win_s = TO_4326.transform(cx - mx, cy - mx)
win_e, win_n = TO_4326.transform(cx + mx, cy + mx)
basemap, bm_ext = _fetch_basemap(win_w, win_s, win_e, win_n, 12)

fig, (ax, axb) = plt.subplots(1, 2, figsize=(13.5, 7.2), dpi=115,
                              gridspec_kw={"width_ratios": [2.2, 1.0]})
ax.imshow(basemap, extent=bm_ext, origin="upper")
vmax = float(np.nanpercentile(dst, 99)) if np.isfinite(dst).any() else 1.0
im = ax.imshow(dst, extent=(w, e, s_, n), origin="upper", cmap="inferno",
               norm=Normalize(0, vmax), alpha=0.9, zorder=3)
# grid overlay (computational cell lines, coarsened for legibility)
gx = np.linspace(w, e, min(nx, 40)); gy = np.linspace(s_, n, min(ny, 40))
for gxx in gx:
    ax.plot([gxx, gxx], [s_, n], color="white", lw=0.25, alpha=0.35, zorder=4)
for gyy in gy:
    ax.plot([w, e], [gyy, gyy], color="white", lw=0.25, alpha=0.35, zorder=4)
ax.set_xlim(cx - mx, cx + mx); ax.set_ylim(cy - mx, cy + mx)
ax.set_xticks([]); ax.set_yticks([])
ax.set_title("Active crown-fire time of arrival (filled cells, grid overlay)", fontsize=11)
cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
cb.set_label("time of arrival (hours from ignition)")

# numerical-vs-Cruz ROS panel
labels = ["numerical\n(level-set head)", "Cruz 2005\n(closed form)"]
vals = [res.numerical_ros_m_min, res.cruz_ros_m_min]
bars = axb.bar(labels, vals, color=["#1f77b4", "#d62728"])
axb.set_ylabel("active crown-fire ROS (m/min)")
axb.set_title(f"ROS verification: rel. error {res.rel_error*100:.2f}% "
              f"(tol {res.tolerance*100:.0f}%) -> {'PASS' if res.passed else 'FAIL'}",
              fontsize=10)
for b, v in zip(bars, vals):
    axb.text(b.get_x() + b.get_width()/2, v + 1, f"{v:.1f}", ha="center", fontsize=10)
axb.set_ylim(0, max(vals) * 1.18)

cap = (f"ELMFIRE active crown-fire ROS vs the Cruz et al. (2005) closed form "
       f"R=11.02*U10^0.90*CBD^0.19*exp(-0.17*EFFM) [m/min]. Uncapped active-crown "
       f"deck (SH7 fuel, cbd 0.18 kg/m3, EFFM {res.effm_pct:.0f}%, wind "
       f"{res.wind_speed_mph:.0f} mph@20ft). Synthetic constant deck (mid-CONUS "
       f"point; basemap immaterial). Numerical {res.numerical_ros_m_min:.1f} vs "
       f"Cruz {res.cruz_ros_m_min:.1f} m/min = {res.rel_error*100:.2f}% error.")
fig.text(0.5, 0.02, cap, ha="center", fontsize=8.3, wrap=True)
fig.subplots_adjust(bottom=0.15, top=0.94, wspace=0.15)
outp = f"{OUT}/elmfire_crown_fire_active_ros_verification.png"
fig.savefig(outp, dpi=115)
plt.close(fig)
print("wrote", outp)
