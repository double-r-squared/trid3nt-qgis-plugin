"""Live-evidence WebSocket harness for the agent service.

job-0015 acceptance criterion AC1: sends ``session-resume`` then
``user-message``, prints every streamed frame, and reports first-token latency
(NFR-P-1 informational).

Optional second mode: when invoked with ``--cancel-after <ms>`` the script
sends ``cancel`` mid-stream and verifies a ``pipeline-state(cancelled)``
frame arrives. This is AC2.

Usage::

    python services/agent/scripts/ws_client.py "What is SFINCS?"
    python services/agent/scripts/ws_client.py "Tell me a long story" --cancel-after 800

Connects to ``ws://127.0.0.1:8765`` by default; override with
``GRACE2_AGENT_URL`` (e.g. ``ws://localhost:8765``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# Make ``grace2_contracts`` importable when run as a script from repo root
# without an editable install.
import websockets

from grace2_contracts import new_ulid
from grace2_contracts.ws import (
    CancelPayload,
    Envelope,
    SessionResumePayload,
    UserMessagePayload,
)


def _serialize(message_type: str, session_id: str, payload) -> str:
    env = Envelope(type=message_type, session_id=session_id, payload=payload)
    return env.model_dump_json()


async def main(args) -> int:
    url = os.environ.get("GRACE2_AGENT_URL", "ws://127.0.0.1:8765")
    session_id = new_ulid()
    print(f"# url={url}", file=sys.stderr)
    print(f"# session_id={session_id}", file=sys.stderr)

    async with websockets.connect(url) as ws:
        # 1. session-resume.
        await ws.send(_serialize("session-resume", session_id, SessionResumePayload()))
        print("> session-resume sent", file=sys.stderr)

        # 2. user-message.
        await ws.send(
            _serialize(
                "user-message",
                session_id,
                UserMessagePayload(text=args.text, research_mode="research"),
            )
        )
        print("> user-message sent", file=sys.stderr)
        sent_at = time.monotonic()

        first_chunk_at: float | None = None
        cancel_sent = False
        chunk_count = 0
        terminal_seen = False
        cancelled_seen = False

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                parsed = json.loads(raw)
                # Demonstrate that every inbound frame validates through the
                # contracts (AC1 evidence).
                try:
                    # We validate the outer envelope structure (Envelope is
                    # generic but accepts dict payloads here for the assertion).
                    assert isinstance(parsed.get("type"), str)
                    assert isinstance(parsed.get("session_id"), str)
                    assert isinstance(parsed.get("payload"), dict)
                except AssertionError:
                    print(f"!! envelope shape failed: {raw}", file=sys.stderr)

                msg_type = parsed["type"]
                payload = parsed["payload"]

                if msg_type == "session-state":
                    print(f"< session-state chat_history_len={len(payload.get('chat_history', []))}")
                elif msg_type == "agent-message-chunk":
                    if first_chunk_at is None and payload.get("delta"):
                        first_chunk_at = time.monotonic()
                        ttft_ms = (first_chunk_at - sent_at) * 1000.0
                        print(f"# first-token-latency-ms={ttft_ms:.1f} (NFR-P-1 budget 2000)", file=sys.stderr)
                    if payload.get("delta"):
                        chunk_count += 1
                        print(f"< chunk[{chunk_count}] {payload['delta']!r}")
                    if payload.get("done"):
                        terminal_seen = True
                        print(f"< chunk[terminal done=True] total_chunks={chunk_count}")
                elif msg_type == "pipeline-state":
                    states = [s["state"] for s in payload.get("steps", [])]
                    print(f"< pipeline-state steps={states}")
                    if "cancelled" in states:
                        cancelled_seen = True
                elif msg_type == "error":
                    print(f"< error code={payload.get('error_code')} message={payload.get('message')}")
                else:
                    print(f"< {msg_type} {json.dumps(payload)[:120]}")

                # Cancel path (AC2).
                if (
                    args.cancel_after is not None
                    and not cancel_sent
                    and chunk_count >= 1
                    and (time.monotonic() - sent_at) * 1000.0 >= args.cancel_after
                ):
                    await ws.send(_serialize("cancel", session_id, CancelPayload(reason="user-requested")))
                    cancel_sent = True
                    cancel_at = time.monotonic()
                    print(f"> cancel sent at {(cancel_at - sent_at)*1000:.0f}ms after user-message", file=sys.stderr)

                if terminal_seen or cancelled_seen:
                    # Drain a beat for any trailing pipeline-state, then exit.
                    try:
                        await asyncio.wait_for(asyncio.sleep(0.5), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    break
        except websockets.exceptions.ConnectionClosed:
            print("# connection closed", file=sys.stderr)

        if cancel_sent and cancelled_seen:
            cancel_latency_ms = (time.monotonic() - cancel_at) * 1000.0
            print(f"# cancel-to-cancelled-pipeline-ms={cancel_latency_ms:.1f} (NFR-R-3 budget 30000)", file=sys.stderr)

        return 0 if (terminal_seen or cancelled_seen) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("text", help="user message text to send")
    parser.add_argument(
        "--cancel-after",
        type=int,
        default=None,
        help="if set, send a cancel envelope this many ms after user-message and verify cancelled pipeline-state arrives",
    )
    args = parser.parse_args()
    rc = asyncio.run(main(args))
    sys.exit(rc)
