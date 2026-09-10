# `trid3nt_server` -- the agent service (WebSocket + tool dispatch)

The Python application that serves the plugin's WebSocket protocol, hosts the
tool registry (fetchers, search, processing, display, meta, and the engine
workflows), runs the multi-turn generation loop against a **pluggable LLM
provider**, streams replies, propagates cancellation, and enforces the
determinism boundary and the confirmation-before-consequence gates.

The package's own maps are the checkable ones and this page does not restate
them: [`trid3nt_server/README.md`](../../trid3nt_server/README.md) maps the
package, and each subpackage carries its own README (the daemon core is
[`trid3nt_server/server/README.md`](../../trid3nt_server/server/README.md), the
providers are
[`trid3nt_server/adapters/README.md`](../../trid3nt_server/adapters/README.md)).

## The provider seam

The live default is `MODEL_PROVIDER=openai` -> `adapters/openai_adapter.py`,
which speaks the OpenAI-compatible `chat/completions` streaming API against
Ollama (default), vLLM, llama.cpp, LM Studio, OpenAI, Groq, DeepSeek, or
OpenRouter. `MODEL_PROVIDER=anthropic` selects the first-party Messages API
path; `MODEL_PROVIDER=scripted` replays a recorded cassette for zero-cost
deterministic tests. Provider selection itself lives in
`adapters/model_selection.py` and is read at call time, so no adapter import
decides it.

`google.genai.types` is the internal history and tool-declaration shape, and a
provider adapter is the only place a provider's own nouns appear. Every adapter
yields the same streamed events, so the turn loop, the validator, the emitter
and the plugin are untouched by the provider choice. See
[Configuration](../site/configuration.md#llm-provider) for the env vars.

## Running locally

From the repo root:

```bash
make agent
# or, inside the docker group so the agent can reach the docker socket
# for the container-backed engine (recommended):
sg docker -c 'make agent'
# then in another shell:
python scripts/instruments/ws_smoke.py   # WS chat smoke against the running daemon
```

`make agent` runs `scripts/start_agent.sh`, which loads `.env.local`, launches
`trid3nt_server.main` (the `trid3nt-server` console script) via the venv at
`venvs/agent/` (built by `make venv` / `make setup`), on WS `:8765` / HTTP
`:8766` (override with `TRID3NT_AGENT_PORT` / `TRID3NT_AGENT_HTTP_PORT`),
logs to `logs/agent.log`, and writes a PID to `run/agent.pid`. See
[Install](../site/install.md) for first-time setup and
[Configuration](../site/configuration.md) for the full `.env.local` reference.

## Scope

- The adapter round-trip (streamed `agent-message-chunk` deltas, a terminal
  `done: true` frame) against any of the providers above; the Anthropic path
  carries prompt caching through `cache_control` breakpoints.
- `cancel` interrupts in-flight generation and the in-flight solver run, and
  emits a cancelled `pipeline-state`.
- Persistence (cases / sessions / users / secret refs / audit) through the
  `Persistence` seam: **file persistence only**
  (`TRID3NT_DEV_PERSISTENCE_DIR`); there is no cloud backend in this build.
- Heavy or sandboxed compute runs locally: TELEMAC and the mesher through
  locally built docker images, the `code_exec` box in a network-none container
  (`sandbox/`) -- see [Engines](../site/engines.md) for what a shipped engine
  means here. No cloud queue.
- Every wire message validated through `trid3nt_contracts` -- no hand-rolled
  JSON.

## Deploy

There is no separate deploy step for the server: edit `trid3nt_server/`, then
restart the agent (`make agent` from the repo root; the venv installs the
package editable, so a restart picks the change up). See the root
[README's deploy seams](../../README.md#deploy-seams) for the other two seams
(the QGIS plugin, the worker images).
