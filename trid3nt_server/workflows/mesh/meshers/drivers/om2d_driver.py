"""In-container OceanMesh2D driver for the ``om2d`` mesher.

Runs INSIDE ``trid3nt-local/mesh:latest``, the only place the CHLNDDEV
``oceanmesh`` port (OceanMesh2D, GPL-3) is installed. The host mesher mounts this
file and a rundir and shells it; nothing here imports trid3nt code.

THE RECIPE'S OPS TRAVEL AS DATA. The host sends a list of ``{fn, kwargs}`` and
this file calls each one VERBATIM on the library, in the order it was declared.
Because the library lives here, THIS is where the signature is the schema: every
parameter the recipe left unstated is filled from the staged environment by NAME,
a required parameter the environment cannot supply is refused by name, and the
library's own error surfaces verbatim on anything else.

Contract (host <-> container over the mounted /data dir):
  argv[1] = <op>   argv[2] = /data/<config>.json   argv[3] = /data

  build   config: bbox, EITHER shoreline_shp OR domain_geojson,
          min_edge_length_m, max_edge_length_m, seed, max_iter,
          pre_ops [{fn, kwargs}], post_ops [{fn, kwargs}].
          Stages the domain, runs the pre ops into a sizing function, generates,
          runs the post ops, and emits /data/om2d_mesh.npz (points (N,2) lon/lat,
          cells (M,3) 0-based, pfix (K,2)) plus /data/om2d_stats.json.

  post    config: mesh_npz (points, cells, optional bed), min_edge_length_m,
          max_edge_length_m, ops [{fn, kwargs}]. Runs the ops over that mesh and
          emits <config stem>.npz + <config stem>.json (the per-op results).

UNITS. The library works in DEGREES at the domain's own latitude; everything
above it works in metres. The parameters in :data:`_METRE_PARAMS` are therefore
written in METRES by a recipe and converted here, whether they were stated by the
author or threaded from ``resolution_m``. Every conversion is reported in the
stats, so a number the author never wrote is never silently in force.

The domain is EITHER the water side of a GSHHG shoreline or the interior of a
supplied polygon. The shoreline path is the library's own ``Shoreline`` ->
``signed_distance_function`` chain on its sizing GRID. The polygon path cannot
use ``Shoreline`` - it meshes only water touching the region boundary and cannot
mesh a fully enclosed interior - so the signed distance is measured against the
polygon's own densified boundary. Both paths triangulate through the authentic
``om.generate_mesh``, so a polygon domain is not a second mesher.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import math
import sys

import numpy as np
import oceanmesh as om
from scipy.spatial import cKDTree
from shapely import contains_xy
from shapely.geometry import shape as _shape
from shapely.ops import unary_union


#: How wide the finest-edge band around an obstacle outline is, in units of that
#: finest edge. One edge is the narrowest band a triangle can actually resolve.
_OBSTACLE_BAND_EDGES = 1.0

#: The band a SIZED rim is expected in, as a factor either side of the ask. A
#: relaxation places nodes, it does not lay them out: an edge between two nodes it
#: settled runs a little under and a little over whatever the lattice asked for,
#: and twice the ask is the spread a triangulation at one size word actually
#: holds. Reported against every build, asserted by none - a rim NO op sized runs
#: far past this, and saying so is the point.
_RIM_TOLERANCE = 2.0

#: The parameters a recipe writes in METRES and the library reads in degrees.
_METRE_PARAMS = ("min_edge_length", "min_edgelength", "max_edge_length")

#: The parameters whose staged raster path becomes one of the library's own DEM
#: objects before the call. The container half of the typed conversion layer.
_DEM_PARAMS = ("dem",)

#: The ops whose own precondition is ONE closed boundary walk, so the driver
#: offers each connected piece of the mesh its own call rather than handing the
#: function a domain a shoreline cut into two water bodies.
_PER_COMPONENT = ("identify_ocean_boundary_sections",)

#: What the mesh arrays answer to across the library's own clean passes: the same
#: two arrays under the names each function happens to give them.
_POINT_PARAMS = ("vertices", "points", "p")
_CELL_PARAMS = ("entities", "faces", "cells", "t")


class _EmptyAfterOp(Exception):
    """An op took the last element; the empty-mesh refusal states which one."""


class _Refusal(Exception):
    """A typed refusal the HOST re-raises under its own code.

    The refusals this driver owes are about the DOMAIN, and the domain is only
    knowable where the library is - so the code, the reason and the call that
    does what the refused ask could not travel back as a document rather than as
    a return code the host has to guess a meaning for.
    """

    def __init__(self, code: str, message: str, escalation: dict | None = None
                 ) -> None:
        super().__init__(message)
        self.document = {"code": code, "message": message,
                         "escalation": escalation}


def _seed_library_randomness(seed: int) -> None:
    """Bind the recipe's seed onto the one library call that draws without it.

    ``feature_sizing_function`` skeletonizes the shoreline through skimage's
    ``medial_axis``, which breaks ties between equidistant skeleton pixels from a
    generator it creates FRESH per process unless it is handed one. The library
    passes none, so an identical recipe returns a different skeleton, a different
    sizing lattice and a different mesh on every rebuild - measured on a
    shoreline-cut coastal domain as three distinct meshes from three identical
    configs. The seed is bound onto the module the library calls it through
    because the library exposes no parameter to reach it by; the function itself
    is the library's own, unmodified.
    """
    from skimage.morphology import medial_axis

    om.edgefx.medial_axis = functools.partial(medial_axis, rng=int(seed))


def _typed(points, cells) -> tuple[np.ndarray, np.ndarray]:
    """A mesh in the dtypes every op and every reader below indexes with."""
    return np.asarray(points, dtype=float), np.asarray(cells, dtype=np.int64)


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
    """The INDICES of points no closer together than ``spacing``, in input order."""
    kept: list[int] = []
    for index, point in enumerate(points):
        if not kept or np.min(
                np.hypot(*(np.asarray(points)[kept] - point).T)) >= spacing:
            kept.append(index)
    return np.asarray(kept, dtype=np.int64)


class _Holes(om.Domain):
    """The obstacle union as an oceanmesh domain: a signed distance in degrees.

    Being a ``Domain`` is what lets ``om.Difference`` subtract it from the
    shoreline domain with the library's own set algebra.
    """

    def __init__(self, geoms, step: float, bbox) -> None:
        self.union = unary_union(geoms)
        pts: list[tuple[float, float]] = []
        for geom in geoms:
            for ring in _outline_coords(geom):
                pts.extend(_resample(ring, step))
        # Thinned to HALF the finest edge, not to it: a breakline is followed
        # exactly, and locking its vertices no closer than the finest edge cost
        # measured conformal accuracy (an outline vertex 33 m from the nearest
        # node at a 20 m ask, against 23 m at half). The edges along a punched
        # outline therefore run under the size word by design - they are the cut
        # the author asked for, not the rim the ask sizes.
        raw = np.asarray(pts, dtype=float)
        self.outline = raw[_thin(raw, 0.5 * step)]
        self.tree = cKDTree(self.outline)
        super().__init__(bbox, self.signed)

    def signed(self, x: np.ndarray) -> np.ndarray:
        xq = np.nan_to_num(np.asarray(x, dtype=float), nan=1.0e9)
        d, _ = self.tree.query(xq, k=1)
        inside = contains_xy(self.union, xq[:, 0], xq[:, 1])
        return np.where(inside, -d, d)


class _PolygonDomain(om.Domain):
    """A supplied polygon as an oceanmesh domain: signed distance, negative inside.

    Being a ``Domain`` is what lets the polygon interior reach the same
    ``om.generate_mesh`` and the same ``om.Difference`` the shoreline path uses.
    The boundary is densified before the KD-tree is built so the distance field
    is smooth between vertices rather than stepping from one corner to the next.
    """

    def __init__(self, geoms, step: float, bbox) -> None:
        self.union = unary_union(geoms)
        pts: list[tuple[float, float]] = []
        for geom in geoms:
            for ring in _outline_coords(geom):
                pts.extend(_resample(ring, 0.5 * step))
        self.outline = np.asarray(pts, dtype=float)
        self.tree = cKDTree(self.outline)
        super().__init__(bbox, self.signed)

    def signed(self, x: np.ndarray) -> np.ndarray:
        xq = np.nan_to_num(np.asarray(x, dtype=float), nan=1.0e9)
        d, _ = self.tree.query(xq, k=1)
        inside = contains_xy(self.union, xq[:, 0], xq[:, 1])
        return np.where(inside, -d, d)


# --------------------------------------------------------------------------- #
# The build state the pre ops shape.
# --------------------------------------------------------------------------- #
class _Build:
    """What the pre ops act on: the domain, the sizing stack, the obstacles.

    A sizing SOURCE (a function that takes no ``grid``) is pushed onto the stack;
    a sizing TRANSFORM (one that does) consumes the stack's minimum and replaces
    it. With nothing on the stack the domain is meshed uniformly at the one size
    word, which is what an ask that declared no sizing op asked for.
    """

    def __init__(self, cfg: dict) -> None:
        xmin, ymin, xmax, ymax = (float(v) for v in cfg["bbox"])
        self.bbox = (xmin, xmax, ymin, ymax)
        self.mpd = _m_per_deg(0.5 * (ymin + ymax))
        self.min_deg = float(cfg["min_edge_length_m"]) / self.mpd
        self.max_deg = float(cfg["max_edge_length_m"]) / self.mpd
        self.region = om.Region(extent=self.bbox, crs="EPSG:4326")
        self.notes: list[str] = []
        self.active: list[str] = []
        self.threaded: dict[str, dict] = {}
        self.sizing: list = []
        self.holes = None
        self.hole_geoms: list = []
        self.rim = np.empty((0, 2), dtype=float)
        self.rim_walk = np.empty(0, dtype=np.int64)
        self.rim_target = None
        self.shoreline = None
        self.smoothed = None
        self.domain_rings: list = []
        if cfg.get("domain_geojson"):
            geoms = _load_geoms(cfg["domain_geojson"])
            self.sdf = _PolygonDomain(geoms, self.min_deg, self.bbox)
            self.domain_rings = [ring for geom in geoms
                                 for ring in _outline_coords(geom)]
            self.active.append("polygon_sdf(interior)")
        else:
            self.smoothed = True
            try:
                self.shoreline = om.Shoreline(
                    cfg["shoreline_shp"], self.region.bbox, self.min_deg)
            except Exception:  # noqa: BLE001
                # The shoreline smoothing moving-average throws a GEOS
                # side-location conflict on some GSHHG geometries; the unsmoothed
                # shoreline still meshes.
                self.smoothed = False
                self.shoreline = om.Shoreline(
                    cfg["shoreline_shp"], self.region.bbox, self.min_deg,
                    smooth_shoreline=False)
            if not (len(self.shoreline.mainland) or len(self.shoreline.inner)):
                raise _Refusal(
                    "MESH_SHORELINE_DOES_NOT_DESCRIBE_EXTENT",
                    "the shoreline carries no land boundary over the extent "
                    f"{cfg['bbox']}, so there is nothing here to cut water from: "
                    "the signed distance falls back to the box itself and the "
                    "whole extent - streets included - meshes as open water. "
                    "GSHHG L1 describes the boundary between land and OCEAN, so "
                    "a lake, a reservoir or an inland water body is not in it. "
                    "Fetch the water body and mesh ITS polygon: build_mesh takes "
                    "a polygon extent and cuts the same domain from its interior.",
                    {"tool": "fetch_nhd_waterbodies",
                     "overrides": {"bbox": list(cfg["bbox"])}})
            self.sdf = om.signed_distance_function(self.shoreline)
            # The RIM on this path is the extent's own box: the shoreline is the
            # land boundary and the sizing ops are what shape it, while the box
            # edges are where the water simply continues past the ask.
            self.domain_rings = [[(xmin, ymin), (xmax, ymin), (xmax, ymax),
                                  (xmin, ymax), (xmin, ymin)]]
            self.active.append("shoreline_sdf(GSHHG)")

    # -- the environment a pre op's unstated parameters are filled from ---- #
    def environment(self) -> dict:
        return {
            "shoreline": self.shoreline,
            "signed_distance_function": self.sdf,
            "bbox": self.bbox,
            "region": self.region,
            "grid": self.combined(),
            "edge_lengths": list(self.sizing),
            "min_edge_length": self.min_deg,
            "min_edgelength": self.min_deg,
            "max_edge_length": self.max_deg,
        }

    def combined(self):
        """The one sizing object the stack currently amounts to, or ``None``."""
        if not self.sizing:
            return None
        if len(self.sizing) == 1:
            return self.sizing[0]
        return om.compute_minimum(self.sizing)

    def edge_length(self):
        """What ``generate_mesh`` is handed: the sizing stack, or the uniform word."""
        combined = self.combined()
        if combined is not None:
            if len(self.sizing) > 1:
                self.active.append("compute_minimum(%d sizing functions)"
                                   % len(self.sizing))
            return combined
        self.active.append("uniform(min_edge)")
        holes = self.holes
        min_deg, max_deg = self.min_deg, self.max_deg

        def uniform(x: np.ndarray) -> np.ndarray:
            xq = np.nan_to_num(np.asarray(x, dtype=float), nan=1.0e9)
            h = np.full(xq.shape[0], min_deg)
            if holes is not None:
                near = np.abs(holes.signed(xq))
                h = np.where(near <= _OBSTACLE_BAND_EDGES * min_deg, min_deg, h)
            return np.clip(h, min_deg, max_deg)

        return uniform

    def domain(self):
        """The domain ``generate_mesh`` cuts from, obstacles subtracted."""
        if self.holes is None:
            return self.sdf
        return om.Difference([self.sdf, self.holes])

    def pfix(self) -> np.ndarray:
        """The outline vertices DistMesh locks, inside the domain only.

        A constrained vertex outside the domain would pin a node the domain
        excludes, so only the outline inside it is locked. Two outlines can be
        locked - an obstacle punched out of the water, and the domain's own rim
        when a recipe sized it - and they lock the same way.
        """
        locked = [self.rim]
        if self.holes is not None:
            outline = self.holes.outline
            locked.append(outline[self.sdf.eval(outline) < 0.0])
        stacked = np.vstack(locked) if any(a.shape[0] for a in locked) \
            else np.empty((0, 2), dtype=float)
        return stacked


# --------------------------------------------------------------------------- #
# om2d's OWN pre primitives.
# --------------------------------------------------------------------------- #
def set_obstacle(build: _Build, geometry: str, constrain: bool = True) -> None:
    """Punch a geometry out of the domain and lock its outline into the mesh.

    The cut can only follow the outline if the mesh is fine enough there to hold
    it, so the band around the outline is SEEDED at the finest edge on whatever
    sizing the recipe has built so far; the growth away from it is
    ``enforce_mesh_gradation``'s job and belongs in the recipe after this.
    """
    build.hole_geoms.extend(_load_geoms(geometry))
    build.holes = _Holes(build.hole_geoms, build.min_deg, build.bbox)
    if not constrain:
        build.holes.outline = np.empty((0, 2), dtype=float)
    grid = build.combined()
    if grid is not None and hasattr(grid, "create_grid"):
        _seed(grid, build, lambda flat: np.abs(build.holes.signed(flat))
              <= _OBSTACLE_BAND_EDGES * build.min_deg, build.min_deg)
    build.active.append("obstacle(%d part(s),constrain=%s)"
                        % (len(build.hole_geoms), bool(constrain)))


def set_region_size(build: _Build, geometry: str, edge_length_m: float) -> None:
    """Write a target edge inside a drawn region onto the current sizing lattice.

    Onto the lattice rather than into the triangulator, so the transition into the
    region obeys the same gradation the rest of the mesh does instead of being a
    discontinuity DistMesh has to absorb.
    """
    grid = build.combined()
    if grid is None or not hasattr(grid, "create_grid"):
        raise ValueError(
            "set_region_size writes onto a sizing lattice and this recipe has "
            "built none yet; declare a sizing op before it, or size the whole "
            "domain with resolution_m")
    geom = unary_union(_load_geoms(geometry))
    target = float(edge_length_m) / build.mpd
    _seed(grid, build, lambda flat: contains_xy(geom, flat[:, 0], flat[:, 1]),
          target)
    build.active.append("region(edge_length=%.0fm)" % float(edge_length_m))


def set_rim_size(build: _Build, edge_length_m: float | None = None,
                 constrain: bool = True) -> None:
    """Size the DOMAIN RIM at a declared edge and lock it into the mesh.

    Nothing else sizes the rim. A sizing function measures the SHORELINE - the
    feature width, the distance to a line, the wavelength over a depth - and the
    extent's own box is none of those, so the lattice there falls to the coarsest
    edge and the rim comes back an order of magnitude past the ask. That is the
    boundary a solver forces its open condition on, so its spacing is the ask's
    to state.

    ``edge_length_m`` defaults to the recipe's own size word. ``constrain``
    locks the resampled rim as mesh nodes, which is what makes the spacing a
    fact rather than a target the relaxation may drift off; the passes that move
    a constrained cut decline themselves, as they do around an obstacle.

    ORDER: after the sizing ops and before the gradation. The rim's edge is
    written onto the lattice the sizing ops built, and a gradation after it is
    what turns the step from the rim into the interior into a slope instead of a
    fan of slivers.
    """
    if not build.domain_rings:
        raise ValueError(
            "set_rim_size sizes the domain's own outline and this domain states "
            "none; it is the extent's box or a supplied polygon's rings")
    target = (build.min_deg if edge_length_m is None
              else float(edge_length_m) / build.mpd)
    ring: list[tuple[float, float]] = []
    # A WALK POSITION per outline point, with a gap between rings so no two
    # points of different rings ever read as neighbours. It is what lets the
    # measurement tell a rim EDGE from a chord across the land the domain cut
    # out of the rim, whose two ends are both rim points and are not neighbours.
    walk: list[int] = []
    cursor = 0
    for coords in build.domain_rings:
        piece = _resample(list(coords), target)
        ring.extend(piece)
        walk.extend(range(cursor, cursor + len(piece)))
        cursor += len(piece) + 2
    points = np.asarray(ring, dtype=float)
    keep = _thin(points, target)
    points, positions = points[keep], np.asarray(walk, dtype=np.int64)[keep]
    build.rim_target = target
    if constrain:
        # Not strictly inside: a rim point IS the boundary, so on a supplied
        # polygon its signed distance is zero and a strict test drops the whole
        # outline. Half an edge outside is the band a node can be pulled back
        # from; anything past it is the land a shoreline cut out of the rim.
        wet = build.sdf.eval(points) < 0.5 * target
        build.rim, build.rim_walk = points[wet], positions[wet]
    else:
        build.rim = np.empty((0, 2), dtype=float)
        build.rim_walk = np.empty(0, dtype=np.int64)
    grid = build.combined()
    if grid is not None and hasattr(grid, "create_grid"):
        tree = cKDTree(points)
        _seed(grid, build, lambda flat: tree.query(flat, k=1)[0] <= target,
              target)
    else:
        # Locked but not SIZED: with no lattice built yet there is nothing to
        # write the rim's edge onto, so the elements behind the rim keep whatever
        # the ask and the ceiling gave them and the transition is a fan of
        # slivers. Declare this op after the sizing ops and before the gradation.
        build.notes.append(
            "set_rim_size locked the rim at %.0f m but sized no lattice: no "
            "sizing op had built one yet, so the elements behind the rim are "
            "not graded into it" % (target * build.mpd))
    build.active.append("rim(edge_length=%.0fm,constrained=%d)"
                        % (target * build.mpd, int(build.rim.shape[0])))


def _seed(grid, build: _Build, inside, target: float) -> None:
    """Write ``target`` into a sizing lattice wherever ``inside`` says, in place."""
    xg, yg = grid.create_grid()
    flat = np.column_stack([xg.ravel(), yg.ravel()])
    values = np.asarray(grid.values, dtype=float)
    mask = np.asarray(inside(flat)).reshape(xg.shape)
    values = np.where(mask, np.minimum(values, target), values)
    values = np.clip(values, build.min_deg, build.max_deg)
    grid.values = values
    grid.hmin = float(np.nanmin(values[np.isfinite(values) & (values > 0)]))
    grid.build_interpolant()


_PRIMITIVES = {"set_obstacle": set_obstacle,
               "set_region_size": set_region_size,
               "set_rim_size": set_rim_size}


# --------------------------------------------------------------------------- #
# Calling one op verbatim.
# --------------------------------------------------------------------------- #
def _resolve(name: str):
    """The callable behind an op name: our primitive, else the library's own."""
    if name in _PRIMITIVES:
        return _PRIMITIVES[name]
    fn = getattr(om, name, None)
    if fn is None or not callable(fn):
        raise ValueError(
            "oceanmesh has no function %r (this driver runs %s)"
            % (name, getattr(om, "__version__", "?")))
    return fn


def _bind(fn, kwargs: dict, env: dict, mpd: float,
          report: dict) -> dict:
    """Fill what the recipe left unstated from the environment -> the real call.

    THE SIGNATURE IS THE SCHEMA. A parameter the recipe stated is used as
    written; one it did not is taken from the environment when the environment
    has it; one that is required and neither is refused BY NAME, naming what the
    environment does stage.
    """
    signature = inspect.signature(fn)
    bound = {}
    threaded: dict[str, object] = {}
    for name, value in kwargs.items():
        bound[name] = _as_library_value(name, value, mpd, env)
    for name, prm in signature.parameters.items():
        if name in bound or prm.kind in (prm.VAR_POSITIONAL, prm.VAR_KEYWORD):
            continue
        if env.get(name) is not None:
            bound[name] = env[name]
            if name in _METRE_PARAMS:
                threaded[name] = round(float(env[name]) * mpd, 3)
            continue
        if prm.default is prm.empty:
            raise ValueError(
                "%s needs %r and neither the recipe nor this domain supplies it "
                "(the domain stages %s)"
                % (getattr(fn, "__name__", "the op"), name,
                   sorted(k for k, v in env.items() if v is not None)))
    if threaded:
        report["threaded_m"] = threaded
    try:
        signature.bind(**bound)
    except TypeError as exc:
        raise ValueError("%s: %s" % (getattr(fn, "__name__", "the op"), exc)) from exc
    return bound


def _as_library_value(name: str, value, mpd: float, env: dict):
    """One stated kwarg in the units and the type the library reads it in."""
    if name in _METRE_PARAMS and isinstance(value, (int, float)):
        return float(value) / mpd
    if name in _DEM_PARAMS and isinstance(value, str):
        if env.get("region") is None:
            raise ValueError(
                "a staged raster becomes one of the library's own DEM objects "
                "over the domain's REGION, and the ops that run after generation "
                "are handed a mesh rather than a domain; declare %r on a sizing "
                "op instead" % name)
        return om.DEM(value, bbox=env["region"])
    return value


def _is_mesh(result) -> bool:
    """Did this op hand back a triangulation rather than a measurement?"""
    return (isinstance(result, (tuple, list)) and len(result) >= 2
            and isinstance(result[0], np.ndarray) and result[0].ndim == 2
            and result[0].shape[1] == 2
            and isinstance(result[1], np.ndarray) and result[1].ndim == 2
            and result[1].shape[1] in (3, 4))


# --------------------------------------------------------------------------- #
# build.
# --------------------------------------------------------------------------- #
def op_build(cfg: dict, out: str) -> int:
    _seed_library_randomness(int(cfg.get("seed", 0)))
    build = _Build(cfg)
    reports: list[dict] = []

    for entry in cfg.get("pre_ops") or []:
        name = str(entry["fn"])
        fn = _resolve(name)
        report = {"op": name}
        if name in _PRIMITIVES:
            fn(build, **dict(entry.get("kwargs") or {}))
            reports.append(report)
            continue
        env = build.environment()
        transform = "grid" in inspect.signature(fn).parameters \
            or "edge_lengths" in inspect.signature(fn).parameters
        bound = _bind(fn, dict(entry.get("kwargs") or {}), env, build.mpd, report)
        result = fn(**bound)
        build.sizing = [result] if transform else [*build.sizing, result]
        report["kind"] = "transform" if transform else "source"
        reports.append(report)
        build.active.append(name)

    edge_length = build.edge_length()
    pfix = build.pfix()
    points, cells = _typed(*om.generate_mesh(
        build.domain(), edge_length, bbox=build.bbox,
        min_edge_length=build.min_deg, max_iter=int(cfg.get("max_iter", 40)),
        seed=int(cfg.get("seed", 0)),
        pfix=(pfix if pfix.shape[0] else None)))

    gaps: dict[str, float | None] = {}

    def record(stage: str, nodes) -> None:
        if pfix.shape[0]:
            d, _ = cKDTree(np.asarray(nodes, dtype=float)).query(pfix, k=1)
            gaps[stage] = round(float(d.max()) * build.mpd, 2)

    def guard(stage: str, nodes) -> bool:
        """Did this pass walk the mesh off the outline it was constrained to?

        ``delete_boundary_faces`` cannot tell a constrained cut from a sliver -
        the elements along a punched outline ARE boundary faces - so a pass that
        moves the cut is declined and the elements along it stand as generated.
        """
        record(stage, nodes)
        if not pfix.shape[0] or gaps[stage] <= gaps["generated"]:
            return False
        build.notes.append(
            "%s reverted: it moved the constrained cut %.1f m, so the change was "
            "declined and the elements along the cut stand as generated"
            % (stage, gaps[stage] - gaps["generated"]))
        gaps[stage] = gaps["generated"]
        return True

    record("generated", points)
    env_extra = {"pfix": (pfix if pfix.shape[0] else None)}
    points, cells, _ = _run_mesh_ops(
        cfg.get("post_ops") or [], points, cells, None, build.mpd, reports,
        env_extra, on_pass=guard, notes=build.notes)

    if cells.shape[0] == 0:
        # Every number below reduces over the elements, so a generation that
        # yielded none reaches the caller as a zero-size numpy reduction rather
        # than as what it is: an edge length the domain cannot hold.
        raise ValueError(
            "the domain came back with NO elements at the declared resolution "
            f"(min_edge_length_m={cfg['min_edge_length_m']}, "
            f"max_edge_length_m={cfg['max_edge_length_m']})"
            + ("; " + "; ".join(build.notes) if build.notes else "")
            + "; declare an edge length this domain can hold, or a domain that "
            "holds this edge length")
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
        ((tri[:, 0] - tri[:, 2]) ** 2).sum(1)])) * build.mpd
    stats = {
        "rim_edge_length_m": _rim_edges(points, cells, build),
        "engine": "oceanmesh(CHLNDDEV OceanMesh2D port) v%s"
                  % getattr(om, "__version__", "?"),
        "sizing_functions": build.active,
        "ops": reports,
        # None on the polygon path: there is no shoreline to have smoothed.
        "shoreline_smoothed": build.smoothed,
        "seed": int(cfg.get("seed", 0)),
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "constrained_points": int(pfix.shape[0]),
        # What each post op cost the constraint, in metres.
        "pfix_gap_m": gaps,
        "clean_notes": build.notes,
        "n_points": int(points.shape[0]),
        "n_cells": int(cells.shape[0]),
        "edge_min_m": round(float(seg.min()), 1),
        "edge_median_m": round(float(np.median(seg)), 1),
        "edge_max_m": round(float(seg.max()), 1),
    }
    json.dump(stats, open(out + "/om2d_stats.json", "w"), indent=2)
    print("OM2D_OK", json.dumps(stats)[:4000])
    return 0


def _rim_edges(points, cells, build: _Build) -> dict:
    """The RIM's edge lengths against the ask, in metres.

    The rim is where a solver forces its open condition, and every sizing
    function the library has measures the SHORELINE rather than the extent, so
    how far the rim ran from the ask is a number every build reports rather than
    a question a reader has to go and measure.

    WHICH edges: the ones on the outline a rim op sized, when one did - a domain
    boundary is part rim and part shoreline, and holding the shoreline to the
    rim's ask would report the land as a rim failure. With no rim op the whole
    boundary is measured, because none of it was sized and all of it is the ask's
    to answer for.
    """
    xy = np.asarray(points, dtype=float)
    counts: dict[tuple[int, int], int] = {}
    for cell in cells:
        for a, b in ((cell[0], cell[1]), (cell[1], cell[2]), (cell[2], cell[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            counts[key] = counts.get(key, 0) + 1
    edges = [(a, b) for (a, b), n in counts.items() if n == 1]
    measured = "every boundary edge (no rim op sized this domain)"
    if build.rim.shape[0]:
        _, nearest = cKDTree(xy).query(build.rim, k=1)
        at = {int(node): int(position)
              for node, position in zip(nearest, build.rim_walk)}
        edges = [(a, b) for a, b in edges
                 if a in at and b in at and abs(at[a] - at[b]) == 1]
        measured = "the boundary edges on the outline the rim op sized"
    lengths = np.array([float(np.hypot(*(xy[b] - xy[a]))) * build.mpd
                        for a, b in edges])
    if lengths.size == 0:
        return {}
    asked = (build.min_deg if build.rim_target is None
             else build.rim_target) * build.mpd
    return {
        "asked_m": round(float(asked), 2),
        "edges": int(lengths.shape[0]),
        "min_m": round(float(lengths.min()), 2),
        "median_m": round(float(np.median(lengths)), 2),
        "max_m": round(float(lengths.max()), 2),
        "over_ask_median": round(float(np.median(lengths)) / asked, 2),
        "over_ask_max": round(float(lengths.max()) / asked, 2),
        "measured": measured,
        "tolerance": _RIM_TOLERANCE,
        "within_tolerance": bool(
            lengths.min() >= asked / _RIM_TOLERANCE
            and lengths.max() <= asked * _RIM_TOLERANCE),
    }


def _run_mesh_ops(ops, points, cells, bed, mpd: float, reports: list,
                  env_extra: dict, *, on_pass=None,
                  notes: list) -> tuple[np.ndarray, np.ndarray, dict]:
    """Run the ops that act on a generated mesh, verbatim, in declared order.

    An op that hands back a triangulation replaces the mesh; one that hands back
    a measurement is RECORDED under its own name and the mesh stands. An op that
    removes the last element stops the chain and says which one did it - half a
    clean is not a mesh, and a note about it reads afterwards as a mesh that was
    merely cleaned less.
    """
    results: dict = {}
    for entry in ops:
        name = str(entry["fn"])
        fn = _resolve(name)
        report = {"op": name}
        held = (points, cells)
        kwargs = dict(entry.get("kwargs") or {})
        try:
            if name in _PER_COMPONENT:
                result = _over_components(fn, kwargs, points, cells, bed, mpd,
                                          report, env_extra)
            else:
                env = {**dict.fromkeys(_POINT_PARAMS, points),
                       **dict.fromkeys(_CELL_PARAMS, cells),
                       "topobathymetry": bed, **env_extra}
                result = fn(**_bind(fn, kwargs, env, mpd, report))
        except Exception as exc:  # noqa: BLE001 -- re-raised as a typed refusal
            raise ValueError(
                "the op %r stopped inside the library, so the mesh is left "
                "partially processed and is refused rather than solved: %r%s"
                % (name, exc, "; " + "; ".join(notes) if notes else "")) from exc
        if _is_mesh(result):
            points, cells = _typed(result[0], result[1])
            if cells.shape[0] == 0:
                notes.append("%s removed the last element" % name)
                raise _EmptyAfterOp(name)
            if on_pass is not None and on_pass(name, points):
                points, cells = held
                report["reverted"] = True
            report["nodes"] = int(points.shape[0])
            report["elements"] = int(cells.shape[0])
        else:
            results[name] = _measurement(name, result, points, cells, bed)
            report["measured"] = True
        reports.append(report)
    return points, cells, results


def _measurement(name: str, result, points, cells, bed) -> object:
    """One measuring op's result, as the neutral record the host reads back."""
    if name in _PER_COMPONENT:
        return result
    return result if isinstance(result, (int, float, str, bool, type(None))) \
        else np.asarray(result).tolist()


def _over_components(fn, kwargs: dict, points, cells, bed, mpd: float,
                     report: dict, env_extra: dict) -> list[dict]:
    """Call a boundary-walk op once per boundary WALK -> the sections it found.

    Its own precondition, honoured rather than compensated for: the walk it
    indexes into traces ONE closed contour, and a domain has as many as its
    topology gives it - a shoreline cut can come back as two water bodies in one
    array, and an obstacle punched out of one of them adds a rim around the hole.
    The library starts its walk at whichever boundary edge sorts first, so on a
    holed domain WHICH rim it traced was an accident of node numbering and every
    other rim was invisible to it.

    Each connected piece is therefore decomposed into its contours and offered one
    identification PER CONTOUR, each walked from a declared start node; the runs
    come back in the whole mesh's numbering. Contours are offered outer rim first
    (by enclosed area), which is the order a reader checks them in.
    """
    out: list[dict] = []
    pieces = _components(cells, points.shape[0])
    report["components"] = len(pieces)
    report["contours"] = 0
    for mask in pieces:
        kept = np.unique(cells[mask])
        remap = np.full(points.shape[0], -1, dtype=np.int64)
        remap[kept] = np.arange(kept.shape[0])
        sub_cells = remap[cells[mask]]
        sub_points = points[kept]
        sub_bed = None if bed is None else np.asarray(bed)[kept]
        env = {**dict.fromkeys(_POINT_PARAMS, sub_points),
               **dict.fromkeys(_CELL_PARAMS, sub_cells),
               "topobathymetry": sub_bed, **env_extra}
        for rim, first in enumerate(_contour_starts(sub_cells, sub_points)):
            report["contours"] += 1
            with _walk_from(first):
                ends = fn(**_bind(fn, kwargs, env, mpd, report))
                out += [{**s, "rim": rim}
                        for s in _sections(ends, sub_points, sub_cells, sub_bed,
                                           kept, first)]
    return out


@contextlib.contextmanager
def _walk_from(first: int):
    """Run the block with the library's boundary walk started at ``first``.

    ``get_winded_boundary_edges`` takes the start vertex as ``vFirst`` and returns
    the ONE contour it walks from there; ``identify_ocean_boundary_sections`` calls
    it without one, so the start - and with it the contour - falls to whichever
    boundary edge the library's unique-row sort put first. The parameter is bound
    on the module the caller reaches it through, because the caller exposes no way
    to pass it; the walk itself is the library's own.
    """
    import oceanmesh.boundary as boundary

    original = boundary.get_winded_boundary_edges
    boundary.get_winded_boundary_edges = functools.partial(
        original, vFirst=int(first))
    try:
        yield
    finally:
        boundary.get_winded_boundary_edges = original


def _contour_starts(cells, points) -> list[int]:
    """One start node per boundary contour, outer rim first.

    The decomposition is the library's own walk asked repeatedly: from the lowest
    unvisited boundary node it returns exactly the contour that node lies on, so
    walking every contour once covers the whole boundary and the choice of start
    is a stated rule rather than a sort artifact. The order is by ENCLOSED AREA -
    the rim around a hole encloses less than the domain rim, however many nodes
    either of them happens to carry.
    """
    from oceanmesh.edges import get_boundary_edges, get_winded_boundary_edges

    remaining = set(int(n) for n in np.unique(get_boundary_edges(cells)))
    found: list[tuple[float, int]] = []
    while remaining:
        first = min(remaining)
        walk = _walk_nodes(get_winded_boundary_edges(cells, vFirst=first))
        remaining -= set(walk)
        xy = np.asarray(points, dtype=float)[walk]
        area = 0.5 * abs(float(np.sum(
            xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1])))
        found.append((area, first))
    return [first for _area, first in sorted(found, reverse=True)]


def _walk_nodes(winded) -> list[int]:
    """The nodes of one winded edge walk, in walk order, each once."""
    flat = np.asarray(winded).flatten()
    first_seen = np.unique(flat, return_index=True)[1]
    return [int(flat[i]) for i in sorted(first_seen)]


def _components(cells: np.ndarray, npoin: int) -> list[np.ndarray]:
    """The connected pieces of a triangulation, as boolean cell masks.

    A domain cut by a shoreline can come back as two water bodies in one array,
    and the winding walk oceanmesh identifies sections along traces ONE of them.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    rows = np.repeat(np.arange(cells.shape[0]), 3)
    incidence = coo_matrix(
        (np.ones(rows.shape[0]), (rows, cells.ravel())),
        shape=(cells.shape[0], npoin))
    count, label = connected_components(incidence @ incidence.T, directed=False)
    return [label == k for k in range(count)]


def _sections(ends, points, cells, bed, node_ids, first: int) -> list[dict]:
    """Section ENDPOINTS as the runs of nodes between them, in walk order.

    ``identify_ocean_boundary_sections`` returns the first and last node of each
    section; the winding walk it indexes into is what turns those endpoints back
    into a run, so the walk is rebuilt with the same library call FROM THE SAME
    START - a walk from anywhere else is a different contour, or the same one cut
    at a different place. ``node_ids`` maps this piece's numbering back onto the
    whole mesh's.
    """
    from oceanmesh.edges import get_winded_boundary_edges

    walk = _walk_nodes(get_winded_boundary_edges(cells, vFirst=int(first)))
    at = {node: index for index, node in enumerate(walk)}
    out: list[dict] = []
    for start, stop in ends:
        i, j = at.get(int(start)), at.get(int(stop))
        if i is None or j is None:
            continue
        run = walk[i:j + 1] if i <= j else walk[i:] + walk[:j + 1]
        out.append({
            "nodes": [int(node_ids[n]) for n in run],
            "node_count": len(run),
            "mean_bed_m": round(float(np.asarray(bed)[run].mean()), 3),
            "min_bed_m": round(float(np.asarray(bed)[run].min()), 3),
            "centroid": [round(float(points[run, 0].mean()), 6),
                         round(float(points[run, 1].mean()), 6)],
        })
    return out


# --------------------------------------------------------------------------- #
# post.
# --------------------------------------------------------------------------- #
def op_post(cfg: dict, out: str) -> int:
    """Run ops over a mesh the host already holds -> the mesh and the results."""
    npz = np.load(cfg["mesh_npz"])
    points = np.asarray(npz["points"], dtype=float)
    cells = np.asarray(npz["cells"], dtype=np.int64)
    bed = np.asarray(npz["bed"], dtype=float) if "bed" in npz.files else None
    mpd = _m_per_deg(0.5 * (float(points[:, 1].min()) + float(points[:, 1].max())))
    reports: list[dict] = []
    notes: list[str] = []
    env_extra = {
        "min_edge_length": float(cfg["min_edge_length_m"]) / mpd,
        "min_edgelength": float(cfg["min_edge_length_m"]) / mpd,
        "max_edge_length": float(cfg["max_edge_length_m"]) / mpd,
    }
    points, cells, results = _run_mesh_ops(
        cfg.get("ops") or [], points, cells, bed, mpd, reports, env_extra,
        notes=notes)
    stem = out + "/" + str(cfg["out_stem"])
    np.savez(stem + ".npz", points=points, cells=cells)
    report = {"ops": reports, "results": results, "clean_notes": notes}
    json.dump(report, open(stem + ".json", "w"), indent=2)
    print("OM2D_POST_OK", json.dumps({"ops": reports})[:2000])
    return 0


_OPS = {"build": op_build, "post": op_post}


def main() -> int:
    op = sys.argv[1]
    cfg = json.load(open(sys.argv[2]))
    out = sys.argv[3].rstrip("/")
    try:
        return _OPS[op](cfg, out)
    except _Refusal as refusal:
        json.dump(refusal.document, open(out + "/om2d_refusal.json", "w"),
                  indent=2)
        print("OM2D_REFUSED", json.dumps(refusal.document))
        return 3


if __name__ == "__main__":
    sys.exit(main())
