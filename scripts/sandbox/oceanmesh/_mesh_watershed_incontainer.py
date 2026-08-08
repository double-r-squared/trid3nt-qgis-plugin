"""In-container WATERSHED-FIRST mesher for the ADR 0193 sandbox.

Runs INSIDE the GPL-isolated ``trid3nt-local/mesh:latest`` image (mounted, not
baked). Meshes an ARBITRARY polygon interior -- a pysheds catchment -- with the
authentic OceanMesh2D ``generate_mesh``, driven by two custom callbacks that
bypass the coastal ``Shoreline`` path (which only meshes water touching the
rectangular region boundary and cannot mesh a fully-enclosed inland catchment):

  * signed distance function  -- negative inside the catchment polygon (the
    watershed IS the domain; the AOI box never truncates it),
  * edge-length / sizing       -- refined by DISTANCE TO THE RIVER NETWORK
    (fine along the NHD/OSM flowlines, coarse on the ridges), i.e. NATE's
    watershed-then-mesh: delineate the watershed, then mesh its valley network.

Contract (host <-> container over the mounted /data dir):
  argv[1] = /data/mesh_config.json  keys: boubox_coords [[lon,lat],...] (catchment
            exterior, CCW-or-not), river_coords [[lon,lat],...] (flowline vertices),
            min_edge_length_m, max_edge_length_m, grade, max_iter.
  argv[2] = /data                   -- output dir (mounted).
Emits /data/coastal_tin_mesh.npz (points (N,2) lon/lat, cells (M,3)) +
/data/mesh_stats.json.
"""

from __future__ import annotations

import json
import math
import sys

import numpy as np
import oceanmesh as om
from matplotlib.path import Path
from scipy.spatial import cKDTree


def _m_per_deg(mid_lat_deg: float) -> float:
    return 111_320.0 * max(0.15, math.cos(math.radians(mid_lat_deg)))


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")

    poly = np.asarray(cfg["boubox_coords"], dtype=float)
    if not np.allclose(poly[0], poly[-1]):
        poly = np.vstack([poly, poly[0]])
    xmin, ymin = poly[:, 0].min(), poly[:, 1].min()
    xmax, ymax = poly[:, 0].max(), poly[:, 1].max()
    mid_lat = 0.5 * (ymin + ymax)
    mpd = _m_per_deg(mid_lat)
    min_deg = float(cfg["min_edge_length_m"]) / mpd
    max_deg = float(cfg["max_edge_length_m"]) / mpd
    grade = float(cfg.get("grade", 0.2))

    # inside test (fast, vectorized) + distance to boundary (KD-tree on a densified
    # boundary) -> signed distance, negative inside.
    path = Path(poly)
    # densify boundary for a smooth distance field.
    dens = []
    for (x0, y0), (x1, y1) in zip(poly[:-1], poly[1:]):
        seg = max(2, int(math.hypot(x1 - x0, y1 - y0) / (min_deg / 2)))
        for t in np.linspace(0, 1, seg, endpoint=False):
            dens.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    bdry = np.asarray(dens, dtype=float)
    bdry_tree = cKDTree(bdry)

    river = np.asarray(cfg["river_coords"], dtype=float)
    river_tree = cKDTree(river) if river.size else None

    def sdf(x):
        x = np.asarray(x, dtype=float)
        finite = np.isfinite(x).all(axis=1)
        d = np.full(len(x), 1.0e12)
        inside = np.zeros(len(x), dtype=bool)
        if finite.any():
            xf = x[finite]
            d[finite], _ = bdry_tree.query(xf, k=1)
            inside[finite] = path.contains_points(xf)
        return np.where(inside, -d, d)

    def edge_length(x):
        x = np.asarray(x, dtype=float)
        if river_tree is None:
            return np.full(len(x), min_deg)
        dr, _ = river_tree.query(np.nan_to_num(x, nan=1e9), k=1)
        # linear gradation away from the river network, clamped to [min,max].
        h = min_deg + grade * dr
        return np.clip(h, min_deg, max_deg)

    points, cells = om.generate_mesh(
        domain=sdf, edge_length=edge_length,
        bbox=(xmin, xmax, ymin, ymax), min_edge_length=min_deg,
        max_iter=int(cfg.get("max_iter", 50)),
    )
    points, cells = om.make_mesh_boundaries_traversable(points, cells, min_disconnected_area=0.05)
    points, cells = om.delete_faces_connected_to_one_face(points, cells)
    points, cells = om.mesh_clean(
        points, cells, min_element_qual=float(cfg.get("min_element_qual", 0.1)),
        min_percent_disconnected_area=0.05, max_iter=20,
    )

    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    used = np.unique(cells)
    if used.shape[0] != points.shape[0]:
        remap = np.full(points.shape[0], -1, dtype=np.int64)
        remap[used] = np.arange(used.shape[0])
        points = points[used]
        cells = remap[cells]
    np.savez(out + "/coastal_tin_mesh.npz", points=points, cells=cells)

    tri = points[cells]
    seg = np.sqrt(np.concatenate([
        ((tri[:, 1] - tri[:, 0]) ** 2).sum(1),
        ((tri[:, 2] - tri[:, 1]) ** 2).sum(1),
        ((tri[:, 0] - tri[:, 2]) ** 2).sum(1),
    ]))
    seg_m = seg * mpd
    stats = {
        "engine": "oceanmesh(CHLNDDEV OceanMesh2D port) v%s" % getattr(om, "__version__", "?"),
        "sizing_functions": ["catchment_sdf(interior)", "distance_to_river_network"],
        "grade": grade,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "n_points": int(points.shape[0]), "n_cells": int(cells.shape[0]),
        "edge_min_m": round(float(seg_m.min()), 1),
        "edge_median_m": round(float(np.median(seg_m)), 1),
        "edge_max_m": round(float(seg_m.max()), 1),
    }
    json.dump(stats, open(out + "/mesh_stats.json", "w"), indent=2)
    print("MESH_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
