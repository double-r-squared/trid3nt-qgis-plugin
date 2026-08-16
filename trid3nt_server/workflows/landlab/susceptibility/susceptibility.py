"""Engine template ``landlab_susceptibility`` - Landlab surface-process engine
(engine-door refactor - LANDLAB slice; was ``run_landlab_susceptibility``).

The LLM-facing exposure of the Landlab (CSDMS, MIT) surface-process engine: a
hazard CLASS SFINCS/SWMM do not cover (landslide susceptibility /
factor-of-safety + rainfall overland flow). ``landlab_susceptibility(...)`` takes
the ``LandlabRunArgs`` forcing/structure fields, runs the deterministic fetch ->
stage -> Batch-solve -> postprocess chain
(``model_landlab_susceptibility`` below, in this module), and returns a
``LandlabSusceptibilityLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the Landlab analogue of ``swmm_urban_flood`` (SWMM),
``modflow_contaminant_plume`` (MODFLOW), ``sfincs_flood`` (SFINCS) and
``geoclaw_inundation`` (GeoClaw). It is a registered engine TEMPLATE tagged
``engine="landlab", tier="template"`` - EXCLUDED from the default retrieval pool
and surfaced only by the ``run_landlab`` door's gate expansion
(SELECT-THEN-CALL). Like the other templates it declares ``cacheable=False`` +
``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (
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
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    LandlabRunArgs,
    LandlabSusceptibilityLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data.publish_layer.publish_layer import PublishLayerError, publish_layer
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.run_landlab import LANDLAB_RES_SPEC
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    LANDSLIDE_STYLE_PRESET,
    PostprocessLandlabError,
    postprocess_landlab,
)
from trid3nt_server.workflows.landlab.run_landlab import (
    LANDLAB_SOLVER_NAME,
    LandlabStaging,
    LandlabWorkflowError,
    stage_landlab_manifest,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.susceptibility.susceptibility"
)

__all__ = [
    "landlab_susceptibility",
    "RunLandlabError",
    "model_landlab_susceptibility",
    "LandslideWorkflowError",
]


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
    from trid3nt_server.data import TOOL_REGISTRY

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
    resolution_specs=(LANDLAB_RES_SPEC,),
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
    input_mode: str | None = None,
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
        input_mode: run-mode lever. ``"user_gated"`` presents the
            resolved triggering rainfall + demo soil block for review before
            the solve; ``"auto"`` (default) proceeds with them labeled.

    Returns:
        On success: ``LandlabSusceptibilityLayerURI`` -- susceptibility/
        FoS/depth COG, with ``unstable_area_fraction``,
        ``min_factor_of_safety``, ``mean_probability_of_failure``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).

    ``cacheable=False``, ``ttl_class="live-no-cache"``,
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

    # provenance-chain wave: the same rainfall-vs-soil provenance as STRUCTURE, so
    # the narration seam renders it uniformly (source_note kept as the human prose
    # line; the structured list is now the machine-readable source of truth).
    provenance: list[SyntheticInput] = []
    if _need_rainfall:
        if _is_overland:
            provenance.append(SyntheticInput(
                param="rainfall_intensity_mm_hr", value=rainfall_intensity_mm_hr,
                units="mm/hr", basis="derived",
                real_source_if_any="lookup_precip_return_period (NOAA Atlas-14)",
                note=f"{rainfall_return_period_yr}-yr/{_dur_hr:.0f}-hr design storm",
            ))
        else:
            provenance.append(SyntheticInput(
                param="recharge_mm_day", value=recharge_mm_day, units="mm/day",
                basis="derived",
                real_source_if_any="lookup_precip_return_period (NOAA Atlas-14)",
                note="design-storm total as a 1-day triggering pulse",
            ))
    else:
        provenance.append(SyntheticInput(
            param=("rainfall_intensity_mm_hr" if _is_overland else "recharge_mm_day"),
            basis="user",
        ))
    _soil_defaulted = [
        n for n, v in (
            ("cohesion", soil_cohesion_pa),
            ("friction", soil_internal_friction_deg),
            ("density", soil_density_kg_m3),
            ("thickness", soil_thickness_m),
            ("transmissivity", soil_transmissivity_m2_day),
        ) if v is None
    ]
    if _soil_defaulted:
        provenance.append(SyntheticInput(
            param="soil_properties", value="/".join(_soil_defaulted),
            basis="default_demo",
            note="no SSURGO/POLARIS soil fetcher yet; not site-calibrated",
        ))

    # --- two-mode input gate: review-before-run -----------------------
    # The triggering forcing (Atlas-14 rainfall/recharge) + demo soil block are
    # resolved; user_gated mode presents them for review/adjust before the solve.
    _review = await gate_input_review(
        tool_name="landlab_susceptibility",
        mode=input_mode,
        entries=provenance,
        params={
            "rainfall_intensity_mm_hr": rainfall_intensity_mm_hr,
            "recharge_mm_day": recharge_mm_day,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_susceptibility {_review.cancel_reason}",
        }
    provenance = _review.entries
    _rv_rain = _review.params.get("rainfall_intensity_mm_hr", rainfall_intensity_mm_hr)
    _rv_rech = _review.params.get("recharge_mm_day", recharge_mm_day)
    rainfall_intensity_mm_hr = float(_rv_rain) if _rv_rain is not None else None
    recharge_mm_day = float(_rv_rech) if _rv_rech is not None else None

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
        primary = await model_landlab_susceptibility(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
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


# --------------------------------------------------------------------------- #
# The composer.
# A deterministic orchestrator-style workflow (Invariant 2 - no LLM in the
# chain) that composes the Landlab surface-process engine end-to-end:
#
#     fetch DEM (fetch_3dep_extra 1 m -> fetch_dem 10 m fallback)
#       -> stage_landlab_manifest (DEM COG + build_spec -> S3)
#       -> run_solver('landlab')  (AWS Batch - the scale-to-zero island)
#       -> wait_for_completion    (the shared S3 completion poll)
#       -> download the field COG + read the worker's typed `result` block
#       -> postprocess_landlab    (field COG -> EPSG:4326 susceptibility COG)
#       -> publish the primary COG through publish_layer (the render
#          chokepoint).
#
# Returns the primary ``LandlabSusceptibilityLayerURI`` directly (a
# ``LayerURI`` subtype) so the ``emit_tool_call`` ``add_loaded_layer`` gate
# fires on it - exactly like ``modflow_contaminant_plume`` returns a
# ``PlumeLayerURI`` and ``swmm_urban_flood`` returns a ``SWMMDepthLayerURI``.
#
# Determinism boundary (Invariant 1): every number the agent narrates comes
# from the typed ``LandlabSusceptibilityLayerURI.unstable_area_fraction`` /
# ``.min_factor_of_safety`` / ``.mean_probability_of_failure`` fields the
# worker / postprocess computed - never free-generated.
#
# Landlab runs OFF-BOX ONLY (the scale-to-zero island norm) - there is no
# in-process lane, so this composer always dispatches through the generic
# ``run_solver`` / ``wait_for_completion`` Batch seam (the SAME seam SFINCS
# uses).
# --------------------------------------------------------------------------- #

#: Minimum landslide AOI side length (m). A geocoded single-feature bbox can be
#: a few metres across; a landslide-susceptibility scenario needs at least a
#: hillslope. Below this the bbox is EXPANDED (centred) to this side length. A
#: normal hillslope/catchment AOI is far above this, so this is a no-op except
#: on a collapsed bbox. Floor only; never shrinks. (Mirrors the SWMM AOI
#: floor.)
_MIN_LANDSLIDE_AOI_SIDE_M: float = 500.0


class LandslideWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "LANDSLIDE_WORKFLOW_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _enforce_min_landslide_aoi(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Expand a too-small AOI bbox to a sensible landslide minimum, centred.

    Floors BOTH side lengths to ``_MIN_LANDSLIDE_AOI_SIDE_M`` metres about the
    bbox centroid (lon scaled by cos(lat)). Returns the bbox UNCHANGED when both
    sides already meet the floor. Never shrinks. Mirrors
    ``model_swmm_urban_flood._enforce_min_urban_aoi``.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    cen_lat = 0.5 * (min_lat + max_lat)
    cen_lon = 0.5 * (min_lon + max_lon)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(cen_lat)), 1e-6)
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat
    if width_m >= _MIN_LANDSLIDE_AOI_SIDE_M and height_m >= _MIN_LANDSLIDE_AOI_SIDE_M:
        return bbox
    half_lon = 0.5 * max(width_m, _MIN_LANDSLIDE_AOI_SIDE_M) / m_per_deg_lon
    half_lat = 0.5 * max(height_m, _MIN_LANDSLIDE_AOI_SIDE_M) / m_per_deg_lat
    expanded = (
        cen_lon - half_lon,
        cen_lat - half_lat,
        cen_lon + half_lon,
        cen_lat + half_lat,
    )
    logger.info(
        "model_landlab_susceptibility: AOI floor applied - input bbox %s was "
        "%.0fm x %.0fm (below the %.0fm minimum); expanded to %s",
        bbox,
        width_m,
        height_m,
        _MIN_LANDSLIDE_AOI_SIDE_M,
        expanded,
    )
    return expanded


def _localize_to_dem_path(uri: str) -> str:
    """Resolve a DEM ``LayerURI.uri`` (s3:// / file:// / local) to an on-disk
    GeoTIFF path the staging upload can read.

    Mirrors ``model_swmm_urban_flood._localize_to_dem_path``: ``s3://`` objects
    are staged down to a temp file via boto3; ``file://`` + bare local paths pass
    through. On a synthetic / test path the URI is already local.
    """
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if not uri.startswith("s3://"):
        return uri

    import hashlib

    cache_dir = Path(tempfile.gettempdir()) / "trid3nt-landlab-dem-stage"
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(uri).suffix or ".tif"
    local = cache_dir / (hashlib.sha256(uri.encode()).hexdigest()[:24] + suffix)
    if local.exists() and local.stat().st_size > 0:
        return str(local)
    tmp = local.with_suffix(local.suffix + ".part")
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    bucket_name, _, obj_key = uri[len("s3://"):].partition("/")
    resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
    with tmp.open("wb") as fh:
        shutil.copyfileobj(resp["Body"], fh)
    os.replace(tmp, local)
    logger.info("staged DEM %s -> %s (%d bytes)", uri, local, local.stat().st_size)
    return str(local)


def _fetch_dem_for_landslide(
    bbox: tuple[float, float, float, float],
) -> tuple[str, str]:
    """Fetch a DEM for the AOI: ``fetch_3dep_extra`` 1 m primary -> ``fetch_dem``
    10 m fallback (the data-source fallback norm). Returns ``(local_dem_path,
    source_label)``; raises ``LandslideWorkflowError("LANDLAB_DEM_FETCH_FAILED")``
    only when BOTH fail."""
    from trid3nt_server.data import TOOL_REGISTRY

    # fetch_3dep_extra + fetch_dem are spec-driven tools:
    # resolve through the registry seam (keyword-only) rather than deleted twins.
    fetch_3dep_extra = TOOL_REGISTRY["fetch_3dep_extra"].fn
    fetch_dem = TOOL_REGISTRY["fetch_dem"].fn

    try:
        layer = fetch_3dep_extra(bbox, resolution="1 meter", purpose="terrain")
        return _localize_to_dem_path(layer.uri), "USGS 3DEP 1m LiDAR"
    except Exception as exc:  # noqa: BLE001 -- fall through to the 10 m fallback
        logger.info(
            "fetch_3dep_extra(1m) failed (%s); falling back to fetch_dem(10m)", exc
        )

    try:
        layer = fetch_dem(bbox=bbox, resolution_m=10, purpose="terrain")
        return _localize_to_dem_path(layer.uri), "USGS 3DEP 10m"
    except Exception as exc:  # noqa: BLE001
        raise LandslideWorkflowError(
            "LANDLAB_DEM_FETCH_FAILED",
            f"both DEM sources failed for bbox {bbox}: 3DEP-1m + fetch_dem-10m: {exc}",
        ) from exc


def _download_batch_landlab_outputs(
    run_result: Any, run_id: str
) -> tuple[str, dict[str, Any], str, dict[str, str]]:
    """Download the Batch field COG + read the worker's typed ``result`` block.

    The Landlab worker uploads ``landlab_field.tif`` under
    ``s3://<runs_bucket>/<run_id>/`` and records the field URI + the typed
    ``result`` block in completion.json. We re-read completion.json (small,
    already on S3) to find the field key + the result block, download the COG via
    the SAME boto3 client the solver dispatch uses, and return ``(local_cog,
    result_block, tmp_dir, secondary_local_by_token)``.

    levers STEP 3: the worker also writes per-secondary-field COGs
    (``landlab_secondary_<token>.tif``) and records them in
    ``result.secondary_field_files`` (token -> filename). We download each that
    is present and return ``secondary_local_by_token`` (token -> local path) so
    the composer can publish the additional quantities. A missing/undownloadable
    secondary COG is skipped (never sinks the primary).

    Raises ``LandlabWorkflowError("LANDLAB_BATCH_OUTPUT_MISSING")`` when a
    'complete' run produced no downloadable field COG (a real failure, never a
    silent dead-end).
    """
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    # The PRIMARY field COG filename (mirrors
    # workers.landlab.entrypoint.FIELD_COG_NAME /
    # workers.landlab.run_chain.FIELD_COG_NAME). completion.json's
    # ``output_uris`` lists EVERY uploaded artifact for the run -- the input
    # ``dem.tif`` (re-uploaded by the supervisor alongside the outputs) AND
    # every ``landlab_secondary_<token>.tif`` context layer, not just the
    # field COG. BUG (found live): matching "any *.tif in output_uris" picked
    # whichever key sorted first -- typically ``dem.tif`` -- and fed raw DEM
    # elevations (meters, e.g. ~1761-2352) into the probability-of-failure
    # metrics as if they were probabilities: every "active" cell exceeded the
    # 0.75 unstable threshold (unstable_area_fraction=1.0) and the mean
    # elevation clamped to the [0,1] range (mean_probability_of_failure=1.0).
    # The FIX: match the field COG by its EXACT known basename, never by
    # "any .tif".
    FIELD_COG_NAME = "landlab_field.tif"

    field_keys: list[str] = []
    result_block: dict[str, Any] = {}
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        res = manifest.get("result")
        if isinstance(res, dict):
            result_block = res
        for raw in manifest.get("output_uris") or []:
            uri = str(raw)
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
            except Exception:  # noqa: BLE001 -- skip an unparseable entry
                continue
            if Path(key).name == FIELD_COG_NAME:
                field_keys.append(key)
    if not field_keys:
        field_keys = [f"{run_id}/{FIELD_COG_NAME}"]

    tmp_dir = tempfile.mkdtemp(prefix=f"landlab-batch-out-{run_id}-")

    def _download(key: str) -> str | None:
        dest = Path(tmp_dir) / Path(key).name
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Landlab Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )
            return None
        return str(dest)

    local_field = next((p for p in (_download(k) for k in field_keys) if p), None)
    if local_field is None:
        _cleanup_dir(tmp_dir)
        raise LandlabWorkflowError(
            "LANDLAB_BATCH_OUTPUT_MISSING",
            message=(
                f"Landlab Batch run {run_id} completed but produced no "
                f"downloadable field COG under s3://{runs_bucket}/{run_id}/ "
                f"(looked for {field_keys!r})"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    # levers STEP 3: download the secondary-field COGs the worker recorded.
    secondary_local_by_token: dict[str, str] = {}
    sec_files = result_block.get("secondary_field_files")
    if isinstance(sec_files, dict):
        for token, fname in sec_files.items():
            key = f"{run_id}/{fname}"
            local = _download(key)
            if local is not None:
                secondary_local_by_token[str(token)] = local

    return local_field, result_block, tmp_dir, secondary_local_by_token


def _cleanup_dir(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


async def model_landlab_susceptibility(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabSusceptibilityLayerURI:
    """Compose the full Landlab surface-process chain end-to-end (OFF-BOX lane).

    Args:
        run_args: the validated ``LandlabRunArgs`` (bbox + analysis + soil /
            rainfall parameters).
        dem_path: optional on-disk DEM path. When ``None`` the composer fetches
            it (``fetch_3dep_extra`` 1 m -> ``fetch_dem`` 10 m fallback) from
            ``run_args.bbox``. Tests pass a synthetic GeoTIFF to skip the fetch.
        run_id: optional ULID; minted by ``new_ulid`` if absent.
        compute_class: compute class for the Batch dispatch.
        source_note: optional input-provenance string (triggering rainfall =
            Atlas-14 design storm; soil block = demo defaults) stamped onto the
            returned layer's ``source_note`` for honest narration.

    Returns:
        The primary ``LandlabSusceptibilityLayerURI`` (role ``"primary"``)
        carrying the three narration scalars.

    Raises:
        LandslideWorkflowError / LandlabWorkflowError / PostprocessLandlabError on
        a fatal stage failure (the tool wrapper catches these and returns a typed
        error dict so the agent narrates honestly).
    """
    from trid3nt_server.data.simulation.solver.solver import (
        EmitterBinding,
        new_ulid,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    bbox = _enforce_min_landslide_aoi(tuple(run_args.bbox))
    emitter = current_emitter()
    rid = run_id or new_ulid()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning("model_landlab_susceptibility: zoom-to emit failed: %s", exc)

    # --- Declare the planned child count for the live breadcrumb -
    # The composer's user-meaningful internal operations surfaced as nested
    # child rows: (fetch_dem if not supplied) -> stage_landlab_manifest ->
    # run_solver (the Batch solve) -> download_landlab_outputs ->
    # postprocess_landlab -> publish_layer. The DEM fetch only runs when no
    # dem_path was supplied, so the planned count adjusts. No-op when no emitter
    # is bound (verify/CI direct-call path).
    begin_substeps(current_emitter(), 6 if dem_path is None else 5)

    # --- Step 1: DEM (1 m 3DEP primary -> 10 m fallback) --------------------
    # Off the loop (sync blocking I/O) per the no-sync-blocking norm.
    if dem_path is None:
        async with substep(current_emitter(), "fetch_dem"):
            local_dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_landslide, bbox
            )
    else:
        local_dem_path, dem_source = dem_path, "supplied"
    logger.info("model_landlab_susceptibility: DEM=%s (%s)", local_dem_path, dem_source)

    # --- Step 2: stage the DEM + build_spec manifest to S3 ------------------
    async with substep(current_emitter(), "stage_landlab_manifest"):
        staging: LandlabStaging = await asyncio.to_thread(
            stage_landlab_manifest, run_args, dem_path=local_dem_path, run_id=rid
        )

    # --- Step 3: dispatch through the generic Batch seam --------------------
    # Surface the dispatch + Batch wait as a single "run_solver" child row; the
    # live Batch readout stays owned by the two-card Sim observability
    # (mint_dispatch_and_sim_cards) which is PRESERVED as-is.
    async with substep(current_emitter(), "run_solver"):
        handle = run_solver(
            solver=LANDLAB_SOLVER_NAME,
            model_setup_uri=staging.manifest_uri,
            compute_class=compute_class,
        )
        # --- Two-card sim observability (mirror the SWMM off-box lane) ------
        _sim_step_id = await mint_dispatch_and_sim_cards(
            emitter=emitter,
            solver=LANDLAB_SOLVER_NAME,
            handle=handle,
            compute_class=compute_class,
        )
        if emitter is not None and _sim_step_id is not None:
            set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

        try:
            run_result = await wait_for_completion(handle)
        except asyncio.CancelledError:
            logger.info("model_landlab_susceptibility cancelled while awaiting solver")
            await route_sim_terminal(emitter, _sim_step_id, run_result=None)
            raise
        finally:
            set_emitter_binding(None)

        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result.status != "complete":
        raise LandlabWorkflowError(
            "LANDLAB_RUN_FAILED",
            message=(
                "Landlab Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}"
            ),
            details={
                "run_id": rid,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # --- Register-only branch (worker postprocess offload) -------------------
    # If the worker wrote a publish_manifest.json (schema_version==1), read +
    # schema-gate it and SHORT-CIRCUIT the on-box heavy tail (no download, no
    # postprocess_landlab). Degrades cleanly to the legacy on-box path when
    # absent (pre-rebuild worker image) or schema unknown.
    from trid3nt_server.workflows.shared.register_published_manifest import (
        read_publish_manifest,
        register_manifest_layers,
    )

    batch_run_id = getattr(run_result, "run_id", None) or rid
    _manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if _manifest is not None:
        logger.info(
            "model_landlab_susceptibility: REGISTER-ONLY path (worker postprocess "
            "offload) run_id=%s engine=%s layers=%d",
            batch_run_id, _manifest.engine, len(_manifest.layers),
        )
        async with substep(current_emitter(), "publish_layer"):
            _reg = register_manifest_layers(
                _manifest, run_id=batch_run_id, bbox=tuple(bbox)
            )
        _primary_layers = [lyr for lyr in _reg.layers if lyr.role == "primary"]
        _frame_layers = [lyr for lyr in _reg.layers if lyr.role != "primary"]
        if _frame_layers and emitter is not None:
            for _lyr in _frame_layers:
                try:
                    await emitter.add_loaded_layer(_lyr)
                except Exception:  # noqa: BLE001
                    pass
        if not _primary_layers:
            raise LandslideWorkflowError(
                "LANDLAB_NO_LAYERS",
                "worker publish_manifest produced no primary layer (empty solve?)",
            )
        _prim = _primary_layers[0]
        _m = _reg.metrics
        _typed_primary = LandlabSusceptibilityLayerURI(
            uri=_prim.uri,
            layer_type=_prim.layer_type,
            layer_id=_prim.layer_id,
            name=_prim.name,
            style_preset=_prim.style_preset,
            bbox=tuple(bbox),
            role=_prim.role,
            unstable_area_fraction=float(_m.get("unstable_area_fraction", 0.0)),
            min_factor_of_safety=float(_m.get("min_factor_of_safety", 0.0)),
            mean_probability_of_failure=float(_m.get("mean_probability_of_failure", 0.0)),
            source_note=source_note,
            synthetic_inputs=list(synthetic_inputs or []),
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_landlab_susceptibility: authoritative zoom-to emit failed: %s", exc
                )
        return _typed_primary

    # --- Step 4: download the field COG + read the worker result block ------
    async with substep(current_emitter(), "download_landlab_outputs"):
        (
            local_field,
            result_block,
            batch_out_dir,
            secondary_cogs,
        ) = await asyncio.to_thread(
            _download_batch_landlab_outputs, run_result, batch_run_id
        )

    # --- Step 5: postprocess (field COG -> EPSG:4326 susceptibility COG) ----
    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab,
                local_field,
                run_id=rid,
                analysis=run_args.analysis,
                result=result_block,
            )

        # levers STEP 3 (gated): ALSO publish the secondary fields (drainage
        # area / slope / relative wetness / discharge / factor-of-safety) as
        # context layers. Non-fatal -- a failure never sinks the primary.
        import os as _os

        if secondary_cogs and _os.environ.get(
            "TRID3NT_LANDLAB_REGISTRY_QUANTITIES", ""
        ).lower() in ("1", "true", "on", "yes"):
            try:
                from trid3nt_server.workflows.landlab.postprocess_landlab import publish_landlab_quantities
                from trid3nt_server.workflows.shared.register_published_manifest import register_manifest_layers

                reg = await asyncio.to_thread(
                    lambda: publish_landlab_quantities(
                        secondary_cogs,
                        run_id=rid,
                        register_manifest_layers=register_manifest_layers,
                        bbox=tuple(bbox),
                    )
                )
                emitter_now = current_emitter()
                if emitter_now is not None and reg is not None:
                    for extra_layer in getattr(reg, "layers", []) or []:
                        try:
                            await emitter_now.add_loaded_layer(extra_layer)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("could not add landlab registry layer: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_landlab_susceptibility registry-quantity publish failed "
                    "(non-fatal): %s",
                    exc,
                )
    finally:
        _cleanup_dir(batch_out_dir)

    if not layers:
        raise LandslideWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab produced no susceptibility layer (empty solve?)",
        )

    raw_primary = layers[0]

    # --- Step 6: publish the primary COG through publish_layer (render chokepoint)
    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(_publish_primary_layer, raw_primary, rid)

    # Stamp the returned layer's bbox to the floored AOI (the authoritative AOI).
    if tuple(primary.bbox or ()) != tuple(bbox):
        primary = primary.model_copy(update={"bbox": tuple(bbox)})
    _prim_update: dict[str, Any] = {}
    if source_note is not None:
        _prim_update["source_note"] = source_note
    if synthetic_inputs:
        _prim_update["synthetic_inputs"] = list(synthetic_inputs)
    if _prim_update:
        primary = primary.model_copy(update=_prim_update)

    logger.info(
        "model_landlab_susceptibility complete run_id=%s analysis=%s "
        "unstable_frac=%.4f min_fos=%.4f mean_pof=%.4f uri=%s",
        rid,
        run_args.analysis,
        primary.unstable_area_fraction,
        primary.min_factor_of_safety,
        primary.mean_probability_of_failure,
        primary.uri,
    )

    # --- Authoritative LAST zoom-to (supersede any early geocode snap) ------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_landlab_susceptibility: authoritative zoom-to emit failed: %s",
                exc,
            )

    return primary


def _publish_primary_layer(
    raw_primary: LandlabSusceptibilityLayerURI, run_id: str
) -> LandlabSusceptibilityLayerURI:
    """Publish the primary susceptibility COG through publish_layer.

    Routes the raw s3:// COG through ``publish_layer`` (the
    ``_resolve_qgis_style_params`` render seam) and returns a NEW
    ``LandlabSusceptibilityLayerURI`` carrying the published /tiles or WMS URL
    plus the narration scalars. On publish failure the raw layer is returned
    UNCHANGED: the dispatch-level ``emit_layer_uri`` guardrail then drops the
    dead raw-s3:// raster from the map (honest) while the typed metrics still
    narrate. Mirrors ``model_swmm_urban_flood._publish_peak_layer``.
    """
    if raw_primary.layer_type != "raster" or not (
        raw_primary.uri.startswith("gs://") or raw_primary.uri.startswith("s3://")
    ):
        return raw_primary
    layer_id_for_pub = f"landlab-susceptibility-{run_id}"
    style = raw_primary.style_preset or LANDSLIDE_STYLE_PRESET
    try:
        published_uri = publish_layer(
            layer_uri=raw_primary.uri,
            layer_id=layer_id_for_pub,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_susceptibility: publish_layer FAILED for the primary "
            "layer_id=%s error_code=%s (%s) - returning the unpublished layer. "
            "The narration scalars still surface honestly.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_primary
    return LandlabSusceptibilityLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_primary.name,
        layer_type=raw_primary.layer_type,
        uri=published_uri,
        style_preset=style,
        role=raw_primary.role,
        units=raw_primary.units,
        bbox=raw_primary.bbox,
        unstable_area_fraction=raw_primary.unstable_area_fraction,
        min_factor_of_safety=raw_primary.min_factor_of_safety,
        mean_probability_of_failure=raw_primary.mean_probability_of_failure,
    )
