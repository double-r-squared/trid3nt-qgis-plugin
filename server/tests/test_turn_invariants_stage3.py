"""Stage 3 (ADR 0017 mechanism 4) -- TURN-LOOP INVARIANTS, scripted adapter.

Two structural nudges, ONE shared per-turn budget, injected as a user-role
content so the model gets exactly one more round:

  (a) NO-SILENT-END -- a turn terminating with tool results but ZERO assistant
      text since the last tool round gets one "summarize the results"
      continuation nudge; never more than one per turn.
  (b) BARE-GEOCODE BACKSTOP -- a turn whose ONLY tool was geocode_location
      while the user message asked for data/analysis gets the same nudge.

Kill-switch: ``TRID3NT_TURN_INVARIANTS=0``. Driven end-to-end through
``_stream_gemini_reply`` on the scripted (replay) provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapter import GeminiSettings
from trid3nt_server.scripted_adapter import set_script
from trid3nt_contracts import new_ulid


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


def _content_texts(contents) -> list[str]:
    """Flatten the plain-text parts of a genai contents list."""
    texts: list[str] = []
    for content in contents or []:
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    return texts


@pytest.fixture()
def _scripted(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    yield
    set_script(None)


async def _drive(monkeypatch, script, dispatch_results, user_text):
    """Run one scripted turn; return (socket, captured-contents, rounds)."""
    set_script(script)
    captured: dict = {"rounds": 0, "contents": None}
    real_stream = agent_server.stream_events_with_contents

    def _wrap(client, model, contents, **kw):
        captured["rounds"] += 1
        captured["contents"] = contents  # same list object, mutated in place
        return real_stream(client, model, contents, **kw)

    async def _dispatch(_ws, _state, name, _args):
        return dispatch_results[name]

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "stream_events_with_contents", _wrap), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), user_text, "research"
        )
    return sock, captured["contents"], captured["rounds"]


def _nudge_count(contents) -> int:
    return sum(
        1
        for t in _content_texts(contents)
        if t == agent_server._CONTINUATION_NUDGE
    )


# ---------------------------------------------------------------------------
# (a) no-silent-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_end_after_tool_results_gets_one_nudge(
    _scripted, monkeypatch
):
    """Tool round then a fully-silent terminal round -> exactly ONE nudge."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {"tool_call": {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}}},
        {},  # silent terminal round (no text, no tool) -- replayed post-nudge
    ]
    sock, contents, rounds = await _drive(
        monkeypatch, script, {"fetch_dem": {"status": "ok"}}, "get a DEM"
    )
    assert _nudge_count(contents) == 1, _content_texts(contents)
    # tool round + silent round + one nudged retry round == 3 (never more:
    # the retry is also silent but the budget is spent).
    assert rounds == 3


@pytest.mark.asyncio
async def test_narrated_end_gets_no_nudge(_scripted, monkeypatch):
    """A turn that ends with real narration never nudges (no-fire)."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {"tool_call": {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}}},
        {"text": "Here is the DEM for your area."},
    ]
    sock, contents, rounds = await _drive(
        monkeypatch, script, {"fetch_dem": {"status": "ok"}}, "get a DEM"
    )
    assert _nudge_count(contents) == 0
    assert rounds == 2


@pytest.mark.asyncio
async def test_no_tool_turn_gets_no_nudge(_scripted, monkeypatch):
    """A pure-Q&A turn (zero tools) is never nudged, even when short."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [{"text": "Hello!"}]
    sock, contents, rounds = await _drive(monkeypatch, script, {}, "hi")
    assert _nudge_count(contents) == 0
    assert rounds == 1


@pytest.mark.asyncio
async def test_invariants_kill_switch(_scripted, monkeypatch):
    """TRID3NT_TURN_INVARIANTS=0 disables the nudge entirely."""
    monkeypatch.setenv("TRID3NT_TURN_INVARIANTS", "0")
    script = [
        {"tool_call": {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}}},
        {},
    ]
    sock, contents, rounds = await _drive(
        monkeypatch, script, {"fetch_dem": {"status": "ok"}}, "get a DEM"
    )
    assert _nudge_count(contents) == 0
    assert rounds == 2


# ---------------------------------------------------------------------------
# (b) bare-geocode backstop
# ---------------------------------------------------------------------------

_GEOCODE_RESULT = {"bbox": [-82.6, 27.9, -82.3, 28.1], "name": "Tampa"}


@pytest.mark.asyncio
async def test_bare_geocode_with_data_ask_gets_one_nudge(
    _scripted, monkeypatch
):
    """geocode-only turn + a data ask -> one nudge (even though it narrated)."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {
            "text": "Locating Tampa.",
            "tool_call": {"name": "geocode_location", "args": {"query": "Tampa"}},
        },
        {"text": "Tampa is on the west coast of Florida."},
    ]
    sock, contents, rounds = await _drive(
        monkeypatch,
        script,
        {"geocode_location": dict(_GEOCODE_RESULT)},
        "show me flood risk data for Tampa",
    )
    assert _nudge_count(contents) == 1, _content_texts(contents)
    assert rounds == 3  # geocode round + terminal + one nudged retry


@pytest.mark.asyncio
async def test_bare_geocode_pure_locate_ask_no_nudge(_scripted, monkeypatch):
    """'where is X' is a legitimate geocode-only turn -- no nudge (no-fire)."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {
            "text": "Locating Tampa.",
            "tool_call": {"name": "geocode_location", "args": {"query": "Tampa"}},
        },
        {"text": "Tampa is on the west coast of Florida."},
    ]
    sock, contents, rounds = await _drive(
        monkeypatch,
        script,
        {"geocode_location": dict(_GEOCODE_RESULT)},
        "where is Tampa?",
    )
    assert _nudge_count(contents) == 0
    assert rounds == 2


@pytest.mark.asyncio
async def test_geocode_plus_fetch_turn_no_backstop(_scripted, monkeypatch):
    """A turn that geocoded AND fetched is not 'bare geocode' -- no nudge."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {"tool_call": {"name": "geocode_location", "args": {"query": "Tampa"}}},
        {"tool_call": {"name": "fetch_dem", "args": {"bbox": [-82.6, 27.9, -82.3, 28.1]}}},
        {"text": "DEM fetched for Tampa."},
    ]
    sock, contents, rounds = await _drive(
        monkeypatch,
        script,
        {
            "geocode_location": dict(_GEOCODE_RESULT),
            "fetch_dem": {"status": "ok"},
        },
        "show me elevation data for Tampa",
    )
    assert _nudge_count(contents) == 0
    assert rounds == 3


@pytest.mark.asyncio
async def test_bare_geocode_kill_switch(_scripted, monkeypatch):
    monkeypatch.setenv("TRID3NT_TURN_INVARIANTS", "off")
    script = [
        {"tool_call": {"name": "geocode_location", "args": {"query": "Tampa"}}},
        {"text": "Tampa located."},
    ]
    sock, contents, rounds = await _drive(
        monkeypatch,
        script,
        {"geocode_location": dict(_GEOCODE_RESULT)},
        "show me flood risk data for Tampa",
    )
    assert _nudge_count(contents) == 0
    assert rounds == 2


# ---------------------------------------------------------------------------
# Intent heuristic unit checks
# ---------------------------------------------------------------------------


def test_data_intent_heuristic():
    asks = agent_server._asks_for_data_or_analysis
    assert asks("show me flood risk data for Tampa")
    assert asks("model a 100-year flood in Peoria")
    assert asks("fetch building footprints")
    assert asks("what's the average elevation here")
    assert not asks("where is Tampa?")
    assert not asks("hello there")
    assert not asks(None)
