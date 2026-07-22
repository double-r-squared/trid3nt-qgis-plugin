"""Proves the pre-solver progress driver emits frequent pipeline-state DATA
frames during a long, silent, off-loop pre-solver phase -- the fix for the
browser WS force-reconnect ("run hangs/goes dark") during build_sfincs_model.
"""
import asyncio
import time

import pytest

from trid3nt_server.workflows.model_flood_scenario import (
    _drive_presolver_phase_progress,
)


class _FakeEmitter:
    def __init__(self):
        self.progress_calls = []
        self.ts = []

    async def update_current_progress(self, pct):
        self.progress_calls.append(pct)
        self.ts.append(time.monotonic())


@pytest.mark.asyncio
async def test_driver_emits_frequent_frames_during_silent_build(monkeypatch):
    # Tight tick so the test is fast; the real default is 7s.
    monkeypatch.setattr(
        "trid3nt_server.workflows.model_flood_scenario._PRESOLVER_PROGRESS_TICK_S",
        0.1,
    )
    em = _FakeEmitter()
    task = asyncio.ensure_future(
        _drive_presolver_phase_progress(
            em, start_pct=30, end_pct=88, expected_seconds=1.0
        )
    )
    # Simulate a 0.65s silent off-loop build.
    await asyncio.sleep(0.65)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # ~6 ticks over 0.65s at 0.1s cadence -> connection never silent > one tick.
    assert len(em.progress_calls) >= 4, em.progress_calls
    # Max gap between frames stays at ~one tick (no long silence).
    gaps = [b - a for a, b in zip(em.ts, em.ts[1:])]
    assert max(gaps) < 0.3, gaps
    # Percent creeps within the band and never exceeds it.
    assert all(30 <= p <= 88 for p in em.progress_calls), em.progress_calls
    assert em.progress_calls == sorted(em.progress_calls), em.progress_calls


@pytest.mark.asyncio
async def test_driver_is_noop_without_emitter():
    # No emitter (direct/smoke/test path) -> returns immediately, never raises.
    await _drive_presolver_phase_progress(
        None, start_pct=5, end_pct=24, expected_seconds=60.0
    )
