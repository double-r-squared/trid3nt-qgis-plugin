"""Atomic tool ``model_fire_spread`` — ELMFIRE wildfire-spread engine (FIRE-3).

The LLM-facing exposure of the ELMFIRE level-set fire-spread engine.
``model_fire_spread(...)`` takes the AOI + a REQUIRED ignition point + the
scenario weather dial, runs the deterministic fetch -> deck-build -> solve ->
postprocess chain (``workflows/model_fire_spread_scenario.py``), and returns a
``FireSpreadLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the fire analogue of ``run_geoclaw_inundation`` (GeoClaw) /
``run_model_flood_scenario`` (SFINCS). Like those wrappers it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 — workflow exposure surface;
never touches the cache shim). Confirmation before consequence (Invariant 9 —
a solver run) is enforced by the server solver-confirm gate around this tool
(``SOLVER_CONFIRM_TOOLS``): the user sees the cell count + estimated runtime
before the solve dispatches.

Determinism boundary (Invariant 1): every number the agent narrates
(``burned_area_km2`` / ``fire_arrival_max_hr`` / flame length / spread rate)
comes from the typed ``FireSpreadLayerURI`` fields the postprocess computed —
never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.elmfire_contracts import (
    ElmfireRunArgs,
    FireSpreadLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.model_fire_spread_scenario import (
    FireSpreadComposerError,
    model_fire_spread_scenario,
)
from trid3nt_server.workflows.postprocess_elmfire import PostprocessElmfireError
from trid3nt_server.workflows.run_elmfire import ElmfireWorkflowError

logger = logging.getLogger("trid3nt_server.tools.simulation.model_fire_spread")

__all__ = ["model_fire_spread"]


_MODEL_FIRE_SPREAD_METADATA = AtomicToolMetadata(
    name="model_fire_spread",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _MODEL_FIRE_SPREAD_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (LANDFIRE/3DEP fetches go through the cache shim
    # tools; the solve itself is a local container / intra-cloud Batch task),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def model_fire_spread(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    ignition_lonlat: tuple[float, float] | list[float] | None = None,
    wind_speed_mph: float = 15.0,
    wind_dir_deg: float = 0.0,
    fuel_moisture: str = "dry",
    duration_hours: float = 6.0,
    cellsize_m: float = 30.0,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders). Also absorbs the
    # server confirm gate's injected ``confirmed=True``.
    **_extra_ignored: Any,
) -> FireSpreadLayerURI | dict[str, Any]:
    """Run an ELMFIRE wildfire-spread simulation from a point ignition.

    Use this when: the user wants to MODEL/SIMULATE/FORECAST wildfire
    spread from a specific ignition ("if a fire started here, where does
    it spread in 6 hours?") or explore wind/fuel-moisture what-ifs over
    LANDFIRE 30m fuels+terrain. Do NOT use for: observed fire
    perimeters/detections (``fetch_nifc_fire_perimeters``/
    ``fetch_firms_active_fire``/``fetch_goes_active_fire``); satellite
    animations of a real event (``run_model_goes_fire_animation``);
    post-fire debris-flow (``model_debris_flow``); past burn severity
    (``fetch_mtbs_burn_severity``).

    IGNITION POINT IS REQUIRED -- NEVER GUESS IT. If not given, ask the
    user or call ``request_spatial_input(mode="point")`` and pass the
    returned coordinates as ``ignition_lonlat``.

    Params:
        bbox: simulation AOI, EPSG:4326. CONUS-only (LANDFIRE coverage);
            county-scale or smaller (fetch capped ~123km).
        ignition_lonlat: REQUIRED (lon, lat) inside bbox.
        wind_speed_mph: constant wind speed (ELMFIRE 20ft convention,
            default 15).
        wind_dir_deg: direction wind blows FROM, meteorological deg
            [0,360] (default 0).
        fuel_moisture: ``"dry"`` (default, critical fire weather),
            ``"moderate"``, or ``"moist"`` (marginal burning).
        duration_hours: burn duration (>0, <=48, default 6); also
            animation frame count.
        cellsize_m: computational cell size (default 30, LANDFIRE native).
        compute_class: compute class (default "standard").

    Returns:
        On success: ``FireSpreadLayerURI`` -- fire-arrival-time COG,
        hourly burned-extent scrubber animation, flame-length/spread-rate
        layers, with ``burned_area_km2``, ``fire_arrival_max_hr``,
        ``max_flame_length_m``, ``max_spread_rate_m_min``.
        On failure: ``{"status": "error", "error_code", "error_message"}``
        -- notably ``FIRE_IGNITION_REQUIRED`` or ``ELMFIRE_NO_SPREAD``
        (nonburnable fuels at ignition). Not cached
        (``cacheable=False``).
    """
    # --- ignition: REQUIRED, never fabricated --------------------------------
    # All SHAPE handling (string "lon,lat" / dict ignition, string / reordered
    # / point-collapsed / missing bbox deriving a ~5 km domain) lives in
    # ElmfireRunArgs' before-validators - the wrapper no longer pre-validates
    # (its old manual checks rejected the very shapes the contract coerces;
    # observed live 2026-07-08).
    if ignition_lonlat is None:
        return {
            "status": "error",
            "error_code": "FIRE_IGNITION_REQUIRED",
            "error_message": (
                "model_fire_spread requires an ignition point "
                "(ignition_lonlat=[lon, lat]) and it must come from the USER. "
                "Do NOT invent one: ask the user where the fire starts, or "
                "call request_spatial_input(mode='point') so they click the "
                "ignition point on the map, then pass the returned "
                "coordinates as ignition_lonlat."
            ),
        }

    try:
        run_args = ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition_lonlat,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": f"invalid fire-spread run arguments: {exc}",
        }

    logger.info(
        "model_fire_spread bbox=%s ignition=%s wind=%.1fmph@%.0fdeg "
        "moisture=%s duration=%.1fh cellsize=%.0fm",
        run_args.bbox,
        run_args.ignition_lonlat,
        run_args.wind_speed_mph,
        run_args.wind_dir_deg,
        run_args.fuel_moisture,
        run_args.duration_hours,
        run_args.cellsize_m,
    )

    try:
        primary = await model_fire_spread_scenario(
            run_args, compute_class=compute_class
        )
        logger.info(
            "model_fire_spread complete layer_id=%s burned_area_km2=%.4g "
            "arrival_max_hr=%.3g uri=%s",
            primary.layer_id,
            primary.burned_area_km2,
            primary.fire_arrival_max_hr,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        ElmfireWorkflowError,
        PostprocessElmfireError,
        FireSpreadComposerError,
    ) as exc:
        logger.warning("model_fire_spread failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        logger.exception("model_fire_spread unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }
