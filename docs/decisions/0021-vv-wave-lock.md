# 0021 - V&V wave lock: folds and the 9-tool scope

Date: 2026-07-24. Status: accepted.

## Context

The V&V primitive list (docs/validation/tool-list.md) went through group-by-group
review with NATE. Consolidation logic per group: fold where outputs unify and
dispatch is deterministic; split where argument schemas are disjoint.

## Decision

- A folds to ONE read_run_diagnostics(run) dispatcher. Engine identity comes
  from the run handle; the 5 per-engine readers become internal parser modules
  (same pattern as run_solver's per-engine specs). One normalized envelope.
- B folds to 2: compute_skill_metrics (variable="head" preset adds SRMS,
  absorbing the head-stats tool) + compute_flood_extent_skill. Input shapes
  (paired series vs raster pair) justify the remaining split.
- C stays 3: one pairing processor + two observation fetchers with disjoint
  sources, shapes, and caveat semantics. No fold.
- D stays 3: per-engine knob schemas are disjoint (a merged tool = union schema
  with mostly-invalid args); result envelopes DO unify (child handle +
  change audit + plausibility). Copy-on-write lineage, physical bounds enforced.
- E (optimizer drivers) stays FROZEN - embodies loops.
- F (webinar checks) goes BACK-BURNER (NATE 2026-07-24). Fold design recorded
  for the thaw: check_run_plausibility + check_model_setup suite tools.

Wave = 9 registered tools; registry 191 -> 200.

## Consequence

Corpus queries staged as a patch file (docs/validation/corpus-additions.yaml)
until the corpus WIP lands - the retrieval acceptance check is deferred to
that landing. Verification-practice research (split-sample validation,
signature evaluation, metamorphic checks, proxy-basin) maps to PROTOCOLS
composed from these primitives or to back-burner tool candidates - not wave
scope. Supersedes nothing.
