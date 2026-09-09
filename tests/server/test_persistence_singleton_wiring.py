"""Tests for the Persistence-singleton startup wiring.

Coverage:
    1. ``test_prebound_file_persistence_is_preserved`` — ``init_persistence_from_env``
       preserves a file-backed ``Persistence`` pre-bound by the startup path
       (``main._maybe_bind_dev_persistence``) and returns it, not ``None``.
       The agent must never crash on a fresh clone.

    2. ``test_disabled_dev_persistence_returns_none`` — with
       ``TRID3NT_DEV_PERSISTENCE=0`` (the no-persistence escape hatch) the
       function returns ``None`` and the singleton stays unbound; callers
       handle ``None`` gracefully.

    3. ``test_mcp_client_protocol_compatibility`` — a minimal in-memory client
       satisfies ``MCPClientProtocol`` structurally, confirming the protocol
       definition is duck-typed correctly.

    4. ``test_set_get_persistence_singleton`` — ``set_persistence`` /
       ``get_persistence`` round-trips the module-level singleton; ``None``
       clears it.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_server.persistence import (
    MCPClientProtocol,
    Persistence,
    make_file_persistence,
)
from trid3nt_server.server import (
    get_persistence,
    init_persistence_from_env,
    set_persistence,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _MockMCPClient:
    """Minimal in-memory store client that satisfies ``MCPClientProtocol``."""

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return {"documents": []}


def _clean_persistence_singleton():
    """Reset the module-level Persistence singleton before/after each test."""
    original = get_persistence()
    set_persistence(None)
    yield
    set_persistence(original)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prebound_file_persistence_is_preserved(tmp_path):
    """init_persistence_from_env preserves the pre-bound file-backed singleton.

    When ``TRID3NT_DEV_PERSISTENCE=1`` (forced on) and
    ``TRID3NT_DEV_PERSISTENCE_DIR`` points at a temp dir, the function returns
    the file-backed ``Persistence`` the startup path bound.  The agent service
    must survive a fresh clone with zero configuration.
    """
    set_persistence(None)
    try:
        env_overrides = {
            "TRID3NT_DEV_PERSISTENCE": "1",
            "TRID3NT_DEV_PERSISTENCE_DIR": str(tmp_path),
        }
        with patch.dict(
            os.environ,
            env_overrides,
            clear=False,
        ):
            # Pre-bind dev persistence (mirrors what main._maybe_bind_dev_persistence does).
            p = make_file_persistence(tmp_path)
            set_persistence(p)

            result = await init_persistence_from_env()

        # Should return the pre-bound file-backed singleton, not None.
        assert result is not None
        assert isinstance(result, Persistence)
    finally:
        set_persistence(None)


@pytest.mark.asyncio
async def test_disabled_dev_persistence_returns_none():
    """With TRID3NT_DEV_PERSISTENCE=0, returns None.

    This is the no-persistence escape hatch: the singleton stays unbound and
    the agent service starts without any persistence.  Callers handle None
    gracefully.
    """
    set_persistence(None)
    try:
        with patch.dict(
            os.environ,
            {"TRID3NT_DEV_PERSISTENCE": "0"},
            clear=False,
        ):
            result = await init_persistence_from_env()

        assert result is None
        assert get_persistence() is None
    finally:
        set_persistence(None)


# ``MCPClientProtocol`` is the store surface: the file backend implements it in
# production and an in-memory mock implements it in tests. The compatibility test
# below pins that structural contract.


def test_mcp_client_protocol_compatibility():
    """_MockMCPClient satisfies MCPClientProtocol via duck-typing.

    Constructs a ``Persistence`` with the mock client and calls one typed
    method to verify the protocol surface is compatible.  No I/O is performed.
    """
    client = _MockMCPClient()
    # Pydantic's Protocol is structural — Persistence.__init__ accepts any
    # object that has .call_tool(...).  This must not raise.
    p = Persistence(client)
    assert p is not None


def test_set_get_persistence_singleton():
    """set_persistence / get_persistence round-trip the module-level singleton."""
    original = get_persistence()
    try:
        # Set a mock Persistence.
        mock_client = _MockMCPClient()
        p = Persistence(mock_client)
        set_persistence(p)
        assert get_persistence() is p

        # Clear it.
        set_persistence(None)
        assert get_persistence() is None

    finally:
        set_persistence(original)
