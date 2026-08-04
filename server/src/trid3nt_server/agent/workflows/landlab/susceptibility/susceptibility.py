"""Engine template ``landlab_susceptibility`` - Landlab surface-process engine
(engine-door refactor - LANDLAB slice; was ``run_landlab_susceptibility``).

The LLM-facing exposure of the Landlab (CSDMS, MIT) surface-process engine: a
hazard CLASS SFINCS/SWMM do not cover (landslide susceptibility /
factor-of-safety + rainfall overland flow). ``landlab_susceptibility(...)`` takes
the ``LandlabRunArgs`` forcing/structure fields, runs the deterministic fetch ->
stage -> Batch-solve -> postprocess chain
(``workflows/landlab/model_landslide_scenario/``), and returns a
``LandlabSusceptibilityLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the Landlab analogue of ``swmm_urban_flood`` (SWMM),
``modflow_contaminant_plume`` (MODFLOW), ``sfincs_flood`` (SFINCS) and
``geoclaw_inundation`` (GeoClaw). It is a registered engine TEMPLATE tagged
``engine="landlab", tier="template"`` - EXCLUDED from the default retrieval pool
and surfaced only by the ``run_landlab`` door's gate expansion
(SELECT-THEN-CALL). Like the other templates it declares ``cacheable=False`` +
``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (FR-DC-6 -
workflow exposure surface; never touches the cache shim).

Landlab runs OFF-BOX ONLY in a local Docker solver container (the same
scale-to-zero local-docker seam every engine dispatches through) via
``run_solver``.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabSusceptibilityLayerURI.unstable_area_fraction`` /
``.min_factor_of_safety`` / ``.mean_probability_of_failure`` fields the worker /
postprocess computed - never free-generated.
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

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.workflows.landlab._template_card import TemplateCard
from trid3nt_server.agent.workflows.landlab.model_landslide_scenario.model_landslide_scenario import (
    LandslideWorkflowError,
    model_landslide_scenario,
)
from trid3nt_server.agent.workflows.landlab.postprocess_landlab import PostprocessLandlabError
from trid3nt_server.agent.workflows.landlab.run_landlab import LandlabWorkflowError

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.landlab.susceptibility.susceptibility"
)

__all__ = ["landlab_susceptibility", "RunLandlabError"]


class RunLandlabError(RuntimeError):
    """Raised when the Landlab chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` propagated from the failing stage so the
    agent emitter renders a typed error frame (the emitter's
    ``_classify_exception`` reads ``error_code`` off the exception)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Default design-storm return period (years) for the triggering rainfall when
#: the caller does not pass one. A 100-yr storm is the canonical triggering
#: scenario for a planning-grade rainfall-induced-landslide susceptibility map.
_DEFAULT_RAINFALL_RETURN_PERIOD_YR: int = 100
#: Default storm duration (hours) for the Atlas-14 lookup, matching the contract
#: OverlandFlow default (``DEFAULT_STORM_DURATION_HR``).
_DEFAULT_TRIGGER_DURATION_HR: float = 2.0
_INCH_TO_MM: float = 25.4


def _atlas14_design_storm_mm(
    bbox: tuple[float, float, float, float],
    return_period_yr: int,
    duration_hr: float,
) -> float | None:
    """Look up the NOAA Atlas-14 design-storm depth (mm) at the AOI centroid.

    Seam-1: resolves ``lookup_precip_return_period`` via ``TOOL_REGISTRY`` (never
    a module internal). Returns the total storm depth in mm, or ``None`` on lookup
    failure (the caller then raises a typed rainfall gate - never a silent baked
    default)."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["lookup_precip_return_period"].fn
    lat = 0.5 * (bbox[1] + bbox[3])
    lon = 0.5 * (bbox[0] + bbox[2])
    try:
        result = fn(
            location=(lat, lon),
            return_period_years=int(return_period_yr),
            duration_hours=float(duration_hr),
        )
    except Exception as exc:  # noqa: BLE001 - signalled up as a typed gate
        logger.info(
            "landlab: lookup_precip_return_period failed (%s); will gate on the "
            "missing triggering rainfall", exc
        )
        return None
    inches = result.get("precip_inches") if isinstance(result, dict) else None
    if inches is None:
        return None
    return float(inches) * _INCH_TO_MM


#: Curated door-listing card (the run_landlab door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "landslide susceptibility (infinite-slope factor-of-safety / "
        "probability-of-failure raster) OR rainfall OVERLAND-FLOW surface-runoff "
        "depth over a hillslope / catchment (Landlab component grids)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "analysis (landslide_probability / overland_flow), target_resolution_m, "
        "soil_transmissivity_m2_day, soil_cohesion_pa, soil_internal_friction_deg, "
        "soil_density_kg_m3, soil_thickness_m, recharge_mm_day, n_monte_carlo, "
        "rainfall_intensity_mm_hr, storm_duration_hr, rainfall_return_period_yr"
    ),
)


_LANDLAB_SUSCEPTIBILITY_METADATA = AtomicToolMetadata(
    name="landlab_susceptibility",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="landlab",
    tier="template",
)


@register_tool(
    _LANDLAB_SUSCEPTIBILITY_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (AWS Batch + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def landlab_susceptibility(
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
    rainfall_return_period_yr: int = 100,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LandlabSusceptibilityLayerURI | dict[str, Any]:
    """Run a Landlab surface-process simulation over an AOI (landslide susceptibility or overland flow).

    Fidelity: Landlab infinite-slope Monte-Carlo landslide susceptibility;
    planning-grade hillslope envelope, not a site-calibrated geotechnical model.
    Data: the TRIGGERING RAINFALL is sourced from the NOAA Atlas-14 design storm
    for the AOI (``rainfall_return_period_yr`` / ``storm_duration_hr``) - the
    landslide chain's ``recharge_mm_day`` and the overland-flow chain's
    ``rainfall_intensity_mm_hr`` are DERIVED from it when unset; a failed lookup
    STOPS with a typed ``LANDLAB_RAINFALL_INPUT_REQUIRED`` gate (never a baked
    default). The SOIL block (cohesion / friction / density / thickness /
    transmissivity) STAYS demo-defaulted - there is no SSURGO/POLARIS soil fetcher
    yet - and is labeled as such in ``source_note``.
    Off-scope: channel / riverine / coastal inundation -> sfincs_flood; post-fire
    debris-flow over a burn scar -> model_debris_flow; probabilistic seismic
    hazard -> openquake_psha.

    Use this when: the user wants LANDSLIDE susceptibility/slope stability/factor
    of safety over a hillslope or catchment (default
    ``analysis="landslide_probability"``, infinite-slope Monte-Carlo FoS), or
    rainfall OVERLAND FLOW/surface runoff (``analysis="overland_flow"``, de
    Almeida shallow-water). Do NOT use for: riverine/coastal flooding
    (``sfincs_flood``); urban/pluvial (``swmm_urban_flood``); post-fire
    debris-flow hazard (``model_debris_flow``); groundwater plumes
    (``modflow_contaminant_plume``).

    Params:
        bbox: hillslope/small-catchment AOI, EPSG:4326.
        analysis: ``"landslide_probability"`` (default) or
            ``"overland_flow"``; common synonyms normalized.
        target_resolution_m: grid cell size (default 30).
        soil_transmissivity_m2_day/soil_cohesion_pa/
            soil_internal_friction_deg/soil_density_kg_m3/
            soil_thickness_m/n_monte_carlo: optional LandslideProbability
            SOIL params; unset uses demo defaults (not site-calibrated;
            no SSURGO/POLARIS fetcher yet - labeled in source_note).
        recharge_mm_day: LandslideProbability triggering recharge, mm/day.
            Unset -> DERIVED from the Atlas-14 design storm (mean intensity
            of the storm expressed as mm/day). Explicit value overrides.
        rainfall_intensity_mm_hr: OverlandFlow rainfall intensity, mm/hr.
            Unset -> DERIVED from the Atlas-14 design storm
            (depth / storm_duration_hr). Explicit value overrides.
        storm_duration_hr: design-storm / overland duration, hours
            (default 2); also the Atlas-14 lookup duration.
        rainfall_return_period_yr: design-storm return period (years) for
            the Atlas-14 triggering-rainfall lookup (default 100).
        compute_class: compute class (default "standard").

    Returns:
        On success: ``LandlabSusceptibilityLayerURI`` -- susceptibility/
        FoS/depth COG, with ``unstable_area_fraction``,
        ``min_factor_of_safety``, ``mean_probability_of_failure``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` -- cache shim not invoked.
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_susceptibility requires a bbox "
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

    # --- Triggering rainfall: real NOAA Atlas-14 design storm, or a typed gate ---
    # The overland-flow chain's rainfall_intensity and the landslide chain's
    # groundwater recharge are the TRIGGERING forcing. Source them from the
    # Atlas-14 design storm for the AOI when unset; a failed lookup STOPS with a
    # typed gate naming the manual param (never a baked default). The SOIL block
    # stays demo-defaulted (no SSURGO/POLARIS fetcher yet) and is labeled below.
    _is_overland = "overland" in str(analysis).lower()
    _dur_hr = float(storm_duration_hr) if storm_duration_hr is not None else _DEFAULT_TRIGGER_DURATION_HR
    _need_rainfall = (
        (_is_overland and rainfall_intensity_mm_hr is None)
        or (not _is_overland and recharge_mm_day is None)
    )
    source_note: str | None = None
    _rainfall_label = ""
    if _need_rainfall:
        _depth_mm = await asyncio.to_thread(
            _atlas14_design_storm_mm, tuple(coerced), int(rainfall_return_period_yr), _dur_hr
        )
        if _depth_mm is None:
            _param = "rainfall_intensity_mm_hr" if _is_overland else "recharge_mm_day"
            return {
                "status": "error",
                "error_code": "LANDLAB_RAINFALL_INPUT_REQUIRED",
                "error_message": (
                    f"The NOAA Atlas-14 design-storm lookup failed for this AOI "
                    f"({rainfall_return_period_yr}-yr / {_dur_hr:.0f}-hr), so the "
                    f"triggering rainfall is not fabricated. Retry with an explicit "
                    f"{_param} - or an AOI within Atlas-14 coverage (CONUS / PR / USVI)."
                ),
            }
        if _is_overland:
            # Mean design-storm intensity (mm/hr) drives the OverlandFlow forcing.
            rainfall_intensity_mm_hr = round(_depth_mm / _dur_hr, 2)
            _rainfall_label = (
                f"overland rainfall intensity {rainfall_intensity_mm_hr:.1f} mm/hr "
                f"(NOAA Atlas-14 {rainfall_return_period_yr}-yr/{_dur_hr:.0f}-hr design storm, "
                f"{_depth_mm:.1f} mm total)"
            )
        else:
            # The design-storm TOTAL depth as a single-day triggering recharge
            # pulse (mm/day) - NOT a burst intensity (that would over-saturate the
            # steady-state wetness index). A defensible triggering-scenario proxy;
            # the fitted long-term / event recharge source (gridMET pr, SSURGO Ksat)
            # is a future fetcher.
            recharge_mm_day = round(_depth_mm, 1)
            _rainfall_label = (
                f"triggering recharge {recharge_mm_day:.0f} mm/day (NOAA Atlas-14 "
                f"{rainfall_return_period_yr}-yr/{_dur_hr:.0f}-hr design-storm total "
                f"{_depth_mm:.1f} mm as a 1-day pulse)"
            )
    else:
        _rainfall_label = "triggering rainfall: user-supplied"
    source_note = (
        _rainfall_label
        + "; SOIL properties (cohesion / friction / density / thickness / "
        "transmissivity) are demo defaults - no SSURGO/POLARIS soil fetcher yet, "
        "not site-calibrated."
    )

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
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab run arguments: {exc}",
        }

    logger.info(
        "landlab_susceptibility bbox=%s analysis=%s res=%.1fm",
        run_args.bbox,
        run_args.analysis,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landslide_scenario(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
        )
        logger.info(
            "landlab_susceptibility complete layer_id=%s unstable_frac=%.4g "
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
            "landlab_susceptibility failed: %s (%s)", exc.error_code, exc
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("landlab_susceptibility unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }
