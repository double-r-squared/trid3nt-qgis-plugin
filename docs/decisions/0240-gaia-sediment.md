# ADR 0240 - GAIA v3 multi-class graded sediment (grain sorting); cohesive-mud + 3D rows adjudicated

Date: 2026-08-13
Status: accepted

## Context

Module-coverage board GAIA section (docs/validation/module-coverage-board.md).
GAIA v1 (supply-limited suspended sediment) and v2 erodible-bed scour
(ADR 0216) are live. Four open CAND rows remained, the ADR 0216 follow-ons plus
two roster-side rows:

- `mixed_grain_size_sediment_budget` [CAND-M] - how does a mixture of several
  grain sizes SORT and segregate downstream vs a single representative size?
- `cohesive_mud_settling_consolidation` [CAND-M] - how does fine cohesive mud
  (vs sand) settle, consolidate and resuspend in a low-energy backwater?
- `multiclass_sediment_bed_evolution` [CAND-L] - reproduce a documented
  multi-grain-class bedload+suspended trench-infill / bar-formation experiment.
- `coastal_dune_migration_3d_coupling` [CAND-L] - couple TELEMAC-3D to GAIA for
  marine dune / sandwave migration.

GAIA coupling is standardized (ADR 0154/0216): TRID3NT authors GAIA decks
natively via the worker `write_gaia_deck`, riding the ONE `telemac_river_dye`
archetype over fetched US NHDPlus reaches + Copernicus DEM. TELEMAC-2D only.

## Gate-check (baked image `trid3nt-local/telemac:latest`, gaia.dico v9.0)

All keywords the four rows need are present in the baked binary:
- Multi-class: `CLASSES SEDIMENT DIAMETERS` / `CLASSES INITIAL FRACTION` /
  `CLASSES TYPE OF SEDIMENT` arrays (TAILLE=2 auto-growing), `HIDING FACTOR
  FORMULA` (1=Egiazaroff), `ACTIVE LAYER THICKNESS`, D50 surface-diameter
  output var - CONFIRMED.
- Cohesive: `CLASSES TYPE OF SEDIMENT = CO`, `CLASSES CRITICAL SHEAR STRESS FOR
  MUD DEPOSITION`, `LAYERS CRITICAL EROSION SHEAR STRESS OF THE MUD`, `LAYERS
  PARTHENIADES CONSTANT`, `LAYERS MUD CONCENTRATION`, `WEAK SOIL CONCENTRATION
  FOR MUD` - CONFIRMED (a pure-CO run additionally requires `SKIN FRICTION
  CORRECTION = 0`, a gotcha found live).
- MORFAC (`MORPHOLOGICAL FACTOR`) + SUSPENSION/BED LOAD toggles - already used
  by v1/v2.

## Local-first physics proof (baked binary, real Snake River nr Twin Falls mesh)

Direct GAIA solves over the saved real reach mesh (aoi_42_5600_114_4500,
5370 nodes; no network) - the scour lineage site (ADR 0216).

- MULTI-CLASS graded (100/400/1200 um, equal thirds, Egiazaroff hiding),
  MORFAC 20 / 1800 s: surface D50 spans **244 -> 1027 um (range 782 um)** from a
  uniform 567 um mix - the bed ARMORS in scour zones (fines winnow out) and
  FINES in deposition zones. The DISCRIMINATING PAIR vs a single 300 um class
  (surface D50 range = 0 um, sorting structurally impossible). Per-class solid-
  discharge fields (QS1/QS2/QS3) are produced only in the multi-class run.
- Grain-mobility driver (identical flow): uniform-fine 100 um mean solid
  discharge 25.4 vs uniform-coarse 1200 um 53.2 (MPM q ~ d^1.5 above threshold)
  - grain-size-dependent mobility is the physical cause of sorting.
- COHESIVE MUD (CO, Krone/Partheniades) RUNS on the same mesh but strips the
  whole 5 m bed everywhere (min -5000 mm, all 5370 nodes erode, zero
  deposition) - physically correct (mud does not persist in a steep gravel
  river) and proof that cohesive mud needs a LOW-energy backwater, not the
  scour lineage site.

Proof: docs/proof/templates/telemac_multiclass_sediment_sorting_proof.png
(surface grain-size distribution single-spike vs graded-spread + bed-evolution
map, mesh overlay, direct-solve through the baked image).

## Decision

**`mixed_grain_size_sediment_budget` + `multiclass_sediment_bed_evolution` -
LANDED as ONE knob on `telemac_river_dye` (0 new coded tools).** Both rows are
the same question class (multi-class differential mobility -> sorting); the CAND-L
"reproduce a lab experiment" framing is honored as reproducing the SORTING
MECHANISM on US reaches (no non-US lab flume geometry is fetched - honesty floor).

Recipe (worker `write_gaia_deck`, `sediment_gradation` of >= 2 classes):
- `CLASSES TYPE OF SEDIMENT = NCO;NCO;...`, `CLASSES SEDIMENT DIAMETERS`,
  `CLASSES INITIAL FRACTION` (renormalized), `CLASSES SEDIMENT DENSITY` arrays,
- `HIDING FACTOR FORMULA = 1` (Egiazaroff - couples the classes),
- `SUSPENSION FOR ALL SANDS = NO`, `BED LOAD FOR ALL SANDS = YES`,
  `LAYERS INITIAL THICKNESS = bed_thickness_m`, `MORPHOLOGICAL FACTOR`,
- D50 in `VARIABLES FOR GRAPHIC PRINTOUTS` (surface mean diameter = the sorting
  signature).

Coupling: the multi-class NCO bedload path rides the EXACT v2 erodible coupling
(SUSPENSION off -> no suspended tracer appended, dye stays the sole hydraulic
companion; only `COUPLING WITH = 'GAIA'` + `GAIA STEERING FILE` added). Every v1
supply-limited and v2 single-class deck stays byte-identical (pinned by
`test_gaia_erodible.py`).

Surface: `telemac_river_dye` exposes `sediment_gradation` (a preset name -
graded_sand / poorly_sorted / sand_gravel_bimodal / fine_coarse_sand - OR an
explicit `[[d50_um, fraction], ...]` list; >= 2 classes). A gradation forces
`erodible_bed=True` (a mix sorts only on a mobile bed) and the sediment class -
gates in lock-step (ADR 0216 false-green doctrine). Auto-arms from grading
vocabulary (graded / mixed-grain / sorting / armoring / bimodal / fining) shared
by `classify_substance` and the tool. The run reports the surface D50 spread
(`sediment_surface_d50_min/max/range_um` + `sediment_n_classes`) as the
Invariant-1 sorting number. Corpus queries added (retrieval HIT expected on
"how does a mixture of grain sizes sort", "bed armoring", "downstream fining").

**`cohesive_mud_settling_consolidation` [CAND-M] - SCOPED with a PROVEN recipe
(not built).** Per the simplicity/honesty doctrine: the CO/Partheniades path
RUNS in the baked binary, but it is a genuinely different physics regime that
(a) needs its own keyword block (`CLASSES TYPE OF SEDIMENT = CO`, mud critical
shear stresses, Partheniades constant, mud concentration, `SKIN FRICTION
CORRECTION = 0`) AND (b) needs a LOW-energy backwater/estuary site + tuned
critical shear to show settling/consolidation rather than the wholesale erosion
measured on the steep scour reach. It GREW beyond a same-surface knob. Recipe
pinned (keywords proven + the skin-friction gotcha + the site-regime finding);
build queued behind a low-energy US backwater site selector.

**`coastal_dune_migration_3d_coupling` [CAND-L] - STOP.** Requires TELEMAC-3D
hydrodynamics (residual-current-driven marine dune migration); TRID3NT's TELEMAC
surface is `telemac_river_dye` = TELEMAC-2D only. No 3D hydrodynamic driver is
authored. Recipe if ever built: a TELEMAC-3D deck author + a marine/estuary
domain + wave-current residual forcing, then GAIA coupling. Out of the 2D
river-reach archetype.

## Consequences

- Coded tools: **0 new** (knob fold on `telemac_river_dye`). Registry +
  EXPECTED_TEMPLATES unchanged (graded is a knob on the one GAIA template, like
  wind/rain/scour).
- Worker parser bump `telemac-reach-7` -> `telemac-reach-8` (new ReachConfig
  field `sediment_gradation`). Rejection/acceptance via the existing strict
  unknown-field gate (ADR 0158).
- Worker image `trid3nt-local/telemac:latest` REBUILT (write_gaia_deck v3 branch
  + `_normalize_gradation` + entrypoint D50 sorting metric). Behavior proven
  THROUGH the rebuilt image: baked code emits the multi-class deck, solves to
  CORRECT END, entrypoint reports n_classes=3 surface D50 454-1000 um range
  546 um (100/400/1000 mix). v1/v2 decks byte-identical.
- Contract: `TelemacSedimentLayerURI` gains optional
  `sediment_surface_d50_{min,max,range}_um` + `sediment_n_classes` (None on
  single-class / non-sediment). Additive.
- Honesty floor: planning-grade SORTING PATTERN, not a calibrated grain-size
  distribution; the gradation is a demo mix (no bed-composition/sieve fetcher);
  MORFAC is a demo amplifier; Egiazaroff hiding makes the classes near-co-mobile
  so surface-D50 change concentrates at scour/deposit hotspots over a short demo
  (the range is real but node-sparse - shown honestly on a log-y distribution).

## Verification (this wave, offline build session)

- Worker pytest `test_gaia_erodible.py` (+ new multi-class cases) +
  `test_classify_substance.py`: 8 passed, 1 skipped (grace2 env).
- Full worker assertions THROUGH the rebuilt image (deck emit + solve +
  entrypoint metric): green.
- Standalone gradation/classify logic tests: green.
- Server module AST-parse: clean.
- FLAG: the server pytest suite (test_run_river_dye_scenario etc.) + the
  four-slice offline baseline-SIX check + a live daemon E2E could NOT run on
  this box - the grace2 conda env's editable installs point at the OTHER
  (GRACE-2) checkout and `trid3nt_server` does not resolve here (the worktree
  editable-install collision). The server surface is authored + logic-proven but
  NOT server-suite/live-verified this session. Queued for an env with
  trid3nt_server installed.
