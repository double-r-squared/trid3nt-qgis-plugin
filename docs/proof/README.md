# TRID3NT Local - End-to-End Proof

Run date: 2026-07-04

## What was tested

Local stack (all services on localhost):
- Agent WS :8765 + HTTP :8766 (176 tools, MODEL_PROVIDER=openai -> Ollama)
- MinIO :9000 (user=trid3nt, pass=trid3nt-local-dev)
- TiTiler :8080
- Ollama :11434 (llama3.2:3b-32k)
- Vite dev server :5173 (vendor/web)

## Screenshot inventory

| File | What it proves |
|------|---------------|
| 01-app-loaded.png | Vite dev server at :5173 serves the React app; map canvas + chat UI rendered |
| 02-local-llm-chat.png | LLM (llama3.2:3b via Ollama) responded to "Say hello in one short sentence." -- local inference working |
| 03-modflow-running.png | MODFLOW prompt sent to agent; partial response visible (3b model attempted tool routing) |
| 04-modflow-layer-rendered.png | LayerPanel has content after MODFLOW prompt (state from prior direct invocation visible) |
| 04-modflow-tile.png | TiTiler /cog/preview.png from s3://trid3nt-runs/fresno-sy-zy33lti0/drawdown_4326.tif -- 256x256 drawdown COG rendered blue-scale |
| 05-minio-console.png | MinIO web console at :9001 showing trid3nt-runs and trid3nt-cache buckets |

## MODFLOW run evidence

Run via `scripts/run_modflow_direct.py` (direct tool invocation -- fallback path used because
llama3.2:3b did not reliably emit MODFLOW tool calls in the Playwright session):

- run_id: `fresno-sy-zy33lti0`
- archetype: `sustainable_yield`
- location: Fresno, CA (lat=36.7468, lon=-119.7726)
- mf6 binary: `/home/nate/Documents/trid3nt-local/bin/mf6` (MODFLOW 6.5.0 2024-05-23)
- Converged: YES (exit=0, "Normal termination of simulation" in mfsim.lst)
- Elapsed run time: 0.263 seconds
- max_drawdown_m: 6.1337 m (physically realistic for 2000 m3/day pumping)
- COG uploaded: `s3://trid3nt-runs/fresno-sy-zy33lti0/drawdown_4326.tif` (10168 bytes)
- CRS: EPSG:32611 (deck), EPSG:4326 (output COG)
- TiTiler preview: HTTP 200, 3290 bytes (saved as 04-modflow-tile.png)

Full MinIO listing in `artifacts.txt`.

## LLM path status

- Hello message: WORKED (llama3.2:3b responded within 90s)
- MODFLOW tool calls: NOT DRIVEN by 3b model (tool retrieval with GRACE2_TOOL_RETRIEVAL=enforce
  + K=8 narrows tools, but 3b model did not produce valid MODFLOW composer tool calls in the
  3-minute window)
- Fallback used: YES -- `scripts/run_modflow_direct.py` invoked MODFLOW directly

The 3b model is sufficient for simple conversational responses but not reliable for
multi-step tool composition with structured JSON arguments. A larger model
(llama3.2:9b, qwen3.5:9b, or llama3.1:8b) is recommended for full LLM-driven MODFLOW runs.

## Services still running

- Vite dev server: pid in `run/web.pid` (port 5173)
- Agent: pid in `run/agent.pid` (ports 8765/8766)
- MinIO: pid in `run/minio.pid` (port 9000)
- TiTiler: pid in `run/titiler.pid` (port 8080)
- Ollama: system service (port 11434)
