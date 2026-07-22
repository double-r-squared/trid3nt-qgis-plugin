"""Per-session tool circuit breaker (job-B8, Wave 4.10 Stage 3).

Defends the multi-turn agent loop against unstable upstreams (STAC, ERDDAP,
THREDDS, ArcGIS Hub, etc.) that enter a repeated-failure mode.  When a single
tool fails 3 consecutive times **on a transient/upstream fault** (configurable
via ``TRID3NT_CIRCUIT_THRESHOLD``) the breaker trips: subsequent calls to that
tool are short-circuited for a 60s cooldown period (configurable via
``TRID3NT_CIRCUIT_COOLDOWN_S``).

CRITICAL (job 2026-06-17 — Oklahoma-tornado bug): the breaker counts ONLY
upstream/transient failures (``UpstreamAPIError``, timeouts, connection
errors, 5xx) toward the trip threshold.  Deterministic CLIENT/argument errors
(the ``*ArgError`` classes, ``BboxInvalidError``, ``ValueError``/``TypeError``
arg-shape errors — anything carrying ``retryable=False`` that is a model-side
fault) do NOT increment the counter.  A bad-arg failure is the model's fault:
it will fail identically every time, AND the model can self-correct the
argument and retry — so it must NEVER trip a breaker that would then BLOCK the
corrected retry.  Before this fix, ~24 parallel per-year storm-events calls
with a full state name ("Oklahoma") tripped the breaker in 3 calls and the
60s cooldown blocked the corrected-args retry.

Wire points (server.py):
    - ``SessionState.circuit_breaker: ToolCircuitBreaker``
    - Before ``_invoke_tool_via_emitter``:
        if state.circuit_breaker.is_tripped(call.name):
            raise CircuitBreakerError(...)
    - After successful ``_invoke_tool_via_emitter``:
        state.circuit_breaker.record_success(call.name)
    - In the exception handler after ``_invoke_tool_via_emitter`` fails:
        state.circuit_breaker.record_failure(call.name)

``CircuitBreakerError`` follows the same FR-AS-11 typed-exception contract as
``ToolNotFoundError`` and every ``fetch_*`` error class: ``error_code`` is a
SCREAMING_SNAKE_CASE string, ``retryable=False`` (the LLM cannot retry its way
out of a cooldown).  ``summarize_tool_result`` in ``adapter.py`` harvests these
attributes and emits the full Wave 4.9 structured envelope so Gemini reads the
signal and narrates the cooldown honestly.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger("trid3nt_server.circuit_breaker")

# ---------------------------------------------------------------------------
# Environment-overridable defaults
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD = 3
_DEFAULT_COOLDOWN_S = 60.0


def _get_threshold() -> int:
    """Consecutive-failure threshold before the breaker trips.

    Override via ``TRID3NT_CIRCUIT_THRESHOLD`` (integer).  Falls back to 3.
    """
    raw = os.environ.get("TRID3NT_CIRCUIT_THRESHOLD")
    if raw is None:
        return _DEFAULT_THRESHOLD
    try:
        val = int(raw)
        if val < 1:
            raise ValueError("must be >= 1")
        return val
    except (ValueError, TypeError):
        logger.warning(
            "TRID3NT_CIRCUIT_THRESHOLD=%r is not a valid positive integer; "
            "using default %d",
            raw,
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD


def _get_cooldown_s() -> float:
    """Cooldown duration in seconds after the breaker trips.

    Override via ``TRID3NT_CIRCUIT_COOLDOWN_S`` (float).  Falls back to 60.0.
    """
    raw = os.environ.get("TRID3NT_CIRCUIT_COOLDOWN_S")
    if raw is None:
        return _DEFAULT_COOLDOWN_S
    try:
        val = float(raw)
        if val < 0:
            raise ValueError("must be >= 0")
        return val
    except (ValueError, TypeError):
        logger.warning(
            "TRID3NT_CIRCUIT_COOLDOWN_S=%r is not a valid float; "
            "using default %.1f",
            raw,
            _DEFAULT_COOLDOWN_S,
        )
        return _DEFAULT_COOLDOWN_S


# ---------------------------------------------------------------------------
# Typed exception (FR-AS-11 contract)
# ---------------------------------------------------------------------------


class CircuitBreakerError(RuntimeError):
    """Raised when ``ToolCircuitBreaker.is_tripped`` is True for a tool.

    ``retryable=False``: the LLM cannot retry its way out of a cooldown.  It
    should narrate honestly that the tool is temporarily unavailable and
    suggest the user try again after the cooldown expires.

    ``error_code="CIRCUIT_BREAKER_TRIPPED"`` follows the Wave 4.9
    SCREAMING_SNAKE_CASE convention; ``summarize_tool_result`` harvests it.
    """

    error_code: str = "CIRCUIT_BREAKER_TRIPPED"
    retryable: bool = False

    def __init__(self, tool_name: str, cooldown_remaining_s: float) -> None:
        self.tool_name = tool_name
        self.cooldown_remaining_s = cooldown_remaining_s
        super().__init__(
            f"tool {tool_name!r} circuit breaker tripped; "
            f"cooldown remaining: {cooldown_remaining_s:.0f}s. "
            "The tool has failed repeatedly and is temporarily disabled. "
            "Please try again later."
        )


# ---------------------------------------------------------------------------
# Failure classification — only upstream/transient faults count toward a trip.
# ---------------------------------------------------------------------------


def is_client_arg_error(error: BaseException | None) -> bool:
    """Return True if ``error`` is a deterministic CLIENT/argument error.

    A client/arg error is a model-side fault: the same args will fail
    identically every time, and the model can self-correct and retry.  Such
    errors must NOT count toward the circuit-breaker trip threshold — otherwise
    a burst of bad-arg calls would trip the breaker and the cooldown would then
    block the corrected-args retry (the Oklahoma-tornado bug, 2026-06-17).

    Conversely, UPSTREAM/transient faults (timeouts, connection errors, 5xx,
    ``UpstreamAPIError``) ARE the breaker's purpose and DO count.

    Classification (mirrors ``adapter._classify_error`` so the breaker and the
    function_response carry the same retry signal):

    1. Honour the typed-tool exception's ``retryable`` class attribute when
       present: ``retryable is False`` → client/arg error (skip the counter);
       ``retryable is True`` → upstream/transient (count it).  Every
       ``*ArgError`` / ``BboxInvalidError`` declares ``retryable=False``;
       every ``*UpstreamError`` declares ``retryable=True``.
    2. Untyped exceptions: ``ValueError`` / ``TypeError`` / ``KeyError`` /
       ``AttributeError`` are programmer/arg-shape errors → client/arg.
    3. Everything else (timeouts, ``ConnectionError``/``OSError``, bare
       ``RuntimeError``) → treat as upstream/transient (count it) so a genuine
       repeated-upstream-failure loop still trips the breaker.

    ``None`` (no error) is not a client error — returns False.
    """
    if error is None:
        return False
    # 1. Typed-tool retry signal is authoritative.
    retry_attr = getattr(error, "retryable", None)
    if isinstance(retry_attr, bool):
        return retry_attr is False
    # 2. Untyped programmer/arg-shape errors are client errors.
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return True
    # 3. Default: transient/upstream — counts toward the trip threshold.
    return False


# ---------------------------------------------------------------------------
# Per-session breaker state
# ---------------------------------------------------------------------------


@dataclass
class ToolCircuitBreaker:
    """Per-session circuit breaker that tracks consecutive failures per tool.

    Lifecycle (per tool_name):
        CLOSED (normal) → consecutive_failures increments on each failure.
        OPEN (tripped)  → ``cooldown_until`` is set; ``is_tripped`` returns
                          True until ``time.monotonic() >= cooldown_until``.
        AUTO-CLOSE      → after the cooldown window elapses, the next call
                          to ``is_tripped`` returns False and the counter resets;
                          the tool is tried again (HALF-OPEN concept collapsed
                          into CLOSED since we don't need a probe attempt here).

    The threshold and cooldown are read once at construction time so a running
    session is not affected by env changes mid-flight.
    """

    threshold: int = field(default_factory=_get_threshold)
    cooldown_s: float = field(default_factory=_get_cooldown_s)
    # Internal state: consecutive failure counters and cooldown deadlines.
    _consecutive_failures: dict[str, int] = field(default_factory=dict, repr=False)
    _cooldown_until: dict[str, float] = field(default_factory=dict, repr=False)

    def is_tripped(self, tool_name: str) -> bool:
        """Return True if the breaker is currently open (cooling down) for this tool.

        Automatically resets the failure counter when the cooldown window has
        elapsed so the tool becomes available again without explicit intervention.
        """
        deadline = self._cooldown_until.get(tool_name)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            # Cooldown elapsed — auto-close the breaker.
            self._cooldown_until.pop(tool_name, None)
            self._consecutive_failures.pop(tool_name, None)
            logger.info(
                "circuit-breaker: tool=%r cooldown elapsed; breaker auto-closed",
                tool_name,
            )
            return False
        return True

    def cooldown_remaining_s(self, tool_name: str) -> float:
        """Return seconds remaining in the cooldown for ``tool_name``.

        Returns 0.0 if the tool is not currently tripped.
        """
        deadline = self._cooldown_until.get(tool_name)
        if deadline is None:
            return 0.0
        remaining = deadline - time.monotonic()
        return max(0.0, remaining)

    def record_failure(
        self, tool_name: str, error: BaseException | None = None
    ) -> None:
        """Increment the consecutive-failure counter; trip the breaker if threshold hit.

        Call this after a failure in ``_invoke_tool_via_emitter``, passing the
        exception that was raised so the breaker can classify it.

        Only UPSTREAM/transient faults count toward the trip threshold.  A
        deterministic CLIENT/argument error (``*ArgError``, ``BboxInvalidError``,
        ``ValueError``/``TypeError`` arg-shape errors — anything for which
        ``is_client_arg_error`` returns True) is SKIPPED: it is a model-side
        fault that will fail identically and that the model can self-correct,
        so it must NOT trip a breaker that would then block the corrected-args
        retry (the Oklahoma-tornado bug, 2026-06-17).

        ``error=None`` (legacy call sites / unclassifiable failures) counts as a
        failure — the conservative default preserves the original
        repeated-failure-loop protection.
        """
        if is_client_arg_error(error):
            # Model-side / deterministic arg fault — does NOT count toward a
            # trip.  Log at debug for telemetry but leave the counter alone so a
            # corrected-args retry is never blocked.
            logger.debug(
                "circuit-breaker: tool=%r failure is a client/arg error "
                "(%s); NOT counting toward trip threshold",
                tool_name,
                type(error).__name__ if error is not None else "None",
            )
            return
        if self.is_tripped(tool_name):
            # Already open; don't double-reset the clock.
            return
        count = self._consecutive_failures.get(tool_name, 0) + 1
        self._consecutive_failures[tool_name] = count
        logger.debug(
            "circuit-breaker: tool=%r consecutive_failures=%d threshold=%d",
            tool_name,
            count,
            self.threshold,
        )
        if count >= self.threshold:
            deadline = time.monotonic() + self.cooldown_s
            self._cooldown_until[tool_name] = deadline
            logger.warning(
                "circuit-breaker: tool=%r TRIPPED after %d consecutive failures; "
                "cooldown=%.0fs",
                tool_name,
                count,
                self.cooldown_s,
            )

    def record_success(self, tool_name: str) -> None:
        """Reset the consecutive-failure counter for this tool after a success.

        Call this after a successful return from ``_invoke_tool_via_emitter``.
        """
        had_failures = self._consecutive_failures.pop(tool_name, 0)
        # If the tool was previously tripped but auto-closed, this is a no-op.
        # If a success arrives while the breaker is still tripped (impossible
        # under correct wiring since is_tripped would have short-circuited),
        # we clear the cooldown defensively.
        self._cooldown_until.pop(tool_name, None)
        if had_failures:
            logger.info(
                "circuit-breaker: tool=%r SUCCESS after %d prior failure(s); counter reset",
                tool_name,
                had_failures,
            )
