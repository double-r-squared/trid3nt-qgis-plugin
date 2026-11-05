"""What a provider states machine-readably about a key: the three readers on bodies in the shapes providers publish."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.key_statement import arcgis_states_key, ogc_states_key, openapi_states_key


def test_an_arcgis_token_answer_states_a_key_and_a_service_document_states_open() -> None:
    assert arcgis_states_key(b'{"error":{"code":499,"message":"Token Required"}}') is True
    assert arcgis_states_key(b'{"error":{"code":498,"message":"Invalid token."}}') is True
    assert arcgis_states_key(b'{"currentVersion":11.2,"layers":[]}') is False
    assert arcgis_states_key(b'{"authInfo":{"isTokenBasedSecurity":true,"tokenServicesUrl":"https://x.test/t"}}') is True
    assert arcgis_states_key(b'{"authInfo":{"isTokenBasedSecurity":false}}') is False


def test_an_arcgis_error_that_is_not_a_token_answer_states_nothing() -> None:
    assert arcgis_states_key(b'{"error":{"code":400,"message":"Invalid URL"}}') is None
    assert arcgis_states_key(b"<html>not json</html>") is None
    assert arcgis_states_key(b"[1]") is None


def _openapi(**over) -> bytes:
    doc = {"openapi": "3.1.0", "paths": {"/a": {"get": {}}}, "components": {"securitySchemes": {
        "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}}}
    doc.update(over)
    return json.dumps(doc).encode()


def test_an_openapi_global_security_naming_a_secret_scheme_states_a_key() -> None:
    assert openapi_states_key(_openapi(security=[{"key": []}])) is True


def test_an_empty_or_optional_security_states_open() -> None:
    assert openapi_states_key(_openapi(security=[])) is False
    assert openapi_states_key(_openapi(security=[{"key": []}, {}])) is False


def test_a_user_agent_scheme_is_the_client_naming_itself_not_a_secret() -> None:
    nws = _openapi(security=[{"userAgent": []}, {"apiKeyAuth": []}], components={"securitySchemes": {
        "userAgent": {"type": "apiKey", "in": "header", "name": "User-Agent"},
        "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "API-Key"}}})
    assert openapi_states_key(nws) is False


def test_operations_that_all_require_a_scheme_state_a_key() -> None:
    paths = {"/a": {"get": {"security": [{"key": []}]}}, "/b": {"post": {"security": [{"key": []}]}}}
    assert openapi_states_key(_openapi(paths=paths)) is True
    paths["/b"]["post"] = {}
    assert openapi_states_key(_openapi(paths=paths)) is None


@pytest.mark.parametrize("body", [b"{}", b"not json", b'{"paths": {}}'])
def test_a_body_that_is_no_openapi_document_states_nothing(body) -> None:
    assert openapi_states_key(body) is None


def _caps(constraint: str) -> str:
    return f'<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Service><AccessConstraints>{constraint}</AccessConstraints></Service></WMS_Capabilities>'


def test_ogc_access_constraints_none_states_open_and_a_key_word_states_a_key() -> None:
    assert ogc_states_key(_caps("NONE")) is False
    assert ogc_states_key(_caps("none")) is False
    assert ogc_states_key(_caps("An API key is required")) is True
    assert ogc_states_key(_caps("Use constraints apply, see license")) is None
    assert ogc_states_key("<Capabilities/>") is None
