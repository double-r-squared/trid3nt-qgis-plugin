# server/ -- TRID3NT agent service (WebSocket + tool dispatch)

The `trid3nt_server` package: a Python application that serves the
Appendix-A WebSocket protocol, hosts the tool registry (native Python
FunctionTools -- fetchers, discovery, processing, hazard-simulation
workflows), runs the multi-turn generation loop against a **pluggable LLM
provider** (local Ollama or any OpenAI-compatible endpoint by default;
Bedrock retained as an option), streams replies, propagates cancellation,
and enforces the determinism boundary (Invariant 1) and confirmation-before-
consequence hooks (Invariant 9).

> **Provider note.** This service began on Vertex AI / Gemini (`adapter.py`),
> then gained an AWS Bedrock path (`bedrock_adapter.py`). TRID3NT Local's
> live default is `MODEL_PROVIDER=openai` -> `openai_adapter.py`, which
> speaks the OpenAI-compatible `chat/completions` streaming API against
> Ollama (default), vLLM, llama.cpp, LM Studio, OpenAI, Groq, DeepSeek, or
> OpenRouter. `bedrock_adapter.py` is kept as one option of the same
> pluggable-LLM seam (`MODEL_PROVIDER=bedrock`) for anyone pointing this repo
> at a cloud account; `MODEL_PROVIDER=scripted` (aliases `replay`/`fake`)
> replays a canned transcript for zero-cost deterministic tests. The Vertex
> generation path in `adapter.py` is retired; only the provider-neutral
> `google.genai.types` shapes it holds are still used -- every adapter yields
> the same `StreamEvent` union, so `server.py`'s dispatch loop, validator,
> emitter, and UI are untouched by the provider choice. See
> [Configuration](../docs/site/configuration.md#llm-provider) for the env vars.

## Layout

```
server/
├── pyproject.toml            trid3nt-server package, console script `trid3nt-server`
├── README.md                 (this file)
├── wheels/                   PyPI-absent deps committed as wheels (pfdf); every
│                              install path uses --find-links server/wheels
├── src/trid3nt_server/
│   ├── __init__.py
│   ├── main.py                entry point (`trid3nt-server` -> run())
│   ├── server.py               Appendix-A WebSocket server (asyncio + websockets)
│   ├── openai_adapter.py       OpenAI-compatible chat/completions loop (local default)
│   ├── bedrock_adapter.py      AWS Bedrock Converse loop (optional provider) + cachePoint
│   ├── scripted_adapter.py     MODEL_PROVIDER=scripted -- canned-transcript replay, no LLM call
│   ├── adapter.py               StreamEvent union + provider-neutral genai-types helpers
│   ├── persistence.py           Cases/sessions/users persistence -- FilePersistence only
│   │                            (the DynamoDB backend was removed; local-only build)
│   ├── secrets_handler.py       per-Case secret vault -- one local file-vault
│   │                            (`file-vault://...`; GCP/AWS cloud vault backends removed)
│   ├── auth_handshake.py        local WS connect handshake -- anonymous users + the
│   │                            fixed local single-user id; no IdP, no token verification
│   ├── tools/                   the tool registry + atomic/composer/engine tools
│   │   ├── catalog.py            registry + docstring-metadata enforcement
│   │   ├── cache.py              cached-fetch storage (S3-compatible; MinIO locally)
│   │   ├── discovery/            catalog/spatial-function/QGIS-processing discovery + tool retrieval
│   │   ├── fetchers/             data-source fetch tools
│   │   ├── meta/                 code_exec, probe_point, case export/import, web_fetch, ...
│   │   ├── processing/           compute/clip/aggregate/analysis tools
│   │   └── simulation/
│   │       └── solver.py         run_solver / wait_for_completion -- local-docker /
│   │                              local-exec dispatcher (`solver_backend()` is
│   │                              local-only; the AWS Batch arm is decommissioned)
│   └── workflows/                per-engine build/run/postprocess modules (MODFLOW,
│                                  SFINCS, SWMM, TELEMAC, GeoClaw, SWAN, OpenQuake,
│                                  Landlab, ELMFIRE, ...) + the model_*.py hazard scenarios
```

## Running locally

From the repo root (the `trid3nt-local` root, one level up from here):

```bash
make agent
# or, inside the docker group so the agent can reach the docker socket
# for the container-backed engines (recommended):
sg docker -c 'make agent'
# then in another shell:
python scripts/ws_smoke.py   # WS chat smoke against the running daemon
```

`make agent` runs `scripts/start_agent.sh`, which loads `.env.local`, launches
`trid3nt_server.main` (the `trid3nt-server` console script) via the venv at
`venvs/agent/` (built by `make venv` / `make setup`), on WS `:8765` / HTTP
`:8766` (override with `TRID3NT_AGENT_PORT` / `TRID3NT_AGENT_HTTP_PORT`),
logs to `logs/agent.log`, and writes a PID to `run/agent.pid`. See
[Install](../docs/site/install.md) for first-time setup and
[Configuration](../docs/site/configuration.md) for the full `.env.local`
reference.

## Scope

- `openai_adapter.py` round-trip (streamed `agent-message-chunk` deltas,
  terminal `done: true` frame) against Ollama or any OpenAI-compatible
  endpoint; `bedrock_adapter.py` carries the same `cachePoint` prompt-caching
  behavior on Anthropic models when `MODEL_PROVIDER=bedrock` is selected
  instead.
- `cancel` interrupts in-flight generation and the in-flight solver run
  (local docker container / subprocess), and emits cancelled `pipeline-state`
  (Invariant 8).
- Persistence (Cases/sessions/users/secrets-refs/audit) through the
  `Persistence` seam: **FilePersistence only** (`TRID3NT_DEV_PERSISTENCE_DIR`)
  -- both the GCP-era MongoDB MCP path and the AWS DynamoDB backend were
  removed for the local-only build (Decision 0006).
- Heavy/sandboxed compute runs locally: SFINCS via `local-docker`, MODFLOW
  and the Python sandbox against the local `mf6` binary / subprocess,
  GeoClaw/TELEMAC/ELMFIRE via locally-built docker images -- see
  [Engines](../docs/site/engines.md) for the per-engine matrix. No AWS Batch,
  no cloud queue.
- Every wire message validated via `trid3nt_contracts` -- no hand-rolled JSON.

## Deploy

There is no separate deploy step for this repo's server -- edit `server/`,
then restart the agent (`make agent` from the repo root; the venv installs
`server/` editable, so a restart picks the change up). See the root
[README's Deploy seams](../README.md#deploy-seams-when-you-change-code)
section for the other two seams (QGIS plugin, worker images).
