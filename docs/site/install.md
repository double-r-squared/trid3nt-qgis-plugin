# TRID3NT Local -- Install

From-scratch setup on a clean Linux box. Everything lands inside the repo directory
(`bin/`, `venvs/`, `data/`, `logs/`, `run/` are gitignored); nothing is installed system-wide
except Docker and Ollama.

## Prerequisites

| Requirement | Why |
|-------------|-----|
| Linux x86_64 | binaries (mf6, MinIO) and docker images are linux-amd64 |
| Python 3.12 | agent + TiTiler venvs (managed by `uv`) |
| Node 20+ | Vite dev server for the web UI |
| [uv](https://astral.sh/uv) | venv + dependency management (`make venv` assumes `~/.local/bin/uv`) |
| Docker | SFINCS, GeoClaw, and SWAN engines run in containers |
| [Ollama](https://ollama.com) | local LLM serving (any OpenAI-compatible endpoint also works) |

!!! warning "Docker group"
    Your user must be in the `docker` group. If you were **just** added, the running shell has
    not picked up the group -- either log out/in or wrap every docker-touching command
    (including the agent start) in `sg docker -c '...'`. See
    [Troubleshooting](troubleshooting.md#docker-permission-denied).

## 1. Binaries (mf6, minio, mc)

```sh
make binaries          # or: bash scripts/fetch_binaries.sh
```

Idempotent downloader. Installs:

- **MODFLOW 6.5.0** static linux binary -> `bin/mf6` (from the MODFLOW-USGS GitHub release; no
  runtime deps)
- **MinIO server** -> `bin/minio` and **mc client** -> `bin/mc` (from dl.min.io)

Each binary is version-verified after download.

## 2. Python venvs

Agent venv (installs the vendored contracts + agent packages editable):

```sh
make venv              # uv venv venvs/agent + uv pip install -e vendor/packages/contracts -e vendor/services/agent
```

TiTiler venv:

```sh
uv venv --python 3.12 venvs/titiler
uv pip install --python venvs/titiler/bin/python "titiler.application==2.0.4" uvicorn httpx
```

## 3. Web dependencies

```sh
cd vendor/web && npm install
```

(`make web` runs `vendor/web/node_modules/.bin/vite` directly, so the install must happen once.)

## 4. Docker images

**SFINCS** -- pulled from Docker Hub:

```sh
sg docker -c 'docker pull deltares/sfincs-cpu:sfincs-v2.3.3'
```

**GeoClaw and SWAN** -- built locally from the vendored worker Dockerfiles (both compile
Fortran solvers into the image; the build is one-time and cached):

```sh
sg docker -c 'docker build -t trid3nt-local/geoclaw:latest vendor/services/workers/geoclaw/'
sg docker -c 'docker build -t trid3nt-local/swan:latest vendor/services/workers/swan/'
```

These three image names are what `.env.local` points at (`GRACE2_SFINCS_IMAGE`,
`GRACE2_GEOCLAW_IMAGE`, `GRACE2_SWAN_IMAGE`).

## 5. MinIO

```sh
make minio
```

Starts the MinIO server on `:9000` (web console on `:9001`, user `trid3nt`) with data under
`data/minio/`, then runs `scripts/init_minio.sh` to create the two buckets the agent expects:
`trid3nt-runs` and `trid3nt-cache`. Idempotent.

## 6. Ollama + the default model

Install Ollama, then pull the base model and create the 16k-context variant the agent uses by
default:

```sh
ollama pull qwen3:8b
printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > /tmp/Modelfile.qwen3-16k
ollama create qwen3:8b-16k -f /tmp/Modelfile.qwen3-16k
```

Why the custom variant and the `/no_think` requirement:

- Ollama's default `num_ctx` (4096) is too small for the system prompt + tool schemas + case
  context; 16384 fits comfortably. See [Models](models.md) for the full rationale.
- Qwen3's default **thinking mode** routes all tokens to the reasoning channel, so the
  OpenAI-compatible content deltas arrive **empty** and turns render no text. `.env.local`
  ships `GRACE2_OPENAI_EXTRA_SYSTEM=/no_think`, which appends `/no_think` to the system prompt
  and disables thinking mode. Do not remove it while running a Qwen3-family model.

## 7. Configure the environment

`.env.local` at the repo root is the single configuration surface (loaded by
`scripts/start_agent.sh`). The defaults point at Ollama on `:11434`, MinIO on `:9000`, TiTiler
on `:8080`. See [Configuration](configuration.md) for every variable.

## 8. Start services

```sh
make minio                                   # MinIO + bucket init (if not already up)
make titiler                                 # TiTiler on :8080 backed by MinIO
sg docker -c 'bash scripts/start_agent.sh'   # agent (WS :8765, HTTP :8766) -- inside the docker group
make web                                     # Vite dev server on :5173
```

Each start script is stop-then-start (kills a prior instance via its pidfile), writes a PID to
`run/*.pid`, and logs to `logs/*.log`. `make agent` also works but does not enter the docker
group -- use the `sg docker -c` form so the agent can reach the docker socket for the
container-backed engines.

Check and stop:

```sh
make status    # minio (9000) / titiler (8080) / ollama (11434) health
make stop      # stops minio, titiler, agent via pidfiles
```

## Ports

| Port | Service | Notes |
|------|---------|-------|
| 5173 | Web UI (Vite dev server) | `http://localhost:5173` |
| 8765 | Agent WebSocket | chat protocol (`GRACE2_AGENT_PORT`) |
| 8766 | Agent HTTP | tool catalog + stats endpoints (`GRACE2_AGENT_HTTP_PORT`) |
| 9000 | MinIO S3 API | `AWS_ENDPOINT_URL` target; console on 9001 |
| 8080 | TiTiler | raster tiles; health at `/healthz` |
| 11434 | Ollama | OpenAI-compatible endpoint at `/v1` |

## Data directories (gitignored)

- `bin/` -- downloaded binaries (mf6, minio, mc)
- `venvs/` -- Python virtual environments (agent, titiler)
- `data/minio/` -- MinIO object storage
- `data/persistence/` -- agent FilePersistence store (cases, layers)
- `data/runs/` -- solver rundirs mounted into containers (`GRACE2_RUNS_DIR`)
- `data/telemetry/` -- tool-call telemetry JSONL
- `logs/`, `run/` -- service logs and PID files
