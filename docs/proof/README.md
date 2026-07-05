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

---

## LLM-driven retry on qwen3:8b-16k (2026-07-04)

Run date: 2026-07-04 (session started ~22:32 PDT, completed 22:33 PDT)

### Context

The original proof used llama3.2:3b which could not drive multi-step tool composition.
This section documents the retry with qwen3:8b-16k (structured tool-calling proven through
the agent stack) using the Playwright harness `scripts/e2e_modflow_llm.mjs`.

Stack state at retry time:
- Ollama model: `qwen3:8b-16k` (MODEL_PROVIDER=openai via Ollama /v1 endpoint)
- Agent restarted with `GRACE2_MODFLOW_LOCAL=1` (required for local mf6 execution;
  was missing from the original .env.local which had GRACE2_SOLVER_BACKEND=local-exec
  but that env is not the same gate as GRACE2_MODFLOW_LOCAL)
- GRACE2_TOOL_RETRIEVAL=enforce, K=8 -- tool retrieval active but fails-open to full
  registry when the turn user text matches no top-K tools. The sustainable_yield tool
  is NOT in the tool_query_corpus.yaml, so retrieval fell back to full 176-tool context.
- The tool requires `well_location_latlon` AND `pumping_rate_m3_day` as mandatory
  user inputs (never fabricated), but both are typed as Optional in the JSON schema.

### Tool sequence (observed in agent.log)

Turn 1 (no nudges needed):
1. `run_model_sustainable_yield_scenario` -- called directly on the first turn with:
   `aoi_latlon=[36.7468, -119.7726], well_location_latlon=[36.7468, -119.7726], pumping_rate_m3_day=2000`
   (the model followed the explicit prompt instructions)

Internal chain (within run_model_sustainable_yield_scenario):
2. Deck build + S3 stage (MinIO: `s3://trid3nt-cache/modflow/...`)
3. `run_modflow_local` (mf6 binary /home/nate/Documents/trid3nt-local/bin/mf6)
   - exit=0, converged=True, elapsed ~0.07s
4. `postprocess_drawdown` -- run_id=01KWRC8TCVX0AF9TFHFP2PZVM1, max_drawdown_m=6.13371, steps_ts=41
5. `publish_layer` -- drawdown COG -> s3://trid3nt-runs/01KWRC8TCVX0AF9TFHFP2PZVM1/drawdown_4326.tif
   -> TiTiler tile template published to UI

Agent narrated the result with a "Head decline at well over time" time series chart
(visible in screenshot 06-llm-driven-modflow-running.png).

### Result

- **OUTCOME: PASS** -- full LLM-driven chain, no fallback
- run_id: `01KWRC8TCVX0AF9TFHFP2PZVM1`
- archetype: `sustainable_yield`
- location: Fresno, CA (lat=36.7468, lon=-119.7726)
- mf6: exit=0, converged=True, max_drawdown_m=6.13371 m
- COG: `s3://trid3nt-runs/01KWRC8TCVX0AF9TFHFP2PZVM1/drawdown_4326.tif`
- TiTiler layer: published to chat UI as `drawdown-01KWRC8TCVX0AF9TFHFP2PZVM1`
- nudges used: 0 (model called the right tool with correct args on turn 1)
- Inference latency: ~80s first-token (qwen3:8b-16k on CPU/GPU via Ollama)

### Screenshots

| File | What it proves |
|------|---------------|
| 06-llm-driven-modflow-running.png | Chat UI showing the MODFLOW prompt, pipeline card "MODFLOW Sustainable Yield...", and agent narration with "Head decline at well over time" time series chart -- tool chain fired LLM-driven |
| 07-llm-driven-modflow-layer.png | Same state (run completed in <1s after the ~80s inference; chat shows completed analysis) |

### Failure modes encountered during development (model-matrix data)

During harness development, multiple failure modes were uncovered and recorded:

1. **Location constraint**: `run_model_sustainable_yield_scenario` requires EXACTLY ONE of
   `location` OR `aoi_latlon` (not both). The model initially passed both, triggering
   `USER_INPUT_REQUIRED` (the tool's honesty-floor check). Fixed by prompting with
   `aoi_latlon` only.

2. **Missing well_location_latlon**: The tool's JSON schema marks `well_location_latlon`
   as Optional (= None default) even though the business logic requires it. Early runs
   where the model omitted it triggered `USER_INPUT_REQUIRED`. Fixed by explicitly
   specifying the arg in the prompt.

3. **GRACE2_MODFLOW_LOCAL not set**: The agent was started with `GRACE2_SOLVER_BACKEND=local-exec`
   but `run_modflow_archetype_tool.py` gates the local mf6 path on `GRACE2_MODFLOW_LOCAL=1`
   (a separate env var). Without it, the tool tried to create `/opt/grace2/runs/` which
   does not exist. Fixed by restarting the agent with `GRACE2_MODFLOW_LOCAL=1`.

4. **compute_class confusion**: The model occasionally passed `compute_class='sustainable_yield'`
   (confusing the archetype name with the compute class). The allowed values are
   `['gpu', 'large', 'medium', 'small', 'standard', 'xlarge']`. The final working prompt
   did not specify compute_class, so it defaulted to `standard`.

5. **qwen3:8b-16k tool-calling is reliable**: Unlike llama3.2:3b (which never emitted a
   valid MODFLOW tool call), qwen3:8b-16k called `run_model_sustainable_yield_scenario`
   directly on turn 1 in every run once the prompt provided the required mandatory args.
   This confirms qwen3:8b-16k is suitable for single-tool-call MODFLOW composition.
