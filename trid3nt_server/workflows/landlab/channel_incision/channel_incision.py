"""Engine template ``landlab_channel_incision_steady_state`` - detachment-limited
stream-power channel incision to steady state with the analytical slope-area V&V.

A distinct question CLASS (per the capability-naming rule): evolve this catchment
to a steady-state channel profile under a rock-uplift + erodibility forcing and
check whether the resulting channel slope-area relation matches the analytical
stream-power prediction. It is its OWN registered engine TEMPLATE (engine="landlab",
tier="template").

``landlab_channel_incision_steady_state(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain: FastscapeEroder (E = K A^m S^n) + rock uplift
stepped to steady state over the AOI DEM, and returns a
``LandlabChannelIncisionLayerURI`` (the evolved topography) plus the channel
steepness (ksn) raster and the slope-area log-log V&V chart (fitted vs analytical
concavity + K recovery). Landlab runs OFF-BOX in the local-exec / Batch solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from the
typed ``LandlabChannelIncisionLayerURI`` fields the worker / postprocess computed.
The DEM is REAL (USGS 3DEP); the uplift + erodibility are a LABELED demo scenario
(SyntheticInput) - the terrain is real, the forcing is a scenario.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    LandlabChannelIncisionLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_landlab_chart,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    CHANNEL_STEEPNESS_STYLE_PRESET,
    EVOLVED_ELEVATION_STYLE_PRESET,
    PostprocessLandlabError,
    build_slope_area_chart_spec,
    postprocess_landlab_channel_incision,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.channel_incision.channel_incision"
)

__all__ = [
    "landlab_channel_incision_steady_state",
    "model_landlab_channel_incision",
    "ChannelIncisionWorkflowError",
]


class ChannelIncisionWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "does the steady-state channel slope-area relation match the analytical "
        "stream-power prediction - evolve this catchment under uplift + erodibility "
        "to steady state and check S = (U/K)^(1/n) A^(-m/n) (Landlab FastscapeEroder; "
        "evolved topography + ksn raster + slope-area log-log V&V chart)"
    ),
    required_inputs=["bbox"],
    knobs="k_bedrock, m_sp, n_sp, uplift_rate_m_yr, incision_run_duration_yr, target_resolution_m",
)

_METADATA = AtomicToolMetadata(
    name="landlab_channel_incision_steady_state",
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
async def landlab_channel_incision_steady_state(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    k_bedrock: float = 1.0e-5,
    m_sp: float = 0.5,
    n_sp: float = 1.0,
    uplift_rate_m_yr: float = 1.0e-3,
    incision_run_duration_yr: float = 1.0e6,
    incision_n_timesteps: int = 500,
    target_resolution_m: float = 90.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabChannelIncisionLayerURI | dict[str, Any]:
    """Evolve a catchment to steady state (stream power) + check the slope-area law.

    Fidelity: Landlab FastscapeEroder detachment-limited stream power (E = K A^m
    S^n) + rock uplift on a real AOI DEM; a landscape-evolution V&V, not a
    calibrated forecast.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The uplift + erodibility are a
    LABELED demo scenario (the terrain is real, the forcing is a scenario).
    Off-scope: channel steepness / chi diagnostic on the CURRENT terrain (no
    evolution) -> landlab_channel_steepness_chi_map; drainage area / channel network
    -> landlab_flow_accumulation.

    Use this when: the user asks about CHANNEL INCISION, LANDSCAPE EVOLUTION, the
    stream-power law, steady-state channel PROFILES, the slope-area relation, or
    recovering erodibility K / concavity for an AOI.

    Params:
        bbox: catchment AOI, EPSG:4326.
        k_bedrock: stream-power erodibility K in E = K A^m S^n (default 1e-5).
        m_sp: drainage-area exponent m (default 0.5).
        n_sp: slope exponent n (default 1.0). Analytical concavity = m_sp/n_sp.
        uplift_rate_m_yr: rock uplift forcing, m/yr (default 1e-3).
        incision_run_duration_yr: total simulated time, yr (default 1e6).
        incision_n_timesteps: FastscapeEroder steps (default 500).
        target_resolution_m: grid cell size, m (default 90).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" reviews the demo forcing before
            the solve; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabChannelIncisionLayerURI`` - the evolved-elevation COG,
        with ``fitted_concavity``, ``analytical_concavity``, ``k_input``,
        ``k_recovered``, ``fit_r2``, ``n_channel_nodes``. The ksn raster + the
        slope-area log-log V&V chart are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_channel_incision_steady_state requires a bbox "
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
            param="uplift_erodibility_forcing",
            value=(
                f"U={uplift_rate_m_yr} m/yr, K={k_bedrock}, m={m_sp}, n={n_sp}, "
                f"T={incision_run_duration_yr:g} yr"
            ),
            basis="default_demo", consequence="physics",
            real_source_if_any=None,
            note=(
                "landscape-evolution forcing is a labeled demo scenario, not a "
                "site-measured uplift/erodibility; the DEM terrain is real"
            ),
        ),
    ]
    source_note = (
        f"DEM: USGS 3DEP (fetched, REAL). Evolved to steady state under a DEMO "
        f"stream-power forcing (U={uplift_rate_m_yr} m/yr, K={k_bedrock}, "
        f"m={m_sp}, n={n_sp}); the slope-area V&V recovers K + concavity from the "
        "steady state."
    )

    _review = await gate_input_review(
        tool_name="landlab_channel_incision_steady_state",
        mode=input_mode,
        entries=provenance,
        params={
            "k_bedrock": k_bedrock,
            "uplift_rate_m_yr": uplift_rate_m_yr,
            "target_resolution_m": target_resolution_m,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": (
                f"landlab_channel_incision_steady_state {_review.cancel_reason}"
            ),
        }
    provenance = _review.entries
    k_bedrock = float(_review.params.get("k_bedrock", k_bedrock))
    uplift_rate_m_yr = float(_review.params.get("uplift_rate_m_yr", uplift_rate_m_yr))
    target_resolution_m = float(
        _review.params.get("target_resolution_m", target_resolution_m)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="channel_incision",
            target_resolution_m=float(target_resolution_m),
            k_bedrock=float(k_bedrock),
            m_sp=float(m_sp),
            n_sp=float(n_sp),
            uplift_rate_m_yr=float(uplift_rate_m_yr),
            incision_run_duration_yr=float(incision_run_duration_yr),
            incision_n_timesteps=int(incision_n_timesteps),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab channel-incision arguments: {exc}",
        }

    logger.info(
        "landlab_channel_incision_steady_state bbox=%s res=%.1fm K=%.3e U=%.3e "
        "T=%.0f steps=%d",
        run_args.bbox, run_args.target_resolution_m, k_bedrock, uplift_rate_m_yr,
        incision_run_duration_yr, incision_n_timesteps,
    )

    try:
        primary = await model_landlab_channel_incision(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_channel_incision_steady_state complete layer_id=%s "
            "theta_fit=%.4f theta_an=%.4f K_rec=%.3e r2=%.3f uri=%s",
            primary.layer_id, primary.fitted_concavity, primary.analytical_concavity,
            primary.k_recovered, primary.fit_r2, primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        ChannelIncisionWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_channel_incision_steady_state failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_channel_incision_steady_state unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_channel_incision(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabChannelIncisionLayerURI:
    """Compose the channel-incision chain end-to-end (OFF-BOX lane).

    Returns the evolved-elevation ``LandlabChannelIncisionLayerURI``; emits the
    ksn context raster + the slope-area log-log V&V chart as side effects.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )
    ksn_cog = (solve.secondary_cogs or {}).get("channel_steepness")

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_channel_incision,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                ksn_cog_path=ksn_cog,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise ChannelIncisionWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_channel_incision produced no evolved-elevation layer",
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
                default_style=CHANNEL_STEEPNESS_STYLE_PRESET,
            )
            try:
                await emitter.add_loaded_layer(published)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add ksn context layer: %s", exc)

    await emit_landlab_chart(
        emitter,
        build_slope_area_chart_spec(
            metrics.get("scatter") or [],
            k_input=float(metrics.get("k_input", primary.k_input)),
            k_recovered=float(metrics.get("k_recovered", primary.k_recovered)),
            uplift_rate_m_yr=float(metrics.get("uplift_rate_m_yr", primary.uplift_rate_m_yr)),
            m_sp=float(metrics.get("m_sp", 0.5)),
            n_sp=float(metrics.get("n_sp", 1.0)),
            fit_r2=float(metrics.get("fit_r2", primary.fit_r2)),
        ),
        title="Slope-area steady-state V&V (stream power)",
        caption=(
            "Channel slope vs drainage area on log-log axes with the analytical "
            "S = (U/K)^(1/n) A^(-m/n) line at the recovered K - the fitted concavity "
            f"{primary.fitted_concavity:.3f} vs analytical {primary.analytical_concavity:.3f}, "
            f"K recovered {primary.k_recovered:.2e} vs input {primary.k_input:.2e} "
            f"(R^2={primary.fit_r2:.3f})."
        ),
        source_uri=primary.uri,
    )
    await emit_zoom_to(emitter, solve.bbox)
    return primary
