"""In-container WATER-EDGE coastal mesher for the ``coastal_edge`` mesher.

Runs INSIDE the GPL-isolated ``trid3nt-local/mesh:latest`` image (mounted, not
baked). Meshes the interior of a HIGH-RES water polygon (OSM coastline + NHD
areal water; see water_edge.py) with the authentic OceanMesh2D ``generate_mesh``,
driven by two custom callbacks that bypass the coastal ``Shoreline`` path.

The coastal ``Shoreline`` path is deliberately NOT used: it models polygons as
exterior-ring-only coord arrays (holes discarded) and Chaikin-SMOOTHS the
shoreline, which both breaks a "domain-box minus water" land polygon AND moves
the meshed edge off the imagery -- the opposite of the alignment goal. A custom
signed-distance function over the exact water polygon (holes = islands preserved)
keeps the meshed edge ON the real shoreline. This mirrors the proven
watershed custom-SDF mesher, with coastal sizing instead of distance-to-river:

  * signed distance function  -- negative INSIDE the water polygon (islands are
    holes; multi-part water supported),
  * feature / distance-to-shore sizing -- fine at the real shoreline, grading
    coarser offshore (the open/domain-edge boundary is excluded so the mesh is
    NOT forced fine along the artificial offshore arc),
  * wavelength-to-depth sizing (optional) -- h_wl = T_M2*sqrt(g*depth)/wl from
    the topobathy DEM, min'd with the feature size.

Contract (host <-> container over the mounted /data dir):
  argv[1] = /data/mesh_config.json  keys: water_geojson, dem_path, bbox
            [xmin,ymin,xmax,ymax], min_edge_length_m, max_edge_length_m, grade,
            wavelength (bool), wl, max_iter.
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
from scipy.spatial import cKDTree
from shapely import contains_xy
from shapely.geometry import shape as _shape

_T_M2 = 44714.0  # M2 tidal period (s)
_G = 9.81


def _m_per_deg(mid_lat_deg: float) -> float:
    return 111_320.0 * max(0.15, math.cos(math.radians(mid_lat_deg)))


def _rings(geom) -> list[tuple[list, list]]:
    """(exterior_coords, [hole_coords,...]) per polygon of a (Multi)Polygon."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in polys:
        ext = [(float(x), float(y)) for x, y in poly[0]]
        holes = [[(float(x), float(y)) for x, y in ring] for ring in poly[1:]]
        out.append((ext, holes))
    return out


def _densify(coords, step_deg):
    dens = []
    cs = list(coords)
    if cs[0] != cs[-1]:
        cs = cs + [cs[0]]
    for (x0, y0), (x1, y1) in zip(cs[:-1], cs[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step_deg))
        for t in np.linspace(0, 1, n, endpoint=False):
            dens.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    return dens


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")

    xmin, ymin, xmax, ymax = (float(v) for v in cfg["bbox"])
    mid_lat = 0.5 * (ymin + ymax)
    mpd = _m_per_deg(mid_lat)
    min_deg = float(cfg["min_edge_length_m"]) / mpd
    max_deg = float(cfg["max_edge_length_m"]) / mpd
    grade = float(cfg.get("grade", 0.2))

    water = json.load(open(cfg["water_geojson"]))
    geom = water["features"][0]["geometry"] if water.get("type") == "FeatureCollection" else water
    rings = _rings(geom)
    # Vectorized, spatially-indexed inside test (holes = islands handled natively
    # by GEOS -- far faster than per-hole matplotlib contains for many islands).
    water_geom = _shape(geom)

    # Boundary point clouds: all rings feed the SDF distance; the SHORE tree
    # excludes vertices on the open/domain edge so the mesh is fine on the real
    # coast but not forced fine along the artificial offshore arc.
    edge_tol = min_deg * 0.75
    all_bdry, shore_bdry = [], []
    for ext, holes in rings:
        for ring in [ext, *holes]:
            pts = _densify(ring, min_deg * 0.5)
            all_bdry.extend(pts)
            for (x, y) in pts:
                on_edge = (
                    abs(x - xmin) < edge_tol or abs(x - xmax) < edge_tol
                    or abs(y - ymin) < edge_tol or abs(y - ymax) < edge_tol
                )
                if not on_edge:
                    shore_bdry.append((x, y))
    bdry_tree = cKDTree(np.asarray(all_bdry))
    shore_tree = cKDTree(np.asarray(shore_bdry)) if shore_bdry else bdry_tree

    # Optional depth interpolator for wavelength sizing.
    depth_interp = None
    if cfg.get("wavelength"):
        import rasterio
        from scipy.interpolate import RegularGridInterpolator

        with rasterio.open(cfg["dem_path"]) as ds:
            arr = ds.read(1).astype(float)
            t = ds.transform
        ny, nx = arr.shape
        xs = t.c + t.a * (np.arange(nx) + 0.5)
        ys = t.f + t.e * (np.arange(ny) + 0.5)
        if ys[0] > ys[-1]:
            ys = ys[::-1]
            arr = arr[::-1, :]
        arr = np.where(np.isfinite(arr) & (arr > -12000), arr, 0.0)
        depth_interp = RegularGridInterpolator(
            (ys, xs), arr, bounds_error=False, fill_value=0.0
        )

    def sdf(x):
        x = np.asarray(x, dtype=float)
        d = np.full(len(x), 1.0e12)
        inside = np.zeros(len(x), dtype=bool)
        finite = np.isfinite(x).all(axis=1)
        if finite.any():
            xf = x[finite]
            d[finite], _ = bdry_tree.query(xf, k=1)
            inside[finite] = contains_xy(water_geom, xf[:, 0], xf[:, 1])
        return np.where(inside, -d, d)

    # What the wavelength term ACTUALLY did. A sizing function that is switched on
    # but never smaller than the feature size contributes nothing to the mesh, and
    # ``sizing_functions`` must not claim it did: h_wl = T_M2*sqrt(g*h)/wl is ~9.9 km
    # even in 0.5 m of water, so at any coastal max_edge_length it is clipped away.
    wl_bind = {"n": 0, "total": 0, "h_wl_min_m": None}

    def edge_length(x):
        x = np.asarray(x, dtype=float)
        xq = np.nan_to_num(x, nan=1e9)
        ds_shore, _ = shore_tree.query(xq, k=1)
        h = min_deg + grade * ds_shore                       # distance-to-shore
        h = np.clip(h, min_deg, max_deg)
        if depth_interp is not None:
            elev = depth_interp(np.column_stack([xq[:, 1], xq[:, 0]]))
            depth = np.clip(-elev, 0.5, None)
            h_wl = (_T_M2 * np.sqrt(_G * depth) / float(cfg.get("wl", 10))) / mpd
            h_wl_clipped = np.clip(h_wl, min_deg, max_deg)
            wl_bind["n"] += int(np.count_nonzero(h_wl_clipped < h))
            wl_bind["total"] += int(np.size(h))
            seen = float(np.min(h_wl)) * mpd
            prev = wl_bind["h_wl_min_m"]
            wl_bind["h_wl_min_m"] = seen if prev is None else min(prev, seen)
            h = np.minimum(h, h_wl_clipped)
        return h

    points, cells = om.generate_mesh(
        domain=sdf, edge_length=edge_length,
        bbox=(xmin, xmax, ymin, ymax), min_edge_length=min_deg,
        max_iter=int(cfg.get("max_iter", 30)),
    )
    points, cells = om.make_mesh_boundaries_traversable(
        points, cells, min_disconnected_area=0.05
    )
    points, cells = om.delete_faces_connected_to_one_face(points, cells)
    # mesh_clean's laplacian2 raises on a degenerate boundary; keep the clean
    # topology and skip the smoothing pass rather than fail the whole mesh.
    cleaned = False
    if np.asarray(cells).shape[0] > 0:
        try:
            points, cells = om.mesh_clean(
                points, cells, min_element_qual=float(cfg.get("min_element_qual", 0.1)),
                min_percent_disconnected_area=0.05, max_iter=20,
            )
            cleaned = True
        except Exception as exc:  # noqa: BLE001 -- keep the pre-clean mesh
            print("mesh_clean skipped:", exc, flush=True)

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
    active = ["feature_sizing(distance_to_shore)"]
    wl_frac = wl_bind["n"] / max(1, wl_bind["total"])
    if depth_interp is not None:
        if wl_bind["n"]:
            active.append(
                "wavelength_sizing(shallow_water,wl=%d) bound %.1f%% of queries"
                % (int(cfg.get("wl", 10)), 100.0 * wl_frac))
        else:
            active.append(
                "wavelength_sizing(shallow_water,wl=%d) REQUESTED BUT NEVER BOUND "
                "(smallest h_wl %.0f m >= max_edge_length %.0f m; the mesh size is "
                "distance-to-shore alone)"
                % (int(cfg.get("wl", 10)), wl_bind["h_wl_min_m"] or 0.0,
                   float(cfg["max_edge_length_m"])))
    stats = {
        "engine": "oceanmesh(CHLNDDEV OceanMesh2D port) v%s" % getattr(om, "__version__", "?"),
        "sizing_functions": active,
        "shoreline_source": "OSM natural=coastline + NHD areal water (custom SDF, no Shoreline smoothing)",
        "mesh_clean": cleaned,
        "grade": grade,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "wavelength_binding_fraction": round(wl_frac, 6),
        "wavelength_h_wl_min_m": (
            None if wl_bind["h_wl_min_m"] is None
            else round(float(wl_bind["h_wl_min_m"]), 1)),
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
