"""Standalone mesh builder ``generate_mesh``.

Mesh creation is an EXPLICIT user act -- one tool that turns a domain into a
computational mesh a solver (or QGIS) can consume, with the mode INFERRED from the
inputs and the resolution exposed as user levers (the granularity norm):

  * a POUR POINT (or ``mesh_mode="watershed"``) -> the watershed-first
    mesher: delineate the catchment, refine by distance-to-river, project to UTM,
    sample the bed -> a bathymetric SELAFIN (the whole catchment is the domain, so
    the AOI never cookie-cuts the mesh mid-hillslope); and
  * a COASTAL AOI (or ``mesh_mode="coastal"``) -> the water-edge mesher:
    the OSM-coastline + NHD water polygon is the domain, refined by distance-to-
    shore + wavelength-to-depth (the real shoreline, not a bbox).

Both promote the PROVEN ``scripts/sandbox/oceanmesh`` machinery behind ONE tool;
the GPL OceanMesh2D engine stays isolated in ``trid3nt-local/mesh:latest`` (shelled,
never imported). The build EMITS INTO THE CASE:

  * a DISPLAY layer -- an MDAL-loadable ``.2dm`` (``layer_type="mesh"``, explicit
    UTM ``crs_authid``) that lands in ``loaded_layers`` via the ordinary LayerURI
    auto-emit, so a human sees the wireframe in QGIS; and
  * a MESH ARTIFACT record (format URIs, CRS, node/element counts, has_bathymetry,
    open-boundary info, engine-compat) persisted BOTH in a same-process case stash
    and a durable ``mesh_artifact.json`` sidecar beside the mesh objects, so a
    model template in the SAME case can discover it and offer the precondition gate
    (``mesh.precondition_gate``).

The real ``.slf`` (+ best-effort ``.gr3`` / ``fort.14``) solver geometries persist
as case artifacts so acceptance ("use this mesh?") is the common case across
TELEMAC / SCHISM / SWAN.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.data import register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value

logger = logging.getLogger(
    "trid3nt_server.workflows.mesh.generate_mesh.generate_mesh")

# Warm geopandas/shapely on the MAIN thread at registry-load time (this module is
# imported synchronously during tool-registry build, before the daemon spawns its
# background discover-index warm thread). shapely has a thread-first-import
# circular-import race: if a worker thread imports it first the module is left
# partially initialized and every later import in the process fails. Importing it
# here, on the main thread, before any thread can, settles it for the whole daemon
# (a mesh build runs geopandas in an offloaded thread). Best-effort -- a stripped
# environment without geopandas still registers the tool.
try:  # noqa: SIM105
    import geopandas as _geopandas_warm  # noqa: F401
except Exception:  # noqa: BLE001
    pass

__all__ = ["generate_mesh", "GenerateMeshError", "model_generate_mesh"]


class GenerateMeshError(RuntimeError):
    """A typed mesh-build failure (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: DECLARED edge-length ranges. Both are SOLVER (mesh-generator)
#: constraints with a practical 5 m floor: a finer edge reliably trips HEC-RAS's
#: <= 8-sides-per-cell acceptance on any non-trivial AOI (and over-refines a TELEMAC/
#: SCHISM TIN). There is NO fixed coarse ceiling -- the true realizability is
#: AOI-dependent and enforced at build time by a typed GenerateMeshError (>8-sided
#: cells / gmsh failure), never a silent snap. The declaration documents the floor +
#: the 8-side reality so the gate card can quote it.
_MIN_EDGE_SPEC = ResolutionSpec(
    param="min_edge_length_m",
    unit="m",
    min_value=5.0,
    native_hint="3DEP 10 m (fetch_dem) terrain native",
    constraint_source="solver",
    rationale=(
        "finest cell/triangle edge; below ~5 m the seed cloud reliably trips HEC's "
        "<= 8-sides-per-cell acceptance (AOI-dependent, enforced by a typed build "
        "error), and over-refines a TIN; no fixed coarse ceiling (grade + AOI govern)"
    ),
)
_MAX_EDGE_SPEC = ResolutionSpec(
    param="max_edge_length_m",
    unit="m",
    min_value=5.0,
    native_hint="3DEP 10 m (fetch_dem) terrain native",
    constraint_source="solver",
    rationale=(
        "coarsest background edge; bounded below by the 5 m floor, no fixed ceiling "
        "(a coarser hillslope background is valid; realizability is the AOI-dependent "
        "8-side/grade build check, a typed error not a silent snap)"
    ),
)

_METADATA = AtomicToolMetadata(
    name="generate_mesh",
    ttl_class="live-no-cache",
    cacheable=False,
    tier="general",
    resolution_specs=(_MIN_EDGE_SPEC, _MAX_EDGE_SPEC),
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def generate_mesh(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    pour_point: tuple[float, float] | list[float] | str | None = None,
    mesh_mode: str = "auto",
    engine: str | None = None,
    min_edge_length_m: float = 40.0,
    max_edge_length_m: float = 400.0,
    grade: float = 0.20,
    open_boundary_side: str | None = None,
    compute_class: str = "medium",
    **_extra_ignored: Any,
) -> Any:
    """BUILD A COMPUTATIONAL MESH for a domain -> a mesh layer + a solver-ready mesh artifact.

    THE tool for "mesh this watershed / coastline", "build a grid for TELEMAC /
    SCHISM / HEC-RAS", "make an unstructured triangulation of this basin / bay",
    "author a channel-refined HEC-RAS rain-on-grid mesh I can inspect then solve on",
    "generate the model domain for me to reuse". Mesh creation is EXPLICIT and lives
    HERE, never auto-guessed inside a model template -- a template that finds this
    mesh in the case will ASK before consuming it.

    Mode is INFERRED (or forced via ``mesh_mode`` / an ``engine`` hint):
      * a ``pour_point`` (or ``mesh_mode="watershed"``) -> the delineated CATCHMENT
        refined by distance-to-river -> a triangular TIN (.2dm + bathymetric SELAFIN;
        TELEMAC-ready, SCHISM with an ``open_boundary_side``);
      * a coastal AOI (or ``mesh_mode="coastal"``) -> the OSM+NHD WATER polygon
        refined by distance-to-shore + wavelength -> a coastal TIN;
      * ``mesh_mode="hecras"`` (or ``engine="hecras"``) -> a channel-refined HEC-RAS
        rain-on-grid CELL mesh: a coarse hillslope background grading down to a fine
        cell along the delineated channel network (graded Poisson-disk seeds + channel
        breaklines), realized + validated through the HEC meshprobe. It emits the
        realized cell wireframe as the display layer + a PORTABLE authoring bundle a
        later ``hecras_flood_2d`` rain-on-grid run consumes via the precondition gate.

    Resolution levers (granularity norm): ``min_edge_length_m`` / ``max_edge_length_m``
    bound the cell/triangle size (for ``hecras`` these ARE the channel + hillslope
    target cell sizes -- e.g. 22 / 90 m); ``grade`` limits coarsening. Both edges are
    declared >=5 m (mesh/solver); a finer ask is quoted the floor + the AOI-dependent
    <=8-sides-per-cell acceptance (a typed build error), never silently snapped.
    US-only.

    SCHISM readiness: a COASTAL mesh built with an ``open_boundary_side`` also emits a
    SCHISM hgrid.gr3 (seaward OPEN boundary). Without it (or for a watershed mesh)
    SCHISM honestly declines the mesh.

    Params:
        location: place naming the domain (geocoded). Supply this OR ``bbox``.
        bbox: OPTIONAL AOI ``(min_lon,min_lat,max_lon,max_lat)`` EPSG:4326.
        pour_point: OPTIONAL ``(lon, lat)`` catchment outlet -> watershed / hecras.
        mesh_mode: "auto" (infer) | "watershed" | "coastal" | "hecras".
        engine: OPTIONAL target-solver hint; ``"hecras"`` selects the RoG cell mesh.
        min_edge_length_m: finest cell/triangle edge (m); the CHANNEL cell for hecras.
        max_edge_length_m: coarsest edge (m); the HILLSLOPE background for hecras.
        grade: gradation limit (0.15-0.35; smaller = smoother size transitions;
            unused for hecras, whose grading is the seed size field).
        open_boundary_side: OPTIONAL "south"|"north"|"east"|"west" -- the seaward
            edge to designate as the SCHISM open boundary on a coastal mesh.
    """
    return await model_generate_mesh(
        location=location, bbox=bbox, pour_point=pour_point, mesh_mode=mesh_mode,
        engine=engine, min_edge_length_m=min_edge_length_m,
        max_edge_length_m=max_edge_length_m, grade=grade,
        open_boundary_side=open_boundary_side, compute_class=compute_class)


def _infer_mode(mesh_mode: str, pour_point: Any, bbox: Any,
                engine: str | None = None) -> str:
    m = (mesh_mode or "auto").strip().lower()
    # an explicit HEC-RAS target (mode or engine hint) -> the channel-refined RoG mesh.
    if m in ("hecras", "hecras_rog") or str(engine or "").strip().lower() == "hecras":
        return "hecras_rog"
    if m in ("watershed", "coastal", "coastal_water_edge"):
        return "coastal_water_edge" if m.startswith("coastal") else "watershed"
    # auto: a pour point is an unambiguous watershed signal.
    if pour_point is not None:
        return "watershed"
    return "coastal_water_edge"


async def model_generate_mesh(
    *,
    location: str | None,
    bbox: Any,
    pour_point: Any,
    mesh_mode: str,
    min_edge_length_m: float,
    max_edge_length_m: float,
    grade: float,
    open_boundary_side: str | None = None,
    engine: str | None = None,
    compute_class: str,
) -> LayerURI:
    """Deterministic mesh composer: resolve AOI/pour-point -> build -> stage the
    mesh objects to the case -> emit the mesh layer + persist the artifact."""
    import asyncio

    from trid3nt_server.data import TOOL_REGISTRY

    if isinstance(pour_point, str):
        pour_point = coerce_bbox_value(pour_point)
    pp: tuple[float, float] | None = (
        tuple(float(v) for v in pour_point) if pour_point is not None else None)

    aoi = coerce_bbox_value(bbox) if bbox is not None else None
    if aoi is None and pp is not None:
        b = 0.14
        aoi = (max(pp[0] - b, -180.0), max(pp[1] - b, -90.0),
               min(pp[0] + b, 180.0), min(pp[1] + b, 90.0))
    if aoi is None:
        if not location:
            raise GenerateMeshError(
                "GENERATE_MESH_NO_AOI",
                "supply a location (geocoded), a bbox, or a pour_point.")
        geo = await asyncio.to_thread(
            TOOL_REGISTRY["geocode_location"].fn, query=location)
        aoi = coerce_bbox_value(getattr(geo, "bbox", None) or geo["bbox"])
    aoi = tuple(float(v) for v in aoi)

    mode = _infer_mode(mesh_mode, pp, aoi, engine)

    # HEC-RAS channel-refined rain-on-grid cell mesh: a distinct build +
    # artifact (graded seed cloud + breaklines + local terrain frame, meshprobe-
    # validated), consumed by hecras_flood_2d RoG via the precondition gate.
    if mode == "hecras_rog":
        from trid3nt_server.emission.pipeline_emitter import current_turn_case
        from trid3nt_server.workflows.mesh.generate_mesh.hecras_build import (
            build_and_record_hecras_mesh,
        )
        # A RoG mesh grades a whole terrain box down to the channel; the generic AOI
        # buffer (0.14 deg ~ 25 km) would mesh a needlessly huge domain. When the box
        # was auto-derived from a pour point, tighten it to a catchment-scale window.
        hec_aoi = aoi
        if bbox is None and pp is not None:
            # a catchment-tight window (~10 x 9 km): the graded cell mesh's <= 8-sides
            # acceptance is fragile on a large seed cloud, so keep the domain snug to
            # the catchment rather than the generic 0.14 deg buffer.
            hec_aoi = (pp[0] - 0.055, pp[1] - 0.04, pp[0] + 0.055, pp[1] + 0.04)
        case_id = current_turn_case()
        return await asyncio.to_thread(
            build_and_record_hecras_mesh, location=location, bbox=hec_aoi, pour_point=pp,
            channel_m=float(min_edge_length_m), background_m=float(max_edge_length_m),
            case_id=case_id)

    if mode == "watershed" and pp is None:
        pp = ((aoi[0] + aoi[2]) / 2.0, (aoi[1] + aoi[3]) / 2.0)

    mesh_id = new_ulid()
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"mesh-{mesh_id}"
    rundir.mkdir(parents=True, exist_ok=True)

    # Warm the geo stack on the main thread (shapely/geopandas thread-first-import
    # race) before offloading the container-driven build.
    import geopandas as _gpd  # noqa: F401

    if mode == "watershed":
        built = await asyncio.to_thread(
            _build_watershed, pp, aoi, str(rundir),
            min_edge_length_m, max_edge_length_m, grade)
    else:
        built = await asyncio.to_thread(
            _build_coastal, aoi, str(rundir),
            min_edge_length_m, max_edge_length_m, grade)

    obs = (open_boundary_side or "").strip().lower() or None
    if obs is not None and obs not in ("south", "north", "east", "west"):
        raise GenerateMeshError(
            "GENERATE_MESH_BAD_OPEN_SIDE",
            f"open_boundary_side must be south/north/east/west; got {obs!r}.")
    if obs is not None and mode != "coastal_water_edge":
        # An open (seaward) boundary is only meaningful on a coastal mesh; an inland
        # catchment is fully closed. Drop it with a loud note rather than fabricate
        # a seaward boundary on a watershed.
        logger.warning(
            "generate_mesh: open_boundary_side=%s ignored for mode=%s (only a "
            "coastal mesh has a seaward open boundary)", obs, mode)
        obs = None

    name = location or built.get("place") or f"{mode} mesh"
    layer = await asyncio.to_thread(
        _stage_and_record, built, mode=mode, mesh_id=mesh_id, name=str(name),
        aoi=aoi, pp=pp, min_edge_length_m=min_edge_length_m,
        max_edge_length_m=max_edge_length_m, grade=grade,
        open_boundary_side=obs)
    return layer


# --------------------------------------------------------------------------- #
# Build providers (container-driven; live). Both return a normalized dict:
#   points_utm (N,2) m, cells (M,3), bed (N,) m up, utm_epsg, points_lonlat (N,2),
#   area_km2, outlet_lonlat|None, open_boundary_info, local_slf, sizing sources.
# --------------------------------------------------------------------------- #
def _build_watershed(pp, aoi, rundir, min_edge, max_edge, grade) -> dict[str, Any]:
    """Reuse the proven ``acquire_watershed_mesh`` as the watershed
    provider -- the catchment IS the domain, refined by distance-to-river."""
    import numpy as np

    from trid3nt_server.workflows.telemac.rain_on_grid import (
        mesh_acquisition as MA,
    )

    wm = MA.acquire_watershed_mesh(
        pour_point=pp, bbox=aoi, output_dir=rundir,
        min_edge_length_m=float(min_edge), max_edge_length_m=float(max_edge),
        grade=float(grade))
    return {
        "points_utm": np.asarray(wm.points_utm, dtype=float),
        "cells": np.asarray(wm.cells, dtype=np.int64),
        "bed": np.asarray(wm.bed_elev, dtype=float),
        "utm_epsg": int(wm.utm_epsg),
        "points_lonlat": np.asarray(wm.meta["points_lonlat"], dtype=float),
        "area_km2": float(wm.area_km2),
        "outlet_lonlat": tuple(wm.outlet_lonlat) if wm.outlet_lonlat else None,
        "open_boundary_info": {},  # inland catchment: single closed boundary
        "local_slf": wm.slf_path,
        "sizing_source": "pysheds catchment domain; refined by distance to the "
                         "NHDPlus HR/OSM river network",
        "dem_source": "USGS 3DEP bare-earth (bed) + Copernicus GLO-30 (delineation)",
    }


def _build_coastal(aoi, rundir, min_edge, max_edge, grade) -> dict[str, Any]:
    """Water-edge provider: mesh the OSM+NHD water polygon interior.

    Reuses the proven sandbox ``water_edge`` + the isolated in-container water-edge
    mesher. LIVE (needs the mesh image, network, topobathy); the watershed path is
    the live-proof case, so this shares the exact same container seam."""
    import sys

    import numpy as np

    repo = _repo_root()
    sandbox = repo / "scripts/sandbox/oceanmesh"
    for p in (str(sandbox), str(repo / "workers/schism")):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from water_edge import build_coastal_water  # sandbox module (proven)
    except Exception as exc:  # noqa: BLE001
        raise GenerateMeshError(
            "GENERATE_MESH_COASTAL_UNAVAILABLE",
            f"coastal water-edge mesher unavailable in this environment: {exc}")

    water, water_prov = build_coastal_water(tuple(aoi), use_nhd=True)
    if water is None or getattr(water, "is_empty", True):
        raise GenerateMeshError(
            "GENERATE_MESH_NO_WATER",
            f"no water polygon found for coastal AOI {aoi} (OSM coastline + NHD "
            "areal water both empty); is this an inland box?")
    dem_path = _fetch_topobathy(aoi, Path(rundir))
    # The in-container mesher reads the water polygon from a FILE (json.load(open(
    # ...))) under the /data mount, not an inline dict -- write it beside the config.
    (Path(rundir) / "water.geojson").write_text(json.dumps(_mapping(water)))
    conf = {
        "water_geojson": "/data/water.geojson",
        "dem_path": "/data/topobathy.tif",
        "bbox": list(aoi),
        "min_edge_length_m": float(min_edge),
        "max_edge_length_m": float(max_edge),
        "grade": float(grade),
        "wavelength": True, "wl": 10, "max_iter": 60,
    }
    (Path(rundir) / "mesh_config.json").write_text(json.dumps(conf))
    _run_mesh_container(
        Path(rundir), "_mesh_water_edge_incontainer.py", sandbox)
    npz = np.load(Path(rundir) / "coastal_tin_mesh.npz")
    points_ll = np.asarray(npz["points"], dtype=float)
    cells = np.asarray(npz["cells"], dtype=np.int64)
    bed = _sample_raster(dem_path, points_ll)

    from trid3nt_server.workflows.telemac.rain_on_grid.mesh_acquisition import (
        reproject_nodes_to_utm,
    )
    points_m, epsg = reproject_nodes_to_utm(points_ll)
    return {
        "points_utm": points_m, "cells": cells, "bed": bed, "utm_epsg": int(epsg),
        "points_lonlat": points_ll,
        "area_km2": float(_area_km2(water)),
        "outlet_lonlat": None,
        "open_boundary_info": {"source": "OSM coastline + NHD areal water union",
                               "provenance": water_prov},
        "local_slf": None,
        "sizing_source": "OSM natural=coastline + NHDPlus areal water domain; "
                         "distance-to-shore + wavelength-to-depth sizing",
        "dem_source": "topobathy (3DEP + NOAA CoNED where available)",
        "place": None,
    }


# --------------------------------------------------------------------------- #
# Stage + record: write all formats, upload to the case bucket, emit the layer,
# persist the artifact (stash + durable sidecar).
# --------------------------------------------------------------------------- #
def _stage_and_record(
    built: dict[str, Any], *, mode: str, mesh_id: str, name: str, aoi, pp,
    min_edge_length_m: float, max_edge_length_m: float, grade: float,
    open_boundary_side: str | None = None,
) -> LayerURI:
    import numpy as np

    from trid3nt_server.data.simulation.solver.solver import _get_s3_client
    from trid3nt_server.emission.pipeline_emitter import current_turn_case
    from trid3nt_server.workflows.mesh.artifact import (
        MeshArtifact, stash_mesh_artifact, write_mesh_artifact_sidecar,
    )
    from trid3nt_server.workflows.telemac.rain_on_grid.mesh_acquisition import (
        _write_bottom_selafin,
    )

    pts = np.asarray(built["points_utm"], dtype=float)
    cells = np.asarray(built["cells"], dtype=np.int64)
    bed = np.asarray(built["bed"], dtype=float)
    utm_epsg = int(built["utm_epsg"])
    crs_authid = f"EPSG:{utm_epsg}"
    node_count, elem_count = int(pts.shape[0]), int(cells.shape[0])
    has_bathymetry = bool(bed.size == node_count and np.isfinite(bed).any())

    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"mesh-{mesh_id}"
    rundir.mkdir(parents=True, exist_ok=True)

    # 1. write the MDAL .2dm (display) + the SELAFIN (solve) in UTM metres.
    twodm_local = rundir / "mesh.2dm"
    twodm_local.write_text(_write_2dm(pts, cells, bed))
    slf_local = rundir / "mesh.slf"
    if built.get("local_slf") and Path(built["local_slf"]).exists():
        slf_local.write_bytes(Path(built["local_slf"]).read_bytes())
    else:
        _write_bottom_selafin(str(slf_local), pts, cells, bed)

    # 2. upload to the case cache bucket (the plugin reads .2dm via MDAL /vsicurl/).
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise GenerateMeshError(
            "GENERATE_MESH_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the mesh into the case.")
    s3 = _get_s3_client()
    prefix = f"mesh/{mesh_id}"
    twodm_uri = _put(s3, cache_bucket, f"{prefix}/mesh.2dm", twodm_local)
    slf_uri = _put(s3, cache_bucket, f"{prefix}/mesh.slf", slf_local)

    # SCHISM hgrid.gr3 (best-effort): only a COASTAL mesh with a designated seaward
    # open boundary is SCHISM-ready -- an inland/no-open-boundary mesh is honestly
    # left TELEMAC-only (mesh_compatible_with_engine declines SCHISM). The bridge
    # is the worker's proven pure-numpy tin_to_hgrid (SCHISM depth is positive-DOWN,
    # so the mesh bed elevation-up is negated).
    gr3_uri: str | None = None
    open_boundary_info: dict[str, Any] = dict(built.get("open_boundary_info") or {})
    if open_boundary_side and has_bathymetry:
        gr3_uri, open_node_count = _emit_schism_gr3(
            s3, cache_bucket, f"{prefix}/hgrid.gr3", rundir,
            points_lonlat=np.asarray(built["points_lonlat"], dtype=float),
            cells=cells, bed_up=bed, open_boundary_side=open_boundary_side,
            grid_name=f"trid3nt_{mode}")
        if gr3_uri is not None:
            open_boundary_info.update({
                "open_boundary_side": open_boundary_side,
                "open_node_count": int(open_node_count),
                "designated_by": "generate_mesh",
            })

    # lon/lat bbox for the zoom-to camera.
    ll = np.asarray(built["points_lonlat"], dtype=float)
    lonlat_bbox = (float(ll[:, 0].min()), float(ll[:, 1].min()),
                   float(ll[:, 0].max()), float(ll[:, 1].max()))

    provenance = {
        "min_edge_length_m": float(min_edge_length_m),
        "max_edge_length_m": float(max_edge_length_m),
        "grade": float(grade),
        "sizing_source": built.get("sizing_source"),
        "dem_source": built.get("dem_source"),
        "area_km2": built.get("area_km2"),
    }
    engine_compat: list[str] = ["telemac"] if has_bathymetry else []
    if gr3_uri is not None and int(open_boundary_info.get("open_node_count", 0)) > 0:
        engine_compat.append("schism")
    case_id = current_turn_case()
    art = MeshArtifact(
        mesh_id=mesh_id, name=name, mode=mode, display_uri=twodm_uri,
        slf_uri=slf_uri, utm_epsg=utm_epsg, crs_authid=crs_authid,
        has_bathymetry=has_bathymetry, node_count=node_count,
        element_count=elem_count, bbox=lonlat_bbox,
        engine_compat=engine_compat, gr3_uri=gr3_uri,
        outlet_lonlat=built.get("outlet_lonlat"),
        pour_point_lonlat=(tuple(pp) if pp else None),
        open_boundary_info=open_boundary_info,
        provenance=provenance, case_id=case_id)
    stash_mesh_artifact(case_id, art)
    sidecar = write_mesh_artifact_sidecar(art, s3)
    logger.info(
        "generate_mesh: %s mesh %s -> %d nodes %d elems %s (bathy=%s) sidecar=%s",
        mode, mesh_id, node_count, elem_count, crs_authid, has_bathymetry, sidecar)

    synthetic = [
        SyntheticInput(param="mesh_mode", value=mode, basis="derived",
                       note="inferred from the inputs"),
        SyntheticInput(param="min_edge_length_m", value=float(min_edge_length_m),
                       units="m", basis="user"),
        SyntheticInput(param="max_edge_length_m", value=float(max_edge_length_m),
                       units="m", basis="user"),
        SyntheticInput(param="grade", value=float(grade), basis="user"),
        SyntheticInput(param="mesh_domain",
                       value=f"{node_count} nodes / {elem_count} elements",
                       basis="derived",
                       real_source_if_any=built.get("sizing_source")),
    ]
    return LayerURI(
        layer_id=f"mesh-{mesh_id}", name=f"Mesh: {name}", layer_type="mesh",
        uri=twodm_uri, style_preset="mesh_wireframe", role="primary",
        bbox=lonlat_bbox, crs_authid=crs_authid, synthetic_inputs=synthetic)


# --------------------------------------------------------------------------- #
# Minimal MDAL-loadable 2DM (SMS) writer -- self-contained ASCII mesh.
# --------------------------------------------------------------------------- #
def _write_2dm(points: Any, cells: Any, z: Any) -> str:
    """Write an SMS ``.2dm`` (MESH2D) string: E3T triangles + ND nodes (1-based).

    MDAL reads this directly as a ``QgsMeshLayer``; node ``z`` becomes the
    "Bed Elevation" dataset. Coordinates are the mesh's native metres (the
    LayerURI ``crs_authid`` names the CRS, since a 2dm carries none)."""
    import numpy as np

    pts = np.asarray(points, dtype=float)
    cel = np.asarray(cells, dtype=np.int64)
    zz = np.asarray(z, dtype=float)
    lines = ["MESH2D"]
    for i, (a, b, c) in enumerate(cel, start=1):
        lines.append(f"E3T {i} {int(a) + 1} {int(b) + 1} {int(c) + 1} 1")
    for i, (x, y) in enumerate(pts, start=1):
        zi = float(zz[i - 1]) if i - 1 < zz.size else 0.0
        lines.append(f"ND {i} {x:.6f} {y:.6f} {zi:.6f}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# SCHISM hgrid emission (best-effort): the worker's proven tin_to_hgrid bridge.
# --------------------------------------------------------------------------- #
def _emit_schism_gr3(
    s3: Any, bucket: str, key: str, rundir: Path, *,
    points_lonlat: Any, cells: Any, bed_up: Any, open_boundary_side: str,
    grid_name: str,
) -> tuple[str | None, int]:
    """Write + upload a SCHISM ``hgrid.gr3`` with a designated open boundary.

    Reuses the SCHISM worker's pure-numpy ``tin_to_hgrid`` (single source of truth
    -- never re-implemented here). SCHISM depth is positive-DOWN, so the mesh bed
    (elevation positive-UP) is negated. Returns ``(gr3_uri | None, open_node_count)``;
    best-effort -- any bridge/upload failure returns ``(None, 0)`` and the mesh
    stays TELEMAC-only (never fails the whole build)."""
    import numpy as np

    try:
        from trid3nt_server.workflows.schism.deck_authoring import (
            load_gr3_bridge,
        )
        bridge = load_gr3_bridge()
        depth_down = -np.asarray(bed_up, dtype=float)
        gr3_text = bridge.tin_to_hgrid(
            np.asarray(points_lonlat, dtype=float), np.asarray(cells, dtype=np.int64),
            depth=depth_down, grid_name=grid_name,
            open_boundary_side=open_boundary_side, clean_boundary=True)
        open_node_count = _gr3_open_node_count(gr3_text)
        if open_node_count <= 0:
            logger.warning(
                "generate_mesh: tin_to_hgrid designated 0 open-boundary nodes on "
                "side=%s -- leaving the mesh TELEMAC-only", open_boundary_side)
            return None, 0
        gr3_local = Path(rundir) / "hgrid.gr3"
        gr3_local.write_text(gr3_text, encoding="utf-8")
        s3.put_object(Bucket=bucket, Key=key, Body=gr3_local.read_bytes())
        return f"s3://{bucket}/{key}", int(open_node_count)
    except Exception as exc:  # noqa: BLE001 -- gr3 is a bonus, never fatal
        logger.warning(
            "generate_mesh: SCHISM hgrid.gr3 emission failed (%s); mesh stays "
            "TELEMAC-only", exc)
        return None, 0


def _gr3_open_node_count(gr3_text: str) -> int:
    """Parse the open-boundary node total from an hgrid.gr3 string (0 if none)."""
    for line in gr3_text.splitlines():
        if "Total number of open boundary nodes" in line:
            try:
                return int(line.split("=")[0].split()[0])
            except Exception:  # noqa: BLE001
                return 0
    return 0


# --------------------------------------------------------------------------- #
# Small shared helpers.
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    # .../trid3nt_server/workflows/mesh/generate_mesh/generate_mesh.py
    return Path(__file__).resolve().parents[4]


def _put(s3: Any, bucket: str, key: str, local: Path) -> str:
    s3.put_object(Bucket=bucket, Key=key, Body=local.read_bytes())
    return f"s3://{bucket}/{key}"


def _mapping(geom: Any) -> dict:
    from shapely.geometry import mapping
    return mapping(geom)


def _area_km2(geom: Any) -> float:
    import geopandas as gpd
    return float(gpd.GeoSeries([geom], crs=4326).to_crs(6933).area.iloc[0] / 1e6)


def _run_mesh_container(rundir: Path, script: str, sandbox: Path) -> None:
    import subprocess

    image = os.environ.get("TRID3NT_MESH_IMAGE") or "trid3nt-local/mesh:latest"
    argv = [
        "docker", "run", "--rm",
        "-v", f"{sandbox}:/sandbox", "-v", f"{rundir}:/data",
        "--entrypoint", "python", image,
        f"/sandbox/{script}", "/data/mesh_config.json", "/data"]
    logger.info("generate_mesh container: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=2400)
    if cp.returncode != 0 or not (rundir / "coastal_tin_mesh.npz").exists():
        raise GenerateMeshError(
            "GENERATE_MESH_BUILD_FAILED",
            f"mesh worker failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


def _fetch_topobathy(aoi, rundir: Path) -> Path:
    from trid3nt_server.data import TOOL_REGISTRY
    from trid3nt_server.data.cache import read_object_bytes_s3

    layer = TOOL_REGISTRY["fetch_dem"].fn(
        bbox=tuple(aoi), source="3dep", resolution_m=10)
    uri = layer.uri if hasattr(layer, "uri") else layer["uri"]
    dst = rundir / "topobathy.tif"
    dst.write_bytes(
        read_object_bytes_s3(uri) if str(uri).startswith("s3://")
        else Path(uri).read_bytes())
    return dst


def _sample_raster(raster_path: Path, points_lonlat: Any) -> Any:
    from trid3nt_server.workflows.telemac.rain_on_grid.mesh_acquisition import (
        _sample_raster_at_nodes,
    )
    return _sample_raster_at_nodes(raster_path, points_lonlat)
