"""Unit + integration tests for AUTO-CREATE CASE FROM ROOT (job-0262).

Live-demo finding: a chat prompt sent from the Cases root (no active Case)
ran stateless — no Case, no Case view / layer panel, orphaned results.
The fix: ``_prepare_user_turn`` (the pre-dispatch sequence every
``user-message`` runs BEFORE its turn task is created) now mints + activates
a prompt-named Case when a non-directive message arrives with no active
Case, persists the user turn into it, and emits ``case-open`` + ``case-list``
so the web client flips from the Cases root into the Case view.

Coverage:
- ``test_root_prompt_creates_named_active_case_before_turn`` — Case exists,
  is named from the prompt, and is ACTIVE when ``_prepare_user_turn``
  returns (i.e. before the LLM turn task would start).
- ``test_root_prompt_user_turn_persisted_into_new_case`` — the triggering
  message is the new Case's first persisted chat turn.
- ``test_root_prompt_emits_case_open_then_case_list`` — envelope order +
  case-open rehydration carries the first message (Chat.tsx's
  replace-not-reconcile flush must not blank the just-typed bubble).
- ``test_root_prompt_layer_attribution_lands_in_new_case`` — per-turn layer
  emissions recorded after the auto-create attribute into the new Case.
- ``test_root_prompt_does_not_reset_llm_context_or_turn_count`` — the
  message IS the first turn: no job-0245-style context clear.
- ``test_degenerate_prompt_falls_back_to_untitled`` — title fallback.
- ``test_autoname_probe_skipped_for_auto_created_case`` — job-0260 rename
  probe is a no-op for the already-named auto-created Case.
- ``test_existing_case_path_unchanged`` — an active Case means NO new Case
  and NO case-open emission; the turn persists into the existing Case.
- ``test_invoke_directive_stays_stateless`` — ``/invoke`` debug directives
  do not mint a Case.
- ``test_no_persistence_stays_stateless`` — Persistence unbound = M1
  stateless path, no envelopes.
- ``test_upsert_failure_falls_back_to_stateless`` — a failing upsert leaves
  the session at root (no active Case, no envelopes).
- ``test_integration_two_root_prompts_one_case`` — the live repro: two
  consecutive root prompts produce exactly ONE Case holding both turns.
"""

from __future__ import annotations

import asyncio

import pytest

from grace2_agent import server as server_mod
from grace2_agent.persistence import CASES_COLLECTION, Persistence
from grace2_agent.server import (
    SessionState,
    _auto_create_case_from_root,
    _maybe_autoname_case,
    _persist_chat_turn,
    _prepare_user_turn,
    get_persistence,
    set_persistence,
)
from grace2_contracts.common import new_ulid

from .test_persistence import MockMCPClient, _fresh_case_summary
from .test_server_case_handlers import MockWebSocket


@pytest.fixture()
def _persistence_bound():
    """Bind a fresh ``Persistence`` (MockMCPClient-backed); restore on exit."""
    saved = get_persistence()
    p = Persistence(MockMCPClient())
    set_persistence(p)
    try:
        yield p
    finally:
        set_persistence(saved)


def _fresh_state() -> SessionState:
    return SessionState(session_id=new_ulid())


PROMPT = "Show flood risk near Fort Myers after Hurricane Ian"


def _case_docs(p: Persistence) -> list[dict]:
    """All raw Case docs the mock MCP client holds."""
    mcp = p._mcp  # type: ignore[attr-defined]
    return list(mcp._store.get(CASES_COLLECTION, {}).values())


# --------------------------------------------------------------------------- #
# Root-prompt auto-create
# --------------------------------------------------------------------------- #


def test_root_prompt_creates_named_active_case_before_turn(
    _persistence_bound: Persistence,
) -> None:
    """No active Case + chat prompt -> Case created, prompt-named, ACTIVE
    by the time ``_prepare_user_turn`` returns (= before the turn task)."""
    ws = MockWebSocket()
    state = _fresh_state()
    assert state.active_case_id is None

    directive = asyncio.run(_prepare_user_turn(ws, state, PROMPT))

    assert directive is None  # Gemini path
    case_id = state.active_case_id
    assert case_id is not None
    case = asyncio.run(_persistence_bound.get_case(case_id))
    assert case is not None
    assert case.status == "active"
    # Named via the job-0260 heuristic, not left untitled.
    assert case.title != "Untitled Case"
    assert "Flood" in case.title and "Fort" in case.title
    # The connection context is synced to the new Case (no reset on the
    # next dispatch).
    assert state.case_context_synced_to == case_id


def test_root_prompt_user_turn_persisted_into_new_case(
    _persistence_bound: Persistence,
) -> None:
    """The triggering message is the new Case's FIRST persisted chat turn."""
    ws = MockWebSocket()
    state = _fresh_state()
    asyncio.run(_prepare_user_turn(ws, state, PROMPT))

    session_state = asyncio.run(
        _persistence_bound.get_session_state(state.active_case_id)
    )
    assert len(session_state.chat_history) == 1
    msg = session_state.chat_history[0]
    assert msg.role == "user"
    assert msg.content == PROMPT


def test_root_prompt_emits_case_open_then_case_list(
    _persistence_bound: Persistence,
) -> None:
    """case-open (with the first message in the rehydration) then case-list —
    the web hub fans case-open to App.tsx so the UI leaves the Cases root."""
    ws = MockWebSocket()
    state = _fresh_state()
    # job-0252 (OQ-0115): the handshake binds authenticated_user_id before the
    # turn; the auto-create path stamps it as the Case owner and the owner-
    # scoped _emit_case_list lists by it. Simulate the bound user.
    state.authenticated_user_id = new_ulid()
    asyncio.run(_prepare_user_turn(ws, state, PROMPT))

    types = [env["type"] for env in ws.sent]
    assert "case-open" in types
    assert "case-list" in types
    assert types.index("case-open") < types.index("case-list")

    case_open = next(env for env in ws.sent if env["type"] == "case-open")
    ss = case_open["payload"]["session_state"]
    # A null session_state would null the client's activeCaseId — must be
    # the hydrated snapshot.
    assert ss is not None
    assert ss["case"]["case_id"] == state.active_case_id
    # Chat.tsx's case-open handler FLUSHES the local buffer and re-renders
    # from chat_history — the just-typed message must be in the payload.
    history = ss["chat_history"]
    assert len(history) == 1
    assert history[0]["content"] == PROMPT

    case_list = next(env for env in ws.sent if env["type"] == "case-list")
    listed_ids = [c["case_id"] for c in case_list["payload"]["cases"]]
    assert state.active_case_id in listed_ids


def test_root_prompt_layer_attribution_lands_in_new_case(
    _persistence_bound: Persistence,
) -> None:
    """Per-turn layer emissions recorded AFTER the auto-create attribute into
    the new Case (the orphaned-results half of the live repro)."""
    ws = MockWebSocket()
    state = _fresh_state()
    asyncio.run(_prepare_user_turn(ws, state, PROMPT))
    case_id = state.active_case_id
    assert case_id is not None
    # The emitter accumulator was flushed for the fresh Case.
    assert state.emitter is not None
    assert state.emitter.loaded_layers == []

    # Simulate the turn's tool side-effects: a layer emission lands in the
    # per-turn accumulator, then the agent reply persists (the
    # _dispatch_gemini_and_persist finally-block path).
    state.current_turn_layer_ids = ["flood-depth-fort-myers"]
    asyncio.run(
        _persist_chat_turn(
            state, role="agent", content="", pipeline_id=new_ulid()
        )
    )
    session_state = asyncio.run(
        _persistence_bound.get_session_state(case_id)
    )
    agent_msgs = [m for m in session_state.chat_history if m.role == "agent"]
    assert len(agent_msgs) == 1
    assert agent_msgs[0].layer_emissions == ["flood-depth-fort-myers"]


def test_root_prompt_does_not_reset_llm_context_or_turn_count(
    _persistence_bound: Persistence,
) -> None:
    """The message IS the first turn: auto-create must NOT run the
    case-command(create) reset (chat_history clear / turn_count zero)."""
    ws = MockWebSocket()
    state = _fresh_state()
    # Simulate the dispatcher's pre-existing per-turn state: the FR-FR-3 cap
    # counter was already incremented for this in-flight turn, and the
    # connection had previously synced to root (so _sync_case_context is a
    # no-op and does not clear).
    state.turn_count = 3
    state.case_context_synced_to = None
    state.chat_history.append({"role": "user", "text": "earlier root turn"})

    asyncio.run(_prepare_user_turn(ws, state, PROMPT))

    assert state.active_case_id is not None
    assert state.turn_count == 3  # untouched
    assert state.chat_history == [
        {"role": "user", "text": "earlier root turn"}
    ]  # untouched — replace-not-reconcile reset NOT applied here


def test_degenerate_prompt_falls_back_to_untitled(
    _persistence_bound: Persistence,
) -> None:
    """A prompt _derive_case_title can't name still gets a Case."""
    ws = MockWebSocket()
    state = _fresh_state()
    asyncio.run(_prepare_user_turn(ws, state, "hi"))
    case = asyncio.run(_persistence_bound.get_case(state.active_case_id))
    assert case is not None
    assert case.title == "Untitled Case"


def test_autoname_probe_skipped_for_auto_created_case(
    _persistence_bound: Persistence,
) -> None:
    """job-0260's end-of-stream rename probe is a no-op for the auto-created
    Case (it was named at creation from the same prompt)."""
    ws = MockWebSocket()
    state = _fresh_state()
    case_id = asyncio.run(_auto_create_case_from_root(ws, state, PROMPT))
    assert case_id is not None
    assert case_id in server_mod._AUTONAMED_CASES
    renamed = asyncio.run(_maybe_autoname_case(state, "totally different text"))
    assert renamed is False
    case = asyncio.run(_persistence_bound.get_case(case_id))
    assert "Flood" in case.title  # original prompt-derived title intact


def test_root_prompt_rehydration_failure_still_emits_nonnull_case_open(
    _persistence_bound: Persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NATE 2026-06-26: when get_session_state momentarily fails, the auto
    case-open must STILL carry a non-null session_state.case (the just-created
    Case). A null/absent case-open would leave the client's activeCaseId
    unchanged so it never leaves the Cases root, and the turn would then
    dispatch with the new case bound -> cards stamped with a case_id the
    client never opened (nothing renders until reload)."""
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()

    # Force the richer rehydration to raise so the fallback branch fires. The
    # Case itself was already upserted (auto-create persists before this), so
    # the minimal fallback re-fetch still finds it.
    async def _boom(_case_id):  # noqa: ANN001
        raise RuntimeError("rehydration down")

    monkeypatch.setattr(_persistence_bound, "get_session_state", _boom)

    asyncio.run(_prepare_user_turn(ws, state, PROMPT))

    case_open = next(
        (env for env in ws.sent if env["type"] == "case-open"), None
    )
    assert case_open is not None, "expected a case-open even on rehydration fail"
    ss = case_open["payload"]["session_state"]
    # The core guarantee: a NON-NULL session_state with the new Case so the
    # client flips out of the Cases root (a null session_state would null
    # activeCaseId — see the test note above).
    assert ss is not None
    assert ss["case"] is not None
    assert ss["case"]["case_id"] == state.active_case_id


# --------------------------------------------------------------------------- #
# Paths that must NOT auto-create
# --------------------------------------------------------------------------- #


def test_existing_case_path_unchanged(
    _persistence_bound: Persistence,
) -> None:
    """An active Case means no new Case, no case-open frame; the turn
    persists into the EXISTING Case (pre-job-0262 behavior preserved)."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))
    ws = MockWebSocket()
    state = _fresh_state()
    state.active_case_id = case.case_id
    state.case_context_synced_to = case.case_id

    directive = asyncio.run(_prepare_user_turn(ws, state, PROMPT))

    assert directive is None
    assert state.active_case_id == case.case_id  # unchanged
    assert ws.sent == []  # no case-open / case-list churn mid-Case
    assert len(_case_docs(_persistence_bound)) == 1  # no second Case minted
    session_state = asyncio.run(
        _persistence_bound.get_session_state(case.case_id)
    )
    assert [m.content for m in session_state.chat_history] == [PROMPT]


def test_invoke_directive_stays_stateless(
    _persistence_bound: Persistence,
) -> None:
    """``/invoke`` operator-debug directives from root do NOT mint a Case."""
    ws = MockWebSocket()
    state = _fresh_state()
    directive = asyncio.run(
        _prepare_user_turn(ws, state, '/invoke geocode_location {"query": "x"}')
    )
    assert directive == ("geocode_location", {"query": "x"})
    assert state.active_case_id is None
    assert ws.sent == []
    assert _case_docs(_persistence_bound) == []


def test_no_persistence_stays_stateless() -> None:
    """Persistence unbound -> M1 stateless path: no Case, no envelopes."""
    saved = get_persistence()
    set_persistence(None)
    try:
        ws = MockWebSocket()
        state = _fresh_state()
        directive = asyncio.run(_prepare_user_turn(ws, state, PROMPT))
        assert directive is None
        assert state.active_case_id is None
        assert ws.sent == []
    finally:
        set_persistence(saved)


def test_upsert_failure_falls_back_to_stateless(
    _persistence_bound: Persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing Case upsert leaves the session at root — the turn still
    dispatches (stateless), nothing half-activated."""

    async def _boom(_case):  # noqa: ANN001
        raise RuntimeError("mongo down")

    monkeypatch.setattr(_persistence_bound, "upsert_case", _boom)
    ws = MockWebSocket()
    state = _fresh_state()
    directive = asyncio.run(_prepare_user_turn(ws, state, PROMPT))
    assert directive is None
    assert state.active_case_id is None
    assert ws.sent == []


# --------------------------------------------------------------------------- #
# Integration: the live repro
# --------------------------------------------------------------------------- #


def test_integration_two_root_prompts_one_case(
    _persistence_bound: Persistence,
) -> None:
    """The demo repro: two consecutive prompts from the Cases root. The first
    mints the Case; the second lands in it (no duplicate Case, both turns
    persisted, case-open emitted exactly once)."""
    ws = MockWebSocket()
    state = _fresh_state()

    asyncio.run(_prepare_user_turn(ws, state, PROMPT))
    first_case = state.active_case_id
    assert first_case is not None

    asyncio.run(_prepare_user_turn(ws, state, "Now add building footprints"))
    assert state.active_case_id == first_case  # same Case
    assert len(_case_docs(_persistence_bound)) == 1

    session_state = asyncio.run(
        _persistence_bound.get_session_state(first_case)
    )
    assert [m.content for m in session_state.chat_history] == [
        PROMPT,
        "Now add building footprints",
    ]
    # case-open fired once (for the auto-create), not on the second turn.
    assert [e["type"] for e in ws.sent].count("case-open") == 1


# --------------------------------------------------------------------------- #
# A3 (NATE 2026-07-20): a fresh Untitled Case must auto-name from its FIRST user
# message reliably -- even when the turn later fails (LLM_UNAVAILABLE etc.) and
# even across a transient persistence miss (the guard must not burn the one
# naming attempt up front).
# --------------------------------------------------------------------------- #


def _untitled_case():
    """A fresh Case that is genuinely Untitled (not creation-named)."""
    return _fresh_case_summary().model_copy(update={"title": "Untitled Case"})


def test_a3_fresh_untitled_case_names_from_first_message(
    _persistence_bound: Persistence,
) -> None:
    """A brand-new Untitled Case + its first user prompt -> a derived title."""
    case = _untitled_case()
    asyncio.run(_persistence_bound.upsert_case(case))
    server_mod._AUTONAMED_CASES.discard(case.case_id)
    state = _fresh_state()
    state.active_case_id = case.case_id

    named = asyncio.run(
        _maybe_autoname_case(
            state, "Simulate a dye plume traveling downstream in the river"
        )
    )
    assert named is True
    stored = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert stored.title != "Untitled Case"
    assert "Dye" in stored.title  # derived from the first prompt's tokens


def test_a3_name_survives_a_failing_turn(
    _persistence_bound: Persistence,
) -> None:
    """The name lands PRE-dispatch, so a turn that raises afterwards (the
    LLM_UNAVAILABLE root cause) does not clear it."""
    case = _untitled_case()
    asyncio.run(_persistence_bound.upsert_case(case))
    server_mod._AUTONAMED_CASES.discard(case.case_id)
    state = _fresh_state()
    state.active_case_id = case.case_id

    assert asyncio.run(_maybe_autoname_case(state, PROMPT)) is True
    named_title = asyncio.run(_persistence_bound.get_case(case.case_id)).title
    assert named_title != "Untitled Case"

    # Model dispatch raises AFTER the pre-dispatch autoname already landed.
    async def _failing_turn() -> None:
        raise RuntimeError("LLM_UNAVAILABLE")

    with pytest.raises(RuntimeError):
        asyncio.run(_failing_turn())

    still = asyncio.run(_persistence_bound.get_case(case.case_id)).title
    assert still == named_title  # name preserved across the failed turn


def test_a3_transient_miss_does_not_forfeit_name(
    _persistence_bound: Persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient persistence miss on the first attempt must NOT permanently
    mark the Case 'named' -- the next turn still names it (pre-fix the guard was
    set unconditionally up front, so any early miss burned the only attempt)."""
    case = _untitled_case()
    asyncio.run(_persistence_bound.upsert_case(case))
    server_mod._AUTONAMED_CASES.discard(case.case_id)
    state = _fresh_state()
    state.active_case_id = case.case_id

    calls = {"n": 0}
    orig_get = _persistence_bound.get_case

    async def _flaky_get(cid):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient persistence hiccup")
        return await orig_get(cid)

    monkeypatch.setattr(_persistence_bound, "get_case", _flaky_get)

    first = asyncio.run(_maybe_autoname_case(state, PROMPT))
    assert first is False  # transient error swallowed
    assert case.case_id not in server_mod._AUTONAMED_CASES  # NOT burned

    second = asyncio.run(_maybe_autoname_case(state, PROMPT))
    assert second is True  # retried + named on the next turn
    stored = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert stored.title != "Untitled Case"
