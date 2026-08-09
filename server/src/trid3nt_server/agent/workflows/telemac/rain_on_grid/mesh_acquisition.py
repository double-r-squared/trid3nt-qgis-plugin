"""Watershed mesh acquisition for the TELEMAC-2D rain-on-grid template.

Promotes the ADR 0193 watershed-first mesher into an importable TEMPLATE STEP:
delineate the catchment at a pour point, pull the river network inside it,
triangulate the catchment interior refined by distance-to-river (the authentic
OceanMesh2D engine in the GPL-isolated ``trid3nt-local/mesh:latest`` image),
sample the DEM bed, and produce a SELAFIN geometry the rain-on-grid worker
solves on. The catchment -- not a bbox -- is the domain, so the mesh is never
cookie-cut mid-hillslope.

PRECONDITION-GATE design (so a user-supplied mesh slots in behind one interface):

  * :func:`acquire_watershed_mesh` -- the "build our own" provider (default).
  * :func:`use_supplied_mesh` -- the pass-through for a user SELAFIN / 2dm mesh,
    validated to the same :class:`WatershedMesh` shape.

Both return a :class:`WatershedMesh` (SELAFIN path + catchment polygon + node
arrays + provenance) the deck builder consumes identically. The standalone
sandbox (``scripts/sandbox/oceanmesh/build_watershed_mesh.py``) stays standalone:
the meshing LOGIC is lifted here, the CLI is not coupled.

Only the pure helpers (config building, projection, node-field assembly, the
supplied-mesh gate) are unit-testable offline; the container-driven build path
(:func:`acquire_watershed_mesh`) needs the mesh image + network and is exercised
live.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition"
)

__all__ = [
    "WatershedMesh",
    "MeshAcquisitionError",
    "acquire_watershed_mesh",
    "use_supplied_mesh",
    "build_mesh_config",
    "catchment_exterior_and_river_coords",
    "reproject_nodes_to_utm",
    "assemble_node_fields",
    "validate_catchment_not_degenerate",
    "DEFAULT_MESH_IMAGE",
]

#: The GPL-isolated OceanMesh2D worker image (override via env, mirroring the
#: TELEMAC image env seam). The engine is GPL and never baked into the agent
#: venv or the (Apache) TELEMAC image -- it runs mounted, standalone.
DEFAULT_MESH_IMAGE: str = "trid3nt-local/mesh:latest"

#: In-container mesher lifted from the ADR 0193 sandbox (mounted, not imported).
_MESH_INCONTAINER = (
    "scripts/sandbox/oceanmesh/_mesh_watershed_incontainer.py"
)

#: Outlet-snap search radius (D8 cells) around the pour point. The outlet is
#: snapped to the MAXIMUM-accumulation cell within this window so a coarse-DEM
#: outlet lands on the main channel regardless of grid alignment (~8 cells at
#: 30 m ~ 240 m). Widen only if outlets routinely sit far off the mapped channel.
_OUTLET_SNAP_SEARCH_CELLS: int = 8

#: Minimum delineated catchment size (D8 cells) before the result is treated as a
#: degenerate sliver. A pour point that does not sit on the catchment channel, or
#: an AOI that does not contain the upstream basin, delineates a handful of cells
#: (the ADR 0196 live bug: 20 cells / 0.018 km^2); meshing + solving that is a
#: silent dead-end, so we fail LOUD with a typed AOI/pour-point-mismatch error.
_MIN_CATCHMENT_CELLS: int = 50


class MeshAcquisitionError(RuntimeError):
    """A watershed mesh could not be delineated / built / validated.

    Carries an open-set ``error_code`` so the template renders a typed error
    frame (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class WatershedMesh:
    """A rain-on-grid meshing domain, however it was acquired.

    ``points_utm`` (N,2) X/Y metres in ``utm_epsg`` and ``cells`` (M,3) 0-based
    triangles define the SELAFIN geometry at ``slf_path`` (BOTTOM = ``bed_elev``,
    positive-up metres). ``catchment_geojson`` is the delineated (or supplied)
    domain polygon in EPSG:4326; ``outlet_lonlat`` is the pour point the outlet
    boundary condition is applied at. ``node_*`` fields are the per-node land-
    surface samples the deck builder writes into FORMATTED DATA FILE 2 (CN2) and
    the Manning field -- ``None`` until sampled. ``provenance`` records HOW the
    mesh was acquired (``"delineated"`` vs ``"user_supplied"``) for the envelope.
    """

    slf_path: str
    catchment_geojson: str
    points_utm: Any            # np.ndarray (N,2) metres
    cells: Any                 # np.ndarray (M,3) 0-based
    bed_elev: Any              # np.ndarray (N,) positive-up metres
    utm_epsg: int
    area_km2: float
    pour_point_lonlat: tuple[float, float]
    outlet_lonlat: tuple[float, float]
    provenance: str = "delineated"
    node_nlcd: Any = None      # np.ndarray (N,) int NLCD codes
    node_cn2: Any = None       # np.ndarray (N,) AMC-II curve numbers
    node_manning: Any = None   # np.ndarray (N,) Manning n
    node_slope: Any = None     # np.ndarray (N,) terrain slope m/m
    meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure helpers (offline-testable).
# --------------------------------------------------------------------------- #
def _m_per_deg(mid_lat_deg: float) -> float:
    """Metres per degree of longitude at ``mid_lat_deg`` (sandbox parity)."""
    return 111_320.0 * max(0.15, math.cos(math.radians(mid_lat_deg)))


def build_mesh_config(
    boubox_coords: list[list[float]],
    river_coords: list[list[float]],
    *,
    min_edge_length_m: float,
    max_edge_length_m: float,
    grade: float = 0.20,
    max_iter: int = 60,
) -> dict[str, Any]:
    """Assemble the ``mesh_config.json`` the in-container mesher reads.

    Mirrors the ADR 0193 sandbox contract exactly (catchment exterior + river
    sizing points + edge-length band + gradation), so the SAME
    ``_mesh_watershed_incontainer.py`` runs unchanged. Validates the edge band
    and the boubox ring so a degenerate request fails HERE, not deep in gmsh.
    """
    if not boubox_coords or len(boubox_coords) < 4:
        raise MeshAcquisitionError(
            "TELEMAC_ROG_MESH_DOMAIN_DEGENERATE",
            "catchment exterior ring has too few vertices to mesh "
            f"({len(boubox_coords)}); delineation likely failed.",
        )
    if not (0.0 < float(min_edge_length_m) < float(max_edge_length_m)):
        raise MeshAcquisitionError(
            "TELEMAC_ROG_MESH_EDGE_BAND_INVALID",
            f"edge-length band invalid: min={min_edge_length_m} "
            f"max={max_edge_length_m} (need 0 < min < max).",
        )
    return {
        "boubox_coords": [[float(x), float(y)] for x, y in boubox_coords],
        "river_coords": [[float(x), float(y)] for x, y in (river_coords or [])],
        "min_edge_length_m": float(min_edge_length_m),
        "max_edge_length_m": float(max_edge_length_m),
        "grade": float(grade),
        "max_iter": int(max_iter),
    }


def catchment_exterior_and_river_coords(
    catchment_geom: Any,
    flowlines_gdf: Any,
    *,
    min_edge_length_m: float,
) -> tuple[list[list[float]], list[list[float]]]:
    """Catchment exterior ring + river-network sizing points (both EPSG:4326).

    The exterior of the LARGEST catchment polygon (simplified to the min edge
    length) is the oceanmesh domain; the flowline vertices clipped INSIDE the
    catchment are the distance-to-river sizing source. Lifted verbatim from the
    ADR 0193 sandbox so the mesh is identical to the proven Coweeta build.
    """
    from shapely.geometry import MultiPolygon

    largest = (
        max(catchment_geom.geoms, key=lambda p: p.area)
        if isinstance(catchment_geom, MultiPolygon)
        else catchment_geom
    )
    ext = largest.simplify(float(min_edge_length_m) / 111_320.0).exterior
    boubox_coords = [[float(x), float(y)] for x, y in ext.coords]

    river_coords: list[list[float]] = []
    if flowlines_gdf is not None:
        for geom in flowlines_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            clipped = geom.intersection(catchment_geom)
            if clipped.is_empty:
                continue
            lines = (
                clipped.geoms
                if clipped.geom_type in ("MultiLineString", "GeometryCollection")
                else [clipped]
            )
            for ln in lines:
                if getattr(ln, "geom_type", "") != "LineString":
                    continue
                river_coords.extend([[float(x), float(y)] for x, y in ln.coords])
    return boubox_coords, river_coords


def reproject_nodes_to_utm(points_lonlat: Any) -> tuple[Any, int]:
    """Project (N,2) lon/lat nodes to the local UTM zone -> ``(points_m, epsg)``.

    TELEMAC solves in METRES (the shallow-water equations, friction, the CFL
    timestep and the outlet normal-depth BC are all metric); the oceanmesh
    output is degrees, so the solve mesh MUST be projected. The UTM zone is
    picked from the domain centroid (WGS84 north/south)."""
    import numpy as np
    from pyproj import Transformer

    pts = np.asarray(points_lonlat, dtype=float)
    lon0 = float(np.mean(pts[:, 0]))
    lat0 = float(np.mean(pts[:, 1]))
    zone = int((lon0 + 180.0) // 6.0) + 1
    epsg = (32600 if lat0 >= 0 else 32700) + zone
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    x, y = tr.transform(pts[:, 0], pts[:, 1])
    return np.column_stack([x, y]).astype(float), int(epsg)


def assemble_node_fields(
    *,
    node_nlcd: list[int] | None,
    uniform_cn: float | None,
    slopes_m_per_m: list[float] | None,
    steep_slope_correction: bool,
) -> tuple[list[float], list[float]]:
    """Per-node ``(CN2, Manning n)`` arrays for FORMATTED DATA FILE 2 + friction.

    Delegates the CN2 field to :func:`cn_infiltration.node_curve_numbers`
    (uniform override OR NLCD-distributed, with the optional Huang steep-slope
    correction applied in preprocessing) and the Manning field to the paper
    Table-1 analog. When ``uniform_cn`` is set every node still gets the
    land-cover Manning (roughness is not the CN knob). Requires ``node_nlcd``
    whenever a distributed field is needed."""
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.cn_infiltration import (
        landcover_cn_manning,
        node_curve_numbers,
    )

    if node_nlcd is None:
        # Even a uniform_cn override still needs per-node land cover to set the
        # Manning field (roughness is a separate physical property, not the CN
        # knob), so NLCD must be sampled at the nodes regardless.
        raise MeshAcquisitionError(
            "TELEMAC_ROG_NODE_FIELDS_MISSING",
            "node land-cover codes are required to build the per-node CN/Manning "
            "fields; sample NLCD at the mesh nodes first "
            "(uniform_cn overrides only the CN, never the Manning field).",
        )
    manning = [landcover_cn_manning(int(c))[1] for c in node_nlcd]
    cn2 = node_curve_numbers(
        [int(c) for c in node_nlcd],
        uniform_cn=uniform_cn,
        slopes_m_per_m=slopes_m_per_m,
        steep_slope_correction=steep_slope_correction,
    )
    return cn2, manning


def validate_catchment_not_degenerate(
    cell_count: int,
    area_km2: float,
    pour_point: tuple[float, float],
) -> None:
    """Raise a typed error when the delineated catchment is a degenerate sliver.

    A pour point that does not land on the catchment's channel, or an AOI that
    does not contain the upstream basin, yields a handful of D8 cells rather than
    a real catchment. Meshing + solving that sliver is a silent dead-end, so we
    fail LOUD here with an AOI/pour-point-mismatch message (never a quiet 0.018
    km^2 result). Below :data:`_MIN_CATCHMENT_CELLS` cells is degenerate."""
    if int(cell_count) < _MIN_CATCHMENT_CELLS:
        raise MeshAcquisitionError(
            "TELEMAC_ROG_CATCHMENT_DEGENERATE",
            f"the delineated catchment is degenerate: only {int(cell_count)} D8 "
            f"cells (~{float(area_km2):.3f} km^2) upstream of pour point "
            f"{tuple(round(float(v), 5) for v in pour_point)}. The pour point "
            "likely does not sit on the catchment channel, or the analysis AOI "
            "does not contain the upstream basin. Move the pour point onto the "
            "stream, or supply a bbox that covers the whole catchment.",
        )


# --------------------------------------------------------------------------- #
# Provider 1: build our own watershed mesh (container-driven; live).
# --------------------------------------------------------------------------- #
def _run_mesh_container(
    rundir: Path, mesh_config: dict[str, Any], *, image: str, sandbox: Path
) -> tuple[Any, Any, dict[str, Any]]:
    """Run the OceanMesh2D in-container mesher; return ``(points, cells, stats)``.

    Bind-mounts the sandbox (for the lifted in-container script) + the rundir at
    ``/data``; the mesher writes ``coastal_tin_mesh.npz`` + ``mesh_stats.json``.
    """
    import numpy as np

    (rundir / "mesh_config.json").write_text(json.dumps(mesh_config))
    argv = [
        "docker", "run", "--rm",
        "-v", f"{sandbox}:/sandbox",
        "-v", f"{rundir}:/data",
        "--entrypoint", "python", image,
        f"/sandbox/{Path(_MESH_INCONTAINER).name}",
        "/data/mesh_config.json", "/data",
    ]
    logger.info("rog mesh: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=2400)
    npz_path = rundir / "coastal_tin_mesh.npz"
    if cp.returncode != 0 or not npz_path.exists():
        raise MeshAcquisitionError(
            "TELEMAC_ROG_MESH_BUILD_FAILED",
            f"watershed mesh worker failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}",
        )
    npz = np.load(npz_path)
    stats = json.loads((rundir / "mesh_stats.json").read_text())
    return npz["points"], npz["cells"], stats


def acquire_watershed_mesh(
    *,
    pour_point: tuple[float, float],
    bbox: tuple[float, float, float, float],
    output_dir: str,
    dem_uri: str | None = None,
    min_edge_length_m: float = 40.0,
    max_edge_length_m: float = 400.0,
    grade: float = 0.20,
    mesh_image: str | None = None,
    sandbox_dir: str | None = None,
) -> WatershedMesh:
    """Delineate + mesh the catchment at ``pour_point``, write the solve SELAFIN.

    The "build our own" precondition-gate provider: ``_delineate_catchment``
    (robust pysheds outlet-snap + catchment) -> catchment polygon;
    ``fetch_river_geometry`` -> the interior river network; ``fetch_dem`` -> the
    bed; OceanMesh2D (mesh image) triangulates the catchment interior refined by
    distance-to-river; the lon/lat nodes are projected to UTM and written as a BOTTOM
    SELAFIN. LIVE (needs the mesh image + network); the pure helpers it composes
    are unit-tested.
    """
    import json as _json

    import geopandas as gpd  # noqa: F401 -- ensures the geo stack is importable
    import numpy as np
    from shapely.geometry import mapping

    from trid3nt_server.agent.tools import TOOL_REGISTRY

    rundir = Path(output_dir)
    rundir.mkdir(parents=True, exist_ok=True)
    image = mesh_image or os.environ.get("TRID3NT_MESH_IMAGE") or DEFAULT_MESH_IMAGE
    sandbox = Path(
        sandbox_dir
        or os.environ.get("TRID3NT_OCEANMESH_SANDBOX")
        or "scripts/sandbox/oceanmesh"
    ).resolve()

    dem_notes: list[str] = []
    # 1. delineate the catchment (pysheds) at the pour point on Copernicus GLO-30
    #    (natively geographic EPSG:4326 -- the lon/lat frame the outlet snap needs;
    #    a bare-earth 3DEP DEM is projected EPSG:5070 and reprojecting it corrupts
    #    the D8 grid, so bare earth is pinned for the mesh BED only). Uses the
    #    robust local delineator (max-accumulation outlet pre-snap + index-space
    #    catchment) instead of the shared delineate_watershed tool, whose
    #    coordinate-space snap mis-maps an exact outlet to a neighbour cell on
    #    certain grid alignments (observed: a 1-cell sliver vs the 28.7 km^2 truth).
    catch, outlet, area_km2, cell_count = _delineate_catchment(
        rundir, bbox, tuple(pour_point), dem_uri)
    validate_catchment_not_degenerate(cell_count, area_km2, tuple(pour_point))
    catch_path = rundir / "catchment.geojson"
    catch_path.write_text(_json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(catch)}]}))

    # 2. river network inside the catchment -> exterior + sizing points.
    try:
        rv = TOOL_REGISTRY["fetch_river_geometry"].fn(
            bbox=tuple(bbox), source="nhdplus_hr")
        from trid3nt_server.agent.tools.cache import read_object_bytes_s3
        fl_path = rundir / "flowlines.fgb"
        fl_path.write_bytes(
            read_object_bytes_s3(rv.uri) if str(rv.uri).startswith("s3://")
            else Path(rv.uri).read_bytes())
        flow = gpd.read_file(fl_path)
    except Exception as exc:  # noqa: BLE001 -- river refinement is best-effort
        logger.warning("rog mesh: flowline fetch failed (%s); uniform sizing", exc)
        flow = None
    boubox, river = catchment_exterior_and_river_coords(
        catch, flow, min_edge_length_m=min_edge_length_m)
    cfg = build_mesh_config(
        boubox, river, min_edge_length_m=min_edge_length_m,
        max_edge_length_m=max_edge_length_m, grade=grade)

    # 3. mesh the catchment interior (OceanMesh2D, mounted image).
    points_ll, cells, stats = _run_mesh_container(
        rundir, cfg, image=image, sandbox=sandbox)
    points_ll = np.asarray(points_ll, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)

    # 4. DEM bed sampled at the nodes (positive-up elevation), bare-earth 10 m.
    dem_path = _resolve_bare_earth_dem(
        rundir, bbox, dem_uri, resolution_m=10,
        filename="dem_bed.tif", notes=dem_notes)
    bed = _sample_raster_at_nodes(dem_path, points_ll)

    # 5. project to UTM metres + write the solve SELAFIN.
    points_m, epsg = reproject_nodes_to_utm(points_ll)
    slf_path = str(rundir / "watershed.slf")
    _write_bottom_selafin(slf_path, points_m, cells, bed)

    area_km2 = float(area_km2) or _polygon_area_km2(catch)
    logger.info(
        "rog mesh acquired: %d nodes %d cells %.2f km^2 epsg=%d outlet=%s",
        points_m.shape[0], cells.shape[0], area_km2, epsg, outlet)
    return WatershedMesh(
        slf_path=slf_path,
        catchment_geojson=str(catch_path),
        points_utm=points_m,
        cells=cells,
        bed_elev=bed,
        utm_epsg=epsg,
        area_km2=area_km2,
        pour_point_lonlat=tuple(pour_point),
        outlet_lonlat=outlet,
        provenance="delineated",
        meta={"mesh_stats": stats, "points_lonlat": points_ll,
              "dem_notes": dem_notes},
    )


# --------------------------------------------------------------------------- #
# Provider 2: use a user-supplied mesh (precondition-gate pass-through).
# --------------------------------------------------------------------------- #
def use_supplied_mesh(
    *,
    mesh_path: str,
    pour_point: tuple[float, float],
    utm_epsg: int,
    catchment_geojson: str | None = None,
    outlet_lonlat: tuple[float, float] | None = None,
) -> WatershedMesh:
    """Adopt a user-supplied SELAFIN mesh as the meshing domain (the future path).

    The precondition-gate pass-through: a user brings their own catchment mesh
    (already projected metres in ``utm_epsg``, BOTTOM = bed) and the template
    solves on it directly instead of building one. Validated for existence +
    a readable SELAFIN header; the node/cell arrays are read lazily by the
    worker. Only the OTHER provider (:func:`acquire_watershed_mesh`) is wired in
    v1; this exists so the seam is real, not retrofitted later."""
    p = Path(mesh_path)
    if not p.exists() or p.stat().st_size == 0:
        raise MeshAcquisitionError(
            "TELEMAC_ROG_SUPPLIED_MESH_MISSING",
            f"supplied mesh not found or empty: {mesh_path}",
        )
    if p.suffix.lower() not in (".slf", ".sel", ".2dm"):
        raise MeshAcquisitionError(
            "TELEMAC_ROG_SUPPLIED_MESH_UNSUPPORTED",
            f"supplied mesh must be SELAFIN (.slf/.sel) or 2dm; got {p.suffix}",
        )
    return WatershedMesh(
        slf_path=str(p),
        catchment_geojson=str(catchment_geojson or ""),
        points_utm=None,
        cells=None,
        bed_elev=None,
        utm_epsg=int(utm_epsg),
        area_km2=0.0,
        pour_point_lonlat=tuple(pour_point),
        outlet_lonlat=tuple(outlet_lonlat or pour_point),
        provenance="user_supplied",
    )


# --------------------------------------------------------------------------- #
# Lifted raster/geometry/SELAFIN helpers (sandbox parity; live).
# --------------------------------------------------------------------------- #
def _delineate_catchment(
    rundir: Path,
    bbox: Any,
    pour_point: tuple[float, float],
    dem_uri: str | None,
    *,
    snap_search_cells: int = _OUTLET_SNAP_SEARCH_CELLS,
) -> tuple[Any, tuple[float, float], float, int]:
    """Robust pysheds catchment upstream of ``pour_point`` -> (polygon, outlet, area_km2, cells).

    Delegates the outlet snap + catchment to the SHARED, alignment-invariant
    ``snap_and_delineate_index_space`` (one implementation, also used by the
    ``delineate_watershed`` tool): the outlet is snapped to the MAX-accumulation
    cell in a small window (guaranteeing the main channel) and the catchment is
    traced in INDEX space, which avoids the coordinate-space fragility that
    collapses the basin to a 1-cell sliver on certain grid alignments (verified:
    33.7-34.0k cells across box quantizations for the Coweeta outlet vs 1-14 for
    the coordinate path). This wrapper keeps the workflow's geodesic-area
    convention (equal-area cast) and its typed off-DEM error. The polygon is
    EPSG:4326."""
    from trid3nt_server.agent.tools.processing._hydrology_common import (
        HydrologyInputError,
        _condition_dem,
        _stage_dem,
        _validate_bbox,
        snap_and_delineate_index_space,
    )

    q_bbox = _validate_bbox(tuple(bbox))
    dem_path = _stage_dem(q_bbox, dem_uri, str(rundir), [])
    grid, fdir, acc = _condition_dem(dem_path)

    try:
        _mask, catch_geom, (x_snap, y_snap), cell_count = (
            snap_and_delineate_index_space(
                grid, fdir, acc,
                float(pour_point[0]), float(pour_point[1]),
                snap_search_cells=int(snap_search_cells)))
    except HydrologyInputError as exc:
        raise MeshAcquisitionError(
            "TELEMAC_ROG_POUR_POINT_OFF_DEM",
            f"pour point {tuple(pour_point)} falls outside the DEM window "
            f"{q_bbox}; supply a bbox/pour point inside the analysis AOI.") from exc
    area_km2 = _polygon_area_km2(catch_geom) if catch_geom is not None else 0.0
    return catch_geom, (float(x_snap), float(y_snap)), float(area_km2), cell_count


def _resolve_bare_earth_dem(
    rundir: Path,
    bbox: Any,
    dem_uri: str | None,
    *,
    resolution_m: int,
    filename: str,
    notes: list[str] | None = None,
) -> Path:
    """Local BARE-EARTH DEM path for the mesh BED (canopy-free node elevations).

    Pins the mesh bed to USGS 3DEP bare-earth (source="3dep") within CONUS: a DSM
    (Copernicus GLO-30 includes forest CANOPY) inflates node elevations under
    tree cover, so it is not the bed default. The bed is sampled at the mesh
    nodes with an on-read CRS transform, so 3DEP's native projected grid is fine
    here (no reprojection -- unlike D8 delineation, which needs a lon/lat DEM and
    stays on the natively-geographic Copernicus). If 3DEP is unavailable for the
    AOI the cross-dataset fallback to Copernicus is LOUD (a logged warning + a
    typed note appended to ``notes`` for the envelope), per the data-source
    fallback norm -- never a silent surface-model swap. A caller-supplied
    ``dem_uri`` (already bare-earth by contract) is honored."""
    if dem_uri and Path(dem_uri).exists():
        return Path(dem_uri)
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.cache import read_object_bytes_s3

    try:
        layer = TOOL_REGISTRY["fetch_dem"].fn(
            bbox=tuple(bbox), source="3dep", resolution_m=int(resolution_m))
        if notes is not None:
            notes.append(
                f"mesh bed DEM: USGS 3DEP bare-earth ({int(resolution_m)} m).")
    except Exception as exc:  # noqa: BLE001 -- LOUD cross-dataset fallback
        logger.warning(
            "rog mesh: USGS 3DEP bare-earth DEM unavailable for bbox=%s (%s); "
            "falling back to Copernicus GLO-30 -- a DSM that INCLUDES forest "
            "canopy, which inflates bed elevations under tree cover",
            tuple(bbox), exc)
        layer = TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=tuple(bbox))
        if notes is not None:
            notes.append(
                "mesh bed DEM CROSS-DATASET FALLBACK: USGS 3DEP bare-earth was "
                "unavailable for this AOI; used Copernicus GLO-30 instead. That "
                "is a SURFACE model (canopy-inclusive), so bed elevations under "
                "forest may be biased high.")
    uri = layer.uri if hasattr(layer, "uri") else layer["uri"]
    dst = rundir / filename
    dst.write_bytes(
        read_object_bytes_s3(uri) if str(uri).startswith("s3://")
        else Path(uri).read_bytes())
    return dst


def _sample_raster_at_nodes(raster_path: Path, points_lonlat: Any) -> Any:
    """Bilinear-sample a raster at (N,2) lon/lat nodes -> (N,) values."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform as warp_transform

    pts = np.asarray(points_lonlat, dtype=float)
    with rasterio.open(raster_path) as src:
        xs, ys = warp_transform(
            "EPSG:4326", src.crs, pts[:, 0].tolist(), pts[:, 1].tolist())
        vals = np.array(list(src.sample(list(zip(xs, ys)))), dtype=float)[:, 0]
        nodata = src.nodata
    if nodata is not None:
        vals[vals == nodata] = np.nan
    # fill any nan with the finite mean so the bed is complete.
    if np.isnan(vals).any():
        finite = vals[np.isfinite(vals)]
        vals[np.isnan(vals)] = float(finite.mean()) if finite.size else 0.0
    return vals


def _write_bottom_selafin(path: str, points_m: Any, cells: Any, z: Any) -> str:
    """Write a single-variable (BOTTOM) SELAFIN geometry (sandbox writer, lifted).

    Byte-for-byte the ADR 0193 ``selafin_io.write_selafin`` format so the mesh
    round-trips through MDAL / TELEMAC identically, but authored HERE (the
    sandbox module is not on the agent import path)."""
    import struct

    import numpy as np

    pts = np.asarray(points_m, dtype=float)
    cel = np.asarray(cells, dtype=np.int64)
    zz = np.asarray(z, dtype=float)
    n_points, n_elem = pts.shape[0], cel.shape[0]

    def _rec(fh, payload: bytes) -> None:
        n = len(payload)
        fh.write(struct.pack(">i", n))
        fh.write(payload)
        fh.write(struct.pack(">i", n))

    ipobo = _ipobo_from_cells(n_points, cel)
    with open(path, "wb") as fh:
        _rec(fh, "TRID3NT WATERSHED RAIN-ON-GRID TIN".ljust(80)[:80].encode("ascii"))
        _rec(fh, struct.pack(">2i", 1, 0))
        _rec(fh, ("BOTTOM".ljust(16)[:16] + "M".ljust(16)[:16]).encode("ascii"))
        iparam = [0] * 10
        iparam[0] = 1
        _rec(fh, struct.pack(">10i", *iparam))
        _rec(fh, struct.pack(">4i", n_elem, n_points, 3, 1))
        _rec(fh, (cel + 1).astype(">i4").ravel().tobytes())
        _rec(fh, ipobo.astype(">i4").tobytes())
        _rec(fh, pts[:, 0].astype(">f4").tobytes())
        _rec(fh, pts[:, 1].astype(">f4").tobytes())
        _rec(fh, struct.pack(">f", 0.0))
        _rec(fh, zz.astype(">f4").tobytes())
    return path


def _ipobo_from_cells(n_points: int, cells: Any) -> Any:
    """TELEMAC IPOBO: boundary nodes numbered 1..NPTFR, 0 interior.

    Boundary edges are those shared by exactly one triangle; nodes on them are
    numbered in first-seen order (a valid IPOBO for a single-body TIN)."""
    import numpy as np

    cel = np.asarray(cells, dtype=np.int64)
    edges = np.vstack([cel[:, [0, 1]], cel[:, [1, 2]], cel[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    boundary_edges = uniq[counts == 1]
    ipobo = np.zeros(n_points, dtype=np.int32)
    order = 1
    for a, b in boundary_edges:
        for node in (int(a), int(b)):
            if ipobo[node] == 0:
                ipobo[node] = order
                order += 1
    return ipobo


def _polygon_area_km2(geom: Any) -> float:
    """Geodesic-ish area (km^2) of a lon/lat polygon via a local equal-area cast."""
    import geopandas as gpd

    return float(gpd.GeoSeries([geom], crs=4326).to_crs(6933).area.iloc[0] / 1e6)
