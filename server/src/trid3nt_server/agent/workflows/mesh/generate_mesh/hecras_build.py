"""``generate_mesh`` mode=hecras -- build a channel-refined HEC-RAS RoG mesh.

The refined-mesh machinery of (graded seeds + breaklines, previously reachable
ONLY as the ``channel_refinement`` knob embedded in a ``hecras_flood_2d`` rain-on-grid
run) is here made a STANDALONE user act: this builds a channel-refined HEC-RAS cell mesh
into the case, a human inspects the wireframe in QGIS, and a later ``hecras_flood_2d`` RoG
run CONSUMES it through the precondition gate (no fresh delineation / re-seeding).

The portable artifact = the AUTHORING INPUTS (graded seeds + channel breaklines + the
local terrain frame + the modeled catchment/channel), NOT a realized mesh file: the 2025
engine's ``TryCreateMesh`` is deterministic on identical seeds, so consumption re-realizes
exactly the inspected cell mesh. The realized cell polygons are persisted only as the
DISPLAY face (the wireframe NATE approves is the one that solves). ASCII only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.mesh.generate_mesh.hecras_build")

#: the worker freshtopo tree carrying rog_refine + rog2025_pipeline + hecras_mesh.
_WORKERS_FRESHTOPO = (
    Path(__file__).resolve().parents[7]
    / "services/workers/hecras2025/subst/crux/freshtopo"
)


def build_and_record_hecras_mesh(
    *, location: str | None, bbox: tuple, pour_point: tuple | None,
    channel_m: float, background_m: float, case_id: str | None,
) -> LayerURI:
    """Acquire -> build+validate (meshprobe) -> stage bundle -> emit display + artifact.

    Runs fully in a worker thread (docker meshprobe + fetchers block). ``channel_m`` /
    ``background_m`` are the fine-channel + coarse-hillslope target cell sizes (from the
    ``min_edge_length_m`` / ``max_edge_length_m`` granularity levers)."""
    from trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d import (
        acquire_channel_inputs, _fetch_dem_local,
    )
    from trid3nt_server.agent.workflows.mesh.generate_mesh.generate_mesh import (
        GenerateMeshError,
    )
    from trid3nt_server.agent.workflows.mesh.artifact import (
        MeshArtifact, stash_mesh_artifact, write_mesh_artifact_sidecar,
    )
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    mesh_id = new_ulid()
    workdir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"hecmesh-{mesh_id}"
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. delineated catchment + channel network (the modeled domain + refine target).
    #    the provided pour point is the outlet (matches the ADR Coweeta site); absent
    #    it, acquire_channel_inputs derives the lowest-cell outlet.
    catchment_geojson, flowlines_path = acquire_channel_inputs(
        list(bbox), str(workdir), pour_point=pour_point)
    if catchment_geojson is None or flowlines_path is None:
        raise GenerateMeshError(
            "GENERATE_MESH_HECRAS_NO_CHANNEL",
            f"could not delineate a catchment + channel network for AOI {list(bbox)} "
            "(a HEC-RAS RoG mesh grades toward the channel; is this an inland flat box "
            "with no mapped drainage?)")

    # 2. bare-earth DEM for the terrain frame.
    dem_tif = _fetch_dem_local(list(bbox))

    # 3. build + validate the graded cell mesh (host seeds/breaklines + meshprobe).
    if str(_WORKERS_FRESHTOPO) not in sys.path:
        sys.path.insert(0, str(_WORKERS_FRESHTOPO))
        sys.path.insert(0, str(_WORKERS_FRESHTOPO.parents[2]))
    from hecras_mesh import build_hecras_mesh  # type: ignore
    from rog2025_pipeline import Rog2025Error  # type: ignore

    pp = tuple(float(v) for v in pour_point) if pour_point is not None else None
    try:
        built = build_hecras_mesh(
            dem_tif, str(workdir), bbox4326=list(bbox), pour_point=pp,
            catchment_geojson=catchment_geojson, flowlines_path=flowlines_path,
            background_m=float(background_m), channel_m=float(channel_m))
    except Rog2025Error as exc:
        # HEC's <= 8-sides-per-cell acceptance is fragile on a large seed cloud;
        # an unrealizable cloud is a HONEST typed error, not a raw crash.
        raise GenerateMeshError(
            "GENERATE_MESH_HECRAS_MESH_UNREALIZED",
            f"the channel-refined HEC-RAS mesh did not realize a valid cell mesh for "
            f"this AOI (HEC rejects > 8-sided cells): {exc}. Try a tighter AOI (a "
            "catchment-scale window) or a coarser channel size (min_edge_length_m).")

    # 4. stage the portable bundle + display into the case cache bucket (no parallel
    # store; the sidecar rides beside the display face like every other mesh).
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise HecrasFlood2dError(
            "GENERATE_MESH_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the mesh into the case.")
    s3 = _get_s3_client()
    prefix = f"mesh/{mesh_id}"

    def _put(local: str, base: str) -> str:
        key = f"{prefix}/{base}"
        s3.put_object(Bucket=cache_bucket, Key=key, Body=Path(local).read_bytes())
        return f"s3://{cache_bucket}/{key}"

    # the realized channel cell size feeds the consume-path CFL step.
    prep_doc = dict(built.prep)
    prep_doc["channel_m_realized"] = built.size_p5
    prep_json_local = Path(built.prep_json_path)
    prep_json_local.write_text(json.dumps(prep_doc, indent=2))

    display_uri = _put(built.display_fgb_path, "cells_lonlat.fgb")
    hecras_inputs = {
        "seeds": _put(built.seeds_path, "seeds.f64"),
        "breaklines": _put(built.breaklines_path, "breaklines.json"),
        "local_dem": _put(built.local_dem_path, "local_dem.tif"),
        "prep_json": _put(str(prep_json_local), "prep.json"),
        "catchment": _put(catchment_geojson, "catchment.geojson"),
        "flowlines": _put(flowlines_path, "flowlines.fgb"),
    }

    name = location or f"HEC-RAS RoG mesh ({built.cell_count} cells)"
    provenance = {
        "channel_target_m": float(channel_m), "background_m": float(background_m),
        "realized_channel_p5_m": built.size_p5, "realized_p50_m": built.size_p50,
        "realized_p95_m": built.size_p95, "cell_count": built.cell_count,
        "face_count": built.face_count, "n_seeds": built.n_seeds,
        "channel_len_km": built.channel_len_km, "breakline_len_km": built.breakline_len_km,
        "size_hist_edges": built.size_hist_edges, "size_hist_counts": built.size_hist_counts,
        "attempt0_clean": built.attempt0_clean, "badcells_attempt0": built.badcells_attempt0,
        "sizing_source": ("graded Poisson-disk seeds (rog_refine) + main-stem channel "
                          "breaklines; realized + validated via the in-container meshprobe"),
        "dem_source": "USGS 3DEP bare-earth (bed) + Copernicus GLO-30 (delineation)",
        "reproducibility": ("TryCreateMesh is deterministic on identical seeds -- the "
                            "consume path re-realizes exactly this cell mesh"),
    }
    art = MeshArtifact(
        mesh_id=mesh_id, name=str(name), mode="hecras_rog", display_uri=display_uri,
        slf_uri=None, utm_epsg=built.utm_epsg, crs_authid=f"EPSG:{built.utm_epsg}",
        has_bathymetry=True, node_count=built.face_count, element_count=built.cell_count,
        bbox=tuple(built.lonlat_bbox), engine_compat=["hecras"],
        outlet_lonlat=None, pour_point_lonlat=(pp if pp else None),
        provenance=provenance, case_id=case_id, hecras_inputs=hecras_inputs,
        channel_target_size_m=float(channel_m), background_size_m=float(background_m),
        cells_validated=bool(built.validated))
    stash_mesh_artifact(case_id, art)
    sidecar = write_mesh_artifact_sidecar(art, s3)
    logger.info(
        "generate_mesh hecras: mesh %s -> %d cells %d faces channel~%.1fm/%.0fm bg "
        "(attempt0_clean=%s) sidecar=%s", mesh_id, built.cell_count, built.face_count,
        built.size_p5, background_m, built.attempt0_clean, sidecar)

    synthetic = [
        SyntheticInput(param="mesh_mode", value="hecras_rog", basis="user",
                       note="channel-refined HEC-RAS rain-on-grid cell mesh"),
        SyntheticInput(param="channel_target_size_m", value=float(channel_m), units="m",
                       basis="user", note="fine cell size along the delineated channel"),
        SyntheticInput(param="background_size_m", value=float(background_m), units="m",
                       basis="user", note="coarse hillslope cell size"),
        SyntheticInput(param="realized_cells",
                       value=f"{built.cell_count} cells / {built.face_count} faces",
                       basis="derived",
                       real_source_if_any="MeshFactory.TryCreateMesh (meshprobe-validated)",
                       note=f"channel p5 {built.size_p5} m, hillslope p50 {built.size_p50} m; "
                            f"<= 8 sides/cell passed (badcells={built.badcells_attempt0})"),
    ]
    return LayerURI(
        layer_id=f"mesh-{mesh_id}", name=f"Mesh: {name}", layer_type="vector",
        uri=display_uri, style_preset="mesh_wireframe", role="primary",
        bbox=tuple(built.lonlat_bbox), crs_authid=f"EPSG:{built.utm_epsg}",
        synthetic_inputs=synthetic)
