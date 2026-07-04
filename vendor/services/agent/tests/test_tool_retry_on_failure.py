"""Tool-retry-on-failure tests (job-0177).

Extends the job-0169 multi-turn loop with structured error feedback so
Gemini can decide whether to retry (with corrected args / a different
tool) or narrate failure honestly when a tool dispatch raises.

Per the kickoff: when ``_invoke_tool_via_emitter`` raises, the loop
already catches the exception and feeds a ``function_response`` back to
Gemini.  job-0177 enriches that payload with
``{status: "error", error_code: str, message: str, retryable: bool}``
so Gemini reads the structured retry signal rather than a free-form
``error: str``.  The signal is sourced from the tool's own typed
exception class (FR-AS-11 — every tool already declares ``error_code``
+ ``retryable`` on its exception base class) and falls back to a
conservative heuristic for untyped exceptions.

UI visibility is DEFERRED for v0.1 (per memory + kickoff): each retry
attempt produces a new tool card; the CHAIN of cards is the visible
retry signal.  ``MAX_TURN_ITERATIONS`` already caps runaway retry.

Tests:

1. ``summarize_tool_result`` emits the new structured-error shape
   ``{status, error_code, message, retryable, error_type}`` and
   harvests ``error_code`` + ``retryable`` from typed tool exceptions.
2. Untyped exceptions (``RuntimeError``, ``ValueError``,
   ``TimeoutError``, ``ConnectionError``) classify into sensible
   defaults — programmer errors NOT retryable, network errors are.
3. The error shape stays JSON-serializable + within the char budget.
4. End-to-end: a tool dispatch raises → Gemini sees the structured
   error → emits a second function_call (retry with different args) →
   second dispatch succeeds → loop terminates with narrative.
5. End-to-end: a tool dispatch keeps failing → loop fail-stops at
   ``MAX_TURN_ITERATIONS`` rather than retrying forever.
6. End-to-end: legacy ``error`` field is still present (alias of
   ``message``) so older callers / tests don't break.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.adapter import (
    FunctionCallEvent,
    GeminiSettings,
    MAX_TURN_ITERATIONS,
    TextDeltaEvent,
    _classify_error,
    summarize_tool_result,
)
from grace2_contracts import new_ulid


# ---------------------------------------------------------------------------
# Test 1: structured error shape per the kickoff
# ---------------------------------------------------------------------------


def test_summarize_tool_result_error_carries_structured_fields():
    """Error path now emits {status, error_code, message, retryable, error_type}."""
    err = RuntimeError("dem fetch upstream 503")
    summary = summarize_tool_result("fetch_dem", None, error=err)
    assert summary["status"] == "error"
    assert summary["error_code"] == "RUNTIMEERROR"  # fallback from class name
    assert summary["message"] == "dem fetch upstream 503"
    assert summary["retryable"] is True  # RuntimeError defaults to retryable
    assert summary["error_type"] == "RuntimeError"
    # Legacy alias preserved.
    assert summary["error"] == summary["message"]


def test_summarize_tool_result_error_harvests_typed_attributes():
    """A typed tool exception's ``error_code`` + ``retryable`` are harvested.

    Mirrors the shape used across ``fetch_*`` / ``compute_*`` tools — every
    tool's exception base class declares ``error_code: str`` and
    ``retryable: bool`` as class attributes (FR-AS-11).  job-0177 surfaces
    those into the function_response so Gemini sees the retry signal the
    tool already knew.
    """

    class WDPABboxError(RuntimeError):
        error_code = "WDPA_BBOX_INVALID"
        retryable = False

    err = WDPABboxError("bbox out of range")
    summary = summarize_tool_result("fetch_wdpa_protected_areas", None, error=err)
    assert summary["status"] == "error"
    assert summary["error_code"] == "WDPA_BBOX_INVALID"
    assert summary["retryable"] is False
    assert summary["message"] == "bbox out of range"
    assert summary["error_type"] == "WDPABboxError"


def test_summarize_tool_result_error_typed_upstream_is_retryable():
    """An upstream-flavor typed exception flags retryable=True."""

    class WDPAUpstreamError(RuntimeError):
        error_code = "WDPA_UPSTREAM_ERROR"
        retryable = True

    err = WDPAUpstreamError("WDPA ArcGIS REST 503")
    summary = summarize_tool_result("fetch_wdpa_protected_areas", None, error=err)
    assert summary["error_code"] == "WDPA_UPSTREAM_ERROR"
    assert summary["retryable"] is True


# ---------------------------------------------------------------------------
# Test 2: heuristic classification for untyped exceptions
# ---------------------------------------------------------------------------


def test_classify_error_value_error_not_retryable():
    """ValueError = bad args = not retryable (Gemini must change something)."""
    code, retryable = _classify_error(ValueError("invalid bbox shape"))
    assert code == "VALUEERROR"
    assert retryable is False


def test_classify_error_type_error_not_retryable():
    """TypeError = wrong kwargs = not retryable in the strict sense."""
    code, retryable = _classify_error(TypeError("unexpected keyword 'run_name'"))
    assert retryable is False


def test_classify_error_key_attribute_not_retryable():
    """KeyError / AttributeError = programmer error = not retryable."""
    assert _classify_error(KeyError("missing"))[1] is False
    assert _classify_error(AttributeError("no attr"))[1] is False


def test_classify_error_timeout_retryable():
    """asyncio.TimeoutError + TimeoutError = transient = retryable."""
    assert _classify_error(asyncio.TimeoutError())[1] is True
    assert _classify_error(TimeoutError("timed out"))[1] is True


def test_classify_error_connection_retryable():
    """ConnectionError / OSError = network blip = retryable."""
    assert _classify_error(ConnectionError("conn refused"))[1] is True
    assert _classify_error(OSError("upstream"))[1] is True


def test_classify_error_runtime_error_retryable():
    """RuntimeError = unknown but probably transient = retryable."""
    assert _classify_error(RuntimeError("generic"))[1] is True


def test_classify_error_typed_overrides_heuristic():
    """A typed ValueError-subclass that declares retryable=True wins."""

    class TimeoutishValueError(ValueError):
        error_code = "TIMEOUT_VIA_VALUE_ERROR"
        retryable = True

    code, retryable = _classify_error(TimeoutishValueError("transient"))
    assert code == "TIMEOUT_VIA_VALUE_ERROR"
    assert retryable is True


# ---------------------------------------------------------------------------
# Test 3: JSON-serializable + budget-clean
# ---------------------------------------------------------------------------


def test_error_summary_is_json_serializable():
    """The error dict must round-trip through json.dumps without raising."""
    err = RuntimeError("a" * 1000)  # long message exercises the 500-char clip
    summary = summarize_tool_result("fetch_dem", None, error=err)
    encoded = json.dumps(summary)
    decoded = json.loads(encoded)
    assert decoded["status"] == "error"
    assert decoded["retryable"] in (True, False)
    # 500-char clip on the message.
    assert len(decoded["message"]) <= 500


# ---------------------------------------------------------------------------
# Test 4: end-to-end retry-and-succeed
# ---------------------------------------------------------------------------


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str = "c1"):
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


@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 — protocol shim
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_stream_gemini_reply_retry_after_recoverable_failure():
    """First dispatch fails (retryable=True) → second dispatch succeeds.

    Gemini reads the structured error_code + retryable=True in the
    function_response, decides to retry with different args, the second
    dispatch succeeds, and the loop terminates with a narrative turn.
    This is the core kickoff scenario.
    """
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    # Turn 1: Gemini calls fetch_dem with a (made-up) bbox.
    # Turn 2: Gemini sees the upstream error + retryable=True; retries.
    # Turn 3: Gemini narrates success.
    turn1_chunk = _make_fake_chunk_with_function_call(
        "fetch_dem", {"bbox": [-82.0, 26.5, -81.7, 26.8]}, "call-dem-1"
    )
    turn2_chunk = _make_fake_chunk_with_function_call(
        "fetch_dem", {"bbox": [-82.1, 26.4, -81.6, 26.9]}, "call-dem-2"
    )
    turn3_chunk = _make_fake_chunk_with_text(
        "Retrieved the DEM on the second attempt; max elevation 12 m."
    )
    turn_iter = iter([
        iter([turn1_chunk]),
        iter([turn2_chunk]),
        iter([turn3_chunk]),
    ])

    # Capture the function_response payload appended after the failing
    # dispatch — this is the structured-error shape Gemini must see.
    contents_per_turn: list[list[Any]] = []

    def _capture_and_stream(**kwargs):
        snapshot = []
        for c in kwargs["contents"]:
            parts_repr = []
            for p in c.parts:
                if p.text:
                    parts_repr.append(("text", p.text))
                elif getattr(p, "function_call", None):
                    parts_repr.append(("function_call", p.function_call.name))
                elif getattr(p, "function_response", None):
                    parts_repr.append(
                        ("function_response", p.function_response.name, p.function_response.response)
                    )
                else:
                    parts_repr.append(("unknown", None))
            snapshot.append((c.role, parts_repr))
        contents_per_turn.append(snapshot)
        return next(turn_iter)

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = _capture_and_stream

    dispatch_log: list[tuple[str, dict]] = []
    attempt_counter = {"n": 0}

    async def _flaky_invoke(_ws, _state, name, args):
        attempt_counter["n"] += 1
        dispatch_log.append((name, args))
        if attempt_counter["n"] == 1:
            # First dispatch raises an UPSTREAM_API_ERROR-shaped exception.
            class DEMUpstreamError(RuntimeError):
                error_code = "DEM_UPSTREAM_ERROR"
                retryable = True

            raise DEMUpstreamError("DEM upstream 503 (temporary)")
        # Second dispatch returns a payload.
        return {
            "wms_url": "https://qgis.example.com/wms?LAYERS=dem-fortmyers",
            "layer_id": "dem-fortmyers",
            "metrics": {"max_elevation_m": 12.0},
        }

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_flaky_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Get me the DEM for Fort Myers", "research"
        )

    # Both dispatches happened — the retry actually ran.
    assert len(dispatch_log) == 2
    assert dispatch_log[0][0] == "fetch_dem"
    assert dispatch_log[1][0] == "fetch_dem"
    # Second call's args were not identical to first (Gemini chose to vary).
    assert dispatch_log[0][1] != dispatch_log[1][1]

    # Turn 2 carried the function_response with the structured error so
    # Gemini saw it before deciding to retry.
    turn2_parts = [
        p for (_role, parts) in contents_per_turn[1] for p in parts
    ]
    fn_responses = [p for p in turn2_parts if p[0] == "function_response"]
    assert fn_responses, f"turn 2 missing function_response: {contents_per_turn[1]}"
    fn_resp_payload = fn_responses[0][2]
    assert fn_resp_payload["status"] == "error"
    assert fn_resp_payload["error_code"] == "DEM_UPSTREAM_ERROR"
    assert fn_resp_payload["retryable"] is True
    assert "503" in fn_resp_payload["message"]

    # The narrative turn ran (loop terminated cleanly).
    narrative_chunks = [
        json.loads(m) for m in sock.sent if "agent-message-chunk" in m
    ]
    text_seen = "".join(
        c["payload"]["delta"] for c in narrative_chunks if c["payload"].get("delta")
    )
    assert "second attempt" in text_seen.lower()


# ---------------------------------------------------------------------------
# Test 5: retry doesn't loop forever — MAX_TURN_ITERATIONS caps it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_gemini_reply_failed_retry_caps_at_max_iterations():
    """A tool that always raises + a Gemini that always retries → loop stops.

    With the circuit breaker (job-B8, Wave 4.10) wired into the loop, the
    real ``_invoke_tool_via_emitter`` mock is called at most
    ``circuit_breaker.threshold`` times (default 3) before the breaker trips
    and subsequent calls are short-circuited.  The outer loop still terminates
    — either the breaker trips first, or ``MAX_TURN_ITERATIONS`` is hit —
    whichever comes first.

    The invariant this test guards: dispatches to the *actual tool* are
    bounded (the breaker caps them at threshold); the loop does not run forever.
    """
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState
    from grace2_agent.circuit_breaker import ToolCircuitBreaker

    def _always_retry():
        i = 0
        while True:
            i += 1
            yield iter([_make_fake_chunk_with_function_call(
                "fetch_dem", {"bbox": [0, 0, 1, 1], "attempt": i}, f"call-{i}"
            )])

    chunks = _always_retry()
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(chunks)

    dispatch_count = {"n": 0}

    async def _always_fail(_ws, _state, name, args):
        dispatch_count["n"] += 1
        raise RuntimeError("upstream 503")

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_always_fail), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "x", "research"
        )

    # The circuit breaker caps real dispatches at its threshold (default 3);
    # additional Gemini turns that keep calling fetch_dem are short-circuited.
    # Importantly: the loop must terminate (no infinite loop).
    threshold = state.circuit_breaker.threshold
    assert dispatch_count["n"] <= threshold, (
        f"Dispatches exceeded circuit-breaker threshold: "
        f"{dispatch_count['n']} dispatches vs threshold {threshold}. "
        "This indicates the breaker did not trip correctly."
    )
    assert dispatch_count["n"] > 0, "Expected at least one dispatch before breaker trip"


# ---------------------------------------------------------------------------
# Test 6: legacy ``error`` field preserved (no breaking-shape change)
# ---------------------------------------------------------------------------


def test_summarize_tool_result_error_legacy_alias_preserved():
    """Old callers reading ``summary['error']`` still get a string.

    The job-0169 ``test_multi_turn_loop.py::test_summarize_tool_result_error_path``
    asserts on ``summary["error"]`` / ``summary["error_type"]``.  job-0177
    must not break that contract while adding the structured fields.
    """
    err = RuntimeError("upstream 503 — temporary")
    summary = summarize_tool_result("fetch_dem", None, error=err)
    assert "error" in summary  # legacy field preserved
    assert summary["error"] == summary["message"]
    assert summary["error_type"] == "RuntimeError"
    # New structured fields also present.
    assert "error_code" in summary
    assert "retryable" in summary
    assert "message" in summary
