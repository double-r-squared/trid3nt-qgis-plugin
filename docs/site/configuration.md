# TRID3NT Local -- Configuration (.env.local reference)

`.env.local` at the repo root is the single configuration surface. `scripts/start_agent.sh`
sources it with `set -a` (every variable exported) before launching
`python -m grace2_agent.main`, and additionally **unsets** `GRACE2_COGNITO_USER_POOL_ID` /
`GRACE2_COGNITO_APP_CLIENT_ID` so a local session is always anonymous.

The variables below are the complete shipped file, grouped by concern.

---

## LLM provider

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `MODEL_PROVIDER` | `openai` | Selects the adapter in the provider dispatch seam. `openai` routes turns through `openai_adapter.py` (any OpenAI-compatible chat/completions endpoint with streaming tool calls). The cloud build uses `bedrock`. |
| `GRACE2_OPENAI_BASE_URL` | `http://127.0.0.1:11434/v1` | The OpenAI-compatible endpoint. Default is local Ollama. Point it at vLLM, llama.cpp server, LM Studio, or a cloud API (OpenAI, Groq, DeepSeek, OpenRouter) to swap the model without touching code. |
| `GRACE2_OPENAI_MODEL` | `qwen3:8b-16k` | Model name passed to the endpoint. The default is a locally-created Ollama variant of `qwen3:8b` with `num_ctx 16384` -- see [Models](models.md). |
| `GRACE2_OPENAI_API_KEY` | `not-needed` | Bearer token for the endpoint. Ollama ignores it, but the OpenAI client requires a non-empty value; set a real key when pointing at a cloud API. |
| `GRACE2_OPENAI_EXTRA_SYSTEM` | `/no_think` | Optional text appended to the system prompt (generic seam, dormant unless set). Primary use: `/no_think` for Qwen3-family models, whose default thinking mode routes all tokens to the reasoning channel so content deltas arrive empty and the turn renders no text. **Required** while running Qwen3. |

## Object storage (MinIO)

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `AWS_ENDPOINT_URL` | `http://127.0.0.1:9000` | Redirects boto3's ENTIRE S3 surface (runs bucket, cache bucket, COG uploads, completion polling, publish path) to local MinIO -- full S3 parity with zero code change. TiTiler is started with the same env so it reads the same `s3://` URIs. |
| `AWS_ACCESS_KEY_ID` | `trid3nt` | MinIO root user (matches `scripts/start_minio.sh`). |
| `AWS_SECRET_ACCESS_KEY` | `trid3nt-local-dev` | MinIO root password. Local dev only -- there is nothing sensitive behind it. |
| `AWS_REGION` | `us-east-1` | Nominal region for boto3 client construction; MinIO does not care. |
| `GRACE2_RUNS_BUCKET` | `trid3nt-runs` | Bucket for solver run prefixes (`s3://trid3nt-runs/<run_id>/...`: decks, outputs, COGs, `completion.json`). Created by `scripts/init_minio.sh`. Has **no default** under the local-docker backend -- a missing value fails fast. |
| `GRACE2_CACHE_BUCKET` | `trid3nt-cache` | Bucket for the fetch-tool cache (`cache/...` keys) and staged model decks. Created by `scripts/init_minio.sh`. |

!!! warning "The public-bucket endpoint-pin caveat"
    `AWS_ENDPOINT_URL` is honored **globally** by boto3 (>= 1.28) and s3fs/aiobotocore -- including
    anonymous reads of PUBLIC AWS open-data buckets (GOES/GLM granules on `noaa-goesNN`, the
    HRRR zarr mirror). Without countermeasures those reads get silently redirected to MinIO and
    fail with misleading "no data upstream" errors. The agent carries
    `tools/_public_s3.py`, which pins UNSIGNED public-bucket clients to the real
    `https://s3.<region>.amazonaws.com` endpoint. Cloud behavior is unchanged (the env var is
    unset there). If you add a new tool that reads a public bucket anonymously, build its client
    via `_public_s3` -- do not use a bare `boto3.client("s3")`. See
    [Troubleshooting](troubleshooting.md#minio-hijacks-public-noaa-buckets).

## Tiles

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `GRACE2_TILE_SERVER_BASE` | `http://127.0.0.1:8080` | Base URL `publish_layer` embeds in the tile templates it emits to the client. Must point at the local TiTiler; if unset, publishing a raster fails with a typed error. |

## Solvers and engines

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `GRACE2_SOLVER_BACKEND` | `local-docker` | Dispatch substrate for `run_solver` (SFINCS, and the staged-manifest local specs for GeoClaw/SWAN/SWMM). `local-docker` stages deck inputs into `$GRACE2_RUNS_DIR/<run_id>/` and launches the engine container detached; the generic local supervisor uploads outputs + writes `completion.json`. The cloud value is `aws-batch`. |
| `GRACE2_MODFLOW_LOCAL` | `1` | Gates MODFLOW's local-execution mode (run the `mf6` binary directly). **Independent of `GRACE2_SOLVER_BACKEND`** -- MODFLOW checks this first; forgetting it makes the MODFLOW tools try the cloud path (`/opt/grace2/runs` errors). |
| `GRACE2_MF6_BIN` | `<repo>/bin/mf6` | Path to the MODFLOW 6.5.0 static binary installed by `scripts/fetch_binaries.sh`. |
| `GRACE2_SFINCS_IMAGE` | `deltares/sfincs-cpu:sfincs-v2.3.3` | SFINCS container image. The code default is `:latest`, which is not what `docker pull` fetched -- pin the tag you pulled. |
| `GRACE2_GEOCLAW_IMAGE` | `trid3nt-local/geoclaw:latest` | GeoClaw container image, built locally from `services/workers/geoclaw/Dockerfile` (compiled Clawpack 5.14 Fortran). |
| `GRACE2_SWAN_IMAGE` | `trid3nt-local/swan:latest` | SWAN container image, built locally from `services/workers/swan/Dockerfile`. |
| `GRACE2_RUNS_DIR` | `<repo>/data/runs` | Host rundir root for local solves; mounted into engine containers at `/data`. The code default `/opt/grace2/runs` does not exist on a dev box -- set it. |
| `GRACE2_OQ_BIN` | `<repo>/venvs/agent/bin/oq` | Path to the OpenQuake `oq` CLI (installed into the agent venv). First run needs a one-time `oq engine --upgrade-db`. |

## Agent process

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `GRACE2_AGENT_HOST` | `0.0.0.0` | Bind host for the WS (`:8765`) and HTTP (`:8766`) listeners. `0.0.0.0` makes the agent LAN-reachable; `start_agent.sh` defaults it if unset. Ports are overridable via `GRACE2_AGENT_PORT` (default 8765) and `GRACE2_AGENT_HTTP_PORT` (default 8766) -- not set in the shipped file. |
| `GRACE2_DEV_PERSISTENCE_DIR` | `<repo>/data/persistence` | Directory for the FilePersistence JSON store (all collections: cases, layers, users, telemetry shadow...). Keeping it inside the repo keeps state out of `~/.grace2`. |
| `AUTH_REQUIRED` | `false` | Disables the Cognito JWT gate; the server mints anonymous ULID users per connection. `start_agent.sh` also unsets the Cognito pool/client vars defensively. |

## Tool retrieval (small-model routing)

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `GRACE2_TOOL_RETRIEVAL` | `enforce` | Mode of the top-K tool-retrieval layer: `off` (default -- all tools visible), `shadow` (rank + log recall@K, still show everything), `enforce` (subset the registry to the top-K per turn BEFORE building tool declarations; a once-visible tool stays visible within a Case). Enforce keeps the tool context small enough for 8B-class local models. Fails open to the full registry on a cold index or ranking error. |
| `GRACE2_TOOL_RETRIEVAL_K` | `8` | Top-K for `retrieve_visible_tools` (code default 25). K=8 is the benchmarked local setting -- see [Models](models.md#tool-retrieval-top-k). |

## Loop hygiene and telemetry

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `GRACE2_SYNC_TOOL_OFFLOAD` | `global` | Off-loads synchronous tool bodies from the asyncio loop to a thread: `off` (default; only a hand-audited always-offload set of proven-pathological heavy fetchers is off-loaded), `subset` (also the pure `compute_*`/`clip_*` families), `global` (every sync tool body). Armed locally on 2026-07-06 after an abandoned sweep prompt's heavy fetch chain (USACE NSI + river geometry) ran ON the loop and starved new WS handshakes; global mode was proven safe on cloud first. |
| `GRACE2_TELEMETRY_PATH` | `<repo>/data/telemetry/tool_calls.jsonl` | Output path for per-tool-call telemetry JSONL (the local fallback writer; default is under `/tmp`, which does not survive reboots). Feeds the local stats work (roadmap track 3). |

## Data catalog

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `GRACE2_CATALOG_YAML` | `<repo>/public_data_source_catalog.yaml` | Path to the vetted public data-source catalog used by `catalog_search` / `catalog_fetch`. Lives at the repo root; the tool also walks up from its own file to find it, so the env var is belt-and-suspenders. Without any of that the catalog tools raise a typed not-found error. |

## Not in the file, but related

- **Per-tool API keys** for the `KEY`-earmarked fetchers (`GRACE2_AIRNOW_API_KEY`,
  `GRACE2_EBIRD_API_KEY`, `GRACE2_COPERNICUS_CDS_API_KEY`, `GRACE2_IUCN_RED_LIST_API_KEY`,
  `GRACE2_OPENAQ_API_KEY`, `GRACE2_FIRMS_MAP_KEY`, `GRACE2_CAMA_FLOOD_BASE_URL`) -- see the
  [Tool Support Matrix](tool-support.md#key-earmarked-tools) for which tool needs what.
- `VITE_GRACE2_*` web vars are deliberately **absent**: with no overrides the SPA derives
  `ws://<hostname>:8765` and `http://<hostname>:8766` and works against the local agent.
