# Rain-on-Grid replication methodology (recon; for NATE sign-off BEFORE runs)

Purpose: lay out the site, events, data coverage, calibration protocol, and
comparison matrix to replicate the Godara, Bruland and Alfredsen (2024, Front.
Water 6:1384205) TELEMAC-2D vs HEC-RAS 2D rain-on-grid flash-flood protocol on a
US steep gauged catchment. NO experiments are run from this document -- it goes
to NATE for methodology + input sign-off first (NATE-first experiments rule).

Provenance note: the web-search budget was exhausted mid-session, so items below
are marked [VERIFIED] (confirmed this session), [KNOWN] (established fact /
in-repo), or [CONFIRM] (needs a source check before runs). The paper's own site
is Sleddalen, Norway (10.5 km2, avg slope 0.5 m/m, 77-1379 m, gauge 97.5.0 at
15-min) -- the METHODOLOGY source; the US-cases rule requires replication on a US
catchment, so the paper is the protocol, not the site.

## 1. Site + gauge

Primary site: the ADR 0193 **Coweeta Creek watershed**, Nantahala Mtns, NC.
- [KNOWN] Delineated catchment 30.03 km2 (pysheds `delineate_watershed` on 3DEP
  10 m), pour point (-83.40402, 35.05746); stream network 127 branches / 108.7
  km; steep Southern-Appalachian headwater terrain -- a good analog to the
  paper's small steep catchment. Mesh already built (ADR 0193: 4956 nodes / 9727
  elements, 31-272 m, 0 inverted).
- Observed-discharge gauge options:
  - [VERIFIED] USGS **03500240 Cartoogechaye Creek near Franklin, NC**
    (35.159, -83.394), active, `hasDataTypeCd=iv` (instantaneous / sub-daily),
    reachable through our `fetch_usgs_nwis_gauges` hydrograph (windowed-IV) mode.
    CAVEAT: Cartoogechaye Creek is a SEPARATE, larger Little Tennessee tributary
    (not the 30 km2 Coweeta Creek mesh) -- basin-mismatched; usable only if the
    mesh domain is re-cut to the Cartoogechaye catchment.
  - [CONFIRM] Coweeta Creek proper: the authoritative sub-hourly record for the
    Coweeta catchment is the **USFS Coweeta Hydrologic Laboratory** (Coweeta LTER
    / USFS Southern Research Station) experimental watersheds (WS01/02/08/18/36
    ...), continuous 5-min stage since the 1930s -- NOT in USGS NWIS; access via
    the Coweeta LTER / EDI data portal. Exact station id, record length, and
    sub-daily availability need NATE / USFS-portal confirmation (not fetchable by
    our current USGS-NWIS surface). This is the highest-fidelity observed target
    if we can ingest it; otherwise fall back to a USGS-IV catchment (below).

## 2. Candidate events (2 single-storm + 1 multi-peak)

Hard constraint: our hourly precip product (`fetch_mrms_qpe`, 1h) has an S3
archive beginning ~2020-10, so events MUST post-date that (see section 3).

- [CONFIRM] Single-storm A -- **Tropical Storm Fred remnants, 2021-08-17..18**:
  extreme flash flooding in the western-NC mountains (Haywood/Transylvania,
  adjacent to Coweeta). Short high-intensity single-peak event ~ the paper's
  10-20 h class. Confirm the Coweeta-area rainfall + a clean single-peak gauge
  hydrograph.
- [CONFIRM] Single-storm B -- **Hurricane Helene, 2024-09-26..27**: catastrophic
  western-NC flooding covering the Coweeta area; large single-storm event, tests
  the upper end of the RoG envelope. Confirm gauge did not go out / clip.
- [CONFIRM] Multi-peak -- a sustained winter frontal or rain-on-snow event
  (candidate window: a multi-day frontal passage 2021-2024 with inter-peak
  sustained flow). This is the event the paper's RoG approach is EXPECTED to
  reproduce POORLY (no subsurface return flow) -- the deliberate negative
  control demonstrating the applicability envelope. Pick from the gauge record
  where a multi-peak hydrograph with non-zero inter-peak baseflow is visible.

All three dates + peak magnitudes need confirmation against the chosen gauge
record and against MRMS availability before any run.

## 3. Precipitation coverage for the events

- [VERIFIED, in-repo] `fetch_mrms_qpe` -- NOAA MRMS MultiSensor QPE Pass2,
  gauge-corrected, CONUS ~1 km, finest window `1h`. The flash-flood product.
- Coverage: MRMS S3 (`noaa-mrms-pds`) begins ~2020-10 -> Fred (2021) and Helene
  (2024) are IN coverage; any pre-2020 event is NOT.
- Gaps / honest limits: no AORC (hourly ~800 m, 1979-present) and no Stage-IV
  fetcher currently exist -- if NATE wants a pre-2020 event, an AORC or Stage-IV
  fetcher is a prerequisite. gridMET is daily (too coarse); ERA5 is 27 km global.
- The template consumes the hourly MRMS hyetograph via the rainfall-excess
  preprocessing path (ADR 0195 Decision 2), since the native runoff model is
  constant-intensity only.

## 4. Calibration protocol (CN + Manning per land cover, per engine)

Paper Table 1 gives SEPARATE CN and Manning columns for T2D and HR2D per
land-cover class. Our analog is `cn_infiltration.NLCD_CN_MANNING` (NLCD class ->
CN2 + Manning n, paper T2D column, HSG-B mid values). Protocol:

1. Land cover: `fetch_landcover` (NLCD 2021) over the catchment -> per-node class
   at the mesh nodes -> CN2 + Manning via the Table-1 analog. Direct-CN
   alternative: `fetch_gcn250_curve_numbers` (Jaafar 2019, with dry/avg/wet AMC).
2. CN adjustments: `ANTECEDENT MOISTURE CONDITIONS` (event-specific, from
   pre-storm rainfall); `OPTION FOR INITIAL ABSTRACTION RATIO` (0.2 standard);
   steep-slope Huang correction applied to the CN field in preprocessing.
3. Manning n per land-cover class from the same table (bed friction law).
4. Per-engine columns diverge (paper Table 1): T2D uses lower Manning on bare
   rock (0.02) than HR2D (0.1); calibrate each engine to its own column.
5. Calibration target: maximize NSE (eq 14) / R2 (eq 13) of computed-vs-observed
   outlet discharge via the new `nash_sutcliffe_efficiency` / `pearson_r2`
   primitives; sweep CN (per class) and Manning within physically-defensible
   bands. Deterministic grading = the metric values, never LLM-judged.

## 5. Comparison matrix (T2D vs HEC-RAS 2D, per event)

| Axis | Metric / method |
| --- | --- |
| Accuracy | NSE (eq 14) + R2 (eq 13) of outlet discharge vs observed; peak error % + peak-timing (from `compute_skill_metrics`) |
| Runtime | solver wall time per event, same mesh resolution |
| Mesh behavior | T2D unstructured triangular stability on steep slopes vs HEC-RAS structured-grid; count of unstable/dry-wet artifacts |
| Inundation continuity | max-depth field continuity over the catchment; spurious ponding on ridges |
| Envelope adherence | single-storm NSE (expect good) vs multi-peak NSE (expect poor -- the no-subsurface-return signature) |

## 6. Prerequisites before runs (NATE sign-off items)

1. Confirm the observed gauge: USFS Coweeta-lab station (preferred, needs portal
   ingest) vs a USGS-IV catchment (re-cut mesh to that basin).
2. Confirm the 3 event dates + peaks against the gauge and MRMS coverage.
3. Decide pre-2020 events in/out (AORC/Stage-IV fetcher prerequisite if in).
4. Sign off the CN/Manning per-class bands (paper Table 1 analog) as calibration
   inputs.
5. THEN build the registered template (ADR 0195 deferred build wave) and run.
