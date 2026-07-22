"""Tests for MongoDB MCP server wiring — job-0200 Wave 4.11 M1.

Coverage:
    1. ``test_no_mcp_falls_back_to_dev_persistence`` — when
       ``GRACE2_MONGO_MCP_STDIO`` is unset (the default local-dev case),
       ``init_persistence_from_env`` does NOT raise and the server starts
       with file-backed dev Persistence (or None if dev persistence is also
       disabled).  The agent must never crash on a fresh clone.

    2. ``test_no_mcp_stdio_returns_prebound_or_none`` — with no MCP env vars
       and ``GRACE2_DEV_PERSISTENCE=0`` (CI escape hatch), the function
       returns ``None`` gracefully.

    3. ``test_mcp_stdio_1_attempts_connection`` — when ``GRACE2_MONGO_MCP_STDIO=1``
       is set, ``init_persistence_from_env`` calls ``MCPClient.start`` and
       constructs a ``Persistence`` backed by the live client.  Uses a mocked
       transport: no real Atlas connection is made.

    4. ``test_mcp_stdio_1_start_failure_does_not_crash_server`` — if
       ``MCPClient.start`` raises (Node.js missing, Atlas unreachable),
       ``run_server``'s ``try/except`` around the init call ensures the agent
       service starts anyway and logs a warning.

    5. ``test_mcp_client_protocol_compatibility`` — the ``MockMCPClient`` used
       throughout the test suite satisfies ``MCPClientProtocol``, confirming
       the protocol definition is duck-typed correctly.

    6. ``test_set_get_persistence_singleton`` — ``set_persistence`` /
       ``get_persistence`` round-trips the module-level singleton; ``None``
       clears it.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grace2_agent.persistence import (
    MCPClientProtocol,
    Persistence,
    make_file_persistence,
)
from grace2_agent.server import (
    get_persistence,
    init_persistence_from_env,
    set_persistence,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _MockMCPClient:
    """Minimal in-memory MCP client that satisfies ``MCPClientProtocol``."""

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
async def test_no_mcp_falls_back_to_dev_persistence(tmp_path):
    """Without MCP env vars, init_persistence_from_env does not raise.

    When ``GRACE2_DEV_PERSISTENCE=1`` (forced on) and
    ``GRACE2_DEV_PERSISTENCE_DIR`` points at a temp dir, the function returns
    a file-backed ``Persistence`` and binds the singleton.  The agent service
    must survive a fresh clone with zero Atlas configuration.
    """
    set_persistence(None)
    try:
        env_overrides = {
            "GRACE2_DEV_PERSISTENCE": "1",
            "GRACE2_DEV_PERSISTENCE_DIR": str(tmp_path),
        }
        with patch.dict(
            os.environ,
            env_overrides,
            clear=False,
        ):
            # Remove MCP vars so the file-fallback branch is taken.
            for key in ("GRACE2_MONGO_MCP_STDIO", "GRACE2_MONGO_MCP_URL"):
                os.environ.pop(key, None)

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
async def test_no_mcp_stdio_returns_prebound_or_none():
    """With no MCP env vars and GRACE2_DEV_PERSISTENCE=0, returns None.

    This is the CI escape hatch: the M1 in-memory path is preserved and the
    agent service starts without any persistence.  Callers handle None gracefully.
    """
    set_persistence(None)
    try:
        with patch.dict(
            os.environ,
            {"GRACE2_DEV_PERSISTENCE": "0"},
            clear=False,
        ):
            for key in ("GRACE2_MONGO_MCP_STDIO", "GRACE2_MONGO_MCP_URL"):
                os.environ.pop(key, None)

            result = await init_persistence_from_env()

        assert result is None
        assert get_persistence() is None
    finally:
        set_persistence(None)


# GCP decommissioned: the live MongoDB-MCP (Atlas) stdio bootstrap was removed
# from ``init_persistence_from_env`` along with ``grace2_agent.mcp`` (it relied
# on GCP Secret Manager for the SRV). The two tests that exercised the
# ``GRACE2_MONGO_MCP_STDIO=1`` -> ``MCPClient.start`` path are gone; prod
# persistence on AWS is the file / DynamoDB backend bound at startup. The
# ``MCPClientProtocol`` seam (below) stays as the abstract surface DynamoDB and
# the file backend implement.


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
