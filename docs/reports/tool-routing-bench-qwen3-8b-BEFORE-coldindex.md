# Tool Routing Benchmark -- qwen3:8b-16k + /no_think

**Date:** 2026-07-05  
**Model:** qwen3:8b-16k (Ollama, /no_think mode)  
**Tools registered:** 176  
**Agent:** ws://localhost:8765  
**Harness:** `scripts/tool_routing_bench.py` (fresh WS session + case per prompt; solver prompts cancelled via the `cancel` envelope as soon as the composer step appeared in `pipeline-state`; payload-warning cards auto-confirmed with `proceed`)

Scoring note: the initial automated scoring missed three registered composer
names (`run_swmm_urban_flood`, `run_seismic_hazard_psha`,
`run_geoclaw_inundation` -- verified against the vendor tool registry). The
table below is the corrected scoring of the SAME recorded run; no prompt was
re-run.

## Results Table

| # | Prompt (short) | Expected | Actual_First_Tool | Result | Args_Valid | Wall_Time_s |
|---|---------------|----------|-------------------|--------|------------|-------------|
| 1 | Boulder CO bbox | geocode_location | `geocode_location` | CORRECT | YES | 88.4 |
| 2 | DEM Asheville NC | fetch_elevation / fetch_topobathy | `run_model_flood_scenario` | CHAIN_CORRECT | YES | 117.2 |
| 3 | Land cover Sacramento | fetch_landcover | `-` | NO_CALL | YES | 30.9 |
| 4 | Buildings Savannah GA | fetch_buildings | `web_fetch` | WRONG | YES | 91.3 |
| 5 | River network Missoula | fetch_rivers (or similar) | `-` | NO_CALL | YES | 54.6 |
| 6 | Earthquakes San Jose | USGS earthquake fetcher | `web_fetch` | WRONG | YES | 240.0 |
| 7 | Precip radar Kansas | NEXRAD/radar fetcher | `web_fetch` | WRONG | YES | 108.0 |
| 8 | Hillshade Boone NC | compute_hillshade (+ DEM chain) | `compute_colored_relief` | WRONG | YES | 79.3 |
| 9 | Avg elev Provo UT | compute_zonal_statistics chain | `-` | NO_CALL | YES | 27.2 |
| 10 | Pluvial flood Peoria IL | run_model_flood_scenario | `run_swmm_urban_flood` | WRONG | YES | 66.0 |
| 11 | MODFLOW Bakersfield CA | run_model_sustainable_yield_scenario | `run_model_sustainable_yield_scenario` | CORRECT | YES | 72.9 |
| 12 | SWMM Alexandria VA | SWMM composer | `run_swmm_urban_flood` | CORRECT | YES | 48.4 |
| 13 | Tsunami Crescent City | GeoClaw composer | `run_model_saltwater_intrusion_scenario` | WRONG | YES | 47.3 |
| 14 | Seismic hazard SF Bay | OpenQuake composer | `run_seismic_hazard_psha` | CORRECT | YES | 83.3 |
| 15 | Haiku (no tool) | NO_TOOL | `-` | NO_CALL_CORRECT | YES | 29.4 |

## Per-Prompt Notes

### P1 -- Boulder CO bbox (CORRECT)
Single clean `geocode_location` dispatch, correct args, answered with the
bbox. The canonical trivial case works.

### P2 -- DEM Asheville NC (CHAIN_CORRECT)
The model reached for `run_model_flood_scenario` FIRST for a plain DEM fetch
(over-escalation to a solver), which tripped the payload-warning gate; after
auto-confirm the chain did include `fetch_dem` and then
`summarize_layer_statistics`, so the data landed -- but the first-choice
instinct was a composer, not the fetcher.

### P3 -- Land cover Sacramento (NO_CALL)
Answered in prose in 31 s without dispatching `fetch_landcover` at all.

### P4 -- Buildings Savannah GA (WRONG)
Fell back to generic `web_fetch` instead of `fetch_buildings`. The model knows
it needs external data but cannot find the purpose-built fetcher among 176
tools.

### P5 -- River network Missoula (NO_CALL)
No tool fired; prose answer.

### P6 -- Earthquakes San Jose (WRONG)
`web_fetch` again, and the turn ran to the full 240 s cap (the model appeared
to loop on web content rather than dispatch the USGS earthquake fetcher).
Slowest prompt of the run.

### P7 -- Precip radar Kansas (WRONG)
`web_fetch` again. Third instance of the same failure mode: generic web
retrieval substituting for a domain fetcher.

### P8 -- Hillshade Boone NC (WRONG, near-miss)
Chose `compute_colored_relief` instead of `compute_hillshade`. Semantically
adjacent (both terrain-visualization derivatives of a DEM) -- this is a
confusion between two similarly named registered tools, exactly the failure
RAG-style tool retrieval targets.

### P9 -- Avg elev Provo UT (NO_CALL)
The hardest chain prompt (boundary + DEM + zonal stats) produced no tool call
at all; prose answer in 27 s.

### P10 -- Pluvial flood Peoria IL (WRONG, near-miss)
Picked the SWMM urban-flood composer instead of `run_model_flood_scenario`
(SFINCS pluvial). Defensible confusion -- SWMM is also a pluvial/urban runoff
engine -- but per spec the SFINCS composer was expected. Cancelled at solver
start as designed.

### P11 -- MODFLOW Bakersfield CA (CORRECT)
Exact hit on `run_model_sustainable_yield_scenario` with valid args (the
prompt supplied lat/lon and pumping rate). Cancelled at solver start.

### P12 -- SWMM Alexandria VA (CORRECT)
Exact hit on `run_swmm_urban_flood` (the registered SWMM composer). Cancelled
at solver start.

### P13 -- Tsunami Crescent City (WRONG)
Chose `run_model_saltwater_intrusion_scenario` -- a coastal-groundwater
MODFLOW composer -- for a tsunami. Appears to have latched onto
"coastal + ocean" keywords; never touched `run_geoclaw_inundation`. Worst
miss of the run. Cancelled at solver start.

### P14 -- Seismic hazard SF Bay (CORRECT)
Exact hit on `run_seismic_hazard_psha` (OpenQuake composer), followed by its
own chain steps (`resolve_fault_sources`, `stage_openquake_build_spec`).
Cancelled at solver start.

### P15 -- Haiku (NO_CALL_CORRECT)
No tool fired for the no-tool prompt. Zero false positives.

## Overall Stats

- **Prompts run:** 15
- **Tool prompts (1-14):** 14
- **SELECTED_CORRECT (first tool exact match):** 4/14
- **CHAIN_CORRECT (correct tool appeared, not first):** 1/14
- **Total selection accuracy (correct + chain):** 5/14 = **35.7%**
- **SELECTED_WRONG:** 6/14
- **NO_CALL (tool expected, none fired):** 3/14
- **ARGS_VALID:** 15/15 -- no USER_INPUT_REQUIRED or first-call error envelopes anywhere; when the model did pick a tool it filled the args acceptably
- **False-positive rate (P15 haiku):** 0/1
- **Mean wall time, tool prompts that fired a tool:** 94.7 s (min 47.3 s, max 240.0 s)

## VERDICT

**RAG top-k tool retrieval is NEEDED for this model size.** Selection
accuracy is 35.7%, far below the ~80% adequacy bar. The errors are not random:
they form three clear clusters. (1) A `web_fetch` attractor -- three domain
fetch prompts (buildings, earthquakes, radar) all collapsed onto the generic
web tool, meaning the model cannot locate the purpose-built fetcher in a
176-tool catalog. (2) A no-call cluster -- three prompts (landcover, rivers,
zonal stats) got prose answers with zero dispatch. (3) Near-neighbor
confusion -- hillshade vs colored-relief, SFINCS vs SWMM, and tsunami vs
saltwater-intrusion show the model choosing a semantically adjacent but wrong
tool. All three clusters are exactly what top-k retrieval addresses: with 5-15
candidate tools in context instead of 176, the web_fetch attractor loses its
pull, the purpose-built fetcher becomes visible, and near-neighbor
discrimination gets a fighting chance. The positives: argument construction
was clean everywhere (15/15 args valid -- selection, not schema-filling, is
the bottleneck), the false-positive rate on the no-tool prompt was zero, and
the solver composers with distinctive names (MODFLOW sustainable yield, SWMM,
OpenQuake PSHA) routed exactly. This matches the standing hypothesis
(project_tool_retrieval_rag_for_local_models): tool SELECTION accuracy, not
cost, is the binding constraint for 8B-class local models -- proceed with the
RAG top-k retrieval track for the offline build.
