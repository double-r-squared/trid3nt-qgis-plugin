# ADR 0302 -- the tool registry as an MCP surface (v1, stdio)

Status: LANDED (entrypoint + 10 offline tests + live stdio smoke).
Date: 2026-08-22. Design page: `docs/design/mcp-server.md`.

## Context

The registry is reachable exactly one way: through the WebSocket daemon, driven
by an LLM adapter, inside a Case, with the QGIS plugin on the other end. Every
other consumer -- an MCP host, another agent, a person at a terminal -- has to
stand up that whole stack to fetch a DEM.

## Decision

`trid3nt_server/mcp_server.py`: an MCP stdio server that advertises every
registered tool and dispatches straight to its registry function.

Four decisions worth recording:

1. **Package module, not `scripts/`.** `scripts/` holds drivers, smokes and
   image builds. This is a product entrypoint that boots the registry the way
   `main.run()` does, so it lives in the package and ships as the `trid3nt-mcp`
   console script.
2. **`MCPServer`, not FastMCP.** In `mcp` 2.x FastMCP is gone; `MCPServer` is
   its successor and takes a pre-built tool list. Tools are registered from a
   wrapper whose signature is synthesized from the tool's JSON Schema (the SDK
   derives its argument model from a signature, so one must exist), then the
   advertised schema and description are replaced with the registry's own --
   from `build_tool_declarations` -> `_genai_schema_to_json_schema`, the same
   chain the LLM adapters use. Reading raw signatures instead would have leaked
   the private test-injection kwargs that chain strips.
3. **Gates run AUTO, and the payload hard cap REFUSES.** An MCP call is one
   request and one response; a gate waiting for approval would wait forever.
   Input gates use labeled defaults (law 9 still refuses invented physics), and
   a payload over the hard cap returns a typed `PAYLOAD_HARD_CAP` error naming
   the estimate and what to narrow, rather than silently transferring 250 MB or
   hanging on a confirmation that cannot come. No new gate machinery -- the
   declared estimator seam and its thresholds are reused as-is.
4. **Results are the summarized envelope.** `summarize_tool_result` is what the
   daemon already feeds the model, so this surface behaves identically: layer
   URIs, metrics and provenance survive, inline geometry collapses to a shape
   marker, and exceptions become typed error envelopes. A live smoke returned a
   real 3DEP DEM fetch as a 260-char envelope carrying its `s3://` URI.

## Consequences

- Sync tools ALWAYS run on a worker thread here. The daemon's staged
  `TRID3NT_SYNC_TOOL_OFFLOAD` rollout exists because offload interacts with the
  emitter; this surface has no emitter, and the stdio loop is the only thing
  serving the session, so there is nothing to stage.
- Startup binds the qgis_process submitter and the persistence singleton (the
  same two bindings `main.run()` makes). Without them the QGIS tools advertise
  and then fail -- proven by the first stdio smoke, which returned exactly that
  honest error before the bindings were added.
- `mcp>=2,<3` joins the core dependency list.
- What v1 does NOT do: streamed emission, chart cards, WS sessions or Case
  state, and any non-stdio transport.
