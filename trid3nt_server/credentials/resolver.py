"""Runtime credential resolver: in-memory session cache -> the row's env var.

A source that needs a key STATES it on its own row (``auth.credential``), so the
name a key is stored under, the env var it falls back to and the form that offers
it all read one fact. The cache holds raw key material only in process memory for
the session's lifetime; it is never persisted, logged, or echoed on any envelope.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Final

from trid3nt_contracts.source_spec import CredentialSpec

logger = logging.getLogger("trid3nt_server.credentials.resolver")

__all__ = [
    "MissingCredentialError",
    "credential_for_tool",
    "keyed_credentials",
    "resolve_credential",
    "set_session_credential",
    "clear_session",
    "session_provider_ids",
]


class MissingCredentialError(RuntimeError):
    """A keyed source was asked for with no key anywhere to serve it.

    The refusal names the credential and the one place a key is entered; the
    chat is never that place, so it points at the plugin's keys form."""

    error_code: str = "CREDENTIAL_MISSING"
    retryable: bool = False
    actionability: str = "user"

    def __init__(self, credential: CredentialSpec) -> None:
        signup = f" A key is issued at {credential.signup_url}." if credential.signup_url else ""
        super().__init__(
            f"{credential.label} needs a key and none is stored. Open the "
            f"plugin's Settings -> Keys and enter the {credential.label} key "
            f"there (never in the chat).{signup}"
        )
        self.credential_name = credential.name


# Guarded by a lock: ``secret-add`` handling and tool-dispatch resolution run on
# the same asyncio loop today, but the lock keeps the module honest if a value
# is ever written from a worker thread (offloaded fetcher path).
_LOCK: Final[threading.Lock] = threading.Lock()
_SESSION_CREDENTIALS: dict[str, dict[str, str]] = {}


def credential_for_tool(tool_name: str) -> CredentialSpec | None:
    """The credential ``tool_name``'s row declares, or ``None`` for a public one."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    spec = get_spec(tool_name)
    return spec.auth.credential if spec is not None else None


def keyed_credentials() -> dict[str, CredentialSpec]:
    """Every declared credential by name: the rows the keys form offers.
    Two rows served by one upstream account collapse to the single name both
    state, which is why one entered key serves both."""
    from trid3nt_server.tools.fetchers._router.registration import (
        get_spec,
        registered_spec_names,
    )

    out: dict[str, CredentialSpec] = {}
    for name in sorted(registered_spec_names()):
        spec = get_spec(name)
        if spec is not None and spec.auth.credential is not None:
            out[spec.auth.credential.name] = spec.auth.credential
    return out


def set_session_credential(session_id: str, provider_id: str, value: str) -> None:
    """Store a raw key value pushed over the ``secret-add`` seam.
    The value NEVER appears in a log line, and a blank argument is ignored so a
    malformed push cannot create a ghost cache entry."""
    if not session_id or not provider_id or not value:
        return
    with _LOCK:
        _SESSION_CREDENTIALS.setdefault(session_id, {})[provider_id] = value
    logger.info(
        "credential cached session=%s credential=%s (value hidden)",
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
    """The credential names currently cached for ``session_id``."""
    with _LOCK:
        return frozenset(_SESSION_CREDENTIALS.get(session_id, {}))


def resolve_credential(session_id: str, tool_name: str) -> str | None:
    """Resolve a keyed tool's credential value: session cache -> the row's env var.
    ``None`` when the tool is not keyed or neither source holds a value; never
    raises for a missing key, which is the caller's refusal to make."""
    credential = credential_for_tool(tool_name)
    if credential is None:
        return None
    with _LOCK:
        cached = _SESSION_CREDENTIALS.get(session_id, {}).get(credential.name)
    if cached:
        return cached
    env_value = os.environ.get(credential.env_var)
    return env_value.strip() if env_value and env_value.strip() else None
