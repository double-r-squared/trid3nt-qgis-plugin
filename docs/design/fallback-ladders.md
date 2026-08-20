# Fallback ladders - the declared-degradation design

NATE-shaped design (2026-08-17 discussion), approved as written.
Replaces hidden substitution everywhere with one declared, visible,
gated mechanism. Proving case: the SWAN bathymetry rectangle
(docs/design/fallback-audit.md, the mosaic land-fill exhibit).

Wave F1 is LANDED (ADR 0289) - read "As built" at the bottom for the
shipped shapes before touching the machinery.

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
reported). Wave F2: migrate the audit's data-bearing rows + deprecate
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
- REFUSE propagates the capability's own typed error verbatim; the
  ladder only re-wraps when a recorded gap's filling rung failed for an
  unrelated reason.
