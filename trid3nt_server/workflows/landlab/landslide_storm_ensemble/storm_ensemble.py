"""Engine template ``landlab_landslide_storm_ensemble`` - Landlab infinite-slope
landslide susceptibility swept across a storm/recharge ensemble.

A distinct question CLASS from ``landlab_susceptibility`` (per the capability-
naming rule): instead of one fixed daily recharge, how does the failure-
probability map and the unstable-area fraction GROW as the triggering rainfall
varies across a realistic sequence of storm/recharge draws? It is its OWN
registered engine TEMPLATE (engine="landlab", tier="template").

``landlab_landslide_storm_ensemble(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain: the Monte-Carlo LandslideProbability
component is run once per recharge scenario (scenarios drawn from a Landlab
PrecipitationDistribution), and returns a ``LandlabStormEnsembleLayerURI`` (the
ensemble-mean probability-of-failure raster) plus a susceptibility-vs-recharge
sensitivity chart. Landlab runs OFF-BOX in the local-exec / Batch solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabStormEnsembleLayerURI`` fields the worker / postprocess
computed. The DEM is REAL; the storm generator parameters + soil block are demo
defaults (no SSURGO/POLARIS fetcher yet) and are labeled in source_note.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_MEAN_INTERSTORM_DURATION_HR,
    DEFAULT_MEAN_STORM_DEPTH_MM,
    DEFAULT_MEAN_STORM_DURATION_HR,
    DEFAULT_N_RECHARGE_SCENARIOS,
    LandlabRunArgs,
    LandlabStormEnsembleLayerURI,
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
    LANDSLIDE_STYLE_PRESET,
    PostprocessLandlabError,
    build_storm_ensemble_chart_spec,
    postprocess_landlab_storm_ensemble,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.landslide_storm_ensemble.storm_ensemble"
)

__all__ = [
    "landlab_landslide_storm_ensemble",
    "model_landlab_landslide_storm_ensemble",
    "StormEnsembleWorkflowError",
]


class StormEnsembleWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "how landslide susceptibility (infinite-slope probability of failure) "
        "grows with rainfall variability - the failure-probability map swept "
        "across a storm/recharge ensemble + a susceptibility-vs-recharge chart"
    ),
    required_inputs=["bbox"],
    knobs=(
        "mean_storm_duration_hr, mean_interstorm_duration_hr, mean_storm_depth_mm, "
        "n_recharge_scenarios, n_monte_carlo, target_resolution_m"
    ),
)

_METADATA = AtomicToolMetadata(
    name="landlab_landslide_storm_ensemble",
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
async def landlab_landslide_storm_ensemble(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    mean_storm_duration_hr: float = DEFAULT_MEAN_STORM_DURATION_HR,
    mean_interstorm_duration_hr: float = DEFAULT_MEAN_INTERSTORM_DURATION_HR,
    mean_storm_depth_mm: float = DEFAULT_MEAN_STORM_DEPTH_MM,
    n_recharge_scenarios: int = DEFAULT_N_RECHARGE_SCENARIOS,
    n_monte_carlo: int | None = None,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabStormEnsembleLayerURI | dict[str, Any]:
    """Sweep landslide susceptibility across a storm/recharge ensemble over a DEM.

    Fidelity: Landlab infinite-slope Monte-Carlo LandslideProbability run once per
    recharge scenario; a planning-grade sensitivity envelope, not a site-calibrated
    geotechnical model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The recharge scenarios are drawn
    from a Landlab PrecipitationDistribution (Poisson storm generator) whose means
    are demo defaults; the SOIL block (cohesion / friction / density / thickness /
    transmissivity) is a demo default (no SSURGO/POLARIS fetcher yet) - labeled in
    source_note.
    Off-scope: single-recharge susceptibility -> landlab_susceptibility; drainage
    area / channel network -> landlab_flow_accumulation; storm infiltration /
    runoff partition -> landlab_green_ampt_overland_flow.

    Use this when: the user asks how landslide susceptibility CHANGES with rainfall
    variability, for a storm/recharge SENSITIVITY sweep, or an ensemble
    failure-probability map (not a single fixed-recharge solve).

    Params:
        bbox: hillslope / catchment AOI, EPSG:4326.
        mean_storm_duration_hr: Poisson mean storm duration, hours (default 2).
        mean_interstorm_duration_hr: Poisson mean interstorm duration, hours
            (default 48).
        mean_storm_depth_mm: Poisson mean storm depth, mm; each drawn depth is a
            recharge scenario, mm/day (default 15).
        n_recharge_scenarios: number of recharge scenarios swept (default 8).
        n_monte_carlo: inner Monte-Carlo draws per scenario (unset = demo default).
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the storm-generator +
            demo soil block for review before the solve; "auto" (default) proceeds
            with them labeled.

    Returns:
        On success: ``LandlabStormEnsembleLayerURI`` - the ensemble-mean
        probability COG, with ``unstable_area_fraction``,
        ``mean_probability_of_failure``, ``min_recharge_mm_day``,
        ``max_recharge_mm_day``, ``sensitivity_slope``. A susceptibility-vs-recharge
        chart is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_landslide_storm_ensemble requires a bbox "
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
            param="recharge_scenarios",
            value=(
                f"{n_recharge_scenarios} draws, mean depth {mean_storm_depth_mm} mm"
            ),
            basis="default_demo", consequence="scenario",
            real_source_if_any="landlab PrecipitationDistribution (Poisson)",
            note="storm-generator means are demo defaults, not a fitted local climate",
        ),
        SyntheticInput(
            param="soil_properties",
            value="cohesion/friction/density/thickness/transmissivity",
            basis="default_demo", consequence="physics",
            note="no SSURGO/POLARIS soil fetcher yet; not site-calibrated",
        ),
    ]
    source_note = (
        f"recharge ensemble: {n_recharge_scenarios} scenarios drawn from a Poisson "
        f"storm generator (mean depth {mean_storm_depth_mm} mm) - demo climate means; "
        "SOIL block is a demo default (no SSURGO/POLARIS fetcher yet), not "
        "site-calibrated."
    )

    _review = await gate_input_review(
        tool_name="landlab_landslide_storm_ensemble",
        mode=input_mode,
        entries=provenance,
        params={
            "mean_storm_depth_mm": mean_storm_depth_mm,
            "n_recharge_scenarios": n_recharge_scenarios,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_landslide_storm_ensemble {_review.cancel_reason}",
        }
    provenance = _review.entries
    mean_storm_depth_mm = float(
        _review.params.get("mean_storm_depth_mm", mean_storm_depth_mm)
    )
    n_recharge_scenarios = int(
        _review.params.get("n_recharge_scenarios", n_recharge_scenarios)
    )

    try:
        kwargs: dict[str, Any] = dict(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="landslide_storm_ensemble",
            target_resolution_m=float(target_resolution_m),
            mean_storm_duration_hr=float(mean_storm_duration_hr),
            mean_interstorm_duration_hr=float(mean_interstorm_duration_hr),
            mean_storm_depth_mm=float(mean_storm_depth_mm),
            n_recharge_scenarios=int(n_recharge_scenarios),
        )
        if n_monte_carlo is not None:
            kwargs["n_monte_carlo"] = int(n_monte_carlo)
        run_args = LandlabRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab storm-ensemble arguments: {exc}",
        }

    logger.info(
        "landlab_landslide_storm_ensemble bbox=%s n_scenarios=%d mean_depth=%.1fmm res=%.1fm",
        run_args.bbox,
        n_recharge_scenarios,
        mean_storm_depth_mm,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_landslide_storm_ensemble(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_landslide_storm_ensemble complete layer_id=%s unstable=%.4f "
            "recharge=[%.1f,%.1f] slope=%.5f uri=%s",
            primary.layer_id,
            primary.unstable_area_fraction,
            primary.min_recharge_mm_day,
            primary.max_recharge_mm_day,
            primary.sensitivity_slope,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        StormEnsembleWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_landslide_storm_ensemble failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_landslide_storm_ensemble unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_landslide_storm_ensemble(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabStormEnsembleLayerURI:
    """Compose the storm-ensemble landslide chain end-to-end (OFF-BOX lane).

    Returns the ensemble-mean probability ``LandlabStormEnsembleLayerURI``; emits
    the susceptibility-vs-recharge chart as a side effect on the bound emitter.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_storm_ensemble,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise StormEnsembleWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_storm_ensemble produced no probability layer",
        )

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, layers[0], default_style=LANDSLIDE_STYLE_PRESET
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

    await emit_landlab_chart(
        emitter,
        build_storm_ensemble_chart_spec(metrics.get("recharge_scenarios") or []),
        title="Susceptibility vs recharge (storm ensemble)",
        caption=(
            "How the unstable-area fraction grows across the swept recharge "
            "scenarios - landslide susceptibility under rainfall variability."
        ),
        source_uri=primary.uri,
    )
    await emit_zoom_to(emitter, solve.bbox)
    return primary
