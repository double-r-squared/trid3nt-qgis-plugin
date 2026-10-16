"""The FloPy meshers: ``triangle``, ``voronoi`` and ``gridgen``, one facade.

Three registrations and nothing else: op rosters, constructor build params and
the config each box is handed. FloPy and the two executables live in the mesh
image beside the oceanmesh ones; this file composes the ask in the domain's
own UTM metres, shells the box, and reads back the one neutral mesh - so a
triangle mesh is equally a TELEMAC mesh and a quadtree one is a DISV waiting
for its layers."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.tools.mesh.inputs import finest_edge, op_geometry, op_input
from trid3nt_server.tools.mesh.meshers import (
    POST,
    PRE,
    BoundOp,
    Mesh,
    MeshToolError,
    OpNamespace,
    bind_ops,
    register_mesher,
)
from trid3nt_server.workflows.solver.image_script import run_image_script

__all__ = ["GRIDGEN", "TRIANGLE", "VORONOI", "build"]

#: The mesh image (an ENVIRONMENT: oceanmesh plus FloPy plus the two
#: executables) and the script mounted into it.
_MESH_IMAGE_DEFAULT = "trid3nt-local/mesh:latest"
_INCONTAINER_SCRIPT = "flopy_grid.py"
#: What the driver writes when it refuses in its own words: code, reason, and
#: the call that DOES what the refused ask could not.
_REFUSAL_FILE = "flopy_refusal.json"
_CONTAINER_TIMEOUT_S = 600

#: Triangle's own methods, VERBATIM, all pre-build: they write the .poly file
#: the executable reads. The DOMAIN rings are config, not an op - the extent is
#: the recipe's own param - so a declared add_polygon adds a component beside it.
_TRIANGLE_OPS = ("add_polygon", "add_hole", "add_region")
#: Gridgen's own methods, VERBATIM, all pre-build: they write the .dfn inputs
#: the executable reads. With none, the base rectilinear grid IS the mesh.
_GRIDGEN_OPS = ("add_active_domain", "add_refinement_features")

#: The constructors' own keyword arguments, declared like an op roster.
_TRIANGLE_BUILD = ("maximum_area", "angle", "nodes", "additional_args")
_GRIDGEN_BUILD = ("delr", "delc", "xoff", "yoff", "angrot")


def build(recipe: Any) -> Mesh:
    """Cut this recipe's domain with Triangle, its Voronoi dual, or Gridgen."""
    from trid3nt_server.tools.mesh.meshers import get_mesher

    mesher = get_mesher(recipe.mesher)
    ops = bind_ops(mesher, recipe.ops)
    rundir = _rundir()
    outer, holes, source, bbox = _domain(recipe.extent, mesher.name)
    edge, edge_note = finest_edge(recipe.resolution_m, recipe.ops, bbox)
    rings_m, epsg = _project(outer + holes)
    outer_m, holes_m = rings_m[:len(outer)], rings_m[len(outer):]
    bounds_m = _bounds(rings_m)

    config: dict[str, Any] = {
        "mesher": mesher.name,
        "outer_rings": outer_m,
        "holes": holes_m,
        "crs_epsg": epsg,
        "resolution_m": edge,
        "domain_source": source,
        "ops": [_staged(op, rundir, index, epsg)
                for index, op in enumerate(ops) if op.phase == PRE],
    }
    if mesher.name in ("triangle", "voronoi"):
        stated = recipe.build.get("maximum_area")
        config.update({
            "maximum_area": float(edge * edge) if stated is None
            else float(stated),
            "maximum_area_source": "resolution_m squared" if stated is None
            else "stated",
            **{key: recipe.build[key] for key in ("angle", "nodes",
                                                   "additional_args")
               if key in recipe.build},
        })
    else:
        config.update(_gridgen_base(recipe.build, bounds_m, edge))
    (rundir / "flopy_config.json").write_text(json.dumps(config))
    _run_op(rundir, "build", "flopy_config.json", "flopy_mesh.npz")

    mesh = _read_built(rundir, epsg, edge, source,
                       stated=recipe.resolution_m not in (None, ""), ops=ops)
    for op in ops:
        if op.phase == POST:
            mesh = op.fn(mesh, **{name: op_input(value)
                                  for name, value in op.kwargs.items()})
    if edge_note:
        mesh = dataclasses.replace(mesh, meta={**dict(mesh.meta),
                                                "edge_notes": [edge_note]})
    return mesh


def _gridgen_base(build: Mapping[str, Any],
                  bounds: tuple[float, float, float, float],
                  edge: float) -> dict[str, Any]:
    """The base StructuredGrid's words: stated under ``build``, else extent +
    resolution. Cell counts are derived here - one home, never in the driver."""
    minx, miny, maxx, maxy = bounds
    delr, delc = build.get("delr"), build.get("delc")
    ncol = (len(delr) if isinstance(delr, (list, tuple))
            else max(1, int(round((maxx - minx)
                                  / (float(delr) if delr is not None
                                     else edge)))))
    nrow = (len(delc) if isinstance(delc, (list, tuple))
            else max(1, int(round((maxy - miny)
                                  / (float(delc) if delc is not None
                                     else edge)))))
    return {
        "delr": [edge] * ncol if delr is None else
        ([float(delr)] * ncol if isinstance(delr, (int, float))
         else [float(v) for v in delr]),
        "delc": [edge] * nrow if delc is None else
        ([float(delc)] * nrow if isinstance(delc, (int, float))
         else [float(v) for v in delc]),
        "xoff": float(build.get("xoff", minx)),
        "yoff": float(build.get("yoff", miny)),
        "angrot": float(build.get("angrot", 0.0)),
        "sources": {"delr": "stated" if delr is not None else "resolution_m",
                    "delc": "stated" if delc is not None else "resolution_m",
                    "xoff": "stated" if "xoff" in build else "extent",
                    "yoff": "stated" if "yoff" in build else "extent"},
    }


def _domain(extent: Any, mesher: str
            ) -> tuple[list, list, str, tuple[float, float, float, float]]:
    """The recipe's extent as ``(outer rings, holes, source, lonlat bbox)``.

    A BOX is a domain here: a rectangular aquifer is a rectangle, and the
    structured examples arrive as one."""
    if extent is None:
        raise MeshToolError(
            "MESH_EXTENT_MISSING",
            f"mesher {mesher!r} cuts its domain from the recipe's extent and "
            "this recipe declares none; give it the closed outline the "
            "equations are solved over (a polygon, or a rectangular box).")
    if (isinstance(extent, (tuple, list)) and len(extent) == 4
            and all(isinstance(v, (int, float)) for v in extent)):
        west, south, east, north = (float(v) for v in extent)
        ring = [[west, south], [east, south], [east, north], [west, north]]
        return [ring], [], (f"extent box ({west:g}, {south:g}, {east:g}, "
                            f"{north:g})"), _lonlat_bbox([ring])

    polygons = _polygons(op_geometry(extent))
    if not polygons:
        raise MeshToolError(
            "MESH_DOMAIN_NOT_A_POLYGON",
            f"the extent {extent!r} carries no polygon, so there is no "
            "interior to mesh. Give it the closed outline the equations are "
            "solved over - drawn, the user's own layer, or one a producer "
            "measured.")
    from shapely.geometry import shape as _shape

    outers: list[list[list[float]]] = []
    holes: list[list[list[float]]] = []
    for document in polygons:
        geometry = _shape(document)
        parts = geometry.geoms if geometry.geom_type == "MultiPolygon" \
            else [geometry]
        for part in parts:
            outers.append([[float(x), float(y)]
                           for x, y in part.exterior.coords])
            holes.extend([[float(x), float(y)]
                          for interior in part.interiors
                          for x, y in interior.coords])
    bbox = _lonlat_bbox(outers + holes)
    return outers, holes, f"domain polygon ({len(outers)} part(s))", bbox


def _polygons(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every polygon geometry a supplied domain document carries."""
    out: list[Mapping[str, Any]] = []

    def walk(geometry: Any) -> None:
        if not isinstance(geometry, Mapping):
            return
        kind = str(geometry.get("type") or "")
        if kind in ("Polygon", "MultiPolygon"):
            out.append(dict(geometry))
        elif kind == "GeometryCollection":
            for part in geometry.get("geoms") or geometry.get("geometries") or ():
                walk(part)

    features = document.get("features") if isinstance(document, Mapping) else None
    if features is not None:
        for feature in features:
            walk((feature or {}).get("geometry"))
    else:
        walk(document.get("geometry") if "geometry" in document else document)
    return out


def _lonlat_bbox(rings: list[list[list[float]]]) -> tuple[float, float,
                                                           float, float]:
    """The box the rings occupy, checked to BE lon/lat."""
    xs = [x for ring in rings for x, _y in ring]
    ys = [y for ring in rings for _x, y in ring]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    # Planar metres staged as a domain would be projected a second time and
    # land millions of cells away, failing as an allocation error rather than
    # as the CRS mismatch it is.
    if not (-180.0 <= bbox[0] <= 180.0 and -180.0 <= bbox[2] <= 180.0
            and -90.0 <= bbox[1] <= 90.0 and -90.0 <= bbox[3] <= 90.0):
        raise MeshToolError(
            "MESH_DOMAIN_NOT_LONLAT",
            f"the extent spans {bbox}, which is outside the lon/lat range this "
            "mesher works in - it is projected coordinates, most likely metres. "
            "Reproject the domain to EPSG:4326 before handing it over.")
    return bbox


def _bounds(rings: list[list[list[float]]]) -> tuple[float, float, float, float]:
    """The box the rings occupy, in whatever units they are already in."""
    xs = [x for ring in rings for x, _y in ring]
    ys = [y for ring in rings for _x, y in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _project(rings: list[list[list[float]]]) -> tuple[list, int]:
    """The rings in the domain's own UTM zone -> ``(rings_m, epsg)``.

    Triangle and Gridgen are planar and know no CRS, so the whole build - the
    domain, every staged geometry, the returned nodes - is one projection."""
    from trid3nt_server.tools.mesh.shared.nodes import reproject_nodes_to_utm

    import numpy as np

    flat = np.array([point for ring in rings for point in ring], dtype=float)
    projected, epsg = reproject_nodes_to_utm(flat)
    out, cursor = [], 0
    for ring in rings:
        out.append([[float(x), float(y)]
                    for x, y in projected[cursor:cursor + len(ring)]])
        cursor += len(ring)
    return out, int(epsg)


def _staged(op: BoundOp, rundir: Path, index: int, epsg: int) -> dict[str, Any]:
    """One op as the container reads it: its name and its kwargs, geometry
    projected into the mesh's own zone and staged under /data.

    Code-as-data: the name travels verbatim and the driver calls it verbatim.
    A non-geometry value - a layer list, a level, an attribute - passes through."""
    kwargs: dict[str, Any] = {}
    for name, value in op.kwargs.items():
        converted = op_input(value)
        if _is_geojson(converted):
            projected = _project_geojson(converted, epsg)
            local = rundir / f"op{index}_{name}.geojson"
            local.write_text(json.dumps(projected))
            kwargs[name] = f"/data/{local.name}"
        else:
            kwargs[name] = converted
    return {"fn": op.name, "kwargs": kwargs}


def _is_geojson(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "type" in value
    return isinstance(value, list) and bool(value) and all(
        isinstance(entry, Mapping) and "type" in entry for entry in value)


def _project_geojson(value: Any, epsg: int) -> Any:
    """Any position pair inside the document, through the mesh's projection."""
    from pyproj import Transformer

    transform = Transformer.from_crs(4326, epsg, always_xy=True)

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {key: walk(part) for key, part in node.items()
                    if key != "bbox"}
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                x, y = transform.transform(float(node[0]), float(node[1]))
                return [x, y, *[float(v) for v in node[2:]]]
            return [walk(part) for part in node]
        return node

    return walk(value)


def _read_built(rundir: Path, epsg: int, edge: float, source: str,
                stated: bool, ops: tuple[BoundOp, ...]) -> Mesh:
    """The container's arrays as the neutral mesh, in the zone they were built."""
    import numpy as np

    npz = np.load(rundir / "flopy_mesh.npz")
    points = np.asarray(npz["points"], dtype=float)
    if "cells" in npz.files:
        cells: Any = np.asarray(npz["cells"], dtype=np.int64)
    else:
        counts = np.asarray(npz["cell_counts"], dtype=np.int64)
        flat = np.asarray(npz["cells_flat"], dtype=np.int64)
        cells, cursor = [], 0
        for count in counts:
            cells.append(np.asarray(flat[cursor:cursor + int(count)],
                                    dtype=np.int64))
            cursor += int(count)
    stats = json.loads((rundir / "flopy_stats.json").read_text())
    from pyproj import Transformer

    # The acceptance places a mesh on the map by its lon/lat extent, which a mesh built in UTM states itself.
    lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(
        points[:, 0], points[:, 1])
    return Mesh(
        points=points, cells=cells, crs_authid=f"EPSG:{epsg}", bed=None,
        meta={
            "utm_epsg": epsg,
            "lonlat_bbox": (float(np.min(lon)), float(np.min(lat)),
                            float(np.max(lon)), float(np.max(lat))),
            "resolution_m": edge,
            "domain_source": source,
            "probes": dict(stats.get("probes") or {}),
            "artifact": {
                "provenance": {
                    "mesher_library": stats.get("library"),
                    "mesher": stats.get("mesher"),
                    "resolution_m": edge,
                    "crs": f"EPSG:{epsg}",
                    "domain_source": source,
                    **({"maximum_area_m2": stats["maximum_area"],
                        "maximum_area_source": stats["maximum_area_source"]}
                       if "maximum_area" in stats else {}),
                    **({"build_sources": stats["sources"]}
                       if "sources" in stats else {}),
                    "op_notes": [op.note for op in ops if op.note],
                },
            },
            "synthetic_inputs": [
                {"param": "resolution_m", "value": edge, "units": "m",
                 "basis": "user" if stated else "derived"},
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / "
                          f"{stats.get('element_count', 0)} elements",
                 "basis": "derived",
                 "real_source_if_any": source},
            ],
        })


def _rundir() -> Path:
    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _run_op(rundir: Path, op: str, config_name: str, produces: str) -> None:
    """One script op in the mesh box, or a typed refusal carrying its output."""
    image = os.environ.get("TRID3NT_MESH_IMAGE") or _MESH_IMAGE_DEFAULT
    cp = run_image_script(
        image=image, engine="mesh", script=_INCONTAINER_SCRIPT,
        rundir=rundir, argv=[op, f"/data/{config_name}", "/data"],
        timeout_s=_CONTAINER_TIMEOUT_S, entrypoint_override=True)
    if cp.returncode != 0 or not (rundir / produces).exists():
        _refusal(rundir)
        raise MeshToolError(
            "MESH_BUILD_FAILED",
            f"the flopy_grid mesher {op} failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


def _refusal(rundir: Path) -> None:
    """Re-raise the driver's own typed refusal, when it wrote one.

    The refusals about the DOMAIN are only knowable where the library is."""
    document = rundir / _REFUSAL_FILE
    if not document.exists():
        return
    read = json.loads(document.read_text())
    raise MeshToolError(str(read["code"]), str(read["message"]),
                        escalation=read.get("escalation"))


TRIANGLE = register_mesher(
    "triangle",
    build,
    kinds=("unstructured_tri",),
    namespaces=(OpNamespace(origin="triangle", phase=PRE,
                            names=_TRIANGLE_OPS),),
    default_ops=(),
    build_params=_TRIANGLE_BUILD,
)

VORONOI = register_mesher(
    "voronoi",
    build,
    kinds=("voronoi",),
    # Voronoi polygons are the DUAL of the same Triangle build, so they take
    # Triangle's ops verbatim and its constructor's params beside them.
    namespaces=(OpNamespace(origin="triangle", phase=PRE,
                            names=_TRIANGLE_OPS),),
    default_ops=(),
    build_params=_TRIANGLE_BUILD,
)

GRIDGEN = register_mesher(
    "gridgen",
    build,
    kinds=("quadtree",),
    namespaces=(OpNamespace(origin="gridgen", phase=PRE,
                            names=_GRIDGEN_OPS),),
    # No default ops: with no refinement the base rectilinear grid IS the mesh,
    # which is how a structured example reaches DISV through this one path.
    default_ops=(),
    build_params=_GRIDGEN_BUILD,
)
