"""The CATCHMENT mesher: the basin upstream of a pour point IS the domain.

Wraps the catchment strategy - delineate at the outlet, take the river network
inside the basin as the sizing source, triangulate the interior in the
GPL-isolated OceanMesh2D image, project to metres and sample a bed at the nodes.
The whole catchment is the domain, so an AOI never cookie-cuts the mesh
mid-hillslope; the AOI is only the window the delineation runs inside.

What this file adds over the strategy is the DECLARATION: which fields the ask
may carry, what its edits are, and the provenance the built mesh travels with -
copied from the resolvers that actually read the world, never composed here.
"""

from __future__ import annotations

import os
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
from trid3nt_server.workflows.mesh.watershed import (
    DEFAULT_MAX_ITER,
    DEFAULT_OUTLET_SNAP_CELLS,
)

__all__ = ["WATERSHED", "build"]

#: The grid the catchment is DELINEATED on. D8 routing needs a natively geographic
#: grid, so this is a constraint of the delineation method rather than a source the
#: run selected; it is stated apart from the bed, which is resolved per run.
DELINEATION_DEM = "Copernicus GLO-30 (D8 routing needs a geographic grid)"

_FIELDS = (
    MeshField("kind", types=(str,), choices=("unstructured_tri",),
              default="unstructured_tri",
              doc="unstructured_tri - a catchment interior is triangulated"),
    MeshField("extent", types=(tuple, list, dict), required=True,
              doc="(min_lon, min_lat, max_lon, max_lat) the delineation runs "
                  "inside, or the acquired catchment window that carries one "
                  "along with its outlet"),
    MeshField("pour_point", types=(tuple, list),
              doc="(lon, lat) catchment outlet; the window's own outlet, else the "
                  "AOI centre, when unstated"),
    MeshField("min_edge_length_m", types=(int, float), default=40.0,
              doc="finest triangle edge, in the channel band"),
    MeshField("max_edge_length_m", types=(int, float), default=300.0,
              doc="coarsest triangle edge, on the hillslopes"),
    MeshField("grade", types=(int, float), default=0.20,
              doc="gradation limit; smaller means smoother size transitions"),
    MeshField("max_iter", types=(int,), default=DEFAULT_MAX_ITER,
              doc="how many triangulation iterations the sizing is allowed"),
    MeshField("snap_search_cells", types=(int,),
              default=DEFAULT_OUTLET_SNAP_CELLS,
              doc="how far, in routing cells, the outlet may be snapped to find "
                  "the channel it drains"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Delineate the catchment at the pour point and triangulate its interior."""
    from trid3nt_server.workflows.mesh import watershed as strategy

    aoi, window_outlet = _window(spec["extent"])
    pour = spec.get("pour_point") or window_outlet
    pour_point = (tuple(float(v) for v in pour) if pour is not None
                  else ((aoi[0] + aoi[2]) / 2.0, (aoi[1] + aoi[3]) / 2.0))
    min_edge = float(spec.get("min_edge_length_m", 40.0))
    max_edge = float(spec.get("max_edge_length_m", 300.0))
    grade = float(spec.get("grade", 0.20))
    max_iter = int(spec.get("max_iter", DEFAULT_MAX_ITER))
    snap_search_cells = int(spec.get("snap_search_cells", DEFAULT_OUTLET_SNAP_CELLS))
    rundir = _rundir()

    # The resolved artifacts are HELD rather than passed inline: their notes are the
    # only record of which datasets actually landed, and the provenance this build
    # publishes must be COPIED from them, never asserted independently.
    bed_dem = strategy.resolve_bed_dem(bbox=aoi)
    rivers = strategy.resolve_river_network(bbox=aoi)
    catchment = strategy.generate_catchment_mesh(
        pour_point=pour_point, bbox=aoi, slug="watershed", output_dir=str(rundir),
        bed_dem=bed_dem, rivers=rivers,
        min_edge_length_m=min_edge, max_edge_length_m=max_edge, grade=grade,
        max_iter=max_iter, snap_search_cells=snap_search_cells)

    import numpy as np

    points = np.asarray(catchment.points_utm, dtype=float)
    cells = np.asarray(catchment.cells, dtype=np.int64)
    bed = np.asarray(catchment.bed_elev, dtype=float)
    lonlat = np.asarray(catchment.points_lonlat, dtype=float)
    bed_note = _bed_note(bed_dem, catchment)
    dem_source = _bed_provenance(bed_dem, bed_note)
    sizing_source = _sizing_source(rivers)
    fallback_note = bed_note if bed_dem.get("cross_dataset") else None
    return Mesh(
        points=points, cells=cells, crs_authid=f"EPSG:{int(catchment.utm_epsg)}",
        bed=bed,
        meta={
            "extent": aoi,
            "utm_epsg": int(catchment.utm_epsg),
            "lonlat_bbox": (float(lonlat[:, 0].min()), float(lonlat[:, 1].min()),
                            float(lonlat[:, 0].max()), float(lonlat[:, 1].max())),
            # An ADOPTED mesh arrives as a file staged verbatim; a generated
            # catchment carries none and the geometry is authored downstream.
            "files": ({"slf_uri": catchment.source_path}
                      if catchment.source_path else {}),
            "fallback_note": fallback_note,
            "artifact": {
                "outlet_lonlat": (tuple(catchment.outlet_lonlat)
                                  if catchment.outlet_lonlat else None),
                "pour_point_lonlat": pour_point,
                # An inland catchment is a single closed boundary.
                "open_boundary_info": {},
                "provenance": {
                    "min_edge_length_m": min_edge,
                    "max_edge_length_m": max_edge,
                    "grade": grade,
                    "max_iter": max_iter,
                    "snap_search_cells": snap_search_cells,
                    "sizing_source": sizing_source,
                    "dem_source": dem_source,
                    # A degraded bed must travel WITH the mesh: a solver reading
                    # this artifact months later reads its provenance, not the
                    # narration of the turn that built it.
                    "bed_fallback_note": fallback_note,
                    "area_km2": float(catchment.area_km2),
                },
            },
            "synthetic_inputs": _synthetic_inputs(
                min_edge, max_edge, grade, points.shape[0], cells.shape[0],
                sizing_source, dem_source),
        })


def _window(aoi: Any) -> tuple[tuple[float, float, float, float], Any]:
    """The delineation extent, and the outlet the window carries -> ``(bbox, outlet)``.

    An acquired catchment window is a record whose ``bbox`` bounds the search and
    whose ``pour_point`` is the outlet the user placed; a bare extent is only the
    first half, and the outlet then has to be stated separately.
    """
    if isinstance(aoi, Mapping):
        bbox = aoi.get("bbox")
        if bbox is None:
            raise MeshToolError(
                "MESH_WATERSHED_NO_EXTENT",
                f"the catchment window names no 'bbox' ({sorted(aoi)}), so there is "
                "no extent for the delineation to run inside.")
        return tuple(float(v) for v in bbox), aoi.get("pour_point")
    return tuple(float(v) for v in aoi), None


def _rundir() -> Path:
    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _synthetic_inputs(min_edge: float, max_edge: float, grade: float,
                      nodes: int, elements: int, sizing_source: str,
                      dem_source: str) -> list[dict[str, Any]]:
    return [
        {"param": "min_edge_length_m", "value": min_edge, "units": "m",
         "basis": "user"},
        {"param": "max_edge_length_m", "value": max_edge, "units": "m",
         "basis": "user"},
        {"param": "grade", "value": grade, "basis": "user"},
        {"param": "mesh_domain", "value": f"{nodes} nodes / {elements} elements",
         "basis": "derived", "real_source_if_any": sizing_source},
        {"param": "mesh_bed", "value": dem_source, "basis": "fetched",
         "consequence": "physics", "real_source_if_any": dem_source,
         "note": "the elevation every node carries; a solver reads it as the "
                 "domain's bathymetry"},
    ]


def _bed_note(bed_dem: Mapping[str, Any], catchment: Any) -> str:
    """The bed resolver's OWN label, from whichever sink actually carries it.

    The resolver labels its ladder on the artifact it returns, and the catchment
    strategy copies that label into the mesh's notes. Both are read because either
    may be the sink the value survives in; an empty result means the run genuinely
    holds no statement about the bed's source.
    """
    note = str((bed_dem or {}).get("note") or "").strip()
    if note:
        return note
    for candidate in getattr(catchment, "notes", None) or []:
        text = str(candidate).strip()
        if text and "bed" in text.lower():
            return text
    return ""


def _bed_provenance(bed_dem: Mapping[str, Any], bed_note: str) -> str:
    """What ACTUALLY painted the catchment bed, copied from the resolver's note.

    A cross-dataset substitution the run cannot state is a false promise, so it
    REFUSES here rather than name a source it does not know it got; a fully
    labeled build states the source and the label it arrived with.
    """
    source = str((bed_dem or {}).get("source") or "").strip()
    if not bed_note and bool((bed_dem or {}).get("cross_dataset")):
        raise MeshToolError(
            "MESH_UNSOURCED_DEM",
            "the bed DEM resolver reported a CROSS-DATASET fallback but carried no "
            "note naming the elevation source it landed on. This mesh therefore "
            "cannot state which elevation source its bed came from and refuses to "
            "claim one. Re-run once the bed resolver labels its substitution, or "
            "supply a bed DEM whose source is known.")
    if bed_note:
        bed = f"{source}: {bed_note}" if source else bed_note
    elif source:
        bed = f"{source} (the bed resolver reported no note)"
    else:
        bed = ("source UNMEASURED: the bed resolver reported neither a source nor "
               "a note")
    return f"bed: {bed}; delineation: {DELINEATION_DEM}"


def _sizing_source(rivers: Mapping[str, Any]) -> str:
    """What ACTUALLY sized the catchment mesh, copied from the resolver's note.

    River refinement is best-effort: a basin with no mapped flowline is meshed at
    UNIFORM sizing, so a fixed string here would promise refinement that never
    ran. The domain half is the delineation method itself and is always true.
    """
    domain = "pysheds catchment domain"
    note = str((rivers or {}).get("note") or "").strip()
    if note:
        return f"{domain}; {note}"
    source = str((rivers or {}).get("source") or "").strip()
    if source:
        return (f"{domain}; {source} was requested, but the river resolver reported "
                "no note, so the refinement that ran is UNMEASURED")
    return f"{domain}; river sizing UNREPORTED by the resolver"


def _set_edge_band(mesh: Mesh, *, min_edge_length_m: float,
                   max_edge_length_m: Any = None) -> Mesh:
    """Re-derive the catchment at a different edge band - a full rebuild.

    An edge band is a SIZING FUNCTION over the whole interior, so there is no
    local edit that honours it; re-running the triangulator is what changing it
    means, and the recipe replays exactly because the ask is the same value.
    """
    built = dict(mesh.meta["artifact"]["provenance"])
    return build({
        "extent": mesh.meta["extent"],
        "pour_point": mesh.meta["artifact"]["pour_point_lonlat"],
        "min_edge_length_m": float(min_edge_length_m),
        "max_edge_length_m": (float(max_edge_length_m)
                              if max_edge_length_m is not None
                              else built["max_edge_length_m"]),
        "grade": built["grade"],
        "max_iter": built["max_iter"],
        "snap_search_cells": built["snap_search_cells"]})


WATERSHED = register_mesher(
    "watershed",
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
            doc="Re-triangulate the catchment between a different edge band."),
        apply_layer_edits_action(),
    ),
    fields=_FIELDS,
    # MEASURED, not assumed: three rebuilds from one identical spec (the Coweeta
    # Creek outlet, 200/1000 m band, grade 0.20) returned one mesh - 363 nodes /
    # 657 elements, sha256 1236ce84 on all three - so a replay of a watershed
    # recipe reproduces the mesh rather than an equivalent of it.
    deterministic=True,
)
