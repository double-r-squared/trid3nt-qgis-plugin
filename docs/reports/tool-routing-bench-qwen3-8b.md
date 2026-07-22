# Tool Routing Benchmark -- qwen3:8b-16k + /no_think

**Date:** 2026-07-05  
**Model:** qwen3:8b-16k (Ollama, /no_think mode)  
**Tools registered:** 176  
**Agent:** ws://localhost:8765  

## Results Table

| # | Prompt (short) | Expected | Actual_First_Tool | Result | Args_Valid | Wall_Time_s |
|---|---------------|----------|-------------------|--------|------------|-------------|
| 1 | Boulder CO bbox | geocode_location | `geocode_location` | CORRECT | YES | 29.8s |
| 2 | DEM Asheville NC | fetch_elevation / fetch_topobathy | `geocode_location` | CHAIN_CORRECT | YES | 78.6s |
| 3 | Land cover Sacramento | fetch_landcover | `geocode_location` | CHAIN_CORRECT | YES | 235.1s |
| 4 | Buildings Savannah GA | fetch_buildings | `geocode_location` | CHAIN_CORRECT | YES | 28.7s |
| 5 | River network Missoula | fetch_rivers (or similar) | `geocode_location` | WRONG | YES | 38.7s |
| 6 | Earthquakes San Jose | USGS earthquake fetcher | `geocode_location` | CHAIN_CORRECT | YES | 224.5s |
| 7 | Precip radar Kansas | NEXRAD/radar fetcher | `geocode_location` | WRONG | YES | 24.7s |
| 8 | Hillshade Boone NC | compute_hillshade (+ DEM chain) | `geocode_location` | CHAIN_CORRECT | YES | 32.2s |
| 9 | Avg elev Provo UT | compute_zonal_statistics chain | `geocode_location` | CORRECT | YES | 31.0s |
| 10 | Pluvial flood Peoria IL | run_model_flood_scenario | `geocode_location` | CHAIN_CORRECT | YES | 37.9s |
| 11 | MODFLOW Bakersfield CA | run_model_sustainable_yield_scenario | `run_model_sustainable_yield_scenario` | CORRECT | YES | 10.1s |
| 12 | SWMM Alexandria VA | SWMM composer | `geocode_location` | CHAIN_CORRECT | YES | 40.2s |
| 13 | Tsunami Crescent City | GeoClaw composer | `geocode_location` | CHAIN_CORRECT | YES | 32.1s |
| 14 | Seismic hazard SF Bay | OpenQuake composer | `geocode_location` | CHAIN_CORRECT | YES | 27.1s |
| 15 | Haiku (no tool) | NO_TOOL | `-` | NO_CALL_CORRECT | YES | 7.4s |

## Per-Prompt Notes

### P1 -- Boulder CO bbox
- **Expected:** geocode_location
- **Tools fired (in order):** ['geocode_location']
- **Verdict:** SELECTED_CORRECT
- **Args valid:** True
- **Wall time:** 29.8s

### P2 -- DEM Asheville NC
- **Expected:** fetch_elevation / fetch_topobathy
- **Tools fired (in order):** ['geocode_location', 'fetch_dem', 'publish_layer']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 78.6s

### P3 -- Land cover Sacramento
- **Expected:** fetch_landcover
- **Tools fired (in order):** ['geocode_location', 'fetch_landcover', 'publish_layer', 'generate_histogram']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 235.1s
- **Error:** `CONFIRMATION_TIMEOUT` -- code_exec_request '01KY5MR88SXS07K99RNXW4CQW0' approval card was not answered within 180s; the sandbox did not run

### P4 -- Buildings Savannah GA
- **Expected:** fetch_buildings
- **Tools fired (in order):** ['geocode_location', 'fetch_buildings']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 28.7s
- **Error:** `LLM_UNAVAILABLE` -- Model generation failed: Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (124/32

### P5 -- River network Missoula
- **Expected:** fetch_rivers (or similar)
- **Tools fired (in order):** ['geocode_location', 'fetch_river_geometry', 'publish_layer']
- **Verdict:** SELECTED_WRONG
- **Args valid:** True
- **Wall time:** 38.7s

### P6 -- Earthquakes San Jose
- **Expected:** USGS earthquake fetcher
- **Tools fired (in order):** ['geocode_location', 'fetch_usgs_earthquakes', 'publish_layer']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 224.5s
- **Error:** `CONFIRMATION_TIMEOUT` -- code_exec_request '01KY5N1HMJY4EMKEQKBD7RFGHY' approval card was not answered within 180s; the sandbox did not run

### P7 -- Precip radar Kansas
- **Expected:** NEXRAD/radar fetcher
- **Tools fired (in order):** ['geocode_location', 'fetch_nexrad_reflectivity']
- **Verdict:** SELECTED_WRONG
- **Args valid:** True
- **Wall time:** 24.7s

### P8 -- Hillshade Boone NC
- **Expected:** compute_hillshade (+ DEM chain)
- **Tools fired (in order):** ['geocode_location', 'fetch_dem', 'compute_hillshade', 'publish_layer']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 32.2s

### P9 -- Avg elev Provo UT
- **Expected:** compute_zonal_statistics chain
- **Tools fired (in order):** ['geocode_location']
- **Verdict:** SELECTED_CORRECT
- **Args valid:** True
- **Wall time:** 31.0s

### P10 -- Pluvial flood Peoria IL
- **Expected:** run_model_flood_scenario
- **Tools fired (in order):** ['geocode_location', 'run_model_flood_scenario']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 37.9s
- **Cancelled:** YES (solver cancel on start)

### P11 -- MODFLOW Bakersfield CA
- **Expected:** run_model_sustainable_yield_scenario
- **Tools fired (in order):** ['run_model_sustainable_yield_scenario']
- **Verdict:** SELECTED_CORRECT
- **Args valid:** True
- **Wall time:** 10.1s
- **Cancelled:** YES (solver cancel on start)

### P12 -- SWMM Alexandria VA
- **Expected:** SWMM composer
- **Tools fired (in order):** ['geocode_location', 'run_swmm_urban_flood']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 40.2s
- **Cancelled:** YES (solver cancel on start)

### P13 -- Tsunami Crescent City
- **Expected:** GeoClaw composer
- **Tools fired (in order):** ['geocode_location', 'run_geoclaw_inundation']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 32.1s
- **Cancelled:** YES (solver cancel on start)

### P14 -- Seismic hazard SF Bay
- **Expected:** OpenQuake composer
- **Tools fired (in order):** ['geocode_location', 'run_seismic_hazard_psha', 'resolve_fault_sources', 'stage_openquake_build_spec']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 27.1s
- **Cancelled:** YES (solver cancel on start)

### P15 -- Haiku (no tool)
- **Expected:** NO_TOOL
- **Tools fired (in order):** ['(none)']
- **Verdict:** NO_CALL_CORRECT
- **Args valid:** True
- **Wall time:** 7.4s

## Overall Stats

- **Prompts run:** 15
- **Tool prompts (1-14):** 14
- **SELECTED_CORRECT (first tool exact match):** 3/14
- **CHAIN_CORRECT (correct tool appeared, not first):** 9/14
- **Total selection accuracy (correct + chain):** 12/14 = 85.7%
- **SELECTED_WRONG:** 2/14
- **NO_CALL (tool expected, none fired):** 0/14
- **False-positive rate (P15 haiku):** 0/1
- **Mean time to completion (tool prompts with tool fired):** 62.2s

## VERDICT

**ADEQUATE -- RAG top-k retrieval NOT urgently needed.**  Selection accuracy 85.7% (>= 80% threshold). qwen3:8b-16k with 176 tools in context is routing correctly for most task types. The model correctly abstained from tool use for the no-tool prompt (haiku). 
