"""The ``om2d`` mesher: OceanMesh2D, wrapped where it lives.

Three registrations and nothing else.

NAMESPACES. Ops may name any function of the CHLNDDEV ``oceanmesh`` port under
the library's own spelling, tagged by the phase it runs in: the sizing functions
before generation, the clean passes and the ocean-boundary identification after
it. Two om2d-owned primitives ride beside them for the two things the library has
no single word for - punching a geometry out of the domain with its outline
constrained in, and sizing a drawn region. The SHARED primitives (``set_bed``,
``set_boundary_roles``) ride along as they do for every mesher.

ROLE ADAPTER. ``build`` turns the recipe's extent into the library's domain
object - the GSHHG land polygons cut to a lon/lat box, or the interior of a
supplied polygon - and threads ``resolution_m`` as the library's own edge
defaults. Nothing else: an adapter that grows opinions is the old sin.

DEFAULT RECIPE. The hard-baked, visible list below. It sizes no INTERIOR, because
what a domain should be sized TOWARD is the ask's knowledge; an undeclared ask is
meshed uniformly at the one size word, cleaned by the library's own passes, and
bedded from topobathy. The RIM is the one exception: no sizing function the
library has measures the extent's own outline, so an ask that names none comes
back with the boundary a solver forces its open condition on running an order of
magnitude past the size word. A declared recipe replaces this list wholesale.

All of the library is the port's own code, running in ``trid3nt-local/mesh:
latest`` where it is installed; this file composes the ask, shells the box, and
turns what comes back into the one neutral mesh every writer reads.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.mesh.inputs import op_geometry
from trid3nt_server.workflows.mesh.meshers import (
    POST,
    PRE,
    BoundOp,
    Mesh,
    MeshToolError,
    OpNamespace,
    bind_ops,
    mesh_op,
    register_mesher,
)
from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.om2d")

__all__ = ["OM2D", "build"]

#: The GPL-isolated OceanMesh2D image and the driver mounted into it.
_MESH_IMAGE_DEFAULT = "trid3nt-local/mesh:latest"
_INCONTAINER_SCRIPT = "om2d_driver.py"
#: What the driver writes when it refuses in its own words: code, reason, and the
#: call that DOES what the refused ask could not.
_REFUSAL_FILE = "om2d_refusal.json"
_CONTAINER_TIMEOUT_S = 2400

#: DistMesh seeds its initial point cloud from numpy's global generator, which
#: ``generate_mesh`` seeds itself from this value: one number is the whole
#: difference between a replayable recipe and a mesh that drifts per rebuild.
_SEED = 0
_MAX_ITER = 40

#: The one size word, when an ask declares none.
_DEFAULT_RESOLUTION_M = 40.0

#: What the coarsest edge defaults to, as a MULTIPLE of the finest. A fixed metre
#: ceiling turns a coarse resolution into a refusal about a number the caller
#: never wrote; the multiple moves with the resolution. Any op may state its own
#: ``max_edge_length`` instead.
_MAX_EL_FACTOR = 10.0

#: The fraction of the MEDIAN element area below which an element has collapsed:
#: no area to invert, and a solver refuses the whole mesh over it.
_COLLAPSED_AREA_FRAC = 1e-9

#: How close two nodes may sit before a single-precision geometry file writes them
#: as one point, in metres. A UTM northing spends its mantissa on seven digits,
#: which leaves a fraction of a metre.
_COINCIDENT_TOLERANCE_M = 1.0

#: The oceanmesh functions an op may name, by the phase each runs in. Declared as
#: a ROSTER rather than read off the module: the library is installed only inside
#: the GPL-isolated image, so this process cannot import it to introspect. The
#: kwargs of an op from this namespace are bound in the container, against the
#: real signature, and the library's own error surfaces verbatim.
_OCEANMESH_SIZING = (
    "bathymetric_gradient_sizing_function",
    "compute_minimum",
    "distance_sizing_from_line_function",
    "distance_sizing_from_point_function",
    "distance_sizing_function",
    "enforce_mesh_gradation",
    "enforce_mesh_size_bounds_elevation",
    "feature_sizing_function",
    "multiscale_sizing_function",
    "wavelength_sizing_function",
)
_OCEANMESH_ON_A_MESH = (
    "delete_boundary_faces",
    "delete_exterior_faces",
    "delete_faces_connected_to_one_face",
    "delete_interior_faces",
    "fix_mesh",
    "identify_ocean_boundary_sections",
    "laplacian2",
    "make_mesh_boundaries_traversable",
    "mesh_clean",
)

#: om2d's OWN pre-generation primitives, under their real driver ``def`` names.
#: All three impose state on the DOMAIN the library triangulates, which the
#: library has no single word for: ``om.Difference`` subtracts a shape but says
#: nothing about locking its outline in, a sizing lattice has no function that
#: writes a target edge inside a drawn polygon, and every sizing function the
#: library has measures the SHORELINE - none of them the extent's own rim.
_OM2D_PRIMITIVES = ("set_obstacle", "set_region_size", "set_rim_size")

#: The ops list an undeclared ask gets. Hard-baked and visible: the rim at the
#: size word, the library's own clean chain in the order it is meant to run, then
#: the bed.
_DEFAULT_OPS = (
    mesh_op("set_rim_size"),
    mesh_op("delete_boundary_faces"),
    mesh_op("delete_faces_connected_to_one_face"),
    mesh_op("laplacian2"),
    mesh_op("make_mesh_boundaries_traversable"),
    mesh_op("fix_mesh", delete_unused=True),
    mesh_op("set_bed", source="fetch_topobathy", interp="nearest"),
)


# --------------------------------------------------------------------------- #
# The role adapter.
# --------------------------------------------------------------------------- #
def build(recipe: Any) -> Mesh:
    """Mesh the water side of the shoreline, or the interior of a supplied polygon."""
    ops = bind_ops(OM2D, recipe.ops)
    resolution_m = float(recipe.resolution_m or _DEFAULT_RESOLUTION_M)
    rundir = _rundir()
    domain = _domain(recipe.extent, rundir)

    pre = [op for op in ops if op.phase == PRE]
    post = [op for op in ops if op.phase == POST]
    # Everything before the first of OUR primitives runs in the same container
    # call as the generation: the library's clean passes renumber the nodes, and a
    # bed painted on the host cannot survive a renumbering it never saw.
    split = next((i for i, op in enumerate(post)
                  if op.origin == "primitives"), len(post))
    config = {
        "bbox": list(domain.bbox),
        "shoreline_shp": (f"/shoreline/{domain.shoreline.name}"
                          if domain.shoreline is not None else None),
        "domain_geojson": (f"/data/{domain.polygon_name}"
                           if domain.polygon_name is not None else None),
        "min_edge_length_m": resolution_m,
        "max_edge_length_m": _MAX_EL_FACTOR * resolution_m,
        "seed": _SEED,
        "max_iter": _MAX_ITER,
        "pre_ops": [_staged(op, rundir, index) for index, op in enumerate(pre)],
        "post_ops": [_staged(op, rundir, len(pre) + i)
                     for i, op in enumerate(post[:split])],
    }
    (rundir / "om2d_config.json").write_text(json.dumps(config))
    _run_op(rundir, "build", "om2d_config.json", "om2d_mesh.npz",
            shoreline_dir=None if domain.shoreline is None
            else domain.shoreline.parent)

    mesh, stats = _read_built(rundir, domain, resolution_m, ops)
    mesh = _apply_tail(mesh, post[split:], rundir, resolution_m)
    return _emitted(mesh, rundir, domain, stats)


def _read_built(rundir: Path, domain: "_Domain", resolution_m: float,
                ops: tuple[BoundOp, ...]) -> tuple[Mesh, Mapping[str, Any]]:
    """The container's arrays as the neutral mesh, cleaned once and projected."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.nodes import reproject_nodes_to_utm

    npz = np.load(rundir / "om2d_mesh.npz")
    lonlat = np.asarray(npz["points"], dtype=float)
    cells = np.asarray(npz["cells"], dtype=np.int64)
    pfix = np.asarray(npz["pfix"], dtype=float)
    lonlat, cells, repaired = _clean_once(lonlat, cells)
    points, utm_epsg = reproject_nodes_to_utm(lonlat)
    stats = _stats(rundir)
    return Mesh(
        points=points, cells=cells, crs_authid=f"EPSG:{int(utm_epsg)}",
        meta={
            "utm_epsg": int(utm_epsg),
            "lonlat": lonlat,
            "lonlat_bbox": (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()),
                            float(lonlat[:, 0].max()), float(lonlat[:, 1].max())),
            "domain_source": domain.source,
            "probes": {
                **({"rim_edge_length_m": dict(stats["rim_edge_length_m"])}
                   if stats.get("rim_edge_length_m") else {}),
                **_conformal_probe(points, pfix, int(utm_epsg)),
                **({"degenerate_elements_repaired": repaired} if repaired else {}),
                **({"clean_notes": list(stats["clean_notes"])}
                   if stats.get("clean_notes") else {}),
            },
            "artifact": {
                "provenance": {
                    "mesher_library": stats.get("engine", "oceanmesh (unreported)"),
                    "resolution_m": resolution_m,
                    "max_el_m": _MAX_EL_FACTOR * resolution_m,
                    "seed": _SEED,
                    "sizing_source": _sizing_source(stats, domain),
                    "domain_source": domain.source,
                    "op_notes": [op.note for op in ops if op.note],
                },
            },
            "synthetic_inputs": [
                {"param": "resolution_m", "value": resolution_m, "units": "m",
                 "basis": "user"},
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / {cells.shape[0]} elements",
                 "basis": "derived",
                 "real_source_if_any": _sizing_source(stats, domain)},
            ],
        }), stats


def _apply_tail(mesh: Mesh, tail: list[BoundOp], rundir: Path,
                resolution_m: float) -> Mesh:
    """The ops declared after the first primitive, in their declared order.

    OUR primitives run here, on the host, against the real callable. A LIBRARY op
    in this stretch runs in its own container call over the arrays the mesh now
    has, and must not renumber them: a bed painted before it would then belong to
    nodes that no longer exist, which is why a topology-changing op belongs before
    the first primitive.
    """
    import dataclasses

    results = dict(mesh.meta.get("op_results") or {})
    for index, op in enumerate(tail):
        if op.origin == "primitives":
            mesh = op.fn(mesh, **_bound_inputs(op))
            continue
        before = (mesh.node_count, mesh.element_count)
        mesh, result = _run_tail_op(mesh, op, rundir, index, resolution_m)
        if (mesh.node_count, mesh.element_count) != before:
            raise MeshToolError(
                "MESH_OP_RENUMBERED_AFTER_BED",
                f"the op {op.name!r} changed this mesh from {before[0]} nodes / "
                f"{before[1]} elements to {mesh.node_count} / "
                f"{mesh.element_count} after a primitive had already painted node "
                "values onto it; declare a topology-changing op before the first "
                "set_ op in the recipe.")
        results[op.name] = result
    return dataclasses.replace(mesh, meta={**dict(mesh.meta),
                                           "op_results": results})


def _run_tail_op(mesh: Mesh, op: BoundOp, rundir: Path, index: int,
                 resolution_m: float) -> tuple[Mesh, Any]:
    """One library op over the mesh as it now stands -> the mesh and its result."""
    import dataclasses

    import numpy as np

    lonlat = np.asarray(mesh.meta["lonlat"], dtype=float)
    npz_name = f"om2d_tail_{index}_in.npz"
    arrays = {"points": lonlat, "cells": np.asarray(mesh.cells, dtype=np.int64)}
    if mesh.has_bed:
        arrays["bed"] = np.asarray(mesh.bed, dtype=float)
    np.savez(rundir / npz_name, **arrays)
    stem = f"om2d_tail_{index}_out"
    config_name = f"om2d_tail_{index}.json"
    (rundir / config_name).write_text(json.dumps({
        "mesh_npz": f"/data/{npz_name}",
        "out_stem": stem,
        "min_edge_length_m": resolution_m,
        "max_edge_length_m": _MAX_EL_FACTOR * resolution_m,
        "ops": [_staged(op, rundir, index)]}))
    _run_op(rundir, "post", config_name, f"{stem}.json")
    report = json.loads((rundir / f"{stem}.json").read_text())
    out = np.load(rundir / f"{stem}.npz")
    new_lonlat = np.asarray(out["points"], dtype=float)
    if new_lonlat.shape == lonlat.shape and np.allclose(new_lonlat, lonlat):
        return mesh, report["results"].get(op.name)
    from trid3nt_server.workflows.mesh.shared.nodes import reproject_nodes_to_utm

    points, utm_epsg = reproject_nodes_to_utm(new_lonlat)
    return dataclasses.replace(
        mesh, points=points, cells=np.asarray(out["cells"], dtype=np.int64),
        crs_authid=f"EPSG:{int(utm_epsg)}",
        meta={**dict(mesh.meta), "lonlat": new_lonlat,
              "utm_epsg": int(utm_epsg)}), report["results"].get(op.name)


def _bound_inputs(op: BoundOp) -> dict[str, Any]:
    """One primitive's kwargs with every data value converted, once."""
    from trid3nt_server.workflows.mesh.inputs import op_input

    return {name: op_input(value) for name, value in op.kwargs.items()}


def _staged(op: BoundOp, rundir: Path, index: int) -> dict[str, Any]:
    """One op as the container reads it: its name and its kwargs as /data paths.

    Code-as-data. The name travels verbatim and the driver calls it verbatim; a
    kwarg that is a raster or a layer is converted once, written into the mounted
    rundir, and named by the path the container sees.
    """
    from trid3nt_server.workflows.mesh.inputs import op_input

    kwargs: dict[str, Any] = {}
    for name, value in op.kwargs.items():
        converted = op_input(value)
        if isinstance(converted, Path):
            local = rundir / f"op{index}_{name}{converted.suffix or '.tif'}"
            local.write_bytes(converted.read_bytes())
            kwargs[name] = f"/data/{local.name}"
        elif isinstance(converted, Mapping) and "type" in converted:
            local = rundir / f"op{index}_{name}.geojson"
            local.write_text(json.dumps(converted))
            kwargs[name] = f"/data/{local.name}"
        else:
            kwargs[name] = converted
    return {"fn": op.name, "kwargs": kwargs}


# --------------------------------------------------------------------------- #
# The domain the extent resolves to.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Domain:
    """What the mesh is cut from: the shoreline, or a supplied polygon.

    Exactly one of ``shoreline`` and ``polygon_name`` is set. ``bbox`` is the
    lon/lat box the triangulator seeds inside - the extent itself on the shoreline
    path, the polygon's own bounds on the other.
    """

    bbox: tuple[float, float, float, float]
    source: str
    shoreline: Path | None = None
    polygon_name: str | None = None


def _domain(extent: Any, rundir: Path) -> _Domain:
    """Resolve the recipe's extent into the domain the box cuts the mesh from."""
    if extent is None:
        raise MeshToolError(
            "MESH_EXTENT_MISSING",
            "mesher 'om2d' cuts its domain from an extent and this recipe "
            "declares none; give it a (min_lon, min_lat, max_lon, max_lat) box or "
            "a polygon another tool produced.")
    if isinstance(extent, (tuple, list)):
        shoreline = _shoreline_shp()
        source = f"GSHHG land polygons ({shoreline.name})"
        return _Domain(bbox=_lonlat_bounds(tuple(float(v) for v in extent), source),
                       source=source, shoreline=shoreline)
    polygons = _polygons(op_geometry(extent))
    if not polygons:
        raise MeshToolError(
            "MESH_DOMAIN_NOT_A_POLYGON",
            f"the extent {extent!r} carries no polygon, so there is no interior "
            "to mesh; supply a bbox, or a polygon another tool produced.")
    name = "domain.geojson"
    (rundir / name).write_text(json.dumps(
        {"type": "GeometryCollection", "geometries": polygons}))
    source = f"supplied polygon domain ({len(polygons)} part(s))"
    return _Domain(bbox=_lonlat_bounds(_geometry_bounds(polygons), source),
                   source=source, polygon_name=name)


def _lonlat_bounds(bbox: tuple[float, ...], source: str) -> tuple[float, ...]:
    """``bbox`` if it is lon/lat, else the refusal that names what it is instead.

    Every sizing number this mesher works in is degrees converted at the domain's
    own latitude, so an extent handed over in projected metres does not read as a
    wrong answer - it reads as a lattice millions of cells wide, which surfaces as
    an allocation failure inside the triangulator rather than as the CRS mismatch
    it is.
    """
    west, south, east, north = (float(v) for v in bbox)
    if -180.0 <= west <= 180.0 and -180.0 <= east <= 180.0 \
            and -90.0 <= south <= 90.0 and -90.0 <= north <= 90.0:
        return (west, south, east, north)
    raise MeshToolError(
        "MESH_DOMAIN_NOT_LONLAT",
        f"the extent from the {source} spans {(west, south, east, north)}, which "
        "is outside the lon/lat range this mesher works in - it is projected "
        "coordinates, most likely metres. Reproject the domain to EPSG:4326 "
        "before handing it over.")


def _polygons(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every polygon geometry a supplied domain document carries."""
    out: list[dict[str, Any]] = []

    def walk(geometry: Any) -> None:
        if not isinstance(geometry, Mapping):
            return
        kind = str(geometry.get("type") or "")
        if kind in ("Polygon", "MultiPolygon"):
            out.append(dict(geometry))
        elif kind == "GeometryCollection":
            for part in geometry.get("geometries") or ():
                walk(part)

    features = doc.get("features") if isinstance(doc, Mapping) else None
    if features is not None:
        for feature in features:
            walk((feature or {}).get("geometry"))
    else:
        walk(doc.get("geometry") if "geometry" in doc else doc)
    return out


def _geometry_bounds(geometries: list[dict[str, Any]]
                     ) -> tuple[float, float, float, float]:
    """The lon/lat box the supplied domain occupies."""
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    minx, miny, maxx, maxy = unary_union([_shape(g) for g in geometries]).bounds
    return (float(minx), float(miny), float(maxx), float(maxy))


def _shoreline_shp() -> Path:
    """The shoreline polygons the domain is cut from, or a typed refusal.

    The GSHHG shapefile is a machine-local dataset rather than a fetch: it is
    named by env so one copy serves every mesher that needs it.
    """
    declared = (os.environ.get("TRID3NT_GSHHG_SHP") or "").strip()
    path = Path(declared) if declared else None
    if path is None or not path.exists():
        raise MeshToolError(
            "MESH_SHORELINE_UNAVAILABLE",
            "the om2d mesher cuts its domain from GSHHG land polygons: set "
            "TRID3NT_GSHHG_SHP to a GSHHG L1 polygon shapefile "
            f"(currently {declared or 'unset'}).")
    return path


# --------------------------------------------------------------------------- #
# The box.
# --------------------------------------------------------------------------- #
def _rundir() -> Path:
    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _run_op(rundir: Path, op: str, config_name: str, produces: str, *,
            shoreline_dir: Path | None = None) -> None:
    """One driver op in the OceanMesh2D box, or a typed refusal carrying its output."""
    image = os.environ.get("TRID3NT_MESH_IMAGE") or _MESH_IMAGE_DEFAULT
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{rundir}:/data"]
    if shoreline_dir is not None:
        argv += ["-v", f"{shoreline_dir}:/shoreline:ro"]
    argv += ["--entrypoint", "python", image,
             f"/drivers/{_INCONTAINER_SCRIPT}", op, f"/data/{config_name}", "/data"]
    logger.info("om2d mesher %s: %s", op, " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0 or not (rundir / produces).exists():
        _refusal(rundir)
        raise MeshToolError(
            "MESH_BUILD_FAILED",
            f"the om2d mesher {op} failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


def _refusal(rundir: Path) -> None:
    """Re-raise the driver's own typed refusal, when it wrote one.

    The refusals about the DOMAIN are only knowable where the library is; the
    driver writes the code, the reason and the escalation as a document so they
    reach a caller as a typed refusal rather than as a return code wrapped in a
    stack trace.
    """
    document = rundir / _REFUSAL_FILE
    if not document.exists():
        return
    read = json.loads(document.read_text())
    raise MeshToolError(str(read["code"]), str(read["message"]),
                        escalation=read.get("escalation"))


def _stats(rundir: Path) -> dict[str, Any]:
    try:
        return json.loads((rundir / "om2d_stats.json").read_text())
    except Exception:  # noqa: BLE001 -- an unreadable report says so, never guesses
        return {}


def _sizing_source(stats: Mapping[str, Any], domain: _Domain) -> str:
    """What ACTUALLY sized the mesh, copied from the mesher's own report."""
    active = [str(s) for s in (stats.get("sizing_functions") or [])]
    if not active:
        return f"{domain.source}; sizing functions unreported by the mesher"
    return f"{domain.source}; " + "; ".join(active)


# --------------------------------------------------------------------------- #
# The one topology pass, before any writer sees the mesh.
# --------------------------------------------------------------------------- #
def _clean_once(lonlat: Any, cells: Any) -> tuple[Any, Any, int]:
    """Orphan re-indexing, CCW normalization and the fusions a FILE forces.

    The library's own clean passes are ops and have already run; what is left is
    what the geometry FILE forces rather than what the mesh needs. A SELAFIN
    carries its coordinates in SINGLE precision and a UTM northing runs to seven
    digits, so two nodes a fraction of a metre apart are written as the same point
    and the element between them arrives at the solver with no area. A COLLAPSED
    element goes with them: a solver reads a zero determinant and stops, and one
    cell out of twenty-five thousand takes the whole run down. How many were
    dropped is reported rather than absorbed.
    """
    from trid3nt_server.workflows.mesh.shared.nodes import tin_formats

    formats = tin_formats()
    depths = _zeros(lonlat)
    points, cells, depths = formats._clean_and_orient(lonlat, cells, depths)
    points, cells, depths, merged = _merge_coincident(points, cells, depths)
    keep = _has_area(points, cells)
    collapsed = int((~keep).sum())
    if collapsed or merged:
        points, cells, depths = formats._clean_and_orient(points, cells[keep],
                                                          depths)
    return points, cells, collapsed + merged


def _zeros(points: Any) -> Any:
    import numpy as np

    return np.zeros(np.asarray(points).shape[0], dtype=float)


def _merge_coincident(points: Any, cells: Any,
                      depths: Any) -> tuple[Any, Any, Any, int]:
    """Fuse nodes closer together than the geometry file can tell apart."""
    import numpy as np
    from scipy.spatial import cKDTree

    xy = np.asarray(points, dtype=float)
    tol = _COINCIDENT_TOLERANCE_M / (111_320.0 * max(
        0.15, float(np.cos(np.radians(xy[:, 1].mean())))))
    pairs = cKDTree(xy).query_pairs(tol, output_type="ndarray")
    if pairs.size == 0:
        return points, cells, depths, 0
    keep_id = np.arange(xy.shape[0], dtype=np.int64)
    for high, low in np.sort(pairs, axis=1)[:, ::-1]:
        keep_id[high] = keep_id[low]
    merged = int((keep_id != np.arange(xy.shape[0])).sum())
    return points, keep_id[np.asarray(cells, dtype=np.int64)], depths, merged


def _has_area(points: Any, cells: Any) -> Any:
    """Which elements have area a solver can invert, relative to the median one."""
    import numpy as np

    xy = np.asarray(points, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    a, b, c = xy[tri[:, 0]], xy[tri[:, 1]], xy[tri[:, 2]]
    twice = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                   - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    median = float(np.median(twice))
    return twice > _COLLAPSED_AREA_FRAC * median if median > 0.0 else twice > 0.0


def _conformal_probe(points_m: Any, pfix: Any, utm_epsg: int) -> dict[str, Any]:
    """How far the mesh ended up from the outlines it was constrained to.

    Reported, never asserted: the distance in metres from each constrained outline
    vertex to the nearest node the mesh actually has. A build that constrained
    nothing reports nothing.
    """
    import numpy as np
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    pfix = np.asarray(pfix, dtype=float)
    if pfix.size == 0:
        return {}
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x, y = tr.transform(pfix[:, 0], pfix[:, 1])
    distance, _ = cKDTree(np.asarray(points_m, dtype=float)).query(
        np.column_stack([x, y]), k=1)
    return {"constrained_points": int(pfix.shape[0]),
            "breakline_offset_m": {
                "max": float(distance.max()),
                "median": float(np.median(distance)),
                "measured": "distance from each constrained outline vertex to the "
                            "nearest mesh node"}}


# --------------------------------------------------------------------------- #
# Emit: the per-solver geometry, written from what the ops left on the mesh.
# --------------------------------------------------------------------------- #
def _emitted(mesh: Mesh, rundir: Path, domain: _Domain,
             stats: Mapping[str, Any]) -> Mesh:
    """Write the per-solver geometry from ONE boundary segmentation -> the mesh.

    TELEMAC's SELAFIN and its ``.cli`` are written together by telapy, because a
    boundary-conditions file is only valid against the geometry whose boundary
    numbering it was written from. Only formats an engine READS are written.
    """
    import dataclasses

    from trid3nt_server.workflows.mesh.shared.nodes import boundary_contours
    from trid3nt_server.workflows.mesh.shared.selafin_cli import write_telemac_pair
    from trid3nt_server.workflows.mesh.topology import write_topology

    roles = {role: list(nodes) for role, nodes
             in dict(mesh.meta.get("boundary_roles") or {}).items() if nodes}
    sections = _open_sections(mesh)
    info: dict[str, Any] = {"source": domain.source}
    probes: dict[str, Any] = {
        "boundary_loops_measured": len(boundary_contours(mesh.cells))}
    runs = {role: int(count) for role, count
            in dict(mesh.meta.get("boundary_role_runs") or {}).items()}
    if sections:
        roles["open"] = [node for section in sections for node in section["nodes"]]
        runs["open"] = runs.get("open", 0) + len(sections)
        info.update({
            "open_boundary_sections": len(sections),
            "open_node_count": len(roles["open"]),
            "section_node_counts": [len(s["nodes"]) for s in sections],
            "section_rim": [s.get("rim") for s in sections],
            "section_mean_bed_m": [s["mean_bed_m"] for s in sections],
            "section_centroid": [s["centroid"] for s in sections],
            "identified_by": "oceanmesh.identify_ocean_boundary_sections",
            "designated_by": "om2d"})
        probes["open_boundary_sections"] = len(sections)
        probes["open_node_count"] = len(roles["open"])
    if roles:
        info["roles"] = {role: len(nodes) for role, nodes in roles.items()}
    if runs:
        # How many SECTIONS each role landed as, which is the number the solver's
        # own liquid-boundary numbering has to agree with.
        info["role_sections"] = runs
        probes["boundary_role_sections"] = runs

    files: dict[str, str] = {}
    pair = write_telemac_pair(
        rundir, x=mesh.points[:, 0], y=mesh.points[:, 1], cells=mesh.cells,
        bed=mesh.bed, roles=roles, title="TRID3NT OM2D MESH")
    files["slf_uri"] = str(pair["geo_slf"])
    files["cli_uri"] = str(pair["cli"])
    lb_order = list(pair["stats"].get("liquid_boundary_roles") or [])
    probes["liquid_boundaries"] = int(pair["stats"].get("n_liquid_boundaries", 0))
    probes["liquid_boundary_roles"] = lb_order
    probes["boundary_nodes_written"] = int(pair["stats"].get("nptfr", 0))
    if roles:
        # The two facts a SELAFIN cannot state - which stretch carries which role,
        # and the order the solver will number them in - ride beside it.
        files["topology_uri"] = str(write_topology(
            rundir, roles=roles, liquid_boundary_order=lb_order))
    artifact = {**dict(mesh.meta.get("artifact") or {}),
                "open_boundary_info": info}
    return dataclasses.replace(mesh, meta={
        **dict(mesh.meta), "files": files, "artifact": artifact,
        "probes": {**dict(mesh.meta.get("probes") or {}), **probes}})


def _open_sections(mesh: Mesh) -> list[dict[str, Any]]:
    """The contiguous ocean-boundary sections the library identified, if asked.

    A recipe that never named ``identify_ocean_boundary_sections`` has no open
    boundary, which is the right answer for an inland domain and not a missing
    one. EVERY section identified is open: which of them a compass name would have
    picked is a choice the library never made, and dropping the rest silently
    numbered a multi-mouth estuary as a single-mouth one.
    """
    found = (mesh.meta.get("op_results") or {}).get(
        "identify_ocean_boundary_sections")
    return [dict(section) for section in (found or [])]


# --------------------------------------------------------------------------- #
OM2D = register_mesher(
    "om2d",
    build,
    kinds=("unstructured_tri",),
    namespaces=(
        OpNamespace(origin="oceanmesh", phase=PRE, names=_OCEANMESH_SIZING),
        OpNamespace(origin="oceanmesh", phase=POST, names=_OCEANMESH_ON_A_MESH),
        OpNamespace(origin="om2d", phase=PRE, names=_OM2D_PRIMITIVES),
    ),
    default_ops=_DEFAULT_OPS,
    # Measured, not assumed: five in-container rebuilds from one identical config
    # return one mesh, on the domain classes the drift was measured on - a
    # shoreline-cut coastal domain and a harbour domain sized by feature. The
    # constraint that makes it hold is the driver's seeding of the library's own
    # medial-axis tie-break; without it the sizing lattice differs per process and
    # every domain whose recipe names a sizing op drifts.
    deterministic=True,
)
