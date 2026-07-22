# TRID3NT Local -- Offline Architecture (v1 design)
2026-07-04. Grounded in a full seam inventory of the GRACE-2 codebase (verified file:line findings; see the cloud repo's reports for the raw pass).

## Goal

The original goal, revived: the same AI hazard-modeling workbench, running on one
machine. Sims run locally. The LLM is pluggable -- a local model (Ollama, vLLM,
llama.cpp, LM Studio) or any cloud API, through ONE OpenAI-compatible provider
seam. No AWS account, no cloud dependency for the core loop.

## Headline finding: ~60-70% already exists as dormant dev seams

| Seam | Cloud (today) | Existing local fallback | State |
|---|---|---|---|
| Persistence | DynamoDB (authored, not even default) | FileMCPClient -- JSON under ~/.trid3nt/dev_persistence, all 7 collections, Mongo-style operators | COMPLETE. The live cloud box actually runs on it |
| Auth | Cognito JWT + Hosted UI | AUTH_REQUIRED=false default; no VITE_COGNITO_* -> web runs anonymous-only, server mints ULID users (H.3) | COMPLETE for single-user local |
| Web URLs | CloudFront/Vercel env-derived | No VITE_TRID3NT_* -> ws://hostname:8765 + http://hostname:8766; wake overlay + cold paths null out cleanly | COMPLETE |
| MODFLOW | AWS Batch | TRID3NT_SOLVER_BACKEND=local-exec + TRID3NT_MF6_BIN (USGS mf6 6.5.0 static binary, no runtime deps) | WORKS today |
| SFINCS | AWS Batch | TRID3NT_SOLVER_BACKEND=local-docker -> docker run deltares/sfincs-cpu:sfincs-v2.3.3 (public image) | WORKS today (needs docker) |
| Telemetry/audit | DynamoDB via persistence | Graceful degrade to local JSONL (/tmp/trid3nt_tool_call_telemetry.jsonl) / FileMCPClient; never raises | COMPLETE |
| Data fetchers | ~38 tools | All public HTTPS (USGS/NOAA/OSM/Overpass) or anonymous public S3 (GOES, MRMS, HRSL). ZERO requester-pays, zero private-AWS-only sources | COMPLETE (need internet, not AWS) |
| LLM | Bedrock (MODEL_PROVIDER=bedrock) | scripted/replay adapter only (CI); dormant Vertex path | GAP 1 |
| Object storage | S3 runs+cache buckets (scheme hardcoded "s3") | file:// works for INPUT staging (manifests/decks) but NOT the COG output/publish path or cache | GAP 2 |
| Tiles | TiTiler EC2 box | titiler.application==2.0.4 is pip-installable; publish_layer just needs TRID3NT_TILE_SERVER_BASE=http://localhost:8080 | GAP 3 (trivial) |
| SWMM/Landlab/OpenQuake local | AWS Batch | pip-only workers (manylinux wheels), but no LocalSolverSpec registered (only SFINCS has one) | GAP 4 (deferred past v1) |

## The four real gaps -> the v1 work

### GAP 1 -- OpenAI-compatible LLM provider (the only new adapter)
- New `openai_adapter.py` beside `bedrock_adapter.py` (~150-250 lines). The dispatch
  seam is a single branch in `adapter.stream_events_with_contents()`; the contract is:
  consume `genai_types.Content[]` + `FunctionDeclaration[]` + system prompt, yield the
  `StreamEvent` union (TextDelta / FunctionCall / UsageMetadata). Bedrock adapter is the
  reference translation (it already maps genai types -> another wire format).
- Config: `MODEL_PROVIDER=openai`, `TRID3NT_OPENAI_BASE_URL` (e.g. http://localhost:11434/v1
  for Ollama), `TRID3NT_OPENAI_API_KEY` (dummy for local), `TRID3NT_OPENAI_MODEL`.
- One provider covers: Ollama, vLLM, llama.cpp server, LM Studio, OpenAI, Groq,
  DeepSeek, OpenRouter. Streaming tool-calls required (all of the above support it).

### GAP 2 -- local object storage: MinIO, zero code change (v1 decision)
- boto3 honors `AWS_ENDPOINT_URL`; point the ENTIRE existing S3 surface (runs bucket,
  cache bucket, COG uploads, completion polling, publish path) at a local MinIO
  container. No code change, full S3-parity, and TiTiler reads the same s3:// URIs
  transparently with the same env.
- Alternative (pure file://, no MinIO) is a LATER refinement: it needs code in
  cache.storage_scheme(), cog_io.upload_cog(), publish_layer -- more surface, more bugs.
  v1 chooses parity over purity. SFINCS already requires docker, so one more tiny
  container costs nothing.

### GAP 3 -- local TiTiler
- `pip install titiler.application==2.0.4` + one uvicorn (or the official docker image)
  with the MinIO AWS_* env. Agent env: `TRID3NT_TILE_SERVER_BASE=http://localhost:8080`.
  publish_layer emits full tile templates server-side, so the web needs nothing.

### GAP 4 -- LocalSolverSpec for the pip-only engines (POST-v1)
- SWMM / Landlab / OpenQuake are pure-pip workers; they need LocalSolverSpec entries to
  dispatch as local subprocesses (the `_supervise_local_run` supervisor is generic and
  reusable). GeoClaw + SWAN stay docker-only (runtime gfortran compilation).
- v1 ships MODFLOW (local-exec) + SFINCS (local-docker) only -- per scope decision.

## v1 topology

```
browser -> http://localhost:5173  (Vite web, no VITE_* cloud vars)
             |-- ws://localhost:8765   agent (host venv; FilePersistence; anonymous auth)
             |-- http://localhost:8766 agent HTTP (catalog)
             '-- http://localhost:8080 TiTiler (docker or venv)
agent -> http://localhost:11434/v1     Ollama (or any OpenAI-compatible URL, incl. cloud)
agent -> http://localhost:9000        MinIO (runs + cache buckets, AWS_ENDPOINT_URL)
solves:  mf6 subprocess (local-exec)  |  docker run deltares/sfincs-cpu (local-docker)
docker-compose: [minio, titiler, ollama?]   agent+web: host processes (dev-style)
```

Full `.env.local` (the entire cloud-to-local rewiring is ~10 env vars):
`MODEL_PROVIDER=openai`, `TRID3NT_OPENAI_BASE_URL`, `TRID3NT_OPENAI_MODEL`,
`AWS_ENDPOINT_URL=http://localhost:9000` (+ dummy AWS creds for MinIO),
`TRID3NT_RUNS_BUCKET=trid3nt-runs`, `TRID3NT_CACHE_BUCKET=trid3nt-cache`,
`TRID3NT_TILE_SERVER_BASE=http://localhost:8080`, `TRID3NT_SOLVER_BACKEND=local-exec`
(MODFLOW) / `local-docker` (SFINCS), `TRID3NT_MF6_BIN=<path>`, `TRID3NT_AGENT_HOST=0.0.0.0`.

## Repo strategy: upstream seams + vendored snapshot

The seams (openai_adapter, any storage/solver glue) are implemented UPSTREAM in
GRACE-2 (they are dormant there, env-gated, zero cloud impact) -- one agent codebase,
no fork drift. This repo contains:
- `scripts/sync_from_grace2.sh` -- vendors services/agent, services/workers (modflow,
  sfincs, shared _* packages), packages/contracts, and web/ from a PINNED GRACE-2
  commit into this repo (recorded in UPSTREAM_COMMIT). Publishable standalone.
- `compose.yml` (minio + titiler + optional ollama), `.env.local` template,
  `Makefile`/launcher (make up / make agent / make web / make smoke).
- `docs/` -- this design + user setup guide.
Commits authored natealmanza3@gmail.com; NO remote until NATE creates the GitHub repo.

## The local-LLM reality check (standing directive)

Before betting UX on a small model: reliability-test 7B/28B-class models against the
~160-tool catalog FIRST (tool-selection accuracy, not token cost, is the known risk).
The harness: replay a fixed prompt set (the demo specs) through MODEL_PROVIDER=openai
-> Ollama and score tool-call validity + selection. RAG top-k tool retrieval is the
fallback ONLY if selection proves unreliable. Escape hatch is built-in: the same seam
points at any cloud API when a local model is not cutting it.

## v1 milestones (each locally verified before the next)

1. **Boot**: agent starts in a venv with FilePersistence + anonymous auth + MinIO env;
   web dev server dials localhost WS; chat round-trips against Ollama through
   openai_adapter (a plain no-tool prompt).
2. **Tools**: tool-calling turn works (geocode + fetch a public layer end-to-end);
   telemetry lands in local JSONL.
3. **MODFLOW local**: one archetype (sustainable_yield) end-to-end: local-exec mf6 ->
   completion.json in MinIO -> postprocess -> COG -> TiTiler tile renders in the map.
4. **SFINCS local**: small-AOI pluvial via local-docker -> depth layer renders.
   (The cloud flood-smoke prompt is the acceptance script.)
5. **Model matrix**: the milestone-2/3 script run against 2-3 local models + one cloud
   API through the same seam; record tool-selection reliability -> decides the RAG
   question with data.

## Non-goals for v1

QGIS plugin (phase 2, thin client onto this server). GeoClaw/SWAN/OpenQuake/SWMM/
Landlab local dispatch (post-v1, GAP 4). Pure-file storage without MinIO. Multi-user
auth. Offline basemaps (CartoDB tiles need internet; acceptable -- "offline" here means
no-cloud-ACCOUNT, not air-gapped; an air-gapped tile pack is a later option).
