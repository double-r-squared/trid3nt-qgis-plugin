"""The runtime credential resolver over the credential each source row declares.

Resolution order (the session cache over the row's env var, a public tool
resolving to None), the session-cache lifecycle with blank inputs ignored, the
``secret-add`` handler writing the pushed value into that cache without
Persistence and without a file vault, and the rows the keys form offers."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.credentials import resolver
from trid3nt_contracts.secrets import SecretAddEnvelopePayload


@pytest.fixture(autouse=True)
def _clean_cache():
    resolver._SESSION_CREDENTIALS.clear()
    yield
    resolver._SESSION_CREDENTIALS.clear()




def test_session_cache_resolves_for_keyed_tool():
    resolver.set_session_credential("sess-1", "firms", "cache-key")
    assert resolver.resolve_credential("sess-1", "fetch_firms_active_fire") == "cache-key"


def test_env_fallback_when_no_cache(monkeypatch):
    monkeypatch.setenv("TRID3NT_FIRMS_MAP_KEY", "env-key")
    assert resolver.resolve_credential("sess-1", "fetch_firms_active_fire") == "env-key"


def test_session_cache_beats_env(monkeypatch):
    monkeypatch.setenv("TRID3NT_FIRMS_MAP_KEY", "env-key")
    resolver.set_session_credential("sess-1", "firms", "cache-key")
    assert resolver.resolve_credential("sess-1", "fetch_firms_active_fire") == "cache-key"


def test_shared_cds_credential_resolves_for_both_tools(monkeypatch):
    monkeypatch.setenv("TRID3NT_COPERNICUS_CDS_API_KEY", "cds-env")
    # ERA5 and GTSM state the SAME credential name, so one key serves both.
    assert resolver.resolve_credential("s", "fetch_era5_reanalysis") == "cds-env"
    assert resolver.resolve_credential("s", "fetch_gtsm_tide_surge") == "cds-env"


def test_one_pushed_key_serves_every_row_naming_it():
    resolver.set_session_credential("s", "ecmwf_cds", "pushed")
    assert resolver.resolve_credential("s", "fetch_era5_reanalysis") == "pushed"
    assert resolver.resolve_credential("s", "fetch_gtsm_tide_surge") == "pushed"


def test_keyed_credentials_are_the_rows_that_declare_one():
    creds = resolver.keyed_credentials()
    assert set(creds) == {"firms", "ecmwf_cds", "openaq", "airnow"}
    assert creds["airnow"].env_var == "TRID3NT_AIRNOW_API_KEY"
    assert creds["firms"].signup_url is not None


def test_credential_for_public_tool_is_none():
    assert resolver.credential_for_tool("fetch_usgs_water_gauges") is None


def test_refusal_names_the_credential_and_the_form():
    credential = resolver.credential_for_tool("fetch_firms_active_fire")
    err = resolver.MissingCredentialError(credential)
    assert err.credential_name == "firms"
    assert err.actionability == "user"
    assert err.retryable is False
    assert "Settings -> Keys" in str(err)
    assert credential.signup_url in str(err)


def test_non_keyed_tool_resolves_none():
    assert resolver.resolve_credential("sess-1", "fetch_usgs_water_gauges") is None


def test_no_source_resolves_none(monkeypatch):
    monkeypatch.delenv("TRID3NT_FIRMS_MAP_KEY", raising=False)
    assert resolver.resolve_credential("sess-1", "fetch_firms_active_fire") is None




def test_clear_session_drops_entries():
    resolver.set_session_credential("sess-1", "firms", "k")
    resolver.clear_session("sess-1")
    assert resolver.session_provider_ids("sess-1") == frozenset()


def test_blank_inputs_are_ignored():
    resolver.set_session_credential("", "firms", "k")
    resolver.set_session_credential("sess-1", "", "k")
    resolver.set_session_credential("sess-1", "firms", "")
    assert resolver.session_provider_ids("sess-1") == frozenset()


def test_sessions_isolated():
    resolver.set_session_credential("sess-1", "firms", "k1")
    assert resolver.resolve_credential("sess-2", "fetch_firms_active_fire") is None




class _NullWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):  # pragma: no cover - success path sends nothing
        self.sent.append(raw)


def test_handle_secret_add_writes_session_cache():
    from trid3nt_server.server import SessionState, _handle_secret_add
    from trid3nt_contracts.common import new_ulid

    ws = _NullWebSocket()
    state = SessionState(session_id=new_ulid())
    env = SecretAddEnvelopePayload(provider="firms", key_value="pushed-key", case_id=None)

    asyncio.run(_handle_secret_add(ws, state, env))

    assert resolver.resolve_credential(state.session_id, "fetch_firms_active_fire") == "pushed-key"
    # Success path emits no reply (no secrets-list, no vault write).
    assert ws.sent == []
