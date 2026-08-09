# ADR 0203 - AORC precipitation + LTER/EDI record fetchers (the RoG replication unblock)

Date: 2026-08-08
Status: LANDED (offline-green + live-proven; showcase seeding DEFERRED to the
ADR 0200 wave, which holds `scripts/seed_showcase_cases.py`).
Builds on: ADR 0202 (RoG replication STOPPED on a precip/gauge coverage gap), ADR
0112 (universal-fetcher endgame: 0 coded data-fetchers, spec-driven router), ADR
0076 (the `record` output shape), ADR 0074 (library-delegate socket doctrine).

## Context

ADR 0202 stopped the Coweeta rain-on-grid replication on a data-coverage gap: the
highest-resolution observed discharge (Coweeta Ball Creek weir, hourly, m3/s) ends
2019-01-14, while our only historical precip feedstock (`fetch_mrms_qpe`, MRMS QPE
Pass2 S3) begins ~2020-10 -- no event has BOTH observed discharge and a forcing
product. The recommended unblock (path 1) was: build an **AORC** fetcher (NOAA
Analysis of Record for Calibration, hourly ~800 m CONUS, 1979-present) so a storm
WITHIN the 2014-2019 gauge era can be forced and graded. NATE also asked for the
USFS/LTER record itself to become a first-class data endpoint. This ADR lands both,
spec-driven, on the existing router (ADR 0112) -- no coded data-fetcher added.

## Decision

Two new **spec-driven** fetchers, both the `record` output shape (a bare structured
JSON dict, not a renderable layer -- the natural shape for a forcing/observation
time series).

### 1. `fetch_aorc_precip` (weather_atmosphere) -- the replication unblock

- Source: NOAA AORC v1.1, public AWS bucket `noaa-nws-aorc-v1-1-1km` (us-east-1,
  ANONYMOUS), per-year Zarr stores `<year>.zarr`. Total precip = `APCP_surface`
  (kg m-2 == mm, 1-hour accumulation, ending at the top of each hour). Grid: lat
  20..55, lon -130..-60, 0.008333 deg (~800 m / 30 arc-second), hourly; the bucket
  registry advertises "1 km" but the native grid is 30 arc-second.
- Deliverable: an **AOI-mean HYETOGRAPH** -- the hourly precip series averaged over
  the request bbox (the rain-on-grid forcing series for a small basin) + the window
  accumulation (`total_mm`, per-cell min/max/mean). This is what forces the model;
  the "series length" the proof reports is the hyetograph.
- Shape mechanics: `shape: record` on the PURE-RECORD path (no `build_request`); the
  `aorc_precip.build_record` hook OWNS the Zarr socket (anonymous s3fs), the
  sanctioned library impurity (mirroring the library_delegate rasters). The
  `record` executor is HTTP-only, so a Zarr-reading fetcher CANNOT use a
  library_delegate under a `record` shape -- the pure-record path is the in-grammar
  way to let the record hook own the socket. `_open_year` is the single injectable
  I/O seam so the windowing / mean / dict logic is unit-tested offline.
- Endpoint pin: `_open_year` builds `s3fs.S3FileSystem(anon=True,
  client_kwargs={endpoint_url: s3.us-east-1.amazonaws.com}, skip_instance_cache=True)`.
  The local build sets `AWS_ENDPOINT_URL` at MinIO; without BOTH the explicit
  endpoint AND `skip_instance_cache` a cached MinIO-pointed s3fs instance hijacks
  the public read (empty store -> zarr `GroupNotFound`). `_public_s3.public_endpoint`
  supplies the pin; `fsspec.get_mapper` did NOT honor the pin here (only the
  `S3FileSystem`+`S3Map` form did), a live-found gotcha.
- Complements MRMS: AORC is the pre-2020 / any-historical-year forcing MRMS cannot
  reach; the docstring + caveats say so. Gates: `conus_only`, `max_bbox_deg2: 2.0`
  (a forcing series is a small-basin query), `max_range_days: 92`. Not real-time
  (~10-day publication lag) -> a within-lag / pre-1979 window is a typed
  `AORC_PRECIP_NOT_AVAILABLE`.

### 2. `fetch_lter_records` (hydrology) -- the LTER/EDI endpoint

- Source: US-LTER long-term ecological/hydrologic records on the Environmental Data
  Initiative (EDI) repository, read through the PUBLIC DataONE mirror
  (`cn.dataone.org/cn/v2/resolve/<encoded-PASTA-PID>`). ADR 0202's CRITICAL ACCESS
  FACT: EDI's native PASTA API is HTTP 403 anonymously from this environment; the
  DataONE resolve endpoint redirects to the member-node copy (identical bytes). The
  router transport follows the redirect; `resolve/` serves BOTH the EML metadata and
  the data entities (the `object/` endpoint 404s for data -- a live-found detail).
- Scope: a GENERIC package + entity reader (naming = question class, not place). Given
  an EDI `package_id` (`scope.identifier.revision`, e.g. `knb-lter-cwt.3037/19` or
  `.19`) + an optional entity selector, it returns ONE data entity's parsed time
  series with per-column units. The Coweeta Ball Creek weir hourly discharge is the
  proven case, not a hardcoded one.
- Shape mechanics: `shape: record` with the resolve-then-fetch phases. `resolve_build`
  -> the EML metadata request (pre-cache-key); `resolve_parse` -> parse EML, pick the
  entity, extract its data-object URL + delimiter + header rows + column units, merged
  into params so the resolved entity is part of the cache key. `build_request` -> the
  entity's data request; `build_record` -> the delimited parse + window filter +
  per-column peak/min/mean. All hooks PURE over already-fetched bodies (the router owns
  the sockets).

Both are added to `_ALWAYS_OFFLOAD_SYNC_TOOLS` (heavy sync I/O in the record hook:
AORC streams a windowed Zarr read; LTER downloads + parses a multi-MB entity), so
they never stall the WS heartbeat.

## Live proof (Coweeta fork bbox -83.48,35.02,-83.42,35.08; Dec 2015 SE flood)

- `fetch_aorc_precip(start=2015-12-22, end=2015-12-30)`: 216-hour hyetograph, AOI-mean
  accumulation **460.9 mm** (per-cell 359.7-554.8 mm), peak **43.4 mm/hr** at
  2015-12-24T11:00; ~9 s through the registered tool, cache written to MinIO.
- `fetch_lter_records(package_id="knb-lter-cwt.3037/19", start=2015-12-22, end=2015-12-30)`:
  entity `3037_BC9`, **216 rows**, Discharge peak **8.60 m3/s** (min 0.309, mean 2.03,
  units cubicMetersPerSecond); ~7 s, cache written.

These two now OVERLAP (a 2015-2018 storm has AORC forcing AND Ball Creek observed
discharge), so the ADR 0202 gap is unblocked -- the RoG replication grading (NSE/R2
over computed-vs-observed outlet discharge) is data-feasible pending the Ball-Creek-
fork mesh re-cut (ADR 0202 path 1).

## Verification

- Retrieval (model-free `retrieve_ranked_tools`, top-8): both tools rank **#1** for
  all four of their target prompts (co-located `corpus.yaml` lifted into the spec's
  retrieval doc, ADR 0112 mechanism).
- Offline tests: `test_router_aorc_precip.py` (10) + `test_router_lter_records.py`
  (11) -- spec shape, pure hooks, end-to-end `route() -> dict`, coverage/empty/gate
  errors; synthetic xarray Dataset (AORC) + synthetic EML/TSV (LTER) fixtures, the
  real network path unchanged. `test_catalog_surfacing.py` spec counts bumped
  95 -> 97 (registry 237 -> 239; arm pool delta -94 -> -96). Touched-tree slice
  green (`test_categories`, `test_router_spec_loader`, `test_router_promotion`,
  `test_router_hooks`, `test_router_engine`, `test_router_executors`,
  `test_tool_retrieval`). The offline-suite 9-failure baseline (fetch_resolution x4 +
  river_dye x5) is in untouched trees.

## Consequences

- +2 spec-driven fetchers, +0 coded data-fetchers (ADR 0112's 0 holds). Two PURE hook
  modules (`aorc_precip`, `lter_records`) + two `source.yaml`/`corpus.yaml` pairs; no
  worker image, no flood seam -> no flood canary.
- `fetch_aorc_precip` is precip-scoped (`APCP_surface` only); AORC's other seven
  forcing variables + a SPATIAL accumulation COG (a `raster-cog` ERA5-clone sibling)
  are documented follow-ons, not built.
- `fetch_lter_records` needs a known EDI `package_id` (no keyword discovery); a
  package-search endpoint is a possible follow-on.
- Showcase seeding DEFERRED: the ADR 0200 wave holds `scripts/seed_showcase_cases.py`
  + the daemon (unstaged in git status at kickoff), so per the forward-only norm the
  orchestrator seeds both showcases at close-out (natural prompts recorded in the
  final report).

## Close-out note (orchestrator, 2026-08-08)
Showcase seeding attempted and withdrawn: record-shape fetchers
return series to the model (no standalone layer/chart emission), so
the seeder's layer/chart success criterion cannot apply. The
showcase norm covers templates; record fetchers are exercised live
by their consuming workflows instead (the ADR 0204 replication is
the real showcase for both). Two empty "showcase: fetch ..." cases
from the attempt are deletable junk.
