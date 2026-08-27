"""In-container OceanMesh2D driver for the ``om2d`` mesher.

Runs INSIDE ``trid3nt-local/mesh:latest``, the only place the CHLNDDEV
``oceanmesh`` port (OceanMesh2D, GPL-3) is installed. The host mesher mounts this
file and a rundir and shells it; nothing here imports trid3nt code.

Contract (host <-> container over the mounted /data dir):
  argv[1] = /data/om2d_config.json   argv[2] = /data
  config keys: bbox [xmin,ymin,xmax,ymax], shoreline_shp, dem_path (optional -
  enables wavelength sizing), min_edge_length_m, max_edge_length_m, gradation,
  obstacles [{geojson, constrain}], refine_regions [{geojson, edge_length_m}],
  max_iter, seed, wl.
Emits /data/om2d_mesh.npz (points (N,2) lon/lat, cells (M,3) 0-based, pfix (K,2))
and /data/om2d_stats.json.

An obstacle is subtracted from the signed distance function, so the mesh has a
HOLE where it sits, and its outline vertices are passed as ``pfix`` so DistMesh
locks them: that is what makes the cut conformal. The offset is MEASURED on the
host from the returned pfix, never asserted here.

A refine region is written onto the sizing GRID's own lattice before gradation
limiting, so the transition into it obeys the same gradation the rest of the mesh
does rather than being a discontinuity DistMesh has to absorb.
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
from shapely.ops import unary_union


def _m_per_deg(mid_lat_deg: float) -> float:
    return 111_320.0 * max(0.15, math.cos(math.radians(mid_lat_deg)))


def _load_geoms(path: str) -> list:
    doc = json.load(open(path))
    feats = doc.get("features") if isinstance(doc, dict) else None
    if feats is not None:
        return [_shape(f["geometry"]) for f in feats if f.get("geometry")]
    if isinstance(doc, dict) and doc.get("type") == "GeometryCollection":
        return [_shape(g) for g in doc["geometries"]]
    return [_shape(doc)]


def _outline_coords(geom) -> list[list[tuple[float, float]]]:
    """Every ring/line of a geometry as a coordinate list."""
    kind = geom.geom_type
    if kind in ("Polygon",):
        return [list(geom.exterior.coords)] + [list(r.coords) for r in geom.interiors]
    if kind in ("LineString", "LinearRing"):
        return [list(geom.coords)]
    if kind.startswith("Multi") or kind == "GeometryCollection":
        out: list[list[tuple[float, float]]] = []
        for part in geom.geoms:
            out.extend(_outline_coords(part))
        return out
    return []


def _resample(coords, step: float) -> list[tuple[float, float]]:
    """Walk a polyline at a fixed step, keeping every original vertex."""
    out: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        span = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(span / step))
        for t in np.linspace(0.0, 1.0, n, endpoint=False):
            out.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    if coords:
        out.append(tuple(coords[-1]))
    return out


def _thin(points: np.ndarray, spacing: float) -> np.ndarray:
    """Keep points no closer together than ``spacing``, in input order."""
    kept: list[np.ndarray] = []
    for point in points:
        if not kept or np.min(np.hypot(*(np.asarray(kept) - point).T)) >= spacing:
            kept.append(point)
    return np.asarray(kept, dtype=float)


class _Holes:
    """The union of the obstacle geometries, as a signed distance in degrees."""

    def __init__(self, geoms, step: float) -> None:
        self.union = unary_union(geoms)
        pts: list[tuple[float, float]] = []
        for geom in geoms:
            for ring in _outline_coords(geom):
                pts.extend(_resample(ring, step))
        # Two constrained points closer than the finest edge would be locked into a
        # zero-length edge, so the outline is thinned to the spacing the mesh can
        # actually hold (a closed ring's repeated first vertex goes with them).
        self.outline = _thin(np.asarray(pts, dtype=float), 0.5 * step)
        self.tree = cKDTree(self.outline)

    def signed(self, x: np.ndarray) -> np.ndarray:
        xq = np.nan_to_num(np.asarray(x, dtype=float), nan=1.0e9)
        d, _ = self.tree.query(xq, k=1)
        inside = contains_xy(self.union, xq[:, 0], xq[:, 1])
        return np.where(inside, -d, d)


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")

    xmin, ymin, xmax, ymax = (float(v) for v in cfg["bbox"])
    om_bbox = (xmin, xmax, ymin, ymax)
    mpd = _m_per_deg(0.5 * (ymin + ymax))
    min_deg = float(cfg["min_edge_length_m"]) / mpd
    max_deg = float(cfg["max_edge_length_m"]) / mpd
    gradation = float(cfg.get("gradation", 0.15))
    seed = int(cfg.get("seed", 0))

    region = om.Region(extent=om_bbox, crs="EPSG:4326")
    smoothed = True
    try:
        shore = om.Shoreline(cfg["shoreline_shp"], region.bbox, min_deg)
    except Exception:  # noqa: BLE001
        # The shoreline smoothing moving-average throws a GEOS side-location
        # conflict on some GSHHG geometries; the unsmoothed shoreline still meshes.
        smoothed = False
        shore = om.Shoreline(cfg["shoreline_shp"], region.bbox, min_deg,
                             smooth_shoreline=False)
    sdf = om.signed_distance_function(shore)

    active = []
    sizing = [om.feature_sizing_function(
        shore, sdf, r=int(cfg.get("feature_r", 3)),
        min_edge_length=min_deg, max_edge_length=max_deg)]
    active.append("feature_sizing(distance_to_shore,medial_axis)")

    dem = None
    if cfg.get("dem_path"):
        dem = om.DEM(cfg["dem_path"], bbox=region)
        sizing.append(om.wavelength_sizing_function(
            dem, wl=int(cfg.get("wl", 10)),
            min_edgelength=min_deg, max_edge_length=max_deg))
        active.append("wavelength_sizing(shallow_water,wl=%d)" % int(cfg.get("wl", 10)))

    edge_length = om.compute_minimum(sizing) if len(sizing) > 1 else sizing[0]

    obstacles = list(cfg.get("obstacles") or [])
    holes = None
    if obstacles:
        holes = _Holes([g for spec in obstacles for g in _load_geoms(spec["geojson"])],
                       min_deg)

    regions = list(cfg.get("refine_regions") or [])
    if regions or holes is not None:
        xg, yg = edge_length.create_grid()
        flat = np.column_stack([xg.ravel(), yg.ravel()])
        values = np.asarray(edge_length.values, dtype=float)
        for spec in regions:
            target = float(spec["edge_length_m"]) / mpd
            geom = unary_union(_load_geoms(spec["geojson"]))
            inside = contains_xy(geom, flat[:, 0], flat[:, 1]).reshape(xg.shape)
            values = np.where(inside, np.minimum(values, target), values)
            active.append("refine_region(edge_length=%.0fm)"
                          % float(spec["edge_length_m"]))
        if holes is not None:
            # The cut can only follow the outline if the mesh is fine enough there
            # to hold it: an obstacle sizes its own surroundings the way the
            # shoreline does, growing at the same gradation away from the cut.
            near = np.abs(holes.signed(flat)).reshape(xg.shape)
            values = np.minimum(values, min_deg + gradation * near)
            active.append("obstacle_sizing(distance_to_outline)")
        values = np.clip(values, min_deg, max_deg)
        edge_length.values = values
        edge_length.hmin = float(np.nanmin(values[np.isfinite(values) & (values > 0)]))
        edge_length.build_interpolant()

    edge_length = om.enforce_mesh_gradation(edge_length, gradation=gradation)
    if dem is not None:
        edge_length = om.enforce_mesh_size_bounds_elevation(
            edge_length, dem, [[min_deg, max_deg, -1e9, 1e9]])

    domain = sdf
    pfix = np.empty((0, 2), dtype=float)
    if holes is not None:
        base = sdf.eval

        def domain(x):  # noqa: F811 -- the holed domain replaces the bare shoreline
            return np.maximum(base(x), -holes.signed(x))

        if any(spec.get("constrain", True) for spec in obstacles):
            # A constrained vertex on land would pin a node the shoreline excludes,
            # so only the outline inside the water domain is locked.
            pfix = holes.outline[base(holes.outline) < 0.0]
        active.append("obstacles(%d,pfix=%d)" % (len(obstacles), int(pfix.shape[0])))

    points, cells = om.generate_mesh(
        domain, edge_length, bbox=om_bbox, min_edge_length=min_deg,
        max_iter=int(cfg.get("max_iter", 40)), seed=seed,
        pfix=(pfix if pfix.shape[0] else None))

    gaps: dict[str, float | None] = {}
    notes: list[str] = []

    def _record(stage: str, nodes) -> None:
        if pfix.shape[0]:
            d, _ = cKDTree(np.asarray(nodes, dtype=float)).query(pfix, k=1)
            gaps[stage] = round(float(d.max()) * mpd, 2)

    # The clean is run as its own passes rather than through mesh_clean so the
    # constrained cut can be measured after each one: a pass that walks the mesh
    # off its breaklines has to be visible, not averaged into a final number.
    _record("generated", points)
    lock = pfix if pfix.shape[0] else None
    quality = float(cfg.get("min_element_qual", 0.01))
    cleaned = False
    if np.asarray(cells).shape[0] > 0:
        try:
            # delete_boundary_faces cannot tell a constrained cut from a sliver -
            # the elements along a punched outline ARE boundary faces - so the pass
            # is kept only while the cut stays where it was locked.
            held = (points, cells)
            points, cells = om.delete_boundary_faces(points, cells,
                                                     min_qual=quality)
            _record("boundary_faces", points)
            if lock is not None and gaps["boundary_faces"] > gaps["generated"]:
                notes.append(
                    "delete_boundary_faces reverted: it moved the constrained cut "
                    "%.1f m, so the sliver removal was declined and the elements "
                    "along the cut stand as generated"
                    % (gaps["boundary_faces"] - gaps["generated"]))
                points, cells = held
                gaps["boundary_faces"] = gaps["generated"]
            points, cells = om.delete_faces_connected_to_one_face(points, cells)
            _record("one_face", points)
            points, cells = om.laplacian2(points, cells, max_iter=20, tol=0.01,
                                          pfix=lock)
            _record("smoothed", points)
            points, cells = om.make_mesh_boundaries_traversable(
                points, cells, min_disconnected_area=0.05)
            _record("traversable", points)
            points, cells, _ = om.fix_mesh(points, cells, delete_unused=True)
            _record("fixed", points)
            cleaned = True
        except Exception as exc:  # noqa: BLE001 -- the pre-clean topology still stands
            notes.append("mesh clean passes stopped: %s" % exc)
            print("mesh clean passes stopped:", exc, flush=True)

    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    used = np.unique(cells)
    if used.shape[0] != points.shape[0]:
        remap = np.full(points.shape[0], -1, dtype=np.int64)
        remap[used] = np.arange(used.shape[0])
        points = points[used]
        cells = remap[cells]
    np.savez(out + "/om2d_mesh.npz", points=points, cells=cells, pfix=pfix)

    tri = points[cells]
    seg = np.sqrt(np.concatenate([
        ((tri[:, 1] - tri[:, 0]) ** 2).sum(1),
        ((tri[:, 2] - tri[:, 1]) ** 2).sum(1),
        ((tri[:, 0] - tri[:, 2]) ** 2).sum(1)])) * mpd
    stats = {
        "engine": "oceanmesh(CHLNDDEV OceanMesh2D port) v%s"
                  % getattr(om, "__version__", "?"),
        "sizing_functions": active,
        "shoreline_smoothed": smoothed,
        "mesh_clean": cleaned,
        "gradation": gradation,
        "seed": seed,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "constrained_points": int(pfix.shape[0]),
        # What each cleanup pass cost the constraint, in metres.
        "pfix_gap_m": gaps,
        "clean_notes": notes,
        "n_points": int(points.shape[0]),
        "n_cells": int(cells.shape[0]),
        "edge_min_m": round(float(seg.min()), 1),
        "edge_median_m": round(float(np.median(seg)), 1),
        "edge_max_m": round(float(seg.max()), 1),
    }
    json.dump(stats, open(out + "/om2d_stats.json", "w"), indent=2)
    print("OM2D_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
