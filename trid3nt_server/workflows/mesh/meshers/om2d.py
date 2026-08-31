"""The ``om2d`` mesher: OceanMesh2D, wrapped where it lives.

The domain arrives one of two ways. From a BBOX it is a real shoreline: the
GSHHG land polygons cut to the AOI, turned into a signed distance function,
sized by distance to the shore and - when a bed is fetched - by shallow-water
wavelength over depth, gradation-limited, and triangulated by DistMesh. From a
POLYGON it is that polygon's own interior, and the signed distance is measured
against its boundary; a basin, a sectioned river reach or any other narrowed
domain another tool produced is meshed as it stands rather than re-derived here. All of it is
the CHLNDDEV ``oceanmesh`` port's own code, running in ``trid3nt-local/mesh:
latest`` where it is installed; this file composes the ask, shells the box, and
turns what comes back into the one neutral mesh every writer reads.

Its edit actions are the shape a coastal domain is authored in: punch an obstacle
out of the water and lock its outline into the mesh, refine inside a drawn
region, designate the seaward boundary. An obstacle and a region both REBUILD -
the sizing function and the distance function are inputs to DistMesh, not
post-processing - so the mesh stays one converged triangulation rather than a
patched one.

An open boundary is a CONTIGUOUS stretch of the boundary walk, identified by
oceanmesh's own ``identify_ocean_boundary_sections`` from the bed: a solver reads
one liquid boundary as one continuous forcing edge, so a set of scattered nodes
that happens to sit on the same side of the domain is not a boundary. Those
sections number the ``.cli``.

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
from dataclasses import dataclass
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

#: The fraction of the MEDIAN element area below which an element has collapsed:
#: no area to invert, and a solver refuses the whole mesh over it.
_COLLAPSED_AREA_FRAC = 1e-9

#: How close two nodes may sit before a single-precision geometry file writes them
#: as one point, in metres. A UTM northing spends its mantissa on seven digits,
#: which leaves a fraction of a metre.
_COINCIDENT_TOLERANCE_M = 1.0

#: How far past the AOI the bed is fetched, as a fraction of each span. The mesh
#: has nodes ON the AOI corners and a raster's rim rows carry the warp's fill, so
#: the grid has to reach past where the domain ends.
_BED_MARGIN_FRAC = 0.02

#: The refine knobs, and what each one means to the sizing function.
#: ``resolution_m`` is THE granularity word: the finest edge the sizing function
#: is allowed, and - where nothing sizes the interior toward anything - the
#: uniform edge the whole domain is meshed at.
_REFINE_KNOBS = {"resolution_m": 40.0, "max_el": 400.0, "gradation": 0.15}

#: What the coarsest edge defaults to, as a MULTIPLE of the finest. A fixed metre
#: ceiling turns a coarse resolution into a refusal about a number the caller
#: never wrote; the multiple reproduces the shipped 40 m / 400 m pair when
#: neither knob is declared and moves with the resolution when one is.
_MAX_EL_FACTOR = 10.0

_FIELDS = (
    MeshField("kind", types=(str,), choices=("unstructured_tri",),
              default="unstructured_tri",
              doc="unstructured_tri - the water side of the shoreline is triangulated"),
    MeshField("extent", types=(tuple, list, dict, str), required=True,
              doc="what the domain is cut from: (min_lon, min_lat, max_lon, "
                  "max_lat) for the shoreline path, or a POLYGON - inline "
                  "GeoJSON, a geometry mapping, the uri of a polygon layer, or "
                  "the layer a chained row produced - whose interior is meshed "
                  "as it stands"),
    MeshField("refine", types=(dict,),
              doc="{'resolution_m': the finest edge in metres - at the shore on "
                  "the shoreline path, and the uniform edge a polygon interior "
                  "is meshed at, 'max_el': the coarsest background edge in "
                  "metres (defaults to 10x the resolution), 'gradation': how "
                  "fast the two may transition (0.15-0.35)}"),
    MeshField("bed", types=(str, dict),
              default="fetch_topobathy",
              doc="what paints the node elevations: a raster fetcher's name, or a "
                  "uri/path to a raster already fetched. The bed also drives the "
                  "wavelength sizing term"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Mesh the water side of the shoreline, or the interior of a supplied polygon."""
    extent = spec["extent"]
    return _realize({
        "extent": (tuple(float(v) for v in extent)
                   if isinstance(extent, (tuple, list)) else extent),
        "refine": checked_refine("mesher 'om2d'", spec.get("refine"),
                                 _refine_defaults(spec.get("refine"))),
        "bed": spec.get("bed") or "fetch_topobathy",
        "obstacles": [],
        "regions": [],
        "boundary": None,
    })


def _refine_defaults(refine: Any) -> dict[str, float]:
    """The knob defaults this ask is checked against, with the ceiling on the floor.

    A declared ``resolution_m`` with no ``max_el`` beside it moves the ceiling
    with it, so the one number a template states is never contradicted by a
    default it did not write.
    """
    given = dict(refine or {})
    declared = dict(_REFINE_KNOBS)
    finest = given.get("resolution_m")
    if "max_el" not in given and isinstance(finest, (int, float)) \
            and not isinstance(finest, bool):
        declared["max_el"] = _MAX_EL_FACTOR * float(finest)
    return declared


# --------------------------------------------------------------------------- #
# The build itself.
# --------------------------------------------------------------------------- #
def _realize(state: Mapping[str, Any]) -> Mesh:
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.nodes import (
        reproject_nodes_to_utm,
        sample_raster_at_nodes,
    )

    refine = dict(state["refine"])
    if refine["resolution_m"] > refine["max_el"]:
        raise MeshToolError(
            "MESH_SPEC_BAD_VALUE",
            f"mesher 'om2d': refine resolution_m {refine['resolution_m']} m is "
            f"coarser than max_el {refine['max_el']} m; resolution_m is the "
            "finest edge and max_el the coarsest background one.")
    rundir = _rundir()
    domain = _domain(state["extent"], rundir)
    aoi = domain.bbox
    if domain.polygon_name is not None and state["regions"]:
        raise MeshToolError(
            "MESH_REGION_ON_POLYGON_DOMAIN",
            "a polygon domain is sized from a distance callable rather than the "
            "sizing lattice a region refine is written onto, so this mesh has no "
            "grid for the region to land on. Refine the whole domain with the "
            "edge band, or mesh the region's own polygon as the domain.")
    dem_path, bed_provenance, fallback_note = _bed_raster(
        state["bed"], aoi, rundir)

    config: dict[str, Any] = {
        "bbox": list(aoi),
        "shoreline_shp": (f"/shoreline/{domain.shoreline.name}"
                          if domain.shoreline is not None else None),
        "domain_geojson": (f"/data/{domain.polygon_name}"
                           if domain.polygon_name is not None else None),
        "sizing_coords": domain.sizing_coords,
        "dem_path": "/data/bed.tif" if dem_path is not None else None,
        "min_edge_length_m": refine["resolution_m"],
        "max_edge_length_m": refine["max_el"],
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
    _run_container(rundir, domain.shoreline)

    npz = np.load(rundir / "om2d_mesh.npz")
    lonlat = np.asarray(npz["points"], dtype=float)
    cells = np.asarray(npz["cells"], dtype=np.int64)
    pfix = np.asarray(npz["pfix"], dtype=float)
    bed_up = (sample_raster_at_nodes(dem_path, lonlat) if dem_path is not None
              else None)

    lonlat, cells, bed_up, repaired = _clean_once(lonlat, cells, bed_up)
    points, utm_epsg = reproject_nodes_to_utm(lonlat)

    files, boundary_info, boundary_probes = _emit_formats(
        rundir, lonlat=lonlat, cells=cells, points_m=points, bed_up=bed_up,
        boundary=state["boundary"], domain_source=domain.source)
    stats = _stats(rundir)

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
                **({"degenerate_elements_repaired": repaired}
                   if repaired else {}),
                **({"clean_notes": list(stats["clean_notes"])}
                   if stats.get("clean_notes") else {}),
                **boundary_probes,
            },
            "artifact": {
                "open_boundary_info": boundary_info,
                "provenance": {
                    "mesher_library": stats.get("engine", "oceanmesh (unreported)"),
                    "resolution_m": refine["resolution_m"],
                    "max_el_m": refine["max_el"],
                    "gradation": refine["gradation"],
                    "seed": _SEED,
                    "sizing_source": _sizing_source(stats, domain),
                    "dem_source": bed_provenance,
                    "bed_fallback_note": fallback_note,
                    "domain_source": domain.source,
                },
            },
            "synthetic_inputs": [
                {"param": "resolution_m", "value": refine["resolution_m"],
                 "units": "m", "basis": "user"},
                {"param": "max_el_m", "value": refine["max_el"],
                 "units": "m", "basis": "user"},
                {"param": "gradation", "value": refine["gradation"],
                 "basis": "user"},
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / {cells.shape[0]} elements",
                 "basis": "derived",
                 "real_source_if_any": _sizing_source(stats, domain)},
                {"param": "mesh_bed", "value": bed_provenance, "basis": "fetched",
                 "consequence": "physics", "real_source_if_any": bed_provenance,
                 "note": "the elevation every node carries; a solver reads it as "
                         "the domain's bathymetry"},
            ],
        })


def _carry(state: Mapping[str, Any]) -> dict[str, Any]:
    """The rebuild state, as plain values an edit can extend.

    A supplied polygon is carried VERBATIM - a rebuild has to cut from the same
    domain the first build did, and reducing it to its bounding box here would
    quietly widen every edited mesh back out to a rectangle.
    """
    extent = state["extent"]
    return {
        "extent": (tuple(float(v) for v in extent)
                   if isinstance(extent, (tuple, list)) else extent),
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


@dataclass(frozen=True)
class _Domain:
    """What the mesh is cut from, resolved: the shoreline, or a supplied polygon.

    Exactly one of ``shoreline`` and ``polygon_name`` is set. ``bbox`` is the
    lon/lat box the bed is fetched over and the triangulator seeds inside - the
    AOI itself on the shoreline path, the polygon's own bounds on the other.
    ``sizing_coords`` are the polylines supplied ALONGSIDE a domain polygon,
    which the interior is refined toward; empty means the interior is meshed at
    a uniform edge.
    """

    bbox: tuple[float, float, float, float]
    source: str
    shoreline: Path | None = None
    polygon_name: str | None = None
    sizing_coords: Any = ()


def _domain(extent: Any, rundir: Path) -> _Domain:
    """Resolve the ask's extent into the domain the box cuts the mesh from."""
    if isinstance(extent, (tuple, list)):
        shoreline = _shoreline_shp()
        return _Domain(bbox=tuple(float(v) for v in extent),
                       source=f"GSHHG land polygons ({shoreline.name})",
                       shoreline=shoreline)
    polygons, lines = _split_geometry(read_geometry(extent))
    if not polygons:
        raise MeshToolError(
            "MESH_DOMAIN_NOT_A_POLYGON",
            f"the extent {extent!r} carries no polygon, so there is no interior "
            "to mesh; supply a bbox, or a polygon another tool produced.")
    name = "domain.geojson"
    (rundir / name).write_text(json.dumps(
        {"type": "GeometryCollection", "geometries": polygons}))
    return _Domain(bbox=_geometry_bounds(polygons),
                   source=f"supplied polygon domain ({len(polygons)} part(s))",
                   polygon_name=name, sizing_coords=lines)


def _split_geometry(doc: Mapping[str, Any]) -> tuple[list[dict[str, Any]],
                                                     list[list[float]]]:
    """A GeoJSON document -> its polygon geometries and its polyline vertices.

    Both halves come out of ONE supplied geometry on purpose: a domain polygon
    and the channel network inside it are the same acquisition, and separating
    them into two fields would let a chain hand over a sizing source for a
    domain it did not describe.
    """
    polygons: list[dict[str, Any]] = []
    lines: list[list[float]] = []

    def walk(geometry: Any) -> None:
        if not isinstance(geometry, Mapping):
            return
        kind = str(geometry.get("type") or "")
        if kind in ("Polygon", "MultiPolygon"):
            polygons.append(dict(geometry))
        elif kind == "LineString":
            lines.extend([float(c[0]), float(c[1])]
                         for c in geometry.get("coordinates") or ())
        elif kind == "MultiLineString":
            for part in geometry.get("coordinates") or ():
                lines.extend([float(c[0]), float(c[1])] for c in part)
        elif kind == "GeometryCollection":
            for part in geometry.get("geometries") or ():
                walk(part)

    features = doc.get("features") if isinstance(doc, Mapping) else None
    if features is not None:
        for feature in features:
            walk((feature or {}).get("geometry"))
    else:
        walk(doc.get("geometry") if "geometry" in doc else doc)
    return polygons, lines


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


def _bed_raster(bed: Any, aoi: tuple[float, ...],
                rundir: Path) -> tuple[Path | None, str, str | None]:
    """Stage the node bed as an EPSG:4326 raster -> ``(path, provenance, note)``.

    EPSG:4326 on purpose: the in-container wavelength sizer queries the raster's
    own grid with lon/lat, so a projected bed would put every query out of bounds
    and read its fill value as depth.

    The bed is fetched over a MARGIN around the AOI, because the mesh puts nodes
    exactly on the AOI's own corners and a raster's outermost rows are where a
    warp writes its fill: sampled there, an 18 m deep boundary reads as sea level
    and the ocean-boundary identification then finds its open water somewhere
    else entirely.
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
            bbox=_bed_bbox(aoi), target_crs="EPSG:4326", fallback=_BED_FALLBACK)
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


def _bed_bbox(aoi: tuple[float, ...]) -> tuple[float, float, float, float]:
    """The AOI grown by :data:`_BED_MARGIN_FRAC` of its own span, in degrees."""
    dx = (float(aoi[2]) - float(aoi[0])) * _BED_MARGIN_FRAC
    dy = (float(aoi[3]) - float(aoi[1])) * _BED_MARGIN_FRAC
    return (float(aoi[0]) - dx, float(aoi[1]) - dy,
            float(aoi[2]) + dx, float(aoi[3]) + dy)


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
    A LAYER a chain produced enters the same way too: a declared ``extent`` is
    bound to whatever the producing tool returned, and refusing the layer while
    accepting the uri it carries would make a chain depend on the author
    remembering to write ``.uri``.
    """
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.tools.processing._geometry_common import source_uri

    source = source_uri(source)
    if isinstance(source, Mapping):
        return dict(source)
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


def _run_container(rundir: Path, shoreline: Path | None) -> None:
    _run_op(rundir, "build", "om2d_config.json", "om2d_mesh.npz",
            shoreline_dir=None if shoreline is None else shoreline.parent)


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


def _sizing_source(stats: Mapping[str, Any], domain: _Domain) -> str:
    """What ACTUALLY sized the mesh, copied from the mesher's own report."""
    active = [str(s) for s in (stats.get("sizing_functions") or [])]
    if not active:
        return f"{domain.source}; sizing functions unreported by the mesher"
    return f"{domain.source}; " + "; ".join(active)


def _sandbox_formats() -> Any:
    """The repo's shared TIN format writers, importable from the agent venv."""
    path = str(_repo_root() / _SANDBOX)
    if path not in sys.path:
        sys.path.insert(0, path)
    import mesh_formats  # type: ignore

    return mesh_formats


def _clean_once(lonlat: Any, cells: Any,
                bed_up: Any) -> tuple[Any, Any, Any, int]:
    """ONE topology pass, before any writer sees the mesh.

    Pinch cleaning, orphan re-indexing and CCW normalization run here so every
    format is written from the SAME node numbering and the boundary is segmented
    once; each writer's own cleaning pass then finds nothing left to do.

    A COLLAPSED element goes with them. Constraining an outline whose vertices sit
    closer together than the finest edge can leave a triangle with no area at all,
    and a solver reads that as a negative determinant and stops: one cell out of
    twenty-five thousand takes the whole run down. How many were dropped is
    reported rather than absorbed.
    """
    import numpy as np

    depths = (np.zeros(lonlat.shape[0], dtype=float) if bed_up is None
              else np.asarray(bed_up, dtype=float))
    points, cells, depths = _sandbox_formats()._clean_and_orient(
        lonlat, cells, depths)
    points, cells, depths, merged = _merge_coincident(points, cells, depths)
    keep = _has_area(points, cells)
    collapsed = int((~keep).sum())
    if collapsed or merged:
        points, cells, depths = _sandbox_formats()._clean_and_orient(
            points, cells[keep], depths)
    return points, cells, (None if bed_up is None else depths), collapsed + merged


def _merge_coincident(points: Any, cells: Any,
                      depths: Any) -> tuple[Any, Any, Any, int]:
    """Fuse nodes closer together than the geometry file can tell apart.

    A SELAFIN carries its coordinates in SINGLE precision, and a UTM northing runs
    to seven digits: two nodes a fraction of a metre apart are written as the same
    point, and the element between them arrives at the solver with no area. The
    mesh in memory is fine and the file is not, so the fusion happens here, before
    any writer sees it, and the count travels in the probes.
    """
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


def _emit_formats(rundir: Path, *, lonlat: Any, cells: Any, points_m: Any,
                  bed_up: Any, boundary: Any,
                  domain_source: str) -> tuple[dict[str, str],
                                               dict[str, Any], dict[str, Any]]:
    """Write the per-solver geometry from one boundary segmentation.

    TELEMAC's SELAFIN and its ``.cli`` are written together by telapy, because a
    boundary-conditions file is only valid against the geometry whose boundary
    numbering it was written from.

    Only formats an engine READS are written: no worker here consumes an ADCIRC
    ``fort.14`` (the SWAN worker is regular-grid only), so the shared writer stays
    available and nothing calls it on a build.
    """
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.selafin_cli import write_telemac_pair

    formats = _sandbox_formats()
    loops = formats.extract_boundary_loops(np.asarray(cells, dtype=np.int64))

    info: dict[str, Any] = {"source": domain_source}
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
    files: dict[str, str] = {}
    pair = write_telemac_pair(
        rundir, x=points_m[:, 0], y=points_m[:, 1], cells=cells, bed=bed_up,
        open_nodes=open_nodes, title="TRID3NT OM2D MESH")
    files["slf_uri"] = str(pair["geo_slf"])
    files["cli_uri"] = str(pair["cli"])
    probes["liquid_boundaries"] = int(pair["stats"].get("n_liquid_boundaries", 0))
    probes["boundary_nodes_written"] = int(pair["stats"].get("nptfr", 0))
    return files, info, probes


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
