# ADR 0297 -- staged-dataset fetchers; groundwater recharge lands, aquifer thickness parks

Status: LANDED (recharge) / PARKED WITH A FORK (aquifer thickness).
Date: 2026-08-21.

## Context

Two derived-data capabilities were queued together (IDEAS 2026-08-19 and
2026-08-20): aquifer saturated thickness and groundwater recharge. Neither
source is served by a live value API -- the 2026-08-20 probe found the USGS
ArcGIS/WMS distribution links for the recharge release dead, and a MapServer
would return rendered images rather than values anyway. Both were therefore
scoped as STAGED DATASETS: download the published archive once, convert it to a
COG, prove the conversion against the publication, and host it in the agent's
own object store so the fetcher does an ordinary windowed range read.

## Decision 1 -- the staged-dataset pattern

A staged dataset is a published grid this repo converted once and now hosts. It
has three parts:

1. A committed, re-runnable staging script (`scripts/stage_groundwater_recharge.py`)
   that downloads by DOI/ScienceBase URL, checksums the archive, converts to a
   float32 EPSG:4326 COG (internal 512 px tiles + overviews), VALIDATES the
   result against the publisher's own printed numbers, and refuses to upload
   when a check fails.
2. The object plus a `PROVENANCE_SCHEMA=3` sidecar next to it, under
   `s3://<cache-bucket>/staged/<capability>/<version>/`. The `staged/` prefix
   sits OUTSIDE the `cache/<ttl_class>/` tree on purpose: a staged dataset is a
   reviewed artifact, not a cache entry, and must never be reachable by TTL
   eviction.
3. A source spec whose endpoint is the `s3://bucket/key` pair and nothing more.
   The HOST is deployment state, so `transport/staged.py` resolves it against
   `AWS_ENDPOINT_URL` at read time (mirroring the plugin-side `s3_to_http`).
   `direct_window` therefore serves a staged object with no new access mode.

Validation is the load-bearing half. The script checks grid geometry and extent
against the release metadata's declared bounding coordinates and posting, checks
whole-grid min/max/mean against the statistics the publisher shipped inside the
archive, and probes the climate gradient the paper's central claim implies. A
conversion that silently rescaled, flipped, or resampled the grid fails at least
one of those and never reaches the bucket.

## Decision 2 -- `fetch_groundwater_recharge`, two sources on one tool

Both published CONUS grids are staged and selected by a `source` enum rather
than split across two tools:

- `reitz_2017` (default) -- USGS Reitz et al. 2017, doi 10.5066/F7PN93P0, paper
  doi 10.1111/1752-1688.12546. 30 arc-sec (~800 m), 2000-2013 mean TOTAL
  recharge from an empirical water-budget regression. Native m/yr.
- `wolock_2003` -- USGS Wolock 2003, doi 10.5066/P9FSSVF3. 1 km, mean annual
  NATURAL recharge as base-flow index times 1951-80 mean annual runoff. Native
  mm/yr.

One tool, because they answer the SAME question and their disagreement is the
useful part: the methods are unrelated (water-budget regression vs base-flow
partition), so fetching both over one AOI yields an honest uncertainty range
rather than a contradiction to resolve. Two tools would have hidden that pairing
behind a naming choice the model has no reason to make.

Both are staged in mm/yr. The only value transform is the `reitz_2017`
m/yr x 1000 factor, recorded in its provenance sidecar; reprojection is NEAREST
throughout so every staged pixel is a source pixel. The Reitz source is already
on the target 30 arc-sec posting in EPSG:4269, so its own cell geometry is kept
and the NAD83 -> WGS84 step is a pixel-aligned copy -- letting GDAL choose an
unaligned target grid instead dragged nodata across coastlines.

Coverage is CONUS-only and refused as such: `gates.conus_only` raises
`RECHARGE_INPUT_INVALID` naming the CONUS envelope for an AOI beyond it, and an
AOI inside the envelope but off the land grid (open ocean, the Great Lakes)
raises `RECHARGE_EMPTY`.

Supporting fix: `ingest.nodata_gate` was inert for a NaN-sentinel source
(`arr != nan` is True for every pixel, including the nodata ones), so an
entirely-empty window would have passed as a valid layer. The gate now uses the
finiteness test when the sentinel is NaN. No prior spec is affected -- the only
other `nodata_gate` user is gcn250, whose sentinel is 255.

## Decision 3 -- aquifer thickness PARKS; the kickoff premise is false

The queued thickness build assumed the Zell & Sanford 2020 release
(doi 10.5066/P91LFFN1) ships a CONUS saturated-thickness array in
`Data_CONUS.zip`. It does not, on two counts, both verified against the release
itself rather than its summary:

- `Data_CONUS.zip` (sha256 `31053132f7331b91ec6743e14fef7484dfe952aae3fabf75a30bb38de65c2a6d`)
  contains five files: `Clapp_Hornberger_1978_Table2.csv`,
  `conus_c_param_250.tif`, `conus_idomain_250.tif`,
  `conus_MF6_SS_Unconfined_250_rz_realization_0.tif`,
  `fan2017_rootingdepth.csv` -- unsaturated-zone ancillary inputs and the model
  domain mask. No water-table depth, no hydraulic conductivity, no thickness.
- The release publishes no saturated thickness anywhere. The paper abstract
  names four generated data sets: transmissivity, depth to the water table,
  base-flow discharge, and unsaturated-zone water content. The CONUS rasters
  live in `Output_CONUS_trans_dtw.zip` (918 MB) and are depth-to-water (m) plus
  transmissivity (m2/day). The model is also 250 m, not the 1 km the kickoff
  assumed (`_MF6_SS_Unconfined_250`).

Every route from here to a thickness raster is a derivation, and which
derivation is a design decision, not an implementation detail:

- **A1 -- serve what is published.** Stage the Zell-Sanford depth-to-water grid
  as `fetch_water_table_depth`. Directly usable, no derivation, but a different
  capability than the one queued.
- **A2 -- thickness = depth-to-bedrock minus water-table depth.** Pairs the
  staged Zell-Sanford DTW with ISRIC SoilGrids-2017 BDTICM (250 m global, range-
  readable live, units cm). Cheap, but it composes a machine-learning global
  bedrock model with a calibrated US groundwater model across different
  vintages, resolutions, and definitions of "bedrock".
- **A3 -- recover the model's own thickness.** `b = T / K`, or
  `b = top - dtw - bottom`, from the per-subdomain `.hk` / `.top` / `.bottoms`
  ASCII arrays. Faithful to the simulation the paper calibrated, but it means
  mosaicking 75 subdomain model archives (~4 GB) and parsing MODFLOW ASCII
  arrays with per-subdomain georeferencing.

None of these is picked here. The honest limit already recorded in IDEAS
(surficial/unconfined only; confined-aquifer thickness keeps the scenario tag)
still stands whichever route wins.

## Consequence

- Spec count 97 -> 98; registry 254 -> 255. These are SPECS, not coded fetchers:
  the coded-fetcher count is unchanged at 0.
- `staged/groundwater_recharge/` becomes the first tenant of a prefix that later
  staged datasets share. A second staging script should copy the shape of
  `stage_groundwater_recharge.py` rather than invent a second one.
- `transport/staged.py` is the single place a staged `s3://` endpoint becomes a
  URL. A future staged VECTOR source needs the same resolution wired into its
  own executor; only `direct_window` reads it today.
- The recharge tool answers "how fast does groundwater recharge here" but NOT
  "how thick is the aquifer" -- until the fork above is decided, that question
  has no tool and must not be answered by proxy.

## Correction (post-landing review, 2026-08-21)

An adversarial review of the landing found three defects, all fixed in place
(no design change to Decisions 1-3 above):

- **The CONUS-water caveat was false.** The spec claimed an AOI "off the land
  grid (open ocean, the Great Lakes)" raises `RECHARGE_EMPTY` on either source.
  It does not: `reitz_2017` (the default) encodes inland water as a FINITE
  0.0 mm/yr, not a nodata sentinel -- an AOI entirely over Lake Michigan reads
  as a plausible all-zero raster. Only `wolock_2003` stamps inland water NaN
  and raises `RECHARGE_EMPTY` there. Verified live against the staged rasters:
  a mid-Lake-Michigan window is 144/144 finite pixels at 0.0 on `reitz_2017`
  and 0/144 finite on `wolock_2003`. The caveat, the docstring, and the tests
  now state the true per-source split instead of the uniform claim.
- **A staged 404 was indistinguishable from honest no-coverage.** A missing or
  misconfigured `AWS_ENDPOINT_URL` made `staged.staged_object_url` fall back
  silently to real AWS (`endpoint_url=None` is "use the default AWS endpoint"
  to boto3/GDAL, not "no override"), and the resulting 404 against a
  nonexistent/foreign bucket surfaced as `RECHARGE_EMPTY` -- "no data for this
  AOI" for a request fully inside declared CONUS coverage. `staged.py` now
  raises `StagedEndpointNotConfigured` when no endpoint is configured (no
  legitimate real-AWS fallback exists -- the account is decommissioned), and
  `raster_cog._direct_window_to_array` maps both that and any `TransportNotFound`
  against a staged url to a typed `STAGED_OBJECT_UNAVAILABLE` upstream error
  (retryable, naming the endpoint resolution) instead of the coverage-empty
  frame. The equivalent trap is closed in the other direct-network executors
  (`vector_fgb`, `station_timeseries`, `join`) -- a staged `s3://` uri reaching
  one of them now raises a typed error instead of hitting httpx with an
  unsupported scheme; they carry no staged-source today, so this only forecloses
  a future silent failure.
- **Ambient AWS in `scripts/stage_groundwater_recharge.py`.** Its upload step
  built `boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"))`
  -- an unset var resolves real AWS with ambient credentials. The same pattern
  was present, unrelated to this ADR, in 24 other `scripts/` files; all now
  route through a new shared `scripts/_env_guard.py` (`require_local_endpoint`
  for a script whose primary job needs the object store -- exits with a clear
  message when the endpoint is unset or AWS-hosted; `local_endpoint_or_none`
  for a best-effort step that should skip rather than crash the script).

Also, at lower stakes: the `conus_only` gate borrowed gridmet's envelope
(south 25.05), false-refusing Key West (24.55) though the staged Reitz grid
covers to 24.0625 -- `GateSpec.conus_bbox` is now a per-spec override, and this
spec declares the union of both staged grids' real bounds. `validate()`'s
unread `south`/`north`/`res_deg` publisher constants are deleted (`north` did
not even match the staged bounds); `west`/`east`, which the spot check
actually reads, are kept. The Returns block now states that `wolock_2003` is
NEAREST-resampled onto the `reitz_2017` posting, not natively 30 arc-sec.
