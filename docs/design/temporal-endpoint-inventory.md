# Temporal endpoint inventory (TEMPORAL DOCTRINE recon)

Recon for the TEMPORAL DOCTRINE (docs/IDEAS.md, 2026-08-25 entry, generalizing
`event_time`). Every regularly-updated/timestamped source needs: (1) a time
param, default latest, ALWAYS pinned in provenance; (2) declared temporal
metadata (cadence, retention, snap granularity); (3) two-tier invalid-interval
resolution (in-window off-cycle SNAPS with a provenance note, out-of-retention
REFUSES typed). Dataset VINTAGE (NLCD year, DEM release) is the adjacent
cousin -- selectable where sources version, same pinning rule.

This inventory covers all 101 `data/fetchers/*/*/source.yaml` router specs
against that doctrine. `fetch_noaa_nwm_streamflow` is the worked example the
doctrine cites (`valid_time`/`forecast_hour` params, `output.provenance: true`,
per-feature `valid_time` column) and is the reference "done" row below.

## Methodology and confidence

Classification is spec-level: params, `cache.ttl_class`, `output.provenance`,
`ingest.properties` (the per-feature output columns), `caveats`, and
docstrings. It does **not** trace the Python hook implementations
(`hooks.envelope`, `hooks.delegate`, etc.) line by line -- that is a
follow-up, not this recon. Where a pinning verdict rests on inference rather
than a directly-declared field (`output.provenance: true` or a time-valued
column in `ingest.properties`), it is marked **LOW-CONFIDENCE** and the
verdict is `needs-pinning-only (verify)`. Cadence/retention figures come from
each spec's own caveats/docstring (the sources' own stated update rhythm);
where the spec is silent, cadence is inferred from the provider's public
documentation pattern and marked LOW-CONFIDENCE.

Only **4 of 101** specs declare `output.provenance: true`:
`fetch_noaa_nwm_streamflow`, `fetch_goes_satellite`, `fetch_topobathy`,
`fetch_storm_tracks`. Every other fetcher's provenance behavior at the
envelope layer is unverified from the spec alone -- this is the single
biggest confidence caveat on every "done" verdict below that isn't one of
those four.

Classes: **TEMPORAL-HAS-PARAM** (already takes a time/window param) /
**TEMPORAL-LATEST-ONLY** (cadence-updated source, spec always serves latest)
/ **EVENT/RANGE** (inherently time-ranged query) / **VINTAGE** (versioned
static dataset) / **STATIC** (genuinely timeless).

Verdicts: `needs-time-param` | `needs-pinning-only` | `needs-metadata-only` |
`done` | `static`.

---

## Summary counts (101 fetchers, tallied directly off the table below)

| Class | Count |
|---|---|
| STATIC (incl. 1 STATIC/VINTAGE borderline row) | 26 |
| VINTAGE (incl. 1 reproducibility-gap row) | 20 |
| TEMPORAL-LATEST-ONLY | 15 |
| TEMPORAL-HAS-PARAM | 20 |
| EVENT/RANGE | 19 |
| META (temporal-metadata tool itself) | 1 |
| **Total** | **101** |

| Verdict | Count | Notes |
|---|---|---|
| `static` | 25 | no action |
| `done` | 35 | pin + metadata already adequate per spec (includes the 1 doctrine-reference row and 1 exemplar row) |
| `needs-time-param` | 5 | latest-only source, historical param would be the fix (1 is low-priority/deliberate) |
| `needs-pinning-only` | 21 | param exists (or source is a live snapshot); resolved value not confirmed surfaced/pinned -- 12 of these are LOW-CONFIDENCE pending hook-code verification |
| `needs-metadata-only` | 15 | cadence/retention/vintage undocumented or unsurfaced, but no param gap |

(Verified by script tally against the per-fetcher table; see each row below
for the individual call.)

---

## Notable pinning gaps (confirmed, not LOW-CONFIDENCE)

These four are read directly off the spec/docstring, not inferred:

1. **`fetch_noaa_sst`** -- the docstring/caveat states outright: *"Default
   date = the most recent likely day (today-1 UTC), which does not enter the
   cache key."* A default-latest SST fetch is not reproducible or pinned even
   though a `date` param exists. Directly violates doctrine rule (1).
2. **`fetch_hrrr_forecast`** / **`fetch_hrrr_smoke`** -- both resolve a
   `cycle` (walking back up to 6h) via `hooks.delegate_resolve:
   hrrr.resolve_cycle`, and the cache key includes the resolved cycle, but
   neither spec declares an `envelope` hook, `result_model`, or
   `ingest.properties` -- there is no declared channel that surfaces *which*
   cycle was actually used back to the caller/provenance. Pinned internally
   (cache), not pinned externally (doctrine).
3. **`fetch_mrms_qpe`** -- resolves the S3 key via a "latest file / nearest-
   earlier hour within a 24h walkback" when `valid_time` is omitted, but has
   no envelope/properties declared to surface the resolved hour actually
   served. Same shape as HRRR: internally cached, not externally pinned.
4. **`fetch_noaa_coops_currents`** -- caveat states "snapshot semantics:
   latest observed sample / prediction nearest now, not a full time series."
   No time param, `ingest.properties: None`, no provenance flag -- the
   resolved sample time is invisible end-to-end.

All four are forcing/context sources that feed hazard-model boundary
conditions or hazard narratives, which is why they lead the ranked list
below.

---

## Ranked adoption list

Ranked by consequence to sims: forcing-class sources (feed a model's boundary
conditions or a computed-vs-observed comparison) before nice-to-have display
sources.

### Top 8 (forcing-class, act on these first)

1. **`fetch_mrms_qpe`** -- radar-gauge QPE, the primary CONUS pluvial-forcing
   source (SFINCS pluvial, urban PySWMM). Confirmed pinning gap (#3 above).
2. **`fetch_hrrr_forecast`** -- wind/precip forcing for SFINCS/HEC-RAS storm
   surge and pluvial composers. Confirmed pinning gap (#2 above).
3. **`fetch_hrrr_smoke`** -- same delegate/hook shape as #2 (smoke/AOD, lower
   sim-forcing weight but identical fix, same wave).
4. **`fetch_noaa_sst`** -- ocean-temperature input to compound-flood/ocean
   composers. Confirmed pinning gap (#1 above), and the easiest of the four
   to fix (just add the resolved date to the cache key / envelope).
5. **`fetch_noaa_coops_currents`** -- tidal-current forcing at US inlets/
   harbors/channels. Confirmed pinning gap (#4 above); zero temporal
   traceability today.
6. **`fetch_noaa_coops_tides`** / **`fetch_gtsm_tide_surge`** -- the two
   tidal/surge water-level boundary sources (US gauges + global GTSM
   reanalysis) that SFINCS coastal composers interpolate onto `bnd.bzs`.
   Both already take `start_date`/`end_date`, but `ingest.properties` is
   `None` on both -- LOW-CONFIDENCE whether the per-station hourly series
   actually carries a pinned timestamp column through to the FGB. Verify
   before trusting a coastal boundary series is reproducible.
7. **`fetch_esri_landcover_10m`** -- the non-CONUS Manning's-roughness
   source (global complement to `fetch_landcover`/NLCD). Its sibling
   `fetch_landcover` is the doctrine's VINTAGE exemplar (echoes
   `nlcd_vintage_year` + `effective_resolution_m` in its return dict);
   `fetch_esri_landcover_10m` takes an equivalent `year` param (2017-2023)
   but has no declared `properties`/envelope to confirm the same pattern.
   High priority precisely because the working pattern already exists one
   file over -- this should be a copy, not a design.
8. **`fetch_landsat_imagery`** / **`fetch_sentinel1_sar`** /
   **`fetch_sentinel2_truecolor`** -- all three pick "best scene in window"
   (least-cloudy / best-covering / most-recent) from a `start_date`/
   `end_date` range, but none declares `ingest.properties` or a
   `result_model` that would surface *which* scene date was actually
   chosen. This matters most for `fetch_sentinel1_sar` (the canonical
   keyless flood-extent layer) and `fetch_flood_extent_observation`'s
   sibling validation workflows -- computed-vs-observed grading needs to
   know the exact acquisition date, not just the query window.

### Nice-to-have / lower priority

- **Latest-only sources with an existing per-feature pin but no historical
  param** (the param would be a convenience, not a correctness fix):
  `fetch_usgs_groundwater_levels`, `fetch_usgs_water_quality`,
  `fetch_snotel_snow`, `fetch_wdpa_protected_areas`, `fetch_goes_satellite`
  (deliberately -- its own docstring redirects historical replay to
  `fetch_goes_archive_animation`).
- **Metadata-only surfacing gaps** on inventories that change slowly and
  pose low physics risk if stale: `fetch_fema_nfhl_zones`,
  `fetch_nifc_fire_perimeters`, `fetch_landfire_fuels`,
  `fetch_usfs_canopy_fuels`, `fetch_nhd_waterbodies`, `fetch_nwi_wetlands`,
  `fetch_usace_nsi`, `fetch_epa_ejscreen`, `fetch_mobi`,
  `fetch_jrc_global_surface_water`, `fetch_naip` (state-cycle acquisition
  year never surfaced), `fetch_wfigs_incident`.
- **Reproducibility, not doctrine-strict**: `fetch_fault_sources` pulls the
  GEM active-faults GeoJSON off a floating GitHub `master` ref with no
  version/commit param or pin -- not a time-of-day pinning gap, but the same
  "identical query, different answer over time" failure mode the doctrine
  exists to prevent. Worth a metadata fix (pin to a tag/commit) even though
  it falls outside the strict param/cadence framing.
- **Vestigial-looking params, verify before trusting**:
  `fetch_hrsl_population`'s `year` param (default 2020) against a
  `source=meta_hrsl`-only enum that may not actually vary by year;
  `fetch_ghsl_population`'s `epoch` param is bounded `min=max=2020`, so it
  is not currently selectable at all despite looking like a VINTAGE knob.

---

## Per-fetcher classification

### biodiversity/ (7)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_ebird_observations | TEMPORAL-HAS-PARAM | sub-hourly recent-endpoint refresh, 30-day rolling window (LOW-CONF exact latency) | `days_back` (1-30) | per-feature `obsDt` | done |
| fetch_gbif_occurrences | EVENT/RANGE | continuously ingested aggregator, no retention cap | `year_range` (1500-2100) | per-feature `eventDate` | done |
| fetch_inaturalist_observations | TEMPORAL-HAS-PARAM | continuous citizen-science stream | `days_back` (optional) | per-feature `observed_on` | done |
| fetch_iucn_red_list_range | VINTAGE | per-species assessment, irregular (LOW-CONF cadence) | none (single current assessment) | per-feature `assessment_date` + `published_year` | done |
| fetch_mobi | VINTAGE | NatureServe MoBI single release, cadence undocumented (LOW-CONF) | none | raster, no date band/envelope | needs-metadata-only |
| fetch_movebank_tracks | TEMPORAL-HAS-PARAM | "near-real-time but caches batches" per docstring | `time_range` (datetime_range) | `ingest.properties: None` -- unverified (LOW-CONF) | needs-pinning-only (verify) |
| fetch_wdpa_protected_areas | TEMPORAL-LATEST-ONLY | monthly WDPA releases (spec-documented) | none | `status_yr` is designation year, not fetch vintage; no fetch-time pin | needs-time-param |

### climate/ (6)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_chirps_precipitation | TEMPORAL-HAS-PARAM | monthly/daily CHIRPS-2.0, archive back to ~1981 | `date` (required) + `period` (monthly/daily) | raster, no properties; param presence unverified as pinned into output (LOW-CONF) | needs-pinning-only (verify) |
| fetch_climate_normals | VINTAGE | 1991-2020 NOAA baseline, next revision ~2031 | none (fixed baseline, documented in caveat) | baseline period stated in caveat; single fixed field | done |
| fetch_era5_reanalysis | TEMPORAL-HAS-PARAM | ERA5T ~5-day lag / final ~3-month lag; 1940-present | `start_date` + `end_date` (required) | raster, no properties (LOW-CONF) | needs-pinning-only (verify) |
| fetch_gridmet | TEMPORAL-HAS-PARAM | daily, ~3-day lag, 1979-present, 366-day/call cap | `start_date` + `end_date` (required) | raster, no properties (LOW-CONF) | needs-pinning-only (verify) |
| fetch_modis_lst | TEMPORAL-HAS-PARAM | 8-day MODIS composite | `start_date` + `end_date` (optional) | raster, no properties (LOW-CONF) | needs-pinning-only (verify) |
| fetch_us_drought_monitor | TEMPORAL-HAS-PARAM | weekly USDM release (Thursdays) | `date` (optional, `date_compact`) | vector, no properties list declared (LOW-CONF) | needs-pinning-only (verify) |

### hazard/ (17)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_epa_frs_facilities | STATIC | facility registry, explicitly not real-time per caveat | none | n/a | static |
| fetch_fault_sources | VINTAGE (reproducibility gap) | GEM active-faults catalog, irregular releases, fetched off a floating GitHub `master` ref | none | no version/commit captured anywhere | needs-metadata-only |
| fetch_fema_nfhl_zones | TEMPORAL-LATEST-ONLY | LOMRs monthly + DFIRM panel revisions quarterly (spec-documented) | none | "month vintage" folded into the cache key per caveat, but not surfaced to the caller | needs-metadata-only |
| fetch_firms_active_fire | TEMPORAL-HAS-PARAM | near-real-time (satellite pass latency, hours) | `days_back` (1-10) + `date` (optional) | per-feature `acq_date`/`acq_time` | done |
| fetch_hifld_critical_infrastructure | STATIC | "HIFLD is static" per caveat | none | n/a | static |
| fetch_hifld_transmission_lines | STATIC | same | none | n/a | static |
| fetch_landfire_fuels | VINTAGE | LANDFIRE LF2022, ~2-yr release cycle | none | vintage named only in caveat text, not structured output | needs-metadata-only |
| fetch_mtbs_burn_severity | EVENT/RANGE | annual MTBS updates, 1984-present | `year_range` | raster, no properties list confirming per-fire-year pin (LOW-CONF) | needs-pinning-only (verify) |
| fetch_nifc_fire_perimeters | TEMPORAL-LATEST-ONLY | near-real-time (incident-driven, ~hourly) | none (`status` filter only) | no per-feature timestamp declared | needs-metadata-only |
| fetch_openfema_disasters | EVENT/RANGE | full history 1953-present, updated as declarations occur | `start_year` (filter) | per-feature `latest_declaration` | done |
| fetch_tsunami_events | EVENT/RANGE | full historical DB, updated as cataloged | `min_year`/`max_year` | per-feature `year` | done |
| fetch_usace_dams | VINTAGE | NID updated quarterly (spec-documented) | none | per-record `DATA_UPDATED`, `OPERATIONAL_STATUS_DATE` columns | done |
| fetch_usace_levees | STATIC | federally-inspected inventory, slow-changing | none | n/a | static |
| fetch_usfs_canopy_fuels | VINTAGE | LANDFIRE LF2022, same family as fuels | none | vintage named only in caveat text | needs-metadata-only |
| fetch_usgs_earthquakes | EVENT/RANGE | continuous feed, full catalog retention | `start_date`/`end_date` + `min_magnitude` | per-feature `time`/`updated` | done |
| fetch_usgs_volcano_alerts | TEMPORAL-LATEST-ONLY | irregular, USGS-issued alert changes | none | per-feature `sent_utc` | done |
| fetch_wfigs_incident | TEMPORAL-LATEST-ONLY | near-real-time (active incidents only) | none | `record` shape, `properties: None` | needs-metadata-only |

### hydrology/ (17)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_aquifer_thickness | STATIC | single modeled steady-state release | none | n/a | static |
| fetch_aquifer_transmissivity | STATIC | single PEST-calibrated release | none | n/a | static |
| fetch_flood_extent_observation | TEMPORAL-HAS-PARAM | MODIS MCDWD 3-day NRT composite | `date` (optional) | envelope explicitly returns `observation_date` per docstring | done |
| fetch_groundwater_recharge | STATIC | long-term mean-annual, single release | none | n/a | static |
| fetch_high_water_marks | EVENT/RANGE | per-event USGS HWM surveys | `event` (name filter) | per-feature `survey_date` | done |
| fetch_jrc_global_surface_water | VINTAGE | fixed 1984-2021 statistics | none | vintage range only in caveat text | needs-metadata-only |
| fetch_lter_records | TEMPORAL-HAS-PARAM | package-specific cadence (varies by EDI package) | `start_date`/`end_date` (optional) | series carries the package's own `date_col` | done |
| fetch_nhdplus_nldi_navigate | STATIC | NHDPlus v2.1 fixed network | none | n/a | static |
| fetch_nhd_waterbodies | VINTAGE | month vintage in cache key per caveat | none | not surfaced to caller | needs-metadata-only |
| fetch_noaa_nwm_streamflow | TEMPORAL-HAS-PARAM | hourly cadence, ~30-day retention | `valid_time` + `forecast_hour` | `output.provenance: true`, per-feature `valid_time` | done (doctrine reference case) |
| fetch_nwi_wetlands | VINTAGE | month vintage in cache key per caveat | none | not surfaced to caller | needs-metadata-only |
| fetch_nws_river_forecast | TEMPORAL-LATEST-ONLY | live, updates as NWS issues forecasts | none | per-feature `obs_valid_time`/`fcst_valid_time` | done |
| fetch_river_geometry | STATIC | OSM waterway geometry | none | n/a | static |
| fetch_usgs_groundwater_levels | TEMPORAL-LATEST-ONLY | varies by well (some real-time, some periodic) | none | per-feature `datetime` | needs-time-param |
| fetch_usgs_nwis_gauges | TEMPORAL-HAS-PARAM | real-time IV + historical archive | `start_date`/`end_date`/`period` | `properties: None` (LOW-CONF) | needs-pinning-only (verify) |
| fetch_usgs_water_quality | TEMPORAL-LATEST-ONLY | irregular per-site sampling cadence | none | per-feature `result_date` | needs-time-param |
| fetch_water_table_depth | STATIC | single MODFLOW-6 steady-state release | none | n/a | static |

### imagery/ (11)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_goes_active_fire | EVENT/RANGE | 5-min GOES ABI cadence, archive-scoped | `start_utc`/`end_utc` | frame-based (`animation_frames`), per-frame ISO labeled | done |
| fetch_goes_animation | EVENT/RANGE | 5-min cadence (configurable `step_minutes`), ~100-frame recent window | `start_utc`/`end_utc` + `step_minutes` | frame-based, per-frame ISO labeled | done |
| fetch_goes_archive_animation | EVENT/RANGE | 5-min cadence, full historical S3 archive (any past date) | `start_utc`/`end_utc` + `step_minutes` | frame-based, per-frame ISO labeled | done |
| fetch_goes_blend_animation | EVENT/RANGE | 5-min cadence, shares GeoColor+FireTemp georeferencing | `start_utc`/`end_utc` + `step_minutes` | frame-based | done |
| fetch_goes_satellite | TEMPORAL-LATEST-ONLY | CONUS sector refreshes every 5 min | none (by design -- historical routed to `fetch_goes_archive_animation`) | `output.provenance: true`, 15-min cache-rounded `valid_time` | needs-time-param (low priority -- deliberate sibling split, already pinned for "now") |
| fetch_landsat_imagery | EVENT/RANGE | ~16-day revisit, archive to 1980s | `start_date`/`end_date` (optional) | raster, no properties/result_model surfacing chosen scene date | needs-pinning-only |
| fetch_naip | VINTAGE | multi-year state-by-state acquisition cycle | none | acquisition year not surfaced | needs-metadata-only |
| fetch_sentinel1_sar | EVENT/RANGE | ~6-12 day repeat (constellation-dependent) | `start_date`/`end_date` (optional) | raster, no properties surfacing chosen scene date | needs-pinning-only |
| fetch_sentinel2_truecolor | EVENT/RANGE | ~5-day global revisit | `start_date`/`end_date` (optional) | raster, no properties surfacing chosen scene date | needs-pinning-only |
| fetch_slider_timestamps | META (temporal-metadata tool) | live index, turns over every few minutes | n/a -- this tool *is* the availability/cadence lookup | returns `cadence_seconds` + latest ISO time directly | done |
| fetch_viirs_day_fire | EVENT/RANGE | irregular JPSS polar-pass cadence | `start_utc`/`end_utc` | frame-based | done |

### ocean/ (8)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_gtsm_tide_surge | TEMPORAL-HAS-PARAM | GTSM v3.0 reanalysis, historical only, 1950-~2024 | `start_date`/`end_date` (required) | `properties: None` (LOW-CONF) | needs-pinning-only (verify) |
| fetch_noaa_coops_currents | TEMPORAL-LATEST-ONLY | ~88 realtime stations, ~6-min cadence | none | none -- explicit "snapshot ... nearest now" with zero pin (confirmed gap) | needs-pinning-only |
| fetch_noaa_coops_tides | TEMPORAL-HAS-PARAM | hourly, ~300 stations, full historical archive | `start_date`/`end_date` (required) | `properties: None` (LOW-CONF) | needs-pinning-only (verify) |
| fetch_noaa_slr_confidence | STATIC | scenario-parameterized (feet), not date-parameterized | none (`slr_ft` is a scenario level) | n/a | static |
| fetch_noaa_slr_marsh | STATIC | same | none | n/a | static |
| fetch_noaa_slr_scenarios | STATIC | same | none | n/a | static |
| fetch_noaa_sst | TEMPORAL-HAS-PARAM | daily, NOAA CRW, 1985-present | `date` (optional) | confirmed gap -- default date excluded from cache key | needs-pinning-only |
| fetch_topobathy | STATIC | composite build (CUDEM + 3DEP), `output.provenance: true` | none | already exemplary | static |

### socioeconomic/ (13)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_administrative_boundaries | STATIC | 2024 TIGER/Line | none | n/a | static |
| fetch_buildings | STATIC | OSM footprints, continuously edited but treated as context | none | n/a | static |
| fetch_cdc_svi | VINTAGE | SVI 2022 pinned, caveat states "latest published" | none | pinned single vintage, documented | done |
| fetch_census_acs | VINTAGE | ACS 5-yr vintage, annual release | `year` (default 2022) | `properties: None` -- year not confirmed echoed (LOW-CONF) | needs-pinning-only (verify) |
| fetch_epa_ejscreen | VINTAGE | EJScreen 2.x, irregular (~annual) updates | none | no version captured precisely | needs-metadata-only |
| fetch_field_boundaries | STATIC | fixed FTW/fiboa benchmark snapshot per region | none (`dataset` selects region, not time) | n/a | static |
| fetch_ghsl_population | VINTAGE | GHSL R2023A, ~5-yr epoch releases | `epoch` (bounded min=max=2020, not currently selectable) | epoch stated in caveat, but param is a no-op today | needs-metadata-only |
| fetch_hrsl_population | VINTAGE | Meta HRSL, effectively single-vintage | `year` (default 2020) against `source=meta_hrsl`-only enum | unclear whether `year` does anything (LOW-CONF) | needs-metadata-only (verify param validity) |
| fetch_lehd_jobs | VINTAGE | LODES8 annual releases, 2002-2030 | `year` | per-feature `year` | done |
| fetch_overpass_pois | STATIC | OSM POIs, continuously edited context layer | none | n/a | static |
| fetch_population | VINTAGE | WorldPop annual releases, 2000-2020 | `dataset` (`worldpop_YYYY` token) | not confirmed echoed in output (LOW-CONF) | needs-pinning-only (verify) |
| fetch_roads_osm | STATIC | OSM roads, "refreshed at most once per month" per caveat, context layer | none | n/a | static |
| fetch_usace_nsi | STATIC/VINTAGE | single NSI release, "30-day TTL matches update rhythm" per caveat | none | no version captured | needs-metadata-only |

### soil/ (4)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_gcn250_curve_numbers | STATIC | Jaafar 2019 fixed release | none | n/a | static |
| fetch_snotel_snow | TEMPORAL-LATEST-ONLY | daily/sub-daily station updates | none | per-feature `date` | needs-time-param |
| fetch_soilgrids | STATIC | SoilGrids 2.0 fixed ML product | none | n/a | static |
| fetch_statsgo_soils | STATIC | fixed NRCS survey | none | n/a | static |

### terrain/ (5)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_3dep_extra | STATIC | topography within a vintage | none | n/a | static |
| fetch_copernicus_dem | STATIC | topography within a vintage | none | n/a | static |
| fetch_dem | STATIC | topography within a vintage | none | n/a | static |
| fetch_esri_landcover_10m | VINTAGE | annual Esri/Impact Observatory LULC, 2017-2023 | `year` | not confirmed echoed in output, unlike sibling `fetch_landcover` (LOW-CONF) | needs-pinning-only (verify) -- high priority, working pattern exists next door |
| fetch_landcover | VINTAGE | NLCD ~2-3 yr release cadence | `dataset` (`nlcd_YYYY`) | echoes `nlcd_vintage_year` + `dataset` + `effective_resolution_m` in return dict | done (exemplar) |

### weather/ (13)

| Fetcher | Class | Cadence / retention | Temporal params | Pinning | Verdict |
|---|---|---|---|---|---|
| fetch_airnow_air_quality | TEMPORAL-LATEST-ONLY | current-hour only, hourly updates | none | per-feature `UTC` column | done |
| fetch_aorc_precip | TEMPORAL-HAS-PARAM | hourly, 1979-02 to ~10 days ago | `start_date`/`end_date` (required) | returns the hyetograph series itself (the series is the pin) | done |
| fetch_asos_metar | EVENT/RANGE | hourly | `start_time`/`end_time` (optional) | per-feature `valid` column | done |
| fetch_glm_lightning | EVENT/RANGE | continuous satellite lightning groups | `start_utc`/`end_utc` + `accumulation_window_s` | frame-based | done |
| fetch_hrrr_forecast | TEMPORAL-HAS-PARAM | hourly cycles, 18h standard / 48h extended | `cycle` + `forecast_hour` | confirmed gap -- resolved cycle not surfaced (no envelope/properties) | needs-pinning-only |
| fetch_hrrr_smoke | TEMPORAL-HAS-PARAM | same as fetch_hrrr_forecast | `cycle` + `forecast_hour` | confirmed gap, same shape | needs-pinning-only |
| fetch_mrms_qpe | TEMPORAL-HAS-PARAM | ~2h-delayed, 24h walkback if `valid_time` omitted | `valid_time` (optional) + `accumulation` | confirmed gap -- resolved hour not surfaced | needs-pinning-only |
| fetch_nws_alerts_conus | TEMPORAL-LATEST-ONLY | live, current-only 0-7 days (no historical archive via this API) | none | per-feature `effective`/`onset`/`expires` | done |
| fetch_nws_event | TEMPORAL-LATEST-ONLY | live, current-only | none | per-feature `effective`/`onset`/`expires` | done |
| fetch_openaq_measurements | TEMPORAL-LATEST-ONLY | continuous, top-of-hour cache vintage | none | per-feature `datetime_utc`; cache key includes top-of-hour vintage | done |
| fetch_raws_weather | EVENT/RANGE | sub-hourly | `start_time`/`end_time` (optional) | per-feature `utc_valid` | done |
| fetch_storm_events_db | EVENT/RANGE | annual archive updates, 1950-present | `year` (required) + `begin_date`/`end_date` | per-feature `BEGIN_DATE_TIME` | done |
| fetch_storm_tracks | EVENT/RANGE | 3/6-hourly fixes; hourly bucket for active storms | `start_year`/`end_year` + `active_only` | `output.provenance: true` | done |

---

## Commit

`docs/design/temporal-endpoint-inventory.md` (this file), committed
path-scoped as `Nate Almanza <natealmanza3@gmail.com>`, not pushed.
