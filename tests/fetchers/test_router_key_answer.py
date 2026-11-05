"""A provider's answer about a key: the one reader, its typed asks, and the schemas that carry no key."""

from __future__ import annotations

import inspect

import httpx
import pytest

from trid3nt_server.model.credentials import resolver
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._fetch_common import KeyRejectedError, KeyRequiredError
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.key_answer import key_error_from
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers._router.router import route
from trid3nt_server.tools.fetchers._router.transport import TransportAuthError
from trid3nt_server.tools.fetchers._router.transport.client import _body

KEYED = "fetch_airnow_air_quality"
PUBLIC = "fetch_usgs_earthquakes"


def _denied(status: int, body: str) -> TransportAuthError:
    return TransportAuthError(f"access denied (HTTP {status})", status=status, body=body)


def _raised_from(exc: Exception) -> Exception:
    """The exception a router closure raises when a transport error is in its chain."""
    try:
        try:
            raise exc
        except TransportAuthError as inner:
            raise RuntimeError("AIRNOW_UPSTREAM_ERROR") from inner
    except RuntimeError as outer:
        return outer


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)
    resolver._SESSION_CREDENTIALS.clear()
    yield
    resolver._SESSION_CREDENTIALS.clear()


def test_a_declared_row_asks_for_a_key_on_any_401_or_403():
    asked = key_error_from(get_spec(KEYED), _raised_from(_denied(403, "Forbidden")))
    assert isinstance(asked, KeyRequiredError)
    assert (asked.source, asked.credential) == (KEYED, "airnow")


def test_a_stored_key_the_provider_refuses_is_rejected_not_required():
    resolver.set_session_credential("s", "airnow", "stored-test-value")
    token = resolver.bind_session("s")
    try:
        asked = key_error_from(get_spec(KEYED), _raised_from(_denied(401, "bad")))
    finally:
        resolver.unbind_session(token)
    assert isinstance(asked, KeyRejectedError) and asked.error_code == "KEY_REJECTED"
    assert "stored-test-value" not in str(asked)


def test_an_undeclared_row_asks_only_when_the_body_names_a_key_and_names_itself():
    spec = get_spec(PUBLIC)
    asked = key_error_from(spec, _raised_from(_denied(403, '{"message": "Missing API key"}')))
    assert isinstance(asked, KeyRequiredError)
    assert asked.credential == PUBLIC
    assert key_error_from(spec, _raised_from(_denied(403, "Forbidden"))) is None
    assert key_error_from(spec, _raised_from(_denied(403, "<Error><Code>AccessDenied</Code>"))) is None


def test_a_link_is_carried_only_when_the_answer_carries_one():
    spec = get_spec(PUBLIC)
    body = 'API key required, get one at https://example.org/keys?x=1 today'
    assert key_error_from(spec, _raised_from(_denied(401, body))).link == "https://example.org/keys?x=1"
    assert key_error_from(spec, _raised_from(_denied(401, "API key required"))).link is None
    assert "http://" not in str(key_error_from(spec, _raised_from(_denied(401, "API key at http://x.test/k"))))


def test_an_arcgis_token_answer_under_a_200_is_a_key_ask():
    resp = httpx.Response(200, content=b'{"error":{"code":499,"message":"Token Required","details":[]}}',
                          request=httpx.Request("GET", "https://x.test/arcgis/rest/services"))
    with pytest.raises(TransportAuthError) as ei:
        _body(resp, "https://x.test/arcgis/rest/services")
    assert ei.value.status == 499
    assert isinstance(key_error_from(get_spec(PUBLIC), _raised_from(ei.value)), KeyRequiredError)


def test_an_arcgis_error_that_is_not_a_token_answer_is_left_alone():
    resp = httpx.Response(200, content=b'{"error":{"code":400,"message":"Invalid query"}}',
                          request=httpx.Request("GET", "https://x.test/q"))
    assert _body(resp, "https://x.test/q")[0].startswith(b'{"error"')


def test_route_hands_the_ask_out_with_its_cause_kept(monkeypatch):
    cause = _raised_from(_denied(401, "API key invalid"))

    def boom(spec, raw):
        raise cause

    monkeypatch.setattr(router, "_route", boom)
    with pytest.raises(KeyRequiredError) as ei:
        route(get_spec(PUBLIC), {})
    assert ei.value.__cause__ is cause


def test_route_leaves_every_other_failure_as_it_was(monkeypatch):
    monkeypatch.setattr(router, "_route", lambda spec, raw: (_ for _ in ()).throw(ValueError("x")))
    with pytest.raises(ValueError):
        route(get_spec(PUBLIC), {})


def test_no_tool_schema_carries_a_key_shaped_argument():
    shaped = {"api_key", "apikey", "map_key", "secret_ref", "token", "password", "secret"}
    carrying = {name: sorted(shaped & set(inspect.signature(entry.fn).parameters))
                for name, entry in TOOL_REGISTRY.items()
                if shaped & set(inspect.signature(entry.fn).parameters)}
    assert carrying == {}
