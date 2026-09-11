"""Shared LIVE solve-progress heartbeat.

A long solve runs off-loop and emits nothing for minutes, so this runs ON the loop
where the emitter is bound: launched BEFORE the solve, cancelled in a ``finally``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.solver.solve_progress")


#: Cadence (seconds) for the LIVE solve-progress envelope during the long solve.
#: Independent of the solver poll cadence - a UX tick on the running card, kept
#: conservative so a 10-20-min solve emits a steady rather than chatty stream.
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
    ``elapsed_seconds`` is wall-clock, never an estimate; best-effort and
    cancellation-safe, and ``emitter=None`` is a no-op."""
    if emitter is None:
        return
    from trid3nt_server.telemetry import build_live_solve_progress

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
            except Exception as exc:  # noqa: BLE001 -- UX hint, never fatal
                logger.debug(
                    "solve_progress: live solve-progress emit failed "
                    "(non-fatal): %s",
                    exc,
                )
            await asyncio.sleep(_LIVE_SOLVE_PROGRESS_INTERVAL_S)
    except asyncio.CancelledError:
        # Normal teardown when the solve completes - re-raise so the task
        # finalizes cleanly.
        raise


__all__ = [
    "drive_live_solve_progress",
    "_LIVE_SOLVE_PROGRESS_INTERVAL_S",
]
