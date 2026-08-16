#!/usr/bin/env python3
"""Paper-style dynamic-resolution inputs for the 2025 rain-on-grid mesh (ADR 0210).

The ADR 0209 RoG path meshes the AOI as a UNIFORM structured grid (one cell size
everywhere). Godara et al. -- the cross-engine comparison the RoG path replicates --
instead ran a graded mesh: a coarse background refined through nested bands down to
the channel scale along breaklines. This module authors those inputs on the host so
the managed-engine driver can build the graded mesh via ``MeshFactory.TryCreateMesh``
(perimeter + variable-density cell-center seeds + channel breaklines) instead of the
structured ``MeshFactory.FromExtent``.

TryCreateMesh signature (decompiled Geospatial.Vectors.MeshFactory):

    bool TryCreateMesh(Polygon perimeter, IList<Point> cellCenters,
                       IList<Polyline> breaklines, out Mesh mesh, out MeshError error,
                       MeshGenerationParams meshParams = null, ProgressReporter = null)

There is NO explicit "refinement region" argument: the realized cell size IS the
local cell-center spacing (the factory Delaunay-triangulates the seeds into a
Voronoi-like cell mesh), and breaklines only MAGNETIZE facepoints onto the channel
(face alignment, ApplyBreaklines/MagnetizeFacepointsEnsureNonColinear). So paper-
style refinement = a graded SEED point cloud (coarse background, fine near channel)
plus channel breaklines. This module builds both, in the driver's LOCAL SI frame.

Local frame (matches rog2025_pipeline.prepare_local_terrain): origin (0,0) at the
terrain SOUTH-WEST, x east / y north, metres; a UTM point (ux,uy) maps to local
(ux - origin_x, uy - origin_y); the mesh domain is [0,W] x [0,H].

CHANNEL SELECTION: the fetched flowlines are the full OSM waterway network (dense
dendritic; Coweeta ~109 km / 28.7 km2 -> ~3.8 km/km2 drainage density, so refining to
EVERY headwater trickle graded-fills nearly the whole catchment). We refine the MAIN
channel network -- the flowlines that drain the delineated catchment -- clipped to the
catchment (buffered by the outer band), which is how a modeler refines the conveyance
channel and what the paper's single-channel refinement targets.

Offline-first: pure numpy/scipy/shapely/geopandas + rasterio (no docker, no server
deps); the driver only READS the emitted seeds.f64 / breaklines.json.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class RefineConfig:
    """Graded-seed sizing (metres). Seeds are variable-radius Poisson-disk sampled:
    the local disk radius (== cell size) is ``channel_m`` at the channel, grading at
    ``grade`` per metre out to ``background_m`` on the hillslopes."""
    background_m: float = 90.0
    channel_m: float = 22.0
    grade: float = 0.13                 # cell-size growth per metre from the channel
    poisson_k: int = 30                 # Bridson candidate tries per active point
    poisson_pack: float = 1.0           # min spacing = pack * max(r_a, r_b)
    max_faces_per_cell: int = 8         # HEC hard cap (cells with >8 sides are rejected)
    repair_iters: int = 16              # max crowding-relief decimation passes
    jitter_seed: int = 20210            # RNG seed (reproducible seed cloud)
    catchment_buffer_m: float = 300.0   # clip channels to catchment buffered by this
    dist_res_m: float = 15.0            # channel distance-field raster step
    use_breaklines: bool = True
    breakline_min_len_m: float = 300.0  # drop tiny tributaries from breaklines (kept as seeds)
    breakline_simplify_m: float = 15.0


@dataclass
class RefineResult:
    n_seeds: int
    n_seeds_in_catchment: int
    n_breaklines: int
    breakline_len_km: float
    channel_len_km: float
    width_m: float
    height_m: float
    size_hist_edges: list          # cell-size (NN spacing) histogram bin edges, m
    size_hist_counts: list         # counts per bin (all seeds)
    size_p5: float
    size_p50: float
    size_p95: float
    seeds_path: str
    breaklines_path: str
    config: dict


def _write_f64_points(path: Path, pts) -> None:
    """Flat little-endian f64 x,y pairs -- the AuthorMesh ReadPts format."""
    import numpy as np
    a = np.asarray(pts, dtype="<f8").reshape(-1, 2)
    path.write_bytes(a.tobytes())


def _channel_local(prep, catchment_geojson, flowlines_path, cfg: RefineConfig):
    """Return (channel_geom_local, catchment_geom_local, transform_to_local)."""
    import geopandas as gpd
    from shapely.geometry import shape
    from shapely.ops import unary_union, transform as shp_transform
    from pyproj import Transformer

    ox, oy, epsg = prep.origin_x, prep.origin_y, prep.utm_epsg
    cg = json.load(open(catchment_geojson))
    feats = cg["features"] if isinstance(cg, dict) and "features" in cg else [cg]
    cgeom = shape(feats[0]["geometry"])
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
    cutm = shp_transform(tr, cgeom)

    def to_local(x, y, z=None):
        import numpy as np
        return (np.asarray(x, dtype=float) - ox, np.asarray(y, dtype=float) - oy)

    cloc = shp_transform(to_local, cutm)
    g = gpd.read_file(flowlines_path).to_crs(epsg)
    near = g[g.intersects(cutm.buffer(50.0))]
    if near.empty:
        raise ValueError("no flowlines intersect the catchment; cannot refine channel")
    chan_utm = unary_union(list(near.geometry)).intersection(cutm.buffer(cfg.catchment_buffer_m))
    chan_loc = shp_transform(to_local, chan_utm)
    return chan_loc, cloc, cutm


def _distance_field(chan_loc, W, H, dr):
    """Rasterize the channel and Euclidean-distance-transform it (metres to channel).

    Grid cell (iy, ix) centre ~ (ix*dr, iy*dr) in local metres (y up)."""
    import numpy as np
    from scipy import ndimage

    nxg = int(W / dr) + 1
    nyg = int(H / dr) + 1
    mask = np.zeros((nyg, nxg), dtype=bool)
    segs = list(chan_loc.geoms) if hasattr(chan_loc, "geoms") else [chan_loc]
    for s in segs:
        if s.length == 0:
            continue
        for t in np.arange(0.0, s.length, dr * 0.5):
            p = s.interpolate(t)
            ix = int(p.x / dr); iy = int(p.y / dr)
            if 0 <= ix < nxg and 0 <= iy < nyg:
                mask[iy, ix] = True
    dist = ndimage.distance_transform_edt(~mask) * dr
    return dist, nxg, nyg


def _gen_seeds(dist, nxg, nyg, W, H, cfg: RefineConfig):
    """Graded cell-center seeds by VARIABLE-RADIUS Poisson-disk (Bridson) sampling.

    Blue-noise seeds give hexagon-like Voronoi cells (~6 neighbours) with NO
    axis-aligned lattice degeneracy, so the HEC mesh robustly stays <= 8 sides/cell
    even across the coarse->fine size transition (the lattice approach spawned many-
    sided cells at the band interface). The local disk radius IS the target cell size
    (``channel_m`` at the channel grading to ``background_m`` on the hillslopes), so
    the realized cell size follows the size field. Deterministic (seeded RNG)."""
    import math
    import random
    import numpy as np

    ch, bg, gr, dr = cfg.channel_m, cfg.background_m, cfg.grade, cfg.dist_res_m
    # precompute the target-size raster once (r_grid[iy, ix] = local cell size, m);
    # the hot loop then reads a float instead of recomputing the size field.
    r_grid = np.clip(ch + gr * dist, ch, bg)        # (nyg, nxg)
    inv_dr = 1.0 / dr
    nxg1, nyg1 = nxg - 1, nyg - 1

    def rad_at(x, y):
        ix = int(x * inv_dr); iy = int(y * inv_dr)
        if ix < 0: ix = 0
        elif ix > nxg1: ix = nxg1
        if iy < 0: iy = 0
        elif iy > nyg1: iy = nyg1
        return r_grid[iy, ix]

    rng = random.Random(cfg.jitter_seed)
    cell = ch / math.sqrt(2.0)                       # hash grid (<=1 pt/cell at r_min)
    inv_cell = 1.0 / cell
    gw = int(W * inv_cell) + 1
    gh = int(H * inv_cell) + 1
    grid = [-1] * (gw * gh)
    PX = []; PY = []; PR = []                         # accepted point coords + radii
    k = cfg.poisson_k
    pack = cfg.poisson_pack                           # spacing = pack * max(r_a, r_b)
    reach = int(math.ceil(bg / cell)) + 1
    TWO_PI = 2.0 * math.pi

    def far_enough(x, y, rp):
        gx = int(x * inv_cell); gy = int(y * inv_cell)
        y0 = gy - reach if gy - reach > 0 else 0
        y1 = gy + reach + 1 if gy + reach + 1 < gh else gh
        x0 = gx - reach if gx - reach > 0 else 0
        x1 = gx + reach + 1 if gx + reach + 1 < gw else gw
        for jy in range(y0, y1):
            base = jy * gw
            for jx in range(x0, x1):
                idx = grid[base + jx]
                if idx < 0:
                    continue
                dx = x - PX[idx]; dy = y - PY[idx]
                rq = PR[idx]
                lim = pack * (rp if rp > rq else rq)
                if dx * dx + dy * dy < lim * lim:
                    return False
        return True

    def add(x, y, rp):
        PX.append(x); PY.append(y); PR.append(rp)
        grid[int(y * inv_cell) * gw + int(x * inv_cell)] = len(PX) - 1
        return len(PX) - 1

    # seed the active list on a coarse scatter so every region gets reached
    active = []
    for _ in range(24):
        x = rng.uniform(0, W); y = rng.uniform(0, H)
        rp = rad_at(x, y)
        if far_enough(x, y, rp):
            active.append(add(x, y, rp))
    while active:
        ai = rng.randrange(len(active))
        pi = active[ai]
        bx = PX[pi]; by = PY[pi]; rp = PR[pi]
        placed = False
        for _ in range(k):
            ang = rng.uniform(0, TWO_PI)
            rad = rp * (1.0 + rng.random())          # annulus [rp, 2rp]
            x = bx + rad * math.cos(ang); y = by + rad * math.sin(ang)
            if x <= 0 or x >= W or y <= 0 or y >= H:
                continue
            rc = rad_at(x, y)
            if far_enough(x, y, rc):
                active.append(add(x, y, rc))
                placed = True
                break
        if not placed:
            active[ai] = active[-1]                   # O(1) swap-remove
            active.pop()

    acc = np.column_stack([PX, PY]) if PX else np.empty((0, 2))
    acc = _degree_repair(acc, W, H, cfg)
    eps = 1e-6 * max(W, H)
    m = (acc[:, 0] > eps) & (acc[:, 0] < W - eps) & (acc[:, 1] > eps) & (acc[:, 1] < H - eps)
    return acc[m]


def _degree_repair(pts, W, H, cfg: RefineConfig):
    """Relieve local crowding so the HEC mesh stays <= 8 sides/cell.

    HEC hard-rejects ANY cell with >8 sides. Blue-noise leaves a handful of over-degree
    cells at the size transition; each pass removes, for every vertex whose Voronoi/Delaunay
    degree exceeds the cap, its NEAREST neighbour (merging the two tightest cells relieves
    the crowding that drives the high degree). HEC's own face-collapse then carries the
    rest to <= 8 (empirically 0 residual on the Coweeta cloud). The near-wall band is
    included -- a shrunk margin (0.4*background) so boundary-adjacent over-degree cells are
    repaired too -- but the true convex-hull vertices (within one background cell of a wall)
    are skipped, since HEC clips them to the fixed rectangle perimeter (their hull degree is
    not the mesh degree). Deterministic + bounded; the DRIVER retries with a seed drop for
    any residual that survives (Driver.cs)."""
    import numpy as np
    from scipy.spatial import Delaunay

    cap = cfg.max_faces_per_cell
    margin = 0.4 * cfg.background_m
    hull = cfg.background_m
    pts = pts.copy()
    for _ in range(cfg.repair_iters):
        tri = Delaunay(pts)
        indptr, indices = tri.vertex_neighbor_vertices
        dw = np.minimum.reduce([pts[:, 0], W - pts[:, 0], pts[:, 1], H - pts[:, 1]])
        check = dw > margin                              # repair down to 0.4*background
        remove = set()
        for i in np.nonzero(check)[0]:
            nb = indices[indptr[i]:indptr[i + 1]]
            if nb.size <= cap:
                continue
            # skip the true hull vertices (HEC clips them); repair the rest
            if dw[i] <= hull and nb.size <= cap + 2:
                continue
            d = (pts[nb, 0] - pts[i, 0]) ** 2 + (pts[nb, 1] - pts[i, 1]) ** 2
            remove.add(int(nb[int(np.argmin(d))]))
        if not remove:
            return pts
        keep = np.ones(len(pts), dtype=bool)
        keep[list(remove)] = False
        pts = pts[keep]
    return pts


def _breaklines_local(chan_loc, cfg: RefineConfig, W, H):
    """Main-stem channel polylines (local coords) for face magnetization -- drop tiny
    tributaries, simplify, clip to the domain."""
    from shapely.geometry import box
    dom = box(0, 0, W, H)
    segs = list(chan_loc.geoms) if hasattr(chan_loc, "geoms") else [chan_loc]
    out = []
    for s in segs:
        if s.length < cfg.breakline_min_len_m:
            continue
        s = s.simplify(cfg.breakline_simplify_m).intersection(dom)
        parts = list(s.geoms) if hasattr(s, "geoms") else [s]
        for p in parts:
            if getattr(p, "geom_type", "") == "LineString" and p.length >= cfg.breakline_min_len_m:
                out.append([[float(x), float(y)] for x, y in p.coords])
    return out


def build_refined_inputs(prep, catchment_geojson, flowlines_path, stage_dir,
                         cfg: RefineConfig | None = None) -> RefineResult:
    """Build + persist graded seeds (seeds.f64) and channel breaklines (breaklines.json)
    in the driver's local SI frame. ``prep`` is a rog2025_pipeline.Rog2025Prep."""
    import numpy as np
    from scipy.spatial import cKDTree
    from shapely.geometry import Point
    from shapely.prepared import prep as sprep

    cfg = cfg or RefineConfig()
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    W, H = prep.width_m, prep.height_m

    chan_loc, cloc, _ = _channel_local(prep, catchment_geojson, flowlines_path, cfg)
    dist, nxg, nyg = _distance_field(chan_loc, W, H, cfg.dist_res_m)
    seeds = _gen_seeds(dist, nxg, nyg, W, H, cfg)
    if len(seeds) < 100:
        raise ValueError(f"refined seed generation produced too few seeds ({len(seeds)})")

    breaklines = _breaklines_local(chan_loc, cfg, W, H) if cfg.use_breaklines else []

    # realized cell-size proxy: nearest-neighbour spacing of the seeds
    nn = cKDTree(seeds).query(seeds, k=2)[0][:, 1]
    edges = [0, 30, 45, 60, 80, 120, 1e9]
    counts, _ = np.histogram(nn, bins=edges)
    pc = sprep(cloc)
    in_catch = int(sum(pc.contains(Point(x, y)) for x, y in seeds))

    seeds_path = stage_dir / "seeds.f64"
    _write_f64_points(seeds_path, seeds)
    bl_path = stage_dir / "breaklines.json"
    bl_path.write_text(json.dumps(breaklines))

    bl_len = sum(
        sum(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(pl[:-1], pl[1:]))
        for pl in breaklines)

    return RefineResult(
        n_seeds=int(len(seeds)), n_seeds_in_catchment=in_catch,
        n_breaklines=len(breaklines), breakline_len_km=round(bl_len / 1000.0, 2),
        channel_len_km=round(chan_loc.length / 1000.0, 2),
        width_m=float(W), height_m=float(H),
        size_hist_edges=[float(e) for e in edges],
        size_hist_counts=[int(c) for c in counts],
        size_p5=round(float(np.percentile(nn, 5)), 1),
        size_p50=round(float(np.percentile(nn, 50)), 1),
        size_p95=round(float(np.percentile(nn, 95)), 1),
        seeds_path=str(seeds_path), breaklines_path=str(bl_path),
        config=asdict(cfg))
