"""Per-session tool circuit breaker over consecutive tool failures.

Only UPSTREAM/transient faults count toward a trip: a client/arg fault fails identically
every time, so counting one would block the corrected retry.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger("trid3nt_server.gates.circuit_breaker")

# ---------------------------------------------------------------------------
# Environment-overridable defaults
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD = 3
_DEFAULT_COOLDOWN_S = 60.0


def _get_threshold() -> int:
    """Consecutive-failure threshold before the breaker trips.

    ``TRID3NT_CIRCUIT_THRESHOLD`` overrides; anything below 1 falls back to 3."""
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

    ``TRID3NT_CIRCUIT_COOLDOWN_S`` overrides; a negative value falls back to 60."""
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
# Typed exception
# ---------------------------------------------------------------------------


class CircuitBreakerError(RuntimeError):
    """Raised when ``ToolCircuitBreaker.is_tripped`` is True for a tool.

    ``retryable=False`` -- no retry escapes a cooldown, so the model narrates the
    temporary unavailability instead of calling again."""

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

    A client/arg error never counts toward the trip threshold; ``None`` is not
    one, and anything unrecognized is read as upstream and does count."""
    if error is None:
        return False
    # 1. A typed tool exception's own retry signal is authoritative: every
    #    *ArgError / BboxInvalidError declares retryable=False, and every
    #    *UpstreamError declares retryable=True.
    retry_attr = getattr(error, "retryable", None)
    if isinstance(retry_attr, bool):
        return retry_attr is False
    # 2. Untyped programmer/arg-shape errors are client errors.
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return True
    # 3. Everything else -- timeouts, ConnectionError/OSError, a bare
    #    RuntimeError -- is read as upstream/transient and counts, so a genuine
    #    repeated-upstream-failure loop still trips the breaker.
    return False


def _is_operator_class_error(tool_name: str, error: BaseException) -> bool:
    """True when ``error`` is a contract violation, not a repeated tool fault."""
    # The import is function-local: the classifier reaches into the credential
    # registry, and this module must carry no import-time edge to it.
    from .actionability import classify_actionability

    return classify_actionability(tool_name, error) == "operator"


# ---------------------------------------------------------------------------
# Per-session breaker state
# ---------------------------------------------------------------------------


@dataclass
class ToolCircuitBreaker:
    """Per-session breaker tracking consecutive failures per tool.

    Threshold and cooldown are read once at construction, never mid-flight."""

    threshold: int = field(default_factory=_get_threshold)
    cooldown_s: float = field(default_factory=_get_cooldown_s)
    # Internal state: consecutive failure counters and cooldown deadlines.
    _consecutive_failures: dict[str, int] = field(default_factory=dict, repr=False)
    _cooldown_until: dict[str, float] = field(default_factory=dict, repr=False)

    def is_tripped(self, tool_name: str) -> bool:
        """True while the breaker is open (cooling down) for this tool.

        Resets the failure counter once the cooldown window has elapsed."""
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
        """Seconds left in the cooldown; 0.0 when the tool is not tripped."""
        deadline = self._cooldown_until.get(tool_name)
        if deadline is None:
            return 0.0
        remaining = deadline - time.monotonic()
        return max(0.0, remaining)

    def record_failure(
        self, tool_name: str, error: BaseException | None = None
    ) -> None:
        """Increment the consecutive-failure counter; trip it at the threshold.

        Client/arg and operator-class faults are SKIPPED; an unclassifiable
        failure (``error=None``) counts, the conservative default."""
        if is_client_arg_error(error):
            # Model-side / deterministic arg fault: the counter is left alone so
            # a corrected-args retry is never blocked by the cooldown.
            logger.debug(
                "circuit-breaker: tool=%r failure is a client/arg error "
                "(%s); NOT counting toward trip threshold",
                tool_name,
                type(error).__name__ if error is not None else "None",
            )
            return
        if error is not None and _is_operator_class_error(tool_name, error):
            # Contract violation / internal exception: OUR bug, not the tool's
            # repeated fault, so it must not consume the tool's retry budget.
            logger.debug(
                "circuit-breaker: tool=%r failure is operator-class "
                "(contract violation/internal, %s); NOT counting toward "
                "trip threshold",
                tool_name,
                type(error).__name__,
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
        """Reset the consecutive-failure counter for this tool after a success."""
        had_failures = self._consecutive_failures.pop(tool_name, 0)
        # A success cannot arrive while the breaker is open (is_tripped would
        # have short-circuited the call), so clearing the cooldown is defensive.
        self._cooldown_until.pop(tool_name, None)
        if had_failures:
            logger.info(
                "circuit-breaker: tool=%r SUCCESS after %d prior failure(s); counter reset",
                tool_name,
                had_failures,
            )
