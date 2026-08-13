# ADR 0238 - SFINCS SnapWave + nesting: substrate gate (binary-capable) + productionization STOP-recipe

Status: SUBSTRATE-CHARACTERIZED (2026-08-13). Gate PASSED at the binary level:
the baked `sfincs-v2.3.3` binary is a full SnapWave build. But productionization
is NOT a one-wave knob fold - SnapWave forcing requires a hand-authored boundary
writer (no hydromt/cht support) + a snapwave-mask generator + an offshore-wave
data source, and the physics knobs are INERT until that forced deck ships
(Invariant 7). This wave lands the substrate characterization + the scoped
productionization recipe; it does NOT touch the tool surface (coded-tools delta
0). The SnapWave board rows stay CAND/STOP with the gate finding recorded.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD SFINCS block carries a **SnapWave** section (4 rows,
board L1367) and the task pairs it with a **Computational grid & nesting**
section. SnapWave is SFINCS's built-in stationary/implicit nearshore wave
transformation solver (refraction/shoaling, depth-induced breaking, bottom
friction, incident + infragravity setup) that couples wave-driven setup/runup
into the SFINCS shallow-water solve. Nesting = coarse-parent -> fine-child
boundary forcing.

This is the mirror of the TOMAWAC/ARTEMIS gate (ADR 0236/0237): before building,
gate-check whether the physics is even present in the baked binary.

## Gate verdict: SnapWave binary support is PRESENT (proven three ways)

The prior board disposition (STOP ADR 0152 on `incident_wave_setup_toggle`)
correctly stated the DECK-BUILDER emits no SnapWave forcing, but left the BINARY
question open. Resolved here:

1. **Symbol table** - `strings /usr/local/bin/sfincs` in `trid3nt-local/sfincs:latest`
   yields the complete SnapWave surface: `sfincs_snapwave.f90`, `snapwave_gamma`,
   `snapwave_gammax`, `snapwave_alpha`, `snapwave_fw`/`snapwave_fwig`,
   `snapwave_waveforces_factor`, `snapwave_baldock_opt`/`snapwave_baldock_ratio`,
   `snapwave_igwaves`/`snapwave_alpha_ig`/`snapwave_gammaig`, plus the boundary
   file contract (below) and the `hm0`/`hm0ig` output variables.
2. **Runtime banner** - a SnapWave-enabled deck reaches
   `----------- Welcome to SnapWave ---------` /
   `Build-Revision: $Rev: svn 197-branch:SnapWave_IG` / `Build-Date: 2025-04-14`.
   The baked binary is literally built off the SnapWave_IG branch.
3. **Read side already wired** - `services/workers/_raster_postprocess/sfincs_reader.py`
   already selects `hm0` (fallback `hm0ig`) as the SnapWave wave-height layer and
   honestly degrades ("not a SnapWave run") when absent; `postprocess.py` carries
   the `kind="waves"` manifest branch. The OUTPUT half is done.

This is the OPPOSITE finding to a STOP-RECIPE gate (SCHISM iharind, HEC-RAS WQ
walls where the code path does not exist): the solve path is real. What is NOT
built is the TRID3NT deck-AUTHORING layer - and that layer is materially heavier
than the ARTEMIS/TOMAWAC ones were.

## SnapWave input contract (decoded from the binary)

Enable: `snapwave = 1` (+ `snapwave_waveforces_factor = 1` to couple wave force
into the flow momentum - otherwise SnapWave runs diagnostically with zero
feedback, an Invariant-7 no-op).

| input | file keyword | role |
|---|---|---|
| wave mask | `snapwave_mskfile` (or `quadtree_snapwave_mask`) | msk=2 = wave open boundary, msk=1 = active nearshore |
| wave depth | `snapwave_depfile` | SnapWave bathymetry (may reuse the flow dep) |
| boundary points | `snapwave_bndfile` (x y per line) | wave boundary point locations |
| boundary Hs/Tp/dir/spread | `snapwave_bhsfile` / `btpfile` / `bwdfile` / `bdsfile` | offshore sea-state time series per bnd point |
| JONSWAP / netcdf | `snapwave_jonswapfile` / `netsnapwavefile` | alternative spectral boundary inputs |
| output | `point_hm0` / `hm0_wave_height`, `point_hm0ig` / `hm0_ig_wave_height` | nearshore Hm0 (inc + IG) -> `sfincs_map.nc` (reader already consumes) |
| physics | `snapwave_gamma` (Baldock breaker), `snapwave_fw` (Collins friction), `snapwave_igwaves`, `snapwave_waveforces_factor` | the eventual knob surface |

## Discriminating-pair attempt (local-first, in-image, direct binary)

Synthetic sloping-beach regular grid (40x60 @ 50 m, bed -20 m offshore ramping to
+4 m land, offshore row msk=2), run directly through
`/usr/local/bin/sfincs` in the baked image. Scripts +decks in the wave scratchpad
(`snapwave_pair.py`).

- **OFF** (`snapwave=0`, still water, no forcing): SOLVES CLEAN in 0.4 s;
  `sfincs_map.nc` `zs` = 0.0 everywhere (correct flat baseline - the control).
- **ON** (`snapwave=1`, `snapwave_waveforces_factor=1`, offshore Hs=3 m / Tp=10 s /
  onshore direction, `snapwave.bnd/bhs/btp/bwd/bds`, `snapwave.msk` msk=2):
  READS the mask, prints `SnapWave : yes` / `Coupling with SnapWave ...` /
  `Welcome to SnapWave`, then **SIGSEGV inside the SnapWave boundary/directional-grid
  setup** (`snapwave_boundaries.f90` region). Three targeted variants (IG disabled
  via `snapwave_igwaves=0`, `snapwave_dt` set, full offshore-row boundary-point
  coverage) all reach the same in-SnapWave segfault.

**Reading:** the binary is capable (it enters SnapWave and reads the deck), but a
VALID SnapWave-forced deck cannot be hand-authored blind - the enclosure /
directional-sector / IG-boundary contract in `snapwave_boundaries.f90` is exact
and unforgiving, and **neither hydromt_sfincs nor cht_sfincs authors it**
(hydromt only carries `snapwave_mask` rename plumbing on the quadtree; grep of
both packages in-image confirms no SnapWave boundary writer). The setup delta was
NOT captured this wave - honestly reported, not padded.

This is the load-bearing scope signal: SnapWave productionization needs a
boundary writer reverse-engineered against the SFINCS Fortran source (or a
known-good Deltares example deck), which is a characterization wave, not a knob
fold.

## SnapWave board rows -> disposition

| board row | disposition after gate |
|---|---|
| `wave_breaking_gamma_tuning` [CAND-M] | knob-READY (`snapwave_gamma` real) but INERT until the forced deck ships - do NOT land now (Invariant 7). Folds in the productionization wave. |
| `reef_bottom_friction_high_roughness_case` [CAND-M, non-US] | same: `snapwave_fw` real, gated on the forced deck. Non-US site (Ningaloo) - US analogue needed per doctrine (e.g. Florida Keys / Guam reef). |
| `incident_wave_setup_toggle` [STOP ADR 0152] | STOP UPHELD + sharpened: `snapwave_waveforces_factor` toggle is real in the binary but toggles a force that our deck never emits -> still an inert no-op today. Unblocks with the forced deck. |
| `us_coastal_snapwave_boundary_case` [CAND-L] | the KEYSTONE row - lands the SnapWave-forced deck (St Croix USVI paper lineage, or the Hurricane Michael / Mexico Beach SFINCS home-turf case). Gates the other three. |

## Nesting: board-section ownership mismatch (flagged for NATE)

The board's only "Computational grid & nesting" section (L993) is under **SWAN**
(CGRID/NGRID/GROUP/BOUNDNEST), not SFINCS. Its 4 rows -
`two_level_nested_grid_coarse_to_fine_coupling`,
`unstructured_triangular_mesh_local_refinement`,
`ww3_boundary_nested_regional_downscale`,
`curvilinear_grid_coastline_following_domain` - ride the SWAN machinery
(`swan_wave_field`), NOT `sfincs_flood`/quadtree. They are a SWAN wave, out of
scope for this SFINCS/SnapWave batch. Recorded, not silently retargeted.

Separately, the SFINCS coarse->fine nesting the task DESCRIBES (parent water
levels -> child boundary forcing) is genuinely light: the SFINCS deck-builder
ALREADY emits `bnd`/`bzs` water-level boundary forcing with arbitrary
timeseries + locations (`setup_waterlevel_forcing`, deck.py L1489,
`sfincs_builder.py` L304). SFINCS nesting is therefore an ORCHESTRATION recipe
(run coarse parent -> sample `sfincs_his.nc` at the child's boundary points ->
feed as the child's `WaterlevelForcing`), NOT new solver capability - but it has
NO board row of its own today. Candidate row to add if NATE wants it on the
board.

## Decision

1. **Gate recorded**: SnapWave is binary-present (SnapWave_IG build). This flips
   the SnapWave section's "Today: Unknown/not yet surfaced" to
   "binary-capable, deck-authoring unbuilt".
2. **No surface change this wave**: physics knobs (gamma/fw/waveforces) stay
   unlanded because they would be inert over a force our deck never emits
   (Invariant 7). Coded-tools delta 0.
3. **Productionization scoped as a follow-on characterization wave** (heavier
   than TOMAWAC/ARTEMIS - needs a boundary writer + mask gen + offshore-wave data
   source), keystone = `us_coastal_snapwave_boundary_case`.
4. **Nesting**: the named board section is SWAN-owned (defer to a SWAN wave);
   SFINCS coarse->fine nesting is recipe-class on existing bnd/bzs and needs a
   board row if we want it tracked. Both flagged for NATE; nothing retargeted.

## Productionization recipe (the follow-on wave)

1. **Boundary writer** - author `snapwave.bnd`/`bhs`/`btp`/`bwd`/`bds` (and/or the
   JONSWAP/netcdf path) against a known-good Deltares SnapWave example deck (the
   segfault proves the format cannot be guessed). Preferred source: a Deltares
   SFINCS SnapWave testcase or the St Croix paper's supplementary deck.
2. **SnapWave mask generator** - emit `snapwave_mskfile` (regular) and the
   `snapwave_mask` variable on the quadtree netcdf (the plumbing hydromt already
   renames), msk=2 on the offshore wave boundary.
3. **Offshore-wave data source** - Hs/Tp/dir at the boundary. The universal
   fetcher surface has ZERO ndbc/buoy specs today (board roster gap); needs a
   CDIP/NDBC or WW3-hindcast fetcher, or a labeled synthetic demo boundary.
4. **Physics registry** - add `snapwave_gamma`, `snapwave_fw`,
   `snapwave_waveforces_factor` (+ `snapwave_igwaves`) as SFINCS physics keys,
   folding the 4 board rows - ONLY after step 1-2 make them non-inert.
5. **Postprocess** - already reads `hm0`/`hm0ig` (no work).
6. **Parser/image law** - the worker `_sfincs_build` spec parser must hard-error
   on unknown SnapWave fields; rebuild the image; live smoke through the image.
7. **E2E + showcase** - the Hurricane Michael / Mexico Beach coastal lineage
   (SFINCS home turf), discriminating pair per norm #9 (waves ON vs OFF -> nearshore
   setup/runup + hm0 delta), SFINCS = SCREENING fidelity stated.

## Consequences

- The board's SnapWave "Unknown" is resolved to a precise gate finding + recipe;
  STOP ADR 0152 is upheld and sharpened rather than overturned.
- No inert knobs shipped - Invariant 7 held.
- The follow-on wave is de-risked: the input contract is decoded, the read side
  is confirmed done, and the exact remaining blockers (boundary-format source +
  offshore-wave fetcher) are named.
- SFINCS = coastal SCREENING fidelity throughout (TOMAWAC/ARTEMIS = refinement),
  fidelity-ladder doctrine intact.
