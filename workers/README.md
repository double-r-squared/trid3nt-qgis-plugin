# workers/ -- solver worker code

Worker code for the engines the agent dispatches through the shared
`run_solver` / `wait_for_completion` seam (`tools/simulation/solver.py`). This
is a LOCAL-FIRST repo: workers run on this machine, dispatched by the agent's
local solver backend -- there is no live Cloud Run / AWS Batch deploy here.

## The roster

- `telemac/` -- the one solver worker. A bind-mounted rundir (`-v <rundir>:/data`),
  no boto3 in the image: the agent stages `manifest.json` into the rundir before
  launch, the container reads/writes local files, and the agent-side supervisor
  uploads the mounted outputs and writes `completion.json` afterward.

      docker build -t trid3nt-local/telemac:latest workers/telemac/
      # or scripts/build_telemac_image.sh

- `mesh/` -- the in-container mesh writers, dispatched by the mesh router rather
  than by `run_solver`.
- `qgis/` -- the QGIS-Processing worker image, a separate concern from the
  solver dispatch seam.

Env gates + measured runtimes: `docs/site/engines.md`.
