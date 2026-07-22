# TRID3NT Local -- Engines

All eight engines run locally. Three execution mechanisms are in play, all behind the same
`run_solver` / local-supervisor seam the cloud build uses (the supervisor stages deck inputs
into `$TRID3NT_RUNS_DIR/<run_id>/`, launches the engine, uploads outputs to MinIO, and writes
`completion.json` -- identical contract to the AWS Batch workers):

- **binary subprocess** -- a static binary invoked directly (MODFLOW)
- **docker (`local-docker`)** -- a container with the rundir mounted at `/data` (SFINCS) or
  pulling its deck from MinIO via `--network host` (GeoClaw, SWAN)
- **in-process / pip subprocess** -- pure-Python engines from the agent venv (SWMM in-process;
  Landlab and OpenQuake as `exec` subprocesses)

Every engine below has been proven end-to-end locally BOTH tool-direct (a
`scripts/run_*_direct.py` harness) and LLM-driven (`qwen3:8b-16k` called the composer on turn 1
with 0 nudges) -- evidence in `docs/proof/README.md` of the `trid3nt-local` repo.

Runtimes are from the reference consumer box (8-GB-GPU desktop; solves are CPU-bound) at the
small/coarse AOIs used in the proofs -- they scale with AOI and resolution.

---

## Engine matrix

| Engine | Domain | Mechanism | Env gates | Rough local runtime |
|--------|--------|-----------|-----------|---------------------|
| **MODFLOW 6** | groundwater (17 archetype composers) | `bin/mf6` binary subprocess (USGS 6.5.0 static, no runtime deps) | `TRID3NT_MODFLOW_LOCAL=1`, `TRID3NT_MF6_BIN` | solve itself sub-second (~0.07-0.3 s for an archetype deck); the turn is dominated by LLM inference |
| **SFINCS** | coastal/pluvial flood | docker `deltares/sfincs-cpu:sfincs-v2.3.3`, rundir mounted at `/data` | `TRID3NT_SOLVER_BACKEND=local-docker`, `TRID3NT_SFINCS_IMAGE`, `TRID3NT_RUNS_DIR` | ~40 s for a small pluvial AOI (~23k active cells) incl. hydromt build; minutes end-to-end with fetch + postprocess |
| **SWMM (PySWMM)** | urban stormwater | **in-process** pyswmm (`run_swmm_local`, the dev primary path; a `LocalSolverSpec` also exists for an out-of-process lane) | none beyond the defaults (pyswmm 2.1.0 in the agent venv) | ~2 min for a 3-block box / 10-yr 1-hr storm -- **see the slow-local caveat below** |
| **Landlab** | landslide susceptibility | subprocess `run_chain.py` (exec_kind=exec, pip-only) | none beyond the defaults | ~42 s (4 km box, 30 m grid, 25 Monte Carlo iterations) |
| **OpenQuake** | seismic hazard (PSHA) | subprocess `run_oq.py` -> `oq engine` (exec_kind=exec) | `TRID3NT_OQ_BIN` (venv `oq`); one-time `oq engine --upgrade-db` on first run | ~41 s (SF Bay PGA, 475-yr return, 20 km grid, real-fault sources) |
| **GeoClaw** | tsunami / dam-break inundation | docker `trid3nt-local/geoclaw:latest` (locally built; Clawpack 5.14 Fortran compiled into the image), `--network host`, deck pulled from MinIO via `--manifest-uri` | `TRID3NT_GEOCLAW_IMAGE` | ~40 s solve for a 30-min tsunami window, 2 AMR levels, 6 frames (the Fortran compile is baked into the image, not per-run) |
| **SWAN** | spectral waves | docker `trid3nt-local/swan:latest` (locally built), same manifest pattern as GeoClaw | `TRID3NT_SWAN_IMAGE` | ~2.5 min for a stationary 101x101 wave field |
| **pfdf debris flow** | post-fire debris-flow hazard | **in-process** `model_debris_flow` composer over the vendored `pfdf` 3.0.4 wheel (USGS Staley 2017 likelihood + Gartner 2014 volume + Cannon 2010 hazard class) | none (pfdf ships in the agent venv) | fast -- the AOI is clamped to <= 0.15 deg per side (`AoiTooLargeError`), keeping the 30 m watershed analysis to a few-hundred-pixel grid |

Notes:

- `TRID3NT_SOLVER_BACKEND=local-docker` and `TRID3NT_MODFLOW_LOCAL=1` are **independent gates**:
  MODFLOW checks its own flag first, so flipping the solver backend never affects it.
- GeoClaw and SWAN containers reach MinIO at `127.0.0.1:9000` via `--network host`; their
  `build_argv` closure rewrites the staged `--run-id` to the launcher's ULID so container,
  supervisor, and `wait_for_completion` all poll the same S3 prefix.
- The agent must run **inside the docker group** (`sg docker -c 'bash scripts/start_agent.sh'`)
  for the three container engines to dispatch.
- Cancellation works locally: the cancel chain kills the detached process group (`local-exec`)
  or the named container (`local-docker`).

---

## The SWMM slow-local caveat

SWMM is the one engine where "runs locally" and "runs quickly locally" diverge. The quasi-2D
node-link mesh that the composer builds is CPU-heavy in pyswmm, and it runs **in-process** in
the agent. The measured spread:

- Small scenario (downtown 3-block box, 10-yr design storm, coarsest resolution): ~2 minutes,
  LLM-driven e2e PASS.
- The direct tool sweep's default-argument invocation of `run_swmm_urban_flood` **timed out at
  1500 s** -- a default-sized AOI at default resolution is not a practical local run.

Keep SWMM AOIs to a few blocks and take the coarsest resolution the granularity gate offers.
The resolution-picker card is the lever: it shows the autoscaler's suggestion and lets you
coarsen before the solve starts.

## Physics caveats carried over from the proofs

- **GeoClaw**: with ETOPO-fallback bathymetry (no CUDEM coverage) a small tsunami scenario can
  complete with `max_depth_m=0.0` overland -- the wave crosses the domain but the
  overland-mask postprocess yields zero inundation. The layer still publishes (honesty floor
  passes: a valid COG was emitted). Real inundation needs better nearshore bathymetry.
- **SWAN**: the demo boundary synthesizer picks the wave side heuristically (W for west-coast
  AOIs); a wrong side produces an all-zeros wave field that surfaces as a typed
  `PostprocessSwanError` rather than a fake layer -- expected honest behavior.
