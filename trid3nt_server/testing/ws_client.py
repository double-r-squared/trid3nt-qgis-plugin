"""The WS protocol primitives a scripted client needs: envelopes, handshake, cases.

One implementation of the wire shapes every driver was re-deriving. Nothing here
knows about a particular tool - it is the socket, not the test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from trid3nt_contracts import new_ulid

logger = logging.getLogger("trid3nt_server.testing.ws_client")

__all__ = [
    "BLOCKING_EVENTS",
    "WS_URL",
    "approve_confirmation",
    "create_case",
    "delete_case",
    "handshake",
    "mk",
    "parse_tool_status",
]

WS_URL = "ws://127.0.0.1:8765/ws"

#: Events that PAUSE a turn waiting for something this client does not answer.
#: A driver that declares no answer for one of these has hit a dead end, and
#: saying so beats timing out.
BLOCKING_EVENTS = frozenset({
    "spatial-input-request", "disambiguation-request",
    "clarification-request", "recovery-choice",
})


def mk(type_: str, session_id: str, payload: dict[str, Any],
       case_id: str | None = None) -> str:
    """One outbound envelope, serialized."""
    return json.dumps({
        "type": type_,
        "id": new_ulid(),
        "ts": "2026-08-07T00:00:00Z",
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    })


async def handshake(ws: Any, session_id: str) -> None:
    """Authenticate and resume, leaving the socket at a delivered session-state."""
    await ws.send(mk("auth-token", session_id, {"token": ""}))
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    assert ack["type"] == "auth-ack", f"expected auth-ack, got {ack['type']}"
    await ws.send(mk("session-resume", session_id, {"case_id": None}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if msg["type"] == "session-state":
            return


async def create_case(ws: Any, session_id: str, title: str) -> str:
    """Open a fresh Case and return its id."""
    await ws.send(mk("case-command", session_id,
                     {"command": "create", "args": {"title": title}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if msg["type"] == "case-open":
            state = msg["payload"].get("session_state")
            if state:
                return state["case"]["case_id"]


async def delete_case(ws: Any, session_id: str, case_id: str | None) -> None:
    """Soft-delete a Case. Best-effort: a throwaway proof Case cleans itself up."""
    if not case_id:
        return
    try:
        await ws.send(mk("case-command", session_id,
                         {"command": "delete", "case_id": case_id},
                         case_id=case_id))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            if json.loads(raw)["type"] in ("case-list", "error"):
                return
    except Exception:  # noqa: BLE001 - cleanup never fails the run it cleans up after
        logger.exception("delete_case failed for case_id=%s", case_id)


async def approve_confirmation(ws: Any, session_id: str, msg: dict[str, Any]) -> None:
    """Approve a plain confirmation-request."""
    await ws.send(mk("confirm-response", session_id,
                     {"request_id": msg["payload"].get("request_id"),
                      "approved": True}))


def parse_tool_status(payload: dict[str, Any]) -> str | None:
    """The tool's own ``status`` out of a tool-io payload, or the error flag."""
    try:
        obj = json.loads(payload.get("function_response") or "")
    except (json.JSONDecodeError, ValueError):
        return "error" if payload.get("is_error") else None
    if isinstance(obj, dict) and isinstance(obj.get("status"), str):
        return obj["status"]
    return "error" if payload.get("is_error") else None
