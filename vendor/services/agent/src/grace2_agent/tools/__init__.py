"""Atomic-tool registry skeleton (FR-AS-3, FR-CE-8, FR-TA-2, Decision O).

This package is the agent-service-owned surface for atomic tools (M4 substrate).
``schema`` owns ``AtomicToolMetadata`` (in ``grace2_contracts.tool_registry``);
``agent`` owns the registry that collects the decorated functions at import
time and the cache shim that mediates external-API calls (see ``.cache``).
The ``qgis_process`` pass-through tool lives in ``.passthroughs``.

How registration works:

    from grace2_contracts.tool_registry import AtomicToolMetadata
    from grace2_agent.tools import register_tool

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

from grace2_contracts.tool_registry import AtomicToolMetadata

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
      for diagnostics (`"grace2_agent.tools.passthroughs"` etc.).
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
# Importing ``grace2_agent.tools`` should populate ``TOOL_REGISTRY`` with
# every atomic tool the agent service supports. Submodules are imported here
# so their import-time ``@register_tool`` calls fire even if no other code
# references them. Keep this list narrow: only submodules whose tools should
# always be available at startup belong here.
# ---------------------------------------------------------------------------
from . import passthroughs  # noqa: E402,F401 - registers qgis_process
from . import compute_colored_relief  # noqa: E402,F401 - job-0080: registers compute_colored_relief
from . import compute_slope  # noqa: E402,F401 - job-0081: registers compute_slope
from . import compute_aspect  # noqa: E402,F401 - job-0082: registers compute_aspect
from . import compute_zonal_statistics  # noqa: E402,F401 - job-0083: registers compute_zonal_statistics
from . import analyze_affected_fields  # noqa: E402,F401 - ftw-affected-fields demo: registers analyze_affected_fields (which farm fields a MODFLOW plume reaches; intersects the plume COG against FTW/fiboa field polygons via compute_zonal_statistics, joins crop_name, splits affected vs untouched at the plume detection threshold, ranks + headlines; honesty-floor 0-affected result)
from . import compute_layer_bounds  # noqa: E402,F401 - NATE 2026-06-17: registers compute_layer_bounds (fast layer-extent + fit-the-map; replaces sandbox bbox math + drives zoom-to)
from . import clip_raster_to_bbox  # noqa: E402,F401 - job-0085: registers clip_raster_to_bbox
from . import clip_raster_to_polygon  # noqa: E402,F401 - job-0106: registers clip_raster_to_polygon
from . import fetch_administrative_boundaries  # noqa: E402,F401 - job-0084: registers fetch_administrative_boundaries
from . import compute_hillshade  # noqa: E402,F401 - job-0079: registers compute_hillshade
from . import compute_blended_composite  # noqa: E402,F401 - job-0319: registers compute_blended_composite (server-side raster multiply-blend → one shaded COG; MapLibre can't multiply on the client)
from . import enhance_satellite_image  # noqa: E402,F401 - NATE 2026-06-23: registers enhance_satellite_image (OPTIONAL polish pass on ANY RGB image COG - dark-object haze/Rayleigh de-haze + gray-world white-balance + unsharp-mask + Lanczos upscale -> closer to CIRA GeoColor; pure numpy+PIL, no scipy/skimage; multiband RGB publish passthrough, no new style)
from . import compute_contours  # noqa: E402,F401 - F35: registers compute_contours (elevation contour LINES from a DEM via GDAL gdal_contour; vector LineStrings with an 'elev' attr → inline-GeoJSON line layer; pairs with fetch_dem + compute_hillshade)
from . import fetch_wdpa_protected_areas  # noqa: E402,F401 - job-0089: registers fetch_wdpa_protected_areas
from . import fetch_gbif_occurrences  # noqa: E402,F401 - job-0087: registers fetch_gbif_occurrences
from . import fetch_inaturalist_observations  # noqa: E402,F401 - job-0088: registers fetch_inaturalist_observations
from . import web_fetch  # noqa: E402,F401 - job-0092: registers web_fetch
from . import fetch_storm_events_db  # noqa: E402,F401 - job-0091: registers fetch_storm_events_db
from . import fetch_nws_event  # noqa: E402,F401 - job-0090: registers fetch_nws_event
from . import fetch_nws_alerts_conus  # noqa: E402,F401 - job-0105: registers fetch_nws_alerts_conus (CONUS-wide companion to fetch_nws_event)
from . import aggregate_claims_across_sources  # noqa: E402,F401 - job-0093: registers aggregate_claims_across_sources
from . import extract_landcover_class  # noqa: E402,F401 - job-0094: registers extract_landcover_class
from . import compute_impervious_surface  # noqa: E402,F401 - job-0095: registers compute_impervious_surface
from . import compute_building_density  # noqa: E402,F401 - job-0096: registers compute_building_density
from . import fetch_roads_osm  # noqa: E402,F401 - job-0097: registers fetch_roads_osm
from . import fetch_field_boundaries  # noqa: E402,F401 - NATE 2026-06-17: registers fetch_field_boundaries (agricultural field-boundary vectors from Fields of The World / fiboa published GeoParquet on Source Cooperative; CRS-aware bbox pushdown over HTTP range requests; inline-GeoJSON vector like roads/WDPA; FIELDS_NO_COVERAGE outside benchmark regions; on-demand global inference is a future tool)
from . import run_pelicun_damage_assessment  # noqa: E402,F401 - job-0098: registers run_pelicun_damage_assessment (Wave 1 stub; Wave 2 composer is job-0106)
from . import postprocess_pelicun  # noqa: E402,F401 - Wave 4.11 P2: registers postprocess_pelicun (aggregates Pelicun per-asset FGB → ImpactEnvelope)
from ..workflows import compute_impact_envelope as _compute_impact_envelope_workflow  # noqa: E402,F401 - Wave 4.11 P3: registers compute_impact_envelope (composes NSI/MS → Pelicun → postprocess into one envelope tool)
from . import clip_vector_to_polygon  # noqa: E402,F401 - job-0107: registers clip_vector_to_polygon
from . import fetch_goes_satellite  # noqa: E402,F401 - job-0104: registers fetch_goes_satellite (GOES-16/17/18/19 satellite imagery)
from . import fetch_nexrad_reflectivity  # noqa: E402,F401 - job-0102: registers fetch_nexrad_reflectivity (Iowa Mesonet NEXRAD WMS passthrough)
from . import fetch_mrms_qpe  # noqa: E402,F401 - job-0103: registers fetch_mrms_qpe (NOAA MRMS gauge-corrected QPE)
from . import fetch_hrsl_population  # noqa: E402,F401 - job-0112: registers fetch_hrsl_population (Meta + CIESIN HRSL persons/cell, COG via global VRT)
from . import fetch_firms_active_fire  # noqa: E402,F401 - job-0108: registers fetch_firms_active_fire (NASA FIRMS VIIRS/MODIS active-fire detections)
from . import fetch_landfire_fuels  # noqa: E402,F401 - job-0111: registers fetch_landfire_fuels (LANDFIRE LF2022 fuels & canopy rasters)
from . import fetch_gcn250_curve_numbers  # noqa: E402,F401 - job-0113: registers fetch_gcn250_curve_numbers (GCN250 global SCS curve numbers, Figshare AMC-I/II/III)
from . import fetch_mtbs_burn_severity  # noqa: E402,F401 - job-0109: registers fetch_mtbs_burn_severity (MTBS burn-severity polygons)
from . import fetch_nifc_fire_perimeters  # noqa: E402,F401 - job-0110: registers fetch_nifc_fire_perimeters (NIFC active wildfire perimeters)
from . import fetch_wfigs_incident  # noqa: E402,F401 - fire-animation demo S1/J1: registers fetch_wfigs_incident (NIFC/WFIGS named-incident lookup -> authoritative point + discovery time + AOI bbox; resolves by IncidentName so offshore islands work)
from . import fetch_goes_animation  # noqa: E402,F401 - fire-animation demo S3: registers fetch_goes_animation (GOES-18 GeoColor + Fire Temperature CIRA SLIDER multi-timestamp animation frames over a time window; ordered per-frame RGB COGs)
from . import fetch_goes_archive_animation  # noqa: E402,F401 - fire-animation demo B+C: registers fetch_goes_archive_animation (HISTORICAL Fire Temperature animation from the RAW noaa-goes18 S3 ABI-L2-MCMIPC archive for ANY past date; composites C07/C06/C05 Fire-Temp RGB; same list[LayerURI] shape as fetch_goes_animation so Track A + the scrubber consume it unchanged)
from . import fetch_goes_active_fire  # noqa: E402,F401 - fire-port: registers fetch_goes_active_fire (STANDALONE Matson-Dozier C07-vs-C13 split-window active-fire discriminator surfaced as its own atomic tool; reuses the archive module's shared band-read core + fire_hotspots composite; returns transparent RGBA hot-pixel overlay LayerURI(s))
from . import fetch_glm_lightning  # noqa: E402,F401 - tools-seed: registers fetch_glm_lightning (GOES GLM optical-lightning GROUP-ENERGY-DENSITY fetcher; bins GLM-L2-LCFA group_energy onto the ABI ~2 km grid; returns a transparent purple RGBA GED overlay LayerURI, or a step <N> animation when accumulation_window_s is set)
from . import fetch_viirs_day_fire  # noqa: E402,F401 - fire-animation demo J3: registers fetch_viirs_day_fire (JPSS/VIIRS Day Fire CIRA Polar SLIDER multi-day irregular polar-overpass animation frames; day-only; ordered per-pass RGB COGs)
from . import fetch_ebird_observations  # noqa: E402,F401 - job-0128: registers fetch_ebird_observations (Cornell Lab eBird Tier-2 recent sightings)
from . import fetch_iucn_red_list_range  # noqa: E402,F401 - job-0129: registers fetch_iucn_red_list_range (IUCN Red List Tier-2 species range info fetcher)
from . import fetch_movebank_tracks  # noqa: E402,F401 - job-0130: registers fetch_movebank_tracks (Movebank Tier-2 animal-tracking trajectories)
from . import compute_ndvi  # noqa: E402,F401 -- conservation micro-North-Star: registers compute_ndvi (Sentinel-2 L2A NDVI vegetation index via Microsoft Planetary Computer STAC; least-cloudy scene -> single-band float32 NDVI COG -1..1 with RdYlGn vegetation ramp)
from . import fetch_naip  # noqa: E402,F401 -- conservation micro-North-Star: registers fetch_naip (USDA NAIP high-res aerial RGB imagery via Microsoft Planetary Computer STAC; 3-band uint8 COG multiband passthrough base layer; US-only honest no-coverage)
from . import fetch_mobi  # noqa: E402,F401 -- conservation micro-North-Star: registers fetch_mobi (NatureServe Map of Biodiversity Importance imperiled-species richness via Microsoft Planetary Computer STAC mobi; CONUS-windowed single-band float32 COG with YlGn biodiversity ramp)
from . import compute_canopy_height  # noqa: E402,F401 -- canopy-height ML-inference tool: registers compute_canopy_height (Meta HighResCanopyHeight ViT+DPT on CPU AWS Batch; stages a NAIP/RGB COG + dispatches the canopy worker via run_solver('canopy') + publishes a canopy-height-in-metres COG with the greens canopy_height_m ramp)

from . import fetch_era5_reanalysis  # noqa: E402,F401 - job-0131: registers fetch_era5_reanalysis (Copernicus ERA5 reanalysis Tier-2 fetcher; compound-flood global substrate)
from . import fetch_gtsm_tide_surge  # noqa: E402,F401 - job-0132: registers fetch_gtsm_tide_surge (GTSM v3.0 Tier-2 coastal water-level via CDS; compound-flood coastal boundary)
from . import fetch_cama_flood_discharge  # noqa: E402,F401 - job-0133: registers fetch_cama_flood_discharge (CaMa-Flood global river discharge Tier-2 fetcher; compound-flood fluvial forcing)
from . import fetch_usace_nsi  # noqa: E402,F401 - job-A6: registers fetch_usace_nsi (USACE National Structure Inventory; preferred Pelicun assets in CONUS)
from . import fetch_fema_nfhl_zones  # noqa: E402,F401 - job-A1: registers fetch_fema_nfhl_zones (FEMA National Flood Hazard Layer regulatory flood-zone polygons; ArcGIS REST MapServer/28)
from . import fetch_usace_levees  # noqa: E402,F401 - job-A4: registers fetch_usace_levees (USACE National Levee Database critical-infrastructure polygons/lines; ArcGIS REST FeatureServer)
from . import fetch_noaa_nwm_streamflow  # noqa: E402,F401 - job-A3 (Wave 4.10): registers fetch_noaa_nwm_streamflow (NOAA National Water Model streamflow; CONUS fluvial forcing via NHDPlus reaches)
from . import fetch_usgs_nwis_gauges  # noqa: E402,F401 - job-0332 (NATE 2026-06-17): registers fetch_usgs_nwis_gauges (REAL observed USGS NWIS/Water Services stream gauges + latest discharge/stage; the gap NATE hit when the agent fell back to MODELED NWM reach flow - distinct from fetch_noaa_nwm_streamflow; stateCd-or-bbox spatial selector with the ~25 deg^2 bBox guard; IV→Site fallback→typed error)
from . import fetch_hrrr_forecast  # noqa: E402,F401 - job-A2 (Wave 4.10): registers fetch_hrrr_forecast (NOAA HRRR 3km hourly CONUS short-term weather forecast via U.Utah HRRR-Zarr S3 mirror)
from . import fetch_hrrr_smoke  # noqa: E402,F401 - job-A13 (Wave 4.10): registers fetch_hrrr_smoke (NOAA HRRR-Smoke smoke/aerosol forecast via U.Utah HRRR-Zarr S3 mirror; pairs with NIFC fire perimeters for air-quality demo)
from . import fetch_asos_metar  # noqa: E402,F401 - job-A7 (Wave 4.10): registers fetch_asos_metar (Iowa State IEM ASOS/METAR hourly surface observations; station weather obs for hazard context)
from . import fetch_gridmet  # noqa: E402,F401 - job-A8 (Wave 4.10): registers fetch_gridmet (gridMET CONUS daily 4 km meteorology via NKN THREDDS OPeNDAP; fire-weather + drought substrate)
from . import fetch_noaa_coops_tides  # noqa: E402,F401 - job-A9 (Wave 4.10): registers fetch_noaa_coops_tides (NOAA CO-OPS tide-station water-level observations + predictions; SFINCS coastal boundary forcing for US/territory basins)
from . import fetch_usace_dams  # noqa: E402,F401 - job-A5 (Wave 4.10): registers fetch_usace_dams (USACE National Inventory of Dams point inventory via public ESRI Living Atlas mirror; dam-break / hazard-overlay substrate)
from . import fetch_noaa_slr_scenarios  # noqa: E402,F401 - job-A10 (Wave 4.10): registers fetch_noaa_slr_scenarios (NOAA OCM SLR Viewer bathtub inundation polygons for 0-10 ft scenarios; CONUS coastal planning-level overlay)
from . import fetch_noaa_slr_confidence  # noqa: E402,F401 - tools-backlog #1: registers fetch_noaa_slr_confidence (NOAA OCM SLR Viewer conf_* mapping-confidence raster; RGBA overlay via MapServer export -> georeferenced COG)
from . import fetch_noaa_slr_marsh  # noqa: E402,F401 - tools-backlog #2: registers fetch_noaa_slr_marsh (NOAA OCM SLR Viewer marsh_* marsh-migration raster; RGBA overlay via MapServer export -> georeferenced COG)
from . import fetch_usfs_canopy_fuels  # noqa: E402,F401 - job-A14 (Wave 4.10): registers fetch_usfs_canopy_fuels (USFS LANDFIRE LF2022 canopy base height + bulk density rasters; crown-fire model inputs CBH/CBD)
from . import fetch_statsgo_soils  # noqa: E402,F401 - job-A11 (Wave 4.10): registers fetch_statsgo_soils (USGS STATSGO COG collection - KFFACT / THICK - via pfdf.data.usgs.statsgo; post-fire debris-flow + runoff-CN substrate)
from . import fetch_nhdplus_nldi_navigate  # noqa: E402,F401 - job-A11 (Wave 4.10): registers fetch_nhdplus_nldi_navigate (USGS NLDI navigate over the NHDPlus v2.1 channel network - UM / UT / DM / DD traversal from a seed point or COMID)
from . import fetch_raws_weather  # noqa: E402,F401 - job-A12 (Wave 4.10): registers fetch_raws_weather (Iowa Mesonet IEM RAWS fire-weather station observations; wind/RH/temp/solar for wildfire hazard context + fire-behavior model forcing)
from . import fetch_3dep_extra  # noqa: E402,F401 - job-A11 (Wave 4.10): registers fetch_3dep_extra (USGS 3DEP non-default resolutions via pfdf.data.usgs.tnm.dem - 1 arc-sec / 1/9 arc-sec / 1 m / 2 arc-sec / 5 m)
from . import fetch_topobathy  # noqa: E402,F401 - SFINCS North Star P1: registers fetch_topobathy (NOAA NCEI CUDEM 1/9 arc-sec topo-bathy tiles merged with USGS 3DEP land DEM into one seamless EPSG:32616 NAVD88 positive-up float32 COG for coastal SFINCS setup_dep; degrades to 3DEP-land-only with an honest bathymetry-absent warning when CUDEM is missing)
from . import fetch_fault_sources  # noqa: E402,F401 - task #199 (real-fault OpenQuake): registers fetch_fault_sources (REAL active-fault traces + slip rates from the GEM Global Active Faults harmonized GeoJSON; parses the '(best,min,max)' string triples; bbox-filters worldwide faults to the AOI; honest empty-AOI degrade with a typed note; feeds the worker's render_fault_source_model_xml moment-balanced simpleFaultSource builder for fault-aligned PSHA)
from . import discover_dataset  # noqa: E402,F401 - job-B7 (Wave 4.10 Stage 2): registers discover_dataset (hybrid BM25 + dense retrieval over audited docstrings + tool_query_corpus.yaml; routes free-text user queries to top-k atomic tools via RRF fusion; hot-set tool surfaced by B5 per-turn filter)
from . import analytical_qa  # noqa: E402,F401 - job-0224 (sprint-13 Stage 1): registers summarize_layer_statistics + count_features_above_threshold + aggregate_property_within_zone
from . import chart_tools  # noqa: E402,F401 - job-0230 (sprint-13 Stage 2): registers generate_histogram + generate_choropleth_legend + generate_time_series + generate_damage_distribution
from . import compute_cross_section  # noqa: E402,F401 - cross-section/profile tool: registers compute_cross_section (samples raster value(s) at N stations along a drawn-or-derived line -> Vega-Lite distance-vs-value line chart via the chart-emission chat-card path; multi-layer overlay on one shared distance axis; reuses chart_tools.build_chart_payload + clip_raster_to_polygon's s3-staging/CRS pattern)
from . import merge_features  # noqa: E402,F401 - QGIS-wrapping backlog (DigitizingTools DtMerge): registers merge_features (shapely unary_union dissolve of selected features -> one feature, keeper attrs preserved)
from . import cut_features_with_polygon  # noqa: E402,F401 - QGIS-wrapping backlog (DigitizingTools DtCutWithPolygon): registers cut_features_with_polygon (per-feature shapely difference by a cutter, in-place attr preservation, delete_emptied policy)
from . import fill_gaps  # noqa: E402,F401 - QGIS-wrapping backlog (DigitizingTools DtFillGap): registers fill_gaps (union + interior-ring harvest -> enclosed sliver gaps as new polygons)
from . import compute_terrain_profile  # noqa: E402,F401 - QGIS-wrapping backlog (Profile tool): registers compute_terrain_profile (rasterio DEM sampling along a line -> Vega-Lite distance-vs-elevation chart; CRS-correct station reprojection; sibling of compute_cross_section)
from . import run_modflow_tool  # noqa: E402,F401 - job-0227 (sprint-13 Stage 2): registers run_modflow_job (MODFLOW 6 + MF6-GWT groundwater-plume engine; Cloud Workflows + local mf6 modes)
from . import run_swmm_tool  # noqa: E402,F401 - sprint-16 P4 (Path A): registers run_swmm_urban_flood (quasi-2D PySWMM urban-flood engine; pyswmm in-process LOCAL lane - buildings/walls/flap-gates + animated overland depth)
from . import spatial_input_tool  # noqa: E402,F401 - FR-AS-10/FR-WC-16: registers request_spatial_input (pauses the turn, opens the terra-draw surface, returns the role-split drawn geometry - AOI bbox + engine-ready barriers FeatureCollection for run_swmm_urban_flood)
from . import code_exec_tool  # noqa: E402,F401 - job-0233 (sprint-13 Stage 2): registers code_exec_request (user-confirmed Python sandbox; conversational data-analysis escape hatch)
from . import list_run_frames  # noqa: E402,F401 - sandbox-staging: registers list_run_frames (ordered animation-frame COG URIs from a run's publish_manifest.json -> feeds code_exec_request multi-frame layer_refs for per-frame viz snippets)

# sprint-17 NEW engines (parallel lanes) - bridge tools wired into the surface.
from . import run_river_seepage_tool  # noqa: E402,F401 - sprint-17: registers run_river_seepage_job (MODFLOW RIV-coupled river<->aquifer seepage + along-river plume bridge)
from . import run_geoclaw_tool  # noqa: E402,F401 - sprint-17: registers run_geoclaw_inundation (GeoClaw dam-break / coastal inundation bridge; imports model_dambreak_geoclaw_scenario)
from . import run_openquake_tool  # noqa: E402,F401 - sprint-17: registers run_seismic_hazard_psha (OpenQuake PSHA seismic-hazard bridge; imports model_seismic_hazard_scenario)
from . import run_landlab_tool  # noqa: E402,F401 - sprint-17: registers run_landlab_susceptibility (Landlab landslide-probability / overland-flow bridge; imports model_landslide_scenario)
from . import run_swan_tool  # noqa: E402,F401 -- SWAN Phase 1: registers run_swan_waves (SWAN third-generation spectral nearshore wave-field bridge; ADDITIVE comparison engine vs SFINCS+SnapWave; imports model_wave_scenario)
# AWS / Australian-Water-School "Making Waves: Wave Modeling with SWAN" lecture
# (reports/references/lecture_aws_swan_making_waves): two pure-analytic coastal
# post-processors flagged as easy/trivial candidate tools -- no fetch, no solver,
# no cache shim. Both are deterministic closed-form sanity/post-processing tools.
from . import compute_wave_nomograph  # noqa: E402,F401 -- registers compute_wave_nomograph (wind+fetch -> Hs/Tp deep-water fetch-limited wave-growth sanity estimate; SPM 1984 / CEM Part II-2; pre-flight reality-check on a SWAN run)
from . import compute_overtopping  # noqa: E402,F401 -- registers compute_overtopping (EurOtop 2018 Ch.5 mean wave-overtopping discharge over a sloped coastal structure; deterministic post-processor on nearshore Hs/Tp from SWAN/SnapWave)
from . import digitize_water_body  # noqa: F401  -- NDWI surface-water polygons (land cover)
from . import model_fire_spread  # noqa: E402,F401 -- FIRE-3: registers model_fire_spread (ELMFIRE level-set wildfire-spread engine: LANDFIRE fbfm40/cbh/cbd/cc/ch + DEM/slope/aspect -> FIRE-2 same-grid deck -> run_solver('elmfire') [local-docker trid3nt/elmfire:dev now; Batch job-def = the FIRE-4 seam] -> time-of-arrival COG + hourly burned-extent animation frames + flame-length/spread-rate COGs; ignition point REQUIRED, never fabricated; solver-confirm gated with cell count + est runtime)
from . import model_debris_flow  # noqa: E402,F401 -- registers model_debris_flow (USGS post-fire debris-flow hazard via pfdf: watershed analysis -> stream segments -> Staley 2017 M1 likelihood + Gartner 2014 emergency volume + Cannon 2010 combined hazard class over fetched Copernicus DEM / MTBS burn perimeters / STATSGO KFFACT, with dem_uri/severity_uri/kf_uri overrides; honest NoBurnDataError on unburned AOIs)
from . import hydrology_primitives  # noqa: E402,F401 -- registers delineate_watershed (pysheds D8 catchment upstream of a snapped pour point -> watershed polygon vector; auto 0.1-deg bbox around the point; honest edge-truncation note) + extract_stream_network (D8 accumulation >= threshold cells -> stream LineStrings vector; typed NoStreamsError on flat/over-thresholded AOIs); DEM = fetch_copernicus_dem or dem_uri override; pysheds path (base dep via pfdf), 0.3-deg AOI clamp
from . import compute_sediment_yield  # noqa: E402,F401 -- registers compute_sediment_yield (RUSLE annual soil loss A = R*K*LS*C*P t/ha/yr over a bbox: LS from the fetched Copernicus GLO-30 DEM gradient (Wischmeier-Smith unit-plot form, cell-size slope length -- no flow routing, noted), K from STATSGO KFFACT (constant 0.2 fallback, noted), C mapped from Esri/IO 10 m land-cover classes via a documented literature table, R = rainfall_erosivity param or an honest constant-300 default, P=1; single-band float32 COG styled with a LOG-SCALED interval colormap (style_preset=sediment_yield_t_ha_yr); dem_uri/k_uri/landcover_uri overrides; 0.2-deg AOI clamp)
from . import compute_idf_curve  # noqa: E402,F401 -- quick-win batch 2026-07-07: registers compute_idf_curve (full NOAA Atlas 14 IDF curve chart for a point: the SAME PFDS endpoint/parse as lookup_precip_return_period but consumes the FULL 19-duration x 10-ARI matrix -> house chart-emission payload, duration log-x, intensity in/hr (or depth) y, one line per return period; honest typed no-coverage outside Atlas-14 project areas)
from . import compute_urban_heat_island  # noqa: E402,F401 -- quick-win batch 2026-07-07: registers compute_urban_heat_island (MODIS 8-day LST deg-C x Esri/IO 10 m land cover: LST resampled coarse->fine onto the class grid (bilinear, documented no-new-detail note) -> mean LST per land-cover class + uhi_delta_c = Built-area mean minus vegetation-union mean; returns the aligned LST COG (land_surface_temp_c paint) + per-class table on the LayerURI subclass; lst_uri/landcover_uri overrides; honest None delta when built or vegetation absent)
from . import compute_flood_depth_damage  # noqa: E402,F401 -- quick-win batch 2026-07-07: registers compute_flood_depth_damage (HAZUS-style SCREENING flood damage: sample ANY depth COG at each structure point (USACE NSI over the raster bounds by default, assets_uri override) -> EGM 04-01 one-story no-basement residential curve -> damage fraction x NSI val_struct -> point FGB styled by damage fraction + totals; honest not-Pelicun caveat in every result)
from . import compute_change_detection  # noqa: E402,F401 -- quick-win batch 2026-07-07: registers compute_change_detection (two-date Sentinel-2 NDVI/NDWI difference over a bbox -> thresholded |delta|>=0.15 gain/loss change polygons as a FlatGeobuf vector with a categorical gain/loss legend; reuses the compute_ndvi PC-STAC search/sign/windowed-read helpers; imagery_a_uri/imagery_b_uri precomputed-index overrides for offline use; honest typed no-imagery/no-change errors; 0.2-deg AOI clamp)
from . import fetch_usgs_earthquakes  # noqa: E402,F401 — registers fetch_usgs_earthquakes (REAL recorded USGS FDSN Event Web Service earthquakes as epicenter points; bbox|global + time window (default ~30d) + min_magnitude (default 2.5); props mag/depth_km/mag_type/place/time/url; style_preset='earthquakes'; supports_global_query=True; honest EarthquakesNoEventsError on zero events + EarthquakesResultTooLargeError on the 20000-event FDSN cap)
from . import fetch_hifld_critical_infrastructure  # noqa: F401
from . import fetch_cdc_svi  # noqa: E402,F401 — registers fetch_cdc_svi (CDC/ATSDR Social Vulnerability Index 2022 census-tract choropleth: overall RPL_THEMES + 4 theme percentile ranks as FlatGeobuf; CDC/ATSDR OneMap ArcGIS REST FeatureServer layer 2; US-only, public no-key; social-vulnerability/equity exposure overlay)
from . import fetch_sentinel2_truecolor  # noqa: F401  (registers fetch_sentinel2_truecolor)
from . import compute_home_range_kde  # noqa: E402,F401 — registers compute_home_range_kde (kernel-density home range / utilization-distribution isopleths from animal-track POINTS: scipy.stats.gaussian_kde over the fetch_movebank_tracks point FGB -> 50%/95% UD isopleth Polygons via skimage contouring in local UTM; vector FGB inline-GeoJSON layer; honest TOO_FEW_POINTS empty; pairs with fetch_movebank_tracks + compute_zonal_statistics)
from . import compute_movement_trajectory  # noqa: F401
from . import fetch_epa_frs_facilities  # noqa: F401 — registers fetch_epa_frs_facilities (EPA FRS regulated-facility POINTS by bbox via EPA NEPAssist public ArcGIS REST MapServer; facility_program=frs/tri/superfund/air/water/hazwaste/brownfields; ties the MODFLOW contamination-plume demo to real chemical-facility exposure)
from . import fetch_us_drought_monitor  # noqa: E402,F401 — registers fetch_us_drought_monitor (US Drought Monitor weekly drought-category polygons D0-D4 from Esri Living Atlas US_Drought_Intensity_v1 ArcGIS REST FeatureServer; current week (layer 3) or past release via optional date param against archive layer 2; dm 0-4 class + label + period + valid_date props as FlatGeobuf; US-only, public no-key; new drought hazard domain with fire/ag relevance)
from . import fetch_overpass_pois  # noqa: E402,F401 — registers fetch_overpass_pois (generic OSM Overpass key=value POI fetcher -> point/centroid FlatGeobuf; node/way/relation via `out center`; public-mirror fallback chain; honest typed empty; the flexible global exposure-layer complement to the fixed US-only fetch_hifld_critical_infrastructure)
from . import fetch_census_acs  # noqa: E402,F401 — registers fetch_census_acs (US Census ACS 5-year demographics as a census-tract choropleth FlatGeobuf; KEYLESS two-source join: Census TIGERweb tract geometry ArcGIS REST + Census data.census.gov backend table API for ACS estimates, joined by 11-digit GEOID; friendly variable registry median_income/median_age/median_home_value/poverty_rate/pct_renters/pct_no_vehicle plus raw ACS B-code passthrough; US-only, supports_global_query=False; generalizes population-only fetchers to arbitrary EJ/vulnerability demographics; honest typed errors + empty-FGB outside US)
from . import fetch_landsat_imagery as _fetch_landsat_imagery  # noqa: F401
from . import fetch_noaa_sst as _fetch_noaa_sst  # noqa: F401  (registers fetch_noaa_sst via @register_tool)
from . import fetch_openfema_disasters  # noqa: E402,F401 — registers fetch_openfema_disasters (FEMA OpenFEMA DisasterDeclarationsSummaries aggregated to per-county declaration counts/incident-types/dates, joined to Census TIGERweb county polygons by 5-digit FIPS -> FlatGeobuf county overlay; state_code or bbox selector + optional incident_type/start_year; US-only, supports_global_query=False; semi-static-7d cache)
from . import fetch_esri_landcover_10m  # noqa: F401  (registers fetch_esri_landcover_10m)
from . import fetch_usgs_volcano_alerts  # noqa: F401
from . import fetch_usgs_water_quality  # noqa: F401
from . import fetch_usgs_groundwater_levels  # noqa: F401
from . import fetch_snotel_snow  # noqa: F401
from . import fetch_sentinel1_sar  # noqa: F401
from . import fetch_modis_lst  # noqa: F401
from . import fetch_hifld_transmission_lines  # noqa: F401
from . import fetch_lehd_jobs  # noqa: F401
from . import fetch_nws_river_forecast  # noqa: F401
from . import fetch_storm_tracks  # noqa: F401 - registers fetch_storm_tracks (IBTrACS historical + NHC active)
from . import fetch_copernicus_dem  # noqa: F401
from . import fetch_chirps_precipitation  # noqa: F401
from . import fetch_ghsl_population  # noqa: F401
from . import fetch_jrc_global_surface_water  # noqa: F401
from . import fetch_soilgrids  # noqa: F401
from . import fetch_epa_ejscreen  # noqa: F401
from . import fetch_tsunami_events  # noqa: F401
from . import fetch_climate_normals  # noqa: F401
from . import fetch_noaa_coops_currents  # noqa: F401
from . import fetch_airnow_air_quality  # noqa: F401
from . import fetch_openaq_measurements  # noqa: F401
from . import export_case_to_qgis  # noqa: E402,F401 - QGIS bridge v1: registers export_case_to_qgis (case layers -> export.gpkg + local GeoTIFF copies + hand-built .qgz project with TiTiler-translated singleband-pseudocolor styling; local-first s3/http/file loader; per-layer skip honesty)
from . import compute_exposure_summary  # noqa: E402,F401 -- case-analysis batch: registers compute_exposure_summary (hazard-footprint exposure: WorldPop population sum + fetch_buildings footprint count + area km^2 inside cells over threshold; per-component honest degrade, typed empty-footprint error; feeds compose_case_report via a session store)
from . import query_point_hazard  # noqa: E402,F401 -- case-analysis batch: registers query_point_hazard (sample EVERY raster layer of the current Case at a lon/lat or geocoded place; per-layer value+units with honest outside-extent/nodata/unreadable entries; typed no-case-layers error)
from . import extract_timeseries_at_point  # noqa: E402,F401 -- case-analysis batch: registers extract_timeseries_at_point (detects the Case's animation-frame raster sequence via the web LayerPanel frame-token grouping port, samples each frame at a point -> ordered (label, value) series; typed no-frame-sequence honest miss)
from . import compose_case_report  # noqa: E402,F401 -- case-analysis batch: registers compose_case_report (markdown situation report for the current Case: title/date/AOI bbox + per-layer key stats via the summarize_layer_statistics machinery + sim params when present + this session's exposure numbers; written to the case artifacts dir; LayerURI-free result)
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
# ``grace2_agent.categories`` for the server.py dispatch loop.
from .. import categories as _categories  # noqa: E402,F401

# bidirectional layer push: the reverse seam of export_case_to_qgis. Registers
# import_user_layer (LLM-facing wrapper over ingest_user_layer, the shared
# core the /api/ingest-layer HTTP route also drives) so an already-uploaded
# QGIS layer can be registered onto a case conversationally, not just via the
# plugin's "Push layer" button.
from . import import_user_layer  # noqa: E402,F401 - registers import_user_layer (vector/raster upload -> case input layer; s3 object existence+size-cap validation, FlatGeobuf/COG registration reusing publish_layer + the #165 durable-vector-geojson writer, F32-style AOI pin)
