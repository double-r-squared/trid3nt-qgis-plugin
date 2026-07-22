"""Local WS connect handshake: anonymous users + the fixed local user.

TRID3NT is a local, single-user product: there is no identity provider and no
token verification. The handshake *orchestration* is unchanged from the H.5
design because it was always provider-agnostic at the seam: this module
resolves every connection to a concrete ``User``, ``server.py`` reads/writes
the envelopes, and the canonical owner identity is an internal ULID
(Decision 10). ``User.firebase_uid`` remains as a provider-agnostic IdP-sub
carrier in the persisted user schema (never populated in the local build; the
field is kept so the file-backed user store needs no migration).

1. On WebSocket connect, the client may send an ``auth-token`` envelope
   (Appendix H.5 shape). In the local build the ``token`` field is ignored --
   there is no verifier -- and only the ``anonymous_user_id`` hint matters.
2. In local single-user mode (``TRID3NT_SOLVER_BACKEND=local-docker``, the
   live default) EVERY connection resolves to the ONE fixed local user
   (``LOCAL_SINGLE_USER_ID``) so the desktop browser, phone, QGIS plugin,
   and test drivers all share one case list.
3. Otherwise the anonymous path (H.3) runs: sticky reuse of a client-presented
   ``anonymous_user_id``, verbatim provisioning when the id is free, or a
   fresh ULID mint. Anonymous users have a stable ``user_id`` ULID, no
   ``firebase_uid``, and ``is_active=True``; Cases they create flow through
   the normal ``owner_user_id`` ownership rule (H.2).

The module is **transport-agnostic** -- it does not touch the WebSocket
itself; ``server.py`` reads / writes envelopes and calls the functions here
for the resolution + provisioning logic. This keeps the handshake testable
without standing up a real socket.

Invariants this module is responsible for:

- **Decision F (wire isolation).** No credential ever persists; the ack
  carries only ``user_id`` / ``is_anonymous`` / ``tier``.
- **Decision 10 (canonical id).** The owner id is the internal ULID minted by
  ``new_ulid`` (or the fixed local-user constant).
- **Canonical persistence.** All user CRUD goes through the ``Persistence``
  interface; no direct driver access.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from trid3nt_contracts.auth import AuthAckEnvelope, AuthTokenEnvelope, TierClaim
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.user import User

from .persistence import Persistence

logger = logging.getLogger("trid3nt_server.auth_handshake")

#: Default time the agent waits for ``auth-token`` before falling through to
#: the anonymous-fallback path (H.3). Override via env for ops flexibility.
DEFAULT_AUTH_TOKEN_TIMEOUT_S: float = float(
    os.environ.get("TRID3NT_AUTH_TOKEN_TIMEOUT_S", "5.0")
)

# --------------------------------------------------------------------------- #
# TRID3NT local build: ONE fixed local user (F1, live-feedback 2026-07-09)
# --------------------------------------------------------------------------- #

#: The single fixed user every connection resolves to in local mode
#: (``TRID3NT_SOLVER_BACKEND=local-docker`` / FilePersistence). A constant,
#: ULID-shaped id ("L0CA1 VSER" in Crockford base32 -- L/O/U are not in the
#: alphabet, hence 1/0/V) so the desktop browser, phone, QGIS plugin, and
#: test drivers all land on the SAME case list regardless of the per-client
#: ``anonymous_user_id`` hint.
LOCAL_SINGLE_USER_ID = "0110CA1VSERAAAAAAAAAAAAAAA"

#: Once-per-process guard for the stray-case adoption sweep (cheap update-many
#: on the file substrate, but no reason to re-run it on every reconnect).
_local_case_adoption_done = False


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


@dataclass
class AuthResult:
    """Outcome of the connect handshake.

    Fields:
    - ``user`` -- the resolved ``User`` (always populated).
    - ``firebase_uid`` -- legacy IdP-sub slot on the ack contract; always
      ``None`` in the local build (no identity provider).
    - ``is_anonymous`` -- True for every locally-resolved user.
    - ``tier`` -- the H.4 tier capability claim. Default ``"free"``.
    """

    user: User
    firebase_uid: str | None
    is_anonymous: bool
    tier: TierClaim


async def authenticate_token(
    token_envelope: AuthTokenEnvelope | None,
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve an ``AuthTokenEnvelope`` to a concrete ``User`` (H.3).

    Branches:

    1. **Local single-user mode** (``TRID3NT_SOLVER_BACKEND=local-docker`` --
       the live local default). Every connection, regardless of token or
       hint, resolves to the ONE fixed local user (F1).

    2. **No / empty token, anonymous hint provided.** When the envelope
       carries ``anonymous_user_id`` and the lookup finds an existing
       ``UserDocument`` with ``is_anonymous=True``, re-bind the same User
       (job-0172 Part C). This is the sticky anonymous path that prevents
       page-refresh from minting a fresh user every reconnect -- the
       persisted Cases stay reachable across browser reloads.

    3. **No / empty token, no usable hint.** Provision an ephemeral
       anonymous user with a fresh ULID, ``firebase_uid=None``,
       ``is_active=True``, ``is_anonymous=True``. If persistence is
       provisioned, the user is upserted; otherwise the anonymous user
       stays in-memory for the session (M1 substrate path).

    4. **Non-empty token.** The local build has no token verifier, so a
       presented token is ignored and a fresh anonymous user is provisioned
       (the same observable behavior as the old
       verification-unavailable fallback).

    Always returns an ``AuthResult`` -- resolution never raises for a bad
    envelope.
    """
    # F1 (2026-07-09): TRID3NT local build -- ONE fixed local user. Every
    # connection (any anonymous_user_id hint, any token) resolves to the same
    # ``LOCAL_SINGLE_USER_ID`` so all clients share one case list. Guarded on
    # the local-docker seam; a non-local backend falls through to the generic
    # anonymous path below.
    if _is_local_single_user_mode():
        return await _resolve_local_single_user(persistence)

    # 1. Anonymous path: no envelope or empty token.
    token_str = (token_envelope.token if token_envelope else "").strip()
    if not token_str:
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
        # of three reasons -- distinguish them so we PROVISION the id VERBATIM
        # ONLY when it is genuinely free, and MINT FRESH when the id collides
        # with an existing (non-anonymous / inactive) record.
        # ``_anonymous_id_is_claimable`` re-reads and reports True only when NO
        # record exists for the id -- so reusing it verbatim can never overwrite
        # / hijack a previously-verified or deactivated user.
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

    # 2. Non-empty token: the local build has no verifier -- ignore it and
    # fall back to a fresh anonymous user (matches the old
    # verification-unavailable behavior byte-for-byte on the wire).
    logger.info(
        "auth-token ignored (local build has no token verification); "
        "falling back to anonymous"
    )
    return await _provision_anonymous_user(persistence)


def _is_local_single_user_mode() -> bool:
    """True when auth must collapse to the ONE fixed local user (F1).

    The canonical is-local seam (same one ``secrets_handler`` and
    ``server._local_compute_lane`` use): ``TRID3NT_SOLVER_BACKEND=local-docker``
    -> ``tools.simulation.solver.solver_backend()`` returns ``local-docker``. The TRID3NT
    local build pins it. Read at call time so a test env injection takes
    effect without re-import.
    """
    from .tools.simulation.solver import SOLVER_BACKEND_LOCAL_DOCKER, solver_backend

    return solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER


async def _resolve_local_single_user(
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve EVERY local-mode connection to ``LOCAL_SINGLE_USER_ID`` (F1).

    NATE 2026-07-09: "persistent cases ... I want to accumulate test cases".
    Pre-fix, each client (desktop browser, phone, QGIS plugin, test driver)
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
    - a ULID ``user_id`` -- the client-presented ``requested_user_id`` when one
      was supplied (cases-vanish fix), else a fresh ULID,
    - ``firebase_uid=None`` (no IdP binding),
    - ``email=None`` / ``display_name=None``,
    - ``is_active=True`` (Cases CAN be created),
    - default ``prefs={}``.

    If persistence is provisioned, the user is upserted so the
    ``owner_user_id`` cascade rule (H.2) has a stable target. If
    persistence is unbound, the anonymous user lives in-memory
    only -- the M1 substrate path keeps working.

    cases-vanish fix: when the client presents a stable ``anonymous_user_id``
    that has no record yet, the caller passes it as ``requested_user_id`` so the
    User is provisioned with THAT EXACT id. This is what lets two sockets of one
    browser session (App + Chat, both replaying the same client-owned id)
    converge on ONE anonymous identity -- whichever socket wins the provision
    race writes the deterministic id, and the loser then *reuses* it. Without
    this, each connection minted a random ULID and the owner-scoped case-list
    forked, so Cases appeared to vanish on refresh. The id is ULID-validated
    upstream (``AuthTokenEnvelope.anonymous_user_id: ULIDStr``) and, on the
    persistence-backed path, only reaches here as ``requested_user_id`` after
    ``_anonymous_id_is_claimable`` confirmed NO record exists for it -- so the
    verbatim ``upsert_user`` can never overwrite / hijack an existing
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
        except Exception as exc:  # noqa: BLE001 -- best-effort
            logger.warning(
                "anonymous user upsert failed (continuing in-memory): %s", exc
            )
    return AuthResult(
        user=user,
        firebase_uid=None,
        is_anonymous=True,
        tier="free",
    )


async def _try_reuse_anonymous_user(
    persistence: Persistence,
    anonymous_user_id: str,
) -> User | None:
    """Look up the User by ULID and reuse iff it's an anonymous record.

    job-0172 Part C: the sticky-anonymous path. Returns the existing User
    only when (a) a record exists for ``anonymous_user_id`` and (b) that
    record is marked ``is_anonymous=True``. Returns ``None`` to fall
    through to fresh-user provisioning when either condition fails.

    Why the is_anonymous gate: an attacker could fish a known non-anonymous
    User id from a log and replay it; we MUST NOT re-bind such a record via
    the hint path. Anonymous Users have no credential, so re-binding them is
    the entire point -- the id IS the only identifier they ever had.
    """
    try:
        existing = await persistence.get_user_by_id(anonymous_user_id)
    except Exception as exc:  # noqa: BLE001 -- best-effort: fall back to fresh
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
        # Forbid re-binding to a non-anonymous record via the hint path.
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
    returns ``None`` for three different reasons -- id-not-found, id-belongs-to-
    a-non-anonymous-record, or id-belongs-to-an-inactive-record. We may only
    claim a presented id VERBATIM in the first case; claiming verbatim in the
    other two would overwrite (hijack) an existing non-anonymous or
    deactivated user's ``_id`` on the next ``upsert_user``. This helper isolates
    "the id is genuinely free" so the caller can branch safely.

    Fail-closed: any lookup error returns ``False`` (treat the id as taken),
    so a transient persistence hiccup mints a fresh ULID rather than risk an
    overwrite. The brief extra read is on the (rare) first-connect-of-a-new-id
    path only; the common sticky-reuse path already returned above.
    """
    try:
        existing = await persistence.get_user_by_id(anonymous_user_id)
    except Exception as exc:  # noqa: BLE001 -- fail closed: treat as taken
        logger.warning(
            "anonymous claim-check: get_user_by_id(%s) failed (%s); "
            "minting fresh (not claiming verbatim)",
            anonymous_user_id,
            exc,
        )
        return False
    return existing is None


def build_auth_ack(result: AuthResult) -> AuthAckEnvelope:
    """Construct the ``auth-ack`` envelope payload for a resolved AuthResult.

    Mirrors only the fields the H.5 ack surfaces -- never any credential
    (Decision F wire isolation). The client uses this to drive its
    sticky-anonymous logic.
    """
    return AuthAckEnvelope(
        user_id=result.user.user_id,
        firebase_uid=result.firebase_uid,
        is_anonymous=result.is_anonymous,
        tier=result.tier,
    )


# --------------------------------------------------------------------------- #
# Timeout helper -- public so server.py can use the same default constant.
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
    "DEFAULT_AUTH_TOKEN_TIMEOUT_S",
    "LOCAL_SINGLE_USER_ID",
    "authenticate_token",
    "build_auth_ack",
    "get_auth_token_timeout_s",
]
