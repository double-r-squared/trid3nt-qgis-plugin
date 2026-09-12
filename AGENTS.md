# TRID3NT - agent charter

You are working in the TRID3NT repo: a QGIS plugin + local agent daemon
for AI-driven geospatial modeling - a framework whose extension points
(specs, contracts, seams) are how capability is added. Read this, then
docs/CONVENTIONS.md, then the docs/design/ page for any feature you
touch, BEFORE writing code. Inherit the structure; do not improvise.

## The map

- `trid3nt_server/` - the framework package, by feature:
  `tools/` (the atomic-tool registry; a tool is one of two kinds -
  `fetch` produces data from outside through a declared source spec
  the router executes, `derive` ingests data and outputs data - with
  `search`, `display` and `meta` beside them as infrastructure),
  `render/` (the FORMAT SET a product reaches the map in - a COG
  raster, a GeoJSON vector, an MDAL mesh with the dataset files beside
  it, a chart payload - the one emission seam, the publish mechanism,
  the presets and the restyle seam), `workflows/` (the declarative
  `runtime/`, the `mesh/` front, the typed `inputs/`, the `solver/`
  executor, and the engine package `telemac/`), `gates/` (GateSpec
  engine + cards + pending
  registries), `adapters/` (LLM providers - the ONLY place provider
  nouns appear), `server/` (session/ turn/ dispatch/ protocol/),
  `persistence.py`, `storage.py` (the object store's one client and the
  bucket names).
- `workers/` - the telemac solver worker plus the mesh and qgis
  legs. Worker code is INERT until its image is
  rebuilt: absolute -f/context paths, provenance-check the new code is
  IN the image, smoke through the image - never through mounted source.
- `contracts/` - typed wire + registry contracts, with their own suite
  in `contracts/tests` (the `test-packages` slice) and their committed
  JSON Schema mirror in `contracts/schemas` (regenerated, never
  hand-edited).
- `plugin/` - the QGIS dock (installs as `trid3nt`). `tests/` - the
  offline suite, mirroring the product tree; the six slices are its
  directories, named by the `make test-*` targets law 1 lists.
  `scripts/` - the entry points you type, plus the model checker.
  `dev/` - the dev tools, which git does not carry and the remote never
  sees: `lint/` (the prose guards), `instruments/` (measure + render
  the generated pages), `packet/` (the delivery renderers), `drivers/`
  (the live drive lane), `testing/` (the live-run harness and the
  canaries), `staging/` and `local/`. `docs/` - the manual, the directory
  maps, the specs, the model, the generated template pages and the
  ledgers. The repo carries the SYSTEM; the rulings record - what was
  decided and why - is kept outside it.

## The laws

1. Six-slice suite from repo root with `venvs/agent`, one target per
   subsystem, each its own foreground invocation:
   `make test-fetchers`, `make test-spatial`, `make test-engines`,
   `make test-server`, `make test-model-surface`, `make test-packages`
   (`make test` runs all six). Each expands to
   `env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest <dirs>
   -p no:cacheprovider --timeout=300 -q`, paths unquoted. The six slices
   are six invocations by convention, not by force: the root
   `[tool.pytest.ini_options]` sets `--import-mode=importlib` and no
   test directory carries an `__init__.py`, so the whole suite collects
   and runs as one. Baseline is EXACTLY ZERO failures in every slice;
   the directory map and the live per-slice counts are `tests/README.md`.
   Anything else: investigate - a flake claim requires an isolation
   rerun as proof.
2. Run gates FOREGROUND and wait for each summary line. Never
   background a gate and exit - your run dies with your process, and
   unverified work is not done work.
3. The live stack runs on THIS box: MinIO :9000, the daemon via
   `make agent`, local docker solvers. `set -a; source .env.local;
   set +a` for env. Server changes end with daemon restart +
   `dev/instruments/ws_smoke.py` (all_passed) + one declared canary
   through the product path,
   `python -m dev.testing.canaries <name>`, which exits
   non-zero unless the run's own products were read AND its delivery
   packet assembled. You run these yourself.
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
   import, catalog-surfacing pins, `EXPECTED_TEMPLATES` (the pinned set
   lives in `tests/search/test_door_dissolution.py`, and
   `tests/tools/test_template_hygiene.py` reads the same set), co-located
   corpus.yaml, retrieval top-8 check.
8. Emission belongs to the framework, not the workflow. Workflows hold
   orchestration and judgment; plumbing belongs to the framework.
   Inputs surface via the emit-on-fetch seam (`purpose=` on router
   fetches); results reach the map through `render`'s format set, and
   what a run published is written onto its own journal record as it is
   emitted - one registry, never two. Cadence is the deck-side
   `output_interval_min` lever and failure retracts nothing. Gates are
   DECLARED (GateSpec metadata + pure estimate/pin providers owned by
   the engine). Hand-wired emission or gating in a composer is a
   defect.

9. NEVER INVENT THE WORLD. No demo/synthetic physics baked into
   product code - a physics-consequential value with no real data
   source REFUSES (typed error naming the need); it never defaults.
   Proofs and demos run on real ingested data; if you cannot make the
   real thing work, STOP and say so LOUDLY - never hand over synthetic
   results for spot-checking as if they were the product. Synthetic is
   for isolated verification gates only, banner-labeled, never the
   deliverable.

## How to write code here

- EVERY KIND OF CODE HAS ONE HOME. The engine's module is the unit: a
  template is values over one module - keywords, DATA rows, an outputs
  list, captions - and nothing below the template layer names a
  template or a question. A piece that writes an engine input file is
  authoring; one that expands a value into keywords is a composite on
  the wrapper; one that reads a variable off a result is a primitive;
  one that fetches or resamples is a DATA row; one that validates or
  places a geometry is a method on Point, Extent or Shape; one that
  fails is a module-level error. `helpers/` holds ONLY a pure physics or
  numerics function none of those express - no fetch, no geometry, no
  file, no failure of its own. A tool is `fetch` or `derive`; a third
  kind invents a schism to justify.
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
- Docstring shape (docs/CONVENTIONS.md), guidance and not a gate:
  three content lines for a function, a method or a class, five for a
  module. LLM-facing tool docstrings are product material - rich,
  front-loaded, citation-free. Private helpers get NOTHING unless a
  non-obvious constraint exists - if one needs a paragraph, it needs a
  better name or a split.
- Names and structure carry meaning; documents carry knowledge
  (docs/design per feature, the model for structure and requirements,
  commit messages for provenance). ASCII hyphens only; no emojis.
- A decision is NATE's and lands as a dated entry in the rulings
  record. What the repo carries is the CONSTRAINT - as a requirement
  in the model, a line here or in docs/CONVENTIONS.md, or a comment at
  the line it governs - never the argument for it. If your change
  reshapes a feature, update its docs/design page in the same change.
