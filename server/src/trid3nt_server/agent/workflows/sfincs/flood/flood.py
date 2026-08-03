"""model_flood_scenario workflow (M5 capstone).

This module implements the **M5 capstone composition**:

    geocode_location (if location_query)
      → fetch_dem
      → fetch_landcover (with NLCD vintage_year sidecar per §4)
      → fetch_river_geometry
      → lookup_precip_return_period
      → build_sfincs_model ← §4 NLCD validation gate fires here
      → run_solver(sfincs, model_setup_uri)
      → wait_for_completion(handle)
      → postprocess_flood
      → AssessmentEnvelope (Flood subtype, Appendix B.4)

Per Decision G + FR-TA-1, this workflow is **deterministic Python composition**
-- there is no LLM in the chain. The workflow returns a typed
``AssessmentEnvelope`` whose ``flood: FloodPayload`` subtype carries the
narration metrics.

LLM exposure (workflow-as-atomic-tool-wrapper pattern):

    @register_tool(AtomicToolMetadata(name="sfincs_flood",
                                       ttl_class="live-no-cache",
                                       source_class="workflow_dispatch",
                                       cacheable=False,
                                       engine="sfincs",
                                       tier="template"))
    def sfincs_flood(bbox?, location_query?, ...) -> dict: ...

The wrapper forwards verbatim to ``model_flood_scenario`` and returns the
envelope's ``model_dump(mode="json")`` (a dict -- the LLM tool surface doesn't
need the pydantic instance). The wrapper carries the FR-DC-6 ``cacheable=False``
flag because workflows are uncacheable (the whole point is the dispatch +
solver run + envelope build, never the cached return).

Partial-failure envelope shape (TENTATIVE per kickoff Open Questions):
    On any internal failure (fetcher exception, NLCD validation gate firing,
    SFINCS dispatch error, solver SOLVER_FAILED, postprocess error), the
    workflow still returns a typed ``AssessmentEnvelope`` -- but with
    ``envelope_type="modeled"``, an empty layers list, and a
    ``FloodPayload`` carrying zero-valued metrics + the error code threaded
    into the ``solver_version`` field (a documented seam). The agent surface narrates the
    envelope honestly ("scenario could not be modeled because …") rather than
    fabricating depth values.

Cross-cutting principles in force:
- **Invariant 1 (Determinism boundary): preserves.** No LLM in the chain.
- **Invariant 2 (Deterministic workflows): preserves.** Straight-line
  composition; each step's failure surfaces as a typed exception caught at
  the workflow boundary.
- **Invariant 7 (no silent wrong answers): EXTENDS -- the headline.** The
  ``build_sfincs_model`` NLCD validation gate is the load-bearing mitigation.
  ``LULC_MAPPING_MISMATCH`` is surfaced as a failed envelope, not a
  dispatched-broken-model SFINCS run.
- **Invariant 8 (Cancellation is first-class): preserves.** The workflow
  awaits ``wait_for_completion`` -- any ``asyncio.CancelledError`` propagates
  through the workflow as-is, triggering the 850ms cancel chain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.envelope import (
    AssessmentEnvelope,
    CriticalFacility,
    DataSource,
    FloodMetrics,
    FloodPayload,
    ForcingSummary,
    Provenance,
    ResultLayer,
)
from trid3nt_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.emission.layer_uri_emit import emit_layer_uri, publish_input_layer
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.sfincs._template_card import TemplateCard
from trid3nt_server.agent.tools.fetchers.climate.lookup_precip_return_period.lookup_precip_return_period import lookup_precip_return_period
from trid3nt_server.agent.tools.fetchers.socioeconomic.geocode_location.geocode_location import geocode_location


def fetch_dem(**kwargs):
    """Registry-closure indirection for the folded ``fetch_dem`` (ADR 0097).

    ``fetch_dem`` is now a spec-driven router tool (no coded module); this
    module-level shim resolves it through ``TOOL_REGISTRY`` at call time and
    preserves the ``flood.fetch_dem`` module-attribute patch seam the flood tests
    monkeypatch. Keyword-only -- the promoted router closure takes ``**kwargs``.
    """
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["fetch_dem"].fn(**kwargs)


def fetch_landcover(*args: Any, **kwargs: Any):
    """fetch_landcover is spec-driven (ADR 0082): the twin was deleted, so this thin
    module-level indirection resolves the promoted router closure off the registry by
    name. Kept as a patchable module symbol for the flood-scenario consumer tests. It
    returns a LandcoverResult (a LayerURI subclass carrying the nlcd_vintage_year
    sidecar) -- the call site tolerates that object OR the twin's legacy dict shape."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["fetch_landcover"].fn(*args, **kwargs)
from trid3nt_server.agent.tools.fetchers.ocean.fetch_topobathy.fetch_topobathy import TopobathyError, fetch_topobathy
from trid3nt_server.agent.tools.publish_layer.publish_layer import PublishLayerError, publish_layer
from trid3nt_server.agent.tools.simulation.solver.solver import (
    run_solver,
    select_compute_class,
    wait_for_completion,
)
from trid3nt_server.agent.workflows.sfincs.postprocess_sfincs import (
    FLOOD_DEPTH_STYLE_PRESET,
    PostprocessError,
    postprocess_flood,
)
from trid3nt_server.agent.workflows.shared.register_published_manifest import (
    read_publish_manifest,
    register_manifest_layers,
)
from trid3nt_server.agent.workflows.sfincs.sfincs_builder import (
    BuildOptions,
    DischargeForcing,
    ForcingSpec,
    # infiltration-loss member (scenario coverage).
    InfiltrationForcing,
    PressureForcing,
    SFINCSSetupError,
    # SPIDERWEB (2026-07-19): parametric-hurricane wind+pressure member.
    SpiderwebForcing,
    WaterlevelForcing,
    WindForcing,
    # Heavy-compute offload: the NLCD validation gate stays a light PRE-SUBMIT
    # check on the fetched landcover (so LULC_MAPPING_MISMATCH still surfaces as
    # the SAME failed envelope), and the bbox-only autoscale sizes the Batch job
    # + telemetry (the worker re-does the DEM-active autoscale for real).
    _extract_unique_nlcd_classes,
    _to_vsigs,
    build_sfincs_model,
    suggest_sfincs_resolution_from_bbox,
    validate_nlcd_vintage_against_mapping,
)
from trid3nt_server.agent.workflows.shared.physics_registry import PhysicsRegistryError, validate_and_resolve_physics
from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import SFINCSForcingAdapterError

__all__ = [
    "model_flood_scenario",
    "sfincs_flood",
    "WorkflowError",
    "PrecipForcingError",
    "compute_precip_area_mean_mm_per_hr",
    "_resolve_surge_forcing_from_fetchers",
]

logger = logging.getLogger("trid3nt_server.agent.workflows.sfincs.flood.flood")


# Default project/session identifiers for ULID-bearing envelope fields. The
# agent runtime threads real IDs through when WS state is present; the
# workflow itself accepts None and falls back to fresh ULIDs so a direct call
# (smoke harness, integration test) still produces a valid envelope.
_FALLBACK_PROJECT_ID = None
_FALLBACK_SESSION_ID = None


# --- Pre-solver phase timeouts (terminal-pipeline-card hardening) -----------
# The fetcher chain (Steps 1-4) + ``build_sfincs_model`` (Step 5) run BEFORE
# ``wait_for_completion``. Each phase is wrapped in ``asyncio.wait_for`` (the
# sync calls go through ``asyncio.to_thread`` so the timeout is enforceable)
# and bounded by a GENEROUS budget -- large enough that a healthy fetch/build
# never trips it, but finite so a true hang surfaces as a typed ``*_TIMEOUT``
# failed envelope instead of an infinite await. Overridable via env for ops
# tuning.
_FETCHER_PHASE_TIMEOUT_S = float(
    os.environ.get("TRID3NT_FLOOD_FETCHER_TIMEOUT_S", "900")  # 15 min
)
_BUILD_PHASE_TIMEOUT_S = float(
    os.environ.get("TRID3NT_FLOOD_BUILD_TIMEOUT_S", "900")  # 15 min
)


# --- Engine-support seams (engine-door conformance split) -------------------
# The solve/progress/telemetry/envelope layer moved to ``run_sfincs.py`` and the
# forcing synthesis/autowire library to ``sfincs_forcing_autowire.py``. They are
# re-imported here so ``model_flood_scenario`` (below) calls them as before and
# ``flood.flood``'s public surface is unchanged.
from trid3nt_server.agent.workflows.sfincs.run_sfincs import (  # noqa: E402,F401
    WorkflowError,
    _COASTAL_OUTPUT_INTERVAL_MIN_DEFAULT,
    _LIVE_SOLVE_PROGRESS_INTERVAL_S,
    _OUTPUT_INTERVAL_MIN_FLOOR,
    _PRESOLVER_PROGRESS_TICK_S,
    _bbox_area_km2,
    _build_failed_envelope,
    _default_runs_prefix,
    _drive_live_solve_progress,
    _drive_presolver_phase_progress,
    _emit_flood_solve_telemetry,
    _emit_presolver_progress,
    _estimate_frame_count,
    _extract_solve_autoscale,
    _record_flood_batch_solve_telemetry,
    _resolve_bbox,
    _resolve_output_interval_min,
)
from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_autowire import (  # noqa: E402,F401
    PrecipForcingError,
    _SURGE_PEAK_M_AT_100YR,
    _SURGE_PEAK_M_CEIL,
    _SURGE_PEAK_M_FLOOR,
    _autowire_coastal_surge_forcing,
    _autowire_river_discharge_forcing,
    _build_surge_forcing_members,
    _os_path_join,
    _parametric_surge_peak_m,
    _resolve_building_obstacle_uri,
    _resolve_infiltration_uri,
    _resolve_spiderweb_forcing,
    _resolve_surge_forcing_from_fetchers,
    _staging_dir_local,
    _synthesize_breach_discharge_forcing,
    _synthesize_parametric_surge_forcing,
    _synthesize_tide_base_forcing,
    _synthesize_tsunami_waterlevel_forcing,
    _timedelta_s,
    compute_precip_area_mean_mm_per_hr,
)



# --------------------------------------------------------------------------- #
# The workflow itself
# --------------------------------------------------------------------------- #


async def model_flood_scenario(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    event_id: str | None = None,
    return_period_yr: int = 100,
    duration_hr: int = 24,
    compute_class: str = "medium",
    forcing_raster_uri: str | None = None,
    surge_forcing: dict[str, Any] | None = None,
    enable_subgrid: bool = False,
    building_obstacles: bool | str = False,
    building_obstacle_mode: str = "exclude",
    coastal: bool = False,
    quadtree: bool = False,
    output_interval_min: float | None = None,
    # SFINCS scenario-coverage intents (fluvial / compound /
    # wind / infiltration / levee-breach / tsunami). All default to today's
    # behaviour so a pluvial run is byte-identical (Invariant 7).
    river: bool = False,
    compound: bool = False,
    wind: dict[str, Any] | None = None,
    advanced_physics: dict[str, Any] | None = None,
    infiltration: bool | str = False,
    breach_point: tuple[float, float] | None = None,
    breach_peak_discharge_m3s: float | None = None,
    breach_arrival_hr: float | None = None,
    tsunami: bool = False,
    tsunami_wave_height_m: float | None = None,
    tsunami_period_min: float | None = None,
    # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure via a Delft3D
    # .spw. Any of these implies coastal + the spiderweb wind path; mutually
    # exclusive with the ``wind`` param (typed input error, never silent).
    storm_name: str | None = None,
    storm_season: int | None = None,
    storm_track_uri: str | None = None,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> AssessmentEnvelope:
    """Compose the full M5 flood-modeling chain.

    Resolves the location (geocode if ``bbox`` not given), fetches DEM (3DEP)
    + landcover (NLCD) + river geometry (NHDPlus HR) + design-storm
    precipitation depth (NOAA Atlas 14), builds an SFINCS model via HydroMT
    (the §4 NLCD validation gate fires here - raises
    ``SFINCSSetupError("LULC_MAPPING_MISMATCH")`` on vintage mismatch),
    dispatches ``run_solver(sfincs, ...)``, awaits ``wait_for_completion``,
    postprocesses the run's NetCDF to a flood-depth COG, and returns a
    typed ``AssessmentEnvelope`` Flood subtype (Appendix B.4).

    On internal failure (fetch error, NLCD gate firing, SFINCS dispatch
    failure, SOLVER_FAILED, postprocess error), returns a typed
    AssessmentEnvelope with zero-valued ``FloodMetrics`` and the error code
    threaded into ``solver_version`` -- never raises (caller-friendly).
    The agent surface narrates the failed envelope honestly.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
            ``None``, ``location_query`` is used to geocode.
        location_query: free-text place name (e.g. ``"Fort Myers, FL"``)
            geocoded via Nominatim. Ignored if ``bbox`` is supplied.
        event_id: optional event ID for provenance (HEP integration future
            hook; v0.1 carries it on the envelope's provenance dict).
        return_period_yr: design-storm ARI. Atlas 14 publishes
            ``{1, 2, 5, 10, 25, 50, 100, 200, 500, 1000}``. Default 100.
        duration_hr: design-storm duration in hours. Atlas 14 publishes a
            fixed row set; 24 hr is the v0.1 default.
        compute_class: FR-CE-3 compute class. Default ``"medium"``.
        forcing_raster_uri: optional ``gs://...`` (or local) URI of an
            OBSERVED accumulated-precip raster (v2, Case 3). When
            set, the workflow SKIPS the ``lookup_precip_return_period`` Atlas
            14 design-storm lookup and instead computes the AREA-MEAN
            accumulated precip over the model domain, converting it to a
            uniform SFINCS ``netamt`` rate (mm/hr) - the area-mean
            fallback (spw spatial upgrade path documented in
            ``sfincs_builder``). ``duration_hr`` is reused as the precip
            accumulation window for the depth→rate conversion. When ``None``
            (the default) the Atlas 14 design-storm path runs unchanged --
            behavior is **identical** to the v1 workflow (regression-critical).
        surge_forcing: optional nested dict wiring the COASTAL SFINCS surge /
            tide / discharge / wind / pressure boundary forcing into the deck --
            ``{"waterlevel": {...}, "discharge": {...}, "wind": {...},
            "pressure": {...}}``. Each sub-dict carries the forcing-file URIs
            (timeseries CSV + locations geofile, or a geodataset / grid netCDF)
            materialised from the forcing fetchers (``fetch_gtsm_tide_surge`` /
            ``fetch_noaa_coops_tides`` / ``fetch_noaa_nwm_streamflow`` /
            ERA5). See
            ``_build_surge_forcing_members``. ``None`` (default) → pure-pluvial
            deck (NO surge blocks emitted; byte-identical to the v0.1 deck).
        enable_subgrid: emit a SFINCS ``setup_subgrid`` block so the solve runs
            on a coarse grid while resolving sub-cell topography + roughness (the
            cheap urban-flood estimate). Auto-enabled when ``building_obstacles``
            is set. Default ``False``.
        building_obstacles: burn building footprints into the deck so the rough
            2D flood routes around buildings (the COASTAL "urban flood" ask).
            ``True`` → BEST-EFFORT OSM-footprint fetch (a fetch failure degrades
            to no obstacles, never aborts the flood); a ``str`` is used verbatim
            as the footprint geofile URI; ``False`` (default) → no obstacles.
        building_obstacle_mode: ``"exclude"`` (default) makes footprint cells
            INACTIVE no-flow holes; ``"raise"`` keeps them active but lifts their
            bed elevation via the subgrid (requires subgrid; auto-enabled).
        coastal: COASTAL-AOI flag (coastal SFINCS). When ``True`` -- OR
            implicitly when ``surge_forcing`` is supplied (a water-level / surge
            boundary is physically incoherent without a nearshore bed) -- the DEM
            fetch is routed through ``fetch_topobathy`` instead of ``fetch_dem``.
            ``fetch_topobathy`` produces ONE seamless topo-bathymetry surface
            (USGS 3DEP land + NOAA NCEI CUDEM bathymetry, CUDEM winning on the
            coast) in the SAME contract as ``fetch_dem`` (single-band float32
            NAVD88-metres COG, positive-up, EPSG:32616), so ``build_sfincs_model``
            / ``setup_dep`` consume it UNCHANGED -- the coastal DEM is a drop-in.
            ``False`` (default) AND no ``surge_forcing`` → the LAND/pluvial path
            is byte-identical to the v0.1 workflow (``fetch_dem``,
            regression-critical). If ``fetch_topobathy`` cannot find CUDEM
            bathymetry for the AOI it degrades INTERNALLY to a 3DEP-land-only
            surface (honest ``fallback_warning``, never a silent dead-end); a
            hard topobathy failure (no CUDEM AND no 3DEP, bad bbox) surfaces as a
            typed failed envelope. The land DEM behaviour for a non-coastal run
            is never touched.
            COASTAL AUTO-WIRE (job: coastal surge-with-waves): when ``is_coastal``
            (this flag OR ``surge_forcing`` OR ``quadtree``) AND no explicit
            ``surge_forcing`` was supplied, the workflow auto-wires a time-varying
            SEA surge water-level boundary via ``_autowire_coastal_surge_forcing``
            (CO-OPS tides -> GTSM -> a parametric design-storm surge scaling with
            ``return_period_yr``). This makes a coastal run show water coming IN
            from the sea, instead of the old silent rainfall-only degrade. ALL
            gated on ``is_coastal``  -  the
            inland/pluvial path is byte-identical (no boundary, quadtree unchanged).
        quadtree: coastal SFINCS -- build the deck with a multi-level
            REFINED QUADTREE grid + SnapWave wave coupling (incident + infragravity
            waves) instead of a regular grid. This authoring requires cht_sfincs
            (GPL-3.0), so it runs in a DEDICATED GPL-isolated Batch worker the
            agent only SUBMITS: the workflow composes a build_spec from the
            already-fetched topobathy + forcing, submits the deck-build Batch job
            (``build_sfincs_quadtree_deck``), and feeds the resulting deck
            manifest URI into the SAME ``run_solver('sfincs', ...)`` solve -- the
            solve half is unchanged. INERT until NATE provisions + flips
            ``TRID3NT_SOLVER_BACKEND=aws-batch`` + the deck-builder job-def
            (``TRID3NT_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER``); when unset the
            quadtree request surfaces as a typed ``DECK_BUILD_FAILED`` failed
            envelope (honest degrade, never silent). Implies ``coastal=True``.
            ``False`` (default) → the regular-grid build_sfincs_model path,
            byte-identical to today. The agent NEVER imports cht_sfincs.
        output_interval_min: animation map-output cadence in MINUTES (the SFINCS
            ``dtout``/``dtmaxout`` stride). Drives how often the solve writes a
            depth snapshot, hence how fast the animation reads. Resolved BY SIM
            TYPE via ``_resolve_output_interval_min``: a COASTAL / quadtree / wave
            run defaults to a FINE ~5-min stride (so the animation shows water
            rolling in  -  waves move in seconds-to-minutes; an hourly surge
            snapshot looks like a slowly-filling bathtub), while the PLUVIAL path
            ALWAYS resolves to ``None`` -> the legacy HOURLY cadence
            (byte-identical, regression-critical). An explicit value overrides the
            coastal default (floored at 1 min; the deck re-floors at 60 s). Frame
            count is bounded by ``MAX_FLOOD_FRAMES`` in postprocess so a fine
            cadence over the full window can't balloon the payload. ``None``
            (default) lets the sim-type default apply.
        river: FLUVIAL run -- auto-wire a river-discharge boundary (NOAA NWM ->
            USGS NWIS -> honest skip). Does NOT imply coastal (stays on
            ``fetch_dem``). ``False`` (default) -> no discharge boundary.
        compound: COMPOUND flood -- auto-wire waterlevel AND discharge AND precip
            together (implies ``coastal`` + ``river``). ``False`` (default).
        wind: optional uniform/gridded WIND forcing -- ``{"magnitude": <m/s>,
            "direction": <deg-from>}`` OR ``{"grid_uri": <nc>}`` (user/ERA5
            supplied, never fabricated). When set, defaults ``advanced_physics`` to
            ``{"advection": 1}`` (the registry exposes ``coriolis_latitude`` +
            ``wind_drag`` for the user to lift). ``None`` (default) -> no wind.
        advanced_physics: optional SFINCS physics overrides validated via
            ``physics_registry`` (keys subset of ``{advection, theta, alpha,
            huthresh, coriolis_latitude, wind_drag}``) and threaded onto
            ``BuildOptions.advanced_physics`` -> the deck ``setup_config`` block.
            ``None`` (default) -> deck physics byte-identical to today.
        infiltration: SOIL-INFILTRATION loss (GCN250 curve numbers). ``True`` ->
            auto-fetch GCN250; a ``str`` -> verbatim CN raster URI; ``False``
            (default) -> no infiltration loss. Best-effort (a fetch failure
            degrades to no loss, never aborts).
        breach_point: ``(lon, lat)`` of a DRAWN levee-breach point. USER-GATED:
            if given WITHOUT ``breach_peak_discharge_m3s`` the run returns a typed
            ``USER_INPUT_REQUIRED`` failed envelope (the composer NEVER fabricates
            a breach hydrograph). ``None`` (default) -> no breach.
        breach_peak_discharge_m3s: peak breach discharge (m^3/s, USER-supplied).
            Paired with ``breach_point`` to synthesize a triangular interior
            point-source jet. ``None`` (default).
        breach_arrival_hr: optional time-to-peak (hr) for the breach hydrograph;
            defaults to ~25% of the window. ``None`` (default).
        tsunami: TSUNAMI run (implies ``coastal``). USER-GATED: if ``True``
            WITHOUT ``tsunami_wave_height_m`` the run returns a typed
            ``USER_INPUT_REQUIRED`` failed envelope (the composer NEVER fabricates
            a wave height). ``False`` (default).
        tsunami_wave_height_m: peak tsunami wave amplitude (m, USER-supplied) ->
            a leading-depression N-wave waterlevel boundary. ``None`` (default).
        tsunami_period_min: tsunami characteristic period (min); defaults to ~15
            min (a SHAPE default, not a magnitude). ``None`` (default).
        project_id / session_id: ULID identifiers from the WS session. When
            ``None``, fresh ULIDs are minted (for direct-call / smoke).

    Returns:
        ``AssessmentEnvelope`` with ``envelope_type="modeled"``,
        ``hazard_type="flood"``, ``workflow_name="model_flood_scenario"``,
        and a populated ``flood: FloodPayload``. On success, ``layers``
        contains the flood-depth COG ``ResultLayer``; on failure the layer
        list is empty and ``FloodMetrics.solver_version`` carries the
        error code.
    """
    workflow_name = "model_flood_scenario"
    now = datetime.now(timezone.utc)
    proj_id = project_id or new_ulid()
    sess_id = session_id or new_ulid()
    data_sources: list[DataSource] = []
    solver_run_ids: list[str] = []
    grid_resolution_m = 30.0  # NFR-P-4 default; §4 immediate

    logger.info(
        "model_flood_scenario start bbox=%s location_query=%r event_id=%r "
        "return_period_yr=%s duration_hr=%s compute_class=%s "
        "forcing_raster_uri=%r",
        bbox,
        location_query,
        event_id,
        return_period_yr,
        duration_hr,
        compute_class,
        forcing_raster_uri,
    )

    # --- Coastal-AOI detection (coastal SFINCS) ---
    # Signal = explicit ``coastal`` flag OR ``surge_forcing`` present. A surge /
    # water-level boundary is physically incoherent on a land-only DEM (there is
    # no nearshore bed to route run-up over), so a surge request implies a
    # coastal AOI that needs the merged topo-bathymetry surface. This is a clean,
    # testable signal off the existing workflow inputs -- no geometry/coastline
    # lookup needed. When False, the DEM fetch stays on ``fetch_dem`` exactly as
    # the v0.1 land/pluvial path (regression-critical).
    # ``quadtree`` (the cht_sfincs quadtree+SnapWave deck-build) is a
    # coastal-only path -- a wave-coupled run needs the merged topo-bathymetry
    # surface -- so it implies coastal regardless of the explicit flag.
    # scenario-coverage couplings. A ``compound`` run is BOTH a
    # coastal-surge AND a fluvial-discharge driver (plus the always-present
    # precip), so it lifts both ``coastal`` and ``river``. A ``tsunami`` run needs
    # the seaward bed + msk==2 boundary, so it implies ``coastal`` too. These are
    # additive -- a pluvial run (all flags off) is byte-identical.
    # SPIDERWEB (2026-07-19): a storm (name+season, or a verbatim track URI)
    # implies coastal + the parametric hurricane wind path. The mutual-exclusion
    # with the ``wind`` param is validated as a typed USER_INPUT error below
    # (after bbox resolution, alongside the other input guards).
    storm_requested = bool(storm_name) or bool(storm_track_uri)
    coastal = bool(coastal) or bool(compound) or bool(tsunami) or storm_requested
    river = bool(river) or bool(compound)
    is_coastal = bool(coastal) or bool(surge_forcing) or bool(quadtree)
    logger.info(
        "model_flood_scenario coastal=%s (explicit=%s, surge_forcing=%s, "
        "quadtree=%s) -- DEM fetch routes through %s",
        is_coastal,
        bool(coastal),
        bool(surge_forcing),
        bool(quadtree),
        "fetch_topobathy" if is_coastal else "fetch_dem",
    )

    # --- Animation cadence by sim type ("looks like rain" fix) ---
    # Coastal/wave -> a FINE minute-scale map-output stride so the animation
    # shows water rolling in; pluvial -> None (legacy hourly, byte-identical).
    resolved_output_interval_min = _resolve_output_interval_min(
        is_coastal=is_coastal,
        output_interval_min=output_interval_min,
        duration_hr=float(duration_hr),
    )
    logger.info(
        "model_flood_scenario output cadence: is_coastal=%s requested=%s -> "
        "resolved_interval_min=%s (~%d frames over %s h; pluvial=hourly)",
        is_coastal,
        output_interval_min,
        resolved_output_interval_min,
        _estimate_frame_count(
            output_interval_min=resolved_output_interval_min,
            duration_hr=float(duration_hr),
        ),
        duration_hr,
    )

    # --- Step 0: bbox resolution (Decision K; bbox-direct wins precedence) ---
    # audit #5: ``_resolve_bbox`` calls ``geocode_location`` -> a SYNC
    # ``requests.get`` to Nominatim (up to ~15s) plus a sync S3 cache read.
    # Run it off the loop so it cannot stall the WS keepalive while geocoding.
    # ``_resolve_bbox`` is EMIT-FREE (no current_emitter()/emit_*/
    # add_loaded_layer): it geocodes + does dict work then returns, so it is
    # safe to move to a worker thread. The async frame still emits around it
    # (the zoom-on-area-first emit below runs back on the loop).
    try:
        resolved_bbox, geocode_result = await asyncio.to_thread(
            _resolve_bbox, bbox=bbox, location_query=location_query
        )
    except WorkflowError as exc:
        # No bbox to anchor a failed envelope on; this is the rare fatal case.
        # Bubble up so the agent surface emits a top-level error frame.
        raise
    if geocode_result is not None:
        data_sources.append(
            DataSource(
                name="OpenStreetMap Nominatim",
                uri=f"nominatim:{geocode_result.get('osm_type','')}/{geocode_result.get('osm_id','')}",
                accessed_at=datetime.now(timezone.utc),
            )
        )

    # ---: USER-INPUT honesty gates (never fabricate magnitudes) ---
    # feedback_never_fabricate_model_inputs_user_gate: the levee-breach peak +
    # the tsunami wave height are PHYSICAL magnitudes the user MUST supply. If a
    # breach/tsunami intent is detected WITHOUT its magnitude, return a typed
    # USER_INPUT_REQUIRED failed envelope (honest gate) rather than inventing a
    # hydrograph / wave height. The drawn breach POINT alone is not enough -- the
    # peak discharge governs the flood, so it must be explicit.
    if breach_point is not None and breach_peak_discharge_m3s is None:
        logger.info(
            "model_flood_scenario: breach_point given without "
            "breach_peak_discharge_m3s -- returning USER_INPUT_REQUIRED (no "
            "fabricated breach hydrograph)."
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="USER_INPUT_REQUIRED",
            error_detail=(
                "A levee-breach scenario needs the peak breach discharge "
                "(breach_peak_discharge_m3s, m^3/s) -- please supply it; the "
                "breach hydrograph is not fabricated."
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=None,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    if tsunami and tsunami_wave_height_m is None:
        logger.info(
            "model_flood_scenario: tsunami=True without tsunami_wave_height_m -- "
            "returning USER_INPUT_REQUIRED (no fabricated wave height)."
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="USER_INPUT_REQUIRED",
            error_detail=(
                "A tsunami scenario needs the peak wave height "
                "(tsunami_wave_height_m, m) -- please supply it; the wave form "
                "is not fabricated."
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=None,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    # SPIDERWEB (2026-07-19): storm (parametric hurricane) XOR the wind param.
    # Both would double-count the wind driver -> typed input error, never silent
    # precedence. Placed here (post-bbox) so the failed envelope carries a valid
    # resolved bbox for the pydantic validator.
    if storm_requested and wind:
        logger.info(
            "model_flood_scenario: storm_name/storm_track_uri given WITH wind "
            "param -- returning STORM_WIND_CONFLICT (no silent precedence)."
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="STORM_WIND_CONFLICT",
            error_detail=(
                "storm_name / storm_track_uri (parametric hurricane spiderweb) is "
                "mutually exclusive with the wind param (uniform/gridded wind) -- "
                "both would double-count the wind driver. Pass exactly one."
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=None,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # --- Zoom-on-area-first: emit ``map-command(zoom-to)`` BEFORE
    # any compute starts. As soon as we have a bbox, the map zooms -- the
    # user sees immediate response while the multi-minute SFINCS chain runs.
    # The emitter binding is set by ``PipelineEmitter.emit_tool_call`` via
    # the ``_CURRENT_EMITTER`` ContextVar; outside that scope (direct call,
    # smoke harness, unit test without an emitter) ``current_emitter()``
    # returns ``None`` and we skip silently -- emitting a transient verb is
    # a UX nice-to-have, not a correctness gate.
    emitter = current_emitter()
    if emitter is not None:
        try:
            await emitter.emit_map_command(
                "zoom-to",
                {"bbox": list(resolved_bbox)},
            )
            logger.info(
                "model_flood_scenario: zoom-on-area-first emitted bbox=%s",
                resolved_bbox,
            )
        except Exception as exc:  # noqa: BLE001 -- non-fatal UX hint
            logger.warning(
                "model_flood_scenario: zoom-on-area-first emit failed (non-fatal): %s",
                exc,
            )

    # --- Sub-step timeline plan ----------------------------------
    # Declare the planned internal-operation count so the parent workflow card's
    # live breadcrumb can show "k/total" while it runs. The fused fetcher phase
    # counts as ONE substep (it runs as a single off-loop ``_fetcher_chain`` under
    # one timeout budget -- see below), then build + solve + postprocess + publish.
    # The quadtree (coastal) path swaps the regular run_solver for the
    # combined deck-build+solve substep and adds a wave-postprocess substep. The
    # plan is best-effort + re-declarable; ``begin_substeps`` no-ops when no
    # emitter is bound (the verify/CI direct-call path) and degrades to label-only
    # if the real count diverges. Surfacing fewer is fine -- the breadcrumb just
    # shows the running index.
    _planned_substeps = (
        1  # fetcher phase (fetch_topobathy/fetch_dem + landcover + river + precip)
        + 1  # build_sfincs_model
        + 1  # solve (run_solver/wait_for_completion OR the combined quadtree run)
        + 1  # postprocess_flood
        + 1  # publish_layer (peak depth)
    )
    begin_substeps(emitter, _planned_substeps)

    # --- Step 1-4: atomic-tool fetcher chain ---
    forcing_summary: ForcingSummary | None = None
    # v2: ``precip_inches`` is the Atlas 14 design-storm depth (None
    # on the observed-raster path); ``precip_magnitude_mm_per_hr`` is the
    # pre-computed uniform netamt rate (None on the design-storm path).
    precip_inches: float | None = None
    precip_magnitude_mm_per_hr: float | None = None
    # Pre-solver progress (terminal-pipeline-card hardening): nudge the card so
    # it is never SILENT during the multi-second fetcher chain.
    await _emit_presolver_progress(emitter, 5)
    # The fetcher chain + ForcingSummary build is SYNCHRONOUS, blocking I/O
    # (HTTP fetches, GDAL VSI reads with no overall timeout). Run it off the
    # event loop in a worker thread and bound it with ``asyncio.wait_for`` so a
    # wedged endpoint surfaces as a typed PRESOLVER_TIMEOUT failed envelope
    # instead of an INFINITE silent await (NATE's "120 min, never finished").
    # The closure mutates ``data_sources`` / ``forcing_summary`` etc. via a
    # results container; single worker thread, sequential, no concurrent reader.
    _fetch_out: dict[str, Any] = {}

    def _fetcher_chain() -> None:
        nonlocal precip_inches, precip_magnitude_mm_per_hr, forcing_summary
        # --- DEM fetch: COASTAL branch (fetch_topobathy) vs LAND/pluvial branch
        # (fetch_dem). Both return a LayerURI with .uri pointing at a single-band
        # float32 NAVD88-metres COG (positive-up, bathymetry NEGATIVE on the
        # coastal path, NO sign flip) in the SAME contract, so the downstream
        # build_sfincs_model(dem_uri=...) seam is identical for both. The
        # non-coastal branch is byte-identical to the v0.1 workflow.
        if is_coastal:
            # ``fetch_topobathy`` REUSES fetch_dem internally for the 3DEP land
            # DEM and merges NOAA NCEI CUDEM bathymetry on top (CUDEM wins on the
            # coast). It DEGRADES internally to 3DEP-land-only with an honest
            # fallback_warning if CUDEM is missing for the AOI (never a silent
            # dead-end); a hard failure (no CUDEM AND no 3DEP, bad bbox, datum
            # mismatch) raises a TopobathyError carrying an error_code that the
            # outer handler threads into the failed envelope.
            topobathy_layer = fetch_topobathy(
                resolved_bbox, resolution_m=int(grid_resolution_m)
            )
            dem_layer = topobathy_layer
            _bathy_present = bool(getattr(topobathy_layer, "bathymetry_present", True))
            _tile_count = int(getattr(topobathy_layer, "cudem_tile_count", 0))
            _fallback_warning = getattr(topobathy_layer, "fallback_warning", None)
            data_sources.append(
                DataSource(
                    name=(
                        "NOAA NCEI CUDEM + USGS 3DEP (merged topo-bathymetry)"
                        if _bathy_present
                        else "USGS 3DEP (topobathy fallback: bathymetry ABSENT)"
                    ),
                    uri=dem_layer.uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
            if not _bathy_present:
                logger.warning(
                    "model_flood_scenario: coastal AOI but fetch_topobathy "
                    "degraded to 3DEP-land-only (cudem_tile_count=%s) -- %s",
                    _tile_count,
                    _fallback_warning
                    or "bathymetry absent; coastal inundation under-represented",
                )
        else:
            dem_layer = fetch_dem(bbox=resolved_bbox, resolution_m=int(grid_resolution_m))
            data_sources.append(
                DataSource(
                    name="USGS 3DEP",
                    uri=dem_layer.uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        # The promoted router closure is keyword-only (_promoted(**kwargs)); pass bbox
        # by keyword (the twin accepted it positionally).
        landcover_result = fetch_landcover(bbox=resolved_bbox, dataset="nlcd_2021")
        # LandcoverResult is a LayerURI subclass: the layer IS the result, and the
        # vintage-year sidecar is an attribute -- tolerate the twin's legacy dict too.
        landcover_layer: LayerURI = (
            landcover_result["layer"] if isinstance(landcover_result, dict) else landcover_result
        )
        _vintage = (
            landcover_result.get("nlcd_vintage_year") if isinstance(landcover_result, dict)
            else landcover_result.nlcd_vintage_year
        )
        nlcd_vintage_year = int(_vintage)
        data_sources.append(
            DataSource(
                name=f"NLCD {nlcd_vintage_year} (MRLC WMS)",
                uri=landcover_layer.uri,
                accessed_at=datetime.now(timezone.utc),
            )
        )
        # river geometry is BEST-EFFORT for the v0.1 pluvial deck.
        # ``build_sfincs_model`` does NOT emit ``setup_river_inflow`` for v0.1
        # pluvial - ``river_geometry_uri`` is accepted but unused, and
        # documented as ``may be None``. So a river-fetch failure must NOT kill an
        # otherwise-valid pluvial flood. Live Case 3 (2026-06-16): Victoria, TX
        # failed with "could not route bbox … to a HUC4 region" (the v0.1
        # HUC4 heuristic only covers a few demo areas), needlessly aborting a
        # flood that needs no river inflow. Degrade to None + narrate; re-enable
        # the hard dependency when v0.2 river-inflow (real ATCF surge) lands.
        river_layer: LayerURI | None
        try:
            # Registry seam (ADR 0074): fetch_river_geometry is now a spec-driven
            # router tool (OSM Overpass waterways), resolved by name rather than a
            # direct twin import (the twin was deleted with the NHDPlus HR leg).
            from trid3nt_server.agent.tools import TOOL_REGISTRY as _TR

            _river_fn = _TR["fetch_river_geometry"].fn
            river_layer = _river_fn(bbox=resolved_bbox, source="nhdplus_hr")
            data_sources.append(
                DataSource(
                    name="OSM Overpass waterways",
                    uri=river_layer.uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:  # noqa: BLE001 -- river is optional for pluvial
            logger.warning(
                "model_flood_scenario: fetch_river_geometry failed for bbox=%s "
                "(%s) -- proceeding WITHOUT river geometry (pluvial deck does not "
                "use river inflow; job-0055/job-0307).",
                resolved_bbox,
                exc,
            )
            river_layer = None
        if forcing_raster_uri is not None:
            # --- v2: OBSERVED-precip forcing branch (Case 3) ---
            # Compute the AREA-MEAN accumulated precip over the model domain
            # and convert to a uniform SFINCS netamt rate (mm/hr). ``duration_hr``
            # is reused as the accumulation window. The Atlas 14 design-storm
            # lookup is SKIPPED entirely on this path.
            precip_magnitude_mm_per_hr, area_mean_mm = (
                compute_precip_area_mean_mm_per_hr(
                    forcing_raster_uri=forcing_raster_uri,
                    bbox=resolved_bbox,
                    accumulation_hours=float(duration_hr),
                )
            )
            data_sources.append(
                DataSource(
                    name="Observed precipitation raster (area-mean netamt)",
                    uri=forcing_raster_uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
            # Envelope-side ``ForcingSummary.forcing_type`` is a contract-owned
            # Literal that does NOT (yet) include ``"pluvial_observed"`` -- the
            # observed precip raster IS a pluvial-precip forcing on the same
            # SFINCS netamt path, so we summarise it as ``"pluvial_synthetic"``
            # and carry the observed/area-mean distinction in the free-form
            # ``parameters`` dict (``forcing_mode="area_mean_netamt"`` +
            # ``forcing_raster_uri``) + the human-readable ``source``. The
            # ENGINE-internal ``ForcingSpec.forcing_type`` (below) is
            # ``"pluvial_observed"`` -- that drives the deck-builder branch and
            # is engine-owned. A future schema amendment could add a dedicated
            # ``"pluvial_observed"`` envelope literal.
            forcing_summary = ForcingSummary(
                forcing_type="pluvial_synthetic",
                source=(
                    f"Observed precip raster {forcing_raster_uri} -- "
                    f"area-mean {area_mean_mm:.2f} mm over {duration_hr}-hr "
                    "accumulation → uniform netamt (OQ-6 area-mean fallback)"
                ),
                parameters={
                    "forcing_raster_uri": forcing_raster_uri,
                    "area_mean_mm": area_mean_mm,
                    "precip_magnitude_mm_per_hr": precip_magnitude_mm_per_hr,
                    "accumulation_hours": float(duration_hr),
                    "forcing_mode": "area_mean_netamt",
                },
                inputs_uri=forcing_raster_uri,
            )
        else:
            # --- Atlas 14 design-storm path (v1 behavior, unchanged) ---
            mid_lon = 0.5 * (resolved_bbox[0] + resolved_bbox[2])
            mid_lat = 0.5 * (resolved_bbox[1] + resolved_bbox[3])
            precip_result = lookup_precip_return_period(
                location=(mid_lat, mid_lon),
                return_period_years=return_period_yr,
                duration_hours=float(duration_hr),
            )
            precip_inches = float(precip_result["precip_inches"])
            data_sources.append(
                DataSource(
                    name=precip_result.get("vintage_volume", "NOAA Atlas 14"),
                    uri="noaa-atlas14-pfds",
                    accessed_at=datetime.now(timezone.utc),
                )
            )
            forcing_summary = ForcingSummary(
                forcing_type="pluvial_synthetic",
                source=(
                    f"{precip_result.get('vintage_volume', 'NOAA Atlas 14')} -- "
                    f"{return_period_yr}-yr / {duration_hr}-hr design storm"
                ),
                parameters={
                    "precip_inches": precip_inches,
                    "duration_hours": float(duration_hr),
                    "return_period_years": return_period_yr,
                    "vintage_volume": precip_result.get("vintage_volume"),
                    "project_area": precip_result.get("project_area"),
                },
            )
        # Hand the downstream-needed locals back to the async frame.
        _fetch_out["dem_layer"] = dem_layer
        _fetch_out["landcover_layer"] = landcover_layer
        _fetch_out["nlcd_vintage_year"] = nlcd_vintage_year
        _fetch_out["river_layer"] = river_layer
        # ``_bathy_present`` is only assigned on the coastal branch; default True
        # for the land/pluvial path (no bathymetry concept there). The quadtree
        # deck-build (coastal) reads it to flag a wave-coupled run.
        _fetch_out["bathymetry_present"] = bool(locals().get("_bathy_present", True))

    try:
        # surface the fused data-fetch phase as ONE nested child row
        # under the parent workflow card. The chain runs ALL fetchers
        # (fetch_topobathy/fetch_dem + fetch_landcover + fetch_river_geometry +
        # lookup_precip_return_period/compute_precip_area_mean) inside a SINGLE
        # off-loop ``_fetcher_chain`` under ONE timeout budget (the hardened
        # terminal-card block), so it cannot be split into per-fetcher async
        # substeps without unwinding that budget - it is wrapped as one substep
        # labelled by the dominant DEM pull (the web humanizes it). ``substep`` is
        # a no-op when no emitter is bound (verify/CI direct-call path), so the
        # ``wait_for``/``to_thread`` body below is byte-identical there. A timeout
        # raises ``asyncio.TimeoutError`` INSIDE the substep -> the child reads red
        # (honesty floor) and the error re-raises to the existing except cascade,
        # which returns the PRESOLVER_TIMEOUT failed envelope unchanged.
        async with substep(
            emitter, "fetch_topobathy" if is_coastal else "fetch_dem"
        ):
            # NO-RECONNECT: the fetcher chain pulls DEM /
            # topobathy / landcover OFF the loop in a worker thread and is SILENT
            # on the wire for tens of seconds (a novel-AOI CUDEM/3DEP merge is the
            # long pole). Drive a periodic pipeline-state DATA frame so the browser
            # WS watchdog stays reset (no ~30 s force-reconnect) + the user sees the
            # fetch is alive. Cancelled the instant the chain returns/raises.
            _fetch_progress_task = asyncio.ensure_future(
                _drive_presolver_phase_progress(
                    emitter, start_pct=5, end_pct=24, expected_seconds=60.0
                )
            )
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_fetcher_chain),
                    timeout=_FETCHER_PHASE_TIMEOUT_S,
                )
            finally:
                _fetch_progress_task.cancel()
                try:
                    await _fetch_progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
    except asyncio.CancelledError:
        # Invariant 8: a true cancel propagates (mark_cancelled fires upstream).
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "model_flood_scenario: fetcher chain exceeded %.0fs budget for "
            "bbox=%s -- returning PRESOLVER_TIMEOUT failed envelope (a hang is "
            "now bounded + visible, not an infinite silent await).",
            _FETCHER_PHASE_TIMEOUT_S,
            resolved_bbox,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="PRESOLVER_TIMEOUT",
            error_detail=(
                f"data-fetch phase exceeded {_FETCHER_PHASE_TIMEOUT_S:.0f}s "
                "(a data endpoint or terrain/landcover read stalled)"
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except TopobathyError as exc:
        # COASTAL DEM hard failure (no CUDEM AND no 3DEP, bad bbox, datum
        # mismatch). The soft "CUDEM missing, 3DEP present" case does NOT reach
        # here -- fetch_topobathy degrades internally and returns a result. This
        # is the honest dead-end: thread the typed error_code into the failed
        # envelope (Invariant 7 -- never a fabricated topobathy success).
        logger.warning(
            "model_flood_scenario: fetch_topobathy hard-failed for coastal "
            "bbox=%s (%s / %s) -- returning failed envelope.",
            resolved_bbox,
            exc.error_code,
            exc,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=exc.error_code,
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("fetcher chain failed: %s", exc)
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=getattr(exc, "error_code", "FETCHER_FAILED"),
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    dem_layer = _fetch_out["dem_layer"]
    landcover_layer = _fetch_out["landcover_layer"]
    nlcd_vintage_year = _fetch_out["nlcd_vintage_year"]
    river_layer = _fetch_out["river_layer"]
    bathymetry_present = bool(_fetch_out.get("bathymetry_present", True))

    # ---: surface the SFINCS INPUT data as renderable layers --------
    # The engine consumes renderable inputs (DEM/topobathy, NLCD landcover,
    # NHDPlus rivers) but historically only the RESULT (flood-depth) was
    # published. Surface them now as role="input" so the user sees the terrain /
    # landcover / river network the model actually ran on. ALL best-effort
    # (publish_input_layer never raises): a failure to surface an input can NEVER
    # fail the solve. Gated on ``emitter is not None`` (no-op on the verify/CI
    # direct-call path).
    if emitter is not None:
        # Stable per-turn id base for the surfaced input layer_ids (the solver
        # run_id is not minted until AFTER the solve, below; an input is surfaced
        # PRE-solve so the user sees the terrain/landcover/rivers immediately).
        _input_id_base = new_ulid()
        # (a) RIVERS -- a VECTOR already carrying role="input"; no publish_layer
        # round-trip (the s3:// FlatGeobuf inlines server-side).
        if river_layer is not None:
            await publish_input_layer(emitter, river_layer)

        # (b) DEM + LANDCOVER -- RASTERs carrying a raw s3:// COG, which MapLibre
        #     cannot fetch; each needs a publish_layer round-trip to mint a
        #     renderable tile/WMS URL FIRST, then emit as role="input" with its
        #     existing preset (continuous_dem / categorical_landcover resolve in
        #     the TiTiler registry). publish_layer runs a sync worker-poll loop ->
        #     OFFLOADED off the loop. On AWS publish_layer fails until QGIS-on-AWS
        # lands; the input is then simply absent (honest no-surface,
        #     never fatal) -- exactly like the result-layer publish-or-drop gate.
        for _raster_in, _fallback_preset, _kind in (
            (dem_layer, "continuous_dem", "DEM"),
            (landcover_layer, "categorical_landcover", "landcover"),
        ):
            if _raster_in is None:
                continue
            try:
                _layer_id = f"input-{_kind.lower()}-{_input_id_base}"
                _wms_url = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=_raster_in.uri,
                    layer_id=_layer_id,
                    style_preset=_raster_in.style_preset or _fallback_preset,
                )
                _renderable = _raster_in.model_copy(
                    update={
                        "layer_id": _layer_id,
                        "uri": _wms_url,
                        "role": "input",
                        "bbox": None,
                        "style_preset": _raster_in.style_preset or _fallback_preset,
                    }
                )
                await publish_input_layer(emitter, _renderable)
            except PublishLayerError as exc:
                logger.warning(
                    "model_flood_scenario: %s input publish failed (non-fatal, "
                    "input absent until QGIS-on-AWS) error_code=%s: %s",
                    _kind,
                    getattr(exc, "error_code", "?"),
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
                logger.warning(
                    "model_flood_scenario: %s input surface failed (non-fatal): %s",
                    _kind,
                    exc,
                )

    await _emit_presolver_progress(emitter, 25)

    # --- Step 5: build_sfincs_model with NLCD validation gate ---
    try:
        # COASTAL SFINCS -- surge / tide / discharge / wind / pressure members.
        # ``surge_forcing`` is a nested dict of forcing URIs. Two shapes are
        # accepted: (a) PRE-MATERIALISED -- sub-dicts already carry
        # ``timeseries_uri`` / ``locations_uri`` / ``geodataset_uri`` (consumed
        # verbatim); (b) RAW FETCHER -- sub-dicts carry ``fetch_uri`` (a GTSM /
        # CO-OPS / NWM FlatGeobuf) or ``cama_cog_uri``, which the forcing ADAPTER
        # (sfincs_forcing_adapter) converts into the bzs/dis CSV + locations files
        # the deck-emission seam expects. The resolver materialises (b) in place;
        # an adapter failure for an EXPLICIT surge request raises (caught below as
        # a typed failed envelope -- never a silent pluvial degrade). Empty/absent
        # → pure-pluvial deck (no surge blocks emitted).
        # NO-SYNC-BLOCKING-ON-LOOP: the forcing adapter does heavy synchronous
        # geopandas/rasterio/pandas work (reads the GTSM/CO-OPS/NWM FlatGeobuf,
        # samples the CaMa COG, writes the bzs/dis CSV + locations files). Run it
        # off the event loop so the WS heartbeat + keepalive stay responsive on
        # the coastal surge path (otherwise the loop stalls -> the client sees
        # ~30s of silence -> force-reconnect -> the turn's socket dies 1005).
        #
        # COASTAL AUTO-WIRE: for a coastal AOI we AUTO-WIRE a time-varying
        # sea-surge water-level boundary (CO-OPS primary -> GTSM -> parametric
        # last-resort) so water rises from the sea and marches inland across
        # the frames. Gated strictly on ``is_coastal`` so the inland / pluvial
        # path is byte-identical (no surge boundary, branch never taken). The
        # fetcher fan-out does sync network I/O, so it runs off the loop
        # alongside the resolve.
        #
        # Auto-wire precedence ladder: each branch fills the SAME
        # ``surge_forcing`` dict so compound combinations compose; all run
        # BEFORE ``_resolve_surge_forcing_from_fetchers`` so any ``fetch_uri``
        # gets materialised. Order: tsunami -> coastal storm-surge (only if no
        # waterlevel yet) -> fluvial/breach discharge -> wind merge.
        surge_forcing = dict(surge_forcing) if surge_forcing else {}

        # 1) TSUNAMI waterlevel (pre-materialised N-wave) -- the magnitude gate
        #    already fired above, so a height is present here. Sets a sentinel so
        #    the coastal storm-surge synth below does NOT also fire (a tsunami is
        #    NOT a storm surge).
        if tsunami and tsunami_wave_height_m is not None and not surge_forcing.get(
            "waterlevel"
        ):
            _tsu = await asyncio.to_thread(
                _synthesize_tsunami_waterlevel_forcing,
                resolved_bbox,
                wave_height_m=float(tsunami_wave_height_m),
                period_min=tsunami_period_min,
                duration_hr=float(duration_hr),
            )
            surge_forcing["waterlevel"] = _tsu
            data_sources.append(
                DataSource(
                    name=(
                        "Tsunami N-wave water level (auto-wired; "
                        f"height {tsunami_wave_height_m} m)"
                    ),
                    uri="synthetic:tsunami-nwave",
                    accessed_at=datetime.now(timezone.utc),
                )
            )

        # 1.5) SPIDERWEB (2026-07-19): parametric hurricane wind+pressure. When a
        #      storm is requested, resolve the IBTrACS track + build the Holland
        #      .spw here and SUPPRESS the parametric surge synthesis below (the
        #      spw wind+pressure GENERATES the surge; a parametric bzs would
        #      double-count it). We still emit a FLAT low tide-base bzs so the
        #      deck keeps msk=2 boundary cells (setup_mask_bounds is gated on a
        #      waterlevel member) and the offshore boundary sits at tide level.
        #      Runs off the loop (fetch + geopandas + Holland build are sync).
        spiderweb_member: "SpiderwebForcing | None" = None
        spiderweb_prov: dict[str, Any] = {}
        spiderweb_utm_epsg: int | None = None
        if storm_requested:
            spiderweb_member, spiderweb_prov = await asyncio.to_thread(
                _resolve_spiderweb_forcing,
                resolved_bbox,
                duration_hr=float(duration_hr),
                storm_name=storm_name,
                storm_season=storm_season,
                storm_track_uri=storm_track_uri,
                data_sources=data_sources,
            )
            spiderweb_utm_epsg = spiderweb_prov.get("utm_epsg")
            # tide-base bzs (msk=2 cells) in place of the parametric surge.
            if not surge_forcing.get("waterlevel"):
                _tide = await asyncio.to_thread(
                    _synthesize_tide_base_forcing,
                    resolved_bbox,
                    duration_hr=float(duration_hr),
                )
                surge_forcing = {**surge_forcing, "waterlevel": _tide}
            data_sources.append(
                DataSource(
                    name=(
                        f"Hurricane {storm_name or 'storm'} parametric spiderweb "
                        f"(Holland; landfall {spiderweb_prov.get('landfall_iso','?')}, "
                        f"RMW {spiderweb_prov.get('rmw_source','?')})"
                    ),
                    uri=str(spiderweb_prov.get("track_uri") or "synthetic:spiderweb"),
                    accessed_at=datetime.now(timezone.utc),
                )
            )

        # 2) COASTAL storm-surge auto-wire -- only when no waterlevel is present
        #    yet (a tsunami / explicit surge / spiderweb tide-base already wins).
        #    SUPPRESSED for the spiderweb path (storm_requested) so the surge is
        #    generated by the spw wind+pressure, never double-counted.
        if is_coastal and not storm_requested and not surge_forcing.get("waterlevel"):
            _surge = await asyncio.to_thread(
                _autowire_coastal_surge_forcing,
                resolved_bbox,
                duration_hr=float(duration_hr),
                return_period_yr=return_period_yr,
                data_sources=data_sources,
            )
            if _surge:
                surge_forcing = {**surge_forcing, **_surge}

        # 3a) LEVEE-BREACH discharge (pre-materialised interior jet) -- distinct
        #     from a domain-edge river discharge; carried onto ForcingSpec.breach
        #     so a compound run can have BOTH. The magnitude gate already fired.
        breach_member = None
        if breach_point is not None and breach_peak_discharge_m3s is not None:
            _br = await asyncio.to_thread(
                _synthesize_breach_discharge_forcing,
                (float(breach_point[0]), float(breach_point[1])),
                peak_m3s=float(breach_peak_discharge_m3s),
                arrival_hr=breach_arrival_hr,
                duration_hr=float(duration_hr),
            )
            breach_member = DischargeForcing(
                timeseries_uri=_br.get("timeseries_uri"),
                locations_uri=_br.get("locations_uri"),
            )
            data_sources.append(
                DataSource(
                    name=(
                        "Levee-breach discharge (auto-wired; "
                        f"peak {breach_peak_discharge_m3s} m^3/s)"
                    ),
                    uri="synthetic:levee-breach",
                    accessed_at=datetime.now(timezone.utc),
                )
            )

        # 3b) FLUVIAL river discharge auto-wire (NWM -> NWIS -> honest skip).
        #     Gated on ``river`` (lifted by ``compound``); does NOT force
        #     is_coastal. Skipped when a discharge boundary is already present.
        if river and not surge_forcing.get("discharge"):
            _dq_wire = await asyncio.to_thread(
                _autowire_river_discharge_forcing,
                resolved_bbox,
                duration_hr=float(duration_hr),
                data_sources=data_sources,
                river_layer_uri=(river_layer.uri if river_layer is not None else None),
            )
            if _dq_wire:
                surge_forcing = {**surge_forcing, **_dq_wire}

        # 4) WIND merge (user/ERA5-supplied; never fabricated).
        if wind:
            surge_forcing = {**surge_forcing, "wind": dict(wind)}

        surge_forcing = await asyncio.to_thread(
            _resolve_surge_forcing_from_fetchers,
            surge_forcing or None,
            resolved_bbox,
            window_hours=float(duration_hr),
            data_sources=data_sources,
        )
        _wl, _dq, _wind, _press = _build_surge_forcing_members(surge_forcing)

        # INFILTRATION loss + ADVANCED-PHYSICS resolution.
        # Infiltration auto-fetches the GCN250 CN raster (best-effort) into an
        # InfiltrationForcing member. advanced_physics defaults to {"advection":1}
        # when WIND forcing is present (so a wind run flips the momentum scheme
        # rather than emitting wind with the deck default); validated via
        # physics_registry and threaded onto BuildOptions below.
        infiltration_member = None
        _inf_uri = await asyncio.to_thread(
            _resolve_infiltration_uri,
            infiltration,
            resolved_bbox,
            data_sources,
        )
        if _inf_uri:
            # Single-band GCN250 raster -> antecedent_moisture None (the deck
            # emits YAML null; the default 'avg' ValueErrors on a bare band).
            infiltration_member = InfiltrationForcing(
                cn_uri=_inf_uri,
                antecedent_moisture=None,
                provenance={"_prov_source": "gcn250"},
            )

        resolved_advanced_physics = advanced_physics
        if resolved_advanced_physics is None and (wind or spiderweb_member is not None):
            # SFINCS coriolis is on-by-default but
            # INERT while latitude==0.0 on a projected CRS, so a wind deck that omits
            # latitude silently runs WITHOUT Coriolis (parameters.html). Pin the
            # AOI-centre latitude alongside advection=1 so a wind run flips the
            # momentum scheme AND activates Coriolis. Never overrides an explicit
            # advanced_physics dict (that path leaves the user fully in control).
            _aoi_centre_lat = 0.5 * (float(resolved_bbox[1]) + float(resolved_bbox[3]))
            resolved_advanced_physics = {
                "advection": 1,
                "coriolis_latitude": _aoi_centre_lat,
            }
        if forcing_raster_uri is not None:
            # Observed-precip netamt path: carry the pre-computed magnitude.
            forcing_spec = ForcingSpec(
                forcing_type="pluvial_observed",
                duration_hours=float(duration_hr),
                precip_magnitude_mm_per_hr=precip_magnitude_mm_per_hr,
                waterlevel=_wl,
                discharge=_dq,
                # scenario-coverage members (breach jet +
                # infiltration loss). None on a pluvial run (byte-identical).
                breach=breach_member,
                wind=_wind,
                pressure=_press,
                # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure. None
                # on every non-storm run (byte-identical). XOR wind/pressure is
                # enforced in the emitter (storm_requested already suppressed the
                # wind param via STORM_WIND_CONFLICT).
                wind_spiderweb=spiderweb_member,
                infiltration=infiltration_member,
                provenance=dict(forcing_summary.parameters if forcing_summary else {}),
            )
        else:
            forcing_spec = ForcingSpec(
                forcing_type="pluvial_synthetic",
                precip_inches=precip_inches,
                duration_hours=float(duration_hr),
                return_period_years=return_period_yr,
                waterlevel=_wl,
                discharge=_dq,
                # scenario-coverage members (breach jet +
                # infiltration loss). None on a pluvial run (byte-identical).
                breach=breach_member,
                wind=_wind,
                pressure=_press,
                # SPIDERWEB (2026-07-19): None on every non-storm run (byte-identical).
                wind_spiderweb=spiderweb_member,
                infiltration=infiltration_member,
                provenance=dict(forcing_summary.parameters if forcing_summary else {}),
            )
        # COASTAL SFINCS -- building-obstacle URI. ``building_obstacles=True``
        # triggers a BEST-EFFORT OSM-footprint fetch (so a footprint-fetch
        # failure NEVER kills the flood - same degrade policy as river geometry);
        # a string is used verbatim as the obstacle geofile URI.
        # NO-SYNC-BLOCKING-ON-LOOP: a True building_obstacles triggers a
        # synchronous OSM Overpass footprint fetch (network I/O). Off-load it so
        # the loop keeps servicing the WS heartbeat.
        building_obstacle_uri = await asyncio.to_thread(
            _resolve_building_obstacle_uri,
            building_obstacles,
            resolved_bbox,
            data_sources,
        )
        # resolve the advanced-physics overrides ONCE (a single
        # resolve point) via the registry so an unknown key / out-of-range value
        # raises a typed error here (caught below as a failed envelope) rather
        # than emitting a silently-wrong deck. None -> {} (deck byte-identical).
        _resolved_physics = validate_and_resolve_physics(
            "sfincs", resolved_advanced_physics
        )
        # SPIDERWEB (2026-07-19): the spw eye coords are lon/lat and SFINCS
        # converts them to the GRID's UTM (utmzone). So the grid MUST be built in
        # the AOI UTM CRS (e.g. EPSG:32616 for Mexico Beach), overriding the
        # EPSG:3857 BuildOptions default. Proven byte-for-byte in the docker smoke
        # (utmzone=16n grid in EPSG:32616). None-guarded so a non-storm run keeps
        # the default crs.
        _spw_crs = (
            f"EPSG:{spiderweb_utm_epsg}"
            if (spiderweb_member is not None and spiderweb_utm_epsg)
            else None
        )
        options = BuildOptions(
            grid_resolution_m=grid_resolution_m,
            simulation_hours=float(duration_hr),
            # SPIDERWEB: UTM crs override (else BuildOptions default EPSG:3857).
            **({"crs": _spw_crs} if _spw_crs else {}),
            # feed the compute_class through so the adaptive-grid cap
            # is sized against the right instance vCPU (the cap derives from the
            # solve budget + vCPU via the perf model). build_sfincs_model snaps
            # grid_resolution_m UP if the estimated active-cell count overruns.
            compute_class=compute_class,
            # COASTAL SFINCS -- subgrid + building-obstacle mask (urban flood).
            # Subgrid is auto-enabled when buildings are present (the obstacle
            # "raise" mode needs it; "exclude" benefits from sub-cell topography).
            enable_subgrid=bool(enable_subgrid or building_obstacle_uri),
            building_obstacle_uri=building_obstacle_uri,
            building_obstacle_mode=building_obstacle_mode,
            # COASTAL/WAVE animation cadence: a fine minute-scale map-output
            # stride for a coastal/wave run, None (legacy hourly) for pluvial.
            # Drives dtout/dtmaxout in the regular-grid deck (the quadtree path
            # threads the same value into the remote deck-build output_dt below).
            output_interval_min=resolved_output_interval_min,
            # resolved advanced-physics dict (advection / theta /
            # alpha / huthresh / coriolis_latitude / wind_drag) -> setup_config
            # block. None/{} -> no physics override (byte-identical pluvial deck).
            advanced_physics=(_resolved_physics or None),
        )
        # ``build_sfincs_model`` is SYNCHRONOUS with no overall timeout
        # (sfincs_builder GDAL VSI cache/timeout is per-read only). Run it off
        # the loop + bound it so a wedged build surfaces as PRESOLVER_TIMEOUT
        # rather than an infinite silent await.
        # surface the deck build as a nested child row. A build timeout
        # (TimeoutError), the NLCD validation gate (SFINCSSetupError), or a forcing
        # adapter failure raises INSIDE the substep -> the child reads red (honesty
        # floor) and re-raises to the existing except cascade below, which returns
        # the corresponding failed envelope unchanged. No-op when no emitter bound.
        async with substep(emitter, "build_sfincs_model"):
            # NO-RECONNECT: build_sfincs_model (hydromt: DEM
            # reproject + active-mask + manning rasterize + deck write + S3
            # upload) is the longest pre-solver phase (~70 s for a city AOI) and
            # runs OFF the loop in a worker thread -- SILENT on the wire. Without a
            # periodic frame the browser WS watchdog trips and force-reconnects
            # mid-build, so the run appears to hang/go dark even though it is
            # healthy and dispatches to Batch. Drive a pipeline-state tick so the
            # connection stays up + the card visibly advances. Cancelled the
            # instant the build returns/raises (the child is still ``running``
            # here, so update_current_progress targets THIS step).
            _build_progress_task = asyncio.ensure_future(
                _drive_presolver_phase_progress(
                    emitter, start_pct=30, end_pct=88, expected_seconds=90.0
                )
            )
            try:
                model_setup = await asyncio.wait_for(
                    asyncio.to_thread(
                        build_sfincs_model,
                        dem_uri=dem_layer.uri,
                        landcover_uri=landcover_layer.uri,
                        # None when the best-effort river fetch failed
                        # (pluvial deck ignores it; build_sfincs_model documents
                        # river_geometry_uri as "may be None").
                        river_geometry_uri=(
                            river_layer.uri if river_layer is not None else None
                        ),
                        forcing=forcing_spec,
                        bbox=resolved_bbox,
                        options=options,
                        nlcd_vintage_year=nlcd_vintage_year,
                    ),
                    timeout=_BUILD_PHASE_TIMEOUT_S,
                )
            finally:
                _build_progress_task.cancel()
                try:
                    await _build_progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        # build_sfincs_model may snap grid_resolution_m UP (coarsen) if the
        # estimated active-cell count overruns the per-job cell cap. Refresh the
        # workflow-local resolution from the ACTUALLY-BUILT value so downstream
        # consumers -- the solve-telemetry record (cells/resolution/vCPU/wall) and
        # any envelope metrics -- report the resolution the solver really ran at,
        # not the pre-coarsen 30 m request.
        _built_res = getattr(model_setup, "grid_resolution_m", None)
        if _built_res:
            grid_resolution_m = float(_built_res)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "model_flood_scenario: build_sfincs_model exceeded %.0fs budget for "
            "bbox=%s -- returning PRESOLVER_TIMEOUT failed envelope.",
            _BUILD_PHASE_TIMEOUT_S,
            resolved_bbox,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="PRESOLVER_TIMEOUT",
            error_detail=(
                f"SFINCS model build exceeded {_BUILD_PHASE_TIMEOUT_S:.0f}s"
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except SFINCSSetupError as exc:
        # The headline failure path -- LULC_MAPPING_MISMATCH and friends
        # surface here. Invariant 7: the failed envelope carries the error
        # code instead of a fabricated FloodPayload.
        logger.warning(
            "build_sfincs_model raised %s (details=%s) -- returning failed envelope",
            exc.error_code,
            exc.details,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=exc.error_code,
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except SFINCSForcingAdapterError as exc:
        # COASTAL SFINCS -- the surge/discharge FETCHER → ADAPTER bridge failed
        # (unreadable fetcher FGB/COG, no usable stations, all-NaN hydrographs).
        # Invariant 7: an EXPLICIT surge request that cannot be materialised
        # surfaces as a typed failed envelope carrying the adapter error code --
        # NOT a silent degrade to a pluvial-only deck.
        logger.warning(
            "surge forcing adapter raised %s (details=%s) -- returning failed envelope",
            exc.error_code,
            exc.details,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=exc.error_code,
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except PhysicsRegistryError as exc:
        # an invalid ``advanced_physics`` override (unknown key
        # or out-of-range value) surfaces as a typed failed envelope rather than
        # an uncaught exception or a silently-wrong deck (Invariant 7).
        logger.warning(
            "model_flood_scenario: invalid advanced_physics override (%s) -- "
            "returning ADVANCED_PHYSICS_INVALID failed envelope.",
            exc,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="ADVANCED_PHYSICS_INVALID",
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # Pre-solver phases done -- the long solve takes over progress emission from
    # here (wait_for_completion drives the binding). Stamp the hand-off so the
    # card shows clear forward motion into Step 7.
    await _emit_presolver_progress(emitter, 40)

    solve_model_setup_uri = model_setup.setup_uri

    # --- Step 6: run_solver (Invariant 9 confirmation seam owned by agent) ---
    # Auto vertical scaling per case: size the compute_class from the AOI/mesh
    # element count the adaptive-grid autoscale already estimated
    # (model_setup.parameters['autoscale']['estimated_active_cells']) instead of
    # always dispatching at the default "standard" (8 vCPU). A big domain grabs
    # more compute (up to the xlarge 48-vCPU tier); a small one stays cheap. When
    # the estimate is unavailable we fall back to the caller's compute_class
    # (default "medium" == standard) -- select_compute_class never raises, so a
    # missing/zero estimate can never crash the dispatch.
    handle: ExecutionHandle | None = None
    _autoscale_for_sizing = _extract_solve_autoscale(model_setup)
    _estimated_elements = _autoscale_for_sizing.get("estimated_active_cells")
    if _estimated_elements:
        effective_compute_class = select_compute_class(_estimated_elements)
        logger.info(
            "model_flood_scenario: auto vertical scaling "
            "estimated_active_cells=%s → compute_class=%s (caller requested %s)",
            _estimated_elements,
            effective_compute_class,
            compute_class,
        )
    else:
        effective_compute_class = compute_class
        logger.info(
            "model_flood_scenario: no element estimate available; using caller "
            "compute_class=%s for the solve dispatch",
            compute_class,
        )
    try:
        # surface the solver DISPATCH (the Batch submit) as a nested
        # child row. This is a fast submit, so the child lands green quickly;
        # the LIVE Batch readout (status ticks + terminal) stays owned by the
        # two-card Sim card (mint_dispatch_and_sim_cards) below - the substep
        # does NOT touch that machinery (HARD INVARIANT). A dispatch failure
        # raises INSIDE the substep -> the child reads red (honesty floor) and
        # re-raises to the existing except handler, which returns the
        # SOLVER_DISPATCH_FAILED failed envelope unchanged. No-op when no
        # emitter is bound.
        async with substep(emitter, "run_solver"):
            # NO-SYNC-BLOCKING-ON-LOOP: ``run_solver`` does a
            # SYNCHRONOUS boto3 Batch ``submit_job`` (TLS + AWS API I/O, with
            # botocore retry/backoff that can stall for many seconds under
            # throttling / a slow control plane). It was the LAST un-offloaded
            # sync call on the flood hot path -- every other heavy step (the
            # fetcher chain, build_sfincs_model, postprocess_flood, publish_layer)
            # already runs via ``asyncio.to_thread``. Offload the submit too so a
            # slow/throttled Batch API call can never stall the 12 s WS
            # heartbeat. ``run_solver`` is EMIT-FREE (it returns an
            # ``ExecutionHandle``; this workflow does all the emitting), so a
            # worker thread is safe -- it mirrors the awaited async
            # ``run_sfincs_quadtree`` on the coastal (quadtree) path.
            handle = await asyncio.to_thread(
                run_solver,
                solver="sfincs",
                # The regular-grid model_setup.setup_uri (the quadtree path no
                # longer reaches here -- it solved inside the combined job).
                model_setup_uri=solve_model_setup_uri,
                compute_class=effective_compute_class,
            )
            solver_run_ids.append(handle.run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_solver dispatch failed: %s", exc)
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=getattr(exc, "error_code", "SOLVER_DISPATCH_FAILED"),
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # --- Two-card sim observability ----------------------------
    # Mint the Dispatch (tool, lands complete) + Sim (compute, bound to the
    # Batch jobId) cards and point the solver emitter binding at the SIM step
    # so wait_for_completion's poller feeds its live batch_status. The
    # ephemeral SFINCS Batch worker has NO inbound WS; status flows agent-side
    # over the EXISTING WS via the poller. Best-effort: emitter None / emit
    # failure -> no cards, solve proceeds unchanged.
    from trid3nt_server.agent.tools.simulation.solver.solver import EmitterBinding, set_emitter_binding

    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=getattr(handle, "solver", "sfincs") or "sfincs",
        handle=handle,
        compute_class=effective_compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    # --- Step 7: wait_for_completion (Invariant 8 cancel chain propagates) ---
    # LIVE big-sim telemetry: drive a solve-progress envelope
    # on the running card every few seconds for the duration of the solve so
    # the user sees grid/cells/vCPU/elapsed/ETA tick rather than a silent
    # spinner. The ETA comes from the perf model (autoscale
    # estimated_solve_seconds) when available, else None (no fabricated ETA).
    # The driver is a side task that we cancel as soon as the solve
    # returns/raises -- it never affects the outcome.
    _autoscale = _extract_solve_autoscale(model_setup)
    _live_active_cells = _autoscale.get("estimated_active_cells")
    _live_vcpus = _autoscale.get("vcpus")
    _live_eta = _autoscale.get("estimated_solve_seconds")
    # Deployment-aware CPU count (fingerprint audit A6): local-docker
    # reports the HOST cpu count (never the perf model's cloud vCPU
    # anchor); aws-batch keeps the autoscale-provenance value
    # byte-identical.
    from trid3nt_server.agent.tools.simulation.solver.solver import solve_progress_vcpus

    _progress_task = asyncio.ensure_future(
        _drive_live_solve_progress(
            emitter=emitter,
            run_id=handle.run_id,
            solver=getattr(handle, "solver", "sfincs") or "sfincs",
            grid_resolution_m=grid_resolution_m,
            active_cell_count=(
                int(_live_active_cells)
                if _live_active_cells is not None
                else None
            ),
            vcpus=solve_progress_vcpus(
                cloud_vcpus=(
                    int(_live_vcpus) if _live_vcpus is not None else None
                )
            ),
            eta_seconds=float(_live_eta) if _live_eta is not None else None,
        )
    )
    try:
        run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        # Invariant 8: the cancel chain is owned by wait_for_completion;
        # propagate immediately so the WS handler emits
        # pipeline-state(cancelled). Route the cancel to the SIM card
        # (best-effort terminal send, J-B-i).
        logger.info("model_flood_scenario cancelled while awaiting solver")
        await route_sim_terminal(emitter, _sim_step_id, run_result=None)
        raise
    finally:
        # Tear down the live-progress driver (success, failure, OR cancel)
        # + clear the compute-card emitter binding.
        _progress_task.cancel()
        try:
            await _progress_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        set_emitter_binding(None)

    # route the SIM compute card to its terminal state from the
    # RunResult (complete -> green, non-complete -> red) before the
    # solve-time telemetry + non-complete guard below.
    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- Solve-time telemetry (SFINCS per-job autoscale) ---
    # Accumulate real (active_cells, vCPU, wall_clock) data so the adaptive-grid
    # cell cap can be re-tuned from logged measurements. Emitted on the CURRENT
    # path (every solve), for BOTH success and failure/timeout -- a censored
    # timeout is itself a data point about a too-big AOI. Best-effort; never
    # breaks the solve loop.
    try:
        _emit_flood_solve_telemetry(
            run_result=run_result,
            handle=handle,
            model_setup=model_setup,
            bbox=resolved_bbox,
            grid_resolution_m=grid_resolution_m,
        )
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break the solve
        logger.warning("solve telemetry emission failed (non-fatal): %s", exc)

    # --- SOLVE telemetry: Batch instance + size + timing breakdown ---
    # Record ONE solve row merging run_result.batch_compute_meta (Spot instance +
    # queue/compute/total timing the wait-loop captured) with the mesh size
    # descriptor (active_cell_count + resolution_m) so a perf model can later infer
    # completion time. ONLY the regular-grid path (handle is not None) records this
    # -- the quadtree submit+wait path is left uninstrumented (consistent with the
    # two-card work). Best-effort; a telemetry failure never affects the solve.
    if handle is not None:
        try:
            _record_flood_batch_solve_telemetry(
                run_result=run_result,
                handle=handle,
                model_setup=model_setup,
                grid_resolution_m=grid_resolution_m,
                session_id=sess_id,
                case_id=None,
            )
        except Exception as exc:  # noqa: BLE001 -- telemetry must never break the solve
            logger.warning(
                "solve batch-compute telemetry failed (non-fatal): %s", exc
            )

    if run_result.status != "complete":
        # SOLVER_FAILED, SOLVER_TIMEOUT, cancelled -- surface as failed envelope.
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=run_result.error_code or run_result.status.upper(),
            error_detail=run_result.error_message or run_result.cancellation_reason or "",
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # --- Postprocess-offload branch (SFINCS Phase 4): worker-written manifest ---
    # When the Batch worker rebuilt with the raster-postprocess offload, it ran
    # the heavy NetCDF -> COG conversion ITSELF (display-ready overview-bearing
    # COGs at deterministic keys) and wrote a thin typed publish_manifest.json
    # (pointed to by completion.json.publish_manifest_uri). ``read_publish_manifest``
    # reads + SCHEMA-GATEs it; a present, schema_version==1 manifest activates the
    # REGISTER-ONLY path below - SHORT-CIRCUITing the on-box heavy tail entirely
    # (NO _resolve_run_output_to_local, NO postprocess_flood/_waves, NO
    # _ensure_raster_has_overviews - has_overviews is true). The agent-side
    # publish-or-honest-drop gate (TRID3NT_TILE_SERVER_BASE) is preserved per layer.
    #
    # ONE-RELEASE SAFETY: manifest absent OR unknown schema_version ->
    # ``read_publish_manifest`` returns None and we run the EXISTING on-box path
    # below unchanged (the raw sfincs_map.nc is still uploaded). Clean if/else.
    published_layers: list[LayerURI] = []
    depth_metrics: dict[str, Any] = {}
    manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    register_only = manifest is not None
    if register_only:
        logger.info(
            "model_flood_scenario: REGISTER-ONLY path (worker postprocess "
            "offload) run_id=%s engine=%s layers=%d",
            run_result.run_id, manifest.engine, len(manifest.layers),
        )
        async with substep(emitter, "publish_layer"):
            reg = register_manifest_layers(
                manifest, run_id=run_result.run_id, bbox=resolved_bbox
            )
        depth_metrics = reg.metrics
        # The merged manifest carries depth + wave layers. Primary layers (peak
        # depth + peak wave) ride into the success envelope's ResultLayer set;
        # context layers (the "... step N" frames) emit OUT-OF-BAND so the web
        # scrubber groups form, exactly as the on-box path does.
        published_layers = [lyr for lyr in reg.layers if lyr.role == "primary"]
        manifest_frames = [lyr for lyr in reg.layers if lyr.role != "primary"]
        if manifest_frames and emitter is not None:
            emitted = 0
            for lyr in manifest_frames:
                try:
                    await emitter.add_loaded_layer(lyr)
                    emitted += 1
                except Exception as exc:  # noqa: BLE001 -- never break the solve
                    logger.warning(
                        "model_flood_scenario: manifest frame emit failed for "
                        "%s: %s", lyr.layer_id, exc,
                    )
            if emitted:
                logger.info(
                    "model_flood_scenario: emitted %d/%d manifest animation "
                    "frames as sequential group(s) (run_id=%s)",
                    emitted, len(manifest_frames), run_result.run_id,
                )
        elif manifest_frames:
            logger.info(
                "model_flood_scenario: %d manifest animation frames available "
                "but no emitter bound - frames not emitted.",
                len(manifest_frames),
            )

    # --- Step 8: postprocess_flood (ON-BOX FALLBACK) ---
    # audit #1: ``postprocess_flood`` downloads the full ``sfincs_map.nc`` via
    # SYNC boto3 and writes N COGs to object storage -- tens of seconds to
    # minutes of blocking I/O right after the solve. Run it off the loop so it
    # cannot stall the WS keepalive. ``postprocess_flood`` is EMIT-FREE (no
    # current_emitter()/emit_*/add_loaded_layer): it produces the LayerURIs +
    # metrics then returns, and THIS workflow does all the emitting (the
    # publish + add_loaded_layer steps below run back on the loop), so it is
    # safe to move to a worker thread. SKIPPED on the register-only path.
    layers: list[LayerURI] = []
    if not register_only:
        try:
            # surface the depth postprocess as a nested child row. A
            # PostprocessError raises INSIDE the substep -> the child reads red
            # (honesty floor) and re-raises to the except handler, which returns
            # the failed envelope unchanged. No-op when no emitter is bound.
            async with substep(emitter, "postprocess_flood"):
                layers, depth_metrics = await asyncio.to_thread(
                    postprocess_flood,
                    run_result.output_uri
                    or _default_runs_prefix(run_result.run_id),
                    run_id=run_result.run_id,
                )
        except PostprocessError as exc:
            logger.warning(
                "postprocess_flood failed: %s (%s)", exc.error_code, exc
            )
            return _build_failed_envelope(
                bbox=resolved_bbox,
                project_id=proj_id,
                session_id=sess_id,
                error_code=exc.error_code,
                error_detail=str(exc),
                workflow_name=workflow_name,
                data_sources=data_sources,
                forcing=forcing_summary,
                solver_run_ids=solver_run_ids,
                return_period_years=return_period_yr,
                duration_hours=float(duration_hr),
                grid_resolution_m=grid_resolution_m,
            )

    # On-box publish (Steps 9/9b/9c) - SKIPPED on the register-only path
    # (the worker already produced display-ready COGs; the manifest branch
    # above did the registration + frame/wave emission).
    if not register_only:
        # --- Step 9: publish_layer (COG → QGIS Server WMS bridge) ---
        # For the primary flood-depth layer, invoke the PyQGIS worker to add the COG
        # to the canonical .qgs project so QGIS Server can serve it as WMS.
        # The returned WMS URL replaces the gs:// uri in the LayerURI/ResultLayer so
        # the client gets a renderable URL directly (layer-emission-contract.md, 2026-06-07).
        #
        # Non-fatal: if publish_layer fails (e.g.
        # is not yet landed), we DROP the primary raster layer from the emitted set
        # rather than fall back to the raw gs:// uri (§1, Decision 11). A
        # gs:// uri never renders -- MapLibre cannot fetch it; emitting it only paints
        # a dead, broken layer row in the LayerPanel. Dropping it keeps the map
        # honest while the rest of the envelope (metrics, provenance, narration)
        # stays intact, so the LLM narrates the publish failure truthfully and the
        # retry-on-failure loop can act. The layer_uri_emit seam enforces
        # this same rule at the emission boundary as a belt-and-suspenders invariant.
        # postprocess_flood returns [peak_primary] + [frame_0..frame_k]. The PEAK
        # layer (role="primary") is the ONE returned by the wrapper + the
        # published/on_map summary source + the habitat/Pelicun hazard raster -- it
        # takes the existing publish-or-honest-drop path UNCHANGED. The FRAME layers
        # (role="context", names "Flood depth step N") are the time-stepped animation
        # (flood animation Phase 1): each is published + emitted OUT-OF-BAND via the
        # emitter so the web SequenceScrubber group forms, WITHOUT changing the tool's
        # single-LayerURI return shape (no re-publish trip in summarize_tool_result).
        primary_layers = [lyr for lyr in layers if lyr.role == "primary"]
        frame_layers = [lyr for lyr in layers if lyr.role != "primary"]

        published_layers: list[LayerURI] = []
        for lyr in primary_layers:
            # s3:// COGs (AWS local-docker backend) take the same
            # publish-or-honest-drop gate as gs:// -- a raw object-store URI never
            # renders in MapLibre (§1), so it must never reach the map.
            # On AWS publish_layer fails until lands QGIS-on-AWS; the
            # layer is dropped and the metrics/narration stay honest.
            if (
                lyr.role == "primary"
                and lyr.layer_type == "raster"
                and (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://"))
            ):
                layer_id_for_wms = f"flood-depth-peak-{run_result.run_id}"
                try:
                    # audit #1: ``publish_layer`` runs a ``time.sleep`` poll loop
                    # (worker job poll) that blocks the loop for tens of seconds.
                    # Run it off the loop so it cannot stall the WS keepalive.
                    # ``publish_layer`` is EMIT-FREE (no current_emitter()/emit_*/
                    # add_loaded_layer): it returns the WMS URL; this workflow does
                    # the emitting (the LayerURI it builds reaches the map via the
                    # wrapper return / out-of-band add_loaded_layer back on the
                    # loop), so it is safe to move to a worker thread.
                    # surface the peak-layer publish as a nested child row.
                    # A PublishLayerError raises INSIDE the substep -> the child reads
                    # red (honesty floor) and re-raises to the existing except handler
                    # below, which DROPS the layer (publish-or-honest-drop,
                    # §1) unchanged. No-op when no emitter is bound.
                    async with substep(emitter, "publish_layer"):
                        wms_url = await asyncio.to_thread(
                            publish_layer,
                            layer_uri=lyr.uri,
                            layer_id=layer_id_for_wms,
                            style_preset=lyr.style_preset or "continuous_flood_depth",
                        )
                    # Substitute the WMS URL into the LayerURI so the client renders
                    # directly (LayerURI.uri is documented
                    # as gs:// but has no validator rejecting WMS URLs; we use it here
                    # as the renderable URL per the kickoff direction. A follow-up
                    # schema job should add a dedicated wms_url field.)
                    published_layers.append(
                        LayerURI(
                            layer_id=layer_id_for_wms,
                            name=lyr.name,
                            layer_type=lyr.layer_type,
                            uri=wms_url,
                            # job (flood-duplicate-layer fix): the published layer
                            # is the ONE styled (white->blue->green) peak-depth
                            # layer the user sees. Carry the canonical preset
                            # unconditionally -- never emit a styleless flood-depth
                            # raster (a styleless COG falls through to TiTiler's
                            # default matplotlib viridis, the redundant unstyled
                            # duplicate this workflow must never produce).
                            style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                            temporal=lyr.temporal,
                            role=lyr.role,
                            units=lyr.units,
                            bbox=resolved_bbox,
                        )
                    )
                    logger.info(
                        "publish_layer succeeded layer_id=%s wms_url=%s",
                        layer_id_for_wms,
                        wms_url,
                    )
                except PublishLayerError as exc:
                    logger.warning(
                        "publish_layer failed for layer_id=%s error_code=%s (%s) -- "
                        "DROPPING the primary flood-depth layer from the emitted set "
                        "(job-0254 §1): a raw gs:// uri never renders in MapLibre, so "
                        "we do NOT fall back to it. The envelope's metrics/provenance "
                        "remain intact and the failure is narrated honestly; the "
                        "retry-on-failure loop (job-0177) can re-attempt publish.",
                        layer_id_for_wms,
                        exc.error_code,
                        exc,
                    )
                    # Intentionally do NOT append `lyr` -- the gs:// uri stays off the
                    # map. (resolution restores the
                    # success path; until then the depth metrics still surface.)
            else:
                published_layers.append(lyr)

        # --- Step 9b: publish + emit the time-step animation frames (Phase 1) ---
        # Each frame is a DISTINCT COG (distinct runs-bucket key → distinct TiTiler
        # url= → distinct pipeline_emitter._layer_identity_key → no dedup collapse).
        # We publish in ASCENDING step order and call emitter.add_loaded_layer for
        # each so all N frames arrive as one contiguous sequential group; the final
        # session-state snapshot carries peak + N frames. Frames are emitted ONLY
        # through the emitter (NOT added to published_layers / result_layers / the
        # wrapper return), so they never reach summarize_tool_result and can't trip a
        # re-publish, and the habitat/Pelicun consumers still see layers[0] = peak.
        # When current_emitter() is None (direct call / smoke / unit test) frame
        # emission is skipped -- the frames still live in the returned `layers` from
        # postprocess_flood for tests to assert on.
        if frame_layers and emitter is not None:
            published_frame_count = 0
            for lyr in frame_layers:
                if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
                    # Already a renderable URL (defensive) -- emit as-is.
                    try:
                        await emitter.add_loaded_layer(lyr)
                        published_frame_count += 1
                    except Exception as exc:  # noqa: BLE001 -- never break the solve
                        logger.warning("frame emit failed for %s: %s", lyr.layer_id, exc)
                    continue
                try:
                    # audit #1: same as the peak ``publish_layer`` above --
                    # ``time.sleep`` poll loop blocks the loop for tens of seconds
                    # per frame. Run it off the loop so it cannot stall the WS
                    # keepalive. EMIT-FREE: it returns the WMS URL; the
                    # ``add_loaded_layer`` emit for this frame runs back on the loop
                    # just below, so moving the publish to a worker thread is safe.
                    frame_wms_url = await asyncio.to_thread(
                        publish_layer,
                        layer_uri=lyr.uri,
                        layer_id=lyr.layer_id,
                        style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                    )
                except PublishLayerError as exc:
                    # Honest drop: a frame that won't publish is dropped (its raw
                    # gs:// never renders). The remaining frames + the peak layer
                    # stay intact. If too many frames drop the group may fall below
                    # 2 members and simply not form -- acceptable, never a fake row.
                    logger.warning(
                        "publish_layer failed for frame layer_id=%s error_code=%s "
                        "(%s) -- dropping this frame from the animation group.",
                        lyr.layer_id, exc.error_code, exc,
                    )
                    continue
                frame_layer = LayerURI(
                    layer_id=lyr.layer_id,
                    name=lyr.name,  # "Flood depth step N" -- the web grouping token
                    layer_type=lyr.layer_type,
                    uri=frame_wms_url,
                    style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                    role=lyr.role,  # "context"
                    units=lyr.units,
                    bbox=resolved_bbox,
                )
                try:
                    await emitter.add_loaded_layer(frame_layer)
                    published_frame_count += 1
                except Exception as exc:  # noqa: BLE001 -- never break the solve
                    logger.warning(
                        "frame add_loaded_layer failed for %s: %s", lyr.layer_id, exc
                    )
            if published_frame_count:
                logger.info(
                    "model_flood_scenario: emitted %d/%d animation frames as a "
                    "sequential group (run_id=%s)",
                    published_frame_count, len(frame_layers), run_result.run_id,
                )
        elif frame_layers:
            logger.info(
                "model_flood_scenario: %d animation frames available but no emitter "
                "bound (direct/smoke/test) -- frames not emitted to the map.",
                len(frame_layers),
            )

    # --- Step 10: build success envelope ---
    bbox_area_km2 = _bbox_area_km2(resolved_bbox)
    result_layers: list[ResultLayer] = [
        ResultLayer(
            layer_id=lyr.layer_id,
            name=lyr.name,
            layer_type=lyr.layer_type,
            uri=lyr.uri,
            style_preset=lyr.style_preset,
            temporal=lyr.temporal,
            role=lyr.role,
            units=lyr.units,
        )
        for lyr in published_layers
    ]
    metrics = FloodMetrics(
        flooded_area_km2=min(
            bbox_area_km2,
            float(depth_metrics.get("flooded_cell_count", 0))
            * (grid_resolution_m * grid_resolution_m / 1_000_000.0),
        ),
        max_depth_m=float(depth_metrics.get("max_depth_m", 0.0)),
        mean_depth_m=float(depth_metrics.get("mean_depth_m", 0.0)),
        p95_depth_m=float(depth_metrics.get("p95_depth_m", 0.0)),
        solver_version="sfincs-v2.3.3",
        grid_resolution_m=grid_resolution_m,
        simulation_duration_hours=int(duration_hr),
    )
    envelope = AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=proj_id,
        session_id=sess_id,
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name=workflow_name,
        bbox=resolved_bbox,
        crs="EPSG:4326",
        forcing=forcing_summary,
        layers=result_layers,
        provenance=Provenance(data_sources=data_sources),
        created_at=now,
        completed_at=datetime.now(timezone.utc),
        solver_run_ids=solver_run_ids,
        flood=FloodPayload(metrics=metrics),
    )
    logger.info(
        "model_flood_scenario complete envelope_id=%s run_ids=%s layers=%d",
        envelope.envelope_id,
        solver_run_ids,
        len(result_layers),
    )
    return envelope


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


TEMPLATE_CARD = TemplateCard(
    question=(
        "peak flood inundation depth over an AOI (pluvial / coastal surge / "
        "riverine / compound; design-storm or observed forcing)"
    ),
    required_inputs=["location_query (or bbox)"],
    knobs=(
        "return_period_yr, duration_hr, coastal, river, compound, quadtree, "
        "building_obstacles, surge_forcing, wind, infiltration, breach_point, "
        "tsunami, storm_name/storm_season (spiderweb)"
    ),
)


_SFINCS_FLOOD_METADATA = AtomicToolMetadata(
    name="sfincs_flood",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="sfincs",
    tier="template",
)


@register_tool(_SFINCS_FLOOD_METADATA)
async def sfincs_flood(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    event_id: str | None = None,
    return_period_yr: int = 100,
    duration_hr: int = 24,
    compute_class: str = "medium",
    forcing_raster_uri: str | None = None,
    surge_forcing: dict[str, Any] | None = None,
    enable_subgrid: bool = False,
    coastal: bool = False,
    quadtree: bool = False,
    building_obstacles: bool | str = False,
    building_obstacle_mode: str = "exclude",
    output_interval_min: float | None = None,
    # SFINCS scenario-coverage intents (fluvial / compound /
    # wind / infiltration / levee-breach / tsunami). All default to today's
    # behaviour so a pluvial run is byte-identical.
    river: bool = False,
    compound: bool = False,
    wind: dict[str, Any] | None = None,
    advanced_physics: dict[str, Any] | None = None,
    infiltration: bool | str = False,
    breach_point: tuple[float, float] | None = None,
    breach_peak_discharge_m3s: float | None = None,
    breach_arrival_hr: float | None = None,
    tsunami: bool = False,
    tsunami_wave_height_m: float | None = None,
    tsunami_period_min: float | None = None,
    # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure via a Delft3D
    # .spw. Any of these implies coastal + the spiderweb wind path; mutually
    # exclusive with ``wind`` (typed STORM_WIND_CONFLICT, never silent).
    storm_name: str | None = None,
    storm_season: int | None = None,
    storm_track_uri: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI | dict[str, Any]:
    """Run the full deterministic SFINCS flood-modeling workflow end-to-end.

    Fidelity: SFINCS reduced-physics SCREENING-grade flood engine (subgrid +
    quadtree); planning-grade peak-depth, not a calibrated regulatory model.
    Off-scope: urban storm-sewer / pipe-network flooding -> swmm_urban_flood;
    groundwater plume -> modflow_contaminant_plume; tsunami / dam-break run-up
    -> geoclaw_inundation; spectral wave field -> swan_wave_field; river dye /
    tracer transport -> telemac_river_dye.

    Nine-step composition chain (all deterministic Python, zero LLM calls):
    1. ``geocode_location(location_query)`` -- optional; derives bbox from
       a free-text place name when ``bbox`` is not provided.
    2. ``fetch_dem(bbox)`` -- downloads USGS 3DEP or CoastalDEM to a COG.
    3. ``fetch_landcover(bbox)`` -- downloads NLCD landcover for Manning's
       roughness parameterization.
    4. ``fetch_river_geometry(bbox)`` -- downloads NHD river geometry for
       channel routing.
    5. ``lookup_precip_return_period(bbox, return_period_years, duration_hours)``
       -- looks up NOAA Atlas 14 design-storm precipitation depth.
    6. ``build_sfincs_model(dem_uri, landcover_uri, river_uri, forcing, bbox)``
       -- assembles the HydroMT-SFINCS deck (s3-staged) with the NLCD
       validation gate.
    7. ``run_solver(model_setup)`` -- dispatches the SFINCS solve to the
       local Docker solver backend.
    8. ``wait_for_completion(run_id)`` -- polls until SUCCEEDED or FAILED;
       emits progress events per FR-WC-12.
    9. ``postprocess_flood(run_outputs_uri)`` → ``publish_layer(flood_depth_cog)``
       -- extracts peak depth COG, uploads to the runs bucket, and publishes
       it as an ``s3://`` COG the QGIS plugin renders natively.

    When to use:
        - User asks to model a flood scenario, simulate flood inundation,
          compute peak flood depth, run a flood simulation, or estimate flood
          extent for a named location.
        - Any request mentioning "return period", "design storm", "ARI",
          "flood risk", "inundation depth", or "flood extent" for a named
          location or bounding box.

    When NOT to use:
        - Custom solver dispatch (use ``run_solver`` + ``wait_for_completion``
          directly).
        - Non-flood hazards (separate workflow milestones).
        - Cancelling a running flood scenario (use the WS ``cancel`` envelope;
          cancellation propagates through ``wait_for_completion``).

    Examples:
        - "model the flood from a 100-year storm in Fort Myers, FL"
          → location_query: Fort Myers, FL ; return_period_years: 100
        - "peak flood depth from a 25-year design storm in Houston"
          → location_query: Houston ; return_period_years: 25
        - "simulate flood inundation for Hurricane Ian near Fort Myers"
          → location_query: Fort Myers ; return_period_years: 100 (default)
        - "500-year flood for New Orleans, 48-hour duration"
          → location_query: New Orleans ; return_period_years: 500 ; duration_hours: 48

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
            ``None``, ``location_query`` is used to geocode. Direct bbox
            wins when both are supplied.
        location_query: free-text place name (geocoded via Nominatim).
        event_id: optional event ID for HEP-side provenance (v0.1: carried
            on the envelope's provenance hook; HEP integration M5.5+).
        return_period_years: design-storm ARI in years. Atlas 14 publishes
            {1, 2, 5, 10, 25, 50, 100, 200, 500, 1000}. Default 100.
            (Alias ``return_period_yr`` is accepted for backward compat.)
        duration_hours: design-storm duration in hours. Atlas 14 publishes
            durations 5-min through 60-day. Default 24.
            (Alias ``duration_hr`` is accepted for backward compat.)
        compute_class: FR-CE-3 compute class. Default ``"medium"``.
        forcing_raster_uri: optional ``s3://...`` URI of an OBSERVED
            accumulated-precipitation raster (e.g. an MRMS QPE COG from
            ``fetch_mrms_qpe``). When provided, the workflow forces SFINCS
            with the AREA-MEAN of this raster over the model domain (converted
            to a uniform rain rate) INSTEAD of the Atlas 14 design storm -- this
            is the Case 3 real-data forcing path. ``duration_hours`` is reused
            as the accumulation window. Leave unset (``None``) for the standard
            return-period design-storm scenario.
        surge_forcing: optional COMPOUND-FLOOD forcing spec -- a nested dict that
            wires the coastal water-level (surge / tide) boundary AND/OR the
            fluvial river-discharge boundary (plus optional wind / pressure)
            into the SFINCS deck, so the run combines coastal surge + river
            discharge + the pluvial design storm. Shape (each sub-dict optional;
            mirror the internal ``model_flood_scenario`` contract exactly):
            ``{"waterlevel": {"timeseries_uri": ..., "locations_uri": ...,
            "offset": ..., "buffer_m": ...} | {"geodataset_uri": ...},
            "discharge": {"timeseries_uri": ..., "rivers_uri": ...,
            "river_upa_km2": ...}, "wind": {"magnitude": ..., "direction": ...},
            "pressure": {"grid_uri": ..., "fill_value": ...}}``. The forcing-file
            URIs come from the forcing fetchers (``fetch_gtsm_tide_surge`` /
            ``fetch_noaa_coops_tides`` / ``fetch_noaa_nwm_streamflow`` /
            ERA5). Supplying a water-level
            boundary IMPLIES ``coastal=True`` (the surge needs a nearshore bed),
            so the DEM fetch auto-routes through ``fetch_topobathy``. ``None``
            (the default) → pure-pluvial deck, BYTE-IDENTICAL to today (no surge /
            discharge blocks emitted; regression-critical).
        enable_subgrid: emit a SFINCS ``setup_subgrid`` block so the solve runs on
            a coarse grid while resolving sub-cell topography + roughness (the
            cheap higher-fidelity urban-flood estimate). Auto-enabled when
            ``building_obstacles`` is set. Default ``False`` (no subgrid block;
            byte-identical to today).
        coastal: set ``True`` for a COASTAL flood / surge / run-up scenario near
            the ocean shoreline. This routes the terrain fetch through
            ``fetch_topobathy`` (a SEAMLESS land-plus-seafloor DEM merging USGS
            3DEP with NOAA NCEI CUDEM bathymetry) instead of the land-only
            ``fetch_dem`` -- so the model has a real nearshore bed to route
            inundation over. Default ``False`` for an inland / pluvial flood
            (land-only DEM, unchanged). Auto-enabled when a surge water-level
            boundary is supplied. Use for prompts mentioning the coast, storm
            surge, hurricane inundation at the shoreline, tide, or "include the
            sea floor / bathymetry".
            ``coastal=True`` also auto-wires a sea water-level boundary + waves
            (no ``surge_forcing`` needed); set it only when the sea is involved.
        quadtree: set ``True`` for storm waves / wave run-up (implies
            ``coastal=True``; auto-enabled for any coastal run). Default ``False``.
        output_interval_min: optional animation frame spacing in minutes (coastal
            runs default fine; pluvial stays hourly). Leave unset unless asked.
        building_obstacles: OPTIONAL, default ``False`` (OFF). When truthy, the
            workflow burns building footprints into the SFINCS grid so the flood
            routes AROUND buildings -- a more realistic (but slightly slower)
            urban-flood estimate. Three forms:
              * ``True`` → best-effort fetch of OSM building footprints (OSM
                Overpass) for the AOI; burned as no-flow ``exclude_mask`` cells.
                A footprint-fetch failure NEVER aborts the flood -- it logs a
                warning and proceeds WITHOUT obstacles (honest degrade, same
                policy as river geometry).
              * a ``str`` → used verbatim as a footprint geofile URI (e.g. a
                prior ``fetch_buildings`` output FlatGeobuf / GeoJSON).
              * ``False`` → no obstacles (terrain-only; the default, unchanged
                plain DEM + Manning deck).
            ASK-WHEN-URBAN: for an URBAN / developed AOI (a named city core,
            downtown / midtown, a dense built-up bbox), if the user has NOT said
            whether to include buildings, ASK before running -- e.g. "Model
            buildings as obstacles so water routes around them -- more realistic
            but a bit slower -- or just terrain?" -- and set ``building_obstacles``
            from the answer. If the user PRE-specified ("include buildings" /
            "route around buildings" → ``True``; "terrain only" → ``False``),
            honor it without asking. RURAL / non-urban AOIs default to no
            buildings WITHOUT asking. Obstacles are OFF by default everywhere.
        building_obstacle_mode: how footprints are burned, default ``"exclude"``.
            ``"exclude"`` makes footprint cells INACTIVE no-flow holes on the
            plain regular grid (fast/rough, no subgrid). ``"raise"`` instead
            lifts the footprint bed elevation via the SFINCS subgrid so flow is
            impeded without disconnecting the domain (higher fidelity; auto-uses
            subgrid). Leave ``"exclude"`` unless higher fidelity is requested.
        river: set ``True`` for a FLUVIAL / river-flooding run -- auto-wires a
            river-discharge boundary (NOAA NWM -> USGS NWIS -> honest skip).
            Stays inland (``fetch_dem``). Default ``False``.
        compound: set ``True`` for a COMPOUND flood (coastal surge AND river
            discharge AND rain together; implies ``coastal`` + ``river``).
            Default ``False``.
        wind: optional WIND forcing ``{"magnitude": <m/s>, "direction":
            <deg-from>}`` or ``{"grid_uri": <nc>}`` (user/ERA5 supplied, never
            invented). Default ``None``.
        advanced_physics: optional SFINCS physics overrides (keys: advection,
            theta, alpha, huthresh, coriolis_latitude, wind_drag), validated +
            threaded into the deck. Default ``None`` (deck unchanged).
        infiltration: ``True`` -> auto-fetch GCN250 curve numbers; a ``str`` ->
            verbatim CN raster URI; ``False`` (default) -> no infiltration loss.
        breach_point: ``(lon, lat)`` of a drawn levee breach. USER-GATED: needs
            ``breach_peak_discharge_m3s`` or the run returns a typed input gate
            (never fabricated). Default ``None``.
        breach_peak_discharge_m3s: peak breach discharge (m^3/s, user-supplied).
            Default ``None``.
        breach_arrival_hr: optional breach time-to-peak (hr). Default ``None``.
        tsunami: ``True`` for a TSUNAMI run (implies ``coastal``). USER-GATED:
            needs ``tsunami_wave_height_m`` or the run returns a typed input gate.
            Default ``False``.
        tsunami_wave_height_m: tsunami peak wave height (m, user-supplied).
            Default ``None``.
        tsunami_period_min: tsunami period (min); defaults to ~15 min. ``None``.
        storm_name: NAMED historical hurricane / tropical cyclone (e.g.
            ``"Michael"``). With ``storm_season`` it resolves the IBTrACS best
            track via ``fetch_storm_tracks`` and builds a parametric Holland
            wind+pressure SPIDERWEB (.spw) that GENERATES the surge over a
            shelf-scale domain -- the asymmetry uniform wind cannot produce
            (inundation concentrated RIGHT of the eye). Implies ``coastal``.
            MUTUALLY EXCLUSIVE with ``wind`` (typed STORM_WIND_CONFLICT).
            Example: "Simulate Hurricane Michael at landfall at Mexico Beach" ->
            ``storm_name="Michael", storm_season=2018,
            location_query="Mexico Beach, FL"``.
        storm_season: the storm's IBTrACS SEASON (calendar year, e.g. ``2018``).
            Names are reused across years, so pair it with ``storm_name``.
        storm_track_uri: a prior ``fetch_storm_tracks`` POINTS-FGB output used
            verbatim as the track (skips the fetch). Also implies coastal +
            spiderweb. Example: reuse a track already shown on the map.
        project_id / session_id: ULID identifiers from the WS session, forwarded
            for provenance / artifact namespacing. When ``None`` (default), the
            internal workflow mints fresh ULIDs (direct-call / smoke path).

    Returns:
        On success: the primary flood-depth COG as a ``LayerURI`` -- the
        ``PipelineEmitter.emit_tool_call`` gate at
        ``pipeline_emitter.py:517`` fires ``add_loaded_layer`` when it sees
        a ``LayerURI`` return, which appends to ``session-state.loaded_layers``
        and emits a fresh ``session-state`` envelope (A.7 replace-not-reconcile).
        See ``docs/decisions/layer-emission-contract.md`` (ADOPTED 2026-06-07).

        On failure (partial-failure envelope with empty layers): the
        AssessmentEnvelope serialized as a dict so the LLM can narrate the
        error. The dict carries the Appendix B.4 Flood subtype shape with the
        error code threaded into ``flood.metrics.solver_version`` as
        ``"failed:<ERROR_CODE>"``.

    FR-DC-6: This wrapper declares ``cacheable=False`` +
    ``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (a new
    FR-DC-6 source class for the workflow exposure surface -- same shape as
    ``solver_dispatch``).

    Cross-tool dependencies:
        Upstream (consumes) -- the 9-step fetch + solve chain above:
        - ``geocode_location`` (optional) → ``fetch_dem`` → ``fetch_landcover``
          → ``fetch_river_geometry`` → ``lookup_precip_return_period``
          → ``build_sfincs_model`` → ``run_solver`` → ``wait_for_completion``
          → ``postprocess_flood`` → ``publish_layer``
        Downstream (feeds):
        - ``pelicun_damage_assessment`` (explicit ``assets_uri`` OR bbox
          auto-fetch mode) -- consumes the returned flood-depth COG
          ``LayerURI.uri`` as ``hazard_raster_uri`` for building-damage assessment.
        - ``spatial_query`` -- flood-depth COG as the value raster for
          population-in-flood-zone or habitat-impact metrics.
    """
    envelope = await model_flood_scenario(
        bbox=bbox,
        location_query=location_query,
        event_id=event_id,
        return_period_yr=return_period_yr,
        duration_hr=duration_hr,
        compute_class=compute_class,
        forcing_raster_uri=forcing_raster_uri,
        surge_forcing=surge_forcing,
        enable_subgrid=enable_subgrid,
        coastal=coastal,
        quadtree=quadtree,
        building_obstacles=building_obstacles,
        building_obstacle_mode=building_obstacle_mode,
        output_interval_min=output_interval_min,
        # SFINCS scenario-coverage intents threaded through.
        river=river,
        compound=compound,
        wind=wind,
        advanced_physics=advanced_physics,
        infiltration=infiltration,
        breach_point=breach_point,
        breach_peak_discharge_m3s=breach_peak_discharge_m3s,
        breach_arrival_hr=breach_arrival_hr,
        tsunami=tsunami,
        tsunami_wave_height_m=tsunami_wave_height_m,
        tsunami_period_min=tsunami_period_min,
        # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure.
        storm_name=storm_name,
        storm_season=storm_season,
        storm_track_uri=storm_track_uri,
        project_id=project_id,
        session_id=session_id,
    )
    # --- Layer-emission contract pin (docs/decisions/layer-emission-contract.md, 2026-06-07) ---
    # Return the primary flood-depth COG as a LayerURI so PipelineEmitter's
    # isinstance(result, LayerURI) gate at pipeline_emitter.py:517 fires
    # add_loaded_layer → session-state.loaded_layers (declarative, A.7
    # replace-not-reconcile).  On failure the envelope has no layers; fall
    # back to the dict so the LLM can narrate the error honestly.
    #
    # bbox fix: include ``envelope.bbox`` on the returned LayerURI so
    # ``PipelineEmitter.add_loaded_layer`` fires the post-publish
    # ``emit_map_command("zoom-to")`` (pipeline_emitter.py:443-447). Prior to
    # this fix the wrapper dropped bbox (``envelope.layers[0]`` is a
    # ``ResultLayer`` with no bbox field) → silent no-zoom after layer landed.
    if envelope.layers:
        primary = envelope.layers[0]
        return LayerURI(
            layer_id=primary.layer_id,
            name=primary.name,
            layer_type=primary.layer_type,
            uri=primary.uri,
            style_preset=primary.style_preset,
            temporal=primary.temporal,
            role=primary.role,
            units=primary.units,
            bbox=envelope.bbox,
        )
    return envelope.model_dump(mode="json")
