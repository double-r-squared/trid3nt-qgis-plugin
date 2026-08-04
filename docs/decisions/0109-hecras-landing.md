# ADR 0109 -- HEC-RAS engine landing (engine #11, template-first)

Status: accepted (2026-08-04)
Follows: ADR 0100 (mesh wave M3 -- the HEC-RAS worker image + Muncie replication
gate + the writer STOP -> template-first verdict), the NATE-signed ras-commander
feasibility spike (`reports/design/ras-commander-feasibility-2026-08-03.md`), ADR
0105 (composer dissolution -- engine templates are the atomic simulation surface),
ADR 0106 (structured `synthetic_inputs`), ADR 0107 (the two-mode input gate).

## Context

M3 (ADR 0100) built the HEC-RAS worker image (HEC's official public-domain 6.6
Linux computation engines), proved the geometry pipeline bit-identical on HEC's own
shipped Muncie test project (White River, Muncie IN), and resolved the writer's
feasibility honestly: from-scratch 2D geometry authoring is BLOCKED on Linux
(RASMapper's terrain subgrid tables need Windows DLLs), so the engine landing goes
TEMPLATE-FIRST -- reuse a RASMapper-built geometry HDF and reparameterize the
forcing. M3 shipped no registered tool / template / contract.

This wave lands HEC-RAS as engine #11 with ONE registered template: the
Muncie-class 1D/2D riverine-flood archetype. Registry 172 -> 173.

## Decisions

### 1. The reparameterization is the unsteady FLOW forcing (empirically pinned)

The Muncie geometry/terrain/mesh are FROZEN (the RASMapper subgrid tables are
prebuilt -- the M3 STOP). What varies is the unsteady inflow hydrograph.

**Flow authority, settled empirically (in-container, 2026-08-04).** The Linux
`RasUnsteady` reads the inflow hydrograph from the `.bNN` ASCII boundary file, NOT
from the plan HDF's `Event Conditions` group: scaling the HDF hydrograph left the
2D max water surface BIT-IDENTICAL (951.927 ft at both 1.0x and 1.3x), while
scaling the `.bNN` moved it (wet cells 4896 -> 5012, depth_max 20.24 -> 20.62 ft,
depth_mean 7.45 -> 7.76 ft at 1.3x). So `services/workers/hecras/deck_edit.py`
`scale_flow_hydrograph` is the authoritative deck edit: it multiplies every flow
ordinate in the `.bNN` Flow-Hydrograph block by the factor, preserving HEC's exact
8-char fixed-field layout (5 pairs/line), and leaves every other deck byte
untouched.

**Two forcing levers.** `flow_scale` (a plain multiplier on the baseline ~21000
cfs peak) is the user/default path; `target_peak_cfs` derives the multiplier from
the baseline peak (the seam-1 fetcher / ADR 0102 path -- pin the forcing to a real
USGS-gauge / NWM peak, `basis="fetched"`). Both clamp to the modelable band
[0.25, 4.0] (a frozen demonstration geometry is only faithful within a band). Sim
window / output interval are NOT exposed in v1 (the deck's dates live in multiple
files; the flow multiplier is the clean, provably-physical lever -- simplicity over
completeness).

### 2. Worker dispatch reuses the M3 image (baked deck, in-image reparameterize)

`run_solver('hecras_muncie_flood')` dispatches to `trid3nt-local/hecras:latest` via
a `LocalSolverSpec` (`workflows/hecras/run_hecras.py`, structural clone of the
TELEMAC local spec: volume-mount `/data`, `classify_exit` reads
`hecras_metrics.json`). The Muncie deck is BAKED in the image, so the manifest
carries only the archetype + flow knobs (`inputs: []`); the entrypoint copies the
baked deck into `/data`, applies the `.bNN` flow scale, runs `RasGeomPreprocess`
then `RasUnsteady` (appending Results to the plan HDF in place), and the supervisor
uploads the solved plan HDF + metrics. Honest failure surface is UNCHANGED from M3:
the Finished sentinel + a Results group gate the run; `classify_exit` mirrors that
as `correct_end` (a clean exit without it reads as error, not empty success).

### 3. Template + contract

- Contract `contracts/hecras_contracts.py`: `HECRASRunArgs` (archetype literal
  starting with the one archetype + `flow_scale` / `target_peak_cfs` /
  `input_mode`), `HecrasDepthLayerURI` (extends `LayerURI`; carries `depth_max_ft`
  / `depth_mean_ft` / `wet_cell_count` / `wse_max_ft` / `flow_scale` /
  `peak_inflow_cfs` / `volume_error_pct` / `n_cells`), and the typed error codes
  (`HECRAS_SOLVE_FAILED`, `HECRAS_INPUT_INVALID`,
  `HECRAS_FINISHED_SENTINEL_MISSING`, `HECRAS_OUTPUT_EMPTY`). The depth raster
  HONESTLY REUSES the flood-depth family style preset (`continuous_flood_depth`) --
  a HEC-RAS 2D depth grid IS an overland flood depth.
- Template `workflows/hecras/muncie_flood/muncie_flood.py` (post-0105 one-file
  composer): params -> input-review gate (0107, `synthetic_inputs`: flow-scale
  basis + the frozen-geometry note `basis=default_demo` labeled "Muncie White River
  IN demonstration geometry") -> stage manifest -> dispatch worker -> postprocess.
- Postprocess `workflows/hecras/postprocess_hecras.py`: the solved plan HDF ->
  per-cell peak DEPTH (max WSE minus `Cells Minimum Elevation`, masked to wet
  cells) -> rasterized to an EPSG:4326 depth COG + published; PLUS the 2D flow-area
  MESH-preview vector layer (reusing `agent/mesh/hecras_geometry.read_2d_flow_area_cells`,
  the M3 read half, `style_preset="mesh_grid"`, `role="context"`); PLUS a
  best-effort inflow-hydrograph forcing chart (the Event Conditions series scaled by
  the flow multiplier -- HEC's own output-plot analogue, every point a real parsed
  engine input).

### 4. Demonstration-geometry honesty is LOUD (NATE no-hand-wave doctrine)

The template answers what-if flow on the Muncie demonstration reach, NOT flooding
at a user AOI. Said in the docstring (fidelity line + the off-scope ->
`sfincs_flood` redirect) AND stamped on every result envelope's `fallback_note`
(`_DEMO_GEOMETRY_NOTE`). The solve is fixture-bounded (~1 min per M3) -- stated
honestly; no granularity ladder needed.

## Consequences

- Registry 172 -> 173 (in-process); CODED tools +1 (the `hecras_muncie_flood`
  template). New: contract `hecras_contracts.py`, worker `deck_edit.py` + entrypoint
  deck-staging/flow-scale extension, `workflows/hecras/` (run_hecras +
  postprocess_hecras + `_template_card` + muncie_flood/ template + corpus). Wired:
  `workflows/__init__.py` (run_hecras solver reg), `tools/__init__.py` (template
  import), `categories.py` (`hazard_modeling`). No flood.py / SFINCS seam touched
  (grep-verified; the hecras acceptance IS this wave's canary -- no flood canary).
- CANARY GREEN (in-container, real RasUnsteady, 2026-08-04): default flow (1.0x,
  peak 21000 cfs, depth_max 20.24 ft, wet cells ~4881, volume error 0.0058%) + a
  scaled flow (1.3x, peak 27300 cfs, depth_max 20.62 ft, wet cells ~4998, volume
  error 0.0056%) -- higher flow deeper + wider (the parameterization changes the
  answer sanely), both inside the M3 < 0.05% volume-accounting gate.
- Retrieval: `hecras_muncie_flood` surfaces in `retrieve_visible_tools(q, None, 8)`
  for all corpus queries (hec-ras flood model / 1d 2d river hydraulics / unsteady
  flow white river / refinement-grade riverine flood / run the Muncie model).
- Offline suite baseline preserved (EXACTLY 9 by SET: fetch_resolution x4 +
  river_dye x5). Container hygiene: image 2.2 GB unchanged (the code layer is tiny;
  the compact solved-HDF TEST fixture is `.dockerignore`d, never baked). The M3 MKL
  / ras-commander-closure trims stay characterized-not-done (ADR 0100).

## What the next archetypes need

- **Bald Eagle Creek 2D levee** (`BaldEagleCrkMulti2D`, Lock Haven PA): bake the
  shipped BEC geometry into the image as a second baked deck + a
  `bald_eagle_2d_levee` archetype literal; the levee/SA-2D-connection weir + breach
  params become the reparameterization knobs (a DISTINCT deck-edit surface from the
  flow multiplier). Verified headless on Linux (the neeraip 0.00% row).
- **Rain-on-grid 2D**: a precipitation boundary (spatially uniform/gridded) as the
  forcing -- pairs with the NWM/precip fetchers; a distinct QUESTION class (pluvial
  vs fluvial). Needs a shipped rain-on-grid tutorial geometry baked.
- Both extend `_BAKED_DECKS` + the archetype literal + (for a non-hydrograph
  forcing) a new `deck_edit` branch; the worker dispatch + postprocess-to-depth-COG
  + mesh-preview spine is reused unchanged.
- HAND raster terrain-hydrology (deferred from ADR 0108) can feed a future real-AOI
  path once headless 2D geometry authoring is solved (the ledgered HEC-RAS 2025
  native-Linux migration may retire that blocker -- feasibility report S2b).
