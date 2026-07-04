"""Re-land proof: the Batch control-plane submit is OFFLOADED off the asyncio
loop AND fail-fast bounded, plus progress frames flow on a regular cadence
across the solve-wait phase.

Context (NATE 2026-06-29): floods rendered only inputs and never the SFINCS
depth layer because the long build+solve starved/dropped the WS. Two fixes were
reverted and are re-landed:

  * offload the synchronous boto3 ``batch.submit_job`` off the event loop
    (``model_flood_scenario`` wraps ``run_solver`` in ``asyncio.to_thread``); and
  * a fail-fast ``botocore`` ``Config`` on the Batch client so a THROTTLED submit
    (the failure mode that hung the FIRST offload re-land for 3+ minutes with no
    jobId) surfaces a typed ``SolverDispatchError`` FAST instead of botocore
    retrying with backoff forever.

These tests prove, locally with no real AWS:

  (a) the event loop stays RESPONSIVE while a slow ``submit_job`` runs in a
      worker thread (heartbeat-class ticks keep firing);
  (b) a throttled / timed-out submit returns a FAST honest error rather than
      hanging, and the Batch client is constructed with a bounded fail-fast
      ``Config``; and
  (c) live solve-progress frames emit on a regular sub-watchdog cadence during
      the long solve-wait (the build-phase cadence is proven by
      ``test_presolver_progress_keepalive``).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from botocore.exceptions import ClientError

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    SolverDispatchError,
    _batch_client_config,
    _get_batch_client,
    run_solver,
    set_batch_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
)
from grace2_agent.workflows.model_flood_scenario import (
    _LIVE_SOLVE_PROGRESS_INTERVAL_S,  # noqa: F401 — referenced for cadence intent
    _drive_live_solve_progress,
)


# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #


def _throttle_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "SubmitJob",
    )


class _SlowSubmitBatchClient:
    """Batch-shaped fake whose ``submit_job`` BLOCKS (``time.sleep``) like a slow
    control-plane call, then returns a jobId. ``time.sleep`` releases the GIL, so
    when dispatched via ``asyncio.to_thread`` the event loop keeps running."""

    def __init__(self, *, block_s: float) -> None:
        self.block_s = block_s
        self.submit_calls: list[dict[str, Any]] = []

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls.append(kwargs)
        time.sleep(self.block_s)
        return {"jobId": "batch-slow-1", "jobName": kwargs.get("jobName")}


class _ThrottledBatchClient:
    """Batch-shaped fake whose ``submit_job`` raises a throttling ``ClientError``
    immediately -- stands in for the post-retry-cap surface the fail-fast
    ``Config`` produces (the real bound is asserted separately)."""

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        raise _throttle_error()


class _FakeSolveEmitter:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.ts: list[float] = []

    async def emit_solve_progress(self, payload: Any) -> None:
        self.calls.append(payload)
        self.ts.append(time.monotonic())


@pytest.fixture()
def reset_seams():
    for setter in (set_s3_client, set_batch_client):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    try:
        yield
    finally:
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


# --------------------------------------------------------------------------- #
# (a) loop stays responsive while the submit runs off-loop in a thread
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_loop_responsive_while_submit_runs_in_thread(reset_seams, batch_env):
    batch = _SlowSubmitBatchClient(block_s=0.6)
    set_batch_client(batch)

    ticks = 0

    async def _heartbeat(stop: asyncio.Event) -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.05)

    stop = asyncio.Event()
    hb = asyncio.ensure_future(_heartbeat(stop))
    # Offload the (blocking) submit exactly as model_flood_scenario does.
    handle = await asyncio.to_thread(
        run_solver,
        solver="sfincs",
        model_setup_uri="s3://deck-bucket/cache/setup/manifest.json",
        compute_class="medium",
    )
    stop.set()
    await hb

    # The submit blocked ~0.6s; an UN-offloaded sync submit would have starved the
    # loop and frozen the heartbeat. Off-loop, the 0.05s ticker keeps firing
    # (>=~8 expected; assert a safe floor) -> the 12s WS heartbeat would too.
    assert ticks >= 5, ticks
    assert handle.run_id
    assert len(batch.submit_calls) == 1


# --------------------------------------------------------------------------- #
# (b) a throttled / slow submit FAILS FAST with an honest error (no hang)
# --------------------------------------------------------------------------- #


def test_batch_client_config_is_bounded():
    """The fail-fast Config caps connect/read timeouts + total retry attempts so
    a throttled control plane cannot retry-with-backoff for minutes."""
    config = _batch_client_config()
    assert config is not None  # botocore is available in this env
    assert config.connect_timeout is not None and config.connect_timeout <= 5
    assert config.read_timeout is not None and config.read_timeout <= 15
    assert config.retries["mode"] == "standard"
    # Few attempts -> worst case ~ attempts * read_timeout, not minutes.
    assert config.retries["max_attempts"] <= 3


def test_get_batch_client_constructs_with_failfast_config(reset_seams, monkeypatch):
    """``_get_batch_client`` wires the bounded ``Config`` into the real boto3
    client (proving the fail-fast bound reaches the wire), not just builds it."""
    import boto3

    captured: dict[str, Any] = {}

    def _fake_client(service: str, **kwargs: Any):
        captured["service"] = service
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(boto3, "client", _fake_client)
    set_batch_client(None)  # force lazy construction
    _get_batch_client()

    assert captured["service"] == "batch"
    cfg = captured["kwargs"].get("config")
    assert cfg is not None, "Batch client built without a fail-fast Config"
    assert cfg.read_timeout is not None and cfg.read_timeout <= 15
    assert cfg.retries["max_attempts"] <= 3


@pytest.mark.asyncio
async def test_throttled_submit_fails_fast_not_hangs(reset_seams, batch_env):
    """A throttled submit (the failure mode that hung the first re-land) raises a
    typed ``SolverDispatchError`` PROMPTLY -- here bounded by asyncio.wait_for so a
    hang would fail the test rather than wedge it."""
    set_batch_client(_ThrottledBatchClient())

    async def _dispatch():
        return await asyncio.to_thread(
            run_solver,
            solver="sfincs",
            model_setup_uri="s3://deck-bucket/cache/setup/manifest.json",
            compute_class="medium",
        )

    started = time.monotonic()
    with pytest.raises(SolverDispatchError) as exc:
        # 2s ceiling: the throttle surfaces immediately; a hang would TimeoutError.
        await asyncio.wait_for(_dispatch(), timeout=2.0)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"submit did not fail fast ({elapsed:.2f}s)"
    # Honest, classifiable error -- not a hang, not a bare crash.
    assert "submit_job" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# (c) live solve-progress frames emit on a regular sub-watchdog cadence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_solve_progress_emits_regular_cadence(monkeypatch):
    """During the long solve-wait, ``_drive_live_solve_progress`` ticks a real
    solve-progress DATA frame on a regular cadence (well under the ~25-30s browser
    WS watchdog) so the connection never goes silent -> no force-reconnect."""
    monkeypatch.setattr(
        "grace2_agent.workflows.model_flood_scenario._LIVE_SOLVE_PROGRESS_INTERVAL_S",
        0.1,
    )
    em = _FakeSolveEmitter()
    task = asyncio.ensure_future(
        _drive_live_solve_progress(
            emitter=em,
            run_id="run-xyz",
            solver="sfincs",
            grid_resolution_m=30.0,
            active_cell_count=12000,
            vcpus=8,
            eta_seconds=120.0,
        )
    )
    await asyncio.sleep(0.65)  # simulate a silent multi-tick solve
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # ~6 frames over 0.65s at 0.1s cadence -> never silent longer than one tick.
    assert len(em.calls) >= 4, em.calls
    gaps = [b - a for a, b in zip(em.ts, em.ts[1:])]
    assert max(gaps) < 0.3, gaps


@pytest.mark.asyncio
async def test_solve_progress_noop_without_emitter():
    # No emitter (direct/smoke/test path) -> returns immediately, never raises.
    await _drive_live_solve_progress(
        emitter=None,
        run_id="run-xyz",
        solver="sfincs",
        grid_resolution_m=None,
        active_cell_count=None,
        vcpus=None,
        eta_seconds=None,
    )
