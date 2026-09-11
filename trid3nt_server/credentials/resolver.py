"""Runtime credential resolver: in-memory session cache -> env fallback.

The cache holds raw key material only in process memory for the session's
lifetime; it is never persisted, logged, or echoed on any reply envelope.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Final

from trid3nt_server.credentials.credential_registry import provider_for_tool

logger = logging.getLogger("trid3nt_server.credentials.resolver")

__all__ = [
    "MissingCredentialError",
    "resolve_credential",
    "set_session_credential",
    "clear_session",
    "session_provider_ids",
]


class MissingCredentialError(RuntimeError):
    """No credential value could be resolved for a keyed tool.
    Raised by callers that require a value; the resolver itself returns
    ``None``, leaving the fetcher's own env path as the floor."""



# Guarded by a lock: ``secret-add`` handling and tool-dispatch resolution run on
# the same asyncio loop today, but the lock keeps the module honest if a value
# is ever written from a worker thread (offloaded fetcher path).
_LOCK: Final[threading.Lock] = threading.Lock()
_SESSION_CREDENTIALS: dict[str, dict[str, str]] = {}


# Env fallback: provider_id -> the env var the tool's own resolver reads.
# Single-key providers only. Movebank is deliberately absent: its credential is
# a composite user + password pair its own fetcher resolves, so the resolver
# never has to reassemble one.
_PROVIDER_ENV_VARS: Final[dict[str, tuple[str, ...]]] = {
    "firms": ("TRID3NT_FIRMS_MAP_KEY",),
    "ebird": ("TRID3NT_EBIRD_API_KEY",),
    "ecmwf_cds": ("TRID3NT_COPERNICUS_CDS_API_KEY",),
    "iucn_red_list": ("TRID3NT_IUCN_RED_LIST_API_KEY",),
}


def set_session_credential(session_id: str, provider_id: str, value: str) -> None:
    """Store a raw key value pushed over the ``secret-add`` seam.
    The value NEVER appears in a log line, and a blank argument is ignored so a
    malformed push cannot create a ghost cache entry."""
    if not session_id or not provider_id or not value:
        return
    with _LOCK:
        _SESSION_CREDENTIALS.setdefault(session_id, {})[provider_id] = value
    logger.info(
        "credential cached session=%s provider=%s (value hidden)",
        session_id,
        provider_id,
    )


def clear_session(session_id: str) -> None:
    """Drop a session's cached credentials (call on disconnect / teardown)."""
    if not session_id:
        return
    with _LOCK:
        _SESSION_CREDENTIALS.pop(session_id, None)


def session_provider_ids(session_id: str) -> frozenset[str]:
    """The provider_ids currently cached for ``session_id`` (test/introspection)."""
    with _LOCK:
        return frozenset(_SESSION_CREDENTIALS.get(session_id, {}))


def _env_value_for_provider(provider_id: str) -> str | None:
    """First non-empty env value across the provider's candidate env vars."""
    for env_name in _PROVIDER_ENV_VARS.get(provider_id, ()):  # noqa: SIM110
        val = os.environ.get(env_name)
        if val and val.strip():
            return val.strip()
    return None


def resolve_credential(session_id: str, tool_name: str) -> str | None:
    """Resolve a keyed tool's credential value: session cache -> env fallback.
    ``None`` when the tool is not keyed or neither source holds a value; never
    raises for a missing key."""
    provider = provider_for_tool(tool_name)
    if provider is None:
        return None
    provider_id = provider.provider_id
    with _LOCK:
        cached = _SESSION_CREDENTIALS.get(session_id, {}).get(provider_id)
    if cached:
        return cached
    return _env_value_for_provider(provider_id)
