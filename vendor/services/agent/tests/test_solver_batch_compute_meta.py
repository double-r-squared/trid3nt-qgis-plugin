"""Batch SOLVE telemetry: instance + problem-size + timing capture (task-153).

Records the AWS Batch Spot instance type + lifecycle + AZ + the queue/compute/
total timing breakdown a solve landed on, MERGED with the mesh size descriptor
(active-cell count + resolution), so a perf model can later infer completion
time. Three layers under test, all AWS-free (boto3 fakes via the
``set_batch_client`` / ``set_ecs_client`` / ``set_ec2_client`` seams):

1. ``solver._capture_batch_compute_meta`` — the describe-jobs -> ECS -> EC2 chain
   returns the merged dict; degrades gracefully to ``None`` (describe-jobs fails)
   or to partial (the Spot box is gone) without ever raising.
2. ``telemetry.record_solve_telemetry`` — writes the expected SOLVE row shape to
   the JSONL sink with the ``record_type="solve"`` discriminator; never raises on
   a bad sink.
3. The composer record helpers (SFINCS ``_record_flood_batch_solve_telemetry`` +
   SWMM ``_record_swmm_batch_solve_telemetry``) — assemble + record ONE solve row
   merging ``run_result.batch_compute_meta`` (instance + timing) with the mesh
   descriptor (cell_count + resolution_m) + solver + status, via a mocked
   telemetry writer.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    _capture_batch_compute_meta,
    _cluster_arn_from_ci_arn,
    run_solver,
    set_batch_client,
    set_ec2_client,
    set_ecs_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    wait_for_completion,
)
from grace2_contracts.execution import RunResult


# --------------------------------------------------------------------------- #
# Fakes — boto3-shaped describe responses.
# --------------------------------------------------------------------------- #

_CI_ARN = (
    "arn:aws:ecs:us-west-2:226996537797:container-instance/"
    "grace2-solvers_Batch_abc/0123456789abcdef0123456789abcdef"
)
_CLUSTER_ARN = "arn:aws:ecs:us-west-2:226996537797:cluster/grace2-solvers_Batch_abc"


class _FakeBatch:
    """``describe_jobs`` returns one job with container + timing + sizing."""

    def __init__(self, job: dict[str, Any] | None, *, raise_on_describe: bool = False) -> None:
        self._job = job
        self._raise = raise_on_describe
        self.describe_calls: list[list[str]] = []

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        # Used by run_solver in the wait-loop integration test.
        return {"jobId": "batch-job-xyz", "jobName": kwargs.get("jobName")}

    def describe_jobs(self, jobs: list[str]) -> dict[str, Any]:  # noqa: N803
        self.describe_calls.append(list(jobs))
        if self._raise:
            raise RuntimeError("describe_jobs boom")
        return {"jobs": [self._job] if self._job is not None else []}

    def terminate_job(self, jobId: str, reason: str) -> dict[str, Any]:  # noqa: N803
        return {}


class _FakeEcs:
    def __init__(self, ec2_instance_id: str | None = "i-0abc") -> None:
        self._id = ec2_instance_id
        self.calls: list[dict[str, Any]] = []

    def describe_container_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        ci = {} if self._id is None else {"ec2InstanceId": self._id}
        return {"containerInstances": [ci]}


class _FakeEc2:
    def __init__(
        self,
        *,
        instance_type: str | None = "c7i.2xlarge",
        lifecycle: str | None = "spot",
        az: str | None = "us-west-2d",
        raise_not_found: bool = False,
    ) -> None:
        self._type = instance_type
        self._lifecycle = lifecycle
        self._az = az
        self._raise = raise_not_found

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:  # noqa: N803
        if self._raise:
            # Mirrors a terminated Spot box: boto3 raises InvalidInstanceID.NotFound.
            raise RuntimeError("InvalidInstanceID.NotFound")
        inst: dict[str, Any] = {}
        if self._type is not None:
            inst["InstanceType"] = self._type
        if self._lifecycle is not None:
            inst["InstanceLifecycle"] = self._lifecycle
        if self._az is not None:
            inst["Placement"] = {"AvailabilityZone": self._az}
        return {"Reservations": [{"Instances": [inst]}]}


def _job(
    *,
    created: int = 1_000_000,
    started: int = 1_030_000,
    stopped: int = 1_530_000,
    vcpus: str = "8",
    memory: str = "16384",
    ci_arn: str | None = _CI_ARN,
) -> dict[str, Any]:
    container: dict[str, Any] = {
        "resourceRequirements": [
            {"type": "VCPU", "value": vcpus},
            {"type": "MEMORY", "value": memory},
        ],
    }
    if ci_arn is not None:
        container["containerInstanceArn"] = ci_arn
    return {
        "jobId": "batch-job-xyz",
        "status": "SUCCEEDED",
        "createdAt": created,
        "startedAt": started,
        "stoppedAt": stopped,
        "container": container,
    }


@pytest.fixture()
def reset_meta_seams():
    for setter in (set_batch_client, set_ecs_client, set_ec2_client):
        setter(None)
    try:
        yield
    finally:
        for setter in (set_batch_client, set_ecs_client, set_ec2_client):
            setter(None)


# --------------------------------------------------------------------------- #
# 1. _capture_batch_compute_meta — full merged dict.
# --------------------------------------------------------------------------- #


def test_cluster_arn_derived_from_ci_arn() -> None:
    assert _cluster_arn_from_ci_arn(_CI_ARN) == _CLUSTER_ARN
    assert _cluster_arn_from_ci_arn("not-an-arn") is None


def test_capture_full_merged_dict(reset_meta_seams) -> None:
    set_batch_client(_FakeBatch(_job()))
    ecs = _FakeEcs("i-0abc")
    set_ecs_client(ecs)
    set_ec2_client(_FakeEc2())

    meta = _capture_batch_compute_meta("batch-job-xyz")
    assert meta is not None
    # Instance fields from the EC2 describe.
    assert meta["instance_type"] == "c7i.2xlarge"
    assert meta["instance_lifecycle"] == "spot"
    assert meta["az"] == "us-west-2d"
    # Sizing from the Batch resourceRequirements.
    assert meta["vcpus"] == 8
    assert meta["memory_mib"] == 16384
    # Timing breakdown (ms -> derived seconds).
    assert meta["created_at_ms"] == 1_000_000
    assert meta["started_at_ms"] == 1_030_000
    assert meta["stopped_at_ms"] == 1_530_000
    assert meta["queue_provision_secs"] == 30.0  # (1_030_000 - 1_000_000) / 1000
    assert meta["compute_secs"] == 500.0  # (1_530_000 - 1_030_000) / 1000
    assert meta["total_secs"] == 530.0  # (1_530_000 - 1_000_000) / 1000
    # The cluster ARN was derived from the CI ARN (no compute-env lookup).
    assert ecs.calls[0]["cluster"] == _CLUSTER_ARN


def test_capture_onendemand_lifecycle_normalized(reset_meta_seams) -> None:
    # On-demand instances OMIT InstanceLifecycle entirely -> normalize to
    # "on-demand" rather than leaving it None.
    set_batch_client(_FakeBatch(_job()))
    set_ecs_client(_FakeEcs("i-0abc"))
    set_ec2_client(_FakeEc2(lifecycle=None))
    meta = _capture_batch_compute_meta("batch-job-xyz")
    assert meta is not None
    assert meta["instance_lifecycle"] == "on-demand"


# --------------------------------------------------------------------------- #
# 1b. Graceful degradation — never raises, returns None / partial.
# --------------------------------------------------------------------------- #


def test_capture_returns_none_when_describe_jobs_raises(reset_meta_seams) -> None:
    set_batch_client(_FakeBatch(None, raise_on_describe=True))
    # ECS/EC2 left None -> the lazy boto3 default would be built, but we never
    # reach them because describe-jobs short-circuits to None.
    assert _capture_batch_compute_meta("batch-job-xyz") is None


def test_capture_returns_none_when_no_job(reset_meta_seams) -> None:
    set_batch_client(_FakeBatch(None))  # empty jobs[]
    assert _capture_batch_compute_meta("missing-job") is None


def test_capture_partial_when_instance_gone(reset_meta_seams) -> None:
    # The Spot box was scale-to-zero terminated by the time the job is terminal:
    # describe-instances raises NotFound. The instance fields degrade to None but
    # the timing + sizing fields (from describe-jobs) still populate, and the
    # capture NEVER raises.
    set_batch_client(_FakeBatch(_job()))
    set_ecs_client(_FakeEcs("i-0gone"))
    set_ec2_client(_FakeEc2(raise_not_found=True))
    meta = _capture_batch_compute_meta("batch-job-xyz")
    assert meta is not None
    assert meta["instance_type"] is None
    assert meta["instance_lifecycle"] is None
    assert meta["az"] is None
    # Timing + sizing survive.
    assert meta["vcpus"] == 8
    assert meta["compute_secs"] == 500.0


def test_capture_partial_when_no_container_instance(reset_meta_seams) -> None:
    # A job that never placed on a container instance (RUNNABLE/capacity timeout)
    # has no containerInstanceArn -> ECS/EC2 are skipped, timing still populates.
    job = _job(ci_arn=None)
    set_batch_client(_FakeBatch(job))
    meta = _capture_batch_compute_meta("batch-job-xyz")
    assert meta is not None
    assert meta["instance_type"] is None
    assert meta["vcpus"] == 8
    assert meta["total_secs"] == 530.0


def test_capture_handles_missing_timing(reset_meta_seams) -> None:
    # A job that has not stopped yet -> stoppedAt absent -> derived seconds None,
    # no crash.
    job = _job()
    del job["stoppedAt"]
    set_batch_client(_FakeBatch(job))
    set_ecs_client(_FakeEcs("i-0abc"))
    set_ec2_client(_FakeEc2())
    meta = _capture_batch_compute_meta("batch-job-xyz")
    assert meta is not None
    assert meta["stopped_at_ms"] is None
    assert meta["compute_secs"] is None
    assert meta["total_secs"] is None
    assert meta["queue_provision_secs"] == 30.0  # started - created still computes


# --------------------------------------------------------------------------- #
# 2. record_solve_telemetry — expected SOLVE row shape, never-raise.
# --------------------------------------------------------------------------- #


def test_record_solve_telemetry_writes_shape(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import logging

    from grace2_agent.telemetry import record_solve_telemetry

    out = tmp_path / "solve_records.jsonl"
    monkeypatch.setenv("GRACE2_SOLVE_TELEMETRY_PATH", str(out))

    row = {
        "run_id": "RUN-1",
        "solver": "sfincs",
        "status": "complete",
        "backend": "aws-batch",
        "active_cell_count": 130_000,
        "resolution_m": 50.0,
        "instance_type": "c7i.2xlarge",
        "instance_lifecycle": "spot",
        "az": "us-west-2d",
        "vcpus": 8,
        "memory_mib": 16384,
        "queue_provision_secs": 30.0,
        "compute_secs": 500.0,
        "total_secs": 530.0,
    }
    with caplog.at_level(logging.INFO, logger="grace2_agent.solve_telemetry"):
        rec = record_solve_telemetry(row)

    # Discriminator + ts stamped.
    assert rec["record_type"] == "solve"
    assert rec["ts"].endswith("Z")
    # JSONL row written with the full merged shape.
    assert out.exists()
    written = json.loads(out.read_text().strip())
    assert written["record_type"] == "solve"
    assert written["run_id"] == "RUN-1"
    assert written["instance_type"] == "c7i.2xlarge"
    assert written["instance_lifecycle"] == "spot"
    assert written["active_cell_count"] == 130_000
    assert written["resolution_m"] == 50.0
    assert written["compute_secs"] == 500.0
    # The structured log line always fires (durable scrape-able signal).
    assert any("solve_record" in r.message for r in caplog.records)


def test_record_solve_telemetry_never_raises_on_bad_sink(monkeypatch) -> None:
    from grace2_agent.telemetry import record_solve_telemetry

    monkeypatch.setenv(
        "GRACE2_SOLVE_TELEMETRY_PATH", "/no/such/dir/solve.jsonl"
    )
    rec = record_solve_telemetry({"run_id": "R"})  # write fails, must not raise
    assert rec["run_id"] == "R"
    assert rec["record_type"] == "solve"


def test_record_solve_telemetry_tolerates_non_dict(monkeypatch, tmp_path) -> None:
    from grace2_agent.telemetry import record_solve_telemetry

    monkeypatch.setenv("GRACE2_SOLVE_TELEMETRY_PATH", str(tmp_path / "s.jsonl"))
    rec = record_solve_telemetry(None)  # type: ignore[arg-type]
    assert rec["record_type"] == "solve"


# --------------------------------------------------------------------------- #
# 3. Composer record helpers — merge batch_compute_meta + mesh descriptor.
# --------------------------------------------------------------------------- #


def _meta() -> dict:
    return {
        "instance_type": "c7i.4xlarge",
        "instance_lifecycle": "spot",
        "az": "us-west-2a",
        "vcpus": 16,
        "memory_mib": 32768,
        "created_at_ms": 1_000_000,
        "started_at_ms": 1_010_000,
        "stopped_at_ms": 1_610_000,
        "queue_provision_secs": 10.0,
        "compute_secs": 600.0,
        "total_secs": 610.0,
    }


def test_flood_composer_records_solve_row(monkeypatch) -> None:
    from grace2_agent.workflows import model_flood_scenario as F

    captured: list[dict] = []
    monkeypatch.setattr(
        "grace2_agent.telemetry.record_solve_telemetry",
        lambda rec: captured.append(rec) or rec,
    )

    run_result = RunResult(
        run_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        handle_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        status="complete",
        duration_seconds=610.0,
        batch_compute_meta=_meta(),
    )
    handle = type(
        "H", (), {"solver": "sfincs", "workflow_name": "aws-batch"}
    )()
    model_setup = type(
        "MS",
        (),
        {
            "grid_resolution_m": 50.0,
            "parameters": {"autoscale": {"estimated_active_cells": 130_000}},
        },
    )()

    row = F._record_flood_batch_solve_telemetry(
        run_result=run_result,
        handle=handle,
        model_setup=model_setup,
        grid_resolution_m=30.0,
        session_id="SESS-1",
        case_id=None,
    )
    assert len(captured) == 1
    assert row is not None
    # Instance + timing folded in from batch_compute_meta.
    assert row["instance_type"] == "c7i.4xlarge"
    assert row["compute_secs"] == 600.0
    # Mesh size descriptor folded in.
    assert row["active_cell_count"] == 130_000
    assert row["resolution_m"] == 50.0  # built res wins over the workflow var
    # Solver + status + backend + session.
    assert row["solver"] == "sfincs"
    assert row["status"] == "complete"
    assert row["backend"] == "aws-batch"
    assert row["session_id"] == "SESS-1"


def test_flood_composer_records_row_without_meta(monkeypatch) -> None:
    # batch_compute_meta None (capture failed) -> the row still records the mesh
    # descriptor + status, just with absent instance fields.
    from grace2_agent.workflows import model_flood_scenario as F

    captured: list[dict] = []
    monkeypatch.setattr(
        "grace2_agent.telemetry.record_solve_telemetry",
        lambda rec: captured.append(rec) or rec,
    )
    run_result = RunResult(
        run_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        handle_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        status="failed",
        batch_compute_meta=None,
    )
    handle = type("H", (), {"solver": "sfincs", "workflow_name": "aws-batch"})()
    model_setup = type(
        "MS", (), {"grid_resolution_m": 30.0, "parameters": {}}
    )()
    row = F._record_flood_batch_solve_telemetry(
        run_result=run_result,
        handle=handle,
        model_setup=model_setup,
        grid_resolution_m=30.0,
        session_id=None,
        case_id=None,
    )
    assert len(captured) == 1
    assert row["status"] == "failed"
    assert row["resolution_m"] == 30.0
    assert "instance_type" not in row or row.get("instance_type") is None


def test_swmm_composer_records_solve_row(monkeypatch) -> None:
    from grace2_agent.workflows import model_urban_flood_swmm as U

    captured: list[dict] = []
    monkeypatch.setattr(
        "grace2_agent.telemetry.record_solve_telemetry",
        lambda rec: captured.append(rec) or rec,
    )

    run_result = type(
        "R",
        (),
        {
            "run_id": "BATCH-RID",
            "status": "complete",
            "batch_compute_meta": _meta(),
        },
    )()
    handle = type(
        "H", (), {"solver": U.SWMM_SOLVER_NAME, "workflow_name": "aws-batch"}
    )()
    build = type("B", (), {"n_active_cells": 4200, "resolution_m": 10.0})()

    row = U._record_swmm_batch_solve_telemetry(
        run_result=run_result,
        handle=handle,
        build=build,
        run_id="STAGED-RID",
        compute_class="large",
    )
    assert len(captured) == 1
    assert row is not None
    assert row["solver"] == U.SWMM_SOLVER_NAME
    assert row["status"] == "complete"
    assert row["compute_class"] == "large"
    # Mesh descriptor.
    assert row["active_cell_count"] == 4200
    assert row["resolution_m"] == 10.0
    # Instance + timing folded in.
    assert row["instance_type"] == "c7i.4xlarge"
    assert row["total_secs"] == 610.0
    # Run id prefers the RunResult's (the Batch worker's minted id).
    assert row["run_id"] == "BATCH-RID"


# --------------------------------------------------------------------------- #
# 4. Wait-loop integration — the terminal RunResult carries batch_compute_meta.
# --------------------------------------------------------------------------- #


class _S3WithCompletion:
    """Minimal S3 fake returning a SUCCEEDED completion.json for any run_id."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self._payload = json.dumps(
            {
                "status": "ok",
                "exit_code": 0,
                "started_at": "2026-06-17T00:00:00Z",
                "finished_at": "2026-06-17T00:08:00Z",
                "output_uris": [],
                "error": None,
            }
        ).encode()

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key.endswith("completion.json"):
            return {"Body": io.BytesIO(self._payload)}
        from botocore.exceptions import ClientError

        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": Key}}, "GetObject"
        )


@pytest.fixture()
def batch_wait_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-solvers")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs-jobdef:7")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


@pytest.mark.asyncio
async def test_wait_loop_attaches_batch_compute_meta(
    reset_meta_seams, batch_wait_env, monkeypatch
) -> None:
    set_runs_bucket(None)
    set_emitter_binding(None)
    batch = _FakeBatch(_job())
    set_batch_client(batch)
    set_s3_client(_S3WithCompletion("test-runs-bucket"))
    set_ecs_client(_FakeEcs("i-0abc"))
    set_ec2_client(_FakeEc2())

    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert result.status == "complete"
    assert result.batch_compute_meta is not None
    assert result.batch_compute_meta["instance_type"] == "c7i.2xlarge"
    assert result.batch_compute_meta["instance_lifecycle"] == "spot"
    assert result.batch_compute_meta["compute_secs"] == 500.0

    set_runs_bucket(None)
    set_emitter_binding(None)
