"""Engine template ``telemac_river_dye`` - TELEMAC-2D river-dye surface-tracer
engine (engine-door refactor - TELEMAC slice; was ``run_telemac``).

The LLM-facing exposure of the TELEMAC-2D river-dye engine (a hazard family the
flood/groundwater engines do not cover: a CONTAMINANT DYE / TRACER released into
a flowing river reach, advected + diluted downstream as an ANIMATED plume).
``telemac_river_dye(...)`` takes natural args (a place OR the case AOI + optional
spill knobs, all with sensible demo defaults so a bare "dye spill in the river
near X" runs), runs the deterministic geocode -> river-reach -> stage -> solve ->
postprocess chain (``model_telemac_river_dye`` below, in this module), and
returns a ``TelemacDyeLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires AND
``open_case_in_qgis`` discovers the SELAFIN mesh sibling for animation).

This is the TELEMAC analogue of ``modflow_contaminant_plume`` (MODFLOW),
``sfincs_flood`` (SFINCS), and ``swmm_urban_flood`` (SWMM). It is a registered
engine TEMPLATE tagged ``engine="telemac", tier="template"`` - EXCLUDED from the
default retrieval pool and surfaced only by the ``run_telemac`` door's gate
expansion (SELECT-THEN-CALL). Like the other templates it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 - workflow exposure surface; never
touches the cache shim). Confirmation before consequence (Invariant 9 - a solver
run) is enforced by the server confirmation hook around this template (the
approve-mesh gate, keyed on ``telemac_river_dye``), not re-implemented here.

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

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import TelemacDyeLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.workflows.telemac._template_card import TemplateCard
from trid3nt_server.agent.workflows.telemac.postprocess_telemac import PostprocessTelemacError

logger = logging.getLogger("trid3nt_server.agent.workflows.telemac.river_dye.river_dye")


def _clamp_domain_extent(
    value: float, *, valid_lo: float, valid_hi: float,
    clamp_lo: float, clamp_hi: float, name: str, unit: str,
) -> tuple[float, str | None]:
    """Clamp a domain-extent value to the modelable window (ADR 0223, audit #9).

    A value INSIDE ``[valid_lo, valid_hi]`` passes through unchanged (``note``
    None); an out-of-range value is clamped to ``[clamp_lo, clamp_hi]`` and a
    labeled note is returned so the guardrail is visible on the run envelope
    instead of only agent.log (R2 transparency). The clamp is a defensible
    screening guardrail (a too-long reach hangs the mesh builder), just no longer
    silent.
    """
    if valid_lo <= value <= valid_hi:
        return value, None
    clamped = min(max(value, clamp_lo), clamp_hi)
    note = (
        f"{name} {value:g} -> {clamped:g} {unit} "
        f"(clamped to the modelable [{clamp_lo:g}, {clamp_hi:g}] {unit} window)"
    )
    return clamped, note


__all__ = [
    "telemac_river_dye",
    "RunTelemacError",
    "model_telemac_river_dye",
    "TelemacBanksUnavailableError",
    "TelemacDyeScenarioError",
    "TelemacReachDegenerateError",
]


class RunTelemacError(RuntimeError):
    """Raised when the TELEMAC dye chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card (the run_telemac door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "how far a DYE / TRACER / CONTAMINANT / oil / sewage / sediment spill "
        "travels DOWNSTREAM in a river reach (surface water) + its peak "
        "concentration; OR where the BED SCOURS / ERODES and re-deposits under a "
        "flood (GAIA erodible-bed morphodynamics) - TELEMAC-2D shallow-water over "
        "a real reach; animated time-stepped plume / bed evolution"
    ),
    required_inputs=["location OR bbox"],
    knobs=(
        "spill_fraction, spill_duration_s, dye_concentration_mgl, "
        "reach_length_km, sim_duration_s, source_q_m3s, channel_width_m, "
        "substance (dye / oil / sewage / sediment / scour / graded), mesh_resolution, "
        "decay_half_life_hours, grain_size_um, erodible_bed, bed_thickness_m, "
        "bedload_formula, morphological_factor, sediment_gradation, "
        "dredging, dredge_mode (scheduled/criterion), dredge_volume_m3, "
        "dredge_disposal, dredge_crit_depth_m, dredge_dig_depth_m, friction_coefficient, "
        "rainfall_mm_per_day, rainfall_gridmet_window, evaporation_mm_per_day"
    ),
)


#: DECLARED mesh_resolution_m range (ADR 0225). SOLVER floor MESH_H_FLOOR_M (3 m):
#: the finest edge the mesh builder authors regardless of ask; below it a screening
#: dye plume gains nothing. No fixed coarse ceiling -- the node-budget floor coarsens
#: a long reach WITHIN this declaration (self-labeled), and the effective edge stays
#: >= 2 cells across the channel. An out-of-range (sub-3 m) explicit ask is quoted
#: back, never silently snapped.
_TELEMAC_RIVER_DYE_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,  # == MESH_H_FLOOR_M (defined below; literal here to avoid a fwd ref)
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; MESH_H_FLOOR_M=3 m is the absolute finest the "
        "builder authors, a long reach is further coarsened under the "
        "MESH_NODE_CAP node budget (self-labeled); no fixed coarse ceiling"
    ),
)

_TELEMAC_RIVER_DYE_METADATA = AtomicToolMetadata(
    name="telemac_river_dye",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC_RIVER_DYE_RES_SPEC,),
)


@register_tool(
    _TELEMAC_RIVER_DYE_METADATA,
    # readOnlyHint=False (runs a solver writing output COG + mesh artifacts),
    # openWorldHint=False (worker container + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + output keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def telemac_river_dye(
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
    erodible_bed: bool | None = None,
    bed_thickness_m: float | None = None,
    bedload_formula: int | None = None,
    morphological_factor: float | None = None,
    sediment_gradation: list | str | None = None,
    dredging: bool | None = None,
    dredge_mode: str | None = None,
    dredge_volume_m3: float | None = None,
    dredge_disposal: bool | None = None,
    dredge_crit_depth_m: float | None = None,
    dredge_dig_depth_m: float | None = None,
    friction_coefficient: float | None = None,
    friction_law: int | None = None,
    velocity_diffusivity: float | None = None,
    tracer_diffusivity: float | None = None,
    wind_speed_mps: float = 0.0,
    wind_direction_deg: float = 0.0,
    rainfall_mm_per_day: float | None = None,
    evaporation_mm_per_day: float | None = None,
    rainfall_gridmet_window: str | None = None,
    compute_class: str = "medium",
    bank_source: str = "nhd_area",
    discharge_m3s: float | None = None,
    input_mode: str | None = None,
    # 2026-07-18 release-seeding tri-state, set ONLY by the approve-mesh
    # decision tail (underscore prefix -> stripped from the LLM schema by
    # _strip_private_params): True = the release coords came on the CALL and
    # also seed the reach; False = they are a gate-picked click (source only,
    # never relocate the previewed reach); None = no gate ran - auto
    # (plausible coords seed the reach).
    _release_seeds_reach: bool | None = None,
    # decouple, also set ONLY by the approve-mesh decision tail: the
    # ORIGINAL call-provided release coords the preview meshed from, preserved
    # separately because the gate click overwrites release_lon/release_lat.
    # The reach seeds from THESE; the click moves the source only.
    _seed_release_lon: float | None = None,
    _seed_release_lat: float | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> TelemacDyeLayerURI | dict[str, Any]:
    """A DYE / TRACER / CONTAMINANT / POLLUTANT PLUME that TRAVELS DOWNSTREAM in a RIVER (surface water).

    Fidelity: TELEMAC-2D full-physics shallow-water surface-water tracer
    transport on an idealized planar bed (no site-bathymetry fetcher);
    planning-grade demo, not a calibrated transport model. Off-scope: groundwater
    plume / river-aquifer seepage -> modflow_contaminant_plume /
    modflow_river_seepage; inundation depth -> sfincs_flood; tsunami / dam-break
    run-up -> geoclaw_inundation.

    THE tool for "simulate a dye plume travels downstream", "how far does the
    dye/contaminant travel down the river", "a dye spill in the river", "a
    contaminant/pollutant spilled into the river/stream and how it travels/
    moves/flows/spreads downstream". SURFACE water carried IN the river
    channel by the current (NOT groundwater/aquifer seepage - that is
    ``modflow_river_seepage``). Runs a TELEMAC-2D shallow-water
    solve with an advected tracer over a REAL river reach: a finite dye pulse
    releases at a mid-reach point source, travels downstream in the surface
    water and dilutes. Produces a peak dye-concentration map layer PLUS the
    engine's native time-stepped mesh (client animates via a Temporal
    Controller scrubber).

    Use this for any "spill in the river ... downstream" surface-water
    transport request.

    Do NOT use this for:
        - GROUNDWATER/AQUIFER contamination, river<->aquifer SEEPAGE, or a
          subsurface plume (use ``modflow_contaminant_plume`` /
          ``modflow_river_seepage`` - THIS tool is surface water
          IN the channel; seepage tools are water UNDER the ground).
        - Riverine/coastal/pluvial FLOODING depth (``sfincs_flood``
          = SFINCS, or ``swmm_urban_flood`` = urban drainage).
        - Dam-break/tsunami/surge inundation (``geoclaw_inundation``).

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
        discharge_m3s: OPTIONAL steady upstream CARRIER discharge, m3/s (the
            river flow that dilutes/transports the release). Unset -> resolved
            from the NOAA National Water Model at the reach; if that lookup
            finds no coverage the run STOPS with a typed
            ``TELEMAC_DISCHARGE_INPUT_REQUIRED`` gate (the discharge is never a
            baked 250 default). An explicit value overrides the NWM lookup.
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
            ribbon). The GAIA path is SUPPLY-LIMITED: the deposition comes from a
            PRESCRIBED UPSTREAM SEDIMENT SUPPLY (source concentration), not an
            initial bed stock - i.e. THIS is the "reservoir-inflow sedimentation /
            upstream sediment supply rate" question. Naming SCOUR / EROSION / a
            mobile bed instead ("scour"/"erosion"/"erodible"/"bedload"/"bed
            degradation") routes to the GAIA v2 ERODIBLE-BED path (see
            ``erodible_bed``): a real erodible bed with active bedload, so the bed
            LOWERS (scours) and re-deposits under the flow. Everything else is a
            plain conservative dye tracer.
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
            default grain size when ``grain_size_um`` unset. Non-cohesive.
        erodible_bed: OPTIONAL - GAIA v2 ERODIBLE-BED MORPHODYNAMICS. Leave
            unset (auto): a substance/prompt naming SCOUR / EROSION / a mobile
            bed ("where does the bed scour below the dam/weir/bridge", "bed
            degradation under a flood") auto-arms it; pass ``True`` to force it,
            ``False`` to force the v1 supply-limited suspended-deposition path.
            When True the run puts a real ERODIBLE BED under the reach with
            active BEDLOAD transport, so the bed SCOURS where the flow steepens
            and re-DEPOSITS where it slackens - the deliverable is a SIGNED
            bed-evolution map (scour negative / deposition positive) plus a
            bed-change profile, and ``max_scour_mm`` / ``max_deposition_mm`` are
            reported. Distinct from the default supply-limited path, where nothing
            erodes (only an injected pulse deposits).
        bed_thickness_m: OPTIONAL erodible-bed only - depth of the erodible bed
            stock (m). Default 5; clamped [0.05, 50]. A thicker stock keeps the
            reach erodible over a long event.
        bedload_formula: OPTIONAL erodible-bed only - GAIA bed-load transport
            law: 1=Meyer-Peter-Mueller (default), 2=Einstein-Brown, 7=van Rijn.
            Other picks fall back to the default.
        morphological_factor: OPTIONAL erodible-bed only - amplifies bed change
            per hydraulic step so a short demo hydrograph yields a readable scour
            depth. Default 10; clamped [1, 100]. Higher = more bed change (a
            speed-up lever, not a physical rate).
        sediment_gradation: OPTIONAL - GAIA MULTI-CLASS GRADED SEDIMENT for the
            "how does a MIXTURE of grain sizes SORT / segregate (vs a single
            representative size)" question. Pass a preset name
            ("graded_sand" / "poorly_sorted" / "sand_gravel_bimodal" /
            "fine_coarse_sand") OR an explicit list of [d50_um, fraction] pairs
            (e.g. [[100,0.34],[400,0.33],[1000,0.33]]); >= 2 classes required.
            A graded mix forces an erodible (mobile) bed; the classes have
            different mobility so the bed ARMORS in scour zones (surface D50
            rises) and FINES in deposition zones - the run reports the surface
            D50 spread (min/max/range um) as the sorting signature. Also
            auto-arms from prompts naming "graded / mixed-grain / sorting /
            armoring / bimodal" sediment. Demo mixes, never a measured site
            sieve curve (no bed-composition fetcher exists).
        dredging: OPTIONAL - NESTOR CHANNEL-MAINTENANCE DREDGING for the "without
            maintenance the navigable channel silts up; a dig rule holds the
            depth" question. Layers an engineered dig/dump rule onto the GAIA
            erodible-bed morphodynamics (forces the sediment class + erodible
            bed). Auto-arms from prompts naming "dredge / maintenance dredging /
            channel maintenance / spoil disposal / shoaling / silting". The
            dredge/disposal zone geometry + volumes/rates are labeled-default
            engineering inputs (input-review gate); the worker builds a
            channel-spanning dredge box at mid-reach by default.
        dredge_mode: OPTIONAL - dredging rule: "scheduled" (default; remove
            dredge_volume_m3 over a time window - the standard maintenance cycle)
            or "criterion" (dig only where the silted bed rises within
            dredge_crit_depth_m of the design grade, down to dredge_dig_depth_m
            below it - a critical-elevation-triggered dig/dump loop).
        dredge_volume_m3: OPTIONAL scheduled-mode target dredged volume (m3);
            labeled default 4000. dredge_disposal True also places the spoil in a
            downstream disposal zone (dredge-and-dump spoil placement).
        dredge_crit_depth_m / dredge_dig_depth_m: OPTIONAL criterion-mode
            siltation tolerance above grade / dig target below grade (m).
        dredge_disposal: OPTIONAL - also place the dug spoil in a downstream
            disposal zone (Dump); default False (dredge-only).
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
        wind_speed_mps: OPTIONAL sustained WIND SPEED (m/s) driving a
            wind-stress term on the free surface - the "how does sustained wind
            set up a surface slope / drive circulation on a wide reach,
            embayment or lake" question. Default 0 = no wind (unchanged solve).
            A positive value turns on a spatially-constant wind; clamped
            [0, 60]. Pair with ``wind_direction_deg``.
        wind_direction_deg: OPTIONAL meteorological wind direction in DEGREES,
            the compass bearing the wind blows FROM (0=N, 90=E, 180=S, 270=W).
            Default 0. Only meaningful when ``wind_speed_mps`` > 0.
        rainfall_mm_per_day: OPTIONAL distributed ON-MESH rainfall rate (mm/day)
            applied as a native TELEMAC-2D source term at EVERY wet mesh node,
            INDEPENDENT of the inflow-boundary hydrograph - the "how does
            distributed rain change inundation depth/timing" question. Default
            None = no rain (unchanged solve). A positive value raises stage +
            wets tidal flats over the whole reach. Pair with (or supersede via)
            ``rainfall_gridmet_window`` for a real US storm total.
        evaporation_mm_per_day: OPTIONAL distributed evaporation rate (mm/day),
            SUBTRACTED from the net rain flux (TELEMAC's signed RAIN OR
            EVAPORATION keyword). Default None. Set with no rain to model a net
            water LOSS from the free surface.
        rainfall_gridmet_window: OPTIONAL real-storm auto-source: an ISO date
            window ``"YYYY-MM-DD:YYYY-MM-DD"`` (e.g. a hurricane landfall week).
            Fetches gridMET daily precipitation (``pr``) over the reach AOI for
            that window and uses the domain-mean daily rate (mm/day) as the
            rainfall forcing - a REAL observed storm total, not a guess.
            Supersedes ``rainfall_mm_per_day`` when set.
        compute_class: FR-CE-3 compute class. Default ``"medium"``.
        bank_source: river-bank geometry source. ``"nhd_area"`` (default) meshes
            REAL banks sampled from USGS NHDArea water polygons; when NO NHDArea
            polygon covers the reach the tool RAISES the typed retryable
            ``TELEMAC_BANKS_UNAVAILABLE`` gate naming the explicit
            ``bank_source="constant_ribbon"`` retry (an assumed constant channel
            width) -- it NEVER silently substitutes the ribbon. Set
            ``"constant_ribbon"`` to mesh the assumed ``channel_width_m`` ribbon
            directly (labeled as an assumption in the result). Do NOT default to
            constant_ribbon to dodge the gate: real banks are more faithful.
        input_mode: run-mode lever (ADR 0107). ``"user_gated"`` presents the
            resolved carrier discharge + bank source for review before the solve;
            ``"auto"`` (default) proceeds with them labeled.

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
                    "telemac_river_dye: bbox %r is a place name - using as location",
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
            "telemac_river_dye: river_geometry_uri %r is not an object URI - ignoring",
            river_geometry_uri,
        )
        river_geometry_uri = None

    has_loc = bool(location and str(location).strip())
    # Need AT LEAST one of location/bbox. When both are present prefer the
    # explicit bbox (drop the redundant location); only a genuinely empty AOI
    # is an error.
    if not has_loc and coerced_bbox is None:
        return {
            "status": "error",
            "error_code": "TELEMAC_PARAMS_INCOMPLETE",
            "error_message": (
                "telemac_river_dye needs a place `location` (geocoded) or an explicit "
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
            "telemac_river_dye: both location and bbox supplied - dropping the LLM "
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
                "telemac_river_dye: parsed spill_location_latlon %r -> lat=%s lon=%s",
                spill_location_latlon, release_lat, release_lon,
            )
        except (ValueError, TypeError):
            logger.warning(
                "telemac_river_dye: unparseable spill_location_latlon %r - ignored",
                spill_location_latlon,
            )
    _release_pair = plausible_release_coords(release_lon, release_lat)
    if _release_pair is None and (release_lon is not None or release_lat is not None):
        logger.warning(
            "telemac_river_dye: implausible release point lon=%r lat=%r - dropped",
            release_lon, release_lat,
        )
    release_lon, release_lat = _release_pair or (None, None)

    # LLM-invented compute_class hardening (live 2026-07-17: the model passed
    # compute_class='dye_spill' and the dispatch crashed AFTER the geocode +
    # river fetch). Coerce anything outside the known ladder to 'medium' -
    # same family as the **_extra_ignored absorption above.
    _ALLOWED_COMPUTE = {"small", "medium", "standard", "large", "xlarge", "gpu"}
    if str(compute_class).strip().lower() not in _ALLOWED_COMPUTE:
        logger.warning(
            "telemac_river_dye: unknown compute_class %r coerced to 'medium'",
            compute_class,
        )
        compute_class = "medium"

    # ADR 0223 (audit #9): the domain-extent guardrails below are defensible
    # (a 50 km reach live-hung gmsh) but were silent. Record each clamp that BINDS
    # so it surfaces as a labeled envelope note instead of only agent.log.
    _domain_clamps: list[str] = []
    # LLM-invented reach-scale hardening (live 2026-07-17: the model asked for a
    # 50 km reach; gmsh hung/crashed banking the 2802-point meandering
    # centerline and the run died silently). Clamp to the modelable window - a
    # dye plume travels ~5-10 km in the demo sim durations anyway.
    try:
        reach_length_km = float(reach_length_km)
    except (TypeError, ValueError):
        reach_length_km = 6.0
    reach_length_km, _n = _clamp_domain_extent(
        reach_length_km, valid_lo=0.5, valid_hi=15.0, clamp_lo=0.5, clamp_hi=8.0,
        name="reach_length_km", unit="km")
    if _n:
        logger.warning("telemac_river_dye: %s", _n)
        _domain_clamps.append(_n)

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
            "telemac_river_dye: spill_fraction %r outside [0.05, 0.9] - clamped "
            "(source must sit inside the reach, not on a boundary)",
            spill_fraction,
        )
        spill_fraction = min(max(spill_fraction, 0.05), 0.9)
    try:
        sim_duration_s = float(sim_duration_s)
    except (TypeError, ValueError):
        sim_duration_s = 3600.0
    sim_duration_s, _n = _clamp_domain_extent(
        sim_duration_s, valid_lo=600.0, valid_hi=14400.0, clamp_lo=600.0,
        clamp_hi=14400.0, name="sim_duration_s", unit="s")
    if _n:
        logger.warning("telemac_river_dye: %s", _n)
        _domain_clamps.append(_n)
    # substance sanitize (label only - never solver-affecting)
    substance = "".join(c for c in str(substance or "dye").strip().lower()
                        if c.isalnum() or c in " -_")[:24] or "dye"
    # M3 close-out (live drive 2026-07-18): models split intent across two
    # fields - substance='dye' AND contaminant='crude oil' - so an oil spill
    # silently ran the tracer class. If substance classifies as tracer but
    # the contaminant string classifies as oil-family, the contaminant IS the
    # substance (same sanitize; oil keywords win over the generic default).
    if contaminant:
        cont = "".join(c for c in str(contaminant).strip().lower()
                       if c.isalnum() or c in " -_")[:24]
        # Promote a tracer-class substance to whatever NON-tracer class the
        # contaminant names (oil OR decay) - the LLM splits intent across the
        # two fields (substance="dye"/"water" + contaminant="crude oil" or
        # "sewage"), proven live twice for oil. Any non-tracer contaminant wins.
        if (cont and classify_substance(substance)[0] == "tracer"
                and classify_substance(cont)[0] != "tracer"):
            logger.info(
                "telemac_river_dye: substance %r is tracer-class but contaminant %r "
                "is %s-family - classifying by contaminant", substance, cont,
                classify_substance(cont)[0],
            )
            substance = cont
    try:
        channel_width_m = float(channel_width_m)
    except (TypeError, ValueError):
        channel_width_m = 60.0
    channel_width_m, _n = _clamp_domain_extent(
        channel_width_m, valid_lo=10.0, valid_hi=1500.0, clamp_lo=10.0,
        clamp_hi=1500.0, name="channel_width_m", unit="m")
    if _n:
        logger.warning("telemac_river_dye: %s", _n)
        _domain_clamps.append(_n)
    try:
        source_q_m3s = float(source_q_m3s)
    except (TypeError, ValueError):
        source_q_m3s = 8.0
    if not (0.5 <= source_q_m3s <= 30.0):
        logger.warning(
            "telemac_river_dye: source_q_m3s %r outside [0.5, 30] - clamped",
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

    # GAIA v2 ERODIBLE-BED MORPHODYNAMICS knobs. erodible_bed auto-arms when the
    # substance/contaminant text names SCOUR / EROSION / a mobile bed (the "where
    # does the bed scour below a dam/weir/bridge contraction" question) even if the
    # LLM did not pass the flag; an explicit True/False always wins. Coerce the
    # tuning levers to sane ranges here so a bogus arg never crashes the call; the
    # worker re-clamps. Only meaningful when the run classifies as the sediment
    # class - a non-sediment substance ignores these.
    _scour_hint = any(w in substance for w in SCOUR_KEYWORDS) or (
        contaminant and any(w in str(contaminant).lower() for w in SCOUR_KEYWORDS))
    if erodible_bed is None:
        erodible_bed = bool(_scour_hint)
    else:
        erodible_bed = bool(erodible_bed)
    bed_thickness_m = _pos_float(bed_thickness_m, 0.05, 50.0)
    morphological_factor = _pos_float(morphological_factor, 1.0, 100.0)
    if bedload_formula is not None:
        try:
            bedload_formula = int(bedload_formula)
        except (TypeError, ValueError):
            bedload_formula = None
        else:
            # gaia.dico v9.0 BED-LOAD TRANSPORT FORMULA FOR ALL SANDS choices
            # compatible with a suspension-off run (1 MPM, 2 Einstein-Brown,
            # 7 van Rijn bedload). Others (Engelund-Hansen total etc.) are dropped
            # to the default so a bad pick never wedges the solve.
            if bedload_formula not in (1, 2, 7):
                logger.warning(
                    "telemac_river_dye: bedload_formula %r not in {1,2,7} - "
                    "using default (1=Meyer-Peter-Mueller)", bedload_formula)
                bedload_formula = None

    # GAIA v3 MULTI-CLASS GRADED SEDIMENT (ADR 0240). A gradation is armed by an
    # explicit sediment_gradation (a preset name OR a list of [d50_um, fraction]
    # pairs) OR by grading vocabulary (graded / mixed-grain / sorting / armoring /
    # bimodal) in the substance/contaminant. A graded mix needs a MOBILE bed to
    # sort, so arming a gradation forces erodible_bed=True (the composer also forces
    # the sediment class). The default mix when a grading word is named with no
    # explicit list is GRADATION_PRESETS['graded_sand'] (a fine/med/coarse sand-
    # gravel mix - an honest demo gradation, never a measured site sieve curve).
    _grad_hint = any(w in substance for w in GRADATION_KEYWORDS) or (
        contaminant and any(w in str(contaminant).lower()
                            for w in GRADATION_KEYWORDS))
    sediment_gradation = resolve_gradation(sediment_gradation)
    if sediment_gradation is None and _grad_hint:
        sediment_gradation = list(GRADATION_PRESETS["graded_sand"])
    if sediment_gradation:
        erodible_bed = True  # a graded mix sorts only on a mobile (erodible) bed

    # NESTOR DREDGING (ADR 0254). Dredging layers a dig/dump rule onto the GAIA
    # erodible-bed morphodynamics: without maintenance the navigable channel silts
    # up; the dig rule holds the depth. Armed by an explicit dredging=True OR by
    # dredging vocabulary (dredge / dredging / maintenance dredging / spoil /
    # disposal / shoaling) in the substance/contaminant. NESTOR digs a real bed
    # stock and needs non-cohesive sand, so dredging FORCES the sediment class +
    # erodible_bed=True. dredge_mode selects the rule: "scheduled" (remove a target
    # volume over a window) or "criterion" (dig only where the silted bed rises
    # within a tolerance of the design grade). Zone geometry + volumes/rates are
    # un-fetchable engineering surfaced through the input-review gate with the
    # worker's labeled defaults; an explicit value overrides. dredge_disposal also
    # places the spoil in a downstream disposal zone.
    _dredge_hint = any(w in substance for w in DREDGE_KEYWORDS) or (
        contaminant and any(w in str(contaminant).lower() for w in DREDGE_KEYWORDS))
    if dredging is None:
        dredging = bool(_dredge_hint)
    else:
        dredging = bool(dredging)
    if dredging:
        # NESTOR digs a real (mobile) bed stock -> erodible_bed=True, which the
        # model's single-source-of-truth gate cascades into the sediment/GAIA class
        # (so classification can never diverge from the morphodynamics run).
        erodible_bed = True
    _dm = str(dredge_mode or "scheduled").lower()
    if _dm not in ("scheduled", "criterion"):
        _dm = "scheduled"
    dredge_mode = _dm
    dredge_volume_m3 = _pos_float(dredge_volume_m3, 1.0, 1.0e7)
    dredge_crit_depth_m = _pos_float(dredge_crit_depth_m, 0.01, 20.0)
    dredge_dig_depth_m = _pos_float(dredge_dig_depth_m, 0.05, 30.0)
    dredge_disposal = bool(dredge_disposal) if dredge_disposal is not None else False

    # TELEMAC-PHYS-1 constitutive-physics overrides (advanced / demo-default
    # levers). Coerce + CLAMP to the physics_registry ranges here so a set value
    # never errors the call (matches this tool's defensive style); the workflow
    # re-validates via validate_and_resolve_physics. Any UNSET value stays None,
    # so the worker emits the historical deck literal (byte-identical).
    friction_coefficient = _pos_float(friction_coefficient, 10.0, 90.0)
    velocity_diffusivity = _pos_float(velocity_diffusivity, 1e-3, 10.0)
    tracer_diffusivity = _pos_float(tracer_diffusivity, 1e-3, 10.0)
    # WIND-STRESS FORCING: clamp speed to a sane meteorological band [0, 60] m/s
    # (0 -> no wind, unchanged solve); normalize the FROM-direction to [0, 360).
    try:
        wind_speed_mps = max(0.0, min(60.0, float(wind_speed_mps)))
    except (TypeError, ValueError):
        wind_speed_mps = 0.0
    try:
        wind_direction_deg = float(wind_direction_deg) % 360.0
    except (TypeError, ValueError):
        wind_direction_deg = 0.0
    if friction_law is not None:
        try:
            friction_law = int(friction_law)
        except (TypeError, ValueError):
            friction_law = None
        else:
            if friction_law not in (2, 3, 4):
                logger.warning(
                    "telemac_river_dye: friction_law %r not in {2,3,4} - ignored",
                    friction_law,
                )
                friction_law = None

    logger.info(
        "telemac_river_dye location=%r bbox=%s spill_frac=%.3g pulse_s=%.0f dye=%.4g "
        "reach_km=%.3g sim_s=%.0f",
        location, coerced_bbox, spill_fraction, spill_duration_s,
        dye_concentration_mgl, reach_length_km, sim_duration_s,
    )

    try:
        peak = await model_telemac_river_dye(
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
            erodible_bed=erodible_bed,
            bed_thickness_m=bed_thickness_m,
            bedload_formula=bedload_formula,
            morphological_factor=morphological_factor,
            sediment_gradation=sediment_gradation,
            dredging=dredging,
            dredge_mode=dredge_mode,
            dredge_volume_m3=dredge_volume_m3,
            dredge_disposal=dredge_disposal,
            dredge_crit_depth_m=dredge_crit_depth_m,
            dredge_dig_depth_m=dredge_dig_depth_m,
            friction_coefficient=friction_coefficient,
            friction_law=friction_law,
            velocity_diffusivity=velocity_diffusivity,
            tracer_diffusivity=tracer_diffusivity,
            wind_speed_mps=wind_speed_mps,
            wind_direction_deg=wind_direction_deg,
            rainfall_mm_per_day=rainfall_mm_per_day,
            evaporation_mm_per_day=evaporation_mm_per_day,
            rainfall_gridmet_window=rainfall_gridmet_window,
            compute_class=compute_class,
            bank_source=bank_source,
            discharge_m3s=(float(discharge_m3s) if discharge_m3s is not None else None),
            input_mode=input_mode,
            domain_clamp_notes=list(_domain_clamps),
        )
        logger.info(
            "telemac_river_dye complete layer_id=%s dye_cmax_mgl=%.4g plume_reach_m=%s "
            "active_frames=%s uri=%s",
            peak.layer_id, peak.dye_cmax_mgl, peak.plume_reach_m,
            peak.active_frames, peak.uri,
        )
        return peak
    except asyncio.CancelledError:
        raise
    except (TelemacBanksUnavailableError, TelemacReachDegenerateError):
        # No inexplicit mesh-source fallback (leg 1) / degenerate-reach gate:
        # RE-RAISE so the adapter's summarize_tool_result surfaces the typed
        # retryable gate + .suggestions (bank_source="constant_ribbon" /
        # longer reach_length_km / explicit river_name) and it rides the
        # tool-retry loop, rather than being swallowed into a flat error dict.
        raise
    except (TelemacDyeScenarioError, PostprocessTelemacError) as exc:
        logger.warning("telemac_river_dye failed: %s (%s)", getattr(exc, "error_code", "?"), exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "TELEMAC_RUN_FAILED"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 - defensive catch-all
        logger.exception("telemac_river_dye unexpected failure")
        return {
            "status": "error",
            "error_code": "TELEMAC_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
import asyncio
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)

from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.tools.publish_layer.publish_layer import PublishLayerError, publish_layer
from trid3nt_server.agent.workflows.shared.physics_registry import (
    PhysicsRegistryError,
    applied_physics_delta,
    validate_and_resolve_physics,
)
from trid3nt_server.agent.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
    postprocess_telemac,
    postprocess_telemac_deposition,
)
from trid3nt_server.agent.workflows.telemac.run_telemac import TELEMAC_SOLVER_NAME
from trid3nt_server.agent.workflows.shared.solve_progress import drive_live_solve_progress



#: Half-width (deg) of the bbox fetched around the geocoded centroid to locate a
#: river reach + pick the seed. ~0.06 deg (~6 km) reliably catches the main stem
#: even when the geocoded city centroid sits a few km off the channel.
DEFAULT_RIVER_AOI_HALF_DEG: float = 0.06

#: Demo defaults so a bare "dye spill in the river near X" runs end-to-end. These
#: mirror the worker ReachConfig demo defaults (Snake River near Twin Falls
#: tuning); the composer only overrides intent-bearing fields.
DEFAULT_REACH_LENGTH_KM: float = 6.0
DEFAULT_CHANNEL_WIDTH_M: float = 60.0
DEFAULT_MESH_SIZE_M: float = 14.0
DEFAULT_SPILL_FRACTION: float = 0.25
DEFAULT_PULSE_WINDOW_S: float = 300.0
DEFAULT_SOURCE_Q_M3S: float = 8.0
DEFAULT_DYE_CONC_MGL: float = 100.0
DEFAULT_SIM_DURATION_S: float = 3600.0

# --------------------------------------------------------------------------- #
# Mesh granularity autoscaler - resolution is a USER/LLM lever, NEVER a
# hardcoded constant. The worker meshes a channel ribbon of length L (the reach)
# x width W with a single uniform gmsh target edge length ``h`` (mesh_size_m).
# Two physics/cost constraints bound ``h``:
#   (1) ACROSS-CHANNEL RESOLUTION: the plume must be resolved across the channel,
#       so we need >= N cells spanning the width -> h <= W / N. N is set by the
#       chosen resolution preset (fine = more cells across, coarse = fewer). This
#       is the dominant constraint for a narrow reach.
#   (2) NODE BUDGET: a triangulated ribbon of area A = L*W has ~A/(k*h^2) nodes
#       (k ~ 0.87 for good-quality equilateral triangles). Cap it at NODE_CAP so
#       a long reach can't explode the solve -> h >= sqrt(A / (k*NODE_CAP)).
# The final h is max(across-channel target, budget floor), then clamped to an
# absolute [MESH_H_FLOOR_M, W/2] sanity range (>= 2 cells across no matter what).
# An explicit override_m (LLM/user "use 8 m edges") wins outright but is still
# budget-clamped so a reckless value can't wedge the solver.
# --------------------------------------------------------------------------- #
#: cells-across-the-channel target per resolution preset. "medium"/"auto" ~= the
#: legacy DEFAULT_MESH_SIZE_M (14 m on a 60 m channel = ~4.3 cells across).
MESH_CELLS_ACROSS_BY_PRESET: dict[str, float] = {
    "fine": 6.0,
    "medium": 60.0 / DEFAULT_MESH_SIZE_M,  # ~4.3, parity with the old default
    "auto": 60.0 / DEFAULT_MESH_SIZE_M,
    "coarse": 3.0,
}
#: node-count ceiling for a single local-docker TELEMAC reach (keeps the solve to
#: minutes, not hours). The autoscaler coarsens ``h`` to stay under this.
MESH_NODE_CAP: int = 60000
#: triangulated-ribbon node-density constant (nodes ~= area / (k * h^2)).
#: CALIBRATED against two live TELEMAC meshes of the 8 km x 60 m Snake reach
#: (h=20 -> 3011 nodes -> k=0.40; h=10 -> 10230 nodes -> k=0.47), so the node
#: estimate the approve-mesh gate shows tracks reality within ~15%.
_MESH_NODE_K: float = 0.43
#: absolute gmsh edge-length floor (below this gmsh quality + solve cost degrade).
MESH_H_FLOOR_M: float = 3.0
#: TELEMAC-2D timestep MUST be coupled to the mesh edge length or the solve
#: DIVERGES (CFL). Proven live 2026-07-17 on the 8 km Snake reach:
#:   (h=20, dt=1.0)   -> OK      (h=14, dt=1.0) -> OK (historical default)
#:   (h=10, dt=1.0)   -> CRASH   (h=10, dt=0.714) -> CRASH   (h=10, dt=0.5) -> OK
#: The stable dt scales with the edge length (constant Courant):
#: dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M). Anchored at h=20 m ->
#: 1 s so the law passes THROUGH both live-proven-stable points - (20, 1.0) and
#: (10, 0.5) - and lands at or below the stable dt at every tested size (h=14 ->
#: 0.7 s, safely under its proven-stable 1.0 s; a smaller dt at a fixed mesh is
#: strictly more stable). An earlier /14 anchor shipped h=10 -> 0.714 s, which
#: the live solve REJECTED - hence the conservative /20. This makes "fine" usable.
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
#: floor on the coupled timestep (a runaway-fine mesh can't drive dt to zero).
TIMESTEP_FLOOR_S: float = 0.2
#: wall-clock target for the SUGGESTED mesh's solve (solves
#: land well under an hour at the default; the ladder still offers finer rungs
#: whose honest estimates the user can knowingly accept).
SOLVE_TIME_BUDGET_S: float = 2700.0

#: M3 substance classes: oil-family substances ALSO run the TELEMAC oil-spill
#: module (floating particle slick + the dissolved fraction in the tracer);
#: everything else runs the proven dissolved-tracer path with the label lever.
#: Keys are matched as substrings of the sanitized substance string.
OIL_SUBSTANCE_PRESETS: dict[str, str] = {
    "diesel": "diesel",
    "gasoline": "diesel",
    "petrol": "diesel",
    "heavy fuel": "heavy_fuel",
    "heavy_fuel": "heavy_fuel",
    "bunker": "heavy_fuel",
    "crude": "light_crude",
    "oil": "light_crude",   # generic 'oil' - matched LAST (substring order)
}

#: WAQTEL v1a first-order DECAY substance class (the third class beside oil and
#: the plain conservative dye tracer). A decaying substance (sewage / E. coli /
#: effluent) rides the UNCHANGED dye tracer but the .cas couples WAQTEL with
#: WATER QUALITY PROCESS = 17, whose nametrac branch applies a first-order decay
#: SINK to every existing user tracer - so ZERO new tracers, ZERO postprocess or
#: contract change; only a sink term in the solve. The dict value picks the
#: WAQTEL degradation law + a literature default coefficient; the run_telemac
#: decay_half_life_hours / decay_rate_per_day param OVERRIDES the coefficient.
#: ``law``: 1 = T90 bacterial die-off (coef = T90 hours), 2 = first-order (coef =
#: k in h^-1), 3 = first-order (coef = k in d^-1) - verified vs telemac2d.dico
#: LAW OF TRACERS DEGRADATION. Keys are matched as substrings AFTER the oil set.
#: Bacterial keywords default to T90 ~ 2 h (daylight-freshwater fecal-coliform
#: die-off, a narrated literature default - never a fabricated observation); a
#: generic "decaying" substance falls to a mild first-order k. Both period-
#: stripped variants ("e coli"/"ecoli", from the run_telemac alnum sanitize) and
#: the raw ("e. coli"/"e.coli") are listed so classify matches on either path.
DECAY_SUBSTANCE_PRESETS: dict[str, dict[str, float]] = {
    "sewage": {"law": 1, "coef": 2.0},
    "e. coli": {"law": 1, "coef": 2.0},
    "e.coli": {"law": 1, "coef": 2.0},
    "e coli": {"law": 1, "coef": 2.0},   # period-stripped by the tool sanitize
    "ecoli": {"law": 1, "coef": 2.0},
    "coliform": {"law": 1, "coef": 2.0},
    "coli": {"law": 1, "coef": 2.0},     # catch-all for the coliform family
    "bacteria": {"law": 1, "coef": 2.0},
    "bacterial": {"law": 1, "coef": 2.0},
    "effluent": {"law": 1, "coef": 2.0},
    "wastewater": {"law": 1, "coef": 2.0},
    "die-off": {"law": 1, "coef": 2.0},
    "decaying": {"law": 2, "coef": 0.35},  # generic first-order k in h^-1
    "half-life": {"law": 2, "coef": 0.35},
}

#: GAIA v1 SEDIMENT substance class (the FOURTH class beside oil, decay and the
#: plain conservative dye tracer). A suspended sediment substance (sand / silt /
#: mud / slurry / tailings / sediment-laden runoff) that SETTLES and DEPOSITS: the
#: .cas couples GAIA (COUPLING WITH = 'GAIA'), which appends ONE suspended class as
#: a second telemac2d tracer (r2d 'NCOH SEDIMENT1', g/l == kg/m3) and writes
#: gaia_river.slf CUMUL BED EVOL (deposition, metres) - pinned by the in-image
#: smoke (2026-07-19). v1 is SUPPLY-LIMITED (bed initial thickness 0: only the
#: injected pulse can deposit, nothing erodes). ``grain_size`` is the default d50
#: in microns for the type (fine sand deposits in-reach; silt mostly exits) and is
#: honestly a demo default / user override - no site bed-composition fetcher
#: exists. ``type`` tunes narration + the default grain. Keys are matched as
#: substrings AFTER oil + decay (so a decaying-sediment stays decay only if a
#: decay word appears first; a plain 'sediment' is the sediment class). All v1
#: types are modeled as NON-cohesive (NCO); cohesive mud (Krone/Partheniades) is
#: v2, so 'mud' is a very-fine NCO approximation, narrated honestly.
SEDIMENT_SUBSTANCE_PRESETS: dict[str, dict[str, float | str]] = {
    "sediment-laden runoff": {"type": "silt", "grain_size": 20.0},
    "sediment": {"type": "sand", "grain_size": 200.0},
    "sand": {"type": "sand", "grain_size": 200.0},
    "silt": {"type": "silt", "grain_size": 20.0},
    "mud": {"type": "mud", "grain_size": 8.0},
    "slurry": {"type": "sand", "grain_size": 200.0},
    "tailings": {"type": "silt", "grain_size": 30.0},
}

#: SCOUR / EROSION / mobile-bed vocabulary - the ONE list shared by the
#: classify_substance sediment branch AND the telemac_river_dye _scour_hint
#: auto-arm, so the classification gate and the erodible_bed gate route off the
#: same words and cannot disagree. Naming any of these is the GAIA v2
#: ERODIBLE-BED question (a real erodible bed with active bedload -> the bed
#: scours and re-deposits), routed to the sediment class here and armed as
#: erodible_bed=True by the tool.
SCOUR_KEYWORDS: tuple[str, ...] = (
    "scour", "erosion", "erod", "bedload", "bed load", "degradation",
    "bed lowering", "mobile bed", "morpholog", "aggrad", "degrade",
)


#: GRADED / MIXED-GRAIN vocabulary - the words that mean "a mixture of several
#: grain sizes that SORTS and segregates" (ADR 0240 GAIA v3 multi-class). Naming
#: any of these routes to the sediment class AND auto-arms a default gradation +
#: erodible_bed (a graded mix needs a mobile bed to sort). Distinct from
#: SCOUR_KEYWORDS: scour is single-grain bed lowering; grading is multi-class
#: differential mobility -> armoring / downstream fining.
GRADATION_KEYWORDS: tuple[str, ...] = (
    "graded", "gradation", "mixed grain", "mixed-grain", "multi-grain",
    "multigrain", "multi-class", "multiclass", "grain size distribution",
    "grain-size distribution", "sorting", "segregat", "armor", "armour",
    "poorly sorted", "well sorted", "well graded", "well-graded", "bimodal",
    "fining", "sediment mixture", "grain mixture",
)

#: DREDGING vocabulary (ADR 0254 NESTOR) - words that mean "mechanically maintain
#: a navigable channel against siltation". Naming any of these auto-arms the NESTOR
#: dig/dump rule on the GAIA erodible-bed base (and thus the sediment class +
#: erodible_bed). Distinct from SCOUR/GRADATION: dredging is an ENGINEERED
#: intervention (dig/dump), not a natural transport process.
DREDGE_KEYWORDS: tuple[str, ...] = (
    "dredg", "maintenance dredging", "channel maintenance", "spoil",
    "disposal placement", "shoaling", "navigation channel depth",
    "keep the channel", "maintain the channel", "silt up", "silting",
    "infill the channel", "dig and dump", "dig-and-dump",
)

#: Named demo gradations (d50 in microns, initial fraction) - honest demo mixes,
#: never a measured site sieve curve (no bed-composition fetcher exists). The
#: worker renormalizes fractions + clamps d50 to [5, 2000] um. Default when a
#: grading word is named with no explicit list: ``graded_sand``.
GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}


def resolve_gradation(spec: list | str | None) -> list[list[float]] | None:
    """Coerce a sediment_gradation arg to a clean [[d50_um, fraction], ...] list.

    Accepts a preset NAME (a GRADATION_PRESETS key), an explicit list of
    [d50_um, fraction] pairs (or {'d50_um','fraction'} dicts), or None. Invalid /
    empty specs return None (single-class path). A surviving list of >= 2 classes
    is what arms the multi-class deck; a 1-class list collapses to None (nothing
    to sort). d50 is clamped to [5, 2000] um; fractions floored at 0 (the worker
    renormalizes). The agent-side mirror of the worker ``_normalize_gradation`` so
    the tool and the deck author agree on what counts as a usable gradation.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        key = spec.strip().lower().replace(" ", "_")
        pairs = GRADATION_PRESETS.get(key)
        if pairs is None:
            return None
        spec = pairs
    out: list[list[float]] = []
    try:
        items = list(spec)
    except TypeError:
        return None
    for item in items:
        try:
            if isinstance(item, dict):
                um = float(item.get("d50_um"))
                fr = float(item.get("fraction", 0.0))
            else:
                um = float(item[0])
                fr = float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (um > 0.0) or fr < 0.0:
            continue
        out.append([min(max(um, 5.0), 2000.0), fr])
    if len(out) < 2:
        return None
    out.sort(key=lambda p: p[0])
    return out[:6]


def classify_substance(substance: str) -> tuple[str, str | dict[str, float] | None]:
    """Route a substance string to a TELEMAC substance class + its payload.

    Returns ``('oil', preset)`` for oil-family substances (payload = the
    OIL_PRESETS key), ``('decay', {law, coef})`` for a first-order-decaying
    substance (payload = the WAQTEL degradation law + default coefficient),
    ``('sediment', {type, grain_size})`` for a settling sediment (payload = the
    GAIA type preset + default d50 in microns), and ``('tracer', None)`` for the
    plain conservative dye. Order matters: oil keywords win first, then decay,
    then sediment (grain names OR the SCOUR_KEYWORDS scour/erosion/bedload
    vocabulary), else the tracer default - so 'oil' stays oil, 'sewage' stays
    decay, 'sand' and 'scour' are sediment, and a bare 'dye' stays a conservative
    tracer.
    """
    s = str(substance or "dye").strip().lower()
    for key, preset in OIL_SUBSTANCE_PRESETS.items():
        if key in s:
            return "oil", preset
    for key, params in DECAY_SUBSTANCE_PRESETS.items():
        if key in s:
            return "decay", dict(params)
    for key, params in SEDIMENT_SUBSTANCE_PRESETS.items():
        if key in s:
            return "sediment", dict(params)
    # SCOUR / EROSION / mobile-bed phrasing with no grain named is still the
    # sediment class (the GAIA erodible-bed path) - matched off the SAME
    # vocabulary the tool uses to auto-arm erodible_bed, so the two gates cannot
    # diverge. Defaults to non-cohesive sand (a demo d50 the grain_size_um param
    # overrides).
    if any(w in s for w in SCOUR_KEYWORDS):
        return "sediment", {"type": "sand", "grain_size": 200.0}
    # GRADED / MIXED-GRAIN phrasing (a mixture that sorts) is also the sediment
    # class - the GAIA v3 multi-class path. Routed off the SAME vocabulary the
    # tool uses to auto-arm sediment_gradation, so the gates cannot diverge.
    if any(w in s for w in GRADATION_KEYWORDS):
        return "sediment", {"type": "sand", "grain_size": 200.0}
    return "tracer", None


def plausible_release_coords(
    release_lon: Any, release_lat: Any
) -> tuple[float, float] | None:
    """``(lon, lat)`` when BOTH parse as in-range EPSG:4326 coords, else None.

    Agent-side mirror of the worker's ``resolve_centerline_seed`` plausibility
    gate (the worker image imports no agent code): numeric, lon in [-180, 180],
    lat in [-90, 90] (NaN/inf fail the range comparisons). Used by the tool
    sanitize, the reach-dict threading, the preview mirror and the approve-mesh
    gate so all four agree on what counts as a usable release point."""
    try:
        lon = float(release_lon)
        lat = float(release_lat)
    except (TypeError, ValueError):
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return (lon, lat)


def suggest_time_step_s(mesh_size_m: float) -> float:
    """CFL-safe TELEMAC timestep for a given mesh edge length (OPEN-27).

    dt scales with the edge length (constant Courant), capped at the proven-stable
    1 s for meshes >= 14 m so the default is unchanged, floored so a very fine mesh
    still terminates. Threaded into the worker manifest as ``time_step_s`` (an
    existing ReachConfig field) - no worker rebuild needed.
    """
    h = max(float(mesh_size_m), MESH_H_FLOOR_M)
    dt = TIMESTEP_REF_S * min(1.0, h / MESH_TIMESTEP_REF_M)
    return round(max(dt, TIMESTEP_FLOOR_S), 3)


#: Conservative TELEMAC throughput in node-steps/second, calibrated on the two
#: live 2026-07-17 runs (coarse 3011 nodes x 10800 steps = 86 s; fine 10230 x
#: 21600 = 358 s -> rates 0.377M and 0.618M/s; take the SLOWER so estimates err
#: HIGH - never promise fast then run slow). Covers the worker's full wall
#: (NLDI + DEM + probe + final solve) for a typical reach.
_TELEMAC_NODE_STEPS_PER_S: float = 377_000.0
#: Fixed overhead outside the node-step model (container start + fetches).
_TELEMAC_SOLVE_OVERHEAD_S: float = 45.0


def estimate_telemac_solve_seconds(
    npoin: int, sim_duration_s: float, time_step_s: float
) -> float:
    """Conservative wall-clock estimate for a full TELEMAC dye solve.

    ``wall ~= npoin * (sim_duration / dt) / RATE + overhead`` - the gate card's
    ``estimated_solve_seconds``. Errs high by design (the calibrated rate is the
    slower of the two live datapoints)."""
    steps = max(float(sim_duration_s), 0.0) / max(float(time_step_s), 1e-6)
    est = max(int(npoin), 0) * steps / _TELEMAC_NODE_STEPS_PER_S
    return round(est + _TELEMAC_SOLVE_OVERHEAD_S, 1)


def _estimate_mesh_nodes(reach_length_km: float, channel_width_m: float, h: float) -> int:
    """Estimated node count for a length x width channel ribbon meshed at edge ``h``."""
    area = max(reach_length_km, 0.0) * 1000.0 * max(channel_width_m, 0.0)
    if h <= 0.0 or area <= 0.0:
        return 0
    return int(round(area / (_MESH_NODE_K * h * h)))


def suggest_mesh_size_m(
    reach_length_km: float,
    channel_width_m: float,
    resolution: str = "auto",
    override_m: float | None = None,
) -> tuple[float, int, str]:
    """Pick the mesh target edge length ``h``. Returns ``(h, est_nodes, label)``.

    ``resolution`` is a preset ("auto"/"medium"/"fine"/"coarse"); ``override_m`` is
    an explicit edge length that wins outright (still budget-clamped). Never
    returns the hardcoded default blindly - it is always derived from the reach
    geometry + the chosen lever, so a small AOI gets a fine mesh and a long reach
    gets coarsened under the node budget.
    """
    L = max(float(reach_length_km), 0.0)
    W = max(float(channel_width_m), 1.0)
    preset = str(resolution or "auto").strip().lower()

    # budget floor: coarsest h that keeps node count <= MESH_NODE_CAP.
    area = L * 1000.0 * W
    budget_floor = (area / (_MESH_NODE_K * MESH_NODE_CAP)) ** 0.5 if area > 0 else MESH_H_FLOOR_M

    if override_m is not None and float(override_m) > 0.0:
        h = float(override_m)
        label = f"custom {h:.3g} m"
    else:
        cells = MESH_CELLS_ACROSS_BY_PRESET.get(preset, MESH_CELLS_ACROSS_BY_PRESET["auto"])
        h = W / cells
        label = f"auto ({preset})" if preset in ("auto",) else preset

    # apply constraints: never finer than the absolute floor / budget floor, never
    # coarser than 2 cells across the channel.
    h = max(h, MESH_H_FLOOR_M, budget_floor)
    h = min(h, W / 2.0)
    if override_m is not None and h > float(override_m):
        label += f" -> {h:.3g} m (budget-clamped)"

    est_nodes = _estimate_mesh_nodes(L, W, h)
    return round(h, 3), est_nodes, label


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #
class TelemacDyeScenarioError(RuntimeError):
    """Base class for ``model_telemac_river_dye`` failures.

    Carries an open-set ``error_code`` propagated to the agent emitter so the
    failure renders a typed error frame (never a silent dead-end)."""

    error_code: str = "TELEMAC_DYE_SCENARIO_ERROR"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelemacDyeScenarioInputError(TelemacDyeScenarioError):
    """Caller supplied neither a location string nor a bbox (or both)."""

    def __init__(self, message: str) -> None:
        super().__init__("TELEMAC_DYE_SCENARIO_INPUT_INVALID", message)


class TelemacBanksUnavailableError(TelemacDyeScenarioError):
    """The default ``bank_source="nhd_area"`` path found no NHDArea coverage.

    NATE oceanmesh-wave leg 1 (no inexplicit mesh-source fallbacks): the worker
    would not silently substitute the constant-width ribbon for missing real
    banks. This typed, RETRYABLE gate (the DEM_FALLBACK_GATE pattern) names the
    explicit retry ``bank_source="constant_ribbon"`` + the assumed channel width
    and carries ``.suggestions`` so it rides the tool-retry loop and the user
    approves the ribbon substitution conversationally.
    """

    retryable = True

    def __init__(self, assumed_channel_width_m: float | None) -> None:
        self.assumed_channel_width_m = (
            float(assumed_channel_width_m)
            if assumed_channel_width_m is not None
            else None
        )
        width_txt = (
            f"an assumed constant {self.assumed_channel_width_m:g} m channel-width "
            "ribbon"
            if self.assumed_channel_width_m is not None
            else "an assumed constant channel-width ribbon"
        )
        super().__init__(
            "TELEMAC_BANKS_UNAVAILABLE",
            "No USGS NHDArea water polygon covers this river reach, so real "
            'per-station banks could not be sampled for bank_source="nhd_area". '
            "No bank geometry was substituted automatically -- switching to an "
            "assumed channel width is a user decision. Retry with "
            f'bank_source="constant_ribbon" to mesh {width_txt} instead, or name a '
            "reach with mapped NHDArea coverage.",
        )
        self.suggestions = [  # type: ignore[attr-defined]
            'Retry with bank_source="constant_ribbon" to mesh '
            + width_txt
            + " (an assumed width, not real surveyed banks).",
            "Or name a larger/mapped river reach that has USGS NHDArea coverage.",
        ]


class TelemacReachDegenerateError(TelemacDyeScenarioError):
    """The reach geometry is degenerate: the channel is wider than the reach is
    long, so the mesh generator would busy-loop (the live Longview-WA hang: a
    292 m NHDFlowline stub with the 500 m default width). The worker gates this
    BEFORE meshing (never a hang); this typed, RETRYABLE gate names the
    corrective args and rides the tool-retry loop."""

    retryable = True

    def __init__(
        self,
        reach_length_m: float | None = None,
        channel_width_m: float | None = None,
    ) -> None:
        self.reach_length_m = (
            float(reach_length_m) if reach_length_m is not None else None
        )
        self.channel_width_m = (
            float(channel_width_m) if channel_width_m is not None else None
        )
        geom_txt = (
            f" (a {self.reach_length_m:.0f} m reach with a "
            f"{self.channel_width_m:.0f} m channel width)"
            if self.reach_length_m is not None
            and self.channel_width_m is not None
            else ""
        )
        super().__init__(
            "TELEMAC_REACH_DEGENERATE",
            "The reach geometry is degenerate: the channel is wider than the "
            f"reach is long{geom_txt}, so the mesh could not be built. Retry "
            "with a longer reach_length_km, an explicit river_name (re-seeds "
            "onto the named mainstem instead of a short tributary stub), or "
            'bank_source="constant_ribbon" with a smaller channel_width_m.',
        )
        self.suggestions = [  # type: ignore[attr-defined]
            "Retry with a longer reach_length_km (mesh more of the river).",
            "Name the river explicitly (river_name) to re-seed onto the "
            "mainstem rather than a short tributary stub.",
            'Retry with bank_source="constant_ribbon" and a smaller '
            "channel_width_m.",
        ]


# --------------------------------------------------------------------------- #
# Registry / geometry helpers
# --------------------------------------------------------------------------- #
def _s3_object_exists(s3: Any, bucket: str, key: str) -> bool:
    """True when ``s3://bucket/key`` physically exists (HEAD 200).

    The upload-before-register guard for vector layers: a fabricated URI is only
    safe to register once the object is confirmed present. Any client/HEAD error
    (NoSuchKey, 404, transport) reads as absent - never register on doubt."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 -- absent / unreachable == do not register
        return False


async def _call_registry_tool(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke a registry tool fn that may be sync (returns the value) or async
    (returns an awaitable) - normalize both (what _maybe_emit does internally)."""
    import inspect

    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


def _is_state_snap_geocode(geo: Any) -> bool:
    """True when geocode_location fell back to a WHOLE-STATE bbox.

    A state-snap centroid is the middle of the state - as a river-reach seed it
    is ~100+ km of drift (e.g. 'Snake River near Twin Falls' geocoding to
    central Idaho). Never seed a reach from one."""
    return isinstance(geo, dict) and (
        geo.get("source") == "state-bbox-fallback"
        or geo.get("fallback_reason") is not None
    )


def _locality_tail(location: str) -> str | None:
    """Extract the locality phrase from a river+locality compound query.

    'Snake River near Twin Falls, Idaho' -> 'Twin Falls, Idaho'. Nominatim
    often has no feature for the compound but pins the locality fine; the
    worker NLDI-snaps the locality seed to the nearest flowline anyway."""
    import re

    for sep in ("near", "at", "by", "outside", "in"):
        m = re.search(rf"\b{sep}\b(.+)$", location, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip(" ,")
            if tail and tail.lower() != location.strip().lower():
                return tail
    return None


_WATERCOURSE_TYPES = ("river", "creek", "slough", "fork", "bayou")
_NAME_STOPWORDS = frozenset({"the", "a", "an", "on", "in", "into", "near", "at", "by"})


def _named_watercourse(location: str) -> str | None:
    """The GNIS-style watercourse name in a location phrase, or None.

    'Columbia River near Longview, Washington' -> 'Columbia River'. OPEN-26:
    the worker re-seeds onto the NAMED mainstem (gnis_name flowline query)
    before the NLDI position-snap, so a geocode near a confluence stops
    landing the mesh on the tributary/slough."""
    import re

    m = re.search(
        rf"\b((?:[\w'.-]+\s+){{1,3}}(?:{'|'.join(_WATERCOURSE_TYPES)}))\b",
        str(location or ""), flags=re.IGNORECASE,
    )
    if not m:
        return None
    words = m.group(1).split()
    while words and words[0].lower() in _NAME_STOPWORDS:
        words = words[1:]
    if len(words) < 2:  # need at least '<Name> River'
        return None
    return " ".join(w.title() for w in words)


async def _geocode_seed_center(
    geocode_fn: Any, location: str, geo: Any
) -> tuple[float, float, str]:
    """Resolve (lon, lat, name) for the reach seed from a geocode result,
    REJECTING state-snaps (OPEN-25a hardening).

    ``geo`` is the first-attempt result (already fetched by the caller so the
    emit-wrapped card shows the user's own phrase). On a state-snap, retry
    ONCE with the locality tail; if that also snaps (or no tail exists), raise
    the typed ambiguity error instead of simulating the wrong river."""
    if _is_state_snap_geocode(geo):
        tail = _locality_tail(location)
        retry = None
        if tail:
            logger.info(
                "telemac seed geocode: %r snapped to a whole state; retrying "
                "with locality tail %r", location, tail,
            )
            try:
                retry = await _call_registry_tool(geocode_fn, tail)
            except Exception as exc:  # noqa: BLE001 -- fall through to the typed error
                logger.warning("telemac seed geocode retry failed: %s", exc)
        if retry is not None and not _is_state_snap_geocode(retry):
            geo = retry
        else:
            raise TelemacDyeScenarioError(
                "TELEMAC_DYE_GEOCODE_AMBIGUOUS",
                f"geocode_location({location!r}) only matched a whole US state "
                "- too coarse to place a river reach (the centroid would be "
                "~100 km off). Give a more specific place (a city/town near "
                "the reach) or an explicit bbox AOI.",
            )
    glat = geo.get("latitude") if isinstance(geo, dict) else None
    glon = geo.get("longitude") if isinstance(geo, dict) else None
    if glat is None or glon is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_GEOCODE_FAILED",
            f"geocode_location({location!r}) returned no centroid lat/lon.",
        )
    return float(glon), float(glat), str(geo.get("name") or location)


def _registry_fn(name: str) -> Any:
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"required atomic tool {name!r} is not registered.",
        )
    return entry.fn


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _bbox_around(lon: float, lat: float, half_deg: float) -> tuple[float, float, float, float]:
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def _layer_field(result: Any, field: str) -> Any:
    if result is None:
        return None
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field)
    return None


def _river_seed_from_geometry(river_uri: str) -> tuple[float, float] | None:
    """Pick a mid-reach seed ``(lon, lat)`` on the LONGEST flowline in the fetched
    river FlatGeobuf, so the worker's NLDI snap lands on the main stem (not a
    stray ditch). Pure geopandas/shapely; downloads the FGB via the SAME boto3
    client the solver uses (MinIO-aware via AWS_ENDPOINT_URL). Returns ``None`` on
    ANY failure (the composer then falls back to the geocoded centroid, which the
    worker NLDI-snaps regardless)."""
    try:
        from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client, _split_object_uri

        local_fgb: str | None = None
        if river_uri.startswith("s3://") or river_uri.startswith("gs://"):
            _scheme, bucket, key = _split_object_uri(river_uri)
            s3 = _get_s3_client()
            tmp = tempfile.NamedTemporaryFile(
                suffix=".fgb", delete=False, prefix="telemac_river_seed_"
            )
            tmp.close()
            resp = s3.get_object(Bucket=bucket, Key=key)
            with open(tmp.name, "wb") as fh:
                fh.write(resp["Body"].read())
            local_fgb = tmp.name
        else:
            local_fgb = river_uri  # a local path (test seam)

        import geopandas as gpd

        gdf = gpd.read_file(local_fgb)
        if gdf.empty:
            return None
        # Reproject to EPSG:4326 for consistent lon/lat + length ranking in a
        # metric-ish sense (geographic length is a fine proxy for "longest").
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            try:
                gdf = gdf.to_crs(4326)
            except Exception:  # noqa: BLE001
                pass
        lines = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
        if lines.empty:
            return None
        longest = max(lines.geometry, key=lambda g: g.length)
        # Explode a MultiLineString to its longest part, then take the midpoint.
        if longest.geom_type == "MultiLineString":
            longest = max(longest.geoms, key=lambda g: g.length)
        mid = longest.interpolate(0.5, normalized=True)
        return (float(mid.x), float(mid.y))
    except Exception as exc:  # noqa: BLE001 -- seed extraction is best-effort
        logger.warning("telemac dye: river-seed extraction failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Manifest staging (cache bucket)
# --------------------------------------------------------------------------- #
def _stage_manifest(
    reach: dict[str, Any], run_tag: str, *, mesh_only: bool = False
) -> str:
    """Write the ``telemac_river_dye`` worker manifest to the cache bucket and
    return its ``s3://`` URI (``run_solver`` downloads it to the rundir).

    ``mesh_only=True`` (approve-mesh gate) flags the worker's fast
    mesh-preview mode: build the mesh, write ``river.slf`` + the EPSG:4326
    ``mesh_preview.geojson`` wireframe + gate-stat metrics, skip the solve."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
        )
    outputs = [
        "r2d_river.slf",
        "river.slf",
        "river.cli",
        "t2d_river.cas",
        "full_listing.log",
        "telemac_metrics.json",
    ]
    # GAIA v1 sediment class: the deposition SELAFIN + its steering file also ship
    # so the postprocess can build the CUMUL BED EVOL deposition COG + animate the
    # gaia_river.slf mesh sibling. Harmless for non-sediment runs (they are simply
    # never produced, so the supervisor's output glob skips them).
    if str((reach or {}).get("substance_class") or "") == "sediment":
        outputs += ["gaia_river.slf", "gaia_river.cas"]
    if mesh_only:
        outputs = ["river.slf", "river.cli", "mesh_preview.geojson",
                   "telemac_metrics.json"]
    manifest = {
        "reach": reach,
        "run_id": run_tag,
        "inputs": [],  # the pipeline self-fetches NHDPlus + the DEM
        "telemac_args": [],  # the image CMD drives the entrypoint
        "outputs": outputs,
    }
    if mesh_only:
        manifest["mesh_only"] = True
    key = f"telemac/{run_tag}/manifest.json"
    s3 = _get_s3_client()
    s3.put_object(
        Bucket=cache_bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{cache_bucket}/{key}"


def _parse_gridmet_window(window: str) -> tuple[str, str]:
    """Parse a ``"YYYY-MM-DD:YYYY-MM-DD"`` rain window -> (start, end) ISO dates.

    Raises ``TelemacDyeScenarioInputError`` on a malformed window so a bad knob
    is a loud typed error, never a silent no-rain solve."""
    parts = [p.strip() for p in str(window or "").split(":") if p.strip()]
    if len(parts) != 2:
        raise TelemacDyeScenarioInputError(
            f"rainfall_gridmet_window must be 'YYYY-MM-DD:YYYY-MM-DD' (got {window!r})."
        )
    import datetime as _dt
    try:
        _dt.date.fromisoformat(parts[0])
        _dt.date.fromisoformat(parts[1])
    except ValueError as exc:
        raise TelemacDyeScenarioInputError(
            f"rainfall_gridmet_window has a non-ISO date: {exc}"
        ) from exc
    return parts[0], parts[1]


def _gridmet_domain_mean_pr(river_bbox: tuple[float, float, float, float],
                            start_date: str, end_date: str) -> float:
    """Domain-mean daily precipitation (mm/day) over ``river_bbox`` for the
    window, from the wired ``fetch_gridmet`` (variable ``pr``, time-mean COG).

    Reuses the registered gridMET fetcher (no new data path), downloads the
    emitted COG, and returns the finite-pixel spatial mean. Raises
    ``TelemacDyeScenarioError`` (TELEMAC_RAIN_SOURCE_FAILED) on any failure so a
    real-storm request never silently degrades to zero rain."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    try:
        layer = TOOL_REGISTRY["fetch_gridmet"].fn(
            bbox=list(river_bbox), variable="pr",
            start_date=start_date, end_date=end_date,
        )
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED",
            f"gridMET precip fetch failed for {start_date}..{end_date}: {exc}",
        ) from exc
    uri = _layer_field(layer, "uri")
    if not uri:
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED", "gridMET fetch returned no COG uri.")
    try:
        if str(uri).startswith("s3://"):
            _, _, rest = str(uri).partition("s3://")
            bucket, _, key = rest.partition("/")
            data = _get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
            with MemoryFile(data) as mem, mem.open() as ds:
                arr = ds.read(1, masked=True).astype("float64")
        else:
            with rasterio.open(str(uri)) as ds:
                arr = ds.read(1, masked=True).astype("float64")
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED",
            f"gridMET COG read failed: {exc}",
        ) from exc
    vals = np.asarray(arr.compressed() if hasattr(arr, "compressed") else arr, dtype="float64")
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED",
            "gridMET precip COG had no finite pixels over the reach AOI.")
    return float(vals.mean())


def _resolve_rain_or_evap_mm_per_day(
    rainfall_mm_per_day: float | None,
    evaporation_mm_per_day: float | None,
    rainfall_gridmet_window: str | None,
    river_bbox: tuple[float, float, float, float],
) -> tuple[float | None, str | None]:
    """Resolve the SIGNED net rain-or-evaporation rate (mm/day) for the deck.

    Precedence for the rain term: ``rainfall_gridmet_window`` (real gridMET
    storm total) supersedes an explicit ``rainfall_mm_per_day``. Evaporation is
    then subtracted (TELEMAC's single signed RAIN OR EVAPORATION keyword:
    positive = net rain, negative = net evaporation). Returns
    ``(signed_rate_or_None, provenance_note)``. ``None`` = no forcing (the deck
    stays byte-identical)."""
    rain: float | None = None
    note_bits: list[str] = []
    if rainfall_gridmet_window is not None and str(rainfall_gridmet_window).strip():
        start_date, end_date = _parse_gridmet_window(rainfall_gridmet_window)
        rain = _gridmet_domain_mean_pr(river_bbox, start_date, end_date)
        note_bits.append(
            f"gridMET pr domain-mean {rain:.1f} mm/day ({start_date}..{end_date})")
    elif rainfall_mm_per_day is not None:
        try:
            rain = float(rainfall_mm_per_day)
        except (TypeError, ValueError):
            rain = None
        else:
            note_bits.append(f"rainfall {rain:.1f} mm/day (user)")
    evap: float | None = None
    if evaporation_mm_per_day is not None:
        try:
            evap = float(evaporation_mm_per_day)
        except (TypeError, ValueError):
            evap = None
        else:
            note_bits.append(f"evaporation {evap:.1f} mm/day")
    if rain is None and evap is None:
        return None, None
    net = (rain or 0.0) - (evap or 0.0)
    # clamp to a physically-sane band (a violent storm ~ 500 mm/day; extreme
    # PET ~ 20 mm/day) so a bad knob cannot destabilize the solve.
    net = float(min(max(net, -50.0), 2000.0))
    return net, "; ".join(note_bits) + f" -> net {net:+.1f} mm/day (distributed on-mesh)"


def _download_telemac_result(run_id: str) -> tuple[str, int]:
    """Download ``r2d_river.slf`` + read ``utm_epsg`` from ``telemac_metrics.json``
    for a completed run. Returns ``(local_slf_path, utm_epsg)``. Raises
    ``TelemacDyeScenarioError`` when the SELAFIN result is missing."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket, _get_s3_client

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    # utm_epsg from telemac_metrics.json (the SELAFIN carries no CRS).
    utm_epsg: int | None = None
    try:
        obj = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")
        metrics = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(metrics, dict) and metrics.get("utm_epsg") is not None:
            utm_epsg = int(metrics["utm_epsg"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac dye: metrics read failed for run %s: %s", run_id, exc)

    slf_key = f"{run_id}/r2d_river.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"telemac-dye-{run_id}-")
    slf_path = str(Path(tmp_dir) / "r2d_river.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}",
        ) from exc

    if utm_epsg is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} produced no utm_epsg in telemac_metrics.json; "
            "cannot georeference the SELAFIN mesh.",
        )
    return slf_path, utm_epsg


def _read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of ``<run_id>/telemac_metrics.json`` from the runs bucket.

    Returns ``{}`` on any miss. The worker uploads this file even on a failed run
    (the supervisor uploads outputs before writing completion.json), so it is the
    channel through which a worker-side typed error_code (e.g.
    ``TELEMAC_BANKS_UNAVAILABLE``) reaches the server."""
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    try:
        s3 = _get_s3_client()
        obj = s3.get_object(
            Bucket=_get_runs_bucket(), Key=f"{run_id}/telemac_metrics.json"
        )
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- absence => no typed gate to surface
        logger.info("telemac: run metrics read miss for %s: %s", run_id, exc)
        return {}


def _normalize_bank_source(value: Any) -> str:
    """Coerce a bank_source arg to the closed set {nhd_area, constant_ribbon}.

    Default + any unknown spelling -> ``nhd_area`` (the real-bank path with its
    typed unavailable gate); legacy/synonym spellings for the ribbon collapse to
    ``constant_ribbon``. Keeps the worker's own legacy-spelling map redundant but
    means the manifest always carries a canonical value."""
    v = str(value or "nhd_area").strip().lower().replace("-", "_")
    if v in ("constant_ribbon", "constant", "ribbon", "constant_width"):
        return "constant_ribbon"
    return "nhd_area"


def _raise_if_banks_unavailable(metrics: dict[str, Any]) -> None:
    """Surface the worker's ``TELEMAC_BANKS_UNAVAILABLE`` gate as the typed,
    retryable :class:`TelemacBanksUnavailableError` (rides the tool-retry loop
    with ``.suggestions``). No-op when the worker did not raise the banks gate."""
    if str(metrics.get("error_code") or "") == "TELEMAC_BANKS_UNAVAILABLE":
        raise TelemacBanksUnavailableError(metrics.get("assumed_channel_width_m"))


def _raise_if_reach_degenerate(metrics: dict[str, Any]) -> None:
    """Surface the worker's ``TELEMAC_REACH_DEGENERATE`` gate as the typed,
    retryable :class:`TelemacReachDegenerateError`. No-op otherwise."""
    if str(metrics.get("error_code") or "") == "TELEMAC_REACH_DEGENERATE":
        raise TelemacReachDegenerateError(
            metrics.get("reach_length_m"),
            metrics.get("degenerate_channel_width_m"),
        )


#: Half-width (deg) of the tiny NWM query box centred on the reach seed. NWM is a
#: ~2.7M-reach point layer; a small box keeps it to a handful of reaches so the
#: nearest-to-seed pick lands on the carrier reach, not a distant tributary.
_DISCHARGE_QUERY_HALF_DEG: float = 0.03


def _resolve_reach_discharge(
    seed_lon: float,
    seed_lat: float,
    explicit_discharge_m3s: float | None,
) -> tuple[float, str] | None:
    """Resolve the reach CARRIER discharge (m3/s) for the dye/spill run.

    Replaces the worker's hardcoded 250 m3/s default (the dominant control on
    dilution) with real streamflow. Seam-1: resolves ``fetch_noaa_nwm_streamflow``
    via ``TOOL_REGISTRY`` (never a module internal). NWM returns a point
    FlatGeobuf of NHDPlus reaches carrying ``streamflow_cms`` (m3/s); the reach
    nearest the seed is the carrier.

    - ``explicit_discharge_m3s`` (user-supplied) short-circuits the fetch.
    - On a fetch/read miss returns ``None`` so the caller raises a typed
      ``TELEMAC_DISCHARGE_INPUT_REQUIRED`` gate naming ``discharge_m3s`` - the
      carrier discharge is NEVER silently reverted to the baked 250.

    Returns ``(discharge_m3s, provenance_note)`` or ``None``. Blocking (network +
    geopandas read); the caller wraps it in ``asyncio.to_thread``.
    """
    if explicit_discharge_m3s is not None:
        return (
            float(explicit_discharge_m3s),
            f"carrier discharge {float(explicit_discharge_m3s):.0f} m3/s (user-supplied)",
        )

    from trid3nt_server.agent.tools import TOOL_REGISTRY

    box = (
        seed_lon - _DISCHARGE_QUERY_HALF_DEG,
        seed_lat - _DISCHARGE_QUERY_HALF_DEG,
        seed_lon + _DISCHARGE_QUERY_HALF_DEG,
        seed_lat + _DISCHARGE_QUERY_HALF_DEG,
    )
    try:
        layer = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(bbox=box)
    except Exception as exc:  # noqa: BLE001 - a fetch miss => typed gate upstream
        logger.info("telemac: NWM streamflow fetch failed for seed %s (%s)", (seed_lon, seed_lat), exc)
        return None
    uri = getattr(layer, "uri", None) or (layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        return None

    local: str | None = None
    try:
        import geopandas as gpd  # lazy: never imported on the offline path

        from trid3nt_server.agent.tools.simulation.solver.solver import (
            _get_s3_client,
            _split_object_uri,
        )

        _scheme, bucket, key = _split_object_uri(str(uri))
        import os as _os

        fd, local = tempfile.mkstemp(prefix="nwm-", suffix=_os.path.splitext(key)[1] or ".fgb")
        _os.close(fd)
        s3 = _get_s3_client()
        resp = s3.get_object(Bucket=bucket, Key=key)
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
        gdf = gpd.read_file(local, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001 - a read miss => typed gate upstream
        logger.info("telemac: could not read NWM streamflow layer %s (%s)", uri, exc)
        return None
    finally:
        if local:
            import os as _os

            if _os.path.exists(local):
                try:
                    _os.unlink(local)
                except OSError:
                    pass

    best_q: float | None = None
    best_d = float("inf")
    for _idx, row in gdf.iterrows():
        try:
            q = float(row["streamflow_cms"])
        except (KeyError, TypeError, ValueError):
            continue
        geom = row.get("geometry")
        try:
            d = (float(geom.x) - seed_lon) ** 2 + (float(geom.y) - seed_lat) ** 2
        except Exception:  # noqa: BLE001
            d = 0.0
        if d < best_d and q > 0.0:
            best_d = d
            best_q = q
    if best_q is None:
        return None
    return (
        round(best_q, 1),
        f"carrier discharge {best_q:.0f} m3/s (NOAA National Water Model, nearest reach to the seed)",
    )


def _download_telemac_gaia(run_id: str) -> tuple[str | None, dict[str, Any]]:
    """Download ``gaia_river.slf`` + read the sediment metrics from
    ``telemac_metrics.json`` for a GAIA sediment run. Returns
    ``(local_gaia_path_or_None, worker_metrics)``. Fail-open: a missing gaia SLF
    returns ``(None, metrics)`` so the concentration COG still publishes."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket, _get_s3_client

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    worker_metrics: dict[str, Any] = {}
    try:
        obj = s3.get_object(Bucket=runs_bucket,
                            Key=f"{run_id}/telemac_metrics.json")
        m = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(m, dict):
            worker_metrics = m
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac sediment: metrics read failed for %s: %s",
                       run_id, exc)

    gaia_key = f"{run_id}/gaia_river.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"telemac-gaia-{run_id}-")
    gaia_path = str(Path(tmp_dir) / "gaia_river.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=gaia_key)
        with open(gaia_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac sediment: gaia_river.slf missing for %s "
                       "(%s) - deposition COG skipped", run_id, exc)
        return None, worker_metrics
    return gaia_path, worker_metrics


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #
async def model_telemac_river_dye(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    spill_fraction: float = DEFAULT_SPILL_FRACTION,
    spill_duration_s: float = DEFAULT_PULSE_WINDOW_S,
    dye_concentration_mgl: float = DEFAULT_DYE_CONC_MGL,
    reach_length_km: float = DEFAULT_REACH_LENGTH_KM,
    sim_duration_s: float = DEFAULT_SIM_DURATION_S,
    source_q_m3s: float = DEFAULT_SOURCE_Q_M3S,
    channel_width_m: float = DEFAULT_CHANNEL_WIDTH_M,
    river_geometry_uri: str | None = None,
    mesh_resolution: str = "auto",
    mesh_resolution_m: float | None = None,
    release_lon: float | None = None,
    release_lat: float | None = None,
    substance: str = "dye",
    decay_half_life_hours: float | None = None,
    decay_rate_per_day: float | None = None,
    grain_size_um: float | None = None,
    sediment_type: str | None = None,
    erodible_bed: bool = False,
    bed_thickness_m: float | None = None,
    bedload_formula: int | None = None,
    morphological_factor: float | None = None,
    sediment_gradation: list | None = None,
    dredging: bool = False,
    dredge_mode: str = "scheduled",
    dredge_volume_m3: float | None = None,
    dredge_disposal: bool = False,
    dredge_crit_depth_m: float | None = None,
    dredge_dig_depth_m: float | None = None,
    *,
    release_seeds_reach: bool | None = None,
    seed_release_lon: float | None = None,
    seed_release_lat: float | None = None,
    friction_coefficient: float | None = None,
    friction_law: int | None = None,
    velocity_diffusivity: float | None = None,
    tracer_diffusivity: float | None = None,
    wind_speed_mps: float = 0.0,
    wind_direction_deg: float = 0.0,
    rainfall_mm_per_day: float | None = None,
    evaporation_mm_per_day: float | None = None,
    rainfall_gridmet_window: str | None = None,
    compute_class: str = "medium",
    bank_source: str = "nhd_area",
    discharge_m3s: float | None = None,
    input_mode: str | None = None,
    # WAQTEL O2 dissolved-oxygen SAG (do_sag class). When set, the reach is solved
    # STARTING at the fully-mixed discharge (CBOD + DO at the inflow), WATER
    # QUALITY PROCESS = 2 couples the O2 module, and the result is postprocessed to
    # a DISSOLVED-O2 field COG + the along-reach sag curve (TelemacDoLayerURI). The
    # dict carries the O2 knobs: bod_mgl, upstream_do_mgl, saturation_mgl,
    # water_temp_c, k1_per_day, k2_per_day, k2_formula, standard_mgl.
    do_sag_config: dict[str, Any] | None = None,
    domain_clamp_notes: list[str] | None = None,
    pipeline_emitter: Any | None = None,
) -> "TelemacDyeLayerURI | TelemacDoLayerURI":
    """Compose place/AOI -> river reach -> TELEMAC-2D dye pulse -> animated layer.

    Supply exactly one of ``location`` (a place name, geocoded - the natural-prompt
    path) or ``bbox`` (an explicit AOI, e.g. a drawn canvas AOI). Optionally pass a
    ``river_geometry_uri`` (an already-fetched ``fetch_river_geometry`` flowline) to
    reuse it for the seed instead of re-fetching. Returns the published
    ``TelemacDyeLayerURI`` (a ``LayerURI`` subtype) so the emit_tool_call
    ``add_loaded_layer`` gate fires and ``open_case_in_qgis`` discovers the
    SELAFIN mesh sibling for animation.

    Raises ``TelemacDyeScenarioError`` (typed error_code) on any fatal step and
    propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    has_loc = bool(location and str(location).strip())
    has_bbox = bbox is not None
    if has_loc == has_bbox:  # both or neither
        raise TelemacDyeScenarioInputError(
            "supply exactly one of location or bbox "
            f"(got location={has_loc}, bbox={has_bbox})."
        )

    emitter = pipeline_emitter or current_emitter()
    prefetched_river = bool(river_geometry_uri and str(river_geometry_uri).strip())

    # Plan the user-meaningful atomic-tool count for the breadcrumb: geocode
    # (place path only) + fetch_river_geometry (only when NOT pre-fetched) +
    # run_solver + postprocess + publish_layer. Each substep is a no-op when no
    # emitter is bound.
    _planned = 3  # run_solver + postprocess + publish
    if has_loc:
        _planned += 1  # geocode_location
    if not prefetched_river:
        _planned += 1  # fetch_river_geometry
    begin_substeps(current_emitter(), _planned)

    # --- Stage 1: resolve the AOI + centroid (F46: geocode, never hand-type) -- #
    if has_loc:
        geocode_fn = _registry_fn("geocode_location")
        async with substep(current_emitter(), "geocode_location"):
            geo = await _maybe_emit(
                pipeline_emitter,
                name=f"Geocode: {location}",
                tool_name="geocode_location",
                invoke=lambda: geocode_fn(location),
            )
        # OPEN-25a hardening: reject whole-state snaps (retry with the locality
        # tail, else typed ambiguity error - never seed from a state centroid).
        center_lon, center_lat, location_name = await _geocode_seed_center(
            geocode_fn, str(location), geo
        )
    else:
        assert bbox is not None
        center_lon, center_lat = _bbox_center(bbox)
        location_name = f"AOI ({center_lat:.4f}, {center_lon:.4f})"

    river_bbox = _bbox_around(center_lon, center_lat, DEFAULT_RIVER_AOI_HALF_DEG)

    # --- Distributed on-mesh rainfall / evaporation forcing ------------------ #
    # Resolve the SIGNED net RAIN OR EVAPORATION rate (mm/day) BEFORE the solve:
    # a real gridMET storm total (window) supersedes an explicit rate, minus any
    # evaporation. None = no forcing (byte-identical deck). The gridMET fetch is
    # a sync network call, so run it off the loop (no keepalive stall).
    rain_or_evap_mm_per_day, rain_note = await asyncio.to_thread(
        _resolve_rain_or_evap_mm_per_day,
        rainfall_mm_per_day, evaporation_mm_per_day,
        rainfall_gridmet_window, river_bbox,
    )
    if rain_note:
        logger.info("model_telemac_river_dye rainfall forcing: %s", rain_note)

    # --- Stage 2: obtain the river flowline (reuse a provided one, else fetch)
    #     + pick a mid-reach seed. When the caller already fetched the reach
    #     (fetch_river_geometry -> river_geometry_uri), reuse it -- no re-fetch. -- #
    river_layer = None
    if prefetched_river:
        river_uri: str | None = str(river_geometry_uri)
    else:
        fetch_river_fn = _registry_fn("fetch_river_geometry")
        async with substep(current_emitter(), "fetch_river_geometry"):
            river_layer = await _maybe_emit(
                pipeline_emitter,
                name="Fetch river geometry",
                tool_name="fetch_river_geometry",
                invoke=lambda: fetch_river_fn(bbox=river_bbox),
            )
        river_uri = _layer_field(river_layer, "uri")
    # The fetched river flowline is already on the map: the fresh fetch above rides
    # ``emit_tool_call`` (which surfaces the returned vector), and a prefetched uri
    # was surfaced by the parent composer's fetch. No hand-built re-surface here.
    seed: tuple[float, float] | None = None
    if river_uri:
        seed = await asyncio.to_thread(_river_seed_from_geometry, str(river_uri))
    if seed is None:
        # Fall back to the geocoded centroid; the worker NLDI-snaps it to the
        # nearest flowline COMID regardless (honest degrade, never a dead-end).
        seed = (center_lon, center_lat)
        seed_source = "geocoded-centroid (NLDI will snap to the nearest flowline)"
    else:
        seed_source = "mid-reach point on the largest fetched flowline"
    seed_lon, seed_lat = seed

    # --- Carrier discharge: real NWM streamflow, or a typed input gate --------- #
    # The carrier discharge governs dilution/transport; it was a hidden worker
    # constant (250 m3/s). Resolve real NWM streamflow at the seed reach (or honor
    # an explicit discharge_m3s). A miss STOPS with a typed gate naming
    # discharge_m3s - never a silent revert to the baked constant. This sets
    # reach["inflow_q_m3s"] as a boundary condition; it is INDEPENDENT of the
    # bank_source work (the worker's width heuristic only fires on the 250 default,
    # which a resolved value now supersedes).
    _discharge = await asyncio.to_thread(
        _resolve_reach_discharge, seed_lon, seed_lat, discharge_m3s
    )
    if _discharge is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DISCHARGE_INPUT_REQUIRED",
            (
                "The NOAA National Water Model streamflow lookup found no carrier "
                "discharge for this river reach, so the discharge that governs "
                "dilution is not fabricated. Retry with an explicit discharge_m3s "
                "(steady upstream carrier discharge, m3/s) for the reach - or name a "
                "reach with NWM (CONUS) coverage."
            ),
        )
    inflow_q_m3s, discharge_note = _discharge
    logger.info(
        "model_telemac_river_dye: %s (seed=%.5f,%.5f)",
        discharge_note, seed_lon, seed_lat,
    )

    # --- ADR 0107 two-mode input gate: review-before-run -----------------------
    # The carrier discharge (real NWM or user) governs dilution and is the
    # physically-dominant reviewable input; present it (with the bank source) for
    # review/adjust before the expensive TELEMAC solve in user_gated mode. auto
    # (session default) + headless proceed labeled. (The mesh preview gate at the
    # server pre-dispatch seam is complementary -- it shows the meshed geometry.)
    _q_user = discharge_m3s is not None
    _review_entries = [
        SyntheticInput(
            param="discharge_m3s", value=round(float(inflow_q_m3s), 2),
            units="m^3/s", basis="user" if _q_user else "fetched",
            real_source_if_any=(None if _q_user else "NOAA National Water Model streamflow"),
            note="carrier discharge governing dilution",
        ),
        SyntheticInput(
            param="bank_source",
            value=_normalize_bank_source(bank_source),
            basis="fetched" if _normalize_bank_source(bank_source) == "nhd_area"
            else "default_demo",
            note=("real NHDArea banks" if _normalize_bank_source(bank_source) == "nhd_area"
                  else "assumed constant-width ribbon"),
        ),
    ]
    _review = await gate_input_review(
        tool_name="telemac_river_dye", mode=input_mode,
        entries=_review_entries, params={"discharge_m3s": float(inflow_q_m3s)},
    )
    if _review.cancelled:
        raise TelemacDyeScenarioError(
            "USER_INPUT_CANCELLED",
            f"telemac_river_dye {_review.cancel_reason}",
        )
    inflow_q_m3s = float(_review.params.get("discharge_m3s", inflow_q_m3s))

    # --- Stage 3: stage the worker manifest (ReachConfig overrides) ----------- #
    # mesh resolution is derived from the reach geometry + the chosen lever
    # (auto/preset/explicit override), NEVER the hardcoded default. Surfaced on the
    # returned layer so the agent narrates it and the approve-mesh gate can show it.
    mesh_size_m, mesh_node_estimate, mesh_resolution_label = suggest_mesh_size_m(
        reach_length_km=reach_length_km,
        channel_width_m=channel_width_m,
        resolution=mesh_resolution,
        override_m=mesh_resolution_m,
    )
    # OPEN-27: couple the timestep to the mesh so a finer mesh does not diverge
    # (CFL). Proven live: fine h=10 crashed at fixed dt=1 s, ran clean at dt<=0.5.
    time_step_s = suggest_time_step_s(mesh_size_m)
    logger.info(
        "model_telemac_river_dye mesh granularity: %s -> h=%.3g m "
        "(~%d nodes, dt=%.3g s, reach=%.3g km x %.3g m)",
        mesh_resolution_label, mesh_size_m, mesh_node_estimate, time_step_s,
        reach_length_km, channel_width_m,
    )
    reach_name = _slug(location_name)
    # OPEN-26: hand the worker the NAMED watercourse so it re-seeds onto the
    # gnis_name mainstem (confluence disambiguation, Columbia-proven).
    river_name = _named_watercourse(location or location_name) or ""
    substance_class, substance_payload = classify_substance(substance)
    # GAIA v3 MULTI-CLASS GRADED SEDIMENT (ADR 0240): a resolved gradation of
    # >= 2 grain classes is a graded-sediment SORTING run. It rides the erodible-
    # bed coupling (a mix sorts only on a MOBILE bed), so it forces erodible_bed
    # True here too - a raw dispatch straight to this workflow (bypassing the tool
    # arm) still routes correctly, keeping the erodible/sediment gates in lock-step.
    sediment_gradation = resolve_gradation(sediment_gradation)
    if sediment_gradation:
        erodible_bed = True
    # SINGLE SOURCE OF TRUTH for the erodible-bed / GAIA gate (ADR 0216
    # false-green fix). An armed erodible bed - an explicit erodible_bed knob OR
    # the scour/erosion/bedload auto-arm in telemac_river_dye - IS a GAIA
    # morphodynamics run, so it MUST route through the sediment class. Otherwise
    # the erodible_bed flag and the substance_class gate diverge: erodible_bed
    # reads True (mesh accepted, layers published) while classify returns a
    # non-sediment class, so author_deck couples NOTHING and the run is a plain
    # tracer solve that only LOOKS morphodynamic. Forcing sediment here makes the
    # divergence impossible-by-construction (asserted below).
    if erodible_bed and substance_class != "sediment":
        logger.info(
            "telemac dye: erodible_bed armed but classify(%r)=%s - forcing the "
            "sediment class (GAIA erodible-bed morphodynamics)",
            substance, substance_class)
        substance_class = "sediment"
        substance_payload = dict(SEDIMENT_SUBSTANCE_PRESETS["sediment"])
    # Honesty floor: an armed erodible bed can NEVER end tracer-classified (a
    # silent no-GAIA fallback) - the deck couples GAIA or the run does not claim
    # morphodynamics.
    assert not (erodible_bed and substance_class != "sediment"), (
        "erodible_bed armed must route to the sediment/GAIA class, never tracer")
    oil_preset = substance_payload if substance_class == "oil" else None
    # WAQTEL v1a decay class: classify_substance picks the degradation law + a
    # literature default coefficient; the run_telemac decay_half_life_hours /
    # decay_rate_per_day param OVERRIDES the coefficient (and, for a half-life,
    # switches to first-order law 2 with k = ln2/hl; for a per-day rate, law 3).
    # These are USER/LLM parameters with narrated defaults - never fabricated
    # observations. The dye pulse (SOURCES column) is UNCHANGED; WAQTEL only
    # adds a first-order decay SINK to the existing dye tracer in the solve.
    decay_law = 1
    decay_coef = 2.0
    if substance_class == "decay":
        if isinstance(substance_payload, dict):
            decay_law = int(substance_payload.get("law", 1))
            decay_coef = float(substance_payload.get("coef", 2.0))
        if decay_half_life_hours is not None:
            try:
                hl = float(decay_half_life_hours)
            except (TypeError, ValueError):
                hl = 0.0
            if hl > 0.0:
                hl = min(max(hl, 0.1), 720.0)   # clamp: 6 min .. 30 days
                decay_law = 2                    # first-order, k in h^-1
                decay_coef = round(math.log(2.0) / hl, 6)
        elif decay_rate_per_day is not None:
            try:
                kd = float(decay_rate_per_day)
            except (TypeError, ValueError):
                kd = 0.0
            if kd > 0.0:
                decay_law = 3                    # first-order, k in d^-1
                decay_coef = round(min(max(kd, 0.01), 100.0), 6)
        logger.info(
            "substance %r -> decay class (WAQTEL process 17, law=%d coef=%.4g): "
            "first-order sink on the dye tracer, no new tracer",
            substance, decay_law, decay_coef,
        )
    if substance_class == "oil":
        logger.info("substance %r -> oil class (preset %s): slick particles + "
                    "dissolved tracer", substance, oil_preset)
    # GAIA v1 sediment class: classify picks the type preset + a default d50 in
    # microns; the run_telemac grain_size_um / sediment_type params OVERRIDE them
    # (honest demo defaults, never a fabricated site value - no bed-composition
    # fetcher exists). The worker couples GAIA and the postprocess emits BOTH the
    # suspended-concentration COG and the CUMUL BED EVOL deposition COG.
    sed_grain_um = 200.0
    sed_type = "sand"
    if substance_class == "sediment":
        if isinstance(substance_payload, dict):
            sed_grain_um = float(substance_payload.get("grain_size", 200.0))
            sed_type = str(substance_payload.get("type", "sand"))
        if sediment_type is not None and str(sediment_type).strip():
            sed_type = str(sediment_type).strip().lower()[:8]
        if grain_size_um is not None:
            try:
                sed_grain_um = float(grain_size_um)
            except (TypeError, ValueError):
                pass
        sed_grain_um = float(min(max(sed_grain_um, 5.0), 2000.0))  # [5,2000] um
        logger.info(
            "substance %r -> sediment class (GAIA, type=%s d50=%.4gum): %s",
            substance, sed_type, sed_grain_um,
            "v2 erodible-bed bedload morphodynamics (scour + deposition)"
            if erodible_bed else "v1 suspended settling + supply-limited deposition")
    # 2026-07-18 release-seeding: plausible release coords ride the manifest;
    # whether they ALSO seed the worker's centerline/corridor resolution
    # (nearest flowline to the RELEASE, not the geocode center) is the
    # release_seeds_reach tri-state. None = no gate ran (raw dispatch) ->
    # call-provided coords seed the reach; the approve-mesh decision tail pins
    # False for a gate-picked click so it moves the SOURCE only (the
    # approved solve must reproduce the previewed mesh).
    release_pair = plausible_release_coords(release_lon, release_lat)
    # decouple: when the gate click overwrote release_lon/release_lat,
    # the decision tail threads the ORIGINAL call coords separately so the
    # reach still seeds from the pair the preview meshed from - the click
    # moves the SOURCE only. Absent (the common case) the release coords
    # seed as before.
    seed_release_pair = plausible_release_coords(seed_release_lon, seed_release_lat)
    if release_seeds_reach is None:
        release_seeds_reach = release_pair is not None
    seeds_reach = bool(release_seeds_reach) and (
        release_pair is not None or seed_release_pair is not None
    )
    # TELEMAC-PHYS-1 constitutive-physics overrides (advanced / demo-default
    # levers). Only the keys the user/LLM explicitly set are validated + carried
    # onto the manifest; anything unset is absent from `reach`, so the worker
    # ReachConfig field stays None and author_deck emits the historical literal
    # (byte-identical). validate_and_resolve_physics range-checks + coerces.
    _phys_overrides: dict[str, Any] = {}
    if friction_coefficient is not None:
        _phys_overrides["friction_coefficient"] = friction_coefficient
    if friction_law is not None:
        _phys_overrides["friction_law"] = friction_law
    if velocity_diffusivity is not None:
        _phys_overrides["velocity_diffusivity"] = velocity_diffusivity
    if tracer_diffusivity is not None:
        _phys_overrides["tracer_diffusivity"] = tracer_diffusivity
    _resolved_phys: dict[str, Any] = {}
    if _phys_overrides:
        try:
            _resolved_phys = validate_and_resolve_physics("telemac", _phys_overrides)
        except PhysicsRegistryError as exc:
            raise TelemacDyeScenarioInputError(
                f"invalid TELEMAC advanced physics: {exc}"
            ) from exc
        logger.info(
            "model_telemac_river_dye advanced physics applied "
            "(user-provided): %s",
            applied_physics_delta("telemac", _resolved_phys),
        )

    reach: dict[str, Any] = {
        "name": reach_name,
        "seed_lon": round(seed_lon, 6),
        "seed_lat": round(seed_lat, 6),
        **_resolved_phys,
        **({"river_name": river_name} if river_name else {}),
        **({"substance_class": "oil", "oil_preset": oil_preset}
           if substance_class == "oil" else {}),
        **({"substance_class": "decay",
            "decay_law": decay_law, "decay_coef": decay_coef}
           if substance_class == "decay" else {}),
        **({"substance_class": "sediment", "sediment_type": sed_type,
            "grain_size_um": sed_grain_um, "sediment_density": 2650.0,
            "erodible_bed": bool(erodible_bed),
            # v2 erodible-bed tuning rides the manifest ONLY when armed AND set;
            # unset lets the worker ReachConfig defaults apply. When erodible_bed is
            # False (v1 supply-limited) none of these are threaded (byte-identical).
            **({"bed_thickness_m": float(bed_thickness_m)}
               if erodible_bed and bed_thickness_m is not None else {}),
            **({"bedload_formula": int(bedload_formula)}
               if erodible_bed and bedload_formula is not None else {}),
            **({"morphological_factor": float(morphological_factor)}
               if erodible_bed and morphological_factor is not None else {}),
            # GAIA v3 multi-class graded sediment: the resolved [[d50_um,frac],...]
            # gradation rides the manifest ONLY when >= 2 classes survived, flipping
            # write_gaia_deck to the multi-class bedload (sorting) deck. Absent (the
            # single-class case) leaves the deck byte-identical.
            **({"sediment_gradation": sediment_gradation}
               if sediment_gradation else {}),
            # NESTOR DREDGING (ADR 0254): layered onto the erodible-bed base ONLY
            # when armed. dredging=True adds the dig/dump rule (mode + the set
            # engineering numbers ride; unset ones use the worker's labeled
            # defaults surfaced through the input-review gate). Absent when not
            # dredging, so every non-dredging sediment run is byte-identical.
            **({"dredging": True, "dredge_mode": str(dredge_mode or "scheduled"),
                "dredge_disposal": bool(dredge_disposal),
                **({"dredge_volume_m3": float(dredge_volume_m3)}
                   if dredge_volume_m3 is not None else {}),
                **({"dredge_crit_depth_m": float(dredge_crit_depth_m)}
                   if dredge_crit_depth_m is not None else {}),
                **({"dredge_dig_depth_m": float(dredge_dig_depth_m)}
                   if dredge_dig_depth_m is not None else {})}
               if dredging else {})}
           if substance_class == "sediment" else {}),
        # WAQTEL O2 do_sag class: the fully-mixed discharge (CBOD + DO) rides in at
        # the inflow (author_deck O2 branch omits the dye point source entirely).
        # Threaded only when do_sag_config is set, so every non-do_sag run is
        # byte-identical. (source_q/pulse/dye_conc below are unused on this path.)
        **({"substance_class": "do_sag",
            "do_sag_bod_mgl": float(do_sag_config.get("bod_mgl", 20.0)),
            "do_sag_upstream_do_mgl": float(do_sag_config.get("upstream_do_mgl", 9.0)),
            "do_sat_mgl": float(do_sag_config.get("saturation_mgl", 9.0)),
            "do_water_temp_c": float(do_sag_config.get("water_temp_c", 20.0)),
            "do_k1_per_day": float(do_sag_config.get("k1_per_day", 0.3)),
            "do_k2_per_day": float(do_sag_config.get("k2_per_day", 0.9)),
            "do_k2_formula": int(do_sag_config.get("k2_formula", 0)),
            "do_standard_mgl": float(do_sag_config.get("standard_mgl", 5.0))}
           if do_sag_config is not None else {}),
        # WIND-STRESS FORCING: threaded onto the manifest ONLY when a positive
        # speed was requested; absent otherwise, so the worker ReachConfig field
        # stays 0.0 and author_deck emits NO wind block (byte-identical solve).
        **({"wind_speed_mps": float(wind_speed_mps),
            "wind_dir_from_deg": float(wind_direction_deg)}
           if wind_speed_mps and float(wind_speed_mps) > 0.0 else {}),
        # DISTRIBUTED ON-MESH RAINFALL / EVAPORATION: threaded onto the manifest
        # ONLY when a net rate was resolved (explicit knob or gridMET storm);
        # absent otherwise, so the worker ReachConfig field stays None and
        # author_deck emits NO rain block (byte-identical solve).
        **({"rain_or_evap_mm_per_day": float(rain_or_evap_mm_per_day)}
           if rain_or_evap_mm_per_day is not None else {}),
        "nav_direction": "DM",
        "distance_km": float(reach_length_km),
        "channel_width_m": float(channel_width_m),
        # EXPLICIT bank source (leg 1): nhd_area (default, real banks or a typed
        # TELEMAC_BANKS_UNAVAILABLE gate) | constant_ribbon (assumed width).
        "bank_source": _normalize_bank_source(bank_source),
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        "dye_conc_mgl": float(dye_concentration_mgl),
        # user-picked release point overrides spill_frac (worker snaps to
        # the nearest interior mesh node, validated within 2 channel widths).
        **({"release_lon": round(release_pair[0], 6),
            "release_lat": round(release_pair[1], 6)}
           if release_pair is not None else {}),
        **({"seed_from_release": True,
            **({"seed_release_lon": round(seed_release_pair[0], 6),
                "seed_release_lat": round(seed_release_pair[1], 6)}
               if seed_release_pair is not None else {})}
           if seeds_reach else {}),
        "spill_frac": float(min(max(spill_fraction, 0.0), 1.0)),
        "pulse_window_s": float(spill_duration_s),
        "source_q_m3s": float(source_q_m3s),
        # Real carrier discharge (NWM / user-supplied), NOT the worker's baked
        # 250 m3/s. A non-250 value also supersedes the worker width heuristic.
        "inflow_q_m3s": float(inflow_q_m3s),
        "duration_s": float(sim_duration_s),
    }
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_manifest, reach, run_tag)
    logger.info(
        "model_telemac_river_dye staged manifest run_tag=%s seed=(%.5f,%.5f) "
        "seed_source=%s reach=%s -> %s",
        run_tag, seed_lon, seed_lat, seed_source, reach_name, manifest_uri,
    )

    # --- Stage 4: dispatch to the solver (generic run_solver seam) ------------ #
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=TELEMAC_SOLVER_NAME,
        model_setup_uri=manifest_uri,
        compute_class=compute_class,
    )
    run_id = handle.run_id

    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=TELEMAC_SOLVER_NAME,
        handle=handle,
        compute_class=compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=run_id,
            solver=TELEMAC_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=None,
            vcpus=None,
            eta_seconds=None,
        )
    )
    run_result = None

    class _SolveReturnedFailed(RuntimeError):
        pass

    # OPEN-29 companion: the default 1800 s wait outran a cap-sized solve live
    # (74k nodes x 14400 steps ~ 38 min -> publish leg lost to the timeout).
    # Bound by the WORST honest mesh (the node cap; the preview re-clamp keeps
    # any approved h at or under it) with 1.5x headroom, floored at 1800.
    _wait_s = max(
        1800.0,
        estimate_telemac_solve_seconds(
            MESH_NODE_CAP, float(reach["duration_s"]), float(reach["time_step_s"])
        ) * 1.5,
    )
    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle, timeout_s=_wait_s)
            except asyncio.CancelledError:
                logger.info("model_telemac_river_dye cancelled awaiting solver")
                await route_sim_terminal(emitter, _sim_step_id, run_result=None)
                raise
            finally:
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                set_emitter_binding(None)
            if run_result.status != "complete":
                raise _SolveReturnedFailed
    except _SolveReturnedFailed:
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        # A worker that aborted on the nhd_area banks gate surfaces the typed,
        # retryable TELEMAC_BANKS_UNAVAILABLE (naming the constant_ribbon retry)
        # rather than a generic run-failed error (no inexplicit fallback).
        _degraded_metrics = await asyncio.to_thread(
            _read_run_metrics, getattr(run_result, "run_id", None) or run_id
        )
        _raise_if_banks_unavailable(_degraded_metrics)
        _raise_if_reach_degenerate(_degraded_metrics)
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_RUN_FAILED",
            "TELEMAC dye solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )

    # --- Stage 5: download the SELAFIN result + postprocess to the dye COG ---- #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    slf_path, utm_epsg = await asyncio.to_thread(_download_telemac_result, batch_run_id)
    # Bank-source PROVENANCE for the result envelope (leg 1): the worker records
    # the OUTPUT provenance (nhd_area = real sampled banks | constant_ribbon =
    # assumed width) in telemac_metrics.json; carry it onto the layer narration.
    _run_metrics = await asyncio.to_thread(_read_run_metrics, batch_run_id)
    _bank_provenance = str(_run_metrics.get("bank_source") or "constant_ribbon")

    # ADR 0231: surface the in-worker-sampled river bed bathymetry as a
    # role=context input. The bed is fetched + fitted INSIDE the worker
    # (fetch_dem_bed), so the composer has no uri until the worker uploads its
    # sampled bed COG + records the key here (the worker-envelope seam). Best-
    # effort: publish_raster_input_cog never raises, so a missing/failed bed COG
    # never voids the solve. Covers every substance class (do_sag returns below).
    await _surface_bed_bathymetry_input(
        emitter, _run_metrics, batch_run_id, reach_name)

    # --- WAQTEL O2 do_sag: DISSOLVED-O2 field COG + sag curve (early return) --- #
    if do_sag_config is not None:
        do_layer = await _postprocess_and_publish_do_sag(
            slf_path, batch_run_id, utm_epsg, reach_name, location_name,
            do_sag_config, mesh_size_m, mesh_node_estimate, mesh_resolution_label,
            emitter,
        )
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return do_layer

    try:
        async with substep(emitter, "postprocess_telemac"):
            layers, metrics = await asyncio.to_thread(
                postprocess_telemac,
                slf_path,
                run_id=batch_run_id,
                utm_epsg=utm_epsg,
                reach_name=reach_name,
                substance=substance,
                substance_class=substance_class,
            )
    finally:
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    if not layers:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_NO_LAYERS",
            "postprocess_telemac produced no dye layer (empty tracer field?).",
        )
    raw_peak = layers[0]

    # provenance-chain wave: structured provenance for the two physically dominant,
    # previously-buried TELEMAC inputs - the carrier discharge that governs dilution
    # (real NWM streamflow or user-supplied, never the old hidden 250 m3/s) and the
    # bank geometry (real NHDArea polygons vs an assumed constant-width ribbon).
    _telemac_provenance: list[SyntheticInput] = [
        SyntheticInput(
            param="discharge_m3s", value=round(float(inflow_q_m3s), 1), units="m3/s",
            basis="user" if discharge_m3s is not None else "fetched",
            real_source_if_any=(
                None if discharge_m3s is not None
                else "fetch_noaa_nwm_streamflow (NOAA National Water Model)"
            ),
            note="carrier discharge governs dilution/transport",
        ),
        SyntheticInput(
            param="bank_geometry", value=_bank_provenance,
            basis="fetched" if _bank_provenance == "nhd_area" else "default_demo",
            real_source_if_any="USGS NHDArea water polygons" if _bank_provenance == "nhd_area" else None,
            note=(
                None if _bank_provenance == "nhd_area"
                else "assumed constant-width ribbon, not surveyed banks"
            ),
        ),
    ]
    # ADR 0223 (audit #9): if a domain-extent guardrail bound (in the tool's
    # arg-hardening, threaded here), surface it as a labeled provenance entry (R2
    # transparency) rather than only a log line.
    if domain_clamp_notes:
        _telemac_provenance.append(SyntheticInput(
            param="domain_extent_clamped", value="; ".join(domain_clamp_notes),
            basis="default_demo",
            note="requested domain values exceeded the modelable window and were "
                 "clamped to keep the mesh builder tractable (screening guardrail)",
        ))

    # --- Stage 6: publish the peak COG (render chokepoint) + honest narration - #
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(
            _publish_peak_layer, raw_peak, batch_run_id, location_name, reach_name,
            mesh_size_m, mesh_node_estimate, mesh_resolution_label, substance,
            _bank_provenance, _telemac_provenance,
        )

    logger.info(
        "model_telemac_river_dye complete run_id=%s reach=%s "
        "dye_cmax_mgl=%.4g plume_reach_m=%s active_frames=%s peak_uri=%s",
        batch_run_id, reach_name, peak.dye_cmax_mgl, peak.plume_reach_m,
        peak.active_frames, peak.uri,
    )

    # --- GAIA v1 sediment class: deposition COG + fold the deposition scalars -- #
    # The returned primary is the suspended-sediment CONCENTRATION ribbon; the
    # CUMUL BED EVOL deposition tongue is emitted as a SECOND map layer beside it
    # (mirrors the oil-slick emit pattern). GAIA's own listing mass balance
    # supplies deposited_mass_kg / deposit_fraction (Invariant 1) - folded onto the
    # returned peak so the agent narrates them. Best-effort: a deposition-COG
    # failure never voids the concentration layer.
    if substance_class == "sediment":
        try:
            gaia_path, worker_sed = await asyncio.to_thread(
                _download_telemac_gaia, batch_run_id)
            # deposited_mass_kg = NET bed mass (CUMULATED BED EVOLUTIONS), clamped
            # >= 0 - the SAME net quantity the deposition map and deposit_fraction
            # integrate. NEVER the gross CUMULATED DEPOSITION: in a supply-limited
            # v1 run gross deposition can equal gross erosion (re-suspension) with
            # net ~= 0, so the map is correctly suppressed as empty and the narrated
            # mass must be ~0 to match - not the gross figure (honesty-floor).
            _net = worker_sed.get("sediment_net_bed_mass_kg")
            _dep = max(float(_net), 0.0) if _net is not None else None
            peak = peak.model_copy(update={
                "deposited_mass_kg": _dep,
                "deposit_fraction": worker_sed.get("sediment_deposit_fraction"),
                "max_deposition_mm": worker_sed.get("sediment_max_deposition_mm"),
                # GAIA v3 multi-class SORTING (ADR 0240): the surface-D50 spread
                # the worker measured off the MEAN DIAMETER field (None on a
                # single-class run). Folded onto the peak so the agent narrates
                # the sorting signature (Invariant 1 - measured, never invented).
                "sediment_n_classes": worker_sed.get("sediment_n_classes"),
                "sediment_surface_d50_min_um":
                    worker_sed.get("sediment_surface_d50_min_um"),
                "sediment_surface_d50_max_um":
                    worker_sed.get("sediment_surface_d50_max_um"),
                "sediment_surface_d50_range_um":
                    worker_sed.get("sediment_surface_d50_range_um"),
            })
            if gaia_path:
                async with substep(emitter, "postprocess_telemac"):
                    dep_layers, _dep_metrics = await asyncio.to_thread(
                        postprocess_telemac_deposition,
                        gaia_path,
                        run_id=batch_run_id,
                        utm_epsg=utm_epsg,
                        reach_name=reach_name,
                        worker_sed_metrics=worker_sed,
                        erodible=bool(erodible_bed),
                    )
                # v2 erodible-bed: fold the deepest scour (mm) onto the returned
                # peak so the agent narrates the scour limb (Invariant 1 - the
                # value comes from the postprocessed field, never invented).
                if erodible_bed and _dep_metrics.get("max_scour_mm") is not None:
                    peak = peak.model_copy(update={
                        "max_scour_mm": _dep_metrics.get("max_scour_mm")})
                try:
                    Path(gaia_path).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                if dep_layers and emitter is not None:
                    dep_raw = dep_layers[0]
                    try:
                        pub_uri = await asyncio.to_thread(
                            publish_layer,
                            layer_uri=dep_raw.uri,
                            layer_id=dep_raw.layer_id,
                            style_preset=dep_raw.style_preset,
                        )
                        dep_pub = dep_raw.model_copy(update={"uri": pub_uri})
                    except PublishLayerError as exc:
                        logger.warning("sediment deposition publish failed (%s) - "
                                       "emitting the unpublished COG", exc)
                        dep_pub = dep_raw
                    from trid3nt_server.emission.layer_uri_emit import publish_input_layer  # noqa: WPS433
                    emitted = await publish_input_layer(emitter, dep_pub)
                    logger.info("sediment deposition layer emitted=%s id=%s "
                                "max_dep_mm=%s deposited_kg=%s", emitted,
                                dep_pub.layer_id, dep_pub.max_deposition_mm,
                                dep_pub.deposited_mass_kg)
        except (PostprocessTelemacError, TelemacDyeScenarioError) as exc:
            logger.warning("sediment deposition postprocess failed (%s) - the "
                           "concentration COG still stands", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sediment deposition unexpected failure (%s)", exc)

    # --- M3 oil class: publish the floating-slick track as a vector layer ---- #
    # (mesh-preview pattern: the worker wrote slick.geojson next to the result;
    # best-effort - a missing slick never voids the concentration layer)
    #
    # UPLOAD-BEFORE-REGISTER (live bug: a run registered
    # s3://.../slick.geojson but the worker's fail-open drogues parse never wrote
    # the object -> a dangling layer handle). Never register a fabricated URI:
    # HEAD the object first and only publish when it physically exists. A missing
    # slick reads as an honest skip, not a broken layer.
    if substance_class == "oil" and emitter is not None:
        try:
            from trid3nt_contracts.execution import LayerURI  # noqa: WPS433

            from trid3nt_server.emission.layer_uri_emit import publish_input_layer  # noqa: WPS433
            from trid3nt_server.agent.tools.simulation.solver.solver import (  # noqa: WPS433
                _get_runs_bucket,
                _get_s3_client,
            )

            _slick_bucket = _get_runs_bucket()
            _slick_key = f"{batch_run_id}/slick.geojson"
            _slick_exists = await asyncio.to_thread(
                _s3_object_exists, _get_s3_client(), _slick_bucket, _slick_key
            )
            if not _slick_exists:
                logger.warning(
                    "oil slick object absent (s3://%s/%s not written by the "
                    "worker) - slick layer skipped, no dangling handle emitted",
                    _slick_bucket, _slick_key)
            else:
                slick_layer = LayerURI(
                    layer_id=f"telemac-oil-slick-{batch_run_id}",
                    name=f"Oil slick track ({oil_preset}, {reach_name})",
                    layer_type="vector",
                    uri=f"s3://{_slick_bucket}/{_slick_key}",
                    style_preset="nhdplus_flowlines",
                    role="primary",
                    bbox=peak.bbox,
                )
                emitted = await publish_input_layer(emitter, slick_layer)
                logger.info("oil slick layer emitted=%s id=%s", emitted,
                            slick_layer.layer_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("oil slick layer skipped: %s", exc)

    # --- Best-effort downstream concentration chart (never blocks) ----------- #
    if emitter is not None:
        try:
            await _maybe_emit_chart(emitter, metrics, location_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telemac dye: concentration chart skipped: %s", exc)

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------- #
    if emitter is not None and peak.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(peak.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_telemac_river_dye: zoom-to failed: %s", exc)

    return peak


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _slug(name: str) -> str:
    """A safe reach slug for the ReachConfig ``name`` (ASCII, underscores)."""
    keep = [c.lower() if (c.isalnum()) else "_" for c in str(name)]
    slug = "".join(keep).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return (slug or "river_dye")[:48]


async def _postprocess_and_publish_do_sag(
    slf_path: str, run_id: str, utm_epsg: int, reach_name: str,
    location_name: str, do_sag_config: dict[str, Any],
    mesh_size_m: float | None, mesh_node_estimate: int | None,
    mesh_resolution_label: str | None, emitter: Any | None,
) -> "TelemacDoLayerURI":
    """Postprocess a WAQTEL O2 solve to the DISSOLVED-O2 field COG + sag curve,
    publish the COG (render chokepoint), emit the sag-curve dock chart, and return
    the enriched ``TelemacDoLayerURI``. The along-reach distance uses the
    principal-flow-axis proxy (no centerline is threaded to the postprocess; the
    honesty label states it)."""
    from trid3nt_server.agent.workflows.telemac.postprocess_telemac import (
        postprocess_telemac_do,
    )
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_DO_STYLE_PRESET,
        TelemacDoLayerURI,
    )

    async with substep(emitter, "postprocess_telemac"):
        layers, metrics = await asyncio.to_thread(
            postprocess_telemac_do,
            slf_path,
            run_id=run_id,
            utm_epsg=utm_epsg,
            reach_name=reach_name,
            saturation_mgl=float(do_sag_config.get("saturation_mgl", 9.0)),
            upstream_do_mgl=float(do_sag_config.get("upstream_do_mgl", 9.0)),
            bod_upstream_mgl=float(do_sag_config.get("bod_mgl", 20.0)),
            standard_mgl=float(do_sag_config.get("standard_mgl", 5.0)),
        )
    raw = layers[0]
    mesh_meta = {
        "mesh_size_m": mesh_size_m,
        "mesh_node_estimate": mesh_node_estimate,
        "mesh_resolution_label": mesh_resolution_label,
    }

    async with substep(emitter, "publish_layer"):
        published = raw
        if raw.uri.startswith(("s3://", "gs://")):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=raw.uri,
                    layer_id=raw.layer_id,
                    style_preset=raw.style_preset or TELEMAC_DO_STYLE_PRESET,
                )
                published = raw.model_copy(update={"uri": pub_uri, **mesh_meta})
            except PublishLayerError as exc:
                logger.warning("do_sag publish_layer failed (%s) - unpublished COG",
                               exc)
                published = raw.model_copy(update=mesh_meta)
        else:
            published = raw.model_copy(update=mesh_meta)

    if emitter is not None:
        try:
            from trid3nt_server.emission.layer_uri_emit import publish_input_layer
            await publish_input_layer(emitter, published)
        except Exception as exc:  # noqa: BLE001
            logger.warning("do_sag layer emit failed: %s", exc)
        try:
            await _maybe_emit_do_sag_chart(emitter, metrics, location_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("do_sag sag-curve chart skipped: %s", exc)
        if published.bbox:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(published.bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("do_sag zoom-to failed: %s", exc)

    logger.info(
        "model_telemac_do_sag complete run_id=%s reach=%s do_min=%.3g mg/L at "
        "%.0fm violates=%s uri=%s", run_id, reach_name, published.do_min_mgl,
        published.do_min_distance_m or 0.0, published.do_violates_standard,
        published.uri,
    )
    return published


async def _maybe_emit_do_sag_chart(
    emitter: Any, metrics: dict[str, Any], location_name: str
) -> None:
    """Best-effort DO-sag dock chart: DO + CBOD vs downstream distance, with the
    DO standard as a reference rule. Non-blocking; the numbers are honest
    postprocess scalars (the binned centerline curve), never a fabricated line."""
    if not hasattr(emitter, "emit_chart"):
        return
    xs = metrics.get("sag_curve_distance_m")
    do = metrics.get("sag_curve_do_mgl")
    bod = metrics.get("sag_curve_bod_mgl")
    if not xs or not do or len(xs) != len(do):
        return
    std = float(metrics.get("do_standard_mgl", 5.0))
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload  # type: ignore

    do_vals = [{"x_km": round(xs[i] / 1000.0, 4), "v": do[i], "series": "Dissolved O2"}
               for i in range(len(xs))]
    bod_vals = ([{"x_km": round(xs[i] / 1000.0, 4), "v": bod[i], "series": "CBOD"}
                 for i in range(len(xs))] if bod and len(bod) == len(xs) else [])
    vega_lite_spec = {
        "layer": [
            {"mark": {"type": "line", "point": False},
             "data": {"values": do_vals + bod_vals},
             "encoding": {
                 "x": {"field": "x_km", "type": "quantitative",
                       "title": "Downstream distance (km)"},
                 "y": {"field": "v", "type": "quantitative",
                       "title": "Concentration (mg/L)"},
                 "color": {"field": "series", "type": "nominal", "title": None}}},
            {"mark": {"type": "rule", "strokeDash": [6, 4], "color": "#c0392b"},
             "data": {"values": [{"y": std}]},
             "encoding": {"y": {"field": "y", "type": "quantitative"}}},
        ]
    }
    dmin = metrics.get("do_min_mgl")
    dloc = metrics.get("do_min_distance_m")
    verdict = ("violates" if metrics.get("do_violates_standard") else "meets")
    payload = build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=f"Dissolved-oxygen sag - {location_name}",
        caption=(
            f"Streeter-Phelps DO sag: minimum {dmin} mg/L at {dloc} m downstream "
            f"({verdict} the {std:g} mg/L standard, dashed). CBOD decay drives the "
            f"sag; reaeration recovers it. Screening/permit grade."
        ),
    )
    await emitter.emit_chart(payload)


def _publish_peak_layer(
    raw_peak: TelemacDyeLayerURI, run_id: str, location_name: str, reach_name: str,
    mesh_size_m: float | None = None,
    mesh_node_estimate: int | None = None,
    mesh_resolution_label: str | None = None,
    substance: str = "dye",
    bank_source: str = "constant_ribbon",
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> TelemacDyeLayerURI:
    """Publish the peak dye COG through publish_layer (render chokepoint) and
    enrich the narration. On publish failure the raw peak is returned UNCHANGED
    (the raw s3:// COG still lets open_case_in_qgis discover the mesh sibling;
    the dispatch-level emit_layer_uri guardrail handles the map honesty).

    The three mesh_* params are the composer's chosen granularity,
    threaded explicitly - referencing composer locals here was a NameError that
    crashed every publish (caught by the seam audit)."""
    surrogate = ""
    if substance and substance != "dye":
        surrogate = (
            f" NOTE: {substance} is modeled as a passively advected dissolved "
            f"tracer (transport + dilution only) - NOT slick physics "
            f"(no spreading/evaporation/weathering/beaching)."
        )
    banks_note = (
        " Banks: real USGS NHDArea water-polygon geometry (per-station sampled "
        "widths)."
        if bank_source == "nhd_area"
        else " Banks: an ASSUMED constant channel-width ribbon (bank_source="
        "constant_ribbon), not real surveyed banks."
    )
    honesty = (
        f"Idealized demo: a FINITE mid-reach point-source {substance or 'dye'} "
        f"pulse released on "
        f"the real {location_name} river reach (NLDI/NHDPlus geometry) over a "
        f"planar idealized channel bed with prescribed tracer dispersion. The "
        f"raster is the PEAK concentration envelope over the run; the animation "
        f"plays from the native SELAFIN mesh. Not a calibrated site study."
        + banks_note
        + surrogate
    )
    # the chosen mesh granularity travels on every return branch so the
    # agent can narrate it and the approve-mesh gate can display it.
    mesh_meta = {
        "mesh_size_m": mesh_size_m,
        "mesh_node_estimate": mesh_node_estimate,
        "mesh_resolution_label": mesh_resolution_label,
        "synthetic_inputs": list(synthetic_inputs or []),
    }
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak.model_copy(update={"fallback_note": honesty, **mesh_meta})
    layer_id_for_pub = f"telemac-dye-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_telemac_river_dye: publish_layer FAILED layer_id=%s "
            "error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub, exc.error_code, exc,
        )
        return raw_peak.model_copy(update={"fallback_note": honesty, **mesh_meta})
    return TelemacDyeLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        legend=raw_peak.legend,
        fallback_note=honesty,
        dye_cmax_mgl=raw_peak.dye_cmax_mgl,
        dye_peak_time_s=raw_peak.dye_peak_time_s,
        plume_reach_m=raw_peak.plume_reach_m,
        active_frames=raw_peak.active_frames,
        mesh_size_m=mesh_size_m,
        mesh_node_estimate=mesh_node_estimate,
        mesh_resolution_label=mesh_resolution_label,
        synthetic_inputs=list(synthetic_inputs or []),
    )


#: DEM-source label for the bed-COG provenance name (the worker records which DEM
#: rung actually sampled the bed in telemac_metrics.json['bed_cog_source']).
_BED_DEM_SOURCE_LABELS: dict[str, str] = {
    "cop-dem-glo-30": "Copernicus GLO-30",
    "usgs-3dep": "USGS 3DEP",
}


async def _surface_bed_bathymetry_input(
    emitter: Any,
    run_metrics: dict[str, Any],
    run_id: str,
    reach_name: str,
) -> bool:
    """BEST-EFFORT: surface the in-worker-sampled river bed bathymetry (ADR 0231).

    fetch_dem_bed samples + fits the bed INSIDE the worker container (no emitter,
    no uri agent-side), so the honest surfacing path is the worker-envelope seam:
    the worker writes the bed it solved on as ``bed_bathymetry.tif`` (a 4326 COG)
    next to the result and records ``bed_cog`` in telemac_metrics.json. Here we
    ride that object through ``publish_raster_input_cog`` (NO re-upload) as a
    role=context input, provenance (the DEM rung) in the name. NEVER raises -- the
    seam is best-effort end to end, so a missing/failed bed COG never voids the
    solve. Generalizes: any future in-worker fetch surfaces this same way.
    """
    if emitter is None:
        return False
    bed_cog = run_metrics.get("bed_cog")
    if not bed_cog:
        return False  # worker did not write one (older image / write failed)
    try:
        from trid3nt_server.emission.layer_uri_emit import publish_raster_input_cog
        from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket

        source = str(run_metrics.get("bed_cog_source") or "")
        source_label = _BED_DEM_SOURCE_LABELS.get(source, "3DEP/Copernicus")
        cog_uri = f"s3://{_get_runs_bucket()}/{run_id}/{bed_cog}"
        return await publish_raster_input_cog(
            emitter,
            cog_uri=cog_uri,
            layer_id=f"input-river-bed-{new_ulid()}",
            name=(
                f"Input: river bed bathymetry ({_slug(reach_name)}, "
                f"{source_label}-sampled, in-worker)"
            ),
            style_preset="continuous_dem",
            role="context",
        )
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning(
            "_surface_bed_bathymetry_input: non-fatal failure (bed input absent; "
            "the solve is unaffected): %s", exc,
        )
        return False


# --------------------------------------------------------------------------- #
# fast mesh-only preview for the approve-mesh gate
# --------------------------------------------------------------------------- #
async def preview_telemac_mesh(
    params: dict[str, Any], *, emitter: Any = None
) -> dict[str, Any]:
    """Build (only) the TELEMAC mesh for the approve-mesh gate - no solve.

    Called by the server's ``_build_telemac_mesh_envelope`` (the ``run_telemac``
    solver-confirm gate builder, mirror of the SWMM builder) BEFORE the tool
    dispatches: resolves the same seed the composer will, stages a ``mesh_only``
    worker manifest, runs the fast mesh-only container (~10-25 s: gmsh, no DEM,
    no solve), emits the resulting triangle-wireframe GeoJSON as a role="input"
    map layer + a zoom-to, and returns the REAL gate stats::

        {run_id, mesh_size_m, time_step_s, npoin, nelem, edge_mean_m,
         est_solve_seconds, resolution_label, location_name, bbox}

    MUST-MATCH NOTE: the seed derivation below (geocode -> river fetch -> mid-
    reach seed, centroid fallback) intentionally mirrors Stages 1-2 of
    ``model_telemac_river_dye`` - both are cache-backed tool calls, so
    the approved solve re-derives the SAME seed and reproduces the previewed
    mesh. Call-provided release coords are mirrored the same way (2026-07-18):
    both manifests carry release_lon/release_lat + seed_from_release, so the
    worker re-seeds the reach from the RELEASE identically in preview + solve.
    If you change the seed logic THERE, change it HERE.

    Raises on any failure - the gate caller fails OPEN (card skipped, tool runs
    with its own typed errors), matching the SWMM builder convention.
    """
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer
    from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
        run_solver,
        wait_for_completion,
    )
    from trid3nt_contracts.execution import LayerURI

    location = params.get("location")
    coerced_bbox = None
    raw_bbox = params.get("bbox")
    if raw_bbox is not None:
        cb = coerce_bbox_value(raw_bbox)
        if cb is not None:
            coerced_bbox = tuple(cb)
        elif isinstance(raw_bbox, str) and any(c.isalpha() for c in raw_bbox) \
                and not (location and str(location).strip()):
            location = raw_bbox  # LLM put a place name in the bbox field
    has_loc = bool(location and str(location).strip())
    if has_loc and coerced_bbox is not None:
        coerced_bbox = None  # LOCATION wins (mirror of run_telemac, 2026-07-18)
    if not has_loc and coerced_bbox is None:
        raise ValueError("preview_telemac_mesh: no location/bbox in params")

    # Mirror of the tool's LLM-arg hardening (the gate builder sees RAW params):
    # a 50 km reach live-hung gmsh on the meandering centerline - clamp.
    try:
        reach_length_km = float(params.get("reach_length_km") or DEFAULT_REACH_LENGTH_KM)
    except (TypeError, ValueError):
        reach_length_km = DEFAULT_REACH_LENGTH_KM
    reach_length_km = min(max(reach_length_km, 0.5), 8.0)
    try:
        channel_width_m = float(params.get("channel_width_m") or DEFAULT_CHANNEL_WIDTH_M)
    except (TypeError, ValueError):
        channel_width_m = DEFAULT_CHANNEL_WIDTH_M
    channel_width_m = min(max(channel_width_m, 10.0), 1500.0)
    try:
        sim_duration_s = float(params.get("sim_duration_s") or DEFAULT_SIM_DURATION_S)
    except (TypeError, ValueError):
        sim_duration_s = DEFAULT_SIM_DURATION_S
    sim_duration_s = min(max(sim_duration_s, 600.0), 14400.0)
    mesh_resolution = str(params.get("mesh_resolution") or "auto")
    mesh_resolution_m = params.get("mesh_resolution_m")
    river_geometry_uri = params.get("river_geometry_uri")
    if river_geometry_uri and not str(river_geometry_uri).startswith(("s3://", "gs://")):
        river_geometry_uri = None  # pseudo-call string, not a real URI
    # 2026-07-18 release-seeding mirror: plausible CALL-provided release coords
    # ride the preview manifest (release point + seed_from_release) so the
    # previewed mesh resolves the reach the RELEASE point pins - not whatever
    # water body sits nearest the geocoded city center (the Longview failure
    # meshed the Cowlitz with the requested Columbia release outside the mesh
    # bbox). The gate builder then verifies the built mesh bbox actually
    # covers the point (never silently mesh elsewhere). Pre-gate params are
    # RAW LLM args, so plausibility is re-checked exactly like the tool does.
    release_pair = plausible_release_coords(
        params.get("release_lon"), params.get("release_lat")
    )

    # --- Stage 1-2 mirror (QUIET: no substep/tool cards pre-gate) ------------ #
    if has_loc:
        geocode_fn = _registry_fn("geocode_location")
        geo = await _call_registry_tool(geocode_fn, location)
        # OPEN-25a hardening (same as the main composer): reject state-snaps.
        center_lon, center_lat, location_name = await _geocode_seed_center(
            geocode_fn, str(location), geo
        )
    else:
        assert coerced_bbox is not None
        center_lon, center_lat = _bbox_center(coerced_bbox)  # type: ignore[arg-type]
        location_name = f"AOI ({center_lat:.4f}, {center_lon:.4f})"

    river_bbox = _bbox_around(center_lon, center_lat, DEFAULT_RIVER_AOI_HALF_DEG)
    if river_geometry_uri and str(river_geometry_uri).strip():
        river_uri: str | None = str(river_geometry_uri)
    else:
        fetch_river_fn = _registry_fn("fetch_river_geometry")
        river_layer = await _call_registry_tool(fetch_river_fn, bbox=river_bbox)
        river_uri = _layer_field(river_layer, "uri")
    seed: tuple[float, float] | None = None
    if river_uri:
        seed = await asyncio.to_thread(_river_seed_from_geometry, str(river_uri))
    if seed is None:
        seed = (center_lon, center_lat)
    seed_lon, seed_lat = seed

    # --- Granularity + reach dict (mirror of Stage 3) ---------------- #
    mesh_size_m, mesh_node_estimate, mesh_resolution_label = suggest_mesh_size_m(
        reach_length_km=reach_length_km,
        channel_width_m=channel_width_m,
        resolution=mesh_resolution,
        override_m=(float(mesh_resolution_m) if mesh_resolution_m else None),
    )
    time_step_s = suggest_time_step_s(mesh_size_m)
    preview_river_name = _named_watercourse(location or location_name) or ""
    reach: dict[str, Any] = {
        "name": _slug(location_name),
        "seed_lon": round(seed_lon, 6),
        "seed_lat": round(seed_lat, 6),
        **({"river_name": preview_river_name} if preview_river_name else {}),
        # Call-provided release coords seed the worker's centerline/corridor
        # (see the release-seeding mirror note above); the approved solve
        # threads the SAME keys so the reach re-resolves identically.
        **({"release_lon": round(release_pair[0], 6),
            "release_lat": round(release_pair[1], 6),
            "seed_from_release": True} if release_pair is not None else {}),
        "nav_direction": "DM",
        "distance_km": reach_length_km,
        "channel_width_m": channel_width_m,
        # EXPLICIT bank source (leg 1) mirrored into the preview so the gate
        # meshes with the SAME banks the approved solve will, and a nhd_area
        # reach with no NHDArea coverage gates at the preview too.
        "bank_source": _normalize_bank_source(params.get("bank_source")),
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
    }
    # OPEN-29: the suggest_mesh_size_m budget floor estimates nodes from the
    # STATED channel width, but real-bank meshing follows the MEASURED river -
    # live 2026-07-18 the Columbia (stated 150-500 m, real ~1400 m) previewed
    # 295k nodes at h=10 against the 60k cap, cascading into a coarsest-rung
    # solve that outran the wait budget. After the first mesh-only build, if
    # the MEASURED npoin blows the cap, re-derive h from the measured node
    # density (nodes scale ~1/h^2) and rebuild ONCE at the honest edge length.
    for attempt in (1, 2):
        run_tag = new_ulid()
        manifest_uri = await asyncio.to_thread(
            _stage_manifest, reach, run_tag, mesh_only=True
        )
        logger.info(
            "preview_telemac_mesh dispatch run_tag=%s seed=(%.5f,%.5f) h=%.3g dt=%.3g",
            run_tag, seed_lon, seed_lat, mesh_size_m, time_step_s,
        )

        # Fast mesh-only worker run (no sim cards; the gate IS the surface).
        handle = run_solver(
            solver=TELEMAC_SOLVER_NAME,
            model_setup_uri=manifest_uri,
            compute_class="small",
        )
        # A healthy mesh-only run is ~10-40 s; 240 s bounds a hung gmsh so a
        # broken preview cannot park the turn before the gate falls open.
        run_result = await wait_for_completion(handle, poll_interval_s=3, timeout_s=240)
        mesh_run_id = getattr(run_result, "run_id", None) or handle.run_id
        if run_result is None or run_result.status != "complete":
            # A nhd_area preview with no NHDArea coverage surfaces the typed,
            # retryable banks gate (naming the constant_ribbon retry) instead of
            # the generic mesh-build failure -- so the gate text appears at the
            # approve-mesh surface too, not only after the (fail-open) solve.
            _mesh_metrics = await asyncio.to_thread(
                _read_run_metrics, mesh_run_id
            )
            _raise_if_banks_unavailable(_mesh_metrics)
            _raise_if_reach_degenerate(_mesh_metrics)
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BUILD_FAILED",
                "mesh-only preview run did not complete "
                f"(status={getattr(run_result, 'status', None)}).",
            )

        def _read_mesh_metrics() -> dict[str, Any]:
            s3 = _get_s3_client()
            obj = s3.get_object(
                Bucket=_get_runs_bucket(), Key=f"{mesh_run_id}/telemac_metrics.json"
            )
            loaded = json.loads(obj["Body"].read().decode("utf-8"))
            return loaded if isinstance(loaded, dict) else {}

        m = await asyncio.to_thread(_read_mesh_metrics)
        npoin = int(m.get("npoin") or 0)
        nelem = int(m.get("nelem") or 0)
        bbox4326 = m.get("bbox4326")
        if npoin <= 0:
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BUILD_FAILED",
                f"mesh-only preview metrics carry no node count (run {mesh_run_id}).",
            )
        if attempt == 1:
            # Re-clamp the SUGGESTED h from the MEASURED node count against
            # BOTH budgets: the node cap (OPEN-29) and a wall-clock target
            # (any rung the user picks should solve fast;
            # the suggestion itself must land under ~45 min). nodes ~ 1/h^2.
            h_needed = mesh_size_m
            if npoin > MESH_NODE_CAP * 1.15:
                h_needed = max(h_needed, mesh_size_m * (npoin / MESH_NODE_CAP) ** 0.5)
            dur = float(reach.get("duration_s") or DEFAULT_SIM_DURATION_S)
            for _ in range(4):  # dt(h) is piecewise; a few passes converge
                n_pred = npoin * (mesh_size_m / h_needed) ** 2
                est = estimate_telemac_solve_seconds(
                    int(n_pred), dur, suggest_time_step_s(h_needed))
                if est <= SOLVE_TIME_BUDGET_S:
                    break
                h_needed *= (est / SOLVE_TIME_BUDGET_S) ** 0.5
            if h_needed > mesh_size_m * 1.05:
                logger.warning(
                    "preview_telemac_mesh: measured %d nodes at h=%.3g breaks "
                    "the budget (cap %d nodes / %ds solve) - rebuilding once "
                    "at h=%.3g",
                    npoin, mesh_size_m, MESH_NODE_CAP,
                    int(SOLVE_TIME_BUDGET_S), h_needed,
                )
                mesh_size_m = round(h_needed, 1)
                time_step_s = suggest_time_step_s(mesh_size_m)
                reach["mesh_size_m"] = mesh_size_m
                reach["time_step_s"] = time_step_s
                continue
        break

    # --- Emit the wireframe as a role='input' vector layer + zoom-to --------- #
    # current_emitter() is NOT bound in the pre-dispatch gate context (live
    # finding 2026-07-17: emitter=NONE) - the server passes state.emitter in.
    if emitter is None:
        emitter = current_emitter()
    preview_layer = LayerURI(
        layer_id=f"telemac-mesh-preview-{mesh_run_id}",
        name=f"Mesh preview ({mesh_size_m:g} m edges, {npoin:,} nodes)",
        layer_type="vector",
        uri=f"s3://{_get_runs_bucket()}/{mesh_run_id}/mesh_preview.geojson",
        style_preset="nhdplus_flowlines",  # known line preset -> sane wireframe styling
        role="input",
        bbox=tuple(bbox4326) if bbox4326 else None,
    )
    emitted = await publish_input_layer(emitter, preview_layer)
    logger.info(
        "preview_telemac_mesh wireframe emit: emitter=%s emitted=%s layer=%s",
        "bound" if emitter is not None else "NONE", emitted,
        preview_layer.layer_id,
    )
    if emitter is not None and bbox4326:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox4326)})
        except Exception as exc:  # noqa: BLE001 -- preview zoom is best-effort
            logger.warning("preview_telemac_mesh zoom-to failed: %s", exc)

    return {
        "run_id": mesh_run_id,
        "mesh_size_m": float(mesh_size_m),
        "time_step_s": float(time_step_s),
        "npoin": npoin,
        "nelem": nelem,
        "edge_mean_m": m.get("edge_mean_m"),
        "est_solve_seconds": estimate_telemac_solve_seconds(
            npoin, sim_duration_s, time_step_s
        ),
        "resolution_label": mesh_resolution_label,
        "node_estimate": mesh_node_estimate,
        "location_name": location_name,
        "bbox": bbox4326,
        "wireframe_capped": bool(m.get("wireframe_capped")),
        # bank-source provenance for the mesh gate stats (leg 1): nhd_area (real
        # sampled banks) | constant_ribbon (assumed width). The gate card reads it.
        "bank_source": str(m.get("bank_source") or "constant_ribbon"),
        "bank_width_mean_m": m.get("bank_width_mean_m"),
    }


async def _maybe_emit_chart(emitter: Any, metrics: dict[str, Any], location_name: str) -> None:
    """Best-effort dye-concentration summary chart (rise-to-peak). Non-blocking:
    swallows any failure so the map deliverable never depends on a chart. The two
    points are HONEST tracer-field scalars (t0=0 concentration -> the peak
    concentration at its arrival time), not a fabricated curve."""
    if not hasattr(emitter, "emit_chart"):
        return
    cmax = metrics.get("dye_cmax_mgl")
    peak_t = metrics.get("dye_peak_time_s")
    if cmax is None or peak_t is None:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload  # type: ignore

    vega_lite_spec = {
        "mark": {"type": "line", "point": True},
        "data": {
            "values": [
                {"t_s": 0.0, "dye_mgl": 0.0},
                {"t_s": float(peak_t), "dye_mgl": float(cmax)},
            ]
        },
        "encoding": {
            "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
            "y": {
                "field": "dye_mgl",
                "type": "quantitative",
                "title": "Dye concentration (mg/L)",
            },
        },
    }
    payload = build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=f"Peak dye concentration - {location_name}",
        caption=(
            "Reach peak dye concentration and its arrival time (idealized-bed demo)."
        ),
    )
    await emitter.emit_chart(payload)


async def _maybe_emit(
    emitter: Any | None, *, name: str, tool_name: str, invoke: Any
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct."""
    if emitter is not None:
        return await emitter.emit_tool_call(name=name, tool_name=tool_name, invoke=invoke)
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result
