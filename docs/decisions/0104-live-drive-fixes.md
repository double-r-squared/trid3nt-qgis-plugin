# ADR 0104 -- Live-drive bug-fix wave (six defects from remote driving)

Status: accepted (2026-08-04, NATE live remote session)

## Context

Six defects surfaced while NATE drove the live daemon remotely (the mesh-run
driver report + the oil-slick investigation). Each is an integrity or
availability leak: a silent hang, an unkillable solve, a poisoned process, a
silently-degraded mesh, a parse crash, and a dangling layer handle.

## Decision

### Bug 1 -- TELEMAC degenerate-reach gate + hard mesh watchdog

"Longview, Washington" snapped the release-seeded reach search to a 292 m
NHDFlowline stub (comid 24521434); with the 500 m default `channel_width_m` the
channel is wider than the reach is long, the offset banks fold, and gmsh's
`mesh.generate(2)` busy-looped in C for 32+ min (docker-killed). The in-process
`SIGALRM(240)` watchdog never fired -- a C busy-loop does not return to Python
to take the signal.

Two layers, in `services/workers/telemac/telemac_river_dye_build.py`:

- **Pre-mesh gate.** `validate_reach_geometry(cl, cfg)` computes the centerline
  arc length vs the effective channel width and raises the typed
  `ReachDegenerateError` when `length/width < 2.0` (aspect gate), BEFORE any
  gmsh work. The error names the corrective args (a longer `reach_length_km`,
  an explicit `river_name`, or `bank_source="constant_ribbon"` with a smaller
  `channel_width_m`) -- the 0091 gate pattern, never a hang, never a silent bad
  mesh. Called at the top of `build_channel_mesh` AND in the guarded wrapper
  (fast, no fork).
- **Hard watchdog.** `build_channel_mesh_guarded` runs the whole gmsh build in a
  killable child process (`multiprocessing` fork) with a wall-clock deadline
  (`_MESH_WALLCLOCK_TIMEOUT_S=300`, env `TELEMAC_MESH_TIMEOUT_S`). On the
  deadline the child's process GROUP is SIGKILLed -- which a C busy-loop cannot
  swallow -- and `MeshBuildTimeout` is raised. The dead in-process SIGALRM is
  removed (clean-as-you-go: it was demonstrably ineffective).

The worker writes `error_code=TELEMAC_REACH_DEGENERATE` metrics; the server
(`model_river_dye_release_scenario`) maps them to the typed, retryable
`TelemacReachDegenerateError` (`.suggestions`, rides the tool-retry loop) via
`_raise_if_reach_degenerate`, mirroring the banks-unavailable gate.

**Reach-SELECTION characterization (report-only, NOT redesigned this wave):**
the release-seeded reach search picks the NHDFlowline nearest the release point
without preferring longer or main-stem lines, so a bare release coordinate near
a confluence (Longview = Columbia x Cowlitz) can land on a 292 m tributary stub.
A durable fix would rank candidate flowlines by length / stream order (prefer
the main stem), or snap to the named GNIS mainstem when `river_name` is absent
but a dominant nearby flowline exists. Left as an open issue -- the degenerate
gate turns the failure honest and actionable in the meantime.

### Bug 2/3 -- SWMM subprocess isolation (killable deadline + dead lock)

`model_urban_flood_swmm` ran the pyswmm solve via a bare `asyncio.to_thread`
with no deadline (a 588k-cell AOI ran 47+ min unkillable), and a stuck/completed
`Simulation` poisons the interpreter (pyswmm single-instance limit) so every
later SWMM run failed until a daemon restart.

The solve moves to a KILLABLE child process (`raster_cell_mesh.
_solve_swmm_in_subprocess` -> `python -m trid3nt_server.agent.mesh.
_swmm_solve_subprocess`). Thin files-on-disk seam: `inp_path` + grid shape +
`sample_every_steps` in; `.out`/`.rpt` (pyswmm) + `peak.npy` + `meta.json` out.
The single shared solve loop is `run_swmm_simulation` (never duplicated). The
mass-balance honesty gate + `RunResult` construction stay in the parent,
byte-identical. Each solve is a FRESH interpreter -> the single-instance lock is
dissolved; a runaway is SIGKILLed at `_swmm_solve_timeout_s(n_active)`
(granularity-gate estimate x4, clamped to [300 s, 3600 s], env
`SWMM_SOLVE_TIMEOUT_S`) -> typed `SWMM_SOLVE_TIMEOUT` naming a coarser
resolution / smaller AOI.

### Bug 4 -- stale positional fetch signatures + LOUD fallbacks

`_fetch_dem_for_urban` and `_fetch_buildings_for_urban` called the post-fold
registry closures (`_promoted(**kwargs)`, keyword-only) with a POSITIONAL
`bbox`; both `TypeError`ed and were swallowed, so the DEM silently fell to 10 m
and buildings silently dropped to ZERO footprints. Fixed to keyword calls
(`bbox=bbox`). The remaining legitimate absences are now LOUD: warnings on
degrade, and `_urban_envelope_suffix` labels the peak layer name -- obstacles
applied, an honest "no building obstructions - OSM footprints unavailable"
label, and a "10 m DEM fallback" note.

**Buildings-absence labeling decision:** LABELED PROCEED (per NATE's lean, audit
doctrine). A mesh whose obstruction layer silently vanished is the integrity
leak NATE outlawed, so buildings absence is named in the envelope name (and the
`n_buildings_dropped=0`-after-attempt signal rides the mesh stats), but a
buildings-fetch miss does NOT hard-fail the flood -- the mesh still solves,
honestly labeled as obstruction-free.

The same stale-signature defect in `sfincs_forcing_autowire.py` (SFINCS building
obstacles) is fixed to keyword too (a SFINCS seam -> flood canary run).

### Bug 5 -- SWMM deck END clock rolls past 24 h

`raster_cell_mesh.build_swmm_mesh` authored `END_TIME=f"{end_hh:02d}:00:00"`,
producing `"25:00:00"` for a 24 h storm + 1 h drain-down. swmm-api's TIME parser
(`strptime %H:%M:%S`) rejects it, crashing the deck round-trip. Now the end
datetime is derived (`start + timedelta(hours=...)`) and split so hours beyond
24 roll into `END_DATE` and `END_TIME` stays a valid 0-24 h clock.

### Bug 6 -- oil-slick upload-before-register

The oil run registered `s3://<bucket>/<run_id>/slick.geojson` unconditionally
whenever `substance_class=="oil"`, never checking the object existed. The worker
writes the slick fail-open (a drogues-parse failure skips it), so a registered-
but-missing layer resulted. **Audit verdict:** the supervisor's
`output_uris`/`completion.json` path DOES hold upload-before-register (it uploads
only globbed files that exist); only the oil-slick VECTOR layer fabricated its
URI and skipped the discipline. Fix: `_s3_object_exists` HEAD-guards the
registration -- a missing object is an honest skip, never a dangling handle.

The `mesh_preview.geojson` registration shares the unconditional pattern but is
a gate artifact the worker always writes on a successful mesh; left as a noted
follow-up, not guarded this wave.

## Consequences

- TELEMAC worker code changed -> worker image rebuilt.
- Each SWMM solve now spawns a child process (~1-3 s startup); acceptable for a
  minutes-long solve and the correctness it buys.
- New module `_swmm_solve_subprocess.py`; SIGALRM mesh watchdog removed.
- Registry unchanged (no tool added/removed).
