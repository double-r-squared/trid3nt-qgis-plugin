"""The ``telapy_mesh`` mesher: an existing TELEMAC geometry, adopted and edited.

telapy generates no mesh - what it owns is the geometry itself: ``HermesFile``
reads a SELAFIN's nodes, connectivity and BOTTOM and writes them back beside the
``.cli`` that classifies its boundary, and ``pretel`` derives the boundary
contours and drops the nodes an edit orphans. So this mesher's build is an
ADOPTION: a mesh someone else authored - a published study's geometry, a
BlueKenue export, a mesh a previous run accepted - enters the tool through
TELEMAC's own reader and from there is editable, probeable and solvable like any
other.

Every operation runs inside ``trid3nt-local/telemac:latest``, the only place
telapy exists. The same box writes the ``.slf``/``.cli`` pair for any mesher that
asks, because a boundary-conditions file is only valid against the geometry whose
boundary numbering it was written from.

Conformality is MEASURED, never asserted: an obstacle is punched by removing the
elements inside it, so the hole follows existing edges and the offset from the
requested outline is reported in metres.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.mesh.meshers import (
    EditAction,
    Mesh,
    MeshField,
    MeshToolError,
    apply_layer_edits_action,
    register_mesher,
)
from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.telapy_mesh")

__all__ = ["TELAPY_MESH", "build", "write_telemac_pair"]

_TELEMAC_IMAGE_DEFAULT = "trid3nt-local/telemac:latest"
_INCONTAINER_SCRIPT = "telapy_mesh_driver.py"
_CONTAINER_TIMEOUT_S = 1800

_SIDES = ("south", "north", "east", "west")
_BOUNDARY_SIDES = _SIDES + ("seaward",)

_FIELDS = (
    MeshField("kind", types=(str,), choices=("unstructured_tri",),
              default="unstructured_tri",
              doc="unstructured_tri - a TELEMAC geometry is a triangulation"),
    MeshField("geometry", types=(str,), required=True, hashed=True,
              doc="path or uri of the TELEMAC geometry to adopt (.slf/SERAFIN)"),
    MeshField("crs_authid", types=(str,), required=True,
              doc="the CRS the geometry's coordinates are in (e.g. EPSG:32619); a "
                  "SELAFIN records none, so it must be stated"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Read an existing TELEMAC geometry through telapy -> the neutral mesh."""
    import numpy as np

    crs_authid = str(spec["crs_authid"]).strip()
    rundir = _rundir()
    local = _stage_input(str(spec["geometry"]), rundir)
    stats = _run(rundir, "read", {
        "geometry": f"/data/{local.name}", "out_npz": "/data/mesh.npz"})
    x, y, cells, bed, contours = _load(rundir / "mesh.npz")

    points = np.column_stack([x, y])
    return _mesh(points, cells, bed, crs_authid, contours,
                 build={"geometry": str(spec["geometry"]),
                        "crs_authid": crs_authid},
                 boundary=None, rundir=rundir,
                 provenance={
                     "adopted_from": str(spec["geometry"]),
                     "geometry_title": stats.get("title") or "(untitled)",
                     "geometry_variables": stats.get("variables") or [],
                     "reader": "telapy HermesFile (SERAFIN)",
                     "dem_source": ("bed: the adopted geometry's own BOTTOM"
                                    if bed is not None else
                                    "bed: NOT PRESENT - the adopted geometry "
                                    "carries no BOTTOM variable")},
                 probes={"contours_measured": len(contours),
                         "elements_removed": 0, "nodes_inserted": 0})


# --------------------------------------------------------------------------- #
# The neutral mesh, and the files a mesh carries.
# --------------------------------------------------------------------------- #
def _mesh(points: Any, cells: Any, bed: Any, crs_authid: str, contours: Any, *,
          build: Mapping[str, Any], boundary: Any, rundir: Path,
          provenance: Mapping[str, Any], probes: Mapping[str, Any]) -> Mesh:
    import numpy as np

    open_nodes: list[int] = []
    info: dict[str, Any] = {"source": "adopted TELEMAC geometry"}
    if boundary is not None and str(boundary.get("type", "open")) == "open":
        side = _resolve_side(str(boundary["side"]), points, contours, bed)
        open_nodes = _nodes_on_side(points, contours, side)
        info.update({"open_boundary_side": side,
                     "requested_side": str(boundary["side"]),
                     "open_node_count": len(open_nodes),
                     "designated_by": "telapy_mesh"})
    elif boundary is not None:
        info.update({"designation": "land",
                     "requested_side": str(boundary["side"]),
                     "designated_by": "telapy_mesh"})

    pair = write_telemac_pair(
        rundir, x=points[:, 0], y=points[:, 1], cells=cells, bed=bed,
        open_nodes=open_nodes, title="TRID3NT TELAPY MESH")
    lonlat = _to_lonlat(points, crs_authid)
    return Mesh(
        points=np.asarray(points, dtype=float), cells=cells,
        crs_authid=crs_authid, bed=bed,
        meta={
            "utm_epsg": _utm_epsg(crs_authid),
            "lonlat_bbox": (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()),
                            float(lonlat[:, 0].max()), float(lonlat[:, 1].max())),
            "build": {**dict(build),
                      "boundary": (dict(boundary) if boundary else None)},
            "files": {"slf_uri": str(pair["geo_slf"]), "cli_uri": str(pair["cli"])},
            "probes": {
                **dict(probes),
                "liquid_boundaries": int(pair["stats"].get("n_liquid_boundaries", 0)),
                "boundary_nodes_written": int(pair["stats"].get("nptfr", 0)),
                **({"open_node_count": len(open_nodes)} if open_nodes else {}),
            },
            "artifact": {
                "engine_compat": ["telemac"] if bed is not None else [],
                "open_boundary_info": info,
                "provenance": {**dict(provenance),
                               "writer": "telapy HermesFile set_mesh + set_bnd; "
                                         "liquid boundaries numbered by "
                                         "Conlim.set_numliq"},
            },
            "synthetic_inputs": [
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / {cells.shape[0]} elements",
                 "basis": "derived",
                 "real_source_if_any": str(provenance.get("adopted_from", ""))},
                {"param": "mesh_bed",
                 "value": str(provenance.get("dem_source", "")),
                 "basis": "fetched" if bed is not None else "derived",
                 "consequence": "physics",
                 "real_source_if_any": str(provenance.get("adopted_from", ""))},
            ],
        })


def write_telemac_pair(rundir: Path | str, *, x: Any, y: Any, cells: Any,
                       bed: Any, open_nodes: Any = (),
                       title: str = "TRID3NT MESH") -> dict[str, Any]:
    """Write the SELAFIN geometry and its ``.cli`` through telapy -> the two paths.

    The pair is written together on purpose: the ``.cli`` rows are ordered by the
    geometry's own IPOBO, so a boundary file written against a different node
    numbering silently classifies the wrong nodes.
    """
    import numpy as np

    rundir = Path(rundir)
    npz = rundir / "telapy_in.npz"
    np.savez(npz, x=np.asarray(x, dtype=float), y=np.asarray(y, dtype=float),
             ikle=np.asarray(cells, dtype=np.int64),
             bottom=(np.asarray(bed, dtype=float) if bed is not None
                     else np.empty(0)),
             contour_nodes=np.empty(0, dtype=np.int64),
             contour_lengths=np.empty(0, dtype=np.int64))
    stats = _run(rundir, "write", {
        "mesh_npz": f"/data/{npz.name}", "geo_slf": "/data/mesh.slf",
        "cli": "/data/mesh.cli", "title": title,
        "open_nodes": [int(n) for n in open_nodes]})
    return {"geo_slf": rundir / "mesh.slf", "cli": rundir / "mesh.cli",
            "stats": stats}


# --------------------------------------------------------------------------- #
# The box.
# --------------------------------------------------------------------------- #
def _rundir() -> Path:
    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _stage_input(source: str, rundir: Path) -> Path:
    """Put the adopted geometry where the box can read it."""
    from trid3nt_server.tools.cache import read_object_bytes_s3

    text = str(source).strip()
    suffix = Path(text).suffix.lower() or ".slf"
    local = rundir / f"input{suffix}"
    if text.startswith("s3://"):
        local.write_bytes(read_object_bytes_s3(text))
        return local
    path = Path(text)
    if not path.exists():
        raise MeshToolError(
            "MESH_GEOMETRY_UNREADABLE",
            f"the geometry {source!r} is neither an object-store uri nor a file on "
            "disk, so there is nothing to adopt.")
    local.write_bytes(path.read_bytes())
    return local


def _run(rundir: Path, op: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """One telapy operation in its box -> the stats it reported."""
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE") or _TELEMAC_IMAGE_DEFAULT
    name = f"telapy_{op}.json"
    (rundir / name).write_text(json.dumps(dict(config)))
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{rundir}:/data",
        image, "python",
        f"/drivers/{_INCONTAINER_SCRIPT}", op, f"/data/{name}", "/data"]
    logger.info("telapy_mesh %s: %s", op, " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0:
        raise MeshToolError(
            "MESH_TELAPY_FAILED",
            f"the telapy {op} operation failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    return json.loads((rundir / "telapy_stats.json").read_text())


def _stage_mesh(rundir: Path, name: str, mesh: Mesh) -> Path:
    """Write the mesh in hand into the rundir in the box's own array shape."""
    import numpy as np

    points = np.asarray(mesh.points, dtype=float)
    npz = rundir / name
    np.savez(npz, x=points[:, 0], y=points[:, 1],
             ikle=np.asarray(mesh.cells, dtype=np.int64),
             bottom=(np.asarray(mesh.bed, dtype=float) if mesh.has_bed
                     else np.empty(0)),
             contour_nodes=np.empty(0, dtype=np.int64),
             contour_lengths=np.empty(0, dtype=np.int64))
    return npz


def _load(npz_path: Path) -> tuple[Any, Any, Any, Any, list[list[int]]]:
    import numpy as np

    npz = np.load(npz_path)
    x = np.asarray(npz["x"], dtype=float)
    y = np.asarray(npz["y"], dtype=float)
    cells = np.asarray(npz["ikle"], dtype=np.int64)
    bed = np.asarray(npz["bottom"], dtype=float)
    bed = bed if bed.shape[0] == x.shape[0] else None
    flat = np.asarray(npz["contour_nodes"], dtype=np.int64)
    contours: list[list[int]] = []
    at = 0
    for length in np.asarray(npz["contour_lengths"], dtype=np.int64):
        contours.append([int(v) for v in flat[at:at + int(length)]])
        at += int(length)
    return x, y, cells, bed, contours


# --------------------------------------------------------------------------- #
# Coordinates and boundary sides.
# --------------------------------------------------------------------------- #
def _utm_epsg(crs_authid: str) -> int | None:
    text = str(crs_authid).upper().replace("EPSG:", "").strip()
    code = int(text) if text.isdigit() else 0
    return code if 32600 < code < 32800 else None


def _to_lonlat(points: Any, crs_authid: str) -> Any:
    import numpy as np
    from pyproj import Transformer

    pts = np.asarray(points, dtype=float)
    if str(crs_authid).upper() == "EPSG:4326":
        if (np.abs(pts[:, 0]).max() > 180.0 or np.abs(pts[:, 1]).max() > 90.0):
            raise MeshToolError(
                "MESH_CRS_MISMATCH",
                f"the adopted geometry was declared EPSG:4326 but its coordinates "
                f"reach ({pts[:, 0].max():.1f}, {pts[:, 1].max():.1f}), which no "
                "lon/lat can; name the projected CRS the geometry is actually in.")
        return pts
    tr = Transformer.from_crs(str(crs_authid), 4326, always_xy=True)
    lon, lat = tr.transform(pts[:, 0], pts[:, 1])
    return np.column_stack([lon, lat])


def _nodes_on_side(points: Any, contours: Any, side: str) -> list[int]:
    """The exterior-contour nodes on one side of the domain."""
    import numpy as np

    if not contours:
        return []
    exterior = list(contours[0])
    xy = np.asarray(points, dtype=float)[exterior]
    axis, high = {"south": (1, False), "north": (1, True),
                  "west": (0, False), "east": (0, True)}[side]
    values = xy[:, axis]
    keep = (values >= np.percentile(values, 85) if high
            else values <= np.percentile(values, 15))
    return [exterior[i] for i in np.where(keep)[0]]


def _resolve_side(side: str, points: Any, contours: Any, bed: Any) -> str:
    """A named side, or the one the bed says faces open water."""
    import numpy as np

    if side != "seaward":
        return side
    if bed is None:
        raise MeshToolError(
            "MESH_SEAWARD_UNMEASURABLE",
            "'seaward' is resolved from the bed and this mesh carries none; name "
            f"the side outright ({', '.join(_SIDES)}).")
    elevation = np.asarray(bed, dtype=float)
    means = {}
    for candidate in _SIDES:
        nodes = _nodes_on_side(points, contours, candidate)
        if nodes:
            means[candidate] = float(elevation[nodes].mean())
    if not means:
        raise MeshToolError(
            "MESH_SEAWARD_UNMEASURABLE",
            "no boundary nodes were found on any side, so the seaward edge cannot "
            "be measured.")
    return min(means, key=lambda k: means[k])


# --------------------------------------------------------------------------- #
# Edit actions.
# --------------------------------------------------------------------------- #
def _metres_per_unit(mesh: Mesh) -> float:
    """The mesh's own coordinate unit, in metres."""
    import math

    import numpy as np

    if str(mesh.crs_authid).upper() != "EPSG:4326":
        return 1.0
    lat = float(np.asarray(mesh.points, dtype=float)[:, 1].mean())
    return 111_320.0 * max(0.15, math.cos(math.radians(lat)))


def _edit(mesh: Mesh, op: str, geometry: str, extra: Mapping[str, Any],
          probe_keys: Mapping[str, str]) -> Mesh:
    """Run one geometry-driven telapy op on the mesh in hand -> the new mesh."""
    import numpy as np

    from trid3nt_server.workflows.mesh.meshers.om2d import read_geometry

    state = dict(mesh.meta.get("build") or {})
    if not state.get("crs_authid"):
        raise MeshToolError(
            "MESH_EDIT_UNSUPPORTED",
            "this mesh carries no telapy_mesh build state, so its coordinates "
            "cannot be interpreted for an edit.")
    rundir = _rundir()
    npz = _stage_mesh(rundir, "telapy_edit.npz", mesh)
    (rundir / "geometry.geojson").write_text(json.dumps(read_geometry(geometry)))
    stats = _run(rundir, op, {
        "mesh_npz": f"/data/{npz.name}", "out_npz": "/data/mesh.npz",
        "geometry": "/data/geometry.geojson", **dict(extra)})
    x, y, cells, bed, contours = _load(rundir / "mesh.npz")
    new_points = np.column_stack([x, y])

    previous = dict(mesh.meta.get("artifact", {}).get("provenance") or {})
    carried = {k: v for k, v in dict(mesh.meta.get("probes") or {}).items()
               if k in ("elements_removed", "nodes_inserted")}
    probes = {"contours_measured": len(contours), **carried,
              **{key: int(carried.get(key, 0)) + int(stats.get(source, 0))
                 for key, source in probe_keys.items()},
              **(_offset_probe(new_points, geometry, mesh) if op == "punch"
                 else {})}
    return _mesh(new_points, cells, bed, str(state["crs_authid"]), contours,
                 build=state, boundary=state.get("boundary"), rundir=rundir,
                 provenance=previous, probes=probes)


def _offset_probe(points: Any, geometry: str, mesh: Mesh) -> dict[str, Any]:
    """How far the punched hole sits from the outline it was asked to follow.

    Element removal follows the edges the mesh already had, so the cut lands on
    existing nodes rather than on the requested polyline. This is the size of that
    gap, in metres - a measurement, not a conformality claim.
    """
    import numpy as np
    from scipy.spatial import cKDTree
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    from trid3nt_server.workflows.mesh.meshers.om2d import read_geometry

    doc = read_geometry(geometry)
    feats = doc.get("features") if isinstance(doc, dict) else None
    geoms = ([_shape(f["geometry"]) for f in feats if f.get("geometry")] if feats
             else [_shape(doc)])
    outline = unary_union(geoms).boundary
    coords = np.asarray(getattr(outline, "coords", None) or
                        [c for part in getattr(outline, "geoms", []) for c in part.coords],
                        dtype=float)
    if coords.size == 0:
        return {}
    lonlat = _to_lonlat(points, str(mesh.crs_authid))
    scale = _metres_per_unit(mesh)
    if str(mesh.crs_authid).upper() == "EPSG:4326":
        query = coords * scale
        tree = cKDTree(np.asarray(lonlat, dtype=float) * scale)
    else:
        query = coords
        tree = cKDTree(np.asarray(points, dtype=float))
    distance, _ = tree.query(query, k=1)
    return {"outline_offset_m": {
        "max": float(distance.max()), "median": float(np.median(distance)),
        "measured": "distance from each outline vertex to the nearest mesh node"}}


def _add_obstacle(mesh: Mesh, *, geometry: str) -> Mesh:
    """Remove the elements the obstacle covers, and re-derive the boundary."""
    return _edit(mesh, "punch", geometry, {},
                 {"elements_removed": "elements_removed"})


def _refine_region(mesh: Mesh, *, geometry: str, edge_length: float) -> Mesh:
    """Insert nodes at the requested spacing inside a region and re-triangulate."""
    return _edit(mesh, "refine", geometry,
                 {"edge_length": float(edge_length) / _metres_per_unit(mesh)},
                 {"nodes_inserted": "nodes_inserted"})


def _set_boundary(mesh: Mesh, *, side: str, type: str = "open") -> Mesh:  # noqa: A002
    """Classify a side of the boundary and rewrite the ``.cli`` from it."""
    import numpy as np

    state = dict(mesh.meta.get("build") or {})
    if not state.get("crs_authid"):
        raise MeshToolError(
            "MESH_EDIT_UNSUPPORTED",
            "this mesh carries no telapy_mesh build state, so its coordinates "
            "cannot be interpreted for an edit.")
    rundir = _rundir()
    npz = _stage_mesh(rundir, "telapy_edit.npz", mesh)
    _run(rundir, "contours", {"mesh_npz": f"/data/{npz.name}",
                              "out_npz": "/data/contours.npz"})
    x, y, cells, bed, contours = _load(rundir / "contours.npz")
    return _mesh(np.column_stack([x, y]), cells, bed, str(state["crs_authid"]),
                 contours, build=state,
                 boundary={"side": str(side), "type": str(type)}, rundir=rundir,
                 provenance=dict(mesh.meta.get("artifact", {}).get("provenance")
                                 or {}),
                 probes={k: v for k, v in dict(mesh.meta.get("probes") or {}).items()
                         if k in ("elements_removed", "nodes_inserted")})


TELAPY_MESH = register_mesher(
    "telapy_mesh",
    build,
    actions=(
        EditAction(
            name="add_obstacle", apply=_add_obstacle,
            inputs={"geometry": MeshField(
                "geometry", types=(str,), required=True, hashed=True,
                doc="path or uri of the polygon(s) to remove from the mesh")},
            doc="Remove the elements an obstacle covers and re-derive the "
                "boundary around the hole."),
        EditAction(
            name="refine_region", apply=_refine_region,
            inputs={
                "geometry": MeshField(
                    "geometry", types=(str,), required=True, hashed=True,
                    doc="path or uri of the region to refine inside"),
                "edge_length": MeshField(
                    "edge_length", types=(int, float), required=True,
                    doc="the target node spacing inside the region, in metres")},
            doc="Insert nodes at a finer spacing inside a region and "
                "re-triangulate."),
        EditAction(
            name="set_boundary", apply=_set_boundary,
            inputs={
                "side": MeshField(
                    "side", types=(str,), required=True, choices=_BOUNDARY_SIDES,
                    doc="which side of the domain to classify; 'seaward' is "
                        "measured from the bed"),
                "type": MeshField(
                    "type", types=(str,), choices=("open", "land"), default="open",
                    doc="open - a liquid boundary a solve forces at; land - a "
                        "solid wall")},
            doc="Classify a side of the boundary and rewrite the .cli from it."),
        apply_layer_edits_action(),
    ),
    fields=_FIELDS,
)
