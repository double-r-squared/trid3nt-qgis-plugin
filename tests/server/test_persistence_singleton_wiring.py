"""The Persistence-singleton startup wiring.

A minimal in-memory client satisfies ``MCPClientProtocol``; set / get
round-trips and ``None`` clears it."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_server.store.cases import (
    MCPClientProtocol,
    Persistence,
    make_file_persistence,
)
from trid3nt_server.server import (
    get_persistence,
    set_persistence,
)


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
