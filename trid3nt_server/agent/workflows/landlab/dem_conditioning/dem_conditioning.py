"""Engine template ``landlab_dem_conditioning`` - Landlab pit-fill DEM
conditioning depth (LakeMapperBarnes).

A distinct question CLASS (per the capability-naming rule): is my DEM routable,
and WHERE did it need filling before flow can route? The per-cell FILL DEPTH -
the metres the surface had to rise to remove closed depressions - is surfaced as
its own layer (the quantity the flow-accumulation chain discards). It is its OWN
registered engine TEMPLATE (engine="landlab", tier="template").

``landlab_dem_conditioning(...)`` runs the deterministic fetch DEM -> stage ->
solve -> postprocess chain: a FlowAccumulator + LakeMapperBarnes fill over the AOI
DEM, and returns a ``LandlabDemConditioningLayerURI`` (the fill-depth raster).
Landlab runs OFF-BOX in the local-exec / Batch solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabDemConditioningLayerURI`` fields the worker / postprocess
computed. The DEM is REAL; the fill mode is a deterministic engine setting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    LandlabDemConditioningLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.agent.workflows.landlab._template_card import TemplateCard
from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
    FILL_DEPTH_STYLE_PRESET,
    PostprocessLandlabError,
    postprocess_landlab_dem_conditioning,
)
from trid3nt_server.agent.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.agent.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.landlab.dem_conditioning.dem_conditioning"
)

__all__ = [
    "landlab_dem_conditioning",
    "model_landlab_dem_conditioning",
    "DemConditioningWorkflowError",
]


class DemConditioningWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "is this DEM routable and WHERE did it need filling - the per-cell "
        "pit-fill depth conditioning raster (Landlab LakeMapperBarnes)"
    ),
    required_inputs=["bbox"],
    knobs="fill_flat, target_resolution_m",
)

_METADATA = AtomicToolMetadata(
    name="landlab_dem_conditioning",
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
async def landlab_dem_conditioning(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    fill_flat: bool = True,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabDemConditioningLayerURI | dict[str, Any]:
    """Map where a DEM needed pit-filling to become routable (fill-depth raster).

    Fidelity: Landlab LakeMapperBarnes depression filling on a real AOI DEM; a
    terrain-conditioning diagnostic, not a hydraulic model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The fill mode (fill_flat) is a
    deterministic engine setting - no synthetic data.
    Off-scope: drainage area / channel network -> landlab_flow_accumulation; lake
    extent and depth -> landlab_lake_mapping; overland-flow depth ->
    landlab_overland_flow_timeseries.

    Use this when: the user asks whether a DEM is ROUTABLE, where it has closed
    depressions / sinks, or for a pit-fill / DEM-conditioning fill-depth map.

    Params:
        bbox: AOI, EPSG:4326.
        fill_flat: True fills each depression flat; False fills to a slight
            downslope incline so flow routes over the lake (default True).
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the fill setting for
            review; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabDemConditioningLayerURI`` - the fill-depth COG, with
        ``max_fill_depth_m``, ``filled_area_fraction``, ``n_depressions``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_dem_conditioning requires a bbox "
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
        f"DEM: USGS 3DEP (fetched). Fill mode: fill_flat={fill_flat} - a "
        "deterministic engine setting, not synthetic data."
    )
    _review = await gate_input_review(
        tool_name="landlab_dem_conditioning",
        mode=input_mode,
        entries=[],
        params={"fill_flat": fill_flat},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_dem_conditioning {_review.cancel_reason}",
        }
    fill_flat = bool(_review.params.get("fill_flat", fill_flat))

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="dem_pit_fill",
            target_resolution_m=float(target_resolution_m),
            fill_flat=bool(fill_flat),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab DEM-conditioning arguments: {exc}",
        }

    logger.info(
        "landlab_dem_conditioning bbox=%s fill_flat=%s res=%.1fm",
        run_args.bbox,
        fill_flat,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_dem_conditioning(
            run_args, compute_class=compute_class, source_note=source_note
        )
        logger.info(
            "landlab_dem_conditioning complete layer_id=%s max_fill=%.3f m "
            "filled_frac=%.4f n_depressions=%d uri=%s",
            primary.layer_id,
            primary.max_fill_depth_m,
            primary.filled_area_fraction,
            primary.n_depressions,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        DemConditioningWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_dem_conditioning failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_dem_conditioning unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_dem_conditioning(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabDemConditioningLayerURI:
    """Compose the DEM pit-fill conditioning chain end-to-end (OFF-BOX lane)."""
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, _metrics = await asyncio.to_thread(
                postprocess_landlab_dem_conditioning,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise DemConditioningWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_dem_conditioning produced no fill-depth layer",
        )

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, layers[0], default_style=FILL_DEPTH_STYLE_PRESET
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

    await emit_zoom_to(emitter, solve.bbox)
    return primary
