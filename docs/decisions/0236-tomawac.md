# ADR 0236 - TOMAWAC spectral-wave engine: local-first physics proof + productionization recipe

Status: Physics PROVEN local-first (in-image, through the baked tomawac binary,
all 4 TOMAWAC question classes, discriminating pairs per proof norm #9).
NOT productionized: no registered LLM tool, no worker composer, no entrypoint
mode, no image rebuild. Registration recipe documented below. Board rows stay
CAND (annotated PHYSICS-PROVEN), not COVERED.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD carries a TOMAWAC section (7 CAND rows across two
board blocks; they collapse to 4 distinct question classes). TOMAWAC is
TELEMAC's third-generation spectral (phase-averaged) wave-action solver -
wind-wave generation, shoaling/breaking, wave-current interaction, bottom
friction. It is the refinement-grade complement to SFINCS/SnapWave's coastal
screening, per the fidelity-ladder doctrine.

Pivotal finding (the STOP-vs-build gate): **the tomawac binary is already baked**
in `trid3nt-local/telemac:latest`. The conda-forge `opentelemac=v9.0.0` package
that supplies the shipped TELEMAC-2D/GAIA/WAQTEL legs also ships the whole
suite: `/opt/conda/opentelemac/builds/gnu.shared/bin/tomawac`, all
`lib*tomawac*.so`, `tomawac.py` on PATH, the `tomawac.dico`, and `sources/tomawac`
(so a `FORTRAN FILE` user routine compiles at run time - see wave-current below).
The conda package does NOT ship an `examples/` tree, so decks were authored from
the dico keyword reference + published wave physics, replicating the physics of
the official `tomawac/fetch_limited`, `tomawac/shoal`, `tomawac/opposing_current`
and `tomawac/bottom_friction` example cases (geography-free idealized
verification - clears the citations law like the GWE analytic V&V).

Because the binary exists, this is NOT a STOP-RECIPE (unlike the SCHISM
iharind/USE_HA or HEC-RAS WQ walls). The solve path is real. What is NOT built
is the TRID3NT productionization layer (composer + registered tool + showcase +
image bookkeeping).

## The 7 board rows -> 4 question classes -> proven physics

| board row(s) | question class | discriminating proof (in-image) |
|---|---|---|
| `wind_generated_wave_growth` [L], `fetch_limited_wind_wave_growth` [M] | fetch-limited wind-wave growth | short vs long fetch (same wind) + low vs high wind (same fetch); tracks CERC/SPM fetch law |
| `nearshore_wave_refraction_shoaling` [L], `nearshore_shoaling_breaking_benchmark` [M] | shoaling + depth-induced breaking | Hs dip (intermediate) -> rise (shoaling) -> break-down (nearshore) over a beach |
| `wave_current_interaction` [L], `wave_current_opposing_interaction` [M] | wave-current interaction | opposing current ramp amplifies Hs, following ramp damps it |
| `bottom_friction_wave_dissipation` [L] | bottom-friction dissipation | shallow shelf, friction OFF vs ON -> Hs lower with friction |

The two board blocks duplicate the first three classes (an L-tier and an M-tier
row for each); a productionized template family folds each duplicate pair into
one question-class tool (the same knobs-vs-new-template judgment used across the
board). Bottom friction is the one unpaired row.

## Live numbers (through the baked binary, wide deep basin unless noted)

- Fetch growth, U=20 m/s: Hs 0.876 m at 10 km fetch -> 2.381 m at 60 km
  (CERC 1.022 / 2.503). Wind sensitivity, 40 km fetch: 0.755 m at U=10 -> 2.557 m
  at U=25 (CERC 1.022 / 2.554). Developed-sea cases match CERC to <5%; young/low
  cases run ~15-26% under (WAM4 physics vs the empirical SPM curve). Internal
  consistency: the long run's Hs at 10 km (0.906 m) equals the short run's end
  (0.876 m) within 3% - growth is FETCH-driven, not a domain artifact.
- Shoaling (offshore swell Hs=1.5 m, beach 40->3 m): offshore 1.497 m ->
  intermediate-depth dip ~1.42 m -> shoaling peak 1.957 m -> nearshore
  break-down to 1.33 m (Hs/depth=0.44 at 3 m, depth-limited).
- Bottom friction (8 m shelf, U=20): Hs_end 1.917 m (off) vs 1.752 m (on), -9%.
- Wave-current (swell into current ramping 0->2.5 m/s): opposing end Hs 4.10 m,
  none 1.28 m, following 1.13 m - opposing amplifies 3.2x vs following.

Proof charts (additions only, never cleaned): `docs/proof/templates/tomawac_{fetch_limited_growth,nearshore_shoaling_breaking,bottom_friction_dissipation,wave_current_interaction}_chart.png`
+ `tomawac_physics_direct_result.json`. Sandbox driver preserved at
`docs/proof/templates/tomawac_sandbox.py` (the canonical composer prototype -
see "Sandbox" below).

## Load-bearing deck findings (the 6 gotchas that cost iterations)

These are the non-obvious facts any TOMAWAC composer MUST bake, discovered by
running the binary (offline-green != correct-physics):

1. **Bathymetry sign.** `BOTTOM` is bed ELEVATION; a submerged bed is NEGATIVE
   (water depth = still-water-level 0 - BOTTOM). A positive constant made the
   whole domain dry -> Hs=0 everywhere.
2. **Initial spectrum type.** `TYPE OF INITIAL DIRECTIONAL SPECTRUM = 1` builds
   JONSWAP from wind+fetch (needs `INITIAL MEAN FETCH VALUE`; default ~0 -> zero
   energy). Use `= 6` (parameterised JONSWAP keyed off `INITIAL SIGNIFICANT WAVE
   HEIGHT` + `INITIAL PEAK FREQUENCY`, works at any wind). Boundary spectrum
   `TYPE OF BOUNDARY DIRECTIONAL SPECTRUM` uses the identical 0-7 numbering (=6).
3. **Linear wave growth.** WAM4 wind input is exponential (Sin=beta*E); with an
   empty/misaligned seed the downwind bins never bootstrap. `LINEAR WAVE GROWTH
   = 1` (Cavaleri-Malanotte-Rizzoli) seeds them - without it Hs decays to ~0.3 mm.
4. **Incident-boundary code.** TOMAWAC imposes the boundary spectrum only where
   `LIFBOR == KENT`; KENT=5 in BIEF. A `.cli` liquid node must be code **5**, not
   1 (KINC) - the swell was not injected until this was fixed.
5. **Wide domain for the 1D fetch law.** In an 8 km-wide box, lateral directional
   spreading bleeds energy to the side walls and the centerline Hs runs ~half the
   CERC law; a 30 km-wide domain recovers CERC to <7%.
6. **Wave-current needs a current GRADIENT.** A spatially UNIFORM current leaves
   steady-state Hs unchanged (no gradient = no action-flux change; a uniform run
   showed only ~9% boundary-transient scatter, wrong sign). The amplification is
   real only where the current varies in space (waves into a strengthening
   opposing current) - imposed here via a compiled `USER_ANACOS` (UCONST=0 in the
   shipped `anacos.f`) ramping UC with X.

Deck skeleton (English keywords) is fully worked in the sandbox: GEOMETRY/
BOUNDARY CONDITIONS/2D RESULTS FILE, NUMBER OF DIRECTIONS/FREQUENCIES, MINIMAL
FREQUENCY + FREQUENTIAL RATIO, CONSIDERATION OF SOURCE TERMS, WIND GENERATION,
WHITE CAPPING DISSIPATION, NON-LINEAR TRANSFERS BETWEEN FREQUENCIES, BOTTOM
FRICTION / DEPTH-INDUCED BREAKING DISSIPATION, CONSIDERATION OF A WIND +
STATIONARY WIND + WIND VELOCITY ALONG X/Y, CONSIDERATION OF A STATIONARY CURRENT.
2D output var mnemonic for Hs is `HM0` (SERAFIN name `WAVE HEIGHT HM0`).

## Sandbox (local-first, not committed to services/)

`docs/proof/templates/tomawac_sandbox.py` (the canonical composer prototype for
productionization). Runs INSIDE the image
(`docker run -v <dir>:/data trid3nt-local/telemac:latest python .../tomawac_sandbox.py <mode>`),
builds a regular-grid triangular mesh (CCW outer ring, rank-based IPOBO), writes
SELAFIN geometry + `.cli` via the same `TelemacFile` API the river-dye composer
uses, authors the `.cas`, runs `tomawac.py --ncsize=1`, reads `HM0`. Modes:
`smoke`, `fetch_pair`, `shoal`, `friction`, `current`. Per-case wall time 6-9 s
(fetch/shoal), ~1 min for wave-current (USER_ANACOS compile).

## Productionization recipe (the remaining, unbuilt engine leg)

The solve path exists; landing a registered capability is a normal worker-leg
build, NOT blocked:

1. **Worker composer** `services/workers/telemac/tomawac_build.py`: promote the
   sandbox (idealized-basin author for the analytic cases; add a real-bathy path
   - Copernicus/3DEP DEM -> mesh, reusing the river-dye DEM+gmsh machinery - for
   US-site live runs, e.g. Lake Superior/Michigan fetch).
2. **Entrypoint mode** in `entrypoint.py`: add `mode == "tomawac"` routing beside
   `river_dye`/`rain_on_grid`, with the strict-unknown-field manifest gate
   (ADR 0158) extended to the wave params.
3. **Host tool + schema**: register the question-class tool(s) with a
   `tool_query_corpus.yaml` block and the model-free `retrieve_visible_tools`
   check BEFORE acceptance (new-tool retrieval-corpus HARD RULE); surface the
   bathymetry input layer (ADR 0231) and `target_resolution_m` + ADR 0225/0232
   for the mesh-resolution knob with LOUD labeled defaults.
4. **Image rebuild + through-image smoke** (ADR 0148 staleness): the binary is
   present so no engine addition is needed, but the new worker python must be
   baked and smoke-run THROUGH the image; the parser must hard-error on unknown
   fields.
5. **Showcase** seeded `--only` GREEN with the physics assertions already written
   here (Hs monotone in fetch + wind; shoaling dip-rise-break; friction on<off;
   opposing>following).
6. Fold each duplicate L/M board pair into one question-class template; mark rows
   COVERED only after the live daemon E2E is GREEN.

## Consequences

- TOMAWAC physics is validated end-to-end through the real solver at zero infra
  cost (binary already shipped) - the fidelity-ladder wave-refinement tier is
  proven reachable, retiring the "no wave-spectrum solver exists" board note.
- No registry growth, no coded-tools delta, no LOC in `services/` this pass -
  deliberately: the composer/tool/showcase build is the next unit of work, with
  every gotcha above pre-solved so it is a mechanical promotion, not research.
- SWAN overlap (queued roster entry): TOMAWAC's edge over SWAN is native TELEMAC
  coupling (current fields from a TELEMAC-2D run) + unstructured meshes; a
  TOMAWAC row does not fold into SWAN. Both remain distinct wave tiers.
