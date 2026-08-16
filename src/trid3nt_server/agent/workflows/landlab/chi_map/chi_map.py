"""Engine template ``landlab_channel_steepness_chi_map`` - Landlab chi index +
normalized channel steepness (ksn) diagnostic (ChiFinder + SteepnessFinder).

A distinct question CLASS (per the capability-naming rule): which channel reaches
are anomalously steep for their drainage area (a knickpoint / tectonic-activity
proxy). A chart-led diagnostic on the routed real DEM. It is its OWN registered
engine TEMPLATE (engine="landlab", tier="template").

``landlab_channel_steepness_chi_map(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain: FlowAccumulator + ChiFinder + SteepnessFinder
over the AOI DEM, and returns a ``LandlabChiMapLayerURI`` (the chi index over the
channel network) plus the ksn raster, the channel-network vector, and the
chi-elevation profile chart. Landlab runs OFF-BOX in the local-exec / Batch solver
seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from the
typed ``LandlabChiMapLayerURI`` fields the worker / postprocess computed. The DEM
is REAL; the diagnostic reads deterministic engine outputs (no synthetic data).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    LandlabChiMapLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_landlab_chart,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.agent.workflows.landlab._template_card import TemplateCard
from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
    CHANNEL_STEEPNESS_STYLE_PRESET,
    CHI_STYLE_PRESET,
    PostprocessLandlabError,
    build_chi_elevation_chart_spec,
    postprocess_landlab_chi_map,
)
from trid3nt_server.agent.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.agent.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.landlab.chi_map.chi_map"
)

__all__ = [
    "landlab_channel_steepness_chi_map",
    "model_landlab_chi_map",
    "ChiMapWorkflowError",
]


class ChiMapWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "which channel reaches are anomalously steep for their drainage area - a "
        "knickpoint / tectonic proxy (Landlab ChiFinder + SteepnessFinder; chi + ksn "
        "rasters over the channel network + the chi-elevation profile chart)"
    ),
    required_inputs=["bbox"],
    knobs="reference_concavity, channel_threshold_cells, target_resolution_m",
)

_METADATA = AtomicToolMetadata(
    name="landlab_channel_steepness_chi_map",
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
async def landlab_channel_steepness_chi_map(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    reference_concavity: float = 0.5,
    channel_threshold_cells: int = 100,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabChiMapLayerURI | dict[str, Any]:
    """Map chi + channel steepness (ksn) to flag anomalously steep reaches.

    Fidelity: Landlab ChiFinder + SteepnessFinder on a real AOI DEM; a geomorphic
    diagnostic (knickpoint / tectonic proxy), not a calibrated model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). chi + ksn read deterministic
    FlowAccumulator outputs - no synthetic data.
    Off-scope: landscape-evolution / steady-state incision V&V ->
    landlab_channel_incision_steady_state; Hack's-law length-area scaling ->
    landlab_hacks_law_scaling; drainage area / channel network ->
    landlab_flow_accumulation.

    Use this when: the user asks about CHANNEL STEEPNESS, the ksn index, a CHI MAP
    / chi analysis, KNICKPOINTS, or which reaches are anomalously steep for their
    drainage area over an AOI.

    Params:
        bbox: watershed / catchment AOI, EPSG:4326.
        reference_concavity: theta chi + ksn are computed at (default 0.5).
        channel_threshold_cells: channel-head drainage-area threshold in cells
            (default 100).
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" reviews the parameters before the
            solve; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabChiMapLayerURI`` - the chi-index COG over the channel
        network, with ``max_chi``, ``max_ksn``, ``mean_ksn``, ``reference_concavity``,
        ``n_channel_nodes``. The ksn raster, channel-network vector, and the
        chi-elevation chart are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_channel_steepness_chi_map requires a bbox "
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
        "DEM: USGS 3DEP (fetched). chi + ksn read deterministic engine outputs "
        f"at reference concavity {reference_concavity}."
    )
    _review = await gate_input_review(
        tool_name="landlab_channel_steepness_chi_map",
        mode=input_mode,
        entries=[],
        params={
            "reference_concavity": reference_concavity,
            "channel_threshold_cells": channel_threshold_cells,
            "target_resolution_m": target_resolution_m,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_channel_steepness_chi_map {_review.cancel_reason}",
        }
    reference_concavity = float(
        _review.params.get("reference_concavity", reference_concavity)
    )
    channel_threshold_cells = int(
        _review.params.get("channel_threshold_cells", channel_threshold_cells)
    )
    target_resolution_m = float(
        _review.params.get("target_resolution_m", target_resolution_m)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="chi_map",
            target_resolution_m=float(target_resolution_m),
            reference_concavity=float(reference_concavity),
            channel_threshold_cells=int(channel_threshold_cells),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab chi-map arguments: {exc}",
        }

    logger.info(
        "landlab_channel_steepness_chi_map bbox=%s res=%.1fm theta=%.3f thresh=%d",
        run_args.bbox, run_args.target_resolution_m, reference_concavity,
        channel_threshold_cells,
    )

    try:
        primary = await model_landlab_chi_map(
            run_args, compute_class=compute_class, source_note=source_note
        )
        logger.info(
            "landlab_channel_steepness_chi_map complete layer_id=%s max_ksn=%.3f "
            "mean_ksn=%.3f n_chan=%d uri=%s",
            primary.layer_id, primary.max_ksn, primary.mean_ksn,
            primary.n_channel_nodes, primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        ChiMapWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_channel_steepness_chi_map failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_channel_steepness_chi_map unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_chi_map(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabChiMapLayerURI:
    """Compose the chi-map diagnostic chain end-to-end (OFF-BOX lane).

    Returns the chi-index ``LandlabChiMapLayerURI``; emits the ksn context raster,
    the channel-network vector, and the chi-elevation chart as side effects.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )
    ksn_cog = (solve.secondary_cogs or {}).get("channel_steepness")
    channel_cog = (solve.secondary_cogs or {}).get("channel_network")

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_chi_map,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                ksn_cog_path=ksn_cog,
                channel_cog_path=channel_cog,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise ChiMapWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_chi_map produced no chi-index layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, raw_primary, default_style=CHI_STYLE_PRESET
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
            published = ctx
            if ctx.layer_type == "raster":
                published = await asyncio.to_thread(
                    publish_raster_layer,
                    ctx,
                    default_style=CHANNEL_STEEPNESS_STYLE_PRESET,
                )
            try:
                await emitter.add_loaded_layer(published)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add chi-map context layer: %s", exc)

    await emit_landlab_chart(
        emitter,
        build_chi_elevation_chart_spec(metrics.get("scatter") or []),
        title="Chi-elevation profile",
        caption=(
            "Channel-node elevation vs chi - a near-linear trend is uniform "
            "steepness; slope breaks flag knickpoints / anomalously steep reaches "
            f"(max ksn {primary.max_ksn:.2f}, mean {primary.mean_ksn:.2f}, "
            f"theta {primary.reference_concavity:.2f})."
        ),
        source_uri=primary.uri,
    )
    await emit_zoom_to(emitter, solve.bbox)
    return primary
