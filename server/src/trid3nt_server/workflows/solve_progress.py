"""Shared LIVE solve-progress heartbeat (extracted seam).

This is the **shared extraction** of the live solve-progress driver. It is a
verbatim copy of the proven SFINCS inline driver in
``model_flood_scenario.py`` (``_drive_live_solve_progress`` +
``_LIVE_SOLVE_PROGRESS_INTERVAL_S``); SFINCS keeps its inline copy for now (the
duplication is deliberate so the proven SFINCS path is not touched), and the
SWMM / MODFLOW local solves import ``drive_live_solve_progress`` from here.

NATE 2026-06-17 (LIVE big-sim telemetry): the long local solves (SWMM, MODFLOW)
run off-loop in ``asyncio.to_thread`` and emit nothing for minutes, so the
running tool/pipeline card shows a silent spinner. This driver runs as a side
task **on the event loop** (the emitter is loop-bound) alongside the off-loop
solve, ticking grid/cells/vCPU/elapsed/ETA every
``_LIVE_SOLVE_PROGRESS_INTERVAL_S`` seconds. The caller launches it via
``asyncio.ensure_future`` BEFORE the ``to_thread`` solve and cancels + awaits it
in a ``finally`` (success, failure, OR cancel).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.solve_progress")


#: Cadence (seconds) for the LIVE solve-progress envelope during the long solve.
#: Independent of the solver poll cadence — this is a UX tick on the running
#: card; conservative so a 10-20-min solve emits a steady (not chatty) stream.
_LIVE_SOLVE_PROGRESS_INTERVAL_S = 10.0


async def drive_live_solve_progress(
    *,
    emitter: Any,
    run_id: str,
    solver: str,
    grid_resolution_m: float | None,
    active_cell_count: int | None,
    vcpus: int | None,
    eta_seconds: float | None,
) -> None:
    """Background loop: emit the LIVE solve-progress envelope every N seconds.

    Runs alongside the off-loop solve so the running tool/pipeline card shows
    grid/cells/vCPU/elapsed/ETA ticking during the long solve (rather than a
    silent multi-minute spinner). ``elapsed_seconds`` is wall-clock from this
    coroutine's start (Invariant 1: never an LLM estimate); ``eta_seconds`` is
    the perf-model ``estimated_solve_seconds`` when available, else ``None``.

    Best-effort + cancellation-safe: the caller cancels this task when the solve
    returns; any emit failure is swallowed (live telemetry is a UX hint, never a
    correctness gate). No-op when ``emitter`` is ``None`` (direct/smoke/test
    call without a WS emitter)."""
    if emitter is None:
        return
    from ..telemetry import build_live_solve_progress

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        while True:
            elapsed = max(0.0, loop.time() - started)
            payload = build_live_solve_progress(
                run_id=run_id,
                solver=solver,
                grid_resolution_m=grid_resolution_m,
                active_cell_count=active_cell_count,
                vcpus=vcpus,
                elapsed_seconds=elapsed,
                eta_seconds=eta_seconds,
            )
            try:
                await emitter.emit_solve_progress(payload)
            except Exception as exc:  # noqa: BLE001 — UX hint, never fatal
                logger.debug(
                    "solve_progress: live solve-progress emit failed "
                    "(non-fatal): %s",
                    exc,
                )
            await asyncio.sleep(_LIVE_SOLVE_PROGRESS_INTERVAL_S)
    except asyncio.CancelledError:
        # Normal teardown when the solve completes — re-raise so the task
        # finalizes cleanly.
        raise


__all__ = [
    "drive_live_solve_progress",
    "_LIVE_SOLVE_PROGRESS_INTERVAL_S",
]
