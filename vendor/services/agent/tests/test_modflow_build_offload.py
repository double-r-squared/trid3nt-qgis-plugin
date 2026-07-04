"""Heavy-compute offload — MODFLOW BUILD + postprocess moved onto the Batch worker.

MODFLOW analogue of ``test_sfincs_build_offload.py`` (SFINCS reference). These
tests prove the agent<->worker CONTRACT + the solver submit routing WITHOUT any
real AWS / flopy (the boto3 batch client is the dict-backed fake).

Covered:
  * ``submit_modflow_build_solve`` routes to the ``modflow-build`` job-def key +
    passes ``--build-spec-uri`` (env + CLI), falling back to the generic
    ``grace2-modflow`` job-def (GRACE2_AWS_BATCH_JOB_DEF_MODFLOW — the SAME image,
    no new job-def) and NEVER to the SFINCS generic GRACE2_AWS_BATCH_JOB_DEF.
  * a non-aws-batch backend stays inert (typed DeckBuildError).
  * the composer's run_args serialization round-trips through the worker's
    ``validate_job_spec`` / ``build_deck_kwargs_from_spec`` (no field loss).
  * the worker's ``validate_job_spec`` accepts a well-formed spec + rejects a
    malformed one (the schema gate the worker runs before the build).
  * the offload is gated OFF by default (``GRACE2_MODFLOW_BUILD_OFFLOAD``).
"""

from __future__ import annotations

from typing import Any

import pytest

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    MODFLOW_BUILD_SOLVER,
    DeckBuildError,
    set_batch_client,
    set_runs_bucket,
    set_s3_client,
    submit_modflow_build_solve,
)


class FakeBatchClient:
    def __init__(self, *, submit_job_id: str = "batch-modflow-build-1") -> None:
        self.submit_job_id = submit_job_id
        self.submit_calls: list[dict[str, Any]] = []

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls.append(kwargs)
        return {"jobId": self.submit_job_id, "jobName": kwargs.get("jobName")}


@pytest.fixture()
def reset_seams():
    for setter in (set_s3_client, set_batch_client):
        setter(None)
    set_runs_bucket(None)
    solver_mod._LOCAL_RUNS.clear()
    try:
        yield
    finally:
        for setter in (set_s3_client, set_batch_client):
            setter(None)
        set_runs_bucket(None)
        solver_mod._LOCAL_RUNS.clear()


@pytest.fixture()
def batch_env(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-solvers")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    for key in (
        "GRACE2_AWS_BATCH_JOB_DEF",
        "GRACE2_AWS_BATCH_JOB_DEF_MODFLOW",
        "GRACE2_AWS_BATCH_JOB_DEF_MODFLOW_BUILD",
    ):
        monkeypatch.delenv(key, raising=False)


_SPEC_URI = "s3://test-cache/cache/static-30d/modflow_build/abc/modflow_build_spec.json"


def test_submit_build_solve_uses_dedicated_jobdef_when_set(
    reset_seams, batch_env, monkeypatch
) -> None:
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_MODFLOW_BUILD", "grace2-modflow-build:2")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_MODFLOW", "grace2-modflow:5")  # ignored
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")  # NEVER cross-routed
    batch = FakeBatchClient()
    set_batch_client(batch)

    handle = submit_modflow_build_solve(_SPEC_URI, compute_class="standard")

    call = batch.submit_calls[0]
    assert call["jobDefinition"] == "grace2-modflow-build:2"
    assert call["jobName"].startswith("grace2-modflow-build-")
    cmd = call["containerOverrides"]["command"]
    assert cmd[0] == "--run-id" and "--build-spec-uri" in cmd
    assert cmd[cmd.index("--build-spec-uri") + 1] == _SPEC_URI
    env = {e["name"]: e["value"] for e in call["containerOverrides"]["environment"]}
    assert env["GRACE2_BUILD_SPEC_URI"] == _SPEC_URI
    assert env["GRACE2_OBJECT_STORE"] == "s3"
    assert handle.solver == MODFLOW_BUILD_SOLVER


def test_submit_build_solve_falls_back_to_generic_modflow_jobdef(
    reset_seams, batch_env, monkeypatch
) -> None:
    # No dedicated build job-def -> the generic grace2-modflow job-def (the SAME
    # image) serves build+solve with no new job-def. The SFINCS generic is set but
    # MUST NOT be used (no cross-routing to the SFINCS container).
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_MODFLOW", "grace2-modflow:5")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")
    batch = FakeBatchClient()
    set_batch_client(batch)

    submit_modflow_build_solve(_SPEC_URI, compute_class="standard")
    assert batch.submit_calls[0]["jobDefinition"] == "grace2-modflow:5"


def test_submit_build_solve_inert_without_any_modflow_jobdef(
    reset_seams, batch_env
) -> None:
    # Only the SFINCS generic is available (batch_env cleared the MODFLOW ones) ->
    # the offload stays inert rather than cross-routing to the SFINCS image.
    with pytest.raises(DeckBuildError):
        submit_modflow_build_solve(_SPEC_URI)


def test_submit_build_solve_inert_off_batch(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    with pytest.raises(DeckBuildError):
        submit_modflow_build_solve(_SPEC_URI)


def test_run_args_round_trip_agent_to_worker() -> None:
    from grace2_contracts.modflow_contracts import MODFLOWRunArgs
    from grace2_agent.workflows.run_modflow import _run_args_to_deck_kwargs
    from services.workers._modflow_build import (
        build_deck_kwargs_from_spec,
        validate_job_spec,
    )

    run_args = MODFLOWRunArgs(
        spill_location_latlon=(40.81, -96.71),
        contaminant="benzene",
        release_rate_kg_s=0.5,
        duration_days=30.0,
    )
    deck_kwargs = _run_args_to_deck_kwargs(run_args)
    spec = {
        "schema_version": 1,
        "engine": "modflow",
        "spec_id": "abc",
        "run_args": deck_kwargs,
        "options": {"compute_class": "standard"},
    }
    validated = validate_job_spec(spec)
    kwargs = build_deck_kwargs_from_spec(validated)

    assert kwargs["contaminant"] == "benzene"
    assert kwargs["release_rate_kg_s"] == 0.5
    assert kwargs["duration_days"] == 30.0
    # spill_location_latlon round-trips to a [lat, lon] list (build_modflow_deck
    # accepts list-or-tuple).
    assert list(kwargs["spill_location_latlon"]) == [40.81, -96.71]
    # aquifer_k_ms / porosity carry the contract defaults into the spec.
    assert "aquifer_k_ms" in kwargs and "porosity" in kwargs
    # Reserved build controls must never leak from the spec.
    assert "workdir" not in kwargs and "write" not in kwargs


def test_worker_validate_job_spec_accepts_and_rejects() -> None:
    from services.workers._modflow_build import validate_job_spec

    ok = validate_job_spec(
        {
            "schema_version": 1,
            "engine": "modflow",
            "run_args": {
                "spill_location_latlon": [40.81, -96.71],
                "contaminant": "TCE",
                "release_rate_kg_s": 0.25,
                "duration_days": 10.0,
            },
        }
    )
    assert ok["options"] == {}
    assert ok["run_args"]["spill_location_latlon"] == [40.81, -96.71]

    # missing required run_args field (duration_days)
    with pytest.raises(ValueError):
        validate_job_spec(
            {
                "schema_version": 1,
                "run_args": {
                    "spill_location_latlon": [1.0, 2.0],
                    "contaminant": "x",
                    "release_rate_kg_s": 1.0,
                },
            }
        )
    # unknown schema_version
    with pytest.raises(ValueError):
        validate_job_spec(
            {"schema_version": 999, "run_args": {
                "spill_location_latlon": [1.0, 2.0], "contaminant": "x",
                "release_rate_kg_s": 1.0, "duration_days": 1.0,
            }}
        )
    # bad spill_location_latlon shape
    with pytest.raises(ValueError):
        validate_job_spec(
            {"schema_version": 1, "run_args": {
                "spill_location_latlon": [1.0], "contaminant": "x",
                "release_rate_kg_s": 1.0, "duration_days": 1.0,
            }}
        )


def test_offload_gated_off_by_default(monkeypatch) -> None:
    from grace2_agent.workflows.run_modflow import modflow_build_offload_enabled

    monkeypatch.delenv("GRACE2_MODFLOW_BUILD_OFFLOAD", raising=False)
    assert modflow_build_offload_enabled() is False
    monkeypatch.setenv("GRACE2_MODFLOW_BUILD_OFFLOAD", "on")
    assert modflow_build_offload_enabled() is True
