"""Atomic tool ``run_landlab_susceptibility`` — Landlab surface-process engine
(sprint-17 — NEW engine).

The LLM-facing exposure of the Landlab (CSDMS, MIT) surface-process engine: a
hazard CLASS previously absent here (landslide susceptibility / factor-of-safety + rainfall
overland flow). ``run_landlab_susceptibility(...)`` takes the ``LandlabRunArgs``
forcing/structure fields, runs the deterministic fetch -> stage -> Batch-solve ->
postprocess chain (``workflows/model_landslide_scenario.py``), and returns a
``LandlabSusceptibilityLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the Landlab analogue of ``run_swmm_urban_flood`` (SWMM),
``run_modflow_job`` (MODFLOW) and ``run_model_flood_scenario`` (SFINCS). Like
those wrappers it declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 — workflow exposure surface; never
touches the cache shim). Confirmation before consequence (Invariant 9 — a solver
run) is enforced by the server confirmation hook around this tool, not
re-implemented here.

Landlab runs OFF-BOX ONLY on AWS Batch (the scale-to-zero island norm) — it is
inert until the orchestrator wires SOLVER_WORKFLOW_REGISTRY["landlab"] +
TRID3NT_AWS_BATCH_JOB_DEF_LANDLAB (the shared-append snippets this lane returns).

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabSusceptibilityLayerURI.unstable_area_fraction`` /
``.min_factor_of_safety`` / ``.mean_probability_of_failure`` fields the worker /
postprocess computed — never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.landlab_contracts import (
    LandlabRunArgs,
    LandlabSusceptibilityLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.model_landslide_scenario import (
    LandslideWorkflowError,
    model_landslide_scenario,
)
from trid3nt_server.workflows.postprocess_landlab import PostprocessLandlabError
from trid3nt_server.workflows.run_landlab import LandlabWorkflowError

logger = logging.getLogger("trid3nt_server.tools.simulation.run_landlab_tool")

__all__ = ["run_landlab_susceptibility", "RunLandlabError"]


class RunLandlabError(RuntimeError):
    """Raised when the Landlab chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` propagated from the failing stage so the
    agent emitter renders a typed error frame (the emitter's
    ``_classify_exception`` reads ``error_code`` off the exception)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_RUN_LANDLAB_METADATA = AtomicToolMetadata(
    name="run_landlab_susceptibility",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_LANDLAB_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (AWS Batch + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_landlab_susceptibility(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    analysis: str = "landslide_probability",
    target_resolution_m: float = 30.0,
    soil_transmissivity_m2_day: float | None = None,
    soil_cohesion_pa: float | None = None,
    soil_internal_friction_deg: float | None = None,
    soil_density_kg_m3: float | None = None,
    soil_thickness_m: float | None = None,
    recharge_mm_day: float | None = None,
    n_monte_carlo: int | None = None,
    rainfall_intensity_mm_hr: float | None = None,
    storm_duration_hr: float | None = None,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LandlabSusceptibilityLayerURI | dict[str, Any]:
    """Run a Landlab surface-process simulation over an AOI (landslide susceptibility or overland flow).

    Use this when: the user wants LANDSLIDE susceptibility/slope
    stability/factor of safety over a hillslope or catchment (default
    ``analysis="landslide_probability"``, infinite-slope Monte-Carlo
    FoS), or rainfall OVERLAND FLOW/surface runoff
    (``analysis="overland_flow"``, de Almeida shallow-water). Do NOT use
    for: riverine/coastal flooding (``run_model_flood_scenario`` --
    SFINCS) or urban/pluvial (``run_swmm_urban_flood``); groundwater
    plumes (``run_modflow_job``).

    Params:
        bbox: hillslope/small-catchment AOI, EPSG:4326.
        analysis: ``"landslide_probability"`` (default) or
            ``"overland_flow"``; common synonyms normalized.
        target_resolution_m: grid cell size (default 30).
        soil_transmissivity_m2_day/soil_cohesion_pa/
            soil_internal_friction_deg/soil_density_kg_m3/
            soil_thickness_m/recharge_mm_day/n_monte_carlo: optional
            LandslideProbability soil params; unset uses noted demo
            defaults (not site-calibrated).
        rainfall_intensity_mm_hr/storm_duration_hr: optional OverlandFlow
            rainfall params; unset uses demo defaults.
        compute_class: compute class (default "standard").

    Returns:
        On success: ``LandlabSusceptibilityLayerURI`` -- susceptibility/
        FoS/depth COG, with ``unstable_area_fraction``,
        ``min_factor_of_safety``, ``mean_probability_of_failure``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "run_landlab_susceptibility requires a bbox "
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
    try:
        kwargs: dict[str, Any] = dict(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis=analysis,
            target_resolution_m=float(target_resolution_m),
        )
        if soil_transmissivity_m2_day is not None:
            kwargs["soil_transmissivity_m2_day"] = float(soil_transmissivity_m2_day)
        if soil_cohesion_pa is not None:
            kwargs["soil_cohesion_pa"] = float(soil_cohesion_pa)
        if soil_internal_friction_deg is not None:
            kwargs["soil_internal_friction_deg"] = float(soil_internal_friction_deg)
        if soil_density_kg_m3 is not None:
            kwargs["soil_density_kg_m3"] = float(soil_density_kg_m3)
        if soil_thickness_m is not None:
            kwargs["soil_thickness_m"] = float(soil_thickness_m)
        if recharge_mm_day is not None:
            kwargs["recharge_mm_day"] = float(recharge_mm_day)
        if n_monte_carlo is not None:
            kwargs["n_monte_carlo"] = int(n_monte_carlo)
        if rainfall_intensity_mm_hr is not None:
            kwargs["rainfall_intensity_mm_hr"] = float(rainfall_intensity_mm_hr)
        if storm_duration_hr is not None:
            kwargs["storm_duration_hr"] = float(storm_duration_hr)
        run_args = LandlabRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab run arguments: {exc}",
        }

    logger.info(
        "run_landlab_susceptibility bbox=%s analysis=%s res=%.1fm",
        run_args.bbox,
        run_args.analysis,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landslide_scenario(
            run_args,
            compute_class=compute_class,
        )
        logger.info(
            "run_landlab_susceptibility complete layer_id=%s unstable_frac=%.4g "
            "min_fos=%.4g mean_pof=%.4g uri=%s",
            primary.layer_id,
            primary.unstable_area_fraction,
            primary.min_factor_of_safety,
            primary.mean_probability_of_failure,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (LandlabWorkflowError, PostprocessLandlabError, LandslideWorkflowError) as exc:
        logger.warning(
            "run_landlab_susceptibility failed: %s (%s)", exc.error_code, exc
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        logger.exception("run_landlab_susceptibility unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }
