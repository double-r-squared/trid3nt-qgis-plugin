"""Engine template ``landlab_green_ampt_overland_flow`` - Landlab Green-Ampt
infiltration + overland-flow storm-partition (the canonical
infilt_green_ampt_with_overland_flow tutorial chain).

A distinct question CLASS from ``landlab_susceptibility`` and
``landlab_flow_accumulation`` (per the capability-naming rule): when a design
storm falls on this terrain, how much INFILTRATES vs how much RUNS OFF, and
WHERE does runoff initiate? It is its OWN registered engine TEMPLATE
(engine="landlab", tier="template"), NOT an enum extension of another template.

``landlab_green_ampt_overland_flow(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain (``model_landlab_green_ampt_overland_flow``
below): ``OverlandFlow`` stepped over a NOAA Atlas-14 design storm while
``SoilInfiltrationGreenAmpt`` removes infiltrated water each step, and returns a
``LandlabGreenAmptLayerURI`` (the infiltration-depth raster) plus a runoff-depth
(rainfall-excess) context raster and an infiltration-vs-runoff partition chart.
Landlab runs OFF-BOX in the local-exec / Batch solver seam (exec_kind "exec";
no baked image) -- the same seam the other Landlab templates dispatch through.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabGreenAmptLayerURI.infiltrated_fraction`` / ``.runoff_fraction``
/ ``.mean_infiltration_mm`` / ``.mean_runoff_mm`` / ``.total_rainfall_mm`` fields
the worker / postprocess computed -- never free-generated. The DEM is REAL
(fetched via seam-1); the triggering rainfall is the real NOAA Atlas-14 design
storm; only the SOIL hydraulic block (K / initial moisture / soil type) is
demo-defaulted (no SSURGO fetcher yet) and labeled as such.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_INITIAL_SOIL_MOISTURE,
    LandlabGreenAmptLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import (
    gate_input_review,
    physics_refusal_reason,
)
from trid3nt_server.workflows.shared.aquifer_resolve import (
    derive_soil_scalars,
    soil_derived_entry,
)
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.run_landlab import LANDLAB_RES_SPEC
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    INFILTRATION_STYLE_PRESET,
    PostprocessLandlabError,
    build_infiltration_partition_chart_spec,
    postprocess_landlab_green_ampt,
)
from trid3nt_server.workflows.landlab.run_landlab import (
    LANDLAB_SOLVER_NAME,
    LandlabStaging,
    LandlabWorkflowError,
    stage_landlab_manifest,
)
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    _DEFAULT_TRIGGER_DURATION_HR,
    LandslideWorkflowError,
    _atlas14_design_storm_mm,
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
    "trid3nt_server.workflows.landlab.green_ampt.green_ampt"
)

__all__ = [
    "landlab_green_ampt_overland_flow",
    "model_landlab_green_ampt_overland_flow",
    "GreenAmptWorkflowError",
]

#: Default design-storm for the Green-Ampt trigger when the caller passes none.
#: A short intense convective burst is the canonical infiltration-partition
#: scenario (the tutorial uses a 5-min 90 mm/hr storm); a 30-min 100-yr Atlas-14
#: burst is the planning-grade analogue.
_DEFAULT_GREEN_AMPT_RETURN_PERIOD_YR: int = 100
_DEFAULT_GREEN_AMPT_DURATION_HR: float = 0.5


class GreenAmptWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card (the run_landlab door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "storm rainfall PARTITION on a watershed DEM: how much of a design storm "
        "infiltrates vs runs off (Green-Ampt infiltration + Landlab OverlandFlow), "
        "the infiltration-depth + runoff-depth rasters, and where runoff initiates"
    ),
    required_inputs=["bbox"],
    knobs=(
        "rainfall_return_period_yr, storm_duration_hr, "
        "soil_hydraulic_conductivity_m_s, initial_soil_moisture_content, "
        "green_ampt_soil_type, target_resolution_m"
    ),
)


_METADATA = AtomicToolMetadata(
    name="landlab_green_ampt_overland_flow",
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
async def landlab_green_ampt_overland_flow(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    rainfall_intensity_mm_hr: float | None = None,
    storm_duration_hr: float = _DEFAULT_GREEN_AMPT_DURATION_HR,
    rainfall_return_period_yr: int = _DEFAULT_GREEN_AMPT_RETURN_PERIOD_YR,
    soil_hydraulic_conductivity_m_s: float | None = None,
    initial_soil_moisture_content: float = DEFAULT_INITIAL_SOIL_MOISTURE,
    green_ampt_soil_type: str | None = None,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LandlabGreenAmptLayerURI | dict[str, Any]:
    """Partition a design storm into infiltration vs runoff over a watershed DEM (Green-Ampt + overland flow).

    Fidelity: Landlab SoilInfiltrationGreenAmpt coupled to the de Almeida
    OverlandFlow chain on a real AOI DEM; a planning-grade infiltration-vs-runoff
    partition surface, not a site-calibrated hydrologic model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The TRIGGERING RAINFALL is the
    real NOAA Atlas-14 design storm (rainfall_return_period_yr / storm_duration_hr)
    -- ``rainfall_intensity_mm_hr`` is DERIVED from it when unset; a failed lookup
    STOPS with a typed gate (never a baked default). The Green-Ampt SOIL hydraulics
    (saturated conductivity + texture class) are DERIVED from SoilGrids texture at
    the AOI (Saxton-Rawls Ksat + the USDA texture class that selects the capillary
    suction); when SoilGrids cannot serve, the run REFUSES in auto (law 9 -- no
    invented soil default). The initial soil moisture is a scenario initial state.
    Off-scope: landslide susceptibility -> landlab_susceptibility; drainage
    area / channel network -> landlab_flow_accumulation; riverine/coastal
    inundation -> sfincs_flood; urban pluvial drainage -> swmm_urban_flood.

    Use this when: the user asks how much of a storm INFILTRATES vs RUNS OFF,
    for a rainfall-partition / infiltration-excess / runoff-generation map, or
    where runoff initiates over a catchment.

    Params:
        bbox: watershed / catchment AOI, EPSG:4326 (min_lon, min_lat, max_lon,
            max_lat).
        rainfall_intensity_mm_hr: overland rainfall intensity, mm/hr. Unset ->
            DERIVED from the Atlas-14 design storm (depth / storm_duration_hr).
        storm_duration_hr: design-storm / overland duration, hours (default 0.5);
            also the Atlas-14 lookup duration.
        rainfall_return_period_yr: design-storm return period, years (default 100).
        soil_hydraulic_conductivity_m_s: Green-Ampt saturated hydraulic
            conductivity, m/s. Unset -> DERIVED from SoilGrids texture at the AOI;
            refuses in auto when SoilGrids cannot serve (law 9).
        initial_soil_moisture_content: initial volumetric soil moisture in [0, 1)
            (default 0.15, a scenario initial state).
        green_ampt_soil_type: USDA soil texture class (selects the Green-Ampt
            suction). Unset -> DERIVED from SoilGrids texture at the AOI.
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the resolved
            triggering rainfall + demo soil block for review before the solve;
            "auto" (default) proceeds with them labeled.

    Returns:
        On success: ``LandlabGreenAmptLayerURI`` -- the infiltration-depth COG,
        with ``infiltrated_fraction``, ``runoff_fraction``, ``mean_infiltration_mm``,
        ``mean_runoff_mm``, ``total_rainfall_mm``. A runoff-depth context raster +
        a partition chart are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_green_ampt_overland_flow requires a bbox "
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

    _dur_hr = (
        float(storm_duration_hr)
        if storm_duration_hr is not None
        else _DEFAULT_TRIGGER_DURATION_HR
    )

    # --- Triggering rainfall: real NOAA Atlas-14 design storm, or a typed gate ---
    provenance: list[SyntheticInput] = []
    _rainfall_label = ""
    if rainfall_intensity_mm_hr is None:
        _depth_mm = await asyncio.to_thread(
            _atlas14_design_storm_mm,
            tuple(coerced),
            int(rainfall_return_period_yr),
            _dur_hr,
        )
        if _depth_mm is None:
            return {
                "status": "error",
                "error_code": "LANDLAB_RAINFALL_INPUT_REQUIRED",
                "error_message": (
                    f"The NOAA Atlas-14 design-storm lookup failed for this AOI "
                    f"({rainfall_return_period_yr}-yr / {_dur_hr:.1f}-hr), so the "
                    f"triggering rainfall is not fabricated. Retry with an explicit "
                    f"rainfall_intensity_mm_hr -- or an AOI within Atlas-14 "
                    f"coverage (CONUS / PR / USVI)."
                ),
            }
        rainfall_intensity_mm_hr = round(_depth_mm / max(_dur_hr, 1e-6), 2)
        _rainfall_label = (
            f"overland rainfall intensity {rainfall_intensity_mm_hr:.1f} mm/hr "
            f"(NOAA Atlas-14 {rainfall_return_period_yr}-yr/{_dur_hr:.1f}-hr design "
            f"storm, {_depth_mm:.1f} mm total)"
        )
        provenance.append(
            SyntheticInput(
                param="rainfall_intensity_mm_hr",
                value=rainfall_intensity_mm_hr,
                units="mm/hr",
                basis="derived",
                real_source_if_any="lookup_precip_return_period (NOAA Atlas-14)",
                note=f"{rainfall_return_period_yr}-yr/{_dur_hr:.1f}-hr design storm",
            )
        )
    else:
        _rainfall_label = "triggering rainfall: user-supplied"
        provenance.append(
            SyntheticInput(param="rainfall_intensity_mm_hr", basis="user")
        )
    # --- law 9: Green-Ampt soil hydraulics DERIVED from SoilGrids or REFUSE ---
    # The saturated conductivity (Saxton-Rawls Ksat) AND the USDA texture class
    # (which selects the Green-Ampt capillary suction) come from ONE SoilGrids
    # texture read at the AOI centroid; when SoilGrids cannot serve, both stay
    # unresolved (default_demo/physics) so the gate REFUSES in auto (no invented
    # soil constant).
    _need_soil = (
        soil_hydraulic_conductivity_m_s is None or green_ampt_soil_type is None
    )
    _lat = 0.5 * (coerced[1] + coerced[3])
    _lon = 0.5 * (coerced[0] + coerced[2])
    _deriv = None
    _soil_meta: dict[str, Any] = {}
    if _need_soil:
        _deriv, _soil_meta = await asyncio.to_thread(derive_soil_scalars, _lat, _lon)

    soil_hydraulic_conductivity_m_s, _k_entry = soil_derived_entry(
        param="soil_hydraulic_conductivity_m_s", units="m/s",
        user_value=soil_hydraulic_conductivity_m_s,
        derived_value=(_deriv.k_m_s if _deriv is not None else None),
        meta=_soil_meta, need="Green-Ampt saturated hydraulic conductivity",
        derived_note="Green-Ampt Ksat",
    )
    provenance.append(_k_entry)

    if green_ampt_soil_type is not None:
        provenance.append(SyntheticInput(
            param="green_ampt_soil_type", value=str(green_ampt_soil_type),
            basis="user", consequence="physics",
            note="caller-supplied Green-Ampt texture class."))
    elif _deriv is not None:
        green_ampt_soil_type = _deriv.texture_class
        provenance.append(SyntheticInput(
            param="green_ampt_soil_type", value=green_ampt_soil_type,
            basis="derived", consequence="physics",
            real_source_if_any="fetch_soilgrids (USDA texture class)",
            note=(f"USDA texture class DERIVED from SoilGrids at the AOI "
                  f"(sand={_deriv.sand_pct}%, clay={_deriv.clay_pct}%); selects the "
                  "Green-Ampt capillary suction. SCREENING near-surface proxy.")))
    else:
        provenance.append(SyntheticInput(
            param="green_ampt_soil_type", value=None, basis="default_demo",
            consequence="physics",
            note=("Green-Ampt soil texture class could not be resolved from SoilGrids "
                  f"at this AOI ({_soil_meta.get('reason', 'unavailable')}). No invented "
                  "default (law 9): supply green_ampt_soil_type or run where SoilGrids "
                  "has coverage.")))

    _soil_prov = (
        "DERIVED from SoilGrids texture at the AOI (Saxton-Rawls Ksat + USDA class)"
        if _deriv is not None
        else "user-supplied" if not _need_soil
        else "UNRESOLVED - SoilGrids could not serve (law-9 refusal)"
    )
    source_note = (
        _rainfall_label
        + f"; Green-Ampt soil hydraulics (Ksat + texture class) {_soil_prov}."
    )

    _review = await gate_input_review(
        tool_name="landlab_green_ampt_overland_flow",
        mode=input_mode,
        entries=provenance,
        params={
            "rainfall_intensity_mm_hr": rainfall_intensity_mm_hr,
            "soil_hydraulic_conductivity_m_s": soil_hydraulic_conductivity_m_s,
            "initial_soil_moisture_content": initial_soil_moisture_content,
        },
    )
    if _review.cancelled:
        _phys = physics_refusal_reason("landlab_green_ampt_overland_flow", provenance)
        return {
            "status": "error",
            "error_code": (
                "LANDLAB_PHYSICS_INPUT_REQUIRED" if _phys else "USER_INPUT_CANCELLED"
            ),
            "error_message": (
                _review.cancel_reason
                or f"landlab_green_ampt_overland_flow {_review.cancel_reason}"
            ),
        }
    provenance = _review.entries
    rainfall_intensity_mm_hr = float(
        _review.params.get("rainfall_intensity_mm_hr", rainfall_intensity_mm_hr)
    )
    _rv_k = _review.params.get(
        "soil_hydraulic_conductivity_m_s", soil_hydraulic_conductivity_m_s
    )
    soil_hydraulic_conductivity_m_s = float(_rv_k) if _rv_k is not None else None
    initial_soil_moisture_content = float(
        _review.params.get(
            "initial_soil_moisture_content", initial_soil_moisture_content
        )
    )
    # law-9 belt-and-suspenders: never build a deck on an unresolved soil value
    # (e.g. the headless user_gated fail-open path that did not refuse).
    if soil_hydraulic_conductivity_m_s is None or green_ampt_soil_type is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PHYSICS_INPUT_REQUIRED",
            "error_message": (
                physics_refusal_reason(
                    "landlab_green_ampt_overland_flow", provenance
                ) or "Green-Ampt soil hydraulics unresolved (law 9)."
            ),
        }

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="green_ampt_overland_flow",
            target_resolution_m=float(target_resolution_m),
            rainfall_intensity_mm_hr=float(rainfall_intensity_mm_hr),
            storm_duration_hr=float(_dur_hr),
            soil_hydraulic_conductivity_m_s=float(soil_hydraulic_conductivity_m_s),
            initial_soil_moisture_content=float(initial_soil_moisture_content),
            green_ampt_soil_type=str(green_ampt_soil_type),
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab Green-Ampt arguments: {exc}",
        }

    logger.info(
        "landlab_green_ampt_overland_flow bbox=%s rain=%.1fmm/hr dur=%.2fh "
        "K=%.1e theta_i=%.2f soil=%s res=%.1fm",
        run_args.bbox,
        run_args.rainfall_intensity_mm_hr,
        run_args.storm_duration_hr,
        run_args.soil_hydraulic_conductivity_m_s,
        run_args.initial_soil_moisture_content,
        run_args.green_ampt_soil_type,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_green_ampt_overland_flow(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_green_ampt_overland_flow complete layer_id=%s "
            "infil_frac=%.3f runoff_frac=%.3f rain=%.1fmm uri=%s",
            primary.layer_id,
            primary.infiltrated_fraction,
            primary.runoff_fraction,
            primary.total_rainfall_mm,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        GreenAmptWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_green_ampt_overland_flow failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("landlab_green_ampt_overland_flow unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer (deterministic, no LLM in the chain -- Invariant 2):
#   fetch DEM -> stage -> run_solver('landlab') -> download infiltration+runoff
#     COGs -> postprocess_landlab_green_ampt -> publish infiltration raster
#     -> add runoff-depth context raster -> emit partition chart.
# Reuses the susceptibility composer's DEM fetch + download + AOI-floor helpers
# (the shared Landlab off-box seam) rather than reinventing them.
# --------------------------------------------------------------------------- #
async def model_landlab_green_ampt_overland_flow(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabGreenAmptLayerURI:
    """Compose the Landlab Green-Ampt overland-flow chain end-to-end (OFF-BOX lane).

    Returns the primary infiltration-depth ``LandlabGreenAmptLayerURI``; emits
    the runoff-depth context raster + the partition chart as side effects on the
    bound emitter.
    """
    from trid3nt_server.workflows.solver.solver import (
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
            logger.warning(
                "model_landlab_green_ampt_overland_flow: zoom-to emit failed: %s", exc
            )

    begin_substeps(current_emitter(), 6 if dem_path is None else 5)

    # --- Step 1: DEM (1 m 3DEP primary -> 10 m fallback) ---
    if dem_path is None:
        async with substep(current_emitter(), "fetch_dem"):
            local_dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_landslide, bbox
            )
    else:
        local_dem_path, dem_source = dem_path, "supplied"
    logger.info(
        "model_landlab_green_ampt_overland_flow: DEM=%s (%s)",
        local_dem_path,
        dem_source,
    )

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
                "Landlab Green-Ampt solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}"
            ),
            details={"run_id": rid},
        )

    batch_run_id = getattr(run_result, "run_id", None) or rid

    # --- Step 4: download the infiltration field COG + the runoff-depth COG ---
    async with substep(current_emitter(), "download_landlab_outputs"):
        (
            local_field,
            result_block,
            batch_out_dir,
            secondary_cogs,
        ) = await asyncio.to_thread(
            _download_batch_landlab_outputs, run_result, batch_run_id
        )

    runoff_cog = secondary_cogs.get("runoff_depth")

    # --- Step 5: postprocess (infiltration 4326 COG + runoff 4326 COG) ---
    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_green_ampt,
                local_field,
                run_id=rid,
                result=result_block,
                runoff_cog_path=runoff_cog,
            )
    finally:
        _cleanup_dir(batch_out_dir)

    if not layers:
        raise GreenAmptWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_green_ampt produced no infiltration-depth layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    # --- Step 6: publish the infiltration raster (render chokepoint) ---
    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(_publish_infiltration_layer, raw_primary, rid)

    if tuple(primary.bbox or ()) != tuple(bbox):
        primary = primary.model_copy(update={"bbox": tuple(bbox)})
    _prim_update: dict[str, Any] = {}
    if source_note is not None:
        _prim_update["source_note"] = source_note
    if synthetic_inputs:
        _prim_update["synthetic_inputs"] = list(synthetic_inputs)
    if _prim_update:
        primary = primary.model_copy(update=_prim_update)

    # --- Runoff-depth context raster (published inline) ---
    if emitter is not None:
        for ctx in context_layers:
            pub_ctx = await asyncio.to_thread(_publish_context_raster, ctx)
            try:
                await emitter.add_loaded_layer(pub_ctx)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add runoff-depth raster: %s", exc)

    # --- Storm-partition chart (infiltration vs runoff) ---
    await _maybe_emit_partition_chart(
        emitter,
        float(metrics.get("infiltrated_fraction", primary.infiltrated_fraction)),
        float(metrics.get("runoff_fraction", primary.runoff_fraction)),
        primary.uri,
    )

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_landlab_green_ampt_overland_flow: authoritative zoom-to failed: %s",
                exc,
            )

    logger.info(
        "model_landlab_green_ampt_overland_flow complete run_id=%s infil_frac=%.3f "
        "runoff_frac=%.3f rain=%.1fmm uri=%s",
        rid,
        primary.infiltrated_fraction,
        primary.runoff_fraction,
        primary.total_rainfall_mm,
        primary.uri,
    )
    return primary


def _publish_infiltration_layer(
    raw_primary: LandlabGreenAmptLayerURI, run_id: str
) -> LandlabGreenAmptLayerURI:
    """Publish the infiltration-depth COG through publish_layer (the render seam).

    On publish failure the raw layer is returned UNCHANGED (the dispatch-level
    guardrail drops the dead raw-s3 raster; the typed scalars still narrate).
    """
    if raw_primary.layer_type != "raster" or not (
        raw_primary.uri.startswith("gs://") or raw_primary.uri.startswith("s3://")
    ):
        return raw_primary
    layer_id_for_pub = f"landlab-infiltration-depth-{run_id}"
    style = raw_primary.style_preset or INFILTRATION_STYLE_PRESET
    try:
        published_uri = publish_layer(
            layer_uri=raw_primary.uri,
            layer_id=layer_id_for_pub,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_green_ampt_overland_flow: publish_layer FAILED layer_id=%s "
            "error_code=%s (%s) - returning the unpublished layer.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_primary
    return raw_primary.model_copy(update={"uri": published_uri, "style_preset": style})


def _publish_context_raster(ctx: LayerURI) -> LayerURI:
    """Publish a context raster (runoff depth) through publish_layer; degrade
    to the raw layer on failure (the dispatch guardrail drops a dead raster)."""
    if ctx.layer_type != "raster" or not (
        ctx.uri.startswith("gs://") or ctx.uri.startswith("s3://")
    ):
        return ctx
    try:
        published_uri = publish_layer(
            layer_uri=ctx.uri,
            layer_id=ctx.layer_id,
            style_preset=ctx.style_preset or INFILTRATION_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_green_ampt_overland_flow: context publish_layer FAILED "
            "layer_id=%s error_code=%s (%s) - returning the unpublished layer.",
            ctx.layer_id,
            exc.error_code,
            exc,
        )
        return ctx
    return ctx.model_copy(update={"uri": published_uri})


async def _maybe_emit_partition_chart(
    emitter: Any,
    infiltrated_fraction: float,
    runoff_fraction: float,
    source_uri: str,
) -> None:
    """Emit the storm-partition chart (infiltration vs runoff) to the charts window."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_infiltration_partition_chart_spec(
        infiltrated_fraction, runoff_fraction
    )
    if spec is None:
        return
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Storm partition (infiltration vs runoff)",
        caption=(
            "How the design storm splits: the infiltrated share (Green-Ampt) "
            "vs the runoff share (rainfall excess) across the AOI."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("partition chart emit failed: %s", exc)
