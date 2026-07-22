# Tool Routing Benchmark -- qwen3:8b-16k + /no_think

**Date:** 2026-07-05  
**Model:** qwen3:8b-16k (Ollama, /no_think mode)  
**Tools registered:** 176  
**Agent:** ws://localhost:8765  

## Results Table

| # | Prompt (short) | Expected | Actual_First_Tool | Result | Args_Valid | Wall_Time_s |
|---|---------------|----------|-------------------|--------|------------|-------------|
| 1 | Boulder CO bbox | geocode_location | `geocode_location` | CORRECT | YES | 45.6s |
| 2 | DEM Asheville NC | fetch_elevation / fetch_topobathy | `geocode_location` | CHAIN_CORRECT | YES | 61.2s |
| 3 | Land cover Sacramento | fetch_landcover | `geocode_location` | WRONG | YES | 240.0s |
| 4 | Buildings Savannah GA | fetch_buildings | `geocode_location` | CHAIN_CORRECT | YES | 89.7s |
| 5 | River network Missoula | fetch_rivers (or similar) | `geocode_location` | WRONG | YES | 44.1s |
| 6 | Earthquakes San Jose | USGS earthquake fetcher | `geocode_location` | CHAIN_CORRECT | YES | 35.3s |
| 7 | Precip radar Kansas | NEXRAD/radar fetcher | `geocode_location` | WRONG | YES | 34.9s |
| 8 | Hillshade Boone NC | compute_hillshade (+ DEM chain) | `geocode_location` | CHAIN_CORRECT | YES | 28.3s |
| 9 | Avg elev Provo UT | compute_zonal_statistics chain | `geocode_location` | CORRECT | YES | 36.5s |
| 10 | Pluvial flood Peoria IL | run_model_flood_scenario | `geocode_location` | WRONG | YES | 240.0s |
| 11 | MODFLOW Bakersfield CA | run_model_sustainable_yield_scenario | `run_model_sustainable_yield_scenario` | CORRECT | YES | 16.0s |
| 12 | SWMM Alexandria VA | SWMM composer | `geocode_location` | CHAIN_CORRECT | YES | 49.0s |
| 13 | Tsunami Crescent City | GeoClaw composer | `geocode_location` | CHAIN_CORRECT | YES | 37.7s |
| 14 | Seismic hazard SF Bay | OpenQuake composer | `geocode_location` | CHAIN_CORRECT | YES | 31.4s |
| 15 | Haiku (no tool) | NO_TOOL | `-` | NO_CALL_CORRECT | YES | 14.2s |

## Per-Prompt Notes

### P1 -- Boulder CO bbox
- **Expected:** geocode_location
- **Tools fired (in order):** ['geocode_location']
- **Verdict:** SELECTED_CORRECT
- **Args valid:** True
- **Wall time:** 45.6s

### P2 -- DEM Asheville NC
- **Expected:** fetch_elevation / fetch_topobathy
- **Tools fired (in order):** ['geocode_location', 'fetch_dem', 'publish_layer']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 61.2s

### P3 -- Land cover Sacramento
- **Expected:** fetch_landcover
- **Tools fired (in order):** ['geocode_location', 'fetch_esri_landcover_10m', 'publish_layer', 'generate_histogram']
- **Verdict:** SELECTED_WRONG
- **Args valid:** True
- **Wall time:** 240.0s
- **Error:** `CONFIRMATION_TIMEOUT` -- code_exec_request '01KY5TEXA4XPTCQQ2W89CC69W5' approval card was not answered within 180s; the sandbox did not run

### P4 -- Buildings Savannah GA
- **Expected:** fetch_buildings
- **Tools fired (in order):** ['geocode_location', 'fetch_buildings', 'publish_layer']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 89.7s

### P5 -- River network Missoula
- **Expected:** fetch_rivers (or similar)
- **Tools fired (in order):** ['geocode_location', 'fetch_river_geometry', 'publish_layer']
- **Verdict:** SELECTED_WRONG
- **Args valid:** True
- **Wall time:** 44.1s

### P6 -- Earthquakes San Jose
- **Expected:** USGS earthquake fetcher
- **Tools fired (in order):** ['geocode_location', 'fetch_usgs_earthquakes']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 35.3s

### P7 -- Precip radar Kansas
- **Expected:** NEXRAD/radar fetcher
- **Tools fired (in order):** ['geocode_location', 'fetch_nexrad_reflectivity']
- **Verdict:** SELECTED_WRONG
- **Args valid:** True
- **Wall time:** 34.9s

### P8 -- Hillshade Boone NC
- **Expected:** compute_hillshade (+ DEM chain)
- **Tools fired (in order):** ['geocode_location', 'fetch_dem', 'compute_hillshade', 'publish_layer']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 28.3s

### P9 -- Avg elev Provo UT
- **Expected:** compute_zonal_statistics chain
- **Tools fired (in order):** ['geocode_location']
- **Verdict:** SELECTED_CORRECT
- **Args valid:** True
- **Wall time:** 36.5s

### P10 -- Pluvial flood Peoria IL
- **Expected:** run_model_flood_scenario
- **Tools fired (in order):** ['geocode_location']
- **Verdict:** SELECTED_WRONG
- **Args valid:** True
- **Wall time:** 240.0s
- **Error:** `CONFIRMATION_TIMEOUT` -- code_exec_request '01KY5TZ6KV9XBD7CK98CPX7HWH' approval card was not answered within 180s; the sandbox did not run

### P11 -- MODFLOW Bakersfield CA
- **Expected:** run_model_sustainable_yield_scenario
- **Tools fired (in order):** ['run_model_sustainable_yield_scenario']
- **Verdict:** SELECTED_CORRECT
- **Args valid:** True
- **Wall time:** 16.0s
- **Cancelled:** YES (solver cancel on start)

### P12 -- SWMM Alexandria VA
- **Expected:** SWMM composer
- **Tools fired (in order):** ['geocode_location', 'run_swmm_urban_flood']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 49.0s
- **Cancelled:** YES (solver cancel on start)

### P13 -- Tsunami Crescent City
- **Expected:** GeoClaw composer
- **Tools fired (in order):** ['geocode_location', 'run_geoclaw_inundation']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 37.7s
- **Cancelled:** YES (solver cancel on start)

### P14 -- Seismic hazard SF Bay
- **Expected:** OpenQuake composer
- **Tools fired (in order):** ['geocode_location', 'discover_dataset', 'list_categories', 'run_seismic_hazard_psha', 'resolve_fault_sources', 'stage_openquake_build_spec']
- **Verdict:** CHAIN_CORRECT
- **Args valid:** True
- **Wall time:** 31.4s
- **Cancelled:** YES (solver cancel on start)

### P15 -- Haiku (no tool)
- **Expected:** NO_TOOL
- **Tools fired (in order):** ['(none)']
- **Verdict:** NO_CALL_CORRECT
- **Args valid:** True
- **Wall time:** 14.2s

## Overall Stats

- **Prompts run:** 15
- **Tool prompts (1-14):** 14
- **SELECTED_CORRECT (first tool exact match):** 3/14
- **CHAIN_CORRECT (correct tool appeared, not first):** 7/14
- **Total selection accuracy (correct + chain):** 10/14 = 71.4%
- **SELECTED_WRONG:** 4/14
- **NO_CALL (tool expected, none fired):** 0/14
- **False-positive rate (P15 haiku):** 0/1
- **Mean time to completion (tool prompts with tool fired):** 70.7s

## VERDICT

**RAG TOP-K RETRIEVAL RECOMMENDED.**  Selection accuracy 71.4% (< 80% threshold) indicates the 8B model is struggling to select the right tool from a 176-tool context. RAG-based tool pre-selection (top-k relevant tools injected per query) would reduce context load and likely improve routing accuracy significantly. The model correctly abstained from tool use for the no-tool prompt (haiku). 
