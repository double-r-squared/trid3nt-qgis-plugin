"""Tier-3 hook contract: the named, registered, PURE extension points.

A source whose bespoke-ness is a single clean irreducible step the declarative
param/ingest surface cannot express references a registered pure function by name
in its ``source.yaml`` (``hooks.build_request`` / ``hooks.parse_response``). This
package is that function set: a name -> callable table (:data:`HOOK_REGISTRY`), the
:func:`register_hook` decorator that fills it, and :func:`resolve_hook` /
:func:`has_hook` the router + registration read.

DOCTRINE (data-router-fold.md, tier-3): hooks are PURE, MINIMAL, REGISTERED,
TESTED. Pure = no I/O (transport, caching, gates, stamps, and the typed-error
FACTORY machinery stay router-owned; a hook only computes and MAY call a shared
``router_*_error`` factory to raise a source-stamped typed error). Minimal = a
hook point exists only because a real source needs it. Registered = referenced by
a name string a spec load validates. Tested = each hook module carries its own
unit tests.

Hook signatures:
- ``build_request(spec, params) -> list[RequestPlan]`` -- source-specific
  request construction + bespoke pre-fetch input validation. 1..N plans.
- ``parse_response(spec, params, bodies: list[bytes]) -> list[dict]`` -- decode
  the source payload(s) into GeoJSON-ish point features; raise the honest-empty /
  too-large / bad-body typed errors.

Chained-resolution mode adds five PURE points for the resolve-then-fetch
/ bounded per-item enrichment shape; the router owns every round trip + the loops:
- ``resolve_build(spec, params) -> list[RequestPlan]`` -- round-1 name->id request(s)
  (or ``[]`` to skip); ``resolve_parse(spec, params, bodies) -> dict`` -- the resolved
  id as a params-merge (runs pre-cache-key so name+id collapse).
- ``next_page(spec, params, bodies) -> RequestPlan | None`` -- offset-paging loop
  control (next page or stop).
- ``enrich_plan(spec, params, features) -> list[(ref_key, RequestPlan)]`` -- per-item
  detail requests; ``enrich_merge(spec, params, features, results) -> list[dict]`` --
  fold the deduped/bounded/best-effort detail back in (every feature survives).

Envelope mode adds the POST-EMIT point for a LayerURI-SUBCLASS result:
- ``envelope(spec, params, layer, data: bytes) -> dict`` -- the last hook the router
  calls; over the assembled ``LayerURI`` + the produced bytes it computes the extra
  business fields (breakdowns / caveats / notes) for the spec's
  ``output.result_model`` subclass. PURE (no transport); the router drops the
  honesty-floor-owned ``uri`` / ``layer_type`` keys so a hook can only enrich.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("trid3nt_server.data.fetchers._router.hooks")

__all__ = [
    "RequestPlan",
    "FramePlan",
    "FrameDegraded",
    "HOOK_REGISTRY",
    "register_hook",
    "resolve_hook",
    "has_hook",
    "HookResolutionError",
]


@dataclass(frozen=True)
class RequestPlan:
    """One request the router transport executes on a ``build_request`` hook's behalf.

    PURE data (no socket): the hook decides the URL / query params / headers /
    method / JSON body; the router owns the actual GET or POST, its retry
    authority, and typed transport errors.

    ``method`` defaults to ``"GET"`` (every prior hook). ``"POST"`` sends
    ``json_body`` as a JSON request body -- the write-method REST shape whose
    query is a body, not a query string (USACE NSI's structures POST) -- or, when
    ``data`` is set instead, a form-encoded body (the Overpass interpreter reads
    its QL from the ``data`` form field). No I/O still happens in the hook: it
    only DESCRIBES the request.
    """

    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class FramePlan:
    """One frame of a ``shape: animation_frames`` sequence.

    PURE data: the ``frames_plan`` hook produces the ORDERED list of these (the
    pre-loop window/subsample already applied), and the ``animation_frames``
    executor drives one ``read_through`` per frame + emits a ``LayerURI`` per frame.

    - ``cache_params`` -- the per-frame read_through cache key (byte-identical to
      the hand-written twin's per-frame params so the fold reuses cached frames).
    - ``name`` -- the emitted ``LayerURI.name``, which MUST carry the monotonic
      scrubber NAME-TOKEN (``step <N>`` + the ISO valid-time) the plugin
      ``render/temporal.py group_frame_layers`` groups on.
    - ``layer_id`` -- the emitted ``LayerURI.layer_id`` (a per-product stem keeps
      sibling products in separate scrubber groups).
    - ``bbox`` -- the AOI bbox stamped on every frame's ``LayerURI.bbox``.
    - ``fetch_context`` -- OPTIONAL out-of-cache-key fetch inputs the frame_bytes
      hook needs but that must NOT enter the read_through key: the raw
      MCMIPC S3 object key + the raw (unrounded) fetch args for an archive frame,
      which is addressed by an opaque per-scan key the ts-addressed SLIDER frames
      never carried. Defaulted empty -> a strict no-op for the wave-1 SLIDER frames.
    """

    cache_params: dict[str, Any]
    name: str
    layer_id: str
    bbox: tuple[float, float, float, float]
    fetch_context: dict[str, Any] = field(default_factory=dict)
    #: OPTIONAL per-frame style_preset override. The archive source emits
    #: distinct bands with distinct presets (goes_rgb_animation for the RGB composites,
    #: goes_fire_hotspots_rgba for the transparent hotspot RGBA) that a single
    #: spec.output.style_preset cannot carry; None -> the executor falls back to
    #: spec.output.style_preset (a strict no-op for the wave-1 single-preset SLIDER frames).
    style_preset: str | None = None


class FrameDegraded(Exception):
    """A ``frame_bytes`` hook raises this to skip ONE degraded frame.

    A transparent / off-swath / upstream-failed single frame is a RECORDED
    degradation the executor drops (never a silent gap); the executor's honesty
    floor raises the source's typed EMPTY error only when EVERY frame degrades.
    ``message`` is preserved for the all-frames-failed error text.
    """


class HookResolutionError(ValueError):
    """A spec referenced a ``hooks.*`` name absent from :data:`HOOK_REGISTRY`."""


#: name -> pure callable. Filled by :func:`register_hook` at hook-module import.
HOOK_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_hook(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a pure hook under ``name`` (``<source_key>.<point>``).

    A duplicate name is a defect (two hooks would answer one spec reference), so
    it raises rather than silently last-wins.
    """

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in HOOK_REGISTRY and HOOK_REGISTRY[name] is not fn:
            raise HookResolutionError(f"duplicate hook name {name!r}")
        HOOK_REGISTRY[name] = fn
        return fn

    return _wrap


def resolve_hook(name: str) -> Callable[..., Any]:
    """Return the registered hook for ``name`` or raise :class:`HookResolutionError`."""
    fn = HOOK_REGISTRY.get(name)
    if fn is None:
        raise HookResolutionError(
            f"no hook registered under {name!r}; known: {sorted(HOOK_REGISTRY)}"
        )
    return fn


def has_hook(name: str) -> bool:
    """True iff ``name`` resolves in :data:`HOOK_REGISTRY`."""
    return name in HOOK_REGISTRY


# Import the hook modules so their ``@register_hook`` decorators populate the
# registry at package import (registration validates names against it at load).
from . import usgs_earthquakes  # noqa: E402,F401
from . import ncei_tsunami  # noqa: E402,F401
from . import usgs_volcano  # noqa: E402,F401
from . import nws_event  # noqa: E402,F401
from . import usace_nsi  # noqa: E402,F401
# chained-resolution mode hooks.
from . import gbif_occurrences  # noqa: E402,F401
from . import inaturalist_observations  # noqa: E402,F401
from . import nws_alerts_conus  # noqa: E402,F401
from . import nws_river_forecast  # noqa: E402,F401
# offset paging + boundary-service FIPS enrich.
from . import openfema_disasters  # noqa: E402,F401
# directory-index resolve -> bulk gzip-CSV point decode.
from . import storm_events_db  # noqa: E402,F401
# station-siblings wave: multi-state discovery + station-observations +
# batched-snapshot + keyed missing-key parity, all via the existing phases.
from . import asos_metar  # noqa: E402,F401
from . import raws_weather  # noqa: E402,F401
from . import snotel_snow  # noqa: E402,F401
from . import airnow_air_quality  # noqa: E402,F401
from . import openaq_measurements  # noqa: E402,F401
# arcgis-odd wave: OBJECTID-cursor paging, WAF headers, prefix-strip,
# raise-on-unknown alias + fail-loud, keyed dual-endpoint, multi-layer union -- all
# via the EXISTING build_request/next_page/parse_response hooks (zero new machinery).
from . import fema_nfhl_zones  # noqa: E402,F401
from . import nwi_wetlands  # noqa: E402,F401
from . import wdpa_protected_areas  # noqa: E402,F401
from . import usace_dams  # noqa: E402,F401
from . import epa_frs_facilities  # noqa: E402,F401
# zip/multi-file wave: TIGER shapefile-ZIP URL planner (FIPS place fan-out)
# for the zip_vector executor (whole-object GET + extract + read + filter + merge).
from . import admin_boundaries  # noqa: E402,F401
# weather/GRIB wave: S3-listed key resolve (latest / targeted walkback)
# feeding the grib_object raster access mode (whole-object .grib2.gz -> COG).
from . import mrms_qpe  # noqa: E402,F401
# Overpass-family wave: OSM QL build_request + JSON->geometry
# parse_response over the 3-mirror endpoint_fallback chain (roads + pois).
from . import overpass  # noqa: E402,F401
# keyed/misc-leftovers wave: NCEI Climate Normals inventory-resolve +
# per-station access-CSV enrich; keyed http_json (ebird/iucn) via classify_status;
# USGS groundwater OGC measurements + best-effort monitoring-location enrich.
from . import climate_normals  # noqa: E402,F401
from . import ebird_observations  # noqa: E402,F401
from . import iucn_red_list_range  # noqa: E402,F401
from . import usgs_groundwater_levels  # noqa: E402,F401
# LayerURI-envelope wave: USGS STN high-water marks -- event name->id
# resolve + states-overlap build_request + bbox-clip/NO_MARKS parse + the
# post-emit envelope hook (quality/type/datum breakdown -> HighWaterMarksLayerURI).
from . import usgs_stn_hwm  # noqa: E402,F401
# Library-delegate raster hooks: pfdf USGS readers (STATSGO soils; 3DEP
# terrain) whose maintained library owns discovery + the socket -- the delegate hook
# calls the library and returns (array, transform, crs) for the shared COG writer.
from . import pfdf_raster  # noqa: E402,F401
# Record-return output shape: WFIGS named-incident lookup -- the 2-endpoint
# best-feature short-circuit + bbox-from-point + epoch->ISO discovery record (dict, not
# a LayerURI), the proof-by-migration for shape=record.
from . import wfigs_incident  # noqa: E402,F401
# movebank finish wave: keyed direct-read CSV with COMPOSITE Basic-Auth
# creds (username+password via the resolver blob path) -> per-geometry_type feature
# parse; classify_status splits 401->AUTH / 403->LICENSE / 4xx->INPUT.
from . import movebank_tracks  # noqa: E402,F401
# satellite family: the SLIDER availability index folds onto the record
# shape as a live-no-cache source (the record shape's first uncacheable fold).
from . import slider_timestamps  # noqa: E402,F401
# quick-folds wave: FIRMS active-fire keyed CSV http_json -- key in the
# URL path, 200-with-error-body auth split in parse_response + classify_status.
from . import firms_active_fire  # noqa: E402,F401
# finisher-mechanisms wave: GEM active faults -- the constant_cache
# two-tier cache (whole-world 10.6 MB GeoJSON downloaded once, AOI-filtered in the
# parse hook) + the variant_by_emptiness output switch (zero-fault AOI -> record
# dict, non-empty -> FaultSourcesResult via the envelope hook).
from . import fault_sources  # noqa: E402,F401

# landcover + flood-extent wave: fetch_flood_extent_observation (the
# LANCE dir-walk pre_resolve + the categorical-COG envelope) and fetch_landcover
# (the WCS GetCoverage build_request + the NLCD sidecar envelope).
from . import flood_extent_observation  # noqa: E402,F401
from . import landcover  # noqa: E402,F401
# endgame HRRR-Zarr wave: fetch_hrrr_forecast + fetch_hrrr_smoke -- the
# library_delegate raster fold whose delegate_resolve walks the s3fs mirror for the
# newest cycle (pre-cache-key) and whose delegate opens the Zarr slice(s), reprojects
# LCC->EPSG:4326, clips, and (forecast) synthesizes hypot(u,v) wind speed. One shared
# module; the per-source variable table + derived/fill_value live in ingest.hrrr.
from . import hrrr  # noqa: E402,F401
# endgame field-boundaries wave: fetch_field_boundaries -- the FTW/fiboa
# GeoParquet row-group bbox pushdown (owned by geopandas.read_parquet over an fsspec
# HTTPS handle) folds onto the VECTOR library_delegate mode (the new-transport
# STOP refuted; the pushdown is library-owned, not a router transport). Pure pre_resolve
# dataset-selection + a delegate read hook returning WGS84 polygon features.
from . import field_boundaries  # noqa: E402,F401
# trigger wave: fetch_lehd_jobs -- the join VALUES-hook seam (the per-state
# LODES bulk gzip-CSV values leg the census Data-API join.values leg cannot express).
# Two PURE hooks (values_plan / values_parse); the join transform owns the I/O.
from . import lehd_jobs  # noqa: E402,F401
# trigger wave: fetch_buildings -- Overpass build_request + a (features,
# tags) parse for the overpass_sidecar executor's constrained tags-sidecar side write.
from . import buildings  # noqa: E402,F401
# post-merge wave: fetch_era5_reanalysis + fetch_gtsm_tide_surge -- the CDS
# library_delegate pair (cdsapi owns the request-poll-download socket). One shared
# module: era5.read (raster) / gtsm.read (vector features) + per-source *.validate
# pre-cache gates; the missing-key/auth classifier maps the cdsapi failure to the
# source's typed *_MISSING_KEY / *_AUTH_ERROR (the credential-card surface).
from . import cds  # noqa: E402,F401
# post-merge wave: fetch_usgs_nwis_gauges -- the last flood-seam twin. The
# parse_fallback http_source mode (IV WaterML-JSON primary -> Site-RDB fallback, honest
# NO_STATIONS on all-empty) + a window-mode pre_resolve that switches the output schema
# (instantaneous 5-col vs hydrograph 12-col) + style/units by the derived _mode.
from . import usgs_nwis_gauges  # noqa: E402,F401
# raster-modes wave: fetch_jrc_global_surface_water -- the pure per-band
# colormap hook (occurrence/recurrence/seasonality/change ramp, a function of the
# band param alone) the stac_continuous_mosaic serializer bakes into the band-1
# palette. The fetch side is the declarative stac_continuous_mosaic access mode.
from . import jrc_global_surface_water  # noqa: E402,F401
# animation wave 1: the frames-list output shape + the SLIDER-stitch
# per-frame mode. fetch_goes_animation / fetch_goes_blend_animation (goes_animation
# hooks, single + blend) + fetch_viirs_day_fire (viirs_day_fire hooks, polar day
# passes) fold onto shape: animation_frames -- the router owns the per-frame
# read_through loop + honesty floor + LayerURI emission; frames_plan resolves the
# windowed frame set, frame_bytes builds one frame's COG.
from . import goes_animation  # noqa: E402,F401
from . import viirs_day_fire  # noqa: E402,F401
# animation wave 2: the netcdf_cf_object per-frame mode. fetch_goes_archive_animation
# (band-selectable Fire-Temp/true-color/hotspot/baked) + fetch_goes_active_fire (split-window
# hotspots) fold onto shape: animation_frames -- ONE frames_plan/frame_bytes pair over the shared
# _goes_archive_core substrate (S3 window list + CF-scaled MCMIPC netCDF band read + composite).
from . import goes_archive  # noqa: E402,F401
# approved-folds wave: fetch_glm_lightning folds onto shape: animation_frames
# (default output becomes a frames list; single accumulation = a one-frame list). glm.frames_plan
# splits the window into buckets, glm.frame_bytes bins GLM-L2-LCFA GROUP energy (numpy.add.at) and
# bakes the purple-log-ramp RGBA COG over the shared imagery._goes_archive_core grid + writer.
from . import glm  # noqa: E402,F401
# approved-folds wave: fetch_population's WorldPop raster leg folds onto the
# library_delegate raster mode (whole-object-download-then-window; WorldPop serves HTTP 200 to
# range requests so /vsicurl cannot window it). worldpop.validate is the pre-cache vintage gate,
# worldpop.read owns the download+window socket. The half-built ACS leg is DROPPED (fetch_census_acs).
from . import worldpop  # noqa: E402,F401

# fetch_dem fold: the 3DEP DEM library-delegate hooks -- validate
# (continent ceiling + auto-path out-of-coverage), coarsen (pixel-budget
# pre_resolve), read (py3dep + bounded watchdog + source-conditional gating),
# and envelope (the dem-{lon}-{lat}-{Nm} naming override). The Dem*Error twins'
# stable home. The source="copernicus" leg is the spec's cross-sibling dispatch.
from . import dem_3dep  # noqa: E402,F401
# fetch_topobathy fold: the coastal topo-bathymetry 4-leg UTM-composite
# library-delegate hooks -- validate (US-coastal + finiteness), read (the CUDEM ->
# regional -> ETOPO -> 3DEP-land warp merge returning (array, transform, crs) +
# the FETCH-TIME provenance RECORD), envelope (twin layer_id/name + the four
# provenance fields replayed from the channel). The TopobathyError twins' stable
# home; consumer #1 of the fetch-time provenance channel.
from . import topobathy  # noqa: E402,F401
# fetch_storm_tracks fold: the hurricane/TC-track library-delegate hooks --
# validate (historical bbox-required + shape), resolve (storm_name canon + season
# window pre-cache-key), read (IBTrACS historical OR NHC active + the binary
# forecast-zip secondary-enrichment round, returning GeoJSON features + a mode
# provenance RECORD), envelope (twin storm-tracks-{seed} id/name + the mode
# provenance replayed from the channel). The StormTracks*Error twins' stable home.
from . import storm_tracks  # noqa: E402,F401
# fetch_goes_satellite fold: the single-band float32 GOES ABI imagery
# library-delegate hooks -- validate (bbox-required + band/satellite + CONUS pre-gate),
# resolve (15-min valid_time cache rounding pre-key), read (list most-recent MCMIPC key
# -> netCDF -> CF-scale physical-units reproject returning (array, transform, crs) + a
# scan-time provenance RECORD), envelope (twin em-dash name + scan provenance replay).
from . import goes_satellite  # noqa: E402,F401
# fetch_noaa_nwm_streamflow fold (, THE FETCHER-FINALE ENDGAME -- the LAST coded
# data-fetcher): the NOAA National Water Model multi-source composite library-delegate
# hooks -- validate (CONUS-intersect + short_range fhour rule + valid_time parse), read
# (own the S3 channel_rt netCDF read -> {feature_id: streamflow} lookup + the NLDI 5x5
# spatial sample -> COMIDs + per-reach geometry + JOIN -> point features, and a
# reference-time/reach-count/NLDI-sample provenance RECORD), envelope (twin
# nwm-streamflow-{product}-{seed} layer_id + name + provenance replay).
from . import nwm_streamflow  # noqa: E402,F401
# (RoG replication unblock): fetch_aorc_precip -- the NOAA AORC v1.1 hourly
# precipitation record. Record shape / pure-record path; the build_record hook owns
# the public-bucket Zarr socket (anonymous s3fs) and returns the AOI-mean hyetograph
# forcing series. The pre-2020 / any-historical-year precip MRMS cannot reach.
from . import aorc_precip  # noqa: E402,F401
# fetch_lter_records -- a generic US-LTER / EDI package+entity reader via the
# public DataONE mirror (PASTA is 403 anonymously). resolve_build/parse resolve the
# entity data URL pre-cache-key; build_request/record parse the delimited time series
# (the Coweeta Ball Creek hourly discharge is the proven case).
from . import lter_records  # noqa: E402,F401
