"""Local WS connect handshake: the ONE fixed local user.

TRID3NT is a local, single-user product: there is no identity provider and no
token verification. ``solver_backend()`` is hardwired to ``local-docker``, so
EVERY connection resolves to the ONE fixed local user
(``LOCAL_SINGLE_USER_ID``) -- the desktop browser, phone, QGIS plugin, and
test drivers all share one case list. The canonical owner identity is an
internal ULID (Decision 10).

On WebSocket connect the client may send an ``auth-token`` envelope (Appendix
H.5 shape). The ``token`` field still rides the wire (clients keep their
handshake unchanged) but is IGNORED here: there is no verifier and no
per-client identity. :func:`authenticate_token` resolves the one local user;
``server.py`` reads / writes the envelopes.

The module is **transport-agnostic** -- it does not touch the WebSocket
itself; ``server.py`` reads / writes envelopes and calls the functions here
for the resolution logic. This keeps the handshake testable without standing
up a real socket.

Invariants this module is responsible for:

- **Wire isolation.** No credential ever persists; the ack
  carries only ``user_id`` / ``is_anonymous``.
- **Decision 10 (canonical id).** The owner id is the fixed local-user
  constant.
- **Canonical persistence.** All user CRUD goes through the ``Persistence``
  interface; no direct driver access.
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
#: the anonymous-fallback path (H.3). Override via env for ops flexibility.
DEFAULT_AUTH_TOKEN_TIMEOUT_S: float = float(
    os.environ.get("TRID3NT_AUTH_TOKEN_TIMEOUT_S", "5.0")
)

# --------------------------------------------------------------------------- #
# Remote-daemon access (2026-07): endpoint advertisement + optional token
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
    unparseable, so the advertised ``http_base`` tracks the actual listener.
    """
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

    IPv4 / hostnames pass through unchanged; ``::1`` becomes ``[::1]`` so the
    ``:port`` suffix is unambiguous.
    """
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def derive_advertised_endpoints(
    local_host: str | None,
) -> AdvertisedEndpoints | None:
    """Build the ``endpoints`` object advertised on the ``auth-ack``.

    Precedence, per field, independently:

    1. Env override -- ``TRID3NT_ADVERTISED_DATA_BASE`` /
       ``TRID3NT_ADVERTISED_HTTP_BASE`` when set (a full ``http://host:port``
       base). Wins unconditionally so an operator can front the daemon behind a
       reverse proxy / different hostname.
    2. Else DERIVED from ``local_host`` -- the server-side socket's local
       address for THIS connection -- plus the known ports
       (:data:`ADVERTISED_DATA_PORT` for data, :func:`_advertised_http_port`
       for HTTP). This is the auto-magic: a laptop dialing ``100.x.x.x:8765``
       over the tailnet gets ``http://100.x.x.x:9000`` / ``:8766`` back, no
       config.

    Returns ``None`` when neither an env override nor a usable ``local_host``
    yields any base (e.g. a test / stub with no real socket and no env) -- the
    ack then carries ``endpoints=None`` and old clients are unaffected.
    """
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

    Read at call time so a test env injection takes effect without re-import.
    An empty string counts as UNSET (gate disabled) so a blank env cannot
    accidentally lock everyone out.
    """
    tok = os.environ.get("TRID3NT_ACCESS_TOKEN")
    return tok if tok else None


def verify_access_token(presented: str | None) -> bool:
    """Constant-time-compare a client-presented token against the gate.

    Returns ``True`` when NO token is configured (the default anon behavior is
    byte-identical) OR the presented token matches ``TRID3NT_ACCESS_TOKEN``.
    Returns ``False`` only when a token IS required and the presented value is
    missing / wrong. The compare uses :func:`hmac.compare_digest` so a
    mismatch does not leak length/prefix via timing.
    """
    required = configured_access_token()
    if required is None:
        return True
    return hmac.compare_digest(str(presented or ""), required)

# --------------------------------------------------------------------------- #
# TRID3NT local build: ONE fixed local user (F1, live-feedback 2026-07-09)
# --------------------------------------------------------------------------- #

#: The single fixed user every connection resolves to in local mode
#: (``TRID3NT_SOLVER_BACKEND=local-docker`` / FilePersistence). A constant,
#: ULID-shaped id ("L0CA1 VSER" in Crockford base32 -- L/O/U are not in the
#: alphabet, hence 1/0/V) so the desktop browser, phone, QGIS plugin, and
#: test drivers all land on the SAME case list.
LOCAL_SINGLE_USER_ID = "0110CA1VSERAAAAAAAAAAAAAAA"


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


@dataclass
class AuthResult:
    """Outcome of the connect handshake.

    Fields:
    - ``user`` -- the resolved ``User`` (always populated).
    - ``is_anonymous`` -- True for every locally-resolved user.
    """

    user: User
    is_anonymous: bool


class NonLocalAuthUnsupported(RuntimeError):
    """Raised when the handshake runs outside local single-user mode.

    ``solver_backend()`` is hardwired to ``local-docker``, so this path is
    unreachable in the product. The explicit raise replaces the deleted
    cloud/multi-user anonymous-provisioning branch: a non-local backend has no
    identity provider and no token verifier here, so it must fail LOUD rather
    than silently resolve an unauthenticated identity.
    """


async def authenticate_token(
    token_envelope: AuthTokenEnvelope | None,
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve an ``AuthTokenEnvelope`` to the ONE fixed local ``User``.

    ``solver_backend()`` is hardwired to ``local-docker``
    (:func:`_is_local_single_user_mode` True), so EVERY connection -- any
    token -- resolves to the ONE fixed local user (``LOCAL_SINGLE_USER_ID``).
    The token field still rides the wire (clients keep their handshake
    unchanged) but is ignored: there is no verifier and no per-client identity.

    Raises :class:`NonLocalAuthUnsupported` when not in local single-user mode
    -- unreachable in the product, made loud so a mis-provisioned backend can
    never silently fall through to an unauthenticated identity.
    """
    if not _is_local_single_user_mode():
        raise NonLocalAuthUnsupported(
            "authenticate_token requires local single-user mode "
            "(solver_backend=local-docker); no non-local auth path exists."
        )
    return await _resolve_local_single_user(persistence)


def _is_local_single_user_mode() -> bool:
    """True when auth must collapse to the ONE fixed local user.

    The canonical is-local seam (same one ``secrets_handler`` and
    ``server._local_compute_lane`` use): ``TRID3NT_SOLVER_BACKEND=local-docker``
    -> ``tools.simulation.solver.solver_backend()`` returns ``local-docker``. The TRID3NT
    local build pins it. Read at call time so a test env injection takes
    effect without re-import.
    """
    from trid3nt_server.data.simulation.solver.solver import SOLVER_BACKEND_LOCAL_DOCKER, solver_backend

    return solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER


async def _resolve_local_single_user(
    persistence: Persistence | None,
) -> AuthResult:
    """Resolve EVERY connection to ``LOCAL_SINGLE_USER_ID``.

    There is exactly one human on a local build, so all connections collapse
    onto one fixed user:

    - reuse the persisted local-user record when it exists (stable
      ``created_at`` / prefs -- no re-upsert churn per reconnect);
    - else provision it verbatim and upsert (when persistence is bound; else
      it lives in-memory for the session);
    - ``is_anonymous`` stays True so the auth-ack keeps the client handshake
      unchanged.
    """
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
    """Construct the ``auth-ack`` envelope payload for a resolved AuthResult.

    Mirrors only the fields the H.5 ack surfaces -- never any credential
    (wire isolation). The client reads ``user_id`` for its session identity.

    ``endpoints`` (remote-daemon access, 2026-07) is the optional
    server-advertised sibling-endpoint object (see
    :func:`derive_advertised_endpoints`). Defaults ``None`` so existing callers
    are unchanged and old clients / stubs stay byte-identical on the wire.
    """
    return AuthAckEnvelope(
        user_id=result.user.user_id,
        is_anonymous=result.is_anonymous,
        endpoints=endpoints,
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
    "NonLocalAuthUnsupported",
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
