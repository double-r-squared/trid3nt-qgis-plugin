"""Runaway-agent guard: per-turn step cap + wall-clock + loop watchdog.

SAFETY-CRITICAL (live incident 2026-06-25): a single prompt on Nova Lite ran
away inside the per-turn model<->tool loop -- it kept emitting tool calls that
made no progress, never terminated, pinned the shared EC2 box, wedged the SSM
channel, and locked the user out behind a "connecting" loop. Only an
``ec2 stop-instances`` killed it. It must never be able to wedge the shared box
again.

This module holds the small, env-overridable, fully unit-testable guard logic
that the per-turn driver (``server._stream_gemini_reply``) consults each
iteration. Keeping it here -- rather than inline in the 10k-line server -- makes
the thresholds + watchdog testable in isolation and keeps normal-turn behavior
untouched under the caps.

Three independent guards, all OR'd into a single per-turn abort:

  1. STEP CAP -- a hard cap on model<->tool ROUNDS within one user turn
     (``GRACE2_MAX_AGENT_STEPS``, default 30). This is the primary bound. It is
     a TIGHTER lower bound than the historical ``MAX_TURN_ITERATIONS`` only for
     cheap/loop-prone models (see #3); the server takes ``min`` of the two so a
     normal Sonnet turn keeps its existing headroom.

  2. WALL-CLOCK -- a per-turn deadline (``GRACE2_MAX_TURN_SECONDS``, default
     420s) that aborts a turn that is taking too long even if it is still under
     the step cap (e.g. each round is slow). Bounds wall time, not just steps.

  3. LOOP WATCHDOG -- detects no-progress looping: the SAME tool called with the
     SAME args ``GRACE2_LOOP_REPEAT_N`` (default 4) times in a row, or the model
     emitting the SAME identical round-signature that many times running. Either
     fires an abort. This catches the runaway that stays *under* the step cap by
     re-issuing one identical call (Nova Lite's failure shape).

CHEAP-MODEL TIGHTER CAP (#3): small / Nova / Haiku-tier models are materially
more loop-prone, so they get HALF the step cap (rounded up, floored at a small
minimum). Data-driven via ``_CHEAP_MODEL_SUBSTRINGS`` -- matched on the model id
substring so future cheap profiles are covered without an edit, rather than a
brittle exact-id table.
"""

from __future__ import annotations

import math
import os

# --------------------------------------------------------------------------- #
# Defaults (all env-overridable). Chosen to leave NORMAL turns untouched: the
# server takes min(MAX_TURN_ITERATIONS, step cap), and MAX_TURN_ITERATIONS (12)
# is the binding bound for full-tier models, so the default step cap of 30 only
# bites a genuinely runaway turn -- while the cheap-model halving DOES tighten
# Nova/Haiku, exactly the loop-prone tier from the incident.
# --------------------------------------------------------------------------- #

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

#: Substrings (lowercased) that mark a small / cheap / loop-prone Bedrock model
#: tier. Matched against the model id; data-driven so new cheap profiles are
#: covered without a code edit. Nova (lite/pro/micro) and Haiku are the live
#: cheap tier; "mini"/"small"/"flash"/"deepseek" cover plausible future adds.
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
    """Full-tier per-turn step cap (``GRACE2_MAX_AGENT_STEPS``)."""
    return _env_int("GRACE2_MAX_AGENT_STEPS", MAX_AGENT_STEPS_DEFAULT)


def max_turn_seconds() -> float:
    """Per-turn wall-clock budget (``GRACE2_MAX_TURN_SECONDS``)."""
    return _env_float("GRACE2_MAX_TURN_SECONDS", MAX_TURN_SECONDS_DEFAULT)


def loop_repeat_n() -> int:
    """Identical-call-in-a-row threshold (``GRACE2_LOOP_REPEAT_N``)."""
    # Floor at 2 -- a single repeat is NEVER a loop (a retry-after-error is
    # legitimate); 2-in-a-row is the smallest meaningful "no progress" signal.
    return _env_int("GRACE2_LOOP_REPEAT_N", LOOP_REPEAT_N_DEFAULT, minimum=2)


def is_cheap_model(model_id: str | None) -> bool:
    """True when ``model_id`` is a small / cheap / loop-prone tier (Nova/Haiku).

    Matched on lowercased substring so future cheap profiles are covered without
    an edit. ``None`` (the env default model) is treated as NOT cheap -- the
    default is Sonnet, a full-tier model.
    """
    if not model_id:
        return False
    mid = model_id.lower()
    return any(sub in mid for sub in _CHEAP_MODEL_SUBSTRINGS)


def step_cap_for_model(model_id: str | None) -> int:
    """Resolve the per-turn step cap for ``model_id``.

    Full-tier models get the full ``max_agent_steps()``. Cheap / loop-prone
    models (Nova, Haiku, ...) get HALF that (rounded up), floored at
    ``_CHEAP_STEP_FLOOR`` so a legitimate short chain still fits. This is the
    #3 "tighter cap for cheap models" guard, kept data-driven.
    """
    full = max_agent_steps()
    if not is_cheap_model(model_id):
        return full
    halved = math.ceil(full / 2)
    return max(_CHEAP_STEP_FLOOR, min(full, halved))


class LoopWatchdog:
    """Detect a turn looping with no progress (guard #2).

    Fed one ROUND at a time via :meth:`record_round`, each round being the list
    of ``(tool_name, args_hash)`` the model emitted that round. Trips when:

      * the SAME single (tool, args_hash) is emitted ``loop_repeat_n()`` rounds
        in a row (the classic "calls the same tool with the same args over and
        over" runaway), OR
      * the model emits the SAME identical round-signature (same set/order of
        calls, e.g. a repeated fan-out) ``loop_repeat_n()`` rounds in a row.

    A round that differs from the prior round RESETS the streak (real progress).
    Text-only rounds (no tool calls) also reset -- the model is narrating, which
    is progress toward a terminal turn. Cheap to call; O(1) state.

    PROGRESS-AWARE RESET (job-186 reconciliation): a round that MADE PROGRESS
    also resets the streak even when its signature repeats. ``made_progress`` is
    the per-round witness the driver passes after dispatch:

      * ``True`` when at least one call produced a real artifact (a published /
        registered layer, a substantive result) -- a model that keeps producing
        NEW output each round is advancing the Case, not wedging the box, so it
        is allowed to run to the step cap / loop-exhausted envelope rather than
        being watchdog-aborted. This is what separates an identical-but-PRODUCING
        loop (-> ``MAX_ITERATIONS_REACHED`` at the cap) from the wedge shape.
      * ``True`` ALSO when every call this round FAILED or was circuit-breaker
        short-circuited -- the CIRCUIT BREAKER owns the failing-tool case (it
        delivers ``CIRCUIT_BREAKER_TRIPPED`` so the model adapts, the turn
        continues gracefully). The watchdog must NOT pre-empt a turn the breaker
        is already handling, so a failure/short-circuit round does not load the
        no-progress streak.

    The watchdog therefore only counts a round toward its no-progress streak when
    that round had calls, repeated the prior signature, AND made no progress
    (``made_progress=False``): the genuine "ignored the result, re-issued the
    same successful no-op call" runaway (Nova Lite's failure shape). Cheap to
    call; O(1) state.

    :meth:`tripped` returns the reason code (``ABORT_LOOP_WATCHDOG``) once the
    streak hits the threshold, else ``None``.
    """

    def __init__(self, threshold: int | None = None) -> None:
        self._threshold = threshold if threshold is not None else loop_repeat_n()
        self._last_signature: tuple[tuple[str, str], ...] | None = None
        self._repeat_count: int = 0

    def record_round(
        self, calls: list[tuple[str, str]], *, made_progress: bool = False
    ) -> str | None:
        """Record one round's ``(tool_name, args_hash)`` calls.

        Returns the abort reason code if this round trips the watchdog, else
        ``None``. ``calls`` empty (a text-only / terminal round) resets the
        streak -- narration is progress. ``made_progress=True`` ALSO resets the
        streak (a producing round, or a round the circuit breaker owns), so the
        watchdog only catches a repeated, SUCCESSFUL-but-NO-OP call.
        """
        if not calls or made_progress:
            # Text-only round, a producing round, or a breaker-owned (all-failed
            # / short-circuited) round -> progress -> reset the no-progress
            # streak. ``_last_signature`` is cleared so the NEXT repeat starts a
            # fresh count rather than resuming an interrupted streak.
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
