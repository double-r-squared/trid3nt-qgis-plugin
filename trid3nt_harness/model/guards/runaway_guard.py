"""Runaway-agent guard: the abort codes a turn stops on, and the loop watchdog.

A turn's step cap and wall clock are the MAX_TURN_STEPS and MAX_TURN_SECONDS
settings, read where the turn starts; the watchdog's repeat count is this
module's own constant.
"""
from __future__ import annotations

#: Identical-in-a-row tool calls (or identical round signatures) that trip the watchdog. Never
#: under 2: one repeat is a legitimate retry after an error.
LOOP_REPEAT_N: int = 4

# Abort reason codes surfaced honestly to the user; distinct so UI and telemetry can tell WHY.
ABORT_WALL_CLOCK = "AGENT_TURN_TIMEOUT"
ABORT_LOOP_WATCHDOG = "AGENT_LOOP_DETECTED"

# Human-readable abort messages (the user sees these). Kept short + honest.
_ABORT_MESSAGES: dict[str, str] = {
    ABORT_WALL_CLOCK: (
        "Agent turn time limit reached - stopping to protect the session. "
        "Try a narrower request or run heavy work as a separate step."
    ),
    ABORT_LOOP_WATCHDOG: (
        "Agent appears to be looping with no progress - stopping to protect "
        "the session. Try rephrasing your request."
    ),
}


def abort_message(reason_code: str) -> str:
    """Honest, user-facing sentence for an abort reason code."""
    return _ABORT_MESSAGES.get(
        reason_code,
        "Agent stopped to protect the session.",
    )


class LoopWatchdog:
    """Detect a turn looping with no progress, fed one ROUND at a time.

    Trips only on a repeated round signature that also made no progress; O(1)
    state, cheap to call every round."""

    def __init__(self, threshold: int | None = None) -> None:
        self._threshold = threshold if threshold is not None else LOOP_REPEAT_N
        self._last_signature: tuple[tuple[str, str], ...] | None = None
        self._repeat_count: int = 0

    def record_round(
        self, calls: list[tuple[str, str]], *, made_progress: bool = False
    ) -> str | None:
        """Record one round's ``(tool_name, args_hash)`` calls; returns the abort code when this
        round trips, else ``None``."""
        # A round resets the streak when it had no calls (narration is progress) or made progress.
        # ``made_progress`` is True when a call produced a real artifact, and also when every call
        # failed or was short-circuited: the circuit breaker owns that case and the watchdog must
        # not pre-empt it. Only repeated, call-bearing, no-progress rounds count.
        if not calls or made_progress:
            # ``_last_signature`` is cleared so the NEXT repeat starts a fresh
            # count rather than resuming an interrupted streak.
            self._last_signature = None
            self._repeat_count = 0
            return None

        signature = tuple(calls)
        if signature == self._last_signature:
            self._repeat_count += 1
        else:
            self._last_signature = signature
            # First time we see this signature -> count of 1 (this occurrence).
            self._repeat_count = 1

        if self._repeat_count >= self._threshold:
            return ABORT_LOOP_WATCHDOG
        return None

    def tripped(self) -> str | None:
        """Reason code if the streak has reached the threshold, else None."""
        if self._repeat_count >= self._threshold:
            return ABORT_LOOP_WATCHDOG
        return None
