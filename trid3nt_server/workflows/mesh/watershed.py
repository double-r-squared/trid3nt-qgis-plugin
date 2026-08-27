"""The CATCHMENT generation strategy of the shared mesh front.

A watershed is a domain SHAPE, not a TELEMAC fact: delineate the basin upstream
of a pour point, take the river network inside it as the sizing source,
triangulate the interior in the GPL-isolated OceanMesh2D image, project the nodes
to metres and sample a bed at them. Nothing here knows which solver consumes the
result - the per-solver writers live one tier down, beside the engine that reads
them (the placement rule: a capability lives at the highest layer where it needs
no specialization).

Two routes converge on one value, exactly as the mesh charter rules:

  * :func:`generate_catchment_mesh` - the GENERATED default, a labeled fallback
    rather than a stance;
  * :func:`adopt_supplied_mesh` / :func:`adopt_supplied_mesh_2dm` - a mesh the
    user AUTHORED elsewhere (a standalone ``build_mesh`` call, an SMS
    ``.2dm``, a hand-edited artifact), adopted as-is.

Both yield a :class:`CatchmentMesh`, so a consumer cannot tell which route ran
except by reading ``provenance`` - which is the whole point of the slate.

The world-READS this module performs sit behind :func:`resolve_bed_dem`,
:func:`resolve_landcover` and :func:`resolve_river_network`, which are declared as
``Data`` producers by the templates that want them. A step never fetches: the
fetcher router's cache, ladders and provenance live once, and a producer is where
that middleware is reached from.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.mesh.watershed")

__all__ = [
    "CatchmentMesh",
    "DEFAULT_BED_RESOLUTION_M",
    "DEFAULT_GRADE",
    "DEFAULT_MAX_EDGE_M",
    "DEFAULT_MAX_ITER",
    "DEFAULT_MESH_IMAGE",
    "DEFAULT_MIN_EDGE_M",
    "DEFAULT_OUTLET_SNAP_CELLS",
    "DEFAULT_RIVER_SOURCE",
    "MeshGenerationError",
    "adopt_supplied_mesh",
    "adopt_supplied_mesh_2dm",
    "build_mesh_config",
    "catchment_aoi",
    "catchment_exterior_and_river_coords",
    "delineate_catchment",
    "generate_catchment_mesh",
    "node_slopes_from_mesh",
    "polygon_area_km2",
    "read_2dm_mesh",
    "reproject_nodes_to_utm",
    "resolve_bed_dem",
    "resolve_landcover",
    "resolve_river_network",
    "sample_raster_at_nodes",
    "utm_epsg_for",
    "validate_catchment_not_degenerate",
]

#: The GPL-isolated OceanMesh2D worker image (override via env, mirroring the
#: TELEMAC image env seam). The engine is GPL and never baked into the agent venv
#: or the (Apache) TELEMAC image - it runs mounted, standalone.
DEFAULT_MESH_IMAGE: str = "trid3nt-local/mesh:latest"

#: In-container mesher, mounted rather than imported (it runs inside the GPL image).
_MESH_INCONTAINER = "scripts/sandbox/oceanmesh/_mesh_watershed_incontainer.py"

#: Wall-clock ceiling on one containerized triangulation. OceanMesh2D iterates to
#: a quality target, so a pathological sizing function can spin; a bound turns
#: that into a typed failure instead of a hung run.
_MESH_CONTAINER_TIMEOUT_S = 2400

#: Minimum delineated catchment size (D8 cells) before the result is a degenerate
#: sliver. A pour point that does not sit on the catchment channel, or an AOI that
#: does not contain the upstream basin, delineates a handful of cells (the live
#: bug: 20 cells / 0.018 km^2); meshing and solving that is a silent dead-end.
#: A FLOOR on believability, not a physics dial - which is why it is a labeled
#: constant here rather than a form row nobody would ever move.
_MIN_CATCHMENT_CELLS: int = 50

#: Metres per degree of latitude, used only to turn a simplification tolerance in
#: metres into the degrees the lon/lat catchment ring is simplified in.
_M_PER_DEG = 111_320.0

# -- the catchment mesher's OWN defaults, in ONE place --------------------- #
# Two callers reach this front: the declarative rain-on-grid template, whose
# params promise these numbers in prose, and the ``watershed`` mesher, whose
# standalone ask has no param sheet at all. A default that lived in either caller
# would be a second source of truth for one dial - which is the exact defect the
# composer this replaces carried (40 m at the call site, 400 m in the signature
# it overrode, and only one of them ever ran).

#: The edge-length BAND a catchment interior is triangulated between: fine in the
#: channel band, coarse on the hillslopes.
DEFAULT_MIN_EDGE_M: float = 40.0
DEFAULT_MAX_EDGE_M: float = 300.0

#: How fast the edge length may grow between the two, and the improvement-
#: iteration cap the triangulator stops at.
DEFAULT_GRADE: float = 0.20
DEFAULT_MAX_ITER: int = 60

#: Search window (D8 cells) the pour point is snapped to the maximum-accumulation
#: cell within, so a coarse-DEM outlet lands on the main channel regardless of
#: grid alignment (~8 cells at 30 m is ~240 m).
DEFAULT_OUTLET_SNAP_CELLS: int = 8

#: Ground resolution the BARE-EARTH bed is sampled at the mesh nodes from.
DEFAULT_BED_RESOLUTION_M: int = 10

#: The channel network mesh refinement is sized by distance to.
DEFAULT_RIVER_SOURCE: str = "nhdplus_hr"


class MeshGenerationError(RuntimeError):
    """A catchment mesh could not be delineated, built, adopted or validated.

    Carries an open-set ``error_code`` so the consuming template renders a typed
    error frame rather than a silent dead-end.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class CatchmentMesh:
    """A delineated (or adopted) catchment, meshed - the engine-neutral artifact.

    ``points_utm`` (N,2) X/Y metres in ``utm_epsg`` and ``cells`` (M,3) 0-based
    triangles are the geometry; ``bed_elev`` is the positive-up bed at each node.
    ``catchment_geojson`` is the domain polygon in EPSG:4326 and ``outlet_lonlat``
    the SNAPPED pour point the outlet boundary condition is applied at - snapped,
    because the point a user clicks is rarely the max-accumulation cell the D8
    grid routes through. ``provenance`` records WHICH route produced it
    (``"generated"`` vs ``"supplied"``), and ``notes`` carries every labeled
    substitution the acquisition made, so a consumer narrates them rather than
    discovering them.
    """

    slug: str
    points_utm: Any            # np.ndarray (N,2) metres
    cells: Any                 # np.ndarray (M,3) 0-based
    bed_elev: Any              # np.ndarray (N,) positive-up metres
    points_lonlat: Any         # np.ndarray (N,2) EPSG:4326
    utm_epsg: int
    area_km2: float
    pour_point_lonlat: tuple[float, float]
    outlet_lonlat: tuple[float, float]
    catchment_geojson: str = ""
    provenance: str = "generated"
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    #: An ADOPTED mesh already exists as a file; the per-solver writer stages that
    #: file verbatim instead of serializing the node arrays. Empty on the
    #: generated route, where the writer authors the geometry itself.
    source_path: str = ""

    @property
    def node_count(self) -> int:
        return int(0 if self.points_utm is None else len(self.points_utm))

    @property
    def element_count(self) -> int:
        return int(0 if self.cells is None else len(self.cells))


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
def utm_epsg_for(lon: float, lat: float) -> int:
    """The WGS84 UTM zone EPSG a lon/lat falls in - ONE implementation.

    The pour point and the mesh centroid must read their zone from this one
    function: a second copy of the arithmetic is a second chance for a zone to
    disagree with itself on a run that straddles a zone boundary.
    """
    zone = int((float(lon) + 180.0) // 6.0) + 1
    return (32600 if float(lat) >= 0.0 else 32700) + zone


def catchment_aoi(pour_point: tuple[float, float],
                  half_deg: float) -> tuple[float, float, float, float]:
    """The analysis AOI a catchment is delineated inside, centred on its OUTLET.

    Centred on the outlet rather than on a geocoded place, because a place bbox
    names a TOWN and need not contain the UPSTREAM catchment: the live bug was
    'Otto, NC' clipping the Coweeta basin mid-hillslope and delineating a 20-cell
    sliver. The delineation truncates at the box edge, so this must OVER-cover.
    """
    lon, lat = float(pour_point[0]), float(pour_point[1])
    b = float(half_deg)
    return (max(lon - b, -180.0), max(lat - b, -90.0),
            min(lon + b, 180.0), min(lat + b, 90.0))


def build_mesh_config(
    boubox_coords: list[list[float]],
    river_coords: list[list[float]],
    *,
    min_edge_length_m: float,
    max_edge_length_m: float,
    grade: float,
    max_iter: int,
) -> dict[str, Any]:
    """Assemble the ``mesh_config.json`` the in-container mesher reads.

    Validates the edge band and the boubox ring so a degenerate request fails
    HERE rather than deep inside gmsh, several container minutes later.
    """
    if not boubox_coords or len(boubox_coords) < 4:
        raise MeshGenerationError(
            "MESH_CATCHMENT_DOMAIN_DEGENERATE",
            "the catchment exterior ring has too few vertices to mesh "
            f"({len(boubox_coords)}); delineation likely failed.")
    if not (0.0 < float(min_edge_length_m) < float(max_edge_length_m)):
        raise MeshGenerationError(
            "MESH_EDGE_BAND_INVALID",
            f"edge-length band invalid: min={min_edge_length_m} "
            f"max={max_edge_length_m} (need 0 < min < max).")
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
    """Catchment exterior ring + river-network sizing points, both EPSG:4326.

    The exterior of the LARGEST catchment polygon (simplified to the minimum edge
    length) is the meshing domain; the flowline vertices clipped INSIDE the
    catchment are the distance-to-river sizing source, which is what refines the
    mesh where the water concentrates.
    """
    from shapely.geometry import MultiPolygon

    largest = (max(catchment_geom.geoms, key=lambda p: p.area)
               if isinstance(catchment_geom, MultiPolygon) else catchment_geom)
    ext = largest.simplify(float(min_edge_length_m) / _M_PER_DEG).exterior
    boubox_coords = [[float(x), float(y)] for x, y in ext.coords]

    river_coords: list[list[float]] = []
    if flowlines_gdf is not None:
        for geom in flowlines_gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            clipped = geom.intersection(catchment_geom)
            if clipped.is_empty:
                continue
            lines = (clipped.geoms
                     if clipped.geom_type in ("MultiLineString", "GeometryCollection")
                     else [clipped])
            for ln in lines:
                if getattr(ln, "geom_type", "") != "LineString":
                    continue
                river_coords.extend([[float(x), float(y)] for x, y in ln.coords])
    return boubox_coords, river_coords


def reproject_nodes_to_utm(points_lonlat: Any) -> tuple[Any, int]:
    """Project (N,2) lon/lat nodes to the local UTM zone -> ``(points_m, epsg)``.

    A shallow-water solver works in METRES - the momentum equations, the friction
    law, the CFL time step and a normal-depth outlet boundary all are - while the
    mesher's output is degrees, so the solve mesh MUST be projected. The zone is
    the domain centroid's.
    """
    import numpy as np
    from pyproj import Transformer

    pts = np.asarray(points_lonlat, dtype=float)
    epsg = utm_epsg_for(float(np.mean(pts[:, 0])), float(np.mean(pts[:, 1])))
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    x, y = tr.transform(pts[:, 0], pts[:, 1])
    return np.column_stack([x, y]).astype(float), int(epsg)


def validate_catchment_not_degenerate(cell_count: int, area_km2: float,
                                      pour_point: tuple[float, float]) -> None:
    """Refuse a delineated catchment that is a degenerate sliver, typed and LOUD.

    A pour point that does not land on the catchment's channel, or an AOI that
    does not contain the upstream basin, yields a handful of D8 cells rather than
    a real catchment. Meshing and solving that sliver is a silent dead-end, so it
    fails here naming both corrective moves - never a quiet 0.018 km^2 answer.
    """
    if int(cell_count) < _MIN_CATCHMENT_CELLS:
        raise MeshGenerationError(
            "MESH_CATCHMENT_DEGENERATE",
            f"the delineated catchment is degenerate: only {int(cell_count)} D8 "
            f"cells (~{float(area_km2):.3f} km^2) upstream of pour point "
            f"{tuple(round(float(v), 5) for v in pour_point)}. The pour point "
            "likely does not sit on the catchment channel, or the analysis AOI "
            "does not contain the upstream basin. Move the pour point onto the "
            "stream, or supply a bbox that covers the whole catchment.")


def sample_raster_at_nodes(raster_path: Any, points_lonlat: Any) -> Any:
    """Sample a raster at (N,2) lon/lat nodes -> (N,) values, holes filled.

    Nodata becomes the finite mean rather than NaN: a bed with holes in it is not
    a bed a solver can start from, and a hole at one node would propagate a NaN
    through the whole free surface.
    """
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
    if np.isnan(vals).any():
        finite = vals[np.isfinite(vals)]
        vals[np.isnan(vals)] = float(finite.mean()) if finite.size else 0.0
    return vals


def node_slopes_from_mesh(points_utm: Any, cells: Any, bed_elev: Any) -> Any:
    """Per-node terrain slope (m/m) from the mesh's OWN piecewise-linear bed.

    The bed is linear over each triangle, so its gradient is exact per element;
    a node's slope is the mean over the elements that touch it. Read off the mesh
    rather than re-sampled from a raster because the mesh IS the discretization
    the solver sees - a slope taken at a finer scale would correct a curve number
    for terrain the run does not resolve.
    """
    import numpy as np

    pts = np.asarray(points_utm, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    z = np.asarray(bed_elev, dtype=float)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    x1, y1 = pts[a, 0], pts[a, 1]
    x2, y2 = pts[b, 0], pts[b, 1]
    x3, y3 = pts[c, 0], pts[c, 1]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    # A zero-area triangle carries no gradient; it contributes nothing rather
    # than an infinity that would poison every node it touches.
    safe = np.where(np.abs(det) > 0.0, det, np.nan)
    dzdx = ((y2 - y3) * (z[a] - z[c]) + (y3 - y1) * (z[b] - z[c])) / safe
    dzdy = ((x3 - x2) * (z[a] - z[c]) + (x1 - x3) * (z[b] - z[c])) / safe
    grad = np.sqrt(dzdx ** 2 + dzdy ** 2)
    total = np.zeros(pts.shape[0], dtype=float)
    count = np.zeros(pts.shape[0], dtype=float)
    finite = np.isfinite(grad)
    for column in (a, b, c):
        np.add.at(total, column[finite], grad[finite])
        np.add.at(count, column[finite], 1.0)
    return np.where(count > 0.0, total / np.maximum(count, 1.0), 0.0)


def polygon_area_km2(geom: Any) -> float:
    """Area (km^2) of a lon/lat polygon via a local equal-area cast."""
    import geopandas as gpd

    return float(gpd.GeoSeries([geom], crs=4326).to_crs(6933).area.iloc[0] / 1e6)


def read_2dm_mesh(twodm_path: str) -> tuple[Any, Any, Any]:
    """Parse an SMS ``.2dm`` -> ``(points (N,2), cells (M,3) 0-based, z (N,))``.

    The inverse of the display face's ``.2dm`` writer: ``ND id x y z`` node rows and
    ``E3T id n1 n2 n3 mat`` triangle rows, both 1-based. Nodes come back in id
    order; coordinates are the mesh's native metres (the artifact's ``utm_epsg``
    names the CRS).
    """
    import numpy as np

    nodes: dict[int, tuple[float, float, float]] = {}
    tris: list[tuple[int, int, int]] = []
    for line in Path(twodm_path).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "ND" and len(parts) >= 5:
            nodes[int(parts[1])] = (float(parts[2]), float(parts[3]), float(parts[4]))
        elif parts[0] in ("E3T", "E3L") and len(parts) >= 5:
            tris.append((int(parts[2]), int(parts[3]), int(parts[4])))
    if not nodes or not tris:
        raise MeshGenerationError(
            "MESH_SUPPLIED_UNREADABLE",
            f"2dm mesh {twodm_path} parsed to {len(nodes)} nodes / {len(tris)} "
            "elements; expected a MESH2D ND/E3T body.")
    order = sorted(nodes)
    remap = {nid: i for i, nid in enumerate(order)}
    points = np.array([[nodes[n][0], nodes[n][1]] for n in order], dtype=float)
    z = np.array([nodes[n][2] for n in order], dtype=float)
    cells = np.array([[remap[a], remap[b], remap[c]] for a, b, c in tris],
                     dtype=np.int64)
    return points, cells, z


# --------------------------------------------------------------------------- #
# The world-reads, as DATA producers.
# --------------------------------------------------------------------------- #
def _domain_bbox(what: str, override: Any = None) -> tuple[float, float, float, float]:
    """The extent a producer reads the world over.

    Declared uses leave ``override`` unset and read the bound DOMAIN, which is
    what makes ``Data`` producers domain-implicit. The STANDALONE mesh tool has no
    plan and therefore no domain, so it names its extent - the one caller allowed
    to, and the reason the parameter exists at all.
    """
    if override is not None:
        return tuple(float(v) for v in override)  # type: ignore[return-value]
    from trid3nt_server.workflows.lib import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise MeshGenerationError(
            "MESH_DOMAIN_UNBOUND",
            f"{what} cannot be fetched: no domain is bound. Resolve the AOI first.")
    return tuple(float(v) for v in domain.bbox)  # type: ignore[return-value]


def resolve_bed_dem(*, resolution_m: int = DEFAULT_BED_RESOLUTION_M,
                    bbox: Any = None,
                    fallback: tuple[str, ...] = ()) -> dict[str, Any]:
    """The BARE-EARTH bed raster for the mesh nodes, with its ladder declared.

    Pins the bed to USGS 3DEP bare earth: a DSM (Copernicus GLO-30 includes
    forest CANOPY) inflates node elevations under tree cover, and a catchment
    meshed on canopy tops routes water down the wrong slopes. Where 3DEP has no
    coverage the fall back to Copernicus is a CROSS-DATASET substitution, so it is
    LOUD by construction: the note rides the returned artifact, which every
    consumer reads, rather than an out-parameter a caller could decline to pass.
    That is what makes the label unbypassable.

    ``fallback`` is the declared ladder the producer was given; it is echoed into
    the note so the run's own record names the rungs that were available to it.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    bbox = _domain_bbox("the mesh bed DEM", bbox)
    rungs = " -> ".join(fallback) if fallback else "usgs_3dep -> copernicus_glo30"
    try:
        layer = TOOL_REGISTRY["fetch_dem"].fn(
            bbox=bbox, source="3dep", resolution_m=int(resolution_m),
            purpose="mesh bed")
        return {
            "uri": layer.uri if hasattr(layer, "uri") else layer["uri"],
            "source": "usgs_3dep_bare_earth",
            "resolution_m": int(resolution_m),
            "cross_dataset": False,
            "note": (f"mesh bed DEM: USGS 3DEP bare-earth ({int(resolution_m)} m); "
                     f"ladder {rungs}"),
        }
    except Exception as exc:  # noqa: BLE001 - a LOUD cross-dataset fallback
        # WHY the rung fired is the fact a reader needs and the one that used to
        # be thrown away: the note said only that 3DEP "was unavailable", so a
        # transient timeout and a genuine coverage hole read identically, and the
        # only record of the difference was a log line nobody keeps.
        reason = f"{type(exc).__name__}: {exc}"
        code = getattr(exc, "error_code", None)
        if code:
            reason = f"{code} ({reason})"
        logger.warning(
            "mesh bed: USGS 3DEP bare-earth unavailable for bbox=%s (%s); falling "
            "back to Copernicus GLO-30, a DSM that INCLUDES forest canopy",
            bbox, reason)
        layer = TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=bbox, purpose="mesh bed")
        return {
            "uri": layer.uri if hasattr(layer, "uri") else layer["uri"],
            "source": "copernicus_glo30",
            "resolution_m": 30,
            "cross_dataset": True,
            "fallback_reason": reason,
            "note": (f"mesh bed DEM CROSS-DATASET FALLBACK. 3DEP FAILED: {reason} "
                     "-> Copernicus GLO-30. That is a SURFACE model "
                     "(canopy-inclusive), so bed elevations under forest may be "
                     f"biased high. Ladder {rungs}."),
        }


def resolve_landcover(*, dataset: str, resolution_m: int,
                      bbox: Any = None) -> dict[str, Any]:
    """The land-cover raster the per-node roughness and curve numbers come from."""
    from trid3nt_server.tools import TOOL_REGISTRY

    bbox = _domain_bbox("the land-cover raster", bbox)
    layer = TOOL_REGISTRY["fetch_landcover"].fn(
        bbox=list(bbox), dataset=str(dataset), resolution_m=int(resolution_m),
        purpose="land cover")
    uri = layer["uri"] if isinstance(layer, dict) else getattr(layer, "uri")
    return {"uri": str(uri), "dataset": str(dataset),
            "resolution_m": int(resolution_m),
            "note": f"per-node curve numbers and Manning n from {dataset} "
                    f"land cover at {int(resolution_m)} m"}


def resolve_river_network(*, source: str = DEFAULT_RIVER_SOURCE,
                          bbox: Any = None) -> dict[str, Any]:
    """The river network inside the AOI - the mesh's distance-to-river sizing source.

    BEST-EFFORT by contract: a catchment with no mapped flowline is meshed at
    uniform sizing and says so, because refusing there would refuse a headwater
    basin for having no NHD reach in it.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    bbox = _domain_bbox("the river network", bbox)
    try:
        layer = TOOL_REGISTRY["fetch_river_geometry"].fn(
            bbox=bbox, source=str(source), purpose="river geometry")
    except Exception as exc:  # noqa: BLE001 - river refinement is best-effort
        logger.warning("catchment mesh: flowline fetch failed (%s); uniform sizing", exc)
        return {"uri": None, "source": str(source),
                "note": (f"no {source} flowlines were available for this AOI, so the "
                         "mesh was sized UNIFORMLY rather than refined toward the "
                         "channel network")}
    return {"uri": str(layer.uri), "source": str(source),
            "note": f"mesh refined by distance to the {source} channel network"}


# --------------------------------------------------------------------------- #
# Route 1: generate.
# --------------------------------------------------------------------------- #
def delineate_catchment(rundir: Path, bbox: Any, pour_point: tuple[float, float],
                        dem_uri: str | None = None, *,
                        snap_search_cells: int = DEFAULT_OUTLET_SNAP_CELLS,
                        ) -> tuple[Any, tuple[float, float], float, int]:
    """The catchment upstream of ``pour_point`` -> (polygon, outlet, area_km2, cells).

    Delegates the outlet snap and the catchment trace to the SHARED,
    alignment-invariant ``snap_and_delineate_index_space``: the outlet is snapped
    to the MAX-accumulation cell in a small window (which guarantees the main
    channel) and the basin is traced in INDEX space, avoiding the coordinate-space
    fragility that collapsed it to a 1-cell sliver on certain grid alignments
    (measured: 33.7-34.0k cells across box quantizations for the Coweeta outlet
    against 1-14 for the coordinate path).

    The DEM is the shared hydrology stack's own unless a caller names one: D8
    routing needs a natively GEOGRAPHIC grid, so Copernicus GLO-30 is a constraint
    of the METHOD rather than a source anyone chose, and it is resolved once
    inside that stack. A caller that already staged a geographic DEM passes it
    rather than fetching the same window twice.
    """
    from trid3nt_server.tools.processing._hydrology_common import (
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
                grid, fdir, acc, float(pour_point[0]), float(pour_point[1]),
                snap_search_cells=int(snap_search_cells)))
    except HydrologyInputError as exc:
        raise MeshGenerationError(
            "MESH_POUR_POINT_OFF_DEM",
            f"pour point {tuple(pour_point)} falls outside the DEM window "
            f"{q_bbox}; supply a bbox or pour point inside the analysis AOI."
        ) from exc
    area = polygon_area_km2(catch_geom) if catch_geom is not None else 0.0
    return catch_geom, (float(x_snap), float(y_snap)), float(area), cell_count


def _run_mesh_container(rundir: Path, mesh_config: dict[str, Any], *, image: str,
                        sandbox: Path) -> tuple[Any, Any, dict[str, Any]]:
    """Run the OceanMesh2D in-container mesher; return ``(points, cells, stats)``.

    Bind-mounts the sandbox (for the lifted in-container script) and the rundir at
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
    logger.info("catchment mesh: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_MESH_CONTAINER_TIMEOUT_S)
    npz_path = rundir / "coastal_tin_mesh.npz"
    if cp.returncode != 0 or not npz_path.exists():
        raise MeshGenerationError(
            "MESH_BUILD_FAILED",
            f"the catchment mesher failed (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    npz = np.load(npz_path)
    stats = json.loads((rundir / "mesh_stats.json").read_text())
    return npz["points"], npz["cells"], stats


def _stage_local(uri: str, dst: Path) -> Path:
    from trid3nt_server.tools.cache import read_object_bytes_s3

    dst.write_bytes(read_object_bytes_s3(uri) if str(uri).startswith("s3://")
                    else Path(uri).read_bytes())
    return dst


def generate_catchment_mesh(
    *,
    pour_point: tuple[float, float],
    bbox: tuple[float, float, float, float],
    slug: str,
    output_dir: str,
    bed_dem: dict[str, Any],
    rivers: dict[str, Any] | None,
    min_edge_length_m: float = DEFAULT_MIN_EDGE_M,
    max_edge_length_m: float = DEFAULT_MAX_EDGE_M,
    grade: float = DEFAULT_GRADE,
    max_iter: int = DEFAULT_MAX_ITER,
    snap_search_cells: int = DEFAULT_OUTLET_SNAP_CELLS,
    mesh_image: str | None = None,
    sandbox_dir: str | None = None,
) -> CatchmentMesh:
    """Delineate and triangulate the catchment at ``pour_point``.

    The GENERATED route: delineate -> exterior + river sizing points ->
    OceanMesh2D -> project to metres -> sample the bed. The declared artifacts
    (``bed_dem``, ``rivers``) arrive already fetched, because a step does not
    fetch; what happens here is meshing, and only meshing.
    """
    import json as _json

    import geopandas as gpd
    import numpy as np
    from shapely.geometry import mapping

    rundir = Path(output_dir)
    rundir.mkdir(parents=True, exist_ok=True)
    image = mesh_image or os.environ.get("TRID3NT_MESH_IMAGE") or DEFAULT_MESH_IMAGE
    sandbox = Path(sandbox_dir or os.environ.get("TRID3NT_OCEANMESH_SANDBOX")
                   or "scripts/sandbox/oceanmesh").resolve()
    notes: list[str] = []

    catch, outlet, area_km2, cell_count = delineate_catchment(
        rundir, bbox, tuple(pour_point), snap_search_cells=snap_search_cells)
    validate_catchment_not_degenerate(cell_count, area_km2, tuple(pour_point))
    catch_path = rundir / "catchment.geojson"
    catch_path.write_text(_json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(catch)}]}))

    flow = None
    if rivers and rivers.get("uri"):
        fl_path = _stage_local(str(rivers["uri"]), rundir / "flowlines.fgb")
        flow = gpd.read_file(fl_path)
    if rivers and rivers.get("note"):
        notes.append(str(rivers["note"]))

    boubox, river = catchment_exterior_and_river_coords(
        catch, flow, min_edge_length_m=min_edge_length_m)
    cfg = build_mesh_config(boubox, river, min_edge_length_m=min_edge_length_m,
                            max_edge_length_m=max_edge_length_m, grade=grade,
                            max_iter=max_iter)
    points_ll, cells, stats = _run_mesh_container(
        rundir, cfg, image=image, sandbox=sandbox)
    points_ll = np.asarray(points_ll, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)

    bed_path = _stage_local(str(bed_dem["uri"]), rundir / "dem_bed.tif")
    bed = sample_raster_at_nodes(bed_path, points_ll)
    notes.append(str(bed_dem.get("note") or ""))

    points_m, epsg = reproject_nodes_to_utm(points_ll)
    area_km2 = float(area_km2) or polygon_area_km2(catch)
    logger.info("catchment mesh built: %d nodes %d cells %.2f km^2 epsg=%d outlet=%s",
                points_m.shape[0], cells.shape[0], area_km2, epsg, outlet)
    return CatchmentMesh(
        slug=slug, points_utm=points_m, cells=cells, bed_elev=bed,
        points_lonlat=points_ll, utm_epsg=epsg, area_km2=area_km2,
        pour_point_lonlat=tuple(pour_point), outlet_lonlat=outlet,
        catchment_geojson=str(catch_path), provenance="generated",
        notes=[n for n in notes if n], stats=stats)


# --------------------------------------------------------------------------- #
# Route 2: adopt what the user authored.
# --------------------------------------------------------------------------- #
def adopt_supplied_mesh_2dm(*, twodm_path: str, slug: str, utm_epsg: int,
                            pour_point: tuple[float, float],
                            outlet_lonlat: tuple[float, float] | None = None,
                            area_km2: float = 0.0, source_path: str = "",
                            note: str | None = None) -> CatchmentMesh:
    """Adopt an authored ``.2dm`` mesh END TO END, nodes and bed included.

    The UTM nodes are projected back to lon/lat so every downstream node sampler
    behaves exactly as it does on the generated route - a supplied mesh must not
    take a different code path to its curve numbers than a built one.
    """
    import numpy as np
    from pyproj import Transformer

    points_m, cells, bed = read_2dm_mesh(twodm_path)
    tr = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = tr.transform(points_m[:, 0], points_m[:, 1])
    return CatchmentMesh(
        slug=slug, points_utm=points_m, cells=cells, bed_elev=bed,
        points_lonlat=np.column_stack([lon, lat]).astype(float),
        utm_epsg=int(utm_epsg), area_km2=float(area_km2),
        pour_point_lonlat=tuple(pour_point),
        outlet_lonlat=tuple(outlet_lonlat or pour_point),
        provenance="supplied", source_path=str(source_path),
        notes=[note or "solved on a mesh supplied for this invocation"])


def adopt_supplied_mesh(*, mesh_path: str, slug: str, utm_epsg: int,
                        pour_point: tuple[float, float],
                        outlet_lonlat: tuple[float, float] | None = None,
                        note: str | None = None) -> CatchmentMesh:
    """Adopt an authored mesh FILE whose nodes the solver reads for itself.

    A ``.slf`` handed in already carries its geometry and bed, so the node arrays
    are left empty and the consumer stages the file verbatim. A supplied mesh
    whose interior the template must sample takes :func:`adopt_supplied_mesh_2dm`
    instead.
    """
    p = Path(mesh_path)
    if not p.exists() or p.stat().st_size == 0:
        raise MeshGenerationError(
            "MESH_SUPPLIED_MISSING", f"supplied mesh not found or empty: {mesh_path}")
    if p.suffix.lower() not in (".slf", ".sel", ".2dm"):
        raise MeshGenerationError(
            "MESH_SUPPLIED_UNSUPPORTED",
            f"supplied mesh must be SELAFIN (.slf/.sel) or 2dm; got {p.suffix}")
    return CatchmentMesh(
        slug=slug, points_utm=None, cells=None, bed_elev=None, points_lonlat=None,
        utm_epsg=int(utm_epsg), area_km2=0.0,
        pour_point_lonlat=tuple(pour_point),
        outlet_lonlat=tuple(outlet_lonlat or pour_point),
        provenance="supplied", source_path=str(p),
        notes=[note or f"solved on the supplied mesh {p.name}"])


