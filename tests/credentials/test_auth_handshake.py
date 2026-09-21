"""``trid3nt_server.credentials.auth_handshake``: one token, one user, always on.

The daemon mints its access token into the config file at first start, every
connection must present that token, and a verified connection is scoped to the
one fixed session identity. Offline."""

from __future__ import annotations

import json
import stat

import pytest

from trid3nt_contracts.auth import AuthAckEnvelope, AuthTokenEnvelope
from trid3nt_contracts.common import new_ulid

from trid3nt_server.credentials.auth_handshake import (
    LOCAL_SINGLE_USER_ID,
    access_token_path,
    build_auth_ack,
    configured_access_token,
    ensure_access_token,
    verify_access_token,
)


@pytest.fixture(autouse=True)
def _config_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the config home at an empty tmp dir so no case reads or writes the
    dev box's own minted token, and clear the env override."""
    monkeypatch.delenv("TRID3NT_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("TRID3NT_HOME", str(tmp_path))


def test_mints_token_once_into_the_config_file() -> None:
    """First start writes the token 0600; a later start reads the same one."""
    path = access_token_path()
    assert not path.exists()

    minted = ensure_access_token()
    assert minted
    assert path.read_text(encoding="utf-8").strip() == minted
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert ensure_access_token() == minted
    assert configured_access_token() == minted


def test_env_override_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """TRID3NT_ACCESS_TOKEN wins and no config file is written."""
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "from-the-env")
    assert ensure_access_token() == "from-the-env"
    assert configured_access_token() == "from-the-env"
    assert not access_token_path().exists()


def test_minted_token_is_the_only_one_that_verifies() -> None:
    minted = ensure_access_token()
    assert verify_access_token(minted) is True
    assert verify_access_token(minted + "x") is False
    assert verify_access_token("") is False
    assert verify_access_token(None) is False


def test_build_auth_ack_carries_the_fixed_identity() -> None:
    """The ack mirrors the one session identity and NEVER a credential."""
    ack = build_auth_ack()
    assert ack.user_id == LOCAL_SINGLE_USER_ID
    dumped = ack.model_dump(mode="json")
    assert "token" not in dumped
    assert dumped["endpoints"] is None


class _FakeWebSocket:
    """Minimal stand-in for ``websockets.asyncio.server.ServerConnection``:
    every envelope the handler sends lands in ``sent``, and a close is recorded
    rather than performed."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.local_address = None
        self.closed_with: tuple[int, str] | None = None

    async def send(self, raw) -> None:
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


@pytest.fixture()
def _no_persistence():
    """Run the handshake with the in-memory (None) persistence path."""
    from trid3nt_server.server import set_persistence

    set_persistence(None)
    yield
    set_persistence(None)


@pytest.mark.asyncio
async def test_right_token_binds_the_session(_no_persistence) -> None:
    from trid3nt_server.server import SessionState, _handle_auth_token

    token = ensure_access_token()
    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(ws, state, {"token": token})

    assert [e["type"] for e in ws.sent] == ["auth-ack"]
    assert ws.closed_with is None
    payload = ws.sent[0]["payload"]
    assert payload["user_id"] == LOCAL_SINGLE_USER_ID
    assert "token" not in payload
    assert state.authenticated_user_id == LOCAL_SINGLE_USER_ID
    assert state.auth_handshake_complete is True


@pytest.mark.asyncio
async def test_token_less_connect_is_refused(_no_persistence) -> None:
    """No token presented at all -> the typed refusal, and nothing is bound."""
    from trid3nt_server.server import SessionState, _handle_auth_token

    ensure_access_token()
    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(ws, state, {})

    assert [e["type"] for e in ws.sent] == ["error"]
    assert ws.sent[0]["payload"]["error_code"] == "AUTH_FAILED"
    assert ws.closed_with == (1008, "AUTH_FAILED")
    assert state.auth_handshake_complete is False
    assert state.authenticated_user_id is None


@pytest.mark.asyncio
async def test_malformed_envelope_is_refused(_no_persistence) -> None:
    """A payload that is not an auth-token presents nothing, so it is refused
    rather than falling through to any lesser path."""
    from trid3nt_server.server import SessionState, _handle_auth_token

    ensure_access_token()
    state = SessionState(session_id=new_ulid())
    ws = _FakeWebSocket()
    await _handle_auth_token(ws, state, {"token": ["not", "a", "string"]})

    assert ws.sent[0]["payload"]["error_code"] == "AUTH_FAILED"
    assert ws.closed_with == (1008, "AUTH_FAILED")
    assert state.auth_handshake_complete is False


def test_auth_envelope_contracts_round_trip() -> None:
    """Auth envelope contracts JSON-round-trip cleanly (agent-side guard)."""
    tok = AuthTokenEnvelope(token="minted-token-value")
    a = tok.model_dump(mode="json")
    b = AuthTokenEnvelope.model_validate(json.loads(json.dumps(a))).model_dump(
        mode="json"
    )
    assert a == b

    ack = AuthAckEnvelope(user_id=new_ulid())
    c = ack.model_dump(mode="json")
    d = AuthAckEnvelope.model_validate(json.loads(json.dumps(c))).model_dump(
        mode="json"
    )
    assert c == d
