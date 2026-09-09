"""job-0245 (OQ-0245-CONTEXT-CARRYOVER-MISROUTE): Case switch resets LLM context.

Round-3 live testing proved a reused WS session re-routed EVERY post-switch
prompt to the PREVIOUS Case's composer (a Fort Myers flood ask and a numpy ask
both got the Twin Falls groundwater confirmation gate) because
``build_contents_from_history`` kept feeding ``state.chat_history`` from the
old Case. These tests pin the clean-slate rule (Wave 4.8 A.7, server-side):
case SELECT and case CREATE both clear the per-connection LLM conversation.
"""

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


def test_create_branch_source_clears_context() -> None:
    """Source-level pin for the create path (full handler needs Persistence;
    the reset lines are asserted structurally so a refactor that drops them
    fails loudly)."""
    import inspect

    import trid3nt_server.server as server_mod

    src = inspect.getsource(server_mod._handle_case_command)
    create_idx = src.index('if command == "create"')
    select_idx = src.index('if command == "select"')
    create_block = src[create_idx:select_idx]
    # job-0269: the reset REBINDS (never .clear()) — an in-flight turn holds
    # the old list via its stream-entry capture and must keep it intact.
    assert "state.chat_history = []" in create_block
    assert "state.chat_history.clear()" not in create_block
    assert "state.turn_count = 0" in create_block
