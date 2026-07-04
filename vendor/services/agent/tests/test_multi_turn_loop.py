"""Multi-turn function_call → function_response loop tests (job-0169).

Verifies the fix for the CRITICAL BLOCKER reported by user testing 2026-06-08:
every multi-tool natural-language prompt failed because Gemini stopped after
the first tool call. The previous (job-0154) shape dispatched the first
function_call but never appended a ``function_response`` Content to the next
Gemini turn — so Gemini had no idea its first call had returned, and
generation stopped.

Tests:

1. ``summarize_tool_result`` produces sensible compact summaries for the
   common tool-return shapes (LayerURI metadata, dict with metrics, error,
   None, primitive, oversized payload).
2. ``stream_events_with_contents`` parses both text and function_call chunks
   the same way ``stream_events`` did (parity check — the existing routing
   tests already exercise ``stream_events`` which delegates here).
3. ``_stream_gemini_reply`` drives the multi-turn loop end-to-end:
   - First turn: Gemini emits a function_call (``geocode_location``).
   - Second turn: Gemini sees the function_response (with bbox) and emits
     another function_call (``fetch_wdpa_protected_areas`` with the bbox).
   - Third turn: Gemini emits a final narrative text and no function_call,
     so the loop terminates.
   - Asserts: both tools dispatched in order, ``contents`` passed to Gemini
     on the second + third turns contain the call + response pairs, terminal
     ``agent-message-chunk done=True`` and ``pipeline-state complete`` are
     emitted.
4. Tool dispatch error is captured in the function_response (not raised),
   so Gemini can read the error and retry / narrate.
5. ``MAX_TURN_ITERATIONS`` is enforced — a runaway Gemini that keeps emitting
   function_calls forever is fail-stopped, not infinite-looped.
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
    build_contents_from_history,
    build_function_call_content,
    build_function_response_content,
    stream_events_with_contents,
    summarize_tool_result,
)
from grace2_contracts import new_ulid


# ---------------------------------------------------------------------------
# Test 1: summarize_tool_result shape coverage
# ---------------------------------------------------------------------------


def test_summarize_tool_result_dict_with_metrics():
    """A dict result with metrics is preserved compactly."""
    result = {
        "wms_url": "https://qgis.example.com/wms?LAYERS=flood-fortmyers",
        "layer_id": "flood-fortmyers-100yr",
        "metrics": {"max_depth_m": 1.21, "mean_depth_m": 0.42, "area_km2": 18.3},
        "bbox": [-82.0, 26.5, -81.7, 26.8],
    }
    summary = summarize_tool_result("run_model_flood_scenario", result)
    assert summary["tool"] == "run_model_flood_scenario"
    assert summary["status"] == "ok"
    # Critical fields the LLM needs to narrate the answer survive the summary.
    assert summary["result"]["metrics"]["max_depth_m"] == 1.21
    assert "wms_url" in summary["result"]
    assert summary["result"]["bbox"] == [-82.0, 26.5, -81.7, 26.8]


def test_summarize_tool_result_error_path():
    """A dispatch error becomes an error-shaped summary, not a raise."""
    err = RuntimeError("dem fetch upstream 503")
    summary = summarize_tool_result("fetch_dem", None, error=err)
    assert summary["status"] == "error"
    assert "503" in summary["error"]
    assert summary["error_type"] == "RuntimeError"


def test_summarize_tool_result_none():
    """None result falls back to {status: "no_result"}.

    B-rev: TOOL_NOT_FOUND and PAYLOAD_WARNING_CANCELLED now raise typed
    exceptions, not return None. ``no_result`` is still the contract for
    tool callables that legitimately return None (e.g. side-effect-only
    tools). The fallback is retained — it no longer represents routing
    failures.
    """
    summary = summarize_tool_result("publish_layer", None)
    assert summary["status"] == "no_result"


def test_summarize_tool_result_primitive():
    """A primitive (string URL) result is wrapped in {result: ...}."""
    summary = summarize_tool_result("publish_layer", "https://qgis.example.com/wms?LAYERS=x")
    assert summary["status"] == "ok"
    assert "wms" in summary["result"]


def test_summarize_tool_result_oversized_clipped():
    """A megabyte-sized result is clipped to the char budget, not sent raw."""
    # 50 KB string — well over the 4 KB budget.
    big = "x" * 50_000
    summary = summarize_tool_result("fetch_buildings", {"geojson": big, "count": 1})
    encoded = json.dumps(summary)
    assert len(encoded) < 10_000, (
        f"summary not clipped: {len(encoded)} chars (budget = 4000)"
    )
    # The clip marker is present.
    assert "clipped" in encoded or "truncated" in encoded


def test_summarize_tool_result_list_truncated():
    """A long list is truncated to a few items plus a count marker."""
    result = {"items": list(range(100))}
    summary = summarize_tool_result("fetch_nws_alerts_conus", result)
    items = summary["result"]["items"]
    assert isinstance(items, list)
    # 5 items + 1 "more items" marker = 6.
    assert len(items) == 6
    assert "more items" in items[-1]


# ---------------------------------------------------------------------------
# Test 2: build_contents_from_history conversion
# ---------------------------------------------------------------------------


def test_build_contents_from_history_collapses_roles():
    """agent/assistant/model roles collapse to 'model'; empty text dropped."""
    history = [
        {"role": "user", "text": "Hello"},
        {"role": "agent", "text": "Hi there"},
        {"role": "assistant", "text": ""},  # dropped
        {"role": "user", "text": "What about Fort Myers?"},
    ]
    contents = build_contents_from_history("Now show me protected areas.", history)
    # 3 history entries (one dropped) + the new user_text.
    assert len(contents) == 4
    assert contents[0].role == "user"
    assert contents[1].role == "model"  # agent → model
    assert contents[2].role == "user"
    assert contents[3].role == "user"
    assert contents[-1].parts[0].text == "Now show me protected areas."


# ---------------------------------------------------------------------------
# Test 3: build_function_call_content / build_function_response_content
# ---------------------------------------------------------------------------


def test_build_function_call_and_response_content_pair():
    """The (function_call, function_response) Content pair is well-formed."""
    call_content = build_function_call_content(
        "geocode_location", {"query": "Fort Myers, FL"}, call_id="call-1"
    )
    assert call_content.role == "model"
    assert call_content.parts[0].function_call.name == "geocode_location"
    assert call_content.parts[0].function_call.args == {"query": "Fort Myers, FL"}

    resp_content = build_function_response_content(
        "geocode_location",
        {"tool": "geocode_location", "status": "ok", "result": {"bbox": [1, 2, 3, 4]}},
        call_id="call-1",
    )
    # google-genai accepts function responses under the "user" or "function"
    # role; we use "user" as it is the documented multi-turn shape.
    assert resp_content.role in ("user", "function")
    fr = resp_content.parts[0].function_response
    assert fr.name == "geocode_location"
    assert fr.response["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 4: stream_events_with_contents — parity with single-turn dispatch
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
async def test_stream_events_with_contents_yields_function_call():
    """stream_events_with_contents demultiplexes function_call parts."""
    fake_chunk = _make_fake_chunk_with_function_call(
        "geocode_location", {"query": "Fort Myers, FL"}, "call-1"
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([fake_chunk])

    contents = build_contents_from_history("Show me Fort Myers.", None)
    events: list = []
    async for evt in stream_events_with_contents(
        fake_client, "gemini-2.5-pro", contents
    ):
        events.append(evt)

    assert len(events) == 1
    assert isinstance(events[0], FunctionCallEvent)
    assert events[0].name == "geocode_location"
    assert events[0].args == {"query": "Fort Myers, FL"}


# ---------------------------------------------------------------------------
# Test 5: end-to-end multi-turn loop in _stream_gemini_reply
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    """Minimal WebSocket shim that records every ``send`` payload."""

    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 — protocol shim
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_stream_gemini_reply_multi_turn_loop():
    """Proper recovery flow: geocode → list_tools_in_category → wdpa → narrative.

    Scenario mirrors the kickoff: "Show me protected areas in Fort Myers".
    This test exercises the Option-A (Wave 4.10) recovery flow where Gemini
    must first call ``list_tools_in_category`` to open the conservation_ecology
    category before ``fetch_wdpa_protected_areas`` is reachable.

    Turn 1: Gemini calls ``geocode_location`` (always in HOT_SET).
    Turn 2: Gemini calls ``list_tools_in_category("conservation_ecology")``
            — opens the category and adds fetch_wdpa_protected_areas to the
            allowed set.
    Turn 3: Gemini calls ``fetch_wdpa_protected_areas`` with the bbox from
            the geocode result — now reachable because the category is open.
    Turn 4: Gemini emits a final narrative text and stops.
    """
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    # Four pre-canned Gemini turns.  Each turn returns one chunk.
    turn1_chunk = _make_fake_chunk_with_function_call(
        "geocode_location", {"query": "Fort Myers, FL"}, "call-geo"
    )
    turn2_chunk = _make_fake_chunk_with_function_call(
        "list_tools_in_category",
        {"category_id": "conservation_ecology"},
        "call-list",
    )
    turn3_chunk = _make_fake_chunk_with_function_call(
        "fetch_wdpa_protected_areas",
        {"bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-wdpa",
    )
    turn4_chunk = _make_fake_chunk_with_text(
        "Found 2 protected areas in Fort Myers — listing them now."
    )

    turn_responses = iter([
        iter([turn1_chunk]),
        iter([turn2_chunk]),
        iter([turn3_chunk]),
        iter([turn4_chunk]),
    ])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = (
        lambda **_: next(turn_responses)
    )

    # Capture all contents lists passed to generate_content_stream across
    # the four turns — this verifies the loop appends function_call +
    # function_response between turns.
    contents_per_turn: list[list[Any]] = []

    def _capture_and_stream(**kwargs):
        contents_per_turn.append([
            (c.role, [
                ("text", p.text) if p.text else (
                    "function_call", p.function_call.name
                ) if getattr(p, "function_call", None) else (
                    "function_response",
                    p.function_response.name,
                ) if getattr(p, "function_response", None) else ("unknown", None)
                for p in c.parts
            ])
            for c in kwargs["contents"]
        ])
        return next(turn_responses)

    fake_client.models.generate_content_stream.side_effect = _capture_and_stream

    # Stub the registry-dispatch helper so we can assert exactly which tools
    # were dispatched without bringing up GCS / Nominatim / WDPA.
    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        if name == "geocode_location":
            return {
                "name": "Fort Myers, FL",
                "bbox": [-82.0, 26.5, -81.7, 26.8],
                "precision_class": "precise",
            }
        if name == "list_tools_in_category":
            # Return the canonical shape so the server-side open_category
            # logic fires (server.py:731 checks result.get("category_id")).
            return {
                "category_id": args.get("category_id", "conservation_ecology"),
                "tools": [
                    {"name": "fetch_wdpa_protected_areas", "description_snippet": "Fetch WDPA areas."},
                ],
            }
        if name == "fetch_wdpa_protected_areas":
            return {
                "layer_id": "wdpa-fort-myers",
                "wms_url": "https://qgis.example.com/wms?LAYERS=wdpa-fort-myers",
                "feature_count": 2,
            }
        return None

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro",
        project="test",
        location="us-central1",
        use_vertex=True,
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Show me protected areas in Fort Myers", "research"
        )

    # All three tools were dispatched in the correct order.
    assert [name for (name, _) in dispatch_log] == [
        "geocode_location",
        "list_tools_in_category",
        "fetch_wdpa_protected_areas",
    ], f"unexpected dispatch order: {dispatch_log}"

    # The wdpa call received the bbox Gemini synthesized from the geocode
    # result — proving the function_response was fed back through.
    assert dispatch_log[2][1].get("bbox") == [-82.0, 26.5, -81.7, 26.8]

    # Four Gemini calls happened (one per turn).
    assert len(contents_per_turn) == 4

    # Turn 1 contents: only the user message.
    turn1_roles = [r for (r, _parts) in contents_per_turn[0]]
    assert turn1_roles == ["user"]

    # Turn 2 contents: user + (model function_call for geocode) + (function_response).
    turn2_kinds = [
        kind
        for (_role, parts) in contents_per_turn[1]
        for (kind, _name) in parts
    ]
    assert "function_call" in turn2_kinds, (
        f"turn 2 missing function_call in contents: {contents_per_turn[1]}"
    )
    assert "function_response" in turn2_kinds, (
        f"turn 2 missing function_response in contents: {contents_per_turn[1]}"
    )

    # Turn 3 contents: user + geocode pair + list_tools pair.
    turn3_call_names = [
        name
        for (_role, parts) in contents_per_turn[2]
        for (kind, name) in parts
        if kind == "function_call"
    ]
    assert turn3_call_names == ["geocode_location", "list_tools_in_category"], (
        f"turn 3 function_call sequence wrong: {turn3_call_names}"
    )

    # Turn 4 contents: user + all three call+response pairs.
    turn4_call_names = [
        name
        for (_role, parts) in contents_per_turn[3]
        for (kind, name) in parts
        if kind == "function_call"
    ]
    assert turn4_call_names == [
        "geocode_location",
        "list_tools_in_category",
        "fetch_wdpa_protected_areas",
    ], f"turn 4 function_call sequence wrong: {turn4_call_names}"

    # The terminal narrative text reached the wire as an agent-message-chunk.
    narrative_chunks = [
        json.loads(m)
        for m in sock.sent
        if "agent-message-chunk" in m
    ]
    text_seen = "".join(
        c["payload"]["delta"] for c in narrative_chunks if c["payload"].get("delta")
    )
    assert "protected areas" in text_seen.lower()

    # Terminal frame with done=True was sent.
    done_chunk = [
        c for c in narrative_chunks
        if c["payload"].get("done") is True
    ]
    assert done_chunk, "no terminal agent-message-chunk(done=True) emitted"

    # The outer pipeline-state(complete) was emitted.
    pipeline_frames = [
        json.loads(m) for m in sock.sent if "pipeline-state" in m
    ]
    last_pipeline = pipeline_frames[-1]
    assert last_pipeline["payload"]["steps"][0]["state"] == "complete"


@pytest.mark.asyncio
async def test_stream_gemini_reply_tool_error_does_not_kill_loop():
    """Dispatch error is summarized into the function_response; loop continues."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    turn1_chunk = _make_fake_chunk_with_function_call(
        "fetch_dem", {"bbox": [-82.0, 26.5, -81.7, 26.8]}, "call-dem"
    )
    turn2_chunk = _make_fake_chunk_with_text(
        "Couldn't load the DEM; trying a different region."
    )
    turn_iter = iter([iter([turn1_chunk]), iter([turn2_chunk])])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    async def _failing_invoke(_ws, _state, name, args):
        raise RuntimeError("DEM upstream 503 — temporary")

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_failing_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        # MUST NOT raise — the error becomes a function_response payload.
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Model flood depth at Fort Myers.", "research"
        )

    # The narrative second turn ran — proves the error was fed back, not raised.
    narrative_chunks = [
        json.loads(m) for m in sock.sent if "agent-message-chunk" in m
    ]
    text_seen = "".join(
        c["payload"]["delta"] for c in narrative_chunks if c["payload"].get("delta")
    )
    assert "different region" in text_seen.lower()


@pytest.mark.asyncio
async def test_stream_gemini_reply_caps_runaway_loop():
    """A Gemini that emits a function_call every turn is fail-stopped at the cap."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    # Generator that always returns one chunk asking for another tool call.
    def _infinite_calls():
        i = 0
        while True:
            i += 1
            yield iter([_make_fake_chunk_with_function_call(
                "fetch_dem", {"bbox": [0, 0, 1, 1]}, f"call-{i}"
            )])

    chunks = _infinite_calls()
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(chunks)

    dispatch_count = 0

    async def _counting_invoke(_ws, _state, name, args):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_counting_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "x", "research"
        )

    # job-186: the runaway is fail-stopped FAST. Every round is the SAME
    # tool+args (identical fetch_dem), so the LOOP WATCHDOG trips at
    # loop_repeat_n() rounds - well before the historical MAX_TURN_ITERATIONS
    # cap. (A varied-tool runaway hits the step cap instead - next test.)
    from grace2_agent.runaway_guard import loop_repeat_n

    assert dispatch_count <= loop_repeat_n(), (
        f"identical-repeat runaway not watchdog-capped: {dispatch_count} "
        f"dispatches vs watchdog threshold {loop_repeat_n()}"
    )
    assert dispatch_count < MAX_TURN_ITERATIONS


# ---------------------------------------------------------------------------
# job-0315: live-wire narration-segment interleave (one bubble per contiguous
# run of agent text between tool-call rounds).
# ---------------------------------------------------------------------------


def _make_fake_chunk_multi_call(calls):
    """A single chunk carrying N function_call parts (multiple calls one round)."""
    parts = []
    for name, args, call_id in calls:
        fn_call = MagicMock()
        fn_call.name = name
        fn_call.id = call_id
        fn_call.args = args
        p = MagicMock()
        p.function_call = fn_call
        p.text = None
        parts.append(p)
    fake_content = MagicMock()
    fake_content.parts = parts
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


def _agent_chunks(sock):
    """Decode every agent-message-chunk envelope in wire order."""
    return [
        json.loads(m) for m in sock.sent if "agent-message-chunk" in m
    ]


def _segment_ids_in_order(sock):
    """The message_ids of agent-message-chunk frames, first-seen in wire order."""
    seen: list[str] = []
    for c in _agent_chunks(sock):
        mid = c["payload"]["message_id"]
        if mid not in seen:
            seen.append(mid)
    return seen


@pytest.mark.asyncio
async def test_stream_segments_interleave_distinct_message_ids():
    """text -> tool -> text -> tool -> text emits THREE distinct bubble ids, each
    non-terminal segment finalized (done=True) BEFORE its round's tool frames,
    and no id ever receives an empty-only done=True without prior text."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    # Round 1: narrate then call geocode. Round 2: narrate then call publish.
    # Round 3: narrate, no call (terminal).
    turn1 = iter([
        _make_fake_chunk_with_text("Fetching... "),
        _make_fake_chunk_with_function_call(
            "geocode_location", {"query": "Fort Myers, FL"}, "call-geo"
        ),
    ])
    turn2 = iter([
        _make_fake_chunk_with_text("Now publishing... "),
        _make_fake_chunk_with_function_call(
            "publish_layer", {"layer_uri": "h-1", "layer_id": "L"}, "call-pub"
        ),
    ])
    turn3 = iter([_make_fake_chunk_with_text("Done.")])
    turn_iter = iter([turn1, turn2, turn3])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    # Record the wire length at each dispatch moment so we can prove the tool
    # dispatch lands BETWEEN the surrounding bubbles' frames (the emitter that
    # would emit a real pipeline-state tool card is mocked out here, so we use
    # the dispatch's wire position as the interleave witness).
    dispatch_at_wire_len: list[int] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_at_wire_len.append(len(sock.sent))
        if name == "geocode_location":
            return {"bbox": [-82.0, 26.5, -81.7, 26.8]}
        return {"ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Show me Fort Myers protected areas", "research"
        )

    seg_ids = _segment_ids_in_order(sock)
    assert len(seg_ids) == 3, f"expected 3 distinct bubble ids, got {seg_ids}"

    # Each id received >=1 text delta BEFORE its done=True.
    chunks = _agent_chunks(sock)
    for mid in seg_ids:
        frames = [c["payload"] for c in chunks if c["payload"]["message_id"] == mid]
        text_frames = [f for f in frames if f.get("delta") and not f.get("done")]
        done_frames = [f for f in frames if f.get("done")]
        assert text_frames, f"segment {mid} had no text before done=True"
        assert len(done_frames) == 1, f"segment {mid} done=True count != 1"

    # Wire ordering: each NON-terminal segment's done=True precedes the
    # following round's pipeline-state/tool frames. We assert the first two
    # segments are finalized before the final 'Done.' segment opens, and the
    # tool dispatch frames fall between consecutive bubbles. Concretely:
    # the done=True of seg0 must appear in the wire BEFORE the first text
    # delta of seg1, with a pipeline-state (tool card) frame in between.
    wire = sock.sent

    def _done_index(mid):
        for i, m in enumerate(wire):
            if "agent-message-chunk" not in m:
                continue
            p = json.loads(m)["payload"]
            if p["message_id"] == mid and p.get("done"):
                return i
        return -1

    def _first_text_index(mid):
        for i, m in enumerate(wire):
            if "agent-message-chunk" not in m:
                continue
            p = json.loads(m)["payload"]
            if p["message_id"] == mid and p.get("delta") and not p.get("done"):
                return i
        return -1

    # seg0 done < first tool dispatch < seg1 first text. The first dispatch
    # (geocode) happened at wire length dispatch_at_wire_len[0]: every frame
    # before that index was already on the wire, so the dispatch sits strictly
    # AFTER seg0's done=True and strictly BEFORE seg1's first text.
    seg0_done = _done_index(seg_ids[0])
    seg1_text = _first_text_index(seg_ids[1])
    assert 0 <= seg0_done < seg1_text, "seg0 not finalized before seg1 opened"
    first_dispatch_pos = dispatch_at_wire_len[0]
    assert seg0_done < first_dispatch_pos <= seg1_text, (
        "tool dispatch did not interleave between seg0(done) and seg1(text)"
    )
    # And the LAST (terminal) segment carries a done=True too.
    assert _done_index(seg_ids[2]) > seg1_text


@pytest.mark.asyncio
async def test_stream_no_leading_text_before_first_tool_no_empty_bubble():
    """Round 1 = function_call ONLY (no text), round 2 = text. Exactly ONE
    message_id appears across all agent frames (the tool round mints nothing),
    and it carries exactly one done=True."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    turn1 = iter([
        _make_fake_chunk_with_function_call(
            "geocode_location", {"query": "x"}, "call-geo"
        ),
    ])
    turn2 = iter([_make_fake_chunk_with_text("Here it is.")])
    turn_iter = iter([turn1, turn2])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    async def _fake_invoke(_ws, _state, name, args):
        return {"bbox": [0, 0, 1, 1]}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "x", "research"
        )

    seg_ids = _segment_ids_in_order(sock)
    assert len(seg_ids) == 1, f"expected exactly 1 bubble, got {seg_ids}"
    done_frames = [
        c for c in _agent_chunks(sock) if c["payload"].get("done")
    ]
    assert len(done_frames) == 1
    # No empty-only done=True for an id that never got text: the single id DID
    # get text ("Here it is."), so this is correct by construction.
    assert done_frames[0]["payload"]["message_id"] == seg_ids[0]


@pytest.mark.asyncio
async def test_stream_multiple_calls_one_round_single_finalize():
    """Round 1 = text + TWO function_calls in ONE generation, round 2 = text.
    Exactly TWO distinct bubbles (one contiguous run each), one done=True per
    id, and BOTH tool dispatches occur between the two bubbles' frames."""
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    turn1 = iter([
        _make_fake_chunk_with_text("Fetching both. "),
        _make_fake_chunk_multi_call([
            ("geocode_location", {"query": "a"}, "c1"),
            ("geocode_location", {"query": "b"}, "c2"),
        ]),
    ])
    turn2 = iter([_make_fake_chunk_with_text("Both done.")])
    turn_iter = iter([turn1, turn2])

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    dispatch_log: list[str] = []
    dispatch_at_wire_len: list[int] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append(name)
        dispatch_at_wire_len.append(len(sock.sent))
        return {"bbox": [0, 0, 1, 1]}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "x", "research"
        )

    # Both calls dispatched in the single round.
    assert dispatch_log == ["geocode_location", "geocode_location"]

    seg_ids = _segment_ids_in_order(sock)
    assert len(seg_ids) == 2, f"expected 2 bubbles (one per run), got {seg_ids}"
    chunks = _agent_chunks(sock)
    for mid in seg_ids:
        done_frames = [
            c for c in chunks
            if c["payload"]["message_id"] == mid and c["payload"].get("done")
        ]
        assert len(done_frames) == 1, f"segment {mid} done=True count != 1"

    # Both pipeline-state(tool) dispatches fall between the two bubbles: after
    # seg0's done=True and before seg1's first text.
    wire = sock.sent

    def _done_index(mid):
        for i, m in enumerate(wire):
            if "agent-message-chunk" not in m:
                continue
            p = json.loads(m)["payload"]
            if p["message_id"] == mid and p.get("done"):
                return i
        return -1

    def _first_text_index(mid):
        for i, m in enumerate(wire):
            if "agent-message-chunk" not in m:
                continue
            p = json.loads(m)["payload"]
            if p["message_id"] == mid and p.get("delta") and not p.get("done"):
                return i
        return -1

    seg0_done = _done_index(seg_ids[0])
    seg1_text = _first_text_index(seg_ids[1])
    assert 0 <= seg0_done < seg1_text
    # Both dispatches happened after seg0's done=True and before seg1's text:
    # the single finalize fired ONCE before the whole round dispatched.
    assert all(
        seg0_done < pos <= seg1_text for pos in dispatch_at_wire_len
    ), "tool dispatches not interleaved between the two bubbles"
