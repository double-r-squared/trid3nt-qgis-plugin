# Fallback ladders - the declared-degradation design

NATE-shaped design (2026-08-17 discussion), for redline before build.
Replaces hidden substitution everywhere with one declared, visible,
gated mechanism. Proving case: the SWAN bathymetry rectangle
(docs/design/fallback-audit.md, the mosaic land-fill exhibit).

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
