"""Test anonymous-hint handling under the LOCAL single-user build.

History: job-0172 Part C made the H.3 anonymous path sticky (the client
replays its assigned ``user_id`` via ``AuthTokenEnvelope.anonymous_user_id``
and the agent re-binds the same record). The TRID3NT local build then pinned
``solver_backend()`` to ``local-docker``, so ``authenticate_token`` now takes
the F1 single-user branch UNCONDITIONALLY: the ``anonymous_user_id`` hint is
still accepted on the wire (clients keep their sticky logic unchanged), but
EVERY connection resolves to the ONE fixed local user
(``auth_handshake.LOCAL_SINGLE_USER_ID``). The per-hint reuse/verbatim
provisioning branches below it are unreachable in this build.

These tests pin that resolution truth in isolation (the web persistence is
verified separately in the web test suite).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from trid3nt_server.auth_handshake import LOCAL_SINGLE_USER_ID, authenticate_token
from trid3nt_server.persistence import Persistence
from trid3nt_contracts.auth import AuthTokenEnvelope
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.user import User


class FakeMCPClient:
    """In-memory MCP client that round-trips users/cases for tests."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        coll = args.get("collection")
        if coll != "users":
            return {"document": None}
        if name == "find-one":
            filt = args.get("filter", {})
            uid = filt.get("_id")
            if uid and uid in self.users:
                return {"document": self.users[uid]}
            return {"document": None}
        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {}).get("$set", {})
            uid = filt.get("_id")
            if uid is None:
                return {"matchedCount": 0, "modifiedCount": 0}
            if uid in self.users:
                self.users[uid].update(update)
            elif args.get("upsert"):
                self.users[uid] = dict(update)
            return {"matchedCount": 1, "modifiedCount": 1}
        return {}


@pytest.mark.asyncio
async def test_anonymous_reuse_rebinds_same_user_on_reconnect() -> None:
    """A reconnect re-binds the SAME user record: the fixed local user."""
    client = FakeMCPClient()
    p = Persistence(client)

    # First connect -- no hint, no token -> the fixed local single user.
    first = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert first.is_anonymous
    assert first.user.is_anonymous is True
    assert first.user.user_id == LOCAL_SINGLE_USER_ID
    assert first.user.user_id in client.users  # persisted

    # Second connect replaying that id as the sticky hint -- same user_id.
    hint = first.user.user_id
    second = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=hint), p
    )
    assert second.is_anonymous
    assert second.user.user_id == hint
    # Same User document -- never a fresh ULID in the local build.
    assert second.user.user_id == first.user.user_id
    assert list(client.users.keys()) == [LOCAL_SINGLE_USER_ID]


@pytest.mark.asyncio
async def test_anonymous_reuse_rejects_non_anonymous_record() -> None:
    """A hint replaying a non-anonymous record's id must NOT re-bind it.

    In the local build this holds trivially: the hint is ignored and the
    connection lands on the fixed local user, so a fished non-anonymous id
    can never be hijacked via the hint path -- and the seeded record stays
    byte-identical (never overwritten by the local-user upsert).
    """
    client = FakeMCPClient()
    p = Persistence(client)

    # Pre-seed a non-anonymous User record (legacy IdP-sub carrier populated).
    verified_id = new_ulid()
    verified = User(
        user_id=verified_id,
        firebase_uid="firebase-uid-001",
        created_at=now_utc(),
        is_anonymous=False,
    )
    await p.upsert_user(verified)
    seeded_doc = dict(client.users[verified_id])

    result = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=verified_id), p
    )
    assert result.is_anonymous
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.user_id != verified_id
    assert result.user.is_anonymous is True
    # The seeded non-anonymous record is untouched.
    assert client.users[verified_id] == seeded_doc


@pytest.mark.asyncio
async def test_anonymous_hint_for_unknown_id_lands_on_local_user() -> None:
    """A presented id with no record is accepted on the wire but NOT honored.

    F1 single-user truth: the hint that the sticky client replays is read
    without error, yet resolution lands on ``LOCAL_SINGLE_USER_ID`` -- no
    per-hint user is ever provisioned, so every device (desktop, phone, QGIS
    plugin, test driver) shares the one local case list.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    fake_id = new_ulid()
    result = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=fake_id), p
    )
    assert result.is_anonymous
    # Resolution lands on the fixed local user, never the presented id.
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.is_anonymous is True
    # No user record is minted for the hint; only the local user persists.
    assert fake_id not in client.users
    assert LOCAL_SINGLE_USER_ID in client.users
    assert client.users[LOCAL_SINGLE_USER_ID]["is_anonymous"] is True


@pytest.mark.asyncio
async def test_anonymous_hint_without_persistence_lands_on_local_user() -> None:
    """No Persistence -> the hint is still ignored; local user in-memory.

    Even with no collection to look up, the local build resolves to the fixed
    ``LOCAL_SINGLE_USER_ID`` (provisioned in-memory only on this path), so the
    session's sockets converge on one identity on the CI / no-persistence path
    too -- never on the client-presented id.
    """
    hint = new_ulid()
    result = await authenticate_token(
        AuthTokenEnvelope(token="", anonymous_user_id=hint), persistence=None
    )
    assert result.is_anonymous
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.user_id != hint
    assert result.user.is_anonymous is True


@pytest.mark.asyncio
async def test_anonymous_no_hint_no_persistence_lands_on_local_user() -> None:
    """No hint + no Persistence -> the fixed local user, not a fresh mint."""
    result = await authenticate_token(
        AuthTokenEnvelope(token=""), persistence=None
    )
    assert result.is_anonymous
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.is_anonymous is True
