"""job-CASE-AUTHORITY: the CLIENT's current Case is the authority for
turn-binding + reconnect replay (fixes THE SNAP).

ROOT CAUSE (wf_baa3273e): two un-reconciled sources of truth for "which case."
The client never told the server its Case, so a bare ``session-resume {}``
replayed the server's STALE in-memory pointer, and a ``user-message`` bound the
turn to whatever Case the server pointer had drifted to.

FIX surface exercised here (all in server.py + persistence.py):
- ``_handle_session_resume(client_case_id=...)`` re-binds the server's
  active-Case pointer to the client's Case BEFORE the layer replay.
- ``_prepare_user_turn(client_case_id=...)`` re-binds the turn to the
  message's Case before the sync / auto-create / pin.
- ``Persistence.set_session_active_case`` / ``get_session_active_case`` persist
  the pointer so it survives an EC2 auto-stop/restart; the in-memory dict is a
  cache. ``_reload_session_active_case`` warms a fresh SessionState from it.

INVARIANT (job-0356): a genuine fresh reconnect STILL replays the active
Case's rendered layers — we correct WHICH Case, never remove replay.
"""

from __future__ import annotations

import asyncio

import pytest

import trid3nt_server.server as server
from trid3nt_server.persistence import FileMCPClient, Persistence
from trid3nt_server.server import SessionState


SID = "0" * 26
# Valid ULIDs: the first char must be <= '7' (the 48-bit timestamp must not
# overflow), so "A"*26 / "B"*26 are NOT valid ULIDs and fail SessionDocument's
# project_ids ULID validation. A leading "0" keeps the A/B mnemonic while valid.
CASE_A = "0" + "A" * 25
CASE_B = "0" + "B" * 25


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty session-active-case registry +
    no Persistence bound, and restores both after."""
    saved_reg = dict(server._SESSION_ACTIVE_CASE)
    saved_p = server.get_persistence()
    server._SESSION_ACTIVE_CASE.clear()
    server.set_persistence(None)
    try:
        yield
    finally:
        server._SESSION_ACTIVE_CASE.clear()
        server._SESSION_ACTIVE_CASE.update(saved_reg)
        server.set_persistence(saved_p)


def _file_persistence(tmp_path) -> Persistence:
    return Persistence(FileMCPClient(base_dir=tmp_path / "store"))


def _stub_resume(monkeypatch):
    """Stub the heavy resume side-effects so we test the re-bind logic only."""
    class _FakeEmitter:
        async def emit_session_state(self):
            return None

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(
        server,
        "_ensure_emitter",
        lambda ws, s: setattr(s, "emitter", _FakeEmitter()) if s.emitter is None else None,
    )
    monkeypatch.setattr(server, "_rebind_live_turns", lambda *a, **k: 0)
    monkeypatch.setattr(server, "_emit_case_list", _noop)
    monkeypatch.setattr(server, "_emit_turn_complete", _noop)


# --------------------------------------------------------------------------- #
# Persistence round-trip of the active-case pointer (Requirement 4)
# --------------------------------------------------------------------------- #


def test_persistence_active_case_pointer_round_trip(tmp_path):
    p = _file_persistence(tmp_path)

    async def run():
        # No record yet -> None.
        assert await p.get_session_active_case(SID) is None
        # Write -> read back.
        await p.set_session_active_case(SID, CASE_A)
        assert await p.get_session_active_case(SID) == CASE_A
        # Overwrite (a Case switch).
        await p.set_session_active_case(SID, CASE_B)
        assert await p.get_session_active_case(SID) == CASE_B
        # Clear (a Case exit / deselect).
        await p.set_session_active_case(SID, None)
        assert await p.get_session_active_case(SID) is None

    asyncio.run(run())


def test_persistence_pointer_survives_a_fresh_get_session_record(tmp_path):
    """The storage-only ``last_active_case_id`` field never breaks
    ``get_session_record`` (it is dropped before SessionDocument validation)."""
    p = _file_persistence(tmp_path)

    async def run():
        await p.set_session_active_case(SID, CASE_A)
        # A separate touch creates the well-formed header.
        await p.touch_session(SID, case_id=CASE_A)
        rec = await p.get_session_record(SID)
        assert rec is not None  # validated SessionDocument despite the extra field
        # And the pointer is still readable.
        assert await p.get_session_active_case(SID) == CASE_A

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# _handle_session_resume re-binds to the client's Case (Requirement 2)
# --------------------------------------------------------------------------- #


def test_resume_rebinds_active_case_to_client_then_replays(monkeypatch, tmp_path):
    """A client mid-reconnect in CASE_B while the server pointer is stale on
    CASE_A: the resume re-binds to CASE_B BEFORE replay reads active_case_id."""
    _stub_resume(monkeypatch)
    server.set_persistence(_file_persistence(tmp_path))

    st = SessionState(session_id=SID)
    st.active_case_id = CASE_A  # stale server pointer

    replayed_for: list = []

    async def _fake_replay(state):
        replayed_for.append(state.active_case_id)

    monkeypatch.setattr(server, "_replay_active_case_layers", _fake_replay)

    asyncio.run(
        server._handle_session_resume(object(), st, client_case_id=CASE_B)
    )

    # Pointer re-bound to the CLIENT's Case...
    assert st.active_case_id == CASE_B
    # ...BEFORE replay ran (replay saw the corrected Case, not the stale one).
    assert replayed_for == [CASE_B]
    # ...and the sync marker was invalidated so the next turn re-syncs.
    assert st.case_context_synced_to == server._CASE_SYNC_NEVER
    # ...and the pointer was persisted (survives a restart).
    p = server.get_persistence()
    assert asyncio.run(p.get_session_active_case(SID)) == CASE_B


def test_resume_without_case_id_keeps_current_behavior(monkeypatch, tmp_path):
    """An older client (no stamp) leaves the server pointer + replay untouched
    — job-0356 fresh-reconnect replay still runs for the existing Case."""
    _stub_resume(monkeypatch)
    server.set_persistence(_file_persistence(tmp_path))

    st = SessionState(session_id=SID)
    st.active_case_id = CASE_A

    replayed_for: list = []

    async def _fake_replay(state):
        replayed_for.append(state.active_case_id)

    monkeypatch.setattr(server, "_replay_active_case_layers", _fake_replay)

    asyncio.run(server._handle_session_resume(object(), st, client_case_id=None))

    assert st.active_case_id == CASE_A  # unchanged
    assert replayed_for == [CASE_A]  # still replays (invariant preserved)


def test_resume_same_case_is_noop_rebind(monkeypatch, tmp_path):
    """Client and server already agree -> no re-bind, no marker churn, but the
    replay still runs (the durability invariant)."""
    _stub_resume(monkeypatch)
    server.set_persistence(_file_persistence(tmp_path))

    st = SessionState(session_id=SID)
    st.active_case_id = CASE_A
    st.case_context_synced_to = CASE_A  # already synced

    replayed_for: list = []

    async def _fake_replay(state):
        replayed_for.append(state.active_case_id)

    monkeypatch.setattr(server, "_replay_active_case_layers", _fake_replay)

    asyncio.run(
        server._handle_session_resume(object(), st, client_case_id=CASE_A)
    )

    assert st.active_case_id == CASE_A
    assert st.case_context_synced_to == CASE_A  # not invalidated (no change)
    assert replayed_for == [CASE_A]


# --------------------------------------------------------------------------- #
# Cold-start reload from the persisted pointer (Requirement 4)
# --------------------------------------------------------------------------- #


def test_reload_warms_pointer_after_restart(monkeypatch, tmp_path):
    """Fresh process: empty registry, but the persisted pointer is reloaded so
    a bare resume (no client stamp) lands on the right Case."""
    _stub_resume(monkeypatch)
    p = _file_persistence(tmp_path)
    server.set_persistence(p)

    # Simulate a prior process having persisted the pointer.
    asyncio.run(p.set_session_active_case(SID, CASE_A))

    # Fresh SessionState — the in-memory registry is empty (cleared by fixture).
    st = SessionState(session_id=SID)
    assert st.active_case_id is None  # cache cold

    replayed_for: list = []

    async def _fake_replay(state):
        replayed_for.append(state.active_case_id)

    monkeypatch.setattr(server, "_replay_active_case_layers", _fake_replay)

    # Bare resume (older client, no stamp) must still recover CASE_A.
    asyncio.run(server._handle_session_resume(object(), st, client_case_id=None))

    assert st.active_case_id == CASE_A  # warmed from persistence
    assert replayed_for == [CASE_A]


def test_reload_never_overwrites_a_live_pointer(tmp_path):
    """A pointer already live this process (set by a case-command) is the
    truth — reload must not clobber it with a stale persisted value."""
    p = _file_persistence(tmp_path)
    server.set_persistence(p)
    asyncio.run(p.set_session_active_case(SID, CASE_A))  # stale persisted

    st = SessionState(session_id=SID)
    st.active_case_id = CASE_B  # live this process

    asyncio.run(server._reload_session_active_case(st))

    assert st.active_case_id == CASE_B  # live value wins


# --------------------------------------------------------------------------- #
# _prepare_user_turn binds the turn to the message's Case (Requirement 3)
# --------------------------------------------------------------------------- #


def test_user_turn_rebinds_to_message_case(monkeypatch, tmp_path):
    """A 'resize bbox' user-message stamped with CASE_B runs in CASE_B even
    though the server pointer was stale on CASE_A. The turn pin follows."""
    server.set_persistence(_file_persistence(tmp_path))

    synced_to: list = []

    async def _fake_sync(ws, state):
        synced_to.append(state.active_case_id)

    async def _fake_persist_turn(state, *, role, content, **kw):
        return None

    monkeypatch.setattr(server, "_sync_case_context", _fake_sync)
    monkeypatch.setattr(server, "_persist_chat_turn", _fake_persist_turn)

    st = SessionState(session_id=SID)
    st.active_case_id = CASE_A  # stale

    directive = asyncio.run(
        server._prepare_user_turn(
            object(), st, "resize the bbox", client_case_id=CASE_B
        )
    )

    assert directive is None  # Gemini path
    # Re-bound BEFORE the sync (the sync observed the corrected Case).
    assert synced_to == [CASE_B]
    assert st.active_case_id == CASE_B
    # The turn pin follows the corrected Case.
    assert st.current_turn_case_id == CASE_B
    # Persisted for restart durability.
    p = server.get_persistence()
    assert asyncio.run(p.get_session_active_case(SID)) == CASE_B


def test_user_turn_without_case_id_keeps_server_pointer(monkeypatch, tmp_path):
    """No stamp (older client): the turn binds to the server's pointer exactly
    as before (prior behavior preserved)."""
    server.set_persistence(_file_persistence(tmp_path))

    async def _fake_sync(ws, state):
        return None

    async def _fake_persist_turn(state, *, role, content, **kw):
        return None

    monkeypatch.setattr(server, "_sync_case_context", _fake_sync)
    monkeypatch.setattr(server, "_persist_chat_turn", _fake_persist_turn)
    # Guard: auto-create must NOT fire here (there IS an active case).
    monkeypatch.setattr(
        server,
        "_auto_create_case_from_root",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("auto-create fired")),
    )

    st = SessionState(session_id=SID)
    st.active_case_id = CASE_A

    asyncio.run(
        server._prepare_user_turn(object(), st, "hello", client_case_id=None)
    )

    assert st.active_case_id == CASE_A
    assert st.current_turn_case_id == CASE_A
