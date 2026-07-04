"""Turn-cancel Batch kill path tests (the submit/wait-window gap).

THE GAP: ``run_solver`` submits a Batch job (``batch.submit_job``) and returns
an ``ExecutionHandle`` in ONE tool call; ``wait_for_completion`` polls +
terminates-on-cancel in a SEPARATE, LATER tool call. The Invariant-8
``CancelledError -> batch.terminate_job`` chain inside ``wait_for_completion``
only fires when that coroutine is the frame being awaited. If the user cancels
the turn (stop button / same-stream re-prompt supersede) in the WINDOW between
submit and wait -- during the intervening LLM generation, or before the agent
ever issues ``wait_for_completion`` -- nothing terminated the job, so it kept
running on Spot (cost + orphaned result).

These tests prove the fix:

1.  ``run_solver`` (aws-batch) records the submitted jobId on the per-turn
    in-flight list once tracking is begun.
2.  CANCEL-IN-THE-WINDOW: submit, never wait, then
    ``terminate_inflight_batch_jobs`` calls ``terminate_job(jobId,
    reason="cancelled by user")`` -- the orphaned job is killed.
3.  HAPPY PATH: ``wait_for_completion`` running to a normal terminal clears the
    jobId, so a subsequent ``terminate_inflight_batch_jobs`` does NOT terminate
    (no spurious kill of a finished job).
4.  NO DOUBLE-TERMINATE: ``wait_for_completion``'s own cancel handler clears the
    jobId, so the turn-cancel cleanup that follows issues no second
    ``terminate_job``.
5.  Idempotent / safe no-op when nothing is in flight, and when no turn-tracking
    was begun (unbound ContextVar).
6.  The ``server._terminate_turn_inflight_batch_jobs`` integration: off-loop
    (``asyncio.to_thread``) terminate of the per-turn list end to end.

All boto3 batch + S3 calls go through the ``set_batch_client`` / ``set_s3_client``
seams with dict-backed fakes -- NO real AWS.
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
from typing import Any

import pytest

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    begin_turn_inflight_tracking,
    inflight_batch_jobs,
    run_solver,
    set_batch_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    terminate_inflight_batch_jobs,
    wait_for_completion,
)


# --------------------------------------------------------------------------- #
# Fakes (mirror test_solver_aws_batch.py)
# --------------------------------------------------------------------------- #


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": f"missing {Key}"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        return {}


class FakeBatchClient:
    """boto3-Batch-shaped fake recording submit/describe/terminate calls."""

    def __init__(self, *, submit_job_id: str = "batch-job-kill1") -> None:
        self.submit_job_id = submit_job_id
        self.submit_calls: list[dict[str, Any]] = []
        self.describe_calls: list[list[str]] = []
        self.terminate_calls: list[tuple[str, str]] = []

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls.append(kwargs)
        return {"jobId": self.submit_job_id, "jobName": kwargs.get("jobName")}

    def describe_jobs(self, jobs: list[str]) -> dict[str, Any]:  # noqa: N803
        self.describe_calls.append(list(jobs))
        return {"jobs": [{"jobId": j, "status": "RUNNING"} for j in jobs]}

    def terminate_job(self, jobId: str, reason: str) -> dict[str, Any]:  # noqa: N803
        self.terminate_calls.append((jobId, reason))
        return {}


def _seed_completion(
    s3: FakeS3Client, run_id: str, *, bucket: str, status: str = "ok"
) -> None:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "sfincs_stdout_uri": f"s3://{bucket}/{run_id}/sfincs.stdout",
        "sfincs_stderr_uri": f"s3://{bucket}/{run_id}/sfincs.stderr",
        "output_uris": [f"s3://{bucket}/{run_id}/sfincs_map.nc"]
        if status == "ok"
        else [],
        "started_at": "2026-06-17T00:00:00Z",
        "finished_at": "2026-06-17T00:08:00Z",
        "error": None if status == "ok" else "sfincs exited 1",
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
    yield
    for setter in (set_s3_client, set_batch_client):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)


@pytest.fixture()
def batch_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-sfincs-queue")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs-jobdef:7")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


def _run_in_fresh_context(fn):
    """Run ``fn`` in an isolated copy of the current Context.

    Each test that begins turn-tracking must do so in its OWN Context so the
    per-turn in-flight ContextVar binding does not leak across tests (mirrors a
    real per-task turn). ``contextvars.copy_context().run`` gives us that.
    """
    return contextvars.copy_context().run(fn)


# --------------------------------------------------------------------------- #
# 1. run_solver records the submitted jobId once tracking is begun
# --------------------------------------------------------------------------- #


def test_run_solver_records_inflight_jobid(reset_seams, batch_env) -> None:
    batch = FakeBatchClient(submit_job_id="batch-job-rec1")
    set_batch_client(batch)

    def body() -> None:
        begin_turn_inflight_tracking()
        assert inflight_batch_jobs() == []
        handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        assert handle.workflows_execution_id == "batch-job-rec1"
        # The submitted jobId is now tracked for this turn.
        assert inflight_batch_jobs() == ["batch-job-rec1"]

    _run_in_fresh_context(body)


def test_run_solver_without_tracking_is_noop(reset_seams, batch_env) -> None:
    # No begin_turn_inflight_tracking() -> register is a harmless no-op; the
    # existing wait_for_completion cancel chain still covers that case.
    batch = FakeBatchClient(submit_job_id="batch-job-untracked")
    set_batch_client(batch)

    def body() -> None:
        # Defensive: ensure no stray binding leaked in.
        assert inflight_batch_jobs() == []
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        assert inflight_batch_jobs() == []  # still unbound -> nothing tracked

    _run_in_fresh_context(body)


# --------------------------------------------------------------------------- #
# 2. CANCEL-IN-THE-WINDOW: submit, never wait -> terminate kills the orphan
# --------------------------------------------------------------------------- #


def test_terminate_inflight_kills_orphaned_job(reset_seams, batch_env) -> None:
    batch = FakeBatchClient(submit_job_id="batch-job-orphan")
    set_batch_client(batch)

    def body() -> list[str]:
        begin_turn_inflight_tracking()
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        # The user cancels the turn HERE -- wait_for_completion was never called.
        attempted = terminate_inflight_batch_jobs("cancelled by user")
        return attempted

    attempted = _run_in_fresh_context(body)

    assert attempted == ["batch-job-orphan"]
    assert batch.terminate_calls, "terminate_job must kill the orphaned job"
    jid, reason = batch.terminate_calls[-1]
    assert jid == "batch-job-orphan"
    assert "cancel" in reason.lower()


def test_terminate_inflight_kills_multiple_jobs(reset_seams, batch_env) -> None:
    # A turn that submitted two solvers (e.g. compound run) before cancel.
    batch = FakeBatchClient()
    seen_ids = ["batch-job-a", "batch-job-b"]
    it = iter(seen_ids)

    def submit_job(**kwargs: Any) -> dict[str, Any]:
        batch.submit_calls.append(kwargs)
        return {"jobId": next(it)}

    batch.submit_job = submit_job  # type: ignore[method-assign]
    set_batch_client(batch)

    def body() -> list[str]:
        begin_turn_inflight_tracking()
        run_solver(solver="sfincs", model_setup_uri="s3://b/a.json")
        run_solver(solver="sfincs", model_setup_uri="s3://b/b.json")
        return terminate_inflight_batch_jobs("cancelled by user")

    attempted = _run_in_fresh_context(body)
    assert set(attempted) == set(seen_ids)
    assert {c[0] for c in batch.terminate_calls} == set(seen_ids)


# --------------------------------------------------------------------------- #
# 3. HAPPY PATH: normal completion clears tracking -> no spurious terminate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_normal_completion_clears_inflight_no_terminate(
    reset_seams, batch_env
) -> None:
    batch = FakeBatchClient(submit_job_id="batch-job-happy")
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)

    async def body() -> None:
        begin_turn_inflight_tracking()
        handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        assert inflight_batch_jobs() == ["batch-job-happy"]
        # Seed a terminal-OK completion so the wait returns immediately.
        _seed_completion(s3, handle.run_id, bucket="test-runs-bucket")
        result = await wait_for_completion(handle, poll_interval_s=0)
        assert result.status == "complete"
        # The terminal poll cleared the jobId -- nothing left in flight.
        assert inflight_batch_jobs() == []
        # A turn-cancel cleanup that NOW runs must terminate nothing.
        attempted = terminate_inflight_batch_jobs("cancelled by user")
        assert attempted == []

    # Run the whole flow inside one fresh Context (one "turn").
    ctx = contextvars.copy_context()
    await ctx.run(lambda: asyncio.ensure_future(body()))

    # The ONLY interaction is the happy-path solve -- NO terminate_job at all.
    assert batch.terminate_calls == []


# --------------------------------------------------------------------------- #
# 4. NO DOUBLE-TERMINATE: wait cancel handler clears, cleanup is a no-op
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wait_cancel_clears_so_cleanup_does_not_double_terminate(
    reset_seams, batch_env
) -> None:
    batch = FakeBatchClient(submit_job_id="batch-job-nodbl")
    s3 = FakeS3Client()  # never seed -> wait loops until cancelled
    set_batch_client(batch)
    set_s3_client(s3)

    async def body() -> None:
        begin_turn_inflight_tracking()
        handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        assert inflight_batch_jobs() == ["batch-job-nodbl"]

        task = asyncio.ensure_future(
            wait_for_completion(handle, poll_interval_s=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # wait_for_completion's cancel handler already terminated AND cleared.
        assert inflight_batch_jobs() == []
        assert len(batch.terminate_calls) == 1  # exactly the wait-handler kill
        # The turn-cancel cleanup that follows must NOT terminate again.
        attempted = terminate_inflight_batch_jobs("cancelled by user")
        assert attempted == []
        assert len(batch.terminate_calls) == 1  # still exactly one

    ctx = contextvars.copy_context()
    await ctx.run(lambda: asyncio.ensure_future(body()))


# --------------------------------------------------------------------------- #
# 5. Idempotent / safe no-ops
# --------------------------------------------------------------------------- #


def test_terminate_inflight_noop_when_nothing_in_flight(
    reset_seams, batch_env
) -> None:
    batch = FakeBatchClient()
    set_batch_client(batch)

    def body() -> None:
        begin_turn_inflight_tracking()  # bound but empty
        assert terminate_inflight_batch_jobs("cancelled by user") == []
        assert batch.terminate_calls == []

    _run_in_fresh_context(body)


def test_terminate_inflight_noop_when_unbound(reset_seams, batch_env) -> None:
    # No begin_turn_inflight_tracking() at all (e.g. a non-turn context).
    batch = FakeBatchClient()
    set_batch_client(batch)

    def body() -> None:
        assert inflight_batch_jobs() == []
        assert terminate_inflight_batch_jobs("cancelled by user") == []
        assert batch.terminate_calls == []

    _run_in_fresh_context(body)


def test_terminate_inflight_idempotent_second_call(
    reset_seams, batch_env
) -> None:
    batch = FakeBatchClient(submit_job_id="batch-job-idem")
    set_batch_client(batch)

    def body() -> None:
        begin_turn_inflight_tracking()
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        first = terminate_inflight_batch_jobs("cancelled by user")
        second = terminate_inflight_batch_jobs("cancelled by user")
        assert first == ["batch-job-idem"]
        assert second == []  # already drained
        assert len(batch.terminate_calls) == 1  # never double-terminated

    _run_in_fresh_context(body)


def test_terminate_inflight_swallows_client_errors(reset_seams, batch_env) -> None:
    # A bad jobId / dead Batch client must never break the cancel.
    class BoomBatch(FakeBatchClient):
        def terminate_job(self, jobId: str, reason: str) -> dict[str, Any]:  # noqa: N803
            raise RuntimeError("boom: terminate failed")

    batch = BoomBatch(submit_job_id="batch-job-boom")
    set_batch_client(batch)

    def body() -> list[str]:
        begin_turn_inflight_tracking()
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        # Must NOT raise even though terminate_job throws.
        return terminate_inflight_batch_jobs("cancelled by user")

    attempted = _run_in_fresh_context(body)
    assert attempted == ["batch-job-boom"]  # it ATTEMPTED, swallowed the error
    assert inflight_batch_jobs() == []  # never observable across the boundary


# --------------------------------------------------------------------------- #
# 6. server._terminate_turn_inflight_batch_jobs integration (off-loop kill)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_server_helper_terminates_turn_inflight_offloop(
    reset_seams, batch_env
) -> None:
    from grace2_agent.server import (
        SessionState,
        _terminate_turn_inflight_batch_jobs,
    )

    batch = FakeBatchClient(submit_job_id="batch-job-srv")
    set_batch_client(batch)
    state = SessionState(session_id="sess-kill")

    async def body() -> None:
        begin_turn_inflight_tracking()
        run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
        assert inflight_batch_jobs() == ["batch-job-srv"]
        # The server cancel-cleanup helper (off-loop terminate via to_thread).
        await _terminate_turn_inflight_batch_jobs(state)
        assert inflight_batch_jobs() == []

    ctx = contextvars.copy_context()
    await ctx.run(lambda: asyncio.ensure_future(body()))

    assert batch.terminate_calls, "server helper must terminate the orphan job"
    jid, reason = batch.terminate_calls[-1]
    assert jid == "batch-job-srv"
    assert "cancel" in reason.lower()


@pytest.mark.asyncio
async def test_server_helper_noop_when_nothing_in_flight(
    reset_seams, batch_env
) -> None:
    from grace2_agent.server import (
        SessionState,
        _terminate_turn_inflight_batch_jobs,
    )

    batch = FakeBatchClient()
    set_batch_client(batch)
    state = SessionState(session_id="sess-clean")

    async def body() -> None:
        begin_turn_inflight_tracking()  # empty turn -- no solver submitted
        await _terminate_turn_inflight_batch_jobs(state)

    ctx = contextvars.copy_context()
    await ctx.run(lambda: asyncio.ensure_future(body()))

    assert batch.terminate_calls == []
