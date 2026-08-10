# ADR 0216 - TELEMAC GAIA v2 erodible-bed scour morphodynamics

Date: 2026-08-10
Status: accepted

## Context

Module-coverage board GAIA rows (docs/validation/module-coverage-board.md,
"### GAIA"). GAIA v1 (supply-limited suspended sediment: LAYERS INITIAL
THICKNESS = 0, deposition-only, single class) shipped at commit f9eea1bb and
is the "upstream sediment supply / reservoir-inflow sedimentation" answer
(ADR 0154 row 3). The board's natural GAIA lead was the v2 row:

- [CAND-M] `erodible_bed_scour_morphodynamics` - where does the bed SCOUR (not
  just deposit) below a dam/weir/bridge contraction under a flood, and where
  does the eroded material re-deposit downstream?

plus two board follow-ons (`mixed_grain_size_sediment_budget`,
`cohesive_mud_settling_consolidation`).

GAIA coupling is standardized (ADR 0154 row 4): TRID3NT authors GAIA decks
natively via the worker `write_gaia_deck`; it has no legacy-SISYPHE ingestion.
The TELEMAC surface is ONE archetype composer `telemac_river_dye` +
`telemac_river_dye_build.py`, which AUTHORS meshes/decks from fetched US
NHDPlus reaches + Copernicus DEM. The `erodible_bed` ReachConfig flag was
already stubbed (v2 placeholder) with no code behind it.

Keywords verified in-image against `gaia.dico` v9.0
(`/opt/conda/opentelemac/sources/gaia/gaia.dico`): `LAYERS INITIAL THICKNESS`,
`BED LOAD FOR ALL SANDS`, `BED-LOAD TRANSPORT FORMULA FOR ALL SANDS` (INTEGER,
DEFAUT 1; 1=Meyer-Peter-Mueller, 2=Einstein-Brown, 3/30=Engelund-Hansen,
7=van Rijn bedload, ...; formulas 3/30/9 must NOT be used with suspension),
`MORPHOLOGICAL FACTOR` (MOFAC, DEFAUT 1).

## Decision

**`erodible_bed_scour_morphodynamics` - LANDED as a knob on `telemac_river_dye`
(0 new coded tools).** Consistent with the ADR 0094/0154 "templates rejoin the
main search surface, fidelity lives in docstrings + corpus" doctrine, this is a
knob fold on the existing GAIA sediment path, not a new registered tool.

Recipe (worker `write_gaia_deck`, `erodible_bed=True` branch):
- `SUSPENSION FOR ALL SANDS = NO` (pure bedload morphodynamics -> a clean
  scour/deposition signal AND no suspended tracer appended to TELEMAC-2D, so the
  dye stays the SOLE hydraulic-companion tracer),
- `BED LOAD FOR ALL SANDS = YES`,
- `BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = <bedload_formula>` (default 1,
  Meyer-Peter-Mueller; van Rijn=7, Einstein-Brown=2 also allowed),
- `LAYERS INITIAL THICKNESS = <bed_thickness_m>` (a real erodible stock, default
  5 m, so the reach can lower),
- `MORPHOLOGICAL FACTOR = <morphological_factor>` (default 10; amplifies bed
  change per hydraulic step so a short demo hydrograph yields a readable scour
  depth - a speed-up lever, not a physical rate).

Coupling (worker `author_deck`): the bedload path appends NO suspended tracer,
so - unlike the v1 suspended path - it does NOT add `T2` to the graphic
printouts and does NOT widen `PRESCRIBED TRACERS VALUES`; only the
`COUPLING WITH = 'GAIA'` + `GAIA STEERING FILE` lines are added. Every v1
supply-limited deck stays byte-identical (pinned by
`tests/test_gaia_erodible.py`).

Postprocess (`postprocess_telemac_deposition`, `erodible=True`): rasterizes the
SIGNED final `CUMUL BED EVOL` field (scour negative / deposition positive) on
the diverging `TELEMAC_BED_EVOLUTION_STYLE_PRESET` ramp centered on 0, reports
`max_scour_mm` beside `max_deposition_mm`, and is valid as long as the bed moved
either way (the v1 path still errors when nothing deposited). The diverging
range is capped at the 99th percentile of |bed change| so a single
inflow-boundary bedload pile-up node (a known GAIA artifact) does not wash the
interior pattern off the ramp.

Surface: the tool exposes `erodible_bed` (auto-arms when the substance/prompt
names scour/erosion/bedload/degradation/mobile-bed; explicit True/False wins),
`bed_thickness_m`, `bedload_formula` (1/2/7), `morphological_factor`. The
returned peak carries `max_scour_mm` + `max_deposition_mm` (Invariant 1 - from
the postprocessed field, never invented). Corpus queries + docstring make the
scour question retrievable (model-free retrieval HIT confirmed on 3 scour
prompts).

**Walkthrough**

| aspect | value |
|---|---|
| question class | `erodible_bed_scour_morphodynamics` |
| module | GAIA bedload (SUSPENSION off), Exner bed evolution |
| transport law | Meyer-Peter-Mueller (ICF=1, default; van Rijn=7, Einstein-Brown=2) |
| citation | Tassi et al., "Introducing GAIA, the brand-new sediment transport module of the TELEMAC-MASCARET system" (2019); gaia.dico v9.0 keyword reference |
| user knobs | grain_size_um (d50), erodible_bed, bed_thickness_m, bedload_formula (transport-law select), morphological_factor |
| deliverable | signed bed-evolution raster (scour<0<deposition, diverging ramp) + along-channel bed-change profile |

## Live smoke (in-image, worker-image law)

Direct `run_pipeline` solve through the worker (my edited source mounted over
the baked copy, then re-verified through the REBUILT image), Snake River near
Twin Falls, ID (default seed), `bank_source=constant_ribbon`, distance 2 km,
d50 300 um, MOFAC 3, Q 250 m3/s, duration 600 s. `CORRECT END OF RUN`, ~25-31 s
solver wall. The GAIA result `gaia_river.slf` `CUMUL BED EVOL` final frame:

- SCOUR: 1194 nodes, deepest -0.82 m (-819 mm),
- DEPOSITION: 1091 nodes, peak +1.18 m (dominated by one inflow-boundary
  pile-up node at the domain end),
- mean bed change ~0 (mass balance closes; `CUMULATED LOST MASS` ~1e-6 kg),
- 90% of interior nodes within +/- 5 cm (a pool-scour / riffle-deposition
  pattern along the meandering upstream reach).

The behavior-proving assertion (WORKER-IMAGE LAW): the bed EVOLVES with
NONZERO SCOUR (min < 0) - not the v1 deposition-only signal. An earlier
MOFAC=30 / Q=400 run confirmed the magnitudes scale ~linearly with MOFAC
(peak 39.5 m -> 1.18 m from 30 -> 3), i.e. the morphological factor is the
demo speed-up lever the docstring promises. Proof render (direct-solver,
mesh overlay per the render-mesh norm):
`docs/proof/templates/telemac_erodible_bed_scour_proof.png`.

## Follow-on rows - scoped, not built (honest, per the simplicity doctrine)

Rather than half-build three rows, the two follow-ons land with a recipe:

- **`mixed_grain_size_sediment_budget`** (board CAND-M, yen_multigrain proxy):
  emit MULTIPLE `CLASSES` (distinct d50/density/fraction) with per-class active-
  layer stratification (`NUMBER OF LAYERS`, `ACTIVE LAYER THICKNESS`) and a
  hiding/exposure formula; ride the same erodible-bed coupling. New worker knob:
  a grain-class gradation list threaded to `write_gaia_deck`. Recipe pinned;
  build queued.
- **`cohesive_mud_settling_consolidation`** (board CAND-M, bosse_vase proxy):
  `CLASSES TYPE OF SEDIMENT = CO` with Krone/Partheniades critical shear
  stresses for erosion/deposition + consolidation layering - a genuinely
  different physics path from Shields-based sand, hence deferred behind the
  non-cohesive scour lead. Recipe pinned; build queued.

## Consequences

- Coded tools: 0 new (knob fold on `telemac_river_dye`). Registry unchanged;
  EXPECTED_TEMPLATES unchanged (wind/rain/scour are all knobs on the one GAIA
  template).
- Worker parser bump `telemac-reach-5` -> `telemac-reach-6` (new ReachConfig
  fields `bed_thickness_m`, `bedload_formula`, `morphological_factor`; the
  `erodible_bed` stub is now live). Rejection + acceptance tests updated
  (`test_entrypoint.py`).
- Worker image `trid3nt-local/telemac:latest` REBUILT (write_gaia_deck v2 branch
  + author_deck coupling branch + ReachConfig fields). v1 supply-limited +
  every non-sediment deck byte-identical (pinned).
- Contract: `TelemacSedimentLayerURI` + `TelemacDyeLayerURI` gain optional
  `max_scour_mm` (None on v1 / non-sediment). Additive.
- Postprocess: `postprocess_telemac_deposition` gains `erodible` (signed
  scour+deposition rasterization). v1 path unchanged.
- Honesty floor: planning-grade scour/deposition PATTERN, not a calibrated
  scour depth; MORPHOLOGICAL FACTOR is a demo amplifier; grain size is a demo
  default (no bed-composition fetcher); the inflow-boundary bedload pile-up is
  a named, capped GAIA artifact.

## Amendment 2026-08-10 - false-green: `substance='scour'` never coupled GAIA

The showcase-seeded scour case (old case `01KZPTN56PMHR0NVM6G01FQSBC`) passed the
seeder's layer-count grade but was a **plain tracer solve**, not morphodynamics:
its deck carried ZERO GAIA keywords and its run prefix had no
`telemac_sediment_deposition.tif`.

Root cause - two divergent gates on the SAME intent:
- `classify_substance()` recognized only grain words
  (`sediment`/`sand`/`silt`/`mud`/`slurry`/`tailings`), so `substance='scour'`
  fell through to the `tracer` class. The GAIA coupling + deposition postprocess
  are gated on `substance_class == 'sediment'`, so they silently never ran.
- INDEPENDENTLY, the tool's `_scour_hint` auto-armed `erodible_bed=True` from the
  word 'scour'. So the run LOOKED morphodynamic (erodible bed accepted, layers
  published) while coupling no GAIA - a false green.

Fix (one source of truth, not keyword whack-a-mole),
`server/src/.../telemac/river_dye/river_dye.py`:
- The composer FORCES `substance_class='sediment'` whenever `erodible_bed` is
  armed (explicit knob OR the scour auto-arm), so the erodible_bed gate and the
  classification gate cannot diverge. An armed erodible bed can never end
  tracer-classified (asserted in the composer AND in a test) - the honesty floor:
  the deck couples GAIA or the run does not claim morphodynamics.
- `SCOUR_KEYWORDS` (scour/erosion/erod/bedload/degradation/aggrad/mobile bed/...)
  is now the ONE vocabulary shared by `classify_substance` (routes to sediment)
  AND the tool `_scour_hint` (auto-arms erodible_bed), so pure-prompt phrasing
  routes right even without the knob. Worker unchanged (it already trusted
  `substance_class`); server-only fix, daemon restart, no image rebuild.

Live re-seed (fixed): new case `01KZPX3ZJZ8DS7MS2NEEJV7N29`, run
`01KZPX5ED64AJJP9RVBBRV1P8E`.
`!run telemac_river_dye(location='Snake River near Twin Falls, Idaho', substance='scour', erodible_bed=True, morphological_factor=5.0, grain_size_um=300.0, bed_thickness_m=5.0, sim_duration_s=900)`
Physics assertions (downloaded from the run prefix, not layer counts):
`status=ok`, `correct_end=True`, `substance_class=sediment`; t2d deck
`COUPLING WITH = 'GAIA'`; `gaia_river.cas` `BED LOAD FOR ALL SANDS = YES`;
`telemac_sediment_deposition.tif` present and SIGNED - max scour 254.9 mm, max
deposition 199.6 mm, net bed mass -519,603 kg (net scour). The old
`01KZPTN56PMHR0NVM6G01FQSBC` is superseded junk (a seeder case; noted for NATE's
dock cleanup, not deleted).

## Follow-up (recorded, not fixed here): seeder grades layer-count

The seeder graded this run OK on layer count and passed a wrong-physics run. The
stronger criterion is a PER-TEMPLATE physics assertion (like the
`telemac_sediment_deposition.tif` + `substance_class=sediment` check above) - a
deposition-raster / coupling-keyword gate per engine, not just a count. Queued.
