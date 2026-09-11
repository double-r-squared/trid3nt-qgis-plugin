"""Turn-invariant logging, and the geocode-only backstop over the bench prompt.

The two turn invariants used to skip silently; every terminal round now logs one
INFO line per invariant - FIRED, or SKIPPED with its reason, or gate-inactive
when the budget is spent or the kill-switch is set. For a true geocode-only turn
the backstop rescues the prompt. Offline: scripted provider, no model."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapters.adapter import ModelSettings
from trid3nt_server.adapters.scripted_adapter import set_script
from trid3nt_contracts import new_ulid

#: The P10 bench prompt.
P10_PROMPT = (
    "Run a small pluvial flood simulation for a 4km box in Peoria, Illinois "
    "with a 50-year storm."
)

_GEOCODE_RESULT = {
    "bbox": [-89.6535, 40.6398, -89.5247, 40.7479],
    "name": "Peoria, Peoria County, Illinois, United States",
}


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _settings() -> ModelSettings:
    return ModelSettings(
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


def _nudge_count(contents) -> int:
    return sum(
        1 for t in _content_texts(contents) if t == agent_server._CONTINUATION_NUDGE
    )


@pytest.fixture()
def _scripted(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    yield
    set_script(None)


async def _drive(script, dispatch_results, user_text):
    set_script(script)
    captured: dict = {"rounds": 0, "contents": None}
    real_stream = agent_server.stream_events_with_contents

    def _wrap(client, model, contents, **kw):
        captured["rounds"] += 1
        captured["contents"] = contents
        return real_stream(client, model, contents, **kw)

    async def _dispatch(_ws, _state, name, _args):
        return dispatch_results[name]

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "stream_events_with_contents", _wrap), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, _settings(), user_text, "research"
        )
    return sock, captured["contents"], captured["rounds"]




def test_p10_prompt_matches_data_intent_heuristic():
    """The heuristic did NOT miss P10 -- "flood"/"simulation" both match."""
    assert agent_server._asks_for_data_or_analysis(P10_PROMPT)


@pytest.mark.asyncio
async def test_p10_shape_geocode_only_sim_ask_nudge_fires(
    _scripted, monkeypatch, caplog
):
    """The P10 bench shape as a TRUE geocode-only turn: the bare-geocode
    backstop rescues it (one nudge, one extra round) and logs the fire."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {
            "text": "Locating Peoria.",
            "tool_call": {
                "name": "geocode_location",
                "args": {"query": "Peoria, Illinois"},
            },
        },
        {"text": "Peoria is on the Illinois River."},
    ]
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        _sock, contents, rounds = await _drive(
            script, {"geocode_location": dict(_GEOCODE_RESULT)}, P10_PROMPT
        )
    assert _nudge_count(contents) == 1, _content_texts(contents)
    assert rounds == 3  # geocode round + terminal + the nudged retry
    assert any(
        "turn-invariant nudge (bare-geocode)" in r.getMessage()
        for r in caplog.records
    )




@pytest.mark.asyncio
async def test_skip_reasons_logged_on_narrated_multi_tool_turn(
    _scripted, monkeypatch, caplog
):
    """geocode+fetch turn with a closing narration -> both invariants log
    their skip reason (has-closing-text / tools-not-geocode-only)."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {"tool_call": {"name": "geocode_location", "args": {"query": "Peoria"}}},
        {
            "tool_call": {
                "name": "fetch_dem",
                "args": {"bbox": [-89.65, 40.64, -89.52, 40.75]},
            }
        },
        {"text": "DEM fetched for Peoria."},
    ]
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        _sock, contents, _rounds = await _drive(
            script,
            {
                "geocode_location": dict(_GEOCODE_RESULT),
                "fetch_dem": {"status": "ok"},
            },
            P10_PROMPT,
        )
    assert _nudge_count(contents) == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "turn-invariant no-silent-end skipped" in m and "has-closing-text" in m
        for m in msgs
    ), msgs
    assert any(
        "turn-invariant bare-geocode skipped" in m
        and "tools-not-geocode-only" in m
        for m in msgs
    ), msgs


@pytest.mark.asyncio
async def test_skip_reason_no_tools_on_pure_qa_turn(
    _scripted, monkeypatch, caplog
):
    """A pure-Q&A turn logs no-tools-dispatched / not-a-data-or-analysis-ask
    style skips rather than staying silent."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [{"text": "Hello there."}]
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        _sock, contents, _rounds = await _drive(script, {}, "hi")
    assert _nudge_count(contents) == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "turn-invariant no-silent-end skipped" in m and "no-tools-dispatched" in m
        for m in msgs
    ), msgs
    assert any("turn-invariant bare-geocode skipped" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_disabled_invariants_log_the_reason(
    _scripted, monkeypatch, caplog
):
    """TRID3NT_TURN_INVARIANTS=0 -> one honest gate-inactive line, no nudge."""
    monkeypatch.setenv("TRID3NT_TURN_INVARIANTS", "0")
    script = [
        {"tool_call": {"name": "geocode_location", "args": {"query": "Peoria"}}},
        {"text": "Peoria located."},
    ]
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        _sock, contents, _rounds = await _drive(
            script, {"geocode_location": dict(_GEOCODE_RESULT)}, P10_PROMPT
        )
    assert _nudge_count(contents) == 0
    assert any(
        "turn-invariants skipped" in r.getMessage()
        and "disabled-by-env" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_spent_budget_logs_gate_inactive(_scripted, monkeypatch, caplog):
    """After the one-per-turn nudge is spent, the next terminal round logs
    nudge-budget-spent instead of silently doing nothing."""
    monkeypatch.delenv("TRID3NT_TURN_INVARIANTS", raising=False)
    script = [
        {"tool_call": {"name": "geocode_location", "args": {"query": "Peoria"}}},
        {"text": "Peoria located."},  # terminal -> bare-geocode nudge fires
        {"text": "Still just narration."},  # nudged retry -> budget spent
    ]
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        _sock, contents, _rounds = await _drive(
            script, {"geocode_location": dict(_GEOCODE_RESULT)}, P10_PROMPT
        )
    assert _nudge_count(contents) == 1
    assert any(
        "turn-invariants skipped" in r.getMessage()
        and "nudge-budget-spent" in r.getMessage()
        for r in caplog.records
    )
