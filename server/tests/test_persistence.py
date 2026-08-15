"""Unit + integration tests for ``trid3nt_server.persistence`` (job-0115).

The ``Persistence`` wrapper translates between agent-side typed contracts
(``CaseSummary`` / ``CaseChatMessage`` / ``User``) and the MongoDB Atlas MCP
server's CRUD tools (``insert-one`` / ``update-one`` / ``find-one`` / ``find``).

Coverage:
- ``test_get_case_returns_none_on_missing`` — find-one with no match.
- ``test_upsert_case_then_get_round_trip`` — upsert -> get returns equal model.
- ``test_list_cases_for_user`` — find with user filter returns list.
- ``test_archive_case_sets_status`` — archive sets status="archived".
- ``test_delete_case_sets_status`` — delete sets status="deleted".
- ``test_append_chat_message_and_hydrate_session`` — chat append +
  ``get_session_state`` re-hydrates.
- ``test_user_round_trip`` — upsert + get_user_by_id.
- ``test_live_mcp_write_then_read_or_skip`` — live integration with the
  MongoDB MCP server when ``TRID3NT_MONGO_MCP_STDIO=1``; else surfaces the
  OQ-0115-MCP-NOT-PROVISIONED skip.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from trid3nt_server.persistence import (
    CASES_COLLECTION,
    CHAT_COLLECTION,
    USERS_COLLECTION,
    Persistence,
)
from trid3nt_contracts.case import CaseChatMessage, CaseSummary
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.user import User


# --------------------------------------------------------------------------- #
# Mock MCP client
# --------------------------------------------------------------------------- #


class MockMCPClient:
    """In-memory mock of the MongoDB MCP server.

    Implements just enough of the tool surface ``Persistence`` calls into:
    ``find-one`` / ``find`` / ``insert-one`` / ``update-one``. Records every
    call so tests can assert routing.
    """

    def __init__(self) -> None:
        # collection -> id -> document
        self._store: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):  # noqa: D401
        args = dict(arguments or {})
        self.calls.append((name, args))
        coll = args.get("collection") or "_default"
        store = self._store.setdefault(coll, {})

        if name == "insert-one":
            doc = args["document"]
            store[doc["_id"]] = doc
            return {"insertedId": doc["_id"]}

        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {})
            set_ = update.get("$set", {})
            upsert = args.get("upsert", False)
            target_id = filt.get("_id")
            if target_id and target_id in store:
                store[target_id].update(set_)
            elif upsert and target_id:
                store[target_id] = {**set_, "_id": target_id}
            elif filt:
                # Update by a non-_id filter (first match wins).
                for doc in store.values():
                    if all(doc.get(k) == v for k, v in filt.items()):
                        doc.update(set_)
                        break
            return {"matchedCount": 1, "modifiedCount": 1}

        if name == "find-one":
            filt = args.get("filter", {})
            for doc in store.values():
                if self._matches(doc, filt):
                    return {"document": doc}
            return {"document": None}

        if name == "find":
            filt = args.get("filter", {})
            sort = args.get("sort", {})
            results = [d for d in store.values() if self._matches(d, filt)]
            if sort:
                key = next(iter(sort.keys()))
                direction = sort[key]
                results.sort(
                    key=lambda d: d.get(key, ""),
                    reverse=(direction == -1),
                )
            return {"documents": results}

        raise NotImplementedError(f"mock MCP: unknown tool {name!r}")

    @staticmethod
    def _matches(doc: dict, filt: dict) -> bool:
        """Tiny query matcher: equality, ``$or``, ``$exists=False``, ``$nin``."""
        for k, v in filt.items():
            if k == "$or":
                if not any(MockMCPClient._matches(doc, sub) for sub in v):
                    return False
                continue
            if isinstance(v, dict) and "$exists" in v:
                present = k in doc
                if v["$exists"] is False and present:
                    return False
                if v["$exists"] is True and not present:
                    return False
                continue
            if isinstance(v, dict) and "$nin" in v:
                # job-0267: mirrors FileMCPClient — a missing field matches
                # (doc.get returns None, which is "not in" the exclusion
                # list unless None is listed).
                if doc.get(k) in v["$nin"]:
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_case_summary() -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title="Hurricane Ian — Fort Myers flood scenario",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=["nlcd-fort-myers", "flood-depth-01HX"],
    )


def _fresh_chat_message(case_id: str, *, role="user") -> CaseChatMessage:
    return CaseChatMessage(
        message_id=new_ulid(),
        case_id=case_id,
        role=role,
        content="model the flooding",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
    )


def _fresh_user_record() -> User:
    return User(
        user_id=new_ulid(),
        email="natealmanza3@gmail.com",
        display_name="Nate Almanza",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        is_active=True,
        prefs={"theme": "dark"},
    )


# --------------------------------------------------------------------------- #
# Case CRUD
# --------------------------------------------------------------------------- #


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

    job-0252 (OQ-0115-CASE-USER-LINK): the ``$exists:false`` backward-compat
    leak clause is GONE. Cases are now owner-scoped — a Case is visible only
    to the user stamped as its owner at creation
    (``upsert_case(owner_user_id=...)``).
    """
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


# --------------------------------------------------------------------------- #
# Chat history + session state
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Users (Auth/Users-track stub)
# --------------------------------------------------------------------------- #


def test_user_round_trip() -> None:
    """upsert_user then get_user_by_id returns equal model."""
    mock = MockMCPClient()
    p = Persistence(mock)
    user = _fresh_user_record()

    asyncio.run(p.upsert_user(user))
    fetched = asyncio.run(p.get_user_by_id(user.user_id))
    assert fetched is not None
    assert fetched.user_id == user.user_id
    assert fetched.email == "natealmanza3@gmail.com"
    assert fetched.display_name == "Nate Almanza"
    assert fetched.prefs == {"theme": "dark"}

    # Routed to the users collection
    upserts = [(n, a) for n, a in mock.calls if n == "update-one"
               and a.get("collection") == USERS_COLLECTION]
    assert upserts


def test_user_lookup_returns_none_when_missing() -> None:
    mock = MockMCPClient()
    p = Persistence(mock)
    assert asyncio.run(p.get_user_by_id("never-existed")) is None


def test_get_user_by_id_tolerates_stale_firebase_uid_key() -> None:
    """An OLD-SHAPE user row carrying the retired ``firebase_uid`` key loads
    without crashing: ``get_user_by_id`` filters to the known User fields
    before ``model_validate``, so the dropped IdP-sub carrier never reaches the
    ``extra="forbid"`` contract."""
    mock = MockMCPClient()
    uid = new_ulid()
    # Seed the store directly with a legacy document shape (pre-chop the field
    # was persisted on every user row).
    mock._store[USERS_COLLECTION] = {
        uid: {
            "_id": uid,
            "schema_version": "v1",
            "user_id": uid,
            "firebase_uid": "legacy-idp-sub-value",
            "email": None,
            "display_name": None,
            "created_at": "2026-06-08T12:00:00Z",
            "is_active": True,
            "prefs": {},
            "is_anonymous": True,
        }
    }
    p = Persistence(mock)
    fetched = asyncio.run(p.get_user_by_id(uid))
    assert fetched is not None
    assert fetched.user_id == uid
    assert not hasattr(fetched, "firebase_uid")


# --------------------------------------------------------------------------- #
# Live integration (env-guarded)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("TRID3NT_MONGO_MCP_STDIO") != "1",
    reason="OQ-0115-MCP-NOT-PROVISIONED: set TRID3NT_MONGO_MCP_STDIO=1 to run",
)
def test_live_mcp_write_then_read() -> None:  # pragma: no cover — env-guarded
    """Live MCP round-trip: upsert a test Case, fetch it back, delete it.

    Only runs when ``TRID3NT_MONGO_MCP_STDIO=1`` and the agent has been launched
    with credentials sufficient to call the SRV secret. Else surfaces as the
    OQ-0115-MCP-NOT-PROVISIONED skip.
    """
    from trid3nt_server.mcp import MCPClient, fetch_srv_from_secret_manager

    async def _run() -> None:
        srv = fetch_srv_from_secret_manager()
        client = await MCPClient.start(srv)
        try:
            p = Persistence(client)
            case = _fresh_case_summary()
            await p.upsert_case(case)
            fetched = await p.get_case(case.case_id)
            assert fetched is not None
            assert fetched.case_id == case.case_id
            await p.delete_case(case.case_id)
        finally:
            await client.close()

    asyncio.run(_run())
