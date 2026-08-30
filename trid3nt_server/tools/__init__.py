"""Atomic-tool registry skeleton.

This package (``trid3nt_server.tools``) is the fetcher/tool surface: specs,
router, emit-on-fetch, resolution specs, and the registry that collects the
decorated functions at import time plus the cache shim that mediates
external-API calls (see ``.cache``). ``AtomicToolMetadata`` lives in
``trid3nt_contracts.tool_registry``.
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
  a tool whose metadata fails the uncacheable-cross-field rule.
- Stores ``(fn, metadata, module)`` in module-level ``TOOL_REGISTRY``
  keyed by ``metadata.name``.
- **Fails fast on duplicate names**: a second registration under
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
``ToolRegistrationError`` surfaces at startup (fail-fast).
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
    "MOUNTED_TOOLS",
    "mount_tool",
    "mounted_tool_names",
    "unmount_tool",
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

    Fail-fast invariants:

    - ``metadata`` must already be a valid ``AtomicToolMetadata`` (pydantic
      auto-validates at construction, including the uncacheable cross-field
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


#: Names in ``TOOL_REGISTRY`` that a live session MOUNTED rather than an import
#: registered. They come and go with the thing they act on, so every visibility
#: floor must carry them by name: the retrieval index is built from the tools
#: that existed when it was built and can never rank one of these.
MOUNTED_TOOLS: set[str] = set()


def mount_tool(metadata: AtomicToolMetadata,
               fn: Callable[..., Any]) -> str:
    """Add one session-scoped tool to the registry -> its name.

    A name already registered - mounted or imported - is refused rather than
    replaced: the caller would be shadowing a tool it does not own.
    """
    name = metadata.name
    existing = TOOL_REGISTRY.get(name)
    if existing is not None:
        raise ToolRegistrationError(
            f"tool {name!r} is already registered (from module "
            f"{existing.module!r}); a mounted tool cannot shadow it.")
    TOOL_REGISTRY[name] = RegisteredTool(
        metadata=metadata, fn=fn, module=getattr(fn, "__module__", "<mounted>"))
    MOUNTED_TOOLS.add(name)
    return name


def unmount_tool(name: str) -> None:
    """Remove a MOUNTED tool. An imported tool is never removed by this seam."""
    if name not in MOUNTED_TOOLS:
        return
    MOUNTED_TOOLS.discard(name)
    TOOL_REGISTRY.pop(name, None)


def mounted_tool_names() -> frozenset[str]:
    """The currently mounted tool names, as a visibility floor."""
    return frozenset(MOUNTED_TOOLS)


def get_registered_tools() -> list[RegisteredTool]:
    """Return a stable-ordered snapshot of the current registry.

    Used by the agent service at startup to build the Bedrock Converse tool
    declarations (raw SDK loop in ``adapter.py``). Sorted by ``metadata.name``
    so the registration order is deterministic across runs (important for
    review diffs).
    """
    return sorted(TOOL_REGISTRY.values(), key=lambda t: t.metadata.name)


def clear_registry_for_tests() -> None:
    """Empty the registry. ONLY for tests; never call from product code.

    Atomic-tool registration is import-time; tests that need a fresh registry
    or want to swap implementations call this in a fixture.
    """
    TOOL_REGISTRY.clear()
    MOUNTED_TOOLS.clear()


# ---------------------------------------------------------------------------
# Eager submodule import (fail-fast).
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
# fetch_glm_lightning: animation_frames fold -- twin DELETED, now spec-driven
# (source.yaml + glm.frames_plan/frame_bytes hooks; default output = a frames list), auto-
# registered by _register_router_specs() below; no eager twin import.
# fetch_hrrr_forecast + fetch_hrrr_smoke: HRRR-Zarr library-delegate fold
# -- twins DELETED, now spec-driven (source.yaml + hrrr.resolve_cycle/read/validate
# delegate hooks: s3fs cycle-walk delegate_resolve + Zarr open -> LCC->4326 reproject
# + clip + forecast hypot(u,v)); auto-registered by _register_router_specs() below.
# fetch_mrms_qpe: weather/GRIB fold -- twin DELETED, now spec-driven
# (source.yaml + mrms_qpe hooks: S3-listed key resolve -> grib_object whole-object COG).
# show_nexrad_radar (a DISPLAY tool, NOT a fetcher: composes a live WMS GetMap URL,
# transfers no bytes) is imported from the -- display -- group below.
# fetch_nws_alerts_conus: data-router fold chained-resolution mode -- twin
# DELETED, now spec-driven (source.yaml + nws_alerts_conus hooks: single /alerts/active
# GET + per-alert zone-polygon enrichment), registered by _register_router_specs() below.
# fetch_nws_event: data-router fold tier-3 hooks -- twin DELETED, now
# spec-driven (source.yaml + nws_event build_request/parse_response hooks, single-GET
# NWS /alerts/active GeoJSON), registered by _register_router_specs() below.
# fetch_storm_events_db: data-router fold -- twin DELETED, now spec-driven
# (source.yaml + storm_events_db hooks: directory-index resolve -> newest bulk-CSV URL,
# then bulk gzip-CSV point decode), registered by _register_router_specs() below.
# fetch_storm_tracks FOLDED to a spec-driven surface: its source.yaml +
# storm_tracks library-delegate hooks (historical IBTrACS + active NHC incl. the binary
# forecast-zip secondary-enrichment round) are promoted by register_specs_from_tree;
# the StormTracks*Error twins live in _router/hooks/storm_tracks.py. No eager twin import.

# -- fetchers/hydrology --
# V&V wave (lane C): observed flood-validation data fetchers.
# fetch_flood_extent_observation FOLDED to a spec-driven surface: its
# source.yaml + categorical_tile_grid mode is promoted by register_specs_from_tree.
# fetch_high_water_marks FOLDED to a spec-driven surface: its source.yaml
# + usgs_stn_hwm hooks register at import via register_specs_from_tree (envelope hook).
# fetch_jrc_global_surface_water FOLDED to a spec-driven surface: its
# source.yaml + the stac_continuous_mosaic access mode + the jrc_global_surface_water
# colormap hook register at import via register_specs_from_tree (twin DELETED).
# fetch_nhd_waterbodies: data-router fold phase-2 wave-2 -- twin DELETED, now
# spec-driven (source.yaml + router), registered by _register_router_specs() below.
# fetch_nhdplus_nldi_navigate: data-router fold phase-2 wave-3 -- twin
# DELETED, now spec-driven (source.yaml + dataretrieval-delegating router),
# registered by _register_router_specs() below.
# fetch_noaa_nwm_streamflow FOLDED to a spec-driven surface (the LAST coded
# data-fetcher): its source.yaml + the nwm_streamflow.* library-
# delegate hooks (the S3 channel_rt netCDF read -> {feature_id: streamflow} lookup + the NLDI
# 5x5 spatial sample -> COMIDs + per-reach geometry + JOIN -> point FGB, over the fetch-time
# provenance channel) register at import via register_specs_from_tree; the NWMStreamflow*Error
# twins live in _router/hooks/nwm_streamflow.py. No eager twin import.
# fetch_nws_river_forecast: data-router fold chained-resolution mode -- twin
# DELETED, now spec-driven (source.yaml + nws_river_forecast hooks: gauges-by-bbox / single
# detail + bounded per-gauge threshold/stageflow enrichment), registered below.
# fetch_river_geometry: Overpass-family river fold -- twin DELETED, now
# spec-driven (source.yaml + overpass_river build_request/parse_response hooks over the
# http_json endpoint_fallback mirror chain); the vestigial NHDPlus HR HUC4 leg was
# dropped (NATE-decided). Auto-registered by _register_router_specs() below.
# fetch_usgs_nwis_gauges: CDS-era flood-seam fold -- twin DELETED, now
# spec-driven (source.yaml + parse_fallback IV->Site + usgs_nwis hooks), auto-registered.
# fetch_usgs_water_quality: data-router fold phase-2 wave-3 -- twin
# DELETED, now spec-driven (source.yaml + dataretrieval-delegating router),
# registered by _register_router_specs() below.

# -- fetchers/ocean --
# fetch_gtsm_tide_surge: CDS library_delegate fold -- twin DELETED, now
# spec-driven (source.yaml + cds.gtsm_read), auto-registered via _register_router_specs().
# fetch_noaa_coops_currents: data-router fold phase-2 wave-4 -- twin
# DELETED, now spec-driven (source.yaml + the station snapshot router mode),
# registered by _register_router_specs() below.
# fetch_noaa_coops_tides: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_noaa_slr_confidence + fetch_noaa_slr_marsh: raster-stragglers wave --
# twins DELETED, now spec-driven (source.yaml + the router mapserver_export
# RGBA mode), registered by _register_router_specs() below.
# fetch_noaa_slr_scenarios: data-router fold phase-2 wave-6 -- twin
# DELETED, now spec-driven (source.yaml + the declarative fan-out router mode),
# registered by _register_router_specs() below.
# fetch_noaa_sst: quick-folds wave -- twin DELETED, now spec-driven
# (source.yaml + the raster_cog griddap access mode), registered by _register_router_specs().
# fetch_topobathy: coastal topo-bathymetry fold -- twin DELETED, now
# spec-driven (source.yaml + the topobathy.* library-delegate hooks over the 4-leg
# UTM composite + the fetch-time provenance channel), registered by
# _register_router_specs() below.

# -- fetchers/terrain --
# fetch_3dep_extra: library-delegate raster fold -- twin DELETED, now
# spec-driven (source.yaml + pfdf_3dep delegate/validate hooks over the generic
# library_delegate mode; per-resolution payload table);
# auto-registered by _register_router_specs() below.
# fetch_copernicus_dem: fetcher-fold wave-8 -- twin DELETED, now spec-driven
# (source.yaml + router stac_float), registered by _register_router_specs() below.
# fetch_dem FOLDED to a spec-driven surface: its source.yaml +
# library_delegate py3dep hooks + the source="copernicus" cross-sibling dispatch
# is promoted by register_specs_from_tree; no eager twin import.
# fetch_esri_landcover_10m: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_landcover FOLDED to a spec-driven surface: its source.yaml +
# wcs_getcoverage mode + sidecar envelope is promoted by register_specs_from_tree.

# -- fetchers/imagery --
# fetch_goes_animation + fetch_goes_blend_animation FOLDED to spec-driven surfaces
#; fetch_goes_archive_animation + fetch_goes_active_fire FOLDED:
# source.yaml + shape: animation_frames + goes_archive.frames_plan / frame_bytes over
# the shared imagery._goes_archive_core substrate (netcdf_cf_object per-frame mode),
# auto-registered by register_specs_from_tree. The substrate lives on in
# imagery/_goes_archive_core.py (no registered tool).
# fetch_goes_satellite FOLDED to a spec-driven surface: its source.yaml +
# goes_satellite library-delegate raster hooks (most-recent MCMIPC read + 15-min
# valid_time cache rounding) are promoted by register_specs_from_tree; the shared
# satellite-identifier / S3 substrate lives on in imagery/_goes_common.py (no registered
# tool). No eager twin import.
# fetch_landsat_imagery / fetch_naip / fetch_sentinel2_truecolor: STAC-composite wave
# -- twins DELETED, now spec-driven (source.yaml + raster_cog
# stac_multi_asset_rgb: N reflectance assets + QA/SCL mask + joint 2/98 stretch /
# inferno LST / raw uint8 passthrough), auto-registered by _register_router_specs().
# fetch_slider_timestamps folded to a record-shape spec; the promoted
# tool auto-registers via register_specs_from_tree (SLIDER availability + cadence index).
# fetch_sentinel1_sar: quick-folds wave -- twin DELETED, now spec-driven
# (source.yaml + raster_cog stac_float + coverage-select + log10_db), auto-registered.
# fetch_viirs_day_fire FOLDED to a spec-driven surface: source.yaml +
# shape: animation_frames + viirs_day_fire.frames_plan / frame_bytes (JPSS polar
# day-pass SLIDER stitch), auto-registered by register_specs_from_tree.

# -- fetchers/climate --
# fetch_chirps_precipitation: data-router fold phase-2 wave-9 -- twin
# DELETED, now spec-driven (source.yaml + gzip_object), auto-registered.
# fetch_era5_reanalysis: CDS library_delegate fold -- twin DELETED, now
# spec-driven (source.yaml + cds.era5_read), auto-registered via _register_router_specs().
# fetch_gridmet: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_modis_lst: data-router fold phase-2 wave-7 -- twin DELETED, now
# spec-driven (source.yaml + stac_float continuous-float mode), registered by
# _register_router_specs() below.
# fetch_us_drought_monitor: data-router fold phase-2 wave-2 -- twin DELETED, now
# spec-driven (source.yaml + router), registered by _register_router_specs() below.
from .fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: E402,F401

# -- fetchers/biodiversity --
# fetch_gbif_occurrences / fetch_inaturalist_observations: data-router fold chained-
# resolution mode -- twins DELETED, now spec-driven (source.yaml + the
# resolve-then-fetch hooks: name->id species/match | /v1/taxa GET, then the offset-paged
# occurrence / observation search), registered by _register_router_specs() below.
# fetch_movebank_tracks: keyed CSV http_json fold -- twin DELETED, now
# spec-driven (source.yaml + movebank_tracks build_request/parse_response/classify_status
# hooks; composite Basic-Auth via the resolver blob path), auto-registered by
# _register_router_specs() below.

# -- fetchers/socioeconomic --
# fetch_administrative_boundaries: data-router fold zip/multi-file wave --
# twin DELETED, now spec-driven (source.yaml + admin_boundaries.build_request FIPS
# planner + the zip_vector whole-object extract executor), registered by
# _register_router_specs() below.
# fetch_buildings: sidecar-write fold (trigger wave) -- twin DELETED, now
# spec-driven (source.yaml + buildings.build_request/parse hooks + the overpass_sidecar
# executor's constrained tags.json side write), promoted by _register_router_specs().
# fetch_cdc_svi: data-router fold phase-2 wave-2 -- twin DELETED, now spec-driven
# (source.yaml + router), registered by _register_router_specs() below.
# fetch_census_acs: data-router fold pilot -- twin DELETED, now spec-driven
# (source.yaml + router JOIN transform), registered by _register_router_specs() below.
# fetch_epa_ejscreen: data-router fold phase-2 wave-6 -- twin DELETED,
# now spec-driven (source.yaml + the esri_json ingest mode + percentile/fraction/
# raw column kinds + from_param routing), registered by _register_router_specs().
# fetch_field_boundaries: FTW/fiboa GeoParquet-pushdown fold -- twin DELETED,
# now spec-driven (source.yaml + field_boundaries.select pre_resolve + field_boundaries.read
# VECTOR library_delegate hook; the GeoParquet 1.1 row-group bbox pushdown is owned by
# geopandas.read_parquet over an fsspec HTTPS handle, not a router transport -- the
# new-transport STOP refuted). Auto-registered by _register_router_specs() below.
# fetch_ghsl_population: data-router fold zip/multi-file wave -- twin
# DELETED, now spec-driven (source.yaml + the raster fixed_tile_grid whole-object
# per-tile ZIP extract mode), registered by _register_router_specs() below.
# fetch_hrsl_population: data-router fold phase-2 wave-9 -- twin
# DELETED, now spec-driven (source.yaml + multi_url VRT fan-out), auto-registered.
# fetch_lehd_jobs: join VALUES-hook fold (trigger wave) -- twin DELETED,
# now spec-driven (source.yaml + join transform + lehd_jobs.values_plan/values_parse
# for the per-state LODES bulk gzip-CSV values leg), promoted by _register_router_specs().
# fetch_overpass_pois + fetch_roads_osm: Overpass-family fold -- twins
# DELETED, now spec-driven (source.yaml + overpass build_request/parse_response
# hooks over the http_json endpoint_fallback mirror chain), auto-registered by
# _register_router_specs() below.
# fetch_population: WorldPop library_delegate raster fold -- twin DELETED, now
# spec-driven (source.yaml + worldpop.validate/read hooks); the half-built ACS leg dropped
# (fetch_census_acs serves tract population), auto-registered below; no eager twin import.
# fetch_usace_nsi: data-router fold tier-3 hooks -- twin DELETED, now
# spec-driven (source.yaml + usace_nsi build_request/parse_response hooks + the
# RequestPlan POST transport extension), registered by _register_router_specs() below.
from .fetchers.socioeconomic.geocode_location import geocode_location  # noqa: E402,F401

# -- fetchers/hazard --
# fetch_fault_sources: finisher-mechanisms wave -- twin DELETED, now
# spec-driven (source.yaml + fault_sources build_request/parse_response/envelope
# hooks + the constant_cache two-tier cache + the variant_by_emptiness output
# switch), auto-registered by _register_router_specs() below.
# fetch_firms_active_fire: quick-folds wave -- twin DELETED, now spec-driven
# (source.yaml + firms_active_fire keyed CSV http_json hooks), auto-registered.
# fetch_hifld_critical_infrastructure: data-router fold pilot -- twin DELETED, now
# spec-driven (source.yaml + router), registered by _register_router_specs() below.
# fetch_hifld_transmission_lines: data-router fold phase-2 wave-2 -- twin DELETED,
# now spec-driven (source.yaml + router), registered by _register_router_specs().
# fetch_landfire_fuels: data-router fold phase-2 wave-7 -- twin DELETED,
# now spec-driven (source.yaml + imageserver_export mode), registered by
# _register_router_specs() below.
# fetch_mtbs_burn_severity + fetch_nifc_fire_perimeters: data-router fold phase-2
# wave-2 -- twins DELETED, now spec-driven (source.yaml + router), registered by
# _register_router_specs() below.
# fetch_openfema_disasters: data-router fold -- twin DELETED, now spec-driven
# (source.yaml + openfema_disasters hooks: offset paging + per-county aggregate joined to
# TIGERweb county polygons by FIPS), registered by _register_router_specs() below.
# fetch_tsunami_events: data-router fold phase-2 wave-10 (tier-3 hooks) --
# twin DELETED, now spec-driven (source.yaml + ncei_tsunami build_request/parse_response
# hooks + paging), registered by _register_router_specs() below.
# fetch_usace_levees: data-router fold phase-2 wave-6 -- twin DELETED,
# now spec-driven (source.yaml + endpoint_by_param sub-layer routing +
# properties_by_param), registered by _register_router_specs() below.
# fetch_usfs_canopy_fuels: data-router fold phase-2 wave-7 -- twin
# DELETED, now spec-driven (source.yaml + imageserver_export mode), registered by
# _register_router_specs() below.
# fetch_usgs_earthquakes: data-router fold phase-2 wave-10 (tier-3 hooks) --
# twin DELETED, now spec-driven (source.yaml + usgs_earthquakes build_request/parse_response
# hooks, single-GET FDSN GeoJSON), registered by _register_router_specs() below.
# fetch_usgs_volcano_alerts: data-router fold phase-2 wave-10 (tier-3 hooks) --
# twin DELETED, now spec-driven (source.yaml + usgs_volcano build_request/parse_response
# hooks, multi-GET HANS join), registered by _register_router_specs() below.
# fetch_wfigs_incident: record-return output-shape fold -- twin DELETED,
# now spec-driven (source.yaml + wfigs_incident build_request/record hooks over the
# shape=record executor; a bare discovery dict, not a LayerURI), auto-registered by
# _register_router_specs() below.

# -- fetchers/soil --
# fetch_gcn250_curve_numbers: fetcher-fold wave-8 -- twin DELETED, now spec-driven
# (source.yaml + router direct_window), registered by _register_router_specs() below.
# fetch_soilgrids FOLDED to a spec-driven surface: its source.yaml + the
# projected_vrt_window access mode (Homolosine VRT windowed in the source projection +
# native->4326 bilinear reproject + per-property Int16 scale) register at import via
# register_specs_from_tree (twin DELETED).
# fetch_statsgo_soils: library-delegate raster fold -- twin DELETED, now
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
from .display.restyle_layer.restyle_layer import restyle_layer  # noqa: E402,F401 - DISPLAY-state re-emission of an already-published layer
from .display.show_nexrad_radar.show_nexrad_radar import show_nexrad_radar  # noqa: E402,F401

# -- processing (compute / clip / extract / vector-edit / charts) --
from .processing.clip_raster_to_polygon import clip_raster_to_polygon  # noqa: E402,F401
# The two generic geometry composition links: one document out of several layers
# (``combine``), and the two ends of a line (``endpoints``). Both exist so a
# domain narrows by CHAINING tools rather than by a mesher growing a domain-prep
# of its own.
from .processing.combine import combine  # noqa: E402,F401
from .processing.compute_aspect import compute_aspect  # noqa: E402,F401
from .processing.compute_blended_composite import compute_blended_composite  # noqa: E402,F401
from .processing.compute_building_density import compute_building_density  # noqa: E402,F401
from .processing.compute_change_detection import compute_change_detection  # noqa: E402,F401
from .processing.compute_colored_relief import compute_colored_relief  # noqa: E402,F401
from .processing.compute_contours import compute_contours  # noqa: E402,F401
from .processing.compute_cross_section import compute_cross_section  # noqa: E402,F401
from .processing.compute_exposure_summary import compute_exposure_summary  # noqa: E402,F401
from .processing.compute_flood_depth_damage import compute_flood_depth_damage  # noqa: E402,F401
# V&V wave (lane B): flood-extent skill (raster/vector confusion).
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
# V&V wave (lane B): model-fit skill-metrics wrap (spotpy).
from .processing.compute_skill_metrics import compute_skill_metrics  # noqa: E402,F401
from .processing.compute_slope import compute_slope  # noqa: E402,F401
# compute_zonal_statistics DEMOTED to the code_exec playground
# (docs/playbooks/zonal-statistics-recipe.md, docs/decisions/0043).
from .processing.delineate_watershed import delineate_watershed  # noqa: E402,F401
from .processing.digitize_water_body import digitize_water_body  # noqa: E402,F401
from .processing.enhance_satellite_image import enhance_satellite_image  # noqa: E402,F401
from .processing.endpoints import endpoints  # noqa: E402,F401
from .processing.extract_landcover_class import extract_landcover_class  # noqa: E402,F401
# V&V wave (lane C): model-vs-observation pairing primitive.
from .processing.extract_model_at_observations import extract_model_at_observations  # noqa: E402,F401
from .processing.extract_stream_network import extract_stream_network  # noqa: E402,F401
from .processing.extract_timeseries_at_point import extract_timeseries_at_point  # noqa: E402,F401
from .processing.charts.generate_chart import generate_chart  # noqa: E402,F401
from .processing.query_point_hazard import query_point_hazard  # noqa: E402,F401
from .processing.section import section  # noqa: E402,F401
# DuckDB spatial-query fold (Phase B): ONE read-only SQL surface replaces the
# three analytical Q&A tools (summarize_layer_statistics /
# count_features_above_threshold / aggregate_property_within_zone).
from .processing.spatial_query import spatial_query  # noqa: E402,F401

# -- simulation (engine bridges, model_* engines, solver seam) --
# Run-diagnostics dispatcher: one registered tool over the per-engine parser
# modules under workflows/solver/diagnostics/, which are NOT themselves registered.
from trid3nt_server.workflows.solver.diagnostics import read_run_diagnostics  # noqa: E402,F401
from trid3nt_server.tools.processing.model_debris_flow import model_debris_flow  # noqa: E402,F401
# RERUN-WITH-OVERRIDES: derive a run from a run, with named values moved. The
# skeleton's recalibration interface - what-if, failure recovery, calibration
# loops.
from trid3nt_server.workflows.lib.rerun import rerun_workflow  # noqa: E402,F401
from trid3nt_server.workflows.solver import solver  # noqa: E402,F401
# -- engine templates: tier=template members are ordinary retrieval-pool tools,
# registered by their own @register_tool in the workflow-composer block below and
# callable DIRECTLY. The solver-seam module workflows/telemac/run_telemac.py
# (WorkflowError classes, solver specs) is separate; the templates import it.

# -- discovery (dataset/tool retrieval) --
# NOTE: search_data_catalog / fetch_from_catalog / qgis_discovery register at
# daemon startup via main.py's eager-import block, NOT here - importing this
# package alone deliberately leaves them out of TOOL_REGISTRY (pre-reorg
# behavior: the plain ``import trid3nt_server.tools`` surface is 190 tools,
# 191 after added search_spatial_functions here).
from .search.search_tools import search_tools  # noqa: E402,F401
from .search.search_spatial_functions import search_spatial_functions  # noqa: E402,F401
# ESRI Living Atlas: a scoped search over the harvested catalog + a
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

# ---------------------------------------------------------------------------
# Workflow-composer registrations (each carries its OWN @register_tool) and
# the 12-category registry meta-tools. Comments preserved from the original
# registration list.
# ---------------------------------------------------------------------------
# telemac_river_dye + telemac_do_sag TEMPLATES (engine="telemac", tier="template"),
# workflows/telemac/river_dye/ + do_sag/: the two REACH fronts. Each narrows its
# domain by CHAINING processing tools - the NLDI mainstem names the stretch, its
# endpoints name where the stretch stops, and the section cut through the mapped
# NHDArea banks is the polygon om2d triangulates - so the two end faces are the
# transects the inflow and the outflow are prescribed on. The edge length is an
# explicit sheet value on both.
from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye as _telemac_river_dye  # noqa: E402,F401 - reach tracer/morphodynamics front (engine=telemac, tier=template)
from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag as _telemac_do_sag  # noqa: E402,F401 - reach dissolved-oxygen front (engine=telemac, tier=template)
# telemac_rain_on_grid is DECLARED PARKED (register_workflow(parked=...)): the
# module imports here like every other template, its plan validates, and the tool
# is simply never registered - so the model never sees it and the roster does not
# depend on import order. It declares its chain (delineate_watershed -> combine ->
# om2d over the basin), but its mesh STEP still reads the retired catchment
# mesher's fields off the declaration. Unparking is the one keyword on its
# register_workflow call. What landed, what did not, and the failures it leaves:
# docs/design/worker-unification-port.md.
from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import telemac_rain_on_grid as _telemac_rain_on_grid  # noqa: E402,F401 - parked: declared, off the model surface
# tomawac_wave_field TEMPLATE (engine="telemac", tier="template"), workflows/
# telemac/wave_field/: the TOMAWAC third-generation spectral-wave engine.
# ONE question-class tool, four modes (fetch_growth / shoaling /
# bottom_friction / wave_current); real Great Lakes lake-datum bathymetry or an
# idealized basin. Physics proven through the baked tomawac binary; the
# refinement-grade complement to SFINCS/SnapWave coastal screening.
from trid3nt_server.workflows.telemac.wave_field.wave_field import tomawac_wave_field as _tomawac_wave_field  # noqa: E402,F401 - TOMAWAC wave front (engine=telemac, tier=template)
# artemis_harbor_agitation TEMPLATE (engine="telemac", tier="template"),
# workflows/telemac/agitation/: the ARTEMIS phase-resolving elliptic mild-slope
# (Berkhoff) harbour-agitation engine. ONE question-class tool, three
# modes (diffraction / resonance / shoal); real Great Lakes lake-datum bathymetry
# with a schematic breakwater, or an idealized analytic domain. Physics proven
# through the baked artemis binary; the phase-resolving complement to the TOMAWAC
# spectral tier.
from trid3nt_server.workflows.telemac.agitation.agitation import artemis_harbor_agitation as _artemis_harbor_agitation  # noqa: E402,F401 - ARTEMIS agitation front (engine=telemac, tier=template)
# coastal_tidal_surge TEMPLATE (engine="telemac", tier="template"), workflows/
# telemac/coastal_tidal_surge/: the coastal tidal/surge inundation front.
# ONE question-class tool, two series types (observed storm surge /
# astronomical prediction); a real-topobathy open-water domain with ONE seaward
# liquid boundary driven by a NOAA CO-OPS series through the LIQUID BOUNDARIES
# FILE. Physics proven through the baked telemac2d binary (Apalachicola / Michael
# 220x); the storm-tide complement to the SFINCS coastal screening.
from trid3nt_server.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge import coastal_tidal_surge as _coastal_tidal_surge  # noqa: E402,F401 - coastal tidal/surge front (engine=telemac, tier=template)
# telemac3d_stratified_flow TEMPLATE (engine="telemac", tier="template"),
# workflows/telemac/stratified_flow/: the TELEMAC-3D three-dimensional baroclinic
# Navier-Stokes engine - the one genuinely NEW solver leg in the family.
# ONE question-class tool, three modes (stratification / wind_circulation /
# salt_wedge); real Great Lakes lake-datum bathymetry (thermal / wind modes) or an
# idealized closed basin. Physics proven through the baked telemac3d binary; the 3D
# refinement tier that unblocks the AED2 lake-ecology + dune-migration STOPs.
from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import telemac3d_stratified_flow as _telemac3d_stratified_flow  # noqa: E402,F401 - TELEMAC-3D stratified front (engine=telemac, tier=template)
# build_mesh: the one mesh router. A parametric spec plus a per-mesher registry of
# named edit actions wrapping the official mesh libraries; declared in a template it
# is a frozen lazy ask, called standalone it builds now and stashes the artifact in
# the case. Importing it registers every mesher behind it - the catchment, the
# coastal water edge, the river corridor, the HEC-RAS graded cell mesh and the
# regular grid - and emits an MDAL display layer plus the durable mesh artifact a
# model template discovers through the precondition gate.
from trid3nt_server.workflows.mesh.tool import build_mesh as _build_mesh  # noqa: E402,F401 - mesh domain primitive (tier=general)


# COPY-ME authoring template (docs/authoring/writing-a-tool.md). Importing the
# module is always safe: its @register_tool call is gated behind the
# TRID3NT_ENABLE_EXAMPLE_TOOL env flag, so it registers example_bbox_area ONLY
# when a developer explicitly enables it (demo / retrieval-visibility check).
# Default = imported-but-inert, so it never pollutes the production catalog.
from . import _example_tool_template  # noqa: E402,F401 - INERT unless TRID3NT_ENABLE_EXAMPLE_TOOL is set
