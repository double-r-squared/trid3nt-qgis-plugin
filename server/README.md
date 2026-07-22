# server/ - Agent service (Bedrock + WebSocket)

**Owner:** `agent` specialist. **Container/deploy:** `infra` (EC2 + systemd; AWS Batch for heavy solves).

The agent service (SRS v0.3 Decision E/G, FR-AS-*): a Python application that
serves the Appendix-A WebSocket protocol, hosts the tool registry (native
Python FunctionTools + the persistence + hazard-modeling tools), runs the
multi-turn generation loop against **AWS Bedrock** (Claude Sonnet 4.6 by
default; Haiku 4.5 / Nova selectable), streams replies, propagates cancellation,
and enforces the determinism boundary (Invariant 1) and confirmation-before-
consequence hooks (Invariant 9).

> **Provider note.** This service began on Vertex AI / Gemini (`adapter.py`). The
> AWS migration moved the live LLM path to Amazon Bedrock via
> `bedrock_adapter.py` (`MODEL_PROVIDER=bedrock`). The agent contract is still
> expressed in `google.genai.types` shapes — `bedrock_adapter` converts them
> to/from the Bedrock Converse API at the boundary — so `google-genai` is a
> load-bearing dependency on AWS even though no Vertex call is made. The
> Vertex generation path in `adapter.py` is retired; only the provider-neutral
> genai-types helpers it holds are still used.

## Layout

```
server/
├── pyproject.toml            trid3nt-server package, console script `trid3nt-server`
├── README.md                 (this file)
├── src/trid3nt_server/
│   ├── __init__.py
│   ├── main.py               entry point (`trid3nt-server` → run())
│   ├── server.py             Appendix-A WebSocket server (asyncio + websockets)
│   ├── bedrock_adapter.py    AWS Bedrock Converse loop (live provider) + cachePoint
│   ├── adapter.py            StreamEvent union + provider-neutral genai-types helpers
│   ├── persistence.py        Cases/sessions/users persistence (file default, DynamoDB opt-in)
│   ├── secrets_handler.py    secrets vault — AWS SSM Parameter Store (aws-ssm://)
│   ├── auth_handshake.py     Cognito ID-token (JWKS/RS256) verification on WS connect
│   ├── tools/                the tool registry + atomic/composer/engine tools
│   │   ├── catalog.py        registry + docstring-metadata enforcement
│   │   ├── cache.py          cached-fetch storage (S3; storage_scheme default s3)
│   │   ├── solver.py         run_solver — AWS Batch dispatcher + S3 completion poll
│   │   └── …                 fetchers, QGIS discovery, spatial-input, draw, engine tools
│   └── workflows/            multi-step hazard workflows (flood, MODFLOW, …)
└── scripts/
    └── ws_client.py          live-evidence harness
```

## Running locally

```bash
# from repo root, requires the project's virtualenv (.venv-agent/)
make run-agent
# then in another shell:
python server/scripts/ws_client.py "What is SFINCS?"
```

`make run-agent` sources the venv at `.venv-agent/` and launches `trid3nt-server`
on port 8765 (override with `TRID3NT_AGENT_PORT`).

### Environment (live AWS shape)

The agent reads its provider/storage/solver seams from the environment at call
time, so the deployed systemd unit injects them without a code change. The live
EC2 box (`trid3nt-server.service`) sets:

```ini
MODEL_PROVIDER=bedrock                 # → bedrock_adapter; Vertex path is retired
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6   # default; Haiku/Nova selectable in-chat
AWS_REGION=us-west-2
TRID3NT_STORAGE_BACKEND=s3              # cache/staging on S3 (boto3 / s3fs)
TRID3NT_SOLVER_BACKEND=aws-batch       # heavy solves dispatch to AWS Batch (Spot, scale-to-zero)
TRID3NT_AWS_BATCH_QUEUE=grace2-solvers
TRID3NT_AWS_BATCH_JOB_DEF=grace2-sfincs
TRID3NT_CACHE_BUCKET=trid3nt-cache
TRID3NT_RUNS_BUCKET=trid3nt-runs
# TRID3NT_PERSISTENCE_BACKEND=dynamodb # opt-in; unset → file-backed (the demo default)
```

Credentials come from the EC2 instance role (no static keys). Bedrock model
access, S3 buckets, the Batch queue/job-defs, the SSM secrets vault, and the
optional DynamoDB tables are all in account `226996537797` (us-west-2).

## Scope

- Real Bedrock Converse round-trip with streamed `agent-message-chunk` deltas,
  terminal `done: true` frame, and `cachePoint` prompt-caching on Anthropic
  models (Sonnet/Haiku); Nova/DeepSeek omit cachePoint (the server gates it on
  the Anthropic family).
- `cancel` interrupts in-flight generation, cancels the in-flight AWS Batch
  job, and emits cancelled `pipeline-state` (Invariant 8) — within 30s.
- Persistence (Cases/sessions/users/secrets-refs/audit) through the
  `Persistence` seam: file-backed by default, DynamoDB when
  `TRID3NT_PERSISTENCE_BACKEND=dynamodb`. (The GCP-era MongoDB MCP path is
  removed.)
- Heavy/sandboxed compute runs externally: SFINCS + SWMM on AWS Batch (Spot,
  scale-to-zero), MODFLOW + the Python sandbox via local subprocess on the
  EC2 box (the AWS local-exec path). Tiles serve from TiTiler on the always-on
  tiles box.
- Every wire message validated via `trid3nt_contracts` — no hand-rolled JSON.

## Deploy

The agent runs as `trid3nt-server.service` (systemd) on the agent EC2 box. Deploy
is a code sync + `systemctl restart trid3nt-server` over SSM (no Cloud Run, no
`gcloud`); deploys land continuously as work goes green. The agent box
auto-stops when idle and wakes on the web "Wake up agent" overlay
(`infra/aws-autostop/`). See `infra/aws-batch/RUNBOOK.md` for the Batch
substrate and the env-flip steps. IAM / login remain NATE's interactive step.
