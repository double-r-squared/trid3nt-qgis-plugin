# E2E test harness for the V&V wave (methodology SIGNED OFF 2026-07-24)

NATE sign-offs: case = Hurricane Harvey / Houston; scope = core loop +
metamorphic check + split-sample validation; EA 2D benchmark suite = QUEUED
(backlog, separate verification deck). Concrete run inputs (AOI, window,
split) get a final NATE look before any live execution, and every live run
asks permission first (methodology rule).

## Principle: assert the machinery, never the model

The harness asserts that the TOOLS work: envelopes complete and honest,
datums reconciled and recorded, dropped observations listed with reasons,
child-model lineage intact, metrics mathematically correct, monotonicity
holds. Model skill (NSE, CSI values) is a FINDING the harness reports -
never a pass/fail gate. Skill judgments stay with NATE.

## Levels

- L0 offline unit/golden: per-parser tests on captured artifacts, known-answer
  metrics. Lands with the wave build (vv-wave-build workflow).
- L1 registry/retrieval smoke: all 9 tools direct-callable via TOOL_REGISTRY;
  model-free retrieval check runs when the corpus WIP + corpus-additions.yaml
  land in tool_query_corpus.yaml.
- L2 live closed loop (this doc): direct-call script, real data, no chatbot
  drive.
- L3 NATE-in-QGIS: natural-prompt chat turn driving the verify chain
  conversationally + visual pass.

## L2: Harvey / Houston closed loop

Proposed concrete inputs (PENDING final NATE look before live):
- AOI: a Buffalo Bayou / west-Houston sub-basin - small enough for fast
  SFINCS iteration, inside the dense STN HWM cluster.
- Window: 2017-08-25 to 2017-09-01 (landfall + peak rainfall).
- Forcing: observed precipitation via existing fetchers.
- Obs: live USGS STN HWM fetch + USGS stream gauges.

Chain:
1. run flood sim (baseline)
2. read_run_diagnostics - run sound? (A)
3. fetch_high_water_marks - live STN (C)
4. extract_model_at_observations - pairing, datum honesty (C)
5. compute_skill_metrics - baseline skill (B)
6. set_sfincs_parameters - one manning tweak (D)
7. re-run, re-score - skill delta, lineage intact

Riders:
- METAMORPHIC: identical sim with 1.5x precipitation; assert flooded volume
  and peak depth are non-decreasing. Catches unit/sign/forcing-window bugs
  deterministically; needs zero observation data.
- SPLIT-SAMPLE (spatial split - one event cannot split temporally):
  calibrate against USGS stream gauges (or one HWM subset), score against
  HELD-OUT HWMs the calibration never touched. Held-out skill is the honest
  number; both reported.

Discipline: exact-args/bbox OK here (mechanical contract check, not a prompt
test); ask permission per live run; log costs; UPSTREAM failures (STN outage
etc.) surface as typed upstream errors, never internalized as harness bugs.

## Backlog

- EA 2D benchmark suite (Neelz & Pender SC120002): canonical solver
  verification deck, synthetic geometries, published cross-model numbers.
  Build after the L2 harness lands.
- Signature evaluation (compute_hydrological_signatures: FDC, recession) and
  metamorphic checks as registered tools: back-burner with group F.
