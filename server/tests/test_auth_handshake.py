"""Unit + integration tests for ``trid3nt_server.credentials.auth_handshake`` (local build).

The local build has NO token verification and no identity provider:
``solver_backend()`` is hardwired to ``local-docker``, so EVERY connection --
any token -- resolves to the ONE fixed local user
(``LOCAL_SINGLE_USER_ID``). Coverage:

1. ``test_authenticate_token_nonempty_token_resolves_local_user`` -- a
   presented token is ignored -> the fixed local user.
2. ``test_authenticate_token_empty_token_resolves_local_user`` -- empty token.
3. ``test_authenticate_token_no_envelope_resolves_local_user`` -- None envelope.
4. ``test_local_user_shape`` -- resolved user is anonymous, is_active=True.
5. ``test_build_auth_ack_shape`` -- ack envelope mirrors AuthResult fields,
   no raw token leaks.
6. ``test_persistence_unbound_returns_in_memory_user`` -- Persistence=None
   path returns an in-memory User without raising.
7. Integration: ``test_server_connect_handshake_flow_with_mocks`` -- drives
   the full ``_handle_auth_token`` path through the server using mock
   Persistence; asserts SessionState binding and the auth-ack envelope.
8. ``test_connection_context_retains_authenticated_user_id`` -- a second
   handshake call never rebinds a completed session.
9. ``test_auth_envelope_contracts_round_trip`` -- wire-contract guard.
10. ``test_non_local_mode_raises`` -- outside local single-user mode the
    handshake fails LOUD (typed rejection), never silently resolves.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.credentials import auth_handshake
from trid3nt_server.credentials.auth_handshake import (
    LOCAL_SINGLE_USER_ID,
    AuthResult,
    NonLocalAuthUnsupported,
    authenticate_token,
    build_auth_ack,
)
from trid3nt_server.persistence import Persistence
from trid3nt_contracts.auth import AuthAckEnvelope, AuthTokenEnvelope
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.user import User


# --------------------------------------------------------------------------- #
# Mock MCP client (subset of trid3nt_server.tests.test_persistence.MockMCPClient)
# --------------------------------------------------------------------------- #


class MockMCPClient:
    """In-memory mock of the MongoDB MCP server's tool surface."""

    def __init__(self) -> None:
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
            return {"matchedCount": 1, "modifiedCount": 1}

        if name == "find-one":
            filt = args.get("filter", {})
            for doc in store.values():
                if all(doc.get(k) == v for k, v in filt.items()):
                    return {"document": doc}
            return {"document": None}

        if name == "find":
            filt = args.get("filter", {})
            out = []
            for doc in store.values():
                if all(doc.get(k) == v for k, v in filt.items()):
                    out.append(doc)
            return {"documents": out}

        raise RuntimeError(f"MockMCPClient: unhandled tool {name}")


@pytest.fixture()
def persistence() -> Persistence:
    return Persistence(MockMCPClient())


# --------------------------------------------------------------------------- #
# 1. Non-empty token -> anonymous fallback (no verifier in the local build)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authenticate_token_nonempty_token_resolves_local_user(
    persistence: Persistence,
) -> None:
    """A presented token is ignored -> the fixed local user."""
    result = await authenticate_token(
        AuthTokenEnvelope(token="any.jwt.like.string"), persistence
    )

    assert result.is_anonymous is True
    assert result.user.user_id == LOCAL_SINGLE_USER_ID
    assert result.user.is_active is True


# --------------------------------------------------------------------------- #
# 2. Empty token -> local user
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authenticate_token_empty_token_resolves_local_user(
    persistence: Persistence,
) -> None:
    """Empty token string -> the fixed local user."""
    result = await authenticate_token(AuthTokenEnvelope(token=""), persistence)
    assert result.is_anonymous is True
    assert result.user.user_id == LOCAL_SINGLE_USER_ID


# --------------------------------------------------------------------------- #
# 3. None envelope -> local user
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authenticate_token_no_envelope_resolves_local_user(
    persistence: Persistence,
) -> None:
    """No envelope at all -> the fixed local user."""
    result = await authenticate_token(None, persistence)
    assert result.is_anonymous is True
    assert result.user.user_id == LOCAL_SINGLE_USER_ID


# --------------------------------------------------------------------------- #
# 4. Local user shape
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_local_user_shape(
    persistence: Persistence,
) -> None:
    """Resolved local User: anonymous, is_active=True, no email."""
    result = await authenticate_token(None, persistence)
    u = result.user
    assert u.is_anonymous is True
    assert u.email is None
    assert u.is_active is True
    assert u.user_id == LOCAL_SINGLE_USER_ID


# --------------------------------------------------------------------------- #
# 5. build_auth_ack shape + no token leak
# --------------------------------------------------------------------------- #


def test_build_auth_ack_shape() -> None:
    """``build_auth_ack`` mirrors AuthResult and never carries the raw token."""
    uid = new_ulid()
    user = User(
        user_id=uid,
        created_at=now_utc(),
        is_anonymous=True,
    )
    result = AuthResult(
        user=user,
        is_anonymous=True,
    )
    ack = build_auth_ack(result)
    assert ack.user_id == uid
    assert ack.is_anonymous is True

    # Critical Decision-F backstop: the ack's wire form must NOT carry the
    # token, the email, or any credential.
    a = ack.model_dump(mode="json")
    assert "token" not in a
    assert "email" not in a
    assert "password" not in a


# --------------------------------------------------------------------------- #
# 6. Persistence unbound returns in-memory user
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_persistence_unbound_returns_in_memory_user() -> None:
    """Persistence=None -> in-memory local User, no raise."""
    result = await authenticate_token(AuthTokenEnvelope(token="x"), None)
    assert result.is_anonymous is True
    assert result.user.user_id == LOCAL_SINGLE_USER_ID

    # No envelope with Persistence=None also lands on the local user.
    result2 = await authenticate_token(None, None)
    assert result2.is_anonymous is True
    assert result2.user.user_id == LOCAL_SINGLE_USER_ID


# --------------------------------------------------------------------------- #
# 7. Integration: full WS connect -> auth-token -> auth-ack flow
# --------------------------------------------------------------------------- #


class _FakeWebSocket:
    """Minimal stand-in for ``websockets.asyncio.server.ServerConnection``.

    Only ``send`` is exercised -- every envelope the handler tries to send
    lands in ``self.sent`` as a JSON-decoded dict so tests can assert types
    + payloads.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_server_connect_handshake_flow_with_mocks() -> None:
    """Integration: full WS connect -> auth-token -> auth-ack with mocks.

    Drives ``server._handle_auth_token`` end-to-end against a
    MockMCPClient-backed Persistence (no live store needed).

    Verifies:
    - SessionState ``authenticated_user_id`` is populated.
    - SessionState ``is_anonymous`` is True (local build: every connection
      is anonymous).
    - The wire emits exactly one envelope of type ``auth-ack`` carrying
      the resolved user_id.
    - A subsequent non-handshake envelope arriving without an auth-token
      flips the implicit-anonymous fallback path on a fresh state.
    """
    from trid3nt_server.server import (
        SessionState,
        _ensure_auth_handshake,
        _handle_auth_token,
        set_persistence,
    )

    # Bind the mock Persistence into the server singleton.
    p = Persistence(MockMCPClient())
    set_persistence(p)

    # Path A: explicit auth-token envelope (token ignored -> anonymous).
    state_a = SessionState(session_id=new_ulid())
    ws_a = _FakeWebSocket()
    await _handle_auth_token(
        ws_a,  # type: ignore[arg-type]
        state_a,
        {"token": "eyJ.fake.jwt", "anonymous": False},
    )

    # SessionState was bound.
    assert state_a.authenticated_user_id is not None
    assert state_a.is_anonymous is True
    assert state_a.auth_handshake_complete is True

    # The wire emitted an auth-ack with the right shape.
    assert len(ws_a.sent) == 1
    ack_env = ws_a.sent[0]
    assert ack_env["type"] == "auth-ack"
    assert ack_env["session_id"] == state_a.session_id
    payload = ack_env["payload"]
    assert payload["user_id"] == state_a.authenticated_user_id
    assert "firebase_uid" not in payload
    assert payload["is_anonymous"] is True
    assert "tier" not in payload  # tier claim cut (wave 11)
    # Decision F: no raw token on the wire.
    assert "token" not in payload

    # Path B: implicit fallback on a fresh state -- no auth-token envelope
    # ever arrives.
    state_b = SessionState(session_id=new_ulid())
    ws_b = _FakeWebSocket()
    await _ensure_auth_handshake(ws_b, state_b)  # type: ignore[arg-type]

    assert state_b.is_anonymous is True
    assert state_b.auth_handshake_complete is True
    # Auth-ack emitted for the anonymous fallback path too.
    assert len(ws_b.sent) == 1
    assert ws_b.sent[0]["type"] == "auth-ack"
    assert ws_b.sent[0]["payload"]["is_anonymous"] is True

    # Cleanup the persistence singleton so other tests get a clean slate.
    set_persistence(None)


# --------------------------------------------------------------------------- #
# 8. Connection-context retains authenticated_user_id across subsequent envelopes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_connection_context_retains_authenticated_user_id() -> None:
    """SessionState.authenticated_user_id survives across a second handshake call."""
    from trid3nt_server.server import (
        SessionState,
        _bind_auth_result,
        _ensure_auth_handshake,
    )

    state = SessionState(session_id=new_ulid())
    fixed_user_id = new_ulid()
    result = AuthResult(
        user=User(
            user_id=fixed_user_id,
            created_at=now_utc(),
            is_anonymous=True,
        ),
        is_anonymous=True,
    )
    _bind_auth_result(state, result)
    assert state.authenticated_user_id == fixed_user_id
    assert state.auth_handshake_complete is True

    # A second ``_ensure_auth_handshake`` call is a no-op (handshake already
    # complete) -- the bound user_id MUST NOT be overwritten.
    class _NoopWS:
        async def send(self, raw):
            raise AssertionError(
                "send must not be called when handshake already complete"
            )

    await _ensure_auth_handshake(_NoopWS(), state)  # type: ignore[arg-type]
    assert state.authenticated_user_id == fixed_user_id
    assert state.is_anonymous is True


# --------------------------------------------------------------------------- #
# 9. AuthTokenEnvelope round-trip across the wire (contract handshake)
# --------------------------------------------------------------------------- #


def test_auth_envelope_contracts_round_trip() -> None:
    """Auth envelope contracts JSON-round-trip cleanly (agent-side guard)."""
    tok = AuthTokenEnvelope(token="eyJabc.payload.sig", anonymous=False)
    a = tok.model_dump(mode="json")
    b = AuthTokenEnvelope.model_validate(json.loads(json.dumps(a))).model_dump(
        mode="json"
    )
    assert a == b

    ack = AuthAckEnvelope(
        user_id=new_ulid(),
        is_anonymous=True,
    )
    c = ack.model_dump(mode="json")
    d = AuthAckEnvelope.model_validate(json.loads(json.dumps(c))).model_dump(
        mode="json"
    )
    assert c == d


# --------------------------------------------------------------------------- #
# 10. Non-local mode fails LOUD (typed rejection)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_non_local_mode_raises(
    persistence: Persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside local single-user mode the handshake raises a typed rejection.

    Unreachable in the product (``solver_backend()`` is hardwired to
    ``local-docker``), but the guard must fail LOUD rather than silently
    resolve an unauthenticated identity -- the deleted cloud/multi-user
    anonymous-provisioning branch.
    """
    monkeypatch.setattr(
        auth_handshake, "_is_local_single_user_mode", lambda: False
    )
    with pytest.raises(NonLocalAuthUnsupported):
        await authenticate_token(AuthTokenEnvelope(token=""), persistence)
