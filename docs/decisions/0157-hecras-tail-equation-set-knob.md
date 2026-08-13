# ADR 0157 - HEC-RAS CAND-S tail: diffusion-wave equation-set knob + five triage STOPs

Date: 2026-08-05
Status: accepted

## Context

Six HEC-RAS CAND-S board rows were the FINAL easy-tier batch of the
module-coverage campaign:

1. `mixed_regime_multi_profile_solve` - 1D steady mixed-flow-regime hydraulic jump.
2. `storage_area_network_flow_reversal` - synthetic "Diamond River" 1D unsteady
   network flow-reversal + lateral-weir activation (App Guide Example 17).
3. `simple_2d_diffusion_wave_mesh` - uniform-cell diffusion-wave 2D with a
   normal-depth downstream BC, stable low-volume-error convergence.
4. `pump_station_trigger_and_ramp_control` - SA/2D interior-drainage pump: stage
   triggers + startup ramp.
5. `wq_module_smoke_test_suite` - HEC's bundled water-quality test datasets, run
   to completion.
6. `simple_breach_geometry_setup` - minimal SA/2D-connection breach bring-up.

Per the triage-first law, each S-tier claim was checked against the ACTUAL code
before any build. The HEC-RAS surface is: a 6.6 Linux solver image
(`trid3nt-local/hecras:latest`, engines `RasGeomPreprocess` / `RasUnsteady` /
`RasSteady`), the shipped-Muncie reparameterization path (`deck_edit.py`:
`scale_flow_hydrograph`, `set_breach_enabled`), the fresh-AOI pure-2D authoring
chain (`hecras2025` authoring image + `compose_pure2d_deck` +
`hecras_event_conditions`), and three registered templates (`hecras_riverine_flood`,
`hecras_levee_breach`, `hecras_flood_2d`).

Triage findings (the roster's "1D steady signed" claim was WRONG - a prior-board
error):

- **No steady solver is wired anywhere.** Every workflow runs `RasGeomPreprocess`
  + `RasUnsteady`; `RasSteady` is baked in the image but never invoked, and there
  is no `.fNN` steady-flow-profile writer nor a mixed-regime geometry. Row 1 =
  STOP.
- **No 1D network authoring.** The only geometry paths are shipped-Muncie
  reparameterization and fresh pure-2D mesh authoring; there is no multi-reach /
  junction / storage-area / lateral-weir authoring and no Diamond River fixture.
  Row 2 = STOP.
- **No pump-station machinery.** `compose_pure2d_deck` strips `Structures`
  entirely (`_STRIP_2D_COUPLING`); the Muncie deck has no pump. Row 4 = STOP.
- **No water-quality engine.** The image carries only the hydraulic engines; there
  is no `RasWQ`/water-quality binary and no bundled WQ test datasets in the repo.
  Row 5 = STOP.
- **No fresh breach-geometry authoring.** `set_breach_enabled` toggles the
  SHIPPED Muncie lateral-structure breaches; the composed fresh deck strips
  `Structures`, so a freshly-authored SA/2D-connection breach (crest / spillway /
  low-level outlet / progressive growth) does not exist. Row 6 = STOP. NOTE: the
  breach-RUNS-TO-COMPLETION bring-up INTENT is already served by
  `hecras_levee_breach` (breach_enabled=True) - a GREEN lateral-structure SA/2D
  breach smoke gate; only the fresh dam-structure authoring is the residual (shared
  with the two M-effort breach rows and the QUEUED Bald Eagle case, ADR 0125).
- **Row 3 is buildable.** The `hecras_flood_2d` authoring chain already solves a
  uniform-cell 2D mesh with a normal-depth downstream BC. Critically, the copied
  Muncie plan skeleton already carries `Plan Data/Plan Parameters/2D Equation Set =
  "Diffusion Wave"` - so `hecras_flood_2d` was ALREADY answering row 3's question
  (low-volume-error diffusion-wave convergence) by SILENT skeleton inheritance,
  never as an explicit, reviewed, auditable choice.

## Decision

**Row 3: LAND an explicit `equation_set` knob** on the `hecras_flood_2d` template
and the `compose_pure2d_deck` composer, converting the silently-inherited
diffusion-wave setting into a first-class, offline-tested, input-reviewed
capability. The engine reads the 2D solver from the plan HDF (ADR 0136), so the
knob is a pure host-side h5py attribute write - NO solver-image rebuild.

- `compose_pure2d_deck(..., equation_set="Diffusion Wave")` validates against
  `EQUATION_SETS = ("Diffusion Wave", "SWE-ELM", "SWE-EM")` and stamps the plan
  attr; the value rides the provenance dict.
- The template surfaces `equation_set` as `"diffusion_wave"` (default, VALIDATED)
  or `"full_swe"` (-> `"SWE-ELM"`), threaded through
  `flood2d_pipeline.author_and_compose` and added as an input-review `SyntheticInput`.
- Leverage: this is exactly the toggle the M-row `2d_diffusion_wave_vs_full_swe_regression`
  needs; it is now unblocked.

**Rows 1, 2, 4, 5, 6: honest STOPs** with the recipes above.

## Consequences

- No new registered tool (a new PARAM on an existing template) -> registry stays
  217, `EXPECTED_TEMPLATES` stays 59, no corpus/categories change. This is a FOLD.
- No image rebuild (both HEC-RAS images UNCHANGED: the plan-attr edit is host-side
  h5py; the authoring image and solver image are untouched).
- Live evidence (carve -> compose -> solve through the real 6.6 engine, Muncie
  NW-quadrant carve, 2068 cells, 2000 cfs peak):
  - `diffusion_wave`: 1906 wet cells, max depth 12.218 ft, WSE 946.935 ft, volume
    error 0.011207%, flux in/out 141176/141011 - reproduces the ADR 0138 acceptance
    EXACTLY.
  - `full_swe` (SWE-ELM): stamped correctly on the plan (offline-verified) and
    solves green, byte-identical to diffusion wave on this low-gradient reach.
- HONEST LIMITATION: because Diffusion Wave and SWE-ELM coincide to 6 digits on
  this gentle carve, the smoke confirms both RUN and are correctly stamped, but
  does NOT independently exhibit a solver DIFFERENCE (a transient
  `RasGeomPreprocess` MKL segfault on one fresh SWE compose was a flake - two clean
  re-solves succeeded). Demonstrating a DW-vs-SWE divergence needs an
  inertially-significant regime (steep/supercritical) - that is the M-row's job,
  which the knob now unblocks. `full_swe` is labeled advanced/less-tested on the
  template.
- Proof: `docs/proof/templates/hecras_flood_2d_equation_set_convergence.png`
  (rendered through the plugin chart dock's own `render_spec` interpreter).
- Tests: `test_hecras_deck2d.py::test_compose_stamps_equation_set_on_plan` +
  `::test_compose_rejects_unknown_equation_set`;
  `test_hecras_flood2d_template.py::test_equation_set_map_covers_choices`. Offline
  slice green (77 passed).

The HEC-RAS module-coverage board's remaining unbuilt rows all reduce to two
genuinely-new capabilities not on the current surface: (a) a 1D steady/unsteady
NETWORK deck author (rows 1, 2) and (b) STRUCTURE authoring on fresh decks - pumps
(row 4), fresh breaches (row 6), and their WQ (row 5) counterpart engine. These are
the next real build fronts, not S-tier knobs-on-existing.
