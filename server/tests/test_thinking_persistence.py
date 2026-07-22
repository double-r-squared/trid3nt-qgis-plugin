"""Thinking persistence + NEVER-REHYDRATE guard (LANE CORE, 2026-07-22).

Server side of the same-bubble thinking contract: when the per-turn
``show_thinking`` toggle is ON, the accumulated reasoning text persists as the
``thinking`` FIELD on the agent chat row that carries the answer (field name
"thinking" is the fixed cross-lane interface -- the QGIS plugin reads it).

NEVER-REHYDRATE (NATE requirement): persisted thinking is display replay
material ONLY. ``adapter.build_contents_from_history`` and
``adapter.rehydrate_history_from_case`` skip the field BY RULE
(``adapter.NEVER_REHYDRATE_FIELDS``) -- these tests pin that thinking text
NEVER appears in LLM-bound contents, including via the full-fidelity
``parts_blob`` path.

Offline: fake stream events; no network, no model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapter import (
    NEVER_REHYDRATE_FIELDS,
    FunctionCallEvent,
    GeminiSettings,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    build_contents_from_history,
    rehydrate_history_from_case,
)
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.case import CaseChatMessage

_SECRET = "NEVER-IN-LLM-CONTENTS-8f3a"


# ---------------------------------------------------------------------------
# Harness: drive _stream_gemini_reply against a canned StreamEvent script
# ---------------------------------------------------------------------------


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


def _event_stream_factory(rounds: list[list]):
    """Return a stream_events_with_contents stand-in replaying ``rounds`` --
    one inner list of StreamEvents per model round."""
    calls = {"n": 0}

    async def _fake_stream(*_args, **_kwargs):
        idx = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        for ev in rounds[idx]:
            yield ev

    return _fake_stream


async def _drive_events(
    rounds, *, show_thinking, dispatch_results=None,
    user_text="run a flood simulation",
):
    """Run one turn; return (socket, persisted-agent-rows as kwargs list)."""
    persisted: list[dict] = []

    async def _capture_persist(_state, **kw):
        persisted.append(kw)

    async def _dispatch(_ws, _state, name, _args):
        return (dispatch_results or {}).get(name, {"status": "ok"})

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(
        agent_server, "stream_events_with_contents", _event_stream_factory(rounds)
    ), patch.object(
        agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch
    ), patch.object(
        agent_server, "build_tool_declarations", return_value=[]
    ), patch.object(
        agent_server, "_persist_chat_turn", side_effect=_capture_persist
    ):
        await agent_server._stream_gemini_reply(
            sock,
            state,
            _settings(),
            user_text,
            "research",
            show_thinking=show_thinking,
        )
    return sock, persisted


# ---------------------------------------------------------------------------
# Persist path: thinking rides the SAME agent row as the answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_persisted_on_same_agent_row():
    rounds = [
        [
            ThinkingDeltaEvent(delta="Let me think. "),
            ThinkingDeltaEvent(delta="OK."),
            TextDeltaEvent(delta="The answer is 42."),
        ]
    ]
    _sock, persisted = await _drive_events(rounds, show_thinking=True)
    agent_rows = [p for p in persisted if p.get("role") == "agent"]
    assert len(agent_rows) == 1
    row = agent_rows[0]
    assert row["content"] == "The answer is 42."
    assert row["thinking"] == "Let me think. OK."


@pytest.mark.asyncio
async def test_no_thinking_field_when_toggle_off():
    """show_thinking OFF -> the row persists with thinking=None even if the
    model leaked reasoning deltas."""
    rounds = [
        [
            ThinkingDeltaEvent(delta="leaked reasoning"),
            TextDeltaEvent(delta="Answer."),
        ]
    ]
    _sock, persisted = await _drive_events(rounds, show_thinking=False)
    agent_rows = [p for p in persisted if p.get("role") == "agent"]
    assert len(agent_rows) == 1
    assert agent_rows[0].get("thinking") is None


@pytest.mark.asyncio
async def test_thinking_only_segment_attaches_to_next_answer_row():
    """Round 1: thinking + tool call, NO text (thinking-only segment persists
    no row -- the no-phantom-bubble invariant). Round 2: the answer. The
    round-1 thinking must attach to the answer row, not be dropped."""
    rounds = [
        [
            ThinkingDeltaEvent(delta="I should geocode first. "),
            FunctionCallEvent(name="geocode_location", call_id="c1",
                              args={"query": "Tampa"}),
        ],
        [
            ThinkingDeltaEvent(delta="Now I can answer."),
            TextDeltaEvent(delta="Tampa located."),
        ],
    ]
    _sock, persisted = await _drive_events(
        rounds,
        show_thinking=True,
        dispatch_results={"geocode_location": {"status": "ok"}},
        # A pure locate ask so the bare-geocode backstop does not add a
        # nudged retry round (which would replay + double the terminal text).
        user_text="where is Tampa?",
    )
    agent_rows = [p for p in persisted if p.get("role") == "agent"]
    assert len(agent_rows) == 1
    assert agent_rows[0]["content"] == "Tampa located."
    assert agent_rows[0]["thinking"] == "I should geocode first. Now I can answer."


@pytest.mark.asyncio
async def test_persist_chat_turn_writes_thinking_field_on_row():
    """_persist_chat_turn passes ``thinking`` into the CaseChatMessage row."""
    state = agent_server.SessionState(session_id=new_ulid())
    case_id = new_ulid()
    p = MagicMock()
    p.append_chat_message = AsyncMock()
    with patch.object(agent_server, "get_persistence", return_value=p), \
         patch.object(agent_server, "_touch_session_record", new=AsyncMock()):
        await agent_server._persist_chat_turn(
            state,
            role="agent",
            content="the answer",
            case_id=case_id,
            thinking="the reasoning",
        )
    assert p.append_chat_message.await_count == 1
    msg = p.append_chat_message.await_args.args[0]
    assert isinstance(msg, CaseChatMessage)
    assert msg.thinking == "the reasoning"
    assert msg.content == "the answer"
    # And the wire/storage dump carries the interface field name verbatim.
    assert msg.model_dump(mode="json")["thinking"] == "the reasoning"


def test_case_chat_message_thinking_is_additive():
    """Pre-existing rows (no thinking key) validate unchanged -> None."""
    msg = CaseChatMessage(
        message_id=new_ulid(),
        case_id=new_ulid(),
        role="agent",
        content="old row",
        created_at=now_utc(),
    )
    assert msg.thinking is None


# ---------------------------------------------------------------------------
# NEVER-REHYDRATE guard: thinking never reaches LLM-bound contents
# ---------------------------------------------------------------------------


def _all_part_texts(contents) -> str:
    chunks: list[str] = []
    for content in contents or []:
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def test_never_rehydrate_fields_names_thinking():
    assert "thinking" in NEVER_REHYDRATE_FIELDS


def test_history_entry_thinking_never_reaches_contents():
    history = [
        {"role": "user", "text": "run a flood sim"},
        {"role": "agent", "text": "done", "thinking": _SECRET},
    ]
    contents = build_contents_from_history("next question", history)
    joined = _all_part_texts(contents)
    assert "done" in joined
    assert _SECRET not in joined


def test_parts_blob_thinking_never_reaches_contents():
    """The full-fidelity parts_blob path strips a leaked thinking key BY RULE."""
    history = [
        {
            "role": "agent",
            # A hypothetical future writer that leaks thinking INTO the blob
            # entry must still never re-inject it into the model contents.
            "parts_blob": [
                {"text": "the visible answer", "thinking": _SECRET},
                {
                    "function_call": {"name": "fetch_dem", "args": {}},
                    "thinking": _SECRET,
                },
            ],
            "thinking": _SECRET,
        },
    ]
    contents = build_contents_from_history("next", history)
    joined = _all_part_texts(contents)
    assert "the visible answer" in joined
    assert _SECRET not in joined
    # The function_call part survived the strip (only thinking was removed).
    fc_names = [
        part.function_call.name
        for content in contents
        for part in (content.parts or [])
        if getattr(part, "function_call", None) is not None
    ]
    assert "fetch_dem" in fc_names


def test_rehydrate_chain_never_carries_thinking():
    """Persisted rows WITH thinking -> rehydrate -> build_contents: the
    reasoning text appears nowhere in the LLM-bound contents."""
    rows = [
        CaseChatMessage(
            message_id=new_ulid(),
            case_id=new_ulid(),
            role="user",
            content="run a flood sim for Tampa",
            created_at=now_utc(),
        ),
        CaseChatMessage(
            message_id=new_ulid(),
            case_id=new_ulid(),
            role="agent",
            content="Flood sim complete.",
            thinking=_SECRET,
            created_at=now_utc(),
        ),
    ]
    history, dropped = rehydrate_history_from_case(rows)
    assert dropped == 0
    # No history dict may carry the guarded field at all.
    assert all("thinking" not in entry for entry in history)
    contents = build_contents_from_history("what about Miami?", history)
    joined = _all_part_texts(contents)
    assert "Flood sim complete." in joined
    assert _SECRET not in joined


def test_rehydrate_chain_dict_rows_never_carry_thinking():
    """Same chain with raw dict rows (the storage-shaped duck-typed path)."""
    rows = [
        {"role": "agent", "content": "Answer.", "thinking": _SECRET},
    ]
    history, _ = rehydrate_history_from_case(rows)
    contents = build_contents_from_history("next", history)
    joined = _all_part_texts(contents)
    assert "Answer." in joined
    assert _SECRET not in joined
