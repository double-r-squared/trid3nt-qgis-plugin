"""A stopped solve: the user's stop as it travels up from the wait to the workflow that publishes what the run wrote.

Both exceptions ARE cancellations, so everything that handles a cancel still sees one.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable

__all__ = ["SolveStopped", "Stopped", "finish"]


class SolveStopped(asyncio.CancelledError):
    """The user stopped the wait on ``run_id`` and the container is down. ``at`` is the engine's last stated
    step (``None`` before it stated one) and ``started_at`` when the solve began, as the run's completion states it; ``solved`` is the run handle of what it wrote, which each layer
    up the stack completes in the shape its normal handle has."""

    def __init__(self, run_id: str, at: str | None, started_at: str | None = None) -> None:
        super().__init__()
        self.run_id = run_id
        self.at = at
        self.started_at = started_at
        self.solved: dict[str, Any] = {}

    def sentence(self) -> str:
        """How every partial product and the run's record say what stopped it."""
        return f"stopped by the user at {self.at}" if self.at else "stopped by the user before the engine stated a step"


class Stopped(asyncio.CancelledError):
    """A launch ended by the user's stop, after publishing what the run wrote: ``run`` carries the launch's collected record,
    ``value`` what the partial publish returned (``None`` when it published nothing), ``why`` the stop's sentence."""

    def __init__(self, run: Any, value: Any, why: str) -> None:
        super().__init__()
        self.run = run
        self.value = value
        self.why = why


async def finish(work: Awaitable[Any]) -> Any:
    """Await ``work`` to its end though the surrounding task is cancelled again meanwhile; the cancel it absorbed is raised once the work is done."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
