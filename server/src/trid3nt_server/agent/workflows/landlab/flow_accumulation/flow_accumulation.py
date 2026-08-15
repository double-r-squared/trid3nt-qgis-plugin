"""Engine template ``landlab_flow_accumulation`` - Landlab drainage-area +
channel-network extraction (the canonical the_FlowAccumulator tutorial chain).

A distinct question CLASS from ``landlab_susceptibility`` (per the
capability-naming rule): where does water flow and drainage area accumulate
across a landscape, how does the routing choice (D8 / D-infinity / MFD) move the
concentrated flow paths, and where is the extracted channel network? It is its
OWN registered engine TEMPLATE (engine="landlab", tier="template"), NOT an enum
extension of ``landlab_susceptibility`` -- flow accumulation and landslide
susceptibility are different questions with different outputs and different
narration scalars.

``landlab_flow_accumulation(...)`` runs the deterministic fetch DEM -> stage ->
solve -> postprocess chain (``model_landlab_flow_accumulation`` below): a
``FlowAccumulator`` (or ``PriorityFloodFlowRouter``) over the AOI DEM, and
returns a ``LandlabFlowAccumulationLayerURI`` (the log-styled drainage-area
raster) plus a channel-network vector context layer and a routing-comparison
chart. Landlab runs OFF-BOX in the local-exec / Batch solver seam (exec_kind
"exec"; no baked image) -- the same seam ``landlab_susceptibility`` dispatches
through.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabFlowAccumulationLayerURI.max_drainage_area_km2`` /
``.mean_drainage_area_km2`` / ``.channelized_area_fraction`` fields the worker /
postprocess computed -- never free-generated. The DEM is REAL (fetched via
seam-1 ``fetch_3dep_extra`` / ``fetch_dem``); the routing / depression /
channel-threshold knobs are deterministic engine settings, not synthetic data.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabFlowAccumulationLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.agent.workflows.landlab._template_card import TemplateCard
from trid3nt_server.agent.workflows.landlab.run_landlab import LANDLAB_RES_SPEC
from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
    DRAINAGE_AREA_STYLE_PRESET,
    PostprocessLandlabError,
    build_routing_comparison_chart_spec,
    postprocess_landlab_flow_accumulation,
)
from trid3nt_server.agent.workflows.landlab.run_landlab import (
    LANDLAB_SOLVER_NAME,
    LandlabStaging,
    LandlabWorkflowError,
    stage_landlab_manifest,
)
from trid3nt_server.agent.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
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
    "trid3nt_server.agent.workflows.landlab.flow_accumulation.flow_accumulation"
)

__all__ = [
    "landlab_flow_accumulation",
    "model_landlab_flow_accumulation",
    "FlowAccumulationWorkflowError",
]


class FlowAccumulationWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card (the run_landlab door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "flow accumulation / drainage-area raster + extracted channel network "
        "over a watershed DEM, and how the flow-routing choice (D8 / Dinf / MFD) "
        "changes where concentrated flow ends up (Landlab FlowAccumulator)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "flow_director (D8 / Dinf / MFD), depression_handler (fill / "
        "priority_flood), channel_threshold_cells, target_resolution_m"
    ),
)


_METADATA = AtomicToolMetadata(
    name="landlab_flow_accumulation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="landlab",
    tier="template",
    resolution_specs=(LANDLAB_RES_SPEC,),
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def landlab_flow_accumulation(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    flow_director: str = "D8",
    depression_handler: str = "fill",
    channel_threshold_cells: int = 100,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LandlabFlowAccumulationLayerURI | dict[str, Any]:
    """Route flow over a watershed DEM: drainage-area raster + channel network + routing comparison.

    Fidelity: Landlab FlowAccumulator drainage-area accumulation on a real AOI
    DEM; a planning-grade routing/channel-extraction surface, not a calibrated
    hydrologic-routing model.
    Data: the DEM is REAL (USGS 3DEP 1 m LiDAR -> 10 m fallback via seam-1). The
    routing choice (flow_director), depression handling (depression_handler), and
    channel-head threshold (channel_threshold_cells) are DETERMINISTIC engine
    settings -- no synthetic/demo DATA is used.
    Off-scope: landslide susceptibility / factor of safety -> landlab_susceptibility;
    rainfall overland-flow depth -> landlab_susceptibility(analysis="overland_flow");
    riverine/coastal inundation -> sfincs_flood; urban pluvial -> swmm_urban_flood.

    Use this when: the user wants flow accumulation / DRAINAGE AREA, a CHANNEL
    NETWORK extracted from a DEM, or a comparison of flow-routing algorithms
    (D8 vs D-infinity vs MFD) over a watershed / catchment.

    Params:
        bbox: watershed / catchment AOI, EPSG:4326 (min_lon, min_lat, max_lon,
            max_lat).
        flow_director: routing director, one of "D8" (default), "Dinf", "MFD".
        depression_handler: "fill" (DepressionFinderAndRouter, D8-only; default)
            or "priority_flood" (PriorityFloodFlowRouter, any director).
        channel_threshold_cells: channel-head drainage-area threshold as a
            multiple of the grid cell area (contributing cells; default 100).
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the resolved
            routing / depression / threshold knobs for review before the solve;
            "auto" (default) proceeds with them labeled.

    Returns:
        On success: ``LandlabFlowAccumulationLayerURI`` -- the drainage-area COG,
        with ``max_drainage_area_km2``, ``mean_drainage_area_km2``,
        ``channelized_area_fraction``. A channel-network vector layer + a
        routing-comparison chart are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_flow_accumulation requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }

    # The routing / depression / threshold knobs are the only levers; the DEM is
    # real-fetched. Provenance is a single line: real DEM + deterministic engine
    # settings (nothing synthesized). The input gate presents the knobs.
    provenance: list[SyntheticInput] = []
    source_note = (
        f"DEM: USGS 3DEP (fetched). Routing: flow_director={flow_director}, "
        f"depression_handler={depression_handler}, "
        f"channel_threshold_cells={channel_threshold_cells} -- deterministic engine "
        f"settings, not synthetic data."
    )

    _review = await gate_input_review(
        tool_name="landlab_flow_accumulation",
        mode=input_mode,
        entries=provenance,
        params={
            "flow_director": flow_director,
            "depression_handler": depression_handler,
            "channel_threshold_cells": channel_threshold_cells,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_flow_accumulation {_review.cancel_reason}",
        }
    flow_director = str(_review.params.get("flow_director", flow_director))
    depression_handler = str(_review.params.get("depression_handler", depression_handler))
    channel_threshold_cells = int(
        _review.params.get("channel_threshold_cells", channel_threshold_cells)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="flow_accumulation",
            target_resolution_m=float(target_resolution_m),
            depression_handler=depression_handler,
            channel_threshold_cells=int(channel_threshold_cells),
            advanced_physics={"flow_director": flow_director},
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab flow-accumulation arguments: {exc}",
        }

    logger.info(
        "landlab_flow_accumulation bbox=%s director=%s depression=%s "
        "threshold_cells=%d res=%.1fm",
        run_args.bbox,
        flow_director,
        depression_handler,
        channel_threshold_cells,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_flow_accumulation(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_flow_accumulation complete layer_id=%s max_da=%.4g km2 "
            "mean_da=%.4g km2 chan_frac=%.4g uri=%s",
            primary.layer_id,
            primary.max_drainage_area_km2,
            primary.mean_drainage_area_km2,
            primary.channelized_area_fraction,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        FlowAccumulationWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_flow_accumulation failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("landlab_flow_accumulation unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer (deterministic, no LLM in the chain -- Invariant 2):
#   fetch DEM -> stage -> run_solver('landlab') -> download field+channel COGs
#     -> postprocess_landlab_flow_accumulation -> publish drainage-area raster
#     -> add channel-network vector -> emit routing-comparison chart.
# Reuses the susceptibility composer's DEM fetch + download + AOI-floor helpers
# (the shared Landlab off-box seam) rather than reinventing them.
# --------------------------------------------------------------------------- #
async def model_landlab_flow_accumulation(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabFlowAccumulationLayerURI:
    """Compose the Landlab flow-accumulation chain end-to-end (OFF-BOX lane).

    Returns the primary drainage-area ``LandlabFlowAccumulationLayerURI``; emits
    the channel-network vector + the routing-comparison chart as side effects on
    the bound emitter.
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
            logger.warning("model_landlab_flow_accumulation: zoom-to emit failed: %s", exc)

    begin_substeps(current_emitter(), 6 if dem_path is None else 5)

    # --- Step 1: DEM (1 m 3DEP primary -> 10 m fallback) ---
    if dem_path is None:
        async with substep(current_emitter(), "fetch_dem"):
            local_dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_landslide, bbox
            )
    else:
        local_dem_path, dem_source = dem_path, "supplied"
    logger.info("model_landlab_flow_accumulation: DEM=%s (%s)", local_dem_path, dem_source)

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
                "Landlab flow-accumulation solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}"
            ),
            details={"run_id": rid},
        )

    batch_run_id = getattr(run_result, "run_id", None) or rid

    # --- Step 4: download the drainage-area field COG + the channel mask COG ---
    async with substep(current_emitter(), "download_landlab_outputs"):
        (
            local_field,
            result_block,
            batch_out_dir,
            secondary_cogs,
        ) = await asyncio.to_thread(
            _download_batch_landlab_outputs, run_result, batch_run_id
        )

    channel_cog = secondary_cogs.get("channel_network")

    # --- Step 5: postprocess (drainage-area 4326 COG + channel vector) ---
    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_flow_accumulation,
                local_field,
                run_id=rid,
                result=result_block,
                channel_cog_path=channel_cog,
            )
    finally:
        _cleanup_dir(batch_out_dir)

    if not layers:
        raise FlowAccumulationWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_flow_accumulation produced no drainage-area layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    # --- Step 6: publish the drainage-area raster (render chokepoint) ---
    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(_publish_drainage_layer, raw_primary, rid)

    if tuple(primary.bbox or ()) != tuple(bbox):
        primary = primary.model_copy(update={"bbox": tuple(bbox)})
    _prim_update: dict[str, Any] = {}
    if source_note is not None:
        _prim_update["source_note"] = source_note
    if synthetic_inputs:
        _prim_update["synthetic_inputs"] = list(synthetic_inputs)
    if _prim_update:
        primary = primary.model_copy(update=_prim_update)

    # --- Channel-network vector context layer (rendered inline via GeoJSON) ---
    if emitter is not None:
        for ctx in context_layers:
            try:
                await emitter.add_loaded_layer(ctx)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add channel-network vector: %s", exc)

    # --- Routing-comparison chart (the tutorial's central figure) ---
    await _maybe_emit_routing_chart(
        emitter, metrics.get("routing_comparison") or [], primary.uri
    )

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_landlab_flow_accumulation: authoritative zoom-to failed: %s", exc
            )

    logger.info(
        "model_landlab_flow_accumulation complete run_id=%s max_da=%.4g km2 "
        "chan_frac=%.4f uri=%s",
        rid,
        primary.max_drainage_area_km2,
        primary.channelized_area_fraction,
        primary.uri,
    )
    return primary


def _publish_drainage_layer(
    raw_primary: LandlabFlowAccumulationLayerURI, run_id: str
) -> LandlabFlowAccumulationLayerURI:
    """Publish the drainage-area COG through publish_layer (the render seam).

    On publish failure the raw layer is returned UNCHANGED (the dispatch-level
    guardrail drops the dead raw-s3 raster; the typed scalars still narrate).
    Mirrors ``susceptibility._publish_primary_layer``.
    """
    if raw_primary.layer_type != "raster" or not (
        raw_primary.uri.startswith("gs://") or raw_primary.uri.startswith("s3://")
    ):
        return raw_primary
    layer_id_for_pub = f"landlab-drainage-area-{run_id}"
    style = raw_primary.style_preset or DRAINAGE_AREA_STYLE_PRESET
    try:
        published_uri = publish_layer(
            layer_uri=raw_primary.uri,
            layer_id=layer_id_for_pub,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_flow_accumulation: publish_layer FAILED layer_id=%s "
            "error_code=%s (%s) - returning the unpublished layer.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_primary
    return raw_primary.model_copy(update={"uri": published_uri, "style_preset": style})


async def _maybe_emit_routing_chart(
    emitter: Any, routing_comparison: list[dict[str, Any]], source_uri: str
) -> None:
    """Emit the routing-comparison chart (D8 vs Dinf vs MFD) to the charts window."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_routing_comparison_chart_spec(routing_comparison)
    if spec is None:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Flow-routing comparison (channelized area by director)",
        caption=(
            "How much the flow-routing choice (D8 / D-infinity / MFD) moves the "
            "concentrated flow paths -- the fraction of the AOI that channelizes "
            "under each director (all priority-flood routed)."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("routing-comparison chart emit failed: %s", exc)
