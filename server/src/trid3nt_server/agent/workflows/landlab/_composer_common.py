"""Shared off-box boilerplate for the Landlab diagnostic templates.

Every Landlab template composer runs the SAME deterministic chain (Invariant 1 -
no LLM in the chain): fetch DEM -> stage manifest -> run_solver('landlab') ->
wait -> download the field COG + the worker result block + any secondary COGs.
This module owns that boilerplate ONCE (fetch/stage/solve/download + the emitter
substep breadcrumbs + the two-card Sim observability) so each template module is
only its own postprocess + publish + chart wiring.

The DEM is REAL (fetched via seam-1 fetch_3dep_extra 1 m -> fetch_dem 10 m
fallback); Landlab runs OFF-BOX through the generic run_solver / wait_for_completion
seam (exec_kind "exec"; no baked image).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import LandlabRunArgs

from trid3nt_server.agent.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.agent.workflows.landlab.run_landlab import (
    LANDLAB_SOLVER_NAME,
    LandlabStaging,
    LandlabWorkflowError,
    stage_landlab_manifest,
)
from trid3nt_server.agent.workflows.landlab.susceptibility.susceptibility import (
    _cleanup_dir,
    _download_batch_landlab_outputs,
    _enforce_min_landslide_aoi,
    _fetch_dem_for_landslide,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.landlab._composer_common"
)

__all__ = [
    "LandlabSolveResult",
    "stage_solve_download",
    "publish_raster_layer",
    "emit_zoom_to",
    "emit_landlab_chart",
    "cleanup_solve",
]


@dataclass
class LandlabSolveResult:
    """The staged-solve-download result the template composer postprocesses.

    Fields:
        run_id: the run identifier the output COGs are keyed under.
        bbox: the AOI-floored bbox (the authoritative AOI to stamp + zoom to).
        local_field: on-disk path to the downloaded primary field COG.
        result_block: the worker's deterministic ``result`` block (authoritative
            narration scalars).
        secondary_cogs: token -> local COG path for the worker secondary fields.
        batch_out_dir: the temp download dir (pass to ``cleanup_solve``).
    """

    run_id: str
    bbox: tuple[float, float, float, float]
    local_field: str
    result_block: dict[str, Any]
    secondary_cogs: dict[str, str]
    batch_out_dir: str


async def stage_solve_download(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
) -> LandlabSolveResult:
    """Fetch DEM -> stage -> run_solver('landlab') -> wait -> download outputs.

    Emits the fetch_dem / stage / run_solver / download substep breadcrumbs + the
    two-card Sim observability. Raises ``LandlabWorkflowError`` on a solve that
    does not complete. The caller owns postprocess + publish + cleanup.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        EmitterBinding,
        new_ulid,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    bbox = _enforce_min_landslide_aoi(tuple(run_args.bbox))
    emitter = current_emitter()
    rid = run_id or new_ulid()

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning("stage_solve_download: zoom-to emit failed: %s", exc)

    begin_substeps(current_emitter(), 6 if dem_path is None else 5)

    # --- Step 1: DEM (1 m 3DEP primary -> 10 m fallback) ---
    if dem_path is None:
        async with substep(current_emitter(), "fetch_dem"):
            local_dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_landslide, bbox
            )
    else:
        local_dem_path, dem_source = dem_path, "supplied"
    logger.info("stage_solve_download: DEM=%s (%s)", local_dem_path, dem_source)

    # --- Step 2: stage DEM + build_spec manifest ---
    async with substep(current_emitter(), "stage_landlab_manifest"):
        staging: LandlabStaging = await asyncio.to_thread(
            stage_landlab_manifest, run_args, dem_path=local_dem_path, run_id=rid
        )

    # --- Step 3: dispatch through the generic solver seam ---
    async with substep(current_emitter(), "run_solver"):
        handle = run_solver(
            solver=LANDLAB_SOLVER_NAME,
            model_setup_uri=staging.manifest_uri,
            compute_class=compute_class,
        )
        _sim_step_id = await mint_dispatch_and_sim_cards(
            emitter=emitter,
            solver=LANDLAB_SOLVER_NAME,
            handle=handle,
            compute_class=compute_class,
        )
        if emitter is not None and _sim_step_id is not None:
            set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))
        run_result = None
        try:
            run_result = await wait_for_completion(handle)
        except asyncio.CancelledError:
            await route_sim_terminal(emitter, _sim_step_id, run_result=None)
            raise
        finally:
            set_emitter_binding(None)
        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result.status != "complete":
        raise LandlabWorkflowError(
            "LANDLAB_RUN_FAILED",
            message=(
                f"Landlab {run_args.analysis} solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}"
            ),
            details={"run_id": rid},
        )

    batch_run_id = getattr(run_result, "run_id", None) or rid

    # --- Step 4: download the field COG + secondary COGs + result block ---
    async with substep(current_emitter(), "download_landlab_outputs"):
        (
            local_field,
            result_block,
            batch_out_dir,
            secondary_cogs,
        ) = await asyncio.to_thread(
            _download_batch_landlab_outputs, run_result, batch_run_id
        )

    return LandlabSolveResult(
        run_id=rid,
        bbox=bbox,
        local_field=local_field,
        result_block=result_block,
        secondary_cogs=secondary_cogs,
        batch_out_dir=batch_out_dir,
    )


def publish_raster_layer(layer: Any, *, default_style: str | None = None) -> Any:
    """Publish a raster LayerURI through publish_layer (the render chokepoint).

    Keeps the layer's assigned ``layer_id`` (load-bearing for the animation
    peak/frame web tokens). On publish failure returns the layer UNCHANGED (the
    dispatch guardrail drops the dead raw raster; typed scalars still narrate). A
    non-raster / already-published layer passes through.
    """
    if layer.layer_type != "raster" or not (
        layer.uri.startswith("gs://") or layer.uri.startswith("s3://")
    ):
        return layer
    style = layer.style_preset or default_style
    try:
        published_uri = publish_layer(
            layer_uri=layer.uri,
            layer_id=layer.layer_id,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "publish_raster_layer FAILED layer_id=%s error_code=%s (%s) - "
            "returning the unpublished layer.",
            layer.layer_id,
            exc.error_code,
            exc,
        )
        return layer
    return layer.model_copy(update={"uri": published_uri, "style_preset": style})


async def emit_zoom_to(
    emitter: Any, bbox: tuple[float, float, float, float]
) -> None:
    """Emit an authoritative zoom-to for the AOI (non-fatal)."""
    if emitter is None:
        return
    try:
        await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
    except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
        logger.warning("emit_zoom_to failed: %s", exc)


async def emit_landlab_chart(
    emitter: Any,
    spec: dict[str, Any] | None,
    *,
    title: str,
    caption: str,
    source_uri: str,
) -> None:
    """Emit a Vega-Lite chart to the charts window (non-fatal; None spec skips)."""
    if emitter is None or spec is None or not hasattr(emitter, "emit_chart"):
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("landlab chart emit failed: %s", exc)


def cleanup_solve(solve: LandlabSolveResult) -> None:
    """Remove the temp Batch-output download dir."""
    _cleanup_dir(solve.batch_out_dir)
