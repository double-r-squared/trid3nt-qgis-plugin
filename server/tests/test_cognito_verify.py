"""Unit tests for the AWS Cognito ID-token verifier (GCP→AWS migration).

These tests exercise ``grace2_agent.auth_handshake.cognito_verify`` directly
with a locally-generated RSA keypair + an injected JWKS, so no live Cognito
pool / network is required. They prove the fail-closed contract:

- A valid Cognito-shaped ID token (correct iss / aud / token_use=='id' / live
  exp, signed by the pool key) → ``{"uid": sub, ...}``.
- Wrong issuer, wrong audience (app client id), expired exp, wrong
  ``token_use`` (access token), bad signature (signed by a different key),
  and missing ``sub`` each return ``None`` (anonymous fallback).
- No pool configured (``GRACE2_COGNITO_USER_POOL_ID`` unset) → ``None`` for
  every token (the live-demo-safe default).
- The Cognito ``sub`` maps to the ``uid`` key the existing
  ``authenticate_token`` reads, and the verifier slots in as the default hook
  so ``set_verify_hook(None)`` restores it.
- sub→ULID resolution + missing-user (gate-on) fail-closed via
  ``authenticate_token``.

The JWKS is injected straight into the module cache so ``_fetch_jwks`` is
never called over the network.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa

from grace2_agent import auth_handshake
from grace2_agent.auth_handshake import (
    COGNITO_CLIENT_ENV,
    COGNITO_POOL_ENV,
    COGNITO_REGION_ENV,
    authenticate_token,
    cognito_verify,
    set_verify_hook,
)
from grace2_contracts.auth import AuthTokenEnvelope
from grace2_contracts.common import new_ulid, now_utc
from grace2_contracts.user import User

# --------------------------------------------------------------------------- #
# Fixtures: a local RSA keypair published as a JWK, plus a second "rogue" key.
# --------------------------------------------------------------------------- #

REGION = "us-west-2"
POOL_ID = "us-west-2_TestPool01"
CLIENT_ID = "test-app-client-id-123"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
KID = "test-kid-1"
ROGUE_KID = "rogue-kid-2"


def _make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict:
    """Build a Cognito-shaped public JWK from an RSA key."""
    # PyJWT's RSAAlgorithm can serialize a public key to a JWK string.
    jwk_str = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    jwk = json.loads(jwk_str)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


_POOL_KEY = _make_key()
_ROGUE_KEY = _make_key()


def _sign(private_key: rsa.RSAPrivateKey, kid: str, claims: dict) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _valid_claims(**overrides) -> dict:
    now = int(time.time())
    base = {
        "sub": "cognito-sub-abc-123",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "email": "demo@example.com",
        "name": "Demo User",
        "iat": now - 5,
        "exp": now + 3600,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _cognito_env(monkeypatch):
    """Configure the Cognito env + inject the JWKS into the module cache."""
    monkeypatch.setenv(COGNITO_POOL_ENV, POOL_ID)
    monkeypatch.setenv(COGNITO_CLIENT_ENV, CLIENT_ID)
    monkeypatch.setenv(COGNITO_REGION_ENV, REGION)
    # Inject the public JWK so _fetch_jwks is never hit over the network.
    auth_handshake._jwks_cache[ISSUER] = {KID: _public_jwk(_POOL_KEY, KID)}
    yield
    auth_handshake._jwks_cache.clear()
    set_verify_hook(None)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_valid_id_token_maps_sub_to_uid() -> None:
    token = _sign(_POOL_KEY, KID, _valid_claims())
    claims = cognito_verify(token)
    assert claims is not None
    assert claims["uid"] == "cognito-sub-abc-123"
    assert claims["email"] == "demo@example.com"
    assert claims["name"] == "Demo User"
    assert claims["tier"] == "free"


def test_custom_tier_passes_through() -> None:
    token = _sign(_POOL_KEY, KID, _valid_claims(**{"custom:tier": "pro"}))
    claims = cognito_verify(token)
    assert claims is not None
    assert claims["tier"] == "pro"


def test_cognito_username_used_when_name_absent() -> None:
    c = _valid_claims()
    c.pop("name")
    c["cognito:username"] = "fallback-username"
    token = _sign(_POOL_KEY, KID, c)
    claims = cognito_verify(token)
    assert claims is not None
    assert claims["name"] == "fallback-username"


# --------------------------------------------------------------------------- #
# Fail-closed paths — each returns None (anonymous fallback)
# --------------------------------------------------------------------------- #


def test_wrong_issuer_fails_closed() -> None:
    bad = _valid_claims(iss="https://cognito-idp.us-east-1.amazonaws.com/other_Pool")
    token = _sign(_POOL_KEY, KID, bad)
    assert cognito_verify(token) is None


def test_wrong_audience_fails_closed() -> None:
    token = _sign(_POOL_KEY, KID, _valid_claims(aud="some-other-client"))
    assert cognito_verify(token) is None


def test_expired_token_fails_closed() -> None:
    now = int(time.time())
    token = _sign(_POOL_KEY, KID, _valid_claims(exp=now - 600, iat=now - 1200))
    assert cognito_verify(token) is None


def test_access_token_use_fails_closed() -> None:
    # An access token (token_use='access') must be rejected even if otherwise
    # well-formed — the web client must send the ID token.
    token = _sign(_POOL_KEY, KID, _valid_claims(token_use="access"))
    assert cognito_verify(token) is None


def test_bad_signature_fails_closed() -> None:
    # Signed by the rogue key but advertising the pool's kid → signature check
    # against the pool JWK fails.
    token = _sign(_ROGUE_KEY, KID, _valid_claims())
    assert cognito_verify(token) is None


def test_unknown_kid_fails_closed() -> None:
    # Signed by a key whose kid is not in the JWKS (and the single re-fetch
    # will fail because there is no live endpoint) → None.
    token = _sign(_ROGUE_KEY, ROGUE_KID, _valid_claims())
    assert cognito_verify(token) is None


def test_missing_sub_fails_closed() -> None:
    c = _valid_claims()
    c.pop("sub")
    token = _sign(_POOL_KEY, KID, c)
    # PyJWT's options.require=['sub'] rejects this during decode → None.
    assert cognito_verify(token) is None


def test_garbage_token_fails_closed() -> None:
    assert cognito_verify("not.a.jwt") is None
    assert cognito_verify("") is None


def test_no_pool_configured_returns_none(monkeypatch) -> None:
    # The master gate: unset the pool id → every token is anonymous-fallback.
    monkeypatch.delenv(COGNITO_POOL_ENV, raising=False)
    token = _sign(_POOL_KEY, KID, _valid_claims())
    assert cognito_verify(token) is None


def test_empty_client_id_fails_closed(monkeypatch) -> None:
    # Misconfigured app client id (empty) must NOT accept any audience.
    monkeypatch.setenv(COGNITO_CLIENT_ENV, "")
    token = _sign(_POOL_KEY, KID, _valid_claims())
    assert cognito_verify(token) is None


# --------------------------------------------------------------------------- #
# Default-hook wiring: set_verify_hook(None) restores the Cognito verifier.
# --------------------------------------------------------------------------- #


def test_default_hook_is_cognito_verify() -> None:
    set_verify_hook(None)
    # The default hook should verify a real token end-to-end.
    token = _sign(_POOL_KEY, KID, _valid_claims())
    claims = auth_handshake._verify_id_token_hook(token)
    assert claims is not None
    assert claims["uid"] == "cognito-sub-abc-123"


# --------------------------------------------------------------------------- #
# Integration with authenticate_token: sub→ULID + Decision 10.
# --------------------------------------------------------------------------- #


class _MockMCPClient:
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def call_tool(self, name, arguments=None):
        args = dict(arguments or {})
        coll = args.get("collection") or "_default"
        store = self._store.setdefault(coll, {})
        if name == "insert-one":
            doc = args["document"]
            store[doc["_id"]] = doc
            return {"insertedId": doc["_id"]}
        if name == "update-one":
            filt = args.get("filter", {})
            set_ = args.get("update", {}).get("$set", {})
            upsert = args.get("upsert", False)
            tid = filt.get("_id")
            if tid and tid in store:
                store[tid].update(set_)
            elif upsert and tid:
                store[tid] = {**set_, "_id": tid}
            return {"matchedCount": 1, "modifiedCount": 1}
        if name == "find-one":
            filt = args.get("filter", {})
            for doc in store.values():
                if all(doc.get(k) == v for k, v in filt.items()):
                    return {"document": doc}
            return {"document": None}
        if name == "find":
            filt = args.get("filter", {})
            return {
                "documents": [
                    d
                    for d in store.values()
                    if all(d.get(k) == v for k, v in filt.items())
                ]
            }
        raise RuntimeError(f"unhandled {name}")


@pytest.mark.asyncio
async def test_authenticate_token_resolves_cognito_sub_to_ulid() -> None:
    """A live Cognito token resolves to an internal ULID (Decision 10)."""
    from grace2_agent.persistence import Persistence

    p = Persistence(_MockMCPClient())
    set_verify_hook(None)  # use the real cognito_verify

    token = _sign(_POOL_KEY, KID, _valid_claims())
    result = await authenticate_token(AuthTokenEnvelope(token=token), p)

    assert result.is_anonymous is False
    # The stored lookup key is the Cognito sub...
    assert result.firebase_uid == "cognito-sub-abc-123"
    assert result.user.firebase_uid == "cognito-sub-abc-123"
    # ...but the canonical owner id is an internal ULID, never the raw sub.
    assert result.user.user_id != "cognito-sub-abc-123"
    assert len(result.user.user_id) == 26  # ULID discipline

    # Second connect with the same sub returns the SAME user (no duplicate).
    result2 = await authenticate_token(AuthTokenEnvelope(token=token), p)
    assert result2.user.user_id == result.user.user_id


@pytest.mark.asyncio
async def test_authenticate_token_gate_on_invalid_token_no_persist(monkeypatch) -> None:
    """Gate ON + invalid token → unprovisioned anonymous (fail-closed)."""
    from grace2_agent.persistence import Persistence

    monkeypatch.setenv("AUTH_REQUIRED", "true")
    p = Persistence(_MockMCPClient())
    set_verify_hook(None)

    # Wrong-audience token → cognito_verify returns None → anonymous.
    bad = _sign(_POOL_KEY, KID, _valid_claims(aud="wrong"))
    result = await authenticate_token(AuthTokenEnvelope(token=bad), p)
    assert result.is_anonymous is True
    assert result.firebase_uid is None
    # No user row was written on the rejected path (fail-closed, no junk rows).
    assert await p.get_user_by_firebase_uid("cognito-sub-abc-123") is None
