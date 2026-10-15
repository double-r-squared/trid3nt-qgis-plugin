"""In-container FloPy driver for the ``triangle``, ``voronoi`` and ``gridgen`` meshers.

Runs INSIDE the mesh image where FloPy, Triangle and Gridgen are installed and
imports nothing from trid3nt. The recipe's ops travel as ``{fn, kwargs}`` data,
each called VERBATIM on the library: THIS is where the signature is the schema.
The domain arrives as rings already projected to the mesh's own UTM zone, and
the mesh leaves as one neutral array - cells counter-clockwise, 0-based."""

from __future__ import annotations

import inspect
import json
import sys

import numpy as np
from shapely.geometry import Polygon, shape as _shape

#: Cells with this or smaller unsigned area are construction artifacts, not
#: elements: the build is in metres, so a square nanometre is below anything
#: a mesh cell can be.
_DEGENERATE_AREA_M2 = 1e-9


class _Refusal(Exception):
    """A typed refusal the HOST re-raises under its own code.

    The code, the reason and the escalation travel back as a document."""

    def __init__(self, code: str, message: str, escalation: dict | None = None
                 ) -> None:
        super().__init__(message)
        self.document = {"code": code, "message": message,
                         "escalation": escalation}


def _load_geoms(path: str) -> list:
    """The staged GeoJSON document as shapely geometries."""
    try:
        doc = json.load(open(path))
    except (OSError, ValueError) as exc:
        raise _Refusal(
            "MESH_OP_GEOMETRY_EMPTY",
            f"the staged geometry {path} cannot be read as GeoJSON ({exc}), so "
            "the op has nothing to draw with; restage the geometry and retry")
    if isinstance(doc, dict) and doc.get("type") == "Feature":
        doc = doc.get("geometry")
    if isinstance(doc, dict) and doc.get("type") == "GeometryCollection":
        geoms = [_shape(g) for g in doc.get("geometries") or ()]
    elif isinstance(doc, dict) and "features" in doc:
        geoms = [_shape(f["geometry"]) for f in doc.get("features") or ()
                 if (f or {}).get("geometry")]
    elif isinstance(doc, dict) and "type" in doc:
        geoms = [_shape(doc)]
    else:
        geoms = []
    if not geoms:
        raise _Refusal(
            "MESH_OP_GEOMETRY_EMPTY",
            f"the staged geometry {path} carries no geometry, so the op has "
            "nothing to draw with; stage a document with a geometry and retry")
    return geoms


def _polygons(geoms) -> list:
    """Every polygon part, flattening multiparts."""
    out = []
    for geom in geoms:
        if geom.geom_type == "Polygon":
            out.append(geom)
        elif geom.geom_type == "MultiPolygon":
            out.extend(geom.geoms)
    return out


def _open(ring) -> list[tuple[float, float]]:
    """The ring's walk without its closing repeat, as coordinate pairs.

    FloPy strips a closing duplicate itself, but a hole ring's pocket maths
    below needs the repeat gone first."""
    coords = [(float(x), float(y)) for x, y in ring]
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords


def _bind_call(fn, kwargs: dict):
    """Bind the kwargs against the library's own signature, then call it.

    THE SIGNATURE IS THE SCHEMA: a kwarg the method does not take refuses."""
    try:
        inspect.signature(fn).bind(**kwargs)
    except TypeError as exc:
        raise ValueError("%s: %s" % (getattr(fn, "__name__", "the op"),
                                     exc)) from exc
    return fn(**kwargs)


class _TriangleBuild:
    """One Triangle (or Voronoi-over-Triangle) build: rings in, ``.build()`` out.

    Rings are registered first and the executable's inputs are written last, so
    a hole's pockets are computed with every island already known: the hole
    point must fall in the pocket that is actually water, not on an island
    inside the hole."""

    def __init__(self, cfg: dict, out: str) -> None:
        from flopy.utils.triangle import Triangle

        words = {key: cfg[key] for key in
                 ("maximum_area", "angle", "nodes", "additional_args")
                 if key in cfg}
        self.tri = Triangle(model_ws=out, exe_name="triangle", **words)
        self.outer: list[list[tuple[float, float]]] = []
        self.holes: list[list[tuple[float, float]]] = []
        for ring in cfg.get("outer_rings") or ():
            self.outer.append(_open(ring))
        for ring in cfg.get("holes") or ():
            self.holes.append(_open(ring))

    def add_outer(self, ring) -> None:
        self.outer.append(_open(ring))

    def add_hole_ring(self, ring) -> None:
        self.holes.append(_open(ring))

    def entry(self, entry: dict) -> dict:
        """One staged op, called verbatim on the library -> its report."""
        name = str(entry["fn"])
        kwargs = dict(entry.get("kwargs") or {})
        report = {"op": name}
        if name == "add_polygon":
            geoms = _load_geoms(kwargs.pop("polygon"))
            ignore_holes = bool(kwargs.get("ignore_holes", False))
            parts = _polygons(geoms)
            if not parts:
                raise _Refusal(
                    "MESH_OP_GEOMETRY_EMPTY",
                    "add_polygon draws filled ground and the staged geometry "
                    "carries no polygon; stage a Polygon (or MultiPolygon) "
                    "and retry")
            # Every component first, then every interior: an island that is a
            # later part must already be land before any hole's pocket is cut.
            for geom in parts:
                self.add_outer(geom.exterior.coords)
            for geom in parts:
                if not ignore_holes:
                    for interior in geom.interiors:
                        self.add_hole_ring(interior.coords)
            report["components"] = len(parts)
        elif name == "add_hole":
            parts = _polygons(_load_geoms(kwargs.pop("hole")))
            if not parts:
                raise _Refusal(
                    "MESH_OP_HOLE_NOT_ENCLOSING",
                    "add_hole empties a REGION and a region is bounded by its "
                    "own edge: stage the ring of the area to leave unmeshed, "
                    "not a bare point or line (a point empties everything "
                    "enclosing it, which is the whole domain)")
            for geom in parts:
                self.add_hole_ring(geom.exterior.coords)
                # An island inside a hole is ground: its ring is land.
                for interior in geom.interiors:
                    self.add_outer(interior.coords)
            report["components"] = len(parts)
        elif name == "add_region":
            geom = _load_geoms(kwargs.pop("point"))[0]
            kwargs["point"] = geom.representative_point().coords[0]
            _bind_call(self.tri.add_region, kwargs)
            report["called"] = 1
        else:
            raise ValueError(
                "the triangle mesher has no method %r (this driver runs the "
                "flopy_grid script)" % name)
        return report

    def build(self):
        """Write the .poly inputs and run the executable -> the built object."""
        for ring in self.outer:
            _bind_call(self.tri.add_polygon, {"polygon": ring})
        for index, ring in enumerate(self.holes):
            # The hole's own edges bound its pocket, so the ring is segments
            # exactly like an outer - what makes it a hole is the point below.
            _bind_call(self.tri.add_polygon, {"polygon": ring})
            # Only what is NESTED inside this hole subtracts: islands that are
            # outers, and holes within holes. The domain outer that CONTAINS
            # the hole is the region the hole punches out of, not ground in it.
            base = Polygon(ring)
            pockets = base
            for outer in self.outer:
                if base.contains(Polygon(outer)):
                    pockets = pockets.difference(Polygon(outer))
            for position, other in enumerate(self.holes):
                if position != index and base.contains(Polygon(other)):
                    pockets = pockets.difference(Polygon(other))
            if pockets.is_empty:
                raise _Refusal(
                    "MESH_HOLE_HAS_NO_INTERIOR",
                    "this hole ring encloses no unmeshed area of its own - "
                    "every point inside it is already an island or another "
                    "hole - so there is nowhere to place the point that "
                    "empties it")
            parts = list(pockets.geoms) if pockets.geom_type == "MultiPolygon" \
                else [pockets]
            for part in parts:
                point = part.representative_point()
                self.tri.add_hole(hole=(point.x, point.y))
        self.tri.build()
        return self.tri


def _gridgen(cfg: dict, out: str) -> tuple[list, list, list[dict], dict]:
    """One Gridgen build: the base rectilinear grid, the ops, DISV out."""
    from flopy.discretization import StructuredGrid
    from flopy.utils.gridgen import Gridgen

    delr = np.asarray(cfg["delr"], dtype=float)
    delc = np.asarray(cfg["delc"], dtype=float)
    nrow, ncol = int(delc.size), int(delr.size)
    reports: list[dict] = []
    declared: list[int] = []
    for entry in cfg.get("ops") or ():
        layers = (entry.get("kwargs") or {}).get("layers")
        if isinstance(layers, list):
            declared.extend(int(layer) for layer in layers)
        elif layers is not None:
            declared.append(int(layers))
    # The mesh takes no VERTICAL from gridgen: top and botm are placeholders
    # only to satisfy the library's completeness check, distinct so no layer
    # reads as zero-thickness. Real layers are the engine's own inputs.
    nlay = 1 + (max(declared) if declared else 0)
    top = np.zeros((nrow, ncol))
    botm = -np.arange(1, nlay + 1, dtype=float)[:, None, None] \
        * np.ones((1, nrow, ncol))
    modelgrid = StructuredGrid(
        delr=delr, delc=delc, top=top, botm=botm,
        xoff=float(cfg["xoff"]), yoff=float(cfg["yoff"]),
        angrot=float(cfg.get("angrot", 0.0)),
        crs="EPSG:%d" % int(cfg["crs_epsg"]))
    grid = Gridgen(modelgrid, model_ws=out, exe_name="gridgen")
    for entry in cfg.get("ops") or ():
        name = str(entry["fn"])
        kwargs = dict(entry.get("kwargs") or {})
        report = {"op": name}
        if name == "add_active_domain":
            kwargs["feature"] = _load_geoms(kwargs.pop("feature"))
        elif name == "add_refinement_features":
            kwargs["features"] = _load_geoms(kwargs.pop("features"))
        else:
            raise ValueError(
                "the gridgen mesher has no method %r (this driver runs the "
                "flopy_grid script)" % name)
        _bind_call(getattr(grid, name), kwargs)
        reports.append(report)
    grid.build()
    try:
        props = grid.get_gridprops_disv()
    except AssertionError as exc:
        raise _Refusal(
            "MESH_GRIDGEN_DISV_NOT_UNIFORM",
            "the ops declare layers %s of a %d-layer grid, so gridgen meshes "
            "the layers separately and they came out with different cell "
            "counts; a DISV cell is a prism over every layer, so the readback "
            "cannot be one grid. State `layers` on each op as every layer the "
            "grid spans (0..%d), or [0] when the mesh is single-layer"
            % (declared, nlay, nlay - 1),
            escalation={"layers": list(range(nlay))}) from exc
    points = [[float(x), float(y)] for _iv, x, y in props["vertices"]]
    rows = [[int(node) for node in cell[4:]]
            for cell in props["cell2d"]]
    # DISV's cell2d closes each ring by repeating its first vertex; the mesh
    # cells are walks and carry no repeat.
    rows = [row[:-1] if len(row) > 1 and row[0] == row[-1] else row
            for row in rows]
    return points, rows, reports, {"sources": cfg.get("sources")}


def _neutenize(points, rows) -> tuple[np.ndarray, list, dict]:
    """The library's arrays as the neutral mesh: 0-based, CCW, no dead cells.

    Voronoi polygons come back clockwise and DISV comes back with its closing
    vertex, so orientation and the repeat are normalised here once for every
    mesher - the consumers downstream see one convention."""
    pts = np.asarray(points, dtype=float)
    cells: list[list[int]] = [list(map(int, row)) for row in rows]
    norm = {"index_base": "0", "dropped_degenerate_cells": 0}
    if cells:
        lowest = min(min(row) for row in cells if row)
        highest = max(max(row) for row in cells if row)
        if lowest >= 1 and highest >= pts.shape[0]:
            cells = [[node - 1 for node in row] for row in cells]
            norm["index_base"] = "normalised from 1"
        elif highest >= pts.shape[0] or lowest < 0:
            raise _Refusal(
                "MESH_CELL_INDEX_OUT_OF_RANGE",
                "the mesher returned a cell naming node %d of %d, which is "
                "neither a valid 0-based nor 1-based index; the build output "
                "does not describe this mesh and is refused"
                % (highest, pts.shape[0]))
    kept: list[list[int]] = []
    for row in cells:
        if len(row) > 1 and row[0] == row[-1]:
            row = row[:-1]
        if len(set(row)) < 3:
            norm["dropped_degenerate_cells"] += 1
            continue
        xy = pts[row]
        area = 0.5 * float(np.sum(
            xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1]))
        if abs(area) <= _DEGENERATE_AREA_M2:
            norm["dropped_degenerate_cells"] += 1
            continue
        kept.append(row[::-1] if area < 0.0 else row)
    if not kept:
        raise _Refusal(
            "MESH_BUILD_EMPTY",
            "the mesher returned no usable cells - every cell was degenerate "
            "or empty - so there is no mesh to hand over; the domain or the "
            "resolution cannot hold a cell")
    used = np.unique(np.concatenate([np.asarray(row) for row in kept]))
    remap = np.full(pts.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(used.shape[0], dtype=np.int64)
    pts = pts[used]
    kept = [remap[row].tolist() for row in kept]
    return pts, kept, norm


def op_build(cfg: dict, out: str) -> int:
    mesher = str(cfg["mesher"])
    reports: list[dict] = []
    extra: dict = {}
    if mesher in ("triangle", "voronoi"):
        builder = _TriangleBuild(cfg, out)
        for entry in cfg.get("ops") or ():
            reports.append(builder.entry(entry))
        tri = builder.build()
        extra = {"maximum_area": cfg["maximum_area"],
                 "maximum_area_source": cfg["maximum_area_source"]}
        if mesher == "voronoi":
            from flopy.utils.voronoi import VoronoiGrid

            dual = VoronoiGrid(tri)
            points = dual.verts
            rows = [list(map(int, row)) for row in dual.iverts]
        else:
            points, rows = tri.verts, [list(map(int, row))
                                       for row in tri.iverts]
    else:
        points, rows, reports, extra = _gridgen(cfg, out)

    pts, cells, norm = _neutenize(points, rows)
    counts = sorted({len(row) for row in cells})
    uniform = counts == [3]
    if uniform:
        np.savez(out + "/flopy_mesh.npz", points=pts,
                 cells=np.asarray(cells, dtype=np.int64))
    else:
        np.savez(out + "/flopy_mesh.npz", points=pts,
                 cells_flat=np.concatenate(
                     [np.asarray(row) for row in cells]).astype(np.int64),
                 cell_counts=np.asarray([len(row) for row in cells],
                                        dtype=np.int64))

    import flopy

    stats = {
        "mesher": mesher,
        "library": "flopy %s" % getattr(flopy, "__version__", "?"),
        "element_count": int(len(cells)),
        "node_count": int(pts.shape[0]),
        "crs_epsg": int(cfg["crs_epsg"]),
        "resolution_m": cfg.get("resolution_m"),
        "domain_source": cfg.get("domain_source"),
        "ops": reports,
        **extra,
        "probes": {
            "library": "flopy %s" % getattr(flopy, "__version__", "?"),
            "index_base": norm["index_base"],
            "dropped_degenerate_cells": int(norm["dropped_degenerate_cells"]),
            "cell_vertex_counts": {str(n): sum(1 for row in cells
                                               if len(row) == n)
                                   for n in counts},
        },
    }
    json.dump(stats, open(out + "/flopy_stats.json", "w"), indent=2)
    print("FLOPY_OK", json.dumps(stats)[:4000])
    return 0


_OPS = {"build": op_build}


def main() -> int:
    # Contract (host <-> container over the mounted /data dir):
    #   argv[1] = <op>   argv[2] = /data/<config>.json   argv[3] = /data
    #
    #   build  config: mesher, outer_rings, holes (rings already in the
    #          mesh's UTM metres), crs_epsg, resolution_m, domain_source,
    #          ops [{fn, kwargs}] with geometry staged under /data, and the
    #          mesher's own words - maximum_area/angle/nodes/additional_args
    #          for triangle and voronoi, delr/delc/xoff/yoff/angrot for
    #          gridgen. Runs the executable and emits /data/flopy_mesh.npz
    #          (points (N,2); cells (M,3) when triangular, else cells_flat +
    #          cell_counts) plus /data/flopy_stats.json.
    op = sys.argv[1]
    cfg = json.load(open(sys.argv[2]))
    out = sys.argv[3].rstrip("/")
    try:
        return _OPS[op](cfg, out)
    except _Refusal as refusal:
        json.dump(refusal.document, open(out + "/flopy_refusal.json", "w"),
                  indent=2)
        print("FLOPY_REFUSED", json.dumps(refusal.document))
        return 3


if __name__ == "__main__":
    sys.exit(main())
