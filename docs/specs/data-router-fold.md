# Generic data router - the fetcher fold (FOR NATE REVIEW)

NATE's vision (2026-07-28, greenlit as the next campaign): "a generic router
for data similar to the catalog structure where the endpoint is defined and
the piping adapts to the data being ingested... reusable since most of the
data is similar." Target: the 71,836-line fetcher family (38% of the agent
surface, 1/3 of the package). "Redundancy that doesn't pay off" goes; the
routing worry becomes a measured gate, not a fear.

## The architecture

1. SOURCE SPECS (data, not code): one YAML per data source -
   endpoint(s) + auth mode, request-param schema (bbox/time/product knobs),
   response format + shape (COG / vector / station-timeseries / tiles),
   normalization directives (CRS, units, quantity stamp, datum), cache
   TTL class, payload estimate, honest caveats, fallback chain,
   supports_global_query, AND the corpus phrasings - co-located, exactly
   like tools today. Adding a source = adding a YAML. Zero registry cost,
   zero routing cost (phrasings carry the routing, as they always did).
2. THE ROUTER (one engine, reusable piping): resolve spec -> build request
   (shared param validation, granularity gate integration) -> fetch w/
   retry/fallback per the data-source norm -> ADAPTIVE INGESTION keyed on
   declared shape (raster->COG pipeline, vector->FGB pipeline,
   timeseries->station-FGB w/ time_series_csv, tiles->assembly) -> stamp
   (CRS/units/quantity/datum) -> cache -> publish/emit envelope. All the
   seams every fetcher duplicates today (_fetch_common, cache read-through,
   payload estimates, typed upstream errors) exist ONCE.
3. RETRIEVAL: source specs index into the SAME corpus machinery (tree-walk
   loader). Either surfaced per-source as virtual tools (registration
   decoupled from code - the tier mechanism exists) or via
   search_data_catalog + fetch_from_catalog as the consumption pair -
   decided by the bench (whichever routes better).

## The derisking gates (why this cannot silently degrade the product)

- PHASE 1 (build + pilot): router + spec schema + 5 representative sources
  spanning the shapes: one COG raster (fetch_gridmet), one vector API
  (fetch_usgs_nwis_gauges or wdpa), one station-timeseries
  (fetch_noaa_coops_tides), one tiled/imagery, one awkward case picked by
  the audit. Hand-written twins STAY during the pilot.
- BENCH GATE (the routing worry, measured): retrieval_probe + canonical
  routing checks - catalog-routed phrasings must rank >= the hand-written
  baseline for the pilot sources. Deterministic, NATE-methodology rules
  (sign-off on inputs, no LLM judging). Fail -> we learned cheaply,
  architecture adjusts before any cut.
- REPLICATION GATE per source (cull doctrine): same request -> envelope
  compared vs the hand-written fetcher (values, layer output, caveats,
  error behavior incl. upstream-failure paths). A fetcher dies ONLY when
  its spec passes both gates.
- PHASE 2: family-by-family migration (USGS family, NOAA family, satellite
  family...) with per-source proofs; each family landing removes its
  hand-written twins same-commit (clean-as-you-go).
- PHASE 3: the residual set - fetchers with genuinely bespoke logic
  (Overpass query construction, multi-endpoint stitching, animation
  assembly) STAY AS CODE, honestly classified by the phase-1 audit.
  Expect 60-80% foldable; even 60% = ~40k lines out.

## Consumer compatibility

Workflows/templates import ~a dozen fetchers directly (nested use). The
router exposes the same callable seam per source (registry-resolved), so
nested consumers migrate mechanically; envelope shapes unchanged.

## Retention principle (NATE 2026-07-28)

EVERY working endpoint is RETAINED - "they drive functionality." Usage/
telemetry is NEVER a cut criterion (it informs migration ORDER and pilot
picks only). A fetcher changes form solely by replication-proven
substitution: spec-driven, hybrid (spec + named transform), or retained
code. INDISTINGUISHABILITY: hand-rolled spec-driven sources flow through
the IDENTICAL pipeline and surface as catalog-native ones - a consumer
cannot tell the origin, because nothing differs.

## Sequencing

After the in-flight SFINCS remediation lands: phase-1 audit (classify all
~100 fetchers by shape + bespoke-ness; telemetry = ordering info only) ->
NATE reviews the classification -> build pilot -> routing-parity EXPERIMENT
(experiments/fetcher_fold_routing, NATE-signed inputs) + replication gates
-> family fan-out. THEN the tool/workflow extensions (template growth),
per NATE's ordering.

## The endgame: one engine, three tiers (NATE 2026-07-31)

NATE's endgame directive, verbatim: **"adding a data_fetch method is just adding
a YAML entry."** Every data fetcher folds into ONE spec-driven engine; the coded
fetcher tools go to ZERO. A source reaches the engine at one of three tiers:

- **TIER 1 -- pure shape.** The source is a plain raster-COG / vector-FGB /
  station-timeseries and needs NOTHING but ``shape`` + ``endpoints`` + ``params``
  + ``normalize`` + ``output`` (the pilot: gridmet, coops_tides, ...).
- **TIER 2 -- shape + a declarative mode.** A named declarative directive on the
  ``ingest`` / ``params`` / ``normalize`` / ``output`` block carries the last
  15-25%: ArcGIS paging + ``where_clauses`` + ``column_map``, the ``dataretrieval``
  delegate, ``stac_float``, ``multi_url`` VRT fan-out, ``gzip_object``, the JOIN
  transform, the declarative fan-out (waves 2-9). Still zero source code.
- **TIER 3 -- shape + a hook (ADR 0056).** The source has ONE irreducible step no
  declarative directive can carry -- bespoke request construction and/or a bespoke
  payload decode. It references a REGISTERED PURE function by name
  (``hooks.build_request`` / ``hooks.parse_response``); the router owns everything
  else. This wave landed the contract + proved it on earthquakes (single GET),
  tsunami (paged), volcano (multi-GET join).

HOOK DOCTRINE: hooks are **PURE** (no I/O -- transport, caching, gates, stamps, and
the typed-error factory machinery stay router-owned; a hook only computes and MAY
call a shared ``router_*_error`` factory), **MINIMAL** (a hook point exists only
because a real source needs it -- ``post_process`` was evaluated and rejected in
favor of declarative ``output.bbox_from_features``), **REGISTERED** (a name string a
spec load validates against ``HOOK_REGISTRY``), and **TESTED** (each hook module
carries its own unit tests). END STATE: the fetcher package is ``_router/`` (the
engine) + ``_router/hooks/`` (the pure per-source steps) + ``**/source.yaml`` (the
data) + ``**/corpus.yaml`` (the phrasings). Coded fetcher tools -> 0.

### Remaining coded-fetcher worklist (rough, by target tier)

Post-wave-10: 27 sources spec-served, 72 coded fetchers remain. A ROUGH target-tier
classification for future waves (pattern-inferred from the fold history + the
wave-10 reads; NOT a per-tool audit -- each still gets its own read + two-gate
replication before folding):

- **TIER-2-able (~15) -- an existing declarative mode already covers them.**
  Single-endpoint ArcGIS vector (fetch_fema_nfhl_zones, fetch_nwi_wetlands,
  fetch_epa_frs_facilities), ``dataretrieval``/station (fetch_usgs_nwis_gauges,
  fetch_usgs_groundwater_levels, fetch_snotel_snow, fetch_asos_metar,
  fetch_raws_weather), single-COG raster (fetch_topobathy, fetch_3dep_extra,
  fetch_landcover, fetch_ghsl_population, fetch_noaa_slr_confidence,
  fetch_noaa_slr_marsh).
- **TIER-3 hook-able (~14) -- ONE clean irreducible step (a JSON point/obs API).**
  fetch_usace_nsi, fetch_usace_dams, fetch_firms_active_fire, fetch_nws_event,
  fetch_openaq_measurements, fetch_airnow_air_quality, fetch_gbif_occurrences,
  fetch_inaturalist_observations, fetch_ebird_observations, fetch_iucn_red_list_range,
  fetch_lehd_jobs, fetch_storm_events_db, fetch_climate_normals, fetch_nws_river_forecast.
- **SCOPED-JOB / needs a NEW mode (~43) -- genuinely multi-step.** Secondary-fetch
  join / fan-out (fetch_openfema_disasters -> TIGER counties, fetch_nws_alerts_conus
  -> zone geometries -- a "secondary-fetch" mode); dict-output not a layer
  (fetch_wfigs_incident, geocode_location, lookup_precip_return_period -- an
  ``output: scalar`` mode); Overpass QL construction (fetch_overpass_pois,
  fetch_roads_osm, fetch_buildings); GRIB/netcdf gridded binary (fetch_hrrr_forecast,
  fetch_hrrr_smoke, fetch_mrms_qpe, fetch_nexrad_reflectivity, fetch_glm_lightning,
  fetch_noaa_nwm_streamflow, fetch_gtsm_tide_surge, fetch_cama_flood_discharge);
  animation/frame assembly (fetch_goes_animation, fetch_goes_archive_animation,
  fetch_slider_timestamps, the satellite family); CDS-async (fetch_era5_reanalysis);
  projected-VRT reproject (fetch_soilgrids, ADR 0055); griddap/ERDDAP (fetch_noaa_sst,
  HELD); custom-envelope + secondary-endpoint (fetch_high_water_marks); composites
  over several sources (fetch_dem, fetch_population, fetch_administrative_boundaries);
  colormap-ramp DSL (fetch_jrc_global_surface_water, dead by stop-rule); track/line
  assembly (fetch_storm_tracks, fetch_movebank_tracks); other domain fetchers
  (fetch_statsgo_soils, fetch_field_boundaries, fetch_river_geometry,
  fetch_flood_extent_observation, fetch_mobi, fetch_fault_sources,
  fetch_wdpa_protected_areas, fetch_naip/landsat/sentinel STAC-raster subset).

The scoped-job bucket is where the next MODES come from (secondary-fetch, scalar
-output, overpass-QL, grib-window, frame-assembly) -- each a small declarative
addition that then collapses a whole family, exactly as multi_url/gzip_object did.
