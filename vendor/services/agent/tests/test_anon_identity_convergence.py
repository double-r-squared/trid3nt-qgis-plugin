"""cases-vanish fix: dual-socket anon-identity convergence (task #163).

ROOT CAUSE: the web mounts TWO WebSocket connections per tab (App.tsx +
Chat.tsx, one localStorage session_id). Each connection ran its OWN auth
handshake; with no token + no hint, ``_provision_anonymous_user`` minted a
FRESH random ULID PER CONNECTION. The two sockets then forked the owner-scoped
``list_cases_for_user`` view, so Cases appeared to vanish on refresh.

The fix is two layers:

1. SERVER honors a client-presented ``anonymous_user_id`` VERBATIM — reused
   when a record exists, PROVISIONED with that exact id when it does not (so the
   web's always-replayed client-owned id deterministically resolves to ONE
   user across both sockets). Covered here + in ``test_sticky_anonymous_user``.

2. A belt-and-suspenders SESSION-SCOPED anon-id registry on the server
   (``server._SESSION_ANON_ID``, mirroring ``_SESSION_ACTIVE_CASE``): when a
   connection binds an anon user for a ``session_id``, it is recorded; a second
   connection of the SAME ``session_id`` with NO usable hint reuses it instead
   of minting fresh. This collapses the (now rare) no-hint first-connect window.

These tests pin both layers + the case-list stability they guarantee, and prove
the authed (Cognito) path is unaffected.
"""

from __future__ import annotations

from typing import Any

import pytest

from grace2_agent import server
from grace2_agent.auth_handshake import authenticate_token, set_verify_hook
from grace2_agent.persistence import Persistence
from grace2_contracts.auth import AuthTokenEnvelope
from grace2_contracts.case import CaseSummary
from grace2_contracts.common import new_ulid, now_utc
from grace2_contracts.user import User


class FakeMCPClient:
    """In-memory MCP client round-tripping users + projects (cases) for tests.

    Supports the exact tool shapes the Persistence layer issues:
    - users: find-one by ``_id``, update-one (upsert) by ``_id``.
    - projects: find with the ``$or: [{user_id}, {owner_user_id}]`` +
      ``status $nin`` filter ``list_cases_for_user`` uses, and update-one
      (upsert) stamping ``user_id`` from ``$set`` for ownership.
    """

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        coll = args.get("collection")
        store = (
            self.users
            if coll == "users"
            else self.projects
            if coll == "projects"
            else None
        )
        if store is None:
            return {"document": None}

        if name == "find-one":
            filt = args.get("filter", {})
            key = filt.get("_id")
            if key and key in store:
                return {"document": store[key]}
            return {"document": None}

        if name == "find":
            filt = args.get("filter", {})
            owners = set()
            for clause in filt.get("$or", []):
                for v in clause.values():
                    owners.add(v)
            status_block = set()
            status_filt = filt.get("status", {})
            if isinstance(status_filt, dict):
                status_block = set(status_filt.get("$nin", []))
            out = []
            for doc in store.values():
                doc_owner = doc.get("user_id") or doc.get("owner_user_id")
                if owners and doc_owner not in owners:
                    continue
                if doc.get("status") in status_block:
                    continue
                out.append(doc)
            return {"documents": out}

        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {}).get("$set", {})
            key = filt.get("_id")
            if key is None:
                return {"matchedCount": 0, "modifiedCount": 0}
            if key in store:
                store[key].update(update)
            elif args.get("upsert"):
                store[key] = dict(update)
            return {"matchedCount": 1, "modifiedCount": 1}
        return {}


# --------------------------------------------------------------------------- #
# Layer 1 — server honors the presented anon id (reuse + verbatim provision).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_presented_id_reused_verbatim_when_record_exists() -> None:
    """A presented id whose record exists re-binds it — no new ULID minted."""
    client = FakeMCPClient()
    p = Persistence(client)

    # First connect provisions the client-owned id verbatim.
    cid = new_ulid()
    first = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert first.user.user_id == cid

    # Second connect (sibling socket) presents the SAME id — reused verbatim.
    second = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert second.user.user_id == cid
    assert second.is_anonymous is True
    # Exactly one user record exists for that id.
    assert list(client.users.keys()) == [cid]


@pytest.mark.asyncio
async def test_unknown_presented_id_is_provisioned_with_that_id() -> None:
    """A presented id with NO record provisions a user with THAT id."""
    client = FakeMCPClient()
    p = Persistence(client)

    cid = new_ulid()
    res = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert res.user.user_id == cid  # verbatim, not a fresh ULID
    assert res.is_anonymous is True
    assert cid in client.users
    assert client.users[cid]["is_anonymous"] is True


# --------------------------------------------------------------------------- #
# Layer 2 — session-scoped registry collapses the no-hint race.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_registry():
    server._SESSION_ANON_ID.clear()
    yield
    server._SESSION_ANON_ID.clear()


def test_session_registry_set_get_roundtrip() -> None:
    sid = "session-abc"
    assert server._get_session_anon_id(sid) is None
    anon = new_ulid()
    server._set_session_anon_id(sid, anon)
    assert server._get_session_anon_id(sid) == anon


def test_session_registry_ignores_empty() -> None:
    server._set_session_anon_id("", new_ulid())
    server._set_session_anon_id("sid", "")
    assert server._SESSION_ANON_ID == {}


def test_session_registry_is_bounded() -> None:
    cap = server._SESSION_ANON_ID_CAP
    for i in range(cap + 10):
        server._set_session_anon_id(f"s-{i}", new_ulid())
    assert len(server._SESSION_ANON_ID) <= cap


def test_apply_session_anon_hint_fills_missing_hint() -> None:
    """No-hint envelope on a session with a recorded id gets the id injected."""
    sid = "session-xyz"
    anon = new_ulid()
    server._set_session_anon_id(sid, anon)
    out = server._apply_session_anon_hint(sid, AuthTokenEnvelope(token=""))
    assert out is not None
    assert out.anonymous_user_id == anon


def test_apply_session_anon_hint_fills_none_envelope() -> None:
    sid = "session-none"
    anon = new_ulid()
    server._set_session_anon_id(sid, anon)
    out = server._apply_session_anon_hint(sid, None)
    assert out is not None
    assert out.token == ""
    assert out.anonymous_user_id == anon


def test_apply_session_anon_hint_never_clobbers_client_hint() -> None:
    """A client-supplied hint (durable cross-refresh id) always wins."""
    sid = "session-clobber"
    registry_id = new_ulid()
    client_id = new_ulid()
    server._set_session_anon_id(sid, registry_id)
    out = server._apply_session_anon_hint(
        sid, AuthTokenEnvelope(token="", anonymous_user_id=client_id)
    )
    assert out.anonymous_user_id == client_id  # not registry_id


def test_apply_session_anon_hint_leaves_token_path_untouched() -> None:
    """A non-empty token (verify path) is never diverted to an anon id."""
    sid = "session-token"
    server._set_session_anon_id(sid, new_ulid())
    tok = AuthTokenEnvelope(token="a.real.jwt")
    out = server._apply_session_anon_hint(sid, tok)
    assert out is tok  # unchanged object — authed path owns this connect
    assert out.anonymous_user_id is None


def test_apply_session_anon_hint_noop_without_registry_entry() -> None:
    tok = AuthTokenEnvelope(token="")
    out = server._apply_session_anon_hint("unknown-session", tok)
    assert out is tok


@pytest.mark.asyncio
async def test_two_no_hint_connections_converge_via_registry() -> None:
    """Two no-hint connections of one session converge on ONE anon id.

    Simulates the dual-socket race in the (rare) window where neither App nor
    Chat has a client hint yet. Connection A binds + records; connection B sees
    no hint, the registry fills it, and B resolves to the SAME id.
    """
    client = FakeMCPClient()
    p = Persistence(client)
    sid = "session-race"

    # Connection A: no hint -> mint fresh -> record in registry.
    tok_a = server._apply_session_anon_hint(sid, AuthTokenEnvelope(token=""))
    res_a = await authenticate_token(tok_a, p)
    assert res_a.is_anonymous
    server._set_session_anon_id(sid, res_a.user.user_id)

    # Connection B: no hint -> registry fills A's id -> reuse SAME user.
    tok_b = server._apply_session_anon_hint(sid, AuthTokenEnvelope(token=""))
    res_b = await authenticate_token(tok_b, p)
    assert res_b.user.user_id == res_a.user.user_id
    # Exactly one anon user across both connections.
    assert list(client.users.keys()) == [res_a.user.user_id]


# --------------------------------------------------------------------------- #
# Case-list stability across a reconnect for the same client id.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_case_list_stable_across_reconnect_for_same_client_id() -> None:
    """A Case created under the converged anon id stays visible on reconnect."""
    client = FakeMCPClient()
    p = Persistence(client)
    cid = new_ulid()

    # First connect provisions the client-owned id; a Case is created + owned.
    first = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert first.user.user_id == cid
    case = CaseSummary(
        case_id=new_ulid(),
        title="Refresh Test Case",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    await p.upsert_case(case, owner_user_id=first.user.user_id, ephemeral=True)

    cases_before = await p.list_cases_for_user(first.user.user_id)
    assert [c.case_id for c in cases_before] == [case.case_id]

    # Reconnect (refresh): same client id replayed -> same owner -> same list.
    second = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert second.user.user_id == cid
    cases_after = await p.list_cases_for_user(second.user.user_id)
    assert [c.case_id for c in cases_after] == [case.case_id]


@pytest.mark.asyncio
async def test_case_list_would_fork_without_verbatim_provision() -> None:
    """Control: two DIFFERENT anon ids do NOT see each other's Cases.

    Pins that owner-scoping is intact (not weakened) — the fix works by
    converging the IDENTITY, not by broadening the case-list query.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    a = await authenticate_token(AuthTokenEnvelope(token=""), p)  # fresh ULID
    case = CaseSummary(
        case_id=new_ulid(),
        title="A's Case",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    await p.upsert_case(case, owner_user_id=a.user.user_id)

    b = await authenticate_token(AuthTokenEnvelope(token=""), p)  # different ULID
    assert b.user.user_id != a.user.user_id
    assert await p.list_cases_for_user(b.user.user_id) == []
    assert [c.case_id for c in await p.list_cases_for_user(a.user.user_id)] == [
        case.case_id
    ]


# --------------------------------------------------------------------------- #
# Authed (Cognito) path unaffected.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authed_path_ignores_anon_registry_and_hint() -> None:
    """A verified token resolves by firebase_uid; the anon hint is ignored."""
    client = FakeMCPClient()
    p = Persistence(client)
    sid = "session-authed"
    # Seed a stale registry entry — it must NOT down-bind the authed connect.
    server._set_session_anon_id(sid, new_ulid())

    set_verify_hook(
        lambda _t: {"uid": "cognito-sub-001", "email": "u@example.com", "tier": "free"}
    )
    try:
        # A non-empty token + a stray anon hint: the verify path must win.
        tok = AuthTokenEnvelope(token="real.jwt", anonymous_user_id=new_ulid())
        # The server fills hints only for the anon path; assert it leaves the
        # token envelope untouched.
        filled = server._apply_session_anon_hint(sid, tok)
        assert filled is tok
        res = await authenticate_token(filled, p)
        assert res.is_anonymous is False
        assert res.firebase_uid == "cognito-sub-001"
        assert res.user.firebase_uid == "cognito-sub-001"
        # The user was provisioned by firebase_uid, NOT the anon hint.
        stored = list(client.users.values())
        assert len(stored) == 1
        assert stored[0]["firebase_uid"] == "cognito-sub-001"
    finally:
        set_verify_hook(None)
