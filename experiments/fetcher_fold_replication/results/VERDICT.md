# Replication-parity VERDICT -- data-router fold pilots (5)

Authority: docs/specs/router-pilot-contract.md sec 4.2. Twin vs router,
identical synthetic upstream, offline + deterministic (no MinIO). Twin
behavior is the contract; divergences are recorded, never fudged.

| source | verdict | checks | key divergence |
|---|---|---|---|
| fetch_gridmet | PASS | 22/23 | TWIN DEFECT (flag NATE): twin rioxarray writer drops declared nodata=nan -> None; router correctly writes nodata=nan. Router must NOT copy the twin bug; byte-parity needs a twin fix (rio.write_nodata). |
| fetch_hifld_critical_infrastructure | PASS | 16/16 | - |
| fetch_noaa_coops_tides | PASS | 19/19 | - |
| fetch_esri_landcover_10m | PASS | 14/14 | - |
| fetch_census_acs | PASS | 31/31 | - |

## Per-check detail

### fetch_gridmet -- PASS
- [ok] values.band_count
- [ok] values.dtype
- [ok] values.crs
- [XX] values.nodata: twin=None router='nan' -- TWIN DEFECT (flag NATE): twin rioxarray writer drops declared nodata=nan -> None; router correctly writes nodata=nan. Router must NOT copy the twin bug; byte-parity needs a twin fix (rio.write_nodata).
- [ok] values.min
- [ok] values.max
- [ok] values.mean
- [ok] values.bounds
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- spec carries CONUS-gate + typed-empty honesty
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_enum
- [ok] error.date_order
- [ok] error.date_range
- [ok] error.date_before_coverage
- [ok] error.date_future
- [ok] gate.conus
- [ok] error.empty

### fetch_hifld_critical_infrastructure -- PASS
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] values.value_spotcheck
- [ok] schema.columns
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_enum
- [ok] error.empty -- honest header-only FGB (US-only, empty bbox); no fabricated error
- [ok] gate.max_features_cap -- soft paging cap (not an error frame); both truncate at the same value

### fetch_noaa_coops_tides -- PASS
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] schema.time_format
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- typed-empty + one-bad-station honesty present
- [ok] error.upstream
- [ok] error.empty
- [ok] error.bad_bbox
- [ok] error.bad_enum
- [ok] error.date_order
- [ok] error.date_range
- [ok] gate.max_stations_cap -- soft station cap (not an error frame); both truncate at the same value

### fetch_esri_landcover_10m -- PASS
- [ok] values.band_count
- [ok] values.dtype
- [ok] values.crs
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] values.palette
- [ok] caveats.reproduced -- honest no-coverage caveat present
- [ok] error.upstream
- [ok] error.year_low
- [ok] error.year_high
- [ok] error.bad_bbox
- [ok] gate.max_bbox
- [ok] error.empty

### fetch_census_acs -- PASS
- [ok] values.median_income.n
- [ok] values.median_income.geom
- [ok] values.median_income.crs
- [ok] schema.columns
- [ok] values.median_income.value_spotcheck -- expected 65000.0
- [ok] values.median_income.null_floor
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] values.poverty_rate.n
- [ok] values.poverty_rate.geom
- [ok] values.poverty_rate.crs
- [ok] schema.columns
- [ok] values.poverty_rate.value_spotcheck -- expected 25.0
- [ok] values.poverty_rate.null_floor
- [ok] layer.units.poverty_rate -- per-variable LayerURI.units=percent (full fidelity)
- [ok] values.B19013_001E.n
- [ok] values.B19013_001E.geom
- [ok] values.B19013_001E.crs
- [ok] schema.columns
- [ok] values.B19013_001E.value_spotcheck -- expected 65000.0
- [ok] values.B19013_001E.null_floor
- [ok] layer.units.raw_code -- raw-code passthrough LayerURI.units=count (full fidelity)
- [ok] caveats.reproduced -- null-never-fabricated caveat present
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_enum
- [ok] error.empty -- honest header-only FGB (US-only / empty bbox); no fabricated error
- [ok] gate.max_features_cap -- soft paging cap (not an error frame); both truncate at the same value

## Findings: 5/5 parity across the FULL edge matrix (round-2 gaps CLOSED)

The harness now grades the contract-4.2 edge matrix per source (error paths ARE
values): happy-path values/schema/layer + BOTH honesty-floor empty paths + every
invalid-param class (malformed bbox / out-of-range year+date / bad enum) + every
declared gate (conus / max_bbox / soft caps) + a forced upstream failure. Grading
is tightened: layer.bbox_present is a GATING 4.2 layer-output field; error.* and
gate.* are gating; only info.* + a flagged twin-defect are non-gating.

Round-2 divergences the adversarial parity lens named beyond the fixed requests --
all CLOSED to twin behavior and now COVERED by a harness case that would catch a
regression:

1. [CLOSED] esri empty/no-coverage: router raised ESRI_LANDCOVER_EMPTY; twin +
   the esri caveat say ESRI_LANDCOVER_NO_COVERAGE. FIX: SourceSpec.empty_error_suffix
   (default EMPTY; esri = NO_COVERAGE) threaded through every router_empty_error.
   Covered by esri error.empty (empty STAC search, driven through both entrypoints).

2. [CLOSED] esri year unvalidated: year=1850/2099 silently proceeded to STAC (EMPTY)
   vs twin ESRI_LANDCOVER_YEAR_INVALID. FIX: ParamSpec.min/max range gate + per-param
   error_suffix; esri year = {min:2017, max:2023, error_suffix: YEAR_INVALID}.
   Covered by esri error.year_low + error.year_high.

3. [CLOSED] input-error suffix leaked origin: errors.py hardcoded _INPUT_ERROR, so
   census/hifld emitted *_INPUT_ERROR (twin *_INPUT_INVALID) and esri emitted
   *_INPUT_ERROR (twin *_BBOX_INVALID). FIX: SourceSpec.input_error_suffix
   (hifld/census = INPUT_INVALID) + per-param error_suffix (esri bbox = BBOX_INVALID);
   router_input_error takes the suffix; bbox-class gate failures use bbox_error_suffix.
   Covered by every source's error.bad_bbox / error.bad_enum (+ esri gate.max_bbox).

4. [CLOSED] harness under-covered 4.2 (empty path was coops-only). Empty is now
   graded on all 5: typed error for gridmet (GRIDMET_EMPTY) / esri (NO_COVERAGE) /
   coops (COOPS_TIDES_EMPTY); honest header-only FGB for hifld + census (n=0).

5. [CLOSED] gridmet LayerURI.bbox tell: twin omits it, router always populated it.
   FIX: OutputSpec.emit_bbox (default True; gridmet = false). layer.bbox_present is
   now a gating check and matches.

6. [CLOSED, bonus] gridmet date coverage: start<1979 / future-end are twin
   GRIDMET_NOT_AVAILABLE (distinct from INPUT_ERROR). FIX: ParamSpec.min_date +
   max_future_days -> router_not_available_error. Covered by error.date_before_coverage
   + error.date_future (+ error.date_order / error.date_range as INPUT_ERROR).

7. [DOC] esri source.yaml stale "STAC sub-mode is a stub" NOTE removed (the shipped
   stac_to_mosaic implements sas-sign + reproject + uint8 + palette + mosaic).

REPORTED twin defect (flagged for NATE, NOT copied): gridmet values.nodata -- the
twin's rioxarray writer silently DROPS the declared nodata=nan (emits None); the
router correctly writes nodata=nan. This is an observable COG-metadata difference,
scored honestly as ok=False but non-gating because the ROOT CAUSE is the twin
writer (needs rio.write_nodata()); the router must not propagate the bug. Byte-
identical nodata parity requires a twin fix, which only NATE lands.

NATE DECISION (2026-07-29, phase-2 promotion): router-correct nodata=nan is ACCEPTED
as the go-forward; the twin's dropped-nodata defect DIES WITH THE TWIN (deleted in
the pilot promotion) -- no twin fix is landed, the promoted fetch_gridmet writes the
declared nodata=nan correctly and this divergence is closed by the twin's removal.

Documented + CLOSED (full fidelity, NATE 2026-07-29): LayerURI.units was a router
single-string field while census units are per-variable; the JOIN spec now resolves
LayerURI.units per-variable (usd / years / percent / count-for-raw-codes), matching
the twin, so both the per-FEATURE (FGB) and the per-LAYER units vary correctly. Raw
ACS estimate-code passthrough (e.g. B19013_001E, units=count) restored in the JOIN
transform. Census re-graded 31/31 PASS incl. a raw-code request.

Fold-arm drift fix (out of the replication lens, from the regression lens): server
._default_declarable_registry applied the pool substitution AFTER the tier=template
filter had already dropped the fetch_X__spec alias, so its ON-path swap could never
fire. Reordered to substitute on the FULL pre-filter snapshot (matching
_build_index), so all three pool producers now apply the same substitution -- the
drift guard. Verified: OFF the declarable fetch_gridmet resolves to the twin module,
ON it resolves to the router _virtual module, zero __spec alias leak either arm.