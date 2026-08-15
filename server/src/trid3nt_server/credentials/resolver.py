"""Runtime credential resolver: in-memory session cache -> env fallback.

The single runtime source of an API-key VALUE for a keyed tool. QgsAuthManager
(plugin-side) is the credential HOME; the plugin brokers key values over the
existing ``secret-add`` WS seam into this in-memory session cache. Env vars stay
the headless / dev floor that must never die.

Resolution order (``resolve_credential``):

1. SESSION CACHE -- a value the plugin pushed over ``secret-add`` (connect-time
   or in response to a ``credential-request``), keyed by
   ``session_id -> provider_id``. Wins over env so a user-supplied key overrides
   a dev env var.
2. ENV fallback -- the SAME env var the tool's own ``_resolve_*_key`` reads, so
   a headless canary / driver / CI run with the env set keeps every keyed
   fetcher working with no plugin session at all.

The resolver returns a raw key VALUE (a ``str``). The server injects it into the
tool's ``params["secret_ref"]``; every keyed fetcher's ``_materialize_secret``
accepts a ``str`` secret_ref verbatim, so no file vault and no Persistence read
sit on the path. A ``None`` return means "no key here" -- the fetcher then falls
to its own env path and, absent a key, raises its typed auth error, which the
credential-request flow acts on.

Wire isolation: the cache holds raw key material only in process
memory for the session's lifetime; it is never persisted, logged, or echoed on
any reply envelope.
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

    Typed, user-actionable: the credential-request flow surfaces a card so the
    user supplies the key. Raised by callers that require a value; the resolver
    itself returns ``None`` (the fetcher's own env path is still a valid floor).
    """


# --------------------------------------------------------------------------- #
# Session cache: session_id -> {provider_id: raw_value}
# --------------------------------------------------------------------------- #

# Guarded by a lock: ``secret-add`` handling and tool-dispatch resolution run on
# the same asyncio loop today, but the lock keeps the module honest if a value
# is ever written from a worker thread (offloaded fetcher path).
_LOCK: Final[threading.Lock] = threading.Lock()
_SESSION_CREDENTIALS: dict[str, dict[str, str]] = {}


# --------------------------------------------------------------------------- #
# Env fallback: provider_id -> the env var the tool's own resolver reads.
# Single-key providers only. Movebank is intentionally absent: its credential is
# a composite user + password pair (TRID3NT_MOVEBANK_USER / _PASSWORD) that its
# own fetcher resolves; the session-cache path still serves a pushed Movebank
# JSON blob, and the fetcher's own composite env fallback covers the headless
# case, so the resolver never needs to reassemble it.
# --------------------------------------------------------------------------- #
_PROVIDER_ENV_VARS: Final[dict[str, tuple[str, ...]]] = {
    "firms": ("TRID3NT_FIRMS_MAP_KEY",),
    "ebird": ("TRID3NT_EBIRD_API_KEY",),
    "ecmwf_cds": ("TRID3NT_COPERNICUS_CDS_API_KEY",),
    "iucn_red_list": ("TRID3NT_IUCN_RED_LIST_API_KEY",),
}


def set_session_credential(session_id: str, provider_id: str, value: str) -> None:
    """Store a raw key value pushed over the ``secret-add`` seam.

    The value NEVER appears in a log line. A blank ``session_id`` /
    ``provider_id`` / ``value`` is ignored (a malformed push must not create a
    ghost cache entry).
    """
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

    Returns the raw key ``str`` or ``None`` when the tool is not keyed or no
    value is available in either source. Never raises for a missing key -- a
    ``None`` lets the fetcher's own path run and raise its typed auth error,
    which the credential-request flow then handles.
    """
    provider = provider_for_tool(tool_name)
    if provider is None:
        return None
    provider_id = provider.provider_id
    with _LOCK:
        cached = _SESSION_CREDENTIALS.get(session_id, {}).get(provider_id)
    if cached:
        return cached
    return _env_value_for_provider(provider_id)
