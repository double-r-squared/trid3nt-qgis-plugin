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
(``.passthroughs`` for M4; ``.fetchers`` etc. for M4).
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

     added two metadata flags. They may be set either
    on the constructed ``AtomicToolMetadata`` directly OR passed as
    decorator-level kwargs (kwargs win and produce a new metadata via
    ``model_copy(update=...)``)::

        @register_tool(_BASE_META, supports_global_query=True)
        def fetch_nws_alerts_conus(bbox=None): ...

     added four MCP annotation hints as decorator-level
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
    preserve the earlier behaviour.

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

    # If the caller passed flags at the decorator level,
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
# fetch_glm_lightning: animation_frames fold (ADR 0092) -- twin DELETED, now spec-driven
# (source.yaml + glm.frames_plan/frame_bytes hooks; default output = a frames list), auto-
# registered by _register_router_specs() below; no eager twin import.
# fetch_hrrr_forecast + fetch_hrrr_smoke: HRRR-Zarr library-delegate fold (ADR 0083)
# -- twins DELETED, now spec-driven (source.yaml + hrrr.resolve_cycle/read/validate
# delegate hooks: s3fs cycle-walk delegate_resolve + Zarr open -> LCC->4326 reproject
# + clip + forecast hypot(u,v)); auto-registered by _register_router_specs() below.
# fetch_mrms_qpe: weather/GRIB fold (ADR 0069) -- twin DELETED, now spec-driven
# (source.yaml + mrms_qpe hooks: S3-listed key resolve -> grib_object whole-object COG).
# show_nexrad_radar (a DISPLAY tool, NOT a fetcher: composes a live WMS GetMap URL,
# transfers no bytes) is imported from the -- display -- group below.
# fetch_nws_alerts_conus: data-router fold chained-resolution mode (ADR 0063) -- twin
# DELETED, now spec-driven (source.yaml + nws_alerts_conus hooks: single /alerts/active
# GET + per-alert zone-polygon enrichment), registered by _register_router_specs() below.
# fetch_nws_event: data-router fold tier-3 hooks (ADR 0061) -- twin DELETED, now
# spec-driven (source.yaml + nws_event build_request/parse_response hooks, single-GET
# NWS /alerts/active GeoJSON), registered by _register_router_specs() below.
# fetch_storm_events_db: data-router fold (ADR 0064) -- twin DELETED, now spec-driven
# (source.yaml + storm_events_db hooks: directory-index resolve -> newest bulk-CSV URL,
# then bulk gzip-CSV point decode), registered by _register_router_specs() below.
# fetch_storm_tracks FOLDED to a spec-driven surface (ADR 0111): its source.yaml +
# storm_tracks library-delegate hooks (historical IBTrACS + active NHC incl. the binary
# forecast-zip secondary-enrichment round) are promoted by register_specs_from_tree;
# the StormTracks*Error twins live in _router/hooks/storm_tracks.py. No eager twin import.

# -- fetchers/hydrology --
# V&V wave (ADR 0021, lane C): observed flood-validation data fetchers.
# fetch_flood_extent_observation FOLDED to a spec-driven surface (ADR 0082): its
# source.yaml + categorical_tile_grid mode is promoted by register_specs_from_tree.
# fetch_high_water_marks FOLDED to a spec-driven surface (ADR 0073): its source.yaml
# + usgs_stn_hwm hooks register at import via register_specs_from_tree (envelope hook).
# fetch_jrc_global_surface_water FOLDED to a spec-driven surface (ADR 0086): its
# source.yaml + the stac_continuous_mosaic access mode + the jrc_global_surface_water
# colormap hook register at import via register_specs_from_tree (twin DELETED).
# fetch_nhd_waterbodies: data-router fold phase-2 wave-2 -- twin DELETED, now
# spec-driven (source.yaml + router), registered by _register_router_specs() below.
# fetch_nhdplus_nldi_navigate: data-router fold phase-2 wave-3 (ADR 0040) -- twin
# DELETED, now spec-driven (source.yaml + dataretrieval-delegating router),
# registered by _register_router_specs() below.
# fetch_noaa_nwm_streamflow FOLDED to a spec-driven surface (ADR 0112, THE FETCHER-FINALE
# ENDGAME -- the LAST coded data-fetcher): its source.yaml + the nwm_streamflow.* library-
# delegate hooks (the S3 channel_rt netCDF read -> {feature_id: streamflow} lookup + the NLDI
# 5x5 spatial sample -> COMIDs + per-reach geometry + JOIN -> point FGB, over the fetch-time
# provenance channel) register at import via register_specs_from_tree; the NWMStreamflow*Error
# twins live in _router/hooks/nwm_streamflow.py. No eager twin import.
# fetch_nws_river_forecast: data-router fold chained-resolution mode (ADR 0063) -- twin
# DELETED, now spec-driven (source.yaml + nws_river_forecast hooks: gauges-by-bbox / single
# detail + bounded per-gauge threshold/stageflow enrichment), registered below.
# fetch_river_geometry: Overpass-family river fold (ADR 0074) -- twin DELETED, now
# spec-driven (source.yaml + overpass_river build_request/parse_response hooks over the
# http_json endpoint_fallback mirror chain); the vestigial NHDPlus HR HUC4 leg was
# dropped (NATE-decided). Auto-registered by _register_router_specs() below.
# fetch_usgs_nwis_gauges: CDS-era flood-seam fold (ADR 0085) -- twin DELETED, now
# spec-driven (source.yaml + parse_fallback IV->Site + usgs_nwis hooks), auto-registered.
# fetch_usgs_water_quality: data-router fold phase-2 wave-3 (ADR 0040) -- twin
# DELETED, now spec-driven (source.yaml + dataretrieval-delegating router),
# registered by _register_router_specs() below.

# -- fetchers/ocean --
# fetch_gtsm_tide_surge: CDS library_delegate fold (ADR 0085) -- twin DELETED, now
# spec-driven (source.yaml + cds.gtsm_read), auto-registered via _register_router_specs().
# fetch_noaa_coops_currents: data-router fold phase-2 wave-4 (ADR 0045) -- twin
# DELETED, now spec-driven (source.yaml + the station snapshot router mode),
# registered by _register_router_specs() below.
# fetch_noaa_coops_tides: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_noaa_slr_confidence + fetch_noaa_slr_marsh: raster-stragglers wave (ADR
# 0068) -- twins DELETED, now spec-driven (source.yaml + the router mapserver_export
# RGBA mode), registered by _register_router_specs() below.
# fetch_noaa_slr_scenarios: data-router fold phase-2 wave-6 (ADR 0052) -- twin
# DELETED, now spec-driven (source.yaml + the declarative fan-out router mode),
# registered by _register_router_specs() below.
# fetch_noaa_sst: quick-folds wave (ADR 0079) -- twin DELETED, now spec-driven
# (source.yaml + the raster_cog griddap access mode), registered by _register_router_specs().
# fetch_topobathy: coastal topo-bathymetry fold (ADR 0110) -- twin DELETED, now
# spec-driven (source.yaml + the topobathy.* library-delegate hooks over the 4-leg
# UTM composite + the fetch-time provenance channel), registered by
# _register_router_specs() below.

# -- fetchers/terrain --
# fetch_3dep_extra: library-delegate raster fold (ADR 0075) -- twin DELETED, now
# spec-driven (source.yaml + pfdf_3dep delegate/validate hooks over the generic
# library_delegate mode; output.auto_publish=False + per-resolution payload table);
# auto-registered by _register_router_specs() below.
# fetch_copernicus_dem: fetcher-fold wave-8 -- twin DELETED, now spec-driven
# (source.yaml + router stac_float), registered by _register_router_specs() below.
# fetch_dem FOLDED to a spec-driven surface (ADR 0097): its source.yaml +
# library_delegate py3dep hooks + the source="copernicus" cross-sibling dispatch
# is promoted by register_specs_from_tree; no eager twin import.
# fetch_esri_landcover_10m: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_landcover FOLDED to a spec-driven surface (ADR 0082): its source.yaml +
# wcs_getcoverage mode + sidecar envelope is promoted by register_specs_from_tree.

# -- fetchers/imagery --
# fetch_goes_animation + fetch_goes_blend_animation FOLDED to spec-driven surfaces
# (ADR 0087); fetch_goes_archive_animation + fetch_goes_active_fire FOLDED (ADR 0088):
# source.yaml + shape: animation_frames + goes_archive.frames_plan / frame_bytes over
# the shared imagery._goes_archive_core substrate (netcdf_cf_object per-frame mode),
# auto-registered by register_specs_from_tree. The substrate lives on in
# imagery/_goes_archive_core.py (no registered tool).
# fetch_goes_satellite FOLDED to a spec-driven surface (ADR 0111): its source.yaml +
# goes_satellite library-delegate raster hooks (most-recent MCMIPC read + 15-min
# valid_time cache rounding) are promoted by register_specs_from_tree; the shared
# satellite-identifier / S3 substrate lives on in imagery/_goes_common.py (no registered
# tool). No eager twin import.
# fetch_landsat_imagery / fetch_naip / fetch_sentinel2_truecolor: STAC-composite wave
# (ADR 0080) -- twins DELETED, now spec-driven (source.yaml + raster_cog
# stac_multi_asset_rgb: N reflectance assets + QA/SCL mask + joint 2/98 stretch /
# inferno LST / raw uint8 passthrough), auto-registered by _register_router_specs().
# fetch_slider_timestamps folded to a record-shape spec (ADR 0078); the promoted
# tool auto-registers via register_specs_from_tree (SLIDER availability + cadence index).
# fetch_sentinel1_sar: quick-folds wave (ADR 0079) -- twin DELETED, now spec-driven
# (source.yaml + raster_cog stac_float + coverage-select + log10_db), auto-registered.
# fetch_viirs_day_fire FOLDED to a spec-driven surface (ADR 0087): source.yaml +
# shape: animation_frames + viirs_day_fire.frames_plan / frame_bytes (JPSS polar
# day-pass SLIDER stitch), auto-registered by register_specs_from_tree.

# -- fetchers/climate --
# fetch_chirps_precipitation: data-router fold phase-2 wave-9 (ADR 0055) -- twin
# DELETED, now spec-driven (source.yaml + gzip_object), auto-registered.
# fetch_era5_reanalysis: CDS library_delegate fold (ADR 0085) -- twin DELETED, now
# spec-driven (source.yaml + cds.era5_read), auto-registered via _register_router_specs().
# fetch_gridmet: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_modis_lst: data-router fold phase-2 wave-7 (ADR 0053) -- twin DELETED, now
# spec-driven (source.yaml + stac_float continuous-float mode), registered by
# _register_router_specs() below.
# fetch_us_drought_monitor: data-router fold phase-2 wave-2 -- twin DELETED, now
# spec-driven (source.yaml + router), registered by _register_router_specs() below.
from .fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: E402,F401

# -- fetchers/biodiversity --
# fetch_gbif_occurrences / fetch_inaturalist_observations: data-router fold chained-
# resolution mode (ADR 0063) -- twins DELETED, now spec-driven (source.yaml + the
# resolve-then-fetch hooks: name->id species/match | /v1/taxa GET, then the offset-paged
# occurrence / observation search), registered by _register_router_specs() below.
# fetch_movebank_tracks: keyed CSV http_json fold (ADR 0077) -- twin DELETED, now
# spec-driven (source.yaml + movebank_tracks build_request/parse_response/classify_status
# hooks; composite Basic-Auth via the resolver blob path), auto-registered by
# _register_router_specs() below.

# -- fetchers/socioeconomic --
# fetch_administrative_boundaries: data-router fold zip/multi-file wave (ADR 0067) --
# twin DELETED, now spec-driven (source.yaml + admin_boundaries.build_request FIPS
# planner + the zip_vector whole-object extract executor), registered by
# _register_router_specs() below.
# fetch_buildings: sidecar-write fold (trigger wave, ADR 0084) -- twin DELETED, now
# spec-driven (source.yaml + buildings.build_request/parse hooks + the overpass_sidecar
# executor's constrained tags.json side write), promoted by _register_router_specs().
# fetch_cdc_svi: data-router fold phase-2 wave-2 -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_census_acs: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router JOIN transform), registered by _register_router_specs() below.
# fetch_epa_ejscreen: data-router fold phase-2 wave-6 (ADR 0052) -- twin DELETED,
# now spec-driven (source.yaml + the esri_json ingest mode + percentile/fraction/
# raw column kinds + from_param routing), registered by _register_router_specs().
# fetch_field_boundaries: FTW/fiboa GeoParquet-pushdown fold (ADR 0083) -- twin DELETED,
# now spec-driven (source.yaml + field_boundaries.select pre_resolve + field_boundaries.read
# VECTOR library_delegate hook; the GeoParquet 1.1 row-group bbox pushdown is owned by
# geopandas.read_parquet over an fsspec HTTPS handle, not a router transport -- the ADR 0070
# new-transport STOP refuted). Auto-registered by _register_router_specs() below.
# fetch_ghsl_population: data-router fold zip/multi-file wave (ADR 0067) -- twin
# DELETED, now spec-driven (source.yaml + the raster fixed_tile_grid whole-object
# per-tile ZIP extract mode), registered by _register_router_specs() below.
# fetch_hrsl_population: data-router fold phase-2 wave-9 (ADR 0055) -- twin
# DELETED, now spec-driven (source.yaml + multi_url VRT fan-out), auto-registered.
# fetch_lehd_jobs: join VALUES-hook fold (trigger wave, ADR 0084) -- twin DELETED,
# now spec-driven (source.yaml + join transform + lehd_jobs.values_plan/values_parse
# for the per-state LODES bulk gzip-CSV values leg), promoted by _register_router_specs().
# fetch_overpass_pois + fetch_roads_osm: Overpass-family fold (ADR 0070) -- twins
# DELETED, now spec-driven (source.yaml + overpass build_request/parse_response
# hooks over the http_json endpoint_fallback mirror chain), auto-registered by
# _register_router_specs() below.
# fetch_population: WorldPop library_delegate raster fold (ADR 0092) -- twin DELETED, now
# spec-driven (source.yaml + worldpop.validate/read hooks); the half-built ACS leg dropped
# (fetch_census_acs serves tract population), auto-registered below; no eager twin import.
# fetch_usace_nsi: data-router fold tier-3 hooks (ADR 0061) -- twin DELETED, now
# spec-driven (source.yaml + usace_nsi build_request/parse_response hooks + the
# RequestPlan POST transport extension), registered by _register_router_specs() below.
from .fetchers.socioeconomic.geocode_location import geocode_location  # noqa: E402,F401

# -- fetchers/hazard --
# fetch_fault_sources: finisher-mechanisms wave (ADR 0081) -- twin DELETED, now
# spec-driven (source.yaml + fault_sources build_request/parse_response/envelope
# hooks + the constant_cache two-tier cache + the variant_by_emptiness output
# switch), auto-registered by _register_router_specs() below.
# fetch_firms_active_fire: quick-folds wave (ADR 0079) -- twin DELETED, now spec-driven
# (source.yaml + firms_active_fire keyed CSV http_json hooks), auto-registered.
# fetch_hifld_critical_infrastructure: data-router fold pilot -- twin DELETED, now
# spec-driven (source.yaml + router), registered by _register_router_specs() below.
# fetch_hifld_transmission_lines: data-router fold phase-2 wave-2 -- twin DELETED,
# now spec-driven (source.yaml + router), registered by _register_router_specs().
# fetch_landfire_fuels: data-router fold phase-2 wave-7 (ADR 0053) -- twin DELETED,
# now spec-driven (source.yaml + imageserver_export mode), registered by
# _register_router_specs() below.
# fetch_mtbs_burn_severity + fetch_nifc_fire_perimeters: data-router fold phase-2
# wave-2 -- twins DELETED, now spec-driven (source.yaml + router), registered by
# _register_router_specs() below.
# fetch_openfema_disasters: data-router fold (ADR 0064) -- twin DELETED, now spec-driven
# (source.yaml + openfema_disasters hooks: offset paging + per-county aggregate joined to
# TIGERweb county polygons by FIPS), registered by _register_router_specs() below.
# fetch_tsunami_events: data-router fold phase-2 wave-10 (ADR 0056, tier-3 hooks) --
# twin DELETED, now spec-driven (source.yaml + ncei_tsunami build_request/parse_response
# hooks + paging), registered by _register_router_specs() below.
# fetch_usace_levees: data-router fold phase-2 wave-6 (ADR 0052) -- twin DELETED,
# now spec-driven (source.yaml + endpoint_by_param sub-layer routing +
# properties_by_param), registered by _register_router_specs() below.
# fetch_usfs_canopy_fuels: data-router fold phase-2 wave-7 (ADR 0053) -- twin
# DELETED, now spec-driven (source.yaml + imageserver_export mode), registered by
# _register_router_specs() below.
# fetch_usgs_earthquakes: data-router fold phase-2 wave-10 (ADR 0056, tier-3 hooks) --
# twin DELETED, now spec-driven (source.yaml + usgs_earthquakes build_request/parse_response
# hooks, single-GET FDSN GeoJSON), registered by _register_router_specs() below.
# fetch_usgs_volcano_alerts: data-router fold phase-2 wave-10 (ADR 0056, tier-3 hooks) --
# twin DELETED, now spec-driven (source.yaml + usgs_volcano build_request/parse_response
# hooks, multi-GET HANS join), registered by _register_router_specs() below.
# fetch_wfigs_incident: record-return output-shape fold (ADR 0076) -- twin DELETED,
# now spec-driven (source.yaml + wfigs_incident build_request/record hooks over the
# shape=record executor; a bare discovery dict, not a LayerURI), auto-registered by
# _register_router_specs() below.

# -- fetchers/soil --
# fetch_gcn250_curve_numbers: fetcher-fold wave-8 -- twin DELETED, now spec-driven
# (source.yaml + router direct_window), registered by _register_router_specs() below.
# fetch_soilgrids FOLDED to a spec-driven surface (ADR 0086): its source.yaml + the
# projected_vrt_window access mode (Homolosine VRT windowed in the source projection +
# native->4326 bilinear reproject + per-property Int16 scale) register at import via
# register_specs_from_tree (twin DELETED).
# fetch_statsgo_soils: library-delegate raster fold (ADR 0074) -- twin DELETED, now
# spec-driven (source.yaml + pfdf_statsgo delegate/validate hooks over the generic
# library_delegate mode); auto-registered by _register_router_specs() below.

# -- fetchers/_router: PROMOTED spec-driven sources (data-router fold, phase-2
# wave 1 -- the fold's first real cut). The 5 pilots (fetch_gridmet,
# fetch_hifld_critical_infrastructure, fetch_noaa_coops_tides,
# fetch_esri_landcover_10m, fetch_census_acs) are now served by their
# co-located source.yaml + the shared router engine, registered UNDER THE TWIN
# NAMES at tier="general" (the default retrieval pool). The hand-written twins
# were DELETED (both replication + routing parity gates passed -> cull doctrine).
# Registration walks fetchers/**/source.yaml, so adding a source = adding a YAML.
from .fetchers._router.registration import register_specs_from_tree as _register_router_specs  # noqa: E402,F401

_register_router_specs()

# -- display (live map overlays that compose a service URL, transferring no data
# bytes -- NOT fetchers) --
from .display.show_nexrad_radar.show_nexrad_radar import show_nexrad_radar  # noqa: E402,F401

# -- processing (compute / clip / extract / vector-edit / charts) --
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
# compute_zonal_statistics DEMOTED to the code_exec playground
# (docs/playbooks/zonal-statistics-recipe.md, docs/decisions/0043).
from .processing.delineate_watershed import delineate_watershed  # noqa: E402,F401
from .processing.digitize_water_body import digitize_water_body  # noqa: E402,F401
from .processing.enhance_satellite_image import enhance_satellite_image  # noqa: E402,F401
from .processing.extract_landcover_class import extract_landcover_class  # noqa: E402,F401
# V&V wave (ADR 0021, lane C): model-vs-observation pairing primitive.
from .processing.extract_model_at_observations import extract_model_at_observations  # noqa: E402,F401
from .processing.extract_stream_network import extract_stream_network  # noqa: E402,F401
from .processing.extract_timeseries_at_point import extract_timeseries_at_point  # noqa: E402,F401
from .processing.charts.generate_chart import generate_chart  # noqa: E402,F401
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
# LANDLAB: landlab_susceptibility is the TEMPLATE (engine="landlab",
# tier="template") registered in workflows/landlab/susceptibility/susceptibility.py
# (imported below); the run_landlab door lists + gate-expands it.
# MODFLOW: run_modflow_job (single spill) + run_river_seepage_job are
# unregistered internal engine surfaces -- the single-species spill is folded
# into the modflow_contaminant_plume template; run_river_seepage_job backs the
# modflow_river_seepage template.
# OPENQUAKE: openquake_psha is the TEMPLATE (engine="openquake",
# tier="template") registered in workflows/openquake/psha/psha.py (imported
# below); the run_openquake door lists + gate-expands it.
# PELICUN: run_pelicun_damage_assessment + run_pelicun_with_buildings are ONE
# pelicun_damage_assessment TEMPLATE (engine="pelicun", tier="template") under
# workflows/pelicun/damage_assessment/ (imported below), with a bbox AUTO-FETCH
# input mode (assets_uri absent + bbox -> auto-fetch a building-density
# inventory). The run_pelicun door lists + gate-expands it. postprocess_pelicun
# (above) STAYS general.
# SWAN: swan_wave_field is the TEMPLATE (engine="swan", tier="template")
# registered in workflows/swan/wave_field/wave_field.py (imported below); the
# run_swan door lists + gate-expands it.
# SWMM + TELEMAC: swmm_urban_flood and telemac_river_dye
# (engine="telemac", tier="template", workflows/telemac/river_dye/river_dye.py,
# imported below) are the TEMPLATEs; the run_swmm / run_telemac doors list +
# gate-expand them. run_telemac names the door, not a direct-solve wrapper.
# ADR 0021: derive-not-mutate parameter setters (write a child deck/setup,
# leave the parent immutable).
from .simulation.modflow.set_modflow_parameters import set_modflow_parameters  # noqa: E402,F401
from .simulation.sfincs.set_sfincs_parameters import set_sfincs_parameters  # noqa: E402,F401
from .simulation.swmm.set_swmm_parameters import set_swmm_parameters  # noqa: E402,F401
from .simulation.telemac.set_telemac_parameters import set_telemac_parameters  # noqa: E402,F401 - relocated beside the run_telemac door (engine-door refactor, TELEMAC slice); stays tier=general
from .simulation.solver import solver  # noqa: E402,F401
# -- engine templates (door dissolution, ADR 0094): the 10 engine "door"
# concierge tools were DELETED. Each engine's tier=template members are ordinary
# retrieval-pool tools now, registered by their own @register_tool below (the
# workflow-composer block). Templates are callable DIRECTLY -- no concierge, no
# gate expansion. The solver-seam modules under workflows/<engine>/run_<engine>.py
# (WorkflowError classes, solver specs) are UNRELATED to the deleted doors and
# stay; the templates import them.

# -- discovery (dataset/tool retrieval) --
# NOTE: search_data_catalog / fetch_from_catalog / qgis_discovery register at
# daemon startup via main.py's eager-import block, NOT here - importing this
# package alone deliberately leaves them out of TOOL_REGISTRY (pre-reorg
# behavior: the plain ``import trid3nt_server.agent.tools`` surface is 190 tools,
# 191 after ADR 0019 added search_spatial_functions here).
from .search.search_tools import search_tools  # noqa: E402,F401
from .search.search_spatial_functions import search_spatial_functions  # noqa: E402,F401
# ESRI Living Atlas (ADR 0117): a scoped search over the harvested catalog + a
# generic fetch bridge. Registered here (in-process) so both surface in the tool
# retrieval index for their corpus queries (unlike the daemon-startup catalog
# tools). The two harvested YAML catalogs are DATA, not code.
from .search.search_living_atlas import search_living_atlas  # noqa: E402,F401
from .search.fetch_living_atlas_layer import fetch_living_atlas_layer  # noqa: E402,F401

# -- meta (web fetch, code exec, passthroughs, case utilities) --
from .meta.code_exec_tool import code_exec_tool  # noqa: E402,F401
from .meta.compose_case_report import compose_case_report  # noqa: E402,F401
# open_case_in_qgis + register_case_layer are DEREGISTERED (not LLM-visible):
# their module functions serve the /api/export-qgis + /api/ingest-layer HTTP
# routes directly (lazy-imported at the route), so the modules are NOT imported
# here (importing them no longer registers a tool -- the @register_tool
# decorator was removed).
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
# MODFLOW templates (engine="modflow", tier="template"), one folder per
# template under workflows/modflow/<template>/<template>.py; EXCLUDED from the
# default retrieval pool, surfaced only by the run_modflow door's gate
# expansion. run_modflow_archetype_job / run_modflow_multi_species_job /
# run_river_seepage_job are the unregistered internal engine surfaces the
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
from ..workflows.modflow.vadose_transport.vadose_transport import modflow_vadose_transport as _modflow_vadose_transport  # noqa: E402,F401 - ADR 0228 UZF+UZT unsaturated-zone breakthrough
from ..workflows.modflow.thermal_plume.thermal_plume import modflow_thermal_plume as _modflow_thermal_plume  # noqa: E402,F401 - ADR 0235 GWE heat-transport thermal plume (injection_plume mode)
from ..workflows.modflow.thermal_plume.thermal_plume import modflow_thermal_storage as _modflow_thermal_storage  # noqa: E402,F401 - ADR 0235 GWE heat-transport aquifer thermal energy storage (ates mode)
from ..workflows.modflow.package_validation.package_validation import modflow_package_validation as _modflow_package_validation  # noqa: E402,F401 - NEW capability (ADR 0153, GWF-NPF Newton / GWF-MAW / GWF-HFB package V&V benchmarks) (engine=modflow, tier=template)
# swmm_urban_flood TEMPLATE (engine="swmm", tier="template"), one folder under
# workflows/swmm/urban_flood/; EXCLUDED from the default retrieval pool,
# surfaced only by the run_swmm door's gate expansion. The composer chain
# (model_swmm_urban_flood) is inlined in the template module.
from ..workflows.swmm.urban_flood.urban_flood import swmm_urban_flood as _swmm_urban_flood  # noqa: E402,F401 - RENAME of run_swmm_urban_flood (engine=swmm, tier=template)
# swmm_network_import TEMPLATE (engine="swmm", tier="template"), one folder under
# workflows/swmm/network_import/. A DISTINCT capability: imports a REAL municipal
# storm-drain GIS network (nodes + conduits) into a runnable SWMM model - the
# dual-drainage MINOR system, the practice-verification's #1-ranked gap over the
# DEM-synthesized overland mesh. The composer (model_swmm_network_import) is inlined.
from ..workflows.swmm.network_import.network_import import swmm_network_import as _swmm_network_import  # noqa: E402,F401 - NEW capability (ADR 0124, SWMM network family #1) (engine=swmm, tier=template)
# swmm_dual_drainage_coupling TEMPLATE (engine="swmm", tier="template"), one
# folder under workflows/swmm/dual_drainage/. The DEFINING dual-drainage feature:
# the overland MAJOR-system mesh EXCHANGES flow with the imported piped MINOR
# system (row #1's machinery) at inlets. The composer (model_swmm_dual_drainage)
# is inlined.
from ..workflows.swmm.dual_drainage.dual_drainage import swmm_dual_drainage_coupling as _swmm_dual_drainage_coupling  # noqa: E402,F401 - NEW capability (ADR 0124, SWMM network family #2) (engine=swmm, tier=template)
# Published-deck runner family (ADR 0128): three THIN templates over the shared
# deck-runner core (agent/mesh/swmm_deck_runner.py + workflows/swmm/deck_runner/).
# Each ingests a CITED, PUBLISHED openswmm.org example .inp deck (fetched at
# runtime from the pinned public source; NOT baked - author-posted, redistribution
# unclear), runs it VERBATIM through the headless swmm5_run solver, and charts the
# deck-relevant outputs. The decks are the cited examples' SCHEMATIC networks (no
# georeferenced map); each carries a capability the mesh-builder/GIS-parser cannot
# produce: LID controls, storage routing, and PID/RTC control rules.
from ..workflows.swmm.deck_lid_wq.deck_lid_wq import swmm_lid_raingarden_wq as _swmm_lid_raingarden_wq  # noqa: E402,F401 - NEW capability (ADR 0128, published-deck runner: LID rain-garden WQ) (engine=swmm, tier=template)
from ..workflows.swmm.deck_detention_ponds.deck_detention_ponds import swmm_wwtp_detention_ponds as _swmm_wwtp_detention_ponds  # noqa: E402,F401 - NEW capability (ADR 0128, published-deck runner: storage-routing detention ponds) (engine=swmm, tier=template)
from ..workflows.swmm.deck_pid_pump.deck_pid_pump import swmm_pump_pid_rtc as _swmm_pump_pid_rtc  # noqa: E402,F401 - NEW capability (ADR 0128, published-deck runner: PID pump RTC) (engine=swmm, tier=template)
# ADR 0151 SWMM mechanism-COMPARISON templates: small synthetic decks that vary
# ONE knob across variants and overlay the compared series (CHARTS + typed scalars,
# no georeferenced map). Cover the 12 SWMM CAND-S board rows as knobs.
from ..workflows.swmm.subcatchment_runoff_comparison.subcatchment_runoff_comparison import swmm_subcatchment_runoff_comparison as _swmm_subcatchment_runoff_comparison  # noqa: E402,F401 - NEW capability (ADR 0151, infiltration-method + pre/post-development runoff) (engine=swmm, tier=template)
from ..workflows.swmm.node_hydraulics_comparison.node_hydraulics_comparison import swmm_node_hydraulics_comparison as _swmm_node_hydraulics_comparison  # noqa: E402,F401 - NEW capability (ADR 0151, outlet-structure family / flow diversion / surcharge-ponding) (engine=swmm, tier=template)
from ..workflows.swmm.wetwell_pump_control_comparison.wetwell_pump_control_comparison import swmm_wetwell_pump_control_comparison as _swmm_wetwell_pump_control_comparison  # noqa: E402,F401 - NEW capability (ADR 0151, wet-well pump curve + duty/standby + multi-condition RTC) (engine=swmm, tier=template)
from ..workflows.swmm.lid_performance_comparison.lid_performance_comparison import swmm_lid_performance_comparison as _swmm_lid_performance_comparison  # noqa: E402,F401 - NEW capability (ADR 0151, green roof / rain barrel vs rooftop disconnect / vegetative swale) (engine=swmm, tier=template)
from ..workflows.swmm.wq_buildup_washoff_comparison.wq_buildup_washoff_comparison import swmm_wq_buildup_washoff_comparison as _swmm_wq_buildup_washoff_comparison  # noqa: E402,F401 - NEW capability (ADR 0151, curb-length vs area buildup + EMC vs exp washoff) (engine=swmm, tier=template)
# swmm_rdii_rtk_unit_hydrograph (ADR 0190 row 4): RTK triangular unit-hydrograph
# RDII (rainfall-derived inflow+infiltration) at a node vs direct runoff; closed
# form validated against the native SWMM 5 [HYDROGRAPHS]/[RDII] engine. tier=template.
from ..workflows.swmm.rdii_rtk.rdii_rtk import swmm_rdii_rtk_unit_hydrograph as _swmm_rdii_rtk_unit_hydrograph  # noqa: E402,F401 - RTK unit-hydrograph RDII
# swmm_snowmelt_degree_day (ADR 0218 board rows swmm_snowmelt_degree_day +
# swmm_snow_removal_plowing): Snow Pack [SNOWPACKS] + degree-day melt on a
# subcatchment; rain-on-snow winter flood driver charted snowmelt-vs-rain-only,
# with the snow-removal/plowing REMOVAL block as a knob. Chart-first validation
# class (RDII precedent), host-side pyswmm, no worker image. tier=template.
from ..workflows.swmm.snowmelt_degree_day.snowmelt_degree_day import swmm_snowmelt_degree_day as _swmm_snowmelt_degree_day  # noqa: E402,F401 - Snow Pack degree-day melt (engine=swmm, tier=template)
# swmm_aquifer_baseflow_to_node (ADR 0218 board row swmm_aquifer_baseflow_to_node):
# the [GROUNDWATER] two-zone aquifer under a pervious subcatchment contributing
# baseflow to a drainage node BETWEEN storms; charted with-GW vs no-GW. The SWMM
# analogue of the Landlab groundwater / RoG return-flow theme. tier=template.
from ..workflows.swmm.aquifer_baseflow.aquifer_baseflow import swmm_aquifer_baseflow_to_node as _swmm_aquifer_baseflow_to_node  # noqa: E402,F401 - two-zone aquifer baseflow (engine=swmm, tier=template)
# telemac_river_dye TEMPLATE (engine="telemac", tier="template"), one folder
# under workflows/telemac/river_dye/; EXCLUDED from the default retrieval
# pool, surfaced only by the run_telemac door's gate expansion. The composer
# chain (model_telemac_river_dye) is inlined in the template module;
# workflows/telemac/run_telemac.py is the local solve seam
# (the door holds the run_telemac name; the template submits the solver).
from ..workflows.telemac.river_dye.river_dye import telemac_river_dye as _telemac_river_dye  # noqa: E402,F401 - NAME FLIP of run_telemac (engine=telemac, tier=template)
# telemac_do_sag TEMPLATE (engine="telemac", tier="template"), workflows/telemac/
# do_sag/: the WAQTEL O2 dissolved-oxygen sag (US TMDL/permit). Reuses the
# river_dye reach-seeding + solve via model_telemac_river_dye(do_sag_config=...);
# WATER QUALITY PROCESS = 2, V&V to Streeter-Phelps 0.011 mg/L (ADR 0169).
from ..workflows.telemac.do_sag.do_sag import telemac_do_sag as _telemac_do_sag  # noqa: E402,F401 - WAQTEL O2 front (engine=telemac, tier=template)
# telemac_rain_on_grid TEMPLATE (engine="telemac", tier="template"), workflows/
# telemac/rain_on_grid/: SCS-CN rainfall-runoff on a delineated watershed (ADR
# 0196). Composes acquire_watershed_mesh + NLCD-distributed CN/Manning + the
# native constant-storm SCS-CN worker deck (mode=rain_on_grid) -> outlet
# hydrograph + peak-depth COG. Live V&V: Coweeta Creek NC (docs/proof/templates).
from ..workflows.telemac.rain_on_grid.rain_on_grid import telemac_rain_on_grid as _telemac_rain_on_grid  # noqa: E402,F401 - RoG front (engine=telemac, tier=template)
# tomawac_wave_field TEMPLATE (engine="telemac", tier="template"), workflows/
# telemac/wave_field/: the TOMAWAC third-generation spectral-wave engine (ADR
# 0236). ONE question-class tool, four modes (fetch_growth / shoaling /
# bottom_friction / wave_current); real Great Lakes lake-datum bathymetry or an
# idealized basin. Physics proven through the baked tomawac binary; the
# refinement-grade complement to SFINCS/SnapWave coastal screening.
from ..workflows.telemac.wave_field.wave_field import tomawac_wave_field as _tomawac_wave_field  # noqa: E402,F401 - TOMAWAC wave front (engine=telemac, tier=template)
# artemis_harbor_agitation TEMPLATE (engine="telemac", tier="template"),
# workflows/telemac/agitation/: the ARTEMIS phase-resolving elliptic mild-slope
# (Berkhoff) harbour-agitation engine (ADR 0237). ONE question-class tool, three
# modes (diffraction / resonance / shoal); real Great Lakes lake-datum bathymetry
# with a schematic breakwater, or an idealized analytic domain. Physics proven
# through the baked artemis binary; the phase-resolving complement to the TOMAWAC
# spectral tier.
from ..workflows.telemac.agitation.agitation import artemis_harbor_agitation as _artemis_harbor_agitation  # noqa: E402,F401 - ARTEMIS agitation front (engine=telemac, tier=template)
# telemac3d_stratified_flow TEMPLATE (engine="telemac", tier="template"),
# workflows/telemac/stratified_flow/: the TELEMAC-3D three-dimensional baroclinic
# Navier-Stokes engine (ADR 0241) - the one genuinely NEW solver leg in the family.
# ONE question-class tool, three modes (stratification / wind_circulation /
# salt_wedge); real Great Lakes lake-datum bathymetry (thermal / wind modes) or an
# idealized closed basin. Physics proven through the baked telemac3d binary; the 3D
# refinement tier that unblocks the AED2 lake-ecology + dune-migration STOPs.
from ..workflows.telemac.stratified_flow.stratified_flow import telemac3d_stratified_flow as _telemac3d_stratified_flow  # noqa: E402,F401 - TELEMAC-3D stratified front (engine=telemac, tier=template)
# generate_mesh (ADR 0200): the standalone mesh builder. Promotes the ADR 0193
# watershed + ADR 0194 coastal water-edge sandbox meshers behind ONE tool (mode
# inferred from inputs); emits an MDAL .2dm display layer + a durable mesh artifact
# (facts + solver geometries) a model template discovers via the precondition gate.
from ..workflows.mesh.generate_mesh.generate_mesh import generate_mesh as _generate_mesh  # noqa: E402,F401 - mesh domain primitive (tier=general)
# hecras_riverine_flood TEMPLATE (engine="hecras", tier="template"), engine #11,
# one folder under workflows/hecras/riverine_flood/. TEMPLATE-FIRST: reparameterizes
# HEC's shipped Muncie White River (IN) demonstration deck (frozen geometry, scaled
# unsteady flow forcing). The composer chain (model_hecras_riverine_flood) is inlined
# in the template module; workflows/hecras/run_hecras.py is the local solve seam.
from ..workflows.hecras.riverine_flood.riverine_flood import hecras_riverine_flood as _hecras_riverine_flood  # noqa: E402,F401 - engine #11 (engine=hecras, tier=template)
# hecras_levee_breach TEMPLATE (engine="hecras", tier="template"), engine #11 second
# archetype (ADR 0125): the SAME frozen Muncie White River geometry, whose 2D
# Interior Area is a LEVEED protected floodplain -- toggles the deck's lateral-
# structure breach (levee fails -> floods; holds -> valid dry success). Composer
# inlined in the module; the local solve seam is shared (workflows/hecras/run_hecras.py).
from ..workflows.hecras.levee_breach.levee_breach import hecras_levee_breach as _hecras_levee_breach  # noqa: E402,F401 - engine #11 second archetype (engine=hecras, tier=template)
# hecras_flood_2d TEMPLATE (engine="hecras", tier="template"), engine #11 third
# archetype (ADR 0140 promotion): unlike the two Muncie templates, this AUTHORS the
# 2D mesh + terrain subgrid tables for a GENUINELY-NEW user AOI (fetch_dem -> the
# hecras2025-authoring worker image: AuthorMesh topology + ComputeFrom tables) then
# solves the composed deck through run_solver (the M3-gate no-archetype path).
from ..workflows.hecras.flood_2d.flood_2d import hecras_flood_2d as _hecras_flood_2d  # noqa: E402,F401 - engine #11 third archetype (engine=hecras, tier=template)
# culvert_embankment_flow TEMPLATE (engine="hecras", tier="template"), engine #11 fourth
# archetype (ADR 0251 Stage 2): authors a culvert barrel + BarrelProperties +
# OpeningProperties on a real-reach 2D deck (fetched 3DEP terrain) and runs the
# present-vs-absent A/B on the HEC-RAS 2025 managed CPU engine -- the ONE 2D structure
# the beta wires into the solve (InitializeDriver_Culverts). The barrel conveys reach
# flow the road embankment otherwise blocks (live-proven, North Fork Salt Creek IN).
from ..workflows.hecras.culvert_embankment_flow.culvert_embankment_flow import culvert_embankment_flow as _culvert_embankment_flow  # noqa: E402,F401 - engine #11 fourth archetype (engine=hecras, tier=template)
# schism_tidal_hydro TEMPLATE (engine="schism", tier="template"), engine #12,
# one folder under workflows/schism/tidal_hydro/. Barotropic tidal hydrodynamics
# on an unstructured coastal mesh (ADR 0118): the QuarterAnnulus verification case
# OR an oceanmesh coastal_tin. The composer chain (model_schism_tidal_hydro) is
# inlined in the template module; workflows/schism/run_schism.py is the local solve seam.
from ..workflows.schism.tidal_hydro.tidal_hydro import schism_tidal_hydro as _schism_tidal_hydro  # noqa: E402,F401 - engine #12 (engine=schism, tier=template)
# schism_coupled_waves TEMPLATE (engine="schism", tier="template"), engine #12
# second archetype (ADR 0126/0129): SCHISM+WWM two-way wave-current coupling on the
# bundled Duck NC FRF validation case, with the GOTM k-epsilon closure (the
# pschism_WWM_GOTM_TVD-VL variant). The composer chain (model_schism_coupled_waves)
# is inlined in the template module.
from ..workflows.schism.coupled_waves.coupled_waves import schism_coupled_waves as _schism_coupled_waves  # noqa: E402,F401 - engine #12 second archetype (engine=schism, tier=template)
# schism_transport_validation TEMPLATE (engine="schism", tier="template"), ADR 0156
# SCHISM CAND-S: transport-scheme numerical-mixing V&V (Test_HeatConsv upwind-vs-TVD
# + Test_GEN_MassConsv conservative-tracer mass conservation) on the hydro-core
# binary; the composer chain (model_schism_transport_validation) is inlined.
from ..workflows.schism.transport_validation.transport_validation import schism_transport_validation as _schism_transport_validation  # noqa: E402,F401 - ADR 0156 SCHISM CAND-S transport V&V (engine=schism, tier=template)
# schism_baroclinic_circulation TEMPLATE (engine="schism", tier="template"), ADR 0189
# - density-driven 3D baroclinic estuary circulation + stratification (ibc=0, SZ
# vgrid, river source, salinity gradient) on the hydro-core binary; the composer
# chain (model_schism_baroclinic_circulation) is inlined.
from ..workflows.schism.baroclinic_circulation.baroclinic_circulation import schism_baroclinic_circulation as _schism_baroclinic_circulation  # noqa: E402,F401 - ADR 0189 SCHISM 3D baroclinic template (engine=schism, tier=template)
# schism_pahm_surge TEMPLATE (engine="schism", tier="template"), ADR 0217:
# parametric hurricane storm surge -- a best track -> a standalone Holland-1980
# sflux wind/pressure field (nws=2) -> barotropic surge on a coastal TIN (peak
# surge COG + track overlay + gauge hydrograph). The composer chain
# (model_schism_pahm_surge) is inlined.
from ..workflows.schism.pahm_surge.pahm_surge import schism_pahm_surge as _schism_pahm_surge  # noqa: E402,F401 - ADR 0217 SCHISM PaHM storm-surge template (engine=schism, tier=template)
# geoclaw_inundation TEMPLATE (engine="geoclaw", tier="template"), one folder
# under workflows/geoclaw/inundation/; EXCLUDED from the default retrieval
# pool, surfaced only by the run_geoclaw door's gate expansion. The composer
# chain (model_geoclaw_inundation) is inlined in the template module.
from ..workflows.geoclaw.inundation.inundation import geoclaw_inundation as _geoclaw_inundation  # noqa: E402,F401 - RENAME of run_geoclaw_inundation (engine=geoclaw, tier=template)
# geoclaw_tsunami_gauge_timeseries TEMPLATE (engine="geoclaw", tier="template"), a
# DISTINCT capability (coastal gauge water-level time series), one folder under
# workflows/geoclaw/gauge_timeseries/; surfaced by the run_geoclaw door's gate
# expansion. Rides the inundation composer (model_geoclaw_inundation, emit_gauge_series).
from ..workflows.geoclaw.gauge_timeseries.gauge_timeseries import geoclaw_tsunami_gauge_timeseries as _geoclaw_tsunami_gauge_timeseries  # noqa: E402,F401 - NEW capability (ADR 0123, hazard-easy-four continuation #3) (engine=geoclaw, tier=template)
# geoclaw_amr_refinement_regions TEMPLATE (engine="geoclaw", tier="template"), one
# folder under workflows/geoclaw/amr_regions/: explicit lat/lon/time AMR region
# control (region-based flagging). Rides the inundation composer.
from ..workflows.geoclaw.amr_regions.amr_regions import geoclaw_amr_refinement_regions as _geoclaw_amr_refinement_regions  # noqa: E402,F401 - GeoClaw CAND-S SWE+AMR knob (engine=geoclaw, tier=template)
# geoclaw_regional_manning_friction TEMPLATE (engine="geoclaw", tier="template"), one
# folder under workflows/geoclaw/regional_manning/: spatially-varying (banded)
# Manning bottom-friction. Rides the inundation composer.
from ..workflows.geoclaw.regional_manning.regional_manning import geoclaw_regional_manning_friction as _geoclaw_regional_manning_friction  # noqa: E402,F401 - GeoClaw CAND-S SWE+AMR knob (engine=geoclaw, tier=template)
# geoclaw_storm_surge TEMPLATE (engine="geoclaw", tier="template"), one folder
# under workflows/geoclaw/storm_surge/: parametric-Holland tropical-cyclone storm
# surge (wind + pressure forcing from a storm track, selectable wind drag law).
# Rides the inundation composer. ADR 0168.
from ..workflows.geoclaw.storm_surge.storm_surge import geoclaw_storm_surge as _geoclaw_storm_surge  # noqa: E402,F401 - GeoClaw storm-surge front (engine=geoclaw, tier=template)
# geoclaw_thacker_validation TEMPLATE (engine="geoclaw", tier="template"), one
# folder under workflows/geoclaw/thacker_validation/: a synthetic, non-geographic
# V&V of the wet-dry SWE+AMR solver vs Thacker's 1981 exact paraboloid-basin
# solution (DEM-free composer branch; chart+scalars only). ADR 0187.
from ..workflows.geoclaw.thacker_validation.thacker_validation import geoclaw_thacker_validation as _geoclaw_thacker_validation  # noqa: E402,F401 - GeoClaw SWE+AMR analytic V&V (engine=geoclaw, tier=template)
# swan_wave_field TEMPLATE (engine="swan", tier="template"), one folder under
# workflows/swan/wave_field/; EXCLUDED from the default retrieval pool,
# surfaced only by the run_swan door's gate expansion. The composer chain
# (model_swan_wave_field) is inlined in the template module.
from ..workflows.swan.wave_field.wave_field import swan_wave_field as _swan_wave_field  # noqa: E402,F401 - RENAME of run_swan_waves (engine=swan, tier=template)
from ..workflows.swan.physics_sensitivity_sweep.physics_sensitivity_sweep import swan_physics_sensitivity_sweep as _swan_physics_sensitivity_sweep  # noqa: E402,F401 - SWAN CAND-S: physics-scheme A-vs-B sensitivity sweep (GEN/WCAPPING/QUADRUPL/BREAKING/FRICTION/TRIAD knobs)
from ..workflows.swan.stationary_snapshot_batch.stationary_snapshot_batch import swan_stationary_snapshot_batch as _swan_stationary_snapshot_batch  # noqa: E402,F401 - SWAN CAND-S: batch of stationary snapshots sampling a storm event (MODE)
# landlab_susceptibility TEMPLATE (engine="landlab", tier="template"), one
# folder under workflows/landlab/susceptibility/; EXCLUDED from the default
# retrieval pool, surfaced only by the run_landlab door's gate expansion. The
# composer chain (model_landlab_susceptibility) is inlined in the template
# module; workflows/landlab/run_landlab.py is the distinct solver build/stage
# seam.
from ..workflows.landlab.susceptibility.susceptibility import landlab_susceptibility as _landlab_susceptibility  # noqa: E402,F401 - RENAME of run_landlab_susceptibility (engine=landlab, tier=template)
# landlab_flow_accumulation TEMPLATE (engine="landlab", tier="template"), a
# DISTINCT capability (drainage area + channel network + routing comparison), one
# folder under workflows/landlab/flow_accumulation/; surfaced by the run_landlab
# door's gate expansion. The composer (model_landlab_flow_accumulation) is inlined.
from ..workflows.landlab.flow_accumulation.flow_accumulation import landlab_flow_accumulation as _landlab_flow_accumulation  # noqa: E402,F401 - NEW capability (ADR 0122, hazard-easy-four #1) (engine=landlab, tier=template)
# landlab_green_ampt_overland_flow TEMPLATE (engine="landlab", tier="template"), a
# DISTINCT capability (infiltration-vs-runoff storm partition), one folder under
# workflows/landlab/green_ampt/; surfaced by the run_landlab door's gate
# expansion. The composer (model_landlab_green_ampt_overland_flow) is inlined.
from ..workflows.landlab.green_ampt.green_ampt import landlab_green_ampt_overland_flow as _landlab_green_ampt_overland_flow  # noqa: E402,F401 - NEW capability (ADR 0123, hazard-easy-four continuation #1) (engine=landlab, tier=template)
# Landlab diagnostic TEMPLATES (engine="landlab", tier="template"), each a DISTINCT
# capability, one folder under workflows/landlab/; walked into the main retrieval
# index. Composers (model_landlab_*) are inlined in each template module.
from ..workflows.landlab.landslide_storm_ensemble.storm_ensemble import landlab_landslide_storm_ensemble as _landlab_landslide_storm_ensemble  # noqa: E402,F401 - storm/recharge-ensemble landslide susceptibility sweep
from ..workflows.landlab.overland_flow_timeseries.overland_timeseries import landlab_overland_flow_timeseries as _landlab_overland_flow_timeseries  # noqa: E402,F401 - time-stepped overland-flow depth animation
from ..workflows.landlab.dem_conditioning.dem_conditioning import landlab_dem_conditioning as _landlab_dem_conditioning  # noqa: E402,F401 - DEM pit-fill conditioning depth
from ..workflows.landlab.lake_mapping.lake_mapping import landlab_lake_mapping as _landlab_lake_mapping  # noqa: E402,F401 - lake extent + depth mapping
from ..workflows.landlab.hacks_law.hacks_law import landlab_hacks_law_scaling as _landlab_hacks_law_scaling  # noqa: E402,F401 - Hack's-law basin length-area scaling diagnostic
from ..workflows.landlab.hand_wetness.hand_wetness import landlab_hand_wetness as _landlab_hand_wetness  # noqa: E402,F401 - Height Above Nearest Drainage wetness proxy
from ..workflows.landlab.channel_incision.channel_incision import landlab_channel_incision_steady_state as _landlab_channel_incision_steady_state  # noqa: E402,F401 - ADR 0184: detachment-limited stream-power incision to steady state + slope-area V&V
from ..workflows.landlab.normal_fault.normal_fault import landlab_normal_fault_scarp_evolution as _landlab_normal_fault_scarp_evolution  # noqa: E402,F401 - ADR 0252: NormalFault tectonic-forcing landscape evolution (scarp + footwall drainage)
from ..workflows.landlab.chi_map.chi_map import landlab_channel_steepness_chi_map as _landlab_channel_steepness_chi_map  # noqa: E402,F401 - ADR 0184: chi index + channel steepness (ksn) knickpoint diagnostic
from ..workflows.landlab.storm_sequence.storm_sequence import landlab_storm_sequence_generator as _landlab_storm_sequence_generator  # noqa: E402,F401 - ADR 0184: stochastic storm-sequence forcing generator (PrecipitationDistribution)
from ..workflows.landlab.groundwater_water_table.groundwater_water_table import landlab_groundwater_water_table as _landlab_groundwater_water_table  # noqa: E402,F401 - ADR 0214: GroundwaterDupuitPercolator steady-state water table + seepage + baseflow (mass-conservation V&V)
from ..workflows.landlab.groundwater_storm_recession.groundwater_storm_recession import landlab_groundwater_storm_recession as _landlab_groundwater_storm_recession  # noqa: E402,F401 - ADR 0214: GroundwaterDupuitPercolator storm-driven seepage/baseflow hydrograph + recession
# openquake_psha TEMPLATE (engine="openquake", tier="template"), one folder
# under workflows/openquake/psha/; EXCLUDED from the default retrieval pool,
# surfaced only by the run_openquake door's gate expansion. The composer chain
# (model_openquake_psha) is inlined in the template module.
from ..workflows.openquake.psha.psha import openquake_psha as _openquake_psha  # noqa: E402,F401 - RENAME of run_seismic_hazard_psha (engine=openquake, tier=template)
# OpenQuake scenario ground-motion-field + earthquake secondary-perils TEMPLATES
# (engine="openquake", tier="template"; ADR 0164). scenario_gmf runs the
# in-process oq scenario calculator (single rupture + JB2009-correlated GMFs) and
# maps the mean + across-realization spread; secondary_perils rides that GMF and
# applies the openquake.sep liquefaction (Zhu 2015) + Newmark landslide (Jibson)
# screens over fetched terrain covariates.
from ..workflows.openquake.scenario_gmf.scenario_gmf import openquake_scenario_gmf as _openquake_scenario_gmf  # noqa: E402,F401
from ..workflows.openquake.secondary_perils.secondary_perils import openquake_secondary_perils as _openquake_secondary_perils  # noqa: E402,F401
# OpenQuake disaggregation + event-based-PSHA TEMPLATES (engine="openquake",
# tier="template"; ADR 0182). Both run the installed oq engine locally as a
# composer subprocess (the offline lane): disaggregation decomposes a site's
# hazard into the dominant magnitude-distance-epsilon scenario; event_based
# samples a synthetic earthquake catalogue, maps the event-based hazard, and
# cross-checks the back-derived curve against classical PSHA.
from ..workflows.openquake.disaggregation.disaggregation import openquake_disaggregation as _openquake_disaggregation  # noqa: E402,F401
from ..workflows.openquake.event_based.event_based import openquake_event_based as _openquake_event_based  # noqa: E402,F401
# elmfire_fire_spread TEMPLATE (engine="elmfire", tier="template"), one
# folder under workflows/elmfire/fire_spread/; EXCLUDED from the default
# retrieval pool, surfaced only by the run_elmfire door's gate expansion. The
# composer chain (model_elmfire_fire_spread) is inlined in the template
# module; workflows/elmfire/run_elmfire.py is the distinct solver build/stage
# seam.
from ..workflows.elmfire.fire_spread.fire_spread import elmfire_fire_spread as _elmfire_fire_spread  # noqa: E402,F401 - RENAME of model_fire_spread (engine=elmfire, tier=template)
# elmfire_verification_elliptical_replication TEMPLATE (engine="elmfire",
# tier="template"), a DISTINCT capability (the constant-wind flat-terrain
# elliptical-spread verification / calibration anchor), one folder under
# workflows/elmfire/verification/; surfaced by the run_elmfire door's gate expansion.
from ..workflows.elmfire.verification.verification import elmfire_verification_elliptical_replication as _elmfire_verification_elliptical_replication  # noqa: E402,F401 - NEW capability (ADR 0123, hazard-easy-four continuation #2) (engine=elmfire, tier=template)
# elmfire_crown_fire_active_ros_verification TEMPLATE (engine="elmfire",
# tier="template"): the twin of the elliptical verification for the crown-fire
# regime -- an uncapped active-crown deck's numerical head spread rate vs the Cruz
# (2005) closed-form active crown-fire ROS (the exact-solution regression gate).
from ..workflows.elmfire.verification.crown_ros import elmfire_crown_fire_active_ros_verification as _elmfire_crown_fire_active_ros_verification  # noqa: E402,F401 - Cruz (2005) crown-fire ROS V&V (engine=elmfire, tier=template)
# ELMFIRE CAND-S sensitivity sweep templates (constant flat deck, one knob each):
# each sweeps a &SIMULATOR / &INPUTS knob across a small ladder and returns an
# ElmfireSensitivityLayerURI. tier=template, engine=elmfire.
from ..workflows.elmfire.sensitivity.ltw_ceiling.ltw_ceiling import elmfire_length_to_width_ceiling_sensitivity as _elmfire_length_to_width_ceiling_sensitivity  # noqa: E402,F401 - MAX_LOW length:width ceiling sweep
from ..workflows.elmfire.sensitivity.wind_fluctuation.wind_fluctuation import elmfire_wind_fluctuation_randomization as _elmfire_wind_fluctuation_randomization  # noqa: E402,F401 - WIND_FLUCTUATIONS deterministic-vs-randomized sweep
from ..workflows.elmfire.sensitivity.live_moisture.live_moisture import elmfire_live_fuel_moisture_sensitivity as _elmfire_live_fuel_moisture_sensitivity  # noqa: E402,F401 - live herbaceous/woody moisture override sweep
# ELMFIRE transient/multi-band weather-deck templates (ADR 0161, front A): a
# synthetic time-varying weather schedule (NUM_METEOROLOGY_TIMES>1 + DT_METEOROLOGY
# interpolation) drives a mid-run wind shift and a dead-fuel moisture-recovery
# interpolation-cadence sweep. tier=template, engine=elmfire.
from ..workflows.elmfire.transient.wind_schedule.wind_schedule import elmfire_transient_wind_schedule_spread as _elmfire_transient_wind_schedule_spread  # noqa: E402,F401 - mid-run wind-shift redirection vs constant wind
from ..workflows.elmfire.transient.dead_fuel_interp.dead_fuel_interp import elmfire_dead_fuel_moisture_interpolation_frequency_control as _elmfire_dead_fuel_moisture_interpolation_frequency_control  # noqa: E402,F401 - DT_INTERPOLATE_M1/M10/M100 accuracy-vs-cost sweep
# ELMFIRE crown-fire family (ADR 0161, front B): a folded crown template sweeping
# the surface-to-crown initiation boundary (CRITICAL_CANOPY_COVER) or the Cruz
# active-crown spread-rate ceiling (CROWN_FIRE_SPREAD_RATE_LIMIT) on a canopied
# deck. tier=template, engine=elmfire.
from ..workflows.elmfire.crown.crown_fire import elmfire_crown_fire_initiation_threshold_sweep as _elmfire_crown_fire_initiation_threshold_sweep  # noqa: E402,F401 - crown-fire initiation + Cruz-rate-ceiling folded sweep
# ELMFIRE spotting family (ADR 0239): does wind-driven ember spotting carry the fire
# ACROSS a non-burnable fuel break the contiguous front cannot cross (spotting OFF vs
# ON on a grass deck with a barrier strip). Distinct question class - the barrier-jump
# discriminant; folds the lognormal-distance / generation-gate / ember-count knobs.
from ..workflows.elmfire.spotting.spotting import elmfire_spot_fire_barrier_crossing as _elmfire_spot_fire_barrier_crossing  # noqa: E402,F401 - ember-spotting barrier-jump (engine=elmfire, tier=template)
# ELMFIRE initial-attack POC (ADR 0190 row 2): the Hirsch 1998 closed-form
# probability of containment (fire size + head-fire intensity + attack delay).
# CLOSED-FORM validation class (no engine run, chart/scalars). tier=template.
from ..workflows.elmfire.initial_attack.initial_attack import elmfire_initial_attack_containment_probability as _elmfire_initial_attack_containment_probability  # noqa: E402,F401 - Hirsch POC closed form
# pelicun_damage_assessment TEMPLATE (engine="pelicun", tier="template")
# under workflows/pelicun/damage_assessment/; EXCLUDED from the default
# retrieval pool, surfaced only by the run_pelicun door's gate expansion. Its
# bbox AUTO-FETCH input mode covers the buildings-composer path (one tool,
# two input modes). postprocess_pelicun STAYS general, NOT a template.
from ..workflows.pelicun.damage_assessment.damage_assessment import pelicun_damage_assessment as _pelicun_damage_assessment  # noqa: E402,F401 - FOLD of run_pelicun_damage_assessment + run_pelicun_with_buildings (engine=pelicun, tier=template; explicit assets_uri OR bbox auto-fetch)
# pelicun Assessment-API validation / sensitivity TEMPLATES (engine="pelicun",
# tier="template"): idealized domain-free checks that drive pelicun's real
# assessment.Assessment pipeline on synthetic inputs and emit distribution /
# curve charts (no hazard raster, no map). Each is a distinct question class.
from ..workflows.pelicun.closed_form_validation.closed_form_validation import pelicun_closed_form_validation as _pelicun_closed_form_validation  # noqa: E402,F401 - Monte-Carlo vs analytic closed form (damage-state probability + loss-function identity)
from ..workflows.pelicun.mixed_fragility_loss.mixed_fragility_loss import pelicun_mixed_fragility_loss_assessment as _pelicun_mixed_fragility_loss_assessment  # noqa: E402,F401 - mixed fragility+loss-function assessment + EDP correlation spread
from ..workflows.pelicun.replacement_threshold_sweep.replacement_threshold_sweep import pelicun_replacement_threshold_override_sweep as _pelicun_replacement_threshold_override_sweep  # noqa: E402,F401 - RID-triggered irreparable/replacement threshold sweep (RID inferred from PID)
from ..workflows.pelicun.flood_foundation_depth_damage.flood_foundation_depth_damage import pelicun_flood_foundation_depth_damage_sweep as _pelicun_flood_foundation_depth_damage_sweep  # noqa: E402,F401 - HAZUS flood depth-damage curve sensitivity to foundation type
# pelicun DL_calculation-driven TEMPLATES (engine="pelicun", tier="template") on
# the _dl_calculation CLI harness (tempdir + serialized cwd + to_thread): a full
# HAZUS earthquake building DL run (auto-populated building type) and the HAZUS EQ
# v5.1-vs-v6.1 dataset comparison.
from ..workflows.pelicun.hazus_seismic_dl_run.hazus_seismic_dl_run import pelicun_hazus_seismic_dl_run as _pelicun_hazus_seismic_dl_run  # noqa: E402,F401 - HAZUS earthquake building DL_calculation run with auto-populated building type
from ..workflows.pelicun.hazus_eq_version_comparison.hazus_eq_version_comparison import pelicun_hazus_eq_version_comparison as _pelicun_hazus_eq_version_comparison  # noqa: E402,F401 - HAZUS earthquake v5.1-vs-v6.1 damage/loss dataset comparison
from ..workflows.pelicun.hazus_lifeline_seismic_dl_run.hazus_lifeline_seismic_dl_run import pelicun_hazus_lifeline_seismic_dl_run as _pelicun_hazus_lifeline_seismic_dl_run  # noqa: E402,F401 - HAZUS earthquake lifeline-network (bridge/pipe/substation) DL_calculation run with auto-populated component


# the 12-category registry + the two meta-tools
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
