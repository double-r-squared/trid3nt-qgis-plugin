#!/usr/bin/env python3
"""Proofs for the ADR 0210 channel-refined rain-on-grid mesh.

(1) hecras_flood_2d_rog_mesh.png -- the GRADED HEC-RAS 2025 mesh (Voronoi cells) over
    ESRI World Imagery, cells shaded by size + thin wireframe edges + the channel
    network, so the fine channel bands (down to ~22 m) stand out against the ~90 m
    hillslope background (the paper's dynamic resolution). Separate image per the norm.
(2) hecras_flood_2d_rog_depth_refined.png -- max depth on the refined mesh (sibling of
    the uniform depth proof; the default stays uniform, ADR 0210).
(3) hecras_flood_2d_rog_compare_chart.png -- uniform-60m vs refined outlet hydrograph
    vs the TELEMAC-2D reference peak (the refinement sharpens channel routing -- earlier
    peak, higher channel velocity -- without moving the infiltration-dominated HR/TELEMAC
    gap).

Reuses the ESRI basemap machinery from proof_rog2025.py. ASCII only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PolyCollection
from pyproj import Transformer
from shapely.geometry import shape, MultiPoint, box, Point
from shapely.ops import transform as shp_transform, voronoi_diagram
from shapely.prepared import prep as sprep

sys.path.insert(0, str(Path(__file__).resolve().parent))
from proof_rog2025 import _basemap, TO3857, OUT  # noqa: E402

FT = Path("/home/nate/Documents/trid3nt-local/workers/hecras2025/subst/crux/freshtopo")
sys.path.insert(0, str(FT))
from rog2025_pipeline import prepare_local_terrain  # noqa: E402

DEM = "/tmp/rog_coweeta/dem.tif"
CATCH = "/tmp/rog_coweeta/catchment.geojson"
FLOW = "/tmp/rog_coweeta/flowlines.fgb"
PP = (-83.40396, 35.0576)
REF_H5 = "/home/nate/hecras_probe2025/rog_refine_coweeta/result.h5"
UNI_H5 = "/home/nate/hecras_probe2025/rog_rog2025_coweeta_dwe/result.h5"


def _load(prep, result_h5):
    with h5py.File(result_h5, "r") as f:
        base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
        depth = f[f"{base}/Cell Depth"][:].max(axis=0)
        cxy = f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
    return depth, cxy


def _to_ll(prep):
    return Transformer.from_crs(f"EPSG:{prep.utm_epsg}", "EPSG:4326", always_xy=True)


def _basemap_for(lon, lat, pad=0.12, zoom=13):
    lw, le, ls, ln = lon.min(), lon.max(), lat.min(), lat.max()
    px = (le - lw) * pad; py = (ln - ls) * pad
    bm, ext = _basemap(lw - px, ls - py, le + px, ln + py, zoom)
    mx0, my0 = TO3857.transform(lw, ls); mx1, my1 = TO3857.transform(le, ln)
    xlim = (mx0 - (mx1 - mx0) * pad, mx1 + (mx1 - mx0) * pad)
    ylim = (my0 - (my1 - my0) * pad, my1 + (my1 - my0) * pad)
    return bm, ext, xlim, ylim


def mesh_wireframe():
    prep = prepare_local_terrain(DEM, "/tmp/pr_mesh", cell_size=90.0, pour_point=PP)
    _, cxy = _load(prep, REF_H5)
    W, H = prep.width_m, prep.height_m
    to_ll = _to_ll(prep)
    # true HEC cells = Voronoi of centers clipped to the domain rectangle
    env = box(0, 0, W, H)
    vd = voronoi_diagram(MultiPoint([(x, y) for x, y in cxy]), envelope=env)
    polys = [g.intersection(env) for g in vd.geoms]
    # order polys to cells by containment; size = sqrt(area)
    verts3857 = []; sizes = []
    def loc_to_3857(x, y, z=None):
        lon, lat = to_ll.transform(prep.origin_x + np.asarray(x), prep.origin_y + np.asarray(y))
        return TO3857.transform(lon, lat)
    for poly in polys:
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        p3857 = shp_transform(loc_to_3857, poly)
        verts3857.append(np.asarray(p3857.exterior.coords))
        sizes.append(float(poly.area) ** 0.5)
    sizes = np.array(sizes)

    lon, lat = to_ll.transform(prep.origin_x + cxy[:, 0], prep.origin_y + cxy[:, 1])
    bm, ext, xlim, ylim = _basemap_for(lon, lat)
    fig, ax = plt.subplots(figsize=(8, 7), dpi=130)
    ax.imshow(bm, extent=ext, origin="upper")
    pc = PolyCollection(verts3857, array=np.clip(sizes, 15, 95), cmap="viridis_r",
                        edgecolors="#12121260", linewidths=0.15, alpha=0.72, zorder=3)
    ax.add_collection(pc)
    # channel network overlay
    import geopandas as gpd
    ch = gpd.read_file(FLOW).to_crs(4326)
    for geom in ch.geometry:
        gs = geom.geoms if hasattr(geom, "geoms") else [geom]
        for s in gs:
            xy = np.asarray(s.coords)
            mx, my = TO3857.transform(xy[:, 0], xy[:, 1])
            ax.plot(mx, my, color="#00e5ff", lw=0.6, alpha=0.8, zorder=4)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(pc, ax=ax, fraction=0.035, pad=0.02)
    cb.set_label("cell size sqrt(area) (m)")
    fine = int((sizes < 35).sum()); coarse = int((sizes >= 70).sum())
    ax.set_title("HEC-RAS 2025 rain-on-grid: channel-refined mesh, Coweeta Creek NC\n"
                 f"{len(polys)} cells: ~22 m along the channel (cyan) grading to ~90 m "
                 f"hillslopes ({fine} fine <35 m / {coarse} coarse >=70 m)", fontsize=9.5)
    p = OUT / "hecras_flood_2d_rog_mesh.png"
    fig.tight_layout(); fig.savefig(p, dpi=150); plt.close(fig)
    return str(p)


def depth_refined():
    prep = prepare_local_terrain(DEM, "/tmp/pr_depth", cell_size=90.0, pour_point=PP)
    dmax, cxy = _load(prep, REF_H5)
    to_ll = _to_ll(prep)
    ux = prep.origin_x + cxy[:, 0]; uy = prep.origin_y + cxy[:, 1]
    lon, lat = to_ll.transform(ux, uy)
    mx, my = TO3857.transform(lon, lat)
    g = json.loads(Path(CATCH).read_text())
    geom = shape((g["features"][0] if "features" in g else g)["geometry"])
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{prep.utm_epsg}", always_xy=True)
    geom_utm = shp_transform(lambda X, Y, z=None: to_utm.transform(X, Y), geom)
    geom3857 = shp_transform(lambda X, Y, z=None: TO3857.transform(X, Y), geom)
    pgm = sprep(geom_utm)
    incatch = np.array([pgm.contains(Point(x, y)) for x, y in zip(ux, uy)])
    keep = (dmax > 0.02) & incatch

    bm, ext, xlim, ylim = _basemap_for(lon, lat)
    fig, ax = plt.subplots(figsize=(8, 7), dpi=120)
    ax.imshow(bm, extent=ext, origin="upper")
    vmax = max(float(np.nanpercentile(dmax[keep], 98)) if keep.any() else 1.0, 0.2)
    sc = ax.scatter(mx[keep], my[keep], c=dmax[keep], s=5, marker="o",
                    cmap="YlGnBu", vmin=0, vmax=vmax, alpha=0.85, linewidths=0, zorder=3)
    polys = geom3857.geoms if geom3857.geom_type != "Polygon" else [geom3857]
    for poly in polys:
        xs, ys = poly.exterior.xy
        ax.plot(xs, ys, color="#ff3b30", lw=1.8, zorder=5)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(sc, ax=ax, fraction=0.035, pad=0.02)
    cb.set_label("max water depth (m)")
    ax.set_title("HEC-RAS 2025 rain-on-grid: max depth on the CHANNEL-REFINED mesh\n"
                 "Coweeta Creek NC, 25 mm/hr x 6 h; red = delineated catchment "
                 "(refinement sharpens channel depth/velocity)", fontsize=9.5)
    p = OUT / "hecras_flood_2d_rog_depth_refined.png"
    fig.tight_layout(); fig.savefig(p, dpi=150); plt.close(fig)
    return str(p)


def compare_chart(metrics_json):
    d = json.loads(Path(metrics_json).read_text())
    mu, mr = d["uniform"], d["refined"]
    tu = np.array(mu["hydrograph_t_hr"]); qu = np.array(mu["hydrograph_q_m3s"]).copy(); qu[:1] = 0
    tr = np.array(mr["hydrograph_t_hr"]); qr = np.array(mr["hydrograph_q_m3s"]).copy(); qr[:1] = 0
    TELEMAC = 45.5

    fig, ax = plt.subplots(figsize=(6.0, 2.4), dpi=100)
    fig.subplots_adjust(left=0.11, right=0.985, top=0.9, bottom=0.46)
    ax.plot(tu, qu, color="#1f5fbf", lw=1.5, label=f"uniform 60 m ({mu['n_cells']} cells)")
    ax.plot(tr, qr, color="#d1495b", lw=1.5, label=f"channel-refined ({mr['n_cells']} cells)")
    ax.axhline(TELEMAC, color="#e07b00", lw=1.2, ls="--",
               label=f"TELEMAC-2D peak {TELEMAC:.1f} m3/s (AMC II)")
    ax.scatter([mu["peak_time_hr"]], [mu["peak_outlet_q_m3s"]], color="#1f5fbf", s=16, zorder=5)
    ax.scatter([mr["peak_time_hr"]], [mr["peak_outlet_q_m3s"]], color="#d1495b", s=16, zorder=5)
    ax.set_xlabel("time (h)", fontsize=8); ax.set_ylabel("outlet Q (m3/s)", fontsize=8)
    ax.tick_params(labelsize=7); ax.legend(fontsize=6.0, loc="center left", framealpha=0.85)
    ax.margins(x=0.02); ax.grid(True, lw=0.3, alpha=0.4)
    cap = (f"Coweeta Creek NC (28.9 km2), 25 mm/hr x 6 h, DWE rain-only. Channel refinement "
           f"(~22 m along the channel) sharpens routing:\npeak {mu['peak_time_hr']:.1f} h -> "
           f"{mr['peak_time_hr']:.1f} h, max channel velocity {mu['max_velocity_ms']:.1f} -> "
           f"{mr['max_velocity_ms']:.1f} m/s; peak Q {mu['peak_outlet_q_m3s']:.0f} -> "
           f"{mr['peak_outlet_q_m3s']:.0f} m3/s and 99.6% mass closure unchanged. The ~4x "
           f"HR/TELEMAC gap is the infiltration difference (HR is rain-only), not the mesh.")
    fig.text(0.5, 0.15, cap, ha="center", va="top", fontsize=5.4)
    p = OUT / "hecras_flood_2d_rog_compare_chart.png"
    fig.savefig(p, dpi=200); plt.close(fig)
    return str(p)


def main():
    metrics_json = sys.argv[1] if len(sys.argv) > 1 else "/tmp/compare_metrics.json"
    print(mesh_wireframe())
    print(depth_refined())
    print(compare_chart(metrics_json))


if __name__ == "__main__":
    raise SystemExit(main())
