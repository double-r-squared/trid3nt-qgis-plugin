"""MongoDB MCP round-trip smoke harness.

job-0015 AC3: a real MongoDB MCP tool call from the agent process. Fetches
the SRV from Secret Manager (ADC), launches ``mongodb-mcp-server`` via
``npx`` as a stdio sidecar, completes the MCP handshake, lists tools, and
calls ``list-databases`` then ``list-collections`` against ``grace2_dev``.

This is the M1 proof that the MCP seam works end-to-end with the live
substrate. Wiring MCP tools into Gemini's function-calling loop is a
follow-up job (registry integration sits with the engine specialist), but
the connection seam this script exercises is the same one Gemini will use.

The session-records carveout (FR-AS-8) is documented in the report — no
writes are issued here.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from grace2_agent.mcp import MCPClient, fetch_srv_from_secret_manager


def _redact(s: str) -> str:
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1<redacted>\2", s)


async def main() -> int:
    print("# fetching SRV from Secret Manager (ADC)...", file=sys.stderr)
    srv = fetch_srv_from_secret_manager()
    print(f"# srv={_redact(srv)[:100]}", file=sys.stderr)

    print("# starting mongodb-mcp-server sidecar (npx)...", file=sys.stderr)
    mcp = await MCPClient.start(srv)

    try:
        print("> tools/list")
        tools = await mcp.list_tools()
        names = sorted(t.get("name", "?") for t in tools)
        print(f"< tools ({len(names)}): {names}")

        print("> tools/call name=list-databases")
        result = await mcp.call_tool("list-databases", {})
        # Surface content text without dumping the whole structure.
        content = result.get("content", [])
        for piece in content:
            if piece.get("type") == "text":
                print(f"< databases: {piece['text'][:400]}")
                break
        else:
            print(f"< (raw) {json.dumps(result)[:400]}")

        print("> tools/call name=list-collections database=grace2_dev")
        result = await mcp.call_tool(
            "list-collections", {"database": "grace2_dev"}
        )
        content = result.get("content", [])
        for piece in content:
            if piece.get("type") == "text":
                print(f"< collections: {piece['text'][:400]}")
                break
        else:
            print(f"< (raw) {json.dumps(result)[:400]}")
    finally:
        await mcp.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
