"""Process-global Persistence handle + env bootstrap."""

from __future__ import annotations

import logging
from trid3nt_server.persistence import Persistence

logger = logging.getLogger("trid3nt_server.server")

# App-level Persistence singleton. ``Persistence`` wraps the file-backed
# document store with a typed surface (CaseSummary / User / SecretRecord /
# CaseChatMessage). Bound at startup by ``main._maybe_bind_dev_persistence``;
# otherwise stays ``None`` and callers fall back to in-memory state.
# Module-level (not per-connection): per-session writes only need a typed
# wrapper not connection isolation, and it resets on process restart for tests.
_PERSISTENCE: Persistence | None = None

def get_persistence() -> Persistence | None:
    """Return the app-level ``Persistence`` singleton, or ``None`` if unbound.

    Callers (chiefly the message-dispatch path in this module) MUST handle
    the ``None`` case gracefully -- the in-memory path is still supported
    when persistence is not bound (e.g. CI with ``TRID3NT_DEV_PERSISTENCE=0``).
    """
    return _PERSISTENCE

def set_persistence(p: Persistence | None) -> None:
    """Bind or clear the app-level ``Persistence`` singleton.

    The agent service startup path calls this once after binding the file
    backend; tests call it directly with a mock-backed ``Persistence`` to
    exercise the wired-in code paths. API-key credentials do not resolve
    through Persistence -- ``credentials.resolver`` (session cache -> env) owns
    that, and keyed tools receive the resolved value as a ``str`` secret_ref.
    """
    global _PERSISTENCE
    _PERSISTENCE = p

async def init_persistence_from_env() -> Persistence | None:
    """Resolve the ``Persistence`` singleton for the running server.

    The persistence backend is file-backed, bound by
    ``main._maybe_bind_dev_persistence`` /
    ``persistence.make_persistence_for_backend`` before this runs. This method
    does NOT clear a pre-bound singleton; it preserves whatever the startup
    path already bound. Returns the ``Persistence`` instance or ``None``.
    """
    # This method does NOT clear a pre-bound singleton. The agent
    # startup path (``main._maybe_bind_dev_persistence``)
    # may have already bound a ``Persistence``; we preserve it.
    if get_persistence() is not None:
        logger.info("Persistence singleton already bound; retained")
        return get_persistence()
    logger.info("Persistence singleton remains unbound (no backend configured)")
    return None
