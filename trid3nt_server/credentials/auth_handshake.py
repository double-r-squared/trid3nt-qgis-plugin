"""Local WS connect handshake: the ONE fixed local user.

There is no identity provider and no token verification: every connection
resolves to ``LOCAL_SINGLE_USER_ID``, and no credential ever rides the ack.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass

from trid3nt_contracts.auth import (
    AdvertisedEndpoints,
    AuthAckEnvelope,
    AuthTokenEnvelope,
)
from trid3nt_contracts.common import now_utc
from trid3nt_contracts.user import User

from trid3nt_server.persistence import Persistence

logger = logging.getLogger("trid3nt_server.credentials.auth_handshake")

#: Default time the agent waits for ``auth-token`` before falling through to
#: the anonymous-fallback path.
DEFAULT_AUTH_TOKEN_TIMEOUT_S: float = float(
    os.environ.get("TRID3NT_AUTH_TOKEN_TIMEOUT_S", "5.0")
)

# --------------------------------------------------------------------------- #
# Remote-daemon access: endpoint advertisement + optional token
# --------------------------------------------------------------------------- #

#: Object-store (MinIO) port the daemon co-hosts. Fixed on the local stack;
#: a non-standard MinIO port is handled by the ``TRID3NT_ADVERTISED_DATA_BASE``
#: full-URL override rather than a second port env.
ADVERTISED_DATA_PORT: int = 9000

#: Default agent read-only HTTP port. The real listener binds
#: ``TRID3NT_AGENT_HTTP_PORT`` (default 8766); ``_advertised_http_port`` reads
#: that same env so the advertised base always matches the bound port.
ADVERTISED_HTTP_PORT_DEFAULT: int = 8766


def _advertised_http_port() -> int:
    """The port the agent HTTP surface is bound on (``TRID3NT_AGENT_HTTP_PORT``).
    Falls back to :data:`ADVERTISED_HTTP_PORT_DEFAULT` when the env is unset or
    unparseable, so the advertised base tracks the actual listener."""
    try:
        return int(
            os.environ.get(
                "TRID3NT_AGENT_HTTP_PORT", str(ADVERTISED_HTTP_PORT_DEFAULT)
            )
        )
    except (TypeError, ValueError):
        return ADVERTISED_HTTP_PORT_DEFAULT


def _host_for_url(host: str) -> str:
    """Bracket a bare IPv6 literal for use in an ``http://host:port`` URL.
    IPv4 and hostnames pass through unchanged; ``::1`` becomes ``[::1]`` so the
    ``:port`` suffix is unambiguous."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def derive_advertised_endpoints(
    local_host: str | None,
) -> AdvertisedEndpoints | None:
    """Build the ``endpoints`` object advertised on the ``auth-ack``.
    ``None`` when neither an env override nor a usable ``local_host`` yields any
    base; the ack then carries ``endpoints=None``."""
    # Precedence per field, independently: an env override wins unconditionally
    # so an operator can front the daemon behind a reverse proxy or a different
    # hostname; otherwise each base is derived from THIS connection's own local
    # address plus the known ports.
    data_base = os.environ.get("TRID3NT_ADVERTISED_DATA_BASE") or None
    http_base = os.environ.get("TRID3NT_ADVERTISED_HTTP_BASE") or None
    if local_host:
        host = _host_for_url(local_host)
        if data_base is None:
            data_base = f"http://{host}:{ADVERTISED_DATA_PORT}"
        if http_base is None:
            http_base = f"http://{host}:{_advertised_http_port()}"
    if data_base is None and http_base is None:
        return None
    return AdvertisedEndpoints(data_base=data_base, http_base=http_base)


def configured_access_token() -> str | None:
    """The shared access token gate, or ``None`` when auth is open (default).
    Read at call time, and an EMPTY string counts as unset so a blank env cannot
    lock everyone out."""
    tok = os.environ.get("TRID3NT_ACCESS_TOKEN")
    return tok if tok else None


def verify_access_token(presented: str | None) -> bool:
    """Constant-time-compare a client-presented token against the gate.
    ``True`` when NO token is configured or the presented token matches; ``False``
    only when a token IS required and the value is missing or wrong."""
    required = configured_access_token()
    if required is None:
        return True
    # ``compare_digest`` so a mismatch leaks neither length nor prefix through
    # timing.
    return hmac.compare_digest(str(presented or ""), required)

# --------------------------------------------------------------------------- #
# TRID3NT local build: ONE fixed local user
# --------------------------------------------------------------------------- #

#: The single fixed user every connection resolves to. A constant, ULID-shaped
#: id ("L0CA1 VSER" in Crockford base32 -- L/O/U are not in the alphabet, hence
#: 1/0/V) so every client lands on the SAME case list.
LOCAL_SINGLE_USER_ID = "0110CA1VSERAAAAAAAAAAAAAAA"


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


@dataclass
class AuthResult:
    """Outcome of the connect handshake; ``user`` is always populated."""

    user: User
    is_anonymous: bool


async def authenticate_token(
    token_envelope: AuthTokenEnvelope | None,
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve an ``AuthTokenEnvelope`` to the ONE fixed local ``User``.
    The token field still rides the wire but is IGNORED: there is no verifier
    and no per-client identity to resolve it against."""
    return await _resolve_local_single_user(persistence)


async def _resolve_local_single_user(
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve EVERY connection to ``LOCAL_SINGLE_USER_ID``.
    The persisted record is reused when it exists, so ``created_at`` and prefs
    stay stable across reconnects; unbound persistence means a session-only user."""
    user: User | None = None
    if persistence is not None:
        try:
            user = await persistence.get_user_by_id(LOCAL_SINGLE_USER_ID)
        except Exception as exc:  # noqa: BLE001 -- best-effort: provision fresh
            logger.warning(
                "local user lookup failed (%s); provisioning fresh", exc
            )
            user = None
    if user is None:
        user = User(
            user_id=LOCAL_SINGLE_USER_ID,
            email=None,
            display_name=None,
            created_at=now_utc(),
            is_active=True,
            prefs={},
            is_anonymous=True,
        )
        if persistence is not None:
            try:
                await persistence.upsert_user(user)
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "local user upsert failed (continuing in-memory): %s", exc
                )
    return AuthResult(user=user, is_anonymous=True)


def build_auth_ack(
    result: AuthResult,
    endpoints: AdvertisedEndpoints | None = None,
) -> AuthAckEnvelope:
    """Construct the ``auth-ack`` envelope payload for a resolved ``AuthResult``.
    Mirrors only the acked fields and NEVER a credential; ``endpoints`` is the
    optional advertised-sibling object and defaults to absent."""
    return AuthAckEnvelope(
        user_id=result.user.user_id,
        is_anonymous=result.is_anonymous,
        endpoints=endpoints,
    )


# --------------------------------------------------------------------------- #
# Timeout helper -- public so the connect handler shares the default constant.
# --------------------------------------------------------------------------- #


def get_auth_token_timeout_s(default: float | None = None) -> float:
    """The auth-token-arrival timeout, in seconds.
    An explicit ``default`` short-circuits; otherwise
    :data:`DEFAULT_AUTH_TOKEN_TIMEOUT_S` applies."""
    if default is not None:
        return default
    return DEFAULT_AUTH_TOKEN_TIMEOUT_S


__all__ = [
    "AuthResult",
    "DEFAULT_AUTH_TOKEN_TIMEOUT_S",
    "ADVERTISED_DATA_PORT",
    "ADVERTISED_HTTP_PORT_DEFAULT",
    "LOCAL_SINGLE_USER_ID",
    "authenticate_token",
    "build_auth_ack",
    "configured_access_token",
    "derive_advertised_endpoints",
    "get_auth_token_timeout_s",
    "verify_access_token",
]
