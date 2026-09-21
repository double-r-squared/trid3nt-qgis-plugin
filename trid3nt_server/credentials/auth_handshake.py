"""Local WS connect handshake: one token, one user, always on.

The daemon mints a shared access token at first start and every connection
must present it; a connection that presents nothing is not the user. There is
no identity to resolve - the token IS the one user, ``LOCAL_SINGLE_USER_ID``.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from trid3nt_contracts.auth import (
    AdvertisedEndpoints,
    AuthAckEnvelope,
)

logger = logging.getLogger("trid3nt_server.credentials.auth_handshake")


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


def access_token_path() -> Path:
    """The daemon config file the minted access token lives in.
    ``TRID3NT_HOME`` relocates the config home for a second daemon on one box."""
    home = os.environ.get("TRID3NT_HOME")
    return (Path(home) if home else Path.home() / ".trid3nt") / "access_token"


def ensure_access_token() -> str:
    """The daemon's access token, minted into the config file on first start.
    Logged once here, at the mint, and never again: a later start reads the same
    file silently. ``TRID3NT_ACCESS_TOKEN`` overrides and mints nothing."""
    env = os.environ.get("TRID3NT_ACCESS_TOKEN")
    if env:
        logger.info("access token: TRID3NT_ACCESS_TOKEN env override in force")
        return env
    path = access_token_path()
    existing = _read_token_file(path)
    if existing:
        logger.info("access token: read from %s", path)
        return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600 BEFORE the bytes land: a world-readable window, however
    # short, is the whole secret.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    logger.info(
        "access token minted -> %s\n    paste this into the plugin's "
        "Settings > Server token:\n    %s",
        path,
        token,
    )
    return token


def _read_token_file(path: Path) -> str | None:
    """The token stored in ``path``, or ``None`` when absent or unreadable.
    An empty file counts as absent so a truncated write re-mints."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def configured_access_token() -> str | None:
    """The token a presented one is compared against, read at call time:
    ``TRID3NT_ACCESS_TOKEN`` first, then the minted config file. ``None`` only
    when neither exists, and then NOTHING matches - the gate fails closed."""
    env = os.environ.get("TRID3NT_ACCESS_TOKEN")
    if env:
        return env
    return _read_token_file(access_token_path())


def verify_access_token(presented: str | None) -> bool:
    """Constant-time-compare a client-presented token against the daemon's.
    ``compare_digest`` so a mismatch leaks neither length nor prefix through
    timing; a missing token on either side is a refusal, never a pass."""
    required = configured_access_token()
    if not required:
        return False
    return hmac.compare_digest(str(presented or ""), required)


#: The single fixed session identity every connection is scoped to, the id the
#: rest of the server joins Cases on. A constant, ULID-shaped id ("L0CA1 VSER"
#: in Crockford base32 -- L/O/U are not in the alphabet, hence 1/0/V).
LOCAL_SINGLE_USER_ID = "0110CA1VSERAAAAAAAAAAAAAAA"


def build_auth_ack(
    endpoints: AdvertisedEndpoints | None = None,
) -> AuthAckEnvelope:
    """Construct the ``auth-ack`` payload for a verified connection.
    Carries the fixed session identity and NEVER a credential; ``endpoints`` is
    the optional advertised-sibling object and defaults to absent."""
    return AuthAckEnvelope(
        user_id=LOCAL_SINGLE_USER_ID,
        endpoints=endpoints,
    )


__all__ = [
    "ADVERTISED_DATA_PORT",
    "ADVERTISED_HTTP_PORT_DEFAULT",
    "LOCAL_SINGLE_USER_ID",
    "access_token_path",
    "build_auth_ack",
    "configured_access_token",
    "derive_advertised_endpoints",
    "ensure_access_token",
    "verify_access_token",
]
