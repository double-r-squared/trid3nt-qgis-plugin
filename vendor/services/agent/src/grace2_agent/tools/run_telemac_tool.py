"""Atomic tool ``run_telemac`` - TELEMAC-2D river-dye surface-tracer engine (P4).

The LLM-facing exposure of the TELEMAC-2D river-dye engine (a hazard family the
flood/groundwater engines do not cover: a CONTAMINANT DYE / TRACER released into
a flowing river reach, advected + diluted downstream as an ANIMATED plume).
``run_telemac(...)`` takes natural args (a place OR the case AOI + optional spill
knobs, all with sensible demo defaults so a bare "dye spill in the river near X"
runs), runs the deterministic geocode -> river-reach -> stage -> solve ->
postprocess chain (``workflows/model_river_dye_release_scenario.py``), and returns
a ``TelemacDyeLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires AND
``export_case_to_qgis`` discovers the SELAFIN mesh sibling for animation).

This is the TELEMAC analogue of ``run_geoclaw_inundation`` (GeoClaw) /
``run_seismic_hazard_psha`` (OpenQuake) / ``run_swan_waves`` (SWAN). Like those
wrappers it declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 - workflow exposure surface; never
touches the cache shim). Confirmation before consequence (Invariant 9 - a solver
run) is enforced by the server confirmation hook around this tool.

TELEMAC is LOCAL-DOCKER / BATCH ONLY (the opentelemac engine lives in the worker
image, never the agent venv), so the composer always dispatches through the
generic run_solver seam.

Determinism boundary (Invariant 1): every dye number the agent narrates comes
from the typed ``TelemacDyeLayerURI.dye_cmax_mgl`` / ``.plume_reach_m`` /
``.active_frames`` fields the postprocess computed - never free-generated. The
``fallback_note`` carries the honesty floor (idealized-bed demo).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from grace2_contracts.telemac_contracts import TelemacDyeLayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from ..tool_arg_normalizer import coerce_bbox_value
from ..workflows.model_river_dye_release_scenario import (
    TelemacDyeScenarioError,
    model_river_dye_release_scenario,
)
from ..workflows.postprocess_telemac import PostprocessTelemacError

logger = logging.getLogger("grace2_agent.tools.run_telemac_tool")

__all__ = ["run_telemac", "RunTelemacError"]


class RunTelemacError(RuntimeError):
    """Raised when the TELEMAC dye chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_RUN_TELEMAC_METADATA = AtomicToolMetadata(
    name="run_telemac",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_TELEMAC_METADATA,
    # readOnlyHint=False (runs a solver writing output COG + mesh artifacts),
    # openWorldHint=False (worker container + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + output keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_telemac(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    spill_fraction: float = 0.25,
    spill_duration_s: float = 300.0,
    dye_concentration_mgl: float = 100.0,
    reach_length_km: float = 6.0,
    sim_duration_s: float = 3600.0,
    source_q_m3s: float = 8.0,
    channel_width_m: float = 60.0,
    river_geometry_uri: str | None = None,
    mesh_resolution: str = "auto",
    mesh_resolution_m: float | None = None,
    release_lon: float | None = None,
    release_lat: float | None = None,
    substance: str = "dye",
    compute_class: str = "medium",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> TelemacDyeLayerURI | dict[str, Any]:
    """Dye / contaminant / tracer SPILL IN A RIVER, carried DOWNSTREAM in the water.

    THE tool for "a dye spill in the river", "a contaminant / pollutant spilled
    into the river / stream and how it travels / moves / flows downstream", "track
    a spill down the river", "a tracer released into the channel". It runs a
    TELEMAC-2D shallow-water solve with an advected tracer over a REAL river reach:
    a finite dye pulse is released at a mid-reach point source, then the plume
    travels downstream IN THE SURFACE WATER and dilutes. Produces a peak
    dye-concentration map layer PLUS the engine's native time-stepped mesh, which
    the client animates (a Temporal Controller scrubber over the dye field).

    Use this when:
        - The user asks to simulate a CONTAMINANT / DYE / TRACER / POLLUTANT /
          CHEMICAL SPILL or RELEASE INTO A RIVER / STREAM / CREEK / CHANNEL and
          wants to see how it TRAVELS / MOVES / FLOWS / SPREADS DOWNSTREAM in the
          flowing water (the plume + how far / how strong / how long).
        - Any "spill in the river ... downstream" surface-water transport request.

    Do NOT use this for:
        - GROUNDWATER / AQUIFER contamination, river<->aquifer SEEPAGE, or a
          plume moving through the SUBSURFACE / soil (use ``run_modflow_job`` /
          ``run_model_river_seepage_scenario``). THIS tool is the SURFACE water IN
          the river channel; seepage tools are the water UNDER the ground. A dye
          spill that travels DOWN THE RIVER is THIS tool, not seepage.
        - Riverine / coastal / pluvial FLOODING depth (use ``run_model_flood_scenario``
          = SFINCS, or ``run_swmm_urban_flood`` = urban drainage).
        - Dam-break / tsunami / surge inundation (use ``run_geoclaw_inundation``).

    Params:
        location: a place name near the river (e.g. "Twin Falls, Idaho"). Supply
            this OR ``bbox`` - the location is GEOCODED (never hand-typed coords).
        bbox: OPTIONAL explicit AOI ``(min_lon, min_lat, max_lon, max_lat)`` in
            EPSG:4326 (e.g. a drawn canvas AOI). Supply this OR ``location``.
        spill_fraction: along-reach position of the spill point, 0=upstream ..
            1=downstream. Default 0.25 (near the top of the reach so the plume has
            room to travel).
        spill_duration_s: how long the dye source injects before it turns off
            (the finite pulse window), seconds. Default 300.
        dye_concentration_mgl: source dye concentration, mg/L. Default 100.
        reach_length_km: how far downstream to model from the release, km.
            Default 6.
        sim_duration_s: simulated physical time, seconds. Default 3600.
        source_q_m3s: carrier discharge of the point source, m3/s (small vs the
            river inflow). Default 8.
        channel_width_m: modeled channel width, m. Default 60 (a broad river).
        river_geometry_uri: OPTIONAL. If you ALREADY called
            ``fetch_river_geometry`` for this reach, pass its returned layer
            ``uri`` here and this tool reuses that flowline for the spill point
            (no re-fetch). Otherwise leave unset and the tool fetches the reach
            itself from the place / AOI. (You do NOT need to fetch the river
            first -- ``location`` alone is enough.)
        mesh_resolution: mesh GRANULARITY lever (BK-3c). One of ``"auto"`` (the
            default - the tool sizes the mesh from the reach geometry under a node
            budget), ``"fine"`` (more cells across the channel; sharper plume,
            slower solve), or ``"coarse"`` (fewer cells; faster, blockier). Set it
            from the user's intent, e.g. "high-res mesh" -> ``"fine"``, "quick /
            coarse run" -> ``"coarse"``. NOT hardcoded - the chosen edge length +
            node estimate come back on the layer so they can be shown/approved.
        mesh_resolution_m: OPTIONAL explicit mesh target edge length in METERS
            (e.g. 8.0). Overrides ``mesh_resolution``. Still clamped under the node
            budget so a reckless value can't wedge the solve. Leave unset unless
            the user asks for a specific resolution.
        release_lon: EPSG:4326 longitude of the USER-PICKED spill point (BK-6).
            Comes from the approve-mesh gate's map click - do NOT invent it.
        release_lat: EPSG:4326 latitude of the user-picked spill point.
        substance: WHAT was spilled - e.g. "dye", "oil", "diesel", "sewage",
            "chemical". Set from the user's words. Modeled as a PASSIVELY
            ADVECTED dissolved tracer (transport + dilution); labels/narration
            follow the substance. (True oil-slick physics - spreading,
            evaporation, beaching - is the separate oil-spill module, WIP.)
        compute_class: FR-CE-3 compute class. Default ``"medium"``.

    Returns:
        On success: a ``TelemacDyeLayerURI`` (a ``LayerURI`` subtype) - the emitter
        appends it to ``session-state.loaded_layers`` and the map renders the peak
        dye COG; the client also materializes + animates the SELAFIN mesh sibling.
        It carries ``dye_cmax_mgl`` + ``dye_peak_time_s`` + ``plume_reach_m`` +
        ``active_frames`` (Invariant 1 - the agent narrates these typed numbers,
        never invents them) + a ``fallback_note`` labeling it an idealized-bed demo.

        On failure: a dict with ``status="error"`` + ``error_code`` +
        ``error_message`` so the LLM narrates the failure honestly (no layer).

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` - the cache shim is NOT invoked.
    """
    coerced_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        cb = coerce_bbox_value(bbox)
        if cb is None:
            # LLM-arg salvage (live 2026-07-17: bbox='Twin Falls, Idaho'): a
            # non-numeric string bbox is almost always a PLACE NAME - shift it
            # into location instead of dead-ending the call.
            if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                    and not (location and str(location).strip()):
                logger.warning(
                    "run_telemac: bbox %r is a place name - using as location",
                    bbox,
                )
                location, bbox = bbox, None
            else:
                return {
                    "status": "error",
                    "error_code": "TELEMAC_PARAMS_INVALID",
                    "error_message": (
                        f"invalid bbox (expected 4 numbers min_lon,min_lat,"
                        f"max_lon,max_lat): {bbox!r}"
                    ),
                }
        else:
            coerced_bbox = tuple(cb)  # type: ignore[assignment]

    # LLM-arg salvage: river_geometry_uri must be a real object-store URI; the
    # model sometimes invents pseudo-calls ('fetch_river_geometry(...)').
    if river_geometry_uri and not str(river_geometry_uri).startswith(("s3://", "gs://")):
        logger.warning(
            "run_telemac: river_geometry_uri %r is not an object URI - ignoring",
            river_geometry_uri,
        )
        river_geometry_uri = None

    has_loc = bool(location and str(location).strip())
    # OPEN-24 (2026-07-16): need AT LEAST one of location/bbox. The old guard
    # demanded EXACTLY one and errored when BOTH were given - but the model,
    # having just geocoded the place, naturally passes BOTH the place name AND
    # the resulting bbox, so a correct natural-prompt call was rejected. When
    # both are present prefer the explicit bbox (drop the redundant location);
    # only a genuinely empty AOI is an error.
    if not has_loc and coerced_bbox is None:
        return {
            "status": "error",
            "error_code": "TELEMAC_PARAMS_INCOMPLETE",
            "error_message": (
                "run_telemac needs a place `location` (geocoded) or an explicit "
                "`bbox` AOI. For a natural prompt like 'dye spill in the river "
                "near <place>', pass location='<place>'."
            ),
        }
    if has_loc and coerced_bbox is not None:
        has_loc = False  # explicit bbox wins; ignore the redundant location

    # LLM-invented compute_class hardening (live 2026-07-17: the model passed
    # compute_class='dye_spill' and the dispatch crashed AFTER the geocode +
    # river fetch). Coerce anything outside the known ladder to 'medium' -
    # same job-0164 family as the **_extra_ignored absorption above.
    _ALLOWED_COMPUTE = {"small", "medium", "standard", "large", "xlarge", "gpu"}
    if str(compute_class).strip().lower() not in _ALLOWED_COMPUTE:
        logger.warning(
            "run_telemac: unknown compute_class %r coerced to 'medium'",
            compute_class,
        )
        compute_class = "medium"

    # LLM-invented reach-scale hardening (live 2026-07-17: the model asked for a
    # 50 km reach; gmsh hung/crashed banking the 2802-point meandering
    # centerline and the run died silently). Clamp to the modelable window - a
    # dye plume travels ~5-10 km in the demo sim durations anyway.
    try:
        reach_length_km = float(reach_length_km)
    except (TypeError, ValueError):
        reach_length_km = 6.0
    if not (0.5 <= reach_length_km <= 15.0):
        logger.warning(
            "run_telemac: reach_length_km %r outside [0.5, 15] - clamped",
            reach_length_km,
        )
        reach_length_km = min(max(reach_length_km, 0.5), 15.0)

    # Ill-posed forcing hardening (live 2026-07-17: spill_fraction=1.0 planted
    # the source ON the outflow boundary -> TELEMAC startup abort 'GIVE A
    # POSITIVE DEPTH ... AT THE ENTRANCE'; source_q=100 was ~40% of river
    # inflow). Keep the source strictly INTERIOR and small vs the carrier flow.
    try:
        spill_fraction = float(spill_fraction)
    except (TypeError, ValueError):
        spill_fraction = 0.25
    if not (0.05 <= spill_fraction <= 0.9):
        logger.warning(
            "run_telemac: spill_fraction %r outside [0.05, 0.9] - clamped "
            "(source must sit inside the reach, not on a boundary)",
            spill_fraction,
        )
        spill_fraction = min(max(spill_fraction, 0.05), 0.9)
    try:
        sim_duration_s = float(sim_duration_s)
    except (TypeError, ValueError):
        sim_duration_s = 3600.0
    if not (600.0 <= sim_duration_s <= 14400.0):
        logger.warning(
            "run_telemac: sim_duration_s %r outside [600, 14400] - clamped",
            sim_duration_s,
        )
        sim_duration_s = min(max(sim_duration_s, 600.0), 14400.0)
    # substance sanitize (label only - never solver-affecting)
    substance = "".join(c for c in str(substance or "dye").strip().lower()
                        if c.isalnum() or c in " -_")[:24] or "dye"
    try:
        channel_width_m = float(channel_width_m)
    except (TypeError, ValueError):
        channel_width_m = 60.0
    if not (10.0 <= channel_width_m <= 1500.0):
        logger.warning(
            "run_telemac: channel_width_m %r outside [10, 1500] - clamped",
            channel_width_m,
        )
        channel_width_m = min(max(channel_width_m, 10.0), 1500.0)
    try:
        source_q_m3s = float(source_q_m3s)
    except (TypeError, ValueError):
        source_q_m3s = 8.0
    if not (0.5 <= source_q_m3s <= 30.0):
        logger.warning(
            "run_telemac: source_q_m3s %r outside [0.5, 30] - clamped",
            source_q_m3s,
        )
        source_q_m3s = min(max(source_q_m3s, 0.5), 30.0)

    logger.info(
        "run_telemac location=%r bbox=%s spill_frac=%.3g pulse_s=%.0f dye=%.4g "
        "reach_km=%.3g sim_s=%.0f",
        location, coerced_bbox, spill_fraction, spill_duration_s,
        dye_concentration_mgl, reach_length_km, sim_duration_s,
    )

    try:
        peak = await model_river_dye_release_scenario(
            location=location if has_loc else None,
            bbox=coerced_bbox,
            spill_fraction=float(spill_fraction),
            spill_duration_s=float(spill_duration_s),
            dye_concentration_mgl=float(dye_concentration_mgl),
            reach_length_km=float(reach_length_km),
            sim_duration_s=float(sim_duration_s),
            source_q_m3s=float(source_q_m3s),
            channel_width_m=float(channel_width_m),
            river_geometry_uri=(str(river_geometry_uri) if river_geometry_uri else None),
            mesh_resolution=str(mesh_resolution or "auto"),
            mesh_resolution_m=(float(mesh_resolution_m) if mesh_resolution_m is not None else None),
            release_lon=(float(release_lon) if release_lon is not None else None),
            release_lat=(float(release_lat) if release_lat is not None else None),
            substance=substance,
            compute_class=compute_class,
        )
        logger.info(
            "run_telemac complete layer_id=%s dye_cmax_mgl=%.4g plume_reach_m=%s "
            "active_frames=%s uri=%s",
            peak.layer_id, peak.dye_cmax_mgl, peak.plume_reach_m,
            peak.active_frames, peak.uri,
        )
        return peak
    except asyncio.CancelledError:
        raise
    except (TelemacDyeScenarioError, PostprocessTelemacError) as exc:
        logger.warning("run_telemac failed: %s (%s)", getattr(exc, "error_code", "?"), exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "TELEMAC_RUN_FAILED"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 - defensive catch-all
        logger.exception("run_telemac unexpected failure")
        return {
            "status": "error",
            "error_code": "TELEMAC_INTERNAL_ERROR",
            "error_message": str(exc),
        }
