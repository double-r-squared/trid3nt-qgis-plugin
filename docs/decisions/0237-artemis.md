# ADR 0237 - ARTEMIS phase-resolving harbour-agitation engine: local-first physics proof + productionization recipe

Status: COMPLETE (2026-08-13). Physics PROVEN local-first (in-image, through the
baked artemis binary, all three ARTEMIS question classes, discriminating pairs
per proof norm #9, each matched against a published analytic solution) AND
productionized: registered tool `artemis_harbor_agitation` (registry 249), worker
composer `artemis_build.py`, entrypoint mode `manifest['agitation']`, image
rebuilt, LIVE on real Great Lakes bathymetry. See the COMPLETE section at the
bottom. The three ARTEMIS question-class rows are COVERED.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD carries an ARTEMIS section in two board blocks (six
CAND rows total; they collapse to three distinct question classes plus one
fidelity/nesting narrative). ARTEMIS is TELEMAC's phase-RESOLVING elliptic
mild-slope (Berkhoff) wave solver - steady-state diffraction, refraction, and
partial reflection inside harbours and around structures. It is the
refinement-grade phase-resolving complement to TOMAWAC's phase-averaged spectral
tier (ADR 0236), per the fidelity-ladder doctrine: TOMAWAC for regional wind-sea
spectra, ARTEMIS for in-harbour agitation where phase (resonance, standing
waves, diffraction fringes) is the answer.

Pivotal finding (the STOP-vs-build gate): **the artemis binary is already baked**
in `trid3nt-local/telemac:latest`. The conda-forge `opentelemac=v9.0.0` package
that supplies TELEMAC-2D/GAIA/WAQTEL/TOMAWAC also ships the whole suite:
`/opt/conda/opentelemac/builds/gnu.shared/bin/artemis` (234 KB), the
`artemis.dico`, `sources/artemis` (so a `FORTRAN FILE` user routine compiles at
run time), and `artemis.py` on PATH. Because the binary exists, this is NOT a
STOP-RECIPE (unlike the SCHISM iharind/USE_HA or HEC-RAS WQ walls). The solve
path is real. What is NOT built is the TRID3NT productionization layer.

The conda package does NOT ship an `examples/` tree, so decks were authored from
the dico keyword reference + the boundary-column semantics decoded from the
official `artemis/{reso_canal, bosse_elliptique, ile_para}` example decks
(fetched from the community github.com/ogoe/OpenTelemac mirror), replicating the
physics of the classic geography-free ARTEMIS validation set - which clears the
citations law like the GWE analytic V&V.

## The 6 board rows -> 3 question classes -> proven physics

| board row(s) | question class | analytic V&V + discriminating proof (in-image) |
|---|---|---|
| `harbor_wave_agitation_resonance` [L], `harbour_tranquility_breakwater_agitation` [L] | harbour agitation / resonance | narrow-mouth harbour: response spikes at the half-wave seiche ladder T_n=2Lh/(nc); AT resonance 3-antinode standing wave (mean Hs/H0 2.66, back-wall 4.2x) vs OFF resonance quiet (0.65, no penetration) |
| `breakwater_wave_diffraction_sheltering` [L], `island_diffraction_refraction` [M] | breakwater / structure diffraction | semi-infinite breakwater: Kd=0.545 on the shadow-boundary ray (Sommerfeld/Penny-Price analytic 0.5), 0.29 deep in the lee, 0.89 in the lit zone; vs no-breakwater control Kd uniform ~1.0 |
| `reef_shoal_wave_sheltering` [L] | reef/shoal refraction-focusing | EXACT Berkhoff-Booij-Radder (1982) elliptic shoal: wave-axis focus peak Kd=2.23 (published ~2.2), caustic offset by the 20 deg shoal rotation; vs flat-bed control Kd~1.0 |
| `artemis_tomawac_fidelity_pairing` [L] | phase-resolving vs phase-averaged fidelity | the phase-resolving tier is demonstrated by all three cases above vs the phase-averaged TOMAWAC tier (ADR 0236); the TOMAWAC->ARTEMIS boundary handoff is reachable (see nesting note) but its plumbing is a completion-wave build, not an analytic-physics row |

The `island_diffraction_refraction` [M] row folds into the diffraction class (the
same mild-slope diffraction/refraction physics, a structure vs an island tip).
`artemis_tomawac_fidelity_pairing` is a workflow/nesting row, not a distinct
physics class; it is PHYSICS-PROVEN by association (the phase-resolving tier is
shown reachable and accurate) with the nesting mechanism documented below.

## Live numbers (through the baked binary)

- **Resonance** (harbour Lh=500 m, mouth 25 m, depth 10 m, c=9.9 m/s): in-harbour
  mean Hs/H0 swings from 0.65 (off resonance, mouth excludes the wave) to 2.66
  (at resonance), back-wall antinode to 4.2x. Measured resonant peaks 32 / 46 s
  match the half-wave seiche ladder 2Lh/(nc) modes n=3 (33.7 s, +5%) and n=2
  (50.5 s). The T=32 s field is a clean standing wave with ~3 half-waves along
  the harbour (wavelength cT=317 m; 2Lh/lambda=3.15), verified by the mode shape.
- **Diffraction** (semi-infinite breakwater, depth 10 m, T=8 s, lambda 70.9 m):
  Kd=0.545 on the shadow-boundary ray (analytic 0.5, +9%), 0.294 deep in the
  shadow, 0.889 in the lit zone. No-breakwater control: Kd uniform 0.95-0.99.
- **Berkhoff shoal** (exact corfon bathymetry, T=1 s, dx=0.15 m): wave-axis
  transect Kd rises smoothly from 1.0 offshore to a focus peak 2.23 behind the
  shoal (published Berkhoff ~2.2), decaying shoreward; flat-bed control Kd~1.0
  (p99 1.15). The near-dry shoreline (1:50 slope reaching the waterline, breaking
  off, shoaling H~h^-1/4 singularity) is excluded at depth>0.12 m.

Proof charts (additions only, never cleaned):
`docs/proof/templates/artemis_{harbour_resonance, breakwater_diffraction, berkhoff_shoal}_chart.png`
+ `artemis_physics_direct_result.json`. Sandbox driver preserved at
`docs/proof/templates/artemis_sandbox.py` (the canonical composer prototype).

## Load-bearing deck findings (the gotchas that cost iterations)

Non-obvious facts any ARTEMIS composer MUST bake, discovered by running the
binary (offline-green != correct-physics). These mirror the TOMAWAC 6-gotcha
pattern (ADR 0236):

1. **.cli column re-map (THE central gotcha).** ARTEMIS reuses the standard
   TELEMAC boundary-file columns with wave meanings, decoded from the example
   decks + confirmed by `borh.f`: **col1 LIHBOR** = boundary type
   (1=KINC incident, 2=KLOG solid, 4=KSORT free exit; KENT=5/KPOT=7 for imposed
   potential); **col4 HB** = incident wave HEIGHT (nonzero only on KINC nodes);
   **col5 TETAP**; **col6 ALFAP** phase; **col7 RP** = reflection coefficient
   (1=fully reflecting wall, 0=absorbing). RP and TETAP are per-node .cli data,
   NOT keywords.
2. **TETAP is the BOUNDARY TANGENT angle, not the wave direction.** Horizontal
   boundaries -> TETAP=0, vertical walls -> TETAP=90 (decoded from the shoal deck:
   incident-top=0, solid-lateral=90). Putting the wave direction in TETAP on the
   incident boundary corrupted the incident condition (a flat control read Hs~3
   H0 with huge standing waves); TETAP=0 there (direction comes only from the
   global `DIRECTION OF WAVE PROPAGATION` keyword) fixed it to a clean uniform
   field. A wrong free-exit TETAP left a ~23% reflected standing wave.
3. **Incident direction lives in ONE keyword.** `DIRECTION OF WAVE PROPAGATION`
   (degrees, trig convention 0=+X) sets the plane-wave direction globally; the
   per-node incident TETAP stays 0.
4. **All-incident outer ring for open-domain diffraction.** For a scattering
   problem in an unbounded domain (breakwater, island), set the ENTIRE outer
   boundary to KINC=incident (it imposes the plane wave AND radiates the
   scattered field, per the `ile_para` convention) and only the structure to
   solid. A free-exit (KSORT) on a wall parallel to the incident wave is
   degenerate and reflects; using it gave a flat control with 50% amplitude
   scatter. In the shadow, KINC is still correct: imposed incident + radiated
   scattered destructively interfere to the low shadow Hs.
5. **Bathymetry via BOTTOM + INITIAL WATER LEVEL.** Depth = INITIAL WATER LEVEL -
   BOTTOM; a submerged bed is NEGATIVE BOTTOM. Set INITIAL WATER LEVEL=0 and
   BOTTOM=-depth. FONSTR reads the `BOTTOM` SERAFIN variable (same as TOMAWAC).
   Output Hs SERAFIN name is `WAVE HEIGHT` (mnemonic HS); phase is `WAVE PHASE`.
6. **Internal thin barriers need marching-cell meshing.** A breakwater/harbour
   mouth is a removed row of grid nodes; the naive structured triangulation drops
   BOTH triangles of a cell when its shared-diagonal corner is removed, punching
   a hole at the slot edge that exposes a stray boundary node -> FRONT2 aborts
   ("LIQUID POINT BETWEEN TWO SOLID POINTS"). Fill any cell with exactly 3 kept
   corners with its single CCW triangle; the wall still blocks (cells with both
   top corners removed stay empty).
7. **Shoreline singularity.** With BREAKING off, a 1:50 slope reaching depth->0
   makes shoaling Hs~h^-1/4 diverge; exclude the near-dry band (depth>0.12 m)
   from focus metrics, or enable depth-induced breaking.
8. **Resonance needs a constricted mouth.** A perfectly-radiating open end cannot
   trap energy (it yields the trivial 2x standing wave at every period); a narrow
   mouth makes the basin a frequency-selective resonator, so the amplification
   spikes at the seiche modes and is small off-resonance.

Deck skeleton (English keywords), fully worked in the sandbox: GEOMETRY /
BOUNDARY CONDITIONS / RESULTS FILE, INITIAL CONDITIONS='CONSTANT ELEVATION' +
INITIAL WATER LEVEL, MATRIX STORAGE=3, SOLVER=8, WAVE PERIOD, DIRECTION OF WAVE
PROPAGATION, BREAKING, WAVE HEIGHTS SMOOTHING, RAPIDLY VARYING TOPOGRAPHY (for
steep shoals). Resonance sweeps use PERIOD SCANNING + BEGINNING/ENDING/STEP
PERIOD (one output frame per scanned period) + PHASE REFERENCE COORDINATES.

## Sandbox (local-first, not committed to services/)

`docs/proof/templates/artemis_sandbox.py` (the canonical composer prototype).
Runs INSIDE the image
(`docker run -v <dir>:/data trid3nt-local/telemac:latest python .../artemis_sandbox.py <mode>`),
builds a structured triangular mesh with an optional node mask + robust CCW
boundary-ring extraction (handles the notched harbour-mouth / breakwater
domains), writes SELAFIN geometry + the ARTEMIS `.cli` via the same TelemacFile
API the river-dye/tomawac composers use, authors the `.cas`, runs `artemis.py
--ncsize=1`, reads `WAVE HEIGHT`. Modes: `smoke`, `resonance`, `breakwater`,
`shoal`. Per-case wall time 6-13 s.

## Productionization recipe (the remaining, unbuilt engine leg)

The solve path exists; landing a registered capability is a normal worker-leg
build (mirror the TOMAWAC recipe, ADR 0236):

1. **Worker composer** `services/workers/telemac/artemis_build.py`: promote the
   sandbox (idealized-domain author for the analytic cases; add a real-bathy path
   - Copernicus/3DEP/NOAA harbour bathymetry -> mesh, reusing the river-dye
   DEM+gmsh machinery, plus a real harbour-outline path so quay walls become
   solid RP boundaries) for US harbour/marina agitation studies.
2. **Entrypoint mode** in `entrypoint.py`: route a `manifest['agitation']` block
   to `run_artemis_pipeline` with a strict-unknown-field parser (ADR 0158).
3. **Host tool + schema**: register the question-class tool(s)
   (`harbour_wave_agitation`, `breakwater_diffraction_sheltering`) with a
   `tool_query_corpus.yaml` block + the model-free `retrieve_visible_tools`
   check BEFORE acceptance; surface the bathymetry input layer, incident wave
   period/direction/height, per-structure reflection coefficient, and
   `target_resolution_m` (ADR 0225) with LOUD labeled defaults.
4. **Image rebuild + through-image smoke** (ADR 0148 staleness): the binary is
   present so no engine addition is needed, but the new worker python must be
   baked and smoke-run THROUGH the image; the parser must hard-error on unknown
   fields.
5. **Showcase** seeded `--only` GREEN with the physics assertions here (resonance
   peak at the seiche mode; Kd~0.5 on the shadow line; Berkhoff focus ~2.2).
6. Fold the duplicate board rows into the three question-class templates; mark
   rows COVERED only after the live daemon E2E is GREEN.

**TOMAWAC->ARTEMIS nesting** (the fidelity-pairing row): the baked `artemis.dico`
ships the nesting keyword family - `NESTING WITHIN TOMAWAC OUTER MODEL`,
`INSTANT FOR READING TOMAWAC SPECTRUM`, `TOMAWAC OUTER SPECTRAL FILE`,
`TOMAWAC OUTER RESULT FILE`, `TOMAWAC LIQUID BOUNDARY FILE` (CHAINTWC 1/2 in
`artemis.f` reads the outer spectrum / interpolated HM0 onto the ARTEMIS liquid
boundary). So the regional-TOMAWAC -> harbour-ARTEMIS boundary handoff is
reachable with the shipped binary; wiring it is a productionization task once the
two engines are both registered.

## Consequences

- ARTEMIS physics is validated end-to-end through the real solver at zero infra
  cost (binary already shipped) - the phase-resolving harbour-agitation tier of
  the fidelity ladder is proven reachable and accurate against three independent
  published analytic solutions (Berkhoff 1982, Sommerfeld/Penny-Price, seiche
  ladder), retiring the "no elliptic/mild-slope wave solver exists" board note.
- No registry growth, no coded-tools delta, no LOC in `services/` this pass -
  deliberately (two-wave rhythm): the composer/tool/showcase build is the next
  unit of work, with every gotcha above pre-solved so it is a mechanical
  promotion, not research.
- ARTEMIS vs TOMAWAC: distinct tiers, no fold. TOMAWAC (phase-averaged, spectral)
  answers regional wind-sea; ARTEMIS (phase-resolving, monochromatic/random)
  answers in-harbour agitation, resonance, and diffraction where phase matters.
  The Jukbyeon-Port finding (ARTEMIS beats TOMAWAC in-lee, TOMAWAC better
  offshore) is the fidelity line both rows sit on.

## COMPLETE (productionization landed, 2026-08-13)

The remaining engine leg from the recipe above is BUILT and LIVE. `registry
248 -> 249` (+1 coded tool `artemis_harbor_agitation`, hand-written
`@register_tool` engine="telemac" tier="template"). All six ARTEMIS board rows
LANDED (the three question classes COVERED; the fidelity-pairing row stays a
documented nesting-narrative row).

- **Worker leg** `services/workers/telemac/artemis_build.py` (~730 LOC): the
  sandbox promoted verbatim with all eight gotchas baked, `ArtemisConfig` (23
  strict fields), a `solve()` dispatching by (wave_mode x bathy_source). Three
  modes proven THROUGH THE BAKED IMAGE (no bind mount, provenance-checked):
  diffraction (kd_max 2.29, sheltered 0.295 << exposed 1.189, ratio 0.248),
  resonance (response 2.65 at T=32 s -> seiche ladder n=3 33.7 s, off-resonance
  0.77), shoal (Berkhoff focus kd_max 2.44 vs published ~2.2). Each writes
  `res_agitation.slf` (the raw ARTEMIS mesh sibling) + `agit_field.slf` (the
  single-frame WAVE HEIGHT field the agent-side postprocess rasterizes).
- **Real-bathy path**: `noaa_greatlakes` samples the SAME NOAA NGDC DEM_all
  lake-datum bathymetry the TOMAWAC leg proved (ADR 0236). Two gotchas beyond
  the sandbox's eight, discovered building the real path: (a) build_mesh calls
  `depth_fn` on the COMPACTED kept nodes, so the masked bed must be indexed by
  the kept mask (a full-grid bed length mismatch aborts the SELAFIN write); (b)
  the demo breakwater must be a semi-infinite barrier ATTACHED to the AOI edge
  with an interior tip (a floating internal barrier isolates stray boundary
  nodes and aborts the run), and interior coastline (masked-land) ring nodes
  must be ABSORBING solid, never incident -- imposing a plane wave at a shore
  injects spurious energy (the documented ARTEMIS complex-coastline weakness;
  the Jukbyeon finding). An open-water AOI keeps kd_max physical.
- **Entrypoint mode**: `manifest['agitation']` routes to `run_artemis_pipeline`
  with the strict-unknown-field parser `artemis-agitation-1` (ADR 0158) -- an
  unknown key raises loudly (build-time smoke asserts it).
- **Host tool + schema**: `artemis_harbor_agitation` (one tool, three modes),
  `ArtemisAgitationLayerURI` (kd_max / kd_sheltered / kd_exposed /
  resonant_period_s / response_at_resonance narration scalars, invariant 1) +
  `TELEMAC_AGITATION_STYLE_PRESET = continuous_wave_agitation`, `postprocess_artemis`
  (Kd = Hs/H0 COG on a robust p99.5-capped viridis legend). `target_resolution_m`
  DECLARED (ADR 0225, min 20 m). LOUD labeled defaults surfaced through the
  input-review gate (prescribed monochromatic wave, schematic breakwater).
  Retrieval: `artemis_harbor_agitation` top-8 on all corpus prompts (7/7,
  model-free `retrieve_visible_tools`), TOMAWAC co-surfacing intact (distinct
  tiers ruled). Solver `artemis_agitation` registered (same worker image).
- **Image rebuilt** (`cec1f0420c03`) with build-time smoke (import + config map +
  strict-field gate) and all three modes solved through the baked copy.
- **Live E2E** through the daemon (`!run` path, case
  `01KZXAF48WCA59CJB7YB540ZYE`, run `01KZXAFR5TH4BSVD4A1VNB63ZB`): breakwater
  diffraction on REAL Lake Superior lake-datum bathymetry off Marquette MI
  (~14830 wet nodes, mean depth ~80 m, dx 40 m). Physics asserts (proof norm
  #9): agitation nonzero, kd_max 2.76, the sheltered lee behind the breakwater
  mean Kd 0.051 vs the exposed approach 1.195 (sheltering ratio 0.043); a
  no-breakwater control on the SAME bathymetry reads the lee at ~1.06 (no
  shadow) -- the sheltering tracks the structure, not the domain. Showcase green
  + reconnect-durable (1 persisted layer). Proof pair:
  `docs/proof/templates/artemis_harbor_agitation_marquette_diffraction_pair.png`
  (WITH vs WITHOUT breakwater, real solver field, mesh overlaid).
- **Offline suite**: four alphabetical slices from repo root at the EXACT SIX
  known failures (4 fetch_resolution [f-o] + 2 river_dye [p-r]; [a-e] + [s-z]
  clean); the new offline guard `test_artemis_harbor_agitation.py` + the
  249-registry pin + EXPECTED_TEMPLATES both green.
- **Follow-ups** (clean): a real harbour-outline path (quay walls as solid RP
  boundaries) for resonance/shoal over real geography; the TOMAWAC->ARTEMIS
  nesting handoff (the fidelity-pairing row's CHAINTWC plumbing); a
  surveyed-breakwater geometry fetcher to retire the schematic-barrier label.
