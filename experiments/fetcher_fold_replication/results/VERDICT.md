# Replication-parity VERDICT -- data-router fold pilots (5)

Authority: docs/specs/router-pilot-contract.md sec 4.2. Twin vs router,
identical synthetic upstream, offline + deterministic (no MinIO). Twin
behavior is the contract; divergences are recorded, never fudged.

| source | verdict | checks | key divergence |
|---|---|---|---|
| fetch_nifc_fire_perimeters | PASS | 16/16 | - |
| fetch_hifld_transmission_lines | PASS | 16/16 | - |
| fetch_mtbs_burn_severity | PASS | 16/16 | - |
| fetch_cdc_svi | PASS | 16/16 | - |
| fetch_nhd_waterbodies | PASS | 17/17 | - |
| fetch_us_drought_monitor | PASS | 17/17 | - |

## Per-check detail

### fetch_nifc_fire_perimeters -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.empty -- honest header-only FGB; no fabricated error
- [ok] error.bad_bbox
- [ok] error.bad_enum

### fetch_hifld_transmission_lines -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.empty -- honest header-only FGB; no fabricated error
- [ok] error.bad_bbox
- [ok] error.bad_min_voltage

### fetch_mtbs_burn_severity -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.empty -- honest header-only FGB; no fabricated error
- [ok] error.bad_bbox
- [ok] error.bad_year_range

### fetch_cdc_svi -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.empty -- honest header-only FGB; no fabricated error
- [ok] error.bad_bbox
- [ok] values.sentinel_null -- -999 sentinel -> null in both (never fabricated)

### fetch_nhd_waterbodies -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.empty -- honest header-only FGB; no fabricated error
- [ok] error.bad_bbox
- [ok] values.fallback_recovers -- primary 500 -> medium-res fallback recovers (UPPERCASE fields), both n=1
- [ok] schema.fallback_columns -- case-insensitive column_map matches UPPERCASE fallback fields

### fetch_us_drought_monitor -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.value_spotcheck
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- honest-empty FGB caveat present
- [ok] error.upstream
- [ok] error.empty -- honest header-only FGB; no fabricated error
- [ok] error.bad_bbox
- [ok] error.bad_date
- [ok] gate.endpoint_select_archive -- date present -> archive layer /2 selected (endpoint_select)

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

Documented (not a defect): LayerURI.units is a router single-string field while
gridmet/census units are per-variable; the per-FEATURE units (census FGB) vary
correctly via the JOIN. A general fix needs a normalize.units_by_param hook.

Fold-arm drift fix (out of the replication lens, from the regression lens): server
._default_declarable_registry applied the pool substitution AFTER the tier=template
filter had already dropped the fetch_X__spec alias, so its ON-path swap could never
fire. Reordered to substitute on the FULL pre-filter snapshot (matching
_build_index), so all three pool producers now apply the same substitution -- the
drift guard. Verified: OFF the declarable fetch_gridmet resolves to the twin module,
ON it resolves to the router _virtual module, zero __spec alias leak either arm.