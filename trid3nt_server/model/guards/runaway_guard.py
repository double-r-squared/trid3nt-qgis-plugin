"""Runaway-agent guard: per-turn step cap, wall-clock and loop watchdog.

Three env-overridable bounds OR'd into one abort. A cheap, loop-prone model tier gets
HALF the step cap, floored so a legitimate short chain still fits.
"""
from __future__ import annotations

import math
import os

# The three guards these defaults arm:
#   1. STEP CAP -- a hard cap on model<->tool ROUNDS within one user turn; the
#      primary bound.
#   2. WALL-CLOCK -- a per-turn deadline, so a turn whose rounds are individually
#      slow aborts even while it is under the step cap.
#   3. LOOP WATCHDOG -- the SAME tool with the SAME args, or the SAME round
#      signature, repeated N rounds running. This catches the runaway that stays
#      UNDER the step cap by re-issuing one identical call.
# Chosen to leave NORMAL turns untouched: the turn driver takes the min of its
# own iteration bound and this step cap, and that bound is the binding one for
# full-tier models, so the default step cap only bites a genuinely runaway turn
# -- while the cheap-model halving does tighten the loop-prone tier.

#: Hard cap on model<->tool ROUNDS within a single user turn (full-tier models).
MAX_AGENT_STEPS_DEFAULT: int = 30

#: Per-turn wall-clock budget (seconds). Aborts a turn running too long even if
#: it is still under the step cap.
MAX_TURN_SECONDS_DEFAULT: float = 420.0

#: Number of identical-in-a-row tool calls (or identical round signatures) that
#: trips the loop watchdog.
LOOP_REPEAT_N_DEFAULT: int = 4

#: Floor for the cheap-model halved cap -- never tighten below this so a
#: legitimate short chain (discover -> fetch -> publish -> narrate) still fits.
_CHEAP_STEP_FLOOR: int = 6

#: Substrings (lowercased) that mark a small / cheap / loop-prone model tier,
#: matched against the model id so a new cheap profile needs no code edit.
_CHEAP_MODEL_SUBSTRINGS: tuple[str, ...] = (
    "nova",
    "haiku",
    "mini",
    "small",
    "flash",
    "deepseek",
    "lite",
)

# Abort reason codes surfaced honestly to the user (honesty floor). Distinct
# codes so the UI / telemetry can tell WHY a turn was force-stopped.
ABORT_STEP_CAP = "AGENT_STEP_LIMIT_REACHED"
ABORT_WALL_CLOCK = "AGENT_TURN_TIMEOUT"
ABORT_LOOP_WATCHDOG = "AGENT_LOOP_DETECTED"

# Human-readable abort messages (the user sees these). Kept short + honest.
_ABORT_MESSAGES: dict[str, str] = {
    ABORT_STEP_CAP: (
        "Agent step limit reached - stopping to protect the session. "
        "Try rephrasing your request with a narrower scope."
    ),
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


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Parse a positive int env override, falling back safely on garbage."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
    """Parse a positive float env override, falling back safely on garbage."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def max_agent_steps() -> int:
    """Full-tier per-turn step cap (``TRID3NT_MAX_AGENT_STEPS``)."""
    return _env_int("TRID3NT_MAX_AGENT_STEPS", MAX_AGENT_STEPS_DEFAULT)


def max_turn_seconds() -> float:
    """Per-turn wall-clock budget (``TRID3NT_MAX_TURN_SECONDS``)."""
    return _env_float("TRID3NT_MAX_TURN_SECONDS", MAX_TURN_SECONDS_DEFAULT)


def loop_repeat_n() -> int:
    """Identical-call-in-a-row threshold (``TRID3NT_LOOP_REPEAT_N``)."""
    # Floor at 2 -- a single repeat is NEVER a loop (a retry-after-error is
    # legitimate); 2-in-a-row is the smallest meaningful "no progress" signal.
    return _env_int("TRID3NT_LOOP_REPEAT_N", LOOP_REPEAT_N_DEFAULT, minimum=2)


def is_cheap_model(model_id: str | None) -> bool:
    """True when ``model_id`` is a small / cheap / loop-prone tier.

    ``None`` is NOT cheap: the env default model is a full-tier one."""
    if not model_id:
        return False
    mid = model_id.lower()
    return any(sub in mid for sub in _CHEAP_MODEL_SUBSTRINGS)


def step_cap_for_model(model_id: str | None) -> int:
    """Resolve the per-turn step cap for ``model_id``.

    A cheap tier gets HALF, rounded up, floored at ``_CHEAP_STEP_FLOOR``."""
    full = max_agent_steps()
    if not is_cheap_model(model_id):
        return full
    halved = math.ceil(full / 2)
    return max(_CHEAP_STEP_FLOOR, min(full, halved))


class LoopWatchdog:
    """Detect a turn looping with no progress, fed one ROUND at a time.

    Trips only on a repeated round signature that also made no progress; O(1)
    state, cheap to call every round."""

    def __init__(self, threshold: int | None = None) -> None:
        self._threshold = threshold if threshold is not None else loop_repeat_n()
        self._last_signature: tuple[tuple[str, str], ...] | None = None
        self._repeat_count: int = 0

    def record_round(
        self, calls: list[tuple[str, str]], *, made_progress: bool = False
    ) -> str | None:
        """Record one round's ``(tool_name, args_hash)`` calls.

        Returns the abort reason code when this round trips, else ``None``."""
        # A round resets the no-progress streak when it emitted no calls (a
        # text-only / terminal round: narration is progress), and when it MADE
        # PROGRESS. ``made_progress`` is True when at least one call produced a
        # real artifact -- a model producing NEW output each round is advancing
        # the Case, so it runs on to the step cap rather than being
        # watchdog-aborted -- and ALSO when every call this round failed or was
        # short-circuited, because the circuit breaker already owns the
        # failing-tool case and the watchdog must not pre-empt it. So the streak
        # counts only a round that had calls, repeated the prior signature, and
        # made no progress: the re-issued successful no-op call.
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
