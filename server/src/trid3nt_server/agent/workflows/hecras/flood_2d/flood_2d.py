"""Engine template ``hecras_flood_2d`` -- HEC-RAS 2D flood on a GENUINELY-NEW AOI.

The promotion of the ADR 0136-0139 authoring chain (ADR 0140): unlike
``hecras_riverine_flood`` (which reparameterizes HEC's FROZEN shipped Muncie
geometry), this template AUTHORS the 2D mesh + terrain-sampled subgrid tables for a
place the user names, then solves it with the production HEC-RAS 6.6 engines. The
backend is the proven ``flood2d_pipeline`` chain:

    fetch_dem (seam-1)  -> reproject to a local ftUS grid + mesh seeds
      -> the AUTHORING worker image (trid3nt-local/hecras2025-authoring:
         ras createterrain + AuthorMesh TryCreateMesh topology +
         MeshPropertyTables.ComputeFrom subgrid tables over the terrain)
      -> the deck composer (Mesh2D + tables -> a complete pure-2D deck, stamped
         with the AOI's CRS so the depth COG geolocates)
      -> run_solver (the generic seam; the composed deck rides as manifest
         ``inputs`` and the hecras worker's no-archetype M3-gate path solves it)
      -> postprocess_hecras -> peak-depth COG + 2D mesh preview + inflow chart.

FIDELITY (loud, NATE no-hand-wave): the SOLVE is the refinement-grade production
6.x solver; the GEOMETRY is authored by the HEC-RAS 2025 AuthorMesh path, validated
end-to-end (the transplant-path: subgrid tables 0.99988 corr / writer dWSE 0.0 /
topology bijection, ADR 0132/0139). It is SCREENING-grade until broader per-AOI
V&V. For a FAST screening flood use ``sfincs_flood``; for pluvial/precipitation
forcing on this engine, that is the OI-D residual (not yet wired). ASCII only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.hecras_contracts import (
    HECRAS_INPUT_INVALID,
    HECRAS_SOLVE_FAILED,
    HecrasDepthLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.workflows.hecras._template_card import TemplateCard

logger = logging.getLogger("trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d")

__all__ = [
    "hecras_flood_2d",
    "model_hecras_flood_2d",
    "HecrasFlood2dError",
    "TEMPLATE_CARD",
]

#: The authoring backend lives in the workers tree (proprietary natives image +
#: pure-python composer); imported at CALL time (not import time) so the server
#: package carries no hard dependency on services/workers.
_WORKERS_FRESHTOPO = (
    Path(__file__).resolve().parents[6]
    / "services/workers/hecras2025/subst/crux/freshtopo"
)

#: Default target peak inflow when the user names none (a modest bankfull-ish
#: event; the user pins a real discharge via ``target_peak_cfs``).
_DEFAULT_PEAK_CFS: float = 5000.0

#: Resolution band (m) -- coarser than SFINCS (the 2D subgrid solve is heavier).
_MIN_RES_M: float = 20.0
_MAX_RES_M: float = 200.0
_DEFAULT_RES_M: float = 60.0
#: Soft cell-count ceiling the resolution autoscaler respects (keeps a cheap
#: screening solve minutes-scale); the estimate + this cap are the granularity
#: suggestion surfaced for override (the user-controlled-granularity norm).
_SOFT_CELL_CAP: int = 12000

_FIDELITY_NOTE: str = (
    "REFINEMENT-GRADE production HEC-RAS 6.x solver on a 2025-AUTHORED 2D mesh "
    "(headless AuthorMesh topology + terrain-sampled subgrid tables), transplant-"
    "path validated end-to-end. This floods the AOI you named (NOT frozen "
    "demonstration geometry). SCREENING-grade until broader per-AOI V&V; for a "
    "fast screening flood use sfincs_flood. Forcing is a synthetic inflow "
    "hydrograph unless a real peak discharge is pinned."
)


class HecrasFlood2dError(RuntimeError):
    """Fatal fault before a layer is produced (typed error_code to the emitter)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "peak 2D inundation depth + water surface for a flood at a REAL AOI you "
        "name (refinement-grade HEC-RAS 6.x solver on a headless-AUTHORED 2D mesh "
        "+ terrain-sampled subgrid tables from a fetched DEM). Pin the peak "
        "discharge or scale a default event"
    ),
    required_inputs=["bbox (or a location that resolves to one)"],
    knobs="target_peak_cfs, resolution_m, sim_hours, inlet_edge, outlet_edge, input_mode",
)

_METADATA = AtomicToolMetadata(
    name="hecras_flood_2d",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="hecras",
    tier="template",
)


def _estimate_cells(bbox: list[float], resolution_m: float) -> int:
    """Rough 2D cell-count estimate for a bbox at a resolution (granularity gate)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_mid = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(lat_mid)), 1e-6)
    w_m = abs(max_lon - min_lon) * m_per_deg_lon
    h_m = abs(max_lat - min_lat) * m_per_deg_lat
    return int((w_m / resolution_m) * (h_m / resolution_m))


def _autoscale_resolution(bbox: list[float], resolution_m: float) -> float:
    """Coarsen the resolution until the cell estimate is under the soft cap.

    The autoscaler SUGGESTION (the user overrides via ``resolution_m``); mirrors
    the #154 granularity gate -- a heavy solve never silently launches an
    intractable mesh."""
    res = float(resolution_m)
    while _estimate_cells(bbox, res) > _SOFT_CELL_CAP and res < _MAX_RES_M:
        res = min(res * 1.25, _MAX_RES_M)
    return res


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def hecras_flood_2d(
    bbox: list[float] | None = None,
    location: str | None = None,
    target_peak_cfs: float | None = None,
    resolution_m: float = _DEFAULT_RES_M,
    sim_hours: float = 24.0,
    inlet_edge: str | None = None,
    outlet_edge: str | None = None,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """REFINEMENT-GRADE HEC-RAS 2D FLOOD at a REAL AOI you name (headless-authored geometry).

    THE tool for "flood <a place> with HEC-RAS", "run a HEC-RAS 2D flood at this
    AOI", "what does a big flood look like on <this river reach>", "HEC-RAS
    inundation depth for a real location". Unlike ``hecras_riverine_flood`` (frozen
    Muncie demonstration geometry), this AUTHORS the 2D mesh + terrain subgrid
    tables for the AOI from a fetched DEM, then solves it with the production
    HEC-RAS 6.x engines -- so it floods the place the user actually names.

    Fidelity: production 6.x full-physics 2D unsteady hydraulics on a 2025-authored
    mesh (transplant-path validated end-to-end). SCREENING-grade until broader
    per-AOI V&V. For a FAST screening flood use ``sfincs_flood``; for urban drainage
    use ``swmm_urban_flood``; for a dye/contaminant plume ``telemac_river_dye``.

    Params:
        bbox: the AOI as ``[min_lon, min_lat, max_lon, max_lat]`` (EPSG:4326). The
            primary input -- resolve a named place to a bbox (a county/reach/city
            or a drawn AOI), NOT a single-building geocode.
        location: OPTIONAL convenience -- a place name geocoded to a bbox when
            ``bbox`` is not given (best-effort; prefer passing ``bbox``).
        target_peak_cfs: the PEAK inflow discharge (cfs) that forces the run. Pin
            it to a real gauge/NWM peak; default ~5000 cfs when unset. The inflow
            hydrograph is a ramp to this peak (a real hydrograph override is the
            OI-D residual).
        resolution_m: the 2D cell size (m), clamped [20, 200], granularity-gated
            (auto-coarsened so the mesh stays under a soft cell cap; overridable).
        sim_hours: unsteady window length (hours); default 24.
        inlet_edge / outlet_edge: OPTIONAL compass overrides ("n"/"s"/"e"/"w") for
            where flow enters / drains. Defaults: inflow on the lowest-elevation
            perimeter run, outlet on the south edge (the drainage physics).
        input_mode: ``"user_gated"`` reviews the forcing + resolution + fetched-
            terrain basis before the (heavy) solve; ``"auto"`` (default) proceeds
            with them labeled.

    Returns:
        On success: ``HecrasDepthLayerURI`` -- the peak-depth COG (loaded beside
        the 2D mesh preview + the inflow chart), carrying ``depth_max_ft`` /
        ``depth_mean_ft`` / ``wet_cell_count`` / ``wse_max_ft`` / ``peak_inflow_cfs``
        / ``volume_error_pct`` (narrate these typed numbers only -- invariant 1).
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.
    """
    # --- resolve the AOI bbox -------------------------------------------------- #
    aoi = _coerce_bbox(bbox)
    if aoi is None and location:
        aoi = await asyncio.to_thread(_geocode_bbox, location)
    if aoi is None:
        return {
            "status": "error",
            "error_code": HECRAS_INPUT_INVALID,
            "error_message": "hecras_flood_2d needs a bbox [min_lon,min_lat,max_lon,max_lat] "
            "(or a location that geocodes to one)",
        }

    # --- arg hardening --------------------------------------------------------- #
    try:
        resolution_m = float(resolution_m)
    except (TypeError, ValueError):
        resolution_m = _DEFAULT_RES_M
    resolution_m = min(max(resolution_m, _MIN_RES_M), _MAX_RES_M)
    resolution_m = _autoscale_resolution(aoi, resolution_m)

    peak = _DEFAULT_PEAK_CFS
    if target_peak_cfs is not None:
        try:
            p = float(target_peak_cfs)
            if p > 0.0:
                peak = p
        except (TypeError, ValueError):
            pass

    logger.info(
        "hecras_flood_2d bbox=%s res=%.1fm peak=%.0fcfs sim=%sh inlet=%s outlet=%s",
        aoi, resolution_m, peak, sim_hours, inlet_edge, outlet_edge,
    )

    try:
        depth = await model_hecras_flood_2d(
            bbox=aoi, target_peak_cfs=peak, resolution_m=resolution_m,
            sim_hours=float(sim_hours), inlet_edge=inlet_edge, outlet_edge=outlet_edge,
            input_mode=input_mode,
        )
        if isinstance(depth, dict):
            return depth
        return depth
    except asyncio.CancelledError:
        raise
    except HecrasFlood2dError as exc:
        logger.warning("hecras_flood_2d failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("hecras_flood_2d unexpected failure")
        return {"status": "error", "error_code": "HECRAS_INTERNAL_ERROR", "error_message": str(exc)}


def _coerce_bbox(bbox: Any) -> list[float] | None:
    if not bbox:
        return None
    try:
        vals = [float(x) for x in bbox]
    except (TypeError, ValueError):
        return None
    if len(vals) != 4:
        return None
    min_lon, min_lat, max_lon, max_lat = vals
    if not (max_lon > min_lon and max_lat > min_lat):
        return None
    return [min_lon, min_lat, max_lon, max_lat]


def _geocode_bbox(location: str) -> list[float] | None:
    """Best-effort geocode of a place name to a bbox via ``geocode_location``."""
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY

        res = TOOL_REGISTRY["geocode_location"].fn(query=location)
        bb = getattr(res, "bbox", None) or (res.get("bbox") if isinstance(res, dict) else None)
        return _coerce_bbox(bb)
    except Exception as exc:  # noqa: BLE001
        logger.info("hecras_flood_2d geocode of %r failed: %s", location, exc)
        return None


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.agent.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.agent.workflows.hecras.postprocess_hecras import (
    PostprocessHecrasError,
    postprocess_hecras,
)
from trid3nt_server.agent.workflows.hecras.run_hecras import HECRAS_FLOOD2D_SOLVER_NAME


def _fetch_dem_local(bbox: list[float]) -> str:
    """Fetch the AOI DEM (seam-1) and download it to a local temp GeoTIFF."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    layer = TOOL_REGISTRY["fetch_dem"].fn(bbox=list(bbox), resolution_m=10)
    uri = getattr(layer, "uri", None) or (layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, f"fetch_dem returned no uri for bbox {bbox}")
    from trid3nt_server.agent.tools.simulation.solver.solver import _download_object

    tmp = Path(tempfile.mkdtemp(prefix="flood2d-dem-")) / "dem.tif"
    _download_object(str(uri), tmp)
    return str(tmp)


def _author_and_compose(dem_tif: str, workdir: str, *, peak_cfs: float,
                        resolution_m: float, inlet_edge: str | None,
                        outlet_edge: str | None) -> Any:
    """Run the authoring + compose stages (docker author + host compose)."""
    import sys

    if str(_WORKERS_FRESHTOPO) not in sys.path:
        sys.path.insert(0, str(_WORKERS_FRESHTOPO))
        sys.path.insert(0, str(_WORKERS_FRESHTOPO.parents[2]))  # hecras2025 (writer)
    from flood2d_pipeline import author_and_compose, Flood2dPipelineError  # type: ignore

    try:
        result, info = author_and_compose(
            dem_tif, workdir, peak_cfs=peak_cfs, resolution_m=resolution_m,
            inflow_edge=inlet_edge, ds_edge=(outlet_edge or "s"),
        )
    except Flood2dPipelineError as exc:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, f"authoring/compose failed: {exc}") from exc
    return result


def _stage_deck_manifest(deck_dir: str, run_tag: str) -> str:
    """Upload the composed deck files to the cache bucket + write the run_solver
    manifest (M3-gate no-archetype path: plan_hdf + geom_suffix on staged inputs)."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, "TRID3NT_CACHE_BUCKET must be set")
    s3 = _get_s3_client()
    deck = Path(deck_dir)
    deck_files = ["Fresh2D.p04.tmp.hdf", "Fresh2D.x04", "Fresh2D.b04"]
    inputs = []
    for fn in deck_files:
        p = deck / fn
        if not p.is_file():
            raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, f"composed deck missing {fn}")
        key = f"hecras_flood2d/{run_tag}/{fn}"
        s3.put_object(Bucket=cache_bucket, Key=key, Body=p.read_bytes())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": fn})
    manifest = {
        "run_id": run_tag,
        "plan_hdf": "Fresh2D.p04.tmp.hdf",
        "geom_suffix": "x04",
        "run_geompre": True,
        "inputs": inputs,
        "hecras_args": [],
        "outputs": ["Fresh2D.p04.tmp.hdf", "hecras_metrics.json"],
    }
    key = f"hecras_flood2d/{run_tag}/manifest.json"
    s3.put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{cache_bucket}/{key}"


def _download_plan_hdf(run_id: str) -> str:
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )

    s3 = _get_s3_client()
    key = f"{run_id}/Fresh2D.p04.tmp.hdf"
    tmp = Path(tempfile.mkdtemp(prefix=f"flood2d-{run_id}-")) / "Fresh2D.p04.tmp.hdf"
    try:
        resp = s3.get_object(Bucket=_get_runs_bucket(), Key=key)
        tmp.write_bytes(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise HecrasFlood2dError(
            HECRAS_SOLVE_FAILED, f"solved plan HDF not downloadable ({key}): {exc}"
        ) from exc
    return str(tmp)


async def model_hecras_flood_2d(
    *,
    bbox: list[float],
    target_peak_cfs: float,
    resolution_m: float,
    sim_hours: float = 24.0,
    inlet_edge: str | None = None,
    outlet_edge: str | None = None,
    input_mode: str | None = None,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """fetch DEM -> author+compose -> run_solver -> postprocess -> publish."""
    emitter = current_emitter()
    begin_substeps(emitter, 4)  # fetch+author, run_solver, postprocess, publish

    n_cells_est = _estimate_cells(bbox, resolution_m)
    review_entries: list[SyntheticInput] = [
        SyntheticInput(
            param="geometry", value="authored 2D mesh (HEC-RAS 2025 AuthorMesh)",
            basis="derived",
            note="TryCreateMesh topology + MeshPropertyTables.ComputeFrom subgrid "
            "tables over the fetched terrain (transplant-path validated)",
        ),
        SyntheticInput(
            param="terrain", value="fetch_dem (3DEP/Copernicus)", basis="fetched",
            note="reprojected to a local ftUS grid; elevations m->US survey feet",
        ),
        SyntheticInput(
            param="peak_inflow_cfs", value=round(float(target_peak_cfs), 1), units="cfs",
            basis="user", note="peak of the inflow hydrograph forcing the run",
        ),
        SyntheticInput(
            param="resolution_m", value=round(float(resolution_m), 1), units="m",
            basis="derived", note=f"2D cell size (~{n_cells_est} cells; granularity-gated)",
        ),
    ]
    review = await gate_input_review(
        tool_name="hecras_flood_2d",
        mode=input_mode,
        entries=review_entries,
        params={"bbox": bbox, "target_peak_cfs": target_peak_cfs, "resolution_m": resolution_m},
    )
    if not review.proceed:
        return {
            "status": "error",
            "error_code": "HECRAS_INPUT_REVIEW_CANCELLED",
            "error_message": review.cancel_reason or "input review not approved; the solver did not run",
        }
    target_peak_cfs = float(review.params.get("target_peak_cfs", target_peak_cfs) or target_peak_cfs)
    resolution_m = float(review.params.get("resolution_m", resolution_m) or resolution_m)

    # --- Stage 1: fetch DEM + author + compose the deck (heavy; off-loop) ------ #
    run_tag = new_ulid()
    workdir = tempfile.mkdtemp(prefix=f"flood2d-{run_tag}-")
    async with substep(emitter, "author_compose"):
        dem_tif = await asyncio.to_thread(_fetch_dem_local, bbox)
        result = await asyncio.to_thread(
            _author_and_compose, dem_tif, workdir,
            peak_cfs=target_peak_cfs, resolution_m=resolution_m,
            inlet_edge=inlet_edge, outlet_edge=outlet_edge,
        )
    logger.info(
        "hecras_flood_2d authored deck run_tag=%s cells=%d faces=%d crs=local-ftUS",
        run_tag, result.cells_real, result.faces,
    )

    # --- Stage 2: dispatch the composed deck to run_solver --------------------- #
    manifest_uri = await asyncio.to_thread(_stage_deck_manifest, result.deck_dir, run_tag)
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )

    handle = run_solver(
        solver=HECRAS_FLOOD2D_SOLVER_NAME, model_setup_uri=manifest_uri, compute_class="medium",
    )
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=HECRAS_FLOOD2D_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise HecrasFlood2dError(
            HECRAS_SOLVE_FAILED,
            f"HEC-RAS 2D solve did not complete (status={getattr(run_result,'status',None)}, "
            f"error_code={getattr(run_result,'error_code',None)}): "
            f"{getattr(run_result,'error_message','') or ''}",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag

    metrics = await asyncio.to_thread(_read_run_metrics, batch_run_id)
    va = metrics.get("volume_accounting") or {}
    try:
        vol_err = float(va.get("Error Percent")) if va.get("Error Percent") is not None else None
    except (TypeError, ValueError):
        vol_err = None
    peak_cfs = metrics.get("peak_inflow_cfs") or target_peak_cfs

    # --- Stage 3: postprocess the solved plan HDF ------------------------------ #
    plan_path = await asyncio.to_thread(_download_plan_hdf, batch_run_id)
    try:
        async with substep(emitter, "postprocess_hecras"):
            layers, pp_metrics = await asyncio.to_thread(
                postprocess_hecras, plan_path, run_id=batch_run_id, flow_scale=1.0,
                peak_inflow_cfs=(float(peak_cfs) if peak_cfs is not None else None),
                volume_error_pct=vol_err, fallback_note=_FIDELITY_NOTE,
            )
    except PostprocessHecrasError as exc:
        raise HecrasFlood2dError(exc.error_code, str(exc)) from exc
    finally:
        try:
            Path(plan_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    if not layers:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, "postprocess produced no depth layer")
    depth = layers[0]
    assert isinstance(depth, HecrasDepthLayerURI)
    mesh_layer = layers[1] if len(layers) > 1 else None

    # --- Stage 4: publish the peak-depth COG (render chokepoint) --------------- #
    async with substep(emitter, "publish_layer"):
        depth = await asyncio.to_thread(_publish_depth_layer, depth, review_entries)

    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d mesh preview emit skipped: %s", exc)

    if emitter is not None:
        try:
            await _maybe_emit_inflow_chart(emitter, pp_metrics, bbox)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d inflow chart skipped: %s", exc)

    if emitter is not None and depth.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(depth.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d zoom-to failed: %s", exc)

    logger.info(
        "model_hecras_flood_2d complete run_id=%s depth_max_ft=%.3g wet_cells=%s "
        "peak_cfs=%s vol_err=%s uri=%s",
        batch_run_id, depth.depth_max_ft, depth.wet_cell_count, peak_cfs,
        depth.volume_error_pct, depth.uri,
    )
    return depth


def _read_run_metrics(run_id: str) -> dict[str, Any]:
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )

    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/hecras_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.info("hecras_flood_2d: run metrics read miss for %s: %s", run_id, exc)
        return {}


def _publish_depth_layer(
    depth: HecrasDepthLayerURI, synthetic_inputs: list[SyntheticInput]
) -> HecrasDepthLayerURI:
    out = depth
    if synthetic_inputs:
        try:
            out = out.model_copy(update={"synthetic_inputs": list(synthetic_inputs)})
        except Exception:  # noqa: BLE001
            pass
    try:
        published_uri = publish_layer(
            layer_uri=out.uri, layer_id=out.layer_id, style_preset=out.style_preset,
        )
        return out.model_copy(update={"uri": published_uri})
    except PublishLayerError as exc:
        logger.warning("hecras_flood_2d publish_layer FAILED layer_id=%s (%s)", out.layer_id, exc)
        return out


async def _maybe_emit_inflow_chart(emitter: Any, metrics: dict[str, Any], bbox: list[float]) -> None:
    if not hasattr(emitter, "emit_chart"):
        return
    series = metrics.get("inflow_hydrograph") or []
    if not series:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    values = [{"time_hr": p["t_hr"], "inflow_cfs": p["q_cfs"]} for p in series]
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "color": "#1f5fbf"},
        "encoding": {
            "x": {"field": "time_hr", "type": "quantitative", "title": "time (hours)"},
            "y": {"field": "inflow_cfs", "type": "quantitative", "title": "inflow (cfs)"},
        },
    }
    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="HEC-RAS 2D inflow hydrograph forcing",
        caption="The unsteady inflow forcing the authored-AOI HEC-RAS 2D solve ran with.",
    )
    await emitter.emit_chart(payload)
