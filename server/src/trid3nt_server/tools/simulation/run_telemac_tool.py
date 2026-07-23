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

from trid3nt_contracts.telemac_contracts import TelemacDyeLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.model_river_dye_release_scenario import (
    TelemacDyeScenarioError,
    model_river_dye_release_scenario,
    plausible_release_coords,
)
from trid3nt_server.workflows.postprocess_telemac import PostprocessTelemacError

logger = logging.getLogger("trid3nt_server.tools.simulation.run_telemac_tool")

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
    spill_location_latlon: str | None = None,
    substance: str = "dye",
    contaminant: str | None = None,
    decay_half_life_hours: float | None = None,
    decay_rate_per_day: float | None = None,
    grain_size_um: float | None = None,
    sediment_type: str | None = None,
    friction_coefficient: float | None = None,
    friction_law: int | None = None,
    velocity_diffusivity: float | None = None,
    tracer_diffusivity: float | None = None,
    compute_class: str = "medium",
    # 2026-07-18 release-seeding tri-state, set ONLY by the approve-mesh
    # decision tail (underscore prefix -> stripped from the LLM schema by
    # _strip_private_params): True = the release coords came on the CALL and
    # also seed the reach; False = they are a gate-picked click (source only,
    # never relocate the previewed reach); None = no gate ran - auto
    # (plausible coords seed the reach).
    _release_seeds_reach: bool | None = None,
    # BK-3b decouple, also set ONLY by the approve-mesh decision tail: the
    # ORIGINAL call-provided release coords the preview meshed from, preserved
    # separately because the gate click overwrites release_lon/release_lat.
    # The reach seeds from THESE; the click moves the source only.
    _seed_release_lon: float | None = None,
    _seed_release_lat: float | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> TelemacDyeLayerURI | dict[str, Any]:
    """A DYE / TRACER / CONTAMINANT / POLLUTANT PLUME that TRAVELS DOWNSTREAM in a RIVER (surface water).

    THE tool for "simulate a dye plume travels downstream", "how far does the
    dye/contaminant travel down the river", "a dye spill in the river", "a
    contaminant/pollutant spilled into the river/stream and how it travels/
    moves/flows/spreads downstream". SURFACE water carried IN the river
    channel by the current (NOT groundwater/aquifer seepage - that is
    ``run_model_river_seepage_scenario``). Runs a TELEMAC-2D shallow-water
    solve with an advected tracer over a REAL river reach: a finite dye pulse
    releases at a mid-reach point source, travels downstream in the surface
    water and dilutes. Produces a peak dye-concentration map layer PLUS the
    engine's native time-stepped mesh (client animates via a Temporal
    Controller scrubber).

    Use this for any "spill in the river ... downstream" surface-water
    transport request.

    Do NOT use this for:
        - GROUNDWATER/AQUIFER contamination, river<->aquifer SEEPAGE, or a
          subsurface plume (use ``run_modflow_job`` /
          ``run_model_river_seepage_scenario`` - THIS tool is surface water
          IN the channel; seepage tools are water UNDER the ground).
        - Riverine/coastal/pluvial FLOODING depth (``run_model_flood_scenario``
          = SFINCS, or ``run_swmm_urban_flood`` = urban drainage).
        - Dam-break/tsunami/surge inundation (``run_geoclaw_inundation``).

    Params:
        location: place name near the river (e.g. "Twin Falls, Idaho").
            Supply this OR ``bbox`` - geocoded, never hand-typed coords.
        bbox: OPTIONAL explicit AOI ``(min_lon, min_lat, max_lon, max_lat)``
            EPSG:4326. Supply this OR ``location``.
        spill_fraction: along-reach spill position, 0=upstream..1=downstream.
            Default 0.25.
        spill_duration_s: finite pulse injection window, seconds. Default 300.
        dye_concentration_mgl: source dye concentration, mg/L. Default 100.
        reach_length_km: modeled reach length downstream of release, km.
            Default 6.
        sim_duration_s: simulated physical time, seconds. Default 3600.
        source_q_m3s: point-source discharge, m3/s (small vs river inflow).
            Default 8.
        channel_width_m: modeled channel width, m. Default 60.
        river_geometry_uri: OPTIONAL. If already called
            ``fetch_river_geometry`` for this reach, pass its returned
            ``uri`` to reuse the flowline (no re-fetch); otherwise the tool
            fetches it itself from ``location``/``bbox``.
        mesh_resolution: ``"auto"`` (default - sizes mesh from reach geometry
            under a node budget) | ``"fine"`` (sharper plume, slower solve) |
            ``"coarse"`` (faster, blockier). Set from user intent
            ("high-res" -> fine, "quick/coarse run" -> coarse).
        mesh_resolution_m: OPTIONAL explicit target edge length in METERS;
            overrides ``mesh_resolution``, still clamped under the node budget.
        release_lon / release_lat: EPSG:4326 spill point from the approve-mesh
            gate's map click - do NOT invent.
        substance: what was spilled, e.g. "dye"/"oil"/"diesel"/"sewage"/
            "chemical" - modeled as a passively advected dissolved tracer;
            THREE classes route automatically by keyword: oil-family
            ("oil"/"diesel"/"crude"/"bunker") adds the oil-spill slick
            module; decaying/bacterial ("sewage"/"E. coli"/"coliform"/
            "effluent"/"wastewater"/"bacteria"/"die-off") adds the WAQTEL
            first-order decay module (lower downstream peak, shorter
            persistence); sediment ("sediment"/"sand"/"silt"/"mud"/"slurry"/
            "tailings") activates the GAIA module (settles + deposits on the
            bed -> adds a bed-deposition map in mm beside the concentration
            ribbon). Everything else is a plain conservative dye tracer.
        decay_half_life_hours: OPTIONAL, decaying substance only - first-order
            half-life in HOURS (k = ln(2)/half_life). Default honest
            literature value ~2h (bacterial T90 in daylight freshwater).
            Clamped [0.1, 720].
        decay_rate_per_day: OPTIONAL alternative to
            ``decay_half_life_hours`` - decay rate k per DAY. Clamped
            [0.01, 100]. Use one or the other.
        grain_size_um: OPTIONAL, sediment substance only - median grain
            diameter d50 in MICRONS (~200um fine sand settles within a few
            km, ~20um silt mostly stays suspended). Default 200; clamped
            [5, 2000]. Honest demo default (no site bed-composition
            fetcher) unless user-specified.
        sediment_type: OPTIONAL sediment alias - "sand"/"silt"/"mud" - picks
            default grain size when ``grain_size_um`` unset. Non-cohesive
            in v1.
        friction_coefficient: OPTIONAL ADVANCED lever - bed roughness
            (Strickler Ks). Leave unset for demo default (33); clamped
            [10, 90]. Set only from a site-specific user value.
        friction_law: OPTIONAL ADVANCED lever - law interpreting
            ``friction_coefficient``: 2=Chezy, 3=Strickler (default),
            4=Manning. Set with ``friction_coefficient`` for a Manning n or
            Chezy C.
        velocity_diffusivity: OPTIONAL ADVANCED lever - turbulent momentum
            diffusivity nu_t (m2/s). Default 0.1; clamped [1e-3, 10].
        tracer_diffusivity: OPTIONAL ADVANCED lever - dye/tracer diffusivity
            (m2/s), sets lateral plume spread. Default 0.1; clamped
            [1e-3, 10].
        compute_class: FR-CE-3 compute class. Default ``"medium"``.

    Returns:
        On success: ``TelemacDyeLayerURI`` (``LayerURI`` subtype) - emitter
        loads it onto the map (peak dye COG) and the client animates the
        SELAFIN mesh sibling. Carries ``dye_cmax_mgl`` / ``dye_peak_time_s`` /
        ``plume_reach_m`` / ``active_frames`` (narrate these typed numbers
        only - invariant 1) + a ``fallback_note`` (idealized-bed demo).
        On failure: dict with ``status="error"`` + ``error_code`` +
        ``error_message`` (no layer).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` - cache shim not invoked.
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
        # LOCATION wins (flipped 2026-07-18, live-proven): the model fabricated
        # bbox (-124.2,46.0,-124.0,46.2) - open water at the Columbia MOUTH -
        # alongside location='...near Longview, WA'; the NLDI snap 404'd. The
        # geocoded location is ground truth; an LLM-invented bbox is not (a
        # user-drawn AOI arrives via case state, never this arg).
        logger.warning(
            "run_telemac: both location and bbox supplied - dropping the LLM "
            "bbox %s in favour of geocoding %r", coerced_bbox, location,
        )
        coerced_bbox = None

    # Release-coordinate sanitize (live 2026-07-18: bare release_lat/lon with
    # no river name left the geocoded CITY as the reach seed, so the corridor
    # grabbed the nearest water body - a Longview prompt meshed the Cowlitz
    # instead of the Columbia and the built mesh did not even contain the
    # release point). Plausible coords thread through the reach manifest so
    # the worker can seed the centerline/corridor from the RELEASE (see the
    # _release_seeds_reach tri-state above); implausible ones are dropped
    # with a warning, never a crash.
    # Alias: models pass a combined 'spill_location_latlon' string ("lat,lon")
    # instead of release_lat/release_lon (qwen did this twice on 2026-07-18 -
    # same silent-swallow class as the contaminant field). Parse it only when
    # the split coords are absent; the plausibility gate below still applies.
    if (release_lat is None and release_lon is None
            and spill_location_latlon):
        try:
            _lat_s, _lon_s = str(spill_location_latlon).split(",", 1)
            release_lat, release_lon = float(_lat_s), float(_lon_s)
            logger.info(
                "run_telemac: parsed spill_location_latlon %r -> lat=%s lon=%s",
                spill_location_latlon, release_lat, release_lon,
            )
        except (ValueError, TypeError):
            logger.warning(
                "run_telemac: unparseable spill_location_latlon %r - ignored",
                spill_location_latlon,
            )
    _release_pair = plausible_release_coords(release_lon, release_lat)
    if _release_pair is None and (release_lon is not None or release_lat is not None):
        logger.warning(
            "run_telemac: implausible release point lon=%r lat=%r - dropped",
            release_lon, release_lat,
        )
    release_lon, release_lat = _release_pair or (None, None)

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
        reach_length_km = min(max(reach_length_km, 0.5), 8.0)

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
    # M3 close-out (live drive 2026-07-18): models split intent across two
    # fields - substance='dye' AND contaminant='crude oil' - so an oil spill
    # silently ran the tracer class. If substance classifies as tracer but
    # the contaminant string classifies as oil-family, the contaminant IS the
    # substance (same sanitize; oil keywords win over the generic default).
    if contaminant:
        from trid3nt_server.workflows.model_river_dye_release_scenario import (  # noqa: WPS433
            classify_substance,
        )
        cont = "".join(c for c in str(contaminant).strip().lower()
                       if c.isalnum() or c in " -_")[:24]
        # Promote a tracer-class substance to whatever NON-tracer class the
        # contaminant names (oil OR decay) - the LLM splits intent across the
        # two fields (substance="dye"/"water" + contaminant="crude oil" or
        # "sewage"), proven live twice for oil. Any non-tracer contaminant wins.
        if (cont and classify_substance(substance)[0] == "tracer"
                and classify_substance(cont)[0] != "tracer"):
            logger.info(
                "run_telemac: substance %r is tracer-class but contaminant %r "
                "is %s-family - classifying by contaminant", substance, cont,
                classify_substance(cont)[0],
            )
            substance = cont
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

    # WAQTEL decay override coercion (the workflow does the law-mapping + final
    # clamp; here we only coerce to a positive float or drop to None so a bogus
    # arg never crashes the call). Only meaningful for the decay substance class.
    def _pos_float(v: float | None, lo: float, hi: float) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if not (f > 0.0):
            return None
        return min(max(f, lo), hi)
    decay_half_life_hours = _pos_float(decay_half_life_hours, 0.1, 720.0)
    decay_rate_per_day = _pos_float(decay_rate_per_day, 0.01, 100.0)
    # sediment grain size (microns): only meaningful for the sediment class. Clamp
    # to [5, 2000] um (silt .. coarse sand); a bogus value coerces to None so the
    # composer keeps the type-preset default (honest demo default, not measured).
    grain_size_um = _pos_float(grain_size_um, 5.0, 2000.0)
    # sediment_type alias (sand|silt|mud): label only, sanitized like substance.
    if sediment_type is not None:
        sediment_type = "".join(
            c for c in str(sediment_type).strip().lower()
            if c.isalnum() or c in " -_")[:8] or None

    # TELEMAC-PHYS-1 constitutive-physics overrides (advanced / demo-default
    # levers). Coerce + CLAMP to the physics_registry ranges here so a set value
    # never errors the call (matches this tool's defensive style); the workflow
    # re-validates via validate_and_resolve_physics. Any UNSET value stays None,
    # so the worker emits the historical deck literal (byte-identical).
    friction_coefficient = _pos_float(friction_coefficient, 10.0, 90.0)
    velocity_diffusivity = _pos_float(velocity_diffusivity, 1e-3, 10.0)
    tracer_diffusivity = _pos_float(tracer_diffusivity, 1e-3, 10.0)
    if friction_law is not None:
        try:
            friction_law = int(friction_law)
        except (TypeError, ValueError):
            friction_law = None
        else:
            if friction_law not in (2, 3, 4):
                logger.warning(
                    "run_telemac: friction_law %r not in {2,3,4} - ignored",
                    friction_law,
                )
                friction_law = None

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
            release_lon=release_lon,
            release_lat=release_lat,
            release_seeds_reach=_release_seeds_reach,
            seed_release_lon=_seed_release_lon,
            seed_release_lat=_seed_release_lat,
            substance=substance,
            decay_half_life_hours=decay_half_life_hours,
            decay_rate_per_day=decay_rate_per_day,
            grain_size_um=grain_size_um,
            sediment_type=sediment_type,
            friction_coefficient=friction_coefficient,
            friction_law=friction_law,
            velocity_diffusivity=velocity_diffusivity,
            tracer_diffusivity=tracer_diffusivity,
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
