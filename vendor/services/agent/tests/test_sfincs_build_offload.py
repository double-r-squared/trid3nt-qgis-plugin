"""Heavy-compute offload — SFINCS regular-grid BUILD moved onto the Batch worker.

Reference implementation (reports/design/heavy-compute-offload-2026-07-02.md):
the pluvial hydromt build (the former in-agent 16 GB driver) is composed into a
job_spec and dispatched as ONE combined build+solve Batch job. These tests prove
the agent<->worker CONTRACT + the solver submit routing WITHOUT any real AWS /
hydromt (the boto3 batch client is the dict-backed fake).

Covered:
  * ``submit_sfincs_build_solve`` routes to the ``sfincs-build`` job-def key +
    passes ``--build-spec-uri`` (env + CLI), falling back to the generic
    ``grace2-sfincs`` job-def (same image — no new job-def required).
  * a non-aws-batch backend stays inert (typed DeckBuildError).
  * the composer's forcing/options serialization round-trips through the worker's
    ``forcing_spec_from_dict`` / ``build_options_from_dict`` (no field loss).
  * the worker's ``validate_job_spec`` accepts a well-formed spec + rejects a
    malformed one (the schema gate the worker runs before the build).
  * the offload is gated OFF by default (``GRACE2_SFINCS_BUILD_OFFLOAD``).
"""

from __future__ import annotations

from typing import Any

import pytest

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    SFINCS_BUILD_SOLVER,
    DeckBuildError,
    set_batch_client,
    set_runs_bucket,
    set_s3_client,
    submit_sfincs_build_solve,
)


class FakeBatchClient:
    def __init__(self, *, submit_job_id: str = "batch-build-1") -> None:
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
        "GRACE2_AWS_BATCH_JOB_DEF_SFINCS_BUILD",
    ):
        monkeypatch.delenv(key, raising=False)


_SPEC_URI = "s3://test-cache/cache/static-30d/sfincs_build/abc/sfincs_build_spec.json"


def test_submit_build_solve_uses_dedicated_jobdef_when_set(
    reset_seams, batch_env, monkeypatch
) -> None:
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_BUILD", "grace2-sfincs-build:2")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")  # generic, ignored
    batch = FakeBatchClient()
    set_batch_client(batch)

    handle = submit_sfincs_build_solve(_SPEC_URI, compute_class="standard")

    call = batch.submit_calls[0]
    assert call["jobDefinition"] == "grace2-sfincs-build:2"
    assert call["jobName"].startswith("grace2-sfincs-build-")
    cmd = call["containerOverrides"]["command"]
    assert cmd[0] == "--run-id" and "--build-spec-uri" in cmd
    assert cmd[cmd.index("--build-spec-uri") + 1] == _SPEC_URI
    env = {e["name"]: e["value"] for e in call["containerOverrides"]["environment"]}
    assert env["GRACE2_BUILD_SPEC_URI"] == _SPEC_URI
    assert env["GRACE2_OBJECT_STORE"] == "s3"
    assert handle.solver == SFINCS_BUILD_SOLVER


def test_submit_build_solve_falls_back_to_generic_sfincs_jobdef(
    reset_seams, batch_env, monkeypatch
) -> None:
    # No dedicated build job-def -> the generic grace2-sfincs job-def (the SAME
    # image now bundling hydromt) serves build+solve with no new job-def.
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")
    batch = FakeBatchClient()
    set_batch_client(batch)

    submit_sfincs_build_solve(_SPEC_URI, compute_class="standard")
    assert batch.submit_calls[0]["jobDefinition"] == "grace2-sfincs:9"


def test_submit_build_solve_inert_off_batch(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    with pytest.raises(DeckBuildError):
        submit_sfincs_build_solve(_SPEC_URI)


def test_forcing_and_options_round_trip_agent_to_worker() -> None:
    from grace2_agent.workflows.model_flood_scenario import (
        _build_options_to_dict,
        _forcing_spec_to_dict,
    )
    from grace2_agent.workflows.sfincs_builder import (
        BuildOptions,
        DischargeForcing,
        ForcingSpec,
        InfiltrationForcing,
        WindForcing,
    )
    from services.workers._sfincs_build.deck import (
        build_options_from_dict,
        forcing_spec_from_dict,
    )

    fs = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=8.0,
        duration_hours=24.0,
        return_period_years=100,
        discharge=DischargeForcing(
            timeseries_uri="s3://b/dis.csv", locations_uri="s3://b/loc.fgb"
        ),
        wind=WindForcing(magnitude=20.0, direction=90.0),
        infiltration=InfiltrationForcing(cn_uri="s3://b/cn.tif"),
        provenance={"atlas14": "x"},
    )
    bo = BuildOptions(
        grid_resolution_m=50.0, compute_class="large", advanced_physics={"advection": 1}
    )

    fs2 = forcing_spec_from_dict(_forcing_spec_to_dict(fs))
    bo2 = build_options_from_dict(_build_options_to_dict(bo))

    assert fs2.forcing_type == "pluvial_synthetic" and fs2.precip_inches == 8.0
    assert fs2.discharge.timeseries_uri == "s3://b/dis.csv"
    assert fs2.wind.magnitude == 20.0 and fs2.infiltration.cn_uri == "s3://b/cn.tif"
    assert fs2.provenance == {"atlas14": "x"}
    assert bo2.grid_resolution_m == 50.0 and bo2.compute_class == "large"
    assert bo2.advanced_physics == {"advection": 1}


def test_worker_validate_job_spec_accepts_and_rejects() -> None:
    from services.workers._sfincs_build import validate_job_spec

    ok = validate_job_spec(
        {
            "schema_version": 1,
            "engine": "sfincs",
            "bbox": [-82.0, 26.0, -81.9, 26.1],
            "nlcd_vintage_year": 2021,
            "inputs": {
                "dem_uri": "s3://b/dem.tif",
                "landcover_uri": "s3://b/nlcd.tif",
            },
        }
    )
    assert ok["forcing"] == {} and ok["options"] == {}
    assert ok["bbox"] == [-82.0, 26.0, -81.9, 26.1]

    with pytest.raises(ValueError):
        validate_job_spec(
            {"schema_version": 1, "bbox": [1, 2, 3, 4], "inputs": {}}
        )
    with pytest.raises(ValueError):
        validate_job_spec({"schema_version": 999, "bbox": [1, 2, 3, 4], "inputs": {}})


def test_offload_gated_off_by_default(monkeypatch) -> None:
    from grace2_agent.workflows.model_flood_scenario import (
        _sfincs_build_offload_enabled,
    )

    monkeypatch.delenv("GRACE2_SFINCS_BUILD_OFFLOAD", raising=False)
    assert _sfincs_build_offload_enabled() is False
    monkeypatch.setenv("GRACE2_SFINCS_BUILD_OFFLOAD", "on")
    assert _sfincs_build_offload_enabled() is True
