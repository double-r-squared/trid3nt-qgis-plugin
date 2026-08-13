# ADR 0241 - TELEMAC-3D stratified/3D-hydrodynamics engine leg: local-first physics proof + productionization recipe

Status: COMPLETE 2026-08-13 (wave 2 productionization landed - see "## Completion"
below). Physics PROVEN local-first (wave 1), then the registered engine leg built
+ live: worker `telemac3d_build.py` + composer `telemac3d_stratified_flow` +
entrypoint `manifest['stratified']` mode + solver `telemac3d_strat` + image
rebuild with build-time smoke; three discriminants reproduce through the baked
copy; LIVE over Lake Superior (real NOAA lake-datum bathy) through the daemon.
Board TELEMAC-3D rows -> LANDED/COVERED. Both blocked STOPs (AED2 lake ecology,
coastal dune migration) drop to SINGLE-blocker.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD carries a TELEMAC-3D section (3 CAND rows) plus two
STOP rows elsewhere that are BLOCKED on the absence of a 3D path
(`aed2_lake_ecology_coupling` ADR 0234, `coastal_dune_migration_3d_coupling`
ADR 0240) and a 3D-framed WAQTEL row (`thermal_budget_heat_exchange`). TELEMAC-3D
is the three-dimensional (hydrostatic or non-hydrostatic) Navier-Stokes solver
with active-tracer (temperature/salinity) baroclinic coupling - the physics 2D
depth-averaging structurally cannot resolve. It is the highest-leverage remaining
TELEMAC front: it is the one genuinely NEW SOLVER LEG in the family, and it is
the prerequisite the two blocked STOPs name explicitly.

Pivotal finding (the STOP-vs-build gate): **the telemac3d binary is already
baked** in `trid3nt-local/telemac:latest`. The conda-forge
`opentelemac=v9.0.0` package that supplies the shipped TELEMAC-2D/GAIA/WAQTEL/
TOMAWAC/ARTEMIS legs also ships the 3D suite:
`/opt/conda/opentelemac/builds/gnu.shared/bin/telemac3d`, `telemac3d.py` on PATH,
`sources/telemac3d/telemac3d.dico`, `sources/telemac3d` (so a `FORTRAN FILE` user
routine compiles at run time - used for the initial stratification field), and
crucially both `libgaia4telemac3d.so` AND `libwaqtel4telemac3d.so` are compiled
in - i.e. the GAIA-3D and WAQTEL-3D couplings the two STOPs need are already
linked, not a rebuild.

Because the binary exists, this is NOT a STOP-RECIPE (unlike the SCHISM
iharind/USE_HA or HEC-RAS WQ walls). The solve path is real. What is NOT built
is the TRID3NT productionization layer (composer + registered tool + showcase +
image bookkeeping). The conda package ships no `examples/` tree, so decks were
authored from the dico keyword reference + published 3D hydrodynamics,
replicating the physics of the classic TELEMAC-3D validation set
(lock-exchange, wind-driven closed basin, thermal stratification) - geography-free
idealized verification that clears the citations law like the GWE / TOMAWAC
analytic V&V.

## The board rows -> question classes -> proven physics

| board row(s) | question class | discriminating proof (in-image) |
|---|---|---|
| `salinity_intrusion_estuary` [L] | density-driven flow / salt wedge | lock-exchange gravity current: dense saline column released -> bottom current advancing at the Benjamin front speed; density-ON produces a current, OFF does not |
| `dam_break_3d_cross_check` [L] | hydrostatic vs non-hydrostatic | same lock-exchange run hydrostatic vs non-hydrostatic: non-hydrostatic front is measurably faster (0.396 vs 0.386 Fr) |
| (folds; the marquee 3D discriminant) | 3D vertical velocity structure / wind-driven circulation | wind-driven closed basin: surface downwind + bottom upwind return flow, depth-integrated ~0 (what a 2D model returns everywhere) |
| `thermal_stratification_reservoir` [M] | thermal stratification / lake turnover | warm-over-cold thermocline: calm run KEEPS the gradient (dT 5.4 C), windy run MIXES it away (dT 0.013 C) |
| `thermal_budget_heat_exchange` [M] (WAQTEL) | receiving-water thermal budget | stratification substrate PROVEN; WAQTEL THERMIC atmospheric heat-exchange coupling is the completion-wave addition (waqtel4telemac3d.so is baked) |

Sigma-layer mesh mechanics (`NUMBER OF HORIZONTAL LEVELS` = NPLAN, `MESH
TRANSFORMATION` = 1 sigma) are exercised by every case - the vertical
discretisation IS the 3D degree of freedom; all cases run 11-15 planes.

## Live numbers (through the baked telemac3d binary, single-core, ncsize=1)

Canonical results JSON: `docs/proof/templates/telemac3d_physics_direct_result.json`.

- **Lock-exchange gravity current** (L=16 m channel, H=1 m, salinity S=26.7 ->
  drho/rho = 750e-6*S = 0.0200, g' = 0.196 m/s2; low friction; 40 s). Benjamin
  energy-conserving front speed 0.5*sqrt(g'H) = 0.222 m/s (Fr=0.5). Measured
  bottom-front speed: **hydrostatic 0.171 m/s (Fr 0.386), non-hydrostatic 0.175
  m/s (Fr 0.396)** - front propagates LINEARLY (the constant-speed gravity-current
  signature), non-hydrostatic faster (the dam-break-3D fidelity rung). The ~0.39
  measured Froude number sits just under the ~0.44-0.50 lab/DNS energy-conserving
  band (Shin-Dalziel-Linden 2004; Haertel 2000); the residual gap is the known
  hydrostatic under-prediction plus coarse-grid interfacial numerical mixing
  (dx 0.25 m, 13 planes) - refinement closes it but was not needed for the
  discriminating result.
- **Wind-driven closed-basin circulation** (L=5 km, H=10 m, steady 10 m/s wind
  along +X, 3 h to seiche-damped steady state, 11 planes). Mid-basin vertical
  U profile: **surface +0.041 m/s (downwind), bottom -0.041 m/s (upwind return),
  depth-average -0.002 m/s ~ 0** - the two-layer wind gyre. THE 3D-vs-2D
  discriminant: a 2D depth-averaged shallow-water model returns ~zero velocity
  everywhere in a closed basin; the vertical structure is invisible to it.
- **Thermal stratification vs wind mixing** (L=4 km, H=20 m, warm 25 C
  epilimnion over 8 m thermocline, cold 15 C hypolimnion, DENSITY LAW 1
  freshwater rho max near 4 C, 5 h, 15 planes). Initial top-to-bottom dT = 10 C.
  **Calm (no wind): dT_final 5.40 C - thermocline persists. Windy (12 m/s):
  dT_final 0.013 C - fully mixed** (~400x separation). The stratified 3D water
  column the AED2 lake-ecology STOP requires. (Honest note: the calm case
  diffuses from 10 -> 5.4 C over 5 h under the mixing-length closure + the set
  1e-4 vertical tracer diffusivity; a lower background diffusivity sharpens
  persistence, but calm-retains-strong-gradient vs windy-destroys-it is already
  unambiguous.)

Proof charts (additions only, never cleaned):
`docs/proof/templates/telemac3d_{lock_exchange_gravity_current,wind_driven_circulation,thermal_stratification}_chart.png`.
Sandbox driver preserved at `docs/proof/templates/telemac3d_sandbox.py` (the
canonical composer prototype).

## Load-bearing deck findings (the TELEMAC-3D gotchas)

Discovered by running the binary (offline-green != correct-physics). Any T3D
composer MUST bake these:

1. **`INITIAL VALUES OF TRACERS` is mandatory when NTRAC>0** even when
   USER_CONDI3D_TRAC overrides the field. Omitting it PLANTEs at read-time with
   `GIVE THE KEY-WORD INITIAL VALUES OF TRACERS FOR ALL TRACERS`. Set a scalar
   placeholder (`: 0.`); the fortran hook then paints the real IC.
2. **Tracer-name -> physics-index detection is by NAME PREFIX, not order.**
   `lecdon_telemac3d.F` sets `IND_T=I` iff `NAMETRAC(I)(1:11)=='TEMPERATURE'` and
   `IND_S=I` iff `NAMETRAC(I)(1:7)=='SALINIT'`. The tracer used by DENSITY LAW
   1/2 MUST be named with that exact leading substring or the baroclinic coupling
   silently sees no temperature/salinity (density stays uniform, no gravity
   current). Pad the 32-char SERAFIN name: `'TEMPERATURE     '`, `'SALINITY        '`.
3. **Density-law algebra (`drsurr.f`), so g' is predictable:** DENSITY LAW 1 ->
   rho = rhoref*(1 - 7*(T-4)^2 * 1e-6) (freshwater max density at 4 C - this IS
   the stratification physics); DENSITY LAW 2 -> rho = rhoref*(1 + 750*S*1e-6),
   so drho/rho = 750e-6 * S (S in the tracer's units); DENSITY LAW 3 -> both;
   DENSITY LAW 0 -> barotropic (the density-OFF half of the discriminating pair).
4. **The 3D initial field is set in USER_CONDI3D_TRAC, and X/Y/Z are the NPOIN3
   3D node coordinates** (`X/Y/Z => MESH3D%X/Y/Z%R` in `point_telemac3d.f`), NOT
   the 2D arrays. `Z` is a bed-referenced ELEVATION (surface at still-water 0, so
   depth-below-surface = -Z). CALCOT populates Z BEFORE the tracer hook is called
   (condim.f: `IF(DEBU) CALL CALCOT` at ~line 197 precedes `CALL
   USER_CONDI3D_TRAC` at ~251), so keying an IC off Z(I3) is safe on a fresh run.
   3D node numbering is `(iplan-1)*NPOIN2 + j`, iplan 1=bed .. NPLAN=surface.
5. **The boundary file is a 2D (T2D 13-column) `.cli`,** which T3D reads as the
   HORIZONTAL boundary and extrudes over the planes; the bed/surface are governed
   by keywords, not by the .cli. A closed basin is LIHBOR=LIUBOR=LIVBOR=LITBOR=2
   everywhere (every proof case is a closed initial-value problem - no liquid
   boundary needed for a gravity current, a wind gyre, or a stratified column).
6. **`NON-HYDROSTATIC VERSION` defaults to YES** in telemac3d.dico (unusual - most
   users assume hydrostatic). Set it explicitly per case; it is the
   `dam_break_3d_cross_check` discriminating knob.
7. **`MEAN TEMPERATURE` is NOT a telemac3d keyword** (guessed; the pre-run
   `Checking keyword/rubrique coherence` gate hard-rejects unknown keywords - a
   useful strict gate, the same discipline the worker's spec parsers enforce).
   `AVERAGE WATER DENSITY` IS valid (rhoref for the density law).
8. **Vertical structure needs the vertical turbulence closure.** `VERTICAL
   TURBULENCE MODEL` (ITURBV) default 2 (mixing length) gives the wind-shear
   entrainment that mixes the thermocline and the log-like drift profile; ITURBV
   1 (constant) is cleaner for the low-mixing lock-exchange front. `HORIZONTAL
   TURBULENCE MODEL` 1 (constant viscosity) is fine for these idealized boxes.

Deck skeleton (English keywords) is fully worked in the sandbox: GEOMETRY /
BOUNDARY CONDITIONS / 3D + 2D RESULT FILE, NUMBER OF HORIZONTAL LEVELS, MESH
TRANSFORMATION, NON-HYDROSTATIC VERSION, INITIAL CONDITIONS 'CONSTANT ELEVATION'
+ INITIAL ELEVATION, NUMBER/NAMES/INITIAL VALUES OF TRACERS, DENSITY LAW,
AVERAGE WATER DENSITY, WIND + WIND VELOCITY ALONG X/Y + COEFFICIENT OF WIND
INFLUENCE, HORIZONTAL/VERTICAL TURBULENCE MODEL, the H/V diffusion coefficients,
LAW OF BOTTOM FRICTION + FRICTION COEFFICIENT, MASS-BALANCE. 3D output var
mnemonics: `Z,U,V,W` + tracer `TA1`; SERAFIN names `VELOCITY U/V/W`,
`ELEVATION Z`, `TEMPERATURE`/`SALINITY`.

## Sandbox (local-first, not committed to services/)

`docs/proof/templates/telemac3d_sandbox.py` (the canonical composer prototype).
Runs INSIDE the image
(`docker run -v <dir>:/data trid3nt-local/telemac:latest python .../telemac3d_sandbox.py <mode>`),
builds a regular-grid 2D triangular mesh (CCW ring, rank IPOBO - the same scaffold
the river-dye/TOMAWAC composers use), writes SELAFIN geometry + 2D `.cli` via the
TelemacFile API, authors the `.cas` + a `USER_CONDI3D_TRAC` fortran for the
non-uniform initial stratification / lock gate, runs `telemac3d.py --ncsize=1`,
reads the 3D SELAFIN (reshaping NPOIN3 by plane). Modes: `smoke`, `lock`
(hydro+nonhyd), `wind`, `thermal` (calm+windy), `all` (writes the canonical JSON).
Per-case wall time ~1-4 min single-core.

## Productionization recipe (the remaining, unbuilt engine leg)

The solve path exists; landing a registered capability is a normal worker-leg
build on the two-wave rhythm (this ADR is wave 1). Wave 2:

1. A `telemac3d_build.py` worker leg (parser e.g. `telemac3d-strat-1`, strict-field
   gate) baked into `trid3nt-local/telemac:latest`, mirroring `tomawac_build.py` /
   `artemis_build.py`. It authors the 3D deck + the USER_CONDI3D_TRAC IC fortran
   from a US site (real lake/reservoir/estuary bathymetry via our fetchers - NGDC
   lake-datum DEMs for the Great Lakes, 3DEP+NHD for reservoirs) on the same
   mesh front the 2D composers share.
2. ONE registered `telemac3d_stratified_flow` tool (engine=telemac, tier=template)
   with modes folding the question classes: `stratification` (thermal, calm/windy
   knob), `salinity_intrusion` (density-driven, tidal-forcing knob),
   `wind_circulation` (3D velocity structure), `dam_break_3d` (hydro/non-hydro
   fidelity rung). tool_query_corpus.yaml queries + a model-free
   retrieve_visible_tools(prompt,None,8) check BEFORE acceptance (new-tool law).
3. Showcase seed + live daemon E2E over a real US lake/reservoir, discriminating
   pair asserted through the image; flood-canary unaffected (additive leg).

## STOPs this unlocks (and what their completion additionally needs)

- **`aed2_lake_ecology_coupling`** (ADR 0234 STOP, DOUBLY BLOCKED). Blocker (1)
  "no TELEMAC-3D worker path exists (2D only), and AED2 is not available in 2D"
  is REMOVED by this substrate: a stratified 3D lake is proven, and
  `libwaqtel4telemac3d.so` is baked. Blocker (2) REMAINS: AED2 is an external
  library driven by vendored `.nml` steering decks (`AED2 STEERING FILE`,
  `AED2 PHYTOPLANKTON|ZOOPLANKTON|PATHOGEN|BIVALVE STEERING FILE` in waqtel.dico)
  - completion needs the aed2.nml + config decks vendored into the image + a
  shipped-deck-runner path (distinct from the authored-from-site pipeline), then
  the WAQTEL-3D + AED2 coupling wired onto the stratification deck.
- **`coastal_dune_migration_3d_coupling`** (ADR 0240 STOP). Blocker "no 3D
  hydrodynamic deck author exists" is REMOVED (this substrate + baked
  `libgaia4telemac3d.so` = GAIA-3D coupling is linked). Completion additionally
  needs: a marine/estuary domain (vs the closed-basin idealizations here), a
  residual-current/wave-current forcing (TOMAWAC ADR 0236 supplies the wave side;
  wave-current residual coupling is the new wiring), then GAIA multi-class
  sediment (LANDED 2D via ADR 0240) coupled to the 3D residual flow.

## Consequence

- One new solver family is physics-proven and de-risked; the TELEMAC engine now
  spans 2D hydrodynamics + GAIA sediment + WAQTEL WQ + TOMAWAC spectral waves +
  ARTEMIS mild-slope + **3D baroclinic/stratified hydrodynamics**, all through the
  one baked image, zero new dependencies.
- Two STOP rows downgrade from "blocked on a nonexistent 3D path" to "one
  vendoring/wiring wave away" - recorded on their board rows.
- Board TELEMAC-3D rows annotated PHYSICS-PROVEN (not COVERED - no registered
  tool yet), per the two-wave rhythm.

## Completion (wave 2) - COMPLETE 2026-08-13

The productionization layer is BUILT and LIVE. The registered engine leg mirrors
the tomawac (0236) / artemis (0237) completions structurally.

- **Worker leg + composer**: `services/workers/telemac/telemac3d_build.py`
  promotes the sandbox verbatim (all 8 gotchas baked); `Telemac3dConfig` +
  `solve()` dispatch by `flow_mode` (stratification / wind_circulation /
  salt_wedge). The worker reduces the 3D result to surface + bottom single-frame
  2D SELAFINs (the artemis re-emit pattern) + computes the discriminant scalars +
  the vertical-profile chart off the full 3D column. Entrypoint routes a
  `manifest['stratified']` block -> `run_telemac3d_pipeline`; strict parser
  `telemac3d-strat-1` + typed `Telemac3dManifestUnknownFieldsError` rejection.
  A NOAA Great Lakes real-bathy path (all-wet clamp to `min_depth_m`) serves the
  stratification / wind modes; salt_wedge stays idealized (analytic lock-exchange
  V&V; a real estuary needs a tidal liquid boundary).
  Agent-side `postprocess_telemac3d` (in `postprocess_telemac.py`) rasterizes the
  surface (primary) + bottom (context) layers to two 4326 (real) / placeholder-
  frame (idealized) COGs, folding the worker's typed discriminant scalars onto
  the `Telemac3dLayerURI` (invariant 1). Composer tool `telemac3d_stratified_flow`
  (engine=telemac, tier=template): target_resolution_m + 0225 ResolutionSpec +
  0232 grid coarsen; LOUD labeled defaults through the 0231 input-review gate.
- **Registration**: corpus.yaml (12 phrasings, model-free retrieval top-8 on all
  4 probe prompts) + categories (`simulation_modeling` primary, `hydrology` +
  `coastal` cross-list) + EXPECTED_TEMPLATES 250->251. Solver `telemac3d_strat`
  registered in both registries. `Telemac3dLayerURI` +
  `TELEMAC3D_STRATIFICATION_STYLE_PRESET` contracts. Image rebuilt with a
  build-time smoke (Telemac3dConfig map + strict-gate + telemac3d binary/dico/
  sources presence).
- **Through-image mode solves** (the three discriminants reproduce through the
  baked copy, ncsize=1):
  - salt_wedge: hydrostatic front 0.1710 m/s vs non-hydrostatic 0.1755 m/s
    (non-hydro FASTER - the dam-break-3D fidelity rung).
  - wind_circulation (idealized): u_surface +0.043 / u_bottom -0.040 /
    depth-avg +0.0003 (~0) - the two-layer wind gyre.
  - stratification (idealized calm): dT_final 6.25 C (from 10 C IC - the
    thermocline persists).
- **LIVE at a real US lake (Lake Superior, NOAA lake-datum bathy, depths to
  ~320 m, UTM 16N, 15 sigma planes)** through the daemon, seeded via the product
  `!run` path (`scripts/seed_showcase_cases.py --only telemac3d`, 2 Cases, each
  publishing surface + bottom COGs):
  - wind_circulation @ 12 m/s: mid-basin vertical U OPPOSES with depth -
    u_surface ~ +0.048 / u_bottom ~ -0.019 m/s, depth-avg ~0 (the 3D-vs-2D
    discriminant over REAL bathymetry).
  - stratification calm: persisting surface-vs-bottom temperature structure
    (surface ~18 C over bottom ~16 C, dT ~3 C) + the vertical-profile chart.
  HONEST FINDING (baked into the showcase notes): over a 320 m-deep lake the
  calm-vs-windy pair does NOT discriminate in a short sim - a 14 m/s wind cannot
  overturn a 320 m column in hours (calm dT 3.03 vs windy 3.32, physically
  correct). The calm-vs-windy DESTRUCTION discriminant lives in the SHALLOW
  idealized basin (dT 6.25 calm); over a deep real lake the discriminant is the
  persisting stratification + the vertical profile (a structure a 2D
  depth-averaged model has no representation of). The flood canary is unaffected
  (additive leg, no shared seam touched).

Board TELEMAC-3D rows -> LANDED / COVERED (registered tool + live proof).

## STOP-unlock accounting (updated 2026-08-13)

Both STOPs this substrate named are now **single-blocker** (the 3D-path blocker
is fully removed by the registered leg, not just physics-proven):

- **`aed2_lake_ecology_coupling`** (ADR 0234): the "no TELEMAC-3D worker path"
  blocker is GONE (a registered stratified-3D lake leg exists,
  `libwaqtel4telemac3d.so` baked). SINGLE remaining blocker: vendor the aed2.nml
  + phyto/zoo/pathogen/bivalve config decks + a shipped-deck-runner, then wire
  WAQTEL-3D + AED2 onto the stratification deck.
- **`coastal_dune_migration_3d_coupling`** (ADR 0240): the "no 3D hydrodynamic
  deck author" blocker is GONE (registered 3D author + `libgaia4telemac3d.so`
  baked). SINGLE remaining blocker: a marine/estuary domain + wave-current
  residual forcing (TOMAWAC 0236 supplies the wave side) coupling GAIA (LANDED
  2D) to the 3D residual flow.
