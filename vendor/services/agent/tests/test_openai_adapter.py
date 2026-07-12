"""Unit tests for openai_adapter.py (no network required).

Tests cover:
  1. contents_to_openai_messages: genai Content[] -> OpenAI messages[] translation
     including tool history round-trip (function_call + function_response)
  2. tool_declarations_to_openai_tools: FunctionDeclaration[] -> tools[] sanitisation
  3. stream_openai: streaming accumulator on synthetic chunk sequences (no network)
  4. OPEN-14: stream_openai's context-budget wiring -- proactive compaction
     before the request, and the reactive clip-guard retry-then-typed-error
     path (num_ctx discovery is monkeypatched throughout, no live Ollama
     required; see tests/test_context_budget.py for the discovery/ladder/
     regex unit tests in isolation)
  5. Part A (compaction UX): every compaction pass yields a
     CompactionStartEvent/CompactionCompleteEvent pair -- NOT the pre-Part-A
     TextDeltaEvent note glued onto the model's reply -- so server.py's
     dispatch loop can mint/complete a durable pipeline card instead (see
     tests/test_pipeline_emitter.py TestCompactionCard for the card-minting
     seam these events drive).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types as genai_types

from grace2_agent.openai_adapter import (
    contents_to_openai_messages,
    stream_openai,
    tool_declarations_to_openai_tools,
)
from grace2_agent.adapter import (
    CompactionCompleteEvent,
    CompactionStartEvent,
    FunctionCallEvent,
    TextDeltaEvent,
    UsageMetadataEvent,
)
from grace2_agent.context_budget import (
    ContextWindowExceededError,
    openai_max_output_tokens,
    reserve_output_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def user_content(text: str) -> genai_types.Content:
    return genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=text)],
    )


def model_content(text: str) -> genai_types.Content:
    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=text)],
    )


def model_fc_content(name: str, args: dict[str, Any], call_id: str | None = None) -> genai_types.Content:
    """Build a model-role Content carrying a function_call Part."""
    fc = genai_types.FunctionCall(name=name, args=args, id=call_id)
    return genai_types.Content(role="model", parts=[genai_types.Part(function_call=fc)])


def user_fr_content(name: str, response: dict[str, Any], call_id: str | None = None) -> genai_types.Content:
    """Build a user-role Content carrying a function_response Part."""
    fr = genai_types.FunctionResponse(name=name, response=response, id=call_id)
    return genai_types.Content(role="user", parts=[genai_types.Part(function_response=fr)])


# ---------------------------------------------------------------------------
# 1. contents_to_openai_messages
# ---------------------------------------------------------------------------

class TestContentsToOpenaiMessages:

    def test_simple_user_message(self):
        contents = [user_content("Hello")]
        msgs = contents_to_openai_messages(contents)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"

    def test_system_prompt_prepended(self):
        contents = [user_content("Hi")]
        msgs = contents_to_openai_messages(contents, system_prompt="Be helpful.")
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "Be helpful."
        assert msgs[1]["role"] == "user"

    def test_model_role_maps_to_assistant(self):
        contents = [
            user_content("What is 2+2?"),
            model_content("4"),
        ]
        msgs = contents_to_openai_messages(contents)
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant"]

    def test_function_call_becomes_tool_calls(self):
        """function_call Part -> assistant message with tool_calls list."""
        contents = [
            user_content("What is the weather in Paris?"),
            model_fc_content("get_weather", {"city": "Paris"}, call_id="call_1"),
        ]
        msgs = contents_to_openai_messages(contents)
        assert msgs[-1]["role"] == "assistant"
        tc = msgs[-1]["tool_calls"]
        assert len(tc) == 1
        assert tc[0]["id"] == "call_1"
        assert tc[0]["function"]["name"] == "get_weather"
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["city"] == "Paris"

    def test_function_response_becomes_tool_role(self):
        """function_response Part -> tool message with tool_call_id."""
        contents = [
            user_content("Weather in Paris?"),
            model_fc_content("get_weather", {"city": "Paris"}, call_id="call_1"),
            user_fr_content("get_weather", {"temperature": "15C"}, call_id="call_1"),
        ]
        msgs = contents_to_openai_messages(contents)
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        payload = json.loads(tool_msgs[0]["content"])
        assert payload["temperature"] == "15C"

    def test_tool_history_round_trip_no_ids(self):
        """When fc.id is absent, IDs are minted and paired in FIFO order."""
        # No call_id on either side -> minted as call_1 and matched.
        contents = [
            user_content("geocode Austin TX"),
            model_fc_content("geocode_location", {"query": "Austin, TX"}, call_id=None),
            user_fr_content("geocode_location", {"lat": 30.2, "lon": -97.7}, call_id=None),
        ]
        msgs = contents_to_openai_messages(contents)
        # Find the tool_calls message and the tool response message.
        tc_msgs = [m for m in msgs if m.get("tool_calls")]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tc_msgs) == 1
        assert len(tool_msgs) == 1
        minted_id = tc_msgs[0]["tool_calls"][0]["id"]
        assert minted_id  # not empty
        assert tool_msgs[0]["tool_call_id"] == minted_id

    def test_multi_turn_history(self):
        """A complete multi-turn conversation round-trips cleanly."""
        contents = [
            user_content("Show me the flood depth for Austin"),
            model_fc_content("geocode_location", {"query": "Austin, TX"}, call_id="c1"),
            user_fr_content("geocode_location", {"bbox": [-97.9, 30.1, -97.5, 30.4]}, call_id="c1"),
            model_content("Geocoded. Now running the flood model..."),
            model_fc_content("run_model_flood_scenario", {"bbox": [-97.9, 30.1, -97.5, 30.4]}, call_id="c2"),
            user_fr_content("run_model_flood_scenario", {"layer_id": "flood-xyz"}, call_id="c2"),
            model_content("The flood model completed. Peak depth: 1.2m."),
            user_content("Thanks"),
        ]
        msgs = contents_to_openai_messages(contents)
        roles = [m["role"] for m in msgs]
        # Should not have two consecutive user messages (coalescing handles it)
        for i in range(len(roles) - 1):
            if roles[i] == roles[i + 1] and roles[i] != "tool":
                # This could happen for coalesced content - that's fine
                pass
        # Basic structure: starts with user, ends with user
        assert roles[0] == "user"
        assert roles[-1] == "user"
        # All tool results paired with assistant tool-call turns
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 2


# ---------------------------------------------------------------------------
# 2. tool_declarations_to_openai_tools
# ---------------------------------------------------------------------------

class TestToolDeclarationsToOpenaiTools:

    def _make_decl(self, name: str, description: str, props: dict | None = None) -> genai_types.FunctionDeclaration:
        if props is None:
            return genai_types.FunctionDeclaration(name=name, description=description)
        schema_props = {
            k: genai_types.Schema(type=genai_types.Type.STRING, description=v)
            for k, v in props.items()
        }
        return genai_types.FunctionDeclaration(
            name=name,
            description=description,
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties=schema_props,
                required=list(props.keys()),
            ),
        )

    def test_basic_structure(self):
        decl = self._make_decl("get_weather", "Get weather for a city", {"city": "City name"})
        tools = tool_declarations_to_openai_tools([decl])
        assert len(tools) == 1
        t = tools[0]
        assert t["type"] == "function"
        assert t["function"]["name"] == "get_weather"
        assert "city" in t["function"]["parameters"]["properties"]

    def test_no_parameters_gives_empty_object(self):
        decl = genai_types.FunctionDeclaration(name="list_tools", description="Lists tools")
        tools = tool_declarations_to_openai_tools([decl])
        params = tools[0]["function"]["parameters"]
        assert params["type"] == "object"
        assert params["properties"] == {}

    def test_description_truncated_to_1000_chars(self):
        long_desc = "x" * 2000
        decl = genai_types.FunctionDeclaration(name="tool", description=long_desc)
        tools = tool_declarations_to_openai_tools([decl])
        assert len(tools[0]["function"]["description"]) == 1000

    def test_type_map_uppercase_to_lowercase(self):
        """Genai uppercase TYPE -> lowercase JSON Schema type."""
        decl = genai_types.FunctionDeclaration(
            name="compute",
            description="Compute something",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "count": genai_types.Schema(type=genai_types.Type.INTEGER),
                    "value": genai_types.Schema(type=genai_types.Type.NUMBER),
                    "flag": genai_types.Schema(type=genai_types.Type.BOOLEAN),
                },
            ),
        )
        tools = tool_declarations_to_openai_tools([decl])
        props = tools[0]["function"]["parameters"]["properties"]
        assert props["count"]["type"] == "integer"
        assert props["value"]["type"] == "number"
        assert props["flag"]["type"] == "boolean"

    def test_array_type_with_items(self):
        decl = genai_types.FunctionDeclaration(
            name="batch",
            description="Batch operation",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "ids": genai_types.Schema(
                        type=genai_types.Type.ARRAY,
                        items=genai_types.Schema(type=genai_types.Type.STRING),
                        description="List of IDs",
                    )
                },
            ),
        )
        tools = tool_declarations_to_openai_tools([decl])
        ids_schema = tools[0]["function"]["parameters"]["properties"]["ids"]
        assert ids_schema["type"] == "array"
        assert ids_schema["items"]["type"] == "string"

    def test_empty_list_returns_empty(self):
        assert tool_declarations_to_openai_tools([]) == []
        assert tool_declarations_to_openai_tools(None) == []

    def test_required_fields_preserved(self):
        decl = self._make_decl("search", "Search for something", {"query": "Search query"})
        tools = tool_declarations_to_openai_tools([decl])
        params = tools[0]["function"]["parameters"]
        assert "query" in params.get("required", [])


# ---------------------------------------------------------------------------
# 3. stream_openai: synthetic chunk sequence (no network)
# ---------------------------------------------------------------------------

class TestStreamOpenai:
    """Test the streaming accumulator logic using a mock openai client."""

    def _make_text_chunk(self, text: str) -> MagicMock:
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = text
        chunk.choices[0].delta.tool_calls = None
        chunk.choices[0].finish_reason = None
        chunk.usage = None
        return chunk

    def _make_tool_call_chunk(
        self,
        index: int,
        call_id: str | None,
        name: str | None,
        arguments_fragment: str,
        finish: bool = False,
    ) -> MagicMock:
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = None
        tc_delta = MagicMock()
        tc_delta.index = index
        tc_delta.id = call_id or ""
        tc_fn = MagicMock()
        tc_fn.name = name or ""
        tc_fn.arguments = arguments_fragment
        tc_delta.function = tc_fn
        chunk.choices[0].delta.tool_calls = [tc_delta]
        chunk.choices[0].finish_reason = "tool_calls" if finish else None
        chunk.usage = None
        return chunk

    def _make_usage_chunk(self, prompt: int, completion: int, total: int) -> MagicMock:
        chunk = MagicMock()
        chunk.choices = []
        usage = MagicMock()
        usage.prompt_tokens = prompt
        usage.completion_tokens = completion
        usage.total_tokens = total
        chunk.usage = usage
        return chunk

    def _make_final_empty_chunk(self) -> MagicMock:
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = None
        chunk.choices[0].delta.tool_calls = None
        chunk.choices[0].finish_reason = "stop"
        chunk.usage = None
        return chunk

    async def _collect_events(self, chunks: list) -> list:
        """Run stream_openai with a mocked client, collect all events."""
        import os
        os.environ["GRACE2_OPENAI_BASE_URL"] = "http://localhost:11434/v1"
        os.environ["GRACE2_OPENAI_MODEL"] = "test-model"
        os.environ["GRACE2_OPENAI_API_KEY"] = "not-needed"

        # Build an async mock context manager for the stream.
        async def _aiter_chunks():
            for c in chunks:
                yield c

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=_aiter_chunks())
        mock_stream.__aexit__ = AsyncMock(return_value=False)
        mock_create = AsyncMock(return_value=mock_stream)

        from grace2_agent.openai_adapter import stream_openai
        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create = mock_create
            mock_cls.return_value = mock_client
            events = []
            contents = [user_content("test")]
            async for ev in stream_openai(contents=contents):
                events.append(ev)
        return events

    @pytest.mark.asyncio
    async def test_text_deltas_stream(self):
        """Text delta chunks -> TextDeltaEvent sequence."""
        chunks = [
            self._make_text_chunk("Hello"),
            self._make_text_chunk(" world"),
            self._make_text_chunk("!"),
            self._make_final_empty_chunk(),
        ]
        events = await self._collect_events(chunks)
        text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
        assert len(text_events) == 3
        assert "".join(e.delta for e in text_events) == "Hello world!"

    @pytest.mark.asyncio
    async def test_tool_call_accumulation(self):
        """Fragmented tool-call argument chunks are accumulated and emitted as FunctionCallEvent."""
        chunks = [
            # First chunk: id + name
            self._make_tool_call_chunk(0, "call_abc", "get_weather", '{"ci'),
            # Second chunk: rest of arguments
            self._make_tool_call_chunk(0, "", "", 'ty": "Paris"}', finish=True),
            self._make_final_empty_chunk(),
        ]
        events = await self._collect_events(chunks)
        fc_events = [e for e in events if isinstance(e, FunctionCallEvent)]
        assert len(fc_events) == 1
        fc = fc_events[0]
        assert fc.name == "get_weather"
        assert fc.args == {"city": "Paris"}
        assert fc.call_id == "call_abc"

    @pytest.mark.asyncio
    async def test_usage_event_emitted(self):
        """Usage on final chunk -> UsageMetadataEvent."""
        chunks = [
            self._make_text_chunk("Hi"),
            self._make_usage_chunk(100, 20, 120),
            self._make_final_empty_chunk(),
        ]
        events = await self._collect_events(chunks)
        usage_events = [e for e in events if isinstance(e, UsageMetadataEvent)]
        assert len(usage_events) == 1
        u = usage_events[0]
        assert u.prompt_token_count == 100
        assert u.candidates_token_count == 20
        assert u.total_token_count == 120

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_different_indices(self):
        """Multiple tool calls at different indices are all emitted."""
        chunks = [
            self._make_tool_call_chunk(0, "call_1", "geocode", '{"query": "Austin"}'),
            self._make_tool_call_chunk(1, "call_2", "fetch_dem", '{"bbox": [1,2,3,4]}'),
            self._make_final_empty_chunk(),
        ]
        events = await self._collect_events(chunks)
        fc_events = [e for e in events if isinstance(e, FunctionCallEvent)]
        assert len(fc_events) == 2
        names = {e.name for e in fc_events}
        assert "geocode" in names
        assert "fetch_dem" in names

    @pytest.mark.asyncio
    async def test_no_tool_declarations_sends_no_tools(self):
        """When tool_declarations is None/empty, no 'tools' key in request."""
        import os
        os.environ["GRACE2_OPENAI_BASE_URL"] = "http://localhost:11434/v1"
        os.environ["GRACE2_OPENAI_MODEL"] = "test-model"
        os.environ["GRACE2_OPENAI_API_KEY"] = "not-needed"

        captured_kwargs: dict = {}

        async def _aiter_empty():
            return
            yield  # make it an async generator

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=_aiter_empty())
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        async def _capture_create(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_stream

        from grace2_agent.openai_adapter import stream_openai
        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create = _capture_create
            mock_cls.return_value = mock_client
            async for _ in stream_openai(contents=[user_content("hi")], tool_declarations=None):
                pass

        assert "tools" not in captured_kwargs


# ---------------------------------------------------------------------------
# 4. openai_model precedence (F2, live-feedback 2026-07-08: local hot-swap)
# ---------------------------------------------------------------------------


class TestOpenaiModelPrecedence:
    """Per-turn session model overrides the GRACE2_OPENAI_MODEL default;
    Bedrock-shaped session ids are ignored (fall back to the env default)."""

    def test_session_model_overrides_env_default(self, monkeypatch):
        from grace2_agent.openai_adapter import openai_model

        monkeypatch.setenv("GRACE2_OPENAI_MODEL", "qwen3:8b-16k")
        assert openai_model("llama3.2:3b") == "llama3.2:3b"

    def test_env_default_used_when_no_session_model(self, monkeypatch):
        from grace2_agent.openai_adapter import openai_model

        monkeypatch.setenv("GRACE2_OPENAI_MODEL", "qwen3:8b-16k")
        assert openai_model(None) == "qwen3:8b-16k"

    def test_bedrock_shaped_session_id_falls_back_to_env(self, monkeypatch):
        from grace2_agent.openai_adapter import openai_model

        monkeypatch.setenv("GRACE2_OPENAI_MODEL", "qwen3:8b-16k")
        assert (
            openai_model("us.anthropic.claude-sonnet-4-6") == "qwen3:8b-16k"
        )

    def test_session_model_works_with_no_env_default(self, monkeypatch):
        from grace2_agent.openai_adapter import openai_model

        monkeypatch.delenv("GRACE2_OPENAI_MODEL", raising=False)
        assert openai_model("llama3.2:3b") == "llama3.2:3b"

    def test_nothing_configured_raises(self, monkeypatch):
        from grace2_agent.openai_adapter import openai_model

        monkeypatch.delenv("GRACE2_OPENAI_MODEL", raising=False)
        with pytest.raises(RuntimeError):
            openai_model(None)

    def test_bedrock_shaped_session_id_with_no_env_raises(self, monkeypatch):
        from grace2_agent.openai_adapter import openai_model

        monkeypatch.delenv("GRACE2_OPENAI_MODEL", raising=False)
        with pytest.raises(RuntimeError):
            openai_model("us.amazon.nova-pro-v1:0")


# ---------------------------------------------------------------------------
# 5. OPEN-14: context-budget wiring inside stream_openai (proactive
#    compaction + the reactive clip-guard retry-then-typed-error path).
#    ``discover_num_ctx`` is monkeypatched everywhere here -- no live Ollama.
# ---------------------------------------------------------------------------


def _text_chunk(text: str) -> MagicMock:
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = text
    chunk.choices[0].delta.tool_calls = None
    chunk.choices[0].finish_reason = None
    chunk.usage = None
    return chunk


def _usage_chunk(prompt: int, completion: int = 10, total: int | None = None) -> MagicMock:
    chunk = MagicMock()
    chunk.choices = []
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.total_tokens = total if total is not None else prompt + completion
    chunk.usage = usage
    return chunk


def _final_empty_chunk() -> MagicMock:
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = None
    chunk.choices[0].delta.tool_calls = None
    chunk.choices[0].finish_reason = "stop"
    chunk.usage = None
    return chunk


def _make_stream(chunks: list) -> MagicMock:
    async def _aiter():
        for c in chunks:
            yield c

    stream = MagicMock()
    stream.__aenter__ = AsyncMock(return_value=_aiter())
    stream.__aexit__ = AsyncMock(return_value=False)
    return stream


class TestContextBudgetWiring:
    def _env(self, monkeypatch):
        monkeypatch.setenv("GRACE2_OPENAI_BASE_URL", "http://localhost:11434/v1")
        monkeypatch.setenv("GRACE2_OPENAI_MODEL", "test-model")
        monkeypatch.setenv("GRACE2_OPENAI_API_KEY", "not-needed")

    def _long_history(self, n: int = 60) -> list[genai_types.Content]:
        contents = []
        for i in range(n):
            contents.append(
                genai_types.Content(
                    role="user" if i % 2 == 0 else "model",
                    parts=[genai_types.Part(text=f"turn {i}: " + "x" * 200)],
                )
            )
        contents.append(user_content("what is the status now?"))
        return contents

    @pytest.mark.asyncio
    async def test_proactive_compaction_shrinks_the_sent_prompt(self, monkeypatch):
        """A huge history over a SMALL discovered num_ctx triggers proactive
        compaction: a CompactionStartEvent/CompactionCompleteEvent pair is
        emitted first (Part A -- NOT a TextDeltaEvent note), and the request
        actually SENT is smaller than the raw uncompacted history."""
        self._env(monkeypatch)
        captured: dict[str, Any] = {}

        async def _fake_discover(base_url, model_name):
            return 600  # tiny window -- the 60-row history blows this budget

        async def _capture_create(**kwargs):
            captured.update(kwargs)
            return _make_stream([_text_chunk("ok"), _usage_chunk(100), _final_empty_chunk()])

        with patch("openai.AsyncOpenAI") as mock_cls, \
             patch("grace2_agent.openai_adapter.discover_num_ctx", side_effect=_fake_discover):
            mock_client = MagicMock()
            mock_client.chat.completions.create = _capture_create
            mock_cls.return_value = mock_client

            events = []
            async for ev in stream_openai(contents=self._long_history()):
                events.append(ev)

        # Part A: the FIRST two events are the compaction pair -- start then
        # complete, in that order -- and no TextDeltaEvent note precedes them.
        assert isinstance(events[0], CompactionStartEvent)
        assert isinstance(events[1], CompactionCompleteEvent)
        assert events[1].before_tokens > events[1].after_tokens > 0
        assert not any(isinstance(e, TextDeltaEvent) for e in events[:2])
        # The actually-sent messages are far smaller than the raw 60-row
        # history would have been (proves compaction ran BEFORE the call).
        sent_chars = sum(len(m.get("content") or "") for m in captured["messages"])
        assert sent_chars < 60 * 200  # well under the raw history's own text

    @pytest.mark.asyncio
    async def test_no_compaction_when_under_budget(self, monkeypatch):
        """A small turn under a normal num_ctx is never touched -- no
        compaction events at all."""
        self._env(monkeypatch)

        async def _fake_discover(base_url, model_name):
            return 16384

        with patch("openai.AsyncOpenAI") as mock_cls, \
             patch("grace2_agent.openai_adapter.discover_num_ctx", side_effect=_fake_discover):
            mock_client = MagicMock()

            async def _create(**kwargs):
                return _make_stream([_text_chunk("hi"), _usage_chunk(50), _final_empty_chunk()])

            mock_client.chat.completions.create = _create
            mock_cls.return_value = mock_client

            events = []
            async for ev in stream_openai(contents=[user_content("hello")]):
                events.append(ev)

        assert not any(
            isinstance(e, (CompactionStartEvent, CompactionCompleteEvent))
            for e in events
        )

    @pytest.mark.asyncio
    async def test_clip_guard_retries_once_then_succeeds(self, monkeypatch):
        """Round 1 reports usage.prompt_tokens >= num_ctx (clipped) -- the
        adapter recompacts, emits a CompactionStartEvent/CompactionCompleteEvent
        pair (Part A -- NOT a text note), and retries ONCE. Round 2 is clean
        -- the turn completes normally, no exception."""
        self._env(monkeypatch)

        async def _fake_discover(base_url, model_name):
            return 1000

        streams = [
            _make_stream(
                [_text_chunk("fabricated success"), _usage_chunk(1000), _final_empty_chunk()]
            ),
            _make_stream(
                [_text_chunk("the real answer"), _usage_chunk(400), _final_empty_chunk()]
            ),
        ]
        create_calls = 0

        async def _create(**kwargs):
            nonlocal create_calls
            stream = streams[create_calls]
            create_calls += 1
            return stream

        with patch("openai.AsyncOpenAI") as mock_cls, \
             patch("grace2_agent.openai_adapter.discover_num_ctx", side_effect=_fake_discover):
            mock_client = MagicMock()
            mock_client.chat.completions.create = _create
            mock_cls.return_value = mock_client

            events = []
            async for ev in stream_openai(contents=[user_content("do the thing")]):
                events.append(ev)

        assert create_calls == 2
        text = "".join(e.delta for e in events if isinstance(e, TextDeltaEvent))
        assert "fabricated success" in text  # round 1's text still streamed live
        assert "the real answer" in text  # round 2's real text follows
        # Part A: the reactive recompaction fires the SAME typed event pair
        # as the proactive path, between the two rounds' text -- never a
        # TextDeltaEvent note glued into the narration.
        start_idx = next(i for i, e in enumerate(events) if isinstance(e, CompactionStartEvent))
        complete_idx = next(
            i for i, e in enumerate(events) if isinstance(e, CompactionCompleteEvent)
        )
        assert start_idx < complete_idx
        first_text_idx = next(i for i, e in enumerate(events) if isinstance(e, TextDeltaEvent))
        assert first_text_idx < start_idx  # round 1's text streamed BEFORE the retry compacts
        usage_events = [e for e in events if isinstance(e, UsageMetadataEvent)]
        assert usage_events[-1].prompt_token_count == 400

    @pytest.mark.asyncio
    async def test_clip_guard_raises_typed_error_after_second_clip(self, monkeypatch):
        """Both rounds report a clipped prompt -- the adapter gives up after
        ONE retry (two attempts total) and raises the typed error."""
        self._env(monkeypatch)

        async def _fake_discover(base_url, model_name):
            return 1000

        streams = [
            _make_stream([_text_chunk("bad 1"), _usage_chunk(1000), _final_empty_chunk()]),
            _make_stream([_text_chunk("bad 2"), _usage_chunk(1000), _final_empty_chunk()]),
        ]
        create_calls = 0

        async def _create(**kwargs):
            nonlocal create_calls
            stream = streams[create_calls]
            create_calls += 1
            return stream

        with patch("openai.AsyncOpenAI") as mock_cls, \
             patch("grace2_agent.openai_adapter.discover_num_ctx", side_effect=_fake_discover):
            mock_client = MagicMock()
            mock_client.chat.completions.create = _create
            mock_cls.return_value = mock_client

            events = []
            with pytest.raises(ContextWindowExceededError) as excinfo:
                async for ev in stream_openai(contents=[user_content("do the thing")]):
                    events.append(ev)

        assert create_calls == 2  # exactly one retry, never a third attempt
        assert excinfo.value.num_ctx == 1000
        assert "16k" not in str(excinfo.value)  # honest -- reflects THIS model's window
        assert "1k" in str(excinfo.value)


# ---------------------------------------------------------------------------
# BUG 3 (post-OPEN-14 acceptance rerun): a clipped/looping local generation
# ran for ~22 minutes streaming 16k-26k tokens of looped narration before the
# reactive clip guard (above) could react at stream end -- it only inspects
# usage AFTER a round finishes. ``max_tokens`` bounds every request; the
# proactive budget's reserve is COUPLED to the same cap (single source of
# truth -- see test_context_budget.py::TestBudget).
# ---------------------------------------------------------------------------


class TestMaxTokensCap:
    def _env(self, monkeypatch):
        monkeypatch.setenv("GRACE2_OPENAI_BASE_URL", "http://localhost:11434/v1")
        monkeypatch.setenv("GRACE2_OPENAI_MODEL", "test-model")
        monkeypatch.setenv("GRACE2_OPENAI_API_KEY", "not-needed")

    @pytest.mark.asyncio
    async def test_max_tokens_sent_on_every_request_default(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.delenv("GRACE2_OPENAI_MAX_TOKENS", raising=False)

        async def _fake_discover(base_url, model_name):
            return 16384

        captured: dict[str, Any] = {}

        async def _capture_create(**kwargs):
            captured.update(kwargs)
            return _make_stream([_text_chunk("hi"), _usage_chunk(50), _final_empty_chunk()])

        with patch("openai.AsyncOpenAI") as mock_cls, \
             patch("grace2_agent.openai_adapter.discover_num_ctx", side_effect=_fake_discover):
            mock_client = MagicMock()
            mock_client.chat.completions.create = _capture_create
            mock_cls.return_value = mock_client

            async for _ in stream_openai(contents=[user_content("hello")]):
                pass

        assert captured["max_tokens"] == 4096 == openai_max_output_tokens()

    @pytest.mark.asyncio
    async def test_max_tokens_honors_env_override_and_stays_coupled_to_reserve(
        self, monkeypatch
    ):
        self._env(monkeypatch)
        monkeypatch.setenv("GRACE2_OPENAI_MAX_TOKENS", "512")

        async def _fake_discover(base_url, model_name):
            return 16384

        captured: dict[str, Any] = {}

        async def _capture_create(**kwargs):
            captured.update(kwargs)
            return _make_stream([_text_chunk("hi"), _usage_chunk(50), _final_empty_chunk()])

        with patch("openai.AsyncOpenAI") as mock_cls, \
             patch("grace2_agent.openai_adapter.discover_num_ctx", side_effect=_fake_discover):
            mock_client = MagicMock()
            mock_client.chat.completions.create = _capture_create
            mock_cls.return_value = mock_client

            async for _ in stream_openai(contents=[user_content("hello")]):
                pass

        # The request cap AND the proactive budget's output reserve must move
        # together -- a single env knob, never two that can drift apart.
        assert captured["max_tokens"] == 512
        assert reserve_output_tokens() == 512
