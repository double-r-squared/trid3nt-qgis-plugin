"""Anonymous resolution under the LOCAL single-user build.

``solver_backend()`` is pinned to ``local-docker``, so ``authenticate_token``
takes the F1 single-user branch UNCONDITIONALLY: EVERY connection resolves to
the ONE fixed local user (``auth_handshake.LOCAL_SINGLE_USER_ID``). The sticky
``anonymous_user_id`` client-hint that once rode the wire is DELETED (wave 11
feature cut) -- with resolution pinned to a fixed constant there is no per-hint
reuse/verbatim-provisioning branch to exercise.

These tests pin that resolution truth in isolation (the web persistence is
verified separately in the web test suite).
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID, authenticate_token
from trid3nt_server.persistence import Persistence
from trid3nt_contracts.auth import AuthTokenEnvelope
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.user import User


class FakeMCPClient:
    """In-memory MCP client that round-trips users for tests."""

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
async def test_reconnect_rebinds_same_local_user() -> None:
    """A reconnect re-binds the SAME user record: the fixed local user."""
    client = FakeMCPClient()
    p = Persistence(client)

    first = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert first.is_anonymous
    assert first.user.is_anonymous is True
    assert first.user.user_id == LOCAL_SINGLE_USER_ID
    assert first.user.user_id in client.users  # persisted

    second = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert second.is_anonymous
    assert second.user.user_id == first.user.user_id
    assert list(client.users.keys()) == [LOCAL_SINGLE_USER_ID]


@pytest.mark.asyncio
async def test_pre_seeded_non_anonymous_record_untouched() -> None:
    """A pre-seeded non-anonymous record with a different id is never touched.

    Resolution lands on the fixed local user, so an unrelated record can never
    be hijacked or overwritten by the local-user upsert.
    """
    client = FakeMCPClient()
    p = Persistence(client)

    verified_id = new_ulid()
    verified = User(
        user_id=verified_id,
        created_at=now_utc(),
        is_anonymous=False,
    )
    await p.upsert_user(verified)
    seeded_doc = dict(client.users[verified_id])

    result = await authenticate_token(AuthTokenEnvelope(token=""), p)
    assert result.is_anonymous
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.user_id != verified_id
    assert client.users[verified_id] == seeded_doc


@pytest.mark.asyncio
async def test_without_persistence_lands_on_local_user() -> None:
    """No Persistence -> the fixed local user, provisioned in-memory only."""
    result = await authenticate_token(AuthTokenEnvelope(token=""), persistence=None)
    assert result.is_anonymous
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.is_anonymous is True


@pytest.mark.asyncio
async def test_none_envelope_lands_on_local_user() -> None:
    """A None (implicit-anonymous) envelope resolves to the fixed local user."""
    result = await authenticate_token(None, persistence=None)
    assert result.is_anonymous
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.is_anonymous is True
