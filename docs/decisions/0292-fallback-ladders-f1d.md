# ADR 0292 -- fallback ladders, wave F1d: a fault is not a verdict, every share is measured, the ladder is not the whole account

Status: LANDED. Date: 2026-08-20. Follows ADR 0291 (F1c), which a third adversarial
panel re-refuted on five probe-proven findings. Corrects two claims 0291 made.

## What 0291 got wrong

1. "an untyped failure under a rung wraps as `FALLBACK_LADDER_ERROR`, never the
   capability's coverage code, with `retryable` inherited" was TRUE only where no
   gap had been recorded. The walker's gap branch fired FIRST and hardcoded the
   capability's `refuse_error_code` with `retryable=False`, so the single most
   common production shape -- CUDEM paints 89%, the permitted ETOPO rung hits a
   MinIO hiccup -- surfaced as `TOPOBATHY_COVERAGE_GAP`, non-retryable. Proven
   live: a `read_through` fault on the rung's own fetch produced
   `LadderRefused(TOPOBATHY_COVERAGE_GAP, retryable=False)`, and GeoClaw turned it
   into a terminal `GEOCLAW_NO_BATHYMETRY`. A transport fault read as "this AOI
   has no bathymetry source".

2. "COVERAGE IS MEASURED, NOT PROMISED" held for CUDEM only. `etopo_painted` was a
   BOOLEAN (`any(painted[etopo slice])`) and `_rung_coverage` stamped
   `1.0 - cudem` against it. An ETOPO set that reached only part of the AOI (the
   AOI straddling a 15-degree tile boundary with one tile unreadable) stamped
   `etopo_bathy_base / 1.0` and a "REAL below-waterline bed everywhere" warning
   over a raster that was 52% NaN. Proven live by probe: `rung_coverage =
   {'cudem_nearshore': 0.0, 'etopo_bathy_base': 1.0}`, `bathymetry_present=True`,
   NaN share of the returned grid 0.5179.

Also latent: the DECLINE branch gated on `gap_note` existing AT REFUSE TIME, not on
a gap recorded BEFORE the decline, so with two alternatives a LATER rung's gap
retro-justified an EARLIER decline and laundered a retryable primary error.

## What changed

### 1. A fault under a rung is never a coverage verdict

The walker's refusal branches now split by what was actually PROVEN:

- the capability's own coverage code is reserved for GENUINE coverage refusals --
  no rung permitted (the primary's typed gap surfaces verbatim), a rung declined
  while the gap it would fill was OUTSTANDING, or a filling rung that gapped too;
- everything else under a rung wears `FALLBACK_LADDER_ERROR` with the failing
  rung's own `retryable`, and the message carries BOTH the gap context and the
  cause (which also chains through `__cause__`).

### 2. Composers branch on the CODE, not the exception type

`LadderGap` / `LadderRefused` are one type for two truths, so `except (LadderGap,
LadderRefused) -> terminal NO_BATHYMETRY` discarded the distinction along with the
retryability. `geoclaw/inundation` and `schism/tidal_hydro` now re-raise a
`FALLBACK_LADDER_ERROR` / retryable ladder failure unchanged (the tool dispatcher
harvests `error_code` + `retryable` off it) and keep the terminal typed refusal for
a real coverage verdict.

`sfincs/flood` is DIFFERENT by contract and stays so: its tool promise is a typed
failed envelope, never a raise (pinned by
`test_coastal_topobathy_hard_error_threads_into_failed_envelope`), and
`_build_failed_envelope` has no `retryable` parameter. Raising there to preserve a
boolean would break a pinned contract to fix a smaller problem. The code already
separates the verdicts (`FALLBACK_LADDER_ERROR` vs `TOPOBATHY_COVERAGE_GAP`); the
`error_detail` now says the retryability in words the model can act on.

### 3. Every leg's share is MEASURED, at footprint granularity

`_composite_sources_to_array` returns a `footprints` companion to `painted`: each
source's own georeferenced extent in EPSG:4326, or None. `_select_and_merge`
computes disjoint shares from those extents (shapely rectangle union):

- `cudem_nearshore` = the painted CUDEM tiles' 0.25-degree footprint fraction
  (unchanged -- the exhibit's 8/9 is the same number);
- `regional_fine` = the regional extents MINUS the CUDEM footprint;
- `etopo_bathy_base` = the ETOPO extents MINUS CUDEM and regional.

Their sum is `bed_fraction`, the share of the AOI carrying a real bed. Every
"bed everywhere" claim keys on it: the fetch REFUSES when `bed_fraction` falls
short, in the CUDEM-gap branch AND in a new branch for the AOI that never had a
CUDEM gap to begin with (zero intersecting tiles, a partial ETOPO base). A refusal
that has already tried the coarser bed no longer advertises `force_bathy_base` as
the remedy -- it says there is no param that makes the request honest.

Interior nodata INSIDE a source's extent remains measured by nothing. That is the
same documented open edge the CUDEM footprint measure has carried since 0290, and
it is deliberately not closed here: pixel accounting would change what the exhibit
numbers mean.

### 4. `include_regional_fine` no longer false-refuses

The merge required `etopo_painted` for the exempted-serve path, but
`include_regional_fine` never engages ETOPO (the base engages only under
`force_bathy_base` or zero surviving CUDEM). A FINER regional bed that fully
painted the hole was refused with "NO nearshore bathymetry source" -- false. The
condition is now `bed_complete`, which regional paint satisfies, so the merge gate
and the pre-fetch gate permit exactly the same requests and disagree only about
what actually painted. The sole production caller
(`geoclaw._fetch_fine_nearshore_for_geoclaw`) swallowed every exception at
`logger.info`, so the P2 nested fine-topo layer vanished silently on every
partial-CUDEM AOI. It now returns `(uri, degrade_note)`; both no-layer paths log a
WARNING and hand back the note, and the note rides the answer layer's
`fallback_note` through `_stamp_bed_provenance`.

### 5. A decline only owns a refusal it was actually standing in front of

The walker records `(rung, the gap outstanding when it was declined)`. The decline
branch fires only on that list, so a later rung's gap can never retro-justify an
earlier decline: a declined `alt1` plus a retryable `CAP_UPSTREAM` primary now
surfaces the PRIMARY's error with `retryable=True`, and the decline still rides the
activation.

### 6. The measured map is VALIDATED, and the ladder admits it is not the whole account

`_reconcile_to_paint` no longer drops what it does not recognise:

- a key naming no declared rung is logged LOUD -- the ladder is not a complete
  account of what painted the result -- and the walk's own rows for the declared
  rungs stand;
- shares that do not sum to 1.0 are logged LOUD, naming the direction (above 1.0
  is double-counted paint; below it, part of the request came from outside the
  ladder or from nothing).

`regional_fine` is exactly such a key. It is deliberately NOT declared as a rung: a
ladder alternative must be a DEGRADATION, and the NCEI regional coastal DEM is
FINER than the primary. Reporting it anyway is the lesser evil -- a model reading
`cudem 89% / etopo 0%` with no third row would conclude 11% of its domain is
unpainted. The warning it triggers is the honest standing invitation for F2 to give
the ladder a slot for a non-degrading contributor.

### 7. A row the gate never saw says so

`_reconcile_to_paint` appends rows for rungs the capability laid down itself (the
auto-ETOPO zero-CUDEM path stamps `etopo_bathy_base / cross_dataset / 1.0` even on
a call that permitted nothing). The data already served, so gating is moot -- but
0291's walker docstring claimed the walker "fires the loudness gate before any
degradation", which those rows falsify. The docstring is corrected, the row's note
says the gate never saw the rung, and the narration carries a `GATE-UNSEEN`
sentence.

### 8. An UNMEASURED serve carries no numbers anywhere

Under a caller's `coverage_exempt_params` the activation stamped no rows and said
"the per-rung share of this result is UNMEASURED" -- while the envelope beside it
still carried a numeric `rung_coverage`. `_stamp_activation` now clears
`rung_coverage` (the same measured-share seam the walker reads) on an unverified
activation, for any result type that declares it. The envelope a model reads may
not contradict its own note.

### 9. Cache

`PROVENANCE_SCHEMA` 2 -> 3. The rung shares are measured differently now, so every
sidecar written under schema 2 reports a reading its bytes no longer justify. The
four provenance-bearing sources each refetch once (0291's mechanism, used as
designed).

## Deleted

`_build_merged_topobathy` + `_merge_topobathy_to_array` (ledger row, ADR 0292).
0291 kept them "because they carry the mosaic-precedence coverage"; in fact they
had ZERO production callers, were driven only by four tests, and never received
the ETOPO land-mask fix -- a parallel merge that cannot reproduce the live merge's
honesty pins the wrong behaviour. Deleted with their four tests; the behaviours
they covered are exercised end to end through `_select_and_merge`.

## Evidence

Repo evidence corrected: `_patch_total_cudem_loss` staged the 3DEP leg at +12.0 m,
which made `_mask_land_leg_ocean_fill` a NO-OP -- the fix 0291 landed was unproven
by its own test. The leg is now staged at 0.0 m (the real flat sea-level ocean
fill), and a new test asserts the served composite's max is below 0.0.

Live, exhibit AOI `(-85.55,29.70,-85.40,29.85)` -- UNCHANGED, now on measured
disjoint shares:

- A (no `fallback=`): `TOPOBATHY_COVERAGE_GAP`, `covered_fraction=0.8888888...`,
  `retryable=False`.
- B (`fallback=("etopo_bathy_base",)`): rows `cudem_nearshore 0.888889` +
  `etopo_bathy_base 0.111111`, `rung_coverage={'cudem_nearshore': 0.8888...,
  'etopo_bathy_base': 0.1111...}`, `cudem_tile_count=3`, warning "PARTIAL-CUDEM
  BATHYMETRY ... paint 89% ... 11% is the GLOBAL NOAA ETOPO 2022".
- C (`force_bathy_base=true`): `fallbacks=[]`, `rung_coverage=None`, the UNMEASURED
  note -- no numbers beside the note that says there are none.

Live, Sonoma coast `(-123.50,38.735,-123.47,38.765)` -- a genuine partial-CUDEM AOI
(the CUDEM 1/9" collection stops at lat 38.75) inside the CoNED CA-north regional
footprint:

- no `fallback=`, no exemption: `TOPOBATHY_COVERAGE_GAP`, `covered_fraction=0.5`.
- `include_regional_fine=True`: SERVES. `cudem_tile_count=1`,
  `regional_tile_count=2`, `bathymetry_present=True`, warning "PARTIAL-CUDEM
  BATHYMETRY: ... paint 50% of AOI ...; 50% is the NCEI REGIONAL fine coastal DEM
  (~1 m, finer than CUDEM)". Before this wave the same call refused as "NO
  nearshore bathymetry source".
- `geoclaw._fetch_fine_nearshore_for_geoclaw` on that AOI returns the fine URI with
  `degrade_note=None` -- the P2 nested shore topo surfaces. On the exhibit AOI
  (no regional collection) it returns `(None, note)` with the LABELED DEGRADE text,
  logged at WARNING: the layer degrades, never silently.

Suite: four slices at baseline (4 `fetch_resolution` in `[f-o]` + 2 `river_dye` in
`[p-r]`; `[a-e]` and `[s-z]` fully green), contracts 721, `ws_smoke`
`all_passed=True`, flood canary `status=ok`. No `workers/` path touched, so no
image rebuild.

## Consequences

- `FALLBACK_LADDER_ERROR` is now reachable on the GAP path, so a composer that
  excepts on a ladder must branch on `error_code`, not on the exception type.
- `_composite_sources_to_array` returns a 5-tuple.
- `TopobathyResult.rung_coverage` may carry a `regional_fine` key, and is `None`
  under a caller's exemption.
- `_fetch_fine_nearshore_for_geoclaw` returns `(uri, note)`.
- A one-time cache refetch for the four provenance-bearing sources.

## Noted, not fixed (F2 or later)

- The ladder schema has no slot for a NON-degrading contributor, so `regional_fine`
  can only be reported and logged, never declared. Giving `Ladder` an
  `enhancement` class would remove the standing warning; it is a schema change and
  belongs with F2's full-coverage law, not here.
- Interior nodata inside a painted source extent is still measured by nothing
  (carried from 0290/0291).
