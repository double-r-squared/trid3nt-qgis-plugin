"""Engine template ``landlab_groundwater_storm_recession`` - Landlab
GroundwaterDupuitPercolator driven by a Poisson storm sequence (the
groundwater_flow tutorial's storm-hydrograph chain).

A distinct question CLASS from ``landlab_groundwater_water_table`` (steady state):
through a SEQUENCE of storms, what does the groundwater seepage / return-flow
hydrograph look like, WHERE does seepage emerge during storms, and how fast does
the aquifer DRAIN between storms (the recession timescale)? It is its OWN
registered engine TEMPLATE (engine="landlab", tier="template").

``landlab_groundwater_storm_recession(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain: the ``GroundwaterDupuitPercolator`` forced
by a ``PrecipitationDistribution`` storm sequence and integrated transiently,
returning a ``LandlabGroundwaterStormLayerURI`` (the peak-seepage raster) plus
the baseflow-discharge-vs-time hydrograph chart. Landlab runs OFF-BOX in the
local-exec / Batch solver seam (exec_kind "exec"; no baked image).

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabGroundwaterStormLayerURI`` fields the worker/postprocess
computed. V&V: the transient mass-conservation gate (|rel error| < 1%). The DEM
is REAL; the aquifer block + the stochastic storm sequence are demo-defaulted
(labeled). The storm sequence is DETERMINISTIC (seeded).

Thematic tie: the storm-driven seepage IS the groundwater return-flow to the
surface a surface-only rain-on-grid run (e.g. TELEMAC RoG) omits -- this MODELS
it standalone, it is NOT a coupling of the two engines.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_GW_HYDRAULIC_CONDUCTIVITY_M_S,
    DEFAULT_GW_POROSITY,
    DEFAULT_GW_STORM_AQUIFER_THICKNESS_M,
    DEFAULT_GW_STORM_MEAN_DEPTH_MM,
    DEFAULT_GW_STORM_TOTAL_DAYS,
    LandlabGroundwaterStormLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.agent.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_landlab_chart,
    emit_zoom_to,
    stage_solve_download,
)
from trid3nt_server.agent.workflows.landlab._template_card import TemplateCard
from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
    SEEPAGE_STYLE_PRESET,
    PostprocessLandlabError,
    build_baseflow_hydrograph_chart_spec,
    postprocess_landlab_groundwater_storm,
)
from trid3nt_server.agent.workflows.landlab.run_landlab import (
    LandlabWorkflowError,
)
from trid3nt_server.agent.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.landlab.groundwater_storm_recession."
    "groundwater_storm_recession"
)

__all__ = [
    "landlab_groundwater_storm_recession",
    "model_landlab_groundwater_storm_recession",
    "GroundwaterStormWorkflowError",
]


class GroundwaterStormWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "storm-driven groundwater seepage HYDROGRAPH on a watershed DEM: through a "
        "sequence of storms, what does the baseflow / return-flow hydrograph look "
        "like, where does seepage emerge, and how fast does the aquifer drain "
        "(recession timescale) -- Landlab GroundwaterDupuitPercolator forced by a "
        "Poisson storm sequence"
    ),
    required_inputs=["bbox"],
    knobs=(
        "gw_hydraulic_conductivity_m_s, gw_porosity, gw_storm_aquifer_thickness_m, "
        "gw_storm_mean_depth_mm, gw_storm_total_days, target_resolution_m"
    ),
)


_METADATA = AtomicToolMetadata(
    name="landlab_groundwater_storm_recession",
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
async def landlab_groundwater_storm_recession(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    gw_hydraulic_conductivity_m_s: float = DEFAULT_GW_HYDRAULIC_CONDUCTIVITY_M_S,
    gw_porosity: float = DEFAULT_GW_POROSITY,
    gw_storm_aquifer_thickness_m: float = DEFAULT_GW_STORM_AQUIFER_THICKNESS_M,
    gw_storm_mean_depth_mm: float = DEFAULT_GW_STORM_MEAN_DEPTH_MM,
    gw_storm_total_days: float = DEFAULT_GW_STORM_TOTAL_DAYS,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabGroundwaterStormLayerURI | dict[str, Any]:
    """Trace the storm-driven groundwater seepage/baseflow hydrograph + aquifer recession over a watershed DEM (Landlab Dupuit aquifer).

    Fidelity: Landlab GroundwaterDupuitPercolator forced by a Poisson storm
    sequence and integrated transiently on a real AOI DEM; a planning-grade
    seepage/baseflow hydrograph, not an aquifer-test-calibrated model. V&V: the
    transient mass-conservation gate (|rel error| < 1%). Data: the DEM is REAL
    (USGS 3DEP via seam-1). The aquifer block + the stochastic storm sequence are
    demo defaults (labeled in source_note); the storm sequence is deterministic
    (seeded).
    Off-scope: steady-state water-table depth + seepage map ->
    landlab_groundwater_water_table; storm infiltration-vs-runoff partition ->
    landlab_green_ampt_overland_flow; surface flood depth timeseries ->
    landlab_overland_flow_timeseries; riverine/coastal inundation -> sfincs_flood.
    Note: the storm-driven seepage is the groundwater return-flow a surface-only
    rain-on-grid run omits -- this MODELS it standalone, it is NOT a coupling.

    Use this when: the user asks for a groundwater seepage / baseflow hydrograph
    through a storm sequence, where seepage emerges during storms, or the aquifer
    drainage / recession timescale of a catchment.

    Params:
        bbox: watershed / catchment AOI, EPSG:4326 (min_lon, min_lat, max_lon,
            max_lat).
        gw_hydraulic_conductivity_m_s: saturated hydraulic conductivity K, m/s
            (default demo permeable-sand K).
        gw_porosity: drainable aquifer porosity in (0, 1) (default demo 0.3).
        gw_storm_aquifer_thickness_m: max saturated thickness above the base, m
            (default demo 8 -- thinner so storms move the water table).
        gw_storm_mean_depth_mm: mean per-storm depth, mm (default demo 20).
        gw_storm_total_days: simulated span of the storm sequence, days (default
            120).
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the resolved aquifer +
            storm block for review before the solve; "auto" (default) proceeds.

    Returns:
        On success: ``LandlabGroundwaterStormLayerURI`` -- the peak-seepage COG,
        with ``peak_baseflow_m3s``, ``final_baseflow_m3s``,
        ``recession_timescale_days``, ``seeping_area_fraction``,
        ``mass_balance_rel_error``, ``n_storms``. A baseflow hydrograph chart is
        emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_groundwater_storm_recession requires a bbox "
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

    provenance: list[SyntheticInput] = [
        SyntheticInput(
            param="aquifer_properties",
            value=(
                f"K={gw_hydraulic_conductivity_m_s:.1e} m/s, "
                f"porosity={gw_porosity:.2f}, "
                f"thickness={gw_storm_aquifer_thickness_m:.0f} m"
            ),
            basis="default_demo",
            note="no aquifer-property fetcher yet; not aquifer-test-calibrated",
        ),
        SyntheticInput(
            param="storm_sequence",
            value=f"Poisson storms, mean depth {gw_storm_mean_depth_mm:.0f} mm, "
            f"{gw_storm_total_days:.0f} d span",
            basis="default_demo",
            note="labeled stochastic demo storm climatology (seeded, deterministic); "
            "not a fetched historical record",
        ),
    ]
    source_note = (
        f"transient GroundwaterDupuitPercolator forced by a Poisson storm sequence "
        f"(mean depth {gw_storm_mean_depth_mm:.0f} mm, {gw_storm_total_days:.0f} d); "
        f"aquifer block (K={gw_hydraulic_conductivity_m_s:.1e} m/s, "
        f"porosity={gw_porosity:.2f}, thickness={gw_storm_aquifer_thickness_m:.0f} m) "
        "is a demo default - no aquifer-property fetcher yet, not site-calibrated."
    )

    _review = await gate_input_review(
        tool_name="landlab_groundwater_storm_recession",
        mode=input_mode,
        entries=provenance,
        params={
            "gw_hydraulic_conductivity_m_s": gw_hydraulic_conductivity_m_s,
            "gw_porosity": gw_porosity,
            "gw_storm_aquifer_thickness_m": gw_storm_aquifer_thickness_m,
            "gw_storm_mean_depth_mm": gw_storm_mean_depth_mm,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": (
                f"landlab_groundwater_storm_recession {_review.cancel_reason}"
            ),
        }
    provenance = _review.entries
    gw_hydraulic_conductivity_m_s = float(
        _review.params.get("gw_hydraulic_conductivity_m_s", gw_hydraulic_conductivity_m_s)
    )
    gw_porosity = float(_review.params.get("gw_porosity", gw_porosity))
    gw_storm_aquifer_thickness_m = float(
        _review.params.get("gw_storm_aquifer_thickness_m", gw_storm_aquifer_thickness_m)
    )
    gw_storm_mean_depth_mm = float(
        _review.params.get("gw_storm_mean_depth_mm", gw_storm_mean_depth_mm)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="groundwater_storm",
            target_resolution_m=float(target_resolution_m),
            gw_hydraulic_conductivity_m_s=float(gw_hydraulic_conductivity_m_s),
            gw_porosity=float(gw_porosity),
            gw_storm_aquifer_thickness_m=float(gw_storm_aquifer_thickness_m),
            gw_storm_mean_depth_mm=float(gw_storm_mean_depth_mm),
            gw_storm_total_days=float(gw_storm_total_days),
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab groundwater storm arguments: {exc}",
        }

    logger.info(
        "landlab_groundwater_storm_recession bbox=%s K=%.1e n=%.2f H=%.0fm "
        "storm_depth=%.0fmm days=%.0f res=%.1fm",
        run_args.bbox,
        run_args.gw_hydraulic_conductivity_m_s,
        run_args.gw_porosity,
        run_args.gw_storm_aquifer_thickness_m,
        run_args.gw_storm_mean_depth_mm,
        run_args.gw_storm_total_days,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_groundwater_storm_recession(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_groundwater_storm_recession complete layer_id=%s peak_q=%.4f "
            "tau=%.1f d seep_frac=%.3f rel_err=%.3e n_storms=%d uri=%s",
            primary.layer_id,
            primary.peak_baseflow_m3s,
            primary.recession_timescale_days,
            primary.seeping_area_fraction,
            primary.mass_balance_rel_error,
            primary.n_storms,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        GroundwaterStormWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_groundwater_storm_recession failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("landlab_groundwater_storm_recession unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer (deterministic, no LLM in the chain -- Invariant 1):
#   fetch DEM -> stage -> run_solver('landlab') -> download peak-seepage COG ->
#     postprocess -> publish peak-seepage raster -> emit baseflow hydrograph chart.
# --------------------------------------------------------------------------- #
async def model_landlab_groundwater_storm_recession(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabGroundwaterStormLayerURI:
    """Compose the Landlab transient storm groundwater chain end-to-end (OFF-BOX).

    Returns the primary peak-seepage ``LandlabGroundwaterStormLayerURI``; emits
    the baseflow hydrograph chart as a side effect on the bound emitter.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args,
        dem_path=dem_path,
        run_id=run_id,
        compute_class=compute_class,
    )

    try:
        layers, metrics = await asyncio.to_thread(
            postprocess_landlab_groundwater_storm,
            solve.local_field,
            run_id=solve.run_id,
            result=solve.result_block,
        )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise GroundwaterStormWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_groundwater_storm produced no peak-seepage layer",
        )

    raw_primary = layers[0]
    primary = await asyncio.to_thread(
        _publish_peak_seepage_layer, raw_primary, solve.run_id
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

    chart_spec = build_baseflow_hydrograph_chart_spec(metrics.get("hydrograph") or [])
    await emit_landlab_chart(
        emitter,
        chart_spec,
        title="Groundwater baseflow hydrograph (storm sequence)",
        caption=(
            "Total groundwater + seepage discharge leaving the catchment boundary "
            "through the storm sequence -- the storm response and the between-storm "
            "recession the aquifer produces."
        ),
        source_uri=primary.uri,
    )

    await emit_zoom_to(emitter, solve.bbox)

    logger.info(
        "model_landlab_groundwater_storm_recession complete run_id=%s peak_q=%.4f "
        "tau=%.1f d seep_frac=%.3f rel_err=%.3e n_storms=%d uri=%s",
        solve.run_id,
        primary.peak_baseflow_m3s,
        primary.recession_timescale_days,
        primary.seeping_area_fraction,
        primary.mass_balance_rel_error,
        primary.n_storms,
        primary.uri,
    )
    return primary


def _publish_peak_seepage_layer(
    raw_primary: LandlabGroundwaterStormLayerURI, run_id: str
) -> LandlabGroundwaterStormLayerURI:
    """Publish the peak-seepage COG through publish_layer (the render seam).

    On publish failure the raw layer is returned UNCHANGED (the dispatch-level
    guardrail drops the dead raw-s3 raster; the typed scalars still narrate)."""
    if raw_primary.layer_type != "raster" or not (
        raw_primary.uri.startswith("gs://") or raw_primary.uri.startswith("s3://")
    ):
        return raw_primary
    style = raw_primary.style_preset or SEEPAGE_STYLE_PRESET
    try:
        published_uri = publish_layer(
            layer_uri=raw_primary.uri,
            layer_id=raw_primary.layer_id,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landlab_groundwater_storm_recession: publish_layer FAILED "
            "layer_id=%s error_code=%s (%s) - returning the unpublished layer.",
            raw_primary.layer_id,
            exc.error_code,
            exc,
        )
        return raw_primary
    return raw_primary.model_copy(update={"uri": published_uri, "style_preset": style})
