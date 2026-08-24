"""Engine template ``landlab_normal_fault_scarp_evolution`` - normal-fault
tectonic-forcing landscape evolution (scarp growth + footwall drainage).

A distinct question CLASS (per the capability-naming rule): impose a normal-fault
throw history on this catchment and evolve it under stream-power erosion +
hillslope diffusion, so a fault scarp grows on the footwall, degrades, and the
footwall drainage network develops. It is its OWN registered engine TEMPLATE
(engine="landlab", tier="template").

``landlab_normal_fault_scarp_evolution(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain: a Landlab ``NormalFault`` (footwall throw)
coupled to ``FastscapeEroder`` (E = K A^m S^n) + ``LinearDiffuser`` stepped over
the AOI DEM, and returns a ``LandlabNormalFaultLayerURI`` (the evolved topography
carrying the scarp) plus the cumulative-throw context raster (the footwall
forcing). Landlab runs OFF-BOX in the local-exec / Batch solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabNormalFaultLayerURI`` fields the worker / postprocess
computed. The DEM is REAL (USGS 3DEP); the fault geometry + throw rate are a
LABELED demo scenario (SyntheticInput) - the terrain is real, the forcing is a
scenario.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    LandlabNormalFaultLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    EVOLVED_ELEVATION_STYLE_PRESET,
    FAULT_THROW_STYLE_PRESET,
    PostprocessLandlabError,
    postprocess_landlab_normal_fault,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.normal_fault.normal_fault"
)

__all__ = [
    "landlab_normal_fault_scarp_evolution",
    "model_landlab_normal_fault",
    "NormalFaultWorkflowError",
]


class NormalFaultWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "impose a normal-fault throw history on this landscape and show how the "
        "fault scarp degrades and the footwall drainage network develops (Landlab "
        "NormalFault + FastscapeEroder + LinearDiffuser; evolved topography + "
        "cumulative-throw footwall raster)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "fault_throw_rate_m_yr, fault_dip_deg, fault_position_frac, "
        "incision_run_duration_yr, k_bedrock, target_resolution_m"
    ),
)

_METADATA = AtomicToolMetadata(
    name="landlab_normal_fault_scarp_evolution",
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
async def landlab_normal_fault_scarp_evolution(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    fault_throw_rate_m_yr: float = 1.0e-3,
    fault_dip_deg: float = 60.0,
    fault_position_frac: float = 0.5,
    k_bedrock: float = 1.0e-5,
    hillslope_diffusivity_m2_yr: float = 0.1,
    incision_run_duration_yr: float = 5.0e5,
    incision_n_timesteps: int = 400,
    target_resolution_m: float = 90.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabNormalFaultLayerURI | dict[str, Any]:
    """Impose a normal-fault throw history + evolve the scarp and footwall drainage.

    Fidelity: Landlab NormalFault (footwall throw) coupled to FastscapeEroder
    (E = K A^m S^n) + LinearDiffuser on a real AOI DEM; a tectonic-forcing
    landscape-evolution demo, not a calibrated forecast.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The fault geometry (an E-W
    trace at fault_position_frac of the domain) + throw rate are a LABELED demo
    scenario (the terrain is real, the forcing is a scenario).
    Off-scope: steady-state stream-power incision with a UNIFORM uplift (no
    fault) -> landlab_channel_incision_steady_state; channel steepness / chi on
    the CURRENT terrain -> landlab_channel_steepness_chi_map.

    Use this when: the user asks about a NORMAL FAULT, FAULT SCARP, TECTONIC
    forcing, FOOTWALL uplift / drainage, or landscape evolution under a fault
    throw for an AOI.

    Params:
        bbox: catchment AOI, EPSG:4326.
        fault_throw_rate_m_yr: footwall throw rate, m/yr (default 1e-3 = 1 mm/yr;
            0 is the no-fault control).
        fault_dip_deg: fault dip from horizontal, degrees (default 60).
        fault_position_frac: N-S position of the E-W fault trace as a fraction of
            the domain, [0, 1] (default 0.5 = mid-domain).
        k_bedrock: stream-power erodibility K (default 1e-5).
        hillslope_diffusivity_m2_yr: hillslope diffusivity degrading the scarp,
            m^2/yr (default 0.1).
        incision_run_duration_yr: total simulated time, yr (default 5e5).
        incision_n_timesteps: FastscapeEroder/NormalFault steps (default 400).
        target_resolution_m: grid cell size, m (default 90).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" reviews the demo forcing before
            the solve; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabNormalFaultLayerURI`` - the evolved-elevation COG
        (the scarp), with ``total_throw_m``, ``footwall_relief_m``,
        ``n_footwall_channel_nodes``, ``fault_throw_rate_m_yr``, ``fault_dip_deg``,
        ``run_duration_yr``. The cumulative-throw footwall raster is emitted
        alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_normal_fault_scarp_evolution requires a bbox "
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

    provenance: list[SyntheticInput] = [
        SyntheticInput(
            param="normal_fault_forcing",
            value=(
                f"throw_rate={fault_throw_rate_m_yr} m/yr, dip={fault_dip_deg} deg, "
                f"E-W trace at {fault_position_frac:g} of the domain, "
                f"K={k_bedrock}, T={incision_run_duration_yr:g} yr"
            ),
            basis="default_demo", consequence="scenario",
            real_source_if_any=None,
            note=(
                "the fault geometry + throw rate are a labeled demo scenario, not "
                "a mapped fault trace or a site-measured slip rate; the DEM "
                "terrain is real"
            ),
        ),
    ]
    source_note = (
        f"DEM: USGS 3DEP (fetched, REAL). Evolved under a DEMO normal-fault "
        f"forcing (throw_rate={fault_throw_rate_m_yr} m/yr, dip={fault_dip_deg} "
        f"deg, E-W trace at {fault_position_frac:g} of the domain, K={k_bedrock}); "
        "the footwall uplifts and the scarp degrades under stream-power + "
        "hillslope erosion."
    )

    _review = await gate_input_review(
        tool_name="landlab_normal_fault_scarp_evolution",
        mode=input_mode,
        entries=provenance,
        params={
            "fault_throw_rate_m_yr": fault_throw_rate_m_yr,
            "fault_dip_deg": fault_dip_deg,
            "target_resolution_m": target_resolution_m,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": (
                f"landlab_normal_fault_scarp_evolution {_review.cancel_reason}"
            ),
        }
    provenance = _review.entries
    fault_throw_rate_m_yr = float(
        _review.params.get("fault_throw_rate_m_yr", fault_throw_rate_m_yr)
    )
    fault_dip_deg = float(_review.params.get("fault_dip_deg", fault_dip_deg))
    target_resolution_m = float(
        _review.params.get("target_resolution_m", target_resolution_m)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="normal_fault",
            target_resolution_m=float(target_resolution_m),
            fault_throw_rate_m_yr=float(fault_throw_rate_m_yr),
            fault_dip_deg=float(fault_dip_deg),
            fault_position_frac=float(fault_position_frac),
            k_bedrock=float(k_bedrock),
            hillslope_diffusivity_m2_yr=float(hillslope_diffusivity_m2_yr),
            incision_run_duration_yr=float(incision_run_duration_yr),
            incision_n_timesteps=int(incision_n_timesteps),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab normal-fault arguments: {exc}",
        }

    logger.info(
        "landlab_normal_fault_scarp_evolution bbox=%s res=%.1fm throw=%.3e dip=%.0f "
        "K=%.3e T=%.0f steps=%d",
        run_args.bbox, run_args.target_resolution_m, fault_throw_rate_m_yr,
        fault_dip_deg, k_bedrock, incision_run_duration_yr, incision_n_timesteps,
    )

    try:
        primary = await model_landlab_normal_fault(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_normal_fault_scarp_evolution complete layer_id=%s "
            "total_throw=%.1f footwall_relief=%.1f n_fw_chan=%d uri=%s",
            primary.layer_id, primary.total_throw_m, primary.footwall_relief_m,
            primary.n_footwall_channel_nodes, primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        NormalFaultWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_normal_fault_scarp_evolution failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_normal_fault_scarp_evolution unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_normal_fault(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabNormalFaultLayerURI:
    """Compose the normal-fault chain end-to-end (OFF-BOX lane).

    Returns the evolved-elevation ``LandlabNormalFaultLayerURI`` (the scarp);
    emits the cumulative-throw footwall context raster as a side effect.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )
    throw_cog = (solve.secondary_cogs or {}).get("fault_throw")

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, _metrics = await asyncio.to_thread(
                postprocess_landlab_normal_fault,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                throw_cog_path=throw_cog,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise NormalFaultWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_normal_fault produced no evolved-elevation layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer,
            raw_primary,
            default_style=EVOLVED_ELEVATION_STYLE_PRESET,
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
            published = await asyncio.to_thread(
                publish_raster_layer,
                ctx,
                default_style=FAULT_THROW_STYLE_PRESET,
            )
            try:
                await emitter.add_loaded_layer(published)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add fault-throw context layer: %s", exc)

    await emit_zoom_to(emitter, solve.bbox)
    return primary
