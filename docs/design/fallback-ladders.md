# Fallback ladders - the declared-degradation design

NATE-shaped design (2026-08-17 discussion), approved as written.
Replaces hidden substitution everywhere with one declared, visible,
gated mechanism. Proving case: the SWAN bathymetry rectangle
(docs/design/fallback-audit.md, the mosaic land-fill exhibit).

Waves F1 (ADR 0289), F1b (ADR 0290), F1c (ADR 0291) and F1d (ADR 0292) are
LANDED - read "As built" at the bottom for the shipped shapes before touching
the machinery.

## The contract in five rules

1. LADDERS ARE DATA. A ladder is an ordered list of rungs; each rung is
   the ACTUAL alternative (a source ref from the fetcher specs, or a
   dotted-path callable for non-fetch alternatives) plus its
   consequence class: same_data | cross_dataset | synthetic. The
   terminal rung is always REFUSE (the typed error), stated explicitly.
   Rung definitions live with the capability owner (source specs for
   fetch ladders; the owning module otherwise) in a fallbacks registry.

2. POLICY AT THE CALL SITE, ONE LINER. The composer declares its
   tolerance where the fetch happens, as router-level kwargs on the
   EXISTING fetch call (the purpose= precedent - zero schema churn):

       depths = await fetch_topobathy(bbox,
           fallback=("etopo_bathy_base",),   # rungs, in order
           fallback_gate="auto")             # or "user_gated"

   No fallback= kwarg -> primary-or-typed-error. The raw path has
   nothing to hide because it cannot substitute at all. REFUSE is the
   universal default.

3. ONE WALKER. A single fallbacks module executes every ladder: try
   rungs in order, record which rung served and the coverage fraction
   per rung (mosaics report e.g. "87% rung0 / 13% rung1"), stamp the
   activation into the run's completion payload and narration. No
   per-seam reimplementations - the walker is where the guarantees
   live.

4. ONE GATE. Crossing the loudness floor fires the fallback gate on
   the existing pending-confirmation spine (the input-review/mesh-gate
   family; no new envelope, no plugin change). Floor: same_data rungs
   walk silently; cross_dataset rungs narrate loudly and gate in
   user_gated mode; synthetic rungs ALWAYS gate, and their labeled
   default is refuse. Decline = do not degrade = the typed error
   (mesh-gate decline semantics: the run continues into its own error
   handling, not run-cancel). AUTO/headless applies labeled defaults -
   canaries never hang.

5. VISIBLE ALWAYS. Every activation produces: the narration line, a
   fallbacks block in completion (rung, class, coverage), and the gate
   card when floored. Spot-checking a run answers "what actually
   served?" without reading code.

## What this kills

- The mosaic-internal substitution class (the SWAN 0.0-fill rectangle):
  the topobathy mosaic walks a declared ladder; the land-leg fill
  becomes a rung nobody would declare - the fix is the ladder refusing
  where CUDEM ends unless the composer declared the ETOPO rung.
- The ad-hoc policy params (force_bathy_base/skip_land) - subsumed by
  declared rungs + policy; deprecated as the ladders adopt.
- The audit's NEEDS-LOUDER rows - each becomes a declared rung or an
  honest refuse.

## Out of scope (deliberately)

Behavior fallbacks that are not alternatives-to-data (gate fail-open,
retry/transport) keep their existing honest treatments; they join the
ladder pattern only if a real case demands it. No registry UI/audit
page until a reader exists.

## Build plan (on NATE's go, after redline)

Wave F1: the fallbacks module (rung schema + walker + activation
recording) + the gate + router kwarg plumbing + the SWAN bathymetry
ladder as proving case (fixes the rectangle; live A/B: undeclared ->
typed error naming the gap; declared -> ETOPO rung, loud, coverage
reported). Wave F1b (ADR 0290): the adversarial panel's fixes - all
four exposed `fetch_topobathy` callers migrated onto declared rungs, the
merge reconciling its footprint PROMISE against what actually painted,
the walker surfacing the PRIMARY's error at REFUSE, no coverage claim
under an exemption, honest decline / skip_land / timeout semantics, and
the visibility seams (emit param, run-prefix sidecar). Wave F2: migrate
the audit's remaining data-bearing rows + `spec.fallback` + deprecate
the ad-hoc params, full-coverage law (inventory + verdicts + sweep
guard against naked substitution).

## As built (wave F1, ADR 0289)

- `trid3nt_server/fallbacks/` - `ladder.py` (`Rung` / `Ladder` /
  `REFUSE` / the registry) and `walker.py` (`walk_ladder`, `LadderGap`,
  `LadderRefused`, `Activation`). Capability-neutral: F2's mesh and
  worker rungs use the same walker.
- `trid3nt_server/gates/fallback.py` - the loudness floor
  (`gate_fires` / `labeled_default`) and the pause, on the existing
  `_PENDING_CONFIRMATIONS` spine with a `tool-payload-warning`.
- Rung definitions live with the capability owner; the bathymetry
  ladder is in `data/fetchers/_router/hooks/topobathy.py` as
  `BATHYMETRY_LADDER`.
- A rung's SHARE of the request comes from the seam: a seam that can
  measure its own coverage raises `LadderGap(covered_fraction,
  gap_note)` rather than filling the hole itself.
- Consequence classes are the spec's three degradation classes plus
  three structural ones (`primary`, `user_supplied`, `refuse`). The
  floor keys only on the degradation classes.
- TOP RUNG (NATE, 2026-08-19): a ladder may declare one
  `user_supplied` rung naming the request param that carries the
  user's own data. Present -> it serves and the walk stops; it stamps
  `SyntheticInput(basis="user")`. Not an upload feature.
- Activation rides `LayerURI.fallbacks` (a new additive
  `FallbackActivation` list) plus the existing `fallback_note`
  narration channel - no new envelope, no plugin change.
- REFUSE propagates the PRIMARY rung's own typed error verbatim (later
  rung failures chain via `__cause__`); the ladder re-wraps only when a
  recorded gap's filling rung failed for an unrelated reason, when a
  rung was DECLINED at the gate, or when the failure carries no
  `error_code` at all.

## As built (wave F1b, ADR 0290)

- ALL exposed `fetch_topobathy` callers declare a rung, not just SWAN:
  `sfincs/flood` (coastal), `geoclaw/inundation` (non-tsunami),
  `schism/tidal_hydro`. A gate that fires only for opt-in callers is not
  a floor. GeoClaw's and SCHISM's broad `except` no longer let a
  coverage gap degrade to the LAND-ONLY `fetch_dem` leg.
- PROMISE vs PAINT: a coverage promise read from tile FOOTPRINTS must be
  reconciled against what actually painted. `_composite_sources_to_array`
  returns one painted flag per input source, positionally, and the merge
  raises the same typed gap when the promise and the paint diverge.
- A ladder declares `coverage_exempt_params`: request params that skip
  the capability's own coverage check. Under one, the walk stamps NO
  activation -- a coverage claim nobody measured is a false row.
  Loudness moves to the capability's own labeled warning.
- A DECLINE gets its own typed refusal text and a recorded, visible
  activation row (`RungRecord.declined`, kept at coverage 0.0). Labeled
  defaults apply only where there is nobody to ask; on a live user_gated
  session an unanswered gate is a decline.
- VISIBILITY: `emit_layer_uri(layer, fallbacks=...)` /
  `stamp_fallbacks` re-stamp a layer rebuilt from a bare uri; `route()`
  defers the emit-on-fetch surfacing until after the activation is
  stamped; `persist_run_activations` writes
  `s3://<runs>/<run_id>/fallback_activations.json` so a solved run is
  auditable from the bucket.

## As built (wave F1c, ADR 0291)

- COVERAGE IS MEASURED, NOT PROMISED. A result may report `rung_coverage`
  (rung name -> the fraction of the request that rung's source PAINTED); the
  walker reconciles its own shares against it. Without that seam a serving
  rung's coverage is only `1.0 - <what the previous rung promised>`, which is
  arithmetic, not evidence.
- EXEMPTION SEMANTICS. `coverage_exempt_params` is read from the CALLER's
  params only -- a param a RUNG injects is the ladder exercising its own
  declared alternative, and that attempt is accounted for like any other. Under
  a caller's exemption the activation stamps no number for WHICHEVER rung serves
  (not just the primary) and carries `Activation.unverified_note`, which rides
  `fallback_note` so the serve is visible without being a claim. The adapter
  hoists `fallback_warning` beside `fallback_note`, so the capability's own
  labeled warning reaches the model too.
- GATE MODE decides who is asked, never the presence of a channel: `user_gated`
  asks; `auto` / headless applies the labeled default immediately (synthetic ->
  refuse), matching the `input_review` sibling. A live session in auto is still
  auto.
- REFUSALS carry their own truth: the decline branch fires only when a gap was
  recorded (otherwise the primary's typed error, retryability intact, is what
  surfaces); an untyped failure under a rung wraps as `FALLBACK_LADDER_ERROR`,
  never the capability's coverage code, with `retryable` inherited.
- A capability may not hand back a substitute nobody verified: where NOTHING
  painted the uncovered part of the request, the fetch refuses even under an
  exempting param. An exemption buys a COARSER answer, never a fake one.
- CACHE. The fetch-time provenance sidecar carries `PROVENANCE_SCHEMA`; a cached
  object whose sidecar predates it is a MISS. Bump the constant when a
  provenance field becomes load-bearing for honesty -- otherwise a fixed AOI
  keeps serving the stale reading for the rest of its TTL bucket.

## As built (wave F1d, ADR 0292)

- A FAULT IS NOT A VERDICT. The capability's `refuse_error_code` is reserved for
  GENUINE coverage refusals: no rung permitted, a rung declined while the gap it
  would fill was OUTSTANDING, or a filling rung that gapped too. Everything else
  under a rung -- transport, cache, validation, a typed upstream fault -- wears
  `FALLBACK_LADDER_ERROR` with the failing rung's own `retryable`, and the message
  carries the gap context AND the cause. A recorded gap no longer converts a MinIO
  hiccup into "this AOI has no source".
- CALLERS DISPATCH ON `error_code`, NOT ON THE TYPE. `LadderGap` / `LadderRefused`
  are one type for two truths. `geoclaw` and `schism` re-raise a ladder FAULT
  unchanged (retryability intact) and keep their terminal typed refusal for a
  coverage VERDICT. `sfincs/flood` is different by contract -- its promise is a
  typed failed envelope, never a raise, and the envelope has no retryable field --
  so it separates the two by code and says the retryability in `error_detail`.
- EVERY LEG'S SHARE IS MEASURED, at FOOTPRINT granularity. The compositor returns
  each source's own georeferenced extent beside its painted flag; the merge derives
  disjoint shares (CUDEM tile footprints; regional minus CUDEM; ETOPO minus both).
  Their sum is the share of the request carrying a real bed, and EVERY "bed
  everywhere" claim keys on it -- a partial base refuses rather than stamping 1.0
  over holes, including under an exempting param and including on an AOI that never
  had a primary gap. Interior nodata inside an extent stays unmeasured (the
  documented open edge).
- A FINER LEG COUNTS TOO. The exempted-serve path keys on "something painted the
  hole", not on "ETOPO painted", so the NCEI regional fine DEM fills a partial-CUDEM
  AOI instead of being ignored while the fetch false-refuses. The merge gate and the
  pre-fetch gate now permit exactly the same requests.
- AN ENHANCEMENT LAYER MAY DEGRADE, NEVER SILENTLY. Every no-layer path of GeoClaw's
  nested fine shore topo returns the note that says why, logs it at WARNING, and the
  note rides the answer layer's `fallback_note`.
- THE LADDER ADMITS WHAT IT DOES NOT DECLARE. The measured map is validated: an
  unknown key and shares that do not sum to 1.0 are both said out loud. The
  `regional_fine` share is reported precisely because a model reading only the
  declared rungs would conclude the rest of its domain is unpainted -- the ladder
  schema has no slot for a non-degrading contributor, and F2 owns that.
- THE GATE COVERS WHAT THE WALK DESCENDS TO. A capability that lays one of its own
  alternatives down unasked gets its row stamped (the data served) and marked
  GATE-UNSEEN on the record and in the narration -- never implied approval.
- AN UNMEASURED SERVE CARRIES NO NUMBERS. Under a caller's exemption the stamping
  seam also clears the result's `rung_coverage`: an envelope may not contradict its
  own UNMEASURED note.
