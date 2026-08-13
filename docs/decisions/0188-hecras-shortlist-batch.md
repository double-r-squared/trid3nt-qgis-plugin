# ADR 0188 - HEC-RAS shortlist batch 4: the DW-vs-SWE inertial regression + the 2D stability-diagnostic sweep, on the fresh-AOI flood_2d surface

Date: 2026-08-08
Status: accepted

## Context

Two HEC-RAS "ready NOW" rows from the M/L sign-off shortlist
(`docs/validation/ml-signoff-shortlist.md`), both riding the fresh-AOI
`hecras_flood_2d` authoring chain (ADR 0140) with the `equation_set` knob landed by
ADR 0157:

1. `2d_diffusion_wave_vs_full_swe_regression` - how much do Diffusion Wave and full
   SWE differ on the SAME breach/steep 2D deck? ADR 0157 found them byte-identical on
   the low-gradient Muncie carve and flagged that a real difference needs an INERTIAL
   regime.
2. `2d_model_stability_diagnostic_sweep` - does tightening the solver's stability
   knobs (timestep, downstream slope, ...) drive a 2D model to numerical
   convergence? Anchored on the published Bald Eagle Creek 5-trial path (vol err
   <1e-6%, max WSE err ~0.05 ft).

Per the analysis-is-playground + simplicity doctrine and the ADR 0157/0174
precedent, neither row is a new atomic tool: a comparison / sweep is a COMPOSED
analysis the agent performs by calling `hecras_flood_2d` N times with a varied knob
and differencing the results in the playground. The durable landing is the enabling
KNOBS + reference drivers + the regime/convergence EVIDENCE, not new registered
tools. Registry stays 232, `EXPECTED_TEMPLATES` stays 74.

## What was built

- **`computation_interval` timestep knob** (the missing composable stability knob):
  `compose_pure2d_deck(..., computation_interval=None)` patches the `.bNN`
  `Computation Interval` line (validated int+SEC/MIN/HOUR, loud reject on garbage),
  threaded through `flood2d_pipeline.author_and_compose` / `run_flood2d` and surfaced
  on the `hecras_flood_2d` template. `None` keeps the shipped Chippewa 2MIN default.
  The `equation_set` knob (ADR 0157) is the second composable knob; `ds_slope` is a
  third (composer-level). Host-side only where the deck is authored; no solver-image
  rebuild for the knob itself.
- **Reference drivers** (non-registered, the ADR 0174 form): `compare_equation_sets.py`
  (author once -> solve DW + SWE -> per-cell WSE diff) and `stability_sweep.py`
  (author once -> re-solve at an interval ladder -> peak/vol-err convergence).
- **`equation_set` docstring refined** with the inertial-regime finding (below).

## Row 1 finding - the schemes coincide on the FOOTPRINT, separate LOCALLY

Two regimes, both live through the production 6.6 `RasUnsteady`:

- **Sayers Dam dam-break** (the ADR 0174 `baldeagle_connection` g09 deck, impounded
  688 ft pool over the 683 ft crest, ~300k cfs peak weir flow): max WSE / wet extent
  / max depth **byte-identical** across 19,597 cells (absmax dWSE 0.0000 ft); the
  schemes separate only in the TRANSIENT (connection total flow diverges up to
  ~14,700 cfs instantaneously, TW stage up to 0.26 ft). Max WSE here is
  initial-condition-dominated (the pool), so it cannot discriminate on the peak
  metric - a caveat, not the whole story.
- **Blanco River canyon nr Wimberley TX** (fresh-authored, 8075 cells, 329 ft relief,
  dry start, 15000 cfs sharp inflow - a DRY, dynamics-driven steep case): peak
  footprint **identical** (wet 6192 = 6192, max depth 116.13 = 116.13 ft, max WSE
  1118.04 = 1118.04 ft), but per-cell max WSE **separates up to 1.86 ft** at ~0.35%
  of cells (28 cells >0.1 ft, 5 cells >1.0 ft), concentrated at the high-velocity
  inflow / canyon-head constriction - the localized inertial signature.

Verdict: the `equation_set` knob is HONORED and does change the dynamics, but even in
a genuine inertial regime the peak-inundation deliverable (extent / max depth / max
WSE) is scheme-insensitive; full SWE matters for local momentum detail (constrictions,
rapid transitions, transients), not the flood envelope. Diffusion Wave stays the
cheaper validated default. This extends ADR 0157's Muncie coincidence into a steep,
dry, high-energy regime - the row is answered WITH the divergence quantified, not a
null result.

## Row 2 finding - the coarse-step overshoot converges monotonically

Blanco deck, same mesh, re-solved at a descending interval ladder:

| interval | peak depth (ft) | vol err (%) |
| --- | --- | --- |
| 10MIN | 487.49 (numerically UNSTABLE spurious spike) | 0.003776 |
| 5MIN | 244.84 | 0.011979 |
| 2MIN | 116.13 | 0.002533 |
| 1MIN | 116.09 | 0.008638 |

The peak collapses monotonically as the step tightens and stabilizes at 2MIN: the
2MIN->1MIN peak change is **0.04 ft**, matching the published Bald Eagle ~0.05 ft
max-WSE convergence anchor. Volume error stays sub-0.02% throughout (a secondary
signal here; the primary diagnostic is the peak-WSE stabilization). COMPOSABLE
stability knobs today: `computation_interval` (this sweep) + `ds_slope`. NOT
composable on an authored pure-2D deck (recipe, not built): culvert-invert raising
(`compose_pure2d_deck` strips all Structures) and cell re-alignment (the AuthorMesh
topology is fixed) - the honest subset is landed, the rest recipe'd.

## Two pre-existing in-daemon bugs fixed (the live showcase surfaced them)

`hecras_flood_2d` had never run END-TO-END through the daemon (ADR 0140 accepted it
DIRECT-CALL). The live showcase exposed two latent breaks, both fixed:

1. **`_WORKERS_FRESHTOPO` path** was `parents[6]` -> `.../server/services/...` (does
   not exist); corrected to `parents[7]` -> repo-root `.../services/...`. Without it
   the in-daemon authoring stage raised `ModuleNotFoundError: No module named
   'flood2d_pipeline'`.
2. **Worker strict-parser allowlist** (`entrypoint.py` `_KNOWN_MANIFEST_FIELDS`,
   parser `hecras-manifest-1`, ADR 0158) rejected the GENERIC run_solver-seam
   envelope `run_id` / `inputs` / `outputs` / `hecras_args` that the M3-gate
   no-archetype manifest carries - so every fresh-deck solve exited 1 with
   "manifest.json carries unknown field(s)". These are the seam's contract (the seam
   reads `inputs`/`outputs` to stage the deck + collect results; the worker reads
   only the solve fields), so they are added to the allowlist as accept-and-ignore.
   The worker image `trid3nt-local/hecras:latest` was REBUILT (ADR 0148 law; new id
   `e2216711e2b0`, 2.24 GB; entrypoint import + engine-link smoke green in-build).

## Evidence

- Direct-call live solves (production 6.6 engines): the two Row-1 regimes + the
  four-trial Row-2 ladder above. Solve times 3.5-7.5 min each.
- **Live end-to-end through the restarted daemon** (the showcase !run):
  `hecras_flood_2d(bbox=[-98.115,29.975,-98.083,30.0], target_peak_cfs=15000,
  resolution_m=30, equation_set='full_swe', computation_interval='1MIN')` ->
  authoring honored the knobs (`eq=full_swe interval=1MIN`, 8075 cells) -> run_solver
  staged + the rebuilt worker accepted the manifest -> solve status=ok, exit 0,
  vol_err 0.008619% (matching the direct 1MIN solve to 5 digits) -> postprocess
  depth_max 116.09 ft (matching) -> published the depth COG
  (`overviews/01KZGH76E482J672KBSSG4SMDV.tif`, ylgnbu rescale 0,3) + the mesh context
  layer. Run id `01KZGGV4XRTFDHGJN1Q8YQYCE2`, showcase case `01KZGGTTZGZN8W1Q41XR4MTP5D`.
- Proofs (`docs/proof/templates/`): `hecras_flood_2d_equation_diffmap.png` (per-cell
  |dWSE| over Esri, mesh polygons = wireframe, hotspots at the canyon head),
  `hecras_flood_2d_equation_regression_chart.png` (DW-vs-SWE 1:1 + exceedance,
  deltas in the caption), `hecras_flood_2d_stability_sweep_chart.png` (the
  487->116 ft convergence + vol err, anchor in the caption).
- Tests (offline slice green, `env -u TRID3NT_CACHE_BUCKET ... --timeout=300`):
  `test_hecras_deck2d.py` +3 (`computation_interval` default/override/reject),
  `test_hecras_flood2d_template.py` +1 (interval regex),
  `test_entrypoint.py` +1 (seam-envelope fields accepted); + freshtopo + the pin
  tests (`test_catalog_surfacing` registry 232, `test_door_dissolution`
  `EXPECTED_TEMPLATES` 74 - both UNCHANGED).

## Consequences

- Coded-tools metric: **0 registered tools, 0 templates** added; registry 232 -> 232,
  `EXPECTED_TEMPLATES` 74 -> 74. New capability = one composable PARAM
  (`computation_interval`) on an existing template + two reference drivers. No
  corpus / categories change (no new tool, so no retrieval-corpus requirement).
- Worker image rebuilt (manifest allowlist); host authoring/solver images otherwise
  unchanged. `hecras_flood_2d` now runs end-to-end in the daemon for the first time.
- Board rows `2d_diffusion_wave_vs_full_swe_regression` and
  `2d_model_stability_diagnostic_sweep` -> LANDED with the regime/convergence
  evidence above.
- Residual (recipe, not built): the non-composable stability knobs (culvert-invert,
  mesh re-alignment) need Structure authoring + mesh re-topology on fresh decks - the
  same fronts ADR 0157 named. Multi-inflow tributary BC lines remain the OI-D residual.
