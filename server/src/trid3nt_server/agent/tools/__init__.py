"""Atomic-tool registry skeleton (FR-AS-3, FR-CE-8, FR-TA-2, Decision O).

This package is the agent-service-owned surface for atomic tools (M4 substrate).
``schema`` owns ``AtomicToolMetadata`` (in ``trid3nt_contracts.tool_registry``);
``agent`` owns the registry that collects the decorated functions at import
time and the cache shim that mediates external-API calls (see ``.cache``).
The ``qgis_process`` pass-through tool lives in ``.passthroughs``.

How registration works:

    from trid3nt_contracts.tool_registry import AtomicToolMetadata
    from trid3nt_server.agent.tools import register_tool

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
      for diagnostics (`"trid3nt_server.agent.tools.meta.passthroughs"` etc.).
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
# Importing ``trid3nt_server.agent.tools`` populates ``TOOL_REGISTRY`` with EVERY
# atomic tool the agent service supports: each module below carries at least
# one ``@register_tool`` decorator that fires at import time, so any
# registration-time ``ValidationError`` / ``ToolRegistrationError`` surfaces
# at startup rather than first use. The block is EXPLICIT (no pkgutil walk),
# sorted, and grouped by subpackage; regenerate it when adding a tool module.
# Per-tool rationale lives in each module's docstring.
# ---------------------------------------------------------------------------

# -- fetchers/weather --
from .fetchers.weather.fetch_airnow_air_quality import fetch_airnow_air_quality  # noqa: E402,F401
from .fetchers.weather.fetch_asos_metar import fetch_asos_metar  # noqa: E402,F401
from .fetchers.weather.fetch_glm_lightning import fetch_glm_lightning  # noqa: E402,F401
from .fetchers.weather.fetch_hrrr_forecast import fetch_hrrr_forecast  # noqa: E402,F401
from .fetchers.weather.fetch_hrrr_smoke import fetch_hrrr_smoke  # noqa: E402,F401
from .fetchers.weather.fetch_mrms_qpe import fetch_mrms_qpe  # noqa: E402,F401
from .fetchers.weather.fetch_nexrad_reflectivity import fetch_nexrad_reflectivity  # noqa: E402,F401
from .fetchers.weather.fetch_nws_alerts_conus import fetch_nws_alerts_conus  # noqa: E402,F401
from .fetchers.weather.fetch_nws_event import fetch_nws_event  # noqa: E402,F401
from .fetchers.weather.fetch_openaq_measurements import fetch_openaq_measurements  # noqa: E402,F401
from .fetchers.weather.fetch_raws_weather import fetch_raws_weather  # noqa: E402,F401
from .fetchers.weather.fetch_storm_events_db import fetch_storm_events_db  # noqa: E402,F401
from .fetchers.weather.fetch_storm_tracks import fetch_storm_tracks  # noqa: E402,F401

# -- fetchers/hydrology --
from .fetchers.hydrology.fetch_cama_flood_discharge import fetch_cama_flood_discharge  # noqa: E402,F401
# V&V wave (ADR 0021, lane C): observed flood-validation data fetchers.
from .fetchers.hydrology.fetch_flood_extent_observation import fetch_flood_extent_observation  # noqa: E402,F401
from .fetchers.hydrology.fetch_high_water_marks import fetch_high_water_marks  # noqa: E402,F401
from .fetchers.hydrology.fetch_jrc_global_surface_water import fetch_jrc_global_surface_water  # noqa: E402,F401
from .fetchers.hydrology.fetch_nhd_waterbodies import fetch_nhd_waterbodies  # noqa: E402,F401
from .fetchers.hydrology.fetch_nhdplus_nldi_navigate import fetch_nhdplus_nldi_navigate  # noqa: E402,F401
from .fetchers.hydrology.fetch_noaa_nwm_streamflow import fetch_noaa_nwm_streamflow  # noqa: E402,F401
from .fetchers.hydrology.fetch_nwi_wetlands import fetch_nwi_wetlands  # noqa: E402,F401
from .fetchers.hydrology.fetch_nws_river_forecast import fetch_nws_river_forecast  # noqa: E402,F401
from .fetchers.hydrology.fetch_river_geometry import fetch_river_geometry  # noqa: E402,F401
from .fetchers.hydrology.fetch_usgs_groundwater_levels import fetch_usgs_groundwater_levels  # noqa: E402,F401
from .fetchers.hydrology.fetch_usgs_nwis_gauges import fetch_usgs_nwis_gauges  # noqa: E402,F401
from .fetchers.hydrology.fetch_usgs_water_quality import fetch_usgs_water_quality  # noqa: E402,F401

# -- fetchers/ocean --
from .fetchers.ocean.fetch_gtsm_tide_surge import fetch_gtsm_tide_surge  # noqa: E402,F401
from .fetchers.ocean.fetch_noaa_coops_currents import fetch_noaa_coops_currents  # noqa: E402,F401
from .fetchers.ocean.fetch_noaa_coops_tides import fetch_noaa_coops_tides  # noqa: E402,F401
from .fetchers.ocean.fetch_noaa_slr_confidence import fetch_noaa_slr_confidence  # noqa: E402,F401
from .fetchers.ocean.fetch_noaa_slr_marsh import fetch_noaa_slr_marsh  # noqa: E402,F401
from .fetchers.ocean.fetch_noaa_slr_scenarios import fetch_noaa_slr_scenarios  # noqa: E402,F401
from .fetchers.ocean.fetch_noaa_sst import fetch_noaa_sst  # noqa: E402,F401
from .fetchers.ocean.fetch_topobathy import fetch_topobathy  # noqa: E402,F401

# -- fetchers/terrain --
from .fetchers.terrain.fetch_3dep_extra import fetch_3dep_extra  # noqa: E402,F401
from .fetchers.terrain.fetch_copernicus_dem import fetch_copernicus_dem  # noqa: E402,F401
from .fetchers.terrain.fetch_dem import fetch_dem  # noqa: E402,F401
from .fetchers.terrain.fetch_esri_landcover_10m import fetch_esri_landcover_10m  # noqa: E402,F401
from .fetchers.terrain.fetch_landcover import fetch_landcover  # noqa: E402,F401

# -- fetchers/imagery --
from .fetchers.imagery.fetch_goes_active_fire import fetch_goes_active_fire  # noqa: E402,F401
from .fetchers.imagery.fetch_goes_animation import fetch_goes_animation  # noqa: E402,F401
from .fetchers.imagery.fetch_goes_archive_animation import fetch_goes_archive_animation  # noqa: E402,F401
from .fetchers.imagery.fetch_goes_satellite import fetch_goes_satellite  # noqa: E402,F401
from .fetchers.imagery.fetch_landsat_imagery import fetch_landsat_imagery  # noqa: E402,F401
from .fetchers.imagery.fetch_naip import fetch_naip  # noqa: E402,F401
from .fetchers.imagery.fetch_sentinel1_sar import fetch_sentinel1_sar  # noqa: E402,F401
from .fetchers.imagery.fetch_sentinel2_truecolor import fetch_sentinel2_truecolor  # noqa: E402,F401
from .fetchers.imagery.fetch_viirs_day_fire import fetch_viirs_day_fire  # noqa: E402,F401

# -- fetchers/climate --
from .fetchers.climate.fetch_chirps_precipitation import fetch_chirps_precipitation  # noqa: E402,F401
from .fetchers.climate.fetch_climate_normals import fetch_climate_normals  # noqa: E402,F401
from .fetchers.climate.fetch_era5_reanalysis import fetch_era5_reanalysis  # noqa: E402,F401
from .fetchers.climate.fetch_gridmet import fetch_gridmet  # noqa: E402,F401
from .fetchers.climate.fetch_modis_lst import fetch_modis_lst  # noqa: E402,F401
from .fetchers.climate.fetch_us_drought_monitor import fetch_us_drought_monitor  # noqa: E402,F401
from .fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: E402,F401

# -- fetchers/biodiversity --
from .fetchers.biodiversity.fetch_ebird_observations import fetch_ebird_observations  # noqa: E402,F401
from .fetchers.biodiversity.fetch_gbif_occurrences import fetch_gbif_occurrences  # noqa: E402,F401
from .fetchers.biodiversity.fetch_inaturalist_observations import fetch_inaturalist_observations  # noqa: E402,F401
from .fetchers.biodiversity.fetch_iucn_red_list_range import fetch_iucn_red_list_range  # noqa: E402,F401
from .fetchers.biodiversity.fetch_mobi import fetch_mobi  # noqa: E402,F401
from .fetchers.biodiversity.fetch_movebank_tracks import fetch_movebank_tracks  # noqa: E402,F401
from .fetchers.biodiversity.fetch_wdpa_protected_areas import fetch_wdpa_protected_areas  # noqa: E402,F401

# -- fetchers/socioeconomic --
from .fetchers.socioeconomic.fetch_administrative_boundaries import fetch_administrative_boundaries  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_buildings import fetch_buildings  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_cdc_svi import fetch_cdc_svi  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_census_acs import fetch_census_acs  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_epa_ejscreen import fetch_epa_ejscreen  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_field_boundaries import fetch_field_boundaries  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_ghsl_population import fetch_ghsl_population  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_hrsl_population import fetch_hrsl_population  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_lehd_jobs import fetch_lehd_jobs  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_overpass_pois import fetch_overpass_pois  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_population import fetch_population  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_roads_osm import fetch_roads_osm  # noqa: E402,F401
from .fetchers.socioeconomic.fetch_usace_nsi import fetch_usace_nsi  # noqa: E402,F401
from .fetchers.socioeconomic.geocode_location import geocode_location  # noqa: E402,F401

# -- fetchers/hazard --
from .fetchers.hazard.fetch_epa_frs_facilities import fetch_epa_frs_facilities  # noqa: E402,F401
from .fetchers.hazard.fetch_fault_sources import fetch_fault_sources  # noqa: E402,F401
from .fetchers.hazard.fetch_fema_nfhl_zones import fetch_fema_nfhl_zones  # noqa: E402,F401
from .fetchers.hazard.fetch_firms_active_fire import fetch_firms_active_fire  # noqa: E402,F401
from .fetchers.hazard.fetch_hifld_critical_infrastructure import fetch_hifld_critical_infrastructure  # noqa: E402,F401
from .fetchers.hazard.fetch_hifld_transmission_lines import fetch_hifld_transmission_lines  # noqa: E402,F401
from .fetchers.hazard.fetch_landfire_fuels import fetch_landfire_fuels  # noqa: E402,F401
from .fetchers.hazard.fetch_mtbs_burn_severity import fetch_mtbs_burn_severity  # noqa: E402,F401
from .fetchers.hazard.fetch_nifc_fire_perimeters import fetch_nifc_fire_perimeters  # noqa: E402,F401
from .fetchers.hazard.fetch_openfema_disasters import fetch_openfema_disasters  # noqa: E402,F401
from .fetchers.hazard.fetch_tsunami_events import fetch_tsunami_events  # noqa: E402,F401
from .fetchers.hazard.fetch_usace_dams import fetch_usace_dams  # noqa: E402,F401
from .fetchers.hazard.fetch_usace_levees import fetch_usace_levees  # noqa: E402,F401
from .fetchers.hazard.fetch_usfs_canopy_fuels import fetch_usfs_canopy_fuels  # noqa: E402,F401
from .fetchers.hazard.fetch_usgs_earthquakes import fetch_usgs_earthquakes  # noqa: E402,F401
from .fetchers.hazard.fetch_usgs_volcano_alerts import fetch_usgs_volcano_alerts  # noqa: E402,F401
from .fetchers.hazard.fetch_wfigs_incident import fetch_wfigs_incident  # noqa: E402,F401

# -- fetchers/soil --
from .fetchers.soil.fetch_gcn250_curve_numbers import fetch_gcn250_curve_numbers  # noqa: E402,F401
from .fetchers.soil.fetch_snotel_snow import fetch_snotel_snow  # noqa: E402,F401
from .fetchers.soil.fetch_soilgrids import fetch_soilgrids  # noqa: E402,F401
from .fetchers.soil.fetch_statsgo_soils import fetch_statsgo_soils  # noqa: E402,F401

# -- processing (compute / clip / extract / vector-edit / charts) --
from .processing.aggregate_claims_across_sources import aggregate_claims_across_sources  # noqa: E402,F401
from .processing.clip_raster_to_bbox import clip_raster_to_bbox  # noqa: E402,F401
from .processing.clip_raster_to_polygon import clip_raster_to_polygon  # noqa: E402,F401
from .processing.compute_aspect import compute_aspect  # noqa: E402,F401
from .processing.compute_blended_composite import compute_blended_composite  # noqa: E402,F401
from .processing.compute_building_density import compute_building_density  # noqa: E402,F401
from .processing.compute_canopy_height import compute_canopy_height  # noqa: E402,F401
from .processing.compute_change_detection import compute_change_detection  # noqa: E402,F401
from .processing.compute_colored_relief import compute_colored_relief  # noqa: E402,F401
from .processing.compute_contours import compute_contours  # noqa: E402,F401
from .processing.compute_cross_section import compute_cross_section  # noqa: E402,F401
from .processing.compute_exposure_summary import compute_exposure_summary  # noqa: E402,F401
from .processing.compute_flood_depth_damage import compute_flood_depth_damage  # noqa: E402,F401
# V&V wave (ADR 0021, lane B): flood-extent skill (raster/vector confusion).
from .processing.compute_flood_extent_skill import compute_flood_extent_skill  # noqa: E402,F401
from .processing.compute_hillshade import compute_hillshade  # noqa: E402,F401
from .processing.compute_home_range_kde import compute_home_range_kde  # noqa: E402,F401
from .processing.compute_idf_curve import compute_idf_curve  # noqa: E402,F401
from .processing.compute_impervious_surface import compute_impervious_surface  # noqa: E402,F401
from .processing.compute_layer_bounds import compute_layer_bounds  # noqa: E402,F401
from .processing.compute_model_residuals import compute_model_residuals  # noqa: E402,F401
from .processing.compute_movement_trajectory import compute_movement_trajectory  # noqa: E402,F401
from .processing.compute_ndvi import compute_ndvi  # noqa: E402,F401
from .processing.compute_sediment_yield import compute_sediment_yield  # noqa: E402,F401
# V&V wave (ADR 0021, lane B): model-fit skill-metrics wrap (spotpy).
from .processing.compute_skill_metrics import compute_skill_metrics  # noqa: E402,F401
from .processing.compute_slope import compute_slope  # noqa: E402,F401
from .processing.compute_urban_heat_island import compute_urban_heat_island  # noqa: E402,F401
from .processing.compute_zonal_statistics import compute_zonal_statistics  # noqa: E402,F401
from .processing.delineate_watershed import delineate_watershed  # noqa: E402,F401
from .processing.digitize_water_body import digitize_water_body  # noqa: E402,F401
from .processing.enhance_satellite_image import enhance_satellite_image  # noqa: E402,F401
from .processing.extract_landcover_class import extract_landcover_class  # noqa: E402,F401
# V&V wave (ADR 0021, lane C): model-vs-observation pairing primitive.
from .processing.extract_model_at_observations import extract_model_at_observations  # noqa: E402,F401
from .processing.extract_stream_network import extract_stream_network  # noqa: E402,F401
from .processing.extract_timeseries_at_point import extract_timeseries_at_point  # noqa: E402,F401
from .processing.charts.generate_choropleth_legend import generate_choropleth_legend  # noqa: E402,F401
from .processing.charts.generate_damage_distribution import generate_damage_distribution  # noqa: E402,F401
from .processing.charts.generate_histogram import generate_histogram  # noqa: E402,F401
from .processing.charts.generate_time_series import generate_time_series  # noqa: E402,F401
from .processing.query_point_hazard import query_point_hazard  # noqa: E402,F401
# DuckDB spatial-query fold (Phase B): ONE read-only SQL surface replaces the
# three analytical Q&A tools (summarize_layer_statistics /
# count_features_above_threshold / aggregate_property_within_zone).
from .processing.spatial_query import spatial_query  # noqa: E402,F401

# -- simulation (engine bridges, model_* engines, solver seam) --
# V&V wave (ADR 0021, lane A): per-engine run-diagnostics dispatcher (folds
# the 5 per-engine readers into one registered tool; the internal per-engine
# parser modules under .simulation.diagnostics are NOT registered).
from .simulation.diagnostics import read_run_diagnostics  # noqa: E402,F401
from .simulation.model_debris_flow import model_debris_flow  # noqa: E402,F401
# engine-door refactor (ELMFIRE slice): the model_fire_spread engine tool is
# DELETED from tools/simulation/. The ELMFIRE engine tool is now the
# elmfire_fire_spread TEMPLATE (engine="elmfire", tier="template") registered in
# workflows/elmfire/fire_spread/fire_spread.py (imported below); the run_elmfire
# door lists + gate-expands it.
from .simulation.pelicun.postprocess_pelicun import postprocess_pelicun  # noqa: E402,F401
# engine-door refactor (LANDLAB slice): the run_landlab_susceptibility thin
# wrapper is DELETED. The Landlab engine tool is now the landlab_susceptibility
# TEMPLATE (engine="landlab", tier="template") registered in
# workflows/landlab/susceptibility/susceptibility.py (imported below); the
# run_landlab door lists + gate-expands it.
# engine-door refactor: run_modflow_job (single spill) + run_river_seepage_job
# are DELETED / UNREGISTERED. The single-species spill folded into the
# modflow_contaminant_plume template; run_river_seepage_job is now the
# unregistered internal engine surface the modflow_river_seepage template imports.
# engine-door refactor (OPENQUAKE slice): the run_seismic_hazard_psha thin
# wrapper is DELETED. The OpenQuake engine tool is now the openquake_psha
# TEMPLATE (engine="openquake", tier="template") registered in
# workflows/openquake/psha/psha.py (imported below); the run_openquake door
# lists + gate-expands it.
# engine-door refactor (PELICUN slice) + PELICUN fold: the
# run_pelicun_damage_assessment atomic tool + the run_pelicun_with_buildings
# composer are now ONE pelicun_damage_assessment TEMPLATE (engine="pelicun",
# tier="template") under workflows/pelicun/damage_assessment/ (imported below).
# The former with-buildings composer folded into that template's bbox AUTO-FETCH
# input mode (assets_uri absent + bbox -> auto-fetch a building-density
# inventory). The run_pelicun door lists + gate-expands it. postprocess_pelicun
# (above) + compute_impact_envelope (below) STAY general.
# engine-door refactor (SWAN slice): the run_swan_waves thin wrapper is DELETED.
# The SWAN engine tool is now the swan_wave_field TEMPLATE (engine="swan",
# tier="template") registered in workflows/swan/wave_field/wave_field.py (imported
# below); the run_swan door lists + gate-expands it.
# engine-door refactor (SWMM + TELEMAC slices): the run_swmm_urban_flood and
# run_telemac thin wrappers are DELETED. The SWMM engine tool is now the
# swmm_urban_flood TEMPLATE (imported below); the TELEMAC engine tool is now the
# telemac_river_dye TEMPLATE (engine="telemac", tier="template") registered in
# workflows/telemac/river_dye/river_dye.py (imported below); the run_swmm /
# run_telemac doors list + gate-expand them. The freed run_telemac name is
# reused by the TELEMAC door.
# V&V wave (ADR 0021, lane D): derive-not-mutate parameter setters (write a
# child deck/setup, leave the parent immutable).
from .simulation.modflow.set_modflow_parameters import set_modflow_parameters  # noqa: E402,F401
from .simulation.sfincs.set_sfincs_parameters import set_sfincs_parameters  # noqa: E402,F401
from .simulation.swmm.set_swmm_parameters import set_swmm_parameters  # noqa: E402,F401
from .simulation.telemac.set_telemac_parameters import set_telemac_parameters  # noqa: E402,F401 - relocated beside the run_telemac door (engine-door refactor, TELEMAC slice); stays tier=general
from .simulation.solver import solver  # noqa: E402,F401
# -- engine doors (engine-door refactor: read-only concierge that lists +
# gate-expands its engine's tier=template members; executes nothing). MODFLOW
# pilot. Registry-driven: adding a template folder tagged engine+tier=template
# makes it appear in the door's listing with zero door changes.
from .simulation.modflow.run_modflow.run_modflow import run_modflow  # noqa: E402,F401
from .simulation.geoclaw.run_geoclaw.run_geoclaw import run_geoclaw  # noqa: E402,F401 - GeoClaw run-up door (engine-door refactor, GEOCLAW slice)
from .simulation.sfincs.run_sfincs.run_sfincs import run_sfincs  # noqa: E402,F401 - SFINCS flood door (engine-door refactor, SFINCS slice)
from .simulation.swmm.run_swmm.run_swmm import run_swmm  # noqa: E402,F401 - SWMM urban-drainage door (engine-door refactor, SWMM slice)
from .simulation.telemac.run_telemac.run_telemac import run_telemac  # noqa: E402,F401 - TELEMAC river-transport door (engine-door refactor, TELEMAC slice; reuses the freed run_telemac name)
from .simulation.swan.run_swan.run_swan import run_swan  # noqa: E402,F401 - SWAN nearshore-wave door (engine-door refactor, SWAN slice)
from .simulation.landlab.run_landlab.run_landlab import run_landlab  # noqa: E402,F401 - Landlab surface-process door (engine-door refactor, LANDLAB slice)
from .simulation.openquake.run_openquake.run_openquake import run_openquake  # noqa: E402,F401 - OpenQuake seismic-hazard door (engine-door refactor, OPENQUAKE slice)
from .simulation.elmfire.run_elmfire.run_elmfire import run_elmfire  # noqa: E402,F401 - ELMFIRE wildfire-spread door (engine-door refactor, ELMFIRE slice)
from .simulation.pelicun.run_pelicun.run_pelicun import run_pelicun  # noqa: E402,F401 - Pelicun damage/loss door (engine-door refactor, PELICUN slice)

# -- discovery (dataset/tool retrieval) --
# NOTE: search_data_catalog / fetch_from_catalog / qgis_discovery register at
# daemon startup via main.py's eager-import block, NOT here - importing this
# package alone deliberately leaves them out of TOOL_REGISTRY (pre-reorg
# behavior: the plain ``import trid3nt_server.agent.tools`` surface is 190 tools,
# 191 after ADR 0019 added search_spatial_functions here).
from .search.search_tools import search_tools  # noqa: E402,F401
from .search.search_spatial_functions import search_spatial_functions  # noqa: E402,F401

# -- meta (web fetch, code exec, passthroughs, case utilities) --
from .meta.code_exec_tool import code_exec_tool  # noqa: E402,F401
from .meta.compose_case_report import compose_case_report  # noqa: E402,F401
from .meta.export_case_to_qgis import export_case_to_qgis  # noqa: E402,F401
from .meta.import_user_layer import import_user_layer  # noqa: E402,F401
from .meta.list_run_frames import list_run_frames  # noqa: E402,F401
from .meta.passthroughs import passthroughs  # noqa: E402,F401
from .meta.spatial_input_tool import spatial_input_tool  # noqa: E402,F401
from .search.web_fetch import web_fetch  # noqa: E402,F401

# -- tools/ root (load-bearing chokepoints kept flat) --
# Aliased so the registration-side-effect import of the inner module does NOT
# rebind the ``tools.publish_layer`` attribute (which must stay the subpackage
# so ``trid3nt_server.agent.tools.publish_layer.publish_layer.<attr>`` resolves).
from .publish_layer import publish_layer as _publish_layer_reg  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Workflow-composer registrations (each carries its OWN @register_tool) and
# the 12-category registry meta-tools. Comments preserved from the original
# registration list.
# ---------------------------------------------------------------------------
from ..workflows.pelicun.compute_impact_envelope import compute_impact_envelope as _compute_impact_envelope_workflow  # noqa: E402,F401 - Wave 4.11 P3: registers compute_impact_envelope (composes NSI/MS → Pelicun → postprocess into one envelope tool)
# engine-door refactor (MODFLOW pilot): the MODFLOW composer family is now a set
# of engine TEMPLATES (engine="modflow", tier="template"), one folder per template
# under workflows/modflow/<template>/<template>.py. Importing each module fires its
# @register_tool so the template lands in TOOL_REGISTRY at startup; the templates
# are EXCLUDED from the default retrieval pool and surfaced only by the run_modflow
# door's gate expansion. run_modflow_archetype_job / run_modflow_multi_species_job
# / run_river_seepage_job are the UNREGISTERED internal engine surfaces the
# templates call directly (not imported here).
from ..workflows.modflow.contaminant_plume.contaminant_plume import modflow_contaminant_plume as _modflow_contaminant_plume  # noqa: E402,F401 - FOLD of run_modflow_job + run_model_multi_species_scenario (single OR multi species -> plumes[])
from ..workflows.modflow.river_seepage.river_seepage import modflow_river_seepage as _modflow_river_seepage  # noqa: E402,F401 - FOLD of run_model_river_seepage_scenario + run_river_seepage_job
from ..workflows.modflow.sustainable_yield.sustainable_yield import modflow_sustainable_yield as _modflow_sustainable_yield  # noqa: E402,F401
from ..workflows.modflow.mine_dewatering.mine_dewatering import modflow_mine_dewatering as _modflow_mine_dewatering  # noqa: E402,F401
from ..workflows.modflow.regional_water_budget.regional_water_budget import modflow_regional_water_budget as _modflow_regional_water_budget  # noqa: E402,F401
from ..workflows.modflow.managed_recharge.managed_recharge import modflow_managed_recharge as _modflow_managed_recharge  # noqa: E402,F401
from ..workflows.modflow.asr.asr import modflow_asr as _modflow_asr  # noqa: E402,F401
from ..workflows.modflow.wetland_hydroperiod.wetland_hydroperiod import modflow_wetland_hydroperiod as _modflow_wetland_hydroperiod  # noqa: E402,F401
from ..workflows.modflow.capture_zone.capture_zone import modflow_capture_zone as _modflow_capture_zone  # noqa: E402,F401 - PRT capture zone
from ..workflows.modflow.wellhead_protection.wellhead_protection import modflow_wellhead_protection as _modflow_wellhead_protection  # noqa: E402,F401 - EPA WHPA (split from capture_zone; shared composer)
from ..workflows.modflow.saltwater_intrusion.saltwater_intrusion import modflow_saltwater_intrusion as _modflow_saltwater_intrusion  # noqa: E402,F401 - BUY variable-density Henry-style wedge
# engine-door refactor (SWMM slice): the SWMM urban-flood engine tool is now the
# swmm_urban_flood TEMPLATE (engine="swmm", tier="template") - one folder under
# workflows/swmm/urban_flood/. Importing it fires its @register_tool so the
# template lands in TOOL_REGISTRY at startup; it is EXCLUDED from the default
# retrieval pool and surfaced only by the run_swmm door's gate expansion. The
# model_urban_flood_swmm composition body stays the internal engine surface the
# template calls.
from ..workflows.swmm.urban_flood.urban_flood import swmm_urban_flood as _swmm_urban_flood  # noqa: E402,F401 - RENAME of run_swmm_urban_flood (engine=swmm, tier=template)
# engine-door refactor (TELEMAC slice): the TELEMAC river-dye engine tool is now
# the telemac_river_dye TEMPLATE (engine="telemac", tier="template") - one folder
# under workflows/telemac/river_dye/. Importing it fires its @register_tool so the
# template lands in TOOL_REGISTRY at startup; it is EXCLUDED from the default
# retrieval pool and surfaced only by the run_telemac door's gate expansion. The
# model_river_dye_release_scenario composition body stays the internal engine
# surface the template calls; workflows/telemac/run_telemac.py stays the local
# solve seam (name flip: the door took the run_telemac name, the template submits
# the solver).
from ..workflows.telemac.river_dye.river_dye import telemac_river_dye as _telemac_river_dye  # noqa: E402,F401 - NAME FLIP of run_telemac (engine=telemac, tier=template)
# engine-door refactor (GEOCLAW slice): the GeoClaw run-up engine tool is now the
# geoclaw_inundation TEMPLATE (engine="geoclaw", tier="template") - one folder
# under workflows/geoclaw/inundation/. Importing it fires its @register_tool so
# the template lands in TOOL_REGISTRY at startup; it is EXCLUDED from the default
# retrieval pool and surfaced only by the run_geoclaw door's gate expansion. The
# model_dambreak_geoclaw_scenario composition body stays the internal engine
# surface the template calls.
from ..workflows.geoclaw.inundation.inundation import geoclaw_inundation as _geoclaw_inundation  # noqa: E402,F401 - RENAME of run_geoclaw_inundation (engine=geoclaw, tier=template)
# engine-door refactor (SWAN slice): the SWAN standalone wave-field engine tool is
# now the swan_wave_field TEMPLATE (engine="swan", tier="template") - one folder
# under workflows/swan/wave_field/. Importing it fires its @register_tool so the
# template lands in TOOL_REGISTRY at startup; it is EXCLUDED from the default
# retrieval pool and surfaced only by the run_swan door's gate expansion. The
# model_wave_scenario composition body stays the internal engine surface the
# template calls.
from ..workflows.swan.wave_field.wave_field import swan_wave_field as _swan_wave_field  # noqa: E402,F401 - RENAME of run_swan_waves (engine=swan, tier=template)
# engine-door refactor (LANDLAB slice): the Landlab surface-process engine tool is
# now the landlab_susceptibility TEMPLATE (engine="landlab", tier="template") - one
# folder under workflows/landlab/susceptibility/. Importing it fires its
# @register_tool so the template lands in TOOL_REGISTRY at startup; it is EXCLUDED
# from the default retrieval pool and surfaced only by the run_landlab door's gate
# expansion. The model_landslide_scenario composition body stays the internal
# engine surface the template calls; workflows/landlab/run_landlab.py stays the
# solver build/stage seam (distinct module from the run_landlab door).
from ..workflows.landlab.susceptibility.susceptibility import landlab_susceptibility as _landlab_susceptibility  # noqa: E402,F401 - RENAME of run_landlab_susceptibility (engine=landlab, tier=template)
# engine-door refactor (OPENQUAKE slice): the OpenQuake PSHA engine tool is now
# the openquake_psha TEMPLATE (engine="openquake", tier="template") - one folder
# under workflows/openquake/psha/. Importing it fires its @register_tool so the
# template lands in TOOL_REGISTRY at startup; it is EXCLUDED from the default
# retrieval pool and surfaced only by the run_openquake door's gate expansion.
# The model_seismic_hazard_scenario composition body stays the internal engine
# surface the template calls.
from ..workflows.openquake.psha.psha import openquake_psha as _openquake_psha  # noqa: E402,F401 - RENAME of run_seismic_hazard_psha (engine=openquake, tier=template)
# engine-door refactor (ELMFIRE slice): the ELMFIRE fire-spread engine tool is
# now the elmfire_fire_spread TEMPLATE (engine="elmfire", tier="template") - one
# folder under workflows/elmfire/fire_spread/. Importing it fires its
# @register_tool so the template lands in TOOL_REGISTRY at startup; it is
# EXCLUDED from the default retrieval pool and surfaced only by the run_elmfire
# door's gate expansion. The model_fire_spread_scenario composition body stays
# the internal engine surface the template calls; workflows/elmfire/run_elmfire.py
# stays the solver build/stage seam (distinct module from the run_elmfire door).
from ..workflows.elmfire.fire_spread.fire_spread import elmfire_fire_spread as _elmfire_fire_spread  # noqa: E402,F401 - RENAME of model_fire_spread (engine=elmfire, tier=template)
# engine-door refactor (PELICUN slice) + PELICUN fold: the Pelicun damage/loss
# engine is now ONE pelicun_damage_assessment TEMPLATE (engine="pelicun",
# tier="template") under workflows/pelicun/damage_assessment/. Importing it fires
# its @register_tool so the template lands in TOOL_REGISTRY at startup; it is
# EXCLUDED from the default retrieval pool and surfaced only by the run_pelicun
# door's gate expansion. The former pelicun_damage_with_buildings composer folded
# into this template's bbox AUTO-FETCH input mode (one tool, two input modes).
# compute_impact_envelope (a compute_* composer) and postprocess_pelicun STAY
# general, NOT templates.
from ..workflows.pelicun.damage_assessment.damage_assessment import pelicun_damage_assessment as _pelicun_damage_assessment  # noqa: E402,F401 - FOLD of run_pelicun_damage_assessment + run_pelicun_with_buildings (engine=pelicun, tier=template; explicit assets_uri OR bbox auto-fetch)

# fire-animation demos S5/J5: the satellite fire-animation composer carries its
# OWN @register_tool (run_model_satellite_fire_animation); import it so the
# review-gated GOES/JPSS animation workflow is in TOOL_REGISTRY at startup.
from ..workflows.shared.model_satellite_fire_animation import model_satellite_fire_animation as _model_satellite_fire_animation  # noqa: E402,F401 - fire-animation demos S5/J5: registers run_model_satellite_fire_animation (incident lookup -> bbox+window review gate -> GOES/VIIRS per-frame imagery -> FIRMS+NIFC overlays -> publish)

# fire-demo Track A: the UNATTENDED GOES fire-animation composer carries its OWN
# @register_tool (run_model_goes_fire_animation); import it so the no-confirm-gate
# GOES animation workflow is in TOOL_REGISTRY at startup. It auto-snaps the
# requested window to the nearest available SLIDER frames and proceeds without
# parking (the sibling of model_satellite_fire_animation that does NOT review-gate).
from ..workflows.shared.model_goes_fire_animation import model_goes_fire_animation as _model_goes_fire_animation  # noqa: E402,F401 - fire-demo Track A: registers run_model_goes_fire_animation (auto-snap window -> GOES GeoColor+Fire Temperature per-frame imagery -> FIRMS overlay -> publish; NO confirm gate)

# GLM lightning demo: the DIRECT GOES-19 GLM Group-Energy-Density animation composer
# carries its OWN @register_tool (run_model_glm_lightning_animation); import it so the
# no-news lightning loop is in TOOL_REGISTRY at startup. It takes an AOI bbox + UTC
# window DIRECTLY (NO news/geocode/snap front-half), bins GLM GED onto the ABI 2 km
# grid per 1-min frame, bakes the purple overlay over the grayscale C02 visible base,
# and publishes a scrubbable baked loop + a separable transparent GED overlay.
from ..workflows.shared.model_glm_lightning_animation import model_glm_lightning_animation as _model_glm_lightning_animation  # noqa: E402,F401 - GLM lightning demo: registers run_model_glm_lightning_animation (DIRECT AOI+window -> GLM GED purple overlay baked over GOES-19 C02 visible base, 1-min frames; NO news step)

# job-B5 (Wave 4.10 Stage 2): the 12-category registry + the two meta-tools
# (``list_categories`` + ``list_tools_in_category``) live alongside the rest
# of the tool surface. Importing the module fires its two ``@register_tool``
# decorators so the meta-tools are in TOOL_REGISTRY at startup; the hot set,
# allowed-set tracker, and post-hoc validator are exposed through
# ``trid3nt_server.agent.categories`` for the server.py dispatch loop.
from .. import categories as _categories  # noqa: E402,F401

# COPY-ME authoring template (docs/authoring/writing-a-tool.md). Importing the
# module is always safe: its @register_tool call is gated behind the
# TRID3NT_ENABLE_EXAMPLE_TOOL env flag, so it registers example_bbox_area ONLY
# when a developer explicitly enables it (demo / retrieval-visibility check).
# Default = imported-but-inert, so it never pollutes the production catalog.
from . import _example_tool_template  # noqa: E402,F401 - INERT unless TRID3NT_ENABLE_EXAMPLE_TOOL is set
