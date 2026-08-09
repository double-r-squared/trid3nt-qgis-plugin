# ADR 0202 - Rain-on-grid replication vs the Coweeta gauge: STOPPED on a data coverage gap

Date: 2026-08-08
Status: STOPPED (a real finding, not a failure) -- the gauge record and the precip
archive do not overlap; the replication cannot be graded. Unblock paths recorded.
Source: Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205 (the RoG
methodology). Builds on ADR 0195/0196 (landed RoG machinery), ADR 0193 (Coweeta
watershed mesh). NATE approved the sourcing: Coweeta stays the site, gauge from the
USFS Coweeta Hydrologic Lab / Coweeta LTER record on the EDI portal (not NWIS).

## Context

The replication grades computed-vs-observed OUTLET DISCHARGE deterministically
(NSE eq 14 + R2 eq 13, the landed `compute_skill_metrics` primitives). That
requires, per event: (a) an observed sub-daily discharge series at/near our pour
point, and (b) a precip product covering the same hours to force the model. The
protocol's step 1 mandates checking data coverage FIRST and, if no overlap
exists, STOPPING and reporting the gap rather than substituting silently.

## Data located (EDI portal, PASTA scope knb-lter-cwt)

EDI's native PASTA REST API (search / listDataPackageRevisions / readMetadata /
data) returns HTTP 403 for anonymous "Public Access" from this environment (an
auth/IP restriction, not a network failure -- PASTA answers with authorization
errors). The identical EML + data objects are mirrored PUBLICLY by DataONE
(cn.dataone.org/cn/v2), which resolves each PASTA object PID to its member-node
copy. All coverage below is read THROUGH DataONE from the canonical PASTA PIDs; a
credentialed EDI pull would hit the identical bytes. DataONE's knb-lter-cwt index
is complete through the LTER's final uploads (newest cwt package uploaded
2020-05-05; the NSF Coweeta LTER concluded and data publication to EDI ceased).

Coweeta streamflow holdings, highest-resolution first (verified against the ACTUAL
data entity, not just catalog metadata):

| Package (PASTA PID) | Record | Resolution | Span | Discharge unit |
| --- | --- | --- | --- | --- |
| knb-lter-cwt.3037/19 -- Ball Creek weir house #9 | Water level + discharge | **hourly** | **2014-05-30 .. 2019-01-14** | m3/s |
| knb-lter-cwt.3033/119 -- Watershed 18 (Grady Branch) | Daily discharge | daily | 1936-07-01 .. 2018-10-31 | mm/day (area-norm) |
| knb-lter-cwt.3034/119 -- Watershed 27 | Daily discharge | daily | 1946-11-02 .. 2018-10-31 | mm/day (area-norm) |
| knb-lter-cwt.3032 -- Shope Fork of Coweeta Creek | Daily-ish | coarse | 1934 .. **1999** | -- |
| knb-lter-cwt.3030/16 -- nine "intensive" stage/flow/SSC gauges | stage+flow | sub-daily | 2010-08-16 .. 2011-10-01 | -- |

Provenance (Ball Creek, the one usable sub-daily record):
- Metadata PID: `https://pasta.lternet.edu/package/metadata/eml/knb-lter-cwt/3037/19`
  (sha256 685aa7d1...b9a7526c8, 3,472,282 bytes).
- Data entity `3037_BC9_1_0.TXT` PID:
  `https://pasta.lternet.edu/package/data/eml/knb-lter-cwt/3037/19/c35aa80cb6b763ced2bbae10b9638b48`
  (sha256 0435057d4e239c65a661ce2028be9546abd5f605933dedbec1a68cad2ac91c86;
  3,854,843 bytes; 40,569 hourly rows).
- EML units: `Discharge` = cubicMetersPerSecond (m3/s, direct -- no cfs
  conversion needed); `Water_Level` = meter above the weir blade.
- Reproducible probe: `scripts/sandbox/replication/edi_coweeta_coverage.py` ->
  `coweeta_edi_provenance.json`.

## Weir -> sub-catchment mapping and what our pour point actually drains

Our delineated domain (ADR 0193): pour point (-83.40402, 35.05746), catchment
28.72-30 km2 on 3DEP 10 m. Coweeta Creek is formed by two main forks -- **Ball
Creek** and **Shope Fork**. The gauged records map as:

- **Ball Creek weir #9** (3037): the sub-daily record, on ONE of the two forks.
  Its EML bounding box is W -83.4785 / E -83.4217 / N 35.0738 / S 35.0273 -- the
  whole Coweeta Hydrologic Lab basin ("CWTBASIN", ~2185 ha = 21.85 km2). Ball
  Creek alone drains a fork sub-basin, not the aggregate.
- **WS18 / WS27** (3033/3034): small experimental control watersheds (WS18 =
  Grady Branch, a 120-deg V-notch weir, ~0.12 km2, bbox -83.436..-83.432) --
  daily, and orders of magnitude smaller than our 30 km2 domain.

Spatial caveat (a second finding): our pour-point longitude **-83.40402 is EAST
of the Coweeta Basin's eastern bound -83.4217** -- i.e. ~1.6 km downstream of the
USFS experimental-forest outlet. Our 28.72-30 km2 catchment is Coweeta Creek
BELOW the lab (larger than the 21.85 km2 gauged basin), whereas every EDI weir is
a sub-basin (Ball Creek / Shope Fork forks; WS18/WS27 tiny experimentals)
UPSTREAM within the forest. There is no continuous aggregate Coweeta-Creek-outlet
discharge record in EDI post-2019 (Shope Fork ends 1999; Ball Creek is the fork).
So even ignoring dates, no single EDI weir catchment matches our pour-point
domain -- a re-cut of the mesh to the Ball Creek fork would be required to align
the gauge to the modeled area.

## The coverage gap (the STOP)

- Highest-resolution Coweeta streamflow record (Ball Creek, hourly): ends
  **2019-01-14**.
- Daily records (WS18/WS27): end **2018-10-31**.
- MRMS MultiSensor QPE Pass2 S3 archive (`noaa-mrms-pds`, our `fetch_mrms_qpe`
  feedstock): begins **~2020-10**.
- Overlap: **NONE** -- a ~21-month gap (2019-01-14 -> 2020-10) separates the end
  of the gauge record from the start of the precip archive.

Every candidate event post-dates the gauge record entirely:

| Candidate | Gauge (Ball Creek, ends 2019-01-14) | MRMS (2020-10+) | Gradeable? |
| --- | --- | --- | --- |
| TS Fred remnants 2021-08-17/18 | NO (2.6 yr after record end) | yes | **NO** |
| Hurricane Helene 2024-09-26/27 | NO (5.7 yr after record end) | yes | **NO** |
| Winter multi-peak 2021-2024 (neg. control) | NO | yes | **NO** |

No event has BOTH observed discharge and MRMS forcing. Per the protocol, STOP:
running TELEMAC now would produce computed hydrographs with no observed series to
grade against -- i.e. another template smoke (ADR 0196 C4 already did that), not
a replication. No NSE/R2 can be honestly reported, so no run was executed.

## Unblock paths (recorded; NOT silently pursued)

1. **Precip that reaches the gauge era (recommended).** Ball Creek hourly
   discharge (m3/s, 2014-2019) is a genuine, high-quality fork gauge -- the
   binding constraint is our PRECIP product, not the gauge. Build an **AORC**
   (NOAA Analysis of Record for Calibration, hourly ~1 km, 1979-present, public
   on AWS `noaa-nws-aorc-v1-1km-pds`) or Stage-IV fetcher, then replicate on
   storm events WITHIN 2015-2018 that Ball Creek covers (single-storm calibration
   + validation + a winter multi-peak negative control all pickable from the
   2014-2019 hourly record). Requires re-cutting the mesh to the Ball Creek fork
   sub-basin (bbox above) so gauge and modeled area align. This keeps Coweeta as
   the site AND uses the real USFS gauge. The methodology recon already flagged
   AORC/Stage-IV as the pre-2020 prerequisite.
2. **Post-2019 Coweeta weir data from USFS directly.** The USFS Coweeta
   Hydrologic Lab still operates the weirs; recent (2020+) Ball Creek discharge
   would overlap MRMS and make Fred/Helene gradeable, but that data is not on EDI
   / not programmatically accessible to us -- a NATE/USFS-portal acquisition.
3. **A USGS-IV catchment in the MRMS era.** USGS 03500240 Cartoogechaye Creek
   near Franklin NC (active, sub-daily IV, fetchable) covers 2020-10+, but it is a
   separate, larger Little-Tennessee tributary -- would require re-cutting the
   mesh to the Cartoogechaye basin (abandons Coweeta as the exact site).

## Consequences

- +0 registered tools; no worker image touched; no flood seam -> no flood canary.
- Sandbox reproducibility driver + provenance only: no TELEMAC runs, no
  computed-vs-observed proofs (nothing to compare). The ADR 0196 template-smoke
  proofs (`docs/proof/templates/telemac_rain_on_grid*.png`) remain the current
  RoG visuals.
- The landed RoG machinery (ADR 0195/0196) is unaffected and correct; this ADR
  only records that the Coweeta+MRMS replication as scoped is data-blocked, and
  the cheapest unblock (an AORC fetcher + Ball-Creek-fork mesh, path 1).
