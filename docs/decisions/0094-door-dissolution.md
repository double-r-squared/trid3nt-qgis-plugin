# ADR 0094 -- Engine-door dissolution: templates rejoin the tool-search surface

Status: accepted (2026-08-03)
Supersedes: ADR 0034 (engine-door template registration). Follows ADR 0093
(the doors were just docstring-trimmed) and the template input-provenance audit
(docs/validation/template-input-provenance-audit.md -- its per-template
understanding informed the new docstring fidelity lines; its INPUT_REQUIRED gate
proposal is NOT implemented in this wave). NATE-approved 2026-08-03 (FULL dissolve).

## Context

ADR 0034 introduced the "engine door": each simulation engine (SFINCS, SWMM,
MODFLOW, TELEMAC, SWAN, GeoClaw, Landlab, OpenQuake, ELMFIRE, Pelicun) shipped a
single `run_<engine>` concierge tool (tier=door). The door was the only
discoverable entry; its `tier=template` members were EXCLUDED from the default
retrieval pool and became callable only after the door ran (a per-turn
"gate expansion" side effect). The premise was that surfacing ~20 near-duplicate
engine variants would flood tool selection.

That premise no longer holds. Retrieval (BM25 + local-dense + name-substring RRF
with lexical reinforcement, ADR 0017/0018) plus per-tool corpus.yaml routing
phrasings resolve the right engine template directly. The door added a mandatory
extra hop (call the door, read the concierge envelope, then select-then-call),
duplicated routing doctrine between the system prompt and the door docstrings
(ADR 0093 characterized the envelope-enumeration half of that), and required
bespoke machinery (a tier=door retrieval class, a `_DOOR_EXPAND_CAP` gate
expander, a pool-exclusion filter mirrored across three seams).

## Decision

Dissolve the engine-door pattern entirely.

1. RETRIEVAL INCLUSION. `tier=template` tools join the default retrieval pool.
   The index-build exclusion (search_tools), the fail-open dump exclusion
   (tool_retrieval), and the default-declarable exclusion (server) drop
   `template` -- only `internal`/`catalog` stay withheld. The corpus composer now
   walks `workflows/**/corpus.yaml` (the templates' co-located phrasings) in
   addition to `tools/**/corpus.yaml`.

2. FIDELITY DOCTRINE RELOCATION. Each door's `_FIDELITY_BRIEF` +
   `_MISMATCH_REDIRECT` moved onto the template it fronted: every template
   docstring front-loads a `Fidelity: ... Off-scope: ...` paragraph (fidelity
   class + off-engine redirects, now pointing at TEMPLATES, not doors). The
   cross-engine fidelity ladder statement (screening/planning-grade vs
   refinement-grade; SFINCS is fast reduced-physics screening, never
   refinement-grade; refinement-grade flood work belongs to TELEMAC-2D / HEC-RAS
   on a documented case) went ONCE into the system-prompt flood-routing block
   (test-pinned; the honesty rules were tightened, never weakened).

3. DELETION. The 10 `run_<engine>` door modules, their corpus.yaml, their 10
   test files (9 `test_*_door.py` + `test_engine_door_gating.py`), and the
   gate-expansion machinery (`_engine_door_tool_names`, `_DOOR_EXPAND_CAP`, the
   door branch of the discovery-expand block, the `templates`-key result reader)
   are removed. Every door reference in the system prompt / catalog / categories
   is rewritten to name the template directly. Templates are callable DIRECTLY,
   no gate. HOT_SET swapped `run_sfincs` -> `sfincs_flood` (the always-on flood
   entry). The dead `shared/model_satellite_fire_animation/` dir was removed as a
   rider.

## Consequence

- Registry: 186 -> 176 (the 10 doors die; the 20 templates were already
  registered). Coded tools: 96 -> 86 (doors were coded tools; coded fetchers
  unchanged at 9; spec-served data tools unchanged at 90).
- Templates are now categorized (PRIMARY_CATEGORY + the pelicun/swan/elmfire
  secondary cross-lists moved from the door onto the template); `tier=template`
  is no longer excluded from the category-membership invariant.
- RETRIEVAL, model-free `retrieve_visible_tools(q, None, 8)`: all 20 templates
  surface top-8 for at least one natural corpus query (20/20). Pre-existing
  non-template surface is rank-stable (own-corpus top-8 misses moved 15 -> 14
  under an identical with/without-templates method; zero non-template tool
  regressed). Selection safety on ambiguous asks is sane: urban flood ->
  swmm_urban_flood #1, coastal surge -> sfincs_flood #1, seismic -> openquake_psha
  #1, wildfire -> elmfire_fire_spread #1; a bare "model the flood" surfaces the
  full flood template family (sfincs_flood + swmm_urban_flood + geoclaw_inundation
  + the NWS composer) with the system-prompt ladder + docstring fidelity lines
  doing the arbitration the door used to.
- A residual `tier=door` retrieval-reinforcement constant
  (`_LEX_REINFORCE_GATE_DOOR`) is left inert (no tool carries `tier=door`); it is
  a ranking knob, not the concierge pattern, and removing it is a separate
  retrieval-tuning change. The now-unconsumed `TEMPLATE_CARD` exports +
  `workflows/<engine>/_template_card.py` (the door read them) are QUEUED in the
  deletion ledger, not deleted this wave (they are valid, inert dataclasses; the
  churn belongs to a hygiene batch).

- LIVE EVIDENCE. Daemon boots clean (registry 180 incl the 4 startup-only tools,
  0 doors, all declarations build). A natural coastal-hurricane flood prompt
  offers sfincs_flood directly (visible + declarable, in HOT_SET, named in the
  system prompt), with NO door in the visible set. Flood canary GREEN: a
  direct-call sfincs_flood (Boulder CO AOI, 10-yr / 6-hr design storm) ran the
  full SFINCS solve and published flood_depth_peak.tif + the animation-frame
  series + a tiled overview COG to s3://trid3nt-runs (no SOLVER_FAILED) -- the
  flood template's docstring + selection-path edits did not regress the solve.

The as-we-go rule: a NEW engine ships its template(s) as ordinary
`tier=template` retrieval-pool tools with a front-loaded fidelity+off-scope
docstring and a corpus.yaml -- no door.
