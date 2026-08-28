"""The COASTAL WATER-EDGE mesher: the real shoreline bounds the domain, not a box.

The OSM ``natural=coastline`` line unioned with NHD areal water IS the meshing
domain, refined by distance to the shore and by wavelength over depth. The
triangulation runs in the GPL-isolated OceanMesh2D image, mounted and shelled,
never imported.

A coastal mesh is the one shape with a SEAWARD boundary, so open-boundary
designation lives here: naming a side emits the SCHISM ``hgrid.gr3`` whose open
nodes a barotropic or baroclinic solve forces tides and T-S at. Without a named
side the mesh stays TELEMAC-only and says so, rather than inventing a boundary.
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
    fetch_activation_rows,
    fetch_fallback_note,
    register_mesher,
)
from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.coastal_edge")

__all__ = ["COASTAL_EDGE", "build"]

#: The GPL-isolated OceanMesh2D image and the in-container mesher mounted into it.
_MESH_IMAGE_DEFAULT = "trid3nt-local/mesh:latest"
_INCONTAINER_SCRIPT = "coastal_edge_driver.py"
#: The water-polygon builder is still a sandbox module; it is IMPORTED here, never
#: shelled.
_SANDBOX = "scripts/sandbox/oceanmesh"
_CONTAINER_TIMEOUT_S = 2400

#: The bathymetry rungs a coastal water-edge mesh tolerates. The mesh IS the wet
#: domain, so every node needs a real below-waterline bed: where CUDEM's 1/9"
#: collection stops mid-AOI, the global ETOPO relief is a REAL bed - coarse, on a
#: different vertical datum, loudly labeled. A refusal is the honest outcome; the
#: alternative is meshing an ocean that has no depth.
_BED_FALLBACK = ("etopo_bathy_base",)

_SIDES = ("south", "north", "east", "west")

_FIELDS = (
    MeshField("kind", types=(str,), choices=("unstructured_tri",),
              default="unstructured_tri",
              doc="unstructured_tri - a water body's interior is triangulated"),
    MeshField("extent", types=(tuple, list), required=True,
              doc="(min_lon, min_lat, max_lon, max_lat) the water domain is cut from"),
    MeshField("min_edge_length_m", types=(int, float), default=40.0,
              doc="finest triangle edge, at the shoreline"),
    MeshField("max_edge_length_m", types=(int, float), default=400.0,
              doc="coarsest triangle edge, offshore"),
    MeshField("grade", types=(int, float), default=0.20,
              doc="gradation limit; smaller means smoother size transitions"),
    MeshField("open_boundary_side", types=(str,), choices=_SIDES,
              doc="the seaward edge designated as the open boundary; without one "
                  "the mesh is fully closed and SCHISM declines it"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Mesh the OSM+NHD water polygon interior for the AOI."""
    import numpy as np

    from trid3nt_server.workflows.mesh.watershed import (
        reproject_nodes_to_utm,
        sample_raster_at_nodes,
    )

    aoi = tuple(float(v) for v in spec["extent"])
    min_edge = float(spec.get("min_edge_length_m", 40.0))
    max_edge = float(spec.get("max_edge_length_m", 400.0))
    grade = float(spec.get("grade", 0.20))
    side = (str(spec.get("open_boundary_side") or "").strip().lower() or None)
    rundir = _rundir()

    water, water_provenance = _water_domain(aoi)
    dem_path, bed_layer = _fetch_bed(aoi, rundir)
    _write_container_inputs(rundir, aoi, water, min_edge, max_edge, grade)
    _run_container(rundir)

    npz = np.load(rundir / "coastal_tin_mesh.npz")
    lonlat = np.asarray(npz["points"], dtype=float)
    cells = np.asarray(npz["cells"], dtype=np.int64)
    bed = sample_raster_at_nodes(dem_path, lonlat)
    points, utm_epsg = reproject_nodes_to_utm(lonlat)

    open_boundary_info: dict[str, Any] = {
        "source": "OSM coastline + NHD areal water union",
        "provenance": water_provenance,
    }
    files: dict[str, str] = {}
    engine_compat = ["telemac"]
    if side is not None:
        gr3_local, open_nodes = _write_schism_gr3(
            rundir, lonlat=lonlat, cells=cells, bed_up=bed, side=side)
        if gr3_local is not None:
            files["gr3_uri"] = str(gr3_local)
            open_boundary_info.update({"open_boundary_side": side,
                                       "open_node_count": int(open_nodes),
                                       "designated_by": "coastal_edge"})
            engine_compat.append("schism")

    sizing_source = _sizing_source(rundir)
    dem_source = _bed_provenance(bed_layer)
    fallback_note = fetch_fallback_note(bed_layer)
    return Mesh(
        points=points, cells=cells, crs_authid=f"EPSG:{int(utm_epsg)}", bed=bed,
        meta={
            "extent": aoi,
            "utm_epsg": int(utm_epsg),
            "lonlat_bbox": (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()),
                            float(lonlat[:, 0].max()), float(lonlat[:, 1].max())),
            "files": files,
            "fallback_note": fallback_note,
            "artifact": {
                "engine_compat": engine_compat,
                "open_boundary_info": open_boundary_info,
                "provenance": {
                    "min_edge_length_m": min_edge,
                    "max_edge_length_m": max_edge,
                    "grade": grade,
                    "sizing_source": sizing_source,
                    "dem_source": dem_source,
                    "bed_fallback_note": fallback_note,
                    "area_km2": _area_km2(water),
                },
            },
            "synthetic_inputs": [
                {"param": "min_edge_length_m", "value": min_edge, "units": "m",
                 "basis": "user"},
                {"param": "max_edge_length_m", "value": max_edge, "units": "m",
                 "basis": "user"},
                {"param": "grade", "value": grade, "basis": "user"},
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / {cells.shape[0]} elements",
                 "basis": "derived", "real_source_if_any": sizing_source},
                {"param": "mesh_bed", "value": dem_source, "basis": "fetched",
                 "consequence": "physics", "real_source_if_any": dem_source,
                 "note": "the elevation every node carries; a solver reads it as "
                         "the domain's bathymetry"},
            ],
        })


def _rundir() -> Path:
    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _repo_root() -> Path:
    # .../trid3nt_server/workflows/mesh/meshers/coastal_edge.py
    return Path(__file__).resolve().parents[4]


def _water_domain(aoi: tuple[float, ...]) -> tuple[Any, Any]:
    """The OSM coastline + NHD areal water union for the AOI, or a typed refusal."""
    sandbox = _repo_root() / _SANDBOX
    for path in (str(sandbox), str(_repo_root() / "workers/schism")):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from water_edge import build_coastal_water
    except Exception as exc:  # noqa: BLE001
        raise MeshToolError(
            "MESH_COASTAL_UNAVAILABLE",
            f"the coastal water-edge domain builder is unavailable in this "
            f"environment: {exc}")
    water, provenance = build_coastal_water(tuple(aoi), use_nhd=True)
    if water is None or getattr(water, "is_empty", True):
        raise MeshToolError(
            "MESH_NO_WATER",
            f"no water polygon was found for coastal AOI {aoi} (OSM coastline + "
            "NHD areal water are both empty); is this an inland box?")
    return water, provenance


def _fetch_bed(aoi: tuple[float, ...], rundir: Path) -> tuple[Path, Any]:
    """Fetch the coastal topo-BATHY bed and stage it -> ``(path, layer)``.

    EPSG:4326 on purpose. Two consumers read this raster and they disagree about
    what a coordinate is: the node sampler warps 4326 -> the raster's own CRS, but
    the in-container wavelength sizer builds its interpolator from the raster's
    transform and queries it with lon/lat, so a projected grid puts every query
    out of bounds and the depth term silently reads its fill value.
    """
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    layer = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=tuple(aoi), target_crs="EPSG:4326", fallback=_BED_FALLBACK)
    uri = layer.uri if hasattr(layer, "uri") else layer["uri"]
    dst = rundir / "topobathy.tif"
    dst.write_bytes(read_object_bytes_s3(uri) if str(uri).startswith("s3://")
                    else Path(uri).read_bytes())
    return dst, layer


def _write_container_inputs(rundir: Path, aoi: tuple[float, ...], water: Any,
                            min_edge: float, max_edge: float,
                            grade: float) -> None:
    from shapely.geometry import mapping

    # The in-container mesher reads the water polygon from a FILE under the /data
    # mount rather than an inline dict, so it is written beside the config.
    (rundir / "water.geojson").write_text(json.dumps(mapping(water)))
    (rundir / "mesh_config.json").write_text(json.dumps({
        "water_geojson": "/data/water.geojson",
        "dem_path": "/data/topobathy.tif",
        "bbox": list(aoi),
        "min_edge_length_m": min_edge,
        "max_edge_length_m": max_edge,
        "grade": grade,
        "wavelength": True, "wl": 10, "max_iter": 60,
    }))


def _run_container(rundir: Path) -> None:
    image = os.environ.get("TRID3NT_MESH_IMAGE") or _MESH_IMAGE_DEFAULT
    argv = [
        "docker", "run", "--rm",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{rundir}:/data",
        "--entrypoint", "python", image,
        f"/drivers/{_INCONTAINER_SCRIPT}", "/data/mesh_config.json", "/data"]
    logger.info("coastal_edge mesher: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0 or not (rundir / "coastal_tin_mesh.npz").exists():
        raise MeshToolError(
            "MESH_BUILD_FAILED",
            f"the coastal water-edge mesher failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


def _write_schism_gr3(rundir: Path, *, lonlat: Any, cells: Any, bed_up: Any,
                      side: str) -> tuple[Path | None, int]:
    """Write a SCHISM ``hgrid.gr3`` with a designated open boundary.

    Reuses the SCHISM worker's pure-numpy ``tin_to_hgrid`` - the single gr3
    writer, never re-implemented here. SCHISM depth is positive-DOWN, so the mesh
    bed (elevation positive-UP) is negated. Best-effort: a bridge failure leaves
    the mesh TELEMAC-only rather than failing the whole build, because the SCHISM
    geometry is an addition to a mesh that is already complete without it.
    """
    import numpy as np

    try:
        from trid3nt_server.workflows.schism.deck_authoring import load_gr3_bridge

        bridge = load_gr3_bridge()
        text = bridge.tin_to_hgrid(
            np.asarray(lonlat, dtype=float), np.asarray(cells, dtype=np.int64),
            depth=-np.asarray(bed_up, dtype=float),
            grid_name="trid3nt_coastal_edge", open_boundary_side=side,
            clean_boundary=True)
        open_nodes = _gr3_open_node_count(text)
        if open_nodes <= 0:
            logger.warning(
                "coastal_edge: tin_to_hgrid designated 0 open-boundary nodes on "
                "side=%s - leaving the mesh TELEMAC-only", side)
            return None, 0
        local = rundir / "hgrid.gr3"
        local.write_text(text, encoding="utf-8")
        return local, int(open_nodes)
    except Exception as exc:  # noqa: BLE001 -- the gr3 is an addition, never fatal
        logger.warning(
            "coastal_edge: SCHISM hgrid.gr3 emission failed (%s); the mesh stays "
            "TELEMAC-only", exc)
        return None, 0


def _gr3_open_node_count(gr3_text: str) -> int:
    """The open-boundary node total an hgrid.gr3 states (0 when it names none)."""
    for line in gr3_text.splitlines():
        if "Total number of open boundary nodes" in line:
            try:
                return int(line.split("=")[0].split()[0])
            except Exception:  # noqa: BLE001
                return 0
    return 0


def _area_km2(geom: Any) -> float:
    import geopandas as gpd

    return float(gpd.GeoSeries([geom], crs=4326).to_crs(6933).area.iloc[0] / 1e6)


def _sizing_source(rundir: Path) -> str:
    """What ACTUALLY sized the mesh, from the mesher's own report.

    The container records which sizing functions bound, including one that was
    requested and never bound. Claiming a term the mesh does not carry is the same
    class of false promise as an undeclared substitution, so the claim is copied,
    never composed here.
    """
    domain = "OSM natural=coastline + NHDPlus areal water domain"
    try:
        stats = json.loads((rundir / "mesh_stats.json").read_text())
        active = [str(s) for s in (stats.get("sizing_functions") or [])]
    except Exception:  # noqa: BLE001 -- an unreadable report says so, never guesses
        active = []
    if not active:
        return f"{domain}; sizing functions unreported by the mesher"
    return f"{domain}; " + "; ".join(active)


def _bed_provenance(layer: Any) -> str:
    """What ACTUALLY painted the bed, from the ladder's activation rows."""
    rows = fetch_activation_rows(layer)
    if rows:
        return "topobathy: " + ", ".join(
            f"{rung} {coverage * 100:.0f}%" for rung, coverage in rows)
    note = fetch_fallback_note(layer)
    return (f"topobathy ({note})" if note
            else "topobathy (source UNMEASURED: the fetch reported no activation "
                 "rows)")


def _set_edge_band(mesh: Mesh, *, min_edge_length_m: float,
                   max_edge_length_m: Any = None) -> Mesh:
    """Re-derive the water domain at a different edge band - a full rebuild."""
    built = dict(mesh.meta["artifact"]["provenance"])
    info = dict(mesh.meta["artifact"]["open_boundary_info"])
    return build({
        "extent": mesh.meta["extent"],
        "min_edge_length_m": float(min_edge_length_m),
        "max_edge_length_m": (float(max_edge_length_m)
                              if max_edge_length_m is not None
                              else built["max_edge_length_m"]),
        "grade": built["grade"],
        "open_boundary_side": info.get("open_boundary_side")})


def _set_boundary(mesh: Mesh, *, side: str) -> Mesh:
    """Designate (or re-designate) the seaward open boundary -> the SCHISM geometry."""
    built = dict(mesh.meta["artifact"]["provenance"])
    return build({
        "extent": mesh.meta["extent"],
        "min_edge_length_m": built["min_edge_length_m"],
        "max_edge_length_m": built["max_edge_length_m"],
        "grade": built["grade"],
        "open_boundary_side": str(side)})


COASTAL_EDGE = register_mesher(
    "coastal_edge",
    build,
    actions=(
        EditAction(
            name="set_edge_band", apply=_set_edge_band,
            inputs={
                "min_edge_length_m": MeshField(
                    "min_edge_length_m", types=(int, float), required=True,
                    doc="the new finest triangle edge, in metres"),
                "max_edge_length_m": MeshField(
                    "max_edge_length_m", types=(int, float),
                    doc="the new coarsest triangle edge; unchanged when absent")},
            doc="Re-triangulate the water domain between a different edge band."),
        EditAction(
            name="set_boundary", apply=_set_boundary,
            inputs={"side": MeshField(
                "side", types=(str,), required=True, choices=_SIDES,
                doc="the seaward edge to designate as the open boundary")},
            doc="Designate the seaward open boundary a SCHISM solve forces at."),
        apply_layer_edits_action(),
    ),
    fields=_FIELDS,
    # MEASURED, not assumed: three rebuilds from one identical spec (the southern
    # New Jersey coast AOI, 300/1500 m band, grade 0.20) returned one mesh - 424
    # nodes / 693 elements, sha256 e2025226 on all three - so a replay of a
    # coastal_edge recipe reproduces the mesh rather than an equivalent of it.
    deterministic=True,
)
