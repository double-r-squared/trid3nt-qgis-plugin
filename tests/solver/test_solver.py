"""Backend-agnostic unit tests for ``solver.py``.

What holds whichever backend is selected: both atomic tools registered
uncacheable (``ttl_class="live-no-cache"``, ``source_class="solver_dispatch"``),
an unregistered solver name refused before dispatch, and the wall-clock-linear
clamped progress estimator. Per-backend coverage lives with each backend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.solver.solver import (
    LOCAL_SOLVER_SPEC_REGISTRY,
    SolverNotRegisteredError,
    run_solver,
)




@pytest.fixture()
def reset_solver_di_seams():
    """Reset the module-level DI handles before and after each test so the
    bindings from one test don't leak into the next."""
    storage.set_runs_bucket(None)
    storage.set_client(None)
    try:
        yield
    finally:
        storage.set_runs_bucket(None)
        storage.set_client(None)




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
    assert "telemac" in message
    assert set(LOCAL_SOLVER_SPEC_REGISTRY) >= {"telemac"}




@pytest.mark.parametrize("engine", ["geoclaw", "telemac", "modflow"])
def test_every_engine_solves_under_the_id_its_run_was_staged_with(engine, monkeypatch):
    import asyncio
    import importlib
    from types import SimpleNamespace

    mod = importlib.import_module(f"trid3nt_server.workflows.{engine}.engine")
    seen = {}

    async def _dispatch(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status="complete", started_at=None, run_id=kwargs["run_id"]), kwargs["run_id"]

    monkeypatch.setattr(mod, "dispatch_and_wait", _dispatch)
    monkeypatch.setattr(mod, "read_run_metrics", lambda run_id: {})
    facts = {"name": "x", "duration_s": 1, "utm_epsg": 32616, "mesh_size_m": None, "nelem": None, "ncells": None}
    run = {"run_id": "STAGED", "manifest_uri": "s3://b/m.json", "name": "x", "sheet": {}, "result_basename": "r",
           "case": {"module": "m", "cores": 1, "server_facts": facts}}
    out = asyncio.run(mod.solve_case(run=run, cores=1))

    assert seen["run_id"] == "STAGED" and out["run_id"] == "STAGED"
    assert seen["sheet"] == {} and "timeout_s" not in seen
