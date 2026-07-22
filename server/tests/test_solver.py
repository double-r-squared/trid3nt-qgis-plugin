"""Backend-agnostic unit tests for ``solver.py`` (job-0041, M5 Stage C).

GCP Cloud Workflows is decommissioned. The gcp-workflows happy-path / poll /
cancel / completion-read tests that this file used to carry (driven by a fake
``google.cloud.workflows.executions_v1.ExecutionsClient`` via
``set_workflows_client`` + ``TRID3NT_SOLVER_BACKEND=gcp-workflows``) are gone
with the backend. What remains here is the backend-AGNOSTIC coverage:

1. ``test_registry_registers_solver_tools_uncacheable`` — both atomic tools
   appear in ``TOOL_REGISTRY`` with ``cacheable=False`` +
   ``ttl_class="live-no-cache"`` + ``source_class="solver_dispatch"``
   (FR-DC-6 enumeration honored).
2. ``test_run_solver_rejects_unregistered_solver`` — ``solver="telemac"``
   raises ``SolverNotRegisteredError`` (lazy per-milestone deploy strategy).
   (``modflow`` is now a registered solver — wired to the generic AWS Batch
   seam alongside sfincs/swmm — so an as-yet-unbuilt engine name is used.)
3. ``test_progress_estimator_is_wall_clock_linear_clamped`` — pure-function
   guard on ``_progress_percent``.

The active-backend coverage (local-docker / aws-batch / MODFLOW local-exec)
lives in test_solver_local_docker.py / test_solver_aws_batch.py /
test_modflow_local_backend.py — none of which need a workflows client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.solver import (
    NFR_P_4_TARGET_SECONDS,
    PROGRESS_CLAMP_MAX,
    SOLVER_WORKFLOW_REGISTRY,
    SolverNotRegisteredError,
    _progress_percent,
    run_solver,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_solver_di_seams():
    """Reset the module-level DI handles before and after each test so the
    bindings from one test don't leak into the next."""
    set_emitter_binding(None)
    set_runs_bucket(None)
    set_s3_client(None)
    try:
        yield
    finally:
        set_emitter_binding(None)
        set_runs_bucket(None)
        set_s3_client(None)


# --------------------------------------------------------------------------- #
# 1. Registry: both tools register with FR-DC-6 metadata
# --------------------------------------------------------------------------- #


def test_registry_registers_solver_tools_uncacheable() -> None:
    """Both solver tools live in ``TOOL_REGISTRY`` with FR-DC-6 metadata."""
    assert "run_solver" in TOOL_REGISTRY
    assert "wait_for_completion" in TOOL_REGISTRY

    for tname in ("run_solver", "wait_for_completion"):
        entry = TOOL_REGISTRY[tname]
        meta = entry.metadata
        assert meta.cacheable is False, f"{tname} must be uncacheable (FR-DC-6)"
        assert meta.ttl_class == "live-no-cache", (
            f"{tname} ttl_class must be live-no-cache (FR-DC-6)"
        )
        assert meta.source_class == "solver_dispatch", (
            f"{tname} source_class must be solver_dispatch"
        )


# --------------------------------------------------------------------------- #
# 2. run_solver rejects unregistered solver (backend-agnostic — fails before
#    any dispatch)
# --------------------------------------------------------------------------- #


def test_run_solver_rejects_unregistered_solver(reset_solver_di_seams) -> None:
    """An engine that has NOT landed its milestone (e.g. ``telemac``) raises
    ``SolverNotRegisteredError`` (lazy per-milestone deploy strategy). This is
    backend-agnostic — the registry check fires before any dispatch. NOTE:
    ``modflow`` is now registered (wired to the generic AWS Batch seam), so an
    as-yet-unbuilt engine name is used for the rejection probe."""
    with pytest.raises(SolverNotRegisteredError) as exc_info:
        run_solver(solver="telemac", model_setup_uri="s3://x/y.json")
    assert "telemac" in str(exc_info.value)
    assert "sfincs" in str(exc_info.value)
    # sfincs is always registered; modflow/swmm register lazily when their
    # workflow modules import.
    assert set(SOLVER_WORKFLOW_REGISTRY) >= {"sfincs"}


# --------------------------------------------------------------------------- #
# 3. _progress_percent — pure-function guard
# --------------------------------------------------------------------------- #


def test_progress_estimator_is_wall_clock_linear_clamped() -> None:
    """At t=0 → 0%; at t=NFR_P_4_TARGET_SECONDS/2 → 50%; at and beyond
    t=NFR_P_4_TARGET_SECONDS → clamped to PROGRESS_CLAMP_MAX."""
    submitted = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
    assert _progress_percent(submitted, submitted) == 0
    half = submitted + timedelta(seconds=NFR_P_4_TARGET_SECONDS / 2)
    assert _progress_percent(submitted, half) == 50
    over = submitted + timedelta(seconds=NFR_P_4_TARGET_SECONDS + 1.0)
    assert _progress_percent(submitted, over) == PROGRESS_CLAMP_MAX
    # Determinism: negative elapsed (clock skew) clamps to 0.
    early = submitted - timedelta(seconds=10)
    assert _progress_percent(submitted, early) == 0
