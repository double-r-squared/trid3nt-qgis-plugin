"""AWS Batch solver backend tests (sprint-16, SFINCS per-job autoscale).

Staged-for-cutover backend: ``GRACE2_SOLVER_BACKEND=aws-batch`` mints a run_id,
submits an AWS Batch job (the SAME SFINCS image the local-docker path runs),
stashes the Batch jobId in ``ExecutionHandle.workflows_execution_id`` (NO
contract change), and ``wait_for_completion`` polls the SAME completion.json on
S3 (reusing ``_try_get_completion_s3`` + ``_build_local_run_result``) with an
``batch.describe_jobs`` early-FAILED consult and a ``batch.terminate_job``
cancel branch.

These tests prove (kickoff §test list):

1.  Default / unset env → backend stays gcp-workflows; aws-batch is exact-match
    only (additive — unknown values still fall through).
2.  submit_job args: jobQueue/jobDefinition from env, containerOverrides with
    --run-id / --manifest-uri command + env (GRACE2_RUNS_BUCKET / GRACE2_RUN_ID
    / GRACE2_MANIFEST_URI / OMP_NUM_THREADS) + resourceRequirements VCPU/MEMORY
    from the compute_class sizing map; handle shape (workflow_name=aws-batch,
    jobId in workflows_execution_id).
3.  wait_for_completion reuses the S3 completion poll → RunResult complete.
4.  cancel → batch.terminate_job(jobId) + re-raise (Invariant 8).
5.  INERT when env unset: missing queue / job-def / runs-bucket raises a clean
    typed SolverDispatchError (never a crash).

All boto3 batch + S3 calls go through the ``set_batch_client`` /
``set_s3_client`` seams with dict-backed fakes — NO real AWS.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    AWS_BATCH_COMPUTE_CLASS_SIZING,
    AWS_BATCH_WORKFLOW_NAME,
    SOLVER_BACKEND_AWS_BATCH,
    EmitterBinding,
    SolverDispatchError,
    run_solver,
    set_batch_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    solver_backend,
    wait_for_completion,
)
from grace2_contracts.execution import RunResult


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _no_such_key(key: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": f"missing {key}"}}, "GetObject"
    )


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _no_such_key(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        return {}


class FakeBatchClient:
    """boto3-Batch-shaped fake recording submit/describe/terminate calls."""

    def __init__(self, *, submit_job_id: str = "batch-job-abc123") -> None:
        self.submit_job_id = submit_job_id
        self.submit_calls: list[dict[str, Any]] = []
        self.describe_calls: list[list[str]] = []
        self.terminate_calls: list[tuple[str, str]] = []
        # When set (jobId -> reason), describe_jobs reports FAILED for that id.
        self.failed_jobs: dict[str, str] = {}

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls.append(kwargs)
        return {"jobId": self.submit_job_id, "jobName": kwargs.get("jobName")}

    def describe_jobs(self, jobs: list[str]) -> dict[str, Any]:  # noqa: N803
        self.describe_calls.append(list(jobs))
        out = []
        for jid in jobs:
            if jid in self.failed_jobs:
                out.append({"jobId": jid, "status": "FAILED", "statusReason": self.failed_jobs[jid]})
            else:
                out.append({"jobId": jid, "status": "RUNNING"})
        return {"jobs": out}

    def terminate_job(self, jobId: str, reason: str) -> dict[str, Any]:  # noqa: N803
        self.terminate_calls.append((jobId, reason))
        return {}


def _seed_completion(s3: FakeS3Client, run_id: str, *, bucket: str, status: str = "ok") -> None:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "sfincs_stdout_uri": f"s3://{bucket}/{run_id}/sfincs.stdout",
        "sfincs_stderr_uri": f"s3://{bucket}/{run_id}/sfincs.stderr",
        "output_uris": [f"s3://{bucket}/{run_id}/sfincs_map.nc"] if status == "ok" else [],
        "started_at": "2026-06-17T00:00:00Z",
        "finished_at": "2026-06-17T00:08:00Z",
        "error": None if status == "ok" else "sfincs exited with non-zero code 1",
    }
    s3.objects[(bucket, f"{run_id}/completion.json")] = json.dumps(payload).encode()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_seams():
    for setter in (set_s3_client, set_batch_client):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    solver_mod._LOCAL_RUNS.clear()
    try:
        yield
    finally:
        for setter in (set_s3_client, set_batch_client):
            setter(None)
        set_emitter_binding(None)
        set_runs_bucket(None)
        solver_mod._LOCAL_RUNS.clear()


@pytest.fixture()
def batch_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-sfincs-queue")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs-jobdef:7")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


# --------------------------------------------------------------------------- #
# 1. Backend seam — additive, exact-match only
# --------------------------------------------------------------------------- #


def test_backend_unset_is_aws_batch(reset_seams, monkeypatch) -> None:
    # GCP decommissioned: the unset default is now aws-batch.
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    assert solver_backend() == SOLVER_BACKEND_AWS_BATCH


def test_backend_aws_batch_exact_match(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    assert solver_backend() == SOLVER_BACKEND_AWS_BATCH


def test_backend_unknown_value_falls_through(reset_seams, monkeypatch) -> None:
    # Unknown/typo values now fall through to the aws-batch default.
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "gcp-workflows-typo")
    assert solver_backend() == SOLVER_BACKEND_AWS_BATCH


def test_backend_legacy_gcp_workflows_falls_through(reset_seams, monkeypatch) -> None:
    # GCP Cloud Workflows is decommissioned: the legacy value now falls
    # through to the aws-batch default (no dead gcp-workflows backend).
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "gcp-workflows")
    assert solver_backend() == SOLVER_BACKEND_AWS_BATCH


# --------------------------------------------------------------------------- #
# 2. submit_job args + handle shape
# --------------------------------------------------------------------------- #


def test_aws_batch_submit_job_args_and_handle(reset_seams, batch_env) -> None:
    batch = FakeBatchClient()
    set_batch_client(batch)
    handle = run_solver(
        solver="sfincs",
        model_setup_uri="s3://deck-bucket/cache/setup/manifest.json",
        compute_class="large",
    )

    # Handle shape: aws-batch sentinel + jobId stashed in workflows_execution_id.
    assert handle.workflow_name == AWS_BATCH_WORKFLOW_NAME
    assert handle.workflows_execution_id == "batch-job-abc123"
    assert handle.solver == "sfincs"
    assert handle.compute_class == "large"

    assert len(batch.submit_calls) == 1
    call = batch.submit_calls[0]
    assert call["jobQueue"] == "grace2-sfincs-queue"
    assert call["jobDefinition"] == "grace2-sfincs-jobdef:7"
    overrides = call["containerOverrides"]

    # command carries --run-id + --manifest-uri.
    cmd = overrides["command"]
    assert "--run-id" in cmd and "--manifest-uri" in cmd
    assert cmd[cmd.index("--manifest-uri") + 1] == "s3://deck-bucket/cache/setup/manifest.json"
    assert cmd[cmd.index("--run-id") + 1] == handle.run_id

    # env carries the runs bucket / run id / manifest / OMP threads.
    env = {e["name"]: e["value"] for e in overrides["environment"]}
    assert env["GRACE2_RUNS_BUCKET"] == "test-runs-bucket"
    assert env["GRACE2_RUN_ID"] == handle.run_id
    assert env["GRACE2_MANIFEST_URI"] == "s3://deck-bucket/cache/setup/manifest.json"
    sizing = AWS_BATCH_COMPUTE_CLASS_SIZING["large"]
    assert env["OMP_NUM_THREADS"] == str(sizing["omp_threads"])

    # resourceRequirements VCPU/MEMORY from the sizing map (as strings).
    rr = {r["type"]: r["value"] for r in overrides["resourceRequirements"]}
    assert rr["VCPU"] == str(sizing["vcpus"])
    assert rr["MEMORY"] == str(sizing["mem_mib"])


def test_aws_batch_medium_maps_to_standard_8vcpu(reset_seams, batch_env) -> None:
    batch = FakeBatchClient()
    set_batch_client(batch)
    run_solver(solver="sfincs", model_setup_uri="s3://b/m.json", compute_class="medium")
    overrides = batch.submit_calls[0]["containerOverrides"]
    rr = {r["type"]: r["value"] for r in overrides["resourceRequirements"]}
    assert rr["VCPU"] == "8"  # medium == standard bucket
    assert rr["MEMORY"] == "16384"


# --------------------------------------------------------------------------- #
# 3. wait_for_completion reuses the S3 completion poll
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aws_batch_wait_polls_s3_completion(reset_seams, batch_env) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)
    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
    _seed_completion(s3, handle.run_id, bucket="test-runs-bucket", status="ok")

    result: RunResult = await wait_for_completion(handle, poll_interval_s=0)
    assert result.status == "complete"
    assert result.run_id == handle.run_id
    assert result.output_uri == f"s3://test-runs-bucket/{handle.run_id}/"


@pytest.mark.asyncio
async def test_aws_batch_wait_failed_completion(reset_seams, batch_env) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)
    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
    _seed_completion(s3, handle.run_id, bucket="test-runs-bucket", status="error")

    result = await wait_for_completion(handle, poll_interval_s=0)
    assert result.status == "failed"
    assert result.error_code == "SOLVER_FAILED"


@pytest.mark.asyncio
async def test_aws_batch_wait_early_failed_via_describe(reset_seams, batch_env) -> None:
    """No completion.json + Batch reports FAILED → fail fast (don't poll forever)."""
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)
    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
    batch.failed_jobs[handle.workflows_execution_id] = "CannotPullContainerError"

    result = await wait_for_completion(handle, poll_interval_s=0, timeout_s=300)
    assert result.status == "failed"
    assert result.error_code == "SOLVER_DISPATCH_FAILED"
    assert "CannotPullContainerError" in (result.error_message or "")
    assert batch.describe_calls  # describe_jobs was consulted


# --------------------------------------------------------------------------- #
# task-149: the wait-loop surfaces the live Batch phase on the compute card
# --------------------------------------------------------------------------- #


class _RecordingComputeEmitter:
    """Records the wait-loop's phase signals: ``update_compute_status`` patches +
    the ``solve-progress`` ticks carrying ``phase``. Mirrors the
    PipelineEmitter surface the wait-loop drives via the EmitterBinding."""

    def __init__(self) -> None:
        self.compute_status: list[tuple[str, str]] = []
        self.solve_progress: list[dict] = []

    async def update_compute_status(self, step_id: str, batch_status: str) -> None:
        self.compute_status.append((step_id, batch_status))

    async def update_progress(self, step_id: str, percent: int) -> None:
        # The composer points the binding at the compute step; regular progress
        # rides the same step (no-op for this assertion).
        pass

    async def emit_solve_progress(self, payload: dict) -> None:
        self.solve_progress.append(payload)


class _DeferredCompletionS3(FakeS3Client):
    """Returns the completion.json only AFTER the first poll tick so the wait
    loop runs >= 1 no-completion tick (which reads + surfaces the Batch phase)."""

    def __init__(self, *, ready_after: int) -> None:
        super().__init__()
        self._calls = 0
        self._ready_after = ready_after

    def get_object(self, Bucket: str, Key: str):  # noqa: N803
        if Key.endswith("completion.json"):
            self._calls += 1
            if self._calls <= self._ready_after:
                raise _no_such_key(Key)
        return super().get_object(Bucket=Bucket, Key=Key)


@pytest.mark.asyncio
async def test_aws_batch_wait_surfaces_phase_on_compute_card(
    reset_seams, batch_env
) -> None:
    """task-149: each no-completion poll tick reads the live DescribeJobs status
    (RUNNING) and pushes it to the bound compute card BOTH ways — a
    ``update_compute_status`` patch + a ``solve-progress`` tick carrying
    ``phase`` — and the OK completion surfaces the terminal SUCCEEDED phase."""
    batch = FakeBatchClient()  # describe_jobs reports RUNNING by default
    s3 = _DeferredCompletionS3(ready_after=1)  # 1 RUNNING tick, then OK
    set_batch_client(batch)
    set_s3_client(s3)
    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
    _seed_completion(s3, handle.run_id, bucket="test-runs-bucket", status="ok")

    em = _RecordingComputeEmitter()
    set_emitter_binding(EmitterBinding(emitter=em, step_id="sim-step-1"))

    result: RunResult = await wait_for_completion(handle, poll_interval_s=0)
    assert result.status == "complete"

    # The RUNNING phase was surfaced on the no-completion tick, then SUCCEEDED on
    # the OK completion — both via update_compute_status (the compute card patch).
    statuses = [s for _sid, s in em.compute_status]
    assert "RUNNING" in statuses
    assert "SUCCEEDED" in statuses
    assert all(sid == "sim-step-1" for sid, _ in em.compute_status)

    # And both rode a solve-progress tick carrying the phase verbatim.
    phases = [p.get("phase") for p in em.solve_progress]
    assert "RUNNING" in phases
    assert "SUCCEEDED" in phases


# --------------------------------------------------------------------------- #
# 4. Cancel → terminate_job + re-raise (Invariant 8)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aws_batch_cancel_terminates_job(reset_seams, batch_env) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()  # never seed completion → wait loops until cancelled
    set_batch_client(batch)
    set_s3_client(s3)
    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")

    task = asyncio.create_task(wait_for_completion(handle, poll_interval_s=0.05))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert batch.terminate_calls, "terminate_job must be called on cancel"
    jid, reason = batch.terminate_calls[-1]
    assert jid == handle.workflows_execution_id
    assert "cancel" in reason.lower()


@pytest.mark.asyncio
async def test_aws_batch_timeout_terminates_and_returns_solver_timeout(
    reset_seams, batch_env
) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)
    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")

    result = await wait_for_completion(handle, poll_interval_s=0, timeout_s=1)
    assert result.status == "failed"
    assert result.error_code == "SOLVER_TIMEOUT"
    # The Batch job is terminated on timeout (not a user cancel).
    assert batch.terminate_calls
    assert batch.terminate_calls[-1][0] == handle.workflows_execution_id


# --------------------------------------------------------------------------- #
# 5. INERT until env present — clean typed SolverDispatchError, never a crash
# --------------------------------------------------------------------------- #


def test_aws_batch_missing_queue_raises_typed(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.delenv("GRACE2_AWS_BATCH_QUEUE", raising=False)
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "jd:1")
    set_batch_client(FakeBatchClient())
    with pytest.raises(SolverDispatchError, match="GRACE2_AWS_BATCH_QUEUE"):
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")


def test_aws_batch_missing_job_def_raises_typed(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "q")
    monkeypatch.delenv("GRACE2_AWS_BATCH_JOB_DEF", raising=False)
    set_batch_client(FakeBatchClient())
    with pytest.raises(SolverDispatchError, match="GRACE2_AWS_BATCH_JOB_DEF"):
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")


def test_aws_batch_missing_runs_bucket_raises_typed(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.delenv("GRACE2_RUNS_BUCKET", raising=False)
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "q")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "jd:1")
    set_batch_client(FakeBatchClient())
    with pytest.raises(SolverDispatchError, match="GRACE2_RUNS_BUCKET"):
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")


def test_aws_batch_submit_failure_raises_typed(reset_seams, batch_env) -> None:
    class BoomBatch(FakeBatchClient):
        def submit_job(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException: not authorized to submit")

    set_batch_client(BoomBatch())
    with pytest.raises(SolverDispatchError, match="submit_job failed"):
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")


def test_aws_batch_rejects_plain_path(reset_seams, batch_env) -> None:
    set_batch_client(FakeBatchClient())
    with pytest.raises(SolverDispatchError, match="s3:// or gs://"):
        run_solver(solver="sfincs", model_setup_uri="/tmp/manifest.json")


def test_aws_batch_rejects_file_uri_off_box_honesty_guard(
    reset_seams, batch_env
) -> None:
    """J-A: a ``file://`` deck URI is REJECTED loud + cheap BEFORE any Batch
    submit / Spot spend.

    The ephemeral off-box Batch worker has NO access to the agent box local FS,
    so forwarding a ``file://`` deck silently crashes the solve (the exact SWMM
    0-for-3 failure). The guard now accepts ONLY s3:// / gs:// and raises a typed
    SolverDispatchError (SOLVER_DISPATCH_FAILED) for file:// — protecting SWMM +
    MODFLOW + every future Batch caller. The message must point the caller at
    object-storage staging (data-source honesty norm)."""
    set_batch_client(FakeBatchClient())
    with pytest.raises(SolverDispatchError) as excinfo:
        run_solver(
            solver="swmm",
            model_setup_uri="file:///opt/grace2/runs/deck/manifest.json",
        )
    # Typed code + a clear, honest message that names the off-box FS gap.
    assert excinfo.value.error_code == "SOLVER_DISPATCH_FAILED"
    msg = str(excinfo.value)
    assert "s3:// or gs://" in msg
    assert "local filesystem" in msg


def test_aws_batch_accepts_gs_uri(reset_seams, batch_env) -> None:
    """J-A: ``gs://`` is still accepted (the guard only tightened to drop the
    local-FS file:// case; object-store schemes pass through unchanged)."""
    set_batch_client(FakeBatchClient())
    # Submits cleanly (no SolverDispatchError from the scheme guard).
    handle = run_solver(solver="sfincs", model_setup_uri="gs://b/m.json")
    assert handle.run_id
