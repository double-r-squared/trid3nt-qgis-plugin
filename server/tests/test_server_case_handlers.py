"""Unit tests for the Case lifecycle handlers in ``server.py`` (job-0121).

These tests exercise the server-side dispatch without binding a real
WebSocket — a ``MockWebSocket`` collects every envelope and the tests
assert on the envelope sequence + the persistence side effects.

Coverage (>=10 unit tests + 1 integration):
- ``test_case_create_emits_case_open_and_case_list`` — create dispatches
  to upsert_case + emits case-open with empty session_state, then case-list.
- ``test_case_select_emits_case_open_with_chat_history`` — select hydrates
  CaseSessionState including chat history.
- ``test_case_rename_updates_title_and_refreshes_case_list`` — rename
  updates persisted title; case-list re-emitted.
- ``test_case_archive_soft_archives_and_refreshes_case_list`` — archive
  flips status; case-list re-emitted.
- ``test_case_delete_soft_deletes_and_clears_active_case`` — delete flips
  status; active_case_id cleared when matching.
- ``test_case_command_without_persistence_emits_error`` — no Persistence
  bound -> INTERNAL_ERROR envelope.
- ``test_case_command_rename_missing_case_id_emits_error`` — rename
  without case_id -> INTERNAL_ERROR.
- ``test_case_command_rename_missing_title_emits_error`` — rename
  without args.title -> INTERNAL_ERROR.
- ``test_emit_case_list_skips_when_persistence_unbound`` — emit silently
  skips with no Persistence.
- ``test_active_case_id_set_after_create_and_select`` — active context
  follows command dispatch.
- ``test_persist_chat_turn_writes_when_active_case_set`` — chat message
  appended to mongo.
- ``test_persist_chat_turn_noop_when_no_active_case`` — no active context
  = no write.
- ``test_integration_e2e_case_flow`` — full flow: create case A, persist
  chat, archive, create case B, verify isolation (case A's chat does NOT
  appear in case B's rehydration).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from trid3nt_server import server as server_mod
from trid3nt_server.persistence import (
    CASES_COLLECTION,
    CHAT_COLLECTION,
    Persistence,
)
from trid3nt_server.server import (
    SessionState,
    _emit_case_list,
    _emit_case_open,
    _handle_case_command,
    _persist_chat_turn,
    get_persistence,
    set_persistence,
)
from trid3nt_contracts.case import (
    CaseChatMessage,
    CaseCommandEnvelopePayload,
    CaseSummary,
)
from trid3nt_contracts.common import new_ulid

from .test_persistence import MockMCPClient, _fresh_case_summary


# --------------------------------------------------------------------------- #
# Mocks
# --------------------------------------------------------------------------- #


class MockWebSocket:
    """Collects every envelope ``send`` would have written to the wire.

    Each entry is the parsed envelope as a dict. The tests assert on
    ``type`` + ``payload`` fields.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))
        else:
            self.sent.append(raw)


@pytest.fixture()
def _persistence_bound():
    """Bind a fresh ``Persistence`` (backed by ``MockMCPClient``) for the test.

    Restores the previous binding on teardown so subsequent tests don't see
    the mock leak.
    """
    saved = get_persistence()
    mock = MockMCPClient()
    p = Persistence(mock)
    set_persistence(p)
    try:
        yield p
    finally:
        set_persistence(saved)


def _fresh_state(session_id: str | None = None) -> SessionState:
    return SessionState(session_id=session_id or new_ulid())


# --------------------------------------------------------------------------- #
# Case lifecycle handlers
# --------------------------------------------------------------------------- #


def test_case_create_emits_case_open_and_case_list(_persistence_bound: Persistence) -> None:
    """``case-command(create)`` upserts the Case, sets active context, emits
    ``case-open`` (empty session_state) then ``case-list`` updated."""
    ws = MockWebSocket()
    state = _fresh_state()
    # job-0252 (OQ-0115): the handshake binds authenticated_user_id before any
    # case-command runs; the create path stamps it as the Case owner and
    # _emit_case_list scopes the listing by it. Simulate the bound user.
    state.authenticated_user_id = new_ulid()
    cmd = CaseCommandEnvelopePayload(
        command="create", args={"title": "My new flood case"}
    )
    asyncio.run(_handle_case_command(ws, state, cmd))

    types = [env["type"] for env in ws.sent]
    assert "case-open" in types
    assert "case-list" in types
    # case-open carries the fresh (empty) session_state for the new Case
    case_open = next(env for env in ws.sent if env["type"] == "case-open")
    assert case_open["payload"]["session_state"] is not None
    assert (
        case_open["payload"]["session_state"]["case"]["title"]
        == "My new flood case"
    )
    # Active context is set to the newly-minted Case
    assert state.active_case_id == case_open["payload"]["session_state"]["case"]["case_id"]
    # case-list carries at least one Case (the one we just created)
    case_list = next(env for env in ws.sent if env["type"] == "case-list")
    case_ids = [c["case_id"] for c in case_list["payload"]["cases"]]
    assert state.active_case_id in case_ids


def test_case_create_with_valid_bbox_persists_and_seeds_aoi(
    _persistence_bound: Persistence,
) -> None:
    """#170 AOI-first: a create command carrying a valid ``args.bbox`` persists
    it on ``CaseSummary.bbox`` AND seeds ``state.case_bbox`` so the FIRST turn's
    ``_turn_case_bbox`` returns the user's pre-set extent."""
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    bbox = [-82.0, 26.5, -81.8, 26.7]
    cmd = CaseCommandEnvelopePayload(
        command="create",
        args={"title": "AOI-first case", "bbox": bbox},
    )
    asyncio.run(_handle_case_command(ws, state, cmd))

    # Persisted on the Case (flows into snapshot/manifest via upsert_case).
    case = asyncio.run(_persistence_bound.get_case(state.active_case_id))
    assert case is not None
    assert list(case.bbox) == bbox
    # In-session AOI anchor seeded for the first turn.
    assert state.case_bbox == bbox


def test_case_create_with_invalid_bbox_leaves_aoi_unset(
    _persistence_bound: Persistence,
) -> None:
    """#170 AOI-first: an invalid ``args.bbox`` (wrong length / non-finite)
    is dropped silently - no crash, ``case.bbox`` + ``state.case_bbox`` both
    stay unset (byte-identical to the pre-#170 no-bbox behaviour)."""
    for bad in ([-82.0, 26.5, -81.8], [float("nan"), 1.0, 2.0, 3.0], "nope"):
        ws = MockWebSocket()
        state = _fresh_state()
        state.authenticated_user_id = new_ulid()
        cmd = CaseCommandEnvelopePayload(
            command="create", args={"title": "no AOI", "bbox": bad}
        )
        asyncio.run(_handle_case_command(ws, state, cmd))

        case = asyncio.run(_persistence_bound.get_case(state.active_case_id))
        assert case is not None
        assert case.bbox is None
        assert state.case_bbox is None


def test_case_create_without_bbox_leaves_aoi_unset(
    _persistence_bound: Persistence,
) -> None:
    """#170 AOI-first: an absent ``args.bbox`` keeps the prior behaviour -
    no AOI persisted, no in-session anchor seeded."""
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    cmd = CaseCommandEnvelopePayload(
        command="create", args={"title": "plain case"}
    )
    asyncio.run(_handle_case_command(ws, state, cmd))

    case = asyncio.run(_persistence_bound.get_case(state.active_case_id))
    assert case is not None
    assert case.bbox is None
    assert state.case_bbox is None


def test_case_create_without_bbox_resets_stale_aoi_anchor(
    _persistence_bound: Persistence,
) -> None:
    """Stale-AOI regression: a fresh Case must NOT inherit the PREVIOUS Case's
    in-session AOI anchor.

    Simulates the real incident — a Case was opened/solved with a bbox set
    (``state.case_bbox`` non-None, e.g. the Chattanooga flood extent), then the
    user creates a NEW Case with no bbox. The create handler must reset
    ``state.case_bbox`` to None so the first turn re-geocodes from the place
    name (Twin Falls, Idaho) instead of reusing Chattanooga's extent.

    NOTE: unlike ``test_case_create_without_bbox_leaves_aoi_unset`` (which
    starts from a fresh state where ``case_bbox`` is already None and so passes
    even without the fix), this test pre-seeds a stale anchor, so it FAILS
    without the reset line in the create handler.
    """
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    # Prior Case left a stale AOI anchor (Chattanooga flood extent).
    state.case_bbox = [-85.32, 35.03, -85.28, 35.07]
    cmd = CaseCommandEnvelopePayload(
        command="create", args={"title": "Twin Falls groundwater"}
    )
    asyncio.run(_handle_case_command(ws, state, cmd))

    # Fresh Case created and the stale anchor is gone.
    assert state.active_case_id is not None
    assert state.case_bbox is None
    case = asyncio.run(_persistence_bound.get_case(state.active_case_id))
    assert case is not None
    assert case.bbox is None


def test_case_create_with_bbox_overrides_stale_aoi_anchor(
    _persistence_bound: Persistence,
) -> None:
    """The #170 AOI-first path still works after the stale-anchor reset: a
    create carrying a valid ``args.bbox`` seeds the NEW extent even when a
    prior (different) anchor was cached — the reset clears the old one, the
    conditional seed installs the new one."""
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    # Stale prior anchor (Chattanooga) that must be replaced, not merged.
    state.case_bbox = [-85.32, 35.03, -85.28, 35.07]
    new_bbox = [-114.5, 42.5, -114.4, 42.6]  # Twin Falls, Idaho
    cmd = CaseCommandEnvelopePayload(
        command="create",
        args={"title": "AOI-first Idaho case", "bbox": new_bbox},
    )
    asyncio.run(_handle_case_command(ws, state, cmd))

    assert state.case_bbox == new_bbox
    case = asyncio.run(_persistence_bound.get_case(state.active_case_id))
    assert case is not None
    assert list(case.bbox) == new_bbox


def test_case_select_emits_case_open_with_chat_history(
    _persistence_bound: Persistence,
) -> None:
    """``case-command(select)`` rehydrates chat history via ``get_session_state``."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))
    chat = CaseChatMessage(
        message_id=new_ulid(),
        case_id=case.case_id,
        role="user",
        content="model the flooding",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    asyncio.run(_persistence_bound.append_chat_message(chat))

    ws = MockWebSocket()
    state = _fresh_state()
    cmd = CaseCommandEnvelopePayload(command="select", case_id=case.case_id)
    asyncio.run(_handle_case_command(ws, state, cmd))

    case_open = next(env for env in ws.sent if env["type"] == "case-open")
    payload = case_open["payload"]
    assert payload["session_state"] is not None
    assert payload["session_state"]["case"]["case_id"] == case.case_id
    history = payload["session_state"]["chat_history"]
    assert len(history) == 1
    assert history[0]["content"] == "model the flooding"
    assert state.active_case_id == case.case_id


def test_case_rename_updates_title_and_refreshes_case_list(
    _persistence_bound: Persistence,
) -> None:
    """``case-command(rename)`` updates ``title`` and re-emits case-list."""
    # job-0252 (OQ-0115): seed the Case owned by the user the state lists as,
    # so it survives the now owner-scoped _emit_case_list (the $exists:false
    # leak clause is gone). Rename preserves the owner (the $set body has no
    # user_id key, so an already-stamped owner is never cleared).
    owner = new_ulid()
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case, owner_user_id=owner))
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = owner
    cmd = CaseCommandEnvelopePayload(
        command="rename",
        case_id=case.case_id,
        args={"title": "Renamed case"},
    )
    asyncio.run(_handle_case_command(ws, state, cmd))

    # Persistence reflects the rename
    fetched = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert fetched is not None
    assert fetched.title == "Renamed case"
    # case-list emitted (with the renamed case)
    case_list = next(env for env in ws.sent if env["type"] == "case-list")
    titles = [c["title"] for c in case_list["payload"]["cases"]]
    assert "Renamed case" in titles


def test_case_archive_soft_archives_and_refreshes_case_list(
    _persistence_bound: Persistence,
) -> None:
    """``case-command(archive)`` flips status to ``archived`` and emits case-list."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))
    ws = MockWebSocket()
    state = _fresh_state()
    cmd = CaseCommandEnvelopePayload(command="archive", case_id=case.case_id)
    asyncio.run(_handle_case_command(ws, state, cmd))

    fetched = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert fetched is not None
    assert fetched.status == "archived"
    assert any(env["type"] == "case-list" for env in ws.sent)


def test_case_delete_soft_deletes_and_clears_active_case(
    _persistence_bound: Persistence,
) -> None:
    """``case-command(delete)`` flips status; clears active_case_id when matching."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))
    ws = MockWebSocket()
    state = _fresh_state()
    state.active_case_id = case.case_id  # we're "in" this Case
    cmd = CaseCommandEnvelopePayload(command="delete", case_id=case.case_id)
    asyncio.run(_handle_case_command(ws, state, cmd))

    fetched = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert fetched is not None
    assert fetched.status == "deleted"
    # Active case context cleared because we deleted the active one
    assert state.active_case_id is None


def test_case_command_without_persistence_emits_error() -> None:
    """No Persistence bound -> INTERNAL_ERROR envelope (FR-MP-6 needs Mongo)."""
    saved = get_persistence()
    set_persistence(None)
    try:
        ws = MockWebSocket()
        state = _fresh_state()
        cmd = CaseCommandEnvelopePayload(command="create")
        asyncio.run(_handle_case_command(ws, state, cmd))
        types = [env["type"] for env in ws.sent]
        assert "error" in types
        err = next(env for env in ws.sent if env["type"] == "error")
        assert err["payload"]["error_code"] == "INTERNAL_ERROR"
    finally:
        set_persistence(saved)


def test_case_command_rename_missing_case_id_emits_error(
    _persistence_bound: Persistence,
) -> None:
    """Rename without case_id -> INTERNAL_ERROR (required-field guard)."""
    ws = MockWebSocket()
    state = _fresh_state()
    cmd = CaseCommandEnvelopePayload(
        command="rename", args={"title": "x"}
    )  # no case_id
    asyncio.run(_handle_case_command(ws, state, cmd))
    types = [env["type"] for env in ws.sent]
    assert "error" in types


def test_case_command_rename_missing_title_emits_error(
    _persistence_bound: Persistence,
) -> None:
    """Rename without args.title -> INTERNAL_ERROR (non-empty required)."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))
    ws = MockWebSocket()
    state = _fresh_state()
    cmd = CaseCommandEnvelopePayload(
        command="rename", case_id=case.case_id, args={}
    )
    asyncio.run(_handle_case_command(ws, state, cmd))
    types = [env["type"] for env in ws.sent]
    assert "error" in types


def test_emit_case_list_skips_when_persistence_unbound() -> None:
    """``_emit_case_list`` is a silent no-op when Persistence is unbound."""
    saved = get_persistence()
    set_persistence(None)
    try:
        ws = MockWebSocket()
        state = _fresh_state()
        asyncio.run(_emit_case_list(ws, state))
        # Nothing emitted
        assert ws.sent == []
    finally:
        set_persistence(saved)


def test_emit_case_list_skips_repeat_when_unchanged(
    _persistence_bound: Persistence,
) -> None:
    """OPEN-8 change-guard: calling ``_emit_case_list`` twice with an
    UNCHANGED case list (default ``force=False``) sends the envelope only
    once - the second call is a no-op (no re-serialize, no re-send)."""
    case = _fresh_case_summary()
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(case, owner_user_id=state.authenticated_user_id))

    asyncio.run(_emit_case_list(ws, state))
    asyncio.run(_emit_case_list(ws, state))

    case_list_envelopes = [env for env in ws.sent if env["type"] == "case-list"]
    assert len(case_list_envelopes) == 1


def test_emit_case_list_force_always_emits(
    _persistence_bound: Persistence,
) -> None:
    """``force=True`` bypasses the change-guard - every call sends, even when
    the underlying case list is byte-for-byte identical (the genuine-first-
    resume / explicit-mutation posture)."""
    case = _fresh_case_summary()
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(case, owner_user_id=state.authenticated_user_id))

    asyncio.run(_emit_case_list(ws, state, force=True))
    asyncio.run(_emit_case_list(ws, state, force=True))

    case_list_envelopes = [env for env in ws.sent if env["type"] == "case-list"]
    assert len(case_list_envelopes) == 2


def test_emit_case_list_reemits_after_mutation(
    _persistence_bound: Persistence,
) -> None:
    """A real mutation (a second Case created for the same owner) changes the
    content digest, so the NEXT default (``force=False``) call emits again -
    the guard tracks content, not merely call count."""
    case = _fresh_case_summary()
    ws = MockWebSocket()
    state = _fresh_state()
    state.authenticated_user_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(case, owner_user_id=state.authenticated_user_id))

    asyncio.run(_emit_case_list(ws, state))
    # Unchanged -> skipped.
    asyncio.run(_emit_case_list(ws, state))
    case_list_envelopes = [env for env in ws.sent if env["type"] == "case-list"]
    assert len(case_list_envelopes) == 1

    # A second Case lands for the same owner -> the list content changed.
    other = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(other, owner_user_id=state.authenticated_user_id))
    asyncio.run(_emit_case_list(ws, state))

    case_list_envelopes = [env for env in ws.sent if env["type"] == "case-list"]
    assert len(case_list_envelopes) == 2
    case_ids = {c["case_id"] for c in case_list_envelopes[-1]["payload"]["cases"]}
    assert {case.case_id, other.case_id} <= case_ids


def test_emit_case_list_change_guard_is_per_session(
    _persistence_bound: Persistence,
) -> None:
    """Two distinct sessions each get their OWN change-guard slot - the guard
    is keyed by ``session_id``, so session A's cached digest never suppresses
    session B's first emit."""
    case = _fresh_case_summary()
    owner = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(case, owner_user_id=owner))

    ws_a = MockWebSocket()
    state_a = _fresh_state()
    state_a.authenticated_user_id = owner
    asyncio.run(_emit_case_list(ws_a, state_a))

    ws_b = MockWebSocket()
    state_b = _fresh_state()
    state_b.authenticated_user_id = owner
    asyncio.run(_emit_case_list(ws_b, state_b))

    assert len([env for env in ws_a.sent if env["type"] == "case-list"]) == 1
    assert len([env for env in ws_b.sent if env["type"] == "case-list"]) == 1


def test_active_case_id_set_after_create_and_select(
    _persistence_bound: Persistence,
) -> None:
    """active_case_id follows ``create`` and ``select`` commands."""
    ws = MockWebSocket()
    state = _fresh_state()
    asyncio.run(_handle_case_command(ws, state, CaseCommandEnvelopePayload(command="create")))
    first_active = state.active_case_id
    assert first_active is not None

    # Create a second case, then select the first one again
    asyncio.run(_handle_case_command(ws, state, CaseCommandEnvelopePayload(command="create")))
    second_active = state.active_case_id
    assert second_active is not None
    assert second_active != first_active

    asyncio.run(
        _handle_case_command(
            ws, state, CaseCommandEnvelopePayload(command="select", case_id=first_active)
        )
    )
    assert state.active_case_id == first_active


# --------------------------------------------------------------------------- #
# Chat persistence
# --------------------------------------------------------------------------- #


def test_persist_chat_turn_writes_when_active_case_set(
    _persistence_bound: Persistence,
) -> None:
    """``_persist_chat_turn`` appends a CaseChatMessage when a Case is active."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))
    state = _fresh_state()
    state.active_case_id = case.case_id
    state.current_turn_layer_ids = ["flood-depth-A", "nlcd-AOI"]
    asyncio.run(
        _persist_chat_turn(
            state, role="user", content="model the flooding"
        )
    )
    # One CaseChatMessage landed in the chat collection
    session_state = asyncio.run(_persistence_bound.get_session_state(case.case_id))
    assert len(session_state.chat_history) == 1
    msg = session_state.chat_history[0]
    assert msg.content == "model the flooding"
    assert msg.role == "user"
    assert msg.layer_emissions == ["flood-depth-A", "nlcd-AOI"]


def test_persist_chat_turn_noop_when_no_active_case(
    _persistence_bound: Persistence,
) -> None:
    """``_persist_chat_turn`` is a silent no-op without an active Case context."""
    state = _fresh_state()
    state.active_case_id = None
    asyncio.run(
        _persist_chat_turn(state, role="user", content="hello (no case)")
    )
    # No writes to the chat collection
    mcp_mock = _persistence_bound._mcp  # type: ignore[attr-defined]
    chat_inserts = [
        (n, a) for n, a in mcp_mock.calls
        if n == "insert-one" and a.get("collection") == CHAT_COLLECTION
    ]
    assert chat_inserts == []


# --------------------------------------------------------------------------- #
# Integration: full Case flow
# --------------------------------------------------------------------------- #


def test_integration_e2e_case_flow(_persistence_bound: Persistence) -> None:
    """End-to-end: create Case A → publish-like chat persist → select Case B → isolation.

    Verifies:
    1. Creating Case A sets active context.
    2. Chat turn persists into Case A only.
    3. Creating Case B switches active context.
    4. Case B's rehydration shows ZERO chat history (Case A's chat is isolated).
    5. Selecting Case A again rehydrates the original chat.
    """
    ws = MockWebSocket()
    state = _fresh_state()

    # 1. Create Case A.
    asyncio.run(
        _handle_case_command(
            ws, state, CaseCommandEnvelopePayload(
                command="create", args={"title": "Case A — Fort Myers flood"}
            )
        )
    )
    case_a_id = state.active_case_id
    assert case_a_id is not None

    # 2. Persist a couple of chat turns into Case A.
    state.current_turn_layer_ids = ["flood-depth-A"]
    asyncio.run(_persist_chat_turn(state, role="user", content="model the flooding"))
    state.current_turn_layer_ids = []  # reset
    asyncio.run(
        _persist_chat_turn(
            state,
            role="agent",
            content="[invoked publish_layer]",
            pipeline_id=new_ulid(),
        )
    )

    # 3. Create Case B (switches active context).
    asyncio.run(
        _handle_case_command(
            ws, state, CaseCommandEnvelopePayload(
                command="create", args={"title": "Case B — wildfire smoke"}
            )
        )
    )
    case_b_id = state.active_case_id
    assert case_b_id is not None
    assert case_b_id != case_a_id

    # 4. Case B's rehydration shows ZERO chat history.
    state_b = asyncio.run(_persistence_bound.get_session_state(case_b_id))
    assert state_b.chat_history == []

    # 5. Selecting Case A again rehydrates the original chat.
    ws_a = MockWebSocket()
    asyncio.run(
        _handle_case_command(
            ws_a, state, CaseCommandEnvelopePayload(command="select", case_id=case_a_id)
        )
    )
    assert state.active_case_id == case_a_id
    case_open = next(env for env in ws_a.sent if env["type"] == "case-open")
    history = case_open["payload"]["session_state"]["chat_history"]
    assert len(history) == 2
    contents = sorted(m["content"] for m in history)
    assert contents == sorted(["model the flooding", "[invoked publish_layer]"])


# --------------------------------------------------------------------------- #
# #165 data-island: thin manifest dual-write at Case mutations
# --------------------------------------------------------------------------- #


def test_create_dual_writes_manifest_alongside_snapshot() -> None:
    """A Case create writes BOTH a snapshot and a thin manifest to S3.

    DUAL-WRITE invariant: the manifest lands at the SAME mutation call-site as
    the snapshot, at ``case-manifests/{case_id}.json``, validating as a
    ``CaseManifest``. The snapshot path is unchanged.
    """
    from trid3nt_server.persistence import case_manifest_key, case_view_snapshot_key
    from trid3nt_contracts.case import CaseManifest

    saved = get_persistence()
    mock = MockMCPClient()
    p = Persistence(mock)
    snapshot_puts: list = []
    manifest_puts: list = []

    async def _snap_put(self, key, body, metadata=None):
        snapshot_puts.append((key, body, metadata))

    async def _manifest_put(self, key, body, metadata=None):
        manifest_puts.append((key, body, metadata))

    # Patch the production S3 puts so no real boto3 call is made; capture both.
    orig_snap = Persistence._default_s3_put_case_view
    orig_man = Persistence._default_s3_put_case_manifest
    Persistence._default_s3_put_case_view = _snap_put  # type: ignore[assignment]
    Persistence._default_s3_put_case_manifest = _manifest_put  # type: ignore[assignment]
    set_persistence(p)
    try:
        ws = MockWebSocket()
        state = _fresh_state()
        state.authenticated_user_id = new_ulid()
        asyncio.run(
            _handle_case_command(
                ws, state, CaseCommandEnvelopePayload(
                    command="create", args={"title": "Dual-write case"}
                )
            )
        )
        case_id = state.active_case_id
        assert case_id is not None

        # BOTH writers fired at the create mutation.
        assert len(snapshot_puts) == 1
        assert len(manifest_puts) == 1

        snap_key, _snap_body, _snap_meta = snapshot_puts[0]
        man_key, man_body, _man_meta = manifest_puts[0]
        assert snap_key == case_view_snapshot_key(case_id)
        assert man_key == case_manifest_key(case_id)

        # The manifest body validates as a CaseManifest carrying the title.
        manifest = CaseManifest.model_validate(json.loads(man_body.decode("utf-8")))
        assert manifest.case_id == case_id
        assert manifest.title == "Dual-write case"
    finally:
        Persistence._default_s3_put_case_view = orig_snap  # type: ignore[assignment]
        Persistence._default_s3_put_case_manifest = orig_man  # type: ignore[assignment]
        set_persistence(saved)


def test_persist_case_manifest_noop_without_active_case() -> None:
    """``_persist_case_manifest`` is a no-op with no Case bound (never raises)."""
    from trid3nt_server.server import _persist_case_manifest

    saved = get_persistence()
    mock = MockMCPClient()
    set_persistence(Persistence(mock))
    try:
        state = _fresh_state()
        state.active_case_id = None
        # No case_id arg, no active case -> short-circuits, returns None.
        assert asyncio.run(_persist_case_manifest(state)) is None
    finally:
        set_persistence(saved)


def test_persist_case_manifest_noop_without_persistence() -> None:
    """``_persist_case_manifest`` is a no-op with no Persistence bound."""
    from trid3nt_server.server import _persist_case_manifest

    saved = get_persistence()
    set_persistence(None)
    try:
        state = _fresh_state()
        assert asyncio.run(_persist_case_manifest(state, case_id=new_ulid())) is None
    finally:
        set_persistence(saved)
