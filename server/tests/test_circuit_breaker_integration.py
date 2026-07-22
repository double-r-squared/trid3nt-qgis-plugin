"""Circuit breaker integration tests (job-B8, Wave 4.10 Stage 3).

Verifies the full wire path:
    1. A tool that fails 3 times (threshold) trips the per-session circuit
       breaker.
    2. The 4th attempt is short-circuited: ``_invoke_tool_via_emitter`` is
       NOT called; instead ``CircuitBreakerError`` is raised immediately.
    3. The 4th call's function_response carries the Wave 4.9 structured
       envelope with error_code="CIRCUIT_BREAKER_TRIPPED" and retryable=False.
    4. A successful call after a breaker trip (post-cooldown) records success
       and resets the failure counter.
    5. The circuit breaker is per-session (independent ``SessionState``
       instances have independent breakers).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_server.adapter import (
    GeminiSettings,
    MAX_TURN_ITERATIONS,
    summarize_tool_result,
)
from trid3nt_server.circuit_breaker import CircuitBreakerError, ToolCircuitBreaker
from trid3nt_server.server import SessionState
from trid3nt_contracts import new_ulid


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    fake_part = MagicMock()
    fake_part.function_call = fn_call
    fake_part.text = None
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


def _make_fake_chunk_with_text(text: str):
    fake_part = MagicMock()
    fake_part.function_call = None
    fake_part.text = text
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


# ---------------------------------------------------------------------------
# Test 1+2+3: 3 failures trip the breaker; 4th call is short-circuited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_third_failure_and_short_circuits_fourth():
    """3 consecutive failures trip the breaker; the 4th call is short-circuited.

    The test patches ``_invoke_tool_via_emitter`` to fail for the first 3
    calls and checks that on the 4th turn Gemini would have been asked for
    another function_call but the breaker intercepts it BEFORE the real
    ``_invoke_tool_via_emitter`` runs.  The function_response Gemini gets on
    the 4th call must carry CIRCUIT_BREAKER_TRIPPED + retryable=False.
    """
    from trid3nt_server import server as agent_server

    invoke_call_count = {"n": 0}

    # 4 Gemini turns, each requesting fetch_dem.
    def _gemini_stream(**kw):
        turn = invoke_call_count["n"]  # approximate — close enough for ordering
        return iter([
            _make_fake_chunk_with_function_call(
                "fetch_dem", {"bbox": [0, 0, 1, 1]}, f"call-{turn}"
            )
        ])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **kw: _gemini_stream(**kw)

    # The real _invoke_tool_via_emitter fails on the first 3 calls; would
    # succeed on the 4th — but the breaker should prevent the 4th from ever
    # reaching this mock.
    async def _invoke_stub(_ws, _state, name, args):
        invoke_call_count["n"] += 1
        if invoke_call_count["n"] <= 3:
            class UpstreamError(RuntimeError):
                error_code = "DEM_UPSTREAM_ERROR"
                retryable = True
            raise UpstreamError("upstream 503")
        # Should never reach here — breaker should have short-circuited.
        return {"wms_url": "http://example.com", "layer_id": "dem-x"}

    # Capture all function_response payloads fed back to Gemini so we can
    # inspect the 4th one (the CIRCUIT_BREAKER_TRIPPED envelope).
    contents_per_turn: list[list] = []

    real_stream_side_effect = fake_client.models.generate_content_stream.side_effect

    def _capture_stream(**kw):
        snapshot = []
        for c in kw.get("contents", []):
            parts_repr = []
            for p in c.parts:
                if p.text:
                    parts_repr.append(("text", p.text))
                elif getattr(p, "function_call", None):
                    parts_repr.append(("function_call", p.function_call.name))
                elif getattr(p, "function_response", None):
                    parts_repr.append((
                        "function_response",
                        p.function_response.name,
                        p.function_response.response,
                    ))
                else:
                    parts_repr.append(("unknown", None))
            snapshot.append((c.role, parts_repr))
        contents_per_turn.append(snapshot)
        return real_stream_side_effect(**kw)

    fake_client.models.generate_content_stream.side_effect = _capture_stream

    # Use threshold=3 (default) and a long cooldown so the breaker stays open.
    state = SessionState(session_id=new_ulid())
    state.circuit_breaker = ToolCircuitBreaker(threshold=3, cooldown_s=3600.0)
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )
    sock = _FakeSocket()

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_invoke_stub), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Get me the DEM for Fort Myers", "research"
        )

    # _invoke_tool_via_emitter must have been called exactly 3 times.
    assert invoke_call_count["n"] == 3, (
        f"Expected exactly 3 real invocations before breaker tripped; "
        f"got {invoke_call_count['n']}"
    )

    # Find a function_response that carries CIRCUIT_BREAKER_TRIPPED.
    cb_responses = []
    for turn_contents in contents_per_turn:
        for (_role, parts) in turn_contents:
            for part in parts:
                if part[0] == "function_response":
                    resp = part[2]
                    if isinstance(resp, dict) and resp.get("error_code") == "CIRCUIT_BREAKER_TRIPPED":
                        cb_responses.append(resp)

    assert cb_responses, (
        f"Expected a function_response with CIRCUIT_BREAKER_TRIPPED; "
        f"found none. turns inspected: {len(contents_per_turn)}"
    )
    resp = cb_responses[0]
    assert resp["status"] == "error"
    assert resp["retryable"] is False


# ---------------------------------------------------------------------------
# Test 3 (standalone): summarize_tool_result for CircuitBreakerError
# ---------------------------------------------------------------------------


def test_summarize_circuit_breaker_error_emits_wave49_envelope():
    """``summarize_tool_result`` emits the Wave 4.9 envelope for CircuitBreakerError."""
    err = CircuitBreakerError("fetch_dem", 55.0)
    summary = summarize_tool_result("fetch_dem", None, error=err)
    assert summary["status"] == "error"
    assert summary["error_code"] == "CIRCUIT_BREAKER_TRIPPED"
    assert summary["retryable"] is False
    assert summary["error_type"] == "CircuitBreakerError"
    assert "fetch_dem" in summary["message"]
    # Legacy alias preserved (job-0177 contract).
    assert summary["error"] == summary["message"]


def test_summarize_circuit_breaker_error_is_json_serializable():
    """The circuit-breaker error summary round-trips through JSON without raising."""
    err = CircuitBreakerError("fetch_stac", 12.0)
    summary = summarize_tool_result("fetch_stac", None, error=err)
    encoded = json.dumps(summary)
    decoded = json.loads(encoded)
    assert decoded["error_code"] == "CIRCUIT_BREAKER_TRIPPED"


# ---------------------------------------------------------------------------
# Test 4: success after cooldown resets the counter
# ---------------------------------------------------------------------------


def test_circuit_breaker_success_after_auto_close_resets_counter():
    """After cooldown expiry, record_success resets counter to clean slate."""
    cb = ToolCircuitBreaker(threshold=2, cooldown_s=0.05)
    cb.record_failure("fetch_dem")
    cb.record_failure("fetch_dem")
    assert cb.is_tripped("fetch_dem") is True

    # Wait for cooldown to expire.
    time.sleep(0.1)
    assert cb.is_tripped("fetch_dem") is False

    # Success should be a clean reset.
    cb.record_success("fetch_dem")
    assert cb._consecutive_failures.get("fetch_dem", 0) == 0
    assert cb._cooldown_until.get("fetch_dem") is None


# ---------------------------------------------------------------------------
# Test 5: circuit breaker is per-session (independent state instances)
# ---------------------------------------------------------------------------


def test_circuit_breaker_is_per_session():
    """Two SessionState instances have independent circuit breakers."""
    state_a = SessionState(session_id=new_ulid())
    state_b = SessionState(session_id=new_ulid())

    # Trip the breaker in session A.
    for _ in range(3):
        state_a.circuit_breaker.record_failure("fetch_dem")

    assert state_a.circuit_breaker.is_tripped("fetch_dem") is True
    # Session B is completely clean.
    assert state_b.circuit_breaker.is_tripped("fetch_dem") is False


# ---------------------------------------------------------------------------
# Oklahoma-tornado bug (2026-06-17): arg errors through the full server path
# must NOT trip the breaker — so the model can self-correct and retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arg_errors_through_server_do_not_trip_breaker():
    """Many CLIENT/arg failures via _invoke_tool_via_emitter leave the breaker CLOSED.

    Repro of the live bug: the model fired a burst of fetch_storm_events_db
    calls with a bad state arg (each raising a *ArgError with retryable=False).
    Before the fix those tripped the breaker in 3 calls and the cooldown then
    BLOCKED the corrected-args retry. After the fix the breaker stays closed
    and the corrected call's success path is reachable.
    """
    from trid3nt_server import server as agent_server

    class _ArgError(RuntimeError):
        error_code = "STORM_EVENTS_ARG_INVALID"
        retryable = False  # deterministic client/arg fault

    invoke_count = {"n": 0}

    # The model keeps trying the same tool with a bad arg; on the LAST turn it
    # would succeed (corrected args) — proving the breaker never blocked it.
    N_BAD = 6

    def _gemini_stream(**kw):
        invoke_count_so_far = invoke_count["n"]
        if invoke_count_so_far < N_BAD + 1:
            return iter([
                _make_fake_chunk_with_function_call(
                    "fetch_storm_events_db",
                    {"year": 2013, "state": "Oklahoma"},
                    f"call-{invoke_count_so_far}",
                )
            ])
        return iter([_make_fake_chunk_with_text("Done — added the tornado layer.")])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **kw: _gemini_stream(**kw)

    async def _invoke_stub(_ws, _state, name, args):
        invoke_count["n"] += 1
        # First N_BAD calls raise an arg error; the (N_BAD+1)th succeeds.
        if invoke_count["n"] <= N_BAD:
            raise _ArgError(f"unrecognized US state {args.get('state')!r}")
        return {"layer_id": "storm-events-2013-ok", "uri": "gs://x/ok.fgb"}

    state = SessionState(session_id=new_ulid())
    # Default threshold (3) — well below N_BAD, so the OLD behaviour would have
    # tripped after 3 arg errors and blocked everything after.
    state.circuit_breaker = ToolCircuitBreaker(threshold=3, cooldown_s=3600.0)
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )
    sock = _FakeSocket()

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_invoke_stub), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings,
            "show me historical tornado touchdowns in Oklahoma since 2000",
            "research",
        )

    # The breaker must NEVER have tripped despite N_BAD > threshold arg errors.
    assert state.circuit_breaker.is_tripped("fetch_storm_events_db") is False
    assert state.circuit_breaker._consecutive_failures.get(
        "fetch_storm_events_db", 0
    ) == 0, "arg errors must not load the trip counter"
    # The corrected call (N_BAD+1) was reached — the breaker never blocked it.
    assert invoke_count["n"] >= N_BAD + 1, (
        f"corrected retry was blocked; only {invoke_count['n']} invocations "
        f"(expected at least {N_BAD + 1})"
    )


# ---------------------------------------------------------------------------
# Test: CircuitBreakerError is not counted as a new failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_error_not_counted_as_additional_failure():
    """When the breaker fires (CircuitBreakerError raised), the failure counter
    must NOT increment again — record_failure is skipped for CircuitBreakerError.

    Otherwise the cooldown deadline would keep extending on every subsequent
    call while the breaker is open, which is incorrect behaviour (the deadline
    is set once at trip time and should be fixed).
    """
    from trid3nt_server import server as agent_server

    # Manually trip the breaker with exactly threshold failures.
    state = SessionState(session_id=new_ulid())
    threshold = state.circuit_breaker.threshold
    for _ in range(threshold):
        state.circuit_breaker.record_failure("fetch_dem")
    assert state.circuit_breaker.is_tripped("fetch_dem") is True

    # Record the original deadline.
    deadline_before = state.circuit_breaker._cooldown_until.get("fetch_dem")

    # Two more turns where Gemini keeps trying fetch_dem — both get short-circuited.
    invoked = {"n": 0}
    call_turn = {"t": 0}

    def _gemini_stream(**kw):
        call_turn["t"] += 1
        if call_turn["t"] <= 2:
            return iter([
                _make_fake_chunk_with_function_call(
                    "fetch_dem", {"bbox": [0, 0, 1, 1]}, f"call-{call_turn['t']}"
                )
            ])
        # 3rd turn: Gemini narrates it can't help.
        return iter([_make_fake_chunk_with_text("The DEM service is unavailable.")])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **kw: _gemini_stream(**kw)

    async def _real_invoke(_ws, _state, name, args):
        invoked["n"] += 1
        return {"wms_url": "http://example.com"}

    sock = _FakeSocket()
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_real_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Get DEM", "research"
        )

    # The real _invoke_tool_via_emitter should never have been called (breaker tripped).
    assert invoked["n"] == 0, (
        f"Circuit breaker should have prevented all invocations; got {invoked['n']}"
    )

    # The deadline should be unchanged (no extra record_failure calls that would
    # normally reset it to a later time... but since record_failure returns
    # early when already tripped, the deadline is preserved as-is).
    deadline_after = state.circuit_breaker._cooldown_until.get("fetch_dem")
    assert deadline_before == deadline_after, (
        "Deadline was mutated by extra record_failure calls on already-tripped breaker"
    )
