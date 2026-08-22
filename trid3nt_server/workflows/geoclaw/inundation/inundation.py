"""Engine template ``geoclaw_inundation`` - GeoClaw (Clawpack) adaptive-mesh
shallow-water inundation engine (engine-door refactor - GEOCLAW slice; was
``run_geoclaw_inundation``).

The LLM-facing exposure of the GeoClaw shallow-water engine (tsunami run-up /
dam-break / surge run-up - a hazard family SFINCS/SWMM do not cover).
``geoclaw_inundation(...)`` takes the ``GeoClawRunArgs`` scenario/forcing
fields, runs the deterministic fetch -> stage -> solve -> postprocess chain
(``model_geoclaw_inundation`` below, in this module), and returns a
``GeoClawDepthLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the GeoClaw analogue of ``swmm_urban_flood`` (SWMM) /
``modflow_contaminant_plume`` (MODFLOW) / ``sfincs_flood`` (SFINCS). It is a
registered engine TEMPLATE tagged ``engine="geoclaw", tier="template"`` -
EXCLUDED from the default retrieval pool and surfaced only by the ``run_geoclaw``
door's gate expansion (SELECT-THEN-CALL). Like the other templates it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (workflow exposure surface; never
touches the cache shim).

GeoClaw is CONTAINER-ONLY (the Clawpack Fortran lives in the worker container
image, never in the agent venv), so it always dispatches to a local Docker
solver container via the generic run_solver seam.

Determinism boundary (Invariant 1): every depth number the agent narrates comes
from the typed ``GeoClawDepthLayerURI.max_depth_m`` / ``.flooded_area_km2`` /
``.max_inundation_m`` fields the postprocess computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput, render_fallback_line
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.geoclaw_contracts import (
    GEOCLAW_DEPTH_STYLE_PRESET,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec, GateSpec

from trid3nt_server.data import register_tool
from trid3nt_server.data.resolution_declared import enforce_resolution
from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data.publish_layer.publish_layer import PublishLayerError, publish_layer
from trid3nt_server.workflows.geoclaw._template_card import TemplateCard
from trid3nt_server.workflows.geoclaw.earthquake_source import (
    SUBDUCTION_INTERFACE_DIP_DEG,
    SUBDUCTION_INTERFACE_RAKE_DEG,
    EarthquakeSourceError,
    resolve_earthquake_source,
)
from trid3nt_server.workflows.geoclaw.finite_fault import (
    FiniteFaultError,
    fetch_finite_fault_model,
    to_csvfault_text,
)
from trid3nt_server.workflows.geoclaw.scenario_slab2 import (
    ScenarioSlab2Error,
    resolve_slab2_scenario,
)
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
    GEOCLAW_DEFORMATION_STYLE_PRESET,
    GEOCLAW_TARGET_GROUND_RES_M,
    PostprocessGeoClawError,
    build_gauge_timeseries_chart_spec,
    build_geoclaw_deformation_layer,
    build_geoclaw_mesh_layer,
    build_geoclaw_particle_track_layer,
    build_particle_track_chart_spec,
    compute_geoclaw_grid_shape,
    parse_geoclaw_gauge_series,
    postprocess_geoclaw,
)
from trid3nt_server.workflows.geoclaw.run_geoclaw import (
    GEOCLAW_OFFSHORE_SCENARIOS,
    GEOCLAW_SOLVER_NAME,
    GeoClawWorkflowError,
    finalize_geoclaw_domain,
    plan_geoclaw_domain,
    plan_geoclaw_grid,
    reproject_dem_to_4326,
    resolve_offshore_source,
    stage_finite_fault_csv,
    stage_geoclaw_manifest,
)
from trid3nt_server.workflows.shared.roughness_resolve import resolve_overland_manning
from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
from trid3nt_server.emission.layer_uri_emit import (
    emit_layer_uri,
    publish_input_layer,
    stamp_fallbacks,
)
from trid3nt_server.fallbacks import (
    LADDER_ERROR_CODE,
    LadderGap,
    LadderRefused,
    persist_run_activations,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.inundation.inundation"
)

__all__ = [
    "geoclaw_inundation",
    "RunGeoClawError",
    "model_geoclaw_inundation",
    "GeoClawComposerError",
]


class RunGeoClawError(RuntimeError):
    """Raised when the GeoClaw chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card (the run_geoclaw door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "peak inundation depth + a run-up animation for a TSUNAMI / DAM-BREAK / "
        "storm-SURGE run-up (GeoClaw adaptive-mesh finite-volume shallow water)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "scenario (dam_break / tsunami / surge), sim_duration_s, dam_name, "
        "dam_break_depth_m, source_lonlat, source_magnitude, tsunami_dtopo_uri, "
        "surge_forcing_uri, output_frames, amr_levels, manning_n, sea_level_m, "
        "fault_strike_deg/dip/rake/depth_km, extra_topo_uris, "
        "coastal_gauge_lonlat, fgmax_arrival_tol_m, fgout_frames (smooth animation)"
    ),
)


#: the Slab2 SCENARIO subfault-tiling patch size. The finer floor is the
#: Slab2 grid native spacing (~0.05 deg ~ 5 km) -- a tiling finer than the interface
#: grid buys no geometry fidelity; coarser is a valid cheaper (fewer-subfault) tiling,
#: so no upper bound. Named per the NATE convention (any resolution-class param =
#: target_resolution_m).
_SCENARIO_TILING_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=5000.0,
    native_hint="USGS Slab2 interface grid ~0.05 deg (~5 km) (fetch via scenario_slab2)",
    constraint_source="data",
    rationale=(
        "subfault patch edge for the Slab2 scenario tiling; finer than the ~5 km "
        "Slab2 grid buys no interface-geometry fidelity, coarser is a valid cheaper "
        "tiling so no upper bound"
    ),
)

#: follow-up -- the SCENARIO-scale bathymetry target cell (metres). A
#: basin-scale scenario (a full-margin Slab2 rupture encloses a ~6x6 deg domain)
#: has no use for the fine NOAA CUDEM 1/9" (~3 m) nearshore composite: pulling the
#: 90 dense CUDEM tiles that intersect a full Cascadia margin is ~1e8 cells / ~15 GB
#: of nearshore detail that a deep-ocean propagation grid cannot hold. Deep-water
#: tsunami propagation is well-resolved at the ARCMINUTE class (ETOPO 2022's ~15"
#: deep-ocean native; NOAA's operational tsunami propagation grids run ~4'), so a
#: ~1 arcminute (~1852 m at the equator) ETOPO base is the correct, cheaper substrate.
#: This is a DECLARED scenario default (basis recorded in provenance), NOT a universal
#: rollout -- the same per-tool pattern the surge/tidal path already carries (0224);
#: a caller may override it via the composer's bathy_target_resolution_m thread.
_SCENARIO_BATHY_TARGET_RES_M = 1852.0  # 1 arcminute at the equator

#: precedent: at/above this cell the fine CUDEM 1/9" nearshore composite is
#: SKIPPED -- it is coarser than the global ETOPO 2022 15" base's own ~450 m native
#: cell, so CUDEM's fine structure cannot survive resampling onto the coarse grid and
#: reading dozens of per-tile CUDEM COGs is wasted network cost with zero fidelity gain.
_GEOCLAW_CUDEM_SKIP_RES_M = 500.0

_GEOCLAW_INUNDATION_METADATA = AtomicToolMetadata(
    name="geoclaw_inundation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    tier="template",
    gate_spec=GateSpec(
        kind="solver",
        estimate_provider="trid3nt_server.gates.cards.solver_confirm:estimate_geoclaw",
        title="GeoClaw inundation",
        rationale="A consequential GeoClaw solve: confirm before the run.",
    ),
    resolution_specs=(_SCENARIO_TILING_RES_SPEC,),
)


@register_tool(
    _GEOCLAW_INUNDATION_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (Batch worker + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_inundation(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    scenario: str = "dam_break",
    sim_duration_s: float = 3600.0,
    dam_name: str | None = None,
    dam_break_depth_m: float | None = None,
    source_lonlat: tuple[float, float] | list[float] | None = None,
    source_magnitude: float = 8.0,
    tsunami_dtopo_uri: str | None = None,
    earthquake_source: str | None = None,
    earthquake_min_magnitude: float = 7.0,
    earthquake_start_date: str | None = None,
    earthquake_end_date: str | None = None,
    scenario_fault: str | None = None,
    scenario_magnitude: float | None = None,
    scenario_epicenter_lonlat: tuple[float, float] | list[float] | None = None,
    target_resolution_m: float | None = None,
    surge_forcing_uri: str | None = None,
    output_frames: int = 24,
    amr_levels: int = 2,
    manning_n: float | None = None,
    sea_level_m: float = 0.0,
    fault_strike_deg: float | None = None,
    fault_dip_deg: float | None = None,
    fault_rake_deg: float | None = None,
    fault_depth_km: float | None = None,
    extra_topo_uris: list[str] | None = None,
    coastal_gauge_lonlat: tuple[float, float] | list[float] | None = None,
    fgmax_arrival_tol_m: float | None = None,
    lagrangian_particles: list[list[float]] | None = None,
    fgmax_mask: str = "full",
    fgout_frames: int = 0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Run a GeoClaw (Clawpack) shallow-water inundation simulation over an AOI (TSUNAMI/DAM-BREAK/SURGE run-up).

    Fidelity: GeoClaw adaptive-mesh finite-volume run-up (tsunami / dam-break /
    surge); planning-grade run-up envelope, not a calibrated regulatory model.
    Data: for a DAM_BREAK the dam location + released-column height are resolved
    from the real USACE National Inventory of Dams (NID, ``fetch_usace_dams``) -
    by ``dam_name`` when given, else the NID dam nearest the AOI. When no NID dam
    covers the AOI (or a named dam is not found) the run STOPS with a typed
    ``GEOCLAW_DAM_INPUT_REQUIRED`` gate naming ``source_lonlat`` +
    ``dam_break_depth_m`` (never an invented centroid/height). Explicit
    ``source_lonlat`` + ``dam_break_depth_m`` bypass the NID lookup.
    Off-scope: pluvial / riverine / coastal compound flooding -> sfincs_flood;
    urban storm-sewer -> swmm_urban_flood; spectral wave field -> swan_wave_field.

    Use this when: the user wants a TSUNAMI, DAM BREAK/levee failure, or
    shallow-water storm-SURGE RUN-UP inundation depth + animation -- solves 2D
    nonlinear shallow-water equations with adaptive mesh refinement. Do NOT use
    for: rain-driven riverine/coastal compound flooding (``sfincs_flood``);
    urban/pluvial flooding (``swmm_urban_flood``); groundwater plumes
    (``modflow_contaminant_plume``).

    Params:
        bbox: computational-domain AOI, EPSG:4326.
        scenario: ``"dam_break"`` (default, raised water column at t=0),
            ``"tsunami"`` (seafloor-displacement source), or ``"surge"``
            (raised sea surface).
        sim_duration_s: simulated time, seconds (default 3600).
        dam_name: dam_break only, OPTIONAL name of the NID dam to model;
            when given the NID lookup filters to dams whose name contains
            it (nearest match wins). Unset -> the NID dam nearest the AOI.
        dam_break_depth_m: dam_break only, released column height (m).
            Unset -> the real NID ``DAM_HEIGHT`` of the resolved dam
            (feet -> m). An explicit value overrides the NID height.
        source_lonlat: driver-source location. dam_break: unset -> the
            resolved NID dam's coordinates (never the AOI centroid); an
            explicit value overrides. tsunami/surge: unset -> AOI centroid.
        source_magnitude: tsunami synthetic-source Mw (default 8.0).
        tsunami_dtopo_uri: optional prescribed dtopo file (else synthetic
            Okada source).
        earthquake_source: OPTIONAL name of a seismic REGION (geocoded, e.g.
            "Alaska Peninsula", "Aleutian Islands") -- resolves the LARGEST real
            USGS ComCat event there to the tsunami source (epicenter -> source_lonlat,
            focal depth -> fault_depth_km, Mw -> source_magnitude). Forces scenario
            "tsunami". The epicenter/depth/Mw are REAL catalog values; the fault
            mechanism (strike/dip/rake) is DERIVED (a shallow subduction-interface
            thrust) unless fault_* is supplied -- this is surfaced as a labeled
            provenance entry (MODELED deformation, not an observed field). A tsunami
            drives a seafloor-deformation RASTER product alongside the run-up.
        earthquake_min_magnitude: catalog magnitude floor for earthquake_source
            (default 7.0). earthquake_start_date / earthquake_end_date: OPTIONAL
            ISO window narrowing the catalog search.
        scenario_fault: OPTIONAL subduction-zone name for a SCENARIO ("what if")
            tsunami source -- "Cascadia" or "Alaska-Aleutians". Unlike
            earthquake_source (a REAL catalog event), this builds a HYPOTHETICAL
            rupture on the REAL published USGS Slab2 subduction-interface geometry:
            the Slab2 depth/strike/dip grids are tiled into subfaults following the
            CURVED trench, and a target-Mw tapered slip is distributed over them
            (Strasser 2010 area scaling + a Tukey-tapered slip normalized to the
            moment). Forces scenario "tsunami" and drives a multi-subfault Okada
            deformation that tracks the trench curve. LOUDLY labeled a scenario
            (basis "scenario_slab2") -- never confusable with a real event.
        scenario_magnitude: REQUIRED with scenario_fault -- the target moment
            magnitude Mw (e.g. 9.0 for a full-margin Cascadia rupture).
        scenario_epicenter_lonlat: OPTIONAL (lon, lat) hint centering the rupture
            along the interface (and the domain source point); unset -> the
            slip-weighted rupture centroid.
        target_resolution_m: OPTIONAL Slab2 scenario subfault patch edge (m,
            default 20000). Declared range >= 5000 m (Slab2 grid ~5 km native);
            a finer ask is quoted back.
        surge_forcing_uri: optional sea-surface hydrograph CSV.
        output_frames: animation frame count (default 24) -- the deck-side
            cadence lever (the native GeoClaw ``num_output_times`` count, evenly
            spaced across the sim window; the universal ``output_interval_min``
            vocabulary maps as ``round(sim_duration_min / output_interval_min)``).
            Every solver-written frame is published (never subsampled).
        amr_levels: AMR refinement levels (default 2).
        manning_n: bottom-friction coefficient. Default None -> for dam_break /
            surge (land-dominated / mixed-coastal run-up), DERIVED from NLCD land
            cover over the AOI (area-weighted mean of the SFINCS Manning table,
            the same resolution ``geoclaw_storm_surge`` uses), or REFUSES if NLCD
            cannot serve; for tsunami (offshore -- GEOCLAW_OFFSHORE_SCENARIOS,
            deep-ocean propagation), the published Chow (1959) open-water
            standard 0.025 is used (NLCD has no ocean coverage). Supply a value
            for a calibrated run.
        sea_level_m: still-water datum (default 0.0).
        fault_strike_deg/fault_dip_deg/fault_rake_deg/fault_depth_km:
            optional user-gated Okada fault params (tsunami synthetic
            mode); unset substitutes a noted scenario default.
        extra_topo_uris: optional ordered coarse->fine DEM overlays.
        coastal_gauge_lonlat: optional point to record a water-surface
            time series.
        fgmax_arrival_tol_m: optional wet-cell threshold for arrival time
            (default 0.01m when unset).
        lagrangian_particles: optional list of (lon, lat) seed points for
            LAGRANGIAN particle gauges advected by the flow -- drifters that
            trace the depth-averaged velocity (e.g. a harbour wake / vortex).
            When supplied, their drift TRACKS are emitted as a LineString
            product layer plus a cumulative-drift chart. Unset -> none.
        fgmax_mask: fgmax point set -- "full" (default, a uniform grid over the
            whole AOI) or "onshore" (restrict the fgmax maxima to the DEM
            onshore cells via a topotype-3 mask, cutting the fgmax output size
            for a coastal AOI). Only affects tsunami / surge runs.
        fgout_frames: SMOOTH-animation frame count (default 0 = off). When > 0
            (tsunami / surge) the run emits an fgout fixed-grid monitor -- a
            uniform single-resolution grid over the AOI dumped at this many
            EVENLY-SPACED times -- and those frames BECOME the scrubber
            animation series (a smooth cadence decoupled from the coarse AMR
            fort.q output). The peak-depth layer is unchanged. Try ~12-24 for a
            fluid run-up animation. 0 keeps the fort.q-frame animation.
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. ``"user_gated"`` presents the
            resolved inputs (dam height/location, magnitude, window) for review
            before the solver launches; ``"auto"`` (default, or the session
            default) proceeds with the inputs labeled.

    Returns:
        On success: ``GeoClawDepthLayerURI`` -- peak-depth COG plus
        out-of-band per-timestep scrubber animation, with ``max_depth_m``,
        ``flooded_area_km2``, ``max_inundation_m``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).

    ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` -- cache shim not invoked.
    """
    # --- Validate + coerce into the GeoClawRunArgs contract -----------------
    if bbox is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_inundation requires a bbox "
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

    # --- Dam-break source provenance: real NID dam, or a typed input gate -------
    # For a dam_break the location + released-column height are physically
    # dominant; resolve them from the USACE National Inventory of Dams instead of
    # inventing an AOI-centroid + a baked 10 m column. A user who supplies BOTH
    # source_lonlat AND dam_break_depth_m bypasses the lookup (they chose). When
    # the NID has no dam for the AOI and the user did not supply both, STOP with a
    # typed gate naming the manual params - never a silent invented dam.
    effective_source_lonlat = source_lonlat
    effective_dam_depth = dam_break_depth_m
    dam_source_note: str | None = None
    # finite-fault upgrade: a resolved measured-inversion source (staged
    # CSV + rupture footprint), threaded into the run_args when the ComCat event
    # carries a USGS finite-fault product.
    finite_fault_uri: str | None = None
    finite_fault_footprint: tuple[float, float, float, float] | None = None
    # follow-up: a DECLARED basin-scale bathymetry cell floor, set ONLY by
    # the Slab2 SCENARIO front door (a full-margin rupture domain has no use for the
    # fine CUDEM nearshore composite). None on every other path -> the native fetch.
    bathy_target_resolution_m: float | None = None
    # provenance-chain wave: structured per-input provenance for the physically
    # dominant source parameters. Built alongside the prose ``dam_source_note`` and
    # threaded onto the returned layer so the agent narrates demo-vs-fetched.
    provenance: list[SyntheticInput] = []
    _scenario_l = str(scenario).strip().lower()

    # --- Real-event tsunami source: resolve a named seismic region to
    # the LARGEST USGS ComCat event and drive the Okada source from its REAL
    # epicenter / focal depth / Mw. Forces the tsunami scenario. The mechanism
    # (strike/dip/rake) stays DERIVED (subduction-interface thrust) unless the user
    # supplied fault_* -- surfaced as a labeled provenance entry so the modeled
    # deformation is never mistaken for an observed field.
    if earthquake_source:
        try:
            _eq = resolve_earthquake_source(
                str(earthquake_source),
                min_magnitude=float(earthquake_min_magnitude),
                start_date=earthquake_start_date,
                end_date=earthquake_end_date,
            )
        except EarthquakeSourceError as exc:
            return {
                "status": "error",
                "error_code": exc.error_code,
                "error_message": str(exc),
            }
        scenario = "tsunami"
        _scenario_l = "tsunami"
        effective_source_lonlat = (_eq.lon, _eq.lat)
        source_magnitude = float(_eq.magnitude)
        if _eq.depth_km is not None and fault_depth_km is None:
            fault_depth_km = float(_eq.depth_km)
        if fault_dip_deg is None:
            fault_dip_deg = SUBDUCTION_INTERFACE_DIP_DEG
        if fault_rake_deg is None:
            fault_rake_deg = SUBDUCTION_INTERFACE_RAKE_DEG
        provenance.append(SyntheticInput(
            param="earthquake_source",
            value=_eq.provenance_label,
            basis="fetched",
            note="epicenter/depth/Mw from the USGS ComCat catalog (real event)",
        ))

        # --- Finite-fault UPGRADE (the measured-inversion rung) --------------
        # Fetch THIS event's published USGS finite-fault inversion. When present it
        # drives a MULTI-subfault Okada dtopo (real concentrated slip) and its
        # geometry SUPERSEDES the derived subduction-interface mechanism. Absent
        # (or unparseable) -> the single-subfault scaling synthesis is the DEGRADE
        # rung, LOUDLY labeled derived (the data-source fallback norm).
        _ff_model = None
        try:
            _ff_model = await asyncio.to_thread(
                fetch_finite_fault_model, _eq.event_id
            )
        except FiniteFaultError as exc:
            logger.warning(
                "geoclaw_inundation: finite-fault product present but unparseable "
                "for %s (%s) -> single-subfault degrade rung", _eq.event_id, exc,
            )
        if _ff_model is not None and _ff_model.n_subfaults > 1:
            try:
                finite_fault_uri = await asyncio.to_thread(
                    stage_finite_fault_csv, to_csvfault_text(_ff_model)
                )
                finite_fault_footprint = _ff_model.footprint_bbox
            except Exception as exc:  # noqa: BLE001 - stage failure -> degrade rung
                logger.warning(
                    "geoclaw_inundation: finite-fault CSV staging failed (%s) -> "
                    "single-subfault degrade rung", exc,
                )
                finite_fault_uri = None
        if finite_fault_uri is not None:
            provenance.append(SyntheticInput(
                param="finite_fault_model",
                value=_ff_model.provenance_label,
                basis="measured_inversion",
                real_source_if_any=_ff_model.product_url or "USGS finite-fault product",
                note=(
                    f"MEASURED N-subfault slip inversion ({_ff_model.n_subfaults} "
                    f"patches); the Okada deformation is the superposition of the "
                    f"published inverted slip -- NOT a single scaling-law rectangle. "
                    f"Product: {_ff_model.fsp_url}"
                ),
            ))
        else:
            # Degrade rung: no finite-fault product -> a DERIVED single-subfault
            # mechanism (a shallow interface thrust), loudly labeled.
            provenance.append(SyntheticInput(
                param="fault_mechanism",
                value=f"dip={fault_dip_deg} deg, rake={fault_rake_deg} deg",
                basis="derived",
                note=(
                    "no USGS finite-fault product for this event; a single-subfault "
                    "shallow subduction-interface THRUST assumption -- NOT the "
                    "catalog moment tensor; the Okada deformation is MODELED"
                ),
            ))
        logger.info(
            "geoclaw_inundation earthquake_source=%r resolved -> %s "
            "(source_lonlat=%s Mw=%.1f depth_km=%s finite_fault=%s)",
            earthquake_source, _eq.provenance_label,
            effective_source_lonlat, source_magnitude, fault_depth_km,
            (_ff_model.provenance_label if finite_fault_uri else "none (single-subfault)"),
        )

    # --- SCENARIO tsunami source: a HYPOTHETICAL rupture on the REAL USGS
    # Slab2 subduction-interface geometry. Unlike earthquake_source (a named real
    # catalog event), scenario_fault answers "what if <zone> ruptures at M<x>" -- there
    # is NO measured slip to fetch, so the geometry is the published Slab2 dep/str/dip
    # grids and a target-Mw tapered slip is distributed over the tiled CURVED interface.
    # It reuses the SAME finite_fault_uri seam the measured rung uses, so the worker
    # builds a real multi-subfault Okada dtopo whose deformation tracks the trench.
    # LOUDLY labeled basis="scenario_slab2" so it is never mistaken for a real event.
    # There is no degrade rung: a scenario the interface cannot support is a typed
    # error, never a silent single-rectangle fallback.
    elif scenario_fault:
        if scenario_magnitude is None:
            return {
                "status": "error",
                "error_code": "GEOCLAW_SCENARIO_MAGNITUDE_REQUIRED",
                "error_message": (
                    "scenario_fault requires scenario_magnitude (the target moment "
                    "magnitude, e.g. 9.0 for a full-margin Cascadia rupture)."
                ),
            }
        try:
            # None (the module default subfault size) is always in-range; an
            # out-of-declared-range ask is quoted back.
            enforce_resolution(_SCENARIO_TILING_RES_SPEC, target_resolution_m)
        except Exception as exc:  # noqa: BLE001 - ResolutionOutOfRangeError -> typed
            return {
                "status": "error",
                "error_code": "GEOCLAW_INPUT_INVALID",
                "error_message": str(exc),
            }
        _epi: tuple[float, float] | None = None
        if scenario_epicenter_lonlat is not None:
            _el = list(scenario_epicenter_lonlat)
            if len(_el) == 2:
                _epi = (float(_el[0]), float(_el[1]))
        try:
            _scn_model = await asyncio.to_thread(
                resolve_slab2_scenario,
                str(scenario_fault),
                float(scenario_magnitude),
                epicenter_lonlat=_epi,
                target_resolution_m=float(target_resolution_m or 20_000.0),
            )
        except ScenarioSlab2Error as exc:
            return {
                "status": "error",
                "error_code": exc.error_code,
                "error_message": str(exc),
            }
        scenario = "tsunami"
        _scenario_l = "tsunami"
        source_magnitude = float(scenario_magnitude)
        effective_source_lonlat = _epi if _epi is not None else _scn_model.centroid_lonlat
        # follow-up: a full-margin scenario domain is basin-scale -- floor
        # the domain-wide bathymetry to the ~1 arcminute ETOPO deep-water class and
        # skip the fine CUDEM nearshore composite (the fine coastal AOI still nests
        # its own fine SHORE topo). Declared, not a universal rollout.
        bathy_target_resolution_m = _SCENARIO_BATHY_TARGET_RES_M
        try:
            finite_fault_uri = await asyncio.to_thread(
                stage_finite_fault_csv, to_csvfault_text(_scn_model)
            )
            finite_fault_footprint = _scn_model.footprint_bbox
        except Exception as exc:  # noqa: BLE001 - staging failure -> honest typed error
            return {
                "status": "error",
                "error_code": "GEOCLAW_SCENARIO_STAGING_FAILED",
                "error_message": (
                    f"failed to stage the Slab2 scenario fault CSV: {exc}"
                ),
            }
        _peak = _scn_model.max_slip_m
        _avg = (sum(p.slip_m for p in _scn_model.patches) / _scn_model.n_subfaults
                if _scn_model.n_subfaults else 0.0)
        provenance.append(SyntheticInput(
            param="scenario_fault",
            value=(
                f"Slab2 {scenario_fault} M{scenario_magnitude:.1f}: "
                f"{_scn_model.n_subfaults} subfaults, slip {_scn_model.min_slip_m:.1f}-"
                f"{_peak:.1f} m (avg {_avg:.1f} m)"
            ),
            basis="scenario_slab2",
            real_source_if_any="USGS Slab2 (DOI 10.5066/F7PV6JNV)",
            note=(
                "HYPOTHETICAL scenario rupture -- NOT a real event. Interface geometry "
                "(depth/strike/dip) is the REAL published USGS Slab2 model; the rupture "
                "size uses Strasser et al. (2010) interface scaling and the slip is a "
                "Tukey-tapered distribution normalized to the target moment. The Okada "
                "deformation is MODELED."
            ),
        ))
        provenance.append(SyntheticInput(
            param="bathy_target_resolution_m",
            value=round(float(bathy_target_resolution_m), 1),
            units="m",
            basis="default_demo", consequence="numerical",
            real_source_if_any="ETOPO 2022 (NCEI, ~15 arcsec deep-ocean native)",
            note=(
                f"DECLARED basin-scale bathymetry floor (~1 arcminute) for the "
                f"full-margin scenario domain: deep-ocean tsunami propagation is "
                f"well-resolved at the arcminute class, so the fine CUDEM 1/9\" "
                f"nearshore composite is SKIPPED domain-wide (it cannot survive "
                f"resampling onto the coarse grid). The coastal AOI still nests its "
                f"own fine SHORE topo for the run-up. Overridable."
            ),
        ))
        logger.info(
            "geoclaw_inundation scenario_fault=%r M%.1f -> %d subfaults "
            "(source_lonlat=%s footprint=%s)",
            scenario_fault, scenario_magnitude, _scn_model.n_subfaults,
            effective_source_lonlat, finite_fault_footprint,
        )

    if _scenario_l in ("dam_break", "dambreak", "dam-break"):
        _has_loc = source_lonlat is not None
        _has_height = dam_break_depth_m is not None
        if not (_has_loc and _has_height):
            from trid3nt_server.workflows.geoclaw.nid_dams import resolve_nid_dam

            dam = await asyncio.to_thread(
                resolve_nid_dam, tuple(coerced), dam_name=dam_name
            )
            if dam is not None:
                if not _has_loc:
                    effective_source_lonlat = (dam.lon, dam.lat)
                if not _has_height:
                    effective_dam_depth = dam.height_m
                dam_source_note = dam.note()
                if _has_loc or _has_height:
                    dam_source_note += (
                        " (user-supplied "
                        + " + ".join(
                            n for n, ok in (("location", _has_loc), ("height", _has_height)) if ok
                        )
                        + " kept)."
                    )
                provenance.append(SyntheticInput(
                    param="dam_break_depth_m",
                    value=round(float(effective_dam_depth), 2),
                    units="m",
                    basis="user" if _has_height else "fetched",
                    real_source_if_any=None if _has_height else "fetch_usace_dams (USACE NID DAM_HEIGHT)",
                    note=f"NID dam {dam.name!r}",
                ))
                provenance.append(SyntheticInput(
                    param="source_lonlat",
                    value=f"({effective_source_lonlat[0]:.5f}, {effective_source_lonlat[1]:.5f})",
                    basis="user" if _has_loc else "fetched",
                    real_source_if_any=None if _has_loc else "fetch_usace_dams (USACE NID location)",
                ))
            else:
                named = f" named {dam_name!r}" if dam_name else ""
                return {
                    "status": "error",
                    "error_code": "GEOCLAW_DAM_INPUT_REQUIRED",
                    "error_message": (
                        f"No USACE National Inventory of Dams (NID) dam{named} was "
                        f"found for this AOI, so the dam location + height are not "
                        f"fabricated. To run this dam-break, supply BOTH "
                        f"source_lonlat=(lon, lat) of the dam AND dam_break_depth_m "
                        f"(released-column height, m) - or pass a dam_name that "
                        f"exists in NID within the AOI."
                    ),
                }
        else:
            dam_source_note = "Dam location + released-column height are user-supplied (not NID-sourced)."
            provenance.append(SyntheticInput(
                param="dam_break_depth_m", value=round(float(dam_break_depth_m), 2),
                units="m", basis="user",
            ))
            provenance.append(SyntheticInput(
                param="source_lonlat",
                value=f"({source_lonlat[0]:.5f}, {source_lonlat[1]:.5f})",
                basis="user",
            ))
    elif _scenario_l == "tsunami":
        # provenance-chain wave, item 2c: the tsunami synthetic-Okada honesty that
        # the worker only PRINTED to geoclaw.stdout (setrun_builder.py banner) now
        # rides the envelope as a structured entry -- the same fact, computed
        # deterministically at the server from the tool params. A fault-geometry
        # field the user did not supply is a generic synthetic default, NOT a
        # site-specific seismic source.
        _fault_defaulted = [
            n for n, supplied in (
                ("strike", fault_strike_deg is not None),
                ("dip", fault_dip_deg is not None),
                ("rake", fault_rake_deg is not None),
                ("depth", fault_depth_km is not None),
            ) if not supplied
        ]
        if _fault_defaulted and finite_fault_uri is None:
            provenance.append(SyntheticInput(
                param="fault_geometry",
                value="generic synthetic Okada",
                basis="default_demo", consequence="scenario",
                note=(
                    "fault " + "/".join(_fault_defaulted) + " not user-supplied; "
                    "illustrative, NOT a site-specific seismic source"
                ),
            ))
        # Mw: the contract default (8.0) is a demo magnitude, not a catalog event.
        provenance.append(SyntheticInput(
            param="source_magnitude",
            value=float(source_magnitude),
            units="Mw",
            basis="default_demo" if float(source_magnitude) == 8.0 else "user", consequence="scenario",
            note=None if float(source_magnitude) == 8.0 else "user-supplied Mw",
        ))
    if effective_dam_depth is None:
        # tsunami / surge ignore dam_break_depth_m; give the contract its default.
        effective_dam_depth = 10.0

    # --- law 9 (ADR 0296): resolve bottom-friction Manning's n, split by domain
    # character. dam_break / surge are LAND-DOMINATED / mixed-coastal (NLCD covers
    # the real land cover the run-up crosses, including NLCD's own "Open Water"
    # class over any coastal water inside the AOI) -> NLCD area-weighted
    # derivation (the storm_surge precedent, roughness_resolve.resolve_overland_
    # manning). tsunami is an OFFSHORE scenario (GEOCLAW_OFFSHORE_SCENARIOS): its
    # deep-ocean propagation domain has no NLCD coverage, and 0.025 is the
    # published Chow (1959) open-water friction standard -- the SAME value
    # manning_mapping.csv assigns NLCD class 11 "Open Water" -- so it is kept, now
    # loudly labeled (consequence="numerical", NOT "physics": a well-established
    # universal constant, not an invented site-specific value, so it never
    # triggers the auto-mode refuse) instead of riding silently.
    _manning_offshore = _scenario_l in GEOCLAW_OFFSHORE_SCENARIOS
    _manning_res = None
    if _manning_offshore:
        if manning_n is not None:
            _manning_n_for_gate = float(manning_n)
            provenance.append(SyntheticInput(
                param="manning_n", value=_manning_n_for_gate, units="s/m^(1/3)",
                basis="user", note="caller-supplied bottom-friction Manning's n.",
            ))
        else:
            _manning_n_for_gate = 0.025
            provenance.append(SyntheticInput(
                param="manning_n", value=_manning_n_for_gate, units="s/m^(1/3)",
                basis="default_demo", consequence="numerical",
                note=(
                    "offshore seabed friction: NLCD has no deep-ocean coverage; "
                    "the published Chow (1959) open-water standard (n=0.025, the "
                    "same value manning_mapping.csv assigns NLCD class 11 Open "
                    "Water) is used. Supply manning_n for a calibrated value."
                ),
            ))
    else:
        _manning_res = await resolve_overland_manning(
            coerced, manning_n, param_name="manning_n",
        )
        provenance.append(_manning_res.entry)
        _manning_n_for_gate = _manning_res.manning_n  # may be None (unresolved)

    # --- two-mode input gate: review-before-run -----------------------
    # Inputs are RESOLVED (NID dam or user values / tsunami source); in
    # user_gated mode present them for review/adjust BEFORE the consequential
    # solver dispatch, and stamp exactly the reviewed entries so what-was-approved
    # == what-ran. auto mode (the session default) proceeds with them labeled;
    # a headless direct-call (no live session) also proceeds (fail-open).
    _review = await gate_input_review(
        tool_name="geoclaw_inundation",
        mode=input_mode,
        entries=provenance,
        params={
            "dam_break_depth_m": float(effective_dam_depth),
            "source_magnitude": float(source_magnitude),
            "sim_duration_s": float(sim_duration_s),
            "amr_levels": int(amr_levels),
            "manning_n": _manning_n_for_gate,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"geoclaw_inundation {_review.cancel_reason}",
        }
    provenance = _review.entries
    effective_dam_depth = float(
        _review.params.get("dam_break_depth_m", effective_dam_depth)
    )
    source_magnitude = float(
        _review.params.get("source_magnitude", source_magnitude)
    )
    sim_duration_s = float(_review.params.get("sim_duration_s", sim_duration_s))
    amr_levels = int(_review.params.get("amr_levels", amr_levels))
    _mn_reviewed = _review.params.get("manning_n")
    effective_manning_n = float(_mn_reviewed) if _mn_reviewed is not None else None
    if effective_manning_n is None:
        # Unresolved land-dominated Manning's n (NLCD could not serve) survived
        # to here -- auto mode already refuses via the physics-consequence gate
        # above; this is the user_gated backstop (a "proceed" reply cannot make a
        # None friction coefficient runnable). Mirrors the storm_surge precedent.
        return {
            "status": "error",
            "error_code": "GEOCLAW_PHYSICS_INPUT_REQUIRED",
            "error_message": (
                str(_manning_res.entry.note) if _manning_res is not None
                else "geoclaw_inundation: manning_n could not be resolved."
            ),
        }

    try:
        kwargs: dict[str, Any] = dict(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            scenario=scenario,
            sim_duration_s=float(sim_duration_s),
            dam_break_depth_m=float(effective_dam_depth),
            source_magnitude=float(source_magnitude),
            output_frames=int(output_frames),
            amr_levels=int(amr_levels),
            manning_n=float(effective_manning_n),
            sea_level_m=float(sea_level_m),
        )
        if effective_source_lonlat is not None:
            sl = list(effective_source_lonlat)
            if len(sl) == 2:
                kwargs["source_lonlat"] = (float(sl[0]), float(sl[1]))
        if tsunami_dtopo_uri:
            kwargs["tsunami_dtopo_uri"] = str(tsunami_dtopo_uri)
        # Finite-fault measured-inversion source: staged CSV + rupture
        # footprint. Ignored when a prescribed dtopo was given (a staged dtopo wins).
        if finite_fault_uri and not tsunami_dtopo_uri:
            kwargs["finite_fault_uri"] = str(finite_fault_uri)
            if finite_fault_footprint is not None:
                kwargs["finite_fault_footprint"] = tuple(
                    float(v) for v in finite_fault_footprint
                )
        if surge_forcing_uri:
            kwargs["surge_forcing_uri"] = str(surge_forcing_uri)
        # USER-GATED Okada fault overrides: thread ONLY the ones supplied so the
        # contract default (None) holds otherwise and the engine substitutes a
        # scenario default it surfaces (never silently fabricated).
        if fault_strike_deg is not None:
            kwargs["fault_strike_deg"] = float(fault_strike_deg)
        if fault_dip_deg is not None:
            kwargs["fault_dip_deg"] = float(fault_dip_deg)
        if fault_rake_deg is not None:
            kwargs["fault_rake_deg"] = float(fault_rake_deg)
        if fault_depth_km is not None:
            kwargs["fault_depth_km"] = float(fault_depth_km)
        if extra_topo_uris:
            kwargs["extra_topo_uris"] = [str(u) for u in extra_topo_uris if u]
        if coastal_gauge_lonlat is not None:
            cg = list(coastal_gauge_lonlat)
            if len(cg) == 2:
                kwargs["coastal_gauge_lonlat"] = (float(cg[0]), float(cg[1]))
        if fgmax_arrival_tol_m is not None:
            kwargs["fgmax_arrival_tol_m"] = float(fgmax_arrival_tol_m)
        if lagrangian_particles:
            kwargs["lagrangian_particles"] = [
                (float(p[0]), float(p[1])) for p in lagrangian_particles
            ]
        if str(fgmax_mask).strip().lower() != "full":
            kwargs["fgmax_mask"] = str(fgmax_mask).strip().lower()
        if int(fgout_frames or 0) > 0:
            kwargs["fgout_frames"] = int(fgout_frames)
        run_args = GeoClawRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw run arguments: {exc}",
        }

    logger.info(
        "geoclaw_inundation bbox=%s scenario=%s duration=%.0fs frames=%d "
        "amr_levels=%d",
        run_args.bbox,
        run_args.scenario,
        run_args.sim_duration_s,
        run_args.output_frames,
        run_args.amr_levels,
    )

    try:
        peak = await model_geoclaw_inundation(
            run_args,
            compute_class=compute_class,
            dam_source_note=dam_source_note,
            synthetic_inputs=provenance,
            emit_particle_tracks=bool(run_args.lagrangian_particles),
            bathy_target_resolution_m=bathy_target_resolution_m,
        )
        logger.info(
            "geoclaw_inundation complete layer_id=%s scenario=%s "
            "max_depth_m=%.4g flooded_area_km2=%.6g max_inundation_m=%.4g uri=%s",
            peak.layer_id,
            peak.scenario,
            peak.max_depth_m,
            peak.flooded_area_km2,
            peak.max_inundation_m,
            peak.uri,
        )
        return peak
    except asyncio.CancelledError:
        raise
    except (
        GeoClawWorkflowError,
        PostprocessGeoClawError,
        GeoClawComposerError,
        LadderRefused,
        LadderGap,
    ) as exc:
        # A genuine coverage gap is already wrapped into GeoClawComposerError
        # (GEOCLAW_NO_BATHYMETRY) upstream in _fetch_topo_for_geoclaw and lands
        # here unchanged. LadderRefused/LadderGap reaching this handler directly
        # are the OTHER truth the ladder raises with: a transport/cache/upstream
        # fault under a rung (FALLBACK_LADDER_ERROR) that is NOT a coverage
        # verdict. Thread the exception's own error_code -- never the catch-all
        # code below -- and say the retryability out loud in the message, since
        # this envelope has no dedicated retryable field (mirrors flood.py's
        # ladder_detail pattern).
        error_code = getattr(exc, "error_code", None) or "GEOCLAW_INTERNAL_ERROR"
        ladder_detail = (
            " This is a TRANSIENT fault under a fallback rung, not a bathymetry "
            "coverage verdict: RETRY the same request."
            if isinstance(exc, LadderRefused)
            and getattr(exc, "error_code", None) == LADDER_ERROR_CODE
            and getattr(exc, "retryable", False)
            else ""
        )
        logger.warning(
            "geoclaw_inundation failed: %s (%s)", error_code, exc
        )
        return {
            "status": "error",
            "error_code": error_code,
            "error_message": f"{exc}{ladder_detail}",
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_inundation unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }


#: GeoClaw solve ETA heuristic (s) per base-grid cell - a coarse perf hint for
#: the live progress heartbeat (Invariant 1: a hint, never a narrated number).
_GEOCLAW_SEC_PER_CELL: float = 0.05

#: Output resolution (m) for the FINE nested coastal topo fetched over JUST the AOI
#: (the P2 dense-inundation fix). Fine enough to be well under the ~20 m finest AMR
#: cell so the run-up samples a REAL coast (not a ~450 m ETOPO step), but coarse
#: enough that the nested COG stays light (the worker decimates a too-fine topo
#: anyway). GeoClaw picks finest-in-overlap, so this fine AOI tile wins the coast.
_GEOCLAW_FINE_NEARSHORE_PIXEL_M: float = 10.0

#: Style preset for the surfaced topo/bathy INPUT layer -- the SAME continuous-DEM
#: Scenario families whose computational domain / AOI reaches the OPEN SEA, so the
#: published depth must be masked to OVERLAND cells (topo >= 0) to render coastal
#: inundation instead of the full water column that includes the ocean. tsunami =
#: the offshore Okada/dtopo source; surge = the coastal storm-surge forcing. An
#: inland ``dam_break`` (domain == AOI, no sea) is DELIBERATELY excluded so the mask
#: can never erase a legitimate inland flood (e.g. a below-MSL basin whose terrain
#: is negative relative to the vertical datum).
_GEOCLAW_OCEAN_MASK_SCENARIOS: frozenset[str] = GEOCLAW_OFFSHORE_SCENARIOS | frozenset(
    {"surge"}
)


# --------------------------------------------------------------------------- #
# The composer.
# A deterministic orchestrator-style chain (Invariant 2 - no LLM in the chain):
#   fetch topo/bathy DEM (fetch_topobathy seamless land+bathy -> fetch_dem
#     fallback) -> stage build_spec manifest + DEM reference to S3
#     -> run_solver('geoclaw') -> wait_for_completion (Batch-only, no
#        in-process lane) -> download the fort.q frames
#     -> postprocess_geoclaw (rasterize AMR frames -> peak primary COG +
#        per-frame COGs) -> publish the peak primary + emit the frames
#        out-of-band (the Phase-1 scrubber animation group).
# Determinism boundary (Invariant 1): every depth number the agent narrates
# comes from the typed postprocess fields - never free-generated.
# --------------------------------------------------------------------------- #
class GeoClawComposerError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "GEOCLAW_COMPOSER_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# DEM acquisition (topobathy seamless -> fetch_dem fallback).
# --------------------------------------------------------------------------- #
#: The bathymetry rungs a GeoClaw domain tolerates. Where CUDEM's 1/9" collection
#: stops mid-AOI the global ETOPO relief is a REAL below-waterline bed -- coarse,
#: on a different vertical datum, and loudly labeled. Declared for the NON-tsunami
#: path too: the tsunami path already forces the ETOPO base by param, but a
#: non-offshore coastal run would otherwise refuse at the coverage gate.
_GEOCLAW_BATHY_FALLBACK = ("etopo_bathy_base",)


def _fetch_topo_for_geoclaw(
    bbox: tuple[float, float, float, float],
    *,
    force_bathy_base: bool = False,
    target_resolution_m: float | None = None,
    activation_sink: list[Any] | None = None,
) -> tuple[str, str]:
    """Fetch a topo/bathy DEM for the AOI; return ``(s3_uri, source_label)``.

    ``source_label`` names WHICH data served (for the surfaced input-layer
    provenance): the seamless CUDEM/ETOPO topobathy on the primary path, or the
    3DEP land DEM on the fallback path.

    GeoClaw needs a SEAMLESS land+bathymetry DEM (the shallow-water bed): try
    ``fetch_topobathy`` first (the seamless coastal DEM, the right substrate for
    tsunami / surge run-up), fall back to ``fetch_dem`` (3DEP land-only) for an
    inland dam-break where bathymetry is irrelevant (the data-source fallback
    norm: primary -> fallback -> honest typed error).

    ``force_bathy_base`` (tsunami / offshore): pass through to ``fetch_topobathy``
    so the GLOBAL ETOPO 2022 topo-bathy is laid down as the ALWAYS-ON base over
    the FULL (offshore-extended) domain -- guaranteeing the open-ocean portion is
    genuinely-negative bathymetry rather than a flat land-DEM fill.

    ``target_resolution_m`` (follow-up): a DECLARED bathymetry cell floor
    for a basin-scale acquisition. When set it floors the composite (``min_pixel_m``)
    so a coarse domain fetches a light COG, and -- at/above ``_GEOCLAW_CUDEM_SKIP_RES_M``
    (the 0224 threshold) -- SKIPS the fine CUDEM 1/9" nearshore composite LOUDLY (its
    fine structure cannot survive resampling onto a coarse basin grid, and pulling the
    per-tile COGs is wasted cost). ``None`` keeps the native full-resolution fetch
    (byte-identical to the pre-follow-up default). The scenario front door supplies
    the basin-scale default; a fine coastal AOI still nests its own fine SHORE topo
    separately (``_fetch_fine_nearshore_for_geoclaw``), so the run-up stays resolved.

    ``activation_sink`` collects the fallback-ladder rows the fetch reported, so
    the caller can stamp what actually painted the bed onto its own result.

    Returns the DEM cache/runs ``s3://`` URI (staged BY REFERENCE - the worker
    downloads it directly). Raises ``GeoClawComposerError`` when both sources fail,
    whenever the bathymetry ladder refuses, and whenever the topobathy fetch faults
    with a RETRYABLE typed error: a nearshore coverage gap and a transient upstream
    fault must both stop rather than fall through to the LAND-ONLY 3DEP DEM, which
    would paint flat 0 m ocean over every wet cell.
    """
    # fetch_dem is spec-driven -- resolve the promoted closure (keyword-only).
    from trid3nt_server.data import TOOL_REGISTRY
    from trid3nt_server.data import TOOL_REGISTRY as _TR; fetch_topobathy = lambda bbox=None, **_kw: _TR["fetch_topobathy"].fn(bbox=bbox, **_kw)

    fetch_dem = TOOL_REGISTRY["fetch_dem"].fn

    topo_kw: dict[str, Any] = {"force_bathy_base": force_bathy_base}
    label = "topobathy (CUDEM 1/9\" + ETOPO 2022 seamless)"
    if target_resolution_m is not None:
        # min_pixel_m is the OUTPUT-grid FLOOR (no upper bound) -- it coarsens the
        # whole composite to the basin cell. resolution_m only sets the 3DEP LAND-leg
        # spacing and is capped at the fetch_topobathy spec max (1000 m); a coarse
        # basin run wants the land leg at its coarsest, so clamp rather than exceed it.
        topo_kw["min_pixel_m"] = float(target_resolution_m)
        topo_kw["resolution_m"] = int(min(float(target_resolution_m), 1000.0))
        if float(target_resolution_m) >= _GEOCLAW_CUDEM_SKIP_RES_M:
            topo_kw["skip_cudem"] = True
            logger.info(
                "fetch_topobathy: SCENARIO-scale bathy target %.0f m >= %.0f m -> "
                "CUDEM 1/9\" nearshore composite SKIPPED (basin-scale run, ETOPO 2022 "
                "deep-water column only); the fine nearshore is nested separately over "
                "the coastal AOI",
                target_resolution_m, _GEOCLAW_CUDEM_SKIP_RES_M,
            )
            label = (
                f"topobathy (ETOPO 2022 deep-water column, CUDEM skipped @ "
                f"~{target_resolution_m:.0f} m scenario scale)"
            )

    try:
        layer = fetch_topobathy(
            bbox, purpose="bathymetry",
            fallback=_GEOCLAW_BATHY_FALLBACK, **topo_kw,
        )
        uri = getattr(layer, "uri", None) or (
            layer.get("uri") if isinstance(layer, dict) else None
        )
        rows = getattr(layer, "fallbacks", None) or []
        if uri:
            if activation_sink is not None:
                activation_sink.extend(rows)
            note = render_fallback_line(rows)
            return str(uri), (f"{label} -- {note}" if note else label)
    except (LadderGap, LadderRefused) as exc:
        # Branch on the CODE, never the type: the ladder raises one exception type
        # for two different truths.
        if getattr(exc, "error_code", None) == LADDER_ERROR_CODE or getattr(
            exc, "retryable", False
        ):
            # Not a coverage verdict -- a transport / cache / upstream fault under
            # a rung. Propagating it keeps its retryability, so the turn can retry
            # instead of being told this coast has no bathymetry.
            raise
        # A real coverage gap no permitted rung filled. The 3DEP fallback below is
        # LAND-ONLY: it would paint flat 0 m ocean over every wet cell, which
        # GeoClaw runs as dry ground. Refuse honestly.
        raise GeoClawComposerError(
            "GEOCLAW_NO_BATHYMETRY",
            f"the topo-bathymetry ladder refused for bbox {bbox}: {exc}. The "
            "3DEP land DEM is NOT an acceptable substitute for a GeoClaw bed "
            "(flat 0 m ocean reads as dry ground), so this run stops here.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - fall through to fetch_dem
        # A fault that declares itself RETRYABLE is transport, not geography:
        # an upstream 5xx, a wedged tile read, a cache miss that faulted. Falling
        # through would hand a tsunami/surge run the LAND-ONLY 3DEP DEM (flat 0 m
        # ocean) because one HTTP call blipped. Propagate the fault's own typed
        # code and say the retryability out loud (this envelope has no retryable
        # field), exactly as the LadderRefused branch above does.
        code = getattr(exc, "error_code", None)
        if code and getattr(exc, "retryable", False):
            raise GeoClawComposerError(
                str(code),
                f"topo-bathymetry fetch faulted for bbox {bbox}: {exc}. This is a "
                "TRANSIENT upstream/transport fault, not a coverage verdict -- the "
                "LAND-ONLY 3DEP DEM is not a substitute for a coastal bed, so this "
                "run stops here. RETRY the same request.",
            ) from exc
        logger.warning(
            "fetch_topobathy failed (%s); falling back to the LAND-ONLY "
            "fetch_dem(10m) -- valid for an inland dam-break, never for a "
            "coastal/tsunami bed", exc,
        )

    try:
        layer = fetch_dem(bbox=bbox, resolution_m=10, purpose="bathymetry")
        uri = getattr(layer, "uri", None) or (
            layer.get("uri") if isinstance(layer, dict) else None
        )
        if not uri:
            raise GeoClawComposerError(
                "GEOCLAW_DEM_FETCH_FAILED",
                f"fetch_dem returned no uri for bbox {bbox}",
            )
        return str(uri), "3DEP 10 m DEM (land-only fallback)"
    except GeoClawComposerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GeoClawComposerError(
            "GEOCLAW_DEM_FETCH_FAILED",
            f"both DEM sources failed for bbox {bbox}: topobathy + fetch_dem-10m: {exc}",
        ) from exc


def _fetch_fine_nearshore_for_geoclaw(
    aoi_bbox: tuple[float, float, float, float],
) -> tuple[str | None, str | None]:
    """Fetch a FINE (~10 m) nearshore topo-bathy COG over JUST the AOI for use as a
    GeoClaw nested SHORE topo; return ``(uri, degrade_note)`` -- the ``s3://`` URI,
    or ``None`` with a note saying WHY when no fine source reached the AOI.

    The P2 dense-inundation fix. The PRIMARY topo (the coarse ETOPO base over the
    full offshore-extended domain) under-resolves the nearshore (~450 m), so a
    tsunami inundates only a handful of cells. This pulls the AOI-appropriate NCEI
    REGIONAL integrated topo-bathy DEM (~1 m; e.g. the CoNED Northern California
    collection that covers Crescent City, which CUDEM omits) OR CUDEM where it
    exists, capped to a light ~10 m COG, to stage as a fine NESTED topo over the
    AOI. GeoClaw layers it finest-last and picks finest-in-overlap, so the coast is
    sampled at ~10 m and the run-up resolves into a DENSE inundation sheet.

    The layer is an ENHANCEMENT, so its absence degrades the run rather than
    stopping it -- but never silently: every path that returns no URI returns the
    note that says why, and the caller carries it onto the answer layer. A run
    whose run-up resolved at ~450 m instead of ~10 m must be able to say so.
    """
    from trid3nt_server.data import TOOL_REGISTRY as _TR; fetch_topobathy = lambda bbox=None, **_kw: _TR["fetch_topobathy"].fn(bbox=bbox, **_kw)

    try:
        layer = fetch_topobathy(
            aoi_bbox,
            include_regional_fine=True,
            min_pixel_m=_GEOCLAW_FINE_NEARSHORE_PIXEL_M,
        )
    except Exception as exc:  # noqa: BLE001 - the nested fine layer is best-effort
        note = (
            "LABELED DEGRADE (fine nearshore topo): the ~10 m nested SHORE topo "
            f"fetch FAILED for AOI {aoi_bbox} ({type(exc).__name__}: {exc}). The run "
            "proceeds on the COARSE primary topo (global ETOPO ~450 m nearshore), so "
            "run-up is resolved over far fewer cells and inundation extent is a "
            "lower bound."
        )
        logger.warning("%s", note)
        return None, note
    cudem_n = int(getattr(layer, "cudem_tile_count", 0) or 0)
    regional_n = int(getattr(layer, "regional_tile_count", 0) or 0)
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None
    )
    if (cudem_n or regional_n) and uri:
        logger.info(
            "fine nearshore nested topo for AOI %s: %s (cudem_tiles=%d "
            "regional_tiles=%d, ~%g m)",
            aoi_bbox, uri, cudem_n, regional_n, _GEOCLAW_FINE_NEARSHORE_PIXEL_M,
        )
        return str(uri), None
    note = (
        "LABELED DEGRADE (fine nearshore topo): no genuinely-fine nearshore source "
        f"(NCEI regional or CUDEM) covers AOI {aoi_bbox} (cudem_tiles={cudem_n} "
        f"regional_tiles={regional_n}). The run proceeds on the COARSE primary topo "
        "(global ETOPO ~450 m nearshore); run-up is resolved over far fewer cells."
    )
    logger.warning("%s", note)
    return None, note


def _rasterize_topo_to_depth_grid(
    dem_uri: str,
    bbox: tuple[float, float, float, float],
    grid_shape: tuple[int, int],
) -> Any:
    """Warp the STAGED EPSG:4326 topo/bathy DEM onto the SAME (H, W) grid + AOI
    ``bbox`` as the depth raster so postprocess can split land (topo >= 0) from
    ocean (topo < 0) cell-for-cell.

    ``dem_uri`` is the primary topo/bathy DEM GeoClaw actually ran on (the
    ``resolve_offshore_source`` / reproject_dem_to_4326 output -- the seamless
    ETOPO-bathy base over the full offshore-extended domain, which covers the AOI).
    We read it with rasterio and reproject/resample it onto the depth grid's
    ``from_bounds`` transform (north-up, row 0 = north -- the SAME orientation
    ``rasterize_frame_to_grid`` builds), bilinear so the coastline (the topo=0
    contour) is smooth. Runs off the asyncio loop (blocking S3 read + rasterio) --
    the caller wraps it in ``asyncio.to_thread``.

    Returns the ``(H, W)`` float elevation grid, or ``None`` on ANY failure
    (unreachable DEM, rasterio error) so the run degrades to publishing the
    unmasked total-depth exactly as before this fix (never a hard failure -- the
    data-source fallback norm).
    """
    import os

    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling
    from rasterio.warp import reproject as _warp_reproject

    from trid3nt_server.workflows.geoclaw.run_geoclaw import _dem_uri_to_local

    src_local: str | None = None
    is_temp = False
    try:
        src_local, is_temp = _dem_uri_to_local(dem_uri)
        nrows, ncols = int(grid_shape[0]), int(grid_shape[1])
        min_lon, min_lat, max_lon, max_lat = bbox
        dst_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
        dst = np.full((nrows, ncols), np.nan, dtype="float64")
        with rasterio.open(src_local) as src:
            _warp_reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform,
                src_crs=src.crs or "EPSG:4326",
                dst_transform=dst_transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.bilinear,
            )
        return dst
    except Exception as exc:  # noqa: BLE001 -- best-effort; degrade to unmasked depth
        logger.warning(
            "model_geoclaw_inundation: could not rasterize staged topo %s "
            "onto the depth grid for the overland mask (%s); publishing UNMASKED "
            "total-depth",
            dem_uri,
            exc,
        )
        return None
    finally:
        if is_temp and src_local:
            try:
                os.unlink(src_local)
            except OSError:
                pass


def _record_geoclaw_batch_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    staging: Any,
    compute_class: str,
    session_id: str | None = None,
    case_id: str | None = None,
) -> dict | None:
    """Record ONE SOLVE row for the GeoClaw Batch lane (mirrors the SWMM/SFINCS
    telemetry sibling). Best-effort; returns the recorded row or ``None``."""
    from trid3nt_server.telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or staging.run_id,
        "solver": GEOCLAW_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(getattr(staging, "n_active_cells", 0) or 0),
        "scenario": staging.run_args.scenario,
    }
    row.update(meta)
    return record_solve_telemetry(row)


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_geoclaw_inundation(
    run_args: GeoClawRunArgs,
    *,
    dem_uri: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
    dam_source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
    emit_gauge_series: bool = False,
    emit_particle_tracks: bool = False,
    bathy_target_resolution_m: float | None = None,
) -> GeoClawDepthLayerURI:
    """Compose the full GeoClaw shallow-water inundation chain end-to-end (Batch).

    Args:
        run_args: the validated ``GeoClawRunArgs`` (bbox + scenario + forcing).
        dem_uri: optional topo/bathy DEM ``s3://`` URI. When ``None`` the composer
            fetches it (``fetch_topobathy`` -> ``fetch_dem`` fallback). Tests pass
            a synthetic URI to skip the fetch.
        run_id: optional ULID; minted by the staging step if absent.
        compute_class: compute class for the Batch sizing.
        cleanup_outputs: when True, the downloaded fort.q output dir is removed
            after postprocess (the COGs were already uploaded).
        dam_source_note: optional provenance string (dam-break: the resolved NID
            dam name + height, or a user-supplied note) stamped onto the returned
            layer's ``source_note`` so the agent narrates where the dam came from.
        bathy_target_resolution_m: optional DECLARED bathymetry cell floor (m) for a
            basin-scale acquisition. The Slab2 SCENARIO front door supplies
            the ~1 arcminute basin default so the full-margin domain topo fetch floors
            to a coarse ETOPO deep-water column and SKIPS the fine CUDEM nearshore
            composite (LOUDLY); ``None`` keeps the native full-resolution fetch.

    Returns:
        The PEAK ``GeoClawDepthLayerURI`` (role ``"primary"``, name ``"Peak flood
        depth"``) carrying the three narration scalars + the echoed scenario.
        Per-frame depth layers are emitted out-of-band via the emitter.

    Raises:
        GeoClawComposerError / GeoClawWorkflowError / PostprocessGeoClawError on a
        fatal stage failure (the tool wrapper catches these and returns a typed
        error dict so the agent narrates honestly).
    """
    bbox = tuple(run_args.bbox)
    emitter = current_emitter()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_geoclaw_inundation: zoom-to emit failed: %s", exc
            )

    # --- Sub-step plan: fetch DEM -> stage -> solve -> postprocess
    #     -> publish peak. begin_substeps stamps the parent breadcrumb cap; it is
    #     a no-op outside emit_tool_call (current_emitter() is None).
    begin_substeps(emitter, 5)

    # --- Offshore-domain planning (tsunami) --------------------------------
    # An Okada (seafloor) source can only generate a run-up if the computational
    # domain EXTENDS offshore to span the deep-water source -> the AOI coast. For
    # a tsunami we size that extended domain HERE and fetch the bathymetry over
    # IT (not just the AOI); dam_break / surge keep domain == AOI.
    domain_bbox = plan_geoclaw_domain(bbox, run_args.scenario, run_args.source_lonlat)
    # Finite-fault (measured inversion): the rupture patches have REAL geographic
    # coordinates spanning the whole fault, so the computational domain must ENCLOSE
    # the rupture footprint (else the Okada deformation falls outside the integrated
    # box -> no run-up). Union the AOI-planned domain with the footprint (padded),
    # clamped to valid lon/lat. The source is NOT relocated (unlike the single-point
    # path) -- resolve_offshore_source is skipped below.
    if run_args.finite_fault_uri and run_args.finite_fault_footprint is not None:
        fmin_lon, fmin_lat, fmax_lon, fmax_lat = (
            float(v) for v in run_args.finite_fault_footprint
        )
        _pad = 0.3
        domain_bbox = (
            max(min(domain_bbox[0], fmin_lon - _pad), -180.0),
            max(min(domain_bbox[1], fmin_lat - _pad), -90.0),
            min(max(domain_bbox[2], fmax_lon + _pad), 180.0),
            min(max(domain_bbox[3], fmax_lat + _pad), 90.0),
        )
        logger.info(
            "model_geoclaw_inundation: domain grown to enclose finite-fault "
            "footprint %s -> domain=%s", run_args.finite_fault_footprint, domain_bbox,
        )
    fetch_bbox = domain_bbox  # fetch topo/bathy over the FULL computational domain

    # --- Step 1: topo/bathy DEM (off-loop blocking I/O) ---------------------
    # For an OFFSHORE source (tsunami) force the ETOPO global topo-bathy as the
    # always-on base over the FULL offshore-extended domain so the open ocean is
    # genuinely-negative bathymetry (the flat-ocean root-cause fix), not a flat
    # land-DEM fill.
    _force_bathy_base = run_args.scenario in GEOCLAW_OFFSHORE_SCENARIOS
    bathy_activation: list[Any] = []
    #: Labeled degrades of the BED that are not ladder rungs (the enhancement-only
    #: fine nested shore topo). They ride the same note the ladder narration uses,
    #: so a degraded bed is never invisible on the answer layer.
    bed_notes: list[str] = []

    async def _stamp_bed_provenance(layer: Any, run_id: Any) -> Any:
        """Carry the bathymetry ladder + bed degrades onto the answer layer."""
        if bathy_activation:
            await asyncio.to_thread(
                persist_run_activations, run_id, bathy_activation,
                capability_note="topo-bathymetry bed for the GeoClaw domain",
            )
            layer = stamp_fallbacks(layer, bathy_activation)
        if bed_notes and hasattr(layer, "model_copy"):
            joined = " ".join(bed_notes)
            layer = layer.model_copy(update={
                "fallback_note": (
                    f"{layer.fallback_note} {joined}"
                    if getattr(layer, "fallback_note", None)
                    else joined
                )
            })
        return layer

    if dem_uri is None:
        async with substep(emitter, "fetch_topobathy"):
            resolved_dem_uri, bathy_source = await asyncio.to_thread(
                _fetch_topo_for_geoclaw,
                fetch_bbox,
                force_bathy_base=_force_bathy_base,
                target_resolution_m=bathy_target_resolution_m,
                activation_sink=bathy_activation,
            )
    else:
        resolved_dem_uri = dem_uri
        bathy_source = "user-supplied topo/bathy DEM"

    # --- CRS alignment: reproject the topo/bathy DEM to EPSG:4326 (lon/lat) ----
    # GeoClaw's tsunami solve runs in spherical lat/lon (coordinate_system=2) with
    # a lon/lat computational domain, but fetch_topobathy emits a PROJECTED-METRES
    # (UTM) COG -- a metres extent has ZERO overlap with the lon/lat domain, so
    # GeoClaw aborts ("topo arrays do not cover domain"). Reproject to 4326 BEFORE
    # source-placement (resolve_offshore_source samples the DEM as lon/lat) and
    # staging. Best-effort + idempotent (a 4326 DEM is returned unchanged).
    resolved_dem_uri = await asyncio.to_thread(
        reproject_dem_to_4326, resolved_dem_uri, run_id=run_id
    )

    logger.info(
        "model_geoclaw_inundation: DEM=%s domain=%s aoi=%s",
        resolved_dem_uri,
        domain_bbox,
        bbox,
    )

    # --- Bathymetry-aware Okada source placement (tsunami synthetic source) --
    # Honor a user/composer offshore source when it is over deep water, else
    # project onto the deepest seaward cell of the fetched bathymetry. Skipped
    # for a STAGED dtopo (the source is prescribed by that file), for a
    # FINITE-FAULT source (the patches are at their real inverted coordinates --
    # relocating a single point would corrupt the multi-patch geometry), and for
    # the non-offshore scenarios.
    source_override: tuple[float, float] | None = None
    if (
        run_args.scenario in GEOCLAW_OFFSHORE_SCENARIOS
        and run_args.tsunami_dtopo_uri is None
        and run_args.finite_fault_uri is None
    ):
        source_override = await asyncio.to_thread(
            resolve_offshore_source,
            resolved_dem_uri,
            domain_bbox,
            bbox,
            run_args.source_lonlat,
        )
        if source_override is None:
            logger.warning(
                "model_geoclaw_inundation: no below-waterline cell found in "
                "domain %s; keeping requested source %s (run may not inundate)",
                domain_bbox,
                run_args.source_lonlat,
            )
        else:
            logger.info(
                "model_geoclaw_inundation: Okada source placed offshore at "
                "%s (requested=%s)",
                source_override,
                run_args.source_lonlat,
            )
            # --- Domain/source coordination (issue #9) ---------------------
            # The initial domain was sized from the AOI alone (plan_geoclaw_domain
            # above) but the bathymetry reaches FURTHER offshore, so the resolved
            # deep-water source can land OUTSIDE that domain -> the Okada
            # deformation falls outside the integrated box -> zero inundation.
            # Re-size the domain to ENCLOSE the resolved source (clamped to the
            # fetched-DEM coverage), asserting source-in-domain (loud failure on a
            # future drift). Skipped when no source was resolved (keep the AOI
            # domain + the honest no-inundation warning above).
            domain_bbox = await asyncio.to_thread(
                finalize_geoclaw_domain,
                bbox,
                run_args.scenario,
                source_override,
                resolved_dem_uri,
            )
            logger.info(
                "model_geoclaw_inundation: domain re-sized to enclose "
                "source -> domain=%s source=%s aoi=%s",
                domain_bbox,
                source_override,
                bbox,
            )

    # Cost-bounded grid + AMR plan (the SOLVER_TIMEOUT fix): a COARSE base grid
    # over the full (offshore-extended) propagation domain + NESTED AMR refined
    # ONLY at the AOI to a tens-of-metres run-up resolution, with the finest mesh
    # bounded by a cell budget so a WET coastal solve finishes in minutes. The
    # planned amr_levels OVERRIDES run_args.amr_levels (a level-4 request over a
    # huge AOI is what TIMED OUT); est_finest_cells is the compute-class work proxy.
    (
        base_num_cells,
        planned_amr_levels,
        est_finest_cells,
        propagation_level,
        est_prop_domain_cells,
    ) = plan_geoclaw_grid(domain_bbox, bbox, run_args.amr_levels)
    # Explicit AMR windows GOVERN refinement: the plan's cost-bounded finest is the
    # whole-AOI ceiling, but when the user supplies windows the deck's finest FOLLOWS
    # the finest window (so the user's requested window level is honored and the AOI
    # ambient = finest-1 refines BELOW it -> a demonstrable in-window contrast). The
    # window may push ONE level beyond the plan's whole-AOI ceiling because a window
    # is a bounded sub-box (only its cells reach that finest level), floored at 2 so
    # an ambient (finest-1) always exists. Absent windows -> the plan governs
    # unchanged (every non-window run is identical).
    if run_args.amr_regions:
        _window_finest = max(int(w.max_level) for w in run_args.amr_regions)
        planned_amr_levels = max(2, min(_window_finest, planned_amr_levels + 1))
    logger.info(
        "model_geoclaw_inundation: grid plan base=%s amr_levels=%s "
        "(requested=%s) est_finest_aoi_cells=%d propagation_level=%s "
        "est_propagation_domain_cells=%d domain=%s aoi=%s windows=%d",
        base_num_cells,
        planned_amr_levels,
        run_args.amr_levels,
        est_finest_cells,
        propagation_level,
        est_prop_domain_cells,
        domain_bbox,
        bbox,
        len(run_args.amr_regions),
    )

    # Optional staged tsunami dtopo / surge forcing (already-staged URIs on args).
    dtopo_uri = run_args.tsunami_dtopo_uri
    surge_uri = run_args.surge_forcing_uri
    # Optional additional topo/bathy tiles (ordered coarse -> fine on the args).
    extra_dem_uris = list(run_args.extra_topo_uris or [])

    # --- P2 dense-inundation: a FINE (~10 m) nested SHORE topo over the AOI ------
    # The primary topo is the coarse ETOPO base over the full offshore-extended
    # domain (~450 m nearshore) -- so a tsunami inundates only a handful of cells.
    # Fetch the AOI-appropriate NCEI fine topo-bathy (regional ~1 m where CUDEM
    # omits the coast, e.g. CoNED Northern California over Crescent City; CUDEM
    # elsewhere) over JUST the AOI and append it as a fine NESTED topo (coarse ->
    # fine). GeoClaw picks finest-in-overlap, so the coast samples at ~10 m and the
    # finer AMR run-up mesh resolves a DENSE inundation sheet. Only for an OFFSHORE
    # (tsunami) AUTO-fetch run; skipped when no genuinely-fine source covers the AOI
    # (returns None -> run proceeds on the coarse primary, as before).
    if dem_uri is None and run_args.scenario in GEOCLAW_OFFSHORE_SCENARIOS:
        fine_uri, fine_note = await asyncio.to_thread(
            _fetch_fine_nearshore_for_geoclaw, bbox
        )
        if fine_note:
            bed_notes.append(fine_note)
        if fine_uri:
            # GeoClaw runs in lon/lat (coordinate_system=2): reproject the fine COG
            # to EPSG:4326 too (same as the primary) so it overlaps the domain.
            fine_uri = await asyncio.to_thread(
                reproject_dem_to_4326, fine_uri, run_id=run_id
            )
            extra_dem_uris.append(fine_uri)
            logger.info(
                "model_geoclaw_inundation: staged fine nested SHORE topo "
                "for AOI %s -> %s", bbox, fine_uri,
            )

    # --- Step 2: stage the build_spec manifest + DEM reference --------------
    # The USER-GATED fault_* + coastal_gauge_lonlat + fgmax_arrival_tol_m live on
    # run_args and ride into the build_spec inside stage_geoclaw_manifest ->
    # build_geoclaw_build_spec (only the supplied fault_* are threaded). The
    # offshore-extended domain + resolved source ride in via the new kwargs.
    async with substep(emitter, "stage_geoclaw_manifest"):
        staging = await asyncio.to_thread(
            stage_geoclaw_manifest,
            run_args,
            dem_uri=resolved_dem_uri,
            run_id=run_id,
            dtopo_uri=dtopo_uri,
            finite_fault_uri=run_args.finite_fault_uri,
            surge_uri=surge_uri,
            extra_dem_uris=extra_dem_uris,
            base_num_cells=base_num_cells,
            domain_bbox=domain_bbox,
            source_lonlat_override=source_override,
            amr_levels_override=planned_amr_levels,
        )

    # Size the Batch instance from the FINEST-level AOI cell count (the real work
    # proxy: the finest mesh is pinned over the AOI for the whole run), not the
    # coarse base-grid count, so a wet solve is not under-provisioned.
    try:
        staging.n_active_cells = max(
            int(getattr(staging, "n_active_cells", 0) or 0), int(est_finest_cells)
        )
    except Exception:  # noqa: BLE001 - never break the chain on a proxy update
        pass

    # ONE local compute environment: the solve runs on the host CPUs, so the
    # caller's compute_class flows through unchanged (no auto-scaling).
    import os as _os

    n_active = int(getattr(staging, "n_active_cells", 0) or 0)
    effective_compute_class = compute_class

    # --- Step 3: dispatch via the generic run_solver seam -------------------
    from trid3nt_server.data.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=GEOCLAW_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=effective_compute_class,
    )

    # --- Two-card sim observability (dispatch card + Batch-bound sim card) --
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=GEOCLAW_SOLVER_NAME,
        handle=handle,
        compute_class=effective_compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=staging.run_id,
            solver=GEOCLAW_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=n_active or None,
            vcpus=_os.cpu_count(),
            eta_seconds=(n_active * _GEOCLAW_SEC_PER_CELL) if n_active else None,
        )
    )
    run_result = None
    # surface the solve as a child "run_solver" row in the parent
    # timeline. The Sim card (mint_dispatch_and_sim_cards) STILL owns the live
    # Batch readout (hard invariant); this child is the timeline entry that goes
    # green/red/yellow with the solve. No-op outside emit_tool_call. The original
    # cancel/cleanup flow + telemetry + typed-error raise are PRESERVED verbatim:
    # a returned non-"complete" RunResult raises a SENTINEL inside the substep so
    # the child row reads red (honesty floor), which we swallow right after the
    # context exits so control falls through to the UNCHANGED telemetry + typed-
    # error block below (the parent's own state is owned there, not by the child).
    class _SolveReturnedFailed(RuntimeError):
        pass

    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle)
            except asyncio.CancelledError:
                # Invariant 8: propagate the cancel; route it to the SIM card.
                logger.info(
                    "model_geoclaw_inundation cancelled while awaiting solver"
                )
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
        # Child already marked red by the substep; fall through to the original
        # telemetry + typed-error path (which records + raises GeoClawWorkflowError).
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- SOLVE telemetry (Batch instance + size + timing) ------------------
    try:
        _record_geoclaw_batch_solve_telemetry(
            run_result=run_result,
            handle=handle,
            staging=staging,
            compute_class=effective_compute_class,
        )
    except Exception as exc:  # noqa: BLE001 - never break the solve
        logger.warning(
            "GeoClaw solve batch-compute telemetry failed (non-fatal): %s", exc
        )

    if run_result.status != "complete":
        raise GeoClawWorkflowError(
            "GEOCLAW_RUN_FAILED",
            message=(
                "GeoClaw Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}"
            ),
            details={
                "run_id": staging.run_id,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # Register-only fast path: if the worker wrote a publish_manifest alongside
    # completion.json, skip the fort.q download + agent-side postprocess. The
    # worker already produced COGs + band_stats + TiTiler URLs; we just register
    # them and return early. Falls through when the manifest is absent (pre-
    # manifest workers or unknown schema version).
    from trid3nt_server.workflows.shared.register_published_manifest import (
        read_publish_manifest,
        register_manifest_layers,
    )
    from trid3nt_server.emission.outputs_seam import (
        build_layers_from_outputs,
        read_outputs_manifest,
    )
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    # EMIT-ON-SOLVE SEAM (ADR 0281). When the rebuilt worker wrote an outputs.json
    # under the run prefix, the SEAM owns ALL publication (peak + animation frames)
    # -- proven byte-equivalent to the register path
    # (tests/test_geoclaw_outputs_seam.py). ``publish_manifest`` is STILL read, but
    # ONLY for the top-level narration metrics (the flat outputs.json entries carry
    # no aggregates -- publish_manifest is the metrics carrier, not a second
    # publication). Absent outputs.json -> the legacy register-only publish_manifest
    # path runs byte-unchanged (one-release safety); absent both -> the on-box
    # fort.q download path runs byte-unchanged. Clean seam-or-legacy fork.
    _gc_outputs = await asyncio.to_thread(read_outputs_manifest, run_result)
    _gc_manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if _gc_outputs is not None:
        async with substep(emitter, "postprocess_geoclaw"):
            _seam = build_layers_from_outputs(
                _gc_outputs, run_id=batch_run_id, bbox=tuple(bbox)
            )
        if not _seam.layers:
            raise GeoClawComposerError(
                "GEOCLAW_NO_LAYERS",
                "outputs.json seam produced no registered depth layers "
                "(honesty floor: cannot narrate an empty solve)",
            )
        _gc_m = dict(_gc_manifest.metrics) if _gc_manifest is not None else {}
        _gc_prim = next(l for l in _seam.layers if l.role == "primary")
        _gc_frame_seam = [l for l in _seam.layers if l.role != "primary"]
        peak = GeoClawDepthLayerURI(
            uri=_gc_prim.uri,
            layer_type=_gc_prim.layer_type,
            layer_id=_gc_prim.layer_id,
            name=_gc_prim.name,
            style_preset=_gc_prim.style_preset,
            bbox=tuple(bbox),
            role=_gc_prim.role,
            max_depth_m=float(_gc_m.get("max_depth_m", 0.0)),
            flooded_area_km2=float(_gc_m.get("flooded_area_km2", 0.0)),
            max_inundation_m=float(_gc_m.get("max_inundation_m", 0.0)),
            arrival_time_s=(
                float(_gc_m["arrival_time_s"])
                if _gc_m.get("arrival_time_s") is not None
                else None
            ),
            scenario=run_args.scenario,
            source_note=dam_source_note,
            synthetic_inputs=list(synthetic_inputs or []),
        )
        # Wrap the seam's context frames into GeoClawDepthLayerURI (context metrics
        # 0.0 -- the peak drives narration) so ``_emit_frame_layers``' publish
        # chokepoint re-wrap has the typed depth fields.
        _gc_frame_layers = [
            GeoClawDepthLayerURI(
                uri=l.uri,
                layer_type=l.layer_type,
                layer_id=l.layer_id,
                name=l.name,
                style_preset=l.style_preset,
                bbox=l.bbox or tuple(bbox),
                role=l.role,
                units=l.units,
                max_depth_m=0.0,
                flooded_area_km2=0.0,
                max_inundation_m=0.0,
                scenario=run_args.scenario,
            )
            for l in _gc_frame_seam
        ]
        emitted_frames = await _emit_frame_layers(
            emitter, _gc_frame_layers, batch_run_id
        )
        logger.info(
            "model_geoclaw_inundation (seam path) run_id=%s scenario=%s "
            "max_depth_m=%.4g flooded_area_km2=%.6g max_inundation_m=%.4g "
            "arrival_time_s=%s frames_emitted=%d/%d peak_uri=%s",
            batch_run_id, run_args.scenario, peak.max_depth_m,
            peak.flooded_area_km2, peak.max_inundation_m, peak.arrival_time_s,
            emitted_frames, len(_gc_frame_layers), peak.uri,
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as _ze:  # noqa: BLE001
                logger.warning(
                    "model_geoclaw_inundation: zoom-to (seam path) failed: %s", _ze
                )
        return await _stamp_bed_provenance(peak, batch_run_id)
    if _gc_manifest is not None:
        async with substep(emitter, "postprocess_geoclaw"):
            _gc_reg = register_manifest_layers(
                _gc_manifest, run_id=batch_run_id, bbox=tuple(bbox)
            )
        if not _gc_reg.layers:
            raise GeoClawComposerError(
                "GEOCLAW_NO_LAYERS",
                "worker publish_manifest produced no registered depth layers "
                "(honesty floor: cannot narrate an empty solve)",
            )
        _gc_m = _gc_reg.metrics
        _gc_prim = _gc_reg.layers[0]
        _gc_frame_layers = _gc_reg.layers[1:]
        peak = GeoClawDepthLayerURI(
            uri=_gc_prim.uri,
            layer_type=_gc_prim.layer_type,
            layer_id=_gc_prim.layer_id,
            name=_gc_prim.name,
            style_preset=_gc_prim.style_preset,
            bbox=tuple(bbox),
            role=_gc_prim.role,
            max_depth_m=float(_gc_m.get("max_depth_m", 0.0)),
            flooded_area_km2=float(_gc_m.get("flooded_area_km2", 0.0)),
            max_inundation_m=float(_gc_m.get("max_inundation_m", 0.0)),
            arrival_time_s=(
                float(_gc_m["arrival_time_s"])
                if _gc_m.get("arrival_time_s") is not None
                else None
            ),
            scenario=run_args.scenario,
            source_note=dam_source_note,
            synthetic_inputs=list(synthetic_inputs or []),
        )
        emitted_frames = await _emit_frame_layers(
            emitter,
            _gc_frame_layers,  # type: ignore[arg-type]
            batch_run_id,
        )
        logger.info(
            "model_geoclaw_inundation (manifest path) run_id=%s "
            "scenario=%s max_depth_m=%.4g flooded_area_km2=%.6g "
            "max_inundation_m=%.4g arrival_time_s=%s "
            "frames_emitted=%d/%d peak_uri=%s",
            batch_run_id,
            run_args.scenario,
            peak.max_depth_m,
            peak.flooded_area_km2,
            peak.max_inundation_m,
            peak.arrival_time_s,
            emitted_frames,
            len(_gc_frame_layers),
            peak.uri,
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as _ze:  # noqa: BLE001
                logger.warning(
                    "model_geoclaw_inundation: zoom-to (manifest path) "
                    "failed: %s",
                    _ze,
                )
        return await _stamp_bed_provenance(peak, batch_run_id)

    # --- Step 4: download the Batch fort.q outputs -------------------------
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    out_dir = await asyncio.to_thread(_download_batch_geoclaw_outputs, batch_run_id)

    # Adaptive output raster: size the depth COG from the AOI at the native
    # run-up ground resolution (~25 m, matching the finest CoNED/AMR nest) so the
    # inundation renders as a smooth, dense sheet (SFINCS parity) instead of the
    # legacy fixed 256x256 specks. Floored at 256, capped for huge AOIs.
    geoclaw_grid_shape = compute_geoclaw_grid_shape(bbox)
    logger.info(
        "model_geoclaw_inundation run_id=%s adaptive depth-raster grid "
        "H=%d W=%d (~%.0f m/px) over bbox=%s",
        staging.run_id,
        geoclaw_grid_shape[0],
        geoclaw_grid_shape[1],
        GEOCLAW_TARGET_GROUND_RES_M,
        tuple(bbox),
    )

    # --- Overland mask topo (offshore/coastal only) ------------------------
    # For tsunami / surge the AOI reaches the open sea, so the raw GeoClaw depth is
    # the FULL water column and an offshore AOI renders as ocean, not the coastal
    # flood. Rasterize the STAGED topo/bathy DEM (the one GeoClaw ran on) onto the
    # SAME adaptive depth grid so postprocess masks depth to OVERLAND cells (topo
    # >= 0). Inland dam_break is excluded (mask_ocean stays False) -- its depth is
    # published in full, unchanged. A None topo_grid (fetch failed) degrades to the
    # unmasked total-depth (same as before this fix).
    mask_ocean = run_args.scenario in _GEOCLAW_OCEAN_MASK_SCENARIOS
    topo_grid = None
    mesh_layer: LayerURI | None = None
    if mask_ocean:
        topo_grid = await asyncio.to_thread(
            _rasterize_topo_to_depth_grid,
            resolved_dem_uri,
            bbox,
            geoclaw_grid_shape,
        )
        logger.info(
            "model_geoclaw_inundation run_id=%s overland-mask topo %s for "
            "scenario=%s (grid H=%d W=%d)",
            staging.run_id,
            "rasterized" if topo_grid is not None else "UNAVAILABLE",
            run_args.scenario,
            geoclaw_grid_shape[0],
            geoclaw_grid_shape[1],
        )

    try:
        # --- Step 5: postprocess (rasterize fort.q -> peak + frames) -------
        async with substep(emitter, "postprocess_geoclaw"):
            layers, metrics = await asyncio.to_thread(
                postprocess_geoclaw,
                out_dir,
                bbox,
                run_id=staging.run_id,
                scenario=run_args.scenario,
                grid_shape=geoclaw_grid_shape,
                topo_grid=topo_grid,
                mask_ocean=mask_ocean,
                sea_level_m=run_args.sea_level_m,
                fgmax_arrival_tol_m=run_args.fgmax_arrival_tol_m,
            )

        # --- Coastal gauge time series (the gauge-timeseries template) -------
        # Parse BEFORE the out_dir cleanup below. Non-fatal: a missing/unparseable
        # gauge leaves the scalars empty (the peak layer still narrates).
        gauge_series: dict[str, Any] | None = None
        gauge_scalars: dict[str, float] = {}
        if emit_gauge_series:
            try:
                gauge_series, gauge_scalars = await asyncio.to_thread(
                    parse_geoclaw_gauge_series, out_dir
                )
            except Exception as exc:  # noqa: BLE001 - never sink the solve
                logger.warning(
                    "model_geoclaw_inundation: gauge parse failed (non-fatal): %s", exc
                )

        # --- AMR mesh preview (the RAW GRID as a first-class product) ---------
        # Parse the peak-relevant (final) fort.q frame's AMR patch structure into
        # a grid-line FeatureCollection and upload it as mesh.geojson. One shared
        # seam every GeoClaw inundation template rides. Best-effort: a None mesh
        # never voids the depth result. Built BEFORE the out_dir cleanup below.
        mesh_layer = await asyncio.to_thread(
            build_geoclaw_mesh_layer, out_dir, run_id=staging.run_id
        )

        # --- Okada seafloor-deformation PRODUCT --------------------
        # For a tsunami synthetic-Okada run, rasterize the final-time dZ the worker
        # wrote (deformation_dz.asc) into a SIGNED uplift/subsidence COG -- the
        # direct answer to "what seafloor deformation does this quake drive". Built
        # BEFORE the out_dir cleanup. Best-effort: None on dam_break / surge /
        # staged dtopo (never voids the depth answer).
        deformation_layer: LayerURI | None = None
        deformation_scalars: dict[str, float] = {}
        try:
            deformation_layer, deformation_scalars = await asyncio.to_thread(
                build_geoclaw_deformation_layer, out_dir, run_id=staging.run_id
            )
        except Exception as exc:  # noqa: BLE001 - never sink the solve
            logger.warning(
                "model_geoclaw_inundation: deformation product failed "
                "(non-fatal): %s", exc,
            )

        # --- Lagrangian particle tracks (the wake-tracking fold) --------------
        # When the run seeded Lagrangian particle gauges, parse their drift tracks
        # into a LineString product layer + narration scalars. Built BEFORE the
        # out_dir cleanup below. Best-effort: no tracks -> None (the plain path).
        particle_layer: LayerURI | None = None
        particle_tracks: list[dict[str, Any]] = []
        if emit_particle_tracks:
            try:
                particle_layer, particle_tracks = await asyncio.to_thread(
                    build_geoclaw_particle_track_layer,
                    out_dir,
                    run_id=staging.run_id,
                )
            except Exception as exc:  # noqa: BLE001 - never sink the solve
                logger.warning(
                    "model_geoclaw_inundation: particle-track parse failed "
                    "(non-fatal): %s",
                    exc,
                )
    finally:
        if cleanup_outputs:
            _cleanup_dir(out_dir)

    if not layers:
        raise GeoClawComposerError(
            "GEOCLAW_NO_LAYERS",
            "postprocess_geoclaw produced no depth layers (empty solve?)",
        )

    raw_peak = layers[0]
    frame_layers = layers[1:]

    # --- Step 6: publish the PEAK COG through publish_layer (render chokepoint)
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, staging.run_id)
    _peak_update: dict[str, Any] = {}
    if dam_source_note is not None:
        _peak_update["source_note"] = dam_source_note
    if synthetic_inputs:
        _peak_update["synthetic_inputs"] = list(synthetic_inputs)
    if gauge_scalars:
        _peak_update.update(gauge_scalars)
    if particle_tracks:
        _peak_update["particle_track_count"] = len(particle_tracks)
        _peak_update["particle_max_track_length_m"] = max(
            float(t["length_m"]) for t in particle_tracks
        )
        _peak_update["particle_track_duration_s"] = max(
            float(t["duration_s"]) for t in particle_tracks
        )
    if deformation_scalars:
        # Narrate the MODELED coseismic extremes as a determinism-boundary
        # provenance string on the peak layer (the deformation dipole itself is
        # the raster product below).
        _defo_note = (
            "modeled Okada coseismic seafloor deformation: max uplift "
            f"{deformation_scalars['max_uplift_m']:.3g} m, max subsidence "
            f"{deformation_scalars['max_subsidence_m']:.3g} m"
        )
        _existing_note = _peak_update.get("source_note")
        _peak_update["source_note"] = (
            f"{_existing_note}; {_defo_note}" if _existing_note else _defo_note
        )
    if _peak_update:
        peak = peak.model_copy(update=_peak_update)

    # --- Step 6b: publish + emit the per-frame animation layers OUT-OF-BAND --
    emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)

    # --- Step 6c: surface the AMR mesh preview (raw grid, role="context") -----
    # The mesh rides the reusable input/context seam (publish_input_layer): a
    # vector s3:// geojson passes the emission guardrail untouched and carries its
    # crs_authid onto the WS row. Never fatal (the depth answer stands regardless).
    if mesh_layer is not None:
        await publish_input_layer(emitter, mesh_layer, role="context")

    # --- Step 6d: surface the Okada seafloor-deformation raster -----
    # A SIGNED raster COG, so it rides the render chokepoint (publish_layer) to get
    # its diverging tile URL + data-driven legend before add_loaded_layer. Its bbox
    # is the OFFSHORE Okada source box (distinct from the AOI coast). Never fatal.
    if deformation_layer is not None:
        await _emit_deformation_layer(emitter, deformation_layer)

    # --- Lagrangian particle-track product layer + drift chart -----------------
    # The tracks ARE a product (the drifter / wake paths), so they ride the same
    # context/vector seam as the mesh; the drift chart goes to the charts window.
    if particle_layer is not None:
        await publish_input_layer(emitter, particle_layer, role="context")
    if particle_tracks:
        await _maybe_emit_particle_chart(emitter, particle_tracks, peak.uri)

    # --- Coastal gauge surface-elevation chart (the gauge-timeseries template) ---
    if emit_gauge_series and gauge_series is not None:
        await _maybe_emit_gauge_chart(emitter, gauge_series, peak.uri)

    logger.info(
        "model_geoclaw_inundation complete run_id=%s scenario=%s "
        "max_depth_m=%.4g flooded_area_km2=%.6g max_inundation_m=%.4g "
        "arrival_time_s=%s frames_emitted=%d/%d peak_uri=%s",
        staging.run_id,
        run_args.scenario,
        peak.max_depth_m,
        peak.flooded_area_km2,
        peak.max_inundation_m,
        peak.arrival_time_s,
        emitted_frames,
        len(frame_layers),
        peak.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_geoclaw_inundation: authoritative zoom-to failed: %s",
                exc,
            )

    return await _stamp_bed_provenance(peak, batch_run_id)


async def _emit_deformation_layer(emitter: Any, layer: LayerURI) -> None:
    """Publish + emit the Okada seafloor-deformation raster (render chokepoint).

    Routes the signed s3:// deformation COG through ``publish_layer`` (so it gets
    its diverging tile URL + data-driven legend), then ``add_loaded_layer``. A
    publish failure HONESTLY drops the layer (the depth answer stands). Never
    raises."""
    if emitter is None:
        return
    try:
        published_uri = await asyncio.to_thread(
            publish_layer,
            layer_uri=layer.uri,
            layer_id=layer.layer_id,
            style_preset=layer.style_preset or GEOCLAW_DEFORMATION_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_geoclaw_inundation: publish_layer FAILED for the deformation "
            "layer_id=%s error_code=%s (%s) - dropping it.",
            layer.layer_id, exc.error_code, exc,
        )
        return
    try:
        safe = emit_layer_uri(layer.model_copy(update={"uri": published_uri}))
        if safe is not None:
            await emitter.add_loaded_layer(safe)
    except Exception as exc:  # noqa: BLE001 - never break the solve
        logger.warning(
            "model_geoclaw_inundation: deformation add_loaded_layer failed: %s", exc,
        )


def _publish_peak_layer(
    raw_peak: GeoClawDepthLayerURI, run_id: str
) -> GeoClawDepthLayerURI:
    """Publish the PEAK depth COG through publish_layer (render chokepoint).

    Routes the raw s3:// peak COG through ``publish_layer`` and returns a NEW
    ``GeoClawDepthLayerURI`` carrying the published /tiles or WMS URL plus the
    narration scalars. On publish failure the raw peak is returned UNCHANGED: the
    dispatch-level ``emit_layer_uri`` guardrail then drops the dead raw-s3://
    raster from the map (honest) while the typed metrics still narrate.

    Mirrors the SWMM/SFINCS primary-publish path.
    """
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak
    layer_id_for_pub = f"geoclaw-depth-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_geoclaw_inundation: publish_layer FAILED for the peak "
            "layer_id=%s error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_peak
    return GeoClawDepthLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        max_depth_m=raw_peak.max_depth_m,
        flooded_area_km2=raw_peak.flooded_area_km2,
        max_inundation_m=raw_peak.max_inundation_m,
        arrival_time_s=raw_peak.arrival_time_s,
        scenario=raw_peak.scenario,
    )


async def _maybe_emit_gauge_chart(
    emitter: Any, gauge_series: dict[str, Any] | None, source_uri: str
) -> None:
    """Emit the coastal-gauge surface-elevation chart to the charts window."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_gauge_timeseries_chart_spec(gauge_series)
    if spec is None:
        return
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Coastal gauge surface elevation",
        caption=(
            "Water-surface elevation at the coastal gauge over time -- the tsunami "
            "waveform (leading depression + run-up peaks) and any co-seismic "
            "subsidence (the initial post-quake surface offset)."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("gauge time-series chart emit failed: %s", exc)


async def _maybe_emit_particle_chart(
    emitter: Any, tracks: list[dict[str, Any]] | None, source_uri: str
) -> None:
    """Emit the Lagrangian particle cumulative-drift chart to the charts window."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_particle_track_chart_spec(tracks)
    if spec is None:
        return
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Lagrangian particle drift",
        caption=(
            "Cumulative drift distance of each Lagrangian particle gauge over time "
            "-- the particles are advected by the depth-averaged velocity, tracing "
            "the flow (wake / drift path). The spatial paths are the map overlay."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("particle-track chart emit failed: %s", exc)


async def _emit_frame_layers(
    emitter: Any, frame_layers: list[GeoClawDepthLayerURI], run_id: str
) -> int:
    """Publish + emit per-frame depth COGs out-of-band so the web scrubber forms.

    Each frame COG is routed through ``publish_layer`` (render chokepoint) so it
    carries a renderable URL before ``add_loaded_layer``. The "Flood depth step N"
    name token is preserved so the web ``detectSequentialGroups`` groups them. A
    frame that fails to publish is HONESTLY DROPPED. Returns the number emitted
    (0 when no emitter is bound). Never raises (mirrors SWMM).
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "model_geoclaw_inundation: %d animation frames available "
                "but no emitter bound - frames not emitted.",
                len(frame_layers),
            )
        return 0
    emitted = 0
    for lyr in frame_layers:
        if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
            emit_layer: LayerURI = lyr
        else:
            try:
                frame_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_geoclaw_inundation: publish_layer FAILED for "
                    "frame layer_id=%s error_code=%s (%s) - dropping this frame.",
                    lyr.layer_id,
                    exc.error_code,
                    exc,
                )
                continue
            emit_layer = GeoClawDepthLayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
                max_depth_m=lyr.max_depth_m,
                flooded_area_km2=lyr.flooded_area_km2,
                max_inundation_m=lyr.max_inundation_m,
                scenario=lyr.scenario,
            )
        try:
            safe = emit_layer_uri(emit_layer)
            if safe is not None:
                await emitter.add_loaded_layer(safe)
                emitted += 1
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "model_geoclaw_inundation: frame add_loaded_layer failed "
                "for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "model_geoclaw_inundation: emitted %d/%d animation frames as a "
            "sequential group (run_id=%s)",
            emitted,
            len(frame_layers),
            run_id,
        )
    return emitted


def _cleanup_dir(d: str | Path) -> None:
    """Best-effort removal of a downloaded scratch dir."""
    try:
        shutil.rmtree(Path(d), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _is_geoclaw_output_key(base: str) -> bool:
    """A GeoClaw output object the composer downloads: the ``fort.*`` AMR frames
    (rasterized to depth), the ``fgout*`` SMOOTH fixed-grid animation frames (the
    uniform-grid series the postprocess promotes to the scrubber animation when
    ``fgout_frames > 0``), the coastal ``gaugeNNNNN.txt`` time series (the
    gauge-timeseries template reads it; the plain inundation path ignores it), AND
    the tsunami Okada ``deformation_dz.asc`` (the final-time seafloor dZ the
    postprocess rasterizes into the coseismic-deformation PRODUCT)."""
    return (
        base.startswith("fort.")
        or base.startswith("fgout")
        or (base.startswith("gauge") and base.endswith(".txt"))
        or base == "deformation_dz.asc"
    )


def _download_batch_geoclaw_outputs(run_id: str) -> str:
    """Download the Batch fort.q + gauge outputs to a tmp ``_output/`` dir.

    The GeoClaw Batch worker uploads its fort.q frames under
    ``s3://<runs_bucket>/<run_id>/_output/`` and records their URIs in
    completion.json ``output_uris``. We re-read completion.json (small, already on
    S3) to find the fort.* keys, download them via the SAME boto3 client the
    solver dispatch uses (no new client), and return the local dir holding an
    ``_output/`` subtree the postprocess discovers.

    Raises:
        GeoClawWorkflowError("GEOCLAW_BATCH_OUTPUT_MISSING"): the completed run
            produced no downloadable fort.q (a 'complete' solve with no output is
            a real failure - never a silent dead-end).
    """
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    keys: list[str] = []
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        for raw in manifest.get("output_uris") or []:
            uri = str(raw)
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
            except Exception:  # noqa: BLE001
                continue
            base = key.rsplit("/", 1)[-1]
            if _is_geoclaw_output_key(base):
                keys.append(key)
    if not keys:
        # Defensive fallback: list the runs prefix for fort.* / gauge*.txt objects.
        try:
            resp = s3.list_objects_v2(
                Bucket=runs_bucket, Prefix=f"{run_id}/_output/"
            )
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key", "")
                if _is_geoclaw_output_key(k.rsplit("/", 1)[-1]):
                    keys.append(k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GeoClaw output list fallback failed: %s", exc)

    tmp_dir = tempfile.mkdtemp(prefix=f"geoclaw-batch-out-{run_id}-")
    out_sub = Path(tmp_dir) / "_output"
    out_sub.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        dest = out_sub / base
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GeoClaw Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )

    has_frame = any(
        p.name.startswith("fort.q") for p in out_sub.iterdir() if p.is_file()
    )
    if not has_frame:
        _cleanup_dir(tmp_dir)
        raise GeoClawWorkflowError(
            "GEOCLAW_BATCH_OUTPUT_MISSING",
            message=(
                f"GeoClaw Batch run {run_id} completed but produced no downloadable "
                f"fort.q frames under s3://{runs_bucket}/{run_id}/_output/ "
                f"(downloaded {downloaded} fort.* objects)"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    return tmp_dir
