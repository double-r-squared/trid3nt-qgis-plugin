# services/workers/ -- solver worker code

Worker code for the physics/ML engines the agent dispatches through the shared
`run_solver` / `wait_for_completion` seam (`tools/simulation/solver.py`). This
is a LOCAL-FIRST repo: workers run on this machine, dispatched by the agent's
local solver backend -- there is no live Cloud Run / AWS Batch deploy here.

## Two local dispatch mechanisms

- **Docker image (`local-docker`)** -- SFINCS, GeoClaw, SWAN, TELEMAC, ELMFIRE.
  Each has a real, locally-buildable `Dockerfile` in its subdirectory. Two
  I/O patterns are in play:
  - GeoClaw / SWAN: `--network host`, boto3 IN the image; the entrypoint reads
    its manifest straight from the object store (MinIO locally, via
    `--manifest-uri s3://...` + the standard `AWS_ENDPOINT_URL` /
    `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `TRID3NT_OBJECT_STORE=s3`
    env block) and uploads its own outputs.
  - SFINCS / TELEMAC / ELMFIRE: bind-mounted rundir (`-v <rundir>:/data` or
    `/deck`), NO boto3 in the image. The agent stages `manifest.json` into the
    rundir before launch; the container just reads/writes local files, and the
    agent-side supervisor uploads the mounted outputs + writes
    `completion.json` afterward.

  Build with the documented `docker build` line for the engine (see
  `docs/site/install.md`), or `scripts/build_<engine>_image.sh` where one
  exists (currently `build_telemac_image.sh`):

  ```sh
  docker build -t trid3nt-local/geoclaw:latest -f services/workers/geoclaw/Dockerfile .
  docker build -t trid3nt-local/swan:latest -f services/workers/swan/Dockerfile .
  docker build -t trid3nt-local/telemac:latest services/workers/telemac/   # or scripts/build_telemac_image.sh
  ```
  SFINCS is pulled, not built (`docker pull deltares/sfincs-cpu:sfincs-v2.3.3`).
  ELMFIRE's proven local image (`trid3nt/elmfire:dev`) predates and is
  independent of `services/workers/elmfire/Dockerfile` (that Dockerfile targets
  the AWS Batch cloud lane -- see below).

- **Exec mode (no image)** -- MODFLOW, SWMM, Landlab, OpenQuake. The agent runs
  these directly on the host, not in a container:
  - MODFLOW: the `mf6` static binary via `TRID3NT_MF6_BIN` (a subprocess, no
    Docker at all).
  - SWMM / Landlab / OpenQuake: pip packages already installed in the agent
    venv (`pyswmm`, `landlab`, `openquake-engine`); run in-process (SWMM's
    dev-primary path) or as an `exec` subprocess of a small runner script
    (`run_inp.py` / `run_chain.py` / `run_oq.py`).

Env gates + measured runtimes for every engine: `docs/site/engines.md`.

## Cloud-lane Dockerfiles that are NOT part of local dispatch

`modflow/`, `swmm/`, `landlab/`, `openquake/`, `elmfire/`, and `canopy/` also
carry a `Dockerfile`. Those build AWS Batch worker images (a scale-beyond-local
path this repo does not deploy) -- read as "FILE-ONLY SCAFFOLD" / "AWS Batch
worker" in their own header comments. Locally these four engines run exec-mode
per above; `elmfire` runs its separately-built `trid3nt/elmfire:dev` dev image
instead of `elmfire/Dockerfile`'s cloud build; `canopy` has no local dispatch
wiring yet (no `LocalSolverSpec` registered). `qgis/Dockerfile` is a separate
QGIS-Processing-worker concern (job-0308, EC2/cloud), unrelated to the solver
dispatch seam above.
