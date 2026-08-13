# ADR 0122 -- Hazard easy-four build wave (ADR 0121 greenlit subset)

Status: partial (2026-08-04, NATE AFK easy-tier authorization + cheap-smoke rule)
-- #1 LANDED; #2-#4 scoped-ready, handed forward at a clean boundary (see per-row).
Follows: 0121 (S-tier wave 2 triage -- identified the four contained
server-side/exec-mode feature builds), 0120 (wave-1 lesson + the template hygiene
gate), 0107 (two-mode input gate), the physics_registry seam.

## Context

ADR 0121's triage ground-truthed thirteen hazard-cluster candidate rows and found
NONE was a pure-knob drop-in, but named FOUR as genuine, contained,
live-verifiable-without-an-image-rebuild feature builds (cheapest-first):

1. landlab_flow_accumulation (folds rows 8 + 9; exec-mode, seconds-long smoke)
2. landlab_green_ampt_overland_flow (exec-mode)
3. elmfire_verification_elliptical_replication (server-side, highest value)
4. geoclaw_tsunami_gauge_timeseries (server-side, docker smoke)

This ADR records the build wave over that subset. Each row is taken to the full
wave bar (one-file composer; gates; synthetic_inputs; fidelity/off-scope; corpus
+ model-free retrieval proof; offline tests; live cheap-smoke with numbers + URIs;
hygiene lint; board flip; metrics row) or STOPPED honestly with a precise blocker
if it genuinely walls -- never forced.

## Decision -- per-row outcomes

### #1 landlab_flow_accumulation -- LANDED

A NEW capability-named template (a separate registered tool, NOT an enum
extension of `landlab_susceptibility`): flow accumulation and channel-network
extraction is a distinct question class from landslide susceptibility per the
capability-naming rule, so it earns its own tool at an honest +1 registration
cost. Rows 8 (`flow_accumulation_standalone_layer`) and 9
(`priority_flood_large_aoi_routing`) are FOLDED into it, plus
`multi_flow_direction_routing`.

What landed:

- **Worker** (`services/workers/landlab/component_chain.py`): a new
  `analysis="flow_accumulation"` branch. `FlowAccumulator` (fill /
  DepressionFinderAndRouter, D8-only) or `PriorityFloodFlowRouter` (priority_flood,
  any director -- the folded row-9 component) with `flow_director` D8 / Dinf / MFD
  (carried via `advanced_physics` through `physics_registry["landlab"]`) and a
  `channel_threshold_cells` channel-head knob. PRIMARY output = `drainage_area`
  (m^2); SECONDARY = a channel-network boolean mask + slope. A cheap 3-director
  routing comparison (D8/Dinf/MFD, all priority-flood routed for a fair metric-only
  contrast) records per-director {max_drainage_area_km2, channelized_area_fraction}.
- **Contracts** (`landlab_contracts.py`): `flow_accumulation` added to
  `LandlabAnalysis` (+ synonyms); `depression_handler` (`LandlabDepressionHandler`
  literal, aliased) + `channel_threshold_cells` first-class run-args; a new
  `LandlabFlowAccumulationLayerURI` carrying `max_drainage_area_km2` /
  `mean_drainage_area_km2` / `channelized_area_fraction`.
- **Composer** (one file, `workflows/landlab/flow_accumulation/flow_accumulation.py`):
  the `landlab_flow_accumulation` tool + `model_landlab_flow_accumulation`, reusing
  the susceptibility composer's DEM-fetch / download / AOI-floor helpers (the shared
  Landlab off-box seam). The two-mode input gate presents the routing knobs;
  synthetic_inputs is empty by construction (the DEM is real-fetched; the routing /
  depression / threshold knobs are deterministic engine settings, not synthetic
  data -- stated in `source_note`).
- **Postprocess** (`postprocess_landlab.py`): `postprocess_landlab_flow_accumulation`
  reprojects the drainage-area COG to 4326 (reusing `continuous_drainage_area`
  styling -- a dedicated log-DOMAIN TiTiler expression is a NAMED RESIDUAL),
  vectorizes the channel mask to a EPSG:4326 GeoJSON channel-network vector, and
  `build_routing_comparison_chart_spec` builds the D8/Dinf/MFD routing-comparison
  Vega-Lite chart (the the_FlowAccumulator tutorial's central figure).
- **Registration**: imported in `tools/__init__.py`; `hazard_modeling` category;
  co-located `corpus.yaml`. Registered 181 -> 182 (test_catalog_surfacing pins
  bumped), templates 23 -> 24, coded tools 83 -> 84.

Live cheap-smoke (exec, `run_chain.py`, synthetic UTM DEM, seconds, no MinIO):
drainage-area COG + channel + slope secondary COGs + `landlab_result.json` with
the flow_accumulation block. Numbers: max drainage_area 4.176 km2, channelized
fraction 0.011, routing_comparison 3 rows (D8 0.011 / Dinf 0.129 / MFD 0.123);
determinism verified (byte-identical field on re-run). Model-free retrieval:
`landlab_flow_accumulation` in the top-8 for all four flow-accumulation phrasings.
Offline tests: 2 worker (determinism + 3-director comparison, real landlab) + 9
server. Hygiene lint passes. No flood seam touched (grep-verified; no canary
mandated).

Source (paper-first): the canonical Landlab `the_FlowAccumulator` tutorial +
`the_Flow_Director_Accumulator_PriorityFlood` tutorial.

### #2 landlab_green_ampt_overland_flow -- NOT ATTEMPTED THIS SESSION (scoped-ready)

Deferred to the next session at a CLEAN boundary (Template #1 fully landed + the
offline suite verified back to the 9-by-SET baseline). Rushing #2-#4 in the
remaining budget would risk half-built work across three more engines + a broken
suite -- against the no-half-done / clean-as-you-go doctrine and the close-out
rule. The wall is budget/wall-clock (each remaining row is a full multi-file build
with its own live smoke -- docker solves for #3/#4 -- and ideally its own ~18-min
suite run), NOT a technical blocker. Scoped-ready: `SoilInfiltrationGreenAmpt`
confirmed importable in venvs/agent; exec-mode (no image rebuild); reuses the
landlab off-box seam + the Atlas-14 rainfall seam (`_atlas14_design_storm_mm`
pattern) this wave's #1 sits beside. Build: a Green-Ampt infiltration branch on the
overland chain (infiltration-depth + runoff-depth rasters + the partition chart;
soil K / capillary head / moisture deficit as demo-labeled synthetic_inputs).

### #3 elmfire_verification_elliptical_replication -- NOT ATTEMPTED THIS SESSION (scoped-ready)

Scoped-ready: `trid3nt-local/elmfire:trid3nt-verify` image present (docker smoke
feasible); deck authored ON THE AGENT HOST (live drop-in, no rebuild). Build: a
fuel_model constant-raster override (extend `write_constant_raster` to force
GR2/102) + a verification postprocess that vectorizes the ToA raster into
isochrones, generates the closed-form ellipse, diffs within the documented <0.5%
tolerance, and emits the ellipse-overlay chart + a verification triple.

### #4 geoclaw_tsunami_gauge_timeseries -- NOT ATTEMPTED THIS SESSION (scoped-ready)

Scoped-ready: `trid3nt-local/geoclaw:latest` image present (heaviest smoke -- a
small coarse-AMR short-window docker solve). Build: widen the
`_download_batch_geoclaw_outputs` `fort.`-only key filter to include
`gaugeNNNNN.txt` + a gauge parser + an elevation-timeseries chart (co-seismic
subsidence visible where the case has it).

## Consequences

- Registered 181 -> 182, templates 23 -> 24, coded tools 83 -> 84, category
  `hazard_modeling`. No coded-fetcher / spec-served change. Offline baseline
  intact (the exactly-9-by-SET failure set; no fetcher / router / flood-seam
  change). Board rows `flow_accumulation_standalone_layer` +
  `multi_flow_direction_routing` + `priority_flood_large_aoi_routing` flipped
  CAND -> LANDED (all covered by the one folded template).
- LossyFlowAccumulator remains unsurfaced (out of scope; noted on the board).
- The remaining three rows (#2-#4) are handed forward per the cheap-smoke rule;
  their outcomes are appended to this ADR as they land or honestly STOP.
