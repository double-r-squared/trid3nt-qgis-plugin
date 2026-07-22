"""GRACE-2 SWMM solver AWS Batch worker — FR-CE-1/2/3 substrate (sprint-16 P7).

The CLOUD LANE for the urban PySWMM engine: a thin container entrypoint that
wraps pyswmm for invocation as an AWS Batch job, so SWMM scales beyond the
local in-process path (``workflows/run_swmm.run_swmm_local``). Reads a JSON
setup manifest from S3 (or GCS — dispatched by URI scheme), downloads the
declared inputs, runs pyswmm on the staged ``.inp`` in a scratch dir, uploads
outputs back to the runs bucket, and writes a terminal completion manifest the
agent's ``wait_for_completion`` (job-0041) polls for — the SAME worker contract
the SFINCS and MODFLOW workers honor.

This module's contract is only: read manifest, run pyswmm, write outputs, emit
completion. The mass-balance honesty gate (Flow Routing Continuity error) is the
agent-side classifier's job, not this shim's.
"""
