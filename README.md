# TRID3NT Local

TRID3NT: an AI workbench for multi-hazard geospatial modeling, built as a
**QGIS plugin + its local server**, running entirely on your own machine. This
repo IS the product: the plugin (the only client) and the server it drives,
one clone = a working end-to-end setup.

- **QGIS plugin** - the client, deeply integrated with QGIS
- **Pluggable LLM** via any OpenAI-compatible endpoint: local (Ollama, vLLM,
  llama.cpp, LM Studio) or cloud (OpenAI, Groq, DeepSeek, OpenRouter, ...)
- **Real solvers run locally**: MODFLOW 6, TELEMAC, SFINCS, SWMM, and more via
  local `docker` containers; MODFLOW runs against a local `mf6` binary
- File-based persistence + local tile rendering -- no cloud account required

The server (`server/`), contracts (`contracts/`) and engine workers
(`services/workers/`) are first-class code in THIS repo - there is no upstream
sync. (History note: they were vendored from the GRACE-2 monorepo until
2026-07-21; GRACE-2 remains the home of the separate web + cloud products.)
To extend the harness (write a tool / add an engine), see
`docs/authoring/writing-a-tool.md` and `docs/authoring/adding-an-engine.md`.

## Quickstart

Prerequisites: Linux x86_64, Python 3.12, Node 20+, [uv](https://astral.sh/uv),
and **Docker** (for the container-based solvers). Your user must be in the
`docker` group (log out/in after being added, or wrap docker-touching commands in
`sg docker -c '...'`). An LLM endpoint: either [ollama](https://ollama.com) local,
or an API key for OpenAI/OpenRouter/Groq/etc.

```sh
make setup     # one-time: create .env.local, fetch binaries (mf6/minio/mc), build the agent venv
#              then edit .env.local -- set your LLM endpoint + key (see .env.openrouter.example)
make up        # start the local stack: minio (:9000) + titiler (:8080) + agent (:8765 WS / :8766 HTTP)
make plugin    # install the QGIS plugin into your QGIS profile
#              then in QGIS: enable the TRID3NT plugin (or Plugin Reloader to reload)
make status    # health-check the services
```

Optional browser client (also reachable from a phone/laptop on your LAN/tailnet):

```sh
```

Stop everything with `make down`. Run `make help` for the target list.

### LLM endpoint (.env.local)

`make setup` copies `.env.openrouter.example` to `.env.local`. Set the provider:
- Local Ollama: `MODEL_PROVIDER=openai`, `GRACE2_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`,
  `GRACE2_OPENAI_MODEL=<your ollama model>`, `GRACE2_OPENAI_API_KEY=not-needed`.
- OpenRouter / OpenAI / Groq: set `GRACE2_OPENAI_BASE_URL` + `GRACE2_OPENAI_MODEL` +
  `GRACE2_OPENAI_API_KEY`. Helper: `scripts/use_openrouter.sh <KEY> [model]`.
The model can also be switched live from the plugin's Settings (no restart).

### Engine backends (.env.local)

- `GRACE2_MODFLOW_LOCAL=1` -- MODFLOW runs against the local `mf6` binary (`GRACE2_MF6_BIN`).
- `GRACE2_SOLVER_BACKEND=local-docker` -- container solvers (SFINCS/TELEMAC/...) run via
  local docker. Set `GRACE2_RUNS_DIR=<repo>/data/runs` (the host rundir mounted at `/data`).
  Pull an engine image once, e.g. `sg docker -c 'docker pull deltares/sfincs-cpu:sfincs-v2.3.3'`.
  These two are independent (MODFLOW checks `GRACE2_MODFLOW_LOCAL` first). Start the agent
  inside the docker group so it can reach the socket: `sg docker -c 'make agent'`.

## Service URLs

| Service        | URL                          | Notes                          |
|----------------|------------------------------|--------------------------------|
| Agent WS       | ws://localhost:8765          | plugin/web connect here        |
| Agent HTTP     | http://localhost:8766        | tool catalog + telemetry       |
| TiTiler        | http://localhost:8080        | raster tile server             |
| MinIO API      | http://localhost:9000        | S3-compatible object storage   |
| MinIO Console  | http://localhost:9001        | web UI (user: trid3nt)         |
| Ollama         | http://localhost:11434       | optional local LLM             |

## Repo layout

```
qgis-plugin/trid3nt/   the QGIS plugin (net/ ui/ render/ case/ + plugin.py)
qgis-plugin/tests/     plugin test harnesses + headless E2E drivers
server/                the server (WS + tool dispatch + turn loop + persistence)
contracts/             shared pydantic contracts (grace2-contracts package)
services/workers/      engine workers (mf6, telemac, sfincs, ... docker or exec)
scripts/               run + deploy scripts (start_*, install_plugin, build_*_image, ...)
bin/ venvs/ data/ logs/ run/   gitignored runtime (binaries, venvs, storage, logs, pids)
```

## Deploy seams (when you change code)

Three independent seams -- a git commit alone deploys none of them:
- **Server code**: edit `server/`, then restart the agent (`make agent`) - the
  venv installs `server/` editable, so a restart picks the change up.
- **QGIS plugin**: `make plugin` (rsyncs into the QGIS profile), then reload in QGIS.
- **Worker image**: `scripts/build_<engine>_image.sh` (e.g. `build_telemac_image.sh`).

## Data directories (gitignored)

`./bin/` binaries · `./venvs/` Python envs · `./data/minio/` object storage ·
`./data/persistence/` agent cases/layers · `./logs/` · `./run/` PID files ·
`./cache/` HTTP cache.
