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

    Simulates a fire front spreading from an ignition point across real
    LANDFIRE 30 m fuels + terrain under a constant scenario wind, producing a
    time-of-arrival map, an hourly burned-extent animation, and flame-length /
    spread-rate rasters.

    Use this when:
        - The user asks to MODEL / SIMULATE / FORECAST wildfire spread from a
          specific ignition point ("if a fire started here, where does it
          spread in 6 hours?"), or to explore wind / fuel-moisture what-ifs.

    Do NOT use this for:
        - OBSERVED fire perimeters or detections (use
          ``fetch_nifc_fire_perimeters`` / ``fetch_firms_active_fire`` /
          ``fetch_goes_active_fire``).
        - Satellite fire ANIMATIONS of a real event (use
          ``run_model_goes_fire_animation`` and siblings).
        - Post-fire debris-flow hazard (use ``model_debris_flow``).
        - Burn severity of past fires (use ``fetch_mtbs_burn_severity``).

    IGNITION POINT IS REQUIRED — NEVER GUESS IT. If the user has not given a
    concrete ignition location (a coordinate or an unambiguous named place you
    can geocode), DO NOT invent one: either ask the user, or call
    ``request_spatial_input(mode="point")`` so they click the ignition point on
    the map, then pass the returned ``coordinates`` here as ``ignition_lonlat``.

    Params:
        bbox: the simulation AOI ``(min_lon, min_lat, max_lon, max_lat)``
            EPSG:4326 (lon-first). CONUS-only (LANDFIRE fuels coverage) — an
            AOI outside CONUS fails with a typed error, never invented fuels.
            County-scale or smaller works best (a fetch is capped ~123 km).
        ignition_lonlat: REQUIRED ``(lon, lat)`` of the point ignition, inside
            ``bbox``. From the user's words or a ``request_spatial_input``
            map pick — never fabricated.
        wind_speed_mph: constant wind speed in mph (ELMFIRE's 20 ft
            convention). Default 15 (the canonical tutorial scenario).
        wind_dir_deg: wind direction in meteorological degrees — the direction
            the wind blows FROM (0 = from the north pushing the fire south,
            270 = from the west pushing it east). Range [0, 360]. Default 0.
        fuel_moisture: dead/live fuel-moisture preset, one of
            ``"dry"`` (critical fire weather: 1-h/10-h/100-h = 3/4/5 percent,
            live herb/woody 30/60 — the ELMFIRE tutorial constants),
            ``"moderate"`` (6/7/8, live 60/90), or
            ``"moist"`` (12/13/14, live 90/120 — marginal burning; spread may
            be minimal, which is reported honestly). Default ``"dry"``.
        duration_hours: simulated burn duration in hours (> 0, <= 48).
            Default 6. Also the animation length (one frame per hour).
        cellsize_m: computational cell size in metres. Default 30 (LANDFIRE
            native). Coarsen (e.g. 60-90) for very large AOIs.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``FireSpreadLayerURI`` (a ``LayerURI`` subtype) — the
        map renders the fire-arrival-time COG and the hourly burned-extent
        frames arrive as a scrubber animation group, plus flame-length and
        spread-rate layers. It carries ``burned_area_km2`` +
        ``fire_arrival_max_hr`` + ``max_flame_length_m`` +
        ``max_spread_rate_m_min`` (Invariant 1 — narrate these typed numbers,
        never invent them).

        On failure: a dict with ``status="error"`` + ``error_code`` +
        ``error_message`` so the failure is narrated honestly (no layer).
        Notable typed results: ``FIRE_IGNITION_REQUIRED`` (no ignition given —
        ask the user / request a map pick), ``ELMFIRE_NO_SPREAD`` (the model
        ran but nothing burned — nonburnable fuels at the ignition).

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` — the cache shim is NOT invoked.
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
