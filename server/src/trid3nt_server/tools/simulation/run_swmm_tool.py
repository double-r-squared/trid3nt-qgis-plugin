"""Atomic tool ``run_swmm_urban_flood`` — PySWMM quasi-2D urban-flood engine
(sprint-16 P4, Path A — the LOCAL lane).

The LLM-facing exposure of the quasi-2D PySWMM urban-flood engine (NATE's
PCSWMM screenshot path: animated overland depth around BUILDING obstructions +
a SOUND BARRIER with RED walls / GREEN flap gates). ``run_swmm_urban_flood(...)``
takes the ``SWMMRunArgs`` forcing/structure fields, runs the deterministic
fetch -> build -> solve -> postprocess chain
(``workflows/model_urban_flood_swmm.py``), and returns a ``SWMMDepthLayerURI``
the emitter loads onto the map (it subclasses ``LayerURI`` so the
``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the SWMM analogue of ``run_modflow_job`` (MODFLOW) and
``run_model_flood_scenario`` (SFINCS). Like those wrappers it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 — workflow exposure surface; never
touches the cache shim). Confirmation before consequence (Invariant 9 — a solver
run) is enforced by the server confirmation hook around this tool, not
re-implemented here.

The urban engine runs pyswmm IN-PROCESS (the dev primary path — pyswmm 2.1.0 is
in the agent venv and SWMM5 is fully headless), so unlike SFINCS/MODFLOW it
needs no external solver substrate to produce a real solved ``.out``.

Determinism boundary (Invariant 1): every depth number the agent narrates comes
from the typed ``SWMMDepthLayerURI.max_depth_m`` / ``.flooded_area_km2`` /
``.n_buildings_affected`` fields the postprocess computed — never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.swmm_contracts import SWMMDepthLayerURI, SWMMRunArgs
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.model_urban_flood_swmm import (
    UrbanFloodWorkflowError,
    model_urban_flood_swmm,
)
from trid3nt_server.workflows.postprocess_swmm import PostprocessSWMMError
from trid3nt_server.workflows.run_swmm import SWMMWorkflowError

logger = logging.getLogger("trid3nt_server.tools.simulation.run_swmm_tool")

__all__ = ["run_swmm_urban_flood", "RunSWMMError"]


class RunSWMMError(RuntimeError):
    """Raised when the SWMM chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` propagated from the failing stage so the
    agent emitter renders a typed error frame (the emitter's
    ``_classify_exception`` reads ``error_code`` off the exception)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_RUN_SWMM_URBAN_FLOOD_METADATA = AtomicToolMetadata(
    name="run_swmm_urban_flood",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_SWMM_URBAN_FLOOD_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (in-process pyswmm + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_swmm_urban_flood(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    return_period_yr: int = 100,
    total_rain_depth_mm: float | None = None,
    storm_duration_hr: float = 6.0,
    rain_interval_min: int = 5,
    building_representation: str = "drop",
    infiltration_method: str = "none",
    target_resolution_m: float = 10.0,
    manning_overland: float = 0.03,
    mass_balance_tolerance_pct: float = 5.0,
    barriers: dict[str, Any] | None = None,
    pollutants: list[str] | None = None,
    dry_buildup_days: int = 0,
    washoff_model: str = "exp",
    compute_class: str = "standard",
    enable_autoscale: bool = True,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> SWMMDepthLayerURI | dict[str, Any]:
    """Run a quasi-2D PySWMM urban (pluvial) flood simulation over an AOI.

    Builds a quasi-2D SWMM deck from the AOI DEM + OSM building footprints (one
    storage node per overland cell, 4-connectivity conduits, per-cell rainfall
    subcatchments fed by an Atlas-14 nested design-storm hyetograph, a single
    boundary outfall), runs pyswmm headless in-process, rasterizes the
    per-timestep node depth onto the mesh grid, and returns a
    ``SWMMDepthLayerURI`` carrying the peak overland-depth COG + narration
    scalars. Per-timestep depth frames are emitted as a temporal animation
    group the LayerPanel scrubber plays.

    Use this when:
        - The user asks to model urban/pluvial/drainage/stormwater flooding,
          street-level inundation from a design storm, flooding AROUND
          buildings, or a SWMM/PCSWMM-style urban flood over a city
          block/neighborhood AOI.
        - The scenario involves structural flood controls: a SOUND BARRIER/
          flood WALL (dams water) or a FLAP GATE/one-way drain (passes one
          direction only) — pass these as ``barriers``.
        - The user asks how much POLLUTANT/TSS/SEDIMENT/E.coli/BACTERIA/
          fecal coliform/NUTRIENT (nitrogen/phosphorus) WASHES OFF streets to
          the storm OUTFALL (buildup + washoff) — pass ``pollutants`` (e.g.
          ["tss", "e_coli"]) to add the outfall pollutograph + cumulative
          load + peak washoff-concentration layer alongside the depth result.

    Do NOT use this for:
        - Riverine/coastal/large-watershed flooding (``run_model_flood_scenario``
          = SFINCS).
        - A river-reach dye/sediment slug or in-stream transport
          (``run_telemac`` — river hydrodynamics + GAIA sediment/WAQTEL decay).
        - A groundwater contamination plume in an aquifer (``run_modflow_job``
          — MODFLOW-GWT subsurface transport).
        - Cancelling a running sim (use the WS ``cancel`` envelope).

    Params:
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Keep it
            small — a city block/neighborhood, not a county (adaptive-mesh
            budget coarsens a large AOI).
        return_period_yr: design-storm return period, years (Atlas-14).
            Default 100; ignored when ``total_rain_depth_mm`` is given.
        total_rain_depth_mm: OPTIONAL explicit total storm depth, mm (> 0);
            overrides the Atlas-14 lookup when set.
        storm_duration_hr: design-storm duration, hours (> 0). Default 6.
        rain_interval_min: hyetograph timestep, minutes (> 0). Default 5.
        building_representation: one of {"drop", "raise", "roughness"}.
            ``"drop"`` (default) = building cells become holes, water routes
            AROUND them; ``"raise"`` = cells dam flow; ``"roughness"`` =
            cells bump Manning n.
        infiltration_method: pervious-fraction loss model. ``"none"``
            (default, fully impervious) | ``"scs_cn"`` (SCS Curve Number) |
            ``"green_ampt"``.
        target_resolution_m: requested overland cell size, m (> 0). Default
            10; subject to the adaptive-mesh budget for large AOIs.
        manning_overland: overland-flow Manning n (> 0). Default 0.03.
        mass_balance_tolerance_pct: honesty gate — if SWMM Flow Routing
            Continuity error EXCEEDS this (%), raises typed
            ``SWMM_MASS_BALANCE_EXCEEDED`` instead of publishing a
            silently-wrong depth layer. Default 5%.
        barriers: OPTIONAL GeoJSON FeatureCollection of tagged LineString
            segments; each feature's ``properties.barrier_type`` in
            {"wall", "flap_gate"}: RED ``wall`` omits the overland conduit
            (hard dam), GREEN ``flap_gate`` is a one-way SWMM orifice.
            ``None`` for a plain run.
        pollutants: OPTIONAL list to model buildup/washoff for — any of
            ``"tss"`` (suspended solids), ``"e_coli"`` (bacteria/fecal
            coliform), ``"tn"`` (total nitrogen), ``"tp"`` (total
            phosphorus). ``None`` (default) = plain depth-only hydraulics.
            When set, result carries the outfall pollutograph + cumulative
            load + peak concentration layer. Buildup/washoff coefficients
            are EPA-typical demo defaults (never site-calibrated — no
            per-site pollutant fetcher).
        dry_buildup_days: OPTIONAL antecedent dry days pollutant buildup
            accumulates before the storm (>= 0, default 0); only meaningful
            with ``pollutants`` set.
        washoff_model: ``"exp"`` (default, buildup-driven first-flush) or
            ``"emc"`` (fixed event-mean concentration, no first flush); only
            meaningful with ``pollutants`` set.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.
        enable_autoscale: True (default) lets the adaptive-mesh budget
            COARSEN ``target_resolution_m`` so a large AOI fits the cell
            cap. False honours ``target_resolution_m`` exactly — set only
            by the server-side #154 granularity gate; LLMs should leave
            unset.

    Returns:
        On success: ``SWMMDepthLayerURI`` (``LayerURI`` subtype) — emitter
        loads the peak overland-depth COG onto the map. Carries
        ``max_depth_m`` / ``flooded_area_km2`` / ``n_buildings_affected``
        (narrate these typed numbers only — invariant 1) and echoes
        ``barriers`` back so the client draws RED walls/GREEN flap gates.
        Per-timestep depth frames emitted out-of-band as a temporal group.
        On failure: dict with ``status="error"`` + ``error_code`` +
        ``error_message`` (no layer).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` — cache shim not invoked.
    """
    # --- Validate + coerce into the SWMMRunArgs contract --------------------
    if bbox is None:
        return {
            "status": "error",
            "error_code": "SWMM_PARAMS_INCOMPLETE",
            "error_message": (
                "run_swmm_urban_flood requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    # Bedrock/Gemini frequently pass bbox as a STRING; coerce robustly BEFORE the
    # contract validation (mirrors run_modflow_job's coerce_latlon guard).
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "SWMM_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }
    try:
        kwargs: dict[str, Any] = dict(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            return_period_yr=int(return_period_yr),
            storm_duration_hr=float(storm_duration_hr),
            rain_interval_min=int(rain_interval_min),
            building_representation=building_representation,
            infiltration_method=infiltration_method,
            target_resolution_m=float(target_resolution_m),
            manning_overland=float(manning_overland),
            mass_balance_tolerance_pct=float(mass_balance_tolerance_pct),
        )
        if total_rain_depth_mm is not None:
            kwargs["total_rain_depth_mm"] = float(total_rain_depth_mm)
        if barriers is not None:
            kwargs["barriers"] = barriers
        # WQ (sprint-WQ): thread the OPTIONAL pollutant params. A bare urban-flood
        # call leaves pollutants None => byte-identical depth-only deck.
        if pollutants:
            kwargs["pollutants"] = [str(p) for p in pollutants]
            kwargs["dry_buildup_days"] = int(dry_buildup_days)
            kwargs["washoff_model"] = str(washoff_model)
        run_args = SWMMRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "SWMM_PARAMS_INVALID",
            "error_message": f"invalid SWMM run arguments: {exc}",
        }

    logger.info(
        "run_swmm_urban_flood bbox=%s return_period=%dyr duration=%.1fh "
        "building=%s infiltration=%s res=%.1fm barriers=%s",
        run_args.bbox,
        run_args.return_period_yr,
        run_args.storm_duration_hr,
        run_args.building_representation,
        run_args.infiltration_method,
        run_args.target_resolution_m,
        bool(run_args.barriers),
    )

    try:
        peak = await model_urban_flood_swmm(
            run_args,
            compute_class=compute_class,
            enable_autoscale=bool(enable_autoscale),
        )
        logger.info(
            "run_swmm_urban_flood complete layer_id=%s max_depth_m=%.4g "
            "flooded_area_km2=%.6g n_buildings_affected=%d uri=%s",
            peak.layer_id,
            peak.max_depth_m,
            peak.flooded_area_km2,
            peak.n_buildings_affected,
            peak.uri,
        )
        return peak
    except asyncio.CancelledError:
        raise
    except (SWMMWorkflowError, PostprocessSWMMError, UrbanFloodWorkflowError) as exc:
        logger.warning("run_swmm_urban_flood failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        logger.exception("run_swmm_urban_flood unexpected failure")
        return {
            "status": "error",
            "error_code": "SWMM_INTERNAL_ERROR",
            "error_message": str(exc),
        }
