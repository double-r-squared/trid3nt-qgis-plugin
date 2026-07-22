"""Anon-identity convergence under the LOCAL single-user build.

HISTORY (task #163): the web mounts TWO WebSocket connections per tab
(App.tsx + Chat.tsx, one localStorage session_id); each ran its own auth
handshake and, pre-fix, minted a fresh ULID per connection, forking the
owner-scoped case list. The original fix was two layers: (1) honor the
client-presented ``anonymous_user_id`` verbatim, (2) a session-scoped anon-id
registry (``server._SESSION_ANON_ID``) to collapse the no-hint race.

CURRENT TRUTH (F1, TRID3NT local build): ``solver_backend()`` is hardwired to
``local-docker``, so ``authenticate_token`` resolves EVERY connection --
hinted, no-hint, or token-bearing -- to the ONE fixed local user
(``auth_handshake.LOCAL_SINGLE_USER_ID``). Convergence is therefore
unconditional by construction; identity forks are unrepresentable via the
handshake. The hints stay accepted on the wire (clients keep their sticky
logic), and the session registry remains as live server plumbing that fills
hints BEFORE resolution -- harmless, still tested below at its own seam.

These tests pin: resolution always lands on the local user, the registry
helpers' contracts, and the case-list stability that resolution guarantees.
"""

from __future__ import annotations

from typing import Any

import pytest

from grace2_agent import server
from grace2_agent.auth_handshake import LOCAL_SINGLE_USER_ID, authenticate_token
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
# Layer 1 -- resolution: every presented anon id lands on the local user.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_presented_id_accepted_but_resolution_lands_on_local_user() -> None:
    """Sibling sockets presenting the same hint converge on the local user.

    The hint rides the wire unchanged (no error), but both connections
    resolve to ``LOCAL_SINGLE_USER_ID`` and exactly ONE user record exists.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    cid = new_ulid()
    first = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert first.user.user_id == LOCAL_SINGLE_USER_ID

    # Second connect (sibling socket) presents the SAME id -- same resolution.
    second = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert second.user.user_id == LOCAL_SINGLE_USER_ID
    assert second.is_anonymous is True
    # Exactly one user record exists: the fixed local user, never the hint.
    assert list(client.users.keys()) == [LOCAL_SINGLE_USER_ID]


@pytest.mark.asyncio
async def test_unknown_presented_id_is_not_provisioned_verbatim() -> None:
    """A presented id with NO record does NOT provision that id (F1).

    Pre-F1 the cases-vanish fix claimed the id verbatim; the local build
    instead collapses onto the fixed local user, so no per-client user
    document is ever created.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    cid = new_ulid()
    res = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=cid), p
    )
    assert res.user.user_id == LOCAL_SINGLE_USER_ID
    assert res.is_anonymous is True
    assert cid not in client.users
    assert client.users[LOCAL_SINGLE_USER_ID]["is_anonymous"] is True


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
    """A non-empty token is never diverted to an anon id."""
    sid = "session-token"
    server._set_session_anon_id(sid, new_ulid())
    tok = AuthTokenEnvelope(token="a.real.jwt")
    out = server._apply_session_anon_hint(sid, tok)
    assert out is tok  # unchanged object — the token path owns this connect
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
async def test_case_list_stable_across_reconnect_any_client_id() -> None:
    """A Case stays visible on reconnect EVEN with a different client id.

    F1 strengthens the old guarantee: stability no longer depends on the
    client replaying the same sticky id -- every connection resolves to the
    one local user, so the owner-scoped list is identical regardless of hint.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    # First connect (some client hint); a Case is created + owned.
    first = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=new_ulid()), p
    )
    assert first.user.user_id == LOCAL_SINGLE_USER_ID
    case = CaseSummary(
        case_id=new_ulid(),
        title="Refresh Test Case",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    await p.upsert_case(case, owner_user_id=first.user.user_id, ephemeral=True)

    cases_before = await p.list_cases_for_user(first.user.user_id)
    assert [c.case_id for c in cases_before] == [case.case_id]

    # Reconnect (refresh / other device): a DIFFERENT hint -> same owner ->
    # same list.
    second = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=new_ulid()), p
    )
    assert second.user.user_id == LOCAL_SINGLE_USER_ID
    cases_after = await p.list_cases_for_user(second.user.user_id)
    assert [c.case_id for c in cases_after] == [case.case_id]


@pytest.mark.asyncio
async def test_no_hint_connections_cannot_fork_case_lists() -> None:
    """Two no-hint connections resolve to ONE user; forking is impossible.

    Replaces the pre-F1 control ("two DIFFERENT anon ids do not see each
    other's Cases"): the handshake can no longer produce two distinct
    identities at all, so the dual-socket fork that motivated task #163 is
    structurally unrepresentable. A Case created via connection A is listed
    for connection B because they ARE the same fixed local user.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    a = await authenticate_token(AuthTokenEnvelope(token=""), p)
    b = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert a.user.user_id == b.user.user_id == LOCAL_SINGLE_USER_ID
    assert list(client.users.keys()) == [LOCAL_SINGLE_USER_ID]

    case = CaseSummary(
        case_id=new_ulid(),
        title="A's Case",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    await p.upsert_case(case, owner_user_id=a.user.user_id)
    assert [c.case_id for c in await p.list_cases_for_user(b.user.user_id)] == [
        case.case_id
    ]


# --------------------------------------------------------------------------- #
# Token path: a presented token bypasses the hint fill and mints fresh.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_path_ignores_anon_registry_and_hint() -> None:
    """A non-empty token is ignored; resolution lands on the local user.

    The local build has no token verifier: the registry never fills a hint
    onto a token-bearing envelope (asserted at the server seam), and
    ``authenticate_token`` resolves the connection to the fixed local user --
    never the stray client hint, never the session-registry id.
    """
    client = FakeMCPClient()
    p = Persistence(client)
    sid = "session-token-path"
    # Seed a stale registry entry -- it must NOT be filled onto a token connect.
    server._set_session_anon_id(sid, new_ulid())

    stray_hint = new_ulid()
    tok = AuthTokenEnvelope(token="real.jwt", anonymous_user_id=stray_hint)
    # The server fills hints only for the token-less path; assert it leaves
    # the token envelope untouched.
    filled = server._apply_session_anon_hint(sid, tok)
    assert filled is tok
    res = await authenticate_token(filled, p)
    assert res.is_anonymous is True
    assert res.firebase_uid is None
    # The fixed local user -- neither the stray hint nor the registry id.
    assert res.user.user_id == LOCAL_SINGLE_USER_ID
    assert res.user.user_id != stray_hint
    assert res.user.user_id != server._get_session_anon_id(sid)


# --------------------------------------------------------------------------- #
# F1 (live-feedback 2026-07-09): TRID3NT local build -- ONE fixed local user.
#
# Local mode (GRACE2_SOLVER_BACKEND=local-docker, the FilePersistence build)
# has exactly one human, but each client (desktop browser, phone, QGIS plugin,
# Playwright) presented its own sticky anonymous_user_id -- so every device
# forked its own owner-scoped case list (log 2026-07-09 01:23:14 "hint ...
# not found; minting fresh" -> count=0 on a box full of cases). The fix
# collapses EVERY local-mode connection onto auth_handshake.LOCAL_SINGLE_USER_ID
# and adopts stray cases (minted by the old per-client anon users) onto it.
# These tests run on the REAL local substrate (FileMCPClient) for fidelity.
# --------------------------------------------------------------------------- #

from grace2_agent import auth_handshake
from grace2_agent.persistence import FileMCPClient


def _local_persistence(monkeypatch, tmp_path) -> Persistence:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    # Re-arm the once-per-process adoption sweep for test isolation.
    monkeypatch.setattr(auth_handshake, "_local_case_adoption_done", False)
    return Persistence(FileMCPClient(tmp_path))


def _case(title: str) -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=now_utc(),
        updated_at=now_utc(),
    )


@pytest.mark.asyncio
async def test_local_mode_two_anon_ids_resolve_to_same_user(
    monkeypatch, tmp_path
) -> None:
    """Two DIFFERENT client anon hints both resolve to the fixed local user,
    the ack stays anonymous (clients keep their sticky logic), and a case
    created via one device is listed for the other."""
    p = _local_persistence(monkeypatch, tmp_path)

    desktop = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=new_ulid()), p
    )
    phone = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=new_ulid()), p
    )

    assert desktop.user.user_id == auth_handshake.LOCAL_SINGLE_USER_ID
    assert phone.user.user_id == auth_handshake.LOCAL_SINGLE_USER_ID
    # is_anonymous survives so the auth-ack keeps client sticky logic intact.
    assert desktop.is_anonymous is True
    assert phone.is_anonymous is True

    case = _case("Desktop Case")
    await p.upsert_case(case, owner_user_id=desktop.user.user_id)
    listed = await p.list_cases_for_user(phone.user.user_id)
    assert [c.case_id for c in listed] == [case.case_id]


@pytest.mark.asyncio
async def test_local_mode_adopts_stray_per_anon_user_cases(
    monkeypatch, tmp_path
) -> None:
    """Pre-fix cases owned by per-client anonymous users (and legacy
    owner-less cases) are adopted by the single local user on first auth."""
    p = _local_persistence(monkeypatch, tmp_path)

    stray_a = _case("Phone Case")
    stray_b = _case("Playwright Case")
    orphan = _case("Pre-auth Case")
    await p.upsert_case(stray_a, owner_user_id=new_ulid())
    await p.upsert_case(stray_b, owner_user_id=new_ulid())
    await p.upsert_case(orphan)  # no owner stamped at all

    result = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert result.user.user_id == auth_handshake.LOCAL_SINGLE_USER_ID

    listed = await p.list_cases_for_user(auth_handshake.LOCAL_SINGLE_USER_ID)
    assert {c.case_id for c in listed} == {
        stray_a.case_id,
        stray_b.case_id,
        orphan.case_id,
    }


@pytest.mark.asyncio
async def test_local_mode_user_record_stable_across_reconnects(
    monkeypatch, tmp_path
) -> None:
    """The second connect REUSES the persisted local-user record (no
    re-provision churn) and no hint at all still lands on the local user."""
    p = _local_persistence(monkeypatch, tmp_path)

    first = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=new_ulid()), p
    )
    second = await authenticate_token(AuthTokenEnvelope(token=""), p)
    third = await authenticate_token(None, p)

    assert (
        first.user.user_id
        == second.user.user_id
        == third.user.user_id
        == auth_handshake.LOCAL_SINGLE_USER_ID
    )
    # Stable record: created_at survives the reconnects (reuse, not re-upsert).
    assert second.user.created_at == first.user.created_at
    assert third.user.created_at == first.user.created_at
