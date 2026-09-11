"""One two-turn tool-call conversation, built once in the IR.

The turn builders produce ``Message``/``Part`` objects that carry no provider
type; each adapter converts the SAME conversation to its own wire shape, and
the call and response pair by id on both. The persisted ``parts_blob`` round
trip rebuilds the identical Parts, and no module under ``trid3nt_server``
imports a provider SDK for the IR.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from trid3nt_contracts.message import Message, Part, ToolCall, ToolDeclaration, ToolResponse

from trid3nt_server.adapters.adapter import (
    _decode_parts_blob,
    build_contents_from_history,
    build_function_call_content,
    build_function_response_content,
    encode_parts_blob,
)
from trid3nt_server.adapters.anthropic_adapter import (
    contents_to_anthropic_messages,
    tool_declarations_to_anthropic_tools,
)
from trid3nt_server.adapters.openai_adapter import (
    contents_to_openai_messages,
    tool_declarations_to_openai_tools,
)

_DECL = ToolDeclaration(
    name="geocode_location",
    description="Resolve a place name to a bbox.",
    schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


def _conversation() -> list[Message]:
    """A user ask, one tool call, its response, and a follow-up ask."""
    contents = build_contents_from_history("where is Fort Myers", [])
    contents.append(
        build_function_call_content("geocode_location", {"query": "Fort Myers, FL"}, "call-1")
    )
    contents.append(
        build_function_response_content(
            "geocode_location", {"status": "ok", "bbox": [1, 2, 3, 4]}, "call-1"
        )
    )
    contents.append(Message(role="model", parts=[Part(text="It is in Lee County.")]))
    contents.append(Message(role="user", parts=[Part(text="and the elevation?")]))
    return contents


def test_the_conversation_is_built_from_the_ir_alone() -> None:
    """Every turn is a Message of Parts, each one text, a call, or a response."""
    contents = _conversation()
    assert [c.role for c in contents] == ["user", "model", "user", "model", "user"]
    call = contents[1].parts[0].call
    response = contents[2].parts[0].response
    assert isinstance(call, ToolCall) and call.id == "call-1"
    assert isinstance(response, ToolResponse) and response.id == call.id
    assert response.result["bbox"] == [1, 2, 3, 4]


def test_openai_wire_shape() -> None:
    """The call becomes an assistant ``tool_calls`` entry and the response a
    ``tool`` message carrying the same ``tool_call_id``."""
    messages = contents_to_openai_messages(_conversation(), system_prompt="sys")
    assert messages[0]["role"] == "system"
    assistant = next(m for m in messages if m.get("tool_calls"))
    tool_call = assistant["tool_calls"][0]
    assert tool_call["id"] == "call-1"
    assert tool_call["function"]["name"] == "geocode_location"
    assert json.loads(tool_call["function"]["arguments"]) == {"query": "Fort Myers, FL"}
    result = next(m for m in messages if m.get("role") == "tool")
    assert result["tool_call_id"] == "call-1"
    assert json.loads(result["content"])["bbox"] == [1, 2, 3, 4]

    tools = tool_declarations_to_openai_tools([_DECL])
    assert tools[0]["function"]["name"] == "geocode_location"
    assert tools[0]["function"]["parameters"] == _DECL.schema


def test_anthropic_wire_shape() -> None:
    """The call becomes a ``tool_use`` block and the response a ``tool_result``
    block whose ``tool_use_id`` matches it."""
    messages = contents_to_anthropic_messages(_conversation())
    blocks = [b for m in messages for b in m["content"]]
    use = next(b for b in blocks if b["type"] == "tool_use")
    assert use["id"] == "call-1"
    assert use["name"] == "geocode_location"
    assert use["input"] == {"query": "Fort Myers, FL"}
    result = next(b for b in blocks if b["type"] == "tool_result")
    assert result["tool_use_id"] == "call-1"
    assert json.loads(result["content"])["bbox"] == [1, 2, 3, 4]

    tools = tool_declarations_to_anthropic_tools([_DECL])
    assert tools[0]["input_schema"] == _DECL.schema


def test_parts_blob_round_trips_the_call_and_the_response() -> None:
    """A persisted turn rebuilds the identical Parts, so a replayed
    conversation reaches the provider as the one it originally sent."""
    contents = _conversation()
    parts = contents[1].parts + contents[2].parts
    decoded = _decode_parts_blob(encode_parts_blob(parts))
    assert decoded == parts


def test_no_provider_sdk_is_imported_for_the_ir() -> None:
    """The server package builds a turn with the ``google.genai`` package
    absent from the interpreter."""
    root = Path(__file__).resolve().parents[2]
    probe = (
        "import builtins, sys\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name == 'google.genai' or name.startswith('google.genai.'):\n"
        "        raise ImportError('provider SDK reached the IR: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "from trid3nt_server.adapters.adapter import build_function_call_content\n"
        "import trid3nt_server.adapters.openai_adapter\n"
        "import trid3nt_server.adapters.anthropic_adapter\n"
        "import trid3nt_server.gates.context_budget\n"
        "assert build_function_call_content('t', {}, 'c').parts[0].call.name == 't'\n"
        "print('clean')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=root, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout
