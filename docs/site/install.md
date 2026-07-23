# TRID3NT Local -- Install

There are three install paths, depending on what this machine is for. Pick one:

| Path | This machine is... | What you do | QGIS needed here? |
|------|---------------------|--------------|--------------------|
| [Client-only](#client-only-install) | a laptop that just wants the chat dock | copy one `.zip`, install from ZIP | Yes |
| [Daemon-only](#daemon-only-install-from-scratch) | a PC / headless box running the server | full from-scratch setup below | No -- `qgis-plugin/` is never loaded |
| [Both](#both-one-dev-machine) | one dev machine doing everything | daemon-only steps + `make plugin` | Yes |

Everything the daemon-only / both paths install lands inside the repo directory
(`bin/`, `venvs/`, `data/`, `logs/`, `run/` are gitignored); nothing is installed
system-wide except Docker and (optionally) Ollama.

---

## Client-only install

The client machine needs **no git clone, no Python venv, no server** -- just QGIS
and one file.

**Prerequisites**

| Requirement | Why |
|-------------|-----|
| QGIS 3.28 or later | plugin's `qgisMinimumVersion` (`qgis-plugin/trid3nt/metadata.txt`) |

**Steps**

1. On *any* checkout of this repo (the daemon machine, or a throwaway clone),
   from the repo root:

   ```sh
   make plugin-zip
   ```

   Writes `dist/trid3nt-plugin-<version>.zip` (e.g. `dist/trid3nt-plugin-0.3.2.zip`).
2. Copy that one file to the client machine (scp, USB, a tailnet file share --
   anything; the client never touches git).
3. In QGIS: **Plugins > Manage and Install Plugins > Install from ZIP**, point it
   at the file.
4. Enable **TRID3NT** in the Plugin Manager (check "Show also Experimental
   Plugins" under Settings -- the plugin ships `experimental=True`).
5. Open the dock (toolbar trident icon) > **Settings**, and set:
   - **Server URL** -- `ws://127.0.0.1:8765/ws` if the daemon runs on this same
     machine, or the daemon's tailnet address for a remote daemon, e.g.
     `ws://100.x.x.x:8765/ws`.
   - **Server token** -- leave blank unless the daemon set
     `TRID3NT_ACCESS_TOKEN`, in which case paste the same value here.

   See [Remote daemon access (tailnet)](configuration.md#remote-daemon-access-tailnet)
   for how the plugin learns the MinIO / HTTP endpoints from that one URL.

Full plugin walkthrough (dev install, test suite): [qgis-plugin/README.md](../../qgis-plugin/README.md).

---

## Daemon-only install (from-scratch)

Follow this on the machine that will actually run the server (`trid3nt_server`
+ MinIO + the solvers). If this machine will ALSO run QGIS, this is still the
right starting point -- see [Both](#both-one-dev-machine) for the one extra step.

### Prerequisites

| Requirement | Why |
|-------------|-----|
| Linux x86_64 | binaries (mf6, MinIO) and docker images are linux-amd64 |
| Python 3.12 | agent venv (managed by `uv`) |
| [uv](https://astral.sh/uv) | venv + dependency management (`make venv` assumes `~/.local/bin/uv`) |
| Docker | SFINCS, GeoClaw, and TELEMAC engines run in containers |
| [Ollama](https://ollama.com) | local LLM serving (any OpenAI-compatible endpoint also works) |

!!! warning "Docker group"
    Your user must be in the `docker` group. If you were **just** added, the running shell has
    not picked up the group -- either log out/in or wrap every docker-touching command
    (including the agent start) in `sg docker -c '...'`. See
    [Troubleshooting](troubleshooting.md#docker-permission-denied).

### 1. Binaries (mf6, minio, mc)

```sh
make binaries          # or: bash scripts/fetch_binaries.sh
```

Idempotent downloader. Installs:

- **MODFLOW 6.5.0** static linux binary -> `bin/mf6` (from the MODFLOW-USGS GitHub release; no
  runtime deps)
- **MinIO server** -> `bin/minio` and **mc client** -> `bin/mc` (from dl.min.io)

Each binary is version-verified after download.

### 2. Python venvs

Agent venv (installs the contracts + server packages editable):

```sh
make venv              # uv venv venvs/agent + uv pip install -e contracts -e server
```

`server/wheels/` carries one PyPI-absent dependency (`pfdf`, for post-fire
debris-flow) that `make venv` installs via `--find-links`; do not delete it.

### 3. Docker images

**SFINCS** -- pulled from Docker Hub:

```sh
sg docker -c 'docker pull deltares/sfincs-cpu:sfincs-v2.3.3'
```

**GeoClaw and TELEMAC** -- built locally from the worker Dockerfiles (both compile
Fortran solvers into the image; the build is one-time and cached):

```sh
sg docker -c 'docker build -t trid3nt-local/geoclaw:latest -f services/workers/geoclaw/Dockerfile .'
sg docker -c 'docker build -t trid3nt-local/telemac:latest services/workers/telemac/'  # or: bash scripts/build_telemac_image.sh
```

**SWAN** -- builds locally (verified 2026-07-23; the pinned source-tarball
checksum was a placeholder before that date and the `sha256sum -c` step always
failed -- now fixed to the real digest of the SWAN 41.51 SourceForge tarball):

```sh
sg docker -c 'docker build -t trid3nt-local/swan:latest -f services/workers/swan/Dockerfile .'
```

These image names are what `.env.local` points at (`TRID3NT_SFINCS_IMAGE`,
`TRID3NT_GEOCLAW_IMAGE`, `TRID3NT_SWAN_IMAGE`).

### 4. MinIO

```sh
make minio
```

Starts the MinIO server on `:9000` (web console on `:9001`, user `trid3nt`) with data under
`data/minio/`, then runs `scripts/init_minio.sh` to create the two buckets the agent expects:
`trid3nt-runs` and `trid3nt-cache`. Idempotent.

### 5. Ollama + the default model

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
  ships `TRID3NT_OPENAI_EXTRA_SYSTEM=/no_think`, which appends `/no_think` to the system prompt
  and disables thinking mode. Do not remove it while running a Qwen3-family model. See
  [Troubleshooting](troubleshooting.md#qwen3-thinking-mode-swallows-all-output) if replies
  render no text.

### 6. Configure the environment

`.env.local` at the repo root is the single configuration surface (loaded by
`scripts/start_agent.sh`). The defaults point at Ollama on `:11434` and MinIO on `:9000`. See [Configuration](configuration.md) for every variable.

### 7. Start services

```sh
make minio                                   # MinIO + bucket init (if not already up)
sg docker -c 'bash scripts/start_agent.sh'   # agent (WS :8765, HTTP :8766) -- inside the docker group
```

Each start script is stop-then-start (kills a prior instance via its pidfile), writes a PID to
`run/*.pid`, and logs to `logs/*.log`. `make agent` also works but does not enter the docker
group -- use the `sg docker -c` form so the agent can reach the docker socket for the
container-backed engines. `make up` runs `minio` then `agent` in one shot (same caveat: prefix
with `sg docker -c` if your shell has not picked up the `docker` group yet).

Check and stop:

```sh
make status    # minio (9000) / agent (8766) / ollama (11434) health
make stop      # stops minio and the agent via pidfiles
```

---

## Both (one dev machine)

Run every step under [Daemon-only install](#daemon-only-install-from-scratch)
above, then also install the plugin into your QGIS profile:

```sh
make plugin
```

This runs `scripts/install_plugin.sh`, which `rsync -a --delete`s
`qgis-plugin/trid3nt/` into `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/trid3nt/`
-- QGIS loads that installed profile COPY, never the repo checkout directly, so
a plugin-side code change needs `make plugin` again to reach QGIS. After
syncing, reload in QGIS: **Plugins > Plugin Reloader** (or restart QGIS), then
enable **TRID3NT** in the Plugin Manager if it is not already.

---

## Ports

| Port | Service | Notes |
|------|---------|-------|
| 8765 | Agent WebSocket | chat protocol (`TRID3NT_AGENT_PORT`) |
| 8766 | Agent HTTP | tool catalog + stats endpoints (`TRID3NT_AGENT_HTTP_PORT`) |
| 9000 | MinIO S3 API | `AWS_ENDPOINT_URL` target; console on 9001 |
| 11434 | Ollama | OpenAI-compatible endpoint at `/v1` |

## Data directories (gitignored)

- `bin/` -- downloaded binaries (mf6, minio, mc)
- `venvs/` -- Python virtual environments (agent)
- `data/minio/` -- MinIO object storage
- `data/persistence/` -- agent FilePersistence store (cases, layers)
- `data/runs/` -- solver rundirs mounted into containers (`TRID3NT_RUNS_DIR`)
- `data/telemetry/` -- tool-call telemetry JSONL
- `logs/`, `run/` -- service logs and PID files

## Troubleshooting

Something not working? [Troubleshooting](troubleshooting.md) covers the greatest
hits: Docker group permissions, the Qwen3 thinking-mode empty-reply symptom,
MinIO hijacking public NOAA buckets, a cold tool-retrieval index right after
startup, and more.
