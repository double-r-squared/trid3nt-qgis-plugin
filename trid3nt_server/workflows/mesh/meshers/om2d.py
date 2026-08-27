"""The ``om2d`` mesher: OceanMesh2D, wrapped where it lives.

The domain is a real shoreline: the GSHHG land polygons cut to the AOI, turned
into a signed distance function, sized by distance to the shore and - when a bed
is fetched - by shallow-water wavelength over depth, gradation-limited, and
triangulated by DistMesh. All of that is the CHLNDDEV ``oceanmesh`` port's own
code, running in ``trid3nt-local/mesh:latest`` where it is installed; this file
composes the ask, shells the box, and turns what comes back into the one neutral
mesh every writer reads.

Its edit actions are the shape a coastal domain is authored in: punch an obstacle
out of the water and lock its outline into the mesh, refine inside a drawn
region, designate the seaward boundary. An obstacle and a region both REBUILD -
the sizing function and the distance function are inputs to DistMesh, not
post-processing - so the mesh stays one converged triangulation rather than a
patched one.

Conformality is MEASURED, never asserted: the obstacle outline goes in as
DistMesh's constrained ``pfix`` points and the offset that survives is reported
in the probes, in metres.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.mesh.meshers import (
    EditAction,
    Mesh,
    MeshField,
    MeshToolError,
    apply_layer_edits_action,
    checked_refine,
    register_mesher,
)

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.om2d")

__all__ = ["OM2D", "build"]

#: The GPL-isolated OceanMesh2D image and the driver mounted into it.
_MESH_IMAGE_DEFAULT = "trid3nt-local/mesh:latest"
_INCONTAINER_SCRIPT = "_om2d_incontainer.py"
_SANDBOX = "scripts/sandbox/oceanmesh"
_CONTAINER_TIMEOUT_S = 2400

#: The bathymetry rungs the bed tolerates: where CUDEM stops mid-AOI the global
#: ETOPO relief is a REAL bed - coarse, on another vertical datum, labeled.
_BED_FALLBACK = ("etopo_bathy_base",)

#: DistMesh seeds its initial point cloud from numpy's global generator, which
#: ``generate_mesh`` seeds itself from this value: one number is the whole
#: difference between a replayable recipe and a mesh that drifts per rebuild.
_SEED = 0

_SIDES = ("south", "north", "east", "west")
_BOUNDARY_SIDES = _SIDES + ("seaward",)

#: The refine knobs, and what each one means to the sizing function.
_REFINE_KNOBS = {"edge_length": 400.0, "min_spacing": 40.0, "gradation": 0.15}

_FIELDS = (
    MeshField("kind", types=(str,), choices=("unstructured_tri",),
              default="unstructured_tri",
              doc="unstructured_tri - the water side of the shoreline is triangulated"),
    MeshField("aoi", types=(tuple, list), required=True,
              doc="(min_lon, min_lat, max_lon, max_lat) the domain is cut from"),
    MeshField("refine", types=(dict,),
              doc="{'edge_length': the coarsest background edge in metres, "
                  "'min_spacing': the finest edge at the shore in metres, "
                  "'gradation': how fast the two may transition (0.15-0.35)}"),
    MeshField("bed", types=(str, dict),
              default="fetch_topobathy",
              doc="what paints the node elevations: a raster fetcher's name, or a "
                  "uri/path to a raster already fetched. The bed also drives the "
                  "wavelength sizing term"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Mesh the water side of the shoreline across the AOI."""
    return _realize({
        "aoi": tuple(float(v) for v in spec["aoi"]),
        "refine": checked_refine("mesher 'om2d'", spec.get("refine"),
                                 _REFINE_KNOBS),
        "bed": spec.get("bed") or "fetch_topobathy",
        "obstacles": [],
        "regions": [],
        "boundary": None,
    })


# --------------------------------------------------------------------------- #
# The build itself.
# --------------------------------------------------------------------------- #
def _realize(state: Mapping[str, Any]) -> Mesh:
    import numpy as np

    from trid3nt_server.workflows.mesh.watershed import (
        reproject_nodes_to_utm,
        sample_raster_at_nodes,
    )

    aoi = tuple(float(v) for v in state["aoi"])
    refine = dict(state["refine"])
    if refine["min_spacing"] > refine["edge_length"]:
        raise MeshToolError(
            "MESH_SPEC_BAD_VALUE",
            f"mesher 'om2d': refine min_spacing {refine['min_spacing']} m is "
            f"coarser than edge_length {refine['edge_length']} m; min_spacing is "
            "the finest edge at the shore and edge_length the coarsest offshore.")
    rundir = _rundir()
    shoreline = _shoreline_shp()
    dem_path, bed_provenance, fallback_note = _bed_raster(
        state["bed"], aoi, rundir)

    config: dict[str, Any] = {
        "bbox": list(aoi),
        "shoreline_shp": f"/shoreline/{shoreline.name}",
        "dem_path": "/data/bed.tif" if dem_path is not None else None,
        "min_edge_length_m": refine["min_spacing"],
        "max_edge_length_m": refine["edge_length"],
        "gradation": refine["gradation"],
        "seed": _SEED,
        "obstacles": [{"geojson": f"/data/{name}", "constrain": True}
                      for name in _stage_geometries(
                          rundir, state["obstacles"], "obstacle")],
        "refine_regions": [
            {"geojson": f"/data/{name}",
             "edge_length_m": float(region["edge_length"])}
            for name, region in zip(
                _stage_geometries(rundir, [r["geometry"] for r in state["regions"]],
                                  "region"),
                state["regions"])],
        "max_iter": 40,
    }
    (rundir / "om2d_config.json").write_text(json.dumps(config))
    _run_container(rundir, shoreline.parent)

    npz = np.load(rundir / "om2d_mesh.npz")
    lonlat = np.asarray(npz["points"], dtype=float)
    cells = np.asarray(npz["cells"], dtype=np.int64)
    pfix = np.asarray(npz["pfix"], dtype=float)
    bed_up = (sample_raster_at_nodes(dem_path, lonlat) if dem_path is not None
              else None)

    lonlat, cells, bed_up = _clean_once(lonlat, cells, bed_up)
    points, utm_epsg = reproject_nodes_to_utm(lonlat)

    files, boundary_info, boundary_probes = _emit_formats(
        rundir, lonlat=lonlat, cells=cells, points_m=points, bed_up=bed_up,
        boundary=state["boundary"])
    stats = _stats(rundir)
    engine_compat = ["telemac"] if bed_up is not None else []
    if "gr3_uri" in files:
        engine_compat.append("schism")

    return Mesh(
        points=points, cells=cells, crs_authid=f"EPSG:{int(utm_epsg)}", bed=bed_up,
        meta={
            "utm_epsg": int(utm_epsg),
            "lonlat_bbox": (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()),
                            float(lonlat[:, 0].max()), float(lonlat[:, 1].max())),
            "build": _carry(state),
            "files": files,
            "fallback_note": fallback_note,
            "probes": {
                "obstacles": len(state["obstacles"]),
                "refine_regions": len(state["regions"]),
                **_conformal_probe(points, pfix, int(utm_epsg)),
                **({"clean_notes": list(stats["clean_notes"])}
                   if stats.get("clean_notes") else {}),
                **boundary_probes,
            },
            "artifact": {
                "engine_compat": engine_compat,
                "open_boundary_info": boundary_info,
                "provenance": {
                    "mesher_library": stats.get("engine", "oceanmesh (unreported)"),
                    "min_spacing_m": refine["min_spacing"],
                    "edge_length_m": refine["edge_length"],
                    "gradation": refine["gradation"],
                    "seed": _SEED,
                    "sizing_source": _sizing_source(stats, shoreline),
                    "dem_source": bed_provenance,
                    "bed_fallback_note": fallback_note,
                    "shoreline_source": f"GSHHG land polygons ({shoreline.name})",
                },
            },
            "synthetic_inputs": [
                {"param": "min_spacing_m", "value": refine["min_spacing"],
                 "units": "m", "basis": "user"},
                {"param": "edge_length_m", "value": refine["edge_length"],
                 "units": "m", "basis": "user"},
                {"param": "gradation", "value": refine["gradation"],
                 "basis": "user"},
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / {cells.shape[0]} elements",
                 "basis": "derived",
                 "real_source_if_any": _sizing_source(stats, shoreline)},
                {"param": "mesh_bed", "value": bed_provenance, "basis": "fetched",
                 "consequence": "physics", "real_source_if_any": bed_provenance,
                 "note": "the elevation every node carries; a solver reads it as "
                         "the domain's bathymetry"},
            ],
        })


def _carry(state: Mapping[str, Any]) -> dict[str, Any]:
    """The rebuild state, as plain values an edit can extend."""
    return {
        "aoi": tuple(float(v) for v in state["aoi"]),
        "refine": dict(state["refine"]),
        "bed": state["bed"],
        "obstacles": list(state["obstacles"]),
        "regions": [dict(r) for r in state["regions"]],
        "boundary": (dict(state["boundary"]) if state["boundary"] else None),
    }


def _rundir() -> Path:
    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _repo_root() -> Path:
    # .../trid3nt_server/workflows/mesh/meshers/om2d.py
    return Path(__file__).resolve().parents[4]


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


def _bed_raster(bed: Any, aoi: tuple[float, ...],
                rundir: Path) -> tuple[Path | None, str, str | None]:
    """Stage the node bed as an EPSG:4326 raster -> ``(path, provenance, note)``.

    EPSG:4326 on purpose: the in-container wavelength sizer queries the raster's
    own grid with lon/lat, so a projected bed would put every query out of bounds
    and read its fill value as depth.
    """
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    if isinstance(bed, Mapping):
        bed = bed.get("uri") or bed.get("path") or ""
    name = str(bed).strip()
    if not name:
        return None, "bed: NOT SAMPLED - no bed was declared", None

    layer: Any = None
    if name in TOOL_REGISTRY:
        layer = TOOL_REGISTRY[name].fn(
            bbox=tuple(aoi), target_crs="EPSG:4326", fallback=_BED_FALLBACK)
        uri = layer.uri if hasattr(layer, "uri") else layer["uri"]
    elif name.startswith("s3://") or Path(name).exists():
        uri = name
    else:
        raise MeshToolError(
            "MESH_BED_UNRESOLVED",
            f"mesher 'om2d': bed {name!r} names neither a registered fetcher nor a "
            "readable raster, so the mesh has no elevation to carry.")
    dst = rundir / "bed.tif"
    dst.write_bytes(read_object_bytes_s3(uri) if str(uri).startswith("s3://")
                    else Path(uri).read_bytes())
    return dst, _bed_provenance(name, layer), getattr(layer, "fallback_note", None)


def _bed_provenance(name: str, layer: Any) -> str:
    """What ACTUALLY painted the bed, from the ladder's own activation rows."""
    rows = [r for r in (getattr(layer, "fallbacks", None) or []) if r.coverage > 0.0]
    if rows:
        return f"{name}: " + ", ".join(
            f"{r.rung} {r.coverage * 100:.0f}%" for r in rows)
    note = getattr(layer, "fallback_note", None)
    if note:
        return f"{name} ({note})"
    if layer is None:
        return f"bed raster supplied directly: {name}"
    return (f"{name} (source UNMEASURED: the fetch reported no activation rows)")


def _stage_geometries(rundir: Path, sources: Any, tag: str) -> list[str]:
    """Write each geometry source into the rundir as GeoJSON -> their filenames."""
    names: list[str] = []
    for index, source in enumerate(sources):
        name = f"{tag}_{index}.geojson"
        (rundir / name).write_text(json.dumps(read_geometry(source)))
        names.append(name)
    return names


def read_geometry(source: Any) -> dict[str, Any]:
    """A geometry source -> GeoJSON, whatever vector format it arrived in.

    A source is a path or uri the recipe records and can re-read, so a drawn
    polygon, a fetched breakwater layer and a file on disk all enter the same way.
    """
    from trid3nt_server.tools.cache import read_object_bytes_s3

    text = str(source).strip()
    if text.startswith("{"):
        return json.loads(text)
    if text.startswith("s3://"):
        raw = read_object_bytes_s3(text)
        suffix = Path(text).suffix.lower()
        if suffix in (".geojson", ".json"):
            return json.loads(raw.decode("utf-8"))
        local = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"geom-{new_ulid()}{suffix}"
        local.write_bytes(raw)
        text = str(local)
    path = Path(text)
    if not path.exists():
        raise MeshToolError(
            "MESH_GEOMETRY_UNREADABLE",
            f"the geometry {source!r} could not be read: it is neither inline "
            "GeoJSON, an object-store uri, nor a file on disk.")
    if path.suffix.lower() in (".geojson", ".json"):
        return json.loads(path.read_text())
    import geopandas as gpd

    return json.loads(gpd.read_file(path).to_crs(4326).to_json())


def _run_container(rundir: Path, shoreline_dir: Path) -> None:
    sandbox = _repo_root() / _SANDBOX
    image = os.environ.get("TRID3NT_MESH_IMAGE") or _MESH_IMAGE_DEFAULT
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{sandbox}:/sandbox", "-v", f"{rundir}:/data",
        "-v", f"{shoreline_dir}:/shoreline:ro",
        "--entrypoint", "python", image,
        f"/sandbox/{_INCONTAINER_SCRIPT}", "/data/om2d_config.json", "/data"]
    logger.info("om2d mesher: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0 or not (rundir / "om2d_mesh.npz").exists():
        raise MeshToolError(
            "MESH_BUILD_FAILED",
            f"the om2d mesher failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


def _stats(rundir: Path) -> dict[str, Any]:
    try:
        return json.loads((rundir / "om2d_stats.json").read_text())
    except Exception:  # noqa: BLE001 -- an unreadable report says so, never guesses
        return {}


def _sizing_source(stats: Mapping[str, Any], shoreline: Path) -> str:
    """What ACTUALLY sized the mesh, copied from the mesher's own report."""
    domain = f"GSHHG shoreline domain ({shoreline.name})"
    active = [str(s) for s in (stats.get("sizing_functions") or [])]
    if not active:
        return f"{domain}; sizing functions unreported by the mesher"
    return f"{domain}; " + "; ".join(active)


def _sandbox_formats() -> Any:
    """The repo's shared TIN format writers, importable from the agent venv."""
    for path in (str(_repo_root() / _SANDBOX), str(_repo_root() / "workers/schism")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import mesh_formats  # type: ignore

    return mesh_formats


def _clean_once(lonlat: Any, cells: Any, bed_up: Any) -> tuple[Any, Any, Any]:
    """ONE topology pass, before any writer sees the mesh.

    Pinch cleaning, orphan re-indexing and CCW normalization run here so every
    format is written from the SAME node numbering and the boundary is segmented
    once; each writer's own cleaning pass then finds nothing left to do.
    """
    import numpy as np

    depths = (np.zeros(lonlat.shape[0], dtype=float) if bed_up is None
              else np.asarray(bed_up, dtype=float))
    points, cells, depths = _sandbox_formats()._clean_and_orient(
        lonlat, cells, depths)
    return points, cells, (None if bed_up is None else depths)


def _emit_formats(rundir: Path, *, lonlat: Any, cells: Any, points_m: Any,
                  bed_up: Any, boundary: Any) -> tuple[dict[str, str],
                                                       dict[str, Any], dict[str, Any]]:
    """Write the per-solver geometry from one boundary segmentation.

    TELEMAC's SELAFIN and its ``.cli`` are written together by telapy, because a
    boundary-conditions file is only valid against the geometry whose boundary
    numbering it was written from. SCHISM's ``hgrid.gr3`` and the ADCIRC
    ``fort.14`` carry their own boundary blocks and are written from the same
    node list.
    """
    import numpy as np

    from trid3nt_server.workflows.mesh.meshers.telapy_mesh import write_telemac_pair

    formats = _sandbox_formats()
    loops = formats.extract_boundary_loops(np.asarray(cells, dtype=np.int64))
    exterior = loops[0] if loops else []

    info: dict[str, Any] = {"source": "GSHHG shoreline domain"}
    probes: dict[str, Any] = {"boundary_loops_measured": len(loops)}
    side = None
    open_nodes: list[int] = []
    if boundary is not None and str(boundary.get("type", "open")) == "open":
        side, side_bed = _resolve_side(str(boundary["side"]), lonlat, exterior,
                                       bed_up, formats)
        open_nodes = formats._open_nodes_on_side(lonlat, exterior, side)
        info.update({"open_boundary_side": side,
                     "requested_side": str(boundary["side"]),
                     "open_node_count": len(open_nodes),
                     "designated_by": "om2d"})
        if side_bed:
            # Which side the bed said was deepest, and by how much: a domain that
            # touches two water bodies can open onto the wrong one, and the numbers
            # the choice was made from are what let a reader see it.
            info["side_mean_bed_m"] = side_bed
        probes["open_node_count"] = len(open_nodes)
    elif boundary is not None:
        info.update({"designation": "land",
                     "requested_side": str(boundary["side"]),
                     "designated_by": "om2d"})

    files: dict[str, str] = {}
    pair = write_telemac_pair(
        rundir, x=points_m[:, 0], y=points_m[:, 1], cells=cells, bed=bed_up,
        open_nodes=open_nodes, title="TRID3NT OM2D MESH")
    files["slf_uri"] = str(pair["geo_slf"])
    files["cli_uri"] = str(pair["cli"])
    probes["liquid_boundaries"] = int(pair["stats"].get("n_liquid_boundaries", 0))
    probes["boundary_nodes_written"] = int(pair["stats"].get("nptfr", 0))

    if open_nodes and bed_up is not None:
        depth_down = -np.asarray(bed_up, dtype=float)
        gr3 = _gr3(lonlat, cells, depth_down, side)
        if gr3 is not None:
            local = rundir / "hgrid.gr3"
            local.write_text(gr3, encoding="utf-8")
            files["gr3_uri"] = str(local)
        fort14 = rundir / "fort.14"
        fort14.write_text(formats.write_fort14(
            lonlat, cells, depths=depth_down, grid_name="trid3nt_om2d",
            open_boundary_side=side), encoding="utf-8")
        files["fort14_uri"] = str(fort14)
    return files, info, probes


def _gr3(lonlat: Any, cells: Any, depth_down: Any, side: str) -> str | None:
    """The SCHISM geometry, through the repo's one gr3 writer.

    Best-effort: the mesh is complete without it, so a bridge failure leaves the
    mesh TELEMAC-only rather than failing a build that already succeeded.
    """
    import numpy as np

    try:
        from trid3nt_server.workflows.schism.deck_authoring import load_gr3_bridge

        return load_gr3_bridge().tin_to_hgrid(
            np.asarray(lonlat, dtype=float), np.asarray(cells, dtype=np.int64),
            depth=np.asarray(depth_down, dtype=float), grid_name="trid3nt_om2d",
            open_boundary_side=side, clean_boundary=False)
    except Exception as exc:  # noqa: BLE001 -- the gr3 is an addition, never fatal
        logger.warning("om2d: hgrid.gr3 emission failed (%s); the mesh stays "
                       "TELEMAC-only", exc)
        return None


def _resolve_side(side: str, lonlat: Any, exterior: Any, bed_up: Any,
                  formats: Any) -> tuple[str, dict[str, float]]:
    """A named side, or the one the bed says faces open water, and the evidence.

    ``seaward`` is not a compass direction, so it is MEASURED: the side whose
    boundary nodes sit deepest is the one the domain opens onto. The per-side mean
    comes back with it, because on a domain touching two water bodies the deepest
    side is not always the intended one.
    """
    import numpy as np

    if side != "seaward":
        return side, {}
    if bed_up is None:
        raise MeshToolError(
            "MESH_SEAWARD_UNMEASURABLE",
            "'seaward' is resolved from the bed and this mesh carries none; name "
            f"the side outright ({', '.join(_SIDES)}).")
    bed = np.asarray(bed_up, dtype=float)
    depths = {}
    for candidate in _SIDES:
        nodes = formats._open_nodes_on_side(lonlat, exterior, candidate)
        if nodes:
            depths[candidate] = round(float(bed[nodes].mean()), 2)
    if not depths:
        raise MeshToolError(
            "MESH_SEAWARD_UNMEASURABLE",
            "no boundary nodes were found on any side, so the seaward edge cannot "
            "be measured.")
    return min(depths, key=lambda k: depths[k]), depths


def _conformal_probe(points_m: Any, pfix: Any, utm_epsg: int) -> dict[str, Any]:
    """How far the mesh ended up from the breaklines it was constrained to.

    Reported, never asserted: the distance in metres from each constrained
    outline vertex to the nearest node the mesh actually has. A build with no
    obstacles constrains nothing and reports nothing.
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
# Edit actions.
# --------------------------------------------------------------------------- #
def _state_of(mesh: Mesh) -> dict[str, Any]:
    state = mesh.meta.get("build")
    if not state:
        raise MeshToolError(
            "MESH_EDIT_UNSUPPORTED",
            "this mesh carries no om2d build state, so it cannot be rebuilt with "
            "an edit; a hand-edited layer is adopted as it stands.")
    return _carry(state)


def _add_obstacle(mesh: Mesh, *, geometry: str) -> Mesh:
    """Punch a geometry out of the water and constrain its outline into the mesh."""
    state = _state_of(mesh)
    state["obstacles"] = [*state["obstacles"], geometry]
    return _realize(state)


def _refine_region(mesh: Mesh, *, geometry: str, edge_length: float) -> Mesh:
    """Re-mesh with a finer target edge inside a region."""
    state = _state_of(mesh)
    state["regions"] = [*state["regions"],
                        {"geometry": geometry, "edge_length": float(edge_length)}]
    return _realize(state)


def _set_boundary(mesh: Mesh, *, side: str, type: str = "open") -> Mesh:  # noqa: A002
    """Designate a side of the domain boundary as open water or as land."""
    state = _state_of(mesh)
    state["boundary"] = {"side": str(side), "type": str(type)}
    return _realize(state)


OM2D = register_mesher(
    "om2d",
    build,
    actions=(
        EditAction(
            name="add_obstacle", apply=_add_obstacle,
            inputs={"geometry": MeshField(
                "geometry", types=(str,), required=True, hashed=True,
                doc="path or uri of the polygon(s) to remove from the water")},
            doc="Remove an obstacle from the domain, its outline constrained into "
                "the mesh."),
        EditAction(
            name="refine_region", apply=_refine_region,
            inputs={
                "geometry": MeshField(
                    "geometry", types=(str,), required=True, hashed=True,
                    doc="path or uri of the region to refine inside"),
                "edge_length": MeshField(
                    "edge_length", types=(int, float), required=True,
                    doc="the target triangle edge inside the region, in metres")},
            doc="Re-mesh with a finer target edge inside a region."),
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
            doc="Classify a side of the boundary as open water or as land."),
        apply_layer_edits_action(),
    ),
    fields=_FIELDS,
    # Measured, not assumed: three in-container rebuilds from one identical config
    # (same AOI, same staged bed, same shoreline, same seed) returned two distinct
    # meshes, so a replay of an om2d recipe rebuilds an equivalent mesh rather than
    # the same one.
    deterministic=False,
)
