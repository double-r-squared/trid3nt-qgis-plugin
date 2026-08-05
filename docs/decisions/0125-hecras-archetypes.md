# ADR 0125 -- HEC-RAS archetypes: the levee-breach landing + the archetype triage

Status: accepted (2026-08-04)
Follows: ADR 0109 (HEC-RAS engine landing -- engine #11, the Muncie riverine-flood
archetype + the `.bNN` flow scaler + the local-docker solve spine), ADR 0100 (mesh
wave M3 -- the HEC-RAS worker image + Muncie replication gate + the headless-2D-mesh
STOP), ADR 0105 (composer dissolution -- engine templates are the atomic simulation
surface), ADR 0107 (the two-mode input gate).

## Context

ADR 0109 landed HEC-RAS as engine #11 with ONE archetype (`muncie_riverine_flood`):
a what-if UNSTEADY FLOW forcing on HEC's shipped Muncie White River (Muncie IN)
demonstration project, geometry FROZEN (the RASMapper subgrid tables cannot be
rebuilt headless -- the M3 STOP), the `.bNN` inflow hydrograph the only lever.

This wave is preceded by an ARCHETYPE TRIAGE of the shipped HEC-RAS catalogue --
which next archetype can land NOW on Linux vs which is blocked -- and lands the
green-lit one.

### Triage findings (part of this ADR)

1. **Levee-breach is already latent in the shipped Muncie deck (GREEN-LIT).** The
   baked HEC-official `Muncie.b04` carries a `Breach Data` block (the ASCII-editable
   0109 `.bNN` class) declaring 2 lateral-structure breaches, and the Muncie 2D
   Interior Area IS a leveed protected floodplain. A decisive two-solve experiment
   (real `RasGeomPreprocess` + `RasUnsteady`, ~1 min each, in-container 2026-08-04)
   proved the question is answerable with a fixed-field deck edit alone:
   - breach ON  -> 4881 wet cells / depth_max 20.24 ft / volume error 0.0058%
   - breach OFF -> 0 wet cells (protected side DRY) / volume error 0.0021%

   So the levee-breach archetype needs NO new geometry -- only a breach toggle. This
   is what this wave lands.

2. **The valid breach-OFF edit was pinned empirically.** Two disabling edits were
   tried in-container: (a) set the `Breach Data` count to 0 but KEEP the record
   lines -> `RasUnsteady` CRASHES (fatal in `Unetreal.for`); (b) set the count to 0
   AND DROP the record lines -> clean levee-holds solve (0 wet cells, 0.0021% volume
   error). Only (b) is valid; `set_breach_enabled` implements exactly (b).

3. **Bald Eagle Creek multi-2D levee is BLOCKED on Windows-Phase-1 (characterized,
   not landed).** The shipped Bald Eagle model (HEC's `dam-breach-analysis-with-2d-
   areas` tutorial, example id `hecras_hgt_dam_breach_2d_areas_bald_eagle`, in HEC's
   SHA-pinned example zip) is the real levee/breach V&V target -- published concrete
   numbers (~516,000 cfs peak breach outflow, ~435,000 cfs breach component,
   ~305,000 cfs at the downstream boundary; a matched Diffusion-Wave-vs-full-SWE
   regression pair). But its geometry HDF's terrain subgrid tables are
   RASMapper-authored (Windows DLLs), so re-authoring or intermediate rebuild is NOT
   headless-reproducible on Linux (the ADR 0100 M3 STOP). It awaits a Windows-Phase-1
   intermediates pipeline OR a third-party headless preprocessor (see finding 5).
   Ledgered QUEUED.

4. **Rain-on-grid (pluvial) is a STOP this wave.** A spatially-uniform/gridded
   precipitation boundary is a distinct QUESTION class (pluvial vs fluvial, pairs
   with the NWM/precip fetchers), but the Muncie deck has NO precipitation boundary
   and the forcing is a non-hydrograph deck-edit surface (a new branch beside the
   flow scaler / breach toggle). It needs a shipped rain-on-grid tutorial geometry
   baked first. Ledgered QUEUED.

5. **The neeraip-class preprocessor adoption is PARKED FOR NATE (strategic).** The
   community `neeraip/hecras-v66-linux` repro (the 0.00% Muncie volume-error row
   cited in ADR 0100/0109) shows a Linux path, but authoring RASMapper subgrid
   terrain tables headless -- the frontier blocking every real-AOI and multi-2D
   archetype -- is an open strategic choice: adopt a third-party preprocessor vs wait
   for HEC's native-Linux 2025 migration vs a Windows-Phase-1 intermediates pipeline.
   This ADR RECORDS the decision surface; it does NOT decide it. Ledgered QUEUED
   (PARKED for NATE).

This wave lands finding 1 as the second archetype. Registry 187 -> 188.

## Decisions

### 1. The reparameterization is the LATERAL-STRUCTURE BREACH toggle

`services/workers/hecras/deck_edit.py` gains `set_breach_enabled(text, enabled) ->
(new_text, n_breaches)`: the deterministic fixed-field toggle -- the breach analogue
of `scale_flow_hydrograph`. `enabled=True` returns the deck byte-identical (the
shipped deck breaches ON). `enabled=False` sets the `Breach Data` count to 0
preserving the field width AND removes the breach record lines up to the next
section header (finding 2 -- the only valid disabling edit). It touches a block
DISJOINT from the flow hydrograph, so the two knobs COMPOSE (breach first, then
flow scale). The entrypoint's `_apply_breach` applies it whenever the manifest
carries `breach_enabled` (absent -> the deck is left as-is, so the riverine-flood
archetype is byte-unaffected).

### 2. The levee-HOLDS case is a VALID DRY SUCCESS (allow_dry)

`workflows/hecras/postprocess_hecras.py` gains `allow_dry`: a 0-wet-cell solve, which
by default raises `HECRAS_OUTPUT_EMPTY`, is instead a VALID DRY SUCCESS when
`allow_dry=True` -- an all-nodata depth COG over the mesh bbox + zeroed stats +
`breach_enabled=False` on the layer + a "LEVEE HELD (protected side dry)" layer name.
The empty inundation IS the answer (the levee held), never an empty-output error
(honesty floor: a dry protected side is a real, narratable result). The layer's
`breach_enabled` field makes a dry result self-describing.

### 3. Template + contract (SAME geometry, DISTINCT capability)

- Contract `hecras_contracts.py`: `HECRAS_ARCHETYPES` gains `"muncie_levee_breach"`;
  `HECRASRunArgs` gains the `archetype` literal member + `breach_enabled: bool =
  True`; `HecrasDepthLayerURI` gains `breach_enabled: bool | None` (the levee
  scenario the layer carries -- `True` failed / `False` held / `None` riverine).
- Template `workflows/hecras/levee_breach/levee_breach.py` (post-0105 one-file
  composer, capability-named per ADR 0120): params `breach_enabled` (default True) +
  the 0109 flow knobs (`flow_scale` / `target_peak_cfs`) + `input_mode`. Chain:
  input-review gate (0107 `synthetic_inputs`: geometry=default_demo labeled + flow
  basis + breach-params basis) -> stage manifest (archetype + breach + flow) ->
  dispatch worker -> postprocess(allow_dry=True) -> publish. Best-effort per-run
  inflow-forcing chart (invariant 1, HEC's own output analogue); the breach-vs-holds
  protected-side DEPTH comparison is the cross-run acceptance artifact.
- Solve seam: `run_hecras.py` registers a SECOND solver name
  `hecras_levee_breach` reusing the SAME worker image (`hecras_local_spec(name)`) --
  the archetype + breach toggle ride in the manifest; one image, per-capability
  names for honest dispatch/logs.

### 4. Demonstration-geometry honesty is LOUD (NATE no-hand-wave doctrine)

The template answers "what does the PROTECTED SIDE look like when the levee FAILS vs
HOLDS" on the Muncie demonstration reach, NOT flooding at a user AOI. Said in the
docstring (fidelity line + the off-scope -> `sfincs_flood` redirect) AND stamped on
every result envelope's `fallback_note` (`_DEMO_GEOMETRY_NOTE`, verbatim-class): v1
runs the Muncie White River leveed-floodplain geometry; the Bald Eagle multi-2D
model awaits the Windows-Phase-1 unblock (ledgered).

## Consequences

- Registry 187 -> 188 (in-process); CODED tools +1 (the `hecras_levee_breach`
  template). New: `workflows/hecras/levee_breach/` (template + corpus). Extended:
  `deck_edit.set_breach_enabled`, entrypoint `_apply_breach` + a `muncie_levee_breach`
  baked-deck entry (SAME Muncie wrk_source), `postprocess_hecras.allow_dry`,
  `run_hecras` 2nd solver name, `hecras_contracts` (archetype + 2 fields). Wired:
  `tools/__init__.py` (template import), `categories.py` (`hazard_modeling`). No
  flood.py / SFINCS seam touched (grep-verified; the levee-breach acceptance IS this
  wave's canary -- no separate flood canary).
- ACCEPTANCE GREEN through the REGISTERED TEMPLATE (image rebuilt with the breach
  toggle, real `RasGeomPreprocess` + `RasUnsteady`, MinIO S3, 2026-08-04): breach ON
  -> 4881 wet cells / depth_max 20.236 ft / depth_mean 7.47 ft / vol_err 0.005834%;
  breach OFF -> 0 wet cells / VALID DRY SUCCESS / vol_err 0.002150% (both on the same
  5765-cell 2D Interior Area, bbox-matched). Both depth COGs published (F33 overview
  auto-translate) + the breach-vs-holds comparison chart + on/off proof renders.
  Reproduces the triage two-solve numbers exactly.
- Retrieval: `hecras_levee_breach` surfaces top-8 in `retrieve_visible_tools(q,
  None, 8)` for all 5 corpus queries (levee breach flood model / protected side when
  the levee fails / dam levee holds vs fails / run the levee breach scenario / levee
  failure vs holds comparison).
- Two regressions from the new tool CAUGHT + FIXED before close: `categories.py`
  needed the `hazard_modeling` mapping; `test_door_dissolution` `EXPECTED_TEMPLATES`
  went 29 -> 30 (the templates-count pin).
- Offline suite baseline preserved EXACTLY 9-by-SET across 4 foreground alphabetical
  slices (fetch_resolution x4 in [f-o] + river_dye x5 in [p-r]; [a-e] + [s-z] clean;
  360 files). Daemon boot clean (registry 188). Image 2.2 GB unchanged (code layer
  only; the fetch stage is cached).

## What the next archetypes need (the triage backlog, ledgered)

- **Bald Eagle Creek multi-2D levee** (finding 3): bake the shipped BEC geometry as a
  2nd deck + a `bald_eagle_2d_levee` archetype literal + the SA/2D-connection weir +
  breach params as knobs; the solve/postprocess/mesh-preview spine is reused. BLOCKED
  on Windows-Phase-1 intermediates OR the third-party preprocessor (finding 5).
- **Rain-on-grid 2D** (finding 4): bake a shipped rain-on-grid tutorial deck + a
  precipitation-boundary deck-edit branch; a distinct pluvial QUESTION class.
- **The headless subgrid-table frontier** (finding 5, PARKED for NATE): the strategic
  gate on every real-AOI and multi-2D archetype -- third-party preprocessor vs HEC's
  native-Linux 2025 migration vs Windows-Phase-1 intermediates.
