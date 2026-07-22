"""Part A (compaction UX) -- the durable "Compacting conversation..." /
"Conversation compacted (Nk -> Mk tokens)" pipeline card.

``openai_adapter.stream_openai`` yields a ``CompactionStartEvent`` /
``CompactionCompleteEvent`` pair whenever ``context_budget.compact_contents``
actually changes something (proactive, before the request; reactive, after a
detected clip -- see ``tests/test_openai_adapter.py``). ``server.py``'s
dispatch loop turns that pair into a durable ``pipeline_emitter`` card
(``mint_compaction_card`` / ``complete_compaction_card``) instead of the
pre-Part-A ``TextDeltaEvent`` note glued onto the model's own reply -- the
SAME F10 running-tool-card treatment (animated live) plus the two-card SIM
observability's running-then-upsert-terminal durability (task-208): the
running card persists at mint, and the terminal write UPSERTS the SAME row.

Two layers of coverage, mirroring the existing siblings:

  PART 1 (card-lifecycle durability, mirrors test_sim_card_persistence_task208.py):
    mint_compaction_card / complete_compaction_card driven directly against a
    REAL emitter + file-backed persistence -- running row persists at mint,
    terminal write upserts the SAME row with the renamed label + token counts.

  PART 2 (full dispatch-loop integration, mirrors
    test_context_window_abort_persistence.py): drives the REAL
    ``_stream_gemini_reply`` / ``_dispatch_gemini_and_persist`` seam with a
    mocked ``stream_events_with_contents`` that yields the typed compaction
    events -- proves server.py's wiring end-to-end, and that NO card (and no
    stray narration note) appears when compaction never fires.
"""

from __future__ import annotations

import pytest

from grace2_agent import server
from grace2_agent.adapter import (
    CompactionCompleteEvent,
    CompactionStartEvent,
    GeminiSettings,
    TextDeltaEvent,
)
from grace2_agent.context_budget import COMPACTING_LABEL, compaction_complete_label
from grace2_agent.pipeline_emitter import complete_compaction_card, mint_compaction_card
from grace2_agent.persistence import make_file_persistence
from grace2_contracts.case import CaseCommandEnvelopePayload
from grace2_contracts.common import new_ulid


class FakeWS:
    """Minimal ServerConnection stand-in: records every envelope sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture()
def file_persistence(tmp_path):
    p = make_file_persistence(base_dir=tmp_path)
    server.set_persistence(p)
    try:
        yield p
    finally:
        server.set_persistence(None)


@pytest.fixture(autouse=True)
def _clean_session_registry():
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()


async def _create_case(ws, state, title="Compaction Card Case") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


def _tool_rows(session_state):
    return [m for m in session_state.chat_history if m.role == "tool"]


# --------------------------------------------------------------------------- #
# PART 1 -- card-lifecycle durability (mint_compaction_card / complete_compaction_card)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mint_persists_running_card(file_persistence) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    server._ensure_emitter(ws, state)  # wires the real _tool_card_persist hook
    case_id = await _create_case(ws, state)

    step_id = await mint_compaction_card(emitter=state.emitter)
    assert step_id is not None

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = _tool_rows(session_state)
    assert len(tool_rows) == 1, (
        "the running card must persist the MOMENT it is minted (NATE "
        "'nothing about the chat is transient') so a mid-pass reconnect/reopen "
        "replays the spinning card"
    )
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.tool_name == "context:compact"
    assert card.label == COMPACTING_LABEL
    assert card.state == "running"


@pytest.mark.asyncio
async def test_complete_upserts_the_same_row_with_token_counts(file_persistence) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    server._ensure_emitter(ws, state)
    case_id = await _create_case(ws, state)

    step_id = await mint_compaction_card(emitter=state.emitter)
    await complete_compaction_card(
        emitter=state.emitter, step_id=step_id, before_tokens=12800, after_tokens=3900
    )

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = _tool_rows(session_state)
    assert len(tool_rows) == 1, (
        "the terminal write must UPSERT the running row (stable card_message_id) "
        "-- never append a second row for the same compaction pass"
    )
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.state == "complete"
    assert card.label == compaction_complete_label(12800, 3900)
    assert card.label == "Conversation compacted (13k -> 4k tokens)"
    assert card.tool_name == "context:compact"


@pytest.mark.asyncio
async def test_complete_is_a_noop_when_mint_returned_none(file_persistence) -> None:
    """``step_id=None`` (mint failed, or the emitter was never bound) must
    never raise and must never fabricate a card."""
    await complete_compaction_card(
        emitter=None, step_id=None, before_tokens=1000, after_tokens=200
    )  # must not raise

    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    server._ensure_emitter(ws, state)
    case_id = await _create_case(ws, state)
    await complete_compaction_card(
        emitter=state.emitter, step_id=None, before_tokens=1000, after_tokens=200
    )
    session_state = await file_persistence.get_session_state(case_id)
    assert _tool_rows(session_state) == []


@pytest.mark.asyncio
async def test_mint_with_no_emitter_is_a_noop() -> None:
    step_id = await mint_compaction_card(emitter=None)
    assert step_id is None


# --------------------------------------------------------------------------- #
# PART 2 -- full dispatch-loop integration (server._stream_gemini_reply /
# _dispatch_gemini_and_persist against a mocked stream_events_with_contents)
# --------------------------------------------------------------------------- #


async def _drive_real_stream(ws, state, fake_stream):
    """Drive REAL ``_stream_gemini_reply`` via ``_dispatch_gemini_and_persist``
    with a mocked ``stream_events_with_contents`` (``fake_stream``)."""
    from unittest.mock import patch

    from grace2_agent import server as agent_server

    settings = GeminiSettings(
        model="m", project="p", location="us-central1", use_vertex=True
    )
    with patch.object(agent_server, "build_client", return_value=object()), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "stream_events_with_contents", fake_stream):
        await agent_server._dispatch_gemini_and_persist(
            ws, state, settings, "do the thing", "research"
        )


@pytest.mark.asyncio
async def test_compaction_events_mint_and_complete_a_card_end_to_end(
    file_persistence,
) -> None:
    """The full server wiring: CompactionStartEvent -> CompactionCompleteEvent
    -> a single terminal ``context:compact`` card, and the model's own reply
    carries NO stray compaction note (Part A removed the OPEN-14 narration
    seam -- the card is the ONLY signal now)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    async def fake_stream(*_args, **_kwargs):
        yield CompactionStartEvent()
        yield CompactionCompleteEvent(before_tokens=12800, after_tokens=3900)
        yield TextDeltaEvent(delta="the compacted-context answer")

    await _drive_real_stream(ws, state, fake_stream)

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = _tool_rows(session_state)
    compaction_rows = [r for r in tool_rows if r.tool_card and r.tool_card.tool_name == "context:compact"]
    assert len(compaction_rows) == 1, [r.tool_card for r in tool_rows]
    card = compaction_rows[0].tool_card
    assert card.state == "complete"
    assert card.label == "Conversation compacted (13k -> 4k tokens)"

    agent_rows = [m for m in session_state.chat_history if m.role == "agent"]
    assert len(agent_rows) == 1
    # The reply is exactly the streamed text -- no glued-on compaction note.
    assert agent_rows[0].content == "the compacted-context answer"
    assert "context window" not in agent_rows[0].content.lower()
    assert "summarized" not in agent_rows[0].content.lower()


@pytest.mark.asyncio
async def test_no_compaction_events_means_no_card(file_persistence) -> None:
    """A turn that never triggers compaction persists no ``context:compact``
    row at all -- the card is conditional on the events actually firing."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    async def fake_stream(*_args, **_kwargs):
        yield TextDeltaEvent(delta="a plain answer, no compaction involved")

    await _drive_real_stream(ws, state, fake_stream)

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = _tool_rows(session_state)
    assert not any(r.tool_card and r.tool_card.tool_name == "context:compact" for r in tool_rows)


@pytest.mark.asyncio
async def test_two_compaction_passes_in_one_turn_each_get_their_own_card(
    file_persistence,
) -> None:
    """A round that compacts proactively AND then reactively (the clip-guard
    retry) mints/completes TWO independent cards, not one card overwritten
    twice -- each CompactionStartEvent opens a fresh step."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    async def fake_stream(*_args, **_kwargs):
        yield CompactionStartEvent()
        yield CompactionCompleteEvent(before_tokens=20000, after_tokens=8000)
        yield TextDeltaEvent(delta="round 1 text (clipped)")
        yield CompactionStartEvent()
        yield CompactionCompleteEvent(before_tokens=8000, after_tokens=3000)
        yield TextDeltaEvent(delta="round 2 text (clean)")

    await _drive_real_stream(ws, state, fake_stream)

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = _tool_rows(session_state)
    compaction_rows = [
        r for r in tool_rows if r.tool_card and r.tool_card.tool_name == "context:compact"
    ]
    assert len(compaction_rows) == 2, [r.tool_card for r in tool_rows]
    labels = sorted(r.tool_card.label for r in compaction_rows)
    assert labels == sorted(
        [
            "Conversation compacted (20k -> 8k tokens)",
            "Conversation compacted (8k -> 3k tokens)",
        ]
    )
    assert all(r.tool_card.state == "complete" for r in compaction_rows)
