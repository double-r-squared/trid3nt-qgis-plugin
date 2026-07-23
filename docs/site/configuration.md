# TRID3NT Local -- Configuration (.env.local reference)

`.env.local` at the repo root is the single configuration surface. `scripts/start_agent.sh`
sources it with `set -a` (every variable exported) before launching
`python -m trid3nt_server.main`, and additionally **unsets** `TRID3NT_COGNITO_USER_POOL_ID` /
`TRID3NT_COGNITO_APP_CLIENT_ID` so a local session is always anonymous.

The variables below are the complete shipped file, grouped by concern.

---

## LLM provider

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `MODEL_PROVIDER` | `openai` | Selects the adapter in the provider dispatch seam. `openai` routes turns through `openai_adapter.py` (any OpenAI-compatible chat/completions endpoint with streaming tool calls). The cloud build uses `bedrock`. |
| `TRID3NT_OPENAI_BASE_URL` | `http://127.0.0.1:11434/v1` | The OpenAI-compatible endpoint. Default is local Ollama. Point it at vLLM, llama.cpp server, LM Studio, or a cloud API (OpenAI, Groq, DeepSeek, OpenRouter) to swap the model without touching code. |
| `TRID3NT_OPENAI_MODEL` | `qwen3:8b-16k` | Model name passed to the endpoint. The default is a locally-created Ollama variant of `qwen3:8b` with `num_ctx 16384` -- see [Models](models.md). |
| `TRID3NT_OPENAI_API_KEY` | `not-needed` | Bearer token for the endpoint. Ollama ignores it, but the OpenAI client requires a non-empty value; set a real key when pointing at a cloud API. |
| `TRID3NT_OPENAI_EXTRA_SYSTEM` | `/no_think` | Optional text appended to the system prompt (generic seam, dormant unless set). Primary use: `/no_think` for Qwen3-family models, whose default thinking mode routes all tokens to the reasoning channel so content deltas arrive empty and the turn renders no text. **Required** while running Qwen3. |

## Object storage (MinIO)

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `AWS_ENDPOINT_URL` | `http://127.0.0.1:9000` | Redirects boto3's ENTIRE S3 surface (runs bucket, cache bucket, COG uploads, completion polling, publish path) to local MinIO -- full S3 parity with zero code change. |
| `AWS_ACCESS_KEY_ID` | `trid3nt` | MinIO root user (matches `scripts/start_minio.sh`). |
| `AWS_SECRET_ACCESS_KEY` | `trid3nt-local-dev` | MinIO root password. Local dev only -- there is nothing sensitive behind it. |
| `AWS_REGION` | `us-east-1` | Nominal region for boto3 client construction; MinIO does not care. |
| `TRID3NT_RUNS_BUCKET` | `trid3nt-runs` | Bucket for solver run prefixes (`s3://trid3nt-runs/<run_id>/...`: decks, outputs, COGs, `completion.json`). Created by `scripts/init_minio.sh`. Has **no default** under the local-docker backend -- a missing value fails fast. |
| `TRID3NT_CACHE_BUCKET` | `trid3nt-cache` | Bucket for the fetch-tool cache (`cache/...` keys) and staged model decks. Created by `scripts/init_minio.sh`. |

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

## Solvers and engines

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `TRID3NT_SOLVER_BACKEND` | `local-docker` | Dispatch substrate for `run_solver` (SFINCS, and the staged-manifest local specs for GeoClaw/SWAN/SWMM). `local-docker` stages deck inputs into `$TRID3NT_RUNS_DIR/<run_id>/` and launches the engine container detached; the generic local supervisor uploads outputs + writes `completion.json`. The cloud value is `aws-batch`. |
| `TRID3NT_MODFLOW_LOCAL` | `1` | Gates MODFLOW's local-execution mode (run the `mf6` binary directly). **Independent of `TRID3NT_SOLVER_BACKEND`** -- MODFLOW checks this first; forgetting it makes the MODFLOW tools try the cloud path (`/opt/grace2/runs` errors). |
| `TRID3NT_MF6_BIN` | `<repo>/bin/mf6` | Path to the MODFLOW 6.5.0 static binary installed by `scripts/fetch_binaries.sh`. |
| `TRID3NT_SFINCS_IMAGE` | `deltares/sfincs-cpu:sfincs-v2.3.3` | SFINCS container image. The code default is `:latest`, which is not what `docker pull` fetched -- pin the tag you pulled. |
| `TRID3NT_GEOCLAW_IMAGE` | `trid3nt-local/geoclaw:latest` | GeoClaw container image, built locally from `services/workers/geoclaw/Dockerfile` (compiled Clawpack 5.14 Fortran). |
| `TRID3NT_SWAN_IMAGE` | `trid3nt-local/swan:latest` | SWAN container image, built locally from `services/workers/swan/Dockerfile`. |
| `TRID3NT_RUNS_DIR` | `<repo>/data/runs` | Host rundir root for local solves; mounted into engine containers at `/data`. The code default `/opt/grace2/runs` does not exist on a dev box -- set it. |
| `TRID3NT_OQ_BIN` | `<repo>/venvs/agent/bin/oq` | Path to the OpenQuake `oq` CLI (installed into the agent venv). First run needs a one-time `oq engine --upgrade-db`. |

## Agent process

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `TRID3NT_AGENT_HOST` | `0.0.0.0` | Bind host for the WS (`:8765`) and HTTP (`:8766`) listeners. `0.0.0.0` makes the agent LAN-reachable; `start_agent.sh` defaults it if unset. Ports are overridable via `TRID3NT_AGENT_PORT` (default 8765) and `TRID3NT_AGENT_HTTP_PORT` (default 8766) -- not set in the shipped file. |
| `TRID3NT_DEV_PERSISTENCE_DIR` | `<repo>/data/persistence` | Directory for the FilePersistence JSON store (all collections: cases, layers, users, telemetry shadow...). Keeping it inside the repo keeps state out of `~/.trid3nt`. |

## Remote daemon access (tailnet)

Run the daemon on one machine (desktop/workstation) and reach it from another
(laptop, phone, a second QGIS install) over a [Tailscale](https://tailscale.com)
tailnet. **The tailnet IS the security boundary**: traffic is already
device-authenticated and WireGuard-encrypted end to end, so there is no TLS and
no auth on the wire by default. Only put the daemon on a trusted tailnet.

The remote client needs exactly **one setting -- the WS server URL**
(e.g. `ws://100.x.x.x:8765`). Everything else is advertised by the server on the
connect handshake: the `auth-ack` (the first envelope the client parses) carries
an optional `endpoints` object with `data_base` (MinIO, `:9000`) and `http_base`
(agent HTTP, `:8766`). The server derives both from the address the client
connected TO -- a laptop dialing `100.x.x.x:8765` gets `http://100.x.x.x:9000`
and `http://100.x.x.x:8766` back automatically -- so the sibling services follow
the daemon's host with zero extra config. Old clients that never read the field,
and the offline stub that never sets it, are unaffected (it defaults absent).

**Binding.** All three listeners must bind `0.0.0.0`, not loopback, to be
reachable off-box:

- **Agent WS (`:8765`) + HTTP (`:8766`)** -- `start_agent.sh` forces
  `TRID3NT_AGENT_HOST=0.0.0.0`, and the HTTP catalog listener inherits that
  host. Already remote-ready.
- **MinIO (`:9000`)** -- `scripts/start_minio.sh` starts it with
  `--address :9000` (empty host = all interfaces), so MinIO is already reachable
  off-box. If you ever pin it to `127.0.0.1:9000`, remote layer fetches
  (`s3://` -> path-style http against `data_base`) will fail from other devices;
  keep the address host empty (or `0.0.0.0`).

| Variable | Default | What it does |
|----------|---------|--------------|
| `TRID3NT_ACCESS_TOKEN` | _(unset)_ | Optional shared token. When set, the WS handshake requires the client's `auth-token` to match (constant-time compare); a missing/wrong token is rejected with a typed `AUTH_FAILED` close (WS code 1008) and the client stops retrying. **Unset (default) is byte-identical anonymous access** -- no token required. Set the same value on the client. |
| `TRID3NT_ADVERTISED_DATA_BASE` | _(derived)_ | Override the advertised MinIO base URL (e.g. behind a reverse proxy / different hostname). When unset, derived as `http://<connected-host>:9000`. |
| `TRID3NT_ADVERTISED_HTTP_BASE` | _(derived)_ | Override the advertised agent HTTP base URL. When unset, derived as `http://<connected-host>:<TRID3NT_AGENT_HTTP_PORT>` (default `:8766`). |

### QGIS plugin: pointing at the remote daemon

1. Start the daemon on the machine that should run it (the desktop/PC) --
   `scripts/start_agent.sh` as usual.
2. On the OTHER machine (laptop, a second QGIS install) open the plugin's
   **Settings** dialog. The "Server" section is exactly two fields:
   - **Server URL** -- the same field the loopback default lives in
     (`ws://127.0.0.1:8765/ws`). Point it at the daemon's tailnet address
     instead, e.g. `ws://100.x.x.x:8765/ws`.
   - **Server token (optional)** -- leave blank unless the daemon set
     `TRID3NT_ACCESS_TOKEN`, in which case paste the same value here.
3. That is the whole setup. There is **no second field to configure** for
   MinIO or the agent's HTTP API -- the plugin learns both automatically
   from the connect handshake's advertised `data_base` / `http_base` (see
   above), or derives a `:8766` HTTP fallback from the Server URL's host if
   the daemon predates advertisement. Raster/vector layer loads, map-click
   probing, layer push, case export, and the provider/model pickers all
   follow the same derivation, so pointing the ONE url field at a new host
   is sufficient for everything.
4. **Simultaneous sessions.** The daemon is still single-user underneath
   (Appendix H / F1 local-single-user collapse) -- the desktop and the
   laptop share the SAME case list, not two independent ones. Live
   turn-in-progress updates (streaming chunks, tool cards, layer replay)
   land on whichever machine's connection actually SENT that turn; the
   other machine sees the result once the turn finishes and its own
   session/case state next refreshes (case switch, reconnect, or the next
   message it sends). Do not expect two machines to co-drive one live turn
   like two tabs of the same browser session -- treat it as "one user,
   two doors," not true multi-client collaboration.

## Tool retrieval (small-model routing)

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `TRID3NT_TOOL_RETRIEVAL` | `enforce` | Mode of the top-K tool-retrieval layer: `off` (default -- all tools visible), `shadow` (rank + log recall@K, still show everything), `enforce` (subset the registry to the top-K per turn BEFORE building tool declarations; a once-visible tool stays visible within a Case). Enforce keeps the tool context small enough for 8B-class local models. Fails open to the full registry on a cold index or ranking error. |
| `TRID3NT_TOOL_RETRIEVAL_K` | `8` | Top-K for `retrieve_visible_tools` (code default 25). K=8 is the benchmarked local setting -- see [Models](models.md#tool-retrieval-top-k). |

## Loop hygiene and telemetry

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `TRID3NT_SYNC_TOOL_OFFLOAD` | `global` | Off-loads synchronous tool bodies from the asyncio loop to a thread: `off` (default; only a hand-audited always-offload set of proven-pathological heavy fetchers is off-loaded), `subset` (also the pure `compute_*`/`clip_*` families), `global` (every sync tool body). Armed locally on 2026-07-06 after an abandoned sweep prompt's heavy fetch chain (USACE NSI + river geometry) ran ON the loop and starved new WS handshakes; global mode was proven safe on cloud first. |
| `TRID3NT_TELEMETRY_PATH` | `<repo>/data/telemetry/tool_calls.jsonl` | Output path for per-tool-call telemetry JSONL (the local fallback writer; default is under `/tmp`, which does not survive reboots). Feeds the local stats work (roadmap track 3). |

## Data catalog

| Variable | Shipped value | What it does |
|----------|---------------|--------------|
| `TRID3NT_CATALOG_YAML` | `<repo>/public_data_source_catalog.yaml` | Path to the vetted public data-source catalog used by `catalog_search` / `catalog_fetch`. Lives at the repo root; the tool also walks up from its own file to find it, so the env var is belt-and-suspenders. Without any of that the catalog tools raise a typed not-found error. |

## Not in the file, but related

- **Per-tool API keys** for the `KEY`-earmarked fetchers (`TRID3NT_AIRNOW_API_KEY`,
  `TRID3NT_EBIRD_API_KEY`, `TRID3NT_COPERNICUS_CDS_API_KEY`, `TRID3NT_IUCN_RED_LIST_API_KEY`,
  `TRID3NT_OPENAQ_API_KEY`, `TRID3NT_FIRMS_MAP_KEY`, `TRID3NT_CAMA_FLOOD_BASE_URL`) -- see the
  [Tool Support Matrix](tool-support.md#key-earmarked-tools) for which tool needs what.
- `VITE_TRID3NT_*` web vars are deliberately **absent**: with no overrides the SPA derives
  `ws://<hostname>:8765` and `http://<hostname>:8766` and works against the local agent.
