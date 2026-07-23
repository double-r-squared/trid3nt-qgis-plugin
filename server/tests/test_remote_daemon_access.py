"""Remote-daemon access: endpoint advertisement + optional shared-token gate.

Lane S (server) coverage for the 2026-07 remote-daemon-access work. Two
independent features ride the connect handshake:

1. **Endpoint advertisement.** The server rides an optional ``endpoints`` object
   on the ``auth-ack`` (the first envelope the client parses) so a client
   configured with ONLY the WS URL learns the sibling ``data_base`` (MinIO) and
   ``http_base`` (agent HTTP) surfaces. Values come from
   ``TRID3NT_ADVERTISED_DATA_BASE`` / ``TRID3NT_ADVERTISED_HTTP_BASE`` when set,
   else DERIVED from the connection's own local address + the known ports.

2. **Optional shared token.** ``TRID3NT_ACCESS_TOKEN`` gates the handshake:
   when set, the client token must match (constant-time) or the connection is
   rejected with a typed ``AUTH_FAILED`` close (WS 1008). Unset (default) =
   byte-identical anonymous behavior.

The tests split into pure-unit (``auth_handshake`` derivation + token verify +
contract round-trip) and server-integration (``_handle_auth_token`` /
``_ensure_auth_handshake`` end-to-end against a fake socket). No live socket,
MinIO, or model is required -- everything runs offline.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.auth import AdvertisedEndpoints, AuthAckEnvelope
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.user import User

from trid3nt_server.auth_handshake import (
    ADVERTISED_DATA_PORT,
    ADVERTISED_HTTP_PORT_DEFAULT,
    AuthResult,
    build_auth_ack,
    configured_access_token,
    derive_advertised_endpoints,
    verify_access_token,
)

# Env keys the tests toggle -- cleared before each test so a stray export on
# the dev box can never leak into (or out of) a case.
_REMOTE_ENV_KEYS = (
    "TRID3NT_ADVERTISED_DATA_BASE",
    "TRID3NT_ADVERTISED_HTTP_BASE",
    "TRID3NT_ACCESS_TOKEN",
    "TRID3NT_AGENT_HTTP_PORT",
)


@pytest.fixture(autouse=True)
def _clean_remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a KNOWN-clean remote-access env."""
    for key in _REMOTE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make_auth_result(*, anonymous: bool = True) -> AuthResult:
    user = User(
        user_id=new_ulid(),
        firebase_uid=None,
        created_at=now_utc(),
        is_anonymous=anonymous,
    )
    return AuthResult(
        user=user,
        firebase_uid=None,
        is_anonymous=anonymous,
        tier="free",
    )


# --------------------------------------------------------------------------- #
# 1. derive_advertised_endpoints -- derived from the connection's local host
# --------------------------------------------------------------------------- #


def test_derive_endpoints_from_connection_host() -> None:
    """No env override: both bases derive from the connected-TO host + ports.

    A laptop dialing 100.64.0.1:8765 over the tailnet connected TO 100.64.0.1
    on the server side, so the server hands back that exact host.
    """
    ep = derive_advertised_endpoints("100.64.0.1")
    assert ep is not None
    assert ep.data_base == f"http://100.64.0.1:{ADVERTISED_DATA_PORT}"
    assert ep.http_base == f"http://100.64.0.1:{ADVERTISED_HTTP_PORT_DEFAULT}"


def test_derive_endpoints_default_ports_are_9000_and_8766() -> None:
    """The known ports are MinIO 9000 (data) + agent 8766 (http)."""
    assert ADVERTISED_DATA_PORT == 9000
    assert ADVERTISED_HTTP_PORT_DEFAULT == 8766
    ep = derive_advertised_endpoints("10.0.0.5")
    assert ep is not None
    assert ep.data_base.endswith(":9000")
    assert ep.http_base.endswith(":8766")


def test_derive_endpoints_ipv6_host_is_bracketed() -> None:
    """A bare IPv6 literal is bracketed so the :port suffix is unambiguous."""
    ep = derive_advertised_endpoints("fd7a:115c:a1e0::1")
    assert ep is not None
    assert ep.data_base == "http://[fd7a:115c:a1e0::1]:9000"
    assert ep.http_base == "http://[fd7a:115c:a1e0::1]:8766"


def test_derive_endpoints_http_port_follows_agent_http_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advertised http_base tracks the ACTUAL listener port env."""
    monkeypatch.setenv("TRID3NT_AGENT_HTTP_PORT", "9999")
    ep = derive_advertised_endpoints("192.168.1.10")
    assert ep is not None
    # data port is fixed to MinIO's 9000; only http tracks the env.
    assert ep.data_base == "http://192.168.1.10:9000"
    assert ep.http_base == "http://192.168.1.10:9999"


def test_derive_endpoints_bad_http_port_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable port env falls back to the 8766 default (never raises)."""
    monkeypatch.setenv("TRID3NT_AGENT_HTTP_PORT", "not-a-port")
    ep = derive_advertised_endpoints("192.168.1.10")
    assert ep is not None
    assert ep.http_base == "http://192.168.1.10:8766"


# --------------------------------------------------------------------------- #
# 2. derive_advertised_endpoints -- env override wins, per field
# --------------------------------------------------------------------------- #


def test_derive_endpoints_env_override_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env overrides win unconditionally (reverse-proxy / custom hostname)."""
    monkeypatch.setenv("TRID3NT_ADVERTISED_DATA_BASE", "https://data.example.com")
    monkeypatch.setenv("TRID3NT_ADVERTISED_HTTP_BASE", "https://api.example.com")
    ep = derive_advertised_endpoints("100.64.0.1")
    assert ep is not None
    assert ep.data_base == "https://data.example.com"
    assert ep.http_base == "https://api.example.com"


def test_derive_endpoints_env_override_one_field_derives_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only data overridden -> http still derives from the connection host."""
    monkeypatch.setenv("TRID3NT_ADVERTISED_DATA_BASE", "https://data.example.com")
    ep = derive_advertised_endpoints("100.64.0.1")
    assert ep is not None
    assert ep.data_base == "https://data.example.com"
    assert ep.http_base == "http://100.64.0.1:8766"


def test_derive_endpoints_env_override_with_no_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override applies even without a usable connection host."""
    monkeypatch.setenv("TRID3NT_ADVERTISED_HTTP_BASE", "https://api.example.com")
    ep = derive_advertised_endpoints(None)
    assert ep is not None
    assert ep.http_base == "https://api.example.com"
    # data has neither env nor host -> stays None (partial object is valid).
    assert ep.data_base is None


# --------------------------------------------------------------------------- #
# 3. derive_advertised_endpoints -- absent for old-stub / no-socket path
# --------------------------------------------------------------------------- #


def test_derive_endpoints_absent_without_host_or_env() -> None:
    """No host AND no env override -> None (old-stub / offline path)."""
    assert derive_advertised_endpoints(None) is None
    assert derive_advertised_endpoints("") is None


# --------------------------------------------------------------------------- #
# 4. build_auth_ack -- endpoints ride the ack; backward-compatible default
# --------------------------------------------------------------------------- #


def test_build_auth_ack_carries_endpoints() -> None:
    ep = AdvertisedEndpoints(
        data_base="http://100.64.0.1:9000",
        http_base="http://100.64.0.1:8766",
    )
    ack = build_auth_ack(_make_auth_result(), endpoints=ep)
    assert ack.endpoints is not None
    assert ack.endpoints.data_base == "http://100.64.0.1:9000"
    assert ack.endpoints.http_base == "http://100.64.0.1:8766"


def test_build_auth_ack_endpoints_default_none_backward_compatible() -> None:
    """The pre-existing single-arg call still works; endpoints defaults None."""
    ack = build_auth_ack(_make_auth_result())
    assert ack.endpoints is None
    # Old-client / stub wire shape: the field is present-but-null, never a
    # credential, never breaks extra="forbid" round-trips.
    dumped = ack.model_dump(mode="json")
    assert dumped["endpoints"] is None
    assert "token" not in dumped


def test_auth_ack_round_trip_with_endpoints() -> None:
    """AuthAckEnvelope with endpoints survives a JSON round-trip (extra=forbid)."""
    ep = AdvertisedEndpoints(data_base="http://h:9000", http_base="http://h:8766")
    ack = build_auth_ack(_make_auth_result(), endpoints=ep)
    raw = ack.model_dump_json()
    back = AuthAckEnvelope.model_validate_json(raw)
    assert back.endpoints == ep


def test_auth_ack_old_wire_without_endpoints_parses() -> None:
    """An OLD server's ack (no endpoints key at all) still validates -> None."""
    old_wire = {"user_id": new_ulid(), "is_anonymous": True, "tier": "free"}
    back = AuthAckEnvelope.model_validate(old_wire)
    assert back.endpoints is None


# --------------------------------------------------------------------------- #
# 5. verify_access_token -- on / off / mismatch
# --------------------------------------------------------------------------- #


def test_verify_access_token_disabled_by_default() -> None:
    """Unset TRID3NT_ACCESS_TOKEN -> gate open, every token accepted."""
    assert configured_access_token() is None
    assert verify_access_token(None) is True
    assert verify_access_token("") is True
    assert verify_access_token("whatever") is True


def test_verify_access_token_empty_env_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank env cannot accidentally lock everyone out."""
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "")
    assert configured_access_token() is None
    assert verify_access_token(None) is True


def test_verify_access_token_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "s3cr3t")
    assert configured_access_token() == "s3cr3t"
    assert verify_access_token("s3cr3t") is True


def test_verify_access_token_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "s3cr3t")
    assert verify_access_token("wrong") is False
    assert verify_access_token("") is False
    assert verify_access_token(None) is False


# --------------------------------------------------------------------------- #
# Server integration: fake socket exercising _handle_auth_token / _ensure_*
# --------------------------------------------------------------------------- #


class _FakeWebSocket:
    """Stand-in for ``ServerConnection`` with send / close / local_address.

    ``local_address`` is the (host, port) the CLIENT connected TO on the server
    side -- the derivation source for the advertised endpoints.
    """

    def __init__(self, local_address: tuple | None = ("100.64.0.1", 8765)) -> None:
        self.sent: list[dict] = []
        self.local_address = local_address
        self.closed_with: tuple[int, str] | None = None

    async def send(self, raw) -> None:
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


def _sent_types(ws: _FakeWebSocket) -> list[str]:
    return [e.get("type") for e in ws.sent]


@pytest.fixture()
def _no_persistence():
    """Run the server handshake helpers with the M1 in-memory (None) path."""
    from trid3nt_server.server import set_persistence

    set_persistence(None)
    yield
    set_persistence(None)


@pytest.mark.asyncio
async def test_handle_auth_token_advertises_derived_endpoints(
    _no_persistence,
) -> None:
    """auth-ack carries endpoints derived from the connection's local address."""
    from trid3nt_server.server import SessionState, _handle_auth_token

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket(local_address=("100.64.0.1", 8765))
    await _handle_auth_token(ws, state, {"token": "", "anonymous_user_id": None})

    assert _sent_types(ws) == ["auth-ack"]
    payload = ws.sent[0]["payload"]
    assert payload["endpoints"] == {
        "data_base": "http://100.64.0.1:9000",
        "http_base": "http://100.64.0.1:8766",
    }
    assert ws.closed_with is None


@pytest.mark.asyncio
async def test_handle_auth_token_absent_endpoints_for_socket_without_address(
    _no_persistence,
) -> None:
    """A fake socket with no local_address + no env -> endpoints absent (None).

    This is the old-stub path: nothing changes on the wire for a client that
    connected through a transport we cannot introspect.
    """
    from trid3nt_server.server import SessionState, _handle_auth_token

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket(local_address=None)
    await _handle_auth_token(ws, state, {"token": "", "anonymous_user_id": None})

    assert _sent_types(ws) == ["auth-ack"]
    assert ws.sent[0]["payload"]["endpoints"] is None


@pytest.mark.asyncio
async def test_token_gate_off_is_anonymous_regression(_no_persistence) -> None:
    """Gate unset (default): the anon handshake is byte-identical -- ack, no close."""
    from trid3nt_server.server import SessionState, _handle_auth_token

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(ws, state, {"token": "anything", "anonymous_user_id": None})

    assert _sent_types(ws) == ["auth-ack"]
    assert ws.closed_with is None
    assert state.auth_handshake_complete is True
    assert state.authenticated_user_id is not None


@pytest.mark.asyncio
async def test_token_gate_on_correct_token_accepts(
    _no_persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate set + matching token -> normal auth-ack, no close."""
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "s3cr3t")
    from trid3nt_server.server import SessionState, _handle_auth_token

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(
        ws, state, {"token": "s3cr3t", "anonymous_user_id": None}
    )

    assert _sent_types(ws) == ["auth-ack"]
    assert ws.closed_with is None
    assert state.auth_handshake_complete is True


@pytest.mark.asyncio
async def test_token_gate_on_wrong_token_typed_close(
    _no_persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate set + wrong token -> AUTH_FAILED error + 1008 close, NO auth-ack, no bind."""
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "s3cr3t")
    from trid3nt_server.server import SessionState, _handle_auth_token

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(
        ws, state, {"token": "wrong", "anonymous_user_id": None}
    )

    # A typed error envelope, then a policy-violation (1008) close.
    assert _sent_types(ws) == ["error"]
    assert ws.sent[0]["payload"]["error_code"] == "AUTH_FAILED"
    assert ws.closed_with == (1008, "AUTH_FAILED")
    # The session must NOT be bound on a rejected handshake.
    assert state.auth_handshake_complete is False
    assert state.authenticated_user_id is None


@pytest.mark.asyncio
async def test_token_gate_on_missing_token_typed_close(
    _no_persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate set + empty token -> rejected (an empty token is not the secret)."""
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "s3cr3t")
    from trid3nt_server.server import SessionState, _handle_auth_token

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(ws, state, {"token": "", "anonymous_user_id": None})

    assert _sent_types(ws) == ["error"]
    assert ws.sent[0]["payload"]["error_code"] == "AUTH_FAILED"
    assert ws.closed_with == (1008, "AUTH_FAILED")
    assert state.auth_handshake_complete is False


@pytest.mark.asyncio
async def test_implicit_handshake_rejected_when_token_required(
    _no_persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token-gated daemon rejects the implicit (no-auth-token) path too.

    Otherwise a client could bypass the gate by simply never sending
    auth-token. ``_ensure_auth_handshake`` returns False (do-not-dispatch) and
    closes the socket with the same typed AUTH_FAILED close.
    """
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "s3cr3t")
    from trid3nt_server.server import SessionState, _ensure_auth_handshake

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    proceed = await _ensure_auth_handshake(ws, state)

    assert proceed is False
    assert _sent_types(ws) == ["error"]
    assert ws.sent[0]["payload"]["error_code"] == "AUTH_FAILED"
    assert ws.closed_with == (1008, "AUTH_FAILED")
    assert state.auth_handshake_complete is False


@pytest.mark.asyncio
async def test_implicit_handshake_proceeds_when_token_off(
    _no_persistence,
) -> None:
    """Gate unset: the implicit anonymous path still binds + acks (regression)."""
    from trid3nt_server.server import SessionState, _ensure_auth_handshake

    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    proceed = await _ensure_auth_handshake(ws, state)

    assert proceed is True
    assert _sent_types(ws) == ["auth-ack"]
    assert ws.closed_with is None
    assert state.auth_handshake_complete is True
    # The implicit path also advertises endpoints.
    assert ws.sent[0]["payload"]["endpoints"] == {
        "data_base": "http://100.64.0.1:9000",
        "http_base": "http://100.64.0.1:8766",
    }
