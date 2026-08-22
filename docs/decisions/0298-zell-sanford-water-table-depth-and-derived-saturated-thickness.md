# ADR 0298 -- Zell & Sanford CONUS surficial groundwater: depth to water lands, saturated thickness is recovered from the model

Status: LANDED.
Date: 2026-08-21.
Supersedes the fork left open by ADR 0297 Decision 3.

## Context

ADR 0297 parked aquifer thickness with three routes and no pick: A1 serve the
published depth to water, A2 subtract a staged water-table depth from a global
machine-learning bedrock model, A3 recover the model's own thickness. NATE ruled
**A1 + A3**. A2 is not taken; it survives only as the independent cross-check in
Decision 6.

## Decision 1 -- what the release actually enables (premise check first)

ADR 0297's summary was already wrong once, so nothing here rests on a summary.
Read from the release itself:

- `readme.txt` documents `model/{ID}_MF6_SS_Unconfined_250/` carrying `.top`,
  `.bottoms`, `_1.hk` (hydraulic conductivity, m/day), and `.dis`; and
  `Output_CONUS_dtw_trans/` carrying the CONUS depth-to-water and transmissivity
  mosaics.
- The FGDC record (`zell2020_wrr.xml`) states the models are **two-dimensional**
  and that **transmissivity was the PEST-calibrated field**.
- Subdomain `0601_0602_0603_0604`'s own package files settle the physics:
  `.npf` sets `ICELLTYPE CONSTANT 1` (unconfined) with `K OPEN/CLOSE ..._1.hk`,
  and `.dis` sets `NLAY 1`. So K is an INPUT and T is not -- MODFLOW-6 forms
  `T = K * (head - BOTM)`, and the published transmissivity must be a
  post-processed product of the converged solution.
- The published rasters are **Albers** (`+proj=aea +lat_0=23 +lat_1=29.5
  +lat_2=45.5 +lon_0=-96`), 250 m, 18464 x 11424, nodata -99999 -- NOT the
  lat/lon grid `modelgeoref.txt` might suggest. That file is an informational
  corner listing, not the raster's georeferencing.

So A3 is recoverable, and by two routes that must agree:
`b = T / K` and `b = (TOP - BOTM) - dtw` (since `dtw = TOP - head`).

## Decision 2 -- the derivation is `b = T / K`, and the identity is proved

Both routes were computed for subdomain `0601_0602_0603_0604` and compared over
all 1,695,168 active cells:

    T/K            : min 0.0004  max 150.0333  mean 43.9171
    (TOP-BOTM)-dtw : min 0.0004  max 150.0333  mean 43.9171
    max |difference| = 6.4e-06 m      Pearson r = 1.0000000000

They are the same quantity to float32 rounding. `T / K` is the route taken: it
consumes the published, calibrated CONUS transmissivity mosaic directly, so the
mosaic seams and nodata are the publisher's own, and it needs one array per
subdomain instead of two.

The identity is then re-checked CONUS-wide as a structural test rather than
trusted. Because `TOP - BOTM` is a prescribed per-zone constant, `b + dtw` must
land on one of a few dozen round numbers everywhere:

    124,883,198 of 124,884,102 cells (99.9993%) are within 0.02 m of an integer
    zone thickness; 904 cells (0.0007%) are not.

`validate()` runs this check on every staging run and refuses to upload if it
degrades.

## Decision 3 -- K comes from the parameter-zone map, not the ASCII arrays

The obvious route to a CONUS K mosaic is the 75 subdomains' `_1.hk` arrays, i.e.
the 4.08 GB of `models.NN.zip`. Six of the eighteen (03, 04, 10, 11, 12, 13,
covering the Great Plains and the arid Southwest) were migrated to S3 behind the
ScienceBase file manager in December 2025; their only download route is an
authenticated GraphQL call (`api.sciencebase.gov/graphql`, `UNAUTHENTICATED`
without a bearer token). Interactive auth is NATE's step and never scripted
around, so that route is closed.

It is also unnecessary. K is piecewise constant on the calibration zones -- 31
distinct values against 4.1 million cells in subdomain 0601 -- and both halves
are published unauthenticated:

- `{ID}_surfgeo_transformedID_huc4.tif` (in `Data_Subdomain.zip`, 53 MB) is the
  georeferenced surficial-geology x HUC4 parameter-zone map.
- `{ID}_opt.par` (in `PEST_Subdomain.zip`, 62 MB) holds the optimized
  `hk_<zone>` values.

Reconstructing K as `hk_<zone>` through the zone map reproduces the array the
model actually ran **exactly**: over subdomain 0601's 1,695,168 active cells,
`max |difference| = 0.0`, exact on 100.0000%. `verify_k_reconstruction` re-runs
that proof against the one model archive that is still unauthenticated
(`models.06.zip`, 50 MB) and raises rather than continue if it ever fails.

115 MB of published files replace 4.08 GB, and the result is the release's own
calibration output rather than a re-parse of its ASCII dumps.

## Decision 4 -- the honest limit, and why it is bigger than "surficial only"

ADR 0297 recorded the limit as "surficial/unconfined only". The release is more
restrictive than that, and this is the load-bearing caveat of the whole landing:

**`TOP - BOTM` is a PRESCRIBED ZONAL CONSTANT, not a mapped aquifer base.** In
subdomain 0601 it takes 22 distinct values -- 20, 21, 22, 26, 27, 29, 30, 32, 38,
39, 46, 51, 52, 54, 73, 85, 91, 100, 150 m and a few more -- pairing with the 31
K values across 46 (K, thickness) combinations. These are calibration zones, not
geology. So the derived quantity is exactly

    b = (prescribed zone thickness) - (depth to water)

which is the saturated thickness of the MODELLED SURFICIAL SYSTEM. It is not the
thickness of a named aquifer, and it is bounded above by a modelling choice
(CONUS max 168.7 m).

The same prescription bounds depth to water: the CONUS grid maxes at 240.52 m
while the release's own well set contains observed depths to 296.76 m, and 1.26%
of cells sit within 0.1 m of their zone bottom (0.0033% are fully dry),
concentrated in arid interior-west basins. A deep value in the Southwest is a
LOWER BOUND.

Both caveat blocks and the thickness docstring's **READ THIS FIRST** paragraph
state this plainly. The tool keeps NATE's `fetch_aquifer_thickness` name because
that is the question class it answers, but nothing it emits calls the number a
mapped or drilled thickness.

## Decision 5 -- one cleaning rule, grounded in the model

The published rasters carry 517 cells (of 124,884,786) whose transmissivity is
NEGATIVE, plus 68 whose depth to water is below -10 m, including junk near
-49999, -73023 and -79288. Rather than pick a threshold by eye, one rule
applies: **a negative transmissivity is impossible under `T = K * b` with K > 0
and b >= 0**, so those cells are masked in every product. That single rule
removes every junk value from BOTH rasters -- afterwards the most negative depth
to water is -25.68 m and the series is smooth, so no second threshold is needed.

The remaining negatives are kept: 15.4 million cells (12.3%) have the simulated
water table ABOVE land surface, which is the model's groundwater DISCHARGE
(wetlands, stream corridors). Clamping them to zero would erase a real result.

## Decision 6 -- the A2 comparison, reported not gated

The route ADR 0297 offered as A2 is used here only as an independent check.
Against ISRIC SoilGrids-2017 BDTICM (250 m, cm, range-read live), the derived
thickness exceeds `depth-to-bedrock minus water-table depth` in:

    Story County, IA          97.9% of cells, median excess +57.00 m
    Maricopa County, AZ       88.5% of cells, median excess +55.76 m
    Yazoo basin, MS           99.0% of cells, median excess +25.61 m

No gate: the two define the aquifer base differently and neither is a survey.
But the direction and size are consistent and they confirm Decision 4 from
outside the release -- so the number goes in the caveats rather than in a
footnote. This is the strongest single reason not to present the product as a
physical aquifer thickness.

## Decision 7 -- transmissivity is built and validated, but NOT shipped

(AMENDED 2026-08-21: NATE ruled REGISTER. See "Amendment -- fetch_aquifer_
transmissivity registered" at the end of this file. The decision below is left
as written -- it is why the tool did not exist at first landing.)

`NormalizeSpec` carries `units_by_param` and `OutputSpec` carries
`style_preset_by_param`, but `quantity` is a single static stamp. So
transmissivity cannot ride `fetch_aquifer_thickness`: a m2/day layer would go
out stamped `quantity=aquifer_saturated_thickness` and painted on a 0-150 m
ramp. Its own spec is the honest form.

Nobody has asked the question yet, so no tool and no staged object: the staging
script builds and validates it (`DATASETS["transmissivity"]["upload"] = False`)
because it is the audited numerator of the derivation, and its west/east check
exercises the same pipeline. Landing `fetch_aquifer_transmissivity` later is one
spec plus one `--dataset transmissivity` run.

That check is worth recording, because the obvious form of it is wrong. The
paper reports transmissivities lower in the western CONUS than the eastern. On
the MEAN that reads backwards (west 575, east 501 m2/day) -- a handful of
western alluvial basins run past 100,000 m2/day and drag the mean up. On the
CONUS-wide MEDIAN either side of 100W the finding is unambiguous: **14.05 vs
87.94 m2/day**. `validate()` tests the median.

## Decision 8 -- validation rests on the publisher's own observations

`{ID}_wl.csv` (75 files, 778,932 rows) carries the long-term average water
levels the models were calibrated to, with CONUS-grid coordinates to sample
them at. Splitting by observation type:

    NWIS wells (w_) n= 38,316   r=0.6319  median|err| 3.085 m  RMSE 19.378 m
    NWI wetlands (nw) n=416,537 median simulated depth 0.007 m (observed 0.0)
    NHD streams (nh) n=324,060  median |err| 0.433 m

The wetland and stream rows are 0.0 by construction, so only the well rows are a
fit statistic; the wetland row is a georeferencing check (a shifted grid could
not put the water table at the surface under mapped wetlands). Both are gates in
`validate()`, alongside the extent check against `modelgeoref.txt`'s declared
corners, a value-range check against the Albers source (a nearest resample may
repeat a value but can never invent one), a coverage-AREA check (the pixel COUNT
must not match -- the staged posting is finer than the source by construction),
and the humid-lowland / arid-basin depth gradient.

## Decision 9 -- staged posting is 1/450 deg, DEFLATE + PREDICTOR=3

`raster_cog._direct_window_to_array` applies the EPSG:4326 bbox straight to the
source transform, so a staged object must be EPSG:4326; the native Albers grid
cannot be served directly. The target posting is 1/450 deg = 246.7 m of
latitude, deliberately FINER than the 250 m source cell so the nearest-neighbour
reprojection never skips a source row.

`PREDICTOR=3` is the floating-point predictor; the integer predictor (2) was
what the first build used and cost about 3% on the full grid for no reason. ZSTD
was measured against DEFLATE at the same predictor and gained 0.1%, so DEFLATE
stays and the objects are readable by any GDAL build.

## Supporting fix -- "this" was a name-channel matching token

Landing `fetch_aquifer_thickness` broke a pre-existing gate:
`test_typo_queries_route_to_target_tools[floof depth for this neighborhood]`
started ranking the new tool first and pushing `compute_flood_depth_damage` out
of the top 5. The cause is not the corpus. `_build_channel_rankings`'s
name-substring channel strips a trailing `s` before substring-matching, so the
query word **"this" became "thi"**, which is a substring of "thi-ckness" -- and
of nothing else in the registry. That handed the tool a ONE-ENTRY channel and
the top RRF slot for a query about floods.

Demonstratives carry no routing signal and appear in most AOI phrasings ("in
this county", "over this bbox"), so `this`/`that`/`these`/`those` join
`_STOPWORDS`. Full `test_search_tools.py` passes (31), and the two new tools
still take rank 1 on 8 of 9 natural questions (rank 2 on the ninth).

## Consequence

- Spec count 98 -> 100; registry 255 -> 257. These are SPECS, not coded
  fetchers: the coded-fetcher count is unchanged at 0.
- `staged/zell_sanford_groundwater/zellsanford2020-v1/` holds two objects:
  `water_table_depth_m.tif` (685,739,503 bytes) and `saturated_thickness_m.tif`
  (621,076,295 bytes), each with a `PROVENANCE_SCHEMA=3` sidecar. They are an
  order of magnitude larger than the recharge objects because the source is
  250 m rather than 800 m; that is inherent, not slack.
- Two new style presets: `water_table_depth_m` (0-50 m, `rdylbu_r` -- blue at
  the surface, red when deep) and `aquifer_saturated_thickness_m` (0-150 m,
  `gnbu`).
- `scripts/stage_zell_sanford_groundwater.py` copies
  `stage_groundwater_recharge.py`'s shape as ADR 0297 asked, and adds two things
  a second staged dataset needed: `--step` phases (the build is too long to
  re-run whole), and a reconstruction PROOF step separate from the build.
- The recharge tool's caveat that it must not be read as "a water-table depth,
  an aquifer thickness, or a storage volume" now points at real tools; the
  sentence stays correct and is left alone.
- STILL UNANSWERED, deliberately: thickness of any NAMED or CONFINED aquifer.
  Both docstrings refuse it by name (Ogallala/High Plains, Floridan, Gulf
  Coast). That needs an aquifer-specific hydrogeologic framework, not this
  model, and has no tool.

## Correction (post-landing review, same day)

Two defects in the staging script, both found by inspecting what the first run
actually produced. No design change to the decisions above.

- **The derived product's provenance named no K mosaic.** `build_k_mosaic`
  returned its report in memory only, so running `--step build` as a separate
  invocation -- which the run length makes the normal case -- stamped
  `derivation.k_mosaic: {}` into the sidecar. A derived raster whose provenance
  cannot say what it was divided by is not auditable. The report now persists to
  `conus_k_report.json` beside the mosaic, `--step kverify` folds its proof into
  the same file, and a build of any `derived` dataset REFUSES to start without a
  verified report rather than stamping an empty one. Re-staged: the sidecar now
  carries the mosaic (124,884,583 cells, 278,964 in subdomain overlaps,
  K 0.00348-1000.0 m/day) and the exactness proof. The rebuilt object is
  byte-identical (`staged_sha256 a3b978b9d301...` unchanged), so staging is
  deterministic.
- **A failed download cached itself.** ScienceBase answers a migrated file URL
  with HTTP 200 and an HTML app shell, so the first attempt at `models.03.zip`
  left a 4,255-byte "zip" on disk that `download()` then SKIPPED as already
  present on every later run. `download()` now checks the PK magic on a `.zip`
  destination, discards a cached non-zip, and raises naming the auth-migration
  cause when a fresh fetch is not a zip either.

## Correction (verifier-refuted claim, 2026-08-21)

A verifier reproduced the whole live-acceptance surface for this landing and
refuted one claim: `tests/test_router_zell_sanford_groundwater.py`'s module
docstring recorded Maricopa County, AZ as reading a median depth of 9.34 m and
a median thickness of 135.97 m. That pair does not reproduce from any
discoverable Maricopa County bbox and carried no bbox of its own -- the
Story-County-style `_BBOX` precedent that would have let anyone check it was
missing. NATE ruled the record be corrected rather than the commits rewritten
(commit messages are history).

Re-run live against `trid3nt_server.data.fetchers._router.executors.raster_cog
._direct_window_to_array`, real MinIO endpoint, no mocks, over the county
bbox `[-113.3350468, 32.5049739, -111.0399049, 34.0481432]` (TIGER cartographic
boundary extent):

    fetch_water_table_depth   716,902/716,902 finite  median 43.939 m
    fetch_aquifer_thickness   716,902/716,902 finite  median 71.523 m

The Story County IA pair the same docstring carries (median depth 5.02 m,
median thickness 93.09 m, bbox `[-93.70, 41.86, -93.20, 42.21]`) was re-run the
same way and reproduces exactly -- only the unaccompanied Maricopa pair was
wrong. The test docstring now carries the corrected numbers with the bbox
alongside them.

One wording note, no behavior change, and one self-correction: the landing
commit's summary line reads "Gates: four slices at the exact baseline (4
fetch_resolution + 2 river_dye)", matching `CLAUDE.md`'s "Law 1" (also 4 + 2).
A prior draft of this section, working from a stale cross-session memory note
that said "9 failures (fetch_resolution x4 + river_dye x5)", asserted the
commit undercounted river_dye and edited `CLAUDE.md` to say 5. That edit was
WRONG and has been reverted. Re-run live, twice -- `tests/test_run_river_dye_
scenario.py` in isolation and inside the full `[p-r]` alphabetical slice, both
with and without `.env.local` sourced -- the count is deterministically 2
(`test_tool_rejects_invalid_bbox` and `test_tool_rejects_both_location_and_
bbox`, both a `TELEMAC_DISCHARGE_INPUT_REQUIRED`-vs-`TELEMAC_PARAMS_INCOMPLETE`
error-code mismatch, unrelated to this landing). The commit message and
`CLAUDE.md` were both already correct; the cross-session memory note was the
stale one and needs its own correction outside this repo. The repo's own ADR
trail confirms it: 2 river_dye is the extensively re-verified live baseline
from ADR 0281 onward (`0281`, `0282`, `0284`, `0287`, `0288`, `0291`-`0293` all
state "4 fetch_resolution + 2 river_dye" from live runs) -- "5 river_dye" was
real, but only in the older ADRs (0041 through 0206ish), from before whatever
fix dropped it to 2. Lesson: re-verify a baseline claim live before
"correcting" a record from it -- a memory note can be stale in either
direction.

## Amendment (2026-08-21) -- fetch_aquifer_transmissivity registered

NATE ruled REGISTER on Decision 7's parked transmissivity spec. It now ships
as its own tool, `fetch_aquifer_transmissivity`, m2/day, its own quantity
(`aquifer_transmissivity`) and its own style preset
(`aquifer_transmissivity_m2_day`, a viridis ramp rescaled 1-1000 m2/day --
wide enough for the median-east tail, clamping only the rare
hyper-transmissive alluvial-basin cells).

**Staging.** `--dataset transmissivity` was re-run against the same CONUS
archive (re-downloaded, sha256 verified, no drift) and uploaded --
`DATASETS["transmissivity"]["upload"]` no longer parks it. `validate()`
re-confirmed the release's headline regional finding on the CONUS median:

    median west of 100W  14.05 m2/day
    median east of 100W  87.94 m2/day

byte-for-byte the same as the parked build's numbers -- the pipeline was
already correct, only shipping was withheld. `staged_sha256` and every other
check (CRS, posting, COG layout, extent, value-range and coverage-area
preservation, non-negativity) passed on the same run.

**Retrieval.** All 10 `corpus.yaml` phrasings surface
`fetch_aquifer_transmissivity` in the model-free `retrieve_visible_tools(text,
None, 8)` top-8 (10/10). `test_search_tools.py` (31 cases, including the
demonstrative-stopword typo gate Decision 7's sibling landing fixed) stays
green with the third tool added to the registry.

**Live acceptance**, direct `raster_cog._direct_window_to_array` calls against
the real staged object (no mocks):

    Story County, IA        (east of 100W)  median 183.590  mean  207.434 m2/day
    Great Basin, central NV (west of 100W)  median  22.087  mean   27.913 m2/day
    Maricopa County, AZ     (west of 100W)  median 1334.957 mean 7574.231 m2/day

Story County (east) reads far higher than the Great Basin AOI (west), matching
the paper's median-based claim at the local level too. Maricopa County is
itself one of the "handful of western alluvial basins" the caveats warn about
-- a Phoenix-basin alluvial aquifer with very high local transmissivity, whose
huge mean (7574) versus its still-elevated median (1335) is exactly the
median-vs-mean distortion Decision 7 recorded from the CONUS-wide numbers. It
is cited here, not discarded, because it is a live demonstration of the
caveat rather than a contradiction of it.

**Offline tests.** `tests/test_router_zell_sanford_groundwater.py` was
extended from two specs to three (`_NAMES` now includes
`fetch_aquifer_transmissivity`) rather than duplicated: every generic
structural test (identity, metadata, payload estimate, style-preset
resolution, staged-uri resolution, missing-endpoint and staged-404 typed
errors, CONUS-envelope refusal, Key West / off-domain EMPTY, partial-window
non-gating, unscaled pass-through) now runs against all three by
parametrization. Added: quantity/units distinctness from the thickness spec
(the whole reason this spec exists), west/east-median caveat wording, and a
three-way shared-grid check. 62 offline tests pass (was 31 before this
amendment: parametrization change, not 1:1 new-test count).

**Consequence.** Spec count 100 -> 101; registry 257 -> 258. Coded fetchers
stay 0. `staged/zell_sanford_groundwater/zellsanford2020-v1/` gains
`transmissivity_m2_day.tif` (626,704,756 bytes) with a `PROVENANCE_SCHEMA=3`
sidecar.

## Amendment (2026-08-21) -- hardening fixes from the same review

All found by the verifier that refuted the Maricopa claim above; all
non-blocking, all landed in the same pass:

1. `saturated_thickness_m.provenance.json`'s `source_member` named the
   depth-to-water raster as the derived product's numerator; it is the
   transmissivity raster (`derivation.T` was already correct). Fixed in code
   (`source_member` now keys off `water_table_depth` specifically, trans
   otherwise) and the live sidecar was patched in place (metadata-only PUT;
   the raster bytes are unchanged).
2. `cleaning_rule`'s "517 of 124,884,786" was a literal string pasted from the
   first run's console output. `compute_cleaning_report` now counts it fresh
   at staging time from the extracted CONUS rasters and persists it
   (`conus_cleaning_report.json`, mirroring `K_REPORT_PATH_NAME`'s pattern).
   The freshly computed count on this run matched the old literal exactly
   (517 of 124,884,786) -- the number was right, it just was not being
   recomputed.
3. `DATA_SUBDOMAIN`, `PEST_SUBDOMAIN` and `VERIFY_ARCHIVE` are now sha256-pinned
   (`21b81935f52a...`, `a307154ad8dc...`, `1891d3a29f28...` respectively,
   computed from the copies this run downloaded), through a new
   `download_verified()` that CONUS_ARCHIVE's existing check is left riding
   its own dedicated path. An unpinned zone-map re-issue could previously have
   silently mis-assigned K for any of the 74 subdomains `verify_k_reconstruction`
   does not directly check.
4. `validate()`'s "b + dtw reproduces the model's prescribed zonal TOP-BOTM"
   check was a hardcoded 2-degree Kansas window, labeled SPOT CHECK, while the
   module docstring and this ADR both claimed it ran CONUS-wide. It now
   block-iterates the FULL staged grid (same cost profile as the min/max/mean
   and coverage-area passes already in `validate()`). Live CONUS-wide result
   against the staged objects: 165,991,651 cells considered, 99.9992% within
   0.02 m of an integer zone value, 96 distinct integer zone values, range
   5-150 m.
5. The zone-floor guard (`uniq.min() >= 15.0`) was window-dependent: the
   Kansas window never touched the release's real 5 m coastal zone (LA/ME/NY/
   FL), 1,033,239 of 165,990,406 near-round cells CONUS-wide (0.62%). The
   guard is now `>= 5.0`, and the "20-170 m" band claimed in three places
   (both fetchers' caveats/docstring and a `publish_layer.py` comment) is
   corrected to the true "5-150 m" -- 168.7 m (the CONUS max of the staged
   THICKNESS raster) is `b` alone at a 150 m zone under a negative `dtw`, not a
   larger zone constant, and was never a zone-band number to begin with.
6. `build_k_mosaic`'s cache-guard conjunct `and K_REPORT_PATH_NAME` was always
   truthy (a module-level string constant) -- dead code, removed.
7. See the offline-baseline self-correction above (`CLAUDE.md` + this ADR --
   the wrong number was a stale cross-session memory note, not the repo).

Gates re-run after all of the above: 62/62 `test_router_zell_sanford_
groundwater.py`, 14/14 `test_catalog_surfacing.py`, the four-slice offline
suite at the true 6-failure baseline (4 `fetch_resolution_gate` in `[f-o]` + 2
`run_river_dye_scenario` in `[p-r]`, no new failures), `test_search_tools.py`
31/31, `contracts/tests` 721/721, `scripts/ws_smoke.py` all_passed. `[f-o]`
also carries 2 pre-existing `test_model_fire_spread_chain.py` failures
(`AttributeError: 'FireSpreadLayerURI' object has no attribute 'get'`,
reproduces in isolation, last touched by an unrelated ADR 0297 commit) --
confirmed out of this landing's scope by `git status` (zero ELMFIRE/fire-spread
files touched) and left to a separate job.
