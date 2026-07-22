"""Circuit breaker unit tests (job-B8, Wave 4.10 Stage 3).

Covers:
    1. Default threshold + cooldown values.
    2. Threshold tripping: N failures trip the breaker.
    3. Cooldown expiry: breaker auto-closes after the window elapses.
    4. Success resets the consecutive-failure counter.
    5. Env overrides: TRID3NT_CIRCUIT_THRESHOLD + TRID3NT_CIRCUIT_COOLDOWN_S.
    6. CircuitBreakerError shape: error_code, retryable, message, cooldown_remaining.
    7. record_failure on an already-tripped breaker does not reset the clock.
    8. Multiple independent tools: tripping one does not affect another.
    9. cooldown_remaining_s returns 0 for non-tripped tools.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from trid3nt_server.circuit_breaker import (
    CircuitBreakerError,
    ToolCircuitBreaker,
    is_client_arg_error,
    _DEFAULT_COOLDOWN_S,
    _DEFAULT_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Synthetic upstream / client-arg error classes (mirror real tool exceptions).
# ---------------------------------------------------------------------------


class _FakeUpstreamError(RuntimeError):
    """Transient upstream fault — retryable=True (like *UpstreamError)."""

    error_code = "FAKE_UPSTREAM_ERROR"
    retryable = True


class _FakeArgError(RuntimeError):
    """Deterministic client/arg fault — retryable=False (like *ArgError)."""

    error_code = "FAKE_ARG_INVALID"
    retryable = False


# ---------------------------------------------------------------------------
# Test 1: defaults
# ---------------------------------------------------------------------------


def test_default_threshold_and_cooldown():
    """Breaker uses _DEFAULT_THRESHOLD and _DEFAULT_COOLDOWN_S out of the box."""
    cb = ToolCircuitBreaker()
    assert cb.threshold == _DEFAULT_THRESHOLD
    assert cb.cooldown_s == _DEFAULT_COOLDOWN_S


# ---------------------------------------------------------------------------
# Test 2: threshold tripping
# ---------------------------------------------------------------------------


def test_below_threshold_not_tripped():
    """Fewer failures than threshold → breaker stays closed."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    for _ in range(2):  # 2 < 3
        cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is False


def test_at_threshold_trips_breaker():
    """Exactly threshold failures → breaker opens."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    for _ in range(3):
        cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True


def test_above_threshold_still_tripped():
    """More than threshold failures → breaker remains open."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    for _ in range(5):
        cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True


def test_cooldown_remaining_positive_when_tripped():
    """``cooldown_remaining_s`` returns a positive value while tripped."""
    cb = ToolCircuitBreaker(threshold=1, cooldown_s=60.0)
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True
    remaining = cb.cooldown_remaining_s("fetch_dem")
    assert 0 < remaining <= 60.0


# ---------------------------------------------------------------------------
# Test 3: cooldown expiry → auto-close
# ---------------------------------------------------------------------------


def test_cooldown_expiry_auto_closes_breaker():
    """After the cooldown window elapses, is_tripped returns False."""
    cb = ToolCircuitBreaker(threshold=1, cooldown_s=0.05)  # 50ms cooldown
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True
    # Wait for the cooldown to expire.
    time.sleep(0.1)
    assert cb.is_tripped("fetch_dem") is False


def test_cooldown_expiry_resets_failure_counter():
    """After cooldown, the consecutive-failure counter is also cleared.

    The tool must be triable again with a clean slate — not still holding
    threshold-1 failures from before the cooldown.
    """
    cb = ToolCircuitBreaker(threshold=2, cooldown_s=0.05)
    # Trip the breaker (2 failures).
    cb.record_failure("fetch_dem")
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True

    # Wait for cooldown.
    time.sleep(0.1)
    assert cb.is_tripped("fetch_dem") is False

    # One more failure should NOT re-trip (counter reset to 0, so only 1/2).
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is False


def test_cooldown_remaining_zero_for_non_tripped_tool():
    """``cooldown_remaining_s`` returns 0.0 for a tool that is not tripped."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    assert cb.cooldown_remaining_s("fetch_dem") == 0.0
    # Also for a tool with some failures but below threshold.
    cb.record_failure("fetch_dem")
    assert cb.cooldown_remaining_s("fetch_dem") == 0.0


# ---------------------------------------------------------------------------
# Test 4: success resets the failure counter
# ---------------------------------------------------------------------------


def test_success_resets_failure_counter():
    """A success clears consecutive_failures; subsequent failures restart the count."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    cb.record_failure("fetch_dem")
    cb.record_failure("fetch_dem")
    # Not yet tripped (2 < 3).
    assert cb.is_tripped("fetch_dem") is False

    # Success resets the counter.
    cb.record_success("fetch_dem")

    # Two more failures — should NOT trip (counter was reset to 0 after success).
    cb.record_failure("fetch_dem")
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is False

    # Third failure after the reset — now trips (0 + 1 + 1 + 1 = 3).
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True


def test_success_on_clean_tool_is_noop():
    """record_success on a tool with no prior failures is a no-op."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    cb.record_success("fetch_dem")  # must not raise
    assert cb.is_tripped("fetch_dem") is False


# ---------------------------------------------------------------------------
# Test 5: env overrides
# ---------------------------------------------------------------------------


def test_env_override_threshold(monkeypatch):
    """``TRID3NT_CIRCUIT_THRESHOLD`` overrides the default threshold."""
    monkeypatch.setenv("TRID3NT_CIRCUIT_THRESHOLD", "2")
    cb = ToolCircuitBreaker()
    assert cb.threshold == 2
    # With threshold=2, trip on 2nd failure.
    cb.record_failure("tool_a")
    assert cb.is_tripped("tool_a") is False
    cb.record_failure("tool_a")
    assert cb.is_tripped("tool_a") is True


def test_env_override_cooldown(monkeypatch):
    """``TRID3NT_CIRCUIT_COOLDOWN_S`` overrides the default cooldown."""
    monkeypatch.setenv("TRID3NT_CIRCUIT_COOLDOWN_S", "5.0")
    cb = ToolCircuitBreaker()
    assert cb.cooldown_s == 5.0


def test_env_override_threshold_invalid_falls_back(monkeypatch):
    """An invalid ``TRID3NT_CIRCUIT_THRESHOLD`` falls back to the default."""
    monkeypatch.setenv("TRID3NT_CIRCUIT_THRESHOLD", "not_a_number")
    cb = ToolCircuitBreaker()
    assert cb.threshold == _DEFAULT_THRESHOLD


def test_env_override_cooldown_invalid_falls_back(monkeypatch):
    """An invalid ``TRID3NT_CIRCUIT_COOLDOWN_S`` falls back to the default."""
    monkeypatch.setenv("TRID3NT_CIRCUIT_COOLDOWN_S", "bad")
    cb = ToolCircuitBreaker()
    assert cb.cooldown_s == _DEFAULT_COOLDOWN_S


def test_env_override_threshold_zero_falls_back(monkeypatch):
    """A threshold of 0 (< 1) is invalid and falls back to the default."""
    monkeypatch.setenv("TRID3NT_CIRCUIT_THRESHOLD", "0")
    cb = ToolCircuitBreaker()
    assert cb.threshold == _DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Test 6: CircuitBreakerError shape
# ---------------------------------------------------------------------------


def test_circuit_breaker_error_shape():
    """``CircuitBreakerError`` carries the FR-AS-11 typed-exception contract."""
    err = CircuitBreakerError("fetch_dem", 45.3)
    assert CircuitBreakerError.error_code == "CIRCUIT_BREAKER_TRIPPED"
    assert CircuitBreakerError.retryable is False
    assert err.tool_name == "fetch_dem"
    assert err.cooldown_remaining_s == 45.3
    assert "45" in str(err)  # cooldown seconds in message
    assert "fetch_dem" in str(err)


def test_circuit_breaker_error_is_runtime_error():
    """``CircuitBreakerError`` is a ``RuntimeError`` subclass (existing except patterns)."""
    err = CircuitBreakerError("tool_x", 10.0)
    assert isinstance(err, RuntimeError)


def test_circuit_breaker_error_harvested_by_summarize():
    """``summarize_tool_result`` harvests error_code + retryable from CircuitBreakerError."""
    from trid3nt_server.adapter import summarize_tool_result

    err = CircuitBreakerError("fetch_stac", 30.0)
    summary = summarize_tool_result("fetch_stac", None, error=err)
    assert summary["status"] == "error"
    assert summary["error_code"] == "CIRCUIT_BREAKER_TRIPPED"
    assert summary["retryable"] is False
    assert "fetch_stac" in summary["message"]


# ---------------------------------------------------------------------------
# Test 7: record_failure on an already-tripped breaker does not reset clock
# ---------------------------------------------------------------------------


def test_record_failure_on_tripped_breaker_does_not_reset_clock():
    """Calling record_failure after tripping should not extend or reset the cooldown.

    The is_tripped guard in record_failure returns early when already open,
    so the ``cooldown_until`` timestamp stays unchanged.
    """
    cb = ToolCircuitBreaker(threshold=1, cooldown_s=10.0)
    cb.record_failure("tool_a")  # trip
    assert cb.is_tripped("tool_a") is True

    # Record the deadline immediately after tripping.
    deadline_first = cb._cooldown_until.get("tool_a")

    # Wait a bit, then call record_failure again.
    time.sleep(0.01)
    cb.record_failure("tool_a")

    # Deadline should be unchanged (record_failure returned early).
    deadline_second = cb._cooldown_until.get("tool_a")
    assert deadline_first == deadline_second, (
        f"Cooldown deadline was mutated: {deadline_first} → {deadline_second}"
    )


# ---------------------------------------------------------------------------
# Test 8: multiple tools are independent
# ---------------------------------------------------------------------------


def test_multiple_tools_are_independent():
    """Tripping the breaker for one tool does not affect another."""
    cb = ToolCircuitBreaker(threshold=2, cooldown_s=60.0)
    cb.record_failure("fetch_dem")
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True

    # A different tool is still clean.
    assert cb.is_tripped("fetch_wdpa") is False
    cb.record_failure("fetch_wdpa")
    assert cb.is_tripped("fetch_wdpa") is False  # only 1 failure, threshold=2


# ---------------------------------------------------------------------------
# Test 9: failure classification (LIVE BUG 2026-06-17 — Oklahoma-tornado).
#
# The breaker must trip ONLY on UPSTREAM/transient faults, NEVER on a
# deterministic CLIENT/argument error — otherwise a burst of bad-arg calls
# trips the breaker and the cooldown then BLOCKS the corrected-args retry.
# ---------------------------------------------------------------------------


def test_is_client_arg_error_classification():
    """is_client_arg_error: arg/validation errors True, upstream/transient False."""
    # Typed retryable=False → client/arg error.
    assert is_client_arg_error(_FakeArgError("bad state")) is True
    # Typed retryable=True → upstream/transient.
    assert is_client_arg_error(_FakeUpstreamError("503")) is False
    # Untyped programmer/arg-shape errors → client/arg.
    assert is_client_arg_error(ValueError("nope")) is True
    assert is_client_arg_error(TypeError("nope")) is True
    assert is_client_arg_error(KeyError("nope")) is True
    assert is_client_arg_error(AttributeError("nope")) is True
    # Untyped transient-ish → upstream (counts).
    assert is_client_arg_error(TimeoutError("slow")) is False
    assert is_client_arg_error(ConnectionError("reset")) is False
    assert is_client_arg_error(RuntimeError("???")) is False
    # No error → not a client error.
    assert is_client_arg_error(None) is False


def test_breaker_does_not_trip_on_client_arg_errors():
    """N consecutive CLIENT/arg errors leave the breaker CLOSED.

    This is the Oklahoma-tornado bug fix: a model passing a bad arg
    (e.g. "Oklahoma" before the validator was relaxed) must NOT trip the
    breaker — the model can self-correct and retry, and a tripped breaker
    would block that corrected retry for the whole cooldown.
    """
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    # Fire WAY more than the threshold of arg errors.
    for _ in range(10):
        cb.record_failure("fetch_storm_events_db", _FakeArgError("bad state"))
    assert cb.is_tripped("fetch_storm_events_db") is False
    # The counter must not have advanced at all.
    assert cb._consecutive_failures.get("fetch_storm_events_db", 0) == 0


def test_breaker_does_not_trip_on_untyped_value_error():
    """Untyped ValueError (arg-shape) also does not count toward a trip."""
    cb = ToolCircuitBreaker(threshold=2, cooldown_s=60.0)
    for _ in range(5):
        cb.record_failure("some_tool", ValueError("bad arg"))
    assert cb.is_tripped("some_tool") is False


def test_breaker_trips_on_upstream_errors():
    """N consecutive UPSTREAM/transient faults DO trip the breaker."""
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    for _ in range(3):
        cb.record_failure("fetch_stac", _FakeUpstreamError("503 from STAC"))
    assert cb.is_tripped("fetch_stac") is True


def test_breaker_trips_on_untyped_runtime_and_timeout():
    """Untyped RuntimeError / TimeoutError count as upstream and trip."""
    cb = ToolCircuitBreaker(threshold=2, cooldown_s=60.0)
    cb.record_failure("tool_a", RuntimeError("opaque upstream failure"))
    cb.record_failure("tool_a", TimeoutError("read timed out"))
    assert cb.is_tripped("tool_a") is True


def test_arg_errors_do_not_block_subsequent_upstream_trip():
    """Arg errors don't poison the counter — a later real upstream loop still trips.

    Models the exact Oklahoma scenario: a burst of bad-arg failures, then the
    tool is corrected and a genuine upstream outage occurs. The breaker must
    still be able to trip on the real upstream failures (it was never blocked
    by the arg errors), AND the arg errors must not have pre-loaded the counter.
    """
    cb = ToolCircuitBreaker(threshold=3, cooldown_s=60.0)
    # 5 arg errors — no effect.
    for _ in range(5):
        cb.record_failure("fetch_storm_events_db", _FakeArgError("Oklahoma"))
    assert cb.is_tripped("fetch_storm_events_db") is False
    assert cb._consecutive_failures.get("fetch_storm_events_db", 0) == 0

    # Now 2 real upstream failures — still below threshold (3), because the
    # arg errors did NOT pre-load the counter.
    cb.record_failure("fetch_storm_events_db", _FakeUpstreamError("503"))
    cb.record_failure("fetch_storm_events_db", _FakeUpstreamError("503"))
    assert cb.is_tripped("fetch_storm_events_db") is False
    # Third real upstream failure trips it.
    cb.record_failure("fetch_storm_events_db", _FakeUpstreamError("503"))
    assert cb.is_tripped("fetch_storm_events_db") is True


def test_record_failure_no_error_arg_counts_legacy_behaviour():
    """record_failure() with no exception still counts (conservative default)."""
    cb = ToolCircuitBreaker(threshold=2, cooldown_s=60.0)
    cb.record_failure("legacy_tool")  # no error arg
    cb.record_failure("legacy_tool")
    assert cb.is_tripped("legacy_tool") is True
