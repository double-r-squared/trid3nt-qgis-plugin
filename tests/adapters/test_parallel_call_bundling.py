"""Parallel-call bundling: all calls in one turn, one follow-up stream.

Every ``function_call`` Part in a model turn - in one chunk or spread across
several within the same stream - is accumulated, all are dispatched, and their
responses ride ONE follow-up turn. Ids round-trip 1:1, and mixed text and call
Parts in one chunk both survive. Splitting them across turns is the regression."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.adapters.adapter import (
    FunctionCallEvent,
    ModelSettings,
    TextDeltaEvent,
    stream_events_with_contents,
)
from trid3nt_contracts import new_ulid


@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# Test 1: producer accumulates parallel calls in one chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_yields_three_function_calls_in_one_chunk(fake_llm):
    """One Gemini chunk carrying 3 function_call Parts surfaces 3
    FunctionCallEvents — none dropped, order preserved."""
    fake_llm.script([
        {
            "tool_calls": [
                {"name": "fetch_dem", "args": {"bbox": [-82, 26, -81, 27]}, "call_id": "call-a"},
                {"name": "fetch_landcover", "args": {"bbox": [-82, 26, -81, 27]}, "call_id": "call-b"},
                {"name": "fetch_river_geometry", "args": {"bbox": [-82, 26, -81, 27]}, "call_id": "call-c"},
            ]
        }
    ])

    from google.genai import types as genai_types

    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="test")])
    ]
    events: list = []
    async for evt in stream_events_with_contents(None, "gemini-3-pro", contents):
        events.append(evt)

    assert len(events) == 3
    assert all(isinstance(e, FunctionCallEvent) for e in events)
    names = [e.name for e in events]
    assert names == ["fetch_dem", "fetch_landcover", "fetch_river_geometry"]
    ids = [e.call_id for e in events]
    assert ids == ["call-a", "call-b", "call-c"]


@pytest.mark.asyncio
async def test_producer_yields_parallel_calls_across_chunks(fake_llm):
    """Parallel calls split across multiple chunks in the same stream are
    still surfaced — the producer drains every chunk before terminating."""
    # The old across-chunks distinction was a Vertex-wire artifact; all 3
    # calls belong to the SAME round, so they collapse into one turn dict.
    fake_llm.script([
        {
            "tool_calls": [
                {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "a"},
                {"name": "fetch_landcover", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "b"},
                {"name": "fetch_river_geometry", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "c"},
            ]
        }
    ])

    from google.genai import types as genai_types

    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="t")])
    ]
    events: list = []
    async for evt in stream_events_with_contents(None, "gemini-3-pro", contents):
        events.append(evt)

    assert [e.name for e in events] == [
        "fetch_dem",
        "fetch_landcover",
        "fetch_river_geometry",
    ]


@pytest.mark.asyncio
async def test_producer_yields_mixed_text_and_function_calls(fake_llm):
    """A chunk carrying both text and function_call Parts surfaces BOTH —
    neither is dropped."""
    fake_llm.script([
        {
            "text": "Fetching elevation...",
            "tool_call": {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "a"},
        }
    ])

    from google.genai import types as genai_types

    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="t")])
    ]
    events: list = []
    async for evt in stream_events_with_contents(None, "gemini-3-pro", contents):
        events.append(evt)

    assert len(events) == 2
    assert isinstance(events[0], TextDeltaEvent)
    assert events[0].delta == "Fetching elevation..."
    assert isinstance(events[1], FunctionCallEvent)
    assert events[1].name == "fetch_dem"


# ---------------------------------------------------------------------------
# Test 2: server loop dispatches all 3, bundles responses into ONE turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_dispatches_three_parallel_calls_in_one_turn(fake_llm):
    """Three parallel function_calls in one Gemini response → all three
    dispatch → all three function_response Parts land in the SAME
    follow-up contents list (one re-stream call, not three)."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    # Three registered tools; the goal is the bundling shape, not the
    # specific tools.
    turn1 = {
        "tool_calls": [
            {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "id-dem"},
            {"name": "geocode_location", "args": {"query": "Fort Myers"}, "call_id": "id-geo"},
            {"name": "fetch_nws_alerts_conus", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "id-nws"},
        ]
    }
    turn2 = {"text": "All three datasets fetched."}
    fake_llm.script([turn1, turn2])

    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        return {"layer_id": f"{name}-result", "ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = ModelSettings(
        model="gemini-3-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(
        agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
    ), patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, settings, "Fetch DEM, landcover, and rivers for Fort Myers.",
            "research",
        )

    # All three tools dispatched in one go, in the order Gemini emitted them.
    assert [name for (name, _) in dispatch_log] == [
        "fetch_dem",
        "geocode_location",
        "fetch_nws_alerts_conus",
    ]

    # Rebuild the per-turn (role, parts) snapshot from the recorded fake-provider
    # calls (replaces the old ``_capture`` kwargs snapshot).
    captured_contents: list[list[Any]] = []
    for call in fake_llm.calls:
        snapshot = []
        for c in call["contents"]:
            parts_view = []
            for p in c.parts:
                if getattr(p, "function_call", None) is not None and getattr(
                    p.function_call, "name", None
                ):
                    parts_view.append(("function_call", p.function_call.name, p.function_call.id))
                elif getattr(p, "function_response", None) is not None and getattr(
                    p.function_response, "name", None
                ):
                    parts_view.append(
                        ("function_response", p.function_response.name, p.function_response.id)
                    )
                elif getattr(p, "text", None):
                    parts_view.append(("text", p.text, None))
            snapshot.append((c.role, parts_view))
        captured_contents.append(snapshot)

    # Exactly TWO Gemini calls (turn 1 + turn 2 final narrative) — NOT four
    # (would-be split across three sub-turns).
    assert len(captured_contents) == 2, (
        f"parallel calls split across turns: {len(captured_contents)} streams"
    )

    # Turn 2's contents carry all THREE function_call + THREE function_response
    # Parts plus the original user text — bundled into ONE follow-up turn.
    turn2_kinds = [
        kind
        for (_role, parts) in captured_contents[1]
        for (kind, _name, _id) in parts
    ]
    assert turn2_kinds.count("function_call") == 3, (
        f"expected 3 function_calls in turn 2, got: {turn2_kinds}"
    )
    assert turn2_kinds.count("function_response") == 3, (
        f"expected 3 function_responses in turn 2, got: {turn2_kinds}"
    )

    # ID parity check: the function_response.id values match the
    # function_call.id values 1:1 (call-dem ↔ resp-dem, etc.).
    call_ids = [
        cid
        for (_role, parts) in captured_contents[1]
        for (kind, _name, cid) in parts
        if kind == "function_call"
    ]
    resp_ids = [
        cid
        for (_role, parts) in captured_contents[1]
        for (kind, _name, cid) in parts
        if kind == "function_response"
    ]
    assert call_ids == ["id-dem", "id-geo", "id-nws"]
    assert resp_ids == ["id-dem", "id-geo", "id-nws"], (
        f"function_response ids drifted from function_call ids: {resp_ids}"
    )

    # The narrative reached the wire.
    narrative_chunks = [
        json.loads(m) for m in sock.sent if "agent-message-chunk" in m
    ]
    text_seen = "".join(
        c["payload"].get("delta", "") for c in narrative_chunks
    )
    assert "three datasets" in text_seen.lower()


@pytest.mark.asyncio
async def test_loop_dispatches_parallel_calls_split_across_chunks(fake_llm):
    """When Gemini's one turn streams across multiple chunks (the wire
    shape — chunks are token-level), all function_calls across all chunks
    in that ONE turn are still bundled into a single follow-up turn."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    # Turn 1: 3 calls total, split across two chunks on the old Vertex wire --
    # that split was an artifact; they belong to the SAME round, so they
    # collapse into one turn dict. Use hot-set tools so dispatch validation
    # accepts them.
    turn1 = {
        "tool_calls": [
            {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "a"},
            {"name": "geocode_location", "args": {"query": "x"}, "call_id": "b"},
            {"name": "fetch_nws_alerts_conus", "args": {"bbox": [0, 0, 1, 1]}, "call_id": "c"},
        ]
    }
    turn2 = {"text": "Done."}
    fake_llm.script([turn1, turn2])

    dispatch_log: list[str] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append(name)
        return {"ok": True, "tool": name}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = ModelSettings(
        model="gemini-3-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(
        agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
    ), patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, settings, "Fetch 3 things.", "research"
        )

    # All three dispatched in a single bundle, despite arriving in two chunks.
    assert dispatch_log == ["fetch_dem", "geocode_location", "fetch_nws_alerts_conus"]
