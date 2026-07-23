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
sync.
To extend the harness (write a tool / add an engine), see
`docs/authoring/writing-a-tool.md` and `docs/authoring/adding-an-engine.md`.

## Install paths

There are three ways to end up running TRID3NT, depending on which machine you're
setting up:

| Path | Machine | Steps | Needs QGIS? | Needs git/venv/docker? |
|------|---------|-------|-------------|-------------------------|
| **Daemon-only** | PC / headless box that runs the server | `git clone` + `make setup && make up` | No -- `qgis-plugin/` is inert, nothing there is ever loaded | Yes |
| **Client-only** | laptop that just wants the QGIS dock | `make plugin-zip` on *any* checkout produces `dist/trid3nt-plugin-<version>.zip` -- copy that one file over, then QGIS: **Plugins > Install from ZIP**, then **Settings > Server URL** | Yes | No -- no clone, no venv, no server on this machine |
| **Both** | one dev machine | `git clone` + `make setup && make up` + `make plugin` (syncs into your QGIS profile; reload in QGIS) | Yes | Yes |

Full walkthrough per path (prerequisites, troubleshooting): [docs/site/install.md](docs/site/install.md).

### Daemon-only / Both

```sh
make setup     # one-time: create .env.local, fetch binaries (mf6/minio/mc), build the agent venv
#              then edit .env.local -- set your LLM endpoint + key (make env writes a starter .env.local)
make up        # start the local stack: minio (:9000) + agent (:8765 WS / :8766 HTTP)
make plugin    # (Both only) install the QGIS plugin into your QGIS profile
#              then in QGIS: enable the TRID3NT plugin (or Plugin Reloader to reload)
make status    # health-check the services
```

Prerequisites: Linux x86_64, Python 3.12, [uv](https://astral.sh/uv), and **Docker**
(for the container-based solvers). Your user must be in the `docker` group (log
out/in after being added, or wrap docker-touching commands in `sg docker -c
'...'`). An LLM endpoint: either [ollama](https://ollama.com) local, or an API key
for OpenAI/OpenRouter/Groq/etc.

### Client-only

```sh
make plugin-zip   # run on any checkout (repo clone not required on the client itself)
#                 writes dist/trid3nt-plugin-<version>.zip
```

Copy `dist/trid3nt-plugin-<version>.zip` to the client machine, then in QGIS:
**Plugins > Manage and Install Plugins > Install from ZIP**. Only prerequisite:
QGIS 3.28+. See [qgis-plugin/README.md](qgis-plugin/README.md) for the full
client walkthrough (Server URL / token settings, test suite).

### Remote daemon (tailnet)

A client machine points its plugin's **Server URL** at the daemon's [Tailscale](https://tailscale.com)
address instead of loopback, e.g. `ws://100.x.x.x:8765/ws` -- everything else
(MinIO, the HTTP catalog) is advertised automatically by the server on connect. Set
`TRID3NT_ACCESS_TOKEN` on the daemon for a shared-secret lock; see
[Remote daemon access (tailnet)](docs/site/configuration.md#remote-daemon-access-tailnet)
for the full picture.

### LLM endpoint (.env.local)

`make setup` writes a starter `.env.local` (via `make env`). Set the provider:
- Local Ollama: `MODEL_PROVIDER=openai`, `TRID3NT_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`,
  `TRID3NT_OPENAI_MODEL=<your ollama model>`, `TRID3NT_OPENAI_API_KEY=not-needed`.
- OpenRouter / OpenAI / Groq: set `TRID3NT_OPENAI_BASE_URL` + `TRID3NT_OPENAI_MODEL` +
  `TRID3NT_OPENAI_API_KEY`. Helper: `scripts/use_openrouter.sh <KEY> [model]`.
The model can also be switched live from the plugin's Settings (no restart).

### Engine backends (.env.local)

- `TRID3NT_MODFLOW_LOCAL=1` -- MODFLOW runs against the local `mf6` binary (`TRID3NT_MF6_BIN`).
- `TRID3NT_SOLVER_BACKEND=local-docker` -- container solvers (SFINCS/TELEMAC/...) run via
  local docker. Set `TRID3NT_RUNS_DIR=<repo>/data/runs` (the host rundir mounted at `/data`).
  Pull an engine image once, e.g. `sg docker -c 'docker pull deltares/sfincs-cpu:sfincs-v2.3.3'`.
  These two are independent (MODFLOW checks `TRID3NT_MODFLOW_LOCAL` first). Start the agent
  inside the docker group so it can reach the socket: `sg docker -c 'make agent'`.

## Service URLs

| Service        | URL                          | Notes                          |
|----------------|------------------------------|--------------------------------|
| Agent WS       | ws://localhost:8765          | plugin/web connect here        |
| Agent HTTP     | http://localhost:8766        | tool catalog + telemetry       |
| MinIO API      | http://localhost:9000        | S3-compatible object storage   |
| MinIO Console  | http://localhost:9001        | web UI (user: trid3nt)         |
| Ollama         | http://localhost:11434       | optional local LLM             |

## Repo layout

```
qgis-plugin/trid3nt/   the QGIS plugin (net/ ui/ render/ case/ + plugin.py)
qgis-plugin/tests/     plugin test harnesses + headless E2E drivers
server/                the server (WS + tool dispatch + turn loop + persistence)
contracts/             shared pydantic contracts (trid3nt-contracts package)
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
