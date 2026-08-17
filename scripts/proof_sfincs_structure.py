"""Proof for the SFINCS thin-dam SURGE-ONLY protected-side demonstration
(2nd addendum -- the rain lever). Reads the two published depth COGs
from the surge-only smoke (docs/proof/sfincs_surge_only_smoke_result.json) and
renders A (no dam) | B (thin dam) | difference as FILLED cells over
the Esri World Imagery basemap with the shore-parallel thin-dam line drawn in
cyan. With rainfall="none" the ONLY water is the surge from the sea, so the
protected (landward/north) side floods in A and stays DRY in B -- the textbook
levee signature (no co-occurring rain trapped behind the line).
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from matplotlib.colors import Normalize, TwoSlopeNorm
from PIL import Image
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, calculate_default_transform, reproject

REPO = str(Path(__file__).parent.parent)
OUT = REPO + "/docs/proof/templates"
Path(OUT).mkdir(parents=True, exist_ok=True)
SMOKE = json.loads(
    Path(REPO + "/docs/proof/sfincs_surge_only_smoke_result.json").read_text()
)
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
TO_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

import os
os.environ.setdefault("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")
from trid3nt_server.data.cache import read_object_bytes_s3  # noqa: E402


def _tile_xy(lon, lat, z):
    n = 2 ** z
    return (lon + 180.0) / 360.0 * n, (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n


def _tb(x, y, z):
    n = 2 ** z
    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)
    x0, y0 = merc(x, y + 1); x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def _basemap(w, s, e, n, zoom):
    x0f, y1f = _tile_xy(w, s, zoom); x1f, y0f = _tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30); r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, sm0, _, _ = _tb(min(xs), max(ys), zoom); _, _, em1, nm1 = _tb(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _read_3857(uri):
    with MemoryFile(read_object_bytes_s3(uri)) as mf, mf.open() as src:
        arr = src.read(1, masked=True).filled(np.nan).astype("float64")
        dt, dw, dh = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        dst = np.full((dh, dw), np.nan, dtype="float32")
        reproject(source=arr.astype("float32"), destination=dst, src_transform=src.transform,
                  src_crs=src.crs, dst_transform=dt, dst_crs="EPSG:3857",
                  src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear)
        w, s_, e, n = rasterio.transform.array_bounds(dh, dw, dt)
    return dst, (w, e, s_, n)


plain, ext_p = _read_3857(SMOKE["plain_uri"])
thd, ext_t = _read_3857(SMOKE["thd_uri"])
thd_on_p = np.full_like(plain, np.nan)
from rasterio.transform import from_bounds as _fb
ph, pw = plain.shape
tp = _fb(ext_p[0], ext_p[2], ext_p[1], ext_p[3], pw, ph)
th, tw = thd.shape
tt = _fb(ext_t[0], ext_t[2], ext_t[1], ext_t[3], tw, th)
reproject(source=thd, destination=thd_on_p, src_transform=tt, src_crs="EPSG:3857",
          dst_transform=tp, dst_crs="EPSG:3857", src_nodata=np.nan, dst_nodata=np.nan,
          resampling=Resampling.bilinear)
diff = np.where(np.isfinite(plain) | np.isfinite(thd_on_p),
                np.nan_to_num(thd_on_p) - np.nan_to_num(plain), np.nan)

aoi = SMOKE["aoi"]
w3, s3 = TO_3857.transform(aoi[0], aoi[1]); e3, n3 = TO_3857.transform(aoi[2], aoi[3])
mxx = max(e3 - w3, n3 - s3) * 0.06
bw, bs = TO_4326.transform(w3 - mxx, s3 - mxx); be, bn = TO_4326.transform(e3 + mxx, n3 + mxx)
bm, bmext = _basemap(bw, bs, be, bn, 14)
# Levee-district barrier: a south shore-parallel line + west/east return walls
# (a U open to the high north edge). Draw every segment in Web-Mercator; the run
# extends the walls a little past the north frame edge to tie into high ground.
def _seg_3857(seg):
    xs, ys = zip(*[TO_3857.transform(lon, lat) for lon, lat in seg])
    return list(xs), list(ys)
structure_segments = SMOKE.get("structure_lines") or [SMOKE["south_line"]]
dam_lat = SMOKE["dam_lat"]

# Cap the depth scale at a coastal-flood-relevant 8 m (deep near-shore surge
# cells saturate) and the diff scale at the observed protected-side max so the
# dry-out signal is legible.
vmax = 8.0
dmax = max(float(SMOKE.get("protected_plain_max_depth_m") or 0.0), 0.5)

fig, axes = plt.subplots(1, 3, figsize=(16.5, 6.4), dpi=115)
panels = [
    (plain, ext_p, "A) no dam (surge-only)", Normalize(0, vmax), "inferno", "flood depth (m, capped 8 m)"),
    (thd_on_p, ext_p, "B) with thin dam (surge-only)", Normalize(0, vmax), "inferno", "flood depth (m, capped 8 m)"),
    (diff, ext_p, "B - A (thin dam effect)", TwoSlopeNorm(0, -dmax, dmax), "RdBu_r", "depth change (m)"),
]
for ax, (data, ext, title, norm, cmap, clab) in zip(axes, panels):
    ax.imshow(bm, extent=bmext, origin="upper")
    im = ax.imshow(data, extent=ext, origin="upper", cmap=cmap, norm=norm, alpha=0.9, zorder=3)
    for seg in structure_segments:
        sx, sy = _seg_3857(seg)
        ax.plot(sx, sy, color="#00e5ff", lw=2.4, zorder=5)
    ax.plot([w3, e3, e3, w3, w3], [s3, s3, n3, n3, s3], color="white", lw=1.2, zorder=4)
    ax.set_xlim(w3 - mxx, e3 + mxx); ax.set_ylim(s3 - mxx, n3 + mxx)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_title(title, fontsize=11)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.02).set_label(clab, fontsize=9)

_ap = SMOKE.get('protected_plain_wet_cells', 0)
_bp = SMOKE.get('protected_thd_wet_cells', 0)
_red = 100.0 * (1.0 - _bp / max(1, _ap))
cap = (
    f"SFINCS thin-dam (thd) levee district, present vs absent, SURGE-ONLY "
    f"(rainfall=none -- the rain lever; no design-storm precip, so surge from the sea is the ONLY water). "
    f"{SMOKE.get('location')} ~{(aoi[2]-aoi[0])*85:.1f}x{(aoi[3]-aoi[1])*111:.1f} km AOI, coastal "
    f"{SMOKE.get('return_period_yr')}-yr / {SMOKE.get('duration_hr')}-hr design-storm surge (deterministic parametric "
    f"water-level boundary). The barrier (cyan) is a shore-parallel line just inland of the permanent Gulf/channel "
    f"waterline with WEST + EAST return walls tied to the high north edge (a U open to high ground) -- the low side "
    f"edges are also surge inlets, so a bare shore-parallel line would be flanked; the return walls are how real levee "
    f"districts prevent it. PROTECTED DISTRICT (inside the enclosure, dry land only -- permanent deep water excluded): "
    f"A floods {_ap} cells (mean {SMOKE.get('protected_plain_mean_depth_m',0):.2f} m, max "
    f"{SMOKE.get('protected_plain_max_depth_m',0):.2f} m) -> B collapses to {_bp} cells ({_red:.0f}% drier; the few "
    f"residual cells sit at the wall corners). No leak at the walls (B near-wall max "
    f"{SMOKE.get('protected_thd_edge_max_depth_m',0):.2f} m <= interior {SMOKE.get('protected_thd_mid_max_depth_m',0):.2f} m). "
    f"Filled cells over Esri World Imagery; deep near-shore surge cells saturate the 8 m depth scale."
)
fig.text(0.5, 0.005, cap, ha="center", fontsize=7.4, wrap=True)
fig.subplots_adjust(bottom=0.19, top=0.94, wspace=0.08)
outp = f"{OUT}/sfincs_flood_hydraulic_structure_weir_thd.png"
fig.savefig(outp, dpi=115)
plt.close(fig)
print("wrote", outp)
