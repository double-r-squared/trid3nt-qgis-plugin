# ADR 0154 - TELEMAC CAND-S rows: wind-stress knob fold, GAIA supply already-covered, cross-engine overlap doctrine, two honest STOPs

Date: 2026-08-05
Status: accepted

## Context

Nine TELEMAC-tagged CAND-S board rows were queued: three PHYSICS rows
(wind_stress_forcing, instantaneous_dam_breach_flood_wave,
upstream_sediment_supply_boundary), two MACHINERY/REGRESSION rows
(sisyphe_gaia_migration_path, micropollutant_fate_validation_case), and four
CROSS-ENGINE OVERLAP-GUIDANCE rows (overlap_wwm_vs_swan,
overlap_gaia_vs_sed3d_vs_hecras_sediment, overlap_waqtel_vs_icm_vs_swmmwq,
overlap_rain_on_grid_hecras_swmm_sfincs). All S labels are hypotheses under the
triage-first law.

Triage against the installed engine (opentelemac v9.0 in
`trid3nt-local/telemac:latest`) established, before any build:

1. The dictionaries ship in the image (`sources/*/telemac2d.dico`, `gaia.dico`,
   `waqtel.dico`, ...), so keyword syntax is verifiable in-image. But the
   **examples tree does NOT ship** (`$HOMETEL/examples` is absent; only
   `builds/configs/scripts/sources`). No shipped validation steering file, no
   Malpasset geometry, no sisyphe/gaia/waqtel case decks are present.
2. The TELEMAC surface is ONE archetype composer `telemac_river_dye` +
   the worker `telemac_river_dye_build.py`, which AUTHORS meshes/decks from
   fetched US NHDPlus reaches + Copernicus DEM. It does NOT ingest external
   `.cas`/geometry decks. Substance modes already live: dye tracer, oil spill,
   WAQTEL first-order decay (process 17), and **GAIA v1 SUPPLY-LIMITED suspended
   sediment** (`LAYERS INITIAL THICKNESS = 0`, source concentration prescribed
   upstream).
3. WIND is fully supported in telemac2d.dico v9.0 (`WIND`, `OPTION FOR WIND`,
   `WIND VELOCITY ALONG X/Y`, `COEFFICIENT OF WIND INFLUENCE` default 1.55E-6,
   `THRESHOLD DEPTH FOR WIND` default 1) and is orthogonal forcing (rides any
   substance class) - a clean knob fold, no new tool.

## Decision (per row)

**Row 1 wind_stress_forcing - LANDED (knob fold, 0 new tools).** Wind-stress
forcing folds onto `telemac_river_dye` as `wind_speed_mps` + `wind_direction_deg`
(meteorological FROM-bearing). The worker `author_deck` emits a constant-wind
block (`WIND = YES`, `OPTION FOR WIND = 1`, X/Y velocity components resolved into
the UTM frame, `THRESHOLD DEPTH FOR WIND = 1.`) ONLY when speed > 0; unset (0.0)
leaves every deck BYTE-IDENTICAL (pinned by test). Keywords verified against
telemac2d.dico v9.0. WORKER-IMAGE LAW honored: image rebuilt; live smoke below.

**Row 3 upstream_sediment_supply_boundary - DOC / already-covered.** The
existing GAIA v1 path IS the "prescribed upstream sediment supply vs initial bed
stock / reservoir-inflow sedimentation" question: supply-limited, no bed stock,
deposition from an upstream source load. No worker change; corpus queries +
docstring make it retrievable under that framing (model-free retrieval HITs
confirmed).

**Row 2 instantaneous_dam_breach_flood_wave - STOP (recipe).** The canonical
form is an initial free-surface DISCONTINUITY (two water levels), which in
TELEMAC-2D requires a user-fortran `CONDITIONS INITIALES = 'PARTICULAR'` CONDIN
that the worker does not author or compile (it only writes `CONSTANT DEPTH`). The
canonical published validation geometry (Malpasset) is non-US and outside our
US-fetcher surface (US-only doctrine). A breach-hydrograph proxy (sudden upstream
discharge surge via a `LIQUID BOUNDARIES FILE`) is authorable but risks the
`SUPERCRITICAL ENTRY WITH FREE DEPTH` instability the GAIA work already hit on
Q-prescribed inflow, and is a weaker posing than the true IC step. Recipe: add a
CONDIN fortran authoring+compile path (or vendor a US dam-break benchmark
geometry) + a time-varying breach-hydrograph boundary writer, then smoke for
front arrival / attenuation.

**Row 4 sisyphe_gaia_migration_path - DOC / STOP.** TRID3NT authors GAIA decks
natively (worker `write_gaia_deck`, GAIA v1 live) and standardizes on GAIA; it
has NO legacy-SISYPHE-deck ingestion path, so a SISYPHE->GAIA steering-block
translator serves no internal workflow. Decision: standardize on GAIA (status
quo). Recipe if external-deck ingestion is ever added: parse SISYPHE
`.cas` sediment keywords -> GAIA keyword map against gaia.dico v9.0.

**Row 5 micropollutant_fate_validation_case - STOP (recipe).** The row's premise
is a golden regression "using the shipped validation steering file". That file
(`examples/waqtel/waq2d_micropol/micropol_steer.cas`) does NOT ship in our image
(no examples tree, verified twice) and the worker ingests no external decks. The
premise fails. Recipe: vendor the micropol deck + its geometry (.slf/.cli) into
the image and add a shipped-deck-runner path (distinct from the
authored-from-US-reach pipeline), then pin the WAQ MICROPOL validation numbers as
a regression.

**Rows 6-9 overlap_* - DOC-class fidelity doctrine.** These are cross-engine
"which engine wins when" questions, NOT simulations. Guidance landed in the
adapter system prompt's "Cross-engine OVERLAP routing" block (in-context every
turn), grounded in what TRID3NT ACTUALLY ships (verified against the real
surfaces):
- WAVES: `schism_coupled_waves` is SCHISM+WWM (tight coupling); `swan_wave_field`
  is standalone SWAN. TRID3NT surfaces WWM ONLY inside SCHISM - no standalone WWM.
- SEDIMENT: the GAIA sediment mode of `telemac_river_dye` is the surfaced
  morphodynamic path. SED3D (EPA) is archived/defunct (verified). HEC-RAS 2D
  sediment is NOT surfaced (only HEC-RAS hydraulics).
- WATER QUALITY: SWMM-WQ = urban catchment->pipe->outfall; WAQTEL (telemac decay
  mode) = receiving-water fate. ICM (commercial InfoWorks) is NOT in TRID3NT.
- RAIN-ON-GRID: three-tier ladder - SFINCS (fast screening) / HEC-RAS 2D
  (refinement) / SWMM (when the drainage NETWORK is the object).

## Live smoke (row 1)

Two solves through the real `run_solver(solver='telemac_river_dye')` local-docker
seam, Eel River near Scotia CA (~2.5 km, 1059 nodes / 1778 elements, ~20 m):
baseline (no wind) and 18 m/s wind FROM 270 deg. Both reached CORRECT END OF RUN
(`correct_end=True`), ~30 s solver wall each (~65 s incl. fetch+mesh). The wind
echo (`wind_speed_mps=18.0`, `wind_dir_from_deg=270.0`) rode manifest ->
ReachConfig -> deck -> metrics. Wind setup (wind - baseline free-surface): a
monotonic tilt of range 7.35 cm - ~7 cm setdown on the upwind end converging to
baseline downwind, the textbook wind setup/setdown direction.

Proofs (docs/proof/templates/): `telemac_wind_stress_forcing_chart.png`
(dock-interpreter overlay, baseline vs wind WSE along the wind axis),
`telemac_wind_stress_forcing.png` (setup field over Esri World Imagery, white AOI
box), `telemac_wind_stress_forcing_mesh.png` (raw triangulation).

## Consequences

- Registry unchanged at 216; tier=template EXPECTED_TEMPLATES unchanged at 58
  (wind is a knob on the existing template; the doc/STOP rows add no tools).
- Worker image `trid3nt-local/telemac:latest` REBUILT (author_deck wind block +
  ReachConfig wind fields + metrics echo). Byte-identical guarantee preserved for
  every non-wind run.
- Two honest STOPs recorded with recipes (rows 2, 5) and one honest
  standardize-on-GAIA decision (row 4); one already-covered doc disposition
  (row 3); one cross-engine doctrine landing (rows 6-9).
