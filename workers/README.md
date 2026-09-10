# `workers/` - solver worker code

Worker code for the engines the agent dispatches through the shared
`run_solver` / `wait_for_completion` seam
(`trid3nt_server/workflows/solver/solver.py`). This is a LOCAL-FIRST repo:
workers run on this machine, dispatched by the agent's local solver backend -
there is no live Cloud Run / AWS Batch deploy here. A worker is the engine room:
a staged run directory in, results out, no network and no opinions of its own.

## Files

| file | what it is |
| --- | --- |
| `conftest.py` | Puts this directory on the path so the worker's own tests import it as a package. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `telemac/` | The one solver worker: `entrypoint.py` reads the `manifest.json` the agent staged into the bind-mounted rundir, runs the deck and writes results back into it. No boto3 in the image - the agent-side supervisor uploads the mounted outputs and writes `completion.json` afterward. |
| `mesh/` | A Dockerfile and nothing else: the GPL-isolated OceanMesh2D ENVIRONMENT. It holds no code of ours; the mesh recipe bind-mounts its driver and a rundir into it and overrides the entrypoint. |

    docker build -t trid3nt-local/telemac:latest workers/telemac/   # or scripts/build_telemac_image.sh
    docker build -t trid3nt-local/mesh:latest workers/mesh/

Env gates and measured runtimes: `docs/site/engines.md`.
