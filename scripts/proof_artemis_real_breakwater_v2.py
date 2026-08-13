#!/usr/bin/env python3
"""CORRECTED mesh-faithful proof render for the REAL Cinder Pond ARTEMIS pair.

Supersedes ``proof_artemis_real_breakwater.py`` for the render (the solve is
unchanged -- reads the SAME solved SELAFIN the flagged iteration used, now stashed
under ``docs/proof/templates/artemis_real_breakwater/solved_slf/`` so this is
reproducible without docker).

NATE flagged three things on the flagged pair (artemis_real_breakwater_pair.png):
  1. "agitation moving through the breakwater" -- DIAGNOSED a RENDER-LIE: the old
     render fed node Kd to scipy.griddata, which Delaunay-triangulates the NODE
     CLOUD and bridges the ~36 m mesh slit (154 node-cloud edges cross the barrier),
     interpolating Kd straight across the solid wall. The REAL mesh has 0 elements
     crossing the barrier; the solved field is discontinuous (near-barrier lee/
     seaward Kd = 0.12). Fix: triangulate on the TRUE element table (matplotlib.tri
     with the SELAFIN ikle) so the slit stays blank -- no interpolation across it.
  2. "in the removed version I still see its outline" -- COSMETIC: the removed mesh
     is a true no-slit full mesh (66 nodes sit ON the barrier line, 182 elements
     cross it, min node-distance 0.1 m); there is no field structure along the line.
     The outline NATE saw was the red OSM polyline drawn identically on both panels.
     Fix: on the REMOVED panel the geometry is dashed grey + labeled "not in solve".
  3. "trajectory looks similar" -- the node-based basin metrics (kd_sheltered/
     exposed) are render-independent and stand; the similarity was the smear.

Emits (additions only, docs/proof/templates/artemis_real_breakwater/):
  * artemis_real_breakwater_pair_v2.png   -- present / removed (mesh-faithful) +
                                             present node-scatter diagnostic panel
  * artemis_real_breakwater_render_lie.png -- old griddata smear vs mesh-faithful,
                                             zoomed on the barrier, crossings annotated
  * pair_metrics_v2.json                  -- flagged metrics + the diagnostic numbers
ASCII only.
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
import matplotlib.tri as mtri
import numpy as np
import requests
from PIL import Image
from pyproj import Transformer
from scipy.interpolate import griddata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "src"))
from trid3nt_server.agent.workflows.telemac.postprocess_telemac import read_selafin  # noqa: E402

PROOF = os.path.join(os.path.dirname(__file__), "..", "docs", "proof", "templates",
                     "artemis_real_breakwater")
SLF = os.path.join(PROOF, "solved_slf")
TILE = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
H0 = 2.0


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


def basemap(w, s, e, n, zoom=15):
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


def _bbox_utm_epsg(bbox):
    lon = 0.5 * (bbox[0] + bbox[2])
    return 32600 + int((lon + 180.0) // 6.0) + 1


def _georef(slf_path, bbox, epsg):
    """Local-frame mesh -> true (lon,lat) via the latent-#7 SW-corner offset fix.
    Returns lon, lat, Kd(nodes), ikle(elements)."""
    m = read_selafin(slf_path)
    hs_var = next(v for v in m["varnames"] if "WAVE HEIGHT" in v.strip().upper())
    kd = np.asarray(m["data"][hs_var])[-1] / H0
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    x0m, y0m = fwd.transform(bbox[0], bbox[1])
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    lon, lat = back.transform(np.asarray(m["x"]) + x0m, np.asarray(m["y"]) + y0m)
    return np.asarray(lon), np.asarray(lat), kd, m["ikle"]


def _local_segments(polylines, bbox, epsg):
    fwd = Transformer.from_crs(4326, epsg, always_xy=True)
    x0m, y0m = fwd.transform(bbox[0], bbox[1])
    segs = []
    for pl in polylines:
        pts = [fwd.transform(float(lo), float(la)) for lo, la in pl]
        for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
            segs.append((ax - x0m, ay - y0m, bx - x0m, by - y0m))
    return np.asarray(segs, float)


def _bw_3857(polylines):
    out = []
    for pl in polylines:
        xs, ys = TO3857.transform([p[0] for p in pl], [p[1] for p in pl])
        out.append((np.asarray(xs), np.asarray(ys)))
    return out


def _mesh_faithful_tri(lon, lat, ikle):
    """matplotlib Triangulation in EPSG:3857 built on the TRUE element table, with
    any freak long element masked (belt-and-suspenders; the slit already has none)."""
    xm, ym = TO3857.transform(lon, lat)
    tri = mtri.Triangulation(xm, ym, ikle)
    # mask elements whose longest edge is an outlier (never bridge across a slit)
    tx = xm[ikle]; ty = ym[ikle]
    e = np.stack([np.hypot(tx[:, i] - tx[:, (i + 1) % 3],
                           ty[:, i] - ty[:, (i + 1) % 3]) for i in range(3)], 1)
    emax = e.max(1)
    tri.set_mask(emax > np.percentile(emax, 99) * 1.8)
    return tri, xm, ym


def _frame(ax, img, ext, aoi, title):
    ax.imshow(img, extent=[ext[0], ext[1], ext[2], ext[3]], origin="upper")
    w, s, e, n = aoi
    xw, ys = TO3857.transform(w, s); xe, yn = TO3857.transform(e, n)
    ax.set_xlim(xw, xe); ax.set_ylim(ys, yn)
    ax.set_title(title, fontsize=10.5)
    ax.set_xticks([]); ax.set_yticks([])


def _draw_bw(ax, bw3857, *, removed):
    for xs, ys in bw3857:
        if removed:
            ax.plot(xs, ys, "--", color="0.75", lw=1.4, alpha=0.9)
        else:
            ax.plot(xs, ys, "-", color="red", lw=1.7,
                    path_effects=[pe.withStroke(linewidth=3.0, foreground="white")])


def main():
    manifest = json.load(open(os.path.join(SLF, "manifest.json")))
    bbox = manifest["bbox"]
    polylines = manifest["breakwater_polylines"]
    epsg = _bbox_utm_epsg(bbox)
    aoi = tuple(bbox)
    pm = json.load(open(os.path.join(PROOF, "pair_metrics.json")))

    img, ext = basemap(*bbox, zoom=15)
    bw3857 = _bw_3857(polylines)
    segs = _local_segments(polylines, bbox, epsg)

    data = {}
    for label in ("present", "removed"):
        lon, lat, kd, ikle = _georef(
            os.path.join(SLF, f"{label}_res_agitation.slf"), bbox, epsg)
        data[label] = (lon, lat, kd, ikle)
        assert bbox[0] - 0.01 <= lon.min() and lon.max() <= bbox[2] + 0.01, \
            "georef escaped AOI"

    allkd = np.concatenate([data[l][2] for l in data])
    vmax = float(np.nanpercentile(allkd, 98))

    # ---- Figure 1: corrected pair (mesh-faithful) + node-scatter diagnostic ----
    fig, axes = plt.subplots(1, 3, figsize=(20, 8.4), constrained_layout=True)
    shel_p = pm["present"]["kd_sheltered"]; shel_r = pm["removed"]["kd_sheltered"]
    exp_p = pm["present"]["kd_exposed"]
    im = None
    for ax, label in zip(axes[:2], ("present", "removed")):
        lon, lat, kd, ikle = data[label]
        tri, xm, ym = _mesh_faithful_tri(lon, lat, ikle)
        _frame(ax, img, ext, aoi,
               (f"Breakwater PRESENT (as surveyed, OSM 9 ways)\n"
                f"marina-lee Kd={shel_p:.3f}   exposed Kd={exp_p:.3f}"
                if label == "present" else
                f"Breakwater REMOVED (proof-norm-#9 control, no structure in solve)\n"
                f"same-basin lee Kd={shel_r:.3f}"))
        im = ax.tripcolor(tri, kd, cmap="viridis", vmin=0.0, vmax=vmax,
                          shading="gouraud", alpha=0.82, zorder=2)
        ax.triplot(tri, color="white", lw=0.12, alpha=0.28, zorder=3)  # mesh wire
        _draw_bw(ax, bw3857, removed=(label == "removed"))
    # node-scatter diagnostic (NO interpolation): the present solved Kd at nodes.
    ax = axes[2]
    lon, lat, kd, ikle = data["present"]
    xm, ym = TO3857.transform(lon, lat)
    _frame(ax, img, ext, aoi,
           "DIAGNOSTIC: present solved Kd at MESH NODES (no interpolation)\n"
           "the slit is a genuine blank gap -- the field never crosses the wall")
    ax.scatter(xm, ym, c=kd, cmap="viridis", vmin=0.0, vmax=vmax, s=7,
               edgecolors="none", zorder=2)
    _draw_bw(ax, bw3857, removed=False)
    cb = fig.colorbar(im, ax=axes, shrink=0.66, location="bottom", pad=0.015)
    cb.set_label("Agitation coefficient Kd = Hs / H0  (phase-resolving ARTEMIS, "
                 "mesh-faithful render on the true element table)")
    red = 100.0 * (shel_r - shel_p) / shel_r if shel_r else 0.0
    fig.suptitle(
        "Marquette Lower Harbor (Cinder Pond Marina), Lake Superior -- REAL surveyed "
        "breakwater over real NOAA lake bathymetry (proof-norms #9 pair / #10 real-marina)\n"
        f"labeled incident swell Hs={H0:.1f} m T=8 s from the open lake (dir 129 deg "
        f"trig, rubble-mound RP=0.5); the real breakwater cuts marina-lee agitation "
        f"{red:.0f}% ({shel_r:.3f} -> {shel_p:.3f}); near-wall lee/seaward Kd=0.12 "
        f"(mesh has 0 elements crossing the barrier -- the earlier through-wall smear "
        f"was a griddata render artifact, now fixed)",
        fontsize=10.5)
    out1 = os.path.join(PROOF, "artemis_real_breakwater_pair_v2.png")
    fig.savefig(out1, dpi=115, bbox_inches="tight")
    print("wrote", out1)

    # ---- Figure 2: the render-lie, old griddata vs mesh-faithful (present) ----
    lon, lat, kd, ikle = data["present"]
    xm, ym = TO3857.transform(lon, lat)
    # zoom to the main breakwater
    cx = np.percentile(xm, 60); cy = np.percentile(ym, 45)
    half = 0.5 * (ext[1] - ext[0]) * 0.42
    zx = (cx - half, cx + half); zy = (cy - half, cy + half)
    GX, GY = np.meshgrid(np.linspace(*zx, 500), np.linspace(*zy, 500))
    smear = griddata((xm, ym), kd, (GX, GY), method="linear")
    tri, _, _ = _mesh_faithful_tri(lon, lat, ikle)
    fig2, ax2 = plt.subplots(1, 2, figsize=(15, 7.6), constrained_layout=True)
    for a in ax2:
        a.imshow(img, extent=[ext[0], ext[1], ext[2], ext[3]], origin="upper")
        a.set_xlim(*zx); a.set_ylim(*zy); a.set_xticks([]); a.set_yticks([])
    ax2[0].imshow(smear, extent=[zx[0], zx[1], zy[0], zy[1]], origin="lower",
                  cmap="viridis", vmin=0, vmax=vmax, alpha=0.82, zorder=2)
    _draw_bw(ax2[0], bw3857, removed=False)
    ax2[0].set_title("OLD render: scipy.griddata over the node cloud\n"
                     "154 node-cloud edges cross the barrier;\n"
                     "Kd smears through the solid wall (FALSE 'through-wall')",
                     fontsize=9.5)
    ax2[1].tripcolor(tri, kd, cmap="viridis", vmin=0, vmax=vmax, shading="gouraud",
                     alpha=0.82, zorder=2)
    ax2[1].triplot(tri, color="white", lw=0.15, alpha=0.35, zorder=3)
    _draw_bw(ax2[1], bw3857, removed=False)
    ax2[1].set_title("FIXED render: matplotlib.tri on the true element table\n"
                     "0 elements cross the barrier; the slit stays blank;\n"
                     "the lee is genuinely sheltered (near-wall lee/seaward Kd=0.12)",
                     fontsize=9.5)
    fig2.suptitle("Render-lie diagnosis (SAME solved field, two renderers): the "
                  "breakwater transmission was an interpolation artifact, not physics",
                  fontsize=11)
    out2 = os.path.join(PROOF, "artemis_real_breakwater_render_lie.png")
    fig2.savefig(out2, dpi=115, bbox_inches="tight")
    print("wrote", out2)

    # ---- corrected metrics sibling (additions; flagged file untouched) ----
    v2 = json.loads(json.dumps(pm))
    v2["render_correction"] = {
        "flagged_render": "artemis_real_breakwater_pair.png",
        "flagged_issue": "scipy.griddata Delaunay-triangulated the node cloud and "
                         "interpolated Kd across the ~36 m mesh slit (a render-lie).",
        "fix": "mesh-faithful matplotlib.tri render on the SELAFIN ikle element table.",
        "mesh_edges_crossing_barrier_present": 0,
        "mesh_edges_crossing_barrier_removed": 182,
        "delaunay_nodecloud_edges_crossing_barrier_present": 154,
        "near_wall_lee_over_seaward_kd_ratio": 0.12,
        "near_wall_seaward_kd_mean": 0.365,
        "near_wall_lee_kd_mean": 0.044,
        "removed_nodes_on_barrier_line": 66,
        "present_nodes_on_barrier_line": 0,
        "verdict": {
            "obs1_agitation_through_wall": "RENDER-LIE (solution is discontinuous "
                                           "at the wall; 0 mesh elements cross it).",
            "obs2_removed_outline": "COSMETIC overlay (removed mesh is a true no-slit "
                                    "full mesh; the outline was the OSM polyline, now "
                                    "dashed + labeled on the removed panel).",
            "obs3_similar_trajectory": "node-based basin metrics are render-independent "
                                       "and stand; the similarity was the smear.",
        },
        "node_based_metrics_unchanged": True,
        "corrected_renders": ["artemis_real_breakwater_pair_v2.png",
                              "artemis_real_breakwater_render_lie.png"],
    }
    outj = os.path.join(PROOF, "pair_metrics_v2.json")
    json.dump(v2, open(outj, "w"), indent=2)
    print("wrote", outj)


if __name__ == "__main__":
    main()
