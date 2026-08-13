# ADR 0187 - GeoClaw completion: fgout smooth-animation PROMOTION + Thacker V&V template (LANDED)

Status: Accepted
Date: 2026-08-08

## Context

ADR 0186 landed the fgout engine KNOB (a gated fgout monitor baked into the
worker deck) and DEFERRED the Thacker analytic V&V with a corrected, DEM-free
recipe. This batch finishes both off those corrected recipes:

1. **fgout PROMOTION** -- wire the already-baked fgout frames into the agent-side
   product path so they BECOME the scrubber animation series.
2. **Thacker V&V** -- the DEM-free composer branch + a chart-led validation
   template graded against Thacker's (1981) closed form.

Both were verified LIVE through the product path (the rebuilt image); no
unverified engine code shipped (the honesty-floor / no-unverified-engine-code
doctrines that made 0186 defer Thacker).

## Decision

### Piece 1 - fgout smooth-animation PROMOTION (knob-only, no image rebuild)

Reconnaissance found the load-bearing gap: `_download_batch_geoclaw_outputs`
only harvested `fort.*` / `gauge*.txt`, so the fgout frames (already emitted by
the baked worker knob + listed in `completion.json.output_uris`) never reached
the agent. Two agent-side changes close it:

- `inundation._is_geoclaw_output_key`: also matches `fgout*` -> the frames
  download alongside fort.q.
- `postprocess_geoclaw`: `_discover_fgout_frames` + a shared
  `_read_frames_to_grids` parse the fgout ascii frames with the EXISTING fort.q
  uniform-grid parser (the frames ARE the fort.q layout: an 8-field header,
  AMR_level=0, a single uniform patch, col0=h -- verified against the ADR 0186
  live frames), with NO AMR flatten and NO clawpack import. When present the
  fgout frames BECOME the animation series (`anim_grids`); the fort.q peak (+ any
  fgmax override) stays the peak. The SAME ocean mask (from the fort.q t=0
  still-water frame) is applied to the fgout frames so they are overland-consistent.
- `geoclaw_inundation`: new `fgout_frames` knob (threaded ONLY when > 0);
  `GeoClawRunArgs.fgout_frames` already existed (0186). Corpus: three
  smooth-animation phrasings on the inundation template (retrieval-verified).

**Design call:** KNOB-ONLY, not a promoted template. fgout is a rendering knob on
the SAME inundation question ("peak depth + a run-up animation"), not a distinct
question CLASS, so it rides `geoclaw_inundation` -- no registry / EXPECTED_TEMPLATES
bump for Piece 1 (231 unchanged before Piece 2).

**Live verification (product path):** the ADR 0186 Crescent City fgout run
(5 fort.q + 12 fgout) re-postprocessed through the download+promote path ->
**12 fgout frames became the animation series** (12 frame COGs) with the fort.q
peak (max_depth 1.04 m) retained; a pinned-frame Esri proof map shows the run-up
along the harbor breakwater. Showcase Case `01KZGA1BVJFWQ7JCVYSG7RTMMG` seeded
through the daemon: **14 layers** (peak + 12 fgout frames + mesh).

### Piece 2 - Thacker paraboloid-basin V&V (DEM-free branch + template + rebuild)

The recipe cited `clawpack examples/tsunami/bowl-radial`, but that example uses a
Gaussian hump, NOT the analytic surface; and its period formula
`T = 2*pi*a/sqrt(8 g h0)` is the CURVED radially-symmetric Thacker solution (the
planar `bowl-slosh` uses `sqrt(2 g h0)`). Resolved by implementing the CURVED
solution (matching the recipe's period gate) with the exact still-surface qinit.

- **Shared analytic** (`trid3nt_contracts.geoclaw_thacker`): the closed form
  (`eta(r,t)`, depth, shoreline, `thacker_reference`) both the worker deck-author
  and the agent grader import, so they agree by construction. `g = 9.81` pinned.
- **Worker deck** (`setrun_builder`, `geoclaw-spec-5`): `scenario="thacker"` +
  `bowl_a_m/bowl_h0_m/bowl_eta_amp` (strict-allowlist extended; the parser rejects
  bowl fields on any geographic scenario). A dedicated `render_thacker_setrun_py`
  emits the PLANAR Cartesian deck (`coordinate_system=1`, `capa_index=0`,
  `num_aux=1`, `friction_forcing=False`, all four `bc_*='wall'`, `qinit_type=4`)
  with a center gauge + a dense +x-axis gauge line; `render_thacker_topo` +
  `render_thacker_qinit` write the paraboloid bed + the analytic t=0 surface as
  pure-python topotype-1 files (NO clawpack, NO fetched DEM) -- so the whole deck
  is offline-unit-testable. Cartesian conventions taken from the image's
  `bowl-radial` setrun (authoritative), not guessed.
- **DEM-free composer branch**: `GeoClawRunArgs` gains the bowl fields + a
  model-validator enforcing bowl-mode / geographic-mode MUTUAL EXCLUSION (loud
  typed error, never silent). `stage_geoclaw_manifest` stages NO inputs for a
  thacker run (the worker generates topo + qinit); a staged DEM is refused loudly.
  A separate `model_geoclaw_thacker_validation` composer (the bowl is
  non-geographic + chart-led, so it does NOT ride the geographic
  `model_geoclaw_inundation`) stages -> solves -> grades -> emits.
- **Grader + emission** (`compute_thacker_vandv`, chart spec): the center gauge
  gives numerical PERIOD (autocorrelation, robust to the wetting/dry-front
  wiggles) + central amplitude; the axis gauge line gives the moving SHORELINE;
  the fort.q LEVEL-1 (base) integral gives a threshold-free, overlap-free mass
  conservation proxy. ONE Vega-Lite overlay (numerical vs analytic eta(0,t))
  emitted to the charts window; the deltas ride the caption strip. CHARTS +
  SCALARS ONLY (no geographic COG -- the bowl is not an AOI); a
  `SyntheticInput`-labeled note marks it a non-US idealized V&V, NOT a hazard target.
- **Template** `geoclaw_thacker_validation` (question CLASS = "does the wet-dry
  SWE+AMR solver reproduce Thacker's exact bowl solution + conserve mass?"):
  registry 231 -> 232, EXPECTED_TEMPLATES +1; corpus + retrieval-verified;
  `model_validation` primary (cross-lists `simulation_modeling`).

**Image rebuilt** (`docker build -f <abs>/Dockerfile -t trid3nt-local/geoclaw:latest
<abs>/trid3nt-local`, 0148/0158): build smoke green; `docker history` references
`/home/nate/Documents/GRACE-2` ZERO times (clean provenance).

**Live V&V (rebuilt image, product path):** a=1 m, h0=0.1 m, A=0.5, 2.5 periods.
period **2.20 s vs analytic 2.24 s (1.9%)**, central amplitude **0.1154 vs
0.1155 m (0.1%)**, shoreline **0.80-1.20 m vs 0.76-1.32 m**, mass drift **~5%**
(closed frictionless basin), RMS(eta_num-eta_ana) **0.015 m**. The numerical run
tracks the closed form with honest wetting/dry-front wiggles at the moving
shoreline + small numerical amplitude decay. Showcase Case
`01KZG9Y8CP0KGP4XJG2XY5KMKH` seeded (1 chart, no map layer -- the chart-led design).

## Consequence

- fgout PROMOTED as an inundation knob (registry/EXPECTED_TEMPLATES unchanged by
  Piece 1). Thacker V&V template LANDED (registry 231 -> 232, EXPECTED_TEMPLATES
  +1). Non-fgout / non-thacker geographic decks byte-identical (unit-locked).
- Offline slice green: geoclaw suites + the new fgout + thacker + contract + pin +
  hygiene tests (150 passed / 1 skipped + 37 contracts + 17 pin).
- The whole Thacker deck is clawpack-free + offline-unit-testable (topotype-1
  pure-python topo + qinit); only the Fortran solve needs the image.

## Files changed

- `contracts/src/trid3nt_contracts/geoclaw_thacker.py` (NEW -- shared closed form)
- `contracts/src/trid3nt_contracts/geoclaw_contracts.py` (thacker scenario + bowl fields + mutual-exclusion validator + aliases)
- `services/workers/geoclaw/setrun_builder.py` (spec-5, thacker branch: render_thacker_setrun_py/topo/qinit + build_geoclaw_deck)
- `server/src/trid3nt_server/agent/workflows/geoclaw/postprocess_geoclaw.py` (fgout discovery/read/promotion; compute_thacker_vandv + chart spec)
- `server/src/trid3nt_server/agent/workflows/geoclaw/inundation/inundation.py` (fgout download key + fgout_frames knob)
- `server/src/trid3nt_server/agent/workflows/geoclaw/run_geoclaw.py` (bowl-field threading + DEM-free thacker staging)
- `server/src/trid3nt_server/agent/workflows/geoclaw/thacker_validation/{__init__,thacker_validation,corpus}.{py,yaml}` (NEW template + composer)
- `server/src/trid3nt_server/agent/categories.py`, `server/src/trid3nt_server/agent/tools/__init__.py` (register geoclaw_thacker_validation)
- `server/src/trid3nt_server/agent/workflows/geoclaw/inundation/corpus.yaml` (smooth-animation phrasings)
- tests: `server/tests/test_geoclaw_fgout_animation.py` (NEW), `server/tests/test_geoclaw_thacker.py` (NEW), `services/workers/geoclaw/test_setrun_builder.py` (+thacker), `contracts/tests/test_geoclaw_contracts.py` (+thacker), pins in `server/tests/test_door_dissolution.py` + `server/tests/test_catalog_surfacing.py`
- proofs: `docs/proof/templates/geoclaw_inundation_fgout_animation_crescent_city.png`, `docs/proof/templates/geoclaw_thacker_validation_center_gauge_overlay.png`
