"""Upstream-provider discipline (LANE CORE, 2026-07-22 -- NATE hard rule:
never internalize an upstream failure).

Both adapters classify transient provider errors (429 / 5xx / timeouts /
connection drops / provider overload) as upstream, log the VERBATIM provider
error, retry with exponential backoff (env TRID3NT_PROVIDER_RETRIES /
TRID3NT_PROVIDER_BACKOFF_S, Retry-After honored), and on exhaustion raise the
typed ``adapter.UpstreamProviderError`` so the server ends the turn with an
honest provider-unavailable narration -- never a silent empty turn, never
recorded as an internal error. Non-transient provider errors (auth / bad
request) fail fast unchanged with their own class (``provider_request``).

Offline: mocked clients + a mock clock (captured sleep waits); no network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapter import (
    GeminiSettings,
    UpstreamProviderError,
    classify_provider_error_class,
    provider_backoff_wait,
    provider_retries,
)
from trid3nt_contracts import new_ulid


# ---------------------------------------------------------------------------
# Shared env / backoff seams
# ---------------------------------------------------------------------------


def test_provider_retries_env(monkeypatch):
    monkeypatch.delenv("TRID3NT_PROVIDER_RETRIES", raising=False)
    assert provider_retries() == 3  # the documented default
    monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "1")
    assert provider_retries() == 1


def test_backoff_is_exponential(monkeypatch):
    monkeypatch.setenv("TRID3NT_PROVIDER_BACKOFF_S", "2")
    assert provider_backoff_wait(0) == 2.0
    assert provider_backoff_wait(1) == 4.0
    assert provider_backoff_wait(2) == 8.0
    assert provider_backoff_wait(10, cap=60.0) == 60.0  # capped


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------


class TestOpenAIProviderDiscipline:
    def _req(self):
        import httpx

        return httpx.Request("POST", "http://localhost:11434/v1/chat/completions")

    def _resp(self, status: int, headers: dict | None = None):
        import httpx

        return httpx.Response(status, request=self._req(), headers=headers or {})

    def _client(self, side_effects):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=side_effects)
        return client

    @pytest.mark.asyncio
    async def test_transient_retry_then_success(self, monkeypatch):
        """transient -> retry -> success (the happy retry path)."""
        import openai
        import trid3nt_server.openai_adapter as oa

        monkeypatch.setattr(oa.asyncio, "sleep", AsyncMock())
        exc = openai.APIStatusError(
            "503 service unavailable", response=self._resp(503), body=None
        )
        sentinel = object()
        client = self._client([exc, sentinel])
        result = await oa._create_stream_with_retry(client, {"model": "m"})
        assert result is sentinel
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_exhaustion_raises_typed_upstream_error_with_verbatim(
        self, monkeypatch
    ):
        """Exhaustion -> UpstreamProviderError (typed, provider named,
        VERBATIM provider detail, honest attempt count)."""
        import openai
        import trid3nt_server.openai_adapter as oa

        monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "1")
        monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
        monkeypatch.setattr(oa.asyncio, "sleep", AsyncMock())
        verbatim = (
            "Upstream error from Nvidia: ResourceExhausted: Worker local "
            "total request limit reached (32/32)"
        )
        exc = openai.APIError(verbatim, self._req(), body=None)
        client = self._client([exc, exc, exc])
        with pytest.raises(UpstreamProviderError) as ei:
            await oa._create_stream_with_retry(client, {"model": "m"})
        err = ei.value
        assert err.error_class == "upstream_provider"
        assert verbatim in err.detail  # the provider's verbatim error survives
        assert err.attempts == 2  # 1 original + 1 retry
        assert "openrouter.ai" in err.provider
        assert err.__cause__ is exc
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_after_header_honored(self, monkeypatch):
        """A 429 with Retry-After waits exactly that long (mock clock)."""
        import openai
        import trid3nt_server.openai_adapter as oa

        waits: list[float] = []

        async def _sleep(seconds):
            waits.append(seconds)

        monkeypatch.setattr(oa.asyncio, "sleep", _sleep)
        exc = openai.RateLimitError(
            "429 too many requests",
            response=self._resp(429, headers={"retry-after": "7"}),
            body=None,
        )
        sentinel = object()
        client = self._client([exc, sentinel])
        result = await oa._create_stream_with_retry(client, {"model": "m"})
        assert result is sentinel
        assert waits == [7.0]  # the provider's Retry-After, not the schedule

    @pytest.mark.asyncio
    async def test_backoff_schedule_used_without_retry_after(self, monkeypatch):
        """No Retry-After -> the exponential schedule drives the waits."""
        import openai
        import trid3nt_server.openai_adapter as oa

        monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "2")
        monkeypatch.setenv("TRID3NT_PROVIDER_BACKOFF_S", "2")
        waits: list[float] = []

        async def _sleep(seconds):
            waits.append(seconds)

        monkeypatch.setattr(oa.asyncio, "sleep", _sleep)
        exc = openai.APIStatusError(
            "502 bad gateway", response=self._resp(502), body=None
        )
        sentinel = object()
        client = self._client([exc, exc, sentinel])
        result = await oa._create_stream_with_retry(client, {"model": "m"})
        assert result is sentinel
        assert waits == [2.0, 4.0]  # base * 2**attempt

    @pytest.mark.asyncio
    async def test_connection_error_is_transient(self, monkeypatch):
        """A connection drop / timeout retries (upstream, not internal)."""
        import openai
        import trid3nt_server.openai_adapter as oa

        monkeypatch.setattr(oa.asyncio, "sleep", AsyncMock())
        exc = openai.APIConnectionError(request=self._req())
        assert oa._is_transient_upstream(exc)
        sentinel = object()
        client = self._client([exc, sentinel])
        result = await oa._create_stream_with_retry(client, {"model": "m"})
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_non_transient_fails_fast_unchanged(self, monkeypatch):
        """Auth / bad-request errors propagate unchanged with ZERO retries."""
        import openai
        import trid3nt_server.openai_adapter as oa

        monkeypatch.setattr(oa.asyncio, "sleep", AsyncMock())
        exc = openai.AuthenticationError(
            "invalid api key", response=self._resp(401), body=None
        )
        client = self._client([exc, object()])
        with pytest.raises(openai.AuthenticationError):
            await oa._create_stream_with_retry(client, {"model": "m"})
        assert client.chat.completions.create.await_count == 1

    def test_error_class_classification(self):
        import openai

        req = self._req()
        assert (
            classify_provider_error_class(
                UpstreamProviderError("p", "boom", 3)
            )
            == "upstream_provider"
        )
        assert (
            classify_provider_error_class(
                openai.BadRequestError(
                    "bad", response=self._resp(400), body=None
                )
            )
            == "provider_request"
        )
        assert (
            classify_provider_error_class(
                openai.APIConnectionError(request=req)
            )
            == "upstream_provider"
        )
        assert classify_provider_error_class(RuntimeError("bug")) == "internal"


# ---------------------------------------------------------------------------
# Bedrock adapter
# ---------------------------------------------------------------------------


def _client_error(code: str, status: int = 400):
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} verbatim message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "ConverseStream",
    )


class TestBedrockProviderDiscipline:
    def test_transient_classification(self):
        from botocore.exceptions import ReadTimeoutError
        from trid3nt_server.bedrock_adapter import _is_transient_bedrock_error

        assert _is_transient_bedrock_error(_client_error("ThrottlingException", 429))
        assert _is_transient_bedrock_error(
            _client_error("ServiceUnavailableException", 503)
        )
        assert _is_transient_bedrock_error(_client_error("SomeNewError", 502))
        assert _is_transient_bedrock_error(
            ReadTimeoutError(endpoint_url="https://bedrock")
        )
        # Non-transient request rejections:
        assert not _is_transient_bedrock_error(
            _client_error("ValidationException", 400)
        )
        assert not _is_transient_bedrock_error(
            _client_error("AccessDeniedException", 403)
        )

    def test_transient_retry_then_success(self, monkeypatch):
        from trid3nt_server.bedrock_adapter import _converse_stream_with_retry

        monkeypatch.setattr(time, "sleep", lambda _s: None)
        sentinel = {"stream": []}
        client = MagicMock()
        client.converse_stream = MagicMock(
            side_effect=[_client_error("ThrottlingException", 429), sentinel]
        )
        result = _converse_stream_with_retry(client, {"modelId": "m"})
        assert result is sentinel
        assert client.converse_stream.call_count == 2

    def test_exhaustion_raises_typed_upstream_error(self, monkeypatch):
        from trid3nt_server.bedrock_adapter import _converse_stream_with_retry

        monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "1")
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        exc = _client_error("ThrottlingException", 429)
        client = MagicMock()
        client.converse_stream = MagicMock(side_effect=[exc, exc, exc])
        with pytest.raises(UpstreamProviderError) as ei:
            _converse_stream_with_retry(client, {"modelId": "m"})
        err = ei.value
        assert err.provider == "AWS Bedrock"
        assert err.attempts == 2
        assert "ThrottlingException verbatim message" in err.detail
        assert client.converse_stream.call_count == 2

    def test_non_transient_fails_fast_unchanged(self, monkeypatch):
        from botocore.exceptions import ClientError
        from trid3nt_server.bedrock_adapter import _converse_stream_with_retry

        monkeypatch.setattr(time, "sleep", lambda _s: None)
        client = MagicMock()
        client.converse_stream = MagicMock(
            side_effect=_client_error("ValidationException", 400)
        )
        with pytest.raises(ClientError):
            _converse_stream_with_retry(client, {"modelId": "m"})
        assert client.converse_stream.call_count == 1

    def test_bedrock_error_class_classification(self):
        assert (
            classify_provider_error_class(_client_error("ThrottlingException", 429))
            == "upstream_provider"
        )
        assert (
            classify_provider_error_class(_client_error("ValidationException", 400))
            == "provider_request"
        )


# ---------------------------------------------------------------------------
# Server turn loop: exhaustion -> honest narration + error_class
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


@pytest.mark.asyncio
async def test_exhaustion_ends_turn_with_honest_narration_and_error_class():
    """UpstreamProviderError from the adapter -> the user gets a typed,
    provider-NAMED narration + a retryable error envelope; the per-turn record
    carries error_class="upstream_provider"; the turn is never silent and
    never recorded as internal."""

    async def _dead_provider(*_a, **_k):
        raise UpstreamProviderError(
            "test-provider (openai-compatible)", "worker pool saturated (32/32)", 4
        )
        yield  # pragma: no cover -- makes this an async generator

    turn_records: list[dict] = []

    def _capture_turn(**kw):
        turn_records.append(kw)
        return kw

    persisted: list[dict] = []

    async def _capture_persist(_state, **kw):
        persisted.append(kw)

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "stream_events_with_contents", _dead_provider), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "emit_turn_telemetry", _capture_turn), \
         patch.object(agent_server, "_persist_chat_turn", side_effect=_capture_persist), \
         patch.object(agent_server, "_persist_terminal_failure_card", new=AsyncMock()) as card:
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "run a flood sim", "research"
        )

    # (1) Honest in-chat narration: names the provider, carries the verbatim
    # detail, and terminates its bubble (done=True) -- never a silent turn.
    chunks = [m for m in sock.sent if m.get("type") == "agent-message-chunk"]
    narration = "".join(c["payload"].get("delta", "") for c in chunks)
    assert "test-provider (openai-compatible)" in narration
    assert "worker pool saturated (32/32)" in narration
    assert any(c["payload"].get("done") for c in chunks)

    # (2) Typed wire error: contract-valid LLM_UNAVAILABLE, retryable, naming
    # the provider in the message.
    errors = [m for m in sock.sent if m.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["error_code"] == "LLM_UNAVAILABLE"
    assert "test-provider" in errors[0]["payload"]["message"]
    assert errors[0]["payload"]["retryable"] is True

    # (3) The narration persists as an agent row (Case reopen replays it).
    agent_rows = [p for p in persisted if p.get("role") == "agent"]
    assert any("test-provider" in p.get("content", "") for p in agent_rows)

    # (4) The failure card carries the DISTINCT free-form code.
    assert card.await_count == 1
    assert card.await_args.kwargs["error_code"] == "UPSTREAM_PROVIDER_UNAVAILABLE"

    # (5) Turn telemetry: upstream_provider -- NEVER internal.
    assert len(turn_records) == 1
    assert turn_records[0]["error_class"] == "upstream_provider"
