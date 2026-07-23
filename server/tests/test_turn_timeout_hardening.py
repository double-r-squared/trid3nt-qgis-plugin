"""Turn / Converse timeout hardening (live-down fix 2026-06-20).

THE BUG THIS PINS (live agent DOWN): NATE switched the model to Haiku, then
Nova, and the Bedrock Converse call HUNG. With no client-side timeout on the
``bedrock-runtime`` boto3 client, ``converse_stream`` (and EventStream
iteration) never returned and never raised, so:

  * the ``stream_bedrock`` producer thread (run via ``run_in_executor``) was
    stuck forever, so the consumer's ``await queue.get()`` never completed,
  * the turn coroutine never finished, so its ``_SESSION_LIVE_TURNS`` entry's
    task stayed not-done -> ``inflight_turn_count() > 0`` AND the loop was
    wedged on that turn, so NO model (even Sonnet) could respond,
  * selecting Haiku/Nova produced NOTHING on the wire -- a silent death.

THE FIX (server/src/trid3nt_server/bedrock_adapter.py):

  1. the ``bedrock-runtime`` client now carries a botocore ``Config`` with a
     bounded ``read_timeout`` / ``connect_timeout`` + a small retry policy, so a
     hung call RAISES ``ReadTimeoutError`` instead of hanging the executor
     thread forever (the core fix);
  2. the existing producer ``except BaseException`` puts that exception on the
     queue, ``stream_bedrock`` re-raises it, and the server turn loop's
     ``except Exception`` handler surfaces an honest ``LLM_UNAVAILABLE`` error
     envelope AND lets the turn TERMINATE -> the live-turn task completes ->
     ``inflight_turn_count`` drops.

The bound is on the LLM Converse call ONLY. The minutes-long ``run_solver`` /
``wait_for_completion`` solve path is intentionally NOT bounded.

Run:
    cd server && .venv/bin/python -m pytest \
        tests/test_turn_timeout_hardening.py -q
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from trid3nt_server import bedrock_adapter as ba
from trid3nt_server.adapter import TextDeltaEvent, UpstreamProviderError


# --------------------------------------------------------------------------- #
# Synthetic Converse stream helpers (mirrors test_bedrock_adapter_thinking.py)
# --------------------------------------------------------------------------- #


def _text_delta(text: str, idx: int = 0) -> dict[str, Any]:
    return {"contentBlockDelta": {"contentBlockIndex": idx, "delta": {"text": text}}}


def _metadata_event() -> dict[str, Any]:
    return {
        "metadata": {
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
        }
    }


class _OkBedrockClient:
    """A healthy client: returns a canned ``converse_stream`` event list."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
        return {"stream": list(self._events)}


class _RaisingBedrockClient:
    """A wedged client: ``converse_stream`` RAISES (the hung-call surrogate).

    A real hung call blocks on the socket until botocore's ``read_timeout``
    trips and raises ``ReadTimeoutError``; we simulate the POST-timeout state
    directly by raising on the call, which is exactly what the bounded client
    does once the timeout fires."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._exc


def _drive(client: Any, monkeypatch) -> list[Any]:
    """Drive ``stream_bedrock`` with *client*; return the yielded events.

    Raises whatever ``stream_bedrock`` raises (the timeout re-raise path)."""
    monkeypatch.setattr(ba, "_bedrock_client", lambda: client)

    async def _collect() -> list[Any]:
        out: list[Any] = []
        async for ev in ba.stream_bedrock(contents=[], tool_declarations=None):
            out.append(ev)
        return out

    return asyncio.run(_collect())


def _read_timeout_error() -> ReadTimeoutError:
    return ReadTimeoutError(
        endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com"
    )


# --------------------------------------------------------------------------- #
# 1. The boto3 client carries the read_timeout Config (the core fix)
# --------------------------------------------------------------------------- #


def test_bedrock_client_carries_read_timeout_config(monkeypatch):
    """``_bedrock_client`` builds the runtime client WITH a bounded Config.

    Without this Config a hung Converse call blocks forever (the live-down).
    We capture the kwargs boto3.client is called with and assert the Config
    carries a finite ``read_timeout`` / ``connect_timeout`` + a retry policy."""
    captured: dict[str, Any] = {}

    def _fake_boto3_client(service: str, **kwargs: Any) -> object:
        captured["service"] = service
        captured["kwargs"] = kwargs
        return object()

    import boto3

    monkeypatch.setattr(boto3, "client", _fake_boto3_client)
    ba._bedrock_client()

    assert captured["service"] == "bedrock-runtime"
    cfg = captured["kwargs"].get("config")
    assert cfg is not None, "bedrock-runtime client built WITHOUT a botocore Config"
    # A finite, positive read_timeout is the load-bearing field: it is what
    # makes a hung Converse call RAISE instead of hanging the executor thread.
    assert isinstance(cfg.read_timeout, (int, float))
    assert 0 < cfg.read_timeout < 600
    assert isinstance(cfg.connect_timeout, (int, float))
    assert 0 < cfg.connect_timeout < 120
    assert cfg.retries is not None and cfg.retries.get("max_attempts") >= 1


def test_bedrock_client_default_timeout_values():
    """The chosen defaults are 60s read / 10s connect (documented, not 0/None)."""
    assert ba._BEDROCK_READ_TIMEOUT_DEFAULT == 60.0
    assert ba._BEDROCK_CONNECT_TIMEOUT_DEFAULT == 10.0
    cfg = ba._bedrock_timeout_config()
    assert cfg.read_timeout == 60.0
    assert cfg.connect_timeout == 10.0
    assert cfg.retries["max_attempts"] == 2
    assert cfg.retries["mode"] == "standard"


def test_bedrock_client_timeout_env_override(monkeypatch):
    """Ops can retune the bound via env without a redeploy (safety valve)."""
    monkeypatch.setenv("BEDROCK_READ_TIMEOUT_S", "30")
    monkeypatch.setenv("BEDROCK_CONNECT_TIMEOUT_S", "5")
    monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
    cfg = ba._bedrock_timeout_config()
    assert cfg.read_timeout == 30.0
    assert cfg.connect_timeout == 5.0
    assert cfg.retries["max_attempts"] == 3


def test_bedrock_client_timeout_env_bad_value_falls_back(monkeypatch):
    """A garbage env value falls back to the safe default (never None/0)."""
    monkeypatch.setenv("BEDROCK_READ_TIMEOUT_S", "not-a-number")
    monkeypatch.setenv("BEDROCK_CONNECT_TIMEOUT_S", "0")  # non-positive -> default
    cfg = ba._bedrock_timeout_config()
    assert cfg.read_timeout == ba._BEDROCK_READ_TIMEOUT_DEFAULT
    assert cfg.connect_timeout == ba._BEDROCK_CONNECT_TIMEOUT_DEFAULT


# --------------------------------------------------------------------------- #
# 2. A hung / timed-out Converse RAISES out of stream_bedrock (-> turn ends)
# --------------------------------------------------------------------------- #


def test_stream_bedrock_read_timeout_raises(monkeypatch):
    """A ReadTimeoutError (the hung-call surrogate) propagates out of the stream.

    This is what lets the turn TERMINATE instead of hanging: the producer
    catches the exception, puts it on the queue, and ``stream_bedrock``
    re-raises it for the server turn loop's ``except Exception`` to handle."""
    client = _RaisingBedrockClient(_read_timeout_error())
    # e891d13 upstream-provider discipline: transient timeouts retry then
    # surface as the typed UpstreamProviderError (never the raw botocore error).
    with pytest.raises(UpstreamProviderError):
        _drive(client, monkeypatch)


def test_stream_bedrock_validation_exception_raises(monkeypatch):
    """A Converse ValidationException also propagates (honest, recoverable)."""
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad model id"}},
        "ConverseStream",
    )
    client = _RaisingBedrockClient(err)
    with pytest.raises(ClientError):
        _drive(client, monkeypatch)


def test_stream_bedrock_access_denied_raises(monkeypatch):
    """A Converse AccessDenied (e.g. model access not enabled) propagates."""
    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no access"}},
        "ConverseStream",
    )
    client = _RaisingBedrockClient(err)
    with pytest.raises(ClientError):
        _drive(client, monkeypatch)


def test_stream_bedrock_timeout_midstream_raises(monkeypatch):
    """A read timeout DURING the event stream (not just on the call) propagates.

    The real bound trips on the gap BETWEEN bytes too, so the EventStream
    iterator can raise mid-stream. Simulate with an iterable that yields one
    delta then raises -- the partial text is delivered, then the error
    surfaces (the turn terminates with an honest error, not a hang)."""

    class _MidStreamRaises:
        def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
            def _gen():
                yield _text_delta("partial ")
                raise _read_timeout_error()

            return {"stream": _gen()}

    monkeypatch.setattr(ba, "_bedrock_client", lambda: _MidStreamRaises())

    async def _collect() -> list[Any]:
        out: list[Any] = []
        async for ev in ba.stream_bedrock(contents=[], tool_declarations=None):
            out.append(ev)
        return out

    with pytest.raises(UpstreamProviderError):
        asyncio.run(_collect())


# --------------------------------------------------------------------------- #
# 3. The NORMAL Converse path is UNAFFECTED by the timeout hardening
# --------------------------------------------------------------------------- #


def test_stream_bedrock_normal_path_unaffected(monkeypatch):
    """A healthy stream still yields text + usage cleanly (no regression)."""
    events = [
        _text_delta("Hello "),
        _text_delta("world."),
        _metadata_event(),
    ]
    out = _drive(_OkBedrockClient(events), monkeypatch)
    text = "".join(e.delta for e in out if isinstance(e, TextDeltaEvent))
    assert text == "Hello world."


# --------------------------------------------------------------------------- #
# Server-level: a failed model call does NOT pin busy / wedge the loop.
# THIS IS THE LOAD-BEARING ASSERT for the live-down fix.
# --------------------------------------------------------------------------- #


@dataclass
class _FakeSocket:
    """Minimal WebSocket shim that records every ``send`` payload."""

    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 - protocol shim
        self.sent.append(msg)


def _make_text_stream(*chunks: str):
    """Build an async ``stream_events_with_contents`` stand-in yielding text."""

    async def _fake_stream(*_args: Any, **_kwargs: Any):
        for c in chunks:
            yield TextDeltaEvent(delta=c)

    return _fake_stream


def _make_raising_stream(exc: BaseException):
    """Build an async ``stream_events_with_contents`` stand-in that RAISES.

    Mirrors a Bedrock Converse hung-then-timed-out call surfacing through the
    adapter as a re-raised ``ReadTimeoutError``."""

    async def _fake_stream(*_args: Any, **_kwargs: Any):
        if False:  # pragma: no cover - make this an async generator
            yield TextDeltaEvent(delta="")
        raise exc

    return _fake_stream


@pytest.mark.asyncio
async def test_failed_model_call_clears_busy_and_surfaces_error(monkeypatch):
    """A timed-out model turn TERMINATES + surfaces an error + clears the turn.

    Reproduces the live-down shape:
      * the turn task is REGISTERED as a detached live turn (as the handler
        does when a socket drops mid-turn), so ``inflight_turn_count()`` would
        stay pinned if the turn never finished;
      * the model stream RAISES a ReadTimeoutError (the bounded-client outcome);
      * we assert (a) an LLM_UNAVAILABLE error envelope reached the wire, and
        (b) AFTER the turn task completes, ``inflight_turn_count() == 0``
        -- the failed call did NOT wedge the loop."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_contracts import new_ulid

    settings = agent_server.GeminiSettings(
        model="bedrock", project="test", location="us-west-2", use_vertex=False
    )
    state = SessionState(session_id=new_ulid())
    sock = _FakeSocket()

    raising = _make_raising_stream(_read_timeout_error())

    # Sanity precondition: no live turn before this one.
    assert agent_server.inflight_turn_count() == 0

    with patch.object(agent_server, "stream_events_with_contents", raising), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "build_client", return_value=None):
        # Launch the turn as a TASK and register it as a detached live turn --
        # this is the exact path that pins the registry if the turn never completes.
        task = asyncio.ensure_future(
            agent_server._stream_gemini_reply(
                sock, state, settings, "switch to Nova and run it", "research"
            )
        )
        agent_server._register_live_turn(
            state.session_id, "_test_turn", task, None
        )
        # While running, the detached turn is in flight.
        assert agent_server.inflight_turn_count() >= 1

        await task  # _stream_gemini_reply swallows the error internally
        # Let the task's done-callback (_drop) run so the registry empties.
        await asyncio.sleep(0)

    # (a) An honest error envelope reached the wire (not a silent death).
    import json

    error_frames = [
        json.loads(m) for m in sock.sent if '"type": "error"' in m or "error" in m
    ]
    llm_errors = [
        f
        for f in error_frames
        if f.get("payload", {}).get("error_code") == "LLM_UNAVAILABLE"
    ]
    assert llm_errors, f"no LLM_UNAVAILABLE error envelope on wire: {sock.sent}"

    # (b) THE LOAD-BEARING ASSERT: the failed model call did NOT pin the turn.
    assert task.done()
    assert agent_server.inflight_turn_count() == 0, (
        "detached turn entry NOT cleared after a failed model call "
        "(the live-down wedge)"
    )


@pytest.mark.asyncio
async def test_normal_turn_path_unaffected(monkeypatch):
    """A healthy text turn still completes + clears busy (no regression)."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_contracts import new_ulid

    settings = agent_server.GeminiSettings(
        model="bedrock", project="test", location="us-west-2", use_vertex=False
    )
    state = SessionState(session_id=new_ulid())
    sock = _FakeSocket()

    ok_stream = _make_text_stream("All ", "set.")

    with patch.object(agent_server, "stream_events_with_contents", ok_stream), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "build_client", return_value=None):
        task = asyncio.ensure_future(
            agent_server._stream_gemini_reply(
                sock, state, settings, "hello", "research"
            )
        )
        agent_server._register_live_turn(
            state.session_id, "_test_turn_ok", task, None
        )
        await task
        await asyncio.sleep(0)

    import json

    chunks = [json.loads(m) for m in sock.sent if "agent-message-chunk" in m]
    text = "".join(
        c["payload"]["delta"] for c in chunks if c["payload"].get("delta")
    )
    assert "All set." in text
    # No error envelope on the happy path.
    assert not any('"error_code": "LLM_UNAVAILABLE"' in m for m in sock.sent)
    # Turn registry cleared after a normal turn too.
    assert agent_server.inflight_turn_count() == 0


@pytest.mark.asyncio
async def test_solve_tool_path_unaffected_by_model_bound(monkeypatch):
    """A tool-bearing turn dispatches the tool + completes cleanly.

    Confirms the LLM read_timeout bound does NOT reach into the tool
    dispatch path: a turn that calls a tool then narrates runs to a clean
    terminal with the live-turn registry cleared."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapter import FunctionCallEvent
    from trid3nt_contracts import new_ulid

    settings = agent_server.GeminiSettings(
        model="bedrock", project="test", location="us-west-2", use_vertex=False
    )
    state = SessionState(session_id=new_ulid())
    sock = _FakeSocket()

    # Turn 1 emits a function_call; turn 2 narrates and ends.
    turn_events = iter(
        [
            [FunctionCallEvent(name="geocode_location", call_id="c1", args={"query": "X"})],
            [TextDeltaEvent(delta="Done.")],
        ]
    )

    async def _fake_stream(*_args: Any, **_kwargs: Any):
        for ev in next(turn_events):
            yield ev

    dispatched: list[str] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatched.append(name)
        return {"name": "X", "bbox": [0, 0, 1, 1], "precision_class": "precise"}

    with patch.object(agent_server, "stream_events_with_contents", _fake_stream), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "build_client", return_value=None):
        task = asyncio.ensure_future(
            agent_server._stream_gemini_reply(
                sock, state, settings, "where is X", "research"
            )
        )
        agent_server._register_live_turn(
            state.session_id, "_test_turn_tool", task, None
        )
        await task
        await asyncio.sleep(0)

    assert dispatched == ["geocode_location"]
    # The tool turn ran to a clean terminal: the live-turn registry clears.
    assert agent_server.inflight_turn_count() == 0
