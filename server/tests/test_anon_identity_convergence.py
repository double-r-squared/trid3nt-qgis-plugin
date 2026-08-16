"""Anon-identity convergence under the LOCAL single-user build.

CURRENT TRUTH (F1, TRID3NT local build): ``solver_backend()`` is hardwired to
``local-docker``, so ``authenticate_token`` resolves EVERY connection --
token-bearing or token-less -- to the ONE fixed local user
(``auth_handshake.LOCAL_SINGLE_USER_ID``). Convergence is therefore
unconditional by construction; identity forks are unrepresentable via the
handshake. The sticky ``anonymous_user_id`` hint and the session-scoped anon-id
registry that once collapsed the dual-socket no-hint race are DELETED (wave 11
feature cut): with resolution pinned to a fixed constant, there is no hint to
honor and no race to collapse.

These tests pin: resolution always lands on the local user, and the case-list
stability that resolution guarantees across reconnects.
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID, authenticate_token
from trid3nt_server.persistence import Persistence
from trid3nt_contracts.auth import AuthTokenEnvelope
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid, now_utc


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
# Resolution: every connection lands on the fixed local user.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sibling_sockets_converge_on_local_user() -> None:
    """Two connections of a session both resolve to ``LOCAL_SINGLE_USER_ID``.

    Exactly ONE user record exists across both connects.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    first = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert first.user.user_id == LOCAL_SINGLE_USER_ID

    second = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert second.user.user_id == LOCAL_SINGLE_USER_ID
    assert second.is_anonymous is True
    assert list(client.users.keys()) == [LOCAL_SINGLE_USER_ID]
    assert client.users[LOCAL_SINGLE_USER_ID]["is_anonymous"] is True


@pytest.mark.asyncio
async def test_no_hint_connections_cannot_fork_case_lists() -> None:
    """Two token-less connections resolve to ONE user; forking is impossible.

    The handshake can no longer produce two distinct identities, so the
    dual-socket fork that once motivated the sticky-hint machinery is
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


@pytest.mark.asyncio
async def test_case_list_stable_across_reconnect() -> None:
    """A Case stays visible on reconnect: every connection resolves to the one
    local user, so the owner-scoped list is identical."""
    client = FakeMCPClient()
    p = Persistence(client)

    first = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert first.user.user_id == LOCAL_SINGLE_USER_ID
    case = CaseSummary(
        case_id=new_ulid(),
        title="Refresh Test Case",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    await p.upsert_case(case, owner_user_id=first.user.user_id)

    cases_before = await p.list_cases_for_user(first.user.user_id)
    assert [c.case_id for c in cases_before] == [case.case_id]

    second = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert second.user.user_id == LOCAL_SINGLE_USER_ID
    cases_after = await p.list_cases_for_user(second.user.user_id)
    assert [c.case_id for c in cases_after] == [case.case_id]


@pytest.mark.asyncio
async def test_token_path_resolves_to_local_user() -> None:
    """A non-empty token is ignored (no verifier); resolution lands on the
    fixed local user."""
    client = FakeMCPClient()
    p = Persistence(client)

    res = await authenticate_token(AuthTokenEnvelope(token="real.jwt"), p)
    assert res.is_anonymous is True
    assert res.user.user_id == LOCAL_SINGLE_USER_ID


# --------------------------------------------------------------------------- #
# Real local substrate (FileMCPClient) fidelity.
# --------------------------------------------------------------------------- #

from trid3nt_server.credentials import auth_handshake
from trid3nt_server.persistence import FileMCPClient


def _local_persistence(monkeypatch, tmp_path) -> Persistence:
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    return Persistence(FileMCPClient(tmp_path))


def _case(title: str) -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=now_utc(),
        updated_at=now_utc(),
    )


@pytest.mark.asyncio
async def test_local_mode_connections_resolve_to_same_user(
    monkeypatch, tmp_path
) -> None:
    """Two connections both resolve to the fixed local user, the ack stays
    anonymous, and a case created via one is listed for the other."""
    p = _local_persistence(monkeypatch, tmp_path)

    desktop = await authenticate_token(AuthTokenEnvelope(token=""), p)
    phone = await authenticate_token(AuthTokenEnvelope(token=""), p)

    assert desktop.user.user_id == auth_handshake.LOCAL_SINGLE_USER_ID
    assert phone.user.user_id == auth_handshake.LOCAL_SINGLE_USER_ID
    assert desktop.is_anonymous is True
    assert phone.is_anonymous is True

    case = _case("Desktop Case")
    await p.upsert_case(case, owner_user_id=desktop.user.user_id)
    listed = await p.list_cases_for_user(phone.user.user_id)
    assert [c.case_id for c in listed] == [case.case_id]


def test_stray_case_adoption_removed() -> None:
    """Absence guard: the stray-case adoption sweep is gone (chop 4)."""
    assert not hasattr(Persistence, "adopt_cases_to_user")
    assert not hasattr(auth_handshake, "_local_case_adoption_done")


def test_session_anon_registry_removed() -> None:
    """Absence guard: the session-scoped anon-id mirror is gone (wave 11)."""
    from trid3nt_server import server

    for name in (
        "_SESSION_ANON_ID",
        "_get_session_anon_id",
        "_set_session_anon_id",
        "_apply_session_anon_hint",
    ):
        assert not hasattr(server, name), name


def test_auth_token_envelope_rejects_anonymous_user_id() -> None:
    """Wire proof: the sticky anon-id hint field is cut; ``extra="forbid"``
    rejects it."""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        AuthTokenEnvelope(token="", anonymous_user_id=new_ulid())  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_local_mode_user_record_stable_across_reconnects(
    monkeypatch, tmp_path
) -> None:
    """The second connect REUSES the persisted local-user record (no
    re-provision churn); no token at all still lands on the local user."""
    p = _local_persistence(monkeypatch, tmp_path)

    first = await authenticate_token(AuthTokenEnvelope(token=""), p)
    second = await authenticate_token(AuthTokenEnvelope(token=""), p)
    third = await authenticate_token(None, p)

    assert (
        first.user.user_id
        == second.user.user_id
        == third.user.user_id
        == auth_handshake.LOCAL_SINGLE_USER_ID
    )
    assert second.user.created_at == first.user.created_at
    assert third.user.created_at == first.user.created_at
