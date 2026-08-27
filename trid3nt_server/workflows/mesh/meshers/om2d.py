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

An open boundary is a CONTIGUOUS stretch of the boundary walk, identified by
oceanmesh's own ``identify_ocean_boundary_sections`` from the bed: a solver reads
one liquid boundary as one continuous forcing edge, so a set of scattered nodes
that happens to sit on the same side of the domain is not a boundary. The same
sections number the ``.cli`` and the ``hgrid.gr3``.

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
    fetch_activation_rows,
    fetch_fallback_note,
    register_mesher,
)
from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.om2d")

__all__ = ["OM2D", "build"]

#: The GPL-isolated OceanMesh2D image and the driver mounted into it.
_MESH_IMAGE_DEFAULT = "trid3nt-local/mesh:latest"
_INCONTAINER_SCRIPT = "om2d_driver.py"
#: The shared TIN format writers still live beside the sandbox proofs that also
#: call them; they are IMPORTED here, never shelled.
_SANDBOX = "scripts/sandbox/oceanmesh"
_CONTAINER_TIMEOUT_S = 2400

#: The bathymetry rungs the bed tolerates: where CUDEM stops mid-AOI the global
#: ETOPO relief is a REAL bed - coarse, on another vertical datum, labeled.
_BED_FALLBACK = ("etopo_bathy_base",)

#: DistMesh seeds its initial point cloud from numpy's global generator, which
#: ``generate_mesh`` seeds itself from this value: one number is the whole
#: difference between a replayable recipe and a mesh that drifts per rebuild.
_SEED = 0

_BOUNDARY_SIDES = ("south", "north", "east", "west", "seaward")

#: Which coordinate a compass name ranks the identified sections on, and whether
#: the named direction is the high end of it.
_COMPASS = {"south": (1, False), "north": (1, True),
            "west": (0, False), "east": (0, True)}

#: The bed a boundary node must reach for oceanmesh to read it as ocean, and the
#: shortest run of them that counts as a section. Both are the library's own
#: defaults, and both are on the action so a shallow domain can state its own.
_DEPTH_THRESHOLD_M = -50.0
_MIN_SECTION_NODES = 10

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
    return dst, _bed_provenance(name, layer), fetch_fallback_note(layer)


def _bed_provenance(name: str, layer: Any) -> str:
    """What ACTUALLY painted the bed, from the ladder's own activation rows."""
    rows = fetch_activation_rows(layer)
    if rows:
        return f"{name}: " + ", ".join(
            f"{rung} {coverage * 100:.0f}%" for rung, coverage in rows)
    note = fetch_fallback_note(layer)
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
    _run_op(rundir, "build", "om2d_config.json", "om2d_mesh.npz",
            shoreline_dir=shoreline_dir)


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
        raise MeshToolError(
            "MESH_BUILD_FAILED",
            f"the om2d mesher {op} failed (rc={cp.returncode}):\n"
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
    numbering it was written from. SCHISM's ``hgrid.gr3`` carries its own boundary
    block and is written from the same node list.

    Only formats an engine READS are written: no worker here consumes an ADCIRC
    ``fort.14`` (the SWAN worker is regular-grid only), so the shared writer stays
    available and nothing calls it on a build.
    """
    import numpy as np

    from trid3nt_server.workflows.mesh.meshers.telapy_mesh import write_telemac_pair

    formats = _sandbox_formats()
    loops = formats.extract_boundary_loops(np.asarray(cells, dtype=np.int64))

    info: dict[str, Any] = {"source": "GSHHG shoreline domain"}
    probes: dict[str, Any] = {"boundary_loops_measured": len(loops)}
    sections: list[dict[str, Any]] = []
    if boundary is not None and str(boundary.get("type", "open")) == "open":
        sections, evidence = _open_sections(rundir, lonlat, cells, bed_up, boundary)
        info.update({
            "requested_side": str(boundary["side"]),
            "open_boundary_sections": len(sections),
            "open_node_count": sum(len(s["nodes"]) for s in sections),
            "section_node_counts": [len(s["nodes"]) for s in sections],
            "section_mean_bed_m": [s["mean_bed_m"] for s in sections],
            "section_centroid": [s["centroid"] for s in sections],
            "sections_offered": [
                {"nodes": s["node_count"], "mean_bed_m": s["mean_bed_m"],
                 "centroid": s["centroid"]} for s in evidence["sections"]],
            "identified_by": evidence["library"],
            "depth_threshold_m": evidence["depth_threshold_m"],
            "min_nodes_threshold": evidence["min_nodes_threshold"],
            "sections_identified": len(evidence["sections"]),
            "boundary_walks_measured": evidence["walk_node_counts"],
            "designated_by": "om2d"})
        probes["open_boundary_sections"] = len(sections)
        probes["open_node_count"] = int(info["open_node_count"])
    elif boundary is not None:
        info.update({"designation": "land",
                     "requested_side": str(boundary["side"]),
                     "designated_by": "om2d"})

    open_nodes = [n for s in sections for n in s["nodes"]]
    node_lists = [list(s["nodes"]) for s in sections]
    files: dict[str, str] = {}
    pair = write_telemac_pair(
        rundir, x=points_m[:, 0], y=points_m[:, 1], cells=cells, bed=bed_up,
        open_nodes=open_nodes, title="TRID3NT OM2D MESH")
    files["slf_uri"] = str(pair["geo_slf"])
    files["cli_uri"] = str(pair["cli"])
    probes["liquid_boundaries"] = int(pair["stats"].get("n_liquid_boundaries", 0))
    probes["boundary_nodes_written"] = int(pair["stats"].get("nptfr", 0))

    if node_lists and bed_up is not None:
        depth_down = -np.asarray(bed_up, dtype=float)
        gr3 = _gr3(lonlat, cells, depth_down, node_lists)
        if gr3 is not None:
            local = rundir / "hgrid.gr3"
            local.write_text(gr3, encoding="utf-8")
            files["gr3_uri"] = str(local)
    return files, info, probes


def _gr3(lonlat: Any, cells: Any, depth_down: Any,
         open_sections: list[list[int]]) -> str | None:
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
            open_sections=open_sections, clean_boundary=False)
    except Exception as exc:  # noqa: BLE001 -- the gr3 is an addition, never fatal
        logger.warning("om2d: hgrid.gr3 emission failed (%s); the mesh stays "
                       "TELEMAC-only", exc)
        return None


def _open_sections(rundir: Path, lonlat: Any, cells: Any, bed_up: Any,
                   boundary: Mapping[str, Any]) -> tuple[list[dict[str, Any]],
                                                         dict[str, Any]]:
    """The contiguous open-boundary sections this designation selects, and its evidence.

    Every candidate is a run of boundary nodes oceanmesh itself read as ocean, so
    a compass name SELECTS among those sections rather than cutting its own from a
    coordinate percentile: the one lying furthest in the named direction.
    ``seaward`` names no direction and takes the deepest. Either way every
    identified section, its centroid and its mean bed ride back in the evidence,
    so a section the choice left out is visible rather than silently dropped.
    """
    side = str(boundary["side"])
    if bed_up is None:
        raise MeshToolError(
            "MESH_OPEN_BOUNDARY_UNMEASURABLE",
            "an open boundary is identified from the bed and this mesh carries "
            "none, so no stretch of its boundary can be called ocean.")
    threshold = float(boundary.get("depth_threshold", _DEPTH_THRESHOLD_M))
    min_nodes = int(boundary.get("min_section_nodes", _MIN_SECTION_NODES))
    report = _identify_sections(rundir, lonlat, cells, bed_up, threshold, min_nodes)
    found = list(report["sections"])
    if not found:
        raise MeshToolError(
            "MESH_OPEN_BOUNDARY_UNIDENTIFIED",
            f"oceanmesh found no boundary stretch of at least {min_nodes} nodes at "
            f"or below {threshold} m on this mesh; its boundary bed runs "
            f"{report['boundary_bed_min_m']} m to {report['boundary_bed_max_m']} m, "
            "so state a depth_threshold this domain actually reaches.")
    if side == "seaward":
        chosen = [min(found, key=lambda s: s["mean_bed_m"])]
    else:
        axis, high = _COMPASS[side]
        pick = max if high else min
        chosen = [pick(found, key=lambda s: s["centroid"][axis])]
    return chosen, report


def _identify_sections(rundir: Path, lonlat: Any, cells: Any, bed_up: Any,
                       threshold: float, min_nodes: int) -> dict[str, Any]:
    """Run oceanmesh's own section identification in its box -> the report it wrote."""
    import numpy as np

    np.savez(rundir / "sections_in.npz",
             points=np.asarray(lonlat, dtype=float),
             cells=np.asarray(cells, dtype=np.int64),
             bed=np.asarray(bed_up, dtype=float))
    (rundir / "om2d_sections_config.json").write_text(json.dumps({
        "mesh_npz": "/data/sections_in.npz",
        "depth_threshold": threshold,
        "min_nodes_threshold": min_nodes}))
    _run_op(rundir, "ocean_boundary", "om2d_sections_config.json",
            "om2d_sections.json")
    return json.loads((rundir / "om2d_sections.json").read_text())


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


def _set_boundary(mesh: Mesh, *, side: str, type: str = "open",  # noqa: A002
                  depth_threshold: float = _DEPTH_THRESHOLD_M,
                  min_section_nodes: int = _MIN_SECTION_NODES) -> Mesh:
    """Designate a side of the domain boundary as open water or as land."""
    state = _state_of(mesh)
    state["boundary"] = {"side": str(side), "type": str(type),
                         "depth_threshold": float(depth_threshold),
                         "min_section_nodes": int(min_section_nodes)}
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
                    doc="which side of the domain to classify; 'seaward' takes the "
                        "deepest identified section rather than a direction"),
                "type": MeshField(
                    "type", types=(str,), choices=("open", "land"), default="open",
                    doc="open - a liquid boundary a solve forces at; land - a "
                        "solid wall"),
                "depth_threshold": MeshField(
                    "depth_threshold", types=(int, float),
                    default=_DEPTH_THRESHOLD_M,
                    doc="the bed elevation (negative down) a boundary node must "
                        "reach to be read as ocean; a shallow domain states its "
                        "own or the designation refuses"),
                "min_section_nodes": MeshField(
                    "min_section_nodes", types=(int,), default=_MIN_SECTION_NODES,
                    doc="the shortest run of ocean nodes that counts as one "
                        "section")},
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
