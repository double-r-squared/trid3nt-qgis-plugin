# MODFLOW 6 solver worker

Sprint-13 / MOD-1 / job-0220 / FR-CE-1/2/3. The MODFLOW-6 analogue of
`services/workers/sfincs/` — a containerized `mf6` 6.5.0 solver that runs as a
Cloud Run Job, orchestrated by the `run_modflow` workflow (formerly a GCP
Cloud Workflow; the cloud submit path is decommissioned). Produces the
Case 2 groundwater-contaminant plume.

## What it is

A thin GCS-IN -> mf6-RUN -> GCS-OUT shim around the official USGS MODFLOW 6
binary. The engine specialist's deck builder (`gwt_adapter.py`, job-0221)
assembles the FloPy GWF + GWT input deck and uploads it to the cache bucket;
this worker downloads the deck (preserving the `gwf/`/`gwt/` subdirectory
layout the simulation namefile references), runs `mf6`, parses the simulation
list file for convergence, uploads the binary heads/concentration outputs to
the runs bucket, and writes a terminal `completion.json`.

### Container basis

Unlike SFINCS (a thin layer over the upstream Deltares image), MODFLOW 6 has
no maintained official Docker image matching the USGS release train, so the
`Dockerfile` builds from `python:3.11-slim` and installs the version-pinned
USGS binary explicitly:

- **Base:** `python:3.11-slim` (Bookworm). Python is first-class here (FloPy +
  rasterio back the agent-side postprocess and the build-verification step).
- **Python deps:** installed into a venv at `/opt/grace2/.venv` to sidestep
  PEP 668 on Bookworm — `flopy>=3.7,<4`, `google-cloud-storage>=2.18,<4`,
  `numpy>=1.26,<3`, `rasterio>=1.3,<2`.
- **mf6 binary:** MODFLOW **6.5.0** from the USGS GitHub release zip
  (`mf6.5.0_linux.zip`), SHA-256-verified before extraction
  (`0fac00211c42b7a74c7266abbe50776a6215ea8409c8ce887e5decd4a9335940`),
  installed to `/usr/local/bin/mf6` with `libmf6.so` to `/usr/local/lib/`.

### Single binary: GWF + GWT

MODFLOW 6 distributes ONE binary (`mf6`) that contains both the **GWF**
(groundwater flow) and **GWT** (groundwater transport) models. The `mf6-gwt`
label in the sprint-13 manifest refers to the GWT package within this same
binary, not a separate executable. Enabled packages for the Case 2 demo:

- **GWF:** DIS, IC, NPF, CHD, OC
- **GWT:** IC, ADV, DSP, SRC (spill mass source), OC

Reaction kinetics (biodegradation, sorption) are out of scope for v0.1 — the
demo contaminant is a conservative tracer.

## Contract

### Input

- `--run-id RUN_ID` (or `$TRID3NT_RUN_ID`) — outputs land under
  `gs://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/`.
- `--manifest-uri gs://.../manifest.json` (or `$TRID3NT_MANIFEST_URI`) — JSON
  setup manifest:

  ```json
  {
    "inputs": [
      {"gs_uri": "gs://.../mfsim.nam",     "dest": "mfsim.nam"},
      {"gs_uri": "gs://.../gwf/gwf.nam",   "dest": "gwf/gwf_model.nam"},
      {"gs_uri": "gs://.../gwt/gwt.nam",   "dest": "gwt/gwt_model.nam"},
      ...
    ],
    "mf6_args": [],
    "model_crs": "EPSG:26915",
    "outputs": ["gwf/gwf_model.hds", "gwt/gwt_model.ucn", "*.lst", "mfsim.lst"]
  }
  ```

  All `inputs` are downloaded into the scratch dir (subdir layout preserved)
  before `mf6` runs; all `outputs` (recursive glob) are uploaded after. The
  MODFLOW-specific `model_crs` field is echoed into `completion.json` for the
  agent-side postprocess reprojection (design doc § 6, OQ-MOD-3).

### Output

`gs://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/`:

- every uploaded output file (heads `.hds`, concentration `.ucn`, list files)
- `mf6.stdout`, `mf6.stderr`
- `completion.json` — terminal manifest the agent's wait-for-completion
  (job-0227) polls. Adds `converged` (bool) and `model_crs` to the SFINCS
  completion shape:

  ```json
  {
    "run_id": "...", "status": "ok|error", "exit_code": 0,
    "converged": true, "model_crs": "EPSG:26915",
    "mf6_stdout_uri": "gs://...", "mf6_stderr_uri": "gs://...",
    "output_uris": ["gs://..."],
    "started_at": "...Z", "finished_at": "...Z", "error": null
  }
  ```

### Convergence guard

MODFLOW 6 can exit `0` while emitting a convergence-failure warning to
`mfsim.lst`. The list file is authoritative: the entrypoint parses it for
`FAILED TO MEET SOLVER CONVERGENCE CRITERIA` and, if found, overrides
`exit_code -> 2` / `status -> error` / `converged -> false` even on a 0 exit
(design doc § 8).

### Exit codes (design doc § 8)

| exit | meaning | status |
|---|---|---|
| 0 | converged | ok |
| 2 | solver diverged (mfsim.lst marker, even on mf6 exit 0) | error |
| other nonzero | input error / runtime error | error |

## Fixtures

`fixtures/` holds a minimal **10×10 single-layer steady-state GWF** deck
(no GWT — pure convergence proof) plus `fixtures/manifest.json` pointing at it.
This is the in-container smoke deck: stage `fixtures/*` into the cache bucket
under `modflow/<run_id>/`, point the entrypoint at `fixtures/manifest.json`,
and the entrypoint reproduces the host smoke run. The deck drives a
left-to-right CHD gradient (8 m -> 2 m across 10 columns) so the head output
has a real, verifiable gradient.

The host smoke run that generated these fixtures + verified mf6 6.5.0
convergence is at
`reports/inflight/job-0220-infra-20260609/evidence/mf6_smoke.log`.

## Build + deploy

```bash
make modflow-build      # Cloud Build -> AR (linux/amd64). Logs the new digest.
# update infra/modflow.tf `modflow_image_digest` to the logged digest, then:
make modflow-deploy     # tofu apply the Cloud Run Job + Workflow + SA + IAM
```

`make modflow-build` is documented but NOT executable on a box without
gcloud + a reachable docker daemon. See
`reports/inflight/job-0220-infra-20260609/report.md` § "User unblock steps"
for the host prerequisites.
