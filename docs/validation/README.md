# Validation records

Every file here is a MEASUREMENT: a census, a conformance walk, an evaluation or
a ledger, dated and taken against the tree as it stood. A file in this folder is
not design space and not a proposal - when the thing it measured dies, the file
dies with it, and a later change that falsifies its wording does not rewrite the
finding: the correction goes in the ledger row, or in a one-line dated note at
the top pointing at it.

The validation and calibration DESIGN space that used to live here as six
"STATUS: THINKING" notes is gone: the responsibility cut, the activation
boundary, the review-context rule and the metric background folded into
`docs/design/calibration-methodology.md` (appendix A), and the proposed build
order and the open-questions list were superseded by the rulings in
`docs/IDEAS.md`.

## Censuses and evaluations - what a wave read before it moved anything

| file | what it measured |
|---|---|
| `docs-census.md` | every file under `docs/`, classified, with the delete list this wave executed |
| `scripts-eval.md` | every file under `scripts/`: its consumers, whether it still runs, its fate |
| `tests-eval.md` | every test file mapped to its subject, and the six-slice proposal |
| `scope-census.md` | the extensions ruled out of scope and into the attic |
| `fetcher-fold-census.md` | the two-lens census by protocol family, 97 specs |
| `lean-sweep-inventory.md` | the sweep's D-rows, L-rows and open questions |
| `hygiene-manifest/` | this wave's per-directory lenses - one row per file, written by the agent that read it, plus the coverage audit over them |

## Conformance walks - a spec read clause by clause against the tree

| file | the spec it walks |
|---|---|
| `mesh-recipe-conformance.md` | the mesh recipe, rev 2 |
| `mesh-wave-conformance.md` | the workflow blueprint, at the mesh wave's close |
| `module-surface-conformance.md` | the module surface, with its unmet LOC promise stated |
| `worker-unification-conformance.md` | the worker-unification port |
| `emission-fold-presets-conformance.md`, `emission-fold-store-conformance.md` | the emission fold's two halves |
| `fetcher-fold-conformance.md` | the fetcher fold |
| `worker-unification-proof-interrogation.md` | the adversarial pre-delivery read of that wave's proofs |

## Stage records and measurements

| file | what it holds |
|---|---|
| `fetcher-fold-stage0.md` | THE TRADE, per library, measured before any spec moved |
| `fetcher-fold-raster-half.md`, `fetcher-fold-hydro-stage.md` | the STAC and HyRiver stages |
| `nlcd-manning-tables.md` | our NLCD to Manning table beside pygeohydro's, with each one's published source. A DESIGN-STOP: swapping tables changes run numbers, so nothing is switched |
| `section-vs-hyriver.md` | the measurement that refuted folding the section cut onto `pynhd` |
| `module-surface-loc.md`, `skeleton-loc-ledger.md`, `worker-loc-ledger.md` | LOC measured at a named ref, before and after |
| `module-coverage-board.md` | the engine coverage board, kept as the measured history of runs that happened |
| `external-fetch-audit.md`, `fallback-audit.md`, `demo-physics-defaults-audit.md` | three dated audits, moved here from `docs/design/` because a dated measurement is not a standing design |
| `afk-ledger-2026-08-24.md` | one closed AFK design-decision ledger, moved out of `docs/decisions/` because it is not a decision record |
| `docstring-exemptions.md` | the regenerated `# docstring-exempt:` ledger, written by the guard test |
| `corpus-additions.yaml` | retrieval phrasings staged for addition to a registered tool's own corpus |
| `code-graph/` | the code atlas the instrument writes: its summary, orphan and dead-symbol reports. `graph.json` is machine-only and untracked |
