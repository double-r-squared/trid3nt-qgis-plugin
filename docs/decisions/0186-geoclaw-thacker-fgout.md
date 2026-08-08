# ADR 0186 - GeoClaw fgout smooth-animation engine knob (LANDED) + Thacker V&V (corrected recipe, deferred)

Status: Accepted
Date: 2026-08-08

## Context

ADR 0185 SCOPED two GeoClaw builds with ready-to-execute recipes: the Thacker
analytic SWE V&V (#9) and the fgout smooth-animation frames (#10). This batch
executes them off those recipes. During reconnaissance two recipe assumptions
proved WRONG against the live surface, which reshaped the landing:

1. **Postprocess runs AGENT-SIDE, not in the worker.** The deployed
   `trid3nt-local/geoclaw:latest` image does NOT contain
   `services.workers._geoclaw_postprocess` (the Dockerfile only COPYs
   `services/workers/geoclaw/`), so the entrypoint's worker-side postprocess
   import fails ("non-fatal") and the LIVE path is the agent-side
   `postprocess_geoclaw.py`. The agent venv has NO clawpack. So the 0185 recipe's
   "read fgout frames via `fgout_tools.FGoutFrame`" is not reachable on the live
   path -- the fgout reader must be clawpack-free.

   Resolution: emit fgout with `output_format='ascii'`. Each fgout frame
   (`fgout0001.qNNNN`) then lands in the SAME fort.q-style uniform-grid layout the
   existing `parse_fort_q_frame` + `rasterize_frame_to_grid` already read -- a
   uniform single patch, no AMR flatten, no clawpack import agent-side. This is
   the recipe's stated intent ("reusing rasterize_frame_to_grid into the existing
   scrubber frame convention") realized without the unreachable dependency.

2. **The byte-identical test-lock forces a gated flag, not an always-on block.**
   Existing tsunami/surge decks are locked byte-identical by
   `test_setrun_builder.py`. Emitting an fgout block on every tsunami/surge deck
   would break that lock. So fgout is gated behind a NEW build_spec field
   (`fgout_frames`, default 0 = no fgout block), which also carries the
   `geoclaw-spec-3 -> spec-4` strict-allowlist bump the 0185 recipe anticipated.

## Decision

### fgout smooth-animation frames (#10) - ENGINE KNOB LANDED + LIVE-VERIFIED

Landed the fgout monitor as a gated engine knob through the FULL worker cycle:

- `setrun_builder.py` (`geoclaw-spec-4`): new `fgout_frames` field
  (`_KNOWN_SPEC_FIELDS` + `GeoClawBuildSpec` + `parse_build_spec`, `>= 0`
  validated). `render_setrun_py` emits an fgout block (mirroring the fgmax block)
  ONLY for tsunami/surge with `fgout_frames > 0`: `from clawpack.geoclaw import
  fgout_tools`; `FGoutGrid()` with `point_style=2`, a uniform grid over the AOI at
  the AOI-ambient cell size (SAME `dx` as the fgmax grid), `output_format='ascii'`,
  `nx/ny` from the AOI span, `tstart/tend` = the run window, `nout=fgout_frames`,
  appended to `rundata.fgout_data.fgout_grids`. `fgout_frames == 0` emits NO block
  -> byte-identical to a pre-fgout deck (the additive-off invariant, unit-locked).
- `entrypoint.py`: fgout output globs (`_output/fgout*.q*`, `.t*`, `.b*`) so the
  frames upload alongside fort.q.
- `run_geoclaw.py`: matching fgout globs in `GEOCLAW_OUTPUT_GLOBS` (agent
  download) + `build_geoclaw_build_spec` threads `fgout_frames` ONLY when > 0.
- `geoclaw_contracts.py`: `GeoClawRunArgs.fgout_frames: int = Field(0, ge=0)`.

**Live verification (through the REBUILT image, not the stale one):**
- Image rebuilt: `docker build -f
  /home/nate/Documents/trid3nt-local/services/workers/geoclaw/Dockerfile -t
  trid3nt-local/geoclaw:latest /home/nate/Documents/trid3nt-local` (absolute
  -f/context per 0148/0158; build-time smoke green). Provenance: `docker history`
  references `/home/nate/Documents/GRACE-2` ZERO times (clean).
- FGoutGrid API pre-validated against clawpack 5.14 in-container
  (setrun+`rundata.write()` OK, 13x8 uniform grid over the Crescent City AOI).
- Direct-call live solve (Crescent City tsunami, Mw 8.5, 900 s, amr_levels=2,
  `fgout_frames=12`) via `model_geoclaw_inundation` on the rebuilt image:
  status ok, `max_depth_m=0.934` (wet solve). MinIO run prefix
  `01KZG6HSXJZ2FPJNYE1Y9E1EF6` carries **12 smooth fgout frames**
  (`fgout0001.q0001..q0012`) vs the **6 fort.q AMR baseline** frames -- a smooth
  uniform-grid cadence of 900/12 = 75 s/frame at a single resolution, decoupled
  from the coarse/variable fort.q AMR-patch cadence.

Local-first doctrine: fgout is proven as a direct-call ENGINE knob here; PROMOTION
to a first-class `geoclaw_fgout_animation` template (corpus + categories +
`tools/__init__.py` + registry/EXPECTED_TEMPLATES bump) is the follow-up, NOT
landed this batch (no half-built template registered; pins UNCHANGED 231 / 73).
The agent-side postprocess still emits the fort.q-derived scrubber frames; wiring
the fgout frames to REPLACE them as the animation source (when present) is part of
that same template-promotion follow-up.

### Thacker analytic SWE V&V (#9) - CORRECTED RECIPE, DEFERRED (not landed)

Thacker is a FULLY-SYNTHETIC domain (no `fetch_topobathy`), which the 0185 recipe
noted but under-scoped: the WHOLE composer chain
(`model_geoclaw_inundation`) assumes a real AOI DEM (fetch -> reproject ->
offshore-source placement -> flat-ocean gate). A Thacker run must BYPASS that
chain end-to-end, which is a substantial NEW composer path, not a knob. Landing it
unverified would violate the honesty-floor + "no unverified engine code"
doctrines, so it is DEFERRED with this corrected, ready-to-execute recipe:

1. `setrun_builder.py`: `scenario="thacker"` branch. A `maketopo.py`-style helper
   generates BOTH the paraboloid-bowl topo (`B(r) = -h0 (1 - r^2/a^2)`, written as
   a topotype-3 ASCII the deck references) AND the analytic tilted free-surface
   qinit at t=0 -- so NO DEM is staged. New spec-4 fields `bowl_a_m`,
   `bowl_h0_m`, `bowl_eta_amp` (metres, planar CRS: `coordinate_system=1`, NOT
   lat/lon). Frictionless (`manning_n=0`, `friction_forcing=False`), CLOSED wall
   BCs (`bc_*='wall'`), `sim_duration_s ~ 2-3 T`, `T = 2*pi*a/sqrt(8 g h0)`.
2. Worker/composer: a `thacker` path that skips fetch/reproject/source/flat-ocean
   and stages ONLY the build_spec (the topo is worker-generated). This is the real
   work item -- the composer currently has no DEM-free branch.
3. Postprocess: extract x-axis + diagonal gauge series, compute numerical period +
   amplitude + shoreline position, compare to the closed form; report period
   error, amplitude decay (numerical dissipation), shoreline-position error, mass
   conservation.
4. Emission (`model_validation` category, chart-led): ONE figure overlaying
   numerical vs analytic surface at several phases, deltas in the caption strip.
   Charts/scalars only, OR a NEUTRAL-background synthetic raster captioned as a
   paraboloid bowl (NOT a geographic AOI) -- no Esri basemap, mesh wireframe
   overlaid. SyntheticInput-labeled synthetic mechanism fixture; non-US idealized
   V&V cross-check, NEVER a hazard target.
5. Template `geoclaw_thacker_validation` (question CLASS = "does the wet-dry
   SWE+AMR solver conserve mass/momentum vs Thacker's exact bowl solution"):
   corpus + `retrieve_visible_tools` + categories.py (model_validation) +
   `tools/__init__.py`; registry 231 -> +1, EXPECTED_TEMPLATES 73 -> +1.
   Src: clawpack `examples/tsunami/bowl-radial`.

## Consequence

- fgout engine knob LANDED + live-verified through the rebuilt image (spec-4).
  Registry / EXPECTED_TEMPLATES UNCHANGED (231 / 73): a proven engine knob, no
  half-built template registered. Non-fgout decks byte-identical (unit-locked +
  the geoclaw offline slice green: 140 passed / 1 skipped + 34 contracts).
- Thacker DEFERRED with a corrected recipe (the DEM-free composer branch is the
  real remaining work). No unverified engine code shipped.
- Follow-ups: (a) promote fgout to `geoclaw_fgout_animation` + wire fgout frames
  as the scrubber animation source; (b) the Thacker DEM-free composer path +
  closed-form V&V chart + `geoclaw_thacker_validation` template.

## Files changed

- `services/workers/geoclaw/setrun_builder.py` (spec-4, `fgout_frames`, fgout render block)
- `services/workers/geoclaw/entrypoint.py` (fgout output globs)
- `services/workers/geoclaw/test_setrun_builder.py` (+6 fgout unit tests)
- `server/src/trid3nt_server/agent/workflows/geoclaw/run_geoclaw.py` (fgout globs + build_spec threading)
- `contracts/src/trid3nt_contracts/geoclaw_contracts.py` (`GeoClawRunArgs.fgout_frames`)
