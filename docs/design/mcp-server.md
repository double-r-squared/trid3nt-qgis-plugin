# mcp_server.py -- the tool registry as an MCP surface

`trid3nt_server/mcp_server.py` is a second face on the framework the WebSocket
daemon drives. An MCP client (Claude Code, Claude Desktop, any MCP host) gets
every registered TRID3NT tool -- fetchers, QGIS processing, engine templates,
the playground -- as native MCP tools, without the daemon, the plugin, or a
Case.

It lives in the package rather than `scripts/` because it is a product
entrypoint, not a driver: it boots the registry the way `main.run()` does and
is installed as a console script.

## Running it

```
python -m trid3nt_server.mcp_server        # or: trid3nt-mcp
```

stdio only. Logging goes to stderr -- stdout is the JSON-RPC transport and
anything else written there corrupts the session.

To register it, copy `.mcp.json.example` to `.mcp.json` (or into the client's
own config), replace the absolute paths, and keep the MinIO / runs env block:
the tools read and write through the same object store the daemon uses, and
without those variables a fetcher writes nowhere.

## What it does

1. **Startup.** `load_registry()` calls the daemon's own `_import_tools_registry`
   (the spec-driven and late-bound tools only register through it), then binds
   the `qgis_process` submitter and the file-backed persistence singleton --
   the same two bindings `main.run()` performs. Without them the QGIS and
   Case-writing tools would be advertised and then fail on every call.
2. **Advertising.** Every tool is registered from a wrapper whose signature is
   synthesized from the tool's JSON Schema (the SDK derives its argument model
   from a signature, so one has to exist), then the advertised `input_schema`
   and `description` are replaced with the registry's own. The schema comes
   from `build_tool_declarations` -> `_genai_schema_to_json_schema`, the exact
   chain the Bedrock and Anthropic adapters use, so private test-injection
   kwargs are already stripped and typed containers survive (`bbox` is an array
   of numbers, not a string). Descriptions are capped at 1000 chars, matching
   the adapters.
3. **Dispatch.** `dispatch_tool` calls `TOOL_REGISTRY[name].fn` directly. A
   sync tool runs on a worker thread via `asyncio.to_thread` -- this event loop
   is the only thing serving the stdio session, and a multi-minute fetch on it
   would stall the transport.
4. **Results.** The return value goes through `summarize_tool_result`, the same
   summarizer the daemon feeds the model: layer URIs, metrics, counts,
   provenance and typed error codes survive; inline geometry collapses to a
   shape marker. A tool raising becomes a typed error envelope, never a
   traceback.

## Gates: AUTO mode, and why

There is no duplex confirmation channel on this surface -- an MCP tool call is
one request and one response, so a gate that waits for approval would wait
forever.

- **Input gates** run in AUTO mode (`TRID3NT_INPUT_GATE_MODE`, default `auto`):
  resolved inputs are used with their labels, and the `synthetic_inputs` /
  `assumptions_summary` provenance rides the envelope so the caller can see
  which values were demo defaults. Law 9 still holds: a physics-consequential
  value with no real source refuses rather than being invented. Setting the
  mode to `user_gated` does not gain an approval channel here -- with no live
  emitter the gate fails open, exactly as it does for any headless direct call.
- **The payload gate** is enforced in `payload_verdict`, reusing the declared
  estimator seam (`payload_mb_estimator_name` + the shared thresholds). Under
  the warning threshold (25 MB) the call proceeds; between the threshold and
  the hard cap (250 MB) it proceeds and the envelope carries
  `payload_warning_mb`; over the hard cap it is REFUSED with
  `error_code: "PAYLOAD_HARD_CAP"` naming the estimate, the cap and what to
  narrow. Both thresholds keep their env overrides.

## V1 non-goals

- **No streamed emission.** The daemon's emit-on-fetch and emit-on-solve seams
  push layers onto a live map through a WebSocket session. Here a tool returns
  the layer URI and the client does what it likes with it.
- **No chart cards.** A chart-emitting tool returns its compact chart summary,
  not the Vega-Lite spec.
- **No WS sessions, no Case state.** No session id, no chat history, no
  pipeline cards, no cancellation channel.
- **No HTTP transport.** stdio only; a network-reachable transport is a
  security surface this version does not open.

## Tests

`tests/test_mcp_server.py` drives the real server object through the SDK's
in-process `mcp.Client`: startup and listing against a stub registry, a tool
count matching the REAL registry, a fetcher round trip, sync-tool offload
proven by thread identity, the hard-cap refusal (and that the tool body never
ran), the between-thresholds label, and an oversized result reduced to a URI
plus a shape marker.
