"""MODFLOW 6 solver Cloud Run Job — Case 2 groundwater substrate.

Sprint-13 / MOD-1 / job-0220 / FR-CE-1/2/3. The MODFLOW-6 analogue of the
SFINCS solver worker (services/workers/sfincs/). Reads a JSON setup manifest
from GCS, fetches the FloPy-generated input deck declared in the manifest
(simulation namefile `mfsim.nam` + GWF and GWT model namefiles + their
package files, preserving the `gwf/` and `gwt/` subdirectory layout), runs
the pinned `mf6` 6.5.0 binary in a scratch directory, parses the simulation
list file for convergence, uploads outputs back to
`s3://trid3nt-runs/<run_id>/`, and writes a terminal
`completion.json` the agent's wait-for-completion polls for.

This is INFRA-OWNED scaffolding. The deck-construction semantics
(`gwt_adapter.py` — FloPy GWF+GWT package assembly from MODFLOWRunArgs) land
in the engine specialist's job-0221, and are NOT in this image's scope. This
module's contract is only: read manifest, run binary, parse convergence,
write outputs, emit completion.

Container basis (design doc § 1): unlike SFINCS (thin layer over the upstream
Deltares image), MODFLOW 6 has no maintained official Docker image, so we
build from `python:3.11-slim` and install the version-pinned USGS binary
(mf6 6.5.0) from the GitHub release zip with SHA-256 verification.

Solver (design doc § 2): MODFLOW 6 ships a SINGLE binary (`mf6`) that contains
both the GWF (groundwater flow) and GWT (groundwater transport) models. The
`mf6-gwt` label in the sprint-13 manifest refers to the GWT package within
this same binary, not a separate executable.
"""
