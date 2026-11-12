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
| `telemac/` | The TELEMAC solver worker. The steering readers and the result file are NOT here: the daemon runs the steering scripts over `third_party/opentelemac` and reads SELAFIN in process. `entrypoint.py` reads the `manifest.json` the agent staged into the bind-mounted rundir, runs the deck and writes results back into it. No boto3 in the image - the agent-side supervisor uploads the mounted outputs and writes `completion.json` afterward. |
| `modflow/` | The MODFLOW solver worker: mf6 6.7.0 (public domain, the same pin `scripts/fetch_binaries.sh` installs) beside a stdlib-only `entrypoint.py`. It reads `manifest.json`, runs `mf6` - optionally `--mode validate` first, which DAMOCLES's staged check will drive - and writes `modflow_metrics.json` with mf6's own normal-termination verdict. FloPy authors in the DAEMON and never enters this image. |
| `geoclaw/` | The GeoClaw solver worker: two prebuilt executables compiled ONCE at image build (clawpack v5.14.0): `xgeoclaw` from chile2010's `.exe` target and `xgeoclaw_bouss`, the Boussinesq one, from radial_flat's Makefile against PETSc 3.23.7 and MPICH (run under `mpiexec -n <cores>`), beside a stdlib-only `entrypoint.py`. It runs the staged `.data` directory with no compile and no authoring and writes `geoclaw_metrics.json`, whose verdict reads GeoClaw's own sentences - mass line, Done-integrating time vs the staged `tfinal`, frame count - because a plain-Fortran `stop` exits 0. No netcdf in the image: ascii topotypes only, type 4 refused by name. |
| `pestpp/` | The PEST++ worker: the PEST++ 5.2.27 linux release, the same mf6 6.7.0 release the MODFLOW worker holds and pyEMU 1.7.0 (which a PstFrom interface's forward-run script imports in every trial), all pinned, beside a stdlib `entrypoint.py`. It copies the staged study directory once per agent into the container's `/work`, runs one manager in `/data` and its agents on the loopback, and writes `pestpp_metrics.json` with PEST++'s own `analysis complete` verdict. |

    docker build -t trid3nt-local/telemac:latest workers/telemac/   # or scripts/build_telemac_image.sh
    docker build -t trid3nt-local/modflow:latest workers/modflow/
    docker build -t trid3nt-local/geoclaw:latest workers/geoclaw/
    docker build -t trid3nt-local/pestpp:latest workers/pestpp/

Env gates and measured runtimes: `docs/site/engines.md`.
