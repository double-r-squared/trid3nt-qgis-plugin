"""Engine template ``geoclaw_amr_refinement_regions`` - a GeoClaw run whose
adaptive-mesh refinement is driven by EXPLICIT lat/lon/time region windows rather
than default error-based flagging alone.

A distinct question CLASS from ``geoclaw_inundation`` (per the capability-naming
rule): this asks HOW the AMR mesh is controlled - the user supplies a list of
region windows, each forcing a lat/lon box to a minimum/maximum AMR level over a
time interval. GeoClaw's ``regiondata.regions`` combines overlapping regions by
the MAX of the covering windows' min/max levels, so a window can hold a subregion
at a fixed fine level for a chosen interval (e.g. resolve a harbour only while the
wave is arriving) while ``flag2refine`` error estimation still governs elsewhere.

Rides the EXISTING GeoClaw inundation deck surface: it configures the run with the
explicit ``amr_regions`` threaded onto the setrun ``regiondata`` block, runs the
SAME fetch -> deck -> solve -> postprocess chain (``model_geoclaw_inundation``),
and returns the peak-inundation ``GeoClawDepthLayerURI``.

Determinism boundary (Invariant 1): every number the agent narrates
(``max_depth_m`` / ``flooded_area_km2`` / ``max_inundation_m`` / ``arrival_time_s``)
comes from the typed ``GeoClawDepthLayerURI`` fields the worker / postprocess
computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.geoclaw_contracts import (
    AmrRegionWindow,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.geoclaw._template_card import TemplateCard
from trid3nt_server.agent.workflows.geoclaw.inundation.inundation import (
    GeoClawComposerError,
    model_geoclaw_inundation,
)
from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
    PostprocessGeoClawError,
)
from trid3nt_server.agent.workflows.geoclaw.run_geoclaw import GeoClawWorkflowError

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.geoclaw.amr_regions.amr_regions"
)

__all__ = ["geoclaw_amr_refinement_regions"]


#: Curated door-listing card. One-line question + the real required inputs + a
#: knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "control GeoClaw AMR refinement with EXPLICIT lat/lon/time region windows "
        "(force a box to a min/max mesh level over an interval) instead of relying "
        "on default error flagging alone"
    ),
    required_inputs=["bbox", "amr_regions"],
    knobs=(
        "amr_regions=[{min_level,max_level,t_start_s,t_end_s,min_lon,max_lon,"
        "min_lat,max_lat}], scenario, amr_levels, source_lonlat, source_magnitude, "
        "sim_duration_s, output_frames"
    ),
)


_METADATA = AtomicToolMetadata(
    name="geoclaw_amr_refinement_regions",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_amr_refinement_regions(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    amr_regions: list[dict[str, Any]] | None = None,
    scenario: str = "tsunami",
    source_lonlat: tuple[float, float] | list[float] | None = None,
    source_magnitude: float = 8.0,
    dam_break_depth_m: float = 10.0,
    sim_duration_s: float = 3600.0,
    output_frames: int = 24,
    amr_levels: int = 3,
    manning_n: float = 0.025,
    sea_level_m: float = 0.0,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs + the server confirm gate's injected confirmed=True.
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Run GeoClaw with explicit AMR refinement region windows (region-based flagging).

    Fidelity: GeoClaw adaptive-mesh finite-volume shallow-water run-up whose mesh
    refinement is FORCED by the supplied lat/lon/time regions (a min/max level per
    box+interval), layered on the engine default region tiers; planning-grade.
    Data: the topo/bathy DEM is REAL (fetch_topobathy -> fetch_dem). For a tsunami
    the source is a synthetic Okada displacement from source_lonlat +
    source_magnitude.
    Off-scope: the plain peak-inundation map with default flagging ->
    geoclaw_inundation; the coastal gauge waveform -> geoclaw_tsunami_gauge_timeseries;
    spatially-varying friction -> geoclaw_regional_manning_friction.

    Use this when: the user wants to CONTROL where/when/how finely the AMR mesh
    refines - pin a harbour or a stretch of coast to a fixed level for a time
    window, cap an offshore box coarse to save cost, or contrast explicit regions
    against default error-based refinement.

    Params:
        bbox: computational-domain AOI, EPSG:4326 (min_lon, min_lat, max_lon, max_lat).
        amr_regions: REQUIRED list of region windows; each is a dict with keys
            min_level, max_level, t_start_s, t_end_s, min_lon, max_lon, min_lat,
            max_lat. Each window forces its box to [min_level, max_level] over
            [t_start_s, t_end_s]. Appended AFTER the engine default tiers.
        scenario: driver family ("tsunami"|"dam_break"|"surge"; default "tsunami").
        source_lonlat: source location; unset -> AOI centroid (dam_break) or the
            composer offshore placement (tsunami).
        source_magnitude: synthetic-source Mw for a tsunami (default 8.0).
        dam_break_depth_m: raised-column height for dam_break (default 10.0).
        sim_duration_s: simulated time, seconds (default 3600).
        output_frames: animation frame count (default 24).
        amr_levels: maximum AMR levels available to the regions (default 3).
        manning_n: single global friction coefficient (default 0.025).
        sea_level_m: still-water datum (default 0.0).
        compute_class: compute class (default "standard").

    Returns:
        On success: ``GeoClawDepthLayerURI`` - the peak-inundation COG + depth
        scalars + arrival time. On failure: ``{"status": "error", "error_code",
        "error_message"}``. Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_amr_refinement_regions requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    if not amr_regions:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_amr_refinement_regions requires amr_regions: a non-empty "
                "list of {min_level, max_level, t_start_s, t_end_s, min_lon, "
                "max_lon, min_lat, max_lat} windows."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }

    src = None
    if source_lonlat is not None:
        src = (float(source_lonlat[0]), float(source_lonlat[1]))

    try:
        windows = [
            w if isinstance(w, AmrRegionWindow) else AmrRegionWindow(**dict(w))
            for w in amr_regions
        ]
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid amr_regions window: {exc}",
        }

    try:
        run_args = GeoClawRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            scenario=scenario,  # type: ignore[arg-type]
            source_lonlat=src,
            source_magnitude=float(source_magnitude),
            dam_break_depth_m=float(dam_break_depth_m),
            sim_duration_s=float(sim_duration_s),
            output_frames=int(output_frames),
            amr_levels=int(amr_levels),
            manning_n=float(manning_n),
            sea_level_m=float(sea_level_m),
            amr_regions=windows,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw amr-regions arguments: {exc}",
        }

    logger.info(
        "geoclaw_amr_refinement_regions bbox=%s scenario=%s amr_levels=%d regions=%d",
        run_args.bbox,
        run_args.scenario,
        run_args.amr_levels,
        len(run_args.amr_regions),
    )

    try:
        primary = await model_geoclaw_inundation(
            run_args,
            compute_class=compute_class,
        )
        logger.info(
            "geoclaw_amr_refinement_regions complete layer_id=%s max_depth_m=%.4g "
            "arrival_s=%s uri=%s",
            primary.layer_id,
            primary.max_depth_m,
            primary.arrival_time_s,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        GeoClawWorkflowError,
        PostprocessGeoClawError,
        GeoClawComposerError,
    ) as exc:
        logger.warning(
            "geoclaw_amr_refinement_regions failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "GEOCLAW_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_amr_refinement_regions unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
