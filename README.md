# TRID3NT Local

Offline / local-first build of TRID3NT (GRACE-2): the AI workbench for
multi-hazard geospatial modeling, running entirely on your own machine.

- One local server + the same web UI as the cloud app (browser opens localhost)
- Pluggable LLM via any OpenAI-compatible endpoint: local (Ollama, vLLM,
  llama.cpp, LM Studio) or cloud (OpenAI, Groq, DeepSeek, OpenRouter, ...)
- Simulations run locally: MODFLOW 6 first, SFINCS next; more engines follow
- File-based persistence + local tile rendering -- no cloud account required

Status: pre-alpha scaffold. Design doc lands in `docs/design/`.

## Quickstart

Prerequisites: Linux x86_64, Python 3.12, Node 20+, [uv](https://astral.sh/uv),
[ollama](https://ollama.com) running locally (for local LLM; any OpenAI-compatible
endpoint also works).

### 1. Download binaries (mf6, minio, mc)

```sh
bash scripts/fetch_binaries.sh
# or:
make binaries
```

Downloads MODFLOW 6.5.0 static binary to `./bin/mf6` and MinIO server + client to
`./bin/minio` + `./bin/mc`. Idempotent -- safe to re-run.

### 2. Create the TiTiler venv

```sh
uv venv --python 3.12 venvs/titiler
uv pip install --python venvs/titiler/bin/python "titiler.application==2.0.4" uvicorn httpx
```

### 3. Configure environment

Copy `.env.local` and edit as needed (LLM endpoint, model name):

```sh
cp .env.local .env.local.mine   # optional personal override
```

The defaults point at Ollama on localhost, MinIO on :9000, TiTiler on :8080.

### 4. Start services

```sh
make minio     # starts MinIO + creates buckets trid3nt-runs + trid3nt-cache
make titiler   # starts TiTiler on :8080 backed by MinIO
make agent     # (not yet built -- placeholder)
make web       # (not yet configured -- placeholder)
```

### 5. Check status

```sh
make status
# minio  (9000): OK
# titiler (8080): OK
# ollama (11434): OK
```

### 6. Stop services

```sh
make stop
```

### Service URLs

| Service        | URL                         | Notes                        |
|----------------|-----------------------------|------------------------------|
| MinIO API      | http://localhost:9000       | S3-compatible object storage |
| MinIO Console  | http://localhost:9001       | web UI (user: trid3nt)       |
| TiTiler        | http://localhost:8080       | raster tile server           |
| TiTiler health | http://localhost:8080/healthz | version JSON                |
| Ollama         | http://localhost:11434      | local LLM endpoint           |
| Agent WS       | ws://localhost:8765         | (not yet built)              |
| Web UI         | http://localhost:5173       | (not yet configured)         |

### Data directories (gitignored)

- `./bin/` -- downloaded binaries
- `./venvs/` -- Python virtual environments
- `./data/minio/` -- MinIO object storage
- `./data/persistence/` -- agent file-based persistence (cases, layers)
- `./logs/` -- service log files
- `./run/` -- PID files

## Layout (planned)

```
server/     local agent server (WS + HTTP, LLM provider seam, local solver exec)
web/        the SPA (same UI as cloud, localhost backend)
workers/    engine runners (mf6 subprocess, SFINCS docker)
docs/       design + user docs
```
