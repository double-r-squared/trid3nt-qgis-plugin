"""Parallel-call bundling regression guard (job-B10).

The verification audit (Q4) found that the multi-turn loop in
``server.py::_stream_gemini_reply`` already correctly accumulates ALL
function_call Parts emitted in a single Gemini stream chunk and dispatches
them all before re-streaming, bundling all of their function_response
Parts into the single follow-up content turn. These tests are a
REGRESSION GUARD against future refactors silently splitting parallel
calls across multiple turns (which would defeat Gemini 3's parallel
function-calling and bloat round-trip latency).

Coverage:

1. Three function_call Parts in one Gemini response → all 3 land in
   ``turn_function_calls`` → all 3 dispatch → 6 contents entries appended
   (3 function_call + 3 function_response) → ONE follow-up
   generate_content_stream call (not three).
2. IDs round-trip 1:1 between the harvested call ids and the
   function_response.id sent back to Gemini.
3. Mixed text + function_call Parts in the same chunk → text streamed as
   ``agent-message-chunk`` AND function_call dispatched (no part lost).
4. Parallel calls split across multiple chunks within the SAME stream
   (Gemini's wire shape — a single turn may stream multiple chunks before
   the producer terminates) are still bundled into one turn.
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
    TextDeltaEvent,
    stream_events_with_contents,
)
from grace2_contracts import new_ulid


# Re-use the chunk builders shape used by test_multi_turn_loop.py.
def _fake_part_function_call(name: str, args: dict, call_id: str):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    part = MagicMock()
    part.function_call = fn_call
    part.text = None
    part.thought_signature = None
    return part


def _fake_part_text(text: str):
    part = MagicMock()
    part.function_call = None
    part.text = text
    part.thought_signature = None
    return part


def _make_chunk(parts: list) -> MagicMock:
    content = MagicMock()
    content.parts = parts
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = None
    return chunk


@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# Test 1: producer accumulates parallel calls in one chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_yields_three_function_calls_in_one_chunk():
    """One Gemini chunk carrying 3 function_call Parts surfaces 3
    FunctionCallEvents — none dropped, order preserved."""
    chunk = _make_chunk(
        [
            _fake_part_function_call("fetch_dem", {"bbox": [-82, 26, -81, 27]}, "call-a"),
            _fake_part_function_call(
                "fetch_landcover", {"bbox": [-82, 26, -81, 27]}, "call-b"
            ),
            _fake_part_function_call(
                "fetch_river_geometry", {"bbox": [-82, 26, -81, 27]}, "call-c"
            ),
        ]
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([chunk])

    from google.genai import types as genai_types

    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="test")])
    ]
    events: list = []
    async for evt in stream_events_with_contents(fake_client, "gemini-3-pro", contents):
        events.append(evt)

    assert len(events) == 3
    assert all(isinstance(e, FunctionCallEvent) for e in events)
    names = [e.name for e in events]
    assert names == ["fetch_dem", "fetch_landcover", "fetch_river_geometry"]
    ids = [e.call_id for e in events]
    assert ids == ["call-a", "call-b", "call-c"]


@pytest.mark.asyncio
async def test_producer_yields_parallel_calls_across_chunks():
    """Parallel calls split across multiple chunks in the same stream are
    still surfaced — the producer drains every chunk before terminating."""
    chunk1 = _make_chunk(
        [_fake_part_function_call("fetch_dem", {"bbox": [0, 0, 1, 1]}, "a")]
    )
    chunk2 = _make_chunk(
        [
            _fake_part_function_call("fetch_landcover", {"bbox": [0, 0, 1, 1]}, "b"),
            _fake_part_function_call(
                "fetch_river_geometry", {"bbox": [0, 0, 1, 1]}, "c"
            ),
        ]
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([chunk1, chunk2])

    from google.genai import types as genai_types

    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="t")])
    ]
    events: list = []
    async for evt in stream_events_with_contents(fake_client, "gemini-3-pro", contents):
        events.append(evt)

    assert [e.name for e in events] == [
        "fetch_dem",
        "fetch_landcover",
        "fetch_river_geometry",
    ]


@pytest.mark.asyncio
async def test_producer_yields_mixed_text_and_function_calls():
    """A chunk carrying both text and function_call Parts surfaces BOTH —
    neither is dropped."""
    chunk = _make_chunk(
        [
            _fake_part_text("Fetching elevation..."),
            _fake_part_function_call("fetch_dem", {"bbox": [0, 0, 1, 1]}, "a"),
        ]
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([chunk])

    from google.genai import types as genai_types

    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="t")])
    ]
    events: list = []
    async for evt in stream_events_with_contents(fake_client, "gemini-3-pro", contents):
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
async def test_loop_dispatches_three_parallel_calls_in_one_turn():
    """Three parallel function_calls in one Gemini response → all three
    dispatch → all three function_response Parts land in the SAME
    follow-up contents list (one re-stream call, not three)."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    # Use 3 hot-set tools so ``validate_function_call`` doesn't reject them
    # before they reach the dispatch step. The goal is the bundling shape,
    # not the specific tools.
    turn1_chunk = _make_chunk(
        [
            _fake_part_function_call("fetch_dem", {"bbox": [0, 0, 1, 1]}, "id-dem"),
            _fake_part_function_call(
                "geocode_location", {"query": "Fort Myers"}, "id-geo"
            ),
            _fake_part_function_call(
                "fetch_nws_alerts_conus", {"bbox": [0, 0, 1, 1]}, "id-nws"
            ),
        ]
    )
    turn2_chunk = _make_chunk([_fake_part_text("All three datasets fetched.")])

    turn_iter = iter([iter([turn1_chunk]), iter([turn2_chunk])])

    captured_contents: list[list[Any]] = []

    def _capture(**kwargs):
        # Snapshot the per-Part kind so we can count function_call /
        # function_response entries after the loop completes.
        snapshot = []
        for c in kwargs["contents"]:
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
        return next(turn_iter)

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = _capture

    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        return {"layer_id": f"{name}-result", "ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-3-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), patch.object(
        agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
    ), patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Fetch DEM, landcover, and rivers for Fort Myers.",
            "research",
        )

    # All three tools dispatched in one go, in the order Gemini emitted them.
    assert [name for (name, _) in dispatch_log] == [
        "fetch_dem",
        "geocode_location",
        "fetch_nws_alerts_conus",
    ]

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
async def test_loop_dispatches_parallel_calls_split_across_chunks():
    """When Gemini's one turn streams across multiple chunks (the wire
    shape — chunks are token-level), all function_calls across all chunks
    in that ONE turn are still bundled into a single follow-up turn."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    # Turn 1: two chunks of function_calls (3 calls total).
    # Use hot-set tools so dispatch validation accepts them.
    turn1_chunk_1 = _make_chunk(
        [_fake_part_function_call("fetch_dem", {"bbox": [0, 0, 1, 1]}, "a")]
    )
    turn1_chunk_2 = _make_chunk(
        [
            _fake_part_function_call("geocode_location", {"query": "x"}, "b"),
            _fake_part_function_call(
                "fetch_nws_alerts_conus", {"bbox": [0, 0, 1, 1]}, "c"
            ),
        ]
    )
    turn2_chunk = _make_chunk([_fake_part_text("Done.")])

    turn_iter = iter([
        iter([turn1_chunk_1, turn1_chunk_2]),
        iter([turn2_chunk]),
    ])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    dispatch_log: list[str] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append(name)
        return {"ok": True, "tool": name}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-3-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), patch.object(
        agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
    ), patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Fetch 3 things.", "research"
        )

    # All three dispatched in a single bundle, despite arriving in two chunks.
    assert dispatch_log == ["fetch_dem", "geocode_location", "fetch_nws_alerts_conus"]
