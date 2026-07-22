"""Stage 3 (ADR 0018) -- the tool-candidates ambiguity/ask gate.

Interface contract (fixed between lanes): agent->client ``tool-candidates``
{request_id, stage_label, candidates: [{tool_name, summary, score}], reason:
"ambiguity"|"ask_mode", timeout_s}; client->agent ``tool-choice``
{request_id, tool_name | null, free_text | null}.

Covered here (Lane S owns emission + consumption semantics):
  * ask mode emits the card before dispatch and waits;
  * auto mode emits ONLY on a measured near-tie margin;
  * confident auto never emits;
  * an unanswered card times out BOUNDED and the turn proceeds autonomously
    with a note (never hangs);
  * a tool_name reply pins that tool (directive note + allowed set + visible
    registry);
  * a free_text reply feeds back as a user clarification.

Driven end-to-end through ``_stream_gemini_reply`` on the scripted provider;
the retrieval ranking is patched at its module seam so margins are exact.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

import trid3nt_server.main as agent_main
from trid3nt_server import server as agent_server
from trid3nt_server.adapter import GeminiSettings
from trid3nt_server.scripted_adapter import set_script
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.discovery import tool_retrieval as tr
from trid3nt_contracts import new_ulid

# The pin test asserts against real registry names.
agent_main._import_tools_registry()


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
    texts: list[str] = []
    for content in contents or []:
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    return texts


def _candidate_envelopes(sock: _FakeSocket) -> list[dict]:
    return [
        m
        for m in sock.sent
        if isinstance(m, dict) and m.get("type") == "tool-candidates"
    ]


#: A clear near-tie (relative margin ~0.2% < the 1% default threshold).
_NEAR_TIE = [
    ("run_geoclaw_inundation", 0.0500),
    ("fetch_tsunami_events", 0.0499),
    ("fetch_dem", 0.0300),
]

#: A confident ranking (relative margin 40%).
_CONFIDENT = [
    ("fetch_dem", 0.0500),
    ("fetch_topobathy", 0.0300),
    ("fetch_landcover", 0.0200),
]


@pytest.fixture()
def _scripted(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    set_script([{"text": "ok, done."}])
    yield
    set_script(None)


async def _start_turn(monkeypatch, ranked, user_text="map the coast"):
    """Launch one scripted turn as a task; return (task, sock, state, captured)."""
    monkeypatch.setattr(tr, "retrieve_ranked_tools", lambda text, k=25: list(ranked))
    captured: dict = {"contents": None}
    real_stream = agent_server.stream_events_with_contents

    def _wrap(client, model, contents, **kw):
        captured["contents"] = contents
        return real_stream(client, model, contents, **kw)

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    patcher = patch.object(agent_server, "stream_events_with_contents", _wrap)
    patcher.start()

    async def _run():
        try:
            await agent_server._stream_gemini_reply(
                sock, state, _settings(), user_text, "research"
            )
        finally:
            patcher.stop()

    task = asyncio.create_task(_run())
    return task, sock, state, captured


async def _wait_for_card(sock: _FakeSocket, timeout: float = 2.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        cards = _candidate_envelopes(sock)
        if cards:
            return cards[0]
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"tool-candidates never emitted; sent types: "
        f"{[m.get('type') for m in sock.sent if isinstance(m, dict)]!r}"
    )


# ---------------------------------------------------------------------------
# Emission conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_mode_emits_card_and_choice_pins_tool(_scripted, monkeypatch):
    monkeypatch.setenv("TRID3NT_MODE", "ask")  # env-default ask mode
    task, sock, state2, captured = await _start_turn(monkeypatch, _CONFIDENT)

    card = await _wait_for_card(sock)
    payload = card.get("payload") or {}
    # --- contract shape ---
    assert isinstance(payload.get("request_id"), str) and payload["request_id"]
    assert payload.get("reason") == "ask_mode"
    assert isinstance(payload.get("timeout_s"), (int, float))
    assert isinstance(payload.get("stage_label"), str)
    cands = payload.get("candidates")
    assert isinstance(cands, list) and cands
    for c in cands:
        assert isinstance(c.get("tool_name"), str)
        assert "summary" in c and "score" in c
    assert cands[0]["tool_name"] == "fetch_dem"

    # --- tool_name reply pins the tool for the next dispatch ---
    ok = agent_server._resolve_pending_tool_choice(
        state2.session_id,
        {"request_id": payload["request_id"], "tool_name": "fetch_dem"},
    )
    assert ok is True
    await asyncio.wait_for(task, timeout=5.0)
    texts = _content_texts(captured["contents"])
    assert any(
        "Use the tool 'fetch_dem'" in t for t in texts
    ), texts
    assert "fetch_dem" in state2.allowed_tool_set.explicit_tools


@pytest.mark.asyncio
async def test_auto_near_tie_emits_ambiguity_card(_scripted, monkeypatch):
    monkeypatch.delenv("TRID3NT_MODE", raising=False)  # auto default
    monkeypatch.setenv("TRID3NT_TOOL_CHOICE_TIMEOUT_S", "0.2")
    task, sock, state, captured = await _start_turn(monkeypatch, _NEAR_TIE)
    card = await _wait_for_card(sock)
    assert (card.get("payload") or {}).get("reason") == "ambiguity"
    await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_confident_auto_does_not_emit(_scripted, monkeypatch):
    monkeypatch.delenv("TRID3NT_MODE", raising=False)
    task, sock, state, captured = await _start_turn(monkeypatch, _CONFIDENT)
    await asyncio.wait_for(task, timeout=5.0)
    assert _candidate_envelopes(sock) == []


@pytest.mark.asyncio
async def test_ambiguity_margin_zero_disables_auto_asks(_scripted, monkeypatch):
    monkeypatch.delenv("TRID3NT_MODE", raising=False)
    monkeypatch.setenv("TRID3NT_AMBIGUITY_MARGIN", "0")
    task, sock, state, captured = await _start_turn(monkeypatch, _NEAR_TIE)
    await asyncio.wait_for(task, timeout=5.0)
    assert _candidate_envelopes(sock) == []


# ---------------------------------------------------------------------------
# Wait semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_proceeds_autonomously_with_note(_scripted, monkeypatch):
    """An unanswered card must NEVER hang: bounded timeout, note, autonomy."""
    monkeypatch.setenv("TRID3NT_MODE", "ask")
    monkeypatch.setenv("TRID3NT_TOOL_CHOICE_TIMEOUT_S", "0.15")
    task, sock, state, captured = await _start_turn(monkeypatch, _CONFIDENT)
    await _wait_for_card(sock)
    # No reply -- the turn must still complete promptly.
    await asyncio.wait_for(task, timeout=5.0)
    texts = _content_texts(captured["contents"])
    assert any("proceed" in t.lower() and "autonomous" in t.lower() for t in texts), texts
    # The registry entry is cleaned up (no leak).
    assert not agent_server._PENDING_TOOL_CHOICES


@pytest.mark.asyncio
async def test_free_text_reply_feeds_back_as_clarification(
    _scripted, monkeypatch
):
    monkeypatch.setenv("TRID3NT_MODE", "ask")
    task, sock, state, captured = await _start_turn(monkeypatch, _CONFIDENT)
    card = await _wait_for_card(sock)
    rid = card["payload"]["request_id"]
    ok = agent_server._resolve_pending_tool_choice(
        state.session_id,
        {"request_id": rid, "tool_name": None, "free_text": "I meant bathymetry, not land elevation"},
    )
    assert ok is True
    await asyncio.wait_for(task, timeout=5.0)
    texts = _content_texts(captured["contents"])
    assert any(
        "[User clarification] I meant bathymetry" in t for t in texts
    ), texts


# ---------------------------------------------------------------------------
# Consumption seam hygiene
# ---------------------------------------------------------------------------


def test_resolve_rejects_wrong_session_and_unknown_id():
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        agent_server._register_pending_tool_choice("SESSION-A", "REQ-1", fut)
        try:
            # wrong session -> refused
            assert not agent_server._resolve_pending_tool_choice(
                "SESSION-B", {"request_id": "REQ-1", "tool_name": "fetch_dem"}
            )
            # unknown id -> refused
            assert not agent_server._resolve_pending_tool_choice(
                "SESSION-A", {"request_id": "NOPE", "tool_name": "fetch_dem"}
            )
            # malformed payloads -> refused, never raise
            assert not agent_server._resolve_pending_tool_choice("SESSION-A", None)
            assert not agent_server._resolve_pending_tool_choice("SESSION-A", {})
            # right session resolves
            assert agent_server._resolve_pending_tool_choice(
                "SESSION-A", {"request_id": "REQ-1", "tool_name": "fetch_dem"}
            )
            assert fut.result()["tool_name"] == "fetch_dem"
        finally:
            agent_server._pop_pending_tool_choice("REQ-1")
    finally:
        loop.close()


def test_session_config_sets_routing_mode_defensively():
    """The session-config seam accepts loose dicts; bad modes are ignored."""
    state = agent_server.SessionState(session_id=new_ulid())
    assert agent_server._session_routing_mode(state) == "auto"
    state.routing_mode = "ask"
    assert agent_server._session_routing_mode(state) == "ask"
    state.routing_mode = "bogus"  # unknown -> env default
    assert agent_server._session_routing_mode(state) == "auto"
