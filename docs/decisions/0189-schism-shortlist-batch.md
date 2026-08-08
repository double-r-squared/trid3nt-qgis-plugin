# ADR 0189 - SCHISM shortlist batch 5: the parametric-JONSWAP wave-forcing knobs + the 3D baroclinic estuary template

Date: 2026-08-08
Status: accepted

## Context

Two SCHISM rows from the M/L sign-off shortlist (`docs/validation/ml-signoff-shortlist.md`
rows 7-8), on the engine-#12 template surface (ADR 0118/0126/0131/0156):

1. `parametric_spectra_wave_forcing` (FEATURE-M): given a PRESCRIBED offshore
   parametric spectrum (JONSWAP Hs/Tp/direction/spread), what nearshore wave
   transformation + setup results? The `pschism_WWM_GOTM_TVD-VL` binary was built in
   ADR 0131; the `wwminput.nml.spectra` sample lives in the SCHISM tree's
   `sample_inputs/`. This EXTENDS the landed `schism_coupled_waves` template rather
   than adding a tool.
2. `baroclinic_3d_circulation` (ENGINE-adjacent): density-driven 3D circulation /
   stratification in a shelf-estuary. ADR 0126 triaged this landable on the EXISTING
   hydro-core binary (no build). This is a NEW template authoring the 3D pathway.

## What was built

### Row 1 - parametric JONSWAP boundary = KNOBS on `schism_coupled_waves` (registry unchanged)

Per the "prefer knobs over new registrations" discipline, the parametric-spectrum
capability is four composable knobs on the existing template, NOT a new tool:
`significant_wave_height_m`, `peak_period_s`, `mean_direction_deg`,
`directional_spread`. Setting ANY switches the WWM open boundary from the bundled
non-parametric Duck 8m-array spectrum to a PRESCRIBED parametric JONSWAP boundary;
unset knobs fall back to the Duck fixture's own JONSWAP values (backward compatible -
no knobs = the validated observed-event run).

- `deck_authoring._transform_wwm_input_parametric` rewrites the `wwminput.nml` &BOUC
  block: `LBCWA=T` (parametric on), `LBCSP=F` (file spectrum off), `LINHOM=F`
  (uniform), `LBCSE=F` (steady), `IBOUNDFORMAT=1`, `WBSS=2` (JONSWAP, peak period),
  `WBHS/WBTP/WBDM/WBDS` from the knobs. Threaded through `stage_wwm_duck_deck(...,
  wave_forcing=...)`.
- The observed-gauge cross-shore V&V is SKIPPED in parametric mode (the bundled
  gauges record the real 12 Oct 1994 event; comparing a synthetic forcing to that
  transect would be a dishonest V&V). The honesty note is swapped for a parametric
  fidelity floor (`_PARAMETRIC_NOTE_TMPL`).
- Wave SETUP is added to the deliverable: `postprocess_schism.read_wave_setup_from_out2d`
  estimates the radiation-stress super-elevation (shallow-node mean elevation minus
  deep-water mean) from the coupled-run `elevation` field. New `SchismWaveLayerURI`
  fields: `forcing_mode` / `forced_{hs_m,tp_s,dir_deg,spread_deg}` / `wave_setup_m`.
- Pre-existing latent bug FIXED at source: the observed-mode SyntheticInput entries
  carried `basis="published-fixture"`, which is not a valid `basis` literal (the
  contract accepts fetched/user/prompt_interpreted/default_demo/derived) - so the
  observed coupled_waves run had never completed the input-gate stage live. Corrected
  to `basis="default_demo"`.

### Row 2 - `schism_baroclinic_circulation` NEW template (registry 232 -> 233)

The 3D density-driven pathway on the EXISTING hydro-core binary `pschism_TVD-VL` (no
build). `deck_authoring.author_baroclinic_estuary_deck` authors a coarse georeferenced
channel over a US estuary footprint with:

- an SZ vertical grid (`_author_sz_vgrid`, nvrt=10 pure-sigma layers, bed->surface);
- `ibc=0` baroclinic + `ics=2` (lat/lon spherical - the mesh is in geographic
  degrees; the QA fixture's `ics=1` Cartesian would read degrees AS metres and the
  tracer backtracking overflows, `nbtrk > mxnbt`);
- an estuarine salinity-GRADIENT initial condition (fresh landward -> `ocean_salinity_psu`
  seaward) + a sustained freshwater river point SOURCE (`source_sink.in` /
  `vsource.th` / `msource.th`, S=0) - the "river inflow";
- TVD tracer transport (`itr_met=3` + a `tvd.prop`), a tidal-elevation ocean boundary
  (reuses `_author_bctides`), salinity + temperature outputs.

`postprocess_schism.postprocess_schism_baroclinic` reads the scribed 3D `salinity`
netCDF, takes the topmost-valid (surface) and bottommost-valid (bottom) salinity per
node over the spun-up window, rasterizes surface + bottom salinity COGs, and computes
the top-minus-bottom stratification. New contract `SchismBaroclinicLayerURI` +
`SCHISM_SALINITY_STYLE_PRESET`; new solver id `schism_baroclinic_circulation`
registered in `run_schism.py`; co-located `corpus.yaml`; `SCHISM_ARCHETYPES` gains
`baroclinic_circulation`. The mesh + bathymetry are an IDEALIZED coarse
DEMONSTRATION geometry (a graded lattice, linearly-deepening idealized bathymetry) -
loudly labeled, NOT a surveyed estuary.

### Worker image (WORKER-IMAGE LAW)

The strict manifest parser (`services/workers/schism/entrypoint.py`) rejected the
generic run_solver-seam envelope fields `inputs`/`outputs`/`schism_args` that the
local-docker launcher writes verbatim into `rundir/manifest.json` - so no SCHISM
template had ever solved end-to-end THROUGH the seam+image (the ADR 0126/0131 runs
were MinIO-free direct `mpirun`). Added `_SEAM_ENVELOPE_FIELDS` (accept-and-ignore),
bumped the parser version `schism-manifest-1 -> -2`, kept the typo-rejection (the
ADR 0188 HEC-RAS fix, mirrored). Image `trid3nt-local/schism:latest` REBUILT with
absolute -f/context paths (build stage cached, QA analytical gate RMSE=0.0155 m green
in-build); `docker history` carries NO GRACE-2 reference; the parser-v2 + seam
allowlist verified THROUGH the image.

## Live evidence (product path, the rebuilt image)

- Row 2 (baroclinic) end-to-end through run_solver+MinIO: sim_days=1.0, 240 nodes x
  10 layers, wall **72 s**, run `01KZGMMBZ3C8FJ0XVGTDWPPG53`. Surface salinity
  0.97-25.85 psu, bottom max 32.41 psu, stratification **mean 5.19 / max 9.04 psu** -
  a physically-sane salt wedge (fresh surface over salty bottom, salt intruding
  up-estuary).
- Row 1 (parametric wave) end-to-end: sim_hours=1.0, 17054 nodes, wall **674 s** (the
  WWM+GOTM MSC=MDC=12 cost), run `01KZGMX30R0YDQRJA8ZNFWAT15`, parametric JONSWAP
  Hs=4.0/Tp=13/dir=70/spread=25. `forcing_mode=parametric_jonswap`, offshore Hs
  honored (hs_max 4.0 m), nearshore hs_mean 1.22 m (the offshore->nearshore
  transformation), wave_setup -0.036 m. A calm run (Hs=1.5) confirms the knob is
  honored - the nearshore field scales with the offshore forcing (the
  knob-demonstration cross-shore chart).
- Showcase Cases seeded live (`scripts/seed_showcase_cases.py`, dev-tool-invoke,
  reconnect-durable):
  - `schism_baroclinic_circulation`: case `01KZGNQ36SG737MEKVJZCAQMTA`, 3 layers.
    `!run schism_baroclinic_circulation(location_query='Delaware Bay', river_discharge_m3s=800.0, sim_days=1.0)`
  - `schism_coupled_waves` (parametric):
    `!run schism_coupled_waves(significant_wave_height_m=4.0, peak_period_s=13.0, mean_direction_deg=70.0, directional_spread=25.0, sim_hours=0.5)`

## Proofs (docs/proof/templates/)

- `schism_baroclinic_circulation_surface_salinity.png` / `_bottom_salinity.png`:
  surface + bottom salinity over Esri World Imagery (Delaware Bay footprint), fresh
  river-end grading to salty ocean-end; white box = AOI only.
- `schism_baroclinic_circulation_stratification_chart.png`: surface vs bottom
  salinity along the estuary axis (the salt-wedge stratification band).
- `schism_baroclinic_circulation_mesh.png`: the coarse estuary channel grid.
- `schism_coupled_waves_parametric_hs.png`: nearshore Hs (parametric Hs=4.0) over
  Esri at the Duck FRF (georeferenced via the mesh's lon/lat twin hgrid.ll).
- `schism_coupled_waves_parametric_knob_chart.png`: cross-shore Hs for Hs=4.0 (storm)
  vs Hs=1.5 (calm) - the knob VISIBLY changes the nearshore field.
- `schism_coupled_waves_parametric_mesh.png`: the Duck FRF unstructured mesh.

## Offline coverage (green, `env -u TRID3NT_CACHE_BUCKET ... --timeout`)

- `server/tests/test_schism_baroclinic.py` (NEW, 9 tests): contract shape, deck
  authoring (SZ vgrid + ibc=0/if_source=1 + gradient IC + river source),
  stratification postprocess (synthetic 3D nc), registration pin.
- `server/tests/test_schism_coupled_waves.py` (+4): parametric &BOUC transform,
  parametric deck staging, `_resolve_wave_forcing` validation, wave-setup reader.
- `services/workers/schism/test_entrypoint_manifest.py` (+1): seam-envelope fields
  accepted; typo still rejected.
- Pins: `test_door_dissolution` (EXPECTED_TEMPLATES + `schism_baroclinic_circulation`),
  `test_catalog_surfacing` (registry 232 -> 233), existing `test_schism_landing` /
  `test_schism_transport_validation` unchanged-green.
- Model-free retrieval: `schism_baroclinic_circulation` top-8 for all four
  representative baroclinic-estuary queries; `schism_coupled_waves` top-8 for the
  parametric-spectrum + wave-setup queries.

## Consequences

- Coded-tools metric: **+1 registered template** (`schism_baroclinic_circulation`),
  registry 232 -> 233, EXPECTED_TEMPLATES 74 -> 75. Row 1 added ZERO tools (four
  composable knobs on `schism_coupled_waves`).
- The worker image gained NO binary (both rows ride binaries already baked); the only
  worker-code change is the manifest allowlist. SCHISM now solves end-to-end THROUGH
  the run_solver seam + image for the first time (the latent allowlist gap the
  MinIO-free ADR 0126/0131 runs never exercised).
- Board rows `parametric_spectra_wave_forcing` and `baroclinic_3d_circulation` ->
  LANDED with the live evidence above.
- No flood seam touched -> no flood canary mandated.

## Open issues / deferred

1. **NATE-GATED**: the calibrated `baroclinic_3d_circulation` validation - SCHISM
   Test_CORIE Columbia River estuary 28-day 3D baroclinic hindcast vs ADCP/CTD
   stations (~600 MB deck, hours-class wall, nvrt=54). My live acceptance is the
   coarse spin-up smoke ONLY (a screening geometry proving the pathway executes +
   emits sane stratification); the full CORIE V&V is the NATE-remote-drive class run
   (ADR 0126 sec 2c), recorded here as pending.
2. The coarse baroclinic mesh is a graded rectangle over the estuary bbox (not
   clipped to the real shoreline) with idealized deepening bathymetry - a screening
   demonstration geometry. A surveyed-bathymetry coastal_tin baroclinic deck (DEM +
   oceanmesh) is the refinement, gated behind the same shoreline/oceanmesh deps as
   the tidal_hydro coastal_tin path.
3. Wave setup on the Duck geometry reads small / slightly negative (the FRF domain is
   largely offshore of the surf zone at this forcing); a dedicated surf-zone setup
   V&V would need a shallower breaking-zone AOI.
