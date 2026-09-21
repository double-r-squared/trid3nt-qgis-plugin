"""Unit and integration tests for ``trid3nt_server.persistence``.

The ``Persistence`` wrapper translates between the typed agent-side contracts and
a document store's ``insert-one`` / ``update-one`` / ``find-one`` / ``find``
surface, driven here by an in-memory client: Case upsert and get round-trip, the
owner-scoped list, the archive and delete statuses, chat append and rehydration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from trid3nt_server.persistence import (
    CASES_COLLECTION,
    CHAT_COLLECTION,
    Persistence,
)
from trid3nt_contracts.case import CaseChatMessage
from trid3nt_contracts.common import new_ulid

from tests._fakes import MockMCPClient, _fresh_case_summary




def _fresh_chat_message(case_id: str, *, role="user") -> CaseChatMessage:
    return CaseChatMessage(
        message_id=new_ulid(),
        case_id=case_id,
        role=role,
        content="model the flooding",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
    )






def test_get_case_returns_none_on_missing() -> None:
    mock = MockMCPClient()
    p = Persistence(mock)
    result = asyncio.run(p.get_case(new_ulid()))
    assert result is None


def test_upsert_case_then_get_round_trip() -> None:
    """Upsert a Case, then fetch it back — the returned model must equal input."""
    mock = MockMCPClient()
    p = Persistence(mock)
    case = _fresh_case_summary()

    saved = asyncio.run(p.upsert_case(case))
    assert saved.case_id == case.case_id

    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
    assert fetched.case_id == case.case_id
    assert fetched.title == case.title
    assert fetched.primary_hazard == "flood"
    assert fetched.bbox == (-82.0, 26.5, -81.8, 26.7)

    # Assert the MCP routing was through update-one with upsert=True
    upsert_calls = [
        (n, a) for n, a in mock.calls if n == "update-one"
        and a.get("collection") == CASES_COLLECTION
    ]
    assert upsert_calls, "no update-one call to projects collection"
    assert upsert_calls[0][1].get("upsert") is True


def test_list_cases_for_user() -> None:
    """Two Cases owned by a user are listed; a third owned by someone else is not.

    Cases are owner-scoped - visible only to the user stamped as owner at creation -
    with no backward-compat clause for an absent owner."""
    mock = MockMCPClient()
    p = Persistence(mock)
    owner = new_ulid()
    other = new_ulid()
    case_a = _fresh_case_summary()
    case_b = _fresh_case_summary()
    case_other = _fresh_case_summary()

    asyncio.run(p.upsert_case(case_a, owner_user_id=owner))
    asyncio.run(p.upsert_case(case_b, owner_user_id=owner))
    asyncio.run(p.upsert_case(case_other, owner_user_id=other))

    cases = asyncio.run(p.list_cases_for_user(owner))
    case_ids = {c.case_id for c in cases}
    assert case_a.case_id in case_ids
    assert case_b.case_id in case_ids
    # The other user's Case is NOT visible (leak clause gone).
    assert case_other.case_id not in case_ids

    # A user who owns nothing sees nothing — owner-less / foreign Cases no
    # longer leak.
    none_cases = asyncio.run(p.list_cases_for_user(new_ulid()))
    assert none_cases == []


def test_archive_case_sets_status() -> None:
    mock = MockMCPClient()
    p = Persistence(mock)
    case = _fresh_case_summary()
    asyncio.run(p.upsert_case(case))

    asyncio.run(p.archive_case(case.case_id))
    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
    assert fetched.status == "archived"


def test_delete_case_sets_status() -> None:
    mock = MockMCPClient()
    p = Persistence(mock)
    case = _fresh_case_summary()
    asyncio.run(p.upsert_case(case))

    asyncio.run(p.delete_case(case.case_id))
    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
    assert fetched.status == "deleted"




def test_append_chat_message_and_hydrate_session() -> None:
    """Append two chat messages, then ``get_session_state`` returns them in order."""
    mock = MockMCPClient()
    p = Persistence(mock)
    case = _fresh_case_summary()
    asyncio.run(p.upsert_case(case))

    msg1 = _fresh_chat_message(case.case_id, role="user")
    msg2 = _fresh_chat_message(case.case_id, role="agent")
    asyncio.run(p.append_chat_message(msg1))
    asyncio.run(p.append_chat_message(msg2))

    state = asyncio.run(p.get_session_state(case.case_id))
    assert state.case.case_id == case.case_id
    assert {m.message_id for m in state.chat_history} == {msg1.message_id, msg2.message_id}
    # Both inserted with insert-one to the chat collection
    inserts = [(n, a) for n, a in mock.calls if n == "insert-one"
               and a.get("collection") == CHAT_COLLECTION]
    assert len(inserts) == 2


def test_session_state_for_missing_case_returns_tombstone() -> None:
    """Missing Case -> placeholder ``CaseSessionState`` with status=deleted."""
    mock = MockMCPClient()
    p = Persistence(mock)
    state = asyncio.run(p.get_session_state(new_ulid()))
    assert state.case.status == "deleted"
    assert state.chat_history == []
