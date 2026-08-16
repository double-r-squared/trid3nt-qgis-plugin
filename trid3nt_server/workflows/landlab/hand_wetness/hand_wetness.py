"""Engine template ``landlab_hand_wetness`` - Landlab Height Above Nearest
Drainage (HAND) wetness proxy (HeightAboveDrainageCalculator, Nobre et al. 2011).

A distinct question CLASS (per the capability-naming rule): how high above the
nearest drainage channel is each cell - a relative-elevation / wetness / flood-
proneness proxy. It is its OWN registered engine TEMPLATE (engine="landlab",
tier="template").

``landlab_hand_wetness(...)`` runs the deterministic fetch DEM -> stage -> solve
-> postprocess chain: FlowAccumulator (D8) + a drainage-area channel mask +
HeightAboveDrainageCalculator over the AOI DEM, and returns a
``LandlabHandLayerURI`` (the HAND raster) plus the channel-network vector.
Landlab runs OFF-BOX in the local-exec / Batch solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabHandLayerURI`` fields the worker / postprocess computed. The
DEM is REAL; the channel threshold is a deterministic engine setting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_CHANNEL_THRESHOLD_CELLS,
    LandlabHandLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    HAND_STYLE_PRESET,
    PostprocessLandlabError,
    postprocess_landlab_hand,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.hand_wetness.hand_wetness"
)

__all__ = [
    "landlab_hand_wetness",
    "model_landlab_hand_wetness",
    "HandWorkflowError",
]


class HandWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "how high above the nearest drainage channel is each cell - the Height "
        "Above Nearest Drainage (HAND) wetness / flood-proneness proxy raster "
        "(Landlab HeightAboveDrainageCalculator, Nobre et al. 2011)"
    ),
    required_inputs=["bbox"],
    knobs="channel_threshold_cells, target_resolution_m",
)

_METADATA = AtomicToolMetadata(
    name="landlab_hand_wetness",
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
async def landlab_hand_wetness(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    channel_threshold_cells: int = DEFAULT_CHANNEL_THRESHOLD_CELLS,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabHandLayerURI | dict[str, Any]:
    """Compute Height Above Nearest Drainage (HAND) over a DEM (wetness proxy).

    Fidelity: Landlab HeightAboveDrainageCalculator (Nobre et al. 2011) on a real
    AOI DEM; a terrain wetness / flood-proneness proxy, not a hydraulic model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The channel threshold is a
    deterministic engine setting - no synthetic data.
    Off-scope: drainage area / channel network map -> landlab_flow_accumulation;
    overland-flow inundation depth -> landlab_overland_flow_timeseries; lake extent
    / depth -> landlab_lake_mapping.

    Use this when: the user asks for HAND, height above nearest drainage, a terrain
    wetness / flood-proneness proxy, or relative elevation above the channel
    network over an AOI.

    Params:
        bbox: AOI, EPSG:4326.
        channel_threshold_cells: channel-head drainage-area threshold as a multiple
            of the grid cell area (contributing cells; default 100). Defines the
            drainage network HAND is measured above.
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" reviews the channel threshold; a
            failed review cancels. "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabHandLayerURI`` - the HAND COG, with ``mean_hand_m``,
        ``max_hand_m``, ``channel_area_fraction``, ``lowland_area_fraction``. The
        channel-network vector is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_hand_wetness requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid bbox: {bbox!r}",
        }

    source_note = (
        f"DEM: USGS 3DEP (fetched). Channel threshold: {channel_threshold_cells} "
        "cells - a deterministic engine setting, not synthetic data."
    )
    _review = await gate_input_review(
        tool_name="landlab_hand_wetness",
        mode=input_mode,
        entries=[],
        params={"channel_threshold_cells": channel_threshold_cells},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_hand_wetness {_review.cancel_reason}",
        }
    channel_threshold_cells = int(
        _review.params.get("channel_threshold_cells", channel_threshold_cells)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="hand",
            target_resolution_m=float(target_resolution_m),
            channel_threshold_cells=int(channel_threshold_cells),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab HAND arguments: {exc}",
        }

    logger.info(
        "landlab_hand_wetness bbox=%s threshold_cells=%d res=%.1fm",
        run_args.bbox,
        channel_threshold_cells,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_hand_wetness(
            run_args, compute_class=compute_class, source_note=source_note
        )
        logger.info(
            "landlab_hand_wetness complete layer_id=%s mean_hand=%.3f m "
            "max_hand=%.3f m channel_frac=%.4f uri=%s",
            primary.layer_id,
            primary.mean_hand_m,
            primary.max_hand_m,
            primary.channel_area_fraction,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        HandWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_hand_wetness failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_hand_wetness unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_hand_wetness(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabHandLayerURI:
    """Compose the HAND chain end-to-end (OFF-BOX lane).

    Returns the HAND ``LandlabHandLayerURI``; emits the channel-network vector as
    a side effect on the bound emitter.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )
    channel_cog = (solve.secondary_cogs or {}).get("channel_network")

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, _metrics = await asyncio.to_thread(
                postprocess_landlab_hand,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                channel_cog_path=channel_cog,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise HandWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_hand produced no HAND layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, raw_primary, default_style=HAND_STYLE_PRESET
        )

    if tuple(primary.bbox or ()) != tuple(solve.bbox):
        primary = primary.model_copy(update={"bbox": tuple(solve.bbox)})
    _upd: dict[str, Any] = {}
    if source_note is not None:
        _upd["source_note"] = source_note
    if synthetic_inputs:
        _upd["synthetic_inputs"] = list(synthetic_inputs)
    if _upd:
        primary = primary.model_copy(update=_upd)

    if emitter is not None:
        for ctx in context_layers:
            try:
                await emitter.add_loaded_layer(ctx)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add HAND channel vector: %s", exc)

    await emit_zoom_to(emitter, solve.bbox)
    return primary
