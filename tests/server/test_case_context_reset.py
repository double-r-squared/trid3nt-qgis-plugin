"""A Case switch resets the LLM context.

``build_contents_from_history`` otherwise keeps feeding ``state.chat_history``
from the previous Case, so a post-switch prompt routes to that Case's composer.
Case SELECT and case CREATE both clear the per-connection conversation."""

from __future__ import annotations

import asyncio
import json

from trid3nt_server.server import SessionState, _emit_case_open
from trid3nt_contracts import new_ulid


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _dirty_state() -> SessionState:
    state = SessionState(session_id=new_ulid())
    state.chat_history.append({"role": "user", "text": "model the Twin Falls spill"})
    state.chat_history.append({"role": "model", "text": "running MODFLOW..."})
    state.turn_count = 7
    return state


def test_case_select_clears_llm_context() -> None:
    state = _dirty_state()
    ws = _FakeWS()
    # Persistence unbound → _emit_case_open emits the empty-session fallback,
    # which still exercises the context-reset lines (they run before the
    # persistence check).
    asyncio.run(_emit_case_open(ws, state, new_ulid()))

    assert state.chat_history == []
    assert state.turn_count == 0
    assert any(e.get("type") == "case-open" for e in ws.sent)


def test_case_select_sets_active_case() -> None:
    state = _dirty_state()
    case_id = new_ulid()
    asyncio.run(_emit_case_open(_FakeWS(), state, case_id))
    assert state.active_case_id == case_id
