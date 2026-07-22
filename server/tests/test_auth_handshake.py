"""Unit + integration tests for ``trid3nt_server.auth_handshake`` (local build).

The local build has NO token verification (no identity provider): every
connection resolves through the anonymous path (H.3) or, in local-docker
mode, the ONE fixed local user. Coverage:

1. ``test_authenticate_token_nonempty_token_falls_back_anonymous`` -- a
   presented token is ignored -> anonymous fallback.
2. ``test_authenticate_token_empty_token_is_anonymous`` -- empty token
   string -> anonymous fallback.
3. ``test_authenticate_token_no_envelope_is_anonymous`` -- None envelope
   -> anonymous fallback.
4. ``test_anonymous_user_has_no_firebase_uid`` -- anonymous fallback user
   has firebase_uid=None, is_active=True.
5. ``test_build_auth_ack_shape`` -- ack envelope mirrors AuthResult fields,
   no raw token leaks.
6. ``test_persistence_unbound_returns_in_memory_user`` -- Persistence=None
   path returns a fresh in-memory User without raising.
7. Integration: ``test_server_connect_handshake_flow_with_mocks`` -- drives
   the full ``_handle_auth_token`` path through the server using mock
   Persistence; asserts SessionState binding and the auth-ack envelope on
   the wire.
8. ``test_connection_context_retains_authenticated_user_id`` -- a second
   handshake call never rebinds a completed session.
9. ``test_auth_envelope_contracts_round_trip`` -- wire-contract guard.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.auth_handshake import (
    AuthResult,
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
async def test_authenticate_token_nonempty_token_falls_back_anonymous(
    persistence: Persistence,
) -> None:
    """Anonymous fallback: ephemeral user created without firebase_uid."""
    result = await authenticate_token(
        AuthTokenEnvelope(token="any.jwt.like.string"), persistence
    )

    assert result.is_anonymous is True
    assert result.firebase_uid is None
    assert result.tier == "free"
    assert result.user.firebase_uid is None
    assert result.user.is_active is True


# --------------------------------------------------------------------------- #
# 2. Empty token -> anonymous
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authenticate_token_empty_token_is_anonymous(
    persistence: Persistence,
) -> None:
    """Empty token string -> anonymous fallback."""
    result = await authenticate_token(AuthTokenEnvelope(token=""), persistence)
    assert result.is_anonymous is True


# --------------------------------------------------------------------------- #
# 3. None envelope -> anonymous
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authenticate_token_no_envelope_is_anonymous(
    persistence: Persistence,
) -> None:
    """No envelope at all -> anonymous fallback."""
    result = await authenticate_token(None, persistence)
    assert result.is_anonymous is True
    assert result.firebase_uid is None


# --------------------------------------------------------------------------- #
# 4. Anonymous user shape
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_anonymous_user_has_no_firebase_uid(
    persistence: Persistence,
) -> None:
    """Anonymous fallback User: firebase_uid=None, is_active=True."""
    result = await authenticate_token(None, persistence)
    u = result.user
    assert u.firebase_uid is None
    assert u.email is None
    assert u.is_active is True
    # ULID discipline still holds.
    assert len(u.user_id) == 26


# --------------------------------------------------------------------------- #
# 5. build_auth_ack shape + no token leak
# --------------------------------------------------------------------------- #


def test_build_auth_ack_shape() -> None:
    """``build_auth_ack`` mirrors AuthResult and never carries the raw token."""
    uid = new_ulid()
    user = User(
        user_id=uid,
        firebase_uid=None,
        created_at=now_utc(),
        is_anonymous=True,
    )
    result = AuthResult(
        user=user,
        firebase_uid=None,
        is_anonymous=True,
        tier="free",
    )
    ack = build_auth_ack(result)
    assert ack.user_id == uid
    assert ack.firebase_uid is None
    assert ack.is_anonymous is True
    assert ack.tier == "free"

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
    """Persistence=None -> fresh in-memory anonymous User (M1 fallback)."""
    result = await authenticate_token(AuthTokenEnvelope(token="x"), None)
    assert result.is_anonymous is True
    assert result.user.firebase_uid is None

    # Anonymous fallback with Persistence=None also works with no envelope.
    result2 = await authenticate_token(None, None)
    assert result2.is_anonymous is True


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
    assert state_a.firebase_uid is None
    assert state_a.tier == "free"
    assert state_a.auth_handshake_complete is True

    # The wire emitted an auth-ack with the right shape.
    assert len(ws_a.sent) == 1
    ack_env = ws_a.sent[0]
    assert ack_env["type"] == "auth-ack"
    assert ack_env["session_id"] == state_a.session_id
    payload = ack_env["payload"]
    assert payload["user_id"] == state_a.authenticated_user_id
    assert payload["firebase_uid"] is None
    assert payload["is_anonymous"] is True
    assert payload["tier"] == "free"
    # Decision F: no raw token on the wire.
    assert "token" not in payload

    # Path B: implicit anonymous fallback on a fresh state -- no auth-token
    # envelope ever arrives.
    state_b = SessionState(session_id=new_ulid())
    ws_b = _FakeWebSocket()
    await _ensure_auth_handshake(ws_b, state_b)  # type: ignore[arg-type]

    assert state_b.is_anonymous is True
    assert state_b.firebase_uid is None
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
            firebase_uid=None,
            created_at=now_utc(),
            is_anonymous=True,
        ),
        firebase_uid=None,
        is_anonymous=True,
        tier="free",
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
        firebase_uid=None,
        is_anonymous=True,
        tier="free",
    )
    c = ack.model_dump(mode="json")
    d = AuthAckEnvelope.model_validate(json.loads(json.dumps(c))).model_dump(
        mode="json"
    )
    assert c == d
