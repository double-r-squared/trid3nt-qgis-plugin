# TRID3NT Local -- Engines

ONE solver engine ships in this repo: **TELEMAC**, in a locally built container, plus the
GPL-isolated **OceanMesh2D** meshing environment beside it. Both run through the same
`run_solver` / `wait_for_completion` seam (`trid3nt_server/workflows/solver/solver.py`): the
supervisor stages deck inputs into `$TRID3NT_RUNS_DIR/<run_id>/`, launches the container over
that bind-mounted rundir, uploads the outputs to MinIO and writes `completion.json`. The worker
is the engine room -- a staged run directory in, results out, no network and no opinions of its
own.

An engine counts as proven end to end only through the surface a user drives: a live
run against the running daemon, gates answered the way the dock answers them, judged on
the artifacts the run itself wrote rather than on the turn finishing. That is what a
CANARY is - a declared question, the same one every time - and each one closes by
assembling a delivery packet (the canvas panels, the composite, the charts, an animation
where the solve is time-stepped) and refusing when a piece is missing. The declarations
live in `dev/testing/canaries.py`; the packets they produce are rendered under
`run/proof/<template>/<run-id>/`, delivered from there, and swept after seven days - a
packet is a delivery, not an archive.

Runtimes are from the reference consumer box (8-GB-GPU desktop; solves are CPU-bound) at the
small/coarse AOIs used in the proofs -- they scale with AOI and resolution.

---

## Engine matrix

| Engine | Domain | Mechanism | Env gates | Rough local runtime |
|--------|--------|-----------|-----------|---------------------|
| **TELEMAC** | free-surface hydrodynamics: river dye and tracer release, rain on grid, scour and sediment plume, 3D stratified flow, harbour agitation (ARTEMIS) | docker `trid3nt-local/telemac:latest` (locally built; opentelemac v9.0.0 conda env), rundir bind-mounted at `/data`, no boto3 in the image | `TRID3NT_SOLVER_BACKEND=local-docker`, `TRID3NT_TELEMAC_IMAGE`, `TRID3NT_RUNS_DIR` | ~2.8-3.2 min (167-190 s measured across several real local runs, `wall_s` in `telemac_metrics.json`) for reach-scale unstructured meshes (4.3k-18.6k nodes / 7.2k-35.5k elements); a mesh-only preview run (no physics solve) completes in ~7 s |
| **OceanMesh2D** | unstructured mesh generation from a sizing function | docker `trid3nt-local/mesh:latest` (locally built), an environment only: the recipe bind-mounts its driver and a rundir and overrides the entrypoint | `TRID3NT_MESH_IMAGE`, `TRID3NT_GSHHG_SHP` for a shoreline-driven build | seconds to a few minutes, set by the sizing function rather than the AOI alone |

Notes:

- The agent must run **inside the docker group** (`sg docker -c 'bash scripts/start_agent.sh'`)
  for either container to dispatch -- unless the machine runs rootless Docker, where no group
  membership is needed.
- The rundir is bind-mounted (`-v <rundir>:/data`) and the AGENT-side supervisor does the
  upload; the container reaches no network of its own.
- Cancellation works locally: the cancel chain kills the named container.
- Images are built from the worker Dockerfiles: `scripts/build_telemac_image.sh`, or
  `docker build -t trid3nt-local/mesh:latest workers/mesh/`. Worker code is INERT until its
  image is rebuilt -- edit, rebuild, then smoke THROUGH the image.

---

## What is not here

Engines named by older versions of this page -- MODFLOW, SFINCS, SWMM, Landlab, OpenQuake,
GeoClaw, SWAN, ELMFIRE, the pfdf debris-flow composer -- left the tree with their workers and
their templates. They are not gated off; they are absent. `workers/` holds exactly the two
directories above, and a question this repo cannot answer is refused rather than approximated.
