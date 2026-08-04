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
from .fetchers.hydrology.fetch_noaa_nwm_streamflow import fetch_noaa_nwm_streamflow  # noqa: E402,F401
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
# swmm_urban_flood TEMPLATE (engine="swmm", tier="template"), one folder under
# workflows/swmm/urban_flood/; EXCLUDED from the default retrieval pool,
# surfaced only by the run_swmm door's gate expansion. The composer chain
# (model_swmm_urban_flood) is inlined in the template module.
from ..workflows.swmm.urban_flood.urban_flood import swmm_urban_flood as _swmm_urban_flood  # noqa: E402,F401 - RENAME of run_swmm_urban_flood (engine=swmm, tier=template)
# telemac_river_dye TEMPLATE (engine="telemac", tier="template"), one folder
# under workflows/telemac/river_dye/; EXCLUDED from the default retrieval
# pool, surfaced only by the run_telemac door's gate expansion. The composer
# chain (model_telemac_river_dye) is inlined in the template module;
# workflows/telemac/run_telemac.py is the local solve seam
# (the door holds the run_telemac name; the template submits the solver).
from ..workflows.telemac.river_dye.river_dye import telemac_river_dye as _telemac_river_dye  # noqa: E402,F401 - NAME FLIP of run_telemac (engine=telemac, tier=template)
# hecras_muncie_flood TEMPLATE (engine="hecras", tier="template"), engine #11,
# one folder under workflows/hecras/muncie_flood/. TEMPLATE-FIRST: reparameterizes
# HEC's shipped Muncie White River (IN) demonstration deck (frozen geometry, scaled
# unsteady flow forcing). The composer chain (model_hecras_muncie_flood) is inlined
# in the template module; workflows/hecras/run_hecras.py is the local solve seam.
from ..workflows.hecras.muncie_flood.muncie_flood import hecras_muncie_flood as _hecras_muncie_flood  # noqa: E402,F401 - engine #11 (engine=hecras, tier=template)
# geoclaw_inundation TEMPLATE (engine="geoclaw", tier="template"), one folder
# under workflows/geoclaw/inundation/; EXCLUDED from the default retrieval
# pool, surfaced only by the run_geoclaw door's gate expansion. The composer
# chain (model_geoclaw_inundation) is inlined in the template module.
from ..workflows.geoclaw.inundation.inundation import geoclaw_inundation as _geoclaw_inundation  # noqa: E402,F401 - RENAME of run_geoclaw_inundation (engine=geoclaw, tier=template)
# swan_wave_field TEMPLATE (engine="swan", tier="template"), one folder under
# workflows/swan/wave_field/; EXCLUDED from the default retrieval pool,
# surfaced only by the run_swan door's gate expansion. The composer chain
# (model_swan_wave_field) is inlined in the template module.
from ..workflows.swan.wave_field.wave_field import swan_wave_field as _swan_wave_field  # noqa: E402,F401 - RENAME of run_swan_waves (engine=swan, tier=template)
# landlab_susceptibility TEMPLATE (engine="landlab", tier="template"), one
# folder under workflows/landlab/susceptibility/; EXCLUDED from the default
# retrieval pool, surfaced only by the run_landlab door's gate expansion. The
# composer chain (model_landlab_susceptibility) is inlined in the template
# module; workflows/landlab/run_landlab.py is the distinct solver build/stage
# seam.
from ..workflows.landlab.susceptibility.susceptibility import landlab_susceptibility as _landlab_susceptibility  # noqa: E402,F401 - RENAME of run_landlab_susceptibility (engine=landlab, tier=template)
# openquake_psha TEMPLATE (engine="openquake", tier="template"), one folder
# under workflows/openquake/psha/; EXCLUDED from the default retrieval pool,
# surfaced only by the run_openquake door's gate expansion. The composer chain
# (model_openquake_psha) is inlined in the template module.
from ..workflows.openquake.psha.psha import openquake_psha as _openquake_psha  # noqa: E402,F401 - RENAME of run_seismic_hazard_psha (engine=openquake, tier=template)
# elmfire_fire_spread TEMPLATE (engine="elmfire", tier="template"), one
# folder under workflows/elmfire/fire_spread/; EXCLUDED from the default
# retrieval pool, surfaced only by the run_elmfire door's gate expansion. The
# composer chain (model_elmfire_fire_spread) is inlined in the template
# module; workflows/elmfire/run_elmfire.py is the distinct solver build/stage
# seam.
from ..workflows.elmfire.fire_spread.fire_spread import elmfire_fire_spread as _elmfire_fire_spread  # noqa: E402,F401 - RENAME of model_fire_spread (engine=elmfire, tier=template)
# pelicun_damage_assessment TEMPLATE (engine="pelicun", tier="template")
# under workflows/pelicun/damage_assessment/; EXCLUDED from the default
# retrieval pool, surfaced only by the run_pelicun door's gate expansion. Its
# bbox AUTO-FETCH input mode covers the buildings-composer path (one tool,
# two input modes). postprocess_pelicun STAYS general, NOT a template.
from ..workflows.pelicun.damage_assessment.damage_assessment import pelicun_damage_assessment as _pelicun_damage_assessment  # noqa: E402,F401 - FOLD of run_pelicun_damage_assessment + run_pelicun_with_buildings (engine=pelicun, tier=template; explicit assets_uri OR bbox auto-fetch)


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
