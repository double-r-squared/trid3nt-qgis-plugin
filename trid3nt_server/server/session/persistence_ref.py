"""Process-global Persistence handle + env bootstrap."""

from __future__ import annotations

import logging
from trid3nt_server.persistence import Persistence

logger = logging.getLogger("trid3nt_server.server")

# App-level Persistence singleton, bound at startup and otherwise ``None``, in
# which case callers fall back to in-memory state. Module-level rather than
# per-connection: a per-session write needs a typed wrapper, not connection
# isolation, and the binding resets with the process.
_PERSISTENCE: Persistence | None = None

def get_persistence() -> Persistence | None:
    """The app-level ``Persistence`` singleton, or ``None`` when unbound; every
    caller must handle ``None``, because the in-memory path stays supported."""
    return _PERSISTENCE

def set_persistence(p: Persistence | None) -> None:
    """Bind or clear the app-level ``Persistence`` singleton. API-key
    credentials never resolve through it: the credential resolver owns those and
    hands a keyed tool the resolved value."""
    global _PERSISTENCE
    _PERSISTENCE = p

async def init_persistence_from_env() -> Persistence | None:
    """Resolve the ``Persistence`` singleton for the running server, preserving
    whatever the startup path already bound rather than clearing it."""
    if get_persistence() is not None:
        logger.info("Persistence singleton already bound; retained")
        return get_persistence()
    logger.info("Persistence singleton remains unbound (no backend configured)")
    return None
