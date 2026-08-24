# TRID3NT - agent charter

You are working in the TRID3NT repo: a QGIS plugin + local agent daemon
for AI-driven geospatial modeling - a framework whose extension points
(specs, contracts, seams) are how capability is added. Read this, then
docs/CONVENTIONS.md, then the docs/design/ page for any feature you
touch, BEFORE writing code. Inherit the structure; do not improvise.

## The map

- `trid3nt_server/` - the framework package, by feature:
  `tools/` (the atomic-tool registry: fetcher specs + router +
  emit-on-fetch, processing, search, publish_layer), `data/`
  (category-era fossil - only the per-engine simulation shims remain),
  `mesh/` (shared mesh layer), `workflows/` (engine composers, plus
  `lib/` the declarative library and `solver/` the solve seam),
  `gates/` (GateSpec
  engine + cards + pending registries), `adapters/` (LLM providers -
  the ONLY place provider nouns appear), `server/` (session/ turn/
  dispatch/ protocol/), `emission/` (layer publication + the
  emit-on-solve seam), `persistence/`.
- `workers/` - the solvers: one containerized worker per engine +
  mesh/qgis/postprocess legs. Worker code is INERT until its image is
  rebuilt: absolute -f/context paths, provenance-check the new code is
  IN the image, smoke through the image - never through mounted source.
- `contracts/` - typed wire + registry contracts. `plugin/` - the QGIS
  dock (installs as `trid3nt`). `tests/` - the offline suite.
  `scripts/` - drivers, smokes, image builds. `docs/` - decisions
  (ADRs), design (feature guides), validation, proof/templates (NEVER
  delete anything there).

## The laws

1. Four-slice suite from repo root with `venvs/agent`:
   `env -u TRID3NT_CACHE_BUCKET python -m pytest tests/test_[a-e]*.py
   -p no:cacheprovider --timeout=300 -q` (then `[f-o]`, `[p-r]`,
   `[s-z]`). Baseline failures are EXACTLY 4 fetch_resolution in
   [f-o] + 0 in [p-r]. Anything else: investigate - a flake claim
   requires an isolation rerun as proof.
2. Run gates FOREGROUND and wait for each summary line. Never
   background a gate and exit - your run dies with your process, and
   unverified work is not done work.
3. The live stack runs on THIS box: MinIO :9000, the daemon via
   `make agent`, local docker solvers. `set -a; source .env.local;
   set +a` for env. Server changes end with daemon restart +
   `scripts/ws_smoke.py` (all_passed) + the flood canary
   `scripts/run_sfincs_direct.py` (status=ok). You run these yourself.
4. Behavior-preserving refactors move code verbatim; every reference
   site (tests, monkeypatch paths, source-inspection anchors,
   parents[N] depths) moves WITH it - grep the old path to zero, clear
   bytecode after tree moves.
5. Delete, don't disable. Deletions register in
   docs/DELETION_LEDGER.md with trace evidence. Before deleting or
   building around anything, verify the kickoff's load-bearing claims
   against the code and the ledger - kickoffs can carry stale
   premises, and a correct stop beats a wrong execution.
6. Non-trivial design decisions are NATE's: surface the fork with
   options and a recommendation; do not pick silently. If blocked or
   out of budget, stop CLEAN - tree revertible, report exact state -
   never half-wired.
7. New tools/templates complete the registry checklist: registration
   import, catalog-surfacing pins, EXPECTED_TEMPLATES, co-located
   corpus.yaml, retrieval top-8 check.
8. Emission belongs to the framework, not the workflow. Workflows hold
   orchestration and judgment; plumbing belongs to the framework.
   Inputs surface via the emit-on-fetch seam (`purpose=` on router
   fetches); results via the emit-on-solve seam (the solver leg writes
   `outputs.json`; the seam publishes every entry - never omit frames,
   cadence is the deck-side `output_interval_min` lever, failure
   retracts nothing). Gates are DECLARED (GateSpec metadata + pure
   estimate/pin providers owned by the engine). Hand-wired emission or
   gating in a composer is a defect.

9. NEVER INVENT THE WORLD. No demo/synthetic physics baked into
   product code - a physics-consequential value with no real data
   source REFUSES (typed error naming the need); it never defaults.
   Proofs and demos run on real ingested data; if you cannot make the
   real thing work, STOP and say so LOUDLY - never hand over synthetic
   results for spot-checking as if they were the product. Synthetic is
   for isolated verification gates only, banner-labeled, never the
   deliverable.

## How to write code here

- SIMPLICITY: prefer the boring solution. Reuse an existing seam,
  convention, or pattern before inventing anything. Before adding a
  flag, mode, field, or knob: name who reads it - no reader, no
  feature. Do not add abstraction, generality, or configuration for
  futures nobody has asked for. Simplicity is never an excuse to skip
  correctness, gates, or the honest error path.
- Comments state constraints the code cannot express - an invariant, a
  gotcha, an external-system behavior. Present tense, about THIS code.
  Never: history, ADR/spec citations, attributions, milestones, dead
  systems, or claims about other code you have not verified. If a
  comment smells, suspect the code under it.
- Docstring budget (docs/CONVENTIONS.md): LLM-facing tool docstrings
  are product material - rich, front-loaded, citation-free. Public
  seams get 1-3 lines of promise. Private helpers get NOTHING unless a
  non-obvious constraint exists - if one needs a paragraph, it needs a
  better name or a split.
- Names and structure carry meaning; documents carry knowledge
  (docs/design per feature, ADRs for why, commit messages for
  provenance). ASCII hyphens only; no emojis.
- Decisions get an ADR-lite note in docs/decisions/. If your change
  reshapes a feature, update its docs/design page in the same change.
