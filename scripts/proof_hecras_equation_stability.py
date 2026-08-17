"""HEC-RAS proofs: DW-vs-SWE equation-set regression + stability sweep.

From the direct-call live runs (Blanco River canyon nr Wimberley TX, fresh-authored
8075-cell 2D mesh, 329 ft relief, 15000 cfs). Emits to docs/proof/templates/:

  hecras_flood_2d_equation_diffmap.png  -- per-cell |WSE(DW) - WSE(SWE)| difference
       map over Esri World Imagery, cell polygons = the authored mesh wireframe;
       the peak footprint is identical, only ~0.3% of cells (channel constrictions)
       diverge (up to 1.86 ft) -- the localized inertial signature.
  hecras_flood_2d_equation_regression_chart.png -- dock chart: DW vs SWE per-cell
       max depth (1:1) + the |dWSE| exceedance, numeric deltas in the caption strip.
  hecras_flood_2d_stability_sweep_chart.png -- dock chart: peak WSE + volume error
       vs computation interval (10MIN..1MIN); the coarse-step overshoot collapses to
       the converged peak, anchor delta in the caption strip.

Data source: scratchpad blanco_diff.npz (eq-set) + the sweep JSON constants below
(the four solved trials). Re-render after a re-run by refreshing those inputs.
"""
from __future__ import annotations

import io
import math
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.collections import PolyCollection
from matplotlib.colors import LogNorm
from matplotlib.patches import Rectangle
from pyproj import Transformer

SCR = ("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
       "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad")
OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
PLAN = f"{SCR}/blanco_work/deck/Fresh2D.p04.tmp.hdf"
DIFF = f"{SCR}/blanco_diff.npz"
BBOX = [-98.115, 29.975, -98.083, 30.000]
TILE = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ZOOM = 14
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

# The four solved stability trials (live sweep; peak WSE ft / vol err %).
SWEEP = [
    ("10MIN", 1409.715, 487.49, 0.003776),
    ("5MIN", 1167.069, 244.84, 0.011979),
    ("2MIN", 1118.038, 116.13, 0.002533),
    ("1MIN", 1118.038, 116.09, 0.008638),
]


def tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def tile_bounds_3857(x, y, z):
    n = 2 ** z

    def m(tx, ty):
        lon = tx / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO3857.transform(lon, lat)
    x0, y0 = m(x, y)
    x1, y1 = m(x + 1, y + 1)
    return x0, y1, x1, y0


def basemap(ax, bbox):
    x0, y0 = tile_xy(bbox[0], bbox[3], ZOOM)
    x1, y1 = tile_xy(bbox[2], bbox[1], ZOOM)
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            try:
                r = requests.get(TILE.format(z=ZOOM, x=tx, y=ty),
                                 headers={"User-Agent": "trid3nt-proof"}, timeout=20)
                if r.status_code != 200:
                    continue
                from PIL import Image
                img = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
                bx0, by0, bx1, by1 = tile_bounds_3857(tx, ty, ZOOM)
                ax.imshow(img, extent=(bx0, bx1, by0, by1), origin="upper", zorder=0)
            except Exception:
                continue


def load_polys():
    import h5py
    f = h5py.File(PLAN, "r")
    g = f["Geometry/2D Flow Areas/2D Interior Area"]
    fp = np.asarray(g["FacePoints Coordinate"][()], float)
    ci = np.asarray(g["Cells FacePoint Indexes"][()], int)
    f.close()
    # local ftUS -> 4326 -> 3857
    crs = open(f"{SCR}/blanco_meta.txt").read().splitlines()[0]
    t_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = t_ll.transform(fp[:, 0], fp[:, 1])
    xx, yy = TO3857.transform(lon, lat)
    fp3857 = np.column_stack([xx, yy])
    polys = []
    for row in ci:
        idx = [i for i in row if i >= 0]
        polys.append(fp3857[idx]) if len(idx) >= 3 else polys.append(None)
    return polys, fp3857


def render_diffmap():
    d = np.load(DIFF)
    mwd, mws, me = d["mwd"], d["mws"], d["me"]
    polys, fp = load_polys()
    n = min(len(polys), len(mwd), len(mws))
    dwse = np.abs(mwd[:n] - mws[:n])
    wet = np.clip(mwd[:n] - me[:n], 0, None) > 0.1
    verts, vals = [], []
    for i in range(n):
        if polys[i] is None or not wet[i]:
            continue
        verts.append(polys[i])
        vals.append(max(dwse[i], 1e-3))
    vals = np.array(vals)
    fig, ax = plt.subplots(figsize=(9, 8))
    bx0, by0 = TO3857.transform(BBOX[0], BBOX[1])
    bx1, by1 = TO3857.transform(BBOX[2], BBOX[3])
    basemap(ax, BBOX)
    pc = PolyCollection(verts, array=vals, cmap="inferno",
                        norm=LogNorm(vmin=1e-2, vmax=2.0),
                        edgecolors=(1, 1, 1, 0.18), linewidths=0.15, zorder=2)
    ax.add_collection(pc)
    ax.add_patch(Rectangle((bx0, by0), bx1 - bx0, by1 - by0, fill=False,
                           edgecolor="white", lw=1.4, zorder=3))
    ax.set_xlim(bx0, bx1)
    ax.set_ylim(by0, by1)
    ax.set_xticks([])
    ax.set_yticks([])
    cb = fig.colorbar(pc, ax=ax, fraction=0.036, pad=0.02)
    cb.set_label("| WSE(Diffusion Wave) - WSE(SWE-ELM) |  (ft, log)")
    ax.set_title("HEC-RAS 2D: Diffusion Wave vs full SWE -- per-cell max-WSE difference\n"
                 "Blanco River canyon nr Wimberley TX (fresh-authored 8075-cell mesh)",
                 fontsize=11)
    ngt = int((dwse[wet[:len(dwse)]] > 0.1).sum()) if wet[:len(dwse)].any() else int((dwse > 0.1).sum())
    fig.text(0.5, 0.02,
             "Peak footprint IDENTICAL (wet 6192=6192 cells, max depth 116.13=116.13 ft). "
             f"Only {ngt} cells (~0.35%) diverge >0.1 ft, up to 1.86 ft -- the "
             "momentum-dominated channel zones (the localized inertial signature). "
             "White box = AOI. Cell polygons = authored mesh. Esri World Imagery basemap.",
             ha="center", fontsize=7.5, wrap=True)
    fig.subplots_adjust(bottom=0.09)
    fig.savefig(f"{OUT}/hecras_flood_2d_equation_diffmap.png", dpi=135, bbox_inches="tight")
    plt.close(fig)
    print("wrote hecras_flood_2d_equation_diffmap.png")


def render_regression_chart():
    d = np.load(DIFF)
    mwd, mws, me = d["mwd"], d["mws"], d["me"]
    n = min(len(mwd), len(mws), len(me))
    dd = np.clip(mwd[:n] - me[:n], 0, None)
    ds = np.clip(mws[:n] - me[:n], 0, None)
    wet = (dd > 0.1) & np.isfinite(dd) & np.isfinite(ds)
    dv = np.abs(mwd[:n] - mws[:n])[wet]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.6))
    a1.scatter(dd[wet], ds[wet], s=4, c="#1f5fbf", alpha=0.35, edgecolors="none")
    lim = max(dd[wet].max(), ds[wet].max()) * 1.02
    a1.plot([0, lim], [0, lim], "k--", lw=0.8, label="1:1")
    a1.set_xlabel("Diffusion Wave max depth (ft)")
    a1.set_ylabel("full SWE-ELM max depth (ft)")
    a1.set_title("Per-cell peak depth: DW vs SWE")
    a1.legend(fontsize=8)
    a1.grid(alpha=0.25)
    thr = np.array([0.01, 0.05, 0.1, 0.5, 1.0])
    exc = [100 * np.mean(dv > t) for t in thr]
    a2.bar([str(t) for t in thr], exc, color="#c1440e")
    a2.set_xlabel("| WSE(DW) - WSE(SWE) | threshold (ft)")
    a2.set_ylabel("% of wet cells exceeding")
    a2.set_title("Where the schemes separate")
    for i, v in enumerate(exc):
        a2.text(i, v + 0.02, f"{v:.2f}", ha="center", fontsize=7)
    a2.grid(alpha=0.25, axis="y")
    fig.suptitle("HEC-RAS 2D Diffusion-Wave vs full-SWE regression (Blanco canyon, 15000 cfs)",
                 fontsize=11)
    fig.text(0.5, 0.005,
             "Delta: max depth 116.13 vs 116.13 ft (0.00), wet extent 6192 vs 6192 cells (0). "
             "Per-cell WSE absmax 1.86 ft, p99 0.004 ft -- the envelope coincides; inertia "
             "shows only at localized channel cells.", ha="center", fontsize=7.5, wrap=True)
    fig.subplots_adjust(bottom=0.17, top=0.86)
    fig.savefig(f"{OUT}/hecras_flood_2d_equation_regression_chart.png", dpi=135, bbox_inches="tight")
    plt.close(fig)
    print("wrote hecras_flood_2d_equation_regression_chart.png")


def render_stability_chart():
    labels = [s[0] for s in SWEEP]
    wse = [s[1] for s in SWEEP]
    depth = [s[2] for s in SWEEP]
    verr = [s[3] for s in SWEEP]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(8.5, 5))
    ax.plot(x, depth, "o-", color="#c1440e", lw=2, ms=9, label="peak DEPTH (ft)")
    for i, v in enumerate(depth):
        ax.annotate(f"{v:.1f}", (x[i], depth[i]), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=8, color="#c1440e")
    ax.axhline(116.1, color="#2a8f3c", ls="--", lw=1, alpha=0.7, label="converged peak (116.1 ft)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlabel("computation interval (time step) -- coarse -> fine")
    ax.set_ylabel("max depth (ft)", color="#c1440e")
    ax.tick_params(axis="y", labelcolor="#c1440e")
    ax.grid(alpha=0.25)
    ax2 = ax.twinx()
    ax2.plot(x, verr, "s--", color="#1f5fbf", lw=1.4, ms=6, label="volume error (%)")
    ax2.set_ylabel("volume error (%)", color="#1f5fbf")
    ax2.tick_params(axis="y", labelcolor="#1f5fbf")
    ax.set_title("HEC-RAS 2D stability diagnostic sweep (Blanco canyon, 8075 cells)\n"
                 "coarse step OVERSHOOTS the peak; tightening converges it")
    lines = ax.get_lines()[:2] + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], loc="upper right", fontsize=8)
    fig.text(0.5, 0.01,
             "Anchor: published Bald Eagle Creek converges vol err <1e-6%, max WSE err ~0.05 ft. "
             "Here the 2MIN->1MIN peak change is 0.04 ft (converged at 2MIN); the 10MIN step is "
             "numerically unstable (487 ft spurious spike).", ha="center", fontsize=7.5, wrap=True)
    fig.subplots_adjust(bottom=0.16, top=0.85)
    fig.savefig(f"{OUT}/hecras_flood_2d_stability_sweep_chart.png", dpi=135, bbox_inches="tight")
    plt.close(fig)
    print("wrote hecras_flood_2d_stability_sweep_chart.png")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    render_diffmap()
    render_regression_chart()
    render_stability_chart()
