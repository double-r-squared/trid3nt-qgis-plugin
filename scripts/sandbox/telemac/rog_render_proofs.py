"""Render the C4 Coweeta rain-on-grid proofs to docs/proof/templates/.

Three panels, all EPSG:3857 (Web Mercator; the parallel alignment wave found
vertical misalignment when a latitude frame mixes -- both the ESRI tiles AND the
data are projected to 3857 via merc_render):

  telemac_rain_on_grid.png       -- max WATER DEPTH (AMC II) over ESRI World
                                    Imagery + the delineated catchment boundary.
  telemac_rain_on_grid_chart.png -- dock-exact outlet hydrograph, AMC II vs
                                    AMC I (dry) overlay, with the NSE/R2 slot.
  telemac_rain_on_grid_mesh.png  -- the watershed TIN wireframe over ESRI.

Run in the agent venv (has PIL/matplotlib/pyproj/shapely). No _hermes: the max
fields are read from the npz exported inside the image; the hydrographs from the
per-solve rog_outlet_hydrograph.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.tri import Triangulation
from pyproj import Transformer

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts" / "sandbox" / "oceanmesh"))
import merc_render as MR  # noqa: E402

RUNDIR = Path("/tmp/rog_coweeta")
OUT = REPO / "docs" / "proof" / "templates"
UTM_EPSG = 32617
_TO_LL = Transformer.from_crs(UTM_EPSG, 4326, always_xy=True)


def _utm_to_merc(X, Y):
    lon, lat = _TO_LL.transform(X, Y)
    mx, my = MR.ll_to_merc(np.asarray(lon), np.asarray(lat))
    return mx, my, lon, lat


def _basemap(ax, bbox_ll):
    z = MR.pick_zoom(bbox_ll, max_tiles=8)
    img, extent = MR.fetch_basemap(bbox_ll, z)
    ax.imshow(img, extent=extent, origin="upper", zorder=0)
    return extent


def _catchment_merc():
    facts = json.loads((RUNDIR / "mesh_facts.json").read_text())
    fc = json.loads(Path(facts["catchment_geojson"]).read_text())
    rings = []
    for feat in fc["features"]:
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]]
        for ring in ([g["coordinates"][0]] if g["type"] == "Polygon" else polys):
            arr = np.asarray(ring, dtype=float)
            mx, my = MR.ll_to_merc(arr[:, 0], arr[:, 1])
            rings.append(np.column_stack([mx, my]))
    return rings


def render_map():
    npz = np.load(RUNDIR / "solve_amc2" / "max_fields.npz")
    X, Y, ikle, depth = npz["X"], npz["Y"], npz["ikle"], npz["depth"]
    mx, my, lon, lat = _utm_to_merc(X, Y)
    bbox_ll = (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))
    pad = 0.01
    bbox_ll = (bbox_ll[0] - pad, bbox_ll[1] - pad, bbox_ll[2] + pad, bbox_ll[3] + pad)

    fig, ax = plt.subplots(figsize=(9, 8))
    _basemap(ax, bbox_ll)
    tri = Triangulation(mx, my, ikle)
    d = np.clip(depth, 0.0, None)
    wet = d > 0.02  # 2 cm wet threshold
    dmask = np.where(wet, d, np.nan)
    tcf = ax.tripcolor(tri, dmask, cmap="YlGnBu", shading="gouraud",
                       vmin=0.0, vmax=float(np.nanpercentile(d[wet], 98)) if wet.any() else 1.0,
                       alpha=0.82, zorder=2)
    for ring in _catchment_merc():
        ax.plot(ring[:, 0], ring[:, 1], color="#ff3b30", lw=1.6, zorder=3)
    cb = fig.colorbar(tcf, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label("max water depth (m)  --  pinned 0 to P98")
    ax.set_xlim(min(mx.min(), ax.get_xlim()[0]), max(mx.max(), ax.get_xlim()[1]))
    m2 = json.loads((RUNDIR / "solve_amc2" / "telemac_metrics.json").read_text())
    ax.set_title("telemac_rain_on_grid  --  Coweeta Creek NC (28.7 km2, 4854 nodes)\n"
                 f"AMC II, 25 mm/h x 6 h design storm  --  peak Q {m2['peak_discharge_m3s']:.1f} m3/s, "
                 f"runoff {m2['outflow_volume_m3']/1e3:.0f} x10^3 m3", fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    ax.text(0.01, 0.01, "EPSG:3857  --  ESRI World Imagery  --  red = delineated catchment",
            transform=ax.transAxes, fontsize=7, color="w",
            bbox=dict(fc="k", alpha=0.4, pad=1.5))
    fig.tight_layout()
    fig.savefig(OUT / "telemac_rain_on_grid.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("[render] telemac_rain_on_grid.png")


def render_chart():
    h2 = json.loads((RUNDIR / "solve_amc2" / "rog_outlet_hydrograph.json").read_text())
    h1 = json.loads((RUNDIR / "solve_amc1" / "rog_outlet_hydrograph.json").read_text())
    m2 = json.loads((RUNDIR / "solve_amc2" / "telemac_metrics.json").read_text())
    m1 = json.loads((RUNDIR / "solve_amc1" / "telemac_metrics.json").read_text())
    t2 = np.asarray(h2["t_s"]) / 3600.0
    t1 = np.asarray(h1["t_s"]) / 3600.0

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(t2, h2["q_m3s"], color="#0a84ff", lw=2.2,
            label=f"AMC II (normal)  peak {m2['peak_discharge_m3s']:.1f} m3/s")
    ax.plot(t1, h1["q_m3s"], color="#ff9f0a", lw=2.2, ls="--",
            label=f"AMC I (dry)  peak {m1['peak_discharge_m3s']:.1f} m3/s")
    ax.fill_between(t2, h2["q_m3s"], color="#0a84ff", alpha=0.10)
    ax.set_xlabel("time (h)")
    ax.set_ylabel("outlet discharge Q (m3/s)")
    ax.set_title("telemac_rain_on_grid  --  Coweeta Creek outlet hydrograph\n"
                 "antecedent-moisture (CN) knob: dry vs normal", fontsize=11)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    fig.text(0.5, 0.01,
             "NSE / R2 vs USGS gauge: slot (no gauge wired -- template smoke)  --  "
             "runoff volume  AMC II {:.0f} x10^3 m3   AMC I {:.0f} x10^3 m3".format(
                 m2["outflow_volume_m3"] / 1e3, m1["outflow_volume_m3"] / 1e3),
             ha="center", va="bottom", fontsize=8, color="#3a3a3c")
    fig.savefig(OUT / "telemac_rain_on_grid_chart.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("[render] telemac_rain_on_grid_chart.png")


def render_mesh():
    npz = np.load(RUNDIR / "solve_amc2" / "max_fields.npz")
    X, Y, ikle = npz["X"], npz["Y"], npz["ikle"]
    mx, my, lon, lat = _utm_to_merc(X, Y)
    bbox_ll = (float(lon.min()) - 0.01, float(lat.min()) - 0.01,
               float(lon.max()) + 0.01, float(lat.max()) + 0.01)
    fig, ax = plt.subplots(figsize=(9, 8))
    _basemap(ax, bbox_ll)
    tri = Triangulation(mx, my, ikle)
    ax.triplot(tri, color="#30d158", lw=0.25, alpha=0.8, zorder=2)
    for ring in _catchment_merc():
        ax.plot(ring[:, 0], ring[:, 1], color="#ff3b30", lw=1.6, zorder=3)
    ax.set_title("telemac_rain_on_grid  --  Coweeta Creek watershed TIN\n"
                 f"{X.shape[0]} nodes / {ikle.shape[0]} triangles  (OceanMesh2D, EPSG:3857)",
                 fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(OUT / "telemac_rain_on_grid_mesh.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("[render] telemac_rain_on_grid_mesh.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    render_map()
    render_chart()
    render_mesh()
    print("[render] done ->", OUT)
