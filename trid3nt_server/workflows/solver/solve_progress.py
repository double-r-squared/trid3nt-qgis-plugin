"""The live cards of a solve: the Dispatch and Sim cards, and the progress
heartbeat on the Sim card.

A long solve runs off-loop and emits nothing for minutes, so the heartbeat runs ON
the loop where the emitter is bound: launched BEFORE the solve, cancelled in a
``finally``. The cards are observability only; a card failure never stops a solve.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_server.render.pipeline_emitter import dispatched_tool_name

logger = logging.getLogger("trid3nt_server.workflows.solver.solve_progress")


#: Cadence (seconds) of the live solve-progress envelope: a UX tick independent of the solver poll, conservative for a 10-20 min solve.
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
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        while True:
            elapsed = max(0.0, loop.time() - started)
            payload = {
                "run_id": run_id,
                "solver": solver,
                "grid_resolution_m": (float(grid_resolution_m)
                                      if grid_resolution_m is not None else None),
                "active_cell_count": (int(active_cell_count)
                                      if active_cell_count is not None else None),
                "vcpus": int(vcpus) if vcpus is not None else None,
                "elapsed_seconds": float(elapsed),
                "eta_seconds": (float(eta_seconds)
                                if eta_seconds is not None else None),
            }
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
        # Normal teardown on completion - re-raise so the task finalizes.
        raise


__all__ = [
    "drive_live_solve_progress",
    "_LIVE_SOLVE_PROGRESS_INTERVAL_S",
]


# Every solver dispatch mints a Dispatch card (the submit, complete at once) and a Sim card bound to the run, fed by the wait-loop poller.


async def mint_dispatch_and_sim_cards(
    *,
    emitter: Any,
    solver: str,
    handle: Any,
    cores: int | None = None,
    module: str | None = None,
) -> str | None:
    """Mint the Dispatch (tool) + Sim (compute) cards for a dispatched solve.
    Returns the SIM step's id, or ``None`` on any failure: the cards are an
    observability affordance and the solve proceeds either way.
    """
    if emitter is None:
        return None
    # Named for the CASE, not the solver, so a family sharing one registered solver id does not label every run after one sibling;
    # outside a dispatch the solver id is the only identity.
    case = dispatched_tool_name() or solver
    job_id = str(getattr(handle, "workflows_execution_id", "") or "")
    backend = str(getattr(handle, "workflow_name", "") or "local-docker")
    try:
        dispatch_label = f"Dispatch {case} solve"
        if cores:
            plural = "" if int(cores) == 1 else "s"
            dispatch_label = f"{dispatch_label} ({int(cores)} core{plural})"
        dispatch_id = await emitter.add_step(
            name=dispatch_label, tool_name=f"{case}:dispatch"
        )
        await emitter.mark_running(dispatch_id)
        await emitter.mark_complete(dispatch_id)
        # Persist the terminal Dispatch card so the pair replays on a Case reopen.
        await emitter.persist_terminal_dispatch_card(dispatch_id)
        sim_id = await emitter.add_compute_step(
            name=f"{case} solve",
            tool_name=f"{case}:solve",
            batch_job_id=job_id,
            batch_status="SUBMITTED",
            engine=solver,
            module=module,
        )
        # Persist the SIM card still running so a reconnect mid-solve replays it; the row is upserted when the solve finishes.
        await emitter.persist_running_compute_card(sim_id)
        logger.info(
            "two-card sim observability: minted dispatch + compute cards "
            "case=%s solver=%s backend=%s jobId=%s sim_step_id=%s",
            case,
            solver,
            backend,
            job_id,
            sim_id,
        )
        return sim_id
    except Exception as exc:  # noqa: BLE001 -- observability, never break the solve
        logger.warning("mint_dispatch_and_sim_cards failed (non-fatal): %s", exc)
        return None


async def route_sim_terminal(
    emitter: Any,
    sim_step_id: str | None,
    *,
    run_result: Any,
) -> None:
    """Drive the SIM compute card to its terminal state.
    A ``None`` ``run_result`` is a cancel. Every branch UPSERTS the row written
    running at mint, and an emit failure is swallowed rather than raised.
    """
    if emitter is None or not sim_step_id:
        return
    try:
        status = str(getattr(run_result, "status", "") or "") if run_result is not None else ""
        if run_result is None or status == "cancelled":
            await emitter.mark_cancelled(sim_step_id)
            # A cancel upserts the row written running at mint, leaving no orphan.
            await emitter.persist_terminal_compute_card(sim_step_id)
        elif status == "complete":
            await emitter.mark_complete(sim_step_id)
            # Persisted like a plain tool card so it replays on reconnect or reopen.
            await emitter.persist_terminal_compute_card(sim_step_id)
        else:
            error_code = (
                getattr(run_result, "error_code", None) or (status.upper() if status else "SOLVER_FAILED")
            )
            error_message = (
                getattr(run_result, "error_message", None)
                or getattr(run_result, "cancellation_reason", None)
                or f"solver run {status or 'failed'}"
            )
            await emitter.mark_failed(
                sim_step_id, error_code=str(error_code), error_message=str(error_message)
            )
            # The red card persists too: a terminal failure must survive a socket cycle.
            await emitter.persist_terminal_compute_card(sim_step_id)
    except Exception as exc:  # noqa: BLE001 -- observability, never break the solve
        logger.warning("route_sim_terminal failed (non-fatal): %s", exc)
