"""SFINCS solver Cloud Run Job — FR-CE-1/2/3 substrate (sprint-07 / M5).

Thin entrypoint that wraps the Deltares SFINCS executable for invocation as
a Cloud Run Job. Reads a JSON setup manifest from GCS, fetches the input
files declared in the manifest, runs SFINCS in a scratch directory, uploads
outputs back to `s3://trid3nt-runs/<run_id>/`, and writes a
terminal completion manifest the agent's `wait_for_completion` (job-0041)
polls for.

This is INFRA-OWNED scaffolding. The actual solver-driven semantics
(HydroMT integration, AssessmentEnvelope shaping) land in the engine
specialist's job-0042. This module's contract is only: read manifest,
run binary, write outputs, emit completion.
"""
