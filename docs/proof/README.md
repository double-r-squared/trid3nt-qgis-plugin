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

---

## SFINCS local (docker) 2026-07-05

The FINAL v1 engine milestone: a small-AOI SFINCS pluvial (rain-on-grid) flood
running LOCALLY via docker, end-to-end. This is genuine execution -- a
`deltares/sfincs-cpu` container runs the SFINCS binary against a hydromt-built
deck; outputs (`sfincs_map.nc`, depth COGs) land in MinIO; the depth layer is
published via TiTiler.

### Env changes made

`.env.local` (see repo README quickstart):
- `GRACE2_SOLVER_BACKEND=local-docker` (was `local-exec`) -- routes `run_solver('sfincs')`
  to the docker path. Independent of MODFLOW: MODFLOW's local mf6 path is gated on the
  separate `GRACE2_MODFLOW_LOCAL=1` (checked first, unaffected by this change).
- `GRACE2_SFINCS_IMAGE=deltares/sfincs-cpu:sfincs-v2.3.3` (the pulled image; the code
  default is `:latest`, which is not pulled locally).
- `GRACE2_RUNS_DIR=/home/nate/Documents/trid3nt-local/data/runs` (host rundir mounted
  into the container at `/data`; the code default `/opt/grace2/runs` does not exist here).

The agent was restarted inside the docker group (`sg docker -c 'bash scripts/start_agent.sh'`)
so the agent process can reach the docker socket.

### The pipeline (deterministic, in `run_model_flood_scenario`)

`fetch_dem` (USGS 3DEP) + `fetch_landcover` (NLCD) -> `lookup_precip_return_period`
(NOAA Atlas 14 100yr/1hr) -> `build_sfincs_model` (hydromt-sfincs 1.2.2, in-agent,
`GRACE2_SFINCS_BUILD_OFFLOAD` unset) -> deck staged to `s3://trid3nt-cache/...` (MinIO) ->
`run_solver('sfincs')` (local-docker: stages 11 deck inputs into the rundir, launches
`docker run --rm --name <run_id> -v <rundir>:/data -w /data deltares/sfincs-cpu:sfincs-v2.3.3`) ->
supervisor uploads outputs + writes `completion.json` to `s3://trid3nt-runs/<run_id>/` ->
`wait_for_completion` (polls the completion.json) -> `postprocess_flood` (peak-depth COG) ->
`publish_layer` (TiTiler tile URL).

### Attempt 1 - LLM-driven (PRIMARY proof): PASS

Playwright harness `scripts/e2e_sfincs_llm.mjs` drove the qwen3:8b-16k model through the
web app at :5173 with the pluvial Chattanooga prompt (bbox `[-85.32, 35.03, -85.28, 35.07]`,
100yr/1hr design storm, coarsest resolution).

- **OUTCOME: PASS** -- LLM called `run_model_flood_scenario` on turn 1, 0 nudges
- run_id: `01KWRSKE771W6XVDJRSQDXZYSY`
- SFINCS container ran; `completion.json` status=ok, exit_code=0
- Outputs: `s3://trid3nt-runs/01KWRSKE771W6XVDJRSQDXZYSY/sfincs_map.nc` (1.7 MiB) + stdout/stderr
- 3 layers rendered in the LayerPanel (DEM, NLCD, depth) over Chattanooga
- Note: `docker ps -a` sampling (15s cadence) missed the short-lived `--rm` container;
  the new MinIO run prefix + completion.json are the authoritative genuine-execution proof.

### Attempt 2 - tool-direct (corroborating smoke run): PASS

`scripts/run_sfincs_direct.py` calls the same `run_model_flood_scenario` workflow directly
(no LLM), used first to de-risk the docker path before spending the 30-min LLM window.

- **OUTCOME: PASS**
- run_id: `01KWRSECZGJEYBD44X6T0GRTT9`
- docker exec captured verbatim in `logs/sfincs_direct.log`:
  `docker run --rm --name 01KWRSECZGJEYBD44X6T0GRTT9 -v .../data/runs/01KWRSECZGJEYBD44X6T0GRTT9:/data -w /data deltares/sfincs-cpu:sfincs-v2.3.3`
- `completion.json` status=ok, exit_code=0
- Outputs in `s3://trid3nt-runs/01KWRSECZGJEYBD44X6T0GRTT9/`: `sfincs_map.nc` (1.79 MiB),
  7 `flood_depth_frame_NN.tif` frames, `flood_depth_peak.tif`, stdout/stderr, completion.json
- Published depth layer:
  `http://127.0.0.1:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3://trid3nt-runs/01KWRSECZGJEYBD44X6T0GRTT9/flood_depth_peak.tif&rescale=0,3&colormap_name=ylgnbu`
- estimated_active_cells=22890 -> compute_class=small
- Full result: `sfincs_direct_result.json`

### Screenshots

| File | What it proves |
|------|---------------|
| 08-sfincs-local-running.png | Web app (:5173) mid-run: LayerPanel building NLCD/DEM/Rivers layers over Chattanooga, chat showing the pluvial SFINCS prompt + "resolution confirmed", legend visible -- LLM-driven pipeline in flight |
| 09-sfincs-depth-layer.png | The SFINCS result layer rendered on the MapLibre map over downtown Chattanooga (river channel + land cover), from the local-docker solve output |

Note (honesty): the direct run also rendered a clean depth-only TiTiler `/cog/preview` PNG of
`flood_depth_peak.tif`; the LLM harness subsequently overwrote `09-sfincs-depth-layer.png` with
the richer in-app map render (stronger proof), which is what is committed.

### Known cosmetic noise

`logs/sfincs_direct.log` contains a urllib3 `HeaderParsingError` WARNING/traceback on a
MinIO `Content-Length: 0` HEAD-style response for `sfincs_map.nc`. It is non-fatal (the
poll loop continues and the run completes status=ok); it is a known MinIO/urllib3 header
quirk, not a solve failure.
