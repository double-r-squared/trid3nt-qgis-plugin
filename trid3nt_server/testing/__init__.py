"""Drivers for the product's own WS protocol - a scripted client, not a mock.

A live test declares what to invoke, how to answer the gates, and what must be
true of the answer; :mod:`live_run` walks the socket the plugin walks. Drivers
are product code, so this lives beside the server rather than in the test tree -
``scripts/`` drivers and the offline suite both import it.
"""

from __future__ import annotations

from .live_run import GateAnswers, LiveRun, RunEvidence, drive, run_live
from .ws_client import (
    BLOCKING_EVENTS,
    WS_URL,
    approve_confirmation,
    create_case,
    delete_case,
    handshake,
    mk,
    parse_tool_status,
)

__all__ = [
    "BLOCKING_EVENTS", "GateAnswers", "LiveRun", "RunEvidence", "WS_URL",
    "approve_confirmation", "create_case", "delete_case", "drive", "handshake",
    "mk", "parse_tool_status", "run_live",
]
