"""The HEC-RAS GRADED-SEED mesher: a coarse hillslope grading down to the channel.

A rain-on-grid domain is a whole terrain box whose cells shrink toward the
delineated channel network. What this mesher builds is therefore a graded
Poisson-disk seed cloud plus channel breaklines over a local-SI terrain frame,
realized and validated through the engine's own meshprobe.

Its product is an AUTHORING BUNDLE rather than a connectivity file: the 2025
managed engine's mesh factory is deterministic on identical seeds, so a solve
re-realizes exactly the cell mesh that was inspected. The realized cell polygons
are kept only as the DISPLAY face - the wireframe a human approves is the one
that solves - which is why this mesh carries no cells of its own.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.mesh.meshers import (
    EditAction,
    Mesh,
    MeshField,
    MeshToolError,
    register_mesher,
)

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.hecras")

__all__ = ["HECRAS_ROG", "build"]

#: The worker tree carrying the seed refiner, the terrain frame and the meshprobe.
_FRESHTOPO = (Path(__file__).resolve().parents[4]
              / "workers/hecras2025/subst/crux/freshtopo")

_FIELDS = (
    MeshField("kind", types=(str,), choices=("graded_cells",),
              default="graded_cells",
              doc="graded_cells - a cell mesh the engine realizes from seeds"),
    MeshField("extent", types=(tuple, list), required=True,
              doc="(min_lon, min_lat, max_lon, max_lat) the terrain frame is cut from"),
    MeshField("pour_point", types=(tuple, list),
              doc="(lon, lat) catchment outlet; the lowest cell when unstated"),
    MeshField("min_edge_length_m", types=(int, float), default=22.0,
              doc="the fine CHANNEL cell size, in metres"),
    MeshField("max_edge_length_m", types=(int, float), default=90.0,
              doc="the coarse HILLSLOPE background cell size, in metres"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Acquire the channel network, grade the seeds, and realize the cell mesh."""
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
        _fetch_dem_local,
        acquire_channel_inputs,
    )

    aoi = tuple(float(v) for v in spec["extent"])
    pour = spec.get("pour_point")
    pour_point = tuple(float(v) for v in pour) if pour is not None else None
    channel_m = float(spec.get("min_edge_length_m", 22.0))
    background_m = float(spec.get("max_edge_length_m", 90.0))
    workdir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
               / f"hecmesh-{new_ulid()}")
    workdir.mkdir(parents=True, exist_ok=True)

    # The delineated catchment is the modeled domain and the channel network is
    # what the cells grade toward; the provided pour point is the outlet, and
    # without one the acquisition derives the lowest-cell outlet.
    catchment_geojson, flowlines_path = acquire_channel_inputs(
        list(aoi), str(workdir), pour_point=pour_point)
    if catchment_geojson is None or flowlines_path is None:
        raise MeshToolError(
            "MESH_HECRAS_NO_CHANNEL",
            f"no catchment and channel network could be delineated for AOI "
            f"{list(aoi)}; a graded cell mesh grades toward the channel, so is "
            "this an inland flat box with no mapped drainage?")

    # The terrain uri is the only record of WHICH terrain the mesh was cut from,
    # so it rides into the provenance rather than a dataset name nothing resolved.
    dem_tif, dem_uri = _fetch_dem_local(list(aoi))
    built = _realize(dem_tif, workdir, aoi, pour_point, catchment_geojson,
                     flowlines_path, background_m, channel_m)

    # The realized channel cell size feeds the consume path's CFL step, so it is
    # written into the frame the engine re-realizes from.
    prep_doc = dict(built.prep)
    prep_doc["channel_m_realized"] = built.size_p5
    Path(built.prep_json_path).write_text(json.dumps(prep_doc, indent=2))

    return Mesh(
        points=None, cells=None, crs_authid=f"EPSG:{int(built.utm_epsg)}", bed=None,
        meta={
            "extent": aoi,
            "utm_epsg": int(built.utm_epsg),
            "lonlat_bbox": tuple(built.lonlat_bbox),
            "files": {"display_uri": built.display_fgb_path},
            "bundle": {
                "seeds": built.seeds_path,
                "breaklines": built.breaklines_path,
                "local_dem": built.local_dem_path,
                "prep_json": built.prep_json_path,
                "catchment": catchment_geojson,
                "flowlines": flowlines_path,
            },
            "probes": {
                "cell_count": int(built.cell_count),
                "face_count": int(built.face_count),
                "seed_count": int(built.n_seeds),
                "cell_size_m": {"p5": built.size_p5, "p50": built.size_p50,
                                "p95": built.size_p95},
                "size_histogram": {"bin_edges": built.size_hist_edges,
                                   "counts": built.size_hist_counts},
                "channel_len_km": built.channel_len_km,
                "breakline_len_km": built.breakline_len_km,
                "badcells_first_attempt": int(built.badcells_attempt0),
            },
            "artifact": {
                "node_count": int(built.face_count),
                "element_count": int(built.cell_count),
                "engine_compat": ["hecras"],
                "cells_validated": bool(built.validated),
                "channel_target_size_m": channel_m,
                "background_size_m": background_m,
                "pour_point_lonlat": pour_point,
                "provenance": {
                    "channel_target_m": channel_m,
                    "background_m": background_m,
                    "realized_channel_p5_m": built.size_p5,
                    "realized_p50_m": built.size_p50,
                    "realized_p95_m": built.size_p95,
                    "cell_count": int(built.cell_count),
                    "face_count": int(built.face_count),
                    "n_seeds": int(built.n_seeds),
                    "channel_len_km": built.channel_len_km,
                    "breakline_len_km": built.breakline_len_km,
                    "size_hist_edges": built.size_hist_edges,
                    "size_hist_counts": built.size_hist_counts,
                    "attempt0_clean": bool(built.attempt0_clean),
                    "badcells_attempt0": int(built.badcells_attempt0),
                    "sizing_source": (
                        "graded Poisson-disk seeds + main-stem channel breaklines; "
                        "realized and validated via the in-container meshprobe"),
                    "dem_source": (
                        f"bed: the terrain COG the fetch router served at 10 m "
                        f"({dem_uri}) - the router picks the rung, so no dataset "
                        f"is named here that this build did not resolve"),
                    "reproducibility": (
                        "the engine's mesh factory is deterministic on identical "
                        "seeds, so the consume path re-realizes exactly this mesh"),
                },
            },
            "synthetic_inputs": [
                {"param": "channel_target_size_m", "value": channel_m, "units": "m",
                 "basis": "user",
                 "note": "fine cell size along the delineated channel"},
                {"param": "background_size_m", "value": background_m, "units": "m",
                 "basis": "user", "note": "coarse hillslope cell size"},
                {"param": "realized_cells",
                 "value": f"{built.cell_count} cells / {built.face_count} faces",
                 "basis": "derived",
                 "real_source_if_any": "the engine mesh factory (meshprobe-validated)",
                 "note": f"channel p5 {built.size_p5} m, hillslope p50 "
                         f"{built.size_p50} m; <= 8 sides/cell passed "
                         f"(badcells={built.badcells_attempt0})"},
            ],
        })


def _realize(dem_tif: Any, workdir: Path, aoi: tuple[float, ...],
             pour_point: tuple[float, float] | None, catchment_geojson: str,
             flowlines_path: str, background_m: float, channel_m: float) -> Any:
    """Run the seed refiner + the in-container meshprobe -> the realized result."""
    if str(_FRESHTOPO) not in sys.path:
        sys.path.insert(0, str(_FRESHTOPO))
        sys.path.insert(0, str(_FRESHTOPO.parents[2]))
    from hecras_mesh import build_hecras_mesh  # type: ignore
    from rog2025_pipeline import Rog2025Error  # type: ignore

    try:
        return build_hecras_mesh(
            dem_tif, str(workdir), bbox4326=list(aoi), pour_point=pour_point,
            catchment_geojson=catchment_geojson, flowlines_path=flowlines_path,
            background_m=background_m, channel_m=channel_m)
    except Rog2025Error as exc:
        # The <= 8-sides-per-cell acceptance is fragile on a large seed cloud; an
        # unrealizable cloud is an honest typed refusal, not a raw crash.
        raise MeshToolError(
            "MESH_HECRAS_UNREALIZED",
            f"the graded cell mesh did not realize for this AOI (the engine "
            f"rejects cells with more than 8 sides): {exc}. Try a tighter AOI (a "
            "catchment-scale window) or a coarser channel cell size.")


def _set_cell_sizes(mesh: Mesh, *, min_edge_length_m: float,
                    max_edge_length_m: Any = None) -> Mesh:
    """Re-grade the seed cloud at different channel and hillslope cell sizes.

    The size field IS the seed cloud, so changing either target means re-seeding
    and re-realizing; there is no local edit of a mesh whose cells the engine has
    not yet built.
    """
    built = dict(mesh.meta["artifact"]["provenance"])
    return build({
        "extent": mesh.meta["extent"],
        "pour_point": mesh.meta["artifact"]["pour_point_lonlat"],
        "min_edge_length_m": float(min_edge_length_m),
        "max_edge_length_m": (float(max_edge_length_m)
                              if max_edge_length_m is not None
                              else built["background_m"])})


HECRAS_ROG = register_mesher(
    "hecras_rog",
    build,
    actions=(
        EditAction(
            name="set_cell_sizes", apply=_set_cell_sizes,
            inputs={
                "min_edge_length_m": MeshField(
                    "min_edge_length_m", types=(int, float), required=True,
                    doc="the new fine CHANNEL cell size, in metres"),
                "max_edge_length_m": MeshField(
                    "max_edge_length_m", types=(int, float),
                    doc="the new coarse HILLSLOPE cell size; unchanged when absent")},
            doc="Re-seed and re-realize the cell mesh at different target sizes."),
    ),
    fields=_FIELDS,
)
