"""In-container OceanMesh2D driver for the standalone mesh-front sandbox.

Runs INSIDE the isolated ``trid3nt-local/mesh:latest`` image (the GPL-isolated
CHLNDDEV ``oceanmesh`` install -- the authentic OceanMesh2D Python port). It is
NOT imported by the agent venv and touches no product code; the host driver
(build_coastal_mesh.py) shells it in via ``docker run`` with a mounted rundir.

Contract (host <-> container over the mounted /data dir):
  argv[1] = /data/mesh_config.json   -- bbox, shoreline shp, dem, sizing knobs
  argv[2] = /data                    -- output dir (mounted)

Emits /data/coastal_tin_mesh.npz  (points (N,2) lon/lat, cells (M,3) 0-indexed)
and /data/mesh_stats.json          (sizing settings + oceanmesh-side counts).

OceanMesh2D methodology exercised (Roberts, Pringle, Westerink 2019, GMD
10.5194/gmd-12-1847-2019):
  * feature_sizing_function  -- distance-to-shore / local feature size (medial
    axis) resolution of the shoreline (REQUIRED distance-to-shore sizing).
  * wavelength_sizing_function -- shallow-water wavelength / depth sizing from
    the DEM (REQUIRED bathymetry-driven sizing).
  * bathymetric_gradient_sizing_function -- optional slope sizing.
  * compute_minimum -- per-node min over the active sizing functions.
  * enforce_mesh_gradation -- |grad h| <= g gradation limiting.
  * enforce_mesh_size_bounds_elevation -- min/max edge bounds.
  * generate_mesh + make_mesh_boundaries_traversable + delete_exterior_faces
    -- DistMesh force equilibration then land (exterior) face removal.
"""

from __future__ import annotations

import json
import math
import sys

import numpy as np
import oceanmesh as om


def _m_per_deg(mid_lat_deg: float) -> float:
    return 111_320.0 * max(0.15, math.cos(math.radians(mid_lat_deg)))


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")

    xmin, ymin, xmax, ymax = (float(v) for v in cfg["bbox"])
    om_bbox = (xmin, xmax, ymin, ymax)  # oceanmesh order
    mid_lat = 0.5 * (ymin + ymax)
    mpd = _m_per_deg(mid_lat)

    min_edge_deg = float(cfg["min_edge_length_m"]) / mpd
    max_edge_deg = float(cfg["max_edge_length_m"]) / mpd
    grade = float(cfg.get("grade", 0.2))

    region = om.Region(extent=om_bbox, crs="EPSG:4326")
    smooth_used = True
    try:
        shore = om.Shoreline(cfg["shoreline_shp"], region.bbox, min_edge_deg)
    except Exception:  # noqa: BLE001
        # GSHHG geometries can make the shoreline-smoothing moving average throw
        # a GEOS side-location conflict; fall back to the unsmoothed shoreline.
        smooth_used = False
        shore = om.Shoreline(
            cfg["shoreline_shp"], region.bbox, min_edge_deg, smooth_shoreline=False
        )
    sdf = om.signed_distance_function(shore)

    sizing = []
    active = []

    # REQUIRED: distance-to-shore / feature size.
    ef_feat = om.feature_sizing_function(
        shore, sdf, r=int(cfg.get("feature_r", 3)),
        min_edge_length=min_edge_deg, max_edge_length=max_edge_deg,
    )
    sizing.append(ef_feat)
    active.append("feature_sizing(distance_to_shore,medial_axis)")

    dem = None
    if cfg.get("wavelength") or cfg.get("slope"):
        dem = om.DEM(cfg["dem_path"], bbox=region)

    if cfg.get("wavelength"):
        ef_wl = om.wavelength_sizing_function(
            dem, wl=int(cfg.get("wl", 10)),
            min_edgelength=min_edge_deg, max_edge_length=max_edge_deg,
        )
        sizing.append(ef_wl)
        active.append("wavelength_sizing(shallow_water,wl=%d)" % int(cfg.get("wl", 10)))

    if cfg.get("slope"):
        ef_sl = om.bathymetric_gradient_sizing_function(
            dem, slope_parameter=float(cfg.get("slope_parameter", 20)),
            min_edge_length=min_edge_deg, max_edge_length=max_edge_deg,
        )
        sizing.append(ef_sl)
        active.append("bathymetric_gradient_sizing(slope)")

    edge_length = om.compute_minimum(sizing) if len(sizing) > 1 else sizing[0]
    edge_length = om.enforce_mesh_gradation(edge_length, gradation=grade)
    if dem is not None:
        edge_length = om.enforce_mesh_size_bounds_elevation(
            edge_length, dem, [[min_edge_deg, max_edge_deg, -1e9, 1e9]]
        )

    points, cells = om.generate_mesh(sdf, edge_length, max_iter=int(cfg.get("max_iter", 40)))
    # Land / disconnected removal -> only the water domain survives, then the
    # OceanMesh2D clean pass (sliver deletion + Laplacian smoothing + traversable
    # boundary enforcement) so min element quality clears the sliver floor.
    points, cells = om.make_mesh_boundaries_traversable(points, cells, min_disconnected_area=0.05)
    points, cells = om.delete_faces_connected_to_one_face(points, cells)
    points, cells = om.mesh_clean(
        points, cells,
        min_element_qual=float(cfg.get("min_element_qual", 0.1)),
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

    # oceanmesh-side edge stats (metres via local scaling).
    tri = points[cells]
    seg = np.sqrt(np.concatenate([
        ((tri[:, 1] - tri[:, 0]) ** 2).sum(1),
        ((tri[:, 2] - tri[:, 1]) ** 2).sum(1),
        ((tri[:, 0] - tri[:, 2]) ** 2).sum(1),
    ]))
    seg_m = seg * mpd
    stats = {
        "engine": "oceanmesh(CHLNDDEV OceanMesh2D port) v%s" % getattr(om, "__version__", "?"),
        "sizing_functions": active,
        "shoreline_smoothed": smooth_used,
        "grade": grade,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "n_points": int(points.shape[0]),
        "n_cells": int(cells.shape[0]),
        "edge_min_m": round(float(seg_m.min()), 1),
        "edge_median_m": round(float(np.median(seg_m)), 1),
        "edge_max_m": round(float(seg_m.max()), 1),
    }
    json.dump(stats, open(out + "/mesh_stats.json", "w"), indent=2)
    print("MESH_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
