# Scripted (replay) adapter tests for the agent.
#
# The scripted adapter is the zero-cost deterministic LLM stand-in
# (MODEL_PROVIDER=scripted): it replays a canned transcript of tool calls so the
# full agent loop + tool dispatch + WS + web can be E2E-tested without Bedrock
# spend. These cover provider selection, transcript resolution precedence, turn
# indexing, the emitted StreamEvent shape, and -- the load-bearing one -- that
# the outer dispatch (stream_events_with_contents) routes to the scripted path
# with NO model client (client=None), proving the seam end to end.

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.adapter import (
    FunctionCallEvent,
    TextDeltaEvent,
    UsageMetadataEvent,
    stream_events_with_contents,
)
from trid3nt_server import scripted_adapter as sa


@pytest.fixture(autouse=True)
def _clean_script_env(monkeypatch):
    """Each test starts with no override + no transcript env."""
    sa.clear_script()
    monkeypatch.delenv("TRID3NT_SCRIPTED_TRANSCRIPT_JSON", raising=False)
    monkeypatch.delenv("TRID3NT_SCRIPTED_TRANSCRIPT", raising=False)
    yield
    sa.clear_script()


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Provider selection.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("val,expected", [
    ("scripted", True), ("replay", True), ("fake", True), ("SCRIPTED", True),
    ("bedrock", False), ("vertex", False), ("", False),
])
def test_model_provider_is_scripted(monkeypatch, val, expected):
    monkeypatch.setenv("MODEL_PROVIDER", val)
    assert sa.model_provider_is_scripted() is expected


def test_model_provider_unset_is_not_scripted(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert sa.model_provider_is_scripted() is False


# --------------------------------------------------------------------------- #
# Transcript resolution precedence.
# --------------------------------------------------------------------------- #
def test_load_script_override_wins(monkeypatch):
    monkeypatch.setenv("TRID3NT_SCRIPTED_TRANSCRIPT_JSON", json.dumps([{"text": "env"}]))
    sa.set_script([{"text": "override"}])
    assert sa.load_script() == [{"text": "override"}]


def test_load_script_inline_json_list_and_object(monkeypatch):
    monkeypatch.setenv("TRID3NT_SCRIPTED_TRANSCRIPT_JSON", json.dumps([{"text": "a"}]))
    assert sa.load_script() == [{"text": "a"}]
    # Object form {"turns": [...]} is also accepted.
    monkeypatch.setenv("TRID3NT_SCRIPTED_TRANSCRIPT_JSON", json.dumps({"turns": [{"text": "b"}]}))
    assert sa.load_script() == [{"text": "b"}]


def test_load_script_from_file(monkeypatch, tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"turns": [{"text": "fromfile"}]}), encoding="utf-8")
    monkeypatch.setenv("TRID3NT_SCRIPTED_TRANSCRIPT", str(p))
    assert sa.load_script() == [{"text": "fromfile"}]


def test_load_script_empty_when_nothing_configured():
    assert sa.load_script() == []


def test_load_script_bad_json_is_safe(monkeypatch):
    monkeypatch.setenv("TRID3NT_SCRIPTED_TRANSCRIPT_JSON", "{not json")
    assert sa.load_script() == []


# --------------------------------------------------------------------------- #
# Turn indexing = count of prior model (assistant) turns.
# --------------------------------------------------------------------------- #
def test_turn_index_counts_model_roles():
    assert sa._turn_index([{"role": "user"}]) == 0
    assert sa._turn_index([{"role": "user"}, {"role": "model"}, {"role": "user"}]) == 1
    assert sa._turn_index([{"role": "user"}, {"role": "model"}, {"role": "user"},
                           {"role": "model"}, {"role": "user"}]) == 2
    assert sa._turn_index("not a list") == 0


# --------------------------------------------------------------------------- #
# stream_scripted: emits the right StreamEvents and advances per turn.
# --------------------------------------------------------------------------- #
def test_stream_scripted_emits_text_then_tool_call_at_turn0():
    sa.set_script([
        {"text": "Geocoding.", "tool_call": {"name": "geocode_place", "args": {"q": "Mexico Beach"}}},
        {"text": "All done."},
    ])
    evs = _run(_collect(sa.stream_scripted(contents=[{"role": "user"}])))
    assert isinstance(evs[0], TextDeltaEvent) and evs[0].delta == "Geocoding."
    fc = next(e for e in evs if isinstance(e, FunctionCallEvent))
    assert fc.name == "geocode_place"
    assert fc.args == {"q": "Mexico Beach"}
    assert fc.call_id == "scripted-0"
    assert isinstance(evs[-1], UsageMetadataEvent)
    assert evs[-1].total_token_count == 0  # zero-cost


def test_stream_scripted_advances_to_terminal_turn():
    sa.set_script([
        {"text": "Geocoding.", "tool_call": {"name": "geocode_place", "args": {}}},
        {"text": "All done."},
    ])
    # One prior model turn -> index 1 -> the terminal (no tool_call) turn.
    contents = [{"role": "user"}, {"role": "model"}, {"role": "user"}]
    evs = _run(_collect(sa.stream_scripted(contents=contents)))
    assert any(isinstance(e, TextDeltaEvent) and e.delta == "All done." for e in evs)
    assert not any(isinstance(e, FunctionCallEvent) for e in evs)


def test_stream_scripted_exhausted_emits_terminal_text_no_loop():
    sa.set_script([{"text": "only turn"}])
    # index 2 is past the 1-turn script -> graceful terminal text, no tool call.
    contents = [{"role": "user"}, {"role": "model"}, {"role": "user"},
                {"role": "model"}, {"role": "user"}]
    evs = _run(_collect(sa.stream_scripted(contents=contents)))
    assert any(isinstance(e, TextDeltaEvent) for e in evs)
    assert not any(isinstance(e, FunctionCallEvent) for e in evs)


# --------------------------------------------------------------------------- #
# Integration: the OUTER dispatch routes to scripted with NO model client.
# --------------------------------------------------------------------------- #
def test_dispatch_routes_to_scripted_with_no_client(monkeypatch):
    """stream_events_with_contents(client=None, ...) must yield the scripted
    tool call when MODEL_PROVIDER=scripted -- proving the adapter.py seam routes
    BEFORE the Vertex/Bedrock client path (zero cost, no GCP/AWS creds)."""
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    sa.set_script([{"text": "Running SWAN.", "tool_call": {"name": "run_swan_waves",
                                                            "args": {"bbox": [-85.55, 29.85, -85.3, 30.05]}}}])
    evs = _run(_collect(stream_events_with_contents(
        client=None,            # no model client exists on this path
        model="unused",
        contents=[{"role": "user"}],
    )))
    fc = next(e for e in evs if isinstance(e, FunctionCallEvent))
    assert fc.name == "run_swan_waves"
    assert fc.args["bbox"] == [-85.55, 29.85, -85.3, 30.05]
