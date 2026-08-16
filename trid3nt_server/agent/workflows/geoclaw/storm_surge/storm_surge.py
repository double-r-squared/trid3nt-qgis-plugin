"""Engine template ``geoclaw_storm_surge`` - a GeoClaw tropical-cyclone storm
surge run driven by a PARAMETRIC-HOLLAND wind + pressure field from a storm track.

A distinct question CLASS from ``geoclaw_inundation`` (per the capability-naming
rule): this asks how high a HURRICANE surge floods a coast, forced by an analytic
Holland-1980 wind/pressure field built from the storm's track (eye location +
central pressure + max-wind radius/speed at each forecast time) - GeoClaw's
``geoclaw.surge`` module. It exposes the wind-stress DRAG LAW (none | Garratt |
Powell) as a first-class knob: the law measurably changes the surface stress and
so the surge height.

The storm track is USER-SUPPLIABLE (a list of forecast points); absent, the engine
synthesizes a NON-SITE-SPECIFIC demo storm making landfall at the AOI centroid
(surfaced as synthetic, never presented as a real historical storm). A real
historical track (e.g. an NHC best track) is supplied as ``storm_track``.

Rides the EXISTING GeoClaw inundation deck surface: it configures scenario="surge"
with the storm track + drag law + a pre-landfall run window, runs the SAME fetch
(real coastal topo-bathy) -> deck -> solve -> postprocess chain
(``model_geoclaw_inundation``), and returns the peak-surge ``GeoClawDepthLayerURI``
plus the coastal-gauge waveform.

Determinism boundary (Invariant 1): every number the agent narrates
(``max_depth_m`` / ``flooded_area_km2`` / ``max_inundation_m`` / the gauge scalars)
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
    StormTrackPoint,
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
    "trid3nt_server.agent.workflows.geoclaw.storm_surge.storm_surge"
)

__all__ = ["geoclaw_storm_surge"]


#: Curated door-listing card. One-line question + the real required inputs + a
#: knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "peak tropical-cyclone STORM SURGE depth + coastal-gauge waveform for a "
        "coastal AOI, forced by a parametric-Holland wind+pressure field from a "
        "storm track (with a selectable wind-stress drag law)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "storm_track=[[t_s, lon, lat, max_wind_speed_ms, max_wind_radius_m, "
        "central_pressure_pa, storm_radius_m], ...] (else a synthetic demo storm), "
        "wind_drag_law (none|garratt|powell), surge_t0_s (window start s from "
        "landfall), sim_duration_s, output_frames, amr_levels, sea_level_m, "
        "manning_n, coastal_gauge_lonlat"
    ),
)


_METADATA = AtomicToolMetadata(
    name="geoclaw_storm_surge",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    tier="template",
)


def _coerce_storm_track(raw: Any) -> list[StormTrackPoint]:
    """Coerce an LLM/user-supplied storm track into ``list[StormTrackPoint]``.

    Accepts a list where each point is either a 6/7-element sequence
    ``[t_s, lon, lat, max_wind_speed_ms, max_wind_radius_m, central_pressure_pa,
    (storm_radius_m)]`` or a dict with those keys. Empty / None -> ``[]`` (the
    engine then synthesizes a demo storm). Raises ValueError on a malformed point.
    """
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"storm_track must be a list of track points, got {raw!r}")
    pts: list[StormTrackPoint] = []
    for p in raw:
        if isinstance(p, dict):
            pts.append(StormTrackPoint(**p))
        elif isinstance(p, (list, tuple)) and len(p) in (6, 7):
            vals = [float(v) for v in p]
            if len(vals) == 6:
                vals.append(500000.0)
            pts.append(
                StormTrackPoint(
                    t_s=vals[0], lon=vals[1], lat=vals[2],
                    max_wind_speed_ms=vals[3], max_wind_radius_m=vals[4],
                    central_pressure_pa=vals[5], storm_radius_m=vals[6],
                )
            )
        else:
            raise ValueError(
                f"storm_track point must be a 6/7-element list or a dict, got {p!r}"
            )
    return pts


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_storm_surge(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    storm_track: list[Any] | None = None,
    wind_drag_law: str = "garratt",
    surge_t0_s: float | None = None,
    sim_duration_s: float = 86400.0,
    output_frames: int = 24,
    amr_levels: int = 2,
    manning_n: float = 0.025,
    sea_level_m: float = 0.0,
    coastal_gauge_lonlat: tuple[float, float] | list[float] | None = None,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs + the server confirm gate's injected confirmed=True.
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Run a GeoClaw tropical-cyclone STORM SURGE (parametric-Holland wind + pressure).

    Fidelity: GeoClaw adaptive-mesh finite-volume shallow-water surge driven by an
    analytic Holland-1980 wind/pressure field from the storm track; planning-grade
    surge envelope, not a calibrated operational forecast (e.g. ADCIRC/SLOSH).
    Data: the coastal topo/bathy DEM is REAL (fetch_topobathy -> fetch_dem). The
    storm is the SUPPLIED ``storm_track`` (e.g. an NHC best track), else a
    NON-SITE-SPECIFIC synthetic demo storm making landfall at the AOI centroid
    (surfaced as synthetic, never a real storm).
    Off-scope: a tsunami/dam-break run-up -> geoclaw_inundation; spectral wind-wave
    field -> swan_wave_field; rain-driven riverine/coastal compound flood ->
    sfincs_flood.

    Use this when: the user wants the coastal flood from a HURRICANE / tropical
    cyclone - "how high does <storm> surge flood <coast>", or a what-if surge from a
    track - and optionally to compare wind-stress drag laws.

    Params:
        bbox: coastal computational-domain AOI, EPSG:4326 (min_lon, min_lat,
            max_lon, max_lat).
        storm_track: OPTIONAL list of forecast points, each a 6/7-element list
            ``[t_s, lon, lat, max_wind_speed_ms, max_wind_radius_m,
            central_pressure_pa, storm_radius_m]`` (or a dict with those keys),
            with ``t_s`` SECONDS RELATIVE TO LANDFALL (t=0 at landfall; negative
            before), strictly ascending. Unset -> a synthetic demo storm.
        wind_drag_law: wind-stress drag law - "garratt" (default), "none", or
            "powell". The law measurably changes the surface stress -> surge height.
        surge_t0_s: run-window START, seconds from landfall (< 0 so the storm spins
            up). Unset -> the track's earliest time (or half the run before
            landfall for the demo storm).
        sim_duration_s: simulated time, seconds (default 86400 = 24 h). The run
            spans [surge_t0_s, surge_t0_s + sim_duration_s].
        output_frames: animation frame count (default 24).
        amr_levels: AMR refinement levels (default 2).
        manning_n: bottom-friction coefficient (default 0.025).
        sea_level_m: still-water datum, m (default 0.0; raise for a tide offset).
        coastal_gauge_lonlat: OPTIONAL (lon, lat) of a coastal tide gauge for the
            surge waveform. Unset -> a deterministic seaward-edge fallback.
        compute_class: compute class (default "standard").

    Returns:
        On success: ``GeoClawDepthLayerURI`` - the peak-surge COG + depth scalars
        (+ the coastal-gauge surge waveform scalars). On failure:
        ``{"status": "error", "error_code", "error_message"}``. Not cached.
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_storm_surge requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
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

    try:
        track = _coerce_storm_track(storm_track)
    except Exception as exc:  # noqa: BLE001 - malformed track point
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid storm_track: {exc}",
        }

    gauge = None
    if coastal_gauge_lonlat is not None:
        cg = list(coastal_gauge_lonlat)
        if len(cg) == 2:
            gauge = (float(cg[0]), float(cg[1]))

    try:
        run_args = GeoClawRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            scenario="surge",
            sim_duration_s=float(sim_duration_s),
            output_frames=int(output_frames),
            amr_levels=int(amr_levels),
            manning_n=float(manning_n),
            sea_level_m=float(sea_level_m),
            storm_track=track,
            wind_drag_law=str(wind_drag_law),  # type: ignore[arg-type]
            surge_t0_s=(None if surge_t0_s is None else float(surge_t0_s)),
            coastal_gauge_lonlat=gauge,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw storm-surge arguments: {exc}",
        }

    logger.info(
        "geoclaw_storm_surge bbox=%s drag=%s track_pts=%d t0=%s duration=%.0fs amr=%d",
        run_args.bbox,
        run_args.wind_drag_law,
        len(run_args.storm_track),
        run_args.surge_t0_s,
        run_args.sim_duration_s,
        run_args.amr_levels,
    )

    try:
        primary = await model_geoclaw_inundation(
            run_args,
            compute_class=compute_class,
            emit_gauge_series=True,
        )
        logger.info(
            "geoclaw_storm_surge complete layer_id=%s max_depth_m=%.4g "
            "max_inundation_m=%.4g uri=%s",
            primary.layer_id,
            primary.max_depth_m,
            primary.max_inundation_m,
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
            "geoclaw_storm_surge failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "GEOCLAW_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_storm_surge unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
