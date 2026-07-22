"""ToolNotFoundError typed-exception refactor tests (B-rev job).

Wave 4.9 introduced a structured error envelope for tool results so Gemini
can distinguish "tool ran and returned nothing" from "the dispatch failed."
That envelope is only emitted when ``_invoke_tool_via_emitter`` raises an
exception; when it previously returned ``None`` (TOOL_NOT_FOUND path,
payload-warning cancel path), ``summarize_tool_result`` emitted the weak
``{"status": "no_result"}`` shape.

B-rev fixes this: both routing-failure paths now raise typed exceptions
(``ToolNotFoundError`` and ``PayloadWarningCancelledError``) so the
exception handler at the call site routes them through
``summarize_tool_result(error=...)`` — the same Wave 4.9 structured envelope
that all ``fetch_*`` / ``compute_*`` typed exceptions already use.

Tests:

1. ``ToolNotFoundError`` shape: class attributes, message format, valid-tools
   hint, JSON-serializable.
2. ``_invoke_tool_via_emitter`` raises ``ToolNotFoundError`` for an unknown
   tool (not ``return None``).
3. ``summarize_tool_result(error=ToolNotFoundError(...))`` emits the structured
   envelope with ``error_code="TOOL_NOT_FOUND"`` and ``retryable=False``.
4. Multi-turn loop: a function_call to an unknown tool → ToolNotFoundError
   → function_response carries the structured envelope → Gemini narrates.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.adapter import (
    GeminiSettings,
    summarize_tool_result,
    _classify_error,
)
from grace2_agent.server import (
    SessionState,
    ToolNotFoundError,
    _dispatch_tool_and_persist,
    _invoke_tool_via_emitter,
)
from grace2_contracts import new_ulid


# ---------------------------------------------------------------------------
# Minimal WebSocket / SessionState helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    """Minimal WebSocket stand-in that captures sent strings."""
    sent: list[dict] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 — protocol shim
        self.sent.append(json.loads(msg))


def _make_session() -> SessionState:
    return SessionState(session_id=new_ulid())


# ---------------------------------------------------------------------------
# Test 1: ToolNotFoundError shape
# ---------------------------------------------------------------------------


def test_tool_not_found_error_shape_base_attributes():
    """``ToolNotFoundError`` carries error_code and retryable as class attrs."""
    assert ToolNotFoundError.error_code == "TOOL_NOT_FOUND"
    assert ToolNotFoundError.retryable is False


def test_tool_not_found_error_instance_carries_tool_name():
    """Instance message contains the unknown tool name."""
    err = ToolNotFoundError("fetch_volcano_lava", ["fetch_dem", "fetch_buildings"])
    assert "fetch_volcano_lava" in str(err)


def test_tool_not_found_error_valid_tools_capped_at_20():
    """valid_tools hint is capped at the first 20 entries."""
    long_list = [f"tool_{i}" for i in range(50)]
    err = ToolNotFoundError("nonexistent", long_list)
    assert len(err.valid_tools) == 20


def test_tool_not_found_error_valid_tools_shorter_list():
    """valid_tools hint of <20 entries is retained as-is."""
    short_list = ["fetch_dem", "fetch_buildings", "geocode_location"]
    err = ToolNotFoundError("bad_tool", short_list)
    assert err.valid_tools == short_list


def test_tool_not_found_error_is_json_serializable():
    """str(err) round-trips through JSON (used in summarize_tool_result)."""
    err = ToolNotFoundError("bad_tool", ["fetch_dem"])
    message = str(err)[:500]
    # Should not raise when embedded in a JSON dict.
    encoded = json.dumps({"message": message})
    assert json.loads(encoded)["message"]


def test_tool_not_found_error_classify_error_harvests_attributes():
    """``_classify_error`` harvests error_code + retryable from ToolNotFoundError."""
    err = ToolNotFoundError("bad_tool", ["fetch_dem"])
    code, retryable = _classify_error(err)
    assert code == "TOOL_NOT_FOUND"
    assert retryable is False


# ---------------------------------------------------------------------------
# Test 2: _invoke_tool_via_emitter raises on unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_tool_via_emitter_raises_for_unknown_tool():
    """Unknown tool name → ToolNotFoundError raised (not return None)."""
    ws = _FakeSocket()
    state = _make_session()
    with pytest.raises(ToolNotFoundError) as exc_info:
        await _invoke_tool_via_emitter(ws, state, "totally_fake_tool_xyz", {})
    err = exc_info.value
    assert err.error_code == "TOOL_NOT_FOUND"
    assert err.retryable is False
    assert "totally_fake_tool_xyz" in str(err)


@pytest.mark.asyncio
async def test_invoke_tool_via_emitter_no_longer_returns_none_for_unknown_tool():
    """_invoke_tool_via_emitter must NOT return None for unknown tool.

    B-rev removes the ``return None`` path; returning None silently caused
    ``summarize_tool_result`` to emit ``status: no_result`` which Gemini
    couldn't distinguish from a successful-but-empty tool run.
    """
    ws = _FakeSocket()
    state = _make_session()
    raised = False
    try:
        result = await _invoke_tool_via_emitter(ws, state, "ghost_tool", {})
        # If we reach here without an exception, the old behaviour is still
        # in place — the test should fail.
        assert result is not None, (
            "Expected ToolNotFoundError but got None — old return-None path "
            "is still active (B-rev regression)."
        )
    except ToolNotFoundError:
        raised = True
    assert raised, "Expected ToolNotFoundError was not raised"


# ---------------------------------------------------------------------------
# Test 3: summarize_tool_result emits structured envelope for ToolNotFoundError
# ---------------------------------------------------------------------------


def test_summarize_tool_result_tool_not_found_emits_error_envelope():
    """ToolNotFoundError fed to summarize_tool_result emits full structured envelope."""
    err = ToolNotFoundError("bad_tool", ["fetch_dem", "geocode_location"])
    summary = summarize_tool_result("bad_tool", None, error=err)
    assert summary["status"] == "error"
    assert summary["error_code"] == "TOOL_NOT_FOUND"
    assert summary["retryable"] is False
    assert summary["error_type"] == "ToolNotFoundError"
    # message field carries the human-readable text.
    assert "bad_tool" in summary["message"]
    # Legacy alias preserved (job-0177 contract).
    assert summary["error"] == summary["message"]


def test_summarize_tool_result_tool_not_found_is_json_serializable():
    """The error envelope must round-trip through json.dumps without raising."""
    err = ToolNotFoundError("nonexistent_tool", ["fetch_dem"])
    summary = summarize_tool_result("nonexistent_tool", None, error=err)
    encoded = json.dumps(summary)
    decoded = json.loads(encoded)
    assert decoded["status"] == "error"
    assert decoded["error_code"] == "TOOL_NOT_FOUND"
    assert decoded["retryable"] is False


def test_summarize_tool_result_no_result_still_works_for_genuine_none():
    """summarize_tool_result(None) still emits no_result for non-routing None.

    B-rev removes the return-None routing-failure paths but legitimate tool
    callables that return None (side-effect-only tools) must still map to
    ``status: no_result`` — that contract is unchanged.
    """
    summary = summarize_tool_result("some_side_effect_tool", None)
    assert summary["status"] == "no_result"
    # Must NOT have error fields.
    assert "error_code" not in summary
    assert "retryable" not in summary


# ---------------------------------------------------------------------------
# Test 4: multi-turn loop accumulates function_response with error envelope
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


@pytest.mark.asyncio
async def test_multi_turn_loop_tool_not_found_feeds_error_to_gemini():
    """Unknown-tool call → ToolNotFoundError → function_response carries error.

    The multi-turn loop must NOT propagate ToolNotFoundError out of the loop
    (that would crash the session). Instead the exception handler at
    server.py:500-574 catches it, routes through summarize_tool_result(error=...),
    and appends the error envelope as a function_response — exactly what Gemini
    needs to decide "I called a tool that doesn't exist; I should narrate
    that I cannot do this." After seeing the error Gemini emits a text turn,
    and the loop terminates normally.
    """
    from grace2_agent import server as agent_server

    # Turn 1: Gemini calls a tool that doesn't exist.
    turn1_chunk = _make_fake_chunk_with_function_call(
        "fetch_volcano_lava_flow",  # not in TOOL_REGISTRY
        {"bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-lava-1",
    )
    # Turn 2: Gemini narrates it can't help.
    turn2_chunk = _make_fake_chunk_with_text(
        "I don't have a volcanic lava flow tool; I cannot model that."
    )
    turn_iter = iter([
        iter([turn1_chunk]),
        iter([turn2_chunk]),
    ])

    # Capture the function_response payload that Gemini sees on turn 2.
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
                        (
                            "function_response",
                            p.function_response.name,
                            p.function_response.response,
                        )
                    )
                else:
                    parts_repr.append(("unknown", None))
            snapshot.append((c.role, parts_repr))
        contents_per_turn.append(snapshot)
        return next(turn_iter)

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = _capture_and_stream

    # Let _invoke_tool_via_emitter run for real — it will raise ToolNotFoundError
    # because "fetch_volcano_lava_flow" is not in TOOL_REGISTRY.
    sock = _FakeSocket()
    state = _make_session()
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with (
        patch.object(agent_server, "build_client", return_value=fake_client),
        patch.object(agent_server, "build_tool_declarations", return_value=[]),
    ):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Show me lava flows in Hawaii", "research"
        )

    # Turn 2 must have received a function_response with the structured error.
    assert len(contents_per_turn) >= 2, (
        f"Expected at least 2 Gemini turns; got {len(contents_per_turn)}"
    )
    turn2_parts = [p for (_role, parts) in contents_per_turn[1] for p in parts]
    fn_responses = [p for p in turn2_parts if p[0] == "function_response"]
    assert fn_responses, (
        f"Turn 2 has no function_response part; contents: {contents_per_turn[1]}"
    )
    fn_resp_payload = fn_responses[0][2]
    assert fn_resp_payload["status"] == "error", (
        f"Expected status='error'; got {fn_resp_payload}"
    )
    # Wave 4.10 job-B5: the post-hoc allowed-set validator runs BEFORE
    # _invoke_tool_via_emitter, so an unknown tool name surfaces as
    # OUT_OF_ALLOWED_SET (it isn't in the hot set and the LLM never opened
    # any category). The structured-error round-trip the test was originally
    # verifying still holds — just with a more specific error_code that
    # tells Gemini to widen its allowed set via list_tools_in_category.
    assert fn_resp_payload["error_code"] in {"OUT_OF_ALLOWED_SET", "TOOL_NOT_FOUND"}
    assert fn_resp_payload["retryable"] is False

    # Loop terminated cleanly — narrative reached chat.
    narrative_chunks = [m for m in sock.sent if m.get("type") == "agent-message-chunk"]
    text_seen = "".join(
        c["payload"].get("delta", "") for c in narrative_chunks
    )
    assert "cannot" in text_seen.lower() or "don't" in text_seen.lower(), (
        f"Expected Gemini's fallback narrative in output; got: {text_seen!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: /invoke directive surface (_dispatch_tool_and_persist) catches
# ToolNotFoundError and emits a structured error envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_tool_and_persist_catches_tool_not_found():
    """``/invoke <unknown> {}`` → structured ``error`` envelope on the wire.

    ``_dispatch_tool_and_persist`` is the manual operator-debug surface
    (dispatched via ``asyncio.create_task`` with no awaiter). If
    ``_invoke_tool_via_emitter`` raises ``ToolNotFoundError`` and the
    wrapper does not catch it, the exception surfaces only as an asyncio
    "Task exception was never retrieved" warning at gc time — the operator
    sees nothing on the WebSocket. B-rev FIX: the wrapper catches typed
    routing exceptions and routes them through ``_send_error`` so the chat
    surface receives a structured ``error`` envelope with the same
    ``error_code`` / ``retryable`` shape as the Gemini-loop path.
    """
    sock = _FakeSocket()
    state = _make_session()

    # Drive the directive surface directly with an unregistered tool name.
    await _dispatch_tool_and_persist(
        sock, state, "totally_fake_tool_xyz", {}, "/invoke totally_fake_tool_xyz {}"
    )

    # The wire must carry an ``error`` envelope with the typed shape.
    error_envelopes = [m for m in sock.sent if m.get("type") == "error"]
    assert error_envelopes, (
        f"Expected an ``error`` envelope; got types {[m.get('type') for m in sock.sent]!r}"
    )
    err_payload = error_envelopes[0]["payload"]
    assert err_payload["error_code"] == "TOOL_NOT_FOUND", (
        f"Expected error_code=TOOL_NOT_FOUND; got {err_payload!r}"
    )
    assert err_payload["retryable"] is False, (
        f"Expected retryable=False; got {err_payload!r}"
    )
    assert "totally_fake_tool_xyz" in err_payload["message"], (
        f"Expected unknown tool name in message; got {err_payload!r}"
    )


@pytest.mark.asyncio
async def test_dispatch_tool_and_persist_does_not_raise_unhandled():
    """The wrapper must NOT re-raise ``ToolNotFoundError``.

    If the typed exception escapes the wrapper, the ``asyncio.create_task``
    at server.py:2035 surfaces it as an "exception was never retrieved"
    warning at gc time — silent failure mode. The wrapper catching the
    exception is the contract; this test guards against a regression that
    forgets the catch and lets the exception propagate.
    """
    sock = _FakeSocket()
    state = _make_session()

    # Should complete without raising.
    try:
        await _dispatch_tool_and_persist(
            sock, state, "ghost_tool_name", {}, "/invoke ghost_tool_name {}"
        )
    except ToolNotFoundError:
        pytest.fail(
            "_dispatch_tool_and_persist must not re-raise ToolNotFoundError "
            "— it is dispatched via asyncio.create_task with no awaiter."
        )
