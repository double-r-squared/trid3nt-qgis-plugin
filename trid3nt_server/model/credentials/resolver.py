"""Runtime credential resolver: in-memory session cache -> the row's env var.

A source that needs a key STATES it on its own row (``auth.credential``), so the
name a key is stored under, the env var it falls back to and the label the list that
offers it shows all read one fact. A hook reads the key of the session whose dispatch is running. The cache holds raw key material only in process memory for
the session's lifetime; it is never persisted, logged, or echoed on any envelope.
"""

from __future__ import annotations

import logging
import os
import contextvars
import threading
from typing import Final

from trid3nt_contracts.secrets import LANGUAGE_MODEL_CREDENTIAL
from trid3nt_contracts.source_spec import CredentialSpec, SourceSpec
from trid3nt_server.tools.fetchers._fetch_common import KeyRequiredError

logger = logging.getLogger("trid3nt_server.model.credentials.resolver")

__all__ = [
    "bind_session",
    "credential_value",
    "unbind_session",
    "stored_value",
    "credential_for_tool",
    "keyed_credentials",
    "resolve_credential",
    "set_session_credential",
    "clear_session",
    "session_provider_ids",
]


# Guarded by a lock: ``secret-add`` handling and tool-dispatch resolution run on
# the same asyncio loop today, but the lock keeps the module honest if a value
# is ever written from a worker thread (offloaded fetcher path).
_LOCK: Final[threading.Lock] = threading.Lock()
_SESSION_CREDENTIALS: dict[str, dict[str, str]] = {}

# The session whose tool dispatch is running; a worker thread started from it inherits the binding.
_ACTIVE_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("trid3nt_active_session", default="")


def bind_session(session_id: str) -> contextvars.Token:
    return _ACTIVE_SESSION.set(session_id)


def unbind_session(token: contextvars.Token) -> None:
    _ACTIVE_SESSION.reset(token)


def stored_value(name: str, env_var: str | None = None) -> str | None:
    """The key held under ``name`` for the bound session, else the value of ``env_var`` where one is named; ``None`` when neither holds one."""
    with _LOCK:
        cached = _SESSION_CREDENTIALS.get(_ACTIVE_SESSION.get(), {}).get(name)
    if cached:
        return cached
    env_value = os.environ.get(env_var) if env_var else None
    return env_value.strip() if env_value and env_value.strip() else None


def credential_value(spec: SourceSpec) -> str:
    """The key the row's declared credential holds for the bound session (its env var behind it), else a ``KeyRequiredError``."""
    credential = spec.auth.credential
    if credential is None:
        raise ValueError(f"row {spec.name!r} declares no credential")
    value = stored_value(credential.name, credential.env_var)
    if value is None:
        raise KeyRequiredError(spec.name, credential.name, credential.label)
    return value


def credential_for_tool(tool_name: str) -> CredentialSpec | None:
    """The credential ``tool_name``'s row declares, or ``None`` for a public one."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    spec = get_spec(tool_name)
    return spec.auth.credential if spec is not None else None


def keyed_credentials() -> dict[str, CredentialSpec]:
    """Every declared credential by name, with the label the keys list shows and the env var behind it.
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
    """Hold a raw key value pushed over the ``secret-add`` seam, in memory only.
    The language model's key goes to the process env its adapter reads per call;
    a blank argument is ignored so a malformed push cannot create a ghost entry."""
    if not session_id or not provider_id or not value:
        return
    if provider_id == LANGUAGE_MODEL_CREDENTIAL:
        # The plugin re-pushes on every connect, so a daemon restart that
        # empties the env loses nothing.
        os.environ["TRID3NT_OPENAI_API_KEY"] = value
    else:
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
