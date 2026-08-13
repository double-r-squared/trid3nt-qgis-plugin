# ADR 0153 - MODFLOW CAND-S rows: one package-validation template + a PRT STOP

Date: 2026-08-05
Status: accepted

## Context

Four MODFLOW CAND-S board rows were queued, each exercising a DISTINCT MF6
package that no existing archetype composer exposes: GWF-NPF Newton dry/rewet
(Zaidel), GWF-MAW cross-aquifer (Sokol), GWF-HFB barrier grid-independence, and
PRT forward vs MODPATH 7. All four are S-tier hypotheses under the triage-first
law.

Triage against the installed engine (flopy 3.10.0 + local `mf6` 6.7.0 at
`bin/mf6`; the modflow container is SHA-pinned mf6 6.5.0 but its GCS entrypoint
is retired - MODFLOW runs via the local-exec supervisor on the host) established,
per row, what actually solves before any build:

1. The existing MODFLOW surface is 11 place-based archetype scenario composers
   (capture_zone, contaminant_plume, sustainable_yield, ...). NPF-Newton is
   already USED internally (wetland/stream_depletion) and PRT is used BACKWARD
   (capture_zone), but NONE of MAW, HFB, or a Newton/MAW/HFB V&V benchmark is
   reachable. Folds onto the scenario composers do not serve these rows - the
   rows are solver/package V&V benchmarks (computed-vs-analytical), not
   place-based products.
2. The cited modflow6-examples decks ship locally under
   `third_party/mf6.5.0_linux/.../examples` (ex-gwf-zaidel, ex-gwf-maw-p01a,
   ex-prt-mp7-p01). The zaidel + maw decks solve verbatim on mf6 6.7.0; the
   bundled PRT deck fails only on an OC-format drift (6.5.0-authored), so PRT
   decks must be flopy-3.10-authored.
3. MODPATH 7: NO `mp7` binary exists anywhere in the image or local env (only
   mp7 example INPUT files ship). `which mp7` = none.

The precedent set by ADR 0151 (SWMM mechanism-comparison templates) and the
pelicun / SWAN CAND-S validation templates is: synthetic COMPARISON / VALIDATION
questions that cannot fold as a knob onto a single-run place-based template mint
a small typed template that emits a chart + typed scalars (a NOT-a-LayerURI
carrier), not a georeferenced map. These four rows are that shape.

## Decision

Land ONE new registered template, `modflow_package_validation` (registry
215 -> 216; EXPECTED_TEMPLATES 57 -> 58), with a `case` enum covering three of
the four rows; the fourth (PRT) is an honest STOP.

- Engine core `agent/mesh/modflow_package_validation.py` authors small SYNTHETIC
  benchmark decks via flopy, resolves `mf6` (`$TRID3NT_MF6_BIN` -> PATH ->
  repo `bin/mf6`), solves each, and extracts the computed-vs-reference quantity
  + a Vega-Lite chart spec.
- Composer `workflows/modflow/package_validation/package_validation.py` solves
  the case off the event loop (`asyncio.to_thread`), emits the chart, and
  returns the typed `ModflowValidationResult` (new contract in
  `modflow_contracts.py`: case / package / computed vs reference / delta /
  relative_error / validated / per-case metrics / loud synthetic-benchmark note;
  `schematic_only=True`, `basis="synthetic"`, `SyntheticInput` provenance).

Per-row disposition:

LANDED (cases of the one template):
- `unconfined_newton_dry_rewet_channel` -> case `newton_dry_rewet` (GWF-NPF
  Newton). Zaidel 200x1x1 staircase channel (top 25, botm 20/15/10/5/0, CHD
  23->10, k=1e-4, NEWTON). The notebook publishes no analytical array, so the
  case is a Newton-vs-standard ROBUSTNESS contrast: Newton keeps all 200 cells
  wet in a monotone staircase (0 dry, 10..23 m); the standard formulation
  collapses 62 cells to dry (nonphysical).
- `maw_crossaquifer_nonpumping_analytical` -> case `maw_crossaquifer` (GWF-MAW).
  A non-pumping MAW casing connects two confined aquifers (T=92.9 / 371.6 m2/d,
  near-zero kv). FREE V&V: computed MAW head 7.92800 m vs the Sokol (1963)
  transmissivity-weighted analytical 7.92800 m, delta 2.0e-11 m.
- `hfb_barrier_wall_containment_knob` -> case `hfb_barrier` (GWF-HFB). A
  HYDCHR=1e-6 barrier over a 1000 m domain (CHD 10->1) solved at 10/20/40/80
  columns. Flux 8.9991e-4 m3/d matches the HYDCHR analytical 9.0e-4 (delta
  8.9e-8) AND varies < 8.7e-6 across grids = grid-refinement independent. No
  published worked example exists (board note confirmed) - the reference is the
  MF6 gwf-hfb docs conductance formula, honestly docs-cited not notebook-cited.

STOP (recipe on the board row):
- `prt_forward_structured_steady_vs_modpath7`: the row's load-bearing ask is an
  EXACT PRT-vs-MODPATH7 cross-tool match. No `mp7` binary exists (image or env)
  AND the ex-prt-mp7-p01 notebook publishes no numeric reference values, so both
  the cross-tool match and the "PRT-only vs published reference" fallback are
  unavailable. Native PRT forward tracking itself works (verified). Recipe:
  install USGS MODPATH 7.2.001 + SHA-pin, author the p01 GWF once, run mf6-PRT
  and MODPATH7 off the same GWF output, exact-match per-particle termination +
  travel time, register as a 4th case `prt_forward_vs_modpath7`.

## Drawn-structures connection (HFB)

The `hfb_barrier` case proves the HFB knob mechanism (a CELLID1/CELLID2 pair +
HYDCHR reduces cross-wall flux to a grid-independent target). It PAIRS WITH the
drawn-structures direction: the plugin draw-a-cutoff-wall affordance that would
let a user place a slurry wall / grout curtain and have it rasterized to the
CELLID pairs feeding an HFB block is the natural follow-on. That drawing
affordance is NOT built here (flagged for the drawn-structures wave).

## Consequences

- MODFLOW gains a package-V&V surface exercising GWF-NPF (Newton), GWF-MAW, and
  GWF-HFB - three packages no place-based composer reached. The agent can answer
  "does MODFLOW's Newton formulation handle drying/rewetting", "does the MAW
  package match the Sokol analytical level", "is the HFB barrier grid
  independent" against a known answer.
- WORKER-IMAGE LAW (ADR 0148): NOT triggered. The template runs `mf6` directly
  via flopy on the agent host (the same local-exec path the live MODFLOW runs
  use); it does NOT touch the container's COPY set (services/workers/modflow,
  _modflow_build, _modflow_postprocess) nor the run_modflow supervisor. No image
  rebuild was needed to land or verify it.
- Zero deletions (nothing is superseded). No entries added to the deletion
  ledger.

## Evidence

- Offline slice (from repo root, `env -u TRID3NT_CACHE_BUCKET pytest`, with
  `$TRID3NT_MF6_BIN=bin/mf6` so the gated V&V solves run):
  test_modflow_package_validation + test_categories + test_template_hygiene +
  test_catalog_surfacing (registry 216) + test_door_dissolution (58 templates)
  = 44 passed. Regression: test_modflow_archetypes + test_modflow_contaminant_plume
  + test_run_modflow + test_river_seepage + services/.../test_gwt_adapter =
  183 passed, 13 skipped (pre-existing env-gated real-run skips).
- Model-free retrieval gate: `retrieve_visible_tools(prompt, None, 8)` surfaces
  `modflow_package_validation` for all three case phrasings (Newton staircase /
  MAW Sokol / HFB grid-independence).
- Live V&V (local mf6 6.7.0): newton_dry_rewet validated (Newton 0 dry / standard
  62 dry, monotone 10..23 m); maw_crossaquifer validated (delta 2.0e-11 m);
  hfb_barrier validated (flux 8.9991e-4 vs 9.0e-4, grid variation 8.7e-6).
- Proof charts (through the plugin chart dock's own `render_spec`, 6.0x2.2in,
  savefig dpi 200): docs/proof/templates/modflow_package_validation_newton_dry_rewet.png,
  _maw_crossaquifer.png, _hfb_barrier.png. Each overlays computed-vs-reference
  in one figure with the delta in the caption strip. No spatial layer is emitted
  (schematic decks), so there is no map proof.

## Registry / pins

- TOOL_REGISTRY 215 -> 216; EXPECTED_TEMPLATES 57 -> 58; categories.py +1
  (`modflow_package_validation`: hazard_modeling). CODED tools this landing: +1.
