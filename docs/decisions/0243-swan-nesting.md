# ADR 0243 - SWAN computational grid & nesting: binary gate (all four capable) + two-level nesting PHYSICS-PROVEN + scoped productionization recipes

Status: SUBSTRATE-CHARACTERIZED (2026-08-13). The baked `swan.exe` (41.51AB) is a
FULL-capability build for all four rows of the board's "Computational grid &
nesting" section. Two-level SWAN-to-SWAN nesting (NGRID/NESTOUT -> BOUNDNEST1) is
PHYSICS-PROVEN this wave through the baked binary with a norm-9 discriminating
pair. No tool surface change (coded-tools delta 0); Invariant 7 held. The three
remaining rows are binary-capable but blocked on out-of-swan-scope machinery
(a mesh generator, a WW3 spectra fetcher, a curvilinear-coordinate generator) and
are STOP-recipe'd with the exact remaining blockers named. Mirrors ADR 0238's
posture: gate + proof + recipe, not a forced inert-knob fold.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD "Computational grid & nesting (CGRID/NGRID/GROUP/
BOUNDNEST)" section (board L994) carries four rows, all riding the SWAN machinery
(`swan_wave_field`), NOT SFINCS - the ownership ADR 0238 flagged. This wave
adjudicates them. The deck-builder today emits ONE regular (rectilinear) spherical
grid: `CGRID REGULAR` + `INPGRID BOTTOM REGULAR` + a parametric `BOUNDSPEC SIDE
... PAR` (or a TPAR series). It emits no `NGRID`/`NESTOUT`/`BOUNDNEST*`, no
`CGRID CURVILINEAR`, no `CGRID UNSTRUCTURED`. So for every row the question is
two-part: (a) is the physics in the baked binary, and (b) what does the
deck-authoring + orchestration layer need.

This is the SWAN mirror of the TOMAWAC/ARTEMIS (ADR 0236/0237) and SFINCS SnapWave
(ADR 0238) gates: characterize the binary BEFORE building.

## Gate verdict: all four grid/nesting capabilities are PRESENT in 41.51AB

`strings /opt/swan/swan.exe` in `trid3nt-local/swan:latest` (bffb3ac6cc02):

| capability | binary evidence |
|---|---|
| SWAN-to-SWAN nesting | `NEST`, `NESTOUT`, `Nesting procedure does not work if file has only 1 spectrum`, `SWAN need at least 2 boundary points for nesting`, `BOUNDNEST1` path |
| unstructured (ADCIRC) | `GRID: UNSTRUCTURED`, `SwanReadADCGrid`, `read unstructured grid`, `SwanCompUnstruc.f90` (vertex-centered Gauss-Seidel solver) |
| curvilinear | `GRID: CURVILINEAR`, `read curvilinear coordinates`, `IS fully implemented for curvilinear coord.`, `READGRID COOR` |
| WW3 boundary nesting | `BOUNDNEST3`, `Reading WW3`, `WAVEWATCH III SPECTRA`, `The BOUndnest3 WW3 command requires UNFormatted or FREe`, `BCWW3N` |
| WAM boundary nesting | `WAMNEST`, `WAM nest does not work with only one nesting point` (board notes it untested; out of US scope) |

The solve paths are all real. The differentiator is the deck-authoring +
upstream-data / mesh machinery each needs.

## Two-level nesting: PHYSICS-PROVEN through the baked binary (norm-9 pair)

Synthetic sloping-beach spherical substrate (south-facing open boundary; deep
30 m at the south ramping to +3 m land at the north), run directly through
`/opt/swan/swan.exe` in the baked image. Decks + bathy authored by
`scratchpad/swan_nest/author.py`; the exact three `.swn` decks are persisted at
`docs/proof/templates/swan_nested_grid/`.

Two-pass, exactly as the row describes:
1. **PARENT** (coarse 40x32), forced `BOUNDSPEC SIDE S CONSTANT PAR 3.0 10.0
   180 25`, with `NGRID 'chld' <child outline>` + `NESTOUT 'chld' 'nest.dat'`.
2. **CHILD nested** (fine 40x30, over the child outline), `BOUNDNEST1 NEST
   'nest.dat' CLOSED`.
3. **CHILD standalone** (same fine grid, NO boundary forcing) - the norm-9
   control.

Results (Hsig from the `.mat` BLOCK output, SWAN 41.51AB):

| run | Hs max (m) | Hs mean (m) |
|---|---|---|
| parent (coarse) | 3.01 | 2.08 |
| child nested (BOUNDNEST1) | 2.88 | **2.63** |
| child standalone (no forcing) | 0.00 | **0.00** |

The parent wrote `nest.dat` = 140 boundary spectra on the child outline (LONLAT
spherical, SWAN 41.51AB header). The nested child INHERITS the parent's 3.0 m
storm swell across the nest boundary (mean 2.63 m); the standalone child cannot
see it and is dead flat (0.00 m). Stark, physically-correct discriminating pair.
Proof figure: `docs/proof/templates/swan_nested_grid/swan_nested_grid_norm9_proof.png`
(labelled SYNTHETIC SUBSTRATE - a binary-capability physics proof on an idealised
beach, not a QGIS-true production render).

One authoring gotcha captured for the productionization: the SWAN `PROJECT`
command's run-number string is capped at 4 chars (`** Error: too long string
given for: NR` on a 5-char value) - the production deck's `PROJECT 'TRID3NT'
'WAVE'` is already safe.

## Per-row disposition

| board row | binary | disposition |
|---|---|---|
| `two_level_nested_grid_coarse_to_fine_coupling` [CAND-L] | YES | **PROVEN-LANDABLE.** Physics proven above. Productionization scoped below as a WITHIN-WORKER two-pass (confined to swan-family files). NOT landed this wave: a full landing needs a worker-image rebuild + a live daemon E2E + a showcase seed, which would contend with the ACTIVE T3D-completion wave on the shared daemon / editable-install checkout / 95%-full disk (serialize-server-waves discipline). De-risked, not deferred-blind. |
| `unstructured_triangular_mesh_local_refinement` [CAND-L] | YES (ADCIRC reader) | **STOP-RECIPE (mesh-track dependency).** Needs a `fort.14` ADCIRC-mesh writer + `CGRID UNSTRUCTURED` / `READGRID UNSTRUC ADC` deck emission + per-vertex depth (not `bottom.bot`). The mesh generators live OUTSIDE swan-family (`agent/mesh/coastal_tin.py`, `workflows/mesh/generate_mesh`, the SCHISM/TELEMAC unstructured lineage); board L123 confirms "no fort.14 path". This is a big-3-mesh-layer integration, not a swan-only fold. |
| `ww3_boundary_nested_regional_downscale` [CAND-M] | YES (BOUNDNEST3 WW3) | **STOP-RECIPE (data-gate).** Needs WW3 spectra at the boundary. The universal-fetcher surface carries ZERO WW3 / WAVEWATCH / buoy spectra specs (only `fetch_noaa_coops_tides` = tide levels). Same roster gap ADR 0238 flagged. To land: a WW3-hindcast spectra fetcher (`ww3_outp`-post-processed FREE/UNFORMATTED files, DOI 10.5066/F7G73CP1 Hawaiian lineage) + `BOUNDNEST3 'file' CLOSED FREE`. Most US-relevant nesting entry point once the fetcher exists (NOAA runs WW3 operationally). |
| `curvilinear_grid_coastline_following_domain` [CAND-M] | YES (CGRID CURVILINEAR + READGRID COOR) | **STOP-thin (utility-class).** Needs an external curvilinear-coordinate generator (a channel/coast-following grid tool) writing the `xy` coordinate file. It is a grid-topology variant of the same regular-ish solve with LOW marginal question-class value once regular + nested cover the domain-discretization question. Utility over question-class - do not build ahead of a concrete case that regular+nested cannot serve. |

## Productionization recipe (the follow-on build wave) - two-level nesting

The cleanest surface is a WITHIN-WORKER two-pass (ONE `run_solver` dispatch, ONE
manifest, ONE container that runs SWAN twice), which keeps the composer /
`run_solver` seam byte-unchanged and confines the new machinery to swan-family
worker files:

1. **deck_builder** (`services/workers/swan/deck_builder.py`, pure render):
   add an optional top-level `nest` sub-spec `{child_bbox, child_mx, child_my,
   boundary_density_mx?, boundary_density_my?}`. When present, `build_swan_deck`
   authors TWO decks - the PARENT (main bbox/mx/my, coarse) gains `NGRID 'chld'
   <child extent, boundary-density mesh>` + `NESTOUT 'chld' 'nest.dat'`; the
   CHILD (child_bbox, child_mx/my, fine) gains `BOUNDNEST1 NEST 'nest.dat'
   CLOSED` in place of `BOUNDSPEC`. Validate `child_bbox` is a strict subset of
   the parent bbox. Bump the parser version + reject unknown fields (ADR 0158).
2. **entrypoint** (`services/workers/swan/entrypoint.py`): when `nest` is present,
   author + run the parent (`swanrun -input swan_parent`), then the child
   (`swanrun -input swan_child`), then postprocess the CHILD `swan_out.mat`. The
   all-dry / bathy-coverage guards run on BOTH grids. This is a worker change ->
   the worker-image-staleness law applies: rebuild `swan:latest`, provenance-
   check, and smoke the two-pass THROUGH the image.
3. **run_swan** (`workflows/swan/run_swan.py`): thread the `nest` fields from
   `SwanRunArgs` into `build_swan_build_spec`; stage unchanged (one manifest).
4. **composer knob** (`workflows/swan/wave_field/wave_field.py`): a
   `nest_child_bbox` (+ optional `nest_child_mx/my`) knob on `swan_wave_field`;
   when set, the postprocess bbox becomes the CHILD bbox (the primary field is
   the fine nearshore grid). No new registered tool -> coded-tools delta stays 0;
   this is a knob on the existing template.
5. **contract** (`contracts/.../swan_contracts.py`): `SwanRunArgs.nest` optional
   sub-model; declared resolution on `nest_child_mx/my` per ADR 0225/0232.
6. **E2E + showcase**: a real US bay/inlet where a coarse shelf grid feeds a fine
   inlet grid (e.g. a Great Lakes harbour on NOAA lakebed bathy - the ADR 0236
   Lake Superior lineage - or a CUDEM Gulf inlet). Norm-9 pair = nested child vs
   standalone child (the exact pair proven synthetically here). SWAN =
   engineering/planning-grade wave field (fidelity-ladder stated).

## Consequences

- The board's grid/nesting "Today: regular-grid only / not yet surfaced" is
  resolved to a precise gate: all four capabilities are binary-present; one is
  physics-proven and landable, three are blocked on named out-of-scope machinery.
- No inert knobs shipped - Invariant 7 held; coded-tools delta 0 (245 total
  unchanged).
- The nesting productionization is de-risked to a mechanical build: the two-pass
  spectral handoff is proven to run through the baked binary, the deck syntax is
  captured (incl. the PROJECT-NR 4-char gotcha), and the exact file-by-file wiring
  is named. It is deferred THIS wave only for concurrency safety (the live T3D
  wave owns the daemon), not for any technical unknown.
- SWAN = nearshore engineering/planning-grade spectral wave field throughout
  (SFINCS = coastal screening; TOMAWAC/ARTEMIS = refinement) - fidelity-ladder
  doctrine intact.
