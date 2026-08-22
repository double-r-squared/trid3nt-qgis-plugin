"""Anthropic Messages API adapter (MODEL_PROVIDER=anthropic).

Offline: a mocked ``AsyncAnthropic`` client, no network and no API key. Covers
the request shape (adaptive thinking, no sampling params, mandatory cache
breakpoints), the genai<->Messages conversion, the StreamEvent mapping
including the cache-hit proof, upstream-provider discipline, and the dispatch
seam.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from google.genai import types as genai_types

import anthropic

from trid3nt_server.adapters import anthropic_adapter as aa
from trid3nt_server.adapters.adapter import (
    FunctionCallEvent,
    TextDeltaEvent,
    UpstreamProviderError,
    UsageMetadataEvent,
    classify_provider_error_class,
)


# --------------------------------------------------------------------------- #
# Fixtures / doubles
# --------------------------------------------------------------------------- #


def _decl(name: str, description: str) -> genai_types.FunctionDeclaration:
    return genai_types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=genai_types.Schema(
            type="OBJECT",
            properties={
                "bbox": genai_types.Schema(type="STRING", description="AOI bbox"),
                "count": genai_types.Schema(type="INTEGER"),
            },
            required=["bbox"],
        ),
    )


def _user(text: str) -> genai_types.Content:
    return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])


class _FakeStream:
    """Async-iterable stand-in for the SDK's MessageStream."""

    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    def __aiter__(self) -> "_FakeStream":
        self._it = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:  # noqa: PERF203 -- protocol conversion
            raise StopAsyncIteration from None

    async def get_final_message(self) -> Any:
        return self._final


class _FakeMessages:
    def __init__(self, stream_factory: Any) -> None:
        self._stream_factory = stream_factory
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._stream_factory(len(self.calls))


class _FakeClient:
    def __init__(self, stream_factory: Any) -> None:
        self.messages = _FakeMessages(stream_factory)


def _text_event(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def _final_message(
    *,
    tool_use: tuple[str, dict[str, Any]] | None = None,
    cache_read: int = 0,
    stop_reason: str = "end_turn",
) -> Any:
    content: list[Any] = [SimpleNamespace(type="text", text="hello")]
    if tool_use is not None:
        content.append(
            SimpleNamespace(
                type="tool_use", id="toolu_abc", name=tool_use[0], input=tool_use[1]
            )
        )
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0 if cache_read else 900,
    )
    return SimpleNamespace(
        content=content, usage=usage, stop_reason=stop_reason, stop_details=None
    )


def _install_client(monkeypatch, stream_factory) -> _FakeClient:
    client = _FakeClient(stream_factory)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **k: client)
    return client


def _status_error(cls, status: int, headers: dict[str, str] | None = None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls("boom", response=response, body=None)


# --------------------------------------------------------------------------- #
# Model + key resolution
# --------------------------------------------------------------------------- #


def test_default_model_is_sonnet_5(monkeypatch):
    monkeypatch.delenv("TRID3NT_ANTHROPIC_MODEL", raising=False)
    assert aa.anthropic_model() == "claude-sonnet-5"
    assert aa.ANTHROPIC_DEFAULT_MODEL == "claude-sonnet-5"


def test_env_override_and_session_model(monkeypatch):
    monkeypatch.setenv("TRID3NT_ANTHROPIC_MODEL", "claude-opus-5")
    assert aa.anthropic_model() == "claude-opus-5"
    # A Claude id from the client wins.
    assert aa.anthropic_model("claude-haiku-4-5") == "claude-haiku-4-5"
    # A foreign-provider id is ignored (sending it would be a 404).
    assert aa.anthropic_model("us.anthropic.claude-sonnet-4-6") == "claude-opus-5"
    assert aa.anthropic_model("qwen3:8b") == "claude-opus-5"


def test_missing_api_key_raises_honestly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        aa.anthropic_api_key()


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def test_request_uses_adaptive_thinking_and_no_sampling_params(monkeypatch):
    monkeypatch.delenv("TRID3NT_ANTHROPIC_MODEL", raising=False)
    kwargs = aa._build_message_kwargs([_user("hi")], [_decl("fetch_dem", "d")], "SYS", None)
    assert kwargs["thinking"] == {"type": "adaptive"}
    # budget_tokens and every sampling param are 400s on this model family.
    assert "budget_tokens" not in json.dumps(kwargs)
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["max_tokens"] > 0


def test_cache_breakpoints_on_tools_and_system():
    kwargs = aa._build_message_kwargs(
        [_user("hi")], [_decl("a", "d1"), _decl("b", "d2")], "SYS", None
    )
    tools = kwargs["tools"]
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[0]
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Max 4 breakpoints per request; this path spends exactly 2.
    assert json.dumps(kwargs).count('"cache_control"') == 2


def test_no_system_block_when_prompt_absent():
    kwargs = aa._build_message_kwargs([_user("hi")], None, None, None)
    assert "system" not in kwargs
    assert "tools" not in kwargs


def test_tool_descriptions_are_not_truncated():
    long_doc = "x" * 4000
    tools = aa.tool_declarations_to_anthropic_tools([_decl("fetch_dem", long_doc)])
    assert tools[0]["description"] == long_doc
    schema = tools[0]["input_schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["required"] == ["bbox"]


# --------------------------------------------------------------------------- #
# History conversion
# --------------------------------------------------------------------------- #


def test_tool_use_and_result_ids_pair_and_coalesce():
    contents = [
        _user("model a flood"),
        genai_types.Content(
            role="model",
            parts=[
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        name="fetch_dem", args={"bbox": "1,2,3,4"}
                    )
                )
            ],
        ),
        genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name="fetch_dem", response={"status": "ok"}
                    )
                )
            ],
        ),
        genai_types.Content(
            role="user",
            parts=[genai_types.Part(text="thanks")],
        ),
    ]
    messages = aa.contents_to_anthropic_messages(contents)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    use = messages[1]["content"][0]
    result = messages[2]["content"][0]
    assert use["type"] == "tool_use"
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == use["id"]
    assert json.loads(result["content"])["status"] == "ok"
    # The trailing text Content coalesced into the same user message.
    assert messages[2]["content"][1] == {"type": "text", "text": "thanks"}


def test_messages_start_with_user_and_drop_empty_text():
    contents = [
        genai_types.Content(role="model", parts=[genai_types.Part(text="orphan")]),
        _user("   "),
        _user("real"),
    ]
    messages = aa.contents_to_anthropic_messages(contents)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == [{"type": "text", "text": "real"}]


def test_empty_history_yields_synthetic_user_message():
    messages = aa.contents_to_anthropic_messages([])
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "(context)"}]}]


# --------------------------------------------------------------------------- #
# Streaming -> StreamEvent union
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_yields_text_tool_call_and_usage(monkeypatch):
    final = _final_message(tool_use=("fetch_dem", {"bbox": "1,2,3,4"}), cache_read=0)
    _install_client(
        monkeypatch,
        lambda _n: _FakeStream([_text_event("he"), _text_event("llo")], final),
    )
    events = [
        ev
        async for ev in aa.stream_anthropic([_user("hi")], [_decl("fetch_dem", "d")], "SYS")
    ]
    assert [e.delta for e in events if isinstance(e, TextDeltaEvent)] == ["he", "llo"]
    calls = [e for e in events if isinstance(e, FunctionCallEvent)]
    assert len(calls) == 1
    assert calls[0].name == "fetch_dem"
    assert calls[0].args == {"bbox": "1,2,3,4"}
    assert calls[0].call_id == "toolu_abc"
    usage = [e for e in events if isinstance(e, UsageMetadataEvent)][0]
    assert usage.prompt_token_count == 100
    assert usage.candidates_token_count == 20
    assert usage.cache_hit is False


@pytest.mark.asyncio
async def test_cache_read_tokens_surface_as_cache_hit(monkeypatch):
    final = _final_message(cache_read=8400)
    _install_client(monkeypatch, lambda _n: _FakeStream([_text_event("ok")], final))
    events = [ev async for ev in aa.stream_anthropic([_user("hi")], None, "SYS")]
    usage = [e for e in events if isinstance(e, UsageMetadataEvent)][0]
    assert usage.cached_content_token_count == 8400
    assert usage.cache_hit is True


@pytest.mark.asyncio
async def test_string_tool_input_is_json_parsed(monkeypatch):
    final = _final_message(tool_use=("fetch_dem", {}))
    final.content[-1].input = '{"bbox": "1,2,3,4"}'
    _install_client(monkeypatch, lambda _n: _FakeStream([], final))
    events = [ev async for ev in aa.stream_anthropic([_user("hi")], None, None)]
    call = [e for e in events if isinstance(e, FunctionCallEvent)][0]
    assert call.args == {"bbox": "1,2,3,4"}


@pytest.mark.asyncio
async def test_refusal_is_narrated_honestly(monkeypatch):
    final = _final_message(stop_reason="refusal")
    final.stop_details = SimpleNamespace(category="cyber", explanation=None)
    _install_client(monkeypatch, lambda _n: _FakeStream([], final))
    events = [ev async for ev in aa.stream_anthropic([_user("hi")], None, None)]
    texts = [e.delta for e in events if isinstance(e, TextDeltaEvent)]
    assert any("declined" in t and "cyber" in t for t in texts)


# --------------------------------------------------------------------------- #
# Upstream-provider discipline
# --------------------------------------------------------------------------- #


def test_transient_classification():
    assert aa._is_transient_anthropic_error(_status_error(anthropic.RateLimitError, 429))
    assert aa._is_transient_anthropic_error(
        _status_error(anthropic.InternalServerError, 500)
    )
    assert not aa._is_transient_anthropic_error(
        _status_error(anthropic.BadRequestError, 400)
    )
    assert not aa._is_transient_anthropic_error(
        _status_error(anthropic.AuthenticationError, 401)
    )


@pytest.mark.asyncio
async def test_transient_error_retries_then_raises_typed(monkeypatch):
    monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "2")
    monkeypatch.setenv("TRID3NT_PROVIDER_BACKOFF_S", "0.001")
    attempts: list[int] = []

    def _factory(n: int):
        attempts.append(n)
        raise _status_error(anthropic.InternalServerError, 503)

    _install_client(monkeypatch, _factory)
    with pytest.raises(UpstreamProviderError) as excinfo:
        async for _ in aa.stream_anthropic([_user("hi")], None, None):
            pass
    assert excinfo.value.attempts == 3
    assert excinfo.value.provider == "Anthropic API"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_transient_error_recovers_on_retry(monkeypatch):
    monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "2")
    monkeypatch.setenv("TRID3NT_PROVIDER_BACKOFF_S", "0.001")

    def _factory(n: int):
        if n == 1:
            raise _status_error(anthropic.RateLimitError, 429, {"retry-after": "0"})
        return _FakeStream([_text_event("recovered")], _final_message())

    _install_client(monkeypatch, _factory)
    events = [ev async for ev in aa.stream_anthropic([_user("hi")], None, None)]
    assert [e.delta for e in events if isinstance(e, TextDeltaEvent)] == ["recovered"]


@pytest.mark.asyncio
async def test_non_transient_error_fails_fast(monkeypatch):
    monkeypatch.setenv("TRID3NT_PROVIDER_RETRIES", "3")
    calls: list[int] = []

    def _factory(n: int):
        calls.append(n)
        raise _status_error(anthropic.BadRequestError, 400)

    _install_client(monkeypatch, _factory)
    with pytest.raises(anthropic.BadRequestError):
        async for _ in aa.stream_anthropic([_user("hi")], None, None):
            pass
    assert calls == [1]  # no retry


def test_error_class_telemetry():
    assert (
        classify_provider_error_class(_status_error(anthropic.RateLimitError, 429))
        == "upstream_provider"
    )
    assert (
        classify_provider_error_class(_status_error(anthropic.BadRequestError, 400))
        == "provider_request"
    )


# --------------------------------------------------------------------------- #
# Dispatch seam
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_routes_to_anthropic(monkeypatch):
    from trid3nt_server.adapters import adapter as ad

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    _install_client(monkeypatch, lambda _n: _FakeStream([_text_event("routed")], _final_message()))
    events = [
        ev
        async for ev in ad.stream_events_with_contents(
            client=None, model="ignored", contents=[_user("hi")], system_prompt="SYS"
        )
    ]
    assert any(isinstance(e, TextDeltaEvent) and e.delta == "routed" for e in events)


def test_selected_model_passes_through_on_anthropic(monkeypatch):
    from trid3nt_server.adapters import bedrock_adapter as ba

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    assert ba.resolve_selected_model("claude-opus-5") == ("claude-opus-5", None)
    assert ba.resolve_selected_model("local-default") == (None, None)
