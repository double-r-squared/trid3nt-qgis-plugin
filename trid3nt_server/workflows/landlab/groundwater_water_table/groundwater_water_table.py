"""Engine template ``landlab_groundwater_water_table`` - Landlab
GroundwaterDupuitPercolator steady-state water-table + seepage (the canonical
groundwater_flow tutorial chain).

A distinct question CLASS from the other Landlab templates (per the
capability-naming rule): under sustained recharge, WHERE is the water table
(how deep to water), WHERE does groundwater seep back to the surface, and what
is the catchment baseflow? It is its OWN registered engine TEMPLATE
(engine="landlab", tier="template"), NOT an enum extension of another template.

``landlab_groundwater_water_table(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain (``model_landlab_groundwater_water_table``
below): the ``GroundwaterDupuitPercolator`` relaxed to steady state under a
constant areal recharge, returning a ``LandlabGroundwaterLayerURI`` (the
depth-to-water raster) plus water-table-elevation + seepage context rasters and
the steady baseflow-partition chart. Landlab runs OFF-BOX in the local-exec /
Batch solver seam (exec_kind "exec"; no baked image).

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabGroundwaterLayerURI`` fields the worker/postprocess computed,
never free-generated. The V&V is the tutorial's own mass-conservation gate
(cumulative recharge == fluxes out + storage change; |rel error| < 1%). The DEM
is REAL (fetched via seam-1); the AQUIFER block (K / porosity / thickness) and
the areal recharge are demo-defaulted (no aquifer-property fetcher yet) and
labeled as such.

Thematic tie: the seepage (surface-water specific discharge) field IS the
groundwater return-flow to the surface that a surface-only rain-on-grid run
(e.g. TELEMAC RoG) omits. This template MODELS that return-flow standalone; it
is NOT a coupling of the two engines.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_GW_AQUIFER_THICKNESS_M,
    DEFAULT_GW_RECHARGE_MM_YR,
    LandlabGroundwaterLayerURI,
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
from trid3nt_server.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_landlab_chart,
    emit_zoom_to,
    stage_solve_download,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    DEPTH_TO_WATER_STYLE_PRESET,
    PostprocessLandlabError,
    build_baseflow_partition_chart_spec,
    postprocess_landlab_groundwater,
)
from trid3nt_server.workflows.landlab.run_landlab import (
    LandlabWorkflowError,
)
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.groundwater_water_table."
    "groundwater_water_table"
)

__all__ = [
    "landlab_groundwater_water_table",
    "model_landlab_groundwater_water_table",
    "GroundwaterWaterTableWorkflowError",
]


class GroundwaterWaterTableWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "steady-state water table on a watershed DEM: how deep is the water table "
        "(depth-to-water raster), where does groundwater seep back to the surface, "
        "and what is the catchment baseflow (Landlab GroundwaterDupuitPercolator "
        "under constant recharge, with a mass-conservation V&V)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "gw_hydraulic_conductivity_m_s, gw_porosity, gw_aquifer_thickness_m, "
        "gw_recharge_mm_yr, target_resolution_m"
    ),
)


_METADATA = AtomicToolMetadata(
    name="landlab_groundwater_water_table",
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
async def landlab_groundwater_water_table(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    gw_hydraulic_conductivity_m_s: float | None = None,
    gw_porosity: float | None = None,
    gw_aquifer_thickness_m: float = DEFAULT_GW_AQUIFER_THICKNESS_M,
    gw_recharge_mm_yr: float = DEFAULT_GW_RECHARGE_MM_YR,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabGroundwaterLayerURI | dict[str, Any]:
    """Map the steady-state water table + groundwater seepage over a watershed DEM (Landlab Dupuit aquifer).

    Fidelity: Landlab GroundwaterDupuitPercolator (Dupuit-Forchheimer shallow
    unconfined aquifer) relaxed to steady state on a real AOI DEM; a
    planning-grade water-table / return-flow surface, not an aquifer-test-
    calibrated hydrogeologic model. V&V: the tutorial's mass-conservation gate
    (cumulative recharge == fluxes out + storage change; |rel error| < 1%).
    Data: the DEM is REAL (USGS 3DEP via seam-1). The aquifer K + drainable
    porosity are DERIVED from SoilGrids texture at the AOI (Saxton-Rawls) or
    REFUSE in auto when SoilGrids cannot serve (law 9). The areal recharge (the
    scenario forcing question) and the aquifer thickness (a screening structural
    assumption) proceed labeled.
    Off-scope: storm-driven seepage HYDROGRAPH + recession ->
    landlab_groundwater_storm_recession; landslide susceptibility ->
    landlab_susceptibility; storm infiltration-vs-runoff partition ->
    landlab_green_ampt_overland_flow; riverine/coastal inundation -> sfincs_flood.
    Note: the seepage field is the groundwater return-flow a surface-only
    rain-on-grid run omits -- this MODELS it standalone, it is NOT a coupling.

    Use this when: the user asks how deep the water table is, for a depth-to-water
    / water-table-elevation map, where groundwater seeps back to the surface, or
    the steady baseflow / groundwater discharge of a catchment.

    Params:
        bbox: watershed / catchment AOI, EPSG:4326 (min_lon, min_lat, max_lon,
            max_lat).
        gw_hydraulic_conductivity_m_s: saturated hydraulic conductivity K, m/s.
            Unset -> DERIVED from SoilGrids texture; refuses in auto when
            SoilGrids cannot serve (law 9).
        gw_porosity: drainable aquifer porosity in (0, 1). Unset -> DERIVED from
            texture alongside K; refuses in auto when unavailable.
        gw_aquifer_thickness_m: max saturated thickness above the aquifer base, m
            (default demo 20).
        gw_recharge_mm_yr: constant areal recharge, mm/yr (default demo 200).
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the resolved aquifer +
            recharge block for review before the solve; "auto" (default) proceeds
            with them labeled.

    Returns:
        On success: ``LandlabGroundwaterLayerURI`` -- the depth-to-water COG, with
        ``mean_depth_to_water_m``, ``max_depth_to_water_m``,
        ``baseflow_discharge_m3s``, ``seeping_area_fraction``,
        ``mass_balance_rel_error``. Water-table-elevation + seepage context
        rasters + a baseflow-partition chart are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_groundwater_water_table requires a bbox "
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

    # --- law 9: aquifer K + drainable porosity DERIVED from SoilGrids or REFUSE;
    # recharge + aquifer thickness are scenario/structural (proceed labeled) ---
    # K + porosity come from ONE SoilGrids texture read at the AOI centroid
    # (Saxton-Rawls). Areal recharge is the scenario forcing question (deriving a
    # recharge from a precip fraction would invent the fraction). Aquifer thickness
    # is a screening structural assumption of the Dupuit domain (no fetcher).
    _need_aq = gw_hydraulic_conductivity_m_s is None or gw_porosity is None
    _lat = 0.5 * (coerced[1] + coerced[3])
    _lon = 0.5 * (coerced[0] + coerced[2])
    _deriv = None
    _soil_meta: dict[str, Any] = {}
    if _need_aq:
        _deriv, _soil_meta = await asyncio.to_thread(derive_soil_scalars, _lat, _lon)

    gw_hydraulic_conductivity_m_s, _k_entry = soil_derived_entry(
        param="gw_hydraulic_conductivity_m_s", units="m/s",
        user_value=gw_hydraulic_conductivity_m_s,
        derived_value=(_deriv.k_m_s if _deriv is not None else None),
        meta=_soil_meta, need="aquifer hydraulic conductivity",
        derived_note="Aquifer K",
    )
    gw_porosity, _por_entry = soil_derived_entry(
        param="gw_porosity", units="dimensionless", user_value=gw_porosity,
        derived_value=(_deriv.drainable_porosity if _deriv is not None else None),
        meta=_soil_meta, need="drainable aquifer porosity",
        derived_note="Drainable porosity",
    )
    provenance: list[SyntheticInput] = [
        _k_entry,
        _por_entry,
        SyntheticInput(
            param="gw_aquifer_thickness_m", value=gw_aquifer_thickness_m, units="m",
            basis="default_demo", consequence="scenario", real_source_if_any=None,
            note="screening structural assumption of the Dupuit domain (max "
            "saturated thickness above the base); no depth-to-bedrock fetcher",
        ),
        SyntheticInput(
            param="gw_recharge_mm_yr", value=gw_recharge_mm_yr, units="mm/yr",
            basis="default_demo", consequence="scenario", real_source_if_any=None,
            note="areal recharge is the scenario forcing question (~5-20% of "
            "humid-region precip); a precip-fraction estimate would invent the fraction",
        ),
    ]
    _aq_prov = (
        "DERIVED from SoilGrids texture at the AOI (Saxton-Rawls)"
        if _deriv is not None else "user-supplied" if not _need_aq
        else "UNRESOLVED - SoilGrids could not serve (law-9 refusal)"
    )
    source_note = (
        f"steady GroundwaterDupuitPercolator under {gw_recharge_mm_yr:.0f} mm/yr "
        f"areal recharge (scenario); aquifer K + drainable porosity {_aq_prov}; "
        f"aquifer thickness {gw_aquifer_thickness_m:.0f} m is a screening "
        "structural assumption."
    )

    _review = await gate_input_review(
        tool_name="landlab_groundwater_water_table",
        mode=input_mode,
        entries=provenance,
        params={
            "gw_hydraulic_conductivity_m_s": gw_hydraulic_conductivity_m_s,
            "gw_porosity": gw_porosity,
            "gw_aquifer_thickness_m": gw_aquifer_thickness_m,
            "gw_recharge_mm_yr": gw_recharge_mm_yr,
        },
    )
    if _review.cancelled:
        _phys = physics_refusal_reason("landlab_groundwater_water_table", provenance)
        return {
            "status": "error",
            "error_code": (
                "LANDLAB_PHYSICS_INPUT_REQUIRED" if _phys else "USER_INPUT_CANCELLED"
            ),
            "error_message": (
                _review.cancel_reason or "landlab_groundwater_water_table cancelled"
            ),
        }
    provenance = _review.entries
    _rv_k = _review.params.get(
        "gw_hydraulic_conductivity_m_s", gw_hydraulic_conductivity_m_s
    )
    gw_hydraulic_conductivity_m_s = float(_rv_k) if _rv_k is not None else None
    _rv_por = _review.params.get("gw_porosity", gw_porosity)
    gw_porosity = float(_rv_por) if _rv_por is not None else None
    gw_aquifer_thickness_m = float(
        _review.params.get("gw_aquifer_thickness_m", gw_aquifer_thickness_m)
    )
    gw_recharge_mm_yr = float(_review.params.get("gw_recharge_mm_yr", gw_recharge_mm_yr))
    # law-9 belt-and-suspenders: never solve on an unresolved aquifer property.
    if gw_hydraulic_conductivity_m_s is None or gw_porosity is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PHYSICS_INPUT_REQUIRED",
            "error_message": (
                physics_refusal_reason(
                    "landlab_groundwater_water_table", provenance
                ) or "aquifer K/porosity unresolved (law 9)."
            ),
        }

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="groundwater_steady",
            target_resolution_m=float(target_resolution_m),
            gw_hydraulic_conductivity_m_s=float(gw_hydraulic_conductivity_m_s),
            gw_porosity=float(gw_porosity),
            gw_aquifer_thickness_m=float(gw_aquifer_thickness_m),
            gw_recharge_mm_yr=float(gw_recharge_mm_yr),
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab groundwater arguments: {exc}",
        }

    logger.info(
        "landlab_groundwater_water_table bbox=%s K=%.1e n=%.2f H=%.0fm "
        "rech=%.0fmm/yr res=%.1fm",
        run_args.bbox,
        run_args.gw_hydraulic_conductivity_m_s,
        run_args.gw_porosity,
        run_args.gw_aquifer_thickness_m,
        run_args.gw_recharge_mm_yr,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_groundwater_water_table(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_groundwater_water_table complete layer_id=%s mean_dtw=%.2f m "
            "baseflow=%.4f m3/s seep_frac=%.3f rel_err=%.3e uri=%s",
            primary.layer_id,
            primary.mean_depth_to_water_m,
            primary.baseflow_discharge_m3s,
            primary.seeping_area_fraction,
            primary.mass_balance_rel_error,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        GroundwaterWaterTableWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_groundwater_water_table failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("landlab_groundwater_water_table unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer (deterministic, no LLM in the chain -- Invariant 1):
#   fetch DEM -> stage -> run_solver('landlab') -> download depth-to-water +
#     water-table + seepage COGs -> postprocess -> publish depth-to-water raster
#     -> add water-table + seepage context rasters -> emit baseflow-partition chart.
# Reuses the shared _composer_common stage_solve_download off-box seam.
# --------------------------------------------------------------------------- #
async def model_landlab_groundwater_water_table(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabGroundwaterLayerURI:
    """Compose the Landlab steady groundwater chain end-to-end (OFF-BOX lane).

    Returns the primary depth-to-water ``LandlabGroundwaterLayerURI``; emits the
    water-table + seepage context rasters + the baseflow-partition chart as side
    effects on the bound emitter.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args,
        dem_path=dem_path,
        run_id=run_id,
        compute_class=compute_class,
    )

    water_table_cog = solve.secondary_cogs.get("water_table_elevation")
    seepage_cog = solve.secondary_cogs.get("seepage_specific_discharge")

    try:
        layers, metrics = await asyncio.to_thread(
            postprocess_landlab_groundwater,
            solve.local_field,
            run_id=solve.run_id,
            result=solve.result_block,
            water_table_cog_path=water_table_cog,
            seepage_cog_path=seepage_cog,
        )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise GroundwaterWaterTableWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_groundwater produced no depth-to-water layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    primary = await asyncio.to_thread(
        _publish_depth_to_water_layer, raw_primary, solve.run_id
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
            pub_ctx = await asyncio.to_thread(_publish_context_raster, ctx)
            try:
                await emitter.add_loaded_layer(pub_ctx)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add groundwater context raster: %s", exc)

    chart_spec = build_baseflow_partition_chart_spec(
        float(metrics.get("groundwater_underflow_m3s", 0.0)),
        float(metrics.get("surface_seepage_m3s", 0.0)),
    )
    await emit_landlab_chart(
        emitter,
        chart_spec,
        title="Steady baseflow partition (underflow vs seepage)",
        caption=(
            "How the steady catchment baseflow splits between subsurface "
            "groundwater underflow leaving the boundary and surface seepage "
            "(return flow) -- the two pathways recharge exits the aquifer."
        ),
        source_uri=primary.uri,
    )

    await emit_zoom_to(emitter, solve.bbox)

    logger.info(
        "model_landlab_groundwater_water_table complete run_id=%s mean_dtw=%.2f m "
        "baseflow=%.4f m3/s seep_frac=%.3f rel_err=%.3e uri=%s",
        solve.run_id,
        primary.mean_depth_to_water_m,
        primary.baseflow_discharge_m3s,
        primary.seeping_area_fraction,
        primary.mass_balance_rel_error,
        primary.uri,
    )
    return primary


def _publish_depth_to_water_layer(
    raw_primary: LandlabGroundwaterLayerURI, run_id: str
) -> LandlabGroundwaterLayerURI:
    """Publish the depth-to-water COG through publish_layer (the render seam).

    On publish failure the raw layer is returned UNCHANGED (the dispatch-level
    guardrail drops the dead raw-s3 raster; the typed scalars still narrate)."""
    if raw_primary.layer_type != "raster" or not (
        raw_primary.uri.startswith("gs://") or raw_primary.uri.startswith("s3://")
    ):
        return raw_primary
    style = raw_primary.style_preset or DEPTH_TO_WATER_STYLE_PRESET
    try:
        published_uri = publish_layer(
            layer_uri=raw_primary.uri,
            layer_id=raw_primary.layer_id,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_groundwater_water_table: publish_layer FAILED layer_id=%s "
            "error_code=%s (%s) - returning the unpublished layer.",
            raw_primary.layer_id,
            exc.error_code,
            exc,
        )
        return raw_primary
    return raw_primary.model_copy(update={"uri": published_uri, "style_preset": style})


def _publish_context_raster(ctx: LayerURI) -> LayerURI:
    """Publish a context raster (water table / seepage) through publish_layer;
    degrade to the raw layer on failure (the dispatch guardrail drops a dead one)."""
    if ctx.layer_type != "raster" or not (
        ctx.uri.startswith("gs://") or ctx.uri.startswith("s3://")
    ):
        return ctx
    try:
        published_uri = publish_layer(
            layer_uri=ctx.uri,
            layer_id=ctx.layer_id,
            style_preset=ctx.style_preset,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_groundwater_water_table: context publish_layer FAILED "
            "layer_id=%s error_code=%s (%s) - returning the unpublished layer.",
            ctx.layer_id,
            exc.error_code,
            exc,
        )
        return ctx
    return ctx.model_copy(update={"uri": published_uri})
