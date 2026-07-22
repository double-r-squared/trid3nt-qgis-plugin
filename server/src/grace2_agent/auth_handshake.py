"""AWS Cognito ID-token verification + Persistence wiring for WS connect.

Implements the agent-side of the H.5 session-validation handshake. The
GCP→AWS migration swapped the identity provider from Firebase / GCP Identity
Platform to AWS Cognito (email/password via the Hosted UI OIDC code flow).
The handshake *orchestration* is unchanged because it was always provider-
agnostic at the seam: this module produces a claims dict keyed on ``uid``,
``authenticate_token`` reads ``claims["uid"]`` and stores it as
``User.firebase_uid`` (a provider-agnostic IdP-sub carrier, NOT renamed to
avoid a zero-benefit migration of the file-backed user store), and the
canonical owner identity remains the internal ULID minted by ``new_ulid``
(Decision 10). The Cognito ``sub`` slots in exactly where the Firebase ``uid``
went.

1. On WebSocket connect, the client sends an ``auth-token`` envelope carrying
   its Cognito **ID token** (Appendix H.5). The web client sends the ID token
   (not the access token) — only the ID token carries ``email`` / ``name``
   and ``token_use == "id"``.
2. This module verifies the token against the Cognito user pool's public JWKS
   (``cognito_verify``): RS256 signature against the matching ``kid``, plus
   ``iss`` / ``aud`` (the app client id) / ``token_use == "id"`` / ``exp``
   claim validation. On success it resolves the Cognito ``sub`` to the
   corresponding ``User`` via the FR-MP-1 Persistence interface, auto-
   provisioning on first authenticated connect (H.5 step 3).
3. If verification fails OR no token arrives within the handshake window, this
   module provisions an **ephemeral anonymous user** (H.3 anonymous-fallback
   path) — anonymous users have a stable ``user_id`` ULID, no
   ``firebase_uid``, and ``is_active=True``. Cases they create flow through
   the normal ``owner_user_id`` ownership rule (H.2).

The verifier is **gated behind ``GRACE2_COGNITO_USER_POOL_ID``**: when the
pool id is unset (every current dev/demo session — AUTH_REQUIRED is OFF by
default), the verifier returns ``None`` for every token, mirroring the old
"Firebase not initialized" path → anonymous fallback. This keeps the live
demo unaffected until the orchestrator injects the Cognito env + flips
``AUTH_REQUIRED``.

The module is **transport-agnostic** — it does not touch the WebSocket
itself; ``server.py`` reads / writes envelopes and calls the functions here
for the verification + provisioning logic. This keeps the handshake testable
without standing up a real socket and makes mocking trivial.

JWKS prefetch happens once at agent startup (``main.py`` via
``init_firebase_admin`` — name retained for the call site; it now drives the
Cognito init). It is best-effort + log-only — the JWKS is a public HTTPS
endpoint (no creds), and a cold-start network hiccup just drops the first
connect to anonymous (with the gate OFF this is invisible; the cached JWKS
warms on the next verify).

Invariants this module is responsible for:

- **Decision F (wire isolation).** The raw token never persists. It is
  consumed by ``cognito_verify`` and discarded; only ``firebase_uid`` /
  ``user_id`` / ``tier`` survive past this module.
- **Decision 10 (canonical id).** The owner id is the internal ULID minted by
  ``new_ulid``; the Cognito ``sub`` is only a lookup key stored on
  ``User.firebase_uid``. Under the gate there is NO raw-sub fall-through —
  the sub is always resolved to a ``User`` via Persistence.
- **Invariant 9 (no cost theater).** No cost / quota / spend surfaces.
- **MCP canonical persistence (job-0115).** All ``UserDocument`` CRUD goes
  through ``Persistence.get_user_by_firebase_uid`` /
  ``Persistence.upsert_user``. No direct PyMongo driver.

SRS references:

- Appendix H.5 — session validation (this is the agent-side implementation).
- Appendix H.3 — anonymous fallback (token-arrival timeout).
- Appendix H.4 — tier claim resolution (``free`` default).
- FR-AS-5 — WebSocket server speaks Appendix A (the handshake is now part of
  A.5 Connection Lifecycle).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from grace2_contracts.auth import AuthAckEnvelope, AuthTokenEnvelope, TierClaim
from grace2_contracts.common import new_ulid, now_utc
from grace2_contracts.user import User

from .persistence import Persistence

logger = logging.getLogger("grace2_agent.auth_handshake")

#: Default time the agent waits for ``auth-token`` before falling through to
#: the anonymous-fallback path (H.3). Override via env for ops flexibility.
DEFAULT_AUTH_TOKEN_TIMEOUT_S: float = float(
    os.environ.get("GRACE2_AUTH_TOKEN_TIMEOUT_S", "5.0")
)

# --------------------------------------------------------------------------- #
# TRID3NT local build: ONE fixed local user (F1, live-feedback 2026-07-09)
# --------------------------------------------------------------------------- #

#: The single fixed user every connection resolves to in local mode
#: (``GRACE2_SOLVER_BACKEND=local-docker`` / FilePersistence). A constant,
#: ULID-shaped id ("L0CA1 VSER" in Crockford base32 -- L/O/U are not in the
#: alphabet, hence 1/0/V) so the desktop browser, phone, QGIS plugin, and
#: Playwright all land on the SAME case list regardless of the per-client
#: ``anonymous_user_id`` hint. Never used on the cloud path.
LOCAL_SINGLE_USER_ID = "0110CA1VSERAAAAAAAAAAAAAAA"

#: Once-per-process guard for the stray-case adoption sweep (cheap update-many
#: on the file substrate, but no reason to re-run it on every reconnect).
_local_case_adoption_done = False

# --------------------------------------------------------------------------- #
# Cognito configuration — read at call time so the orchestrator can inject the
# env via the EC2 deploy without re-import, and so dev (no env) stays anonymous.
# --------------------------------------------------------------------------- #

#: Env: the Cognito user pool id, e.g. ``us-west-2_AbCdEf123``. UNSET ⇒ the
#: verifier returns None for every token (anonymous fallback; live demo
#: unaffected). This is the master gate for Cognito verification.
COGNITO_POOL_ENV = "GRACE2_COGNITO_USER_POOL_ID"
#: Env: the SPA app client id — Cognito ID tokens carry this in ``aud``.
COGNITO_CLIENT_ENV = "GRACE2_COGNITO_CLIENT_ID"
#: Env: region for the JWKS / issuer URL. Falls back to AWS_REGION, then
#: us-west-2 (the migration's home region per bedrock_adapter.py).
COGNITO_REGION_ENV = "GRACE2_AWS_REGION"

#: Clock-skew leeway (seconds) for ``exp`` / ``nbf`` / ``iat`` validation.
_JWT_LEEWAY_S = 60

#: HTTPS timeout (seconds) for the public JWKS fetch.
_JWKS_FETCH_TIMEOUT_S = 5.0


def _cognito_region() -> str:
    """Resolve the Cognito region (call-time env read)."""
    return (
        os.environ.get(COGNITO_REGION_ENV)
        or os.environ.get("AWS_REGION")
        or "us-west-2"
    )


def _cognito_issuer(region: str, pool_id: str) -> str:
    """Build the canonical Cognito issuer URL (also the JWKS base)."""
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"


# --------------------------------------------------------------------------- #
# JWKS cache — module-level, keyed by issuer. Public endpoint, no creds.
# --------------------------------------------------------------------------- #

#: Maps issuer URL → {kid: jwk_dict}. Populated lazily on first verify (or
#: prefetched at startup). A single re-fetch absorbs key rotation when an
#: unknown ``kid`` is seen.
_jwks_cache: dict[str, dict[str, dict[str, Any]]] = {}
_jwks_lock = threading.Lock()


def _fetch_jwks(issuer: str) -> dict[str, dict[str, Any]]:
    """Fetch + parse the pool JWKS into a ``{kid: jwk}`` map.

    Public HTTPS endpoint — no AWS creds required. Raises on network / parse
    failure; callers treat any exception as "verification not possible" →
    anonymous fallback.
    """
    import requests  # local import: keep module import cheap + dep optional

    url = f"{issuer}/.well-known/jwks.json"
    resp = requests.get(url, timeout=_JWKS_FETCH_TIMEOUT_S)
    resp.raise_for_status()
    keys = resp.json().get("keys", [])
    return {k["kid"]: k for k in keys if "kid" in k}


def _get_jwk(issuer: str, kid: str, *, allow_refetch: bool = True) -> dict[str, Any] | None:
    """Return the JWK for ``kid`` from the cache, refetching once on a miss.

    The single re-fetch on an unknown ``kid`` absorbs Cognito key rotation
    without hammering the endpoint on every bad token.
    """
    with _jwks_lock:
        cached = _jwks_cache.get(issuer)
    if cached is not None and kid in cached:
        return cached[kid]
    if not allow_refetch:
        return cached.get(kid) if cached else None
    # Cache miss (cold or rotated key) — fetch once.
    try:
        fresh = _fetch_jwks(issuer)
    except Exception as exc:  # noqa: BLE001 — network/parse failure is normal
        logger.info("JWKS fetch failed for %s: %s", issuer, type(exc).__name__)
        return cached.get(kid) if cached else None
    with _jwks_lock:
        _jwks_cache[issuer] = fresh
    return fresh.get(kid)


def init_firebase_admin() -> bool:
    """Initialize Cognito verification (JWKS prefetch). Name kept for caller.

    Returns True if a Cognito user pool is configured AND its JWKS prefetch
    succeeded, False otherwise (no pool configured, or the prefetch failed).
    The return value is informational only — the agent serves regardless, and
    the verifier lazily (re)fetches the JWKS on the first real verify.

    Best-effort + log-only: there is no GCP ADC dependency anymore, so the
    agent boots on EC2 with no Google creds. When ``GRACE2_COGNITO_USER_POOL_ID``
    is unset (dev/demo default), this is a no-op returning False and every
    connect falls through to the anonymous path (H.3) — exactly the old
    "firebase_admin not installed" posture.
    """
    pool_id = os.environ.get(COGNITO_POOL_ENV, "").strip()
    if not pool_id:
        logger.info(
            "Cognito user pool not configured (%s unset); anonymous-fallback "
            "only (set %s to enable token verification)",
            COGNITO_POOL_ENV,
            COGNITO_POOL_ENV,
        )
        return False
    region = _cognito_region()
    issuer = _cognito_issuer(region, pool_id)
    try:
        jwks = _fetch_jwks(issuer)
    except Exception as exc:  # noqa: BLE001 — startup must not abort
        logger.warning(
            "Cognito JWKS prefetch failed for %s (%s); will retry lazily on "
            "first verify",
            issuer,
            exc,
        )
        return False
    with _jwks_lock:
        _jwks_cache[issuer] = jwks
    logger.info(
        "Cognito verification initialized: issuer=%s keys=%d", issuer, len(jwks)
    )
    return True


def cognito_verify(token: str) -> dict[str, Any] | None:
    """Verify a Cognito ID token against the pool JWKS.

    Returns a claims dict keyed on ``uid`` (the Cognito ``sub``) on success,
    ``None`` on any failure. ``None`` is a normal path (anonymous fallback),
    logged at INFO.

    Verification steps (fail-closed — any failure returns ``None``):
    1. ``GRACE2_COGNITO_USER_POOL_ID`` must be set (else None → anonymous).
    2. Decode the JWT header; fetch the matching JWK by ``kid`` (single
       re-fetch on a cache miss for key rotation).
    3. Verify the RS256 signature + ``exp`` (with leeway) against the JWK,
       requiring ``aud`` == the app client id and ``iss`` == the pool issuer.
    4. Require ``token_use == "id"`` (reject access tokens — they carry no
       email/name and a different audience claim).

    On success returns::

        {"uid": sub, "email": ..., "name": ..., "tier": ...}

    so ``authenticate_token`` (which reads ``claims["uid"]`` / email / name)
    and the ``User.firebase_uid`` lookup stay byte-identical to the Firebase
    era — the Cognito ``sub`` becomes the stored ``firebase_uid`` lookup key.
    """
    pool_id = os.environ.get(COGNITO_POOL_ENV, "").strip()
    if not pool_id:
        # Master gate: no pool configured → anonymous fallback. Mirrors the old
        # "_FIREBASE_INITIALIZED is False" early-return.
        return None
    client_id = os.environ.get(COGNITO_CLIENT_ENV, "").strip()
    region = _cognito_region()
    issuer = _cognito_issuer(region, pool_id)

    try:
        import jwt  # PyJWT[crypto] — installed in the agent venv
        from jwt.algorithms import RSAAlgorithm

        # 2. Header → kid. ``get_unverified_header`` does NOT validate the
        #    signature; we use it only to select the right public key.
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            logger.info("Cognito verify: token header missing 'kid'")
            return None
        jwk = _get_jwk(issuer, kid)
        if jwk is None:
            logger.info("Cognito verify: no JWK for kid=%s (issuer=%s)", kid, issuer)
            return None

        public_key = RSAAlgorithm.from_jwk(jwk)

        # 3. Verify signature + iss + exp. We validate ``aud`` ourselves below
        #    only when a client id is configured, so a misconfigured (empty)
        #    client id fails closed rather than silently accepting any aud.
        decode_kwargs: dict[str, Any] = dict(
            algorithms=["RS256"],
            issuer=issuer,
            leeway=_JWT_LEEWAY_S,
            options={
                "require": ["exp", "iss", "sub"],
                "verify_aud": False,  # validated explicitly below
            },
        )
        claims = jwt.decode(token, public_key, **decode_kwargs)
    except Exception as exc:  # noqa: BLE001 — verification failure is normal
        logger.info("Cognito verify failed: %s", type(exc).__name__)
        return None

    # 4. token_use must be 'id' — reject access tokens.
    if claims.get("token_use") != "id":
        logger.info(
            "Cognito verify: token_use=%r (expected 'id'); rejecting",
            claims.get("token_use"),
        )
        return None

    # aud must equal the configured app client id. Fail closed when the client
    # id is unset (misconfiguration) rather than accept any audience.
    if not client_id or claims.get("aud") != client_id:
        logger.info(
            "Cognito verify: aud mismatch (token aud=%r, expected configured "
            "client id)",
            claims.get("aud"),
        )
        return None

    sub = claims.get("sub")
    if not sub:
        logger.info("Cognito verify: claims missing 'sub'; rejecting")
        return None

    return {
        "uid": sub,
        "email": claims.get("email"),
        "name": claims.get("name") or claims.get("cognito:username"),
        "tier": claims.get("custom:tier", "free"),
    }


def _verify_id_token_sync(token: str) -> dict[str, Any] | None:
    """Default production verifier — delegates to ``cognito_verify``.

    Kept as a distinct name so ``set_verify_hook(None)`` restores the real
    Cognito verifier (the 11 handshake tests inject lambdas returning
    ``{"uid": ...}`` and restore this default between tests). Returns the
    decoded claims dict on success, ``None`` on any failure (invalid /
    expired / wrong-aud token, or no pool configured).
    """
    return cognito_verify(token)


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


#: Hook for tests: replace ``_verify_id_token_sync`` with a fake. The
#: signature is ``(token: str) -> dict | None``. Tests set this to a lambda
#: that returns a fixed claims dict; production keeps the default.
_verify_id_token_hook: Callable[[str], dict[str, Any] | None] = _verify_id_token_sync


def set_verify_hook(hook: Callable[[str], dict[str, Any] | None]) -> None:
    """Replace the token-verification hook (test seam).

    The hook signature matches the synchronous Firebase Admin call. Pass
    ``None`` to restore the default (real Firebase verification).
    """
    global _verify_id_token_hook
    _verify_id_token_hook = hook or _verify_id_token_sync


@dataclass
class AuthResult:
    """Outcome of the connect handshake.

    Fields:
    - ``user`` — the resolved ``User`` (always populated; anonymous users get
      a fresh ephemeral User with ``firebase_uid=None``).
    - ``firebase_uid`` — the verified Firebase UID, or ``None`` on anonymous
      fallback.
    - ``is_anonymous`` — True if the user is anonymous (no Firebase
      verification).
    - ``tier`` — the H.4 tier capability claim. Default ``"free"``.
    """

    user: User
    firebase_uid: str | None
    is_anonymous: bool
    tier: TierClaim


async def authenticate_token(
    token_envelope: AuthTokenEnvelope | None,
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve an ``AuthTokenEnvelope`` to a concrete ``User`` (H.5 + H.3).

    Branches:

    1. **Valid token + verification succeeds.** Resolve ``firebase_uid`` to
       a ``UserDocument`` via ``Persistence.get_user_by_firebase_uid``. If
       no user exists, auto-provision via ``Persistence.upsert_user`` (H.5
       step 3). Tier defaults to ``"free"`` if no ``tier`` claim is present
       on the JWT.

    2. **Missing / empty / invalid token, anonymous hint provided.** When
       the envelope carries ``anonymous_user_id`` and the lookup finds an
       existing ``UserDocument`` with ``is_anonymous=True``, re-bind the
       same User (job-0172 Part C). This is the sticky anonymous path that
       prevents page-refresh from minting a fresh user every reconnect —
       the persisted Cases stay reachable across browser reloads.

    3. **Missing / empty / invalid token, no usable hint.** Provision an
       ephemeral anonymous user with a fresh ULID, ``firebase_uid=None``,
       ``is_active=True``, ``is_anonymous=True``. If persistence is
       provisioned, the user is upserted; otherwise the anonymous user
       stays in-memory for the session (M1 substrate path).

    Always returns an ``AuthResult`` — verification failure is a path, not
    an exception.

    **job-0252b — gate-ordering hygiene.** When the ``AUTH_REQUIRED`` gate
    is engaged (``grace2_agent.auth.auth_required()``), every anonymous
    *resolution* on this function's failure paths returns an **unprovisioned**
    anonymous ``AuthResult`` — NO write to the users collection (and no
    sticky-reuse read). The server gate (``server._handle_auth_token`` /
    ``_ensure_auth_handshake``) inspects ``result.is_anonymous`` and rejects
    the socket (A.5 4401 + A.6 ``AUTH_FAILED``) WITHOUT ever persisting a
    junk anonymous row. Provisioning a row only to reject the connection a
    moment later is unbounded junk-row growth + write amplification under
    hostile/bot load. When the gate is OFF (dev/demo), behavior is byte-
    identical to before this change: anonymous provisioning, sticky-anon
    reuse, and the auth-ack all run exactly as the Wave 2 handshake did.
    """
    # When the production sign-in gate is engaged, an anonymous resolution is
    # destined for rejection by the server gate — so do NOT provision/persist
    # (or even read for sticky-reuse). We hand back an unprovisioned anonymous
    # AuthResult; the server reads is_anonymous and closes 4401. Read at call
    # time per ``auth.auth_required`` so dev (no env) is untouched.
    from .auth import auth_required  # local import: avoid an import cycle.

    gate_on = auth_required()

    # F1 (2026-07-09): TRID3NT local build -- ONE fixed local user. Every
    # connection (any anonymous_user_id hint, any token) resolves to the same
    # ``LOCAL_SINGLE_USER_ID`` so all clients share one case list. Guarded on
    # the local-docker seam AND on the gate being OFF (if someone ever arms
    # AUTH_REQUIRED on a local box, the sign-in gate wins). Cloud path:
    # ``solver_backend()`` returns ``aws-batch``, branch never taken.
    if not gate_on and _is_local_single_user_mode():
        return await _resolve_local_single_user(persistence)

    # 1. Anonymous fallback: no envelope, empty token, or no Cognito pool.
    token_str = (token_envelope.token if token_envelope else "").strip()
    if not token_str:
        # Gate ON: short-circuit BEFORE the sticky-reuse read and BEFORE any
        # provisioning write — zero collection access on the rejected path.
        if gate_on:
            return await _anonymous_result_no_persist()
        anon_hint = (
            token_envelope.anonymous_user_id if token_envelope else None
        )
        if anon_hint is None:
            # No client hint at all -> mint a fresh random ULID (server-side
            # session registry handles dual-socket convergence in this window).
            return await _provision_anonymous_user(persistence)
        if persistence is None:
            # No collection to look up / collide with (M1 substrate / CI path).
            # Claim the presented id VERBATIM so this session's sockets still
            # converge on one in-memory anonymous identity.
            return await _provision_anonymous_user(
                persistence, requested_user_id=anon_hint
            )
        existing = await _try_reuse_anonymous_user(persistence, anon_hint)
        if existing is not None:
            logger.info(
                "anonymous reuse: rebound user_id=%s (sticky)", existing.user_id
            )
            return AuthResult(
                user=existing,
                firebase_uid=None,
                is_anonymous=True,
                tier="free",
            )
        # cases-vanish fix: ``_try_reuse_anonymous_user`` returned None for one
        # of three reasons — distinguish them so we PROVISION the id VERBATIM
        # ONLY when it is genuinely free, and MINT FRESH when the id collides
        # with an existing (non-anonymous / inactive) record.
        # ``_anonymous_id_is_claimable`` re-reads and reports True only when NO
        # record exists for the id — so reusing it verbatim can never overwrite
        # / hijack a Firebase-verified or deactivated user.
        if await _anonymous_id_is_claimable(persistence, anon_hint):
            # No record yet (first connect of a fresh client-owned id, or the
            # record never got persisted) -> claim the id VERBATIM. This is the
            # keystone of dual-socket convergence: the web replays one
            # client-owned id from BOTH sockets, so both connections resolve to
            # the SAME ``user_id`` (reuse above, or this same-id provision) and
            # the owner-scoped case-list is stable across reconnects.
            return await _provision_anonymous_user(
                persistence, requested_user_id=anon_hint
            )
        # Id is taken by a non-anonymous / inactive record -> never verbatim;
        # mint a fresh random ULID (the reuse helper already logged why).
        return await _provision_anonymous_user(persistence)

    # 2. Verify the token.
    claims = _verify_id_token_hook(token_str)
    if not claims:
        logger.info("auth-token verification failed; falling back to anonymous")
        if gate_on:
            return await _anonymous_result_no_persist()
        return await _provision_anonymous_user(persistence)

    firebase_uid = claims.get("uid")
    if not firebase_uid:
        # JWT decoded but uid missing — treat as anonymous.
        logger.warning("verified claims missing 'uid' field; anonymous fallback")
        if gate_on:
            return await _anonymous_result_no_persist()
        return await _provision_anonymous_user(persistence)

    # H.4 tier resolution. Default "free" when no claim is present.
    tier_claim = claims.get("tier", "free")
    if tier_claim not in ("free", "pro", "enterprise"):
        # Unknown claim — fall back to free (H.4 v0.1 default).
        logger.warning(
            "unknown tier claim %r; defaulting to 'free'", tier_claim
        )
        tier_claim = "free"

    # 3. Resolve to a UserDocument via Persistence (job-0115 substrate).
    user = await _resolve_or_provision_user(
        persistence,
        firebase_uid=firebase_uid,
        email=claims.get("email"),
        display_name=claims.get("name") or claims.get("display_name"),
    )

    return AuthResult(
        user=user,
        firebase_uid=firebase_uid,
        is_anonymous=False,
        tier=tier_claim,
    )


def _is_local_single_user_mode() -> bool:
    """True when auth must collapse to the ONE fixed local user (F1).

    The canonical is-local seam (same one ``secrets_handler`` and
    ``server._local_compute_lane`` use): ``GRACE2_SOLVER_BACKEND=local-docker``
    -> ``tools.solver.solver_backend()`` returns ``local-docker``. The TRID3NT
    local build pins it; the cloud stack never sets it, so the multi-user
    cloud path stays byte-identical. Read at call time so a test env
    injection takes effect without re-import.
    """
    from .tools.solver import SOLVER_BACKEND_LOCAL_DOCKER, solver_backend

    return solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER


async def _resolve_local_single_user(
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve EVERY local-mode connection to ``LOCAL_SINGLE_USER_ID`` (F1).

    NATE 2026-07-09: "persistent cases ... I want to accumulate test cases".
    Pre-fix, each client (desktop browser, phone, QGIS plugin, Playwright)
    presented its own sticky ``anonymous_user_id``, so every device forked its
    own owner-scoped case list (log 01:23:14: "hint ... not found; minting
    fresh" -> case-list count=0 on a box full of cases). In local mode there
    is exactly one human, so all connections collapse onto one fixed user:

    - reuse the persisted local-user record when it exists (stable
      ``created_at`` / prefs -- no re-upsert churn per reconnect);
    - else provision it verbatim via the sticky-anonymous path;
    - ``is_anonymous`` stays True so the auth-ack keeps clients' sticky
      logic working unchanged;
    - one adoption sweep per process re-owns every stray case (minted by the
      old per-client anonymous users) to the local user, so the accumulated
      test cases all show up everywhere.
    """
    global _local_case_adoption_done
    result: AuthResult | None = None
    if persistence is not None:
        existing = await _try_reuse_anonymous_user(
            persistence, LOCAL_SINGLE_USER_ID
        )
        if existing is not None:
            result = AuthResult(
                user=existing,
                firebase_uid=None,
                is_anonymous=True,
                tier="free",
            )
    if result is None:
        result = await _provision_anonymous_user(
            persistence, requested_user_id=LOCAL_SINGLE_USER_ID
        )
    if persistence is not None and not _local_case_adoption_done:
        _local_case_adoption_done = True
        try:
            adopted = await persistence.adopt_cases_to_user(
                LOCAL_SINGLE_USER_ID
            )
            if adopted:
                logger.info(
                    "local single-user: adopted %d stray case(s) onto %s",
                    adopted,
                    LOCAL_SINGLE_USER_ID,
                )
        except Exception as exc:  # noqa: BLE001 -- adoption is best-effort
            logger.warning(
                "local single-user case adoption failed (non-fatal): %s", exc
            )
    return result


async def _provision_anonymous_user(
    persistence: Persistence | None,
    *,
    requested_user_id: str | None = None,
) -> AuthResult:
    """Provision an ephemeral anonymous ``User`` per H.3 fallback.

    The anonymous User has:
    - a ULID ``user_id`` — the client-presented ``requested_user_id`` when one
      was supplied (cases-vanish fix), else a fresh ULID,
    - ``firebase_uid=None`` (no Firebase binding),
    - ``email=None`` / ``display_name=None``,
    - ``is_active=True`` (Cases CAN be created; web prompts upgrade at save),
    - default ``prefs={}``.

    If persistence is provisioned, the user is upserted so the
    ``owner_user_id`` cascade rule (H.2) has a stable target. If
    persistence is unbound (no MCP env), the anonymous user lives in-memory
    only — the M1 substrate path keeps working.

    cases-vanish fix: when the client presents a stable ``anonymous_user_id``
    that has no record yet, the caller passes it as ``requested_user_id`` so the
    User is provisioned with THAT EXACT id. This is what lets two sockets of one
    browser session (App + Chat, both replaying the same client-owned id)
    converge on ONE anonymous identity — whichever socket wins the provision
    race writes the deterministic id, and the loser then *reuses* it. Without
    this, each connection minted a random ULID and the owner-scoped case-list
    forked, so Cases appeared to vanish on refresh. The id is ULID-validated
    upstream (``AuthTokenEnvelope.anonymous_user_id: ULIDStr``) and, on the
    persistence-backed path, only reaches here as ``requested_user_id`` after
    ``_anonymous_id_is_claimable`` confirmed NO record exists for it — so the
    verbatim ``upsert_user`` can never overwrite / hijack a Firebase-verified
    or deactivated user. On the no-persistence path (CI / M1 substrate) the id
    is claimed verbatim too, but there is no collection to collide with.

    Returns ``AuthResult`` with ``is_anonymous=True``, ``tier="free"``.
    """
    user = User(
        user_id=requested_user_id or new_ulid(),
        firebase_uid=None,
        email=None,
        display_name=None,
        created_at=now_utc(),
        is_active=True,
        prefs={},
        is_anonymous=True,  # job-0172 Part C: pin the H.3 fallback as anonymous.
    )
    if persistence is not None:
        try:
            await persistence.upsert_user(user)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "anonymous user upsert failed (continuing in-memory): %s", exc
            )
    return AuthResult(
        user=user,
        firebase_uid=None,
        is_anonymous=True,
        tier="free",
    )


async def _anonymous_result_no_persist() -> AuthResult:
    """Build an anonymous ``AuthResult`` WITHOUT touching any collection.

    job-0252b: the ``AUTH_REQUIRED`` gate-rejected path. The server inspects
    ``is_anonymous`` and closes the socket (A.5 4401 + A.6 ``AUTH_FAILED``)
    immediately — there is no point provisioning a users row that is never
    bound to a session, and doing so amplifies writes / grows junk rows under
    hostile connection load. So the ephemeral User stays purely in-memory: a
    fresh ULID, ``firebase_uid=None``, never persisted.

    This is exactly ``_provision_anonymous_user(None)`` (the unbound-
    persistence branch), but expressed as its own intent-named helper so the
    "no write on the rejected path" property is explicit at the call sites
    and cannot regress to passing a live ``persistence`` by accident.

    The function is ``async`` to keep the call sites uniform with
    ``_provision_anonymous_user`` even though it never awaits.
    """
    return await _provision_anonymous_user(None)


async def _try_reuse_anonymous_user(
    persistence: Persistence,
    anonymous_user_id: str,
) -> User | None:
    """Look up the User by ULID and reuse iff it's an anonymous record.

    job-0172 Part C: the sticky-anonymous path. Returns the existing User
    only when (a) a record exists for ``anonymous_user_id`` and (b) that
    record is marked ``is_anonymous=True``. Returns ``None`` to fall
    through to fresh-user provisioning when either condition fails.

    Why the is_anonymous gate: an attacker could fish a known authenticated
    User id from a log and replay it; we MUST NOT re-bind a Firebase-verified
    User without the actual JWT. Anonymous Users have no credential, so
    re-binding them is the entire point — the id IS the only identifier
    they ever had.
    """
    try:
        existing = await persistence.get_user_by_id(anonymous_user_id)
    except Exception as exc:  # noqa: BLE001 — best-effort: fall back to fresh
        logger.warning(
            "anonymous reuse: get_user_by_id(%s) failed (%s); minting fresh",
            anonymous_user_id,
            exc,
        )
        return None
    if existing is None:
        logger.info(
            "anonymous reuse: hint %s not found; minting fresh", anonymous_user_id
        )
        return None
    if not existing.is_anonymous:
        # Forbid re-binding to a Firebase-verified record without the JWT.
        logger.warning(
            "anonymous reuse: hint %s belongs to a non-anonymous user; "
            "rejecting (minting fresh anonymous)",
            anonymous_user_id,
        )
        return None
    if not existing.is_active:
        logger.info(
            "anonymous reuse: hint %s is_active=False; minting fresh",
            anonymous_user_id,
        )
        return None
    return existing


async def _anonymous_id_is_claimable(
    persistence: Persistence,
    anonymous_user_id: str,
) -> bool:
    """Return True iff NO user record exists for ``anonymous_user_id``.

    cases-vanish fix: the verbatim-provision gate. ``_try_reuse_anonymous_user``
    returns ``None`` for three different reasons — id-not-found, id-belongs-to-
    a-non-anonymous-record, or id-belongs-to-an-inactive-record. We may only
    claim a presented id VERBATIM in the first case; claiming verbatim in the
    other two would overwrite (hijack) an existing Firebase-verified or
    deactivated user's ``_id`` on the next ``upsert_user``. This helper isolates
    "the id is genuinely free" so the caller can branch safely.

    Fail-closed: any lookup error returns ``False`` (treat the id as taken),
    so a transient persistence hiccup mints a fresh ULID rather than risk an
    overwrite. The brief extra read is on the (rare) first-connect-of-a-new-id
    path only; the common sticky-reuse path already returned above.
    """
    try:
        existing = await persistence.get_user_by_id(anonymous_user_id)
    except Exception as exc:  # noqa: BLE001 — fail closed: treat as taken
        logger.warning(
            "anonymous claim-check: get_user_by_id(%s) failed (%s); "
            "minting fresh (not claiming verbatim)",
            anonymous_user_id,
            exc,
        )
        return False
    return existing is None


async def _resolve_or_provision_user(
    persistence: Persistence | None,
    *,
    firebase_uid: str,
    email: str | None,
    display_name: str | None,
) -> User:
    """Look up the User by ``firebase_uid``, auto-create on first connect.

    H.5 step 3: if no ``UserDocument`` exists for the ``uid``, the resolver
    creates one with default fields. Idempotent — a second connect with the
    same uid returns the existing User.

    If persistence is unbound (no MCP env), returns a fresh in-memory User
    so the session-bind path keeps working — the M1 substrate fallback.
    """
    if persistence is None:
        # No persistence — keep an in-memory User so server.py can still
        # bind a session. This is the local-dev / CI path.
        return User(
            user_id=new_ulid(),
            firebase_uid=firebase_uid,
            email=email,
            display_name=display_name,
            created_at=now_utc(),
            is_active=True,
            prefs={},
        )

    existing = await persistence.get_user_by_firebase_uid(firebase_uid)
    if existing is not None:
        return existing

    # First-login auto-provision.
    new_user = User(
        user_id=new_ulid(),
        firebase_uid=firebase_uid,
        email=email,
        display_name=display_name,
        created_at=now_utc(),
        is_active=True,
        prefs={},
    )
    try:
        await persistence.upsert_user(new_user)
        logger.info(
            "auto-provisioned user user_id=%s firebase_uid=%s",
            new_user.user_id,
            firebase_uid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert_user failed (continuing): %s", exc)
    return new_user


def build_auth_ack(result: AuthResult) -> AuthAckEnvelope:
    """Construct the ``auth-ack`` envelope payload for a resolved AuthResult.

    Mirrors only the fields the H.5 ack surfaces — never the raw token
    (Decision F wire isolation). The web client uses this to drive tier-
    gated UI and the anonymous-upgrade prompt.
    """
    return AuthAckEnvelope(
        user_id=result.user.user_id,
        firebase_uid=result.firebase_uid,
        is_anonymous=result.is_anonymous,
        tier=result.tier,
    )


# --------------------------------------------------------------------------- #
# Timeout helper — public so server.py can use the same default constant.
# --------------------------------------------------------------------------- #


def get_auth_token_timeout_s(default: float | None = None) -> float:
    """Return the configured auth-token-arrival timeout (seconds).

    Used by the server connect-handler to bound how long it waits for the
    client's first ``auth-token`` envelope before flipping into the
    anonymous-fallback path. Tests can stub by setting the env var, or pass
    a tighter ``default`` to short-circuit.
    """
    if default is not None:
        return default
    return DEFAULT_AUTH_TOKEN_TIMEOUT_S


__all__ = [
    "AuthResult",
    "COGNITO_CLIENT_ENV",
    "COGNITO_POOL_ENV",
    "COGNITO_REGION_ENV",
    "DEFAULT_AUTH_TOKEN_TIMEOUT_S",
    "authenticate_token",
    "build_auth_ack",
    "cognito_verify",
    "get_auth_token_timeout_s",
    "init_firebase_admin",
    "set_verify_hook",
]
