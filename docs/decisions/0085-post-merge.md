# ADR 0085 -- Post-merge wave: CDS library_delegate pair (ERA5 + GTSM) + the last flood-seam twin (NWIS) folded; JRC re-attempt STOP

Status: accepted (2026-08-02)
Supersedes: the ADR 0084 fetch_usgs_nwis_gauges "QUEUED / flood-canary-gated" row
(now FOLDED); the ADR 0084/0083 fetch_usgs_nwis_gauges STOP. Re-attempts (and
sharpens) the long-standing fetch_jrc_global_surface_water colormap-DSL rejection.

## Context

The post-merge wave on `refactor/engine-doors`: three folds landed and one re-attempt
STOP-ruled. The CDS pair (ERA5 + GTSM) had never been re-attempted since the
`library_delegate` + `delegate_resolve` machinery landed; the NWIS twin was the LAST
flood-seam twin (canary-gated); JRC's fold was re-opened because the palette machinery
that its computed-colormap rejection predated now exists.

No CDS key is present in the environment, so the CDS pair's proof surface is
input-validation + missing-key parity (the offline surface) plus a mocked-NetCDF
happy-path decode parity -- live-positive was never exercised (no key registered).

## Decisions

### 1. fetch_era5_reanalysis + fetch_gtsm_tide_surge -- FOLDED (shared CDS delegate hook)

The CDS/`cdsapi` client owns the async request-poll-download socket, so both fold onto
the `library_delegate` executor exactly like the HRRR-Zarr precedent: the router keeps
params / gates / stamps / cache / typed-errors, and ONE shared hook module
(`hooks/cds.py`) owns the impurity -- `cdsapi.Client.retrieve` under the declared
`ingest.delegate.timeout_s` watchdog. ERA5 routes through `raster_cog` (`era5.read`
returns `(array, transform, crs)`; the derived `10m_wind_speed` = two component
retrieves combined by `hypot`); GTSM routes through the vector `library_delegate.execute`
(`gtsm.read` returns GeoJSON gauge features for the shared FGB writer).

ZERO router changes were needed: `auth.mode='cds'` is declarative (nothing reads it),
and the key check lives inside the hook (4-path: api_key kwarg -> str secret_ref ->
`TRID3NT_COPERNICUS_CDS_API_KEY` env -> None), raising the source's typed
`*_MISSING_KEY` / `*_AUTH_ERROR` / `*_UPSTREAM_ERROR` from the cdsapi-failure classifier
-- the ebird keyed precedent. Each source declares a pure `*.validate`
(`delegate_validate`) gate reproducing the twin's `_validate_bbox` / `_validate_variable`
(or `_validate_output`) / `_validate_date_range` pre-cache.

The GTSM classifier is a byte-for-byte reproduction of the twin's NARROWER one (no
`.cdsapirc` phrase list): a genuine missing-`~/.cdsapirc` message lacks the substring
"key", so it classifies as `GTSM_UPSTREAM_ERROR`, NOT `GTSM_MISSING_KEY` -- the
pre-existing asymmetry the fold reproduces rather than silently "fixes" (flagged for
NATE; a fix would flip a live behavior + its test).

PROOF: `test_router_cds.py` -- 15/15 offline parity (input-validation + missing-key/auth/
upstream classification, router hook vs twin, fake-cdsapi) all identical error_code +
retryable; ERA5 happy-path array VALUE-identical to the twin (mocked NetCDF, mean
0.077940 == twin, derived wind-speed non-negative); GTSM happy-path FGB column+row parity
(2 in-bbox gauges, identical 11-col schema incl `time_series_csv`). Retrieval unshifted
(ERA5 8/8, GTSM 7/7 top-8). Consumers re-pointed: main.py + tools/__init__ eager imports
removed (auto-registered); sfincs_forcing_autowire GTSM fallback + the coastal test's
GTSM patch -> `TOOL_REGISTRY[...].fn` / a registry stub. Divergences (non-gating):
the payload `per_station` model approximates the twin's `0.5*days*area`; an explicit
api_key/secret_ref kwarg enters the cache key (env/rc path keeps it out); a str
secret_ref only (no SecretRecord-object Persistence path).

### 2. fetch_usgs_nwis_gauges -- FOLDED (parse_fallback + window-mode output switch)

The last flood-seam twin. Its two blockers, both resolved with pure hooks + ONE new
strictly-additive executor mode:

- OUTPUT-SCHEMA switch by window presence. `usgs_nwis.resolve` (`pre_resolve`) derives a
  `_mode` param (instantaneous / hydrograph) pre-cache-key; `ingest.properties_by_param`
  keyed on `_mode` pins the 5-field latest-instantaneous vs 12-field discharge-hydrograph
  FGB column schema (the twin's `_build_flatgeobuf` vs `_build_window_flatgeobuf`), and
  `style_preset_by_param` / `units_by_param` the per-mode stamps -- one declarative switch
  keyed on the derived mode, no new output knob.
- The IV WaterML-JSON -> Site-service RDB cross-parser FALLBACK. `http_json`'s new
  `parse_fallback` mode walks `build_request`'s ORDERED plans, parses EACH body on its own,
  and stops at the first non-empty parse (distinct from `endpoint_fallback`, a first-HTTP-
  success mirror chain). `usgs_nwis.build_request` emits `[IV, Site]` in instantaneous mode
  (a 404/empty IV degrades to Site LOCATIONS) and `[IV-window]` only in hydrograph mode;
  `usgs_nwis.parse` self-detects the payload (JSON -> IV, else -> Site RDB). All-empty ->
  the twin's honest `NWIS_GAUGES_NO_STATIONS` (retryable=false) INSTEAD of a header-only FGB.

`usgs_nwis.resolve` also owns the spatial-selector cross-param gate (state_code XOR bbox,
state wins, the ~24.5 deg^2 bbox area cap -> `NWIS_GAUGES_BBOX_TOO_LARGE`) and the
temporal-window resolver (period-wins, both-or-neither dates, 120-day cap), raised
pre-cache / pre-network.

LIVE PROOF (twin vs router, real USGS, Fort Myers / Lee County FL): 13 sites in BOTH modes,
site-number SETs + FGB column schemas identical, style/units identical
(`usgs_gauges` / `mixed (cfs / ft)` instantaneous; `usgs_gauges_hydrograph` /
`ft^3/s (discharge hydrograph)` hydrograph). Offline `test_router_nwis.py` (21) covers the
parsers, the resolve/build edge matrix, and the parse_fallback IV->Site + all-empty->
NO_STATIONS executor path. Consumer re-point: sfincs_forcing_autowire fluvial NWIS leg ->
`TOOL_REGISTRY[...].fn`. FLOOD CANARY green (`run_sfincs_direct` status=ok + depth COG
published to MinIO). Retrieval unshifted (8/8 top-8). A shared-router hardening rode along:
`build_layer_uri` now skips a bbox param present-but-None (nwis nulls bbox for cache
parity when state_code wins) -- strict no-op for priors (which never carry a None bbox).

### 3. fetch_jrc_global_surface_water -- STOP (colormap resolved; fetch side needs more)

The COLORMAP blocker is RESOLVED: the twin's computed per-band ramp (`_band_colormap` --
a white->deep-blue occurrence/recurrence ramp, a 12-step seasonality ramp, a red->white->
blue diverging change ramp) is a PURE function of the `band` param ALONE (it never reads
the fetched array, does no I/O), so it folds as a post-array `hooks.colormap` the existing
`array_to_cog_bytes(colormap=...)` serializer bakes into an embedded band-1 palette -- NOT
a declarative colormap DSL, so the one-consumer-DSL bar does not apply.

But the FETCH side genuinely needs MORE than the pure colormap hook: JRC is a
CONTINUOUS-value STAC mosaic (BILINEAR resample + a per-band nodata sentinel 0/253)
served through the Planetary Computer REST `/sign` endpoint (`_pc_sign_two_tier`), whereas
the existing `stac_search` / `stac_to_mosaic` mode is categorical (`Resampling.nearest`, a
single static `mosaic.nodata`, the token-based `sas_sign_href`). LIVE-VERIFIED: a
`sas_sign_href` signed URL for a jrc-gsw asset returns HTTP 403 `AuthenticationFailed`
(the token path the twin explicitly rejected for that blob container). Folding JRC would
require a new `stac_continuous_mosaic` access mode (`mosaic.resampling: bilinear` +
`nodata_by_param` + two-tier REST signing) OR regressing the esri_landcover `stac_search`
prior -- "genuinely needs more." QUEUED as a bounded fetch-side follow-up (no longer a
colormap problem).

## Consequences

- Coded fetchers: mechanical `@register_tool fetch_*.py` count 20 -> 17
  (fetch_era5_reanalysis, fetch_gtsm_tide_surge, fetch_usgs_nwis_gauges deleted; JRC
  untouched). Excluding the two utilities (geocode_location + lookup_precip_return_period),
  the true coded-fetcher count is 18 -> 15. (The mission's "19 true fetchers" basis is
  off-by-one against the measured 18-before -> flagged for the orchestrator to reconcile
  against NATE's tally.) n_specs 78 -> 81; registry 190 unchanged (all folds
  name-preserving).
- Two new router branches, both strict no-op for every prior spec: `http_json`'s
  `parse_fallback` mode and the `build_layer_uri` None-bbox guard. One new shared hook
  module (`cds`) + one new source hook module (`usgs_nwis_gauges`); NO new SourceSpec
  contract fields (JRC's `hooks.colormap` was NOT added -- JRC stopped).
- The CDS delegate is the THIRD sanctioned socket family after HRRR-Zarr + dataretrieval:
  a declared timeout, telemetry-marked, the hook owns the taxonomy.
- Offline baseline UNCHANGED: exactly 9 failures (test_fetch_resolution_gate x4 +
  test_run_river_dye_scenario x5) from the repo root; the keyed/stations cluster remains a
  documented order-flake (passes in isolation). test_catalog_surfacing counts updated
  (n_specs 81; declarable-pool delta 80; index tool_names 80).
