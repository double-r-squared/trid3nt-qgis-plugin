"""Per-solver AWS Batch job-def routing tests (sprint-16 P7).

SWMM is the FIRST non-SFINCS Batch user and runs a DIFFERENT image, so the
job-definition is resolved PER SOLVER. The pre-P7 code read ONE
``GRACE2_AWS_BATCH_JOB_DEF`` regardless of solver, which would have submitted a
``solver='swmm'`` run to the SFINCS image. ``_resolve_batch_job_def`` fixes
this with a three-tier resolver (first non-empty wins):

    1. ``GRACE2_AWS_BATCH_JOB_DEF_<SOLVER>`` env (UPPERCASE solver)
    2. ``SOLVER_BATCH_JOBDEF_REGISTRY[solver]`` (in-code per-solver default)
    3. ``GRACE2_AWS_BATCH_JOB_DEF`` env (the generic SFINCS-era fallback)

These tests prove (kickoff §test list):

  * solver='swmm' -> the SWMM job-def
  * solver='sfincs' -> the SFINCS job-def (unchanged)
  * fallback to the generic env when the per-solver SWMM env is unset
  * the FULL submit_job path routes the right job-def per solver
  * the inert-until-provisioned gate raises a clean typed error naming the
    per-solver env when nothing resolves

No real AWS — the boto3 batch client is the dict-backed fake from the sibling
suite via ``set_batch_client``.
"""

from __future__ import annotations

from typing import Any

import pytest

import grace2_agent.tools.solver as solver_mod

# Importing run_swmm registers 'swmm' in SOLVER_WORKFLOW_REGISTRY so
# run_solver(solver='swmm') dispatches (register_swmm_solver() runs at import).
import grace2_agent.workflows.run_swmm  # noqa: F401  (import side effect)
from grace2_agent.tools.solver import (
    SOLVER_BATCH_JOBDEF_REGISTRY,
    SolverDispatchError,
    _resolve_batch_job_def,
    run_solver,
    set_batch_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
)


class FakeBatchClient:
    """boto3-Batch-shaped fake recording submit calls (jobDefinition assert)."""

    def __init__(self, *, submit_job_id: str = "batch-job-p7") -> None:
        self.submit_job_id = submit_job_id
        self.submit_calls: list[dict[str, Any]] = []

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls.append(kwargs)
        return {"jobId": self.submit_job_id, "jobName": kwargs.get("jobName")}


@pytest.fixture()
def reset_seams():
    for setter in (set_s3_client, set_batch_client):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    SOLVER_BATCH_JOBDEF_REGISTRY.clear()
    solver_mod._LOCAL_RUNS.clear()
    try:
        yield
    finally:
        for setter in (set_s3_client, set_batch_client):
            setter(None)
        set_emitter_binding(None)
        set_runs_bucket(None)
        SOLVER_BATCH_JOBDEF_REGISTRY.clear()
        solver_mod._LOCAL_RUNS.clear()


@pytest.fixture()
def clear_jobdef_env(monkeypatch: pytest.MonkeyPatch):
    """Strip every job-def env var so each test sets exactly what it intends."""
    for var in (
        "GRACE2_AWS_BATCH_JOB_DEF",
        "GRACE2_AWS_BATCH_JOB_DEF_SWMM",
        "GRACE2_AWS_BATCH_JOB_DEF_SFINCS",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# 1. _resolve_batch_job_def — the three-tier resolver in isolation
# --------------------------------------------------------------------------- #


def test_resolve_swmm_per_solver_env_wins(reset_seams, clear_jobdef_env, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SWMM", "grace2-swmm:3")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")  # generic, ignored
    assert _resolve_batch_job_def("swmm") == "grace2-swmm:3"


def test_resolve_sfincs_uses_generic_unchanged(reset_seams, clear_jobdef_env, monkeypatch) -> None:
    # SFINCS path stays byte-identical: only the generic var set -> SFINCS uses it.
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")
    assert _resolve_batch_job_def("sfincs") == "grace2-sfincs:9"


def test_resolve_swmm_falls_back_to_generic_when_per_solver_unset(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    # Per-solver SWMM env unset -> falls through to the generic fallback.
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-generic:1")
    assert _resolve_batch_job_def("swmm") == "grace2-generic:1"


def test_resolve_empty_per_solver_env_is_treated_as_unset(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SWMM", "   ")  # whitespace == unset
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-generic:2")
    assert _resolve_batch_job_def("swmm") == "grace2-generic:2"


def test_resolve_registry_tier_between_env_tiers(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    # No per-solver env, but a registry default -> registry wins over generic.
    SOLVER_BATCH_JOBDEF_REGISTRY["swmm"] = "grace2-swmm-registry"
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-generic:5")
    assert _resolve_batch_job_def("swmm") == "grace2-swmm-registry"


def test_resolve_per_solver_env_beats_registry(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    SOLVER_BATCH_JOBDEF_REGISTRY["swmm"] = "grace2-swmm-registry"
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SWMM", "grace2-swmm-env")
    assert _resolve_batch_job_def("swmm") == "grace2-swmm-env"


def test_resolve_nothing_set_raises_typed_naming_per_solver_env(
    reset_seams, clear_jobdef_env
) -> None:
    with pytest.raises(SolverDispatchError, match="GRACE2_AWS_BATCH_JOB_DEF_SWMM"):
        _resolve_batch_job_def("swmm")


# --------------------------------------------------------------------------- #
# 2. End-to-end submit_job routes the right job-def per solver
# --------------------------------------------------------------------------- #


def _batch_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-solvers")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


def test_submit_swmm_routes_to_swmm_jobdef(reset_seams, clear_jobdef_env, monkeypatch) -> None:
    _batch_backend_env(monkeypatch)
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SWMM", "grace2-swmm:4")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")  # SFINCS generic
    batch = FakeBatchClient()
    set_batch_client(batch)

    handle = run_solver(solver="swmm", model_setup_uri="s3://b/swmm/manifest.json")

    assert handle.solver == "swmm"
    assert batch.submit_calls[0]["jobDefinition"] == "grace2-swmm:4"
    # jobName carries the solver tag so the two engines are distinguishable.
    assert batch.submit_calls[0]["jobName"].startswith("grace2-swmm-")
    # command still carries the worker-contract --run-id / --manifest-uri.
    cmd = batch.submit_calls[0]["containerOverrides"]["command"]
    assert cmd[cmd.index("--manifest-uri") + 1] == "s3://b/swmm/manifest.json"


def test_submit_sfincs_routes_to_sfincs_jobdef_unchanged(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    _batch_backend_env(monkeypatch)
    # Same env as above: SWMM per-solver set, SFINCS generic set. SFINCS must
    # NOT pick up the SWMM job-def.
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SWMM", "grace2-swmm:4")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-sfincs:9")
    batch = FakeBatchClient()
    set_batch_client(batch)

    handle = run_solver(solver="sfincs", model_setup_uri="s3://b/sfincs/manifest.json")

    assert handle.solver == "sfincs"
    assert batch.submit_calls[0]["jobDefinition"] == "grace2-sfincs:9"


def test_submit_swmm_fallback_to_generic_when_swmm_env_unset(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    # Only the generic var set -> BOTH solvers fall back to it (a single-job-def
    # box still works for SWMM; the routing degrades gracefully).
    _batch_backend_env(monkeypatch)
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF", "grace2-generic:7")
    batch = FakeBatchClient()
    set_batch_client(batch)

    h_swmm = run_solver(solver="swmm", model_setup_uri="s3://b/m.json")
    h_sfincs = run_solver(solver="sfincs", model_setup_uri="s3://b/m.json")

    assert batch.submit_calls[0]["jobDefinition"] == "grace2-generic:7"
    assert batch.submit_calls[1]["jobDefinition"] == "grace2-generic:7"
    assert h_swmm.solver == "swmm"
    assert h_sfincs.solver == "sfincs"


def test_submit_swmm_inert_when_no_jobdef_resolves(
    reset_seams, clear_jobdef_env, monkeypatch
) -> None:
    # aws-batch backend + queue set, but NO job-def of any tier -> clean typed
    # error naming the per-solver env (never a crash).
    _batch_backend_env(monkeypatch)
    set_batch_client(FakeBatchClient())
    with pytest.raises(SolverDispatchError, match="GRACE2_AWS_BATCH_JOB_DEF_SWMM"):
        run_solver(solver="swmm", model_setup_uri="s3://b/m.json")
