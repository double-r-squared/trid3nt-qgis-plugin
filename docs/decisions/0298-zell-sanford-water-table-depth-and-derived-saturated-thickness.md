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
