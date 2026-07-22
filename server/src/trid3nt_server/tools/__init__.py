"""Atomic-tool registry skeleton (FR-AS-3, FR-CE-8, FR-TA-2, Decision O).

This package is the agent-service-owned surface for atomic tools (M4 substrate).
``schema`` owns ``AtomicToolMetadata`` (in ``trid3nt_contracts.tool_registry``);
``agent`` owns the registry that collects the decorated functions at import
time and the cache shim that mediates external-API calls (see ``.cache``).
The ``qgis_process`` pass-through tool lives in ``.passthroughs``.

How registration works:

    from trid3nt_contracts.tool_registry import AtomicToolMetadata
    from trid3nt_server.tools import register_tool

    @register_tool(AtomicToolMetadata(
        name="fetch_dem",
        ttl_class="static-30d",
        source_class="dem",
        cacheable=True,
    ))
    def fetch_dem(bbox: BBox) -> str:
        ...

The ``@register_tool`` decorator:

- Re-validates the metadata payload (pydantic auto-validates at construction;
  passing an already-validated model just stores it) and refuses to register
  a tool whose metadata fails the FR-DC-6 cross-field rule.
- Stores ``(fn, metadata, module)`` in module-level ``TOOL_REGISTRY``
  keyed by ``metadata.name``.
- **Fails fast on duplicate names** per FR-CE-8: a second registration under
  the same name raises ``ToolRegistrationError`` at import time so the
  agent service cannot start with an inconsistent tool surface.
- Returns the original function unchanged so direct-call testing is trivial.

The ``get_registered_tools()`` helper returns the current registry contents
(a snapshot list) for the agent service's startup-time tool registration. The
live generation loop is the raw Bedrock Converse SDK (``adapter.py``), which
builds its tool declarations directly from this snapshot; there is no ADK
wrapper (``google-adk`` was dropped in the GCP decommission).

Importing the package triggers ``@register_tool`` decorators in submodules
(``.passthroughs`` for M4 job-0032; ``.fetchers`` etc. for M4 job-0033+).
We import them eagerly here so any registration-time ``ValidationError`` or
``ToolRegistrationError`` surfaces at startup (FR-CE-8 fail-fast).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

__all__ = [
    "RegisteredTool",
    "ToolRegistrationError",
    "TOOL_REGISTRY",
    "register_tool",
    "get_registered_tools",
    "clear_registry_for_tests",
]


class ToolRegistrationError(RuntimeError):
    """Raised when a tool fails registration (duplicate name, bad metadata)."""


@dataclass(frozen=True)
class RegisteredTool:
    """A tool entry in ``TOOL_REGISTRY``.

    Fields:
    - ``metadata`` - the validated ``AtomicToolMetadata`` for the tool.
    - ``fn`` - the original (undecorated) callable. The registry deliberately
      does NOT wrap it; tests call the function directly via this attribute.
    - ``module`` - the ``__module__`` attribute at registration time, useful
      for diagnostics (`"trid3nt_server.tools.meta.passthroughs"` etc.).
    """

    metadata: AtomicToolMetadata
    fn: Callable[..., Any]
    module: str


#: Module-level registry, keyed by ``metadata.name``. Populated at import time
#: by ``@register_tool`` calls in submodules. The agent service iterates this
#: at startup (via ``get_registered_tools()``) to build the Bedrock Converse
#: tool declarations in ``adapter.py`` (raw SDK loop; no ADK wrapper).
TOOL_REGISTRY: dict[str, RegisteredTool] = {}


def register_tool(
    metadata: AtomicToolMetadata,
    *,
    supports_global_query: bool | None = None,
    payload_mb_estimator_name: str | None = None,
    read_only_hint: bool | None = None,
    open_world_hint: bool | None = None,
    destructive_hint: bool | None = None,
    idempotent_hint: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Return a decorator that records ``fn`` + ``metadata`` in ``TOOL_REGISTRY``.

    Usage::

        @register_tool(AtomicToolMetadata(name="x", ttl_class="static-30d",
                                          source_class="x"))
        def x(...): ...

    Wave 1.5 (job-0114) added two metadata flags. They may be set either
    on the constructed ``AtomicToolMetadata`` directly OR passed as
    decorator-level kwargs (kwargs win and produce a new metadata via
    ``model_copy(update=...)``)::

        @register_tool(_BASE_META, supports_global_query=True)
        def fetch_nws_alerts_conus(bbox=None): ...

    Wave 4.10 (job-B12) added four MCP annotation hints as decorator-level
    kwargs using the same pattern::

        @register_tool(_BASE_META, read_only_hint=True, open_world_hint=True,
                       destructive_hint=False, idempotent_hint=True)
        def fetch_dem(bbox): ...

    All kwargs default to ``None`` meaning "use whatever the metadata
    already declares" - the kwarg path is a convenience for tool authors
    who want the decorator site to be the single visible declaration of
    the flag. Backward-compatible: existing tools that pre-date the
    kwargs continue to work; the metadata defaults
    (``supports_global_query=False``, ``payload_mb_estimator_name=None``,
    ``read_only_hint=True``, ``open_world_hint=False``,
    ``destructive_hint=False``, ``idempotent_hint=True``)
    preserve pre-Wave-4.10 behaviour.

    Fail-fast invariants (FR-CE-8):

    - ``metadata`` must already be a valid ``AtomicToolMetadata`` (pydantic
      auto-validates at construction, including the FR-DC-6 cross-field
      ``cacheable``/``ttl_class``/``source_class`` rule). Passing anything
      else raises ``TypeError``.
    - The same ``metadata.name`` cannot register twice. A duplicate raises
      ``ToolRegistrationError`` at import time so a misconfigured agent
      service never starts.
    - The original ``fn`` is returned UNCHANGED, so callers can both register
      a tool and call it directly in tests.
    """
    if not isinstance(metadata, AtomicToolMetadata):
        raise TypeError(
            f"register_tool expects AtomicToolMetadata, got {type(metadata).__name__}"
        )

    # If the caller passed Wave-1.5 / Wave-4.10 flags at the decorator level,
    # fold them into a fresh metadata. ``model_copy(update=...)`` re-runs
    # validators because pydantic v2 ``GraceModel`` has
    # ``validate_assignment=True``, so a bad combination still fails fast at
    # import time.
    overrides: dict[str, Any] = {}
    if supports_global_query is not None:
        overrides["supports_global_query"] = supports_global_query
    if payload_mb_estimator_name is not None:
        overrides["payload_mb_estimator_name"] = payload_mb_estimator_name
    if read_only_hint is not None:
        overrides["read_only_hint"] = read_only_hint
    if open_world_hint is not None:
        overrides["open_world_hint"] = open_world_hint
    if destructive_hint is not None:
        overrides["destructive_hint"] = destructive_hint
    if idempotent_hint is not None:
        overrides["idempotent_hint"] = idempotent_hint
    if overrides:
        metadata = metadata.model_copy(update=overrides)

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = metadata.name
        existing = TOOL_REGISTRY.get(name)
        if existing is not None:
            raise ToolRegistrationError(
                f"tool {name!r} is already registered "
                f"(existing from module {existing.module!r}, "
                f"new from module {fn.__module__!r}); duplicate registrations "
                f"are rejected at import time per FR-CE-8."
            )
        TOOL_REGISTRY[name] = RegisteredTool(
            metadata=metadata, fn=fn, module=fn.__module__
        )
        return fn

    return _decorator


def get_registered_tools() -> list[RegisteredTool]:
    """Return a stable-ordered snapshot of the current registry.

    Used by the agent service at startup to build the Bedrock Converse tool
    declarations (raw SDK loop in ``adapter.py``). Sorted by ``metadata.name``
    so the registration order is deterministic across runs (important for
    FR-AS-3 review diffs).
    """
    return sorted(TOOL_REGISTRY.values(), key=lambda t: t.metadata.name)


def clear_registry_for_tests() -> None:
    """Empty the registry. ONLY for tests; never call from product code.

    Atomic-tool registration is import-time; tests that need a fresh registry
    or want to swap implementations call this in a fixture.
    """
    TOOL_REGISTRY.clear()


# ---------------------------------------------------------------------------
# Eager submodule import (FR-CE-8 fail-fast).
#
# Importing ``trid3nt_server.tools`` populates ``TOOL_REGISTRY`` with EVERY
# atomic tool the agent service supports: each module below carries at least
# one ``@register_tool`` decorator that fires at import time, so any
# registration-time ``ValidationError`` / ``ToolRegistrationError`` surfaces
# at startup rather than first use. The block is EXPLICIT (no pkgutil walk),
# sorted, and grouped by subpackage; regenerate it when adding a tool module.
# Per-tool rationale lives in each module's docstring.
# ---------------------------------------------------------------------------

# -- fetchers/weather --
from .fetchers.weather import fetch_airnow_air_quality  # noqa: E402,F401
from .fetchers.weather import fetch_asos_metar  # noqa: E402,F401
from .fetchers.weather import fetch_glm_lightning  # noqa: E402,F401
from .fetchers.weather import fetch_hrrr_forecast  # noqa: E402,F401
from .fetchers.weather import fetch_hrrr_smoke  # noqa: E402,F401
from .fetchers.weather import fetch_mrms_qpe  # noqa: E402,F401
from .fetchers.weather import fetch_nexrad_reflectivity  # noqa: E402,F401
from .fetchers.weather import fetch_nws_alerts_conus  # noqa: E402,F401
from .fetchers.weather import fetch_nws_event  # noqa: E402,F401
from .fetchers.weather import fetch_openaq_measurements  # noqa: E402,F401
from .fetchers.weather import fetch_raws_weather  # noqa: E402,F401
from .fetchers.weather import fetch_storm_events_db  # noqa: E402,F401
from .fetchers.weather import fetch_storm_tracks  # noqa: E402,F401

# -- fetchers/hydrology --
from .fetchers.hydrology import fetch_cama_flood_discharge  # noqa: E402,F401
from .fetchers.hydrology import fetch_jrc_global_surface_water  # noqa: E402,F401
from .fetchers.hydrology import fetch_nhd_waterbodies  # noqa: E402,F401
from .fetchers.hydrology import fetch_nhdplus_nldi_navigate  # noqa: E402,F401
from .fetchers.hydrology import fetch_noaa_nwm_streamflow  # noqa: E402,F401
from .fetchers.hydrology import fetch_nwi_wetlands  # noqa: E402,F401
from .fetchers.hydrology import fetch_nws_river_forecast  # noqa: E402,F401
from .fetchers.hydrology import fetch_river_geometry  # noqa: E402,F401
from .fetchers.hydrology import fetch_usgs_groundwater_levels  # noqa: E402,F401
from .fetchers.hydrology import fetch_usgs_nwis_gauges  # noqa: E402,F401
from .fetchers.hydrology import fetch_usgs_water_quality  # noqa: E402,F401

# -- fetchers/ocean --
from .fetchers.ocean import fetch_gtsm_tide_surge  # noqa: E402,F401
from .fetchers.ocean import fetch_noaa_coops_currents  # noqa: E402,F401
from .fetchers.ocean import fetch_noaa_coops_tides  # noqa: E402,F401
from .fetchers.ocean import fetch_noaa_slr_confidence  # noqa: E402,F401
from .fetchers.ocean import fetch_noaa_slr_marsh  # noqa: E402,F401
from .fetchers.ocean import fetch_noaa_slr_scenarios  # noqa: E402,F401
from .fetchers.ocean import fetch_noaa_sst  # noqa: E402,F401
from .fetchers.ocean import fetch_topobathy  # noqa: E402,F401

# -- fetchers/terrain --
from .fetchers.terrain import fetch_3dep_extra  # noqa: E402,F401
from .fetchers.terrain import fetch_copernicus_dem  # noqa: E402,F401
from .fetchers.terrain import fetch_dem  # noqa: E402,F401
from .fetchers.terrain import fetch_esri_landcover_10m  # noqa: E402,F401
from .fetchers.terrain import fetch_landcover  # noqa: E402,F401

# -- fetchers/imagery --
from .fetchers.imagery import fetch_goes_active_fire  # noqa: E402,F401
from .fetchers.imagery import fetch_goes_animation  # noqa: E402,F401
from .fetchers.imagery import fetch_goes_archive_animation  # noqa: E402,F401
from .fetchers.imagery import fetch_goes_satellite  # noqa: E402,F401
from .fetchers.imagery import fetch_landsat_imagery  # noqa: E402,F401
from .fetchers.imagery import fetch_naip  # noqa: E402,F401
from .fetchers.imagery import fetch_sentinel1_sar  # noqa: E402,F401
from .fetchers.imagery import fetch_sentinel2_truecolor  # noqa: E402,F401
from .fetchers.imagery import fetch_viirs_day_fire  # noqa: E402,F401

# -- fetchers/climate --
from .fetchers.climate import fetch_chirps_precipitation  # noqa: E402,F401
from .fetchers.climate import fetch_climate_normals  # noqa: E402,F401
from .fetchers.climate import fetch_era5_reanalysis  # noqa: E402,F401
from .fetchers.climate import fetch_gridmet  # noqa: E402,F401
from .fetchers.climate import fetch_modis_lst  # noqa: E402,F401
from .fetchers.climate import fetch_us_drought_monitor  # noqa: E402,F401
from .fetchers.climate import lookup_precip_return_period  # noqa: E402,F401

# -- fetchers/biodiversity --
from .fetchers.biodiversity import fetch_ebird_observations  # noqa: E402,F401
from .fetchers.biodiversity import fetch_gbif_occurrences  # noqa: E402,F401
from .fetchers.biodiversity import fetch_inaturalist_observations  # noqa: E402,F401
from .fetchers.biodiversity import fetch_iucn_red_list_range  # noqa: E402,F401
from .fetchers.biodiversity import fetch_mobi  # noqa: E402,F401
from .fetchers.biodiversity import fetch_movebank_tracks  # noqa: E402,F401
from .fetchers.biodiversity import fetch_wdpa_protected_areas  # noqa: E402,F401

# -- fetchers/socioeconomic --
from .fetchers.socioeconomic import fetch_administrative_boundaries  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_buildings  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_cdc_svi  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_census_acs  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_epa_ejscreen  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_field_boundaries  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_ghsl_population  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_hrsl_population  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_lehd_jobs  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_overpass_pois  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_population  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_roads_osm  # noqa: E402,F401
from .fetchers.socioeconomic import fetch_usace_nsi  # noqa: E402,F401
from .fetchers.socioeconomic import geocode_location  # noqa: E402,F401

# -- fetchers/hazard --
from .fetchers.hazard import fetch_epa_frs_facilities  # noqa: E402,F401
from .fetchers.hazard import fetch_fault_sources  # noqa: E402,F401
from .fetchers.hazard import fetch_fema_nfhl_zones  # noqa: E402,F401
from .fetchers.hazard import fetch_firms_active_fire  # noqa: E402,F401
from .fetchers.hazard import fetch_hifld_critical_infrastructure  # noqa: E402,F401
from .fetchers.hazard import fetch_hifld_transmission_lines  # noqa: E402,F401
from .fetchers.hazard import fetch_landfire_fuels  # noqa: E402,F401
from .fetchers.hazard import fetch_mtbs_burn_severity  # noqa: E402,F401
from .fetchers.hazard import fetch_nifc_fire_perimeters  # noqa: E402,F401
from .fetchers.hazard import fetch_openfema_disasters  # noqa: E402,F401
from .fetchers.hazard import fetch_tsunami_events  # noqa: E402,F401
from .fetchers.hazard import fetch_usace_dams  # noqa: E402,F401
from .fetchers.hazard import fetch_usace_levees  # noqa: E402,F401
from .fetchers.hazard import fetch_usfs_canopy_fuels  # noqa: E402,F401
from .fetchers.hazard import fetch_usgs_earthquakes  # noqa: E402,F401
from .fetchers.hazard import fetch_usgs_volcano_alerts  # noqa: E402,F401
from .fetchers.hazard import fetch_wfigs_incident  # noqa: E402,F401

# -- fetchers/soil --
from .fetchers.soil import fetch_gcn250_curve_numbers  # noqa: E402,F401
from .fetchers.soil import fetch_snotel_snow  # noqa: E402,F401
from .fetchers.soil import fetch_soilgrids  # noqa: E402,F401
from .fetchers.soil import fetch_statsgo_soils  # noqa: E402,F401

# -- processing (compute / clip / extract / vector-edit / charts) --
from .processing import aggregate_claims_across_sources  # noqa: E402,F401
from .processing import analyze_affected_fields  # noqa: E402,F401
from .processing import clip_raster_to_bbox  # noqa: E402,F401
from .processing import clip_raster_to_polygon  # noqa: E402,F401
from .processing import clip_vector_to_polygon  # noqa: E402,F401
from .processing import compute_aspect  # noqa: E402,F401
from .processing import compute_blended_composite  # noqa: E402,F401
from .processing import compute_building_density  # noqa: E402,F401
from .processing import compute_canopy_height  # noqa: E402,F401
from .processing import compute_change_detection  # noqa: E402,F401
from .processing import compute_colored_relief  # noqa: E402,F401
from .processing import compute_contours  # noqa: E402,F401
from .processing import compute_cross_section  # noqa: E402,F401
from .processing import compute_exposure_summary  # noqa: E402,F401
from .processing import compute_flood_depth_damage  # noqa: E402,F401
from .processing import compute_hillshade  # noqa: E402,F401
from .processing import compute_home_range_kde  # noqa: E402,F401
from .processing import compute_idf_curve  # noqa: E402,F401
from .processing import compute_impervious_surface  # noqa: E402,F401
from .processing import compute_layer_bounds  # noqa: E402,F401
from .processing import compute_model_residuals  # noqa: E402,F401
from .processing import compute_movement_trajectory  # noqa: E402,F401
from .processing import compute_ndvi  # noqa: E402,F401
from .processing import compute_overtopping  # noqa: E402,F401
from .processing import compute_sediment_yield  # noqa: E402,F401
from .processing import compute_slope  # noqa: E402,F401
from .processing import compute_terrain_profile  # noqa: E402,F401
from .processing import compute_urban_heat_island  # noqa: E402,F401
from .processing import compute_wave_nomograph  # noqa: E402,F401
from .processing import compute_zonal_statistics  # noqa: E402,F401
from .processing import cut_features_with_polygon  # noqa: E402,F401
from .processing import delineate_watershed  # noqa: E402,F401
from .processing import digitize_water_body  # noqa: E402,F401
from .processing import enhance_satellite_image  # noqa: E402,F401
from .processing import extract_landcover_class  # noqa: E402,F401
from .processing import extract_stream_network  # noqa: E402,F401
from .processing import extract_timeseries_at_point  # noqa: E402,F401
from .processing import fill_gaps  # noqa: E402,F401
from .processing import generate_choropleth_legend  # noqa: E402,F401
from .processing import generate_damage_distribution  # noqa: E402,F401
from .processing import generate_histogram  # noqa: E402,F401
from .processing import generate_time_series  # noqa: E402,F401
from .processing import merge_features  # noqa: E402,F401
from .processing import query_point_hazard  # noqa: E402,F401
# DuckDB spatial-query fold (Phase B): ONE read-only SQL surface replaces the
# three analytical Q&A tools (summarize_layer_statistics /
# count_features_above_threshold / aggregate_property_within_zone).
from .processing import spatial_query  # noqa: E402,F401

# -- simulation (engine bridges, model_* engines, solver seam) --
from .simulation import model_debris_flow  # noqa: E402,F401
from .simulation import model_fire_spread  # noqa: E402,F401
from .simulation import postprocess_pelicun  # noqa: E402,F401
from .simulation import run_geoclaw_tool  # noqa: E402,F401
from .simulation import run_landlab_tool  # noqa: E402,F401
from .simulation import run_modflow_tool  # noqa: E402,F401
from .simulation import run_openquake_tool  # noqa: E402,F401
from .simulation import run_pelicun_damage_assessment  # noqa: E402,F401
from .simulation import run_river_seepage_tool  # noqa: E402,F401
from .simulation import run_swan_tool  # noqa: E402,F401
from .simulation import run_swmm_tool  # noqa: E402,F401
from .simulation import run_telemac_tool  # noqa: E402,F401
from .simulation import solver  # noqa: E402,F401

# -- discovery (dataset/tool retrieval) --
# NOTE: catalog_search / catalog_fetch / qgis_discovery register at daemon
# startup via main.py's eager-import block, NOT here - importing this package
# alone deliberately leaves them out of TOOL_REGISTRY (pre-reorg behavior:
# the plain ``import trid3nt_server.tools`` surface is 190 tools).
from .discovery import discover_dataset  # noqa: E402,F401

# -- meta (web fetch, code exec, passthroughs, case utilities) --
from .meta import code_exec_tool  # noqa: E402,F401
from .meta import compose_case_report  # noqa: E402,F401
from .meta import export_case_to_qgis  # noqa: E402,F401
from .meta import import_user_layer  # noqa: E402,F401
from .meta import list_run_frames  # noqa: E402,F401
from .meta import passthroughs  # noqa: E402,F401
from .meta import spatial_input_tool  # noqa: E402,F401
from .meta import web_fetch  # noqa: E402,F401

# -- tools/ root (load-bearing chokepoints kept flat) --
from . import publish_layer  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Workflow-composer registrations (each carries its OWN @register_tool) and
# the 12-category registry meta-tools. Comments preserved from the original
# registration list.
# ---------------------------------------------------------------------------
from ..workflows import compute_impact_envelope as _compute_impact_envelope_workflow  # noqa: E402,F401 - Wave 4.11 P3: registers compute_impact_envelope (composes NSI/MS → Pelicun → postprocess into one envelope tool)
# The river-seepage COMPOSER carries its OWN @register_tool (run_model_river_seepage_scenario);
# its bridge tool above does NOT import it, so register it explicitly (mirrors the
# compute_impact_envelope composer import below). The MODFLOW-seepage verifier flagged
# this composer as never-registered - this import is the fix.
from ..workflows import model_river_seepage_scenario as _model_river_seepage_scenario  # noqa: E402,F401 - sprint-17: registers run_model_river_seepage_scenario (LLM-facing river-seepage composer)

# sprint-18 Wave-1 MODFLOW archetypes (GWF-only): the three new composers each
# carry their OWN @register_tool (LLM-facing run_model_*_scenario) and dispatch
# to the shared run_modflow_archetype_job engine surface. Importing them seeds
# TOOL_REGISTRY at startup (mirrors the river-seepage composer import above). The
# archetype run-tool itself is NOT @register_tool'd (the composers are the surface).
from ..workflows import model_sustainable_yield_scenario as _model_sustainable_yield_scenario  # noqa: E402,F401 - sprint-18 Wave-1: registers run_model_sustainable_yield_scenario (MODFLOW pumping-drawdown composer)
from ..workflows import model_mine_dewatering_scenario as _model_mine_dewatering_scenario  # noqa: E402,F401 - sprint-18 Wave-1: registers run_model_mine_dewatering_scenario (MODFLOW pit-dewatering composer)
from ..workflows import model_regional_water_budget_scenario as _model_regional_water_budget_scenario  # noqa: E402,F401 - sprint-18 Wave-1: registers run_model_regional_water_budget_scenario (MODFLOW zonal-budget composer)

# sprint-18 Wave-2 MODFLOW archetypes (GWF-only): three more composers each carry
# their OWN @register_tool (LLM-facing run_model_*_scenario) + dispatch through the
# shared run_modflow_archetype_job. Importing them seeds TOOL_REGISTRY at startup
# (mirrors the Wave-1 imports above). The archetype run-tool is NOT @register_tool'd.
from ..workflows import model_mar_scenario as _model_mar_scenario  # noqa: E402,F401 - sprint-18 Wave-2: registers run_model_mar_scenario (MODFLOW managed-aquifer-recharge mounding composer)
from ..workflows import model_asr_scenario as _model_asr_scenario  # noqa: E402,F401 - sprint-18 Wave-2: registers run_model_asr_scenario (MODFLOW aquifer-storage-&-recovery composer)
from ..workflows import model_wetland_hydroperiod_scenario as _model_wetland_hydroperiod_scenario  # noqa: E402,F401 - sprint-18 Wave-2: registers run_model_wetland_hydroperiod_scenario (MODFLOW wetland-hydroperiod composer)
from ..workflows import model_multi_species_scenario as _model_multi_species_scenario  # noqa: E402,F401 - sprint-18 Wave-3: registers run_model_multi_species_scenario (MODFLOW N-species transport composer; ONE shared GWF + N GWT -> N per-species plumes)
from ..workflows import model_capture_zone_scenario as _model_capture_zone_scenario  # noqa: E402,F401 - sprint-18 Wave-4: registers run_model_capture_zone_scenario + run_model_wellhead_protection_scenario (MODFLOW PRT backward particle tracking -> capture-zone / WHPA vector polygon)
from ..workflows import model_saltwater_intrusion_scenario as _model_saltwater_intrusion_scenario  # noqa: E402,F401 - sprint-18 Wave-5: registers run_model_saltwater_intrusion_scenario (MODFLOW BUY variable-density GWF+GWT Henry-style saltwater wedge -> cross-section heatmap chart + transect/toe vector layer)

# fire-animation demos S5/J5: the satellite fire-animation composer carries its
# OWN @register_tool (run_model_satellite_fire_animation); import it so the
# review-gated GOES/JPSS animation workflow is in TOOL_REGISTRY at startup.
from ..workflows import model_satellite_fire_animation as _model_satellite_fire_animation  # noqa: E402,F401 - fire-animation demos S5/J5: registers run_model_satellite_fire_animation (incident lookup -> bbox+window review gate -> GOES/VIIRS per-frame imagery -> FIRMS+NIFC overlays -> publish)

# fire-demo Track A: the UNATTENDED GOES fire-animation composer carries its OWN
# @register_tool (run_model_goes_fire_animation); import it so the no-confirm-gate
# GOES animation workflow is in TOOL_REGISTRY at startup. It auto-snaps the
# requested window to the nearest available SLIDER frames and proceeds without
# parking (the sibling of model_satellite_fire_animation that does NOT review-gate).
from ..workflows import model_goes_fire_animation as _model_goes_fire_animation  # noqa: E402,F401 - fire-demo Track A: registers run_model_goes_fire_animation (auto-snap window -> GOES GeoColor+Fire Temperature per-frame imagery -> FIRMS overlay -> publish; NO confirm gate)

# GLM lightning demo: the DIRECT GOES-19 GLM Group-Energy-Density animation composer
# carries its OWN @register_tool (run_model_glm_lightning_animation); import it so the
# no-news lightning loop is in TOOL_REGISTRY at startup. It takes an AOI bbox + UTC
# window DIRECTLY (NO news/geocode/snap front-half), bins GLM GED onto the ABI 2 km
# grid per 1-min frame, bakes the purple overlay over the grayscale C02 visible base,
# and publishes a scrubbable baked loop + a separable transparent GED overlay.
from ..workflows import model_glm_lightning_animation as _model_glm_lightning_animation  # noqa: E402,F401 - GLM lightning demo: registers run_model_glm_lightning_animation (DIRECT AOI+window -> GLM GED purple overlay baked over GOES-19 C02 visible base, 1-min frames; NO news step)

# job-B5 (Wave 4.10 Stage 2): the 12-category registry + the two meta-tools
# (``list_categories`` + ``list_tools_in_category``) live alongside the rest
# of the tool surface. Importing the module fires its two ``@register_tool``
# decorators so the meta-tools are in TOOL_REGISTRY at startup; the hot set,
# allowed-set tracker, and post-hoc validator are exposed through
# ``trid3nt_server.categories`` for the server.py dispatch loop.
from .. import categories as _categories  # noqa: E402,F401

# COPY-ME authoring template (docs/authoring/writing-a-tool.md). Importing the
# module is always safe: its @register_tool call is gated behind the
# TRID3NT_ENABLE_EXAMPLE_TOOL env flag, so it registers example_bbox_area ONLY
# when a developer explicitly enables it (demo / retrieval-visibility check).
# Default = imported-but-inert, so it never pollutes the production catalog.
from . import _example_tool_template  # noqa: E402,F401 - INERT unless TRID3NT_ENABLE_EXAMPLE_TOOL is set
