"""Live-proof driver for the ``!run`` direct tool invocation.

Drives the WS as a client (the pure-stdlib ``AgentClient`` -- no QGIS needed),
exactly as the dock does: parse the ``!run`` line CLIENT-side with the product
parser (``run_invocation.parse_run_invocation``) and send the structured
``dev-tool-invoke``; a non-``!run`` message routes to chat. Prints envelope
excerpts per turn so the four required proofs are visible.

REQUIRES the live daemon to be running the CURRENT server.py (the
``dev-tool-invoke`` handler). Validated OFFLINE first against
``tests/stub_server.py`` via ``test_client.TestCaseAndChat`` (the round-trip +
unknown-tool cases) so a driver bug never burns a live cycle.

Run (from plugin/):
    ../venvs/agent/bin/python tests/headless_run_invocation_proof.py

Env:
    TRID3NT_AGENT_URL   ws:// URL (default ws://127.0.0.1:8765)
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from plugin.net import trid3nt_client as tc  # noqa: E402
from plugin.net.run_invocation import parse_run_invocation  # noqa: E402

URL = os.environ.get("TRID3NT_AGENT_URL", "ws://127.0.0.1:8765")


def _drain_until_turn_complete(client, deadline_s=180.0):
    events = []
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        ev = client.next_event(timeout=1.0)
        if ev is None:
            continue
        events.append(ev)
        if ev.kind == "turn-complete":
            return events
    return events


def _summarize(label, events):
    print(f"\n===== {label} =====")
    for ev in events:
        if ev.kind == "tool-io":
            d = ev.data
            print(
                f"  [tool-io] tool={d.get('tool_name')} "
                f"args={str(d.get('raw_args'))[:160]} "
                f"resp={str(d.get('function_response'))[:200]} "
                f"is_error={d.get('is_error')}"
            )
        elif ev.kind == "pipeline":
            steps = ev.data.get("steps") or []
            for s in steps:
                print(f"  [pipeline] {s.tool_name} -> {s.state}")
        elif ev.kind == "session-state":
            layers = ev.data.get("layers") or []
            print(f"  [session-state] layers={[l.layer_id for l in layers]}")
        elif ev.kind == "error":
            print(
                f"  [error] code={ev.data.get('error_code')} "
                f"msg={str(ev.data.get('message'))[:200]}"
            )
        elif ev.kind == "chunk":
            if ev.data.get("delta"):
                print(f"  [chunk] {ev.data['delta'][:120]}")
        elif ev.kind == "turn-complete":
            print("  [turn-complete]")
    kinds = [e.kind for e in events]
    print(f"  kinds={kinds}")


def _run(client, line):
    inv = parse_run_invocation(line)
    if inv is None:
        # Not a !run -> chat path (routing immunity proof).
        print(f"\n(chat) {line!r} -> parse_run_invocation None -> send_chat")
        client.send_chat(line)
    elif inv.error is not None:
        print(f"\n(local error) {line!r}: {inv.error.splitlines()[0]}")
        return []
    elif inv.help:
        print(f"\n(help) {line!r} -> local usage, nothing sent")
        return []
    else:
        print(f"\n(!run) {line!r} -> dev-tool-invoke name={inv.name} args={inv.args}")
        client.send_dev_tool_invoke(inv.name, inv.args, raw_text=line)
    return _drain_until_turn_complete(client)


def main() -> int:
    client = tc.AgentClient(URL)
    try:
        uid = client.connect()
        print(f"connected user={uid} anon={client.is_anonymous} url={URL}")
        case_id = client.create_case("!run live proof")
        print(f"case={case_id}")

        # Proof 1: a tool returning envelope content (geocode).
        _summarize(
            "PROOF 1  !run geocode_location(query=...)",
            _run(client, '!run geocode_location(query="Boulder, Colorado")'),
        )
        # Proof 2: a fetch that publishes + materializes a layer.
        _summarize(
            "PROOF 2  !run fetch_dem(bbox=..., source=3dep)",
            _run(
                client,
                "!run fetch_dem(bbox=[-105.30, 39.99, -105.20, 40.05], source=\"3dep\")",
            ),
        )
        # Proof 3: the typed unknown-tool error.
        _summarize(
            "PROOF 3  !run nonsense_tool()",
            _run(client, "!run nonsense_tool()"),
        )
        # Proof 4: a mid-sentence !run flows to the LLM chat path normally.
        _summarize(
            "PROOF 4  mid-sentence !run -> chat/LLM path",
            _run(client, "briefly, what does the !run prefix do here?"),
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
