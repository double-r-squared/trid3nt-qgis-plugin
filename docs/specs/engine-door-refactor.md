# Engine-door refactor - wave spec (MODFLOW first)

Status: FOR NATE REVIEW - nothing executes until the go.
Date: 2026-07-26. Grounds: ADR 0024/0025, engine-coverage-inventory.md,
the 2026-07-26 design discussion.

## Terminology (locked)

- DOOR: the one registered tool per engine (run_modflow, run_swmm, ...).
  Retrieval-gated, engine-fidelity character + limitations in its docstring.
- TEMPLATE: a verified deck/workflow recipe behind a door, selected by
  template= argument. Carries: deck recipe, knob manifest, postprocess,
  co-located corpus entries.
- HOT-PATH ALIAS: DROPPED (NATE 2026-07-26, supersedes the alias layer of
  ADR 0025): if a template achieves the same thing, an alias tool is noise -
  one extra nesting layer (door -> template) is acceptable. Hazard-common
  phrasings ("flood over this AOI") route to the DOOR via its co-located
  corpus tier; canonical-query tests re-baseline to expect doors.
  Executed per engine slice (run_model_flood_scenario falls in the SFINCS
  slice, not this one).
- KNOBS: instantiation knobs (call-time args incl. overrides) + deck knobs
  (hardcoded in files, turned by setters). One manifest, two entry points.

## Rules (locked in discussion)

1. FOLD CRITERION: fold by functional sameness, never code-delta size.
   Distinct functionality = separate template even on identical scaffolding.
2. NAMING: folders imply engine (workflows/modflow/capture_zone.py - no
   prefix stutter); registered door names carry the engine (run_modflow).
   Template names say what question they answer - no ambiguous "_job" names.
3. CORPUS CO-LOCATION: every tool/door/template keeps its corpus.yaml beside
   it; a loader composes tiers at startup: general tier + door tier compete
   in per-turn retrieval; TEMPLATE tier engages only at template selection
   inside a door (template growth never dilutes the main gate). DuckDB
   spatial-functions file stays as-is. Redundancy audit: entries duplicating
   search_spatial_functions get retired.
4. Templates FAIL HONESTLY (typed limitation + escape-hatch pointer).
5. Knob ledger + rebuild replay, cost-class field: designed (IDEAS.md),
   implemented with the manifest retrofit.
6. Meta-template scaffolding: BACKLOGGED (IDEAS.md), not this wave.

## Slice 1: MODFLOW (this wave)

1. DOOR: run_modflow grows from run_modflow_archetype_tool.py (it already
   takes archetype= - the embryo). Signature: template=, template knobs,
   overrides= (manifest-validated), compute class.
2. FOLD: run_modflow_job + run_modflow_multi_species_job -> ONE template
   contaminant_plume with knob species=[{name, release_rate}, ...] (min 1).
   - Fix the seam: build_and_stage_modflow_deck forwards species.
   - Envelope unification: postprocess always returns plumes[] (length 1 for
     single) - MIGRATION: all single-plume consumers accept list-of-1;
     verified by test + live template run.
3. MIGRATE the scenario family to templates with honest names (separate per
   the fold criterion - each answers a distinct question): capture_zone,
   mine_dewatering, saltwater_intrusion, managed_recharge, asr,
   sustainable_yield, wellhead_protection, wetland_hydroperiod,
   regional_water_budget, river_seepage, contaminant_plume.
   DECIDED (NATE 2026-07-26): contamination_affected_fields is CUT as a
   tool - the plume half is the contaminant_plume template; the zonal
   field-analysis half re-homes to playground/analysis composition per the
   analysis-is-playground norm.
4. REPO HIERARCHY (NATE-pinned 2026-07-26): folder-per-template, file named
   after its folder, corpus co-located at every level:
     workflows/<engine>/<template_name>/<template_name>.py + corpus.yaml
       (+ knob_manifest.yaml at retrofit)
     tools/<category>/.../<tool_name>/<tool_name>.py + corpus.yaml
   tools/simulation/modflow/ holds door + setter, same folder-per-tool rule.
   PELICUN is promoted to a full engine (door + workflows/pelicun/ template
   entries) - its tools leave the tools/ section; it is simulation-class.
   CALIBRATION (NATE 2026-07-26): workflows/calibration/ as a PEER of the
   engine folders - the engine-agnostic calibration/V&V workflow machinery
   (pairing/skill orchestration, future optimizer drivers when group E
   thaws) lives adjacent to, not inside, any engine.
5. Registry effect: ~14 registered entries -> 1 door (+ aliases if any
   flood-class hot path applies; none expected for MODFLOW). Hot-path
   protection: canonical-query regression tests must PASS after the corpus
   tiering (the 2 currently-failing ranking tests re-baselined with NATE's
   corpus WIP merged first - HARD PREREQUISITE).

## Acceptance

- Registry membership test updated; retrieval canonical-query suite green.
- Offline suites green (envelope migration covered by tests).
- One live template run per migrated template class (at minimum:
  contaminant_plume single + multi, capture_zone) - status ok + honest
  envelopes + diagnostics readable.
- Flood canary (standing rule - registry/corpus seams touched).
- NATE visual pass stays the rendering acceptance.

## Execution sequence (NATE-pinned 2026-07-26)

1. Basic dir restructure everywhere (mechanical, registry byte-identical -
   the 160-move precedent).
2. MODFLOW as the full pilot (door + fold + renames + tiered loader).
3. Roll the pattern through the remaining engines (incl. pelicun promotion).
4. Cull redundant tools in the sweep.

## Explicitly deferred

EVERYTHING not this restructure parks unless load-bearing (NATE 2026-07-26):
HEC-RAS spike, Malpasset fix batch, replication pick, Delft3D, SWMM
deepening (rides step 3's SWMM slice), manifest retrofit beyond what the
restructure needs. Revisit after the restructure lands. Delft3D integrates as a template library from day
one when its infra lands. Meta-template scaffolding, knob-manifest search,
describe_model: IDEAS.md.

## Prerequisites before execution

1. NATE's tool_query_corpus.yaml WIP lands (the corpus explosion detonates
   that file).
2. NATE reviews this spec and calls the go.

## Merge plan (NATE 2026-07-26)

All wave work commits on branch refactor/engine-doors; local master stays the
pre-wave fallback (rollback = checkout master + daemon restart). Merge to
master + single remote push at the milestone: MODFLOW pilot done, gates
green, canary passed, NATE visual pass - push on NATE's confirm.
