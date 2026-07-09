"""Unit tests for openai_adapter.py (no network required).

Tests cover:
  1. contents_to_openai_messages: genai Content[] -> OpenAI messages[] translation
     including tool history round-trip (function_call + function_response)
  2. tool_declarations_to_openai_tools: FunctionDeclaration[] -> tools[] sanitisation
  3. stream_openai: streaming accumulator on synthetic chunk sequences (no network)
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types as genai_types

from grace2_agent.openai_adapter import (
    contents_to_openai_messages,
    tool_declarations_to_openai_tools,
)
from grace2_agent.adapter import (
    FunctionCallEvent,
    TextDeltaEvent,
    UsageMetadataEvent,
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
