"""Solver dispatch atomic tools (job-0041, M5 Stage C).

This module registers two atomic tools that drive the solver-execution
substrate. GCP Cloud Workflows is decommissioned; dispatch is now an
AWS Batch submit (default) or a local container / direct-binary run on the
agent instance (``GRACE2_SOLVER_BACKEND=local-docker``). Together they
implement the **FR-TA-2 solver-dispatch surface**:

    - ``run_solver(solver, model_setup_uri, compute_class="medium")
       -> ExecutionHandle`` — submits a solver run on the active backend.
      Currently only ``solver="sfincs"`` is supported; other values raise
      ``SolverNotRegisteredError`` (FR-TA-2).

    - ``wait_for_completion(handle, poll_interval_s=10, timeout_s=1800)
       -> RunResult`` — polls the run backing ``handle`` every
      ``poll_interval_s`` seconds, emits a ``pipeline-state`` progress update
      on every poll via ``PipelineEmitter.update_progress`` (the opt-in seam
      job-0035 surfaced for M5+ solvers), and on success reads
      ``completion.json`` from the runs bucket and returns a populated
      ``RunResult``. On failure or cancellation the matching terminal
      ``RunResult`` is returned.

Both tools are uncacheable-by-construction per FR-DC-6 (solver dispatchers
are explicitly enumerated): ``cacheable=False``, ``ttl_class="live-no-cache"``,
``source_class="solver_dispatch"``. They never touch the cache shim.

Cross-cutting principles (per CLAUDE.md + agents/AGENTS.md):

- **Invariant 1 (Determinism boundary): preserves.** Progress estimation is
  a wall-clock linear ramp keyed off ``handle.submitted_at`` and the
  NFR-P-4 target (900 s for ``≤15 min``) — not an LLM estimate. The ramp
  is clamped at 95% until the Workflow returns SUCCEEDED (then jumps to
  100%) so we never falsely advertise completion.

- **Invariant 2 (Deterministic workflows): preserves.** ``run_solver`` is a
  thin solver dispatch (local container / direct binary / AWS Batch submit);
  no LLM in the dispatch. The deterministic step graph (stage → invoke →
  read completion) is owned by the backend. FR-CE-2.

- **Invariant 8 (Cancellation is first-class): the headline.** Cancel chain
  end-to-end:

      WS cancel -> server.py inflight_task.cancel()
                -> asyncio.CancelledError inside emit_tool_call
                -> emit_tool_call CALLs invoke() which is our
                    wait_for_completion coroutine
                -> wait_for_completion sees CancelledError in its poll
                    sleep, terminates the live container / Batch job
                    (≤30 s, Invariant-8)
                -> the supervisor writes the status="cancelled"
                    completion.json
                -> wait_for_completion re-raises CancelledError so
                    emit_tool_call's mark_cancelled branch fires

  FR-AS-6 / NFR-R-3 30s budget. The backend handler terminates the run
  *before* re-raising the ``CancelledError`` so the kill is initiated
  atomically with the local cancel.

- **A.7 replace-not-reconcile: preserves.** Every progress emission goes
  through ``PipelineEmitter.update_progress(step_id, ...)``, which already
  builds the full snapshot per A.7. We never hand-roll a partial frame.

- **FR-DC-6 (uncacheable enumeration): preserves.** Both tools declare
  ``cacheable=False`` + ``ttl_class="live-no-cache"`` + a new source class
  ``"solver_dispatch"``. The kickoff explicitly enumerates them.

Dependency-injection seams (mirrors job-0032's ``passthroughs.py`` pattern):

- ``_EMITTER_BINDING`` / ``set_emitter_binding(emitter, step_id)`` — the
  active ``PipelineEmitter`` + the step_id this ``wait_for_completion``
  invocation is bracketed by. Set by the integration site (``server.py``)
  in a follow-up job that wires ``emit_tool_call`` to surface its
  ``step_id`` to the tool body. **TENTATIVE per kickoff Open Questions:**
  for the M5 smoke run we set the binding explicitly from the smoke
  harness; the integration with the WS handler lives in a follow-up agent
  job because ``pipeline_emitter.py`` + ``server.py`` are FROZEN here.

- ``_RUNS_BUCKET`` / ``set_runs_bucket(name)`` — overrides the runs bucket
  name. Used by tests to reach a fixture bucket; production wiring leaves it
  at the env-driven default.

- ``_S3_CLIENT`` / ``set_s3_client(client)`` — the boto3 S3 client used for
  ALL S3 staging / completion I/O. Lazily-default to the EC2 instance-role
  client (job-0289 boto3-not-s3fs lesson).

Run id generation: the agent service generates a ULID per ``run_solver``
call. The same id is used to compose the runs-bucket completion path
(``s3://<runs_bucket>/<run_id>/completion.json``).

Solver backend seam (job-0291, sprint-14-aws)
---------------------------------------------

``GRACE2_SOLVER_BACKEND`` selects the dispatch substrate at call time. GCP
Cloud Workflows is decommissioned; the default is ``aws-batch``:

- ``aws-batch`` (default) — per-job autoscaled solve on an AWS Batch compute
  env. ``run_solver`` mints a run_id and calls ``batch.submit_job``; the
  worker container writes completion.json to ``s3://<runs_bucket>/<run_id>/``.
- ``local-docker`` — the single-instance AWS EC2 path. The S3-IN → sfincs →
  S3-OUT envelope lives INSIDE the agent (testable Python), and the
  container is the PLAIN upstream ``deltares/sfincs-cpu`` binary image run
  via ``docker run`` on the same instance:

      run_solver: mint run_id → download the setup manifest from S3 (boto3)
        → stage every ``inputs[]`` object into ``$GRACE2_RUNS_DIR/<run_id>/``
        (manifest field name stays the legacy ``gs_uri``; the VALUE is an
        ``s3://`` URI resolved by scheme via boto3)
        → launch ``docker run --rm --name <run_id> -v <rundir>:/data -w /data
        $GRACE2_SFINCS_IMAGE [sfincs_args]`` DETACHED (Popen) → return
        ExecutionHandle immediately (``workflow_name="local-docker"``,
        ``workflows_execution_id="local-docker:<run_id>"`` — the container
        name IS the run_id, which is the Invariant-8 cancellation seam).

      supervisor (daemon thread): waits on the docker process, expands the
        manifest's ``outputs[]`` globs in the rundir, uploads outputs +
        sfincs.stdout/sfincs.stderr to ``s3://$GRACE2_RUNS_BUCKET/<run_id>/``
        (boto3), and ALWAYS writes ``completion.json`` (exact entrypoint.py
        schema: run_id/status/exit_code/sfincs_stdout_uri/sfincs_stderr_uri/
        output_uris/started_at/finished_at/error) — even on crash
        (status="error") or cancel (status="cancelled").

      wait_for_completion: dispatches on ``handle.workflow_name`` — local
        handles poll the completion.json object on S3 (cadence/timeout/
        progress-ramp semantics) and build the RunResult with
        ``output_uri = s3://<runs_bucket>/<run_id>/``.

      cancel chain: ``asyncio.CancelledError`` in the poll sleep → mark the
        run cancelled + ``docker kill <run_id>`` (≤30 s, Invariant-8) → the
        supervisor wakes on process exit and writes the status="cancelled"
        completion.json → re-raise.

  ``GRACE2_RUNS_BUCKET`` has NO default under local-docker (a missing value
  raises ``SolverDispatchError``). boto3 is used for ALL S3 I/O (s3fs falls
  back to anonymous credentials on the EC2 instance role — job-0289 lesson).

Generalized local backend (job-0292b, sprint-14-aws)
----------------------------------------------------

job-0292b extends the job-0291 machinery to MODFLOW without forking it. The
staging → detached launch → supervisor → completion.json → S3-poll envelope is
solver-agnostic; the solver-specific knobs are bundled into a
``LocalSolverSpec`` (manifest argv key, launch argv builder, stdout/stderr
artifact names, completion-manifest field names, an optional post-exit
classifier for solver-specific status resolution, and the cancel kind):

- SFINCS keeps the job-0291 ``docker run`` path verbatim
  (``_run_solver_local_docker`` builds the SFINCS spec; the completion.json
  is byte-identical to ``services/workers/sfincs/entrypoint.py``).
- MODFLOW (``workflows/run_modflow.py``) launches the **mf6 binary directly**
  (``exec_kind="exec"`` — no public MODFLOW image exists; the instance gets
  the same SHA-pinned USGS 6.5.0 static binary the GCP Dockerfile installs).
  Its spec's ``classify_exit`` reproduces the MODFLOW entrypoint's
  list-file convergence guard, and the completion.json carries the EXACT
  ``services/workers/modflow/entrypoint.py`` key set (``mf6_stdout_uri`` /
  ``mf6_stderr_uri`` / ``converged`` / ``model_crs``).

Cancel kinds: ``"docker"`` → ``docker kill <run_id>`` (container name ==
run_id, job-0291); ``"exec"`` → ``os.killpg`` on the detached process group
(``start_new_session=True`` makes pgid == pid). Both terminal ≤30 s
(Invariant 8). ``wait_for_completion`` dispatches on the handle's
``workflow_name`` ∈ {``local-docker``, ``local-exec``} — the poll loop is
shared.
"""

from __future__ import annotations

import asyncio
import contextvars
import glob as _glob
import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from grace2_contracts import new_ulid
from grace2_contracts.execution import ExecutionHandle, RunResult
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "run_solver",
    "wait_for_completion",
    "SolverNotRegisteredError",
    "SolverDispatchError",
    "set_emitter_binding",
    "set_runs_bucket",
    "set_s3_client",
    "solver_backend",
    "SOLVER_BACKEND_LOCAL_DOCKER",
    "SOLVER_BACKEND_AWS_BATCH",
    "AWS_BATCH_WORKFLOW_NAME",
    "AWS_BATCH_COMPUTE_CLASS_SIZING",
    "SOLVER_BATCH_JOBDEF_REGISTRY",
    "SFINCS_DECKBUILDER_SOLVER",
    "SFINCS_QUADTREE_SOLVER",
    "DeckBuildError",
    "submit_sfincs_deckbuild",
    "build_sfincs_quadtree_deck",
    "submit_sfincs_quadtree",
    "run_sfincs_quadtree",
    "select_compute_class",
    "COMPUTE_CLASS_SMALL_MAX_ELEMENTS",
    "COMPUTE_CLASS_STANDARD_MAX_ELEMENTS",
    "COMPUTE_CLASS_LARGE_MAX_ELEMENTS",
    "COMPUTE_CLASS_FALLBACK",
    "set_batch_client",
    "set_ecs_client",
    "set_ec2_client",
    "begin_turn_inflight_tracking",
    "inflight_batch_jobs",
    "terminate_inflight_batch_jobs",
    "LOCAL_DOCKER_WORKFLOW_NAME",
    "LOCAL_EXEC_WORKFLOW_NAME",
    "LocalSolverSpec",
    "launch_local_solver",
    "SOLVER_WORKFLOW_REGISTRY",
    "EmitterBinding",
    "NFR_P_4_TARGET_SECONDS",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
    "PROGRESS_CLAMP_MAX",
    "PROGRESS_TERMINAL",
]

logger = logging.getLogger("grace2_agent.tools.solver")


# --------------------------------------------------------------------------- #
# Constants / configuration
# --------------------------------------------------------------------------- #


#: Target run-time budget for ≤200 km² at 30m per NFR-P-4 (15 min).
#: Progress is wall-clock linear in (now - submitted_at) / target.
NFR_P_4_TARGET_SECONDS: float = 900.0

#: Default poll cadence — matches NFR-P-4 ≤15-min budget granularity (≥9 polls).
DEFAULT_POLL_INTERVAL_S: int = 10

#: Default overall timeout (30 min — mirrors the Cloud Run Job task_timeout
#: from job-0040, gives 2× headroom over NFR-P-4). Env-overridable via
#: ``GRACE2_SOLVER_TIMEOUT_S`` so a legitimately long run (a large coastal
#: quadtree + SnapWave solve exceeds the 30-min pluvial budget this constant was
#: sized for) can be given more headroom on the box WITHOUT touching the call
#: sites; absent/garbage env falls back to 1800 so default behaviour is unchanged.
def _default_timeout_s() -> int:
    raw = (os.environ.get("GRACE2_SOLVER_TIMEOUT_S") or "").strip()
    try:
        v = int(raw)
        return v if v > 0 else 1800
    except ValueError:
        return 1800


DEFAULT_TIMEOUT_S: int = _default_timeout_s()

#: Highest progress we ever advertise before the Workflow is SUCCEEDED.
#: Clamp keeps us honest under late runs — the chip never jumps to 100% on
#: estimate alone.
PROGRESS_CLAMP_MAX: int = 95

#: Final progress when the Workflow reports SUCCEEDED.
PROGRESS_TERMINAL: int = 100


#: Solver → workflow name registry. The VALUE is the canonical
#: workflow/composer name for the solver; the registry is consumed purely as a
#: PRESENCE GATE by ``run_solver`` (an unregistered solver raises
#: ``SolverNotRegisteredError``) — the live backend routing + the handle's pinned
#: ``workflow_name`` come from ``solver_backend()`` / the backend sentinels
#: (``AWS_BATCH_WORKFLOW_NAME`` / ``LOCAL_EXEC_WORKFLOW_NAME``), not from this
#: value. SWMM + MODFLOW self-register at import (``setdefault`` to a backend
#: sentinel); GeoClaw also self-registers (``register_geoclaw_solver()``), but
#: because the static literal below is evaluated FIRST its ``setdefault`` is a
#: no-op, so the sprint-17 composer-name value here wins (the lane's
#: ``"geoclaw": "aws-batch"`` was a backend sentinel mistaken for a workflow
#: name; the composer name is the correct, consistent value).
SOLVER_WORKFLOW_REGISTRY: dict[str, str] = {
    "sfincs": "grace-2-sfincs-orchestrator",
    # sprint-17 NEW engines (parallel lanes) — orchestrator-wired per the lane
    # handoff. GeoClaw's value supersedes its own import-time
    # ``setdefault("geoclaw", AWS_BATCH_WORKFLOW_NAME)`` (static literal wins).
    "geoclaw": "model_dambreak_geoclaw_scenario",
    "openquake": "model_seismic_hazard_scenario",
    "landlab": "model_landslide_scenario",
    # SWAN Phase 1: standalone spectral nearshore wave-field engine. Composer name
    # (supersedes run_swan.register_swan_solver's import-time setdefault to the
    # AWS_BATCH_WORKFLOW_NAME sentinel; the static literal here is evaluated first,
    # so the setdefault is a no-op and this consistent composer name wins).
    "swan": "model_wave_scenario",
    # canopy-height ML-inference tool (Meta HighResCanopyHeight on CPU Batch). It
    # is NOT a numerical engine -- it is a compute-heavy ML-inference tool that
    # runs on the SAME CPU SPOT Batch substrate the physics engines use (the spike
    # verdict: CPU-feasible, no GPU CE for v1). The agent stages an RGB COG +
    # build_spec and dispatches via the generic run_solver / wait_for_completion
    # seam; the canopy worker writes the SAME completion.json schema, so the wait
    # branch is reused verbatim. Per-solver job-def: GRACE2_AWS_BATCH_JOB_DEF_CANOPY
    # (INERT until NATE provisions + flips the env, like SWMM/OpenQuake/SWAN).
    # The value is the aws-batch backend sentinel (== AWS_BATCH_WORKFLOW_NAME,
    # which is defined LATER in this module; the registry value is consumed only
    # as a presence-gate by run_solver, so the string literal is used here to
    # avoid a forward-reference at dict-build time).
    "canopy": "aws-batch",
}


# --- Solver backend seam (job-0291, sprint-14-aws) --- #

#: AWS EC2 backend — plain upstream ``deltares/sfincs-cpu`` via ``docker run``
#: on the same instance; staging/upload envelope lives in this module.
SOLVER_BACKEND_LOCAL_DOCKER: str = "local-docker"

#: ``ExecutionHandle.workflow_name`` sentinel for local-docker handles —
#: ``wait_for_completion`` dispatches on it (the handle pins its backend so
#: env churn between submit and wait cannot mis-route the poll).
LOCAL_DOCKER_WORKFLOW_NAME: str = "local-docker"

#: ``ExecutionHandle.workflow_name`` sentinel for image-less local runs that
#: exec a solver binary directly (job-0292b — MODFLOW's mf6 has no public
#: image; the USGS static binary runs on the instance). Same poll loop as
#: local-docker; the cancel chain kills the detached process group instead
#: of a container.
LOCAL_EXEC_WORKFLOW_NAME: str = "local-exec"

# --- AWS Batch backend (sprint-16, SFINCS per-job autoscale) --- #
#
# Staged for cutover — INERT until NATE provisions the Batch compute env /
# queue / job-def + flips the env (GRACE2_SOLVER_BACKEND=aws-batch +
# GRACE2_AWS_BATCH_QUEUE + GRACE2_AWS_BATCH_JOB_DEF). It slots in as a third
# GRACE2_SOLVER_BACKEND value behind the SAME dispatch seam: run_solver mints a
# run_id and calls batch.submit_job; the Batch jobId is stashed in the handle's
# workflows_execution_id (NO ExecutionHandle contract change — it is just a
# string id field) and workflow_name=AWS_BATCH_WORKFLOW_NAME so
# wait_for_completion routes to the Batch poll branch. The Batch container is
# the SAME SFINCS image the local-docker path runs; it writes the SAME
# completion.json to s3://<runs_bucket>/<run_id>/, so the completion poll +
# RunResult build are reused verbatim (_try_get_completion_s3 +
# _build_local_run_result). Every boto3 batch call is gated so missing env /
# missing infra raises a clean typed SolverDispatchError, never a crash.

#: AWS Batch backend — per-job autoscaled SFINCS solve on a Batch compute env.
SOLVER_BACKEND_AWS_BATCH: str = "aws-batch"

#: ``ExecutionHandle.workflow_name`` sentinel for AWS Batch handles —
#: ``wait_for_completion`` dispatches on it (the handle pins its backend so env
#: churn between submit and wait cannot mis-route the poll). The Batch jobId
#: lives in ``workflows_execution_id``.
AWS_BATCH_WORKFLOW_NAME: str = "aws-batch"

#: PER-SOLVER Batch job-definition registry (sprint-16 P7 — SWMM is the FIRST
#: non-SFINCS Batch user). SFINCS and SWMM run DIFFERENT container images
#: (deltares/sfincs-cpu vs the pyswmm worker), so each solver must submit to its
#: OWN job definition. The pre-P7 code read ONE ``GRACE2_AWS_BATCH_JOB_DEF``
#: regardless of solver, which would have sent a ``solver='swmm'`` run to the
#: SFINCS image. This dict is the lowest-priority in-code default; the resolver
#: order is:
#:   1. ``GRACE2_AWS_BATCH_JOB_DEF_<SOLVER>``  (per-solver env, UPPERCASE solver)
#:   2. this registry entry                    (in-code per-solver default)
#:   3. ``GRACE2_AWS_BATCH_JOB_DEF``           (generic fallback — SFINCS-era)
#: An EMPTY value at any tier is treated as unset and falls through to the next.
#: Empty by default so the resolver is driven by env on the deployed box (NATE
#: sets ``GRACE2_AWS_BATCH_JOB_DEF_SWMM`` after ``tofu apply`` registers the
#: SWMM job-def); a deployment may instead seed this dict to pin defaults in
#: code. The generic fallback keeps SFINCS byte-identical: a box that only sets
#: ``GRACE2_AWS_BATCH_JOB_DEF`` still routes ``solver='sfincs'`` correctly.
SOLVER_BATCH_JOBDEF_REGISTRY: dict[str, str] = {}


# --- cht_sfincs quadtree+SnapWave deck-build job (coastal North Star gate) --- #
#
# GPL ISOLATION: the cht_sfincs library (GPL-3.0) is the ONLY thing that can
# author a multi-level refined quadtree connectivity table + the SnapWave mask /
# boundary files from scratch. It MUST NEVER enter the agent venv or be imported
# by any agent code — the GPL boundary. So deck authoring runs in a DEDICATED
# Batch worker image (services/workers/sfincs_deckbuilder/, carrying cht_sfincs) that
# the agent only SUBMITS over the Batch + S3 seam. The agent here composes a
# build_spec JSON, calls batch.submit_job for the deck-builder job-def, polls the
# SAME completion.json the solve worker writes, and reads the deck-build's
# OUTPUT manifest_uri — which is byte-identical in shape to the manifest
# build_sfincs_model already emits, so the EXISTING solve ``run_solver('sfincs',
# model_setup_uri=<that manifest>)`` path is unchanged.
#
# This is the SAME arms-length pattern the agent already uses for the GPL-free
# solve worker (the agent never imports the SFINCS binary either). The deck
# builder needs its OWN job-def (its GPL image differs from the deltares/
# sfincs-cpu solve image), routed via the per-solver ``_resolve_batch_job_def``
# under this distinct solver key so the two images never cross-route.

#: Per-solver key for the cht_sfincs deck-build Batch job. Routed through the
#: SAME ``_resolve_batch_job_def`` resolver as a normal solver, so its job-def is
#: ``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER`` (per-solver env, the canonical
#: knob NATE flips after ``tofu apply`` registers the deck-builder image's
#: job-def) → ``SOLVER_BATCH_JOBDEF_REGISTRY['sfincs-deckbuilder']`` → the
#: generic ``GRACE2_AWS_BATCH_JOB_DEF`` fallback. Kept INERT (no registry seed,
#: no generic fallback intended) so a coastal quadtree run honestly typed-errors
#: until the deck-builder job-def env is set — mirroring how SWMM stays inert.
SFINCS_DECKBUILDER_SOLVER: str = "sfincs-deckbuilder"

#: Per-solver key for the COMBINED cht_sfincs deck-build + SFINCS solve Batch job
#: (the coastal quadtree+SnapWave North Star, single image / single submit). One
#: worker image fuses the GPL deck-builder (cht_sfincs authors the refined
#: quadtree + SnapWave deck) AND the MIT SFINCS solve binary
#: (``/usr/local/bin/sfincs`` from the deltares/sfincs-cpu base): it reads ONE
#: build_spec, builds the deck locally, runs SFINCS in the SAME local deck dir (NO
#: S3 round-trip of the deck), uploads ``sfincs_map.nc`` + stdout/stderr, and
#: writes ONE completion.json. So the agent collapses the prior TWO-job-def,
#: two-submit, two-poll chain (deck-build job-def → poll → extract manifest →
#: solve job-def → poll) into ONE submit + ONE poll against ONE job-def.
#:
#: Routed through the SAME ``_resolve_batch_job_def`` resolver under this distinct
#: solver key, so the canonical knob is
#: ``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE`` (per-solver env NATE flips after
#: ``tofu apply`` registers the combined image's job-def) →
#: ``SOLVER_BATCH_JOBDEF_REGISTRY['sfincs-quadtree']`` → the generic
#: ``GRACE2_AWS_BATCH_JOB_DEF`` fallback. Kept INERT (no registry seed) so a
#: coastal quadtree run honestly typed-errors until the combined job-def env is
#: set — mirroring how SWMM stayed inert. Because the combined image carries the
#: GPL cht_sfincs closure, it is a DIFFERENT image from the deltares/sfincs-cpu
#: solve job-def; the per-solver routing keeps the two images from cross-routing.
SFINCS_QUADTREE_SOLVER: str = "sfincs-quadtree"

#: ``ExecutionHandle.workflow_name`` sentinel for the deck-build Batch job. It is
#: the SAME AWS Batch poll branch (``_wait_for_completion_aws_batch``) — the deck
#: worker writes the SAME completion.json schema — so we reuse
#: ``AWS_BATCH_WORKFLOW_NAME`` rather than a new sentinel.


def _resolve_batch_job_def(solver: str) -> str:
    """Resolve the AWS Batch job-definition name/ARN for ``solver`` (P7).

    Per-solver routing so the FIRST non-SFINCS Batch user (SWMM) submits to its
    OWN image's job-def instead of the SFINCS one. Resolution order (first
    non-empty wins):

        1. ``GRACE2_AWS_BATCH_JOB_DEF_<SOLVER>`` env (solver upper-cased, any
           non-``[A-Z0-9_]`` char mapped to ``_`` — ``swmm`` →
           ``GRACE2_AWS_BATCH_JOB_DEF_SWMM``).
        2. ``SOLVER_BATCH_JOBDEF_REGISTRY[solver]`` (in-code per-solver default).
        3. ``GRACE2_AWS_BATCH_JOB_DEF`` env (the generic SFINCS-era fallback —
           keeps a single-job-def box routing SFINCS unchanged).

    Raises ``SolverDispatchError`` (the inert-until-provisioned gate) when none
    of the three resolve to a non-empty value, naming the per-solver env var so
    the operator knows exactly what to set.
    """
    key = "".join(c if c.isalnum() else "_" for c in solver.strip().upper())
    per_solver_env = f"GRACE2_AWS_BATCH_JOB_DEF_{key}"

    candidate = (os.environ.get(per_solver_env) or "").strip()
    if candidate:
        return candidate

    candidate = (SOLVER_BATCH_JOBDEF_REGISTRY.get(solver) or "").strip()
    if candidate:
        return candidate

    candidate = (os.environ.get("GRACE2_AWS_BATCH_JOB_DEF") or "").strip()
    if candidate:
        return candidate

    raise SolverDispatchError(
        f"No AWS Batch job definition for solver {solver!r}: set "
        f"{per_solver_env} (the per-solver job-def NATE provisions via "
        f"`tofu apply` of infra/aws-batch), or the generic "
        f"GRACE2_AWS_BATCH_JOB_DEF fallback. The backend stays inert until "
        f"one is set."
    )

def _aws_region() -> str:
    """The AWS region for boto3 batch/S3 clients + the AWS Batch handle's
    ``workflow_location`` (env ``AWS_REGION``, default ``us-west-2``)."""
    return os.environ.get("AWS_REGION", "us-west-2")

#: compute_class → {vcpus, mem (MiB), OMP_NUM_THREADS} sizing map (the kickoff
#: instance buckets: 4/8/16/32 vCPU at ~2 GB/vCPU → 8/16/32/64 Gi; OMP threads
#: == vCPU). Batch ``resourceRequirements`` take VCPU as a STRING count and
#: MEMORY in MiB as a string. Aliased the same way ``_COMPUTE_CLASS_ALIAS``
#: maps FR-CE-3 names onto the schema literal, so ``medium`` (== standard)
#: resolves to the 8-vCPU bucket.
#:
#: ``xlarge`` is the higher-powered vertical-scale tier (NATE 2026-06-17 — auto
#: vertical scaling per case so a big AOI/mesh can grab MORE compute). 48 vCPU /
#: 96 GiB at the 2 GB/vCPU ratio is a clean fit for a single c7i.12xlarge (48
#: vCPU / 96 GiB) or m7i.12xlarge — both real, SPOT-eligible, x86_64 instances
#: in us-west-2 — so the Batch CE can place the whole job on ONE box (no NUMA
#: fragmentation across instances for the SFINCS/SWMM OpenMP solve). ``gpu`` is
#: left AS-IS per kickoff (32 vCPU / 64 GiB) — it is a distinct accelerator
#: bucket, not part of the vCPU vertical ladder ``select_compute_class`` walks.
AWS_BATCH_COMPUTE_CLASS_SIZING: dict[str, dict[str, int]] = {
    "small": {"vcpus": 4, "mem_mib": 8192, "omp_threads": 4},
    "standard": {"vcpus": 8, "mem_mib": 16384, "omp_threads": 8},
    "large": {"vcpus": 16, "mem_mib": 32768, "omp_threads": 16},
    "xlarge": {"vcpus": 48, "mem_mib": 98304, "omp_threads": 48},
    "gpu": {"vcpus": 32, "mem_mib": 65536, "omp_threads": 32},
}

#: Default sizing when the compute_class is unknown — the 8-vCPU standard bucket.
_AWS_BATCH_DEFAULT_SIZING: dict[str, int] = AWS_BATCH_COMPUTE_CLASS_SIZING["standard"]

#: The two local workflow_name sentinels ``wait_for_completion`` accepts.
_LOCAL_WORKFLOW_NAMES: tuple[str, str] = (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LOCAL_EXEC_WORKFLOW_NAME,
)

#: ``ExecutionHandle.workflow_location`` for local-docker handles.
LOCAL_DOCKER_WORKFLOW_LOCATION: str = "local"

#: Default rundir root under local-docker (env ``GRACE2_RUNS_DIR``).
DEFAULT_LOCAL_RUNS_DIR: str = "/opt/grace2/runs"

#: Default SFINCS image under local-docker (env ``GRACE2_SFINCS_IMAGE``).
DEFAULT_SFINCS_IMAGE: str = "deltares/sfincs-cpu:latest"

#: Budget for the ``docker kill`` subprocess on cancel — comfortably inside
#: the ≤30 s Invariant-8 / NFR-R-3 envelope.
DOCKER_KILL_TIMEOUT_S: float = 25.0


def solver_backend() -> str:
    """Return the active solver backend (job-0291 dispatch seam).

    GCP is decommissioned: the default backend is now ``aws-batch`` and the
    Cloud Workflows substrate is fully removed.
    ``GRACE2_SOLVER_BACKEND=local-docker`` → ``"local-docker"``; anything else
    (unset, ``aws-batch``, the dead ``gcp-workflows`` value, typos) →
    ``"aws-batch"``. Read at call time so a systemd / test env injection takes
    effect without re-import (mirrors ``cache.storage_scheme``).
    """
    b = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    if b == SOLVER_BACKEND_LOCAL_DOCKER:
        return SOLVER_BACKEND_LOCAL_DOCKER
    return SOLVER_BACKEND_AWS_BATCH


#: Map the kickoff-named compute classes (small/medium/large) onto the
#: ``ExecutionHandle.ComputeClass`` literal contract
#: (``Literal["small", "standard", "large", "gpu"]``). FR-CE-3 names the
#: middle class ``medium`` but the schema-side contract chose ``standard``;
#: rather than break the kickoff parameter surface we pin a mapping here.
#: Surfaced as OQ-41-COMPUTE-CLASS-NAMING for schema to reconcile.
_COMPUTE_CLASS_ALIAS: dict[str, str] = {
    "small": "small",
    "medium": "standard",  # FR-CE-3 medium == schema-side standard
    "standard": "standard",
    "large": "large",
    "xlarge": "xlarge",  # higher-powered vertical-scale tier (48 vCPU / 96 GiB)
    "gpu": "gpu",
}


# --------------------------------------------------------------------------- #
# Auto vertical scaling per case (NATE 2026-06-17)
# --------------------------------------------------------------------------- #
#
# The Batch CE already right-sizes the EC2 instance per job + scales to zero; the
# missing piece was the agent PICKING the right compute_class per case from the
# AOI/mesh size instead of always defaulting to "standard" (8 vCPU). The mesh
# builders (sfincs_builder / swmm_mesh_builder) already estimate the active
# ELEMENT count (cells); ``select_compute_class`` maps that estimate onto the
# vertical vCPU ladder small → standard → large → xlarge. ``gpu`` is NOT on this
# ladder (it is a distinct accelerator bucket, not a vCPU step).
#
# Thresholds are the element-count boundaries between tiers. They are calibrated
# against the SFINCS/SWMM perf models' per-vCPU cell caps (the point at which a
# bigger box buys a meaningfully shorter solve): a small domain stays on the
# cheap 4-vCPU box; a mid domain on the 8-vCPU standard; a large urban/coastal
# AOI on 16 vCPU; only a very large mesh reaches for the 48-vCPU xlarge tier.
# Env-overridable so the ladder re-tunes from logged solve-telemetry without a
# code change (mirrors the autoscale perf-model constants).

#: Element-count → compute_class boundaries (upper-exclusive). An estimate in
#: ``[0, SMALL_MAX)`` → small; ``[SMALL_MAX, STANDARD_MAX)`` → standard;
#: ``[STANDARD_MAX, LARGE_MAX)`` → large; ``>= LARGE_MAX`` → xlarge.
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw.strip()))
    except (TypeError, ValueError):
        logger.warning(
            "select_compute_class: env %s=%r not an int; using default %s",
            name,
            raw,
            default,
        )
        return default


#: Upper bound (exclusive) of the SMALL tier — at/under this many elements a 4
#: vCPU box solves comfortably. Env: ``GRACE2_COMPUTE_CLASS_SMALL_MAX``.
COMPUTE_CLASS_SMALL_MAX_ELEMENTS: int = _env_int(
    "GRACE2_COMPUTE_CLASS_SMALL_MAX", 50_000
)
#: Upper bound (exclusive) of the STANDARD tier (8 vCPU).
#: Env: ``GRACE2_COMPUTE_CLASS_STANDARD_MAX``.
COMPUTE_CLASS_STANDARD_MAX_ELEMENTS: int = _env_int(
    "GRACE2_COMPUTE_CLASS_STANDARD_MAX", 250_000
)
#: Upper bound (exclusive) of the LARGE tier (16 vCPU). At/above this the job
#: reaches for the higher-powered ``xlarge`` (48 vCPU) tier.
#: Env: ``GRACE2_COMPUTE_CLASS_LARGE_MAX``.
COMPUTE_CLASS_LARGE_MAX_ELEMENTS: int = _env_int(
    "GRACE2_COMPUTE_CLASS_LARGE_MAX", 1_000_000
)

#: The class returned when the element estimate is missing / non-positive — the
#: 8-vCPU standard bucket (the prior default; never crash, never under-provision
#: to ``small`` on a blind estimate).
COMPUTE_CLASS_FALLBACK: str = "standard"


def select_compute_class(estimated_elements: int | float | None) -> str:
    """Pick the per-case Batch ``compute_class`` from the estimated ELEMENT count.

    Auto vertical scaling per case (NATE 2026-06-17): the mesh builders already
    estimate the active-cell/element count; this maps that estimate onto the
    vertical vCPU ladder so a big AOI/mesh grabs more compute and a small one
    stays cheap. The ladder (low → high) is::

        elements < SMALL_MAX            → "small"     (4 vCPU / 8 GiB)
        SMALL_MAX <= e < STANDARD_MAX   → "standard"  (8 vCPU / 16 GiB)
        STANDARD_MAX <= e < LARGE_MAX   → "large"     (16 vCPU / 32 GiB)
        e >= LARGE_MAX                  → "xlarge"    (48 vCPU / 96 GiB)

    A missing / zero / negative / non-numeric estimate falls back to
    ``"standard"`` (the prior default) — this function NEVER raises, so the
    workflow always has a usable class even when the autoscale provenance is
    absent. ``gpu`` is intentionally NOT reachable here (it is an accelerator
    bucket, not a vCPU step — selected explicitly by a caller, never by size).

    Args:
        estimated_elements: the estimated active-element count for the run
            (SFINCS active cells / SWMM active cells). ``None`` / non-positive /
            non-numeric → the standard fallback.

    Returns:
        One of ``"small"`` / ``"standard"`` / ``"large"`` / ``"xlarge"`` — a key
        present in ``AWS_BATCH_COMPUTE_CLASS_SIZING`` and ``_COMPUTE_CLASS_ALIAS``
        (so it resolves cleanly through ``_aws_batch_sizing`` + ``run_solver``).
    """
    try:
        n = float(estimated_elements)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 0.0
    if not (n > 0) or n != n:  # non-positive OR NaN
        return COMPUTE_CLASS_FALLBACK
    if n < COMPUTE_CLASS_SMALL_MAX_ELEMENTS:
        chosen = "small"
    elif n < COMPUTE_CLASS_STANDARD_MAX_ELEMENTS:
        chosen = "standard"
    elif n < COMPUTE_CLASS_LARGE_MAX_ELEMENTS:
        chosen = "large"
    else:
        chosen = "xlarge"
    logger.info(
        "select_compute_class: estimated_elements=%d → compute_class=%s "
        "(bounds small<%d standard<%d large<%d)",
        int(n),
        chosen,
        COMPUTE_CLASS_SMALL_MAX_ELEMENTS,
        COMPUTE_CLASS_STANDARD_MAX_ELEMENTS,
        COMPUTE_CLASS_LARGE_MAX_ELEMENTS,
    )
    return chosen


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SolverNotRegisteredError(ValueError):
    """Raised by ``run_solver`` when ``solver`` is not in
    ``SOLVER_WORKFLOW_REGISTRY``. Distinct from a tool-params-invalid error
    so the agent surface can render a useful "solver X not supported in v0.1
    (sprint-07 ships sfincs only — TELEMAC / MODFLOW / HEC-HMS land in
    their respective milestones)" message."""


class SolverDispatchError(RuntimeError):
    """Raised when the backend dispatch (local container / direct binary /
    AWS Batch submit) fails or the completion-manifest read fails. The
    agent's emitter classifier maps this to ``UPSTREAM_API_ERROR``. The
    ``error_code`` attribute carries the open-set A.6 code so a downstream
    wrapper can re-emit it verbatim."""

    error_code: str = "SOLVER_DISPATCH_FAILED"


# --------------------------------------------------------------------------- #
# DI seams (mirrors passthroughs.set_mcp_client / set_worker_submitter)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EmitterBinding:
    """Tuple of (emitter, step_id) the active ``wait_for_completion`` invocation
    should drive progress emissions through.

    The integration site (``server.py``'s ``emit_tool_call`` wrapper) is
    responsible for binding this around each ``wait_for_completion`` call;
    until that follow-up job lands, the smoke harness binds it directly per
    the kickoff TENTATIVE recommendation. Surfaced as
    OQ-41-EMITTER-BINDING-SITE."""

    emitter: Any
    step_id: str


_EMITTER_BINDING: EmitterBinding | None = None
_RUNS_BUCKET: str | None = None
_S3_CLIENT: Any | None = None
_BATCH_CLIENT: Any | None = None
_ECS_CLIENT: Any | None = None
_EC2_CLIENT: Any | None = None


def set_emitter_binding(binding: EmitterBinding | None) -> None:
    """Bind the active ``(emitter, step_id)`` pair for progress emission.

    See class docstring for the integration-site discipline. ``None`` clears
    the binding (the polling loop falls back to no-op progress emission).
    """
    global _EMITTER_BINDING
    _EMITTER_BINDING = binding


def set_runs_bucket(name: str | None) -> None:
    """Override the runs-bucket name. ``None`` restores the env-based default."""
    global _RUNS_BUCKET
    _RUNS_BUCKET = name


def set_s3_client(client: Any) -> None:
    """Bind the boto3 S3 client used for ALL local-docker S3 I/O (job-0291).

    Production wiring leaves this ``None`` (the lazy default builds
    ``boto3.client("s3", region_name=$AWS_REGION)``, which resolves the EC2
    instance-role credentials via IMDS — the job-0289 boto3-not-s3fs lesson).
    Tests inject a tmpdir-backed fake exposing ``get_object`` /
    ``put_object``. ``None`` restores the lazy default.

    The deck-assembly (``sfincs_builder``) and run-output
    (``postprocess_flood``) S3 paths share this seam so one injection covers
    the whole staged-manifest → solve → postprocess chain.
    """
    global _S3_CLIENT
    _S3_CLIENT = client


def set_batch_client(client: Any) -> None:
    """Bind the boto3 AWS Batch client used by the aws-batch backend (sprint-16).

    Production wiring leaves this ``None`` (the lazy default builds
    ``boto3.client("batch", region_name=$AWS_REGION)``, resolving the agent
    box's instance-role credentials via IMDS — same job-0289 boto3 lesson as
    the S3 seam). Tests inject a fake exposing ``submit_job`` /
    ``describe_jobs`` / ``terminate_job``. ``None`` restores the lazy default.
    """
    global _BATCH_CLIENT
    _BATCH_CLIENT = client


def set_ecs_client(client: Any) -> None:
    """Bind the boto3 ECS client used by the Batch compute-meta capture (task-153).

    Production wiring leaves this ``None`` (the lazy default builds
    ``boto3.client("ecs", region_name=$AWS_REGION)``, resolving the agent box's
    instance-role credentials via IMDS — same job-0289 boto3 lesson as the Batch
    seam). Tests inject a fake exposing ``describe_container_instances``. ``None``
    restores the lazy default.
    """
    global _ECS_CLIENT
    _ECS_CLIENT = client


def set_ec2_client(client: Any) -> None:
    """Bind the boto3 EC2 client used by the Batch compute-meta capture (task-153).

    Production wiring leaves this ``None`` (the lazy default builds
    ``boto3.client("ec2", region_name=$AWS_REGION)``). Tests inject a fake
    exposing ``describe_instances``. ``None`` restores the lazy default.
    """
    global _EC2_CLIENT
    _EC2_CLIENT = client


#: FAIL-FAST bound on the Batch CONTROL-PLANE client (NATE 2026-06-29). The
#: prior to_thread offload of the submit (commit 6f5b4ee) still HUNG the turn for
#: 3+ minutes producing no jobId: under Batch API THROTTLING (many test
#: ``submit_job`` calls back-to-back) botocore's DEFAULT retry policy (legacy
#: mode, ~60 s read-timeout, several retries with exponential + adaptive backoff)
#: keeps re-trying for MINUTES before surfacing -- so the work moved off the loop
#: but the SUBMIT itself never returned and the run appeared dead. Cap it: a few
#: short-deadline attempts so a throttled / slow control plane FAILS FAST with an
#: honest ``SOLVER_DISPATCH`` error (the existing ``submit_job`` try/except wraps
#: any ``ConnectTimeout`` / ``ReadTimeout`` / throttling ``ClientError`` into a
#: ``SolverDispatchError``) instead of hanging. Worst-case wall time is bounded
#: by ~``max_attempts * read_timeout`` plus brief backoff. All knobs env-tunable
#: for ops. NB: this governs the SUBMIT/DESCRIBE control plane only -- the SOLVE
#: itself runs on Batch and is polled by ``wait_for_completion`` on its own
#: timeout budget, unaffected by this client config.
_BATCH_CONNECT_TIMEOUT_S: float = float(
    os.environ.get("GRACE2_BATCH_CONNECT_TIMEOUT_S", "5")
)
_BATCH_READ_TIMEOUT_S: float = float(
    os.environ.get("GRACE2_BATCH_READ_TIMEOUT_S", "15")
)
_BATCH_MAX_ATTEMPTS: int = int(
    os.environ.get("GRACE2_BATCH_MAX_ATTEMPTS", "3")
)


def _batch_client_config() -> Any:
    """Build the fail-fast ``botocore`` ``Config`` for the Batch client.

    Caps connect/read timeouts + total retry attempts so a throttled or slow
    Batch control-plane ``submit_job`` / ``describe_jobs`` surfaces a fast error
    rather than hanging the turn (see ``_BATCH_CONNECT_TIMEOUT_S`` rationale).
    Returns ``None`` when botocore is unavailable so the caller still constructs
    a default client (degrade, never crash on a missing optional dep).
    """
    try:
        from botocore.config import Config  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — optional; degrade to default client
        logger.debug(
            "botocore.config.Config unavailable (%s); Batch client uses boto3 "
            "defaults (no fail-fast bound).",
            exc,
        )
        return None
    return Config(
        connect_timeout=_BATCH_CONNECT_TIMEOUT_S,
        read_timeout=_BATCH_READ_TIMEOUT_S,
        # "standard" mode honors max_attempts as a HARD cap (legacy mode can
        # exceed it); a low cap keeps a throttled submit from retrying for
        # minutes.
        retries={"max_attempts": _BATCH_MAX_ATTEMPTS, "mode": "standard"},
    )


def _get_batch_client() -> Any:
    """Return the bound Batch client or lazily construct the boto3 default.

    Gated like the other lazy clients: a missing boto3 raises a clean typed
    ``SolverDispatchError`` (the aws-batch backend stays inert rather than
    crashing the agent) — see ``_run_solver_aws_batch`` for the full
    env-presence gate.

    The default client is built with a FAIL-FAST ``botocore`` ``Config``
    (``_batch_client_config``) so a throttled / slow control-plane call cannot
    hang the turn — it surfaces a typed ``SolverDispatchError`` instead.
    """
    if _BATCH_CLIENT is not None:
        return _BATCH_CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"boto3 not importable: {exc}; the aws-batch solver backend "
            "requires boto3 for batch.submit_job / describe_jobs / "
            "terminate_job (sprint-16)."
        ) from exc
    config = _batch_client_config()
    if config is not None:
        return boto3.client("batch", region_name=_aws_region(), config=config)
    return boto3.client("batch", region_name=_aws_region())


def _get_ecs_client() -> Any:
    """Return the bound ECS client or lazily construct the boto3 default (task-153).

    Raises ``SolverDispatchError`` when boto3 is unimportable so the caller's
    best-effort try/except degrades the compute-meta capture to ``None`` rather
    than crashing the solve.
    """
    if _ECS_CLIENT is not None:
        return _ECS_CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"boto3 not importable: {exc}; the Batch compute-meta capture "
            "requires boto3 for ecs.describe_container_instances (task-153)."
        ) from exc
    return boto3.client("ecs", region_name=_aws_region())


def _get_ec2_client() -> Any:
    """Return the bound EC2 client or lazily construct the boto3 default (task-153).

    Raises ``SolverDispatchError`` when boto3 is unimportable (the caller's
    best-effort wrapper degrades to ``None``).
    """
    if _EC2_CLIENT is not None:
        return _EC2_CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"boto3 not importable: {exc}; the Batch compute-meta capture "
            "requires boto3 for ec2.describe_instances (task-153)."
        ) from exc
    return boto3.client("ec2", region_name=_aws_region())


def _get_s3_client() -> Any:
    """Return the bound S3 client or lazily construct the boto3 default.

    boto3 (NOT s3fs) for all S3 I/O — s3fs falls back to anonymous
    credentials on the EC2 instance role (job-0289). Lazy import so
    GCP-only / CI environments never pay for boto3 at module load.
    """
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"boto3 not importable: {exc}; the local-docker solver backend "
            "requires boto3 for S3 staging/upload (job-0291)."
        ) from exc
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _get_runs_bucket() -> str:
    """Return the overridden runs bucket or the env-default
    (``GRACE2_RUNS_BUCKET`` if set, else the AWS runs bucket).

    GCP is decommissioned: the default is the AWS S3 runs bucket. Production
    sets ``GRACE2_RUNS_BUCKET`` explicitly via systemd (see aws-batch RUNBOOK)."""
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    return os.environ.get("GRACE2_RUNS_BUCKET", "grace2-hazard-runs-226996537797")


def _get_local_runs_bucket() -> str:
    """Runs bucket under local-docker — NO default to a GCP bucket name.

    ``set_runs_bucket`` override wins (test seam); otherwise
    ``GRACE2_RUNS_BUCKET`` must be set explicitly (on AWS the orchestrator
    provisions e.g. ``grace2-hazard-runs-226996537797``). A silent fallback
    to the GCP-named default would make every local run upload to a bucket
    that does not exist on AWS — fail loudly instead.
    """
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    if not bucket:
        raise SolverDispatchError(
            "GRACE2_RUNS_BUCKET must be set when GRACE2_SOLVER_BACKEND="
            "local-docker (no GCP-named default on AWS; job-0291)."
        )
    return bucket


# --------------------------------------------------------------------------- #
# local-docker backend (job-0291, sprint-14-aws)
#
# The GCS-IN → sfincs → GCS-OUT envelope from
# ``services/workers/sfincs/entrypoint.py`` ported into the agent: staging,
# detached ``docker run`` of the plain upstream image, a supervisor thread
# that uploads outputs and ALWAYS writes the entrypoint-schema
# completion.json, S3 completion polling, and the docker-kill cancel chain.
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    """ISO8601-Z timestamp matching the entrypoint's ``_utc_now`` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` → (scheme, bucket, key).

    GCP is decommissioned: only the ``s3://`` scheme is supported. Raises
    ``SolverDispatchError`` on malformed or unsupported URIs.
    """
    prefix = "s3://"
    if uri.startswith(prefix):
        bucket, _, key = uri[len(prefix):].partition("/")
        if not bucket or not key:
            raise SolverDispatchError(f"malformed s3:// URI: {uri!r}")
        return "s3", bucket, key
    raise SolverDispatchError(
        f"unsupported object URI scheme: {uri!r} (expected s3://)"
    )


def _read_object_bytes(uri: str) -> bytes:
    """Read one object's bytes, resolved BY SCHEME (job-0291 kickoff):
    ``s3://`` via boto3, ``file://`` / local path via the filesystem (the
    sfincs_builder local-manifest fallback)."""
    if uri.startswith("file://"):
        return Path(uri[len("file://"):]).read_bytes()
    if not uri.startswith("s3://"):
        return Path(uri).read_bytes()
    _scheme, bucket, key = _split_object_uri(uri)
    resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _download_object(uri: str, dest: Path) -> None:
    """Download one staged input to ``dest``, resolved by scheme.

    The manifest's input entries keep the LEGACY field name ``gs_uri`` but
    the VALUE is an ``s3://`` URI (the job-0289 storage backend) — we dispatch
    on the URI scheme, never the field name. GCP is decommissioned, so only
    ``s3://`` (and ``file://`` / local paths) are resolved.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if uri.startswith("file://") or not uri.startswith("s3://"):
        src = Path(uri[len("file://"):] if uri.startswith("file://") else uri)
        dest.write_bytes(src.read_bytes())
        return
    _scheme, bucket, key = _split_object_uri(uri)
    logger.info("local-docker staging %s -> %s", uri, dest)
    resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
    import shutil

    with dest.open("wb") as fh:
        shutil.copyfileobj(resp["Body"], fh)


def _upload_file_s3(s3: Any, src: Path, bucket: str, key: str) -> str:
    """Upload ``src`` to ``s3://bucket/key`` via boto3; return the s3:// URI."""
    with src.open("rb") as fh:
        s3.put_object(Bucket=bucket, Key=key, Body=fh)
    return f"s3://{bucket}/{key}"


@dataclass(frozen=True)
class LocalSolverSpec:
    """Solver-specific knobs for the shared local backend (job-0292b).

    The job-0291 staging → detached launch → supervisor → completion.json
    envelope is solver-agnostic; this spec carries everything that is not:

    Fields:
        solver: lowercase solver identifier carried on the handle (and used in
            the generic non-zero-exit error message — ``"sfincs exited with
            non-zero code N"`` stays byte-identical for SFINCS).
        workflow_name: the ``ExecutionHandle.workflow_name`` sentinel —
            ``"local-docker"`` (container launch) or ``"local-exec"``
            (direct binary launch). ``wait_for_completion`` accepts both.
        args_key: the manifest key carrying the solver argv tail
            (``"sfincs_args"`` / ``"mf6_args"`` — worker-entrypoint parity).
        build_argv: ``(run_id, rundir, manifest_args) -> argv`` — the full
            launch command. SFINCS builds the ``docker run --rm --name
            <run_id> ...`` line; MODFLOW returns ``[mf6, *args]``.
        stdout_name / stderr_name: the rundir artifact filenames (and the
            runs-prefix upload keys) — ``sfincs.stdout`` / ``mf6.stdout`` etc.
        stdout_uri_field / stderr_uri_field: the completion.json field names
            (``sfincs_stdout_uri`` vs ``mf6_stdout_uri`` — exact entrypoint
            schemas).
        exec_kind: ``"docker"`` → cancel via ``docker kill <run_id>``;
            ``"exec"`` → cancel via ``os.killpg`` on the detached group.
        classify_exit: optional ``(rundir, exit_code) -> (status, exit_code,
            error, extra_completion_fields)`` post-exit hook for
            solver-specific status resolution (MODFLOW's mfsim.lst
            convergence guard + the ``converged``/``model_crs`` completion
            fields). ``None`` → the plain exit-code rule (SFINCS). A user
            cancel overrides whatever the classifier returned.
    """

    solver: str
    workflow_name: str
    args_key: str
    build_argv: Callable[[str, Path, list[str]], list[str]]
    stdout_name: str
    stderr_name: str
    stdout_uri_field: str
    stderr_uri_field: str
    exec_kind: str = "docker"
    classify_exit: (
        Callable[[Path, int], tuple[str, int, str | None, dict[str, Any]]] | None
    ) = None


def _sfincs_local_spec() -> LocalSolverSpec:
    """The job-0291 SFINCS local-docker spec — behavior verbatim."""
    image = os.environ.get("GRACE2_SFINCS_IMAGE") or DEFAULT_SFINCS_IMAGE

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            run_id,
            "-v",
            f"{rundir}:/data",
            "-w",
            "/data",
            image,
            *args,
        ]

    return LocalSolverSpec(
        solver="sfincs",
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="sfincs_args",
        build_argv=build_argv,
        stdout_name="sfincs.stdout",
        stderr_name="sfincs.stderr",
        stdout_uri_field="sfincs_stdout_uri",
        stderr_uri_field="sfincs_stderr_uri",
        exec_kind="docker",
        classify_exit=None,
    )


@dataclass
class _LocalRun:
    """In-process registry entry for one local-backend solver run."""

    run_id: str
    rundir: Path
    runs_bucket: str
    proc: subprocess.Popen
    output_patterns: list[str]
    started_at: str  # ISO8601-Z, entrypoint format
    stdout_path: Path
    stderr_path: Path
    spec: LocalSolverSpec
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    supervisor: threading.Thread | None = None


#: run_id → live local run. In-process only: ``run_solver`` and the cancel
#: chain are co-located in the agent process (the deployed topology). The
#: supervisor pops its entry when the completion.json is written.
_LOCAL_RUNS: dict[str, _LocalRun] = {}


# --------------------------------------------------------------------------- #
# In-flight Batch jobId tracking (turn-cancel kill path)
# --------------------------------------------------------------------------- #
#
# THE GAP this closes: ``run_solver`` submits a Batch job (``batch.submit_job``)
# and returns an ``ExecutionHandle`` in ONE tool call; ``wait_for_completion``
# polls + terminates-on-cancel in a SEPARATE, LATER tool call. The
# Invariant-8 ``CancelledError`` -> ``_terminate_batch_job`` chain only fires
# when ``wait_for_completion`` is the frame actually being awaited. If the user
# cancels the turn (stop button / same-stream re-prompt supersede) in the WINDOW
# between submit and wait -- during the intervening LLM generation, or before the
# agent ever issues ``wait_for_completion`` -- the Batch job keeps running on
# Spot, costing money + producing an orphaned result. Nothing terminated it.
#
# Fix: track the in-flight jobId(s) on a per-turn ContextVar (mirrors
# ``pipeline_emitter._TURN_CASE`` -- per-task, never leaks across concurrent
# turns, and ``asyncio.to_thread`` propagates the Context so an off-loaded sync
# ``run_solver`` still appends to the right turn's list). The submit path
# registers; ``wait_for_completion`` clears on terminal return + on its OWN
# cancel handler (so a job it already terminated is not double-terminated); the
# turn-cancel cleanup in ``server.py`` calls ``terminate_inflight_batch_jobs``
# for whatever remains. Idempotent + a no-op when no job is in flight.
_INFLIGHT_BATCH_JOBS: contextvars.ContextVar[list[str] | None] = (
    contextvars.ContextVar("grace2_inflight_batch_jobs", default=None)
)


def begin_turn_inflight_tracking() -> contextvars.Token:
    """Bind a FRESH empty in-flight-jobs list for this turn task.

    Call once at turn-task entry (``server.py`` ``_dispatch_*_and_persist``).
    Returns the ``Token`` so the caller MAY reset it; in practice the turn task
    owns the Context for its lifetime so a reset is unnecessary (the binding
    dies with the task). Idempotent to call (each call just rebinds a new list).
    """
    return _INFLIGHT_BATCH_JOBS.set([])


def _register_inflight_batch_job(job_id: str) -> None:
    """Append ``job_id`` to the active turn's in-flight list (no-op if unbound).

    Called by every aws-batch submit path right after a successful
    ``batch.submit_job``. Unbound (``None``) outside a turn (tests, smoke
    harness, sub-workflow contexts that never began tracking) -> a harmless
    no-op; the existing ``wait_for_completion`` cancel chain still covers those.
    """
    bucket = _INFLIGHT_BATCH_JOBS.get()
    if bucket is None:
        return
    if job_id not in bucket:
        bucket.append(job_id)


def _clear_inflight_batch_job(job_id: str) -> None:
    """Remove ``job_id`` from the active turn's in-flight list (idempotent).

    Called by ``wait_for_completion`` on EVERY terminal exit (success, solver
    FAILED, early-FAILED, timeout) and inside its own cancel handler -- once a
    poll has reached terminal (or itself terminated the job) the turn-cancel
    cleanup must NOT re-terminate it.
    """
    bucket = _INFLIGHT_BATCH_JOBS.get()
    if bucket is None:
        return
    try:
        bucket.remove(job_id)
    except ValueError:
        pass


def inflight_batch_jobs() -> list[str]:
    """Snapshot of the active turn's still-in-flight Batch jobIds (read-only)."""
    bucket = _INFLIGHT_BATCH_JOBS.get()
    return list(bucket) if bucket else []


def terminate_inflight_batch_jobs(
    reason: str = "cancelled by user",
) -> list[str]:
    """Best-effort terminate EVERY still-in-flight Batch job for this turn.

    The turn-cancel kill path: ``server.py`` calls this from the turn task's
    cancel cleanup (off the event loop via ``asyncio.to_thread`` per the
    no-sync-blocking norm -- this body issues synchronous boto3
    ``terminate_job`` calls). Synchronous + self-contained so it is trivially
    off-loadable and unit-testable with a fake Batch client.

    Idempotent + safe when nothing is in flight (returns ``[]``). Swallows all
    per-job errors (a single bad jobId never blocks terminating the rest, and
    never blocks the cancel itself). Clears each id as it is handled so a second
    call is a no-op. Returns the list of jobIds it ATTEMPTED to terminate.
    """
    bucket = _INFLIGHT_BATCH_JOBS.get()
    if not bucket:
        return []
    # Drain a snapshot; clear the live list first so a concurrent
    # wait_for_completion terminal/cancel cannot double-handle an id.
    job_ids = list(bucket)
    bucket.clear()
    for job_id in job_ids:
        _terminate_batch_job(job_id, reason)
    return job_ids


def _expand_local_outputs(patterns: list[str], rundir: Path) -> list[Path]:
    """Glob-expand the manifest ``outputs[]`` in the rundir — mirrors the
    entrypoints' ``_expand_outputs`` (files only, de-duplicated, sorted).
    ``recursive=True`` so ``**`` patterns behave like the SFINCS/MODFLOW
    worker entrypoints (job-0292b — the MODFLOW manifest carries
    ``**/gwt_model.ucn`` / ``**/*.lst`` belt-and-suspenders nets)."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in _glob.glob(str(rundir / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _write_local_completion(
    s3: Any,
    *,
    runs_bucket: str,
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    started_at: str,
    error: str | None,
    stdout_uri_field: str = "sfincs_stdout_uri",
    stderr_uri_field: str = "sfincs_stderr_uri",
    extra: dict[str, Any] | None = None,
) -> None:
    """Write ``s3://<runs_bucket>/<run_id>/completion.json`` — EXACT
    worker-entrypoint schema (the ``wait_for_completion`` terminal signal).

    job-0292b: the stdout/stderr field names + an ``extra`` field dict are
    spec-driven so the MODFLOW completion carries ``mf6_stdout_uri`` /
    ``mf6_stderr_uri`` / ``converged`` / ``model_crs`` exactly like
    ``services/workers/modflow/entrypoint.py``; the SFINCS defaults are
    byte-identical to job-0291.
    """
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        **(extra or {}),
        stdout_uri_field: stdout_uri,
        stderr_uri_field: stderr_uri,
        "output_uris": output_uris,
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "error": error,
    }
    s3.put_object(
        Bucket=runs_bucket,
        Key=f"{run_id}/completion.json",
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(
        "local-docker wrote completion -> s3://%s/%s/completion.json (status=%s)",
        runs_bucket,
        run_id,
        status,
    )


def _supervise_local_run(run: _LocalRun) -> None:
    """Supervisor body (daemon thread): wait on the solver process, upload
    stdout/stderr + glob-expanded outputs to the S3 runs prefix, and ALWAYS
    write completion.json — even on crash (status="error") or cancel
    (status="cancelled"). Mirrors the entrypoints' best-effort discipline:
    no upload failure may prevent the terminal completion write."""
    status = "error"
    exit_code = 1
    error_msg: str | None = None
    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    completion_extra: dict[str, Any] = {}

    try:
        exit_code = run.proc.wait()
        # Solver-specific post-exit classification first (job-0292b — the
        # MODFLOW spec's mfsim.lst convergence guard); the plain exit-code
        # rule otherwise (SFINCS, byte-identical to job-0291). A user cancel
        # overrides either verdict below.
        if run.spec.classify_exit is not None:
            try:
                status, exit_code, error_msg, completion_extra = (
                    run.spec.classify_exit(run.rundir, exit_code)
                )
            except Exception as exc:  # noqa: BLE001 — classifier must not kill the write
                logger.exception(
                    "local classify_exit failed run_id=%s", run.run_id
                )
                status = "error"
                error_msg = f"classify_exit raised {type(exc).__name__}: {exc}"
        elif exit_code == 0:
            status = "ok"
            error_msg = None
        else:
            status = "error"
            error_msg = f"{run.spec.solver} exited with non-zero code {exit_code}"
        if run.cancel_requested.is_set():
            status = "cancelled"
            error_msg = (
                "run cancelled (docker kill via Invariant-8 cancel chain)"
                if run.spec.exec_kind == "docker"
                else "run cancelled (process-group kill via Invariant-8 cancel chain)"
            )
    except Exception as exc:  # noqa: BLE001 — defensive: wait() itself failed
        logger.exception("local-docker supervisor wait failed run_id=%s", run.run_id)
        status = "error"
        error_msg = f"{type(exc).__name__}: {exc}"

    try:
        s3 = _get_s3_client()
    except Exception as exc:  # noqa: BLE001 — no client ⇒ nothing more we can do
        logger.error(
            "local-docker supervisor could not build S3 client run_id=%s: %s "
            "— completion.json NOT written (poller will time out)",
            run.run_id,
            exc,
        )
        _LOCAL_RUNS.pop(run.run_id, None)
        return

    # Always upload stdout/stderr (entrypoint parity — evidence even on error).
    try:
        if run.stdout_path.exists():
            stdout_uri = _upload_file_s3(
                s3,
                run.stdout_path,
                run.runs_bucket,
                f"{run.run_id}/{run.spec.stdout_name}",
            )
        if run.stderr_path.exists():
            stderr_uri = _upload_file_s3(
                s3,
                run.stderr_path,
                run.runs_bucket,
                f"{run.run_id}/{run.spec.stderr_name}",
            )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "local-docker stdout/stderr upload failed run_id=%s: %s", run.run_id, exc
        )

    try:
        for path in _expand_local_outputs(run.output_patterns, run.rundir):
            rel = path.relative_to(run.rundir).as_posix()
            uri = _upload_file_s3(s3, path, run.runs_bucket, f"{run.run_id}/{rel}")
            output_uris.append(uri)
    except Exception as exc:  # noqa: BLE001 — reflect, but still write completion
        logger.exception(
            "local-docker output upload failed run_id=%s: %s", run.run_id, exc
        )
        if status == "ok":
            status = "error"
            error_msg = f"output upload to s3://{run.runs_bucket}/{run.run_id}/ failed: {exc}"

    try:
        _write_local_completion(
            s3,
            runs_bucket=run.runs_bucket,
            run_id=run.run_id,
            status=status,
            exit_code=exit_code,
            output_uris=output_uris,
            stdout_uri=stdout_uri,
            stderr_uri=stderr_uri,
            started_at=run.started_at,
            error=error_msg,
            stdout_uri_field=run.spec.stdout_uri_field,
            stderr_uri_field=run.spec.stderr_uri_field,
            extra=completion_extra,
        )
    except Exception:  # noqa: BLE001 — terminal-signal write failed; log loudly
        logger.exception(
            "local-docker completion.json write FAILED run_id=%s — "
            "wait_for_completion will hit its timeout",
            run.run_id,
        )
    finally:
        _LOCAL_RUNS.pop(run.run_id, None)


def launch_local_solver(
    spec: LocalSolverSpec,
    model_setup_uri: str,
    *,
    run_id: str | None = None,
    compute_class: str = "medium",
) -> ExecutionHandle:
    """Generic local-backend launcher (job-0291 envelope, job-0292b spec seam).

    Non-blocking — mirrors the Cloud Workflows submit semantics: stage the
    manifest's inputs from the object store, launch the solver detached
    (``spec.build_argv`` — a ``docker run`` line or a direct binary), hand the
    supervisor to a daemon thread, return the ``ExecutionHandle`` immediately.

    Args:
        spec: the solver-specific knobs (see ``LocalSolverSpec``).
        model_setup_uri: ``s3://`` / ``gs://`` / ``file://`` URI of the
            worker-contract manifest; input URIs inside resolve by scheme.
        run_id: optional pre-minted run id (the MODFLOW deck is staged under
            ``modflow/<run_id>/`` BEFORE submit, so its run_id must flow
            through — GCP parity with the ``{run_id, manifest_uri}`` workflow
            argument). Minted fresh when ``None`` (the SFINCS path).
        compute_class: FR-CE-3 class, alias-mapped onto the schema literal.
    """
    if not (
        model_setup_uri.startswith("s3://")
        or model_setup_uri.startswith("gs://")
        or model_setup_uri.startswith("file://")
    ):
        raise SolverDispatchError(
            f"model_setup_uri must be an s3:// / gs:// / file:// URI under "
            f"the local-docker backend; got {model_setup_uri!r}"
        )
    schema_compute_class = _COMPUTE_CLASS_ALIAS.get(compute_class)
    if schema_compute_class is None:
        raise SolverDispatchError(
            f"compute_class {compute_class!r} not recognized; allowed: "
            f"{sorted(_COMPUTE_CLASS_ALIAS)}"
        )
    runs_bucket = _get_local_runs_bucket()  # fail fast on missing env

    run_id = run_id or new_ulid()
    submitted_at = datetime.now(timezone.utc)
    rundir = (
        Path(os.environ.get("GRACE2_RUNS_DIR") or DEFAULT_LOCAL_RUNS_DIR) / run_id
    )
    rundir.mkdir(parents=True, exist_ok=True)

    # --- Manifest read + input staging (the entrypoint's download phase) ---
    try:
        manifest = json.loads(_read_object_bytes(model_setup_uri))
    except SolverDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"local-docker manifest read failed {model_setup_uri}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise SolverDispatchError(
            f"manifest at {model_setup_uri} must be a JSON object"
        )
    inputs = manifest.get("inputs", []) or []
    solver_args = [str(a) for a in (manifest.get(spec.args_key, []) or [])]
    output_patterns = [str(p) for p in (manifest.get("outputs", []) or [])]

    rundir_resolved = rundir.resolve()
    for item in inputs:
        try:
            input_uri = item["gs_uri"]  # legacy field NAME; value resolved by scheme
            dest_rel = item["dest"]
        except (TypeError, KeyError) as exc:
            raise SolverDispatchError(
                f"manifest input entry malformed (need gs_uri + dest): {item!r}"
            ) from exc
        dest = rundir / dest_rel
        # Host-side path-traversal guard (the GCP entrypoint runs sandboxed in
        # its container; here we stage on the instance filesystem).
        if rundir_resolved not in dest.resolve().parents:
            raise SolverDispatchError(
                f"manifest input dest escapes the rundir: {dest_rel!r}"
            )
        try:
            _download_object(input_uri, dest)
        except SolverDispatchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SolverDispatchError(
                f"local-docker input staging failed {input_uri} -> {dest}: {exc}"
            ) from exc

    # --- Detached launch (docker: container name == run_id is the cancel
    # seam; exec: the detached process group is — start_new_session=True
    # makes pgid == pid for os.killpg) ---
    stdout_path = rundir / spec.stdout_name
    stderr_path = rundir / spec.stderr_name
    cmd = spec.build_argv(run_id, rundir, solver_args)
    logger.info("local-%s exec: %s", spec.exec_kind, " ".join(cmd))
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
                cmd,
                stdout=out,
                stderr=err,
                cwd=str(rundir),
                start_new_session=True,  # detach from the agent's signal group
            )
    except Exception as exc:  # noqa: BLE001 — docker/solver binary missing, etc.
        raise SolverDispatchError(
            f"local-{spec.exec_kind} launch failed ({' '.join(cmd[:6])} ...): {exc}"
        ) from exc

    run = _LocalRun(
        run_id=run_id,
        rundir=rundir,
        runs_bucket=runs_bucket,
        proc=proc,
        output_patterns=output_patterns,
        started_at=_utc_now_iso(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        spec=spec,
    )
    _LOCAL_RUNS[run_id] = run
    supervisor = threading.Thread(
        target=_supervise_local_run,
        args=(run,),
        name=f"{spec.solver}-local-supervisor-{run_id}",
        daemon=True,
    )
    run.supervisor = supervisor
    supervisor.start()

    handle = ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver=spec.solver,
        compute_class=schema_compute_class,  # type: ignore[arg-type]
        workflows_execution_id=f"{spec.workflow_name}:{run_id}",
        workflow_name=spec.workflow_name,
        workflow_location=LOCAL_DOCKER_WORKFLOW_LOCATION,
        submitted_at=submitted_at,
    )
    logger.info(
        "local-%s submitted run_id=%s handle_id=%s argv0=%s inputs=%d",
        spec.exec_kind,
        run_id,
        handle.handle_id,
        cmd[0] if cmd else "?",
        len(inputs),
    )
    return handle


def _run_solver_local_docker(
    solver: str, model_setup_uri: str, compute_class: str
) -> ExecutionHandle:
    """``run_solver`` body under ``GRACE2_SOLVER_BACKEND=local-docker`` — the
    job-0291 SFINCS docker path, now a thin spec over the shared launcher."""
    return launch_local_solver(
        _sfincs_local_spec(),
        model_setup_uri,
        compute_class=compute_class,
    )


# --------------------------------------------------------------------------- #
# aws-batch backend (sprint-16, SFINCS per-job autoscale) — staged, INERT
# until NATE provisions the Batch compute env / queue / job-def + flips the env.
# --------------------------------------------------------------------------- #


def _aws_batch_sizing(compute_class: str) -> dict[str, int]:
    """Resolve the {vcpus, mem_mib, omp_threads} bucket for ``compute_class``.

    Aliases FR-CE-3 names onto the sizing keys exactly like
    ``_COMPUTE_CLASS_ALIAS`` (``medium`` == ``standard`` → the 8-vCPU bucket);
    unknown classes fall back to the standard bucket.
    """
    alias = _COMPUTE_CLASS_ALIAS.get((compute_class or "").strip().lower(), "standard")
    return AWS_BATCH_COMPUTE_CLASS_SIZING.get(alias, _AWS_BATCH_DEFAULT_SIZING)


def _run_solver_aws_batch(
    solver: str, model_setup_uri: str, compute_class: str
) -> ExecutionHandle:
    """``run_solver`` body under ``GRACE2_SOLVER_BACKEND=aws-batch`` (sprint-16).

    Mints a run_id and submits an AWS Batch job that runs the SAME SFINCS image
    the local-docker path uses, pointed at the staged manifest. The Batch
    container writes the SAME ``completion.json`` to
    ``s3://<runs_bucket>/<run_id>/`` so ``wait_for_completion`` reuses the S3
    completion poll. The Batch ``jobId`` is stashed in
    ``ExecutionHandle.workflows_execution_id`` (NO contract change — it is a
    plain string id field) and ``workflow_name=AWS_BATCH_WORKFLOW_NAME`` so the
    wait branch routes correctly even if the env churns afterward.

    Per-solver job-def routing (P7): SWMM is the FIRST non-SFINCS Batch user and
    runs a DIFFERENT image, so the job-definition is resolved PER SOLVER via
    ``_resolve_batch_job_def`` (``GRACE2_AWS_BATCH_JOB_DEF_<SOLVER>`` env →
    ``SOLVER_BATCH_JOBDEF_REGISTRY`` → the generic ``GRACE2_AWS_BATCH_JOB_DEF``
    fallback). The queue / CE / IAM are shared (engine-agnostic), so only the
    job-def differs between SFINCS and SWMM. SFINCS stays byte-identical on a box
    that sets only the generic ``GRACE2_AWS_BATCH_JOB_DEF``.

    Inert-until-provisioned gate: every prerequisite (runs bucket, job queue,
    job definition, the boto3 batch client, the submit call itself) raises a
    clean typed ``SolverDispatchError`` on absence/failure — the backend never
    crashes the agent. NATE flips ``GRACE2_SOLVER_BACKEND=aws-batch`` +
    ``GRACE2_AWS_BATCH_QUEUE`` + the per-solver ``GRACE2_AWS_BATCH_JOB_DEF_<SOLVER>``
    (or the generic ``GRACE2_AWS_BATCH_JOB_DEF``) to activate it.
    """
    if not (
        model_setup_uri.startswith("s3://") or model_setup_uri.startswith("gs://")
    ):
        # HONESTY GUARD (SWMM/MODFLOW off-box crash): the ephemeral Batch worker
        # runs on a DIFFERENT box and has NO access to the agent box local FS, so
        # a file:// (or any non-object-store) deck URI cannot be read by the
        # worker and silently crashes the solve AFTER a Spot submit. Reject it
        # here — loud + cheap, BEFORE any Batch submit / Spot spend — and tell the
        # caller to stage the deck to object storage first. Protects SWMM,
        # MODFLOW, and every future Batch caller.
        raise SolverDispatchError(
            f"model_setup_uri must be an s3:// or gs:// URI under the aws-batch "
            f"backend (the ephemeral Batch worker has no access to the agent box "
            f"local filesystem, so a file:// / local deck cannot be read — stage "
            f"the deck to object storage first); got {model_setup_uri!r}"
        )
    schema_compute_class = _COMPUTE_CLASS_ALIAS.get(compute_class)
    if schema_compute_class is None:
        raise SolverDispatchError(
            f"compute_class {compute_class!r} not recognized; allowed: "
            f"{sorted(_COMPUTE_CLASS_ALIAS)}"
        )

    runs_bucket = _get_local_runs_bucket()  # fail fast on missing GRACE2_RUNS_BUCKET

    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    if not queue:
        raise SolverDispatchError(
            "GRACE2_AWS_BATCH_QUEUE must be set when "
            "GRACE2_SOLVER_BACKEND=aws-batch (the Batch job queue ARN/name — "
            "NATE provisions it; the backend stays inert until then)."
        )
    # PER-SOLVER job-def routing (P7): SWMM and SFINCS run different images, so
    # the job-def is resolved per solver (GRACE2_AWS_BATCH_JOB_DEF_<SOLVER> →
    # registry → generic GRACE2_AWS_BATCH_JOB_DEF). SFINCS stays byte-identical
    # on a box that only sets the generic var; SWMM routes to its own job-def.
    job_def = _resolve_batch_job_def(solver)

    sizing = _aws_batch_sizing(compute_class)
    run_id = new_ulid()
    submitted_at = datetime.now(timezone.utc)

    # The Batch container entrypoint is services/workers/sfincs/entrypoint.py
    # (S3-capable after sprint-16's scheme generalization); it takes --run-id +
    # --manifest-uri and reads/writes the runs bucket from env. We pass BOTH the
    # CLI args and the env (the entrypoint accepts either; belt-and-suspenders).
    container_overrides: dict[str, Any] = {
        "command": [
            "--run-id",
            run_id,
            "--manifest-uri",
            model_setup_uri,
        ],
        "environment": [
            {"name": "GRACE2_RUNS_BUCKET", "value": runs_bucket},
            {"name": "GRACE2_RUN_ID", "value": run_id},
            {"name": "GRACE2_MANIFEST_URI", "value": model_setup_uri},
            {"name": "OMP_NUM_THREADS", "value": str(sizing["omp_threads"])},
            # The Batch image runs the scheme-aware entrypoint (s3:// vs gs://);
            # it selects the object-store backend from GRACE2_OBJECT_STORE. On
            # AWS Batch the manifest + completion record live on S3, so pin it
            # explicitly — without this the entrypoint would fall back to its
            # default (GCS) and fail to read the s3:// manifest.
            {"name": "GRACE2_OBJECT_STORE", "value": "s3"},
        ],
        "resourceRequirements": [
            {"type": "VCPU", "value": str(sizing["vcpus"])},
            {"type": "MEMORY", "value": str(sizing["mem_mib"])},
        ],
    }

    logger.info(
        "aws-batch submit_job solver=%s run_id=%s compute_class=%s "
        "vcpus=%d mem_mib=%d queue=%s job_def=%s",
        solver,
        run_id,
        compute_class,
        sizing["vcpus"],
        sizing["mem_mib"],
        queue,
        job_def,
    )

    client = _get_batch_client()
    try:
        resp = client.submit_job(
            jobName=f"grace2-{solver}-{run_id}",
            jobQueue=queue,
            jobDefinition=job_def,
            containerOverrides=container_overrides,
        )
    except SolverDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"AWS Batch submit_job failed (queue={queue} job_def={job_def}): {exc}"
        ) from exc

    job_id = (resp or {}).get("jobId") if isinstance(resp, dict) else getattr(resp, "jobId", None)
    if not job_id:
        raise SolverDispatchError(
            f"AWS Batch submit_job returned no jobId: {resp!r}"
        )
    # Turn-cancel kill path: record the in-flight jobId so a cancel that lands
    # BEFORE wait_for_completion is awaited still terminates this Batch job.
    _register_inflight_batch_job(str(job_id))

    handle = ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver=solver,
        compute_class=schema_compute_class,  # type: ignore[arg-type]
        workflows_execution_id=str(job_id),  # the Batch jobId — cancel/describe key
        workflow_name=AWS_BATCH_WORKFLOW_NAME,
        workflow_location=_aws_region(),
        submitted_at=submitted_at,
    )
    logger.info(
        "aws-batch submitted run_id=%s handle_id=%s jobId=%s",
        run_id,
        handle.handle_id,
        job_id,
    )
    return handle


# --------------------------------------------------------------------------- #
# cht_sfincs quadtree+SnapWave deck-build job (submit + poll + deck URI)
#
# The agent SUBMITS a Batch job that runs the GPL-isolated deck-builder worker
# image, then consumes the deck-build's OUTPUT manifest.json (the SAME shape
# build_sfincs_model emits). NO cht_sfincs import crosses into the agent: this is
# pure batch.submit_job + S3 completion poll, mirroring how the agent reaches the
# GPL-free solve worker. Everything is gated so a missing job-def / queue /
# runs-bucket / boto3 raises a clean typed SolverDispatchError (INERT until NATE
# provisions + flips the env), never a crash — degrade honestly.
# --------------------------------------------------------------------------- #


class DeckBuildError(SolverDispatchError):
    """Raised when the cht_sfincs deck-build Batch job cannot be submitted, the
    poll fails, or the completed job did not produce a deck manifest URI.

    Subclass of ``SolverDispatchError`` so the agent's emitter classifier maps
    it onto the same ``UPSTREAM_API_ERROR`` / ``SOLVER_DISPATCH_FAILED`` surface
    (and existing ``except SolverDispatchError`` handlers in the workflow catch
    it), while letting a caller distinguish the deck-build phase if it wants."""

    error_code: str = "DECK_BUILD_FAILED"


def submit_sfincs_deckbuild(
    build_spec_uri: str, compute_class: str = "small"
) -> ExecutionHandle:
    """Submit the cht_sfincs quadtree+SnapWave deck-build Batch job (coastal).

    Mirrors ``_run_solver_aws_batch`` but targets the deck-builder image's OWN
    job-def (resolved PER SOLVER under ``SFINCS_DECKBUILDER_SOLVER`` so its GPL
    image never cross-routes to the deltares/sfincs-cpu solve image) and passes
    a ``--build-spec-uri`` (the build_spec JSON the worker downloads — AOI,
    topobathy COG URI, grid/mask/snapwave/forcing params, + the output deck_dir /
    manifest URIs). The worker writes the SAME ``completion.json`` to
    ``s3://<runs_bucket>/<run_id>/`` the solve worker writes, so
    ``wait_for_completion`` / ``_wait_for_completion_aws_batch`` poll it
    identically (handle pins ``workflow_name=AWS_BATCH_WORKFLOW_NAME``).

    DECK-BUILD IS A BATCH-ONLY JOB. cht_sfincs is GPL-isolated in its own worker
    image; there is no in-agent / local-docker deck-build path (that would drag
    the GPL library into the agent box's image). When the active backend is NOT
    ``aws-batch`` we raise a clean typed ``DeckBuildError`` rather than silently
    doing nothing — the coastal quadtree path stays honestly inert until the box
    runs ``GRACE2_SOLVER_BACKEND=aws-batch`` + the deck-builder job-def env.

    Inert-until-provisioned: every prerequisite (aws-batch backend, runs bucket,
    job queue, the per-solver deck-builder job-def, the boto3 batch client, the
    submit call itself) raises a clean typed ``DeckBuildError`` /
    ``SolverDispatchError`` on absence/failure. NATE flips
    ``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER`` (the deck-builder image's
    job-def, provisioned via ``tofu apply``) to activate it — mirroring how SWMM
    stayed inert until ``GRACE2_AWS_BATCH_JOB_DEF_SWMM`` was set.

    Returns the ``ExecutionHandle`` (solver=``sfincs-deckbuilder``); feed it to
    ``wait_for_deckbuild`` (or ``wait_for_completion``) to get the deck manifest.
    """
    if not isinstance(build_spec_uri, str) or not build_spec_uri:
        raise DeckBuildError(
            f"build_spec_uri must be a non-empty string; got {build_spec_uri!r}"
        )
    if not (
        build_spec_uri.startswith("s3://")
        or build_spec_uri.startswith("gs://")
        or build_spec_uri.startswith("file://")
    ):
        raise DeckBuildError(
            "build_spec_uri must be an s3:// / gs:// / file:// URI for the "
            f"deck-build worker to download; got {build_spec_uri!r}"
        )

    backend = solver_backend()
    if backend != SOLVER_BACKEND_AWS_BATCH:
        raise DeckBuildError(
            "The cht_sfincs quadtree+SnapWave deck-build runs ONLY as an AWS "
            "Batch job (the GPL deck-builder library is isolated in its own "
            "worker image; there is no in-agent deck-build path). Set "
            "GRACE2_SOLVER_BACKEND=aws-batch + the deck-builder job-def "
            "(GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER) to enable the coastal "
            f"quadtree path. Active backend is {backend!r} — staying inert."
        )

    schema_compute_class = _COMPUTE_CLASS_ALIAS.get(compute_class)
    if schema_compute_class is None:
        raise DeckBuildError(
            f"compute_class {compute_class!r} not recognized; allowed: "
            f"{sorted(_COMPUTE_CLASS_ALIAS)}"
        )

    runs_bucket = _get_local_runs_bucket()  # fail fast on missing GRACE2_RUNS_BUCKET

    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    if not queue:
        raise DeckBuildError(
            "GRACE2_AWS_BATCH_QUEUE must be set for the cht_sfincs deck-build "
            "job (the Batch job queue ARN/name — NATE provisions it; the "
            "deck-build stays inert until then)."
        )
    # PER-SOLVER job-def routing: the deck-builder GPL image has its OWN job-def
    # distinct from the deltares/sfincs-cpu solve image. Resolved under the
    # deck-builder solver key so GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER is
    # the canonical knob (with the registry + generic fallbacks behind it). A
    # missing job-def raises SolverDispatchError naming the per-solver env.
    try:
        job_def = _resolve_batch_job_def(SFINCS_DECKBUILDER_SOLVER)
    except SolverDispatchError as exc:
        # Re-raise as a DeckBuildError so callers can distinguish the build
        # phase, but keep the env-naming message verbatim.
        raise DeckBuildError(str(exc)) from exc

    sizing = _aws_batch_sizing(compute_class)
    run_id = new_ulid()
    submitted_at = datetime.now(timezone.utc)

    # The deck-builder worker entrypoint takes --run-id + --build-spec-uri and
    # reads/writes the runs bucket from env. It downloads the build_spec JSON,
    # authors the quadtree+SnapWave deck via cht_sfincs, uploads the deck dir +
    # the deck manifest.json, then writes completion.json (with the manifest_uri
    # in output_uris). We pass BOTH the CLI args and the env (belt-and-suspenders
    # — the worker accepts either).
    container_overrides: dict[str, Any] = {
        "command": [
            "--run-id",
            run_id,
            "--build-spec-uri",
            build_spec_uri,
        ],
        "environment": [
            {"name": "GRACE2_RUNS_BUCKET", "value": runs_bucket},
            {"name": "GRACE2_RUN_ID", "value": run_id},
            {"name": "GRACE2_BUILD_SPEC_URI", "value": build_spec_uri},
            {"name": "OMP_NUM_THREADS", "value": str(sizing["omp_threads"])},
            # The deck-builder reads/writes S3 on Batch (build_spec + deck +
            # completion all on S3); pin the object store so it does not fall
            # back to its default (GCS) and fail to read the s3:// build_spec.
            {"name": "GRACE2_OBJECT_STORE", "value": "s3"},
        ],
        "resourceRequirements": [
            {"type": "VCPU", "value": str(sizing["vcpus"])},
            {"type": "MEMORY", "value": str(sizing["mem_mib"])},
        ],
    }

    logger.info(
        "aws-batch submit deck-build solver=%s run_id=%s compute_class=%s "
        "vcpus=%d mem_mib=%d queue=%s job_def=%s build_spec=%s",
        SFINCS_DECKBUILDER_SOLVER,
        run_id,
        compute_class,
        sizing["vcpus"],
        sizing["mem_mib"],
        queue,
        job_def,
        build_spec_uri,
    )

    client = _get_batch_client()
    try:
        resp = client.submit_job(
            jobName=f"grace2-{SFINCS_DECKBUILDER_SOLVER}-{run_id}",
            jobQueue=queue,
            jobDefinition=job_def,
            containerOverrides=container_overrides,
        )
    except SolverDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeckBuildError(
            f"AWS Batch submit_job failed for the deck-build "
            f"(queue={queue} job_def={job_def}): {exc}"
        ) from exc

    job_id = (
        (resp or {}).get("jobId")
        if isinstance(resp, dict)
        else getattr(resp, "jobId", None)
    )
    if not job_id:
        raise DeckBuildError(
            f"AWS Batch submit_job returned no jobId for the deck-build: {resp!r}"
        )
    # Turn-cancel kill path: record the in-flight jobId (see _register_inflight_batch_job).
    _register_inflight_batch_job(str(job_id))

    handle = ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver=SFINCS_DECKBUILDER_SOLVER,
        compute_class=schema_compute_class,  # type: ignore[arg-type]
        workflows_execution_id=str(job_id),  # the Batch jobId — cancel/describe key
        workflow_name=AWS_BATCH_WORKFLOW_NAME,
        workflow_location=_aws_region(),
        submitted_at=submitted_at,
    )
    logger.info(
        "aws-batch deck-build submitted run_id=%s handle_id=%s jobId=%s",
        run_id,
        handle.handle_id,
        job_id,
    )
    return handle


def _extract_deck_manifest_uri(result: RunResult, runs_bucket: str) -> str:
    """Pull the deck manifest.json URI out of a completed deck-build RunResult.

    The deck-build worker writes the deck manifest's URI into
    ``completion.json``'s ``output_uris`` (the SAME field the solve worker uses
    for its outputs). We re-read the completion.json (it is small, already on S3)
    and pick the ``output_uri`` that ends in ``manifest.json`` — that is the
    EXACT input the existing solve ``run_solver('sfincs', model_setup_uri=...)``
    consumes. Raises ``DeckBuildError`` if no manifest URI is present (a deck
    build that 'succeeded' without emitting a deck is a real failure — never a
    silent dead-end)."""
    manifest = _try_get_completion_s3(runs_bucket, result.run_id)
    output_uris: list[str] = []
    if isinstance(manifest, dict):
        raw = manifest.get("output_uris")
        if isinstance(raw, list):
            output_uris = [str(u) for u in raw if u]
    deck_manifests = [
        u for u in output_uris if u.rstrip("/").endswith("manifest.json")
    ]
    if not deck_manifests:
        raise DeckBuildError(
            f"deck-build run {result.run_id} completed (status={result.status}) "
            f"but produced no deck manifest.json in completion.json output_uris "
            f"(got {output_uris!r}); cannot feed the solve."
        )
    # Prefer the LAST manifest.json (stable if the worker lists deck files first
    # then the manifest); there should be exactly one in practice.
    return deck_manifests[-1]


async def build_sfincs_quadtree_deck(
    build_spec_uri: str,
    compute_class: str = "small",
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """Submit the cht_sfincs deck-build, await it, and return the deck manifest URI.

    The single coastal-path entrypoint the workflow calls: SUBMIT the GPL-isolated
    quadtree+SnapWave deck-build Batch job (``submit_sfincs_deckbuild``), poll its
    completion.json on S3 (``wait_for_completion`` — same Batch poll +
    early-FAILED consult + cancel/terminate chain as the solve), and on success
    return the deck ``manifest.json`` URI. That URI is byte-identical in shape to
    what ``build_sfincs_model`` emits, so the caller hands it STRAIGHT to the
    existing ``run_solver('sfincs', model_setup_uri=<that URI>)`` — the solve half
    is unchanged.

    Raises ``DeckBuildError`` (a ``SolverDispatchError`` subclass) when the
    deck-build is inert (no job-def / wrong backend), submission fails, the build
    job reports a non-complete terminal status, or it produced no deck manifest.
    Honest typed failure end-to-end — never a silent success. ``asyncio.
    CancelledError`` propagates (Invariant 8: the wait terminates the Batch job).
    """
    handle = submit_sfincs_deckbuild(build_spec_uri, compute_class=compute_class)
    result = await wait_for_completion(
        handle, poll_interval_s=poll_interval_s, timeout_s=timeout_s
    )
    if result.status != "complete":
        raise DeckBuildError(
            f"cht_sfincs deck-build did not complete (status={result.status}, "
            f"error_code={result.error_code}): "
            f"{result.error_message or result.cancellation_reason or 'no detail'}"
        )
    runs_bucket = _get_local_runs_bucket()
    manifest_uri = _extract_deck_manifest_uri(result, runs_bucket)
    logger.info(
        "cht_sfincs deck-build complete run_id=%s -> deck manifest %s",
        result.run_id,
        manifest_uri,
    )
    return manifest_uri


# --------------------------------------------------------------------------- #
# COMBINED cht_sfincs quadtree+SnapWave deck-build + SFINCS solve (ONE job)
#
# The combined worker fuses the GPL deck-builder + the MIT SFINCS solve binary
# into a single Batch image: it reads ONE build_spec, authors the refined
# quadtree + SnapWave deck via cht_sfincs LOCALLY, runs /usr/local/bin/sfincs in
# the SAME local deck dir (no S3 round-trip of the deck), uploads sfincs_map.nc +
# stdout/stderr, and writes ONE completion.json (the solve-worker schema — the
# load-bearing flood output ``sfincs_map.nc`` lands in ``output_uris`` under the
# SAME run_id). The agent SUBMITS one job-def + polls one completion.json — the
# prior two-submit / two-poll / deck-round-trip chain collapses to one.
#
# The agent NEVER imports cht_sfincs: this stays pure batch.submit_job + S3
# completion poll, exactly like the deck-build + solve submit paths. Every
# prerequisite is gated so a missing job-def / queue / runs-bucket / boto3
# raises a clean typed DeckBuildError (INERT until NATE provisions + flips the
# env), never a crash — degrade honestly.
# --------------------------------------------------------------------------- #


def submit_sfincs_quadtree(
    build_spec_uri: str, compute_class: str = "standard"
) -> ExecutionHandle:
    """Submit the COMBINED cht_sfincs quadtree deck-build + SFINCS solve Batch job.

    ONE submit against ONE job-def (``SFINCS_QUADTREE_SOLVER`` →
    ``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE``): the combined worker reads the
    SAME ``build_spec`` JSON the deck-builder reads (``--build-spec-uri`` +
    ``GRACE2_BUILD_SPEC_URI`` env — container command/env UNCHANGED from the
    deck-build submit), builds the deck locally, runs SFINCS in that local deck
    dir, and writes ONE solve-schema ``completion.json`` to
    ``s3://<runs_bucket>/<run_id>/`` (``sfincs_map.nc`` in ``output_uris``). So
    ``wait_for_completion`` / ``_wait_for_completion_aws_batch`` poll it
    identically (handle pins ``workflow_name=AWS_BATCH_WORKFLOW_NAME``,
    ``solver=sfincs-quadtree``); on success the run is DONE — there is no second
    solve submit.

    Because the combined worker BUILDS the deck AND SOLVES it, ``compute_class``
    defaults to ``"standard"`` (heavier than the deck-build-only ``"small"`` —
    the solve is the long pole). The agent normally derives a per-case class from
    the cell budget / estimated elements via ``select_compute_class`` and passes
    it explicitly.

    COMBINED IS A BATCH-ONLY JOB. The combined image carries the GPL cht_sfincs
    closure AND the SFINCS solve binary; there is no in-agent / local-docker
    combined path. When the active backend is NOT ``aws-batch`` we raise a clean
    typed ``DeckBuildError`` rather than silently doing nothing — the coastal
    quadtree path stays honestly inert until the box runs
    ``GRACE2_SOLVER_BACKEND=aws-batch`` + the combined job-def env.

    Inert-until-provisioned: every prerequisite (aws-batch backend, runs bucket,
    job queue, the per-solver combined job-def, the boto3 batch client, the
    submit call itself) raises a clean typed ``DeckBuildError`` /
    ``SolverDispatchError`` on absence/failure. NATE flips
    ``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE`` (the combined image's job-def,
    provisioned via ``tofu apply``) to activate it — mirroring how SWMM stayed
    inert until ``GRACE2_AWS_BATCH_JOB_DEF_SWMM`` was set.
    """
    if not isinstance(build_spec_uri, str) or not build_spec_uri:
        raise DeckBuildError(
            f"build_spec_uri must be a non-empty string; got {build_spec_uri!r}"
        )
    if not (
        build_spec_uri.startswith("s3://")
        or build_spec_uri.startswith("gs://")
        or build_spec_uri.startswith("file://")
    ):
        raise DeckBuildError(
            "build_spec_uri must be an s3:// / gs:// / file:// URI for the "
            f"combined quadtree worker to download; got {build_spec_uri!r}"
        )

    backend = solver_backend()
    if backend != SOLVER_BACKEND_AWS_BATCH:
        raise DeckBuildError(
            "The combined cht_sfincs quadtree+SnapWave deck-build + SFINCS solve "
            "runs ONLY as an AWS Batch job (the GPL deck-builder library + the "
            "SFINCS binary are isolated in one combined worker image; there is no "
            "in-agent path). Set GRACE2_SOLVER_BACKEND=aws-batch + the combined "
            "job-def (GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE) to enable the "
            f"coastal quadtree path. Active backend is {backend!r} — staying inert."
        )

    schema_compute_class = _COMPUTE_CLASS_ALIAS.get(compute_class)
    if schema_compute_class is None:
        raise DeckBuildError(
            f"compute_class {compute_class!r} not recognized; allowed: "
            f"{sorted(_COMPUTE_CLASS_ALIAS)}"
        )

    runs_bucket = _get_local_runs_bucket()  # fail fast on missing GRACE2_RUNS_BUCKET

    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    if not queue:
        raise DeckBuildError(
            "GRACE2_AWS_BATCH_QUEUE must be set for the combined cht_sfincs "
            "quadtree job (the Batch job queue ARN/name — NATE provisions it; "
            "the combined job stays inert until then)."
        )
    # PER-SOLVER job-def routing: the combined GPL+solve image has its OWN job-def
    # distinct from the deltares/sfincs-cpu solve-only image and the deck-build-
    # only image. Resolved under the combined solver key so
    # GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE is the canonical knob (with the
    # registry + generic fallbacks behind it). A missing job-def raises
    # SolverDispatchError naming the per-solver env.
    try:
        job_def = _resolve_batch_job_def(SFINCS_QUADTREE_SOLVER)
    except SolverDispatchError as exc:
        # Re-raise as a DeckBuildError so callers can distinguish the combined
        # quadtree phase, but keep the env-naming message verbatim.
        raise DeckBuildError(str(exc)) from exc

    sizing = _aws_batch_sizing(compute_class)
    run_id = new_ulid()
    submitted_at = datetime.now(timezone.utc)

    # The combined worker entrypoint takes --run-id + --build-spec-uri and
    # reads/writes the runs bucket from env — the SAME command/env shape as the
    # deck-build submit (the combined worker reads the SAME build_spec). It
    # downloads the build_spec JSON, authors the quadtree+SnapWave deck via
    # cht_sfincs, runs SFINCS in that local deck dir, uploads sfincs_map.nc +
    # stdout/stderr, then writes ONE solve-schema completion.json (sfincs_map.nc
    # in output_uris). We pass BOTH the CLI args and the env (belt-and-suspenders
    # — the worker accepts either).
    container_overrides: dict[str, Any] = {
        "command": [
            "--run-id",
            run_id,
            "--build-spec-uri",
            build_spec_uri,
        ],
        "environment": [
            {"name": "GRACE2_RUNS_BUCKET", "value": runs_bucket},
            {"name": "GRACE2_RUN_ID", "value": run_id},
            {"name": "GRACE2_BUILD_SPEC_URI", "value": build_spec_uri},
            {"name": "OMP_NUM_THREADS", "value": str(sizing["omp_threads"])},
            # The combined worker reads/writes S3 on Batch (build_spec + deck +
            # sfincs_map.nc + completion all on S3); pin the object store so it
            # does not fall back to its default (GCS) and fail to read the
            # s3:// build_spec.
            {"name": "GRACE2_OBJECT_STORE", "value": "s3"},
        ],
        "resourceRequirements": [
            {"type": "VCPU", "value": str(sizing["vcpus"])},
            {"type": "MEMORY", "value": str(sizing["mem_mib"])},
        ],
    }

    logger.info(
        "aws-batch submit COMBINED quadtree solver=%s run_id=%s compute_class=%s "
        "vcpus=%d mem_mib=%d queue=%s job_def=%s build_spec=%s",
        SFINCS_QUADTREE_SOLVER,
        run_id,
        compute_class,
        sizing["vcpus"],
        sizing["mem_mib"],
        queue,
        job_def,
        build_spec_uri,
    )

    client = _get_batch_client()
    try:
        resp = client.submit_job(
            jobName=f"grace2-{SFINCS_QUADTREE_SOLVER}-{run_id}",
            jobQueue=queue,
            jobDefinition=job_def,
            containerOverrides=container_overrides,
        )
    except SolverDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeckBuildError(
            f"AWS Batch submit_job failed for the combined quadtree job "
            f"(queue={queue} job_def={job_def}): {exc}"
        ) from exc

    job_id = (
        (resp or {}).get("jobId")
        if isinstance(resp, dict)
        else getattr(resp, "jobId", None)
    )
    if not job_id:
        raise DeckBuildError(
            f"AWS Batch submit_job returned no jobId for the combined "
            f"quadtree job: {resp!r}"
        )
    # Turn-cancel kill path: record the in-flight jobId (see _register_inflight_batch_job).
    _register_inflight_batch_job(str(job_id))

    handle = ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver=SFINCS_QUADTREE_SOLVER,
        compute_class=schema_compute_class,  # type: ignore[arg-type]
        workflows_execution_id=str(job_id),  # the Batch jobId — cancel/describe key
        workflow_name=AWS_BATCH_WORKFLOW_NAME,
        workflow_location=_aws_region(),
        submitted_at=submitted_at,
    )
    logger.info(
        "aws-batch COMBINED quadtree submitted run_id=%s handle_id=%s jobId=%s",
        run_id,
        handle.handle_id,
        job_id,
    )
    return handle


async def run_sfincs_quadtree(
    build_spec_uri: str,
    compute_class: str = "standard",
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> RunResult:
    """Submit the COMBINED quadtree job, await it, and return the solve RunResult.

    The single coastal-path entrypoint the workflow now calls: SUBMIT the
    GPL-isolated combined deck-build + SFINCS-solve Batch job
    (``submit_sfincs_quadtree``), poll its ``completion.json`` on S3
    (``wait_for_completion`` — same Batch poll + early-FAILED consult +
    cancel/terminate chain as a normal solve), and on success RETURN the
    ``RunResult`` (``output_uri = s3://<runs_bucket>/<run_id>/`` carrying
    ``sfincs_map.nc``). There is NO second ``run_solver`` call — the combined job
    already solved; the caller postprocesses ``run_result`` exactly as it would a
    plain SFINCS solve.

    Raises ``DeckBuildError`` (a ``SolverDispatchError`` subclass) when the
    combined job is inert (no job-def / wrong backend) or submission fails;
    returns the terminal ``RunResult`` (status may be ``failed`` / ``cancelled``)
    when the job reaches a non-success terminal state — the caller inspects
    ``run_result.status`` exactly as for a plain solve. ``asyncio.CancelledError``
    propagates (Invariant 8: the wait terminates the Batch job).
    """
    handle = submit_sfincs_quadtree(build_spec_uri, compute_class=compute_class)
    result = await wait_for_completion(
        handle, poll_interval_s=poll_interval_s, timeout_s=timeout_s
    )
    logger.info(
        "combined cht_sfincs quadtree run complete run_id=%s status=%s "
        "output_uri=%s",
        result.run_id,
        result.status,
        getattr(result, "output_uri", None),
    )
    return result


def _batch_terminal_failure(job_id: str) -> str | None:
    """Consult ``batch.describe_jobs`` for an EARLY terminal FAILED detection.

    Returns a status-reason string when the Batch job is in a terminal FAILED
    state (so ``wait_for_completion`` can fail fast instead of polling the S3
    completion.json until timeout when the container never started — e.g.
    image pull failure, no compute capacity). Returns ``None`` for any
    non-terminal / SUCCEEDED state, or on any describe error (best-effort — the
    S3 completion poll remains the primary terminal signal). Never raises.
    """
    try:
        client = _get_batch_client()
        resp = client.describe_jobs(jobs=[job_id])
    except Exception as exc:  # noqa: BLE001 — describe is advisory only
        logger.warning("aws-batch describe_jobs(%s) degraded: %s", job_id, exc)
        return None
    jobs = (resp or {}).get("jobs") if isinstance(resp, dict) else getattr(resp, "jobs", None)
    if not jobs:
        return None
    job = jobs[0]
    status = (job.get("status") if isinstance(job, dict) else getattr(job, "status", "")) or ""
    if str(status).upper() != "FAILED":
        return None
    reason = (
        job.get("statusReason") if isinstance(job, dict) else getattr(job, "statusReason", None)
    )
    return str(reason or "AWS Batch job FAILED")


def _batch_status(job_id: str) -> str | None:
    """Read the current ``DescribeJobs`` status verbatim (task-149).

    Returns the raw Batch status string (SUBMITTED / RUNNABLE / STARTING /
    RUNNING / SUCCEEDED / FAILED) so the wait-loop can surface the live phase on
    the off-box compute card, or ``None`` on any describe error / empty response
    (best-effort — phase is a UX signal, the S3 completion poll stays the
    primary terminal signal). Never raises; never an LLM estimate (Invariant 1).
    """
    try:
        client = _get_batch_client()
        resp = client.describe_jobs(jobs=[job_id])
    except Exception as exc:  # noqa: BLE001 — describe is advisory only
        logger.warning("aws-batch describe_jobs(%s) phase degraded: %s", job_id, exc)
        return None
    jobs = (resp or {}).get("jobs") if isinstance(resp, dict) else getattr(resp, "jobs", None)
    if not jobs:
        return None
    job = jobs[0]
    status = (job.get("status") if isinstance(job, dict) else getattr(job, "status", "")) or ""
    status = str(status).strip()
    return status or None


def _epoch_ms(value: Any) -> int | None:
    """Coerce a Batch ``createdAt``/``startedAt``/``stoppedAt`` field to int ms.

    Batch returns these as epoch-milliseconds (int or float) over the wire (or
    as a ``datetime`` when a mocked SDK hands one back); returns ``None`` for an
    absent / unparseable value. Never raises.
    """
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return int(value.timestamp() * 1000.0)
        return int(value)
    except Exception:  # noqa: BLE001 — advisory only
        return None


def _capture_batch_compute_meta(job_id: str) -> dict | None:
    """Capture the Spot instance + timing breakdown a Batch job landed on (task-153).

    Best-effort lookup chain (NATE-verified live):

        batch.describe_jobs(jobs=[job_id]).jobs[0]
          -> container.containerInstanceArn + createdAt/startedAt/stoppedAt
             + container.resourceRequirements (VCPU / MEMORY)
        -> the cluster ARN is embedded IN the containerInstanceArn
           (arn:aws:ecs:REGION:ACCT:container-instance/CLUSTER/ID) so we derive it
           rather than needing the compute environment
        -> ecs.describe_container_instances(cluster, containerInstances=[ci])
             .containerInstances[0].ec2InstanceId
        -> ec2.describe_instances(InstanceIds=[id]).Reservations[].Instances[]
             .{InstanceType, InstanceLifecycle, Placement.AvailabilityZone}

    The Spot instance is usually TERMINATED by the time the job is terminal
    (scale-to-zero), but describe-instances still returns its type for a while;
    a not-found / empty Reservations response degrades the instance fields to
    ``None`` while the timing + resourceRequirements fields still populate.

    Returns the merged dict, or ``None`` when the describe-jobs call itself fails
    / returns nothing (no job to attribute). EVERY AWS call is wrapped and ALL
    exceptions are swallowed — this MUST never break the solve. Sync boto3: the
    async caller MUST invoke this via ``asyncio.to_thread`` (Invariant: never
    block the event loop).
    """
    # --- describe-jobs: the anchor. A failure here means no attribution. ---
    try:
        batch = _get_batch_client()
        resp = batch.describe_jobs(jobs=[job_id])
    except Exception as exc:  # noqa: BLE001 — capture is advisory only
        logger.warning("compute-meta describe_jobs(%s) degraded: %s", job_id, exc)
        return None
    jobs = (resp or {}).get("jobs") if isinstance(resp, dict) else getattr(resp, "jobs", None)
    if not jobs:
        return None
    job = jobs[0]

    def _g(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    created_ms = _epoch_ms(_g(job, "createdAt"))
    started_ms = _epoch_ms(_g(job, "startedAt"))
    stopped_ms = _epoch_ms(_g(job, "stoppedAt"))

    container = _g(job, "container") or {}
    ci_arn = _g(container, "containerInstanceArn")

    # vCPU / memory from the resourceRequirements (Batch returns these as
    # {"type": "VCPU"|"MEMORY", "value": "<str count>"}).
    vcpus: int | None = None
    memory_mib: int | None = None
    rr = _g(container, "resourceRequirements") or []
    try:
        for req in rr:
            rtype = str(_g(req, "type") or "").upper()
            rval = _g(req, "value")
            if rtype == "VCPU" and rval is not None:
                vcpus = int(float(rval))
            elif rtype == "MEMORY" and rval is not None:
                memory_mib = int(float(rval))
    except Exception as exc:  # noqa: BLE001 — partial sizing is fine
        logger.warning("compute-meta resourceRequirements parse degraded: %s", exc)

    # --- ECS -> EC2: resolve the instance the container landed on. ---
    instance_type: str | None = None
    instance_lifecycle: str | None = None
    az: str | None = None
    ec2_instance_id: str | None = None

    cluster_arn = _cluster_arn_from_ci_arn(ci_arn) if ci_arn else None
    if ci_arn and cluster_arn:
        try:
            ecs = _get_ecs_client()
            ecs_resp = ecs.describe_container_instances(
                cluster=cluster_arn, containerInstances=[ci_arn]
            )
            cis = (
                (ecs_resp or {}).get("containerInstances")
                if isinstance(ecs_resp, dict)
                else getattr(ecs_resp, "containerInstances", None)
            )
            if cis:
                ec2_instance_id = _g(cis[0], "ec2InstanceId")
        except Exception as exc:  # noqa: BLE001 — instance fields degrade to None
            logger.warning(
                "compute-meta describe_container_instances degraded: %s", exc
            )

    if ec2_instance_id:
        try:
            ec2 = _get_ec2_client()
            ec2_resp = ec2.describe_instances(InstanceIds=[ec2_instance_id])
            reservations = (
                (ec2_resp or {}).get("Reservations")
                if isinstance(ec2_resp, dict)
                else getattr(ec2_resp, "Reservations", None)
            ) or []
            inst = None
            for res in reservations:
                insts = _g(res, "Instances") or []
                if insts:
                    inst = insts[0]
                    break
            if inst is not None:
                instance_type = _g(inst, "InstanceType")
                # On-demand instances OMIT InstanceLifecycle entirely; Spot
                # carries "spot". Normalize an absent value to "on-demand".
                instance_lifecycle = _g(inst, "InstanceLifecycle") or "on-demand"
                placement = _g(inst, "Placement") or {}
                az = _g(placement, "AvailabilityZone")
        except Exception as exc:  # noqa: BLE001 — terminated Spot box -> None type
            logger.warning(
                "compute-meta describe_instances(%s) degraded (likely "
                "scale-to-zero terminated): %s",
                ec2_instance_id,
                exc,
            )

    def _secs(a: int | None, b: int | None) -> float | None:
        if a is None or b is None:
            return None
        return round((a - b) / 1000.0, 3)

    return {
        "instance_type": instance_type,
        "instance_lifecycle": instance_lifecycle,
        "az": az,
        "vcpus": vcpus,
        "memory_mib": memory_mib,
        "created_at_ms": created_ms,
        "started_at_ms": started_ms,
        "stopped_at_ms": stopped_ms,
        "queue_provision_secs": _secs(started_ms, created_ms),
        "compute_secs": _secs(stopped_ms, started_ms),
        "total_secs": _secs(stopped_ms, created_ms),
    }


def _cluster_arn_from_ci_arn(ci_arn: str) -> str | None:
    """Derive the ECS cluster ARN from a container-instance ARN (task-153).

    A containerInstance ARN is
    ``arn:aws:ecs:REGION:ACCT:container-instance/CLUSTER/ID`` — the CLUSTER
    segment lets us build ``arn:aws:ecs:REGION:ACCT:cluster/CLUSTER`` without
    needing to look up the compute environment. Returns ``None`` on any
    unexpected shape. Never raises.
    """
    try:
        # Split ARN head (5 colon-fields) from the resource tail.
        head, _, resource = ci_arn.partition(":container-instance/")
        if not resource:
            return None
        cluster_name = resource.split("/", 1)[0]
        if not cluster_name:
            return None
        # head == arn:aws:ecs:REGION:ACCT
        return f"{head}:cluster/{cluster_name}"
    except Exception:  # noqa: BLE001 — advisory only
        return None


async def _emit_compute_phase(batch_status: str | None, run_id: str, solver: str) -> None:
    """Surface a Batch phase on the active compute card (task-149).

    Each poll tick that reads a ``DescribeJobs`` status pushes it BOTH ways
    through the active ``_EMITTER_BINDING`` (the binding the composer pointed at
    the COMPUTE step before the wait):

      1. ``emit_solve_progress`` with ``phase=batch_status`` so the live
         solve-progress tick carries the Batch phase, and
      2. ``update_compute_status`` so the off-box compute card's ``batch_status``
         reflects the control-plane verbatim.

    Both are best-effort + no-op when no binding / no status (live telemetry is a
    UX signal, never a correctness gate; mirrors ``_emit_progress``). The
    ephemeral Batch worker has NO inbound WS — status flows agent-side over the
    EXISTING WS via this poller."""
    binding = _EMITTER_BINDING
    if binding is None or not batch_status:
        return
    emitter = binding.emitter
    # solve-progress tick carrying the phase (elapsed-only payload; the long
    # solve already drives the rich grid/cells telemetry from the composer's
    # heartbeat — this tick is the Batch-phase carrier).
    try:
        await emitter.emit_solve_progress(
            {
                "run_id": run_id,
                "solver": solver,
                "elapsed_seconds": 0.0,
                "phase": batch_status,
            }
        )
    except Exception as exc:  # noqa: BLE001 — emission must never fail the poll
        logger.warning("emitter.emit_solve_progress(phase) raised: %s", exc)
    # Patch the compute card's batch_status (no-op for a plain tool step or an
    # unchanged status — update_compute_status guards both).
    try:
        await emitter.update_compute_status(binding.step_id, batch_status)
    except Exception as exc:  # noqa: BLE001 — emission must never fail the poll
        logger.warning("emitter.update_compute_status raised: %s", exc)


def _terminate_batch_job(job_id: str, reason: str) -> None:
    """Best-effort ``batch.terminate_job(jobId, reason)`` (Invariant-8 cancel).

    Logs + swallows exceptions; the underlying ``CancelledError`` propagates
    from the caller regardless. The Batch container's SFINCS entrypoint catches
    SIGTERM and writes a status="cancelled"/"error" completion.json on the way
    out (entrypoint best-effort write); the poller picks it up."""
    try:
        client = _get_batch_client()
        client.terminate_job(jobId=job_id, reason=reason)
        logger.info("aws-batch terminate_job(%s) issued: %s", job_id, reason)
    except Exception as exc:  # noqa: BLE001 — cancel chain still propagates
        logger.warning(
            "aws-batch terminate_job(%s) raised %s; cancel chain still propagates",
            job_id,
            exc,
        )


async def _wait_for_completion_aws_batch(
    handle: ExecutionHandle, poll_interval_s: int, timeout_s: int
) -> RunResult:
    """``wait_for_completion`` body for aws-batch handles (sprint-16).

    Polls the SAME completion.json on S3 as local-docker (reuse
    ``_try_get_completion_s3`` + ``_build_local_run_result``) and, on each tick
    that finds no completion yet, consults ``batch.describe_jobs(jobId)`` for an
    early terminal FAILED detection so a job that never produced a completion
    (image pull failure, capacity timeout) fails fast rather than waiting out
    the full ``timeout_s``. Same cadence / progress-ramp semantics as the
    local-docker poll. Cancel → ``batch.terminate_job`` + re-raise (Invariant 8).
    """
    runs_bucket = _get_local_runs_bucket()
    job_id = handle.workflows_execution_id
    deadline = handle.submitted_at.timestamp() + float(timeout_s)
    loop = asyncio.get_running_loop()

    logger.info(
        "wait_for_completion(aws-batch) handle_id=%s run_id=%s jobId=%s "
        "poll_interval=%ds timeout=%ds",
        handle.handle_id,
        handle.run_id,
        job_id,
        poll_interval_s,
        timeout_s,
    )

    try:
        while True:
            manifest = await loop.run_in_executor(
                None, _try_get_completion_s3, runs_bucket, handle.run_id
            )
            now = datetime.now(timezone.utc)

            if manifest is not None:
                if str(manifest.get("status", "")).lower() == "ok":
                    await _emit_progress(PROGRESS_TERMINAL)
                    # task-149: the S3 completion landed OK — the worker has run
                    # to SUCCEEDED even if DescribeJobs has not flipped yet; surface
                    # the terminal phase on the compute card so it lands green.
                    await _emit_compute_phase("SUCCEEDED", handle.run_id, handle.solver)
                else:
                    await _emit_progress(_progress_percent(handle.submitted_at, now))
                run_result = _build_local_run_result(handle, manifest, runs_bucket)
                # task-153: capture the Spot instance + timing breakdown the job
                # landed on so the perf model can later infer completion time.
                # Best-effort + off-loop (sync boto3 via to_thread); a None result
                # leaves the field defaulted. Populate on BOTH terminal outcomes
                # (a solver-FAILED manifest is itself a data point).
                meta = await loop.run_in_executor(
                    None, _capture_batch_compute_meta, job_id
                )
                if meta is not None:
                    run_result = run_result.model_copy(
                        update={"batch_compute_meta": meta}
                    )
                # Terminal: drop the in-flight tracking so a later turn-cancel
                # cleanup does not re-terminate an already-finished job.
                _clear_inflight_batch_job(job_id)
                return run_result

            # No completion yet — read the live Batch status ONCE per tick
            # (task-149): surface the phase on the compute card (solve-progress
            # phase + batch_status) AND reuse it to detect an early terminal
            # FAILED so we don't poll a dead job until timeout.
            batch_status = await loop.run_in_executor(None, _batch_status, job_id)
            await _emit_compute_phase(batch_status, handle.run_id, handle.solver)
            batch_failure: str | None = None
            if batch_status is not None and batch_status.upper() == "FAILED":
                batch_failure = await loop.run_in_executor(
                    None, _batch_terminal_failure, job_id
                )
            if batch_failure is not None:
                logger.warning(
                    "wait_for_completion(aws-batch) early FAILED handle_id=%s "
                    "jobId=%s: %s",
                    handle.handle_id,
                    job_id,
                    batch_failure,
                )
                await _emit_progress(_progress_percent(handle.submitted_at, now))
                # task-153: capture the instance + timing even on an early FAILED
                # (capacity timeout / image-pull failure) — a censored failure is
                # itself a data point about the chosen instance/AOI. Best-effort.
                meta = await loop.run_in_executor(
                    None, _capture_batch_compute_meta, job_id
                )
                # Terminal (early FAILED): drop the in-flight tracking.
                _clear_inflight_batch_job(job_id)
                return RunResult(
                    run_id=handle.run_id,
                    handle_id=handle.handle_id,
                    status="failed",
                    output_uri=None,
                    started_at=None,
                    completed_at=now,
                    duration_seconds=None,
                    error_code="SOLVER_DISPATCH_FAILED",
                    error_message=(
                        f"AWS Batch job {job_id} reported FAILED before writing "
                        f"completion.json: {batch_failure}"
                    ),
                    batch_compute_meta=meta,
                )

            await _emit_progress(_progress_percent(handle.submitted_at, now))

            if now.timestamp() >= deadline:
                logger.warning(
                    "wait_for_completion(aws-batch) timed out handle_id=%s "
                    "after %ds; terminating job %s",
                    handle.handle_id,
                    timeout_s,
                    job_id,
                )
                # Timeout ≠ user cancel: terminate WITHOUT implying a cancel
                # verdict (the result is SOLVER_TIMEOUT, mirroring local-docker).
                await loop.run_in_executor(
                    None,
                    _terminate_batch_job,
                    job_id,
                    "wait_for_completion timeout",
                )
                # Terminal (timeout): already terminated above; drop tracking so
                # the turn-cancel cleanup does not issue a second terminate_job.
                _clear_inflight_batch_job(job_id)
                return RunResult(
                    run_id=handle.run_id,
                    handle_id=handle.handle_id,
                    status="failed",
                    output_uri=None,
                    started_at=None,
                    completed_at=now,
                    duration_seconds=None,
                    error_code="SOLVER_TIMEOUT",
                    error_message=(
                        f"wait_for_completion exceeded {timeout_s}s budget while "
                        f"polling s3://{runs_bucket}/{handle.run_id}/completion.json "
                        f"(Batch jobId={job_id})"
                    ),
                )

            await asyncio.sleep(poll_interval_s)

    except asyncio.CancelledError:
        # Invariant 8: terminate the Batch job before re-raising so the cancel
        # propagates to the container within the NFR-R-3 budget.
        logger.info(
            "wait_for_completion(aws-batch) CANCELLED handle_id=%s; "
            "terminating job %s",
            handle.handle_id,
            job_id,
        )
        _terminate_batch_job(job_id, "user cancel (Invariant-8 cancel chain)")
        # Already terminated by this handler; drop tracking so the turn-cancel
        # cleanup in server.py does not issue a duplicate terminate_job.
        _clear_inflight_batch_job(job_id)
        raise


def _docker_kill(run_id: str) -> None:
    """Best-effort ``docker kill <run_id>`` (container name == run_id)."""
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            ["docker", "kill", run_id],
            capture_output=True,
            timeout=DOCKER_KILL_TIMEOUT_S,
            check=False,
        )
        logger.info(
            "docker kill %s rc=%d stderr=%s",
            run_id,
            proc.returncode,
            proc.stderr.decode(errors="replace").strip()[:200],
        )
    except Exception as exc:  # noqa: BLE001 — cancel chain still propagates
        logger.warning("docker kill %s raised %s", run_id, exc)


def _killpg_local_run(run: _LocalRun) -> None:
    """Best-effort SIGKILL to the detached process group of an exec-kind run
    (``start_new_session=True`` at launch makes pgid == pid). job-0292b."""
    try:
        os.killpg(run.proc.pid, signal.SIGKILL)
        logger.info("killpg(%d) issued for run_id=%s", run.proc.pid, run.run_id)
    except ProcessLookupError:
        logger.info(
            "killpg for run_id=%s: process group already gone", run.run_id
        )
    except Exception as exc:  # noqa: BLE001 — cancel chain still propagates
        logger.warning("killpg for run_id=%s raised %s", run.run_id, exc)


def _kill_local_run(run_id: str) -> None:
    """Kind-aware best-effort kill (job-0292b): exec-kind runs get a
    process-group SIGKILL; docker-kind (and unknown — e.g. after an agent
    restart, where ``docker kill`` against the container name is the only
    remaining lever) get ``docker kill <run_id>``."""
    run = _LOCAL_RUNS.get(run_id)
    if run is not None and run.spec.exec_kind == "exec":
        _killpg_local_run(run)
        return
    if run is None:
        logger.warning(
            "local kill for unknown run_id=%s (no in-process supervisor); "
            "issuing docker kill only — an exec-kind run cannot be reached "
            "after an agent restart (OQ-291-LOCAL-CANCEL-CROSS-PROCESS)",
            run_id,
        )
    _docker_kill(run_id)


def _request_local_cancel(run_id: str) -> None:
    """Invariant-8 local cancel: flag the run cancelled, then kill the
    container / process group (kind-aware, job-0292b). The supervisor wakes
    on process exit and writes the status="cancelled" completion.json —
    terminal within ≤30 s."""
    run = _LOCAL_RUNS.get(run_id)
    if run is not None:
        run.cancel_requested.set()
    _kill_local_run(run_id)


def _try_get_completion_s3(runs_bucket: str, run_id: str) -> dict[str, Any] | None:
    """Poll ``s3://<runs_bucket>/<run_id>/completion.json`` once.

    Returns the parsed manifest, ``None`` when the object is not there yet
    (or on a transient read error — the timeout catches persistent faults,
    mirroring the Workflows-poll resilience). Malformed JSON raises
    ``SolverDispatchError`` (S3 PUTs are atomic, so a parse failure is real
    corruption, not a partial write).
    """
    s3 = _get_s3_client()
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/completion.json")
        data = resp["Body"].read()
    except Exception as exc:  # noqa: BLE001
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        logger.warning(
            "local-docker completion poll degraded s3://%s/%s/completion.json: %s; "
            "will retry next poll",
            runs_bucket,
            run_id,
            exc,
        )
        return None
    try:
        manifest = json.loads(data)
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"completion manifest s3://{runs_bucket}/{run_id}/completion.json "
            f"is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise SolverDispatchError(
            f"completion manifest s3://{runs_bucket}/{run_id}/completion.json "
            "is not a JSON object"
        )
    return manifest


def _build_local_run_result(
    handle: ExecutionHandle, manifest: dict[str, Any], runs_bucket: str
) -> RunResult:
    """Map a local-docker completion manifest onto a ``RunResult``.

    ``status="ok"`` → ``complete`` with ``output_uri = s3://<runs_bucket>/
    <run_id>/`` (the runs PREFIX, kickoff-pinned — ``postprocess_flood``
    resolves ``sfincs_map.nc`` inside it); ``"cancelled"`` → ``cancelled``;
    anything else → ``failed`` with the manifest's structured error.
    """
    manifest_status = str(manifest.get("status", "")).lower()
    started_at = _to_utc(manifest.get("started_at"))
    completed_at = _to_utc(manifest.get("finished_at")) or datetime.now(timezone.utc)

    if manifest_status == "ok":
        return RunResult(
            run_id=handle.run_id,
            handle_id=handle.handle_id,
            status="complete",
            output_uri=f"s3://{runs_bucket}/{handle.run_id}/",
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=_duration(started_at, completed_at),
        )
    if manifest_status == "cancelled":
        return RunResult(
            run_id=handle.run_id,
            handle_id=handle.handle_id,
            status="cancelled",
            output_uri=None,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=_duration(started_at, completed_at),
            cancellation_reason=str(
                manifest.get("error") or "local-docker run cancelled"
            ),
        )
    return RunResult(
        run_id=handle.run_id,
        handle_id=handle.handle_id,
        status="failed",
        output_uri=None,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=_duration(started_at, completed_at),
        error_code=_solver_error_code(manifest),
        error_message=str(manifest.get("error") or "solver reported failure"),
    )


async def _wait_for_completion_local(
    handle: ExecutionHandle, poll_interval_s: int, timeout_s: int
) -> RunResult:
    """``wait_for_completion`` body for local-docker handles: poll the
    completion.json object on S3 with the same cadence/timeout/progress-ramp
    semantics as the Cloud Workflows poll (job-0291)."""
    runs_bucket = _get_local_runs_bucket()
    deadline = handle.submitted_at.timestamp() + float(timeout_s)
    loop = asyncio.get_running_loop()

    logger.info(
        "wait_for_completion(local-docker) handle_id=%s run_id=%s "
        "poll_interval=%ds timeout=%ds",
        handle.handle_id,
        handle.run_id,
        poll_interval_s,
        timeout_s,
    )

    try:
        while True:
            manifest = await loop.run_in_executor(
                None, _try_get_completion_s3, runs_bucket, handle.run_id
            )
            now = datetime.now(timezone.utc)

            if manifest is not None:
                if str(manifest.get("status", "")).lower() == "ok":
                    await _emit_progress(PROGRESS_TERMINAL)
                else:
                    await _emit_progress(
                        _progress_percent(handle.submitted_at, now)
                    )
                return _build_local_run_result(handle, manifest, runs_bucket)

            await _emit_progress(_progress_percent(handle.submitted_at, now))

            if now.timestamp() >= deadline:
                logger.warning(
                    "wait_for_completion(local-docker) timed out handle_id=%s "
                    "after %ds; killing container %s",
                    handle.handle_id,
                    timeout_s,
                    handle.run_id,
                )
                # Timeout ≠ user cancel: kill WITHOUT the cancelled flag so the
                # supervisor records status="error" (mirrors the GCP path's
                # best-effort cancel + SOLVER_TIMEOUT result). Kind-aware
                # (job-0292b): docker kill or process-group kill.
                await loop.run_in_executor(None, _kill_local_run, handle.run_id)
                return RunResult(
                    run_id=handle.run_id,
                    handle_id=handle.handle_id,
                    status="failed",
                    output_uri=None,
                    started_at=None,
                    completed_at=now,
                    duration_seconds=None,
                    error_code="SOLVER_TIMEOUT",
                    error_message=(
                        f"wait_for_completion exceeded {timeout_s}s budget while "
                        f"polling s3://{runs_bucket}/{handle.run_id}/completion.json"
                    ),
                )

            await asyncio.sleep(poll_interval_s)

    except asyncio.CancelledError:
        # Invariant 8: docker kill + cancelled completion within ≤30 s, then
        # re-raise so emit_tool_call's mark_cancelled branch fires.
        logger.info(
            "wait_for_completion(local-docker) CANCELLED handle_id=%s; "
            "issuing docker kill %s",
            handle.handle_id,
            handle.run_id,
        )
        _request_local_cancel(handle.run_id)
        raise


# --------------------------------------------------------------------------- #
# run_solver
# --------------------------------------------------------------------------- #


_RUN_SOLVER_METADATA = AtomicToolMetadata(
    name="run_solver",
    ttl_class="live-no-cache",
    source_class="solver_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_SOLVER_METADATA,
    # Annotations: readOnlyHint=False (submits a solver run that ultimately
    # writes output artifacts to the runs bucket), openWorldHint=False
    # (local container / direct binary / AWS Batch — no public external API),
    # destructiveHint=False (writes go to a new runs/ prefix; no existing
    # state overwritten), idempotentHint=False (each call creates a new
    # run with a distinct run_id).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
def run_solver(
    solver: str,
    model_setup_uri: str,
    compute_class: str = "medium",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> ExecutionHandle:
    """Submit a solver execution to the active backend (local / AWS Batch).

    Use this when: the agent has a staged model (e.g. from
    ``build_sfincs_model``) and needs to actually run the solver. Returns an
    ``ExecutionHandle`` whose ``workflow_name`` pins the backend and which is
    the Invariant-8 cancellation seam — feed it to ``wait_for_completion`` to
    poll progress and obtain the ``RunResult``.

    Do NOT use this for: cancelling a running execution (use the WS
    ``cancel`` envelope — the cancel chain reaches the run automatically via
    ``wait_for_completion``'s cancel handler); polling a running execution
    (use ``wait_for_completion``); inspecting a completed run's outputs
    (those land in ``RunResult.output_uri`` per FR-CE-4).

    Params:
        solver: lowercase solver identifier. v0.1 supports ``"sfincs"``
            only; other values raise ``SolverNotRegisteredError`` per
            the kickoff's lazy-per-milestone deploy strategy.
        model_setup_uri: ``s3://`` URI of the manifest the solver envelope
            will read (the job-0040 manifest schema: ``{"inputs":[...],
            "sfincs_args":[...], "outputs":[...]}``). Input URIs inside the
            manifest are resolved by scheme (job-0291). Engine job-0042's
            ``model_flood_scenario`` workflow composes this from the M4
            atomic-tool substrate.
        compute_class: FR-CE-3 compute class — selects the AWS Batch sizing
            bucket (small / standard / large / xlarge / gpu). Defaults to
            ``"medium"`` (== standard).

    Returns:
        ``ExecutionHandle{handle_id, run_id, solver, compute_class,
        workflows_execution_id, workflow_name, workflow_location,
        submitted_at}`` — the Invariant-8 cancellation contract. The
        ``workflow_name`` pins the backend (``local-docker`` / ``local-exec``
        / ``aws-batch``) so ``wait_for_completion`` routes correctly.

    FR-DC-6: This tool is uncacheable-by-construction (solver dispatch is
    explicitly enumerated). The cache shim is NOT invoked.

    Invariant 8 (cancellation): the returned handle carries everything
    ``wait_for_completion`` needs to terminate the live run on the matching
    cancel envelope.

    Raises:
        SolverNotRegisteredError: ``solver`` not in
            ``SOLVER_WORKFLOW_REGISTRY``.
        SolverDispatchError: the backend dispatch failed (IAM,
            quota, malformed manifest). The exception is re-raised so the
            emitter classifier surfaces ``UPSTREAM_API_ERROR`` to the
            client.
    """
    if not isinstance(solver, str) or not solver.strip():
        raise SolverNotRegisteredError(
            f"solver must be a non-empty string; got {solver!r}"
        )
    workflow_name = SOLVER_WORKFLOW_REGISTRY.get(solver)
    if workflow_name is None:
        raise SolverNotRegisteredError(
            f"solver {solver!r} not registered for v0.1; supported: "
            f"{sorted(SOLVER_WORKFLOW_REGISTRY)} (lazy per-milestone deploy "
            "per sprint-07 strategy — TELEMAC / MODFLOW / HEC-HMS land in "
            "their respective milestones)."
        )
    if not isinstance(model_setup_uri, str) or not model_setup_uri:
        raise SolverDispatchError(
            f"model_setup_uri must be a non-empty string; got {model_setup_uri!r}"
        )

    # --- Backend seam (job-0291 local-docker + sprint-16 aws-batch). GCP is
    # decommissioned: the Cloud Workflows dispatch path is removed and
    # ``solver_backend()`` only ever returns ``local-docker`` or ``aws-batch``.
    # The handle each backend returns pins its own backend (workflow_name) so
    # wait_for_completion routes correctly even if the env churns afterward. ---
    backend = solver_backend()
    if backend == SOLVER_BACKEND_LOCAL_DOCKER:
        return _run_solver_local_docker(
            solver=solver,
            model_setup_uri=model_setup_uri,
            compute_class=compute_class,
        )
    # ``aws-batch`` is the default (everything that is not ``local-docker``).
    return _run_solver_aws_batch(
        solver=solver,
        model_setup_uri=model_setup_uri,
        compute_class=compute_class,
    )


# --------------------------------------------------------------------------- #
# wait_for_completion
# --------------------------------------------------------------------------- #


_WAIT_FOR_COMPLETION_METADATA = AtomicToolMetadata(
    name="wait_for_completion",
    ttl_class="live-no-cache",
    source_class="solver_dispatch",
    cacheable=False,
)


def _progress_percent(handle_submitted_at: datetime, now: datetime) -> int:
    """Compute the wall-clock-linear progress estimate clamped to
    ``PROGRESS_CLAMP_MAX`` while the Workflow is still running.

    Invariant 1 (Determinism boundary): this is wall-clock arithmetic, not
    an LLM estimate. The ramp is intentionally simple and conservative —
    a real per-step progress signal would require teaching the SFINCS
    entrypoint to write running progress to ``progress.json`` between
    timesteps, which is a follow-up job (OQ-41-PROGRESS-CURVE).
    """
    elapsed = max(0.0, (now - handle_submitted_at).total_seconds())
    raw = (elapsed / NFR_P_4_TARGET_SECONDS) * 100.0
    capped = min(PROGRESS_CLAMP_MAX, max(0, int(raw)))
    return capped


async def _emit_progress(progress_percent: int) -> None:
    """Push a progress update to the active emitter binding (if any)."""
    binding = _EMITTER_BINDING
    if binding is None:
        return
    try:
        await binding.emitter.update_progress(binding.step_id, progress_percent)
    except Exception as exc:  # noqa: BLE001 — emission must never fail the poll
        logger.warning("emitter.update_progress raised: %s", exc)


@register_tool(
    _WAIT_FOR_COMPLETION_METADATA,
    # Annotations: readOnlyHint=False (emits pipeline-state progress envelopes
    # as a side effect on every poll tick — stateful even though it does not
    # write to the object store directly), openWorldHint=False (polls the S3
    # completion.json + AWS Batch job status; no public external API),
    # destructiveHint=False (reads completion.json from the runs bucket; does
    # not overwrite anything), idempotentHint=False (each call emits progress
    # events; cancellation path terminates the live container / Batch job).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def wait_for_completion(
    handle: ExecutionHandle,
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> RunResult:
    """Poll the solver run backing ``handle`` until terminal.

    Use this when: the agent has an ``ExecutionHandle`` from ``run_solver``
    and needs the ``RunResult`` (and the ``output_uri``) before continuing
    the pipeline. The tool blocks while the solver runs but is cancellable
    via the WS ``cancel`` chain (Invariant 8 — see module docstring).

    Do NOT use this for: starting a new run (use ``run_solver``); short,
    synchronous tool calls (atomic tools are sub-second; this is the
    solver-class blocking pattern).

    Params:
        handle: the ``ExecutionHandle`` returned by ``run_solver``. The
            ``workflow_name`` field pins the backend (``local-docker`` /
            ``local-exec`` / ``aws-batch``) so the poll routes correctly.
        poll_interval_s: seconds between completion polls. Default 10s —
            matches NFR-P-4 ≤15-min budget granularity (≥9 polls per run).
            Surfaced as OQ-41-POLL-INTERVAL.
        timeout_s: hard ceiling. Defaults to 1800 s (30 min — gives 2×
            headroom over NFR-P-4). On timeout the tool returns
            ``RunResult{status="failed", error_code="SOLVER_TIMEOUT"}``
            and best-effort cancels the run.

    Returns:
        ``RunResult{run_id, handle_id, status, output_uri?, started_at,
        completed_at, duration_seconds, error_code?, error_message?,
        cancellation_reason?}`` — terminal outcome. ``status="complete"``
        carries the ``output_uri`` parsed from ``completion.json``;
        ``"failed"`` carries the error code/message; ``"cancelled"``
        carries a ``cancellation_reason``.

    FR-DC-6: This tool is uncacheable-by-construction. The cache shim is
    NOT invoked.

    Invariant 8 (cancellation): when the M1 WS cancel chain raises
    ``asyncio.CancelledError`` inside this coroutine's poll-sleep, the
    backend handler terminates the live container / Batch job before
    re-raising so cancellation is initiated within ≤30 s per NFR-R-3.
    """
    if poll_interval_s < 0:
        raise SolverDispatchError(
            f"poll_interval_s must be non-negative; got {poll_interval_s!r}"
        )
    if timeout_s <= 0:
        raise SolverDispatchError(
            f"timeout_s must be positive; got {timeout_s!r}"
        )

    # --- job-0291 backend seam: a handle pins its backend (the handle's
    # workflow_name, not the env, decides — env churn between submit and wait
    # cannot mis-route the poll). ``local-docker`` / ``local-exec`` (job-0292b,
    # MODFLOW direct-binary) share the S3 completion poll; ``aws-batch``
    # (sprint-16) polls the SAME S3 completion.json + consults
    # batch.describe_jobs. GCP Cloud Workflows is decommissioned. ---
    if handle.workflow_name in _LOCAL_WORKFLOW_NAMES:
        return await _wait_for_completion_local(handle, poll_interval_s, timeout_s)
    if handle.workflow_name == AWS_BATCH_WORKFLOW_NAME:
        return await _wait_for_completion_aws_batch(handle, poll_interval_s, timeout_s)

    raise SolverDispatchError(
        f"unsupported handle backend {handle.workflow_name!r}: the Cloud "
        "Workflows substrate is decommissioned; expected one of "
        f"{(*_LOCAL_WORKFLOW_NAMES, AWS_BATCH_WORKFLOW_NAME)}."
    )


# --------------------------------------------------------------------------- #
# Result-building helpers
# --------------------------------------------------------------------------- #


def _to_utc(value: Any) -> datetime | None:
    """Coerce a value that may be a ``datetime``, a proto Timestamp, or a
    string into a UTC ``datetime``. Returns ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # Proto Timestamp has a ``ToDatetime`` method.
    to_datetime = getattr(value, "ToDatetime", None)
    if callable(to_datetime):
        try:
            dt = to_datetime()
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _duration(started_at: datetime | None, completed_at: datetime) -> float | None:
    if started_at is None:
        return None
    return max(0.0, (completed_at - started_at).total_seconds())


def _solver_error_code(manifest: dict[str, Any]) -> str:
    """Map a completion-manifest error to an open-set A.6 SCREAMING_SNAKE_CASE
    error code. Keep narrow; the catch-all bucket is ``SOLVER_FAILED``.

    Surfaced as OQ-41-ERROR-CODE-REGISTRY — when sprint-08 lands more
    solver-specific failure modes (SFINCS_MASS_BALANCE_DIVERGED,
    MODEL_DECK_INVALID, etc.) the registry expands here.
    """
    exit_code = manifest.get("exit_code")
    if exit_code is not None and exit_code != 0:
        # Surface the most common known SFINCS exit shapes once we observe
        # them in real runs; for now we surface a generic code carrying the
        # exit code in the message.
        return "SOLVER_FAILED"
    return "SOLVER_FAILED"
