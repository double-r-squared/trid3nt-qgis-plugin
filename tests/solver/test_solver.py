"""Backend-agnostic unit tests for ``solver.py``.

What holds whichever backend is selected: both atomic tools registered
uncacheable (``ttl_class="live-no-cache"``, ``source_class="solver_dispatch"``),
an unregistered solver name refused before dispatch, and the wall-clock-linear
clamped progress estimator. Per-backend coverage lives with each backend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.solver.solver import (
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


# 2. run_solver rejects unregistered solver (backend-agnostic — fails before
#    any dispatch)


def test_run_solver_rejects_unregistered_solver(reset_solver_di_seams) -> None:
    """A solver nobody registered raises ``SolverNotRegisteredError`` before any
    dispatch, and the refusal NAMES what is registered - borrowing another engine's
    spec is worse than a loud failure."""
    with pytest.raises(SolverNotRegisteredError) as exc_info:
        run_solver(solver="not_an_engine", model_setup_uri="s3://x/y.json")
    message = str(exc_info.value)
    assert "not_an_engine" in message
    # The refusal quotes the roster, so a caller can see what it could have asked.
    assert "telemac_river_dye" in message
    assert set(SOLVER_WORKFLOW_REGISTRY) >= {"telemac_river_dye"}




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
