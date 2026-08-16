"""Engine template ``geoclaw_regional_manning_friction`` - a GeoClaw run with a
spatially-varying Manning bottom-friction coefficient (different n by topography
band) instead of a single global n.

A distinct question CLASS from ``geoclaw_inundation`` (per the capability-naming
rule): this asks HOW bottom friction varies across the domain. GeoClaw selects the
Manning coefficient per cell from ``geo_data.manning_coefficient`` (a list of n
values) split by ``geo_data.manning_break`` (a list of topography-elevation
breakpoints): a cell with topography B below the first break uses the first
coefficient, between successive breaks uses the intermediate coefficients, and
above the last break uses the last coefficient (e.g. a low offshore n for B < 0
and a higher onshore/vegetated n for B >= 0 with a single break at 0.0).

Rides the EXISTING GeoClaw inundation deck surface: it configures the run with the
banded friction threaded onto the setrun ``geo_data`` block, runs the SAME fetch
-> deck -> solve -> postprocess chain (``model_geoclaw_inundation``), and returns
the peak-inundation ``GeoClawDepthLayerURI``.

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
    "trid3nt_server.agent.workflows.geoclaw.regional_manning.regional_manning"
)

__all__ = ["geoclaw_regional_manning_friction"]


#: Curated door-listing card. One-line question + the real required inputs + a
#: knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "run GeoClaw with a spatially-varying Manning bottom-friction (different n "
        "onshore vs offshore, split by topography-elevation breakpoints) instead of "
        "a single global n"
    ),
    required_inputs=["bbox", "manning_coefficients"],
    knobs=(
        "manning_coefficients=[n_below_first_break, ..., n_above_last_break], "
        "manning_break=[elevation breakpoints, ascending], scenario, amr_levels, "
        "source_lonlat, source_magnitude, sim_duration_s, output_frames"
    ),
)


_METADATA = AtomicToolMetadata(
    name="geoclaw_regional_manning_friction",
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
async def geoclaw_regional_manning_friction(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    manning_coefficients: list[float] | None = None,
    manning_break: list[float] | None = None,
    scenario: str = "tsunami",
    source_lonlat: tuple[float, float] | list[float] | None = None,
    source_magnitude: float = 8.0,
    dam_break_depth_m: float = 10.0,
    sim_duration_s: float = 3600.0,
    output_frames: int = 24,
    amr_levels: int = 2,
    sea_level_m: float = 0.0,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs + the server confirm gate's injected confirmed=True.
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Run GeoClaw with a spatially-varying (banded) Manning bottom-friction coefficient.

    Fidelity: GeoClaw adaptive-mesh finite-volume shallow-water run-up whose Manning
    friction varies by topography band (``manning_coefficients`` split by
    ``manning_break`` elevation breakpoints); planning-grade.
    Data: the topo/bathy DEM is REAL (fetch_topobathy -> fetch_dem). For a tsunami
    the source is a synthetic Okada displacement from source_lonlat +
    source_magnitude.
    Off-scope: a single global friction -> geoclaw_inundation; explicit AMR region
    control -> geoclaw_amr_refinement_regions; the coastal gauge waveform ->
    geoclaw_tsunami_gauge_timeseries.

    Use this when: the user wants friction to DIFFER by terrain - a smooth
    low-friction offshore/channel n and a rougher onshore/vegetated n - to see how
    the roughness split changes run-up depth and inland reach.

    Params:
        bbox: computational-domain AOI, EPSG:4326 (min_lon, min_lat, max_lon, max_lat).
        manning_coefficients: REQUIRED list of Manning n values (each > 0), one per
            topography band. A single break at 0.0 with two values gives an
            offshore n (B < 0) and an onshore n (B >= 0).
        manning_break: elevation breakpoints (m, ascending) splitting the bands;
            length must be len(manning_coefficients) - 1. Default [0.0] when two
            coefficients are given.
        scenario: driver family ("tsunami"|"dam_break"|"surge"; default "tsunami").
        source_lonlat: source location; unset -> AOI centroid (dam_break) or the
            composer offshore placement (tsunami).
        source_magnitude: synthetic-source Mw for a tsunami (default 8.0).
        dam_break_depth_m: raised-column height for dam_break (default 10.0).
        sim_duration_s: simulated time, seconds (default 3600).
        output_frames: animation frame count (default 24).
        amr_levels: AMR refinement levels (default 2).
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
                "geoclaw_regional_manning_friction requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    if not manning_coefficients:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_regional_manning_friction requires manning_coefficients: a "
                "non-empty list of Manning n values (one per topography band)."
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

    coeffs = [float(n) for n in manning_coefficients]
    # Default a single elevation break at MSL (0.0 m) for the common two-band
    # (offshore vs onshore) case when the caller supplies coefficients but no break.
    if manning_break is not None:
        breaks = [float(b) for b in manning_break]
    elif len(coeffs) == 2:
        breaks = [0.0]
    else:
        breaks = []

    src = None
    if source_lonlat is not None:
        src = (float(source_lonlat[0]), float(source_lonlat[1]))

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
            sea_level_m=float(sea_level_m),
            manning_coefficients=coeffs,
            manning_break=breaks,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw regional-manning arguments: {exc}",
        }

    logger.info(
        "geoclaw_regional_manning_friction bbox=%s scenario=%s n=%s breaks=%s amr=%d",
        run_args.bbox,
        run_args.scenario,
        run_args.manning_coefficients,
        run_args.manning_break,
        run_args.amr_levels,
    )

    try:
        primary = await model_geoclaw_inundation(
            run_args,
            compute_class=compute_class,
        )
        logger.info(
            "geoclaw_regional_manning_friction complete layer_id=%s max_depth_m=%.4g "
            "flooded_km2=%.4g uri=%s",
            primary.layer_id,
            primary.max_depth_m,
            primary.flooded_area_km2,
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
            "geoclaw_regional_manning_friction failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "GEOCLAW_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_regional_manning_friction unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
