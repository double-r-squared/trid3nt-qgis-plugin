"""job-0203 (Wave 4.11 M4): Mode-2 candidate audit routes through MongoDB MCP.

The bespoke JSONL writer (``mode2_classifier.append_audit_log``) was deleted
(remove-don't-shim). The server's ``_maybe_emit_mode2_candidate`` now appends
the candidate to the ``audit_log`` collection (D.15) via
``Persistence.append_audit`` — verified here end-to-end against the
file-backed dev substrate and the unbound-Persistence degrade path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from trid3nt_server.persistence import (
    AUDIT_COLLECTION,
    FileMCPClient,
    Persistence,
)
from trid3nt_server.server import (
    SessionState,
    _maybe_emit_mode2_candidate,
    set_persistence,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


MODE2_PAGE_RESULT = {
    "url": "https://data.example.gov/dataset/spill-monitoring",
    "title": "Spill Monitoring Dataset",
    "content": (
        '<script type="application/ld+json">{"@type":"Dataset"}</script>'
        '<a href="/data.csv">Download CSV</a>'
    ),
}


def _run_emission(persistence: Persistence | None) -> FakeWebSocket:
    set_persistence(persistence)
    try:
        ws = FakeWebSocket()
        state = SessionState(session_id="01SESSIONULID0000000000003")
        asyncio.run(_maybe_emit_mode2_candidate(ws, state, MODE2_PAGE_RESULT))
        return ws
    finally:
        set_persistence(None)


def test_mode2_candidate_audit_lands_in_audit_log_collection(tmp_path):
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    ws = _run_emission(p)

    # The chat envelope still goes out.
    assert any(m["type"] == "mode2-candidate" for m in ws.sent)

    # And the audit event landed in the audit_log collection via MCP.
    async def read_audit():
        raw = await p._mcp.call_tool(
            "find",
            {
                "database": p._db,
                "collection": AUDIT_COLLECTION,
                "filter": {"event_type": "mode2-candidate"},
            },
        )
        return raw["documents"]

    docs = asyncio.run(read_audit())
    assert len(docs) == 1
    event = docs[0]
    assert event["event_type"] == "mode2-candidate"
    assert event["payload"]["session_id"] == "01SESSIONULID0000000000003"
    assert event["payload"]["candidate"]["domain"] == "data.example.gov"
    assert "ts" in event


def test_mode2_emission_survives_unbound_persistence():
    """No Persistence bound (explicit CI path) → envelope still emitted,
    audit logged-and-dropped, nothing raises."""
    ws = _run_emission(None)
    assert any(m["type"] == "mode2-candidate" for m in ws.sent)


def test_mode2_emission_survives_audit_write_failure(tmp_path):
    """A failing MCP audit write must never break the envelope path."""

    class ExplodingMCP:
        async def call_tool(self, name, arguments=None):
            raise RuntimeError("atlas down")

    p = Persistence(ExplodingMCP())
    ws = _run_emission(p)
    assert any(m["type"] == "mode2-candidate" for m in ws.sent)
