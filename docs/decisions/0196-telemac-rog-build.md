# ADR 0196 - TELEMAC-2D rain-on-grid build wave (mesh-acquisition step + runoff-path selector; worker/template/live-proof spec)

Date: 2026-08-08
Status: LANDED (2026-08-08) -- C1-C4 all complete and live-verified. The offline
substrate (Decisions 1-2) landed first; the bounded continuation (C1 worker RoG
deck + C2 parser bump + image rebuild + C3 template registration + C4 live
Coweeta proof) landed in the continuation wave. See "Continuation landed" below.
Source: Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205 (NATE-provided).
Builds on: ADR 0195 (RoG foundation), ADR 0193 (pysheds watershed mesh),
ADR 0190 (RAIN OR EVAPORATION deck seam), ADR 0158 (strict manifest parser).

## Context

ADR 0195 landed the RoG foundation (NSE/R2 primitives, the hydrograph overlay
chart, the SCS-CN infiltration module) and explicitly deferred the registered
`telemac_rain_on_grid` template body, the worker deck/parser changes, the
`telemac:latest` rebuild and the hours-class live Coweeta proof, because the
worker-image law requires a full rebuild + behavior-proving smoke THROUGH the
image and the Coweeta storm solve is hours-class -- neither completable nor
verifiable inside one build session without shipping unverified worker code.

This wave lands the next offline-verifiable increment and freezes the design for
the rest so the remainder is a bounded, low-risk continuation rather than an
open-ended build.

## Decision 1 - watershed mesh acquisition promoted to a template STEP (LANDED)

`workflows/telemac/rain_on_grid/mesh_acquisition.py`. The ADR 0193 watershed-
first mesher is lifted (not CLI-coupled) into an importable step with a
PRECONDITION-GATE shape so a user-supplied mesh slots in behind one interface:

- `acquire_watershed_mesh(pour_point, bbox, ...)` -- the "build our own" provider:
  `delineate_watershed` -> catchment polygon; `fetch_river_geometry` -> the
  interior river network; `fetch_dem` -> the bed; the GPL-isolated
  `trid3nt-local/mesh:latest` OceanMesh2D image triangulates the catchment
  interior refined by distance-to-river; the lon/lat nodes are projected to the
  local UTM zone (TELEMAC solves in METRES) and written as a BOTTOM SELAFIN.
- `use_supplied_mesh(mesh_path, ...)` -- the pass-through the future user-mesh
  path uses, validated to the same `WatershedMesh` shape.

Both return a `WatershedMesh` (SELAFIN path + catchment polygon + node arrays +
`provenance` in {`delineated`, `user_supplied`}). The standalone sandbox stays
standalone; the SELAFIN writer, IPOBO, UTM projection, config building and
exterior/river extraction are lifted so nothing imports `scripts/sandbox/*`.

Pure helpers are unit-tested offline (`test_telemac_rain_on_grid_mesh_acquisition.py`,
18 tests): config build + edge-band/ring validation, exterior+river clip, UTM
projection (Coweeta -> EPSG:32617), node CN2/Manning assembly, the supplied-mesh
gate, and the SELAFIN header round-trip. The container-driven build path is
exercised live (needs the mesh image + network).

## Decision 2 - automatic runoff-path selection (LANDED)

`cn_infiltration.select_runoff_path(...)` -> `RunoffPathDecision(path,
time_varying, reason)`, recorded in the run envelope:

- CONSTANT-intensity rain (a design storm: one rate over a duration, or a flat
  hyetograph) -> `native`: TELEMAC's own SCS-CN runoff model
  (`RAINFALL-RUNOFF MODEL = 1` + `ANTECEDENT MOISTURE CONDITIONS` + the per-node
  CN2 field via `FORMATTED DATA FILE 2` / `HYDROMAP`), with the steep-slope
  correction applied to the CN field in preprocessing (the engine branch is
  compiled off, ADR 0195).
- TIME-VARYING rain (an hourly MRMS hyetograph, >= 2 distinct non-zero rates) ->
  `preprocessing`: `rainfall_excess_hyetograph` applies eq 7-8 up front and the
  net (excess) series drives TELEMAC as time-varying rain with
  `RAINFALL-RUNOFF MODEL = 0` (no double counting) -- overcoming the hardcoded
  `RAINDEF=1` constant-rain limit in the installed v9.0.0 build.

The composer calls this, records `runoff_path` + `runoff_path_reason` in the
envelope, and threads `runoff_path` into the worker manifest so the deck builder
authors the matching branch. Offline-tested (4 cases).

## Continuation LANDED (2026-08-08) -- C1-C4

All four continuation items landed and were live-verified through the rebuilt
image. Grounding note: the deck was authored against the ACTUAL installed
v9.0.0 sources (runoff_scs_cn.f, hydromap.f, friction_read.f/friction_user.f,
telemac2d.dico), not guessed.

- C1 worker RoG pipeline: `services/workers/telemac/rog_build.py` +
  `mode="rain_on_grid"` dispatch in `entrypoint.py::run_rog_pipeline`. Consumes
  the staged watershed SELAFIN, rebuilds the boundary + rank IPOBO, marks the
  ring nodes nearest the pour point a FREE exit (KSORT=4, NO fixed stage; the
  rain-fed interior drains out there) with walls (KLOG=2) elsewhere. Writes the
  per-node CN2 scatter (FORMATTED DATA FILE 2, read by HYDROMAP) + distributed
  Manning as native friction ZONES (FRICTION DATA FILE zone laws + ZONES FILE
  node->zone via friction_user KFROPT) + the deck (RAIN OR EVAPORATION +
  RAINFALL-RUNOFF MODEL=1 + AMC + OPTION FOR INITIAL ABSTRACTION RATIO for the
  native path; MODEL=0 net-rain for preprocessing). Extracts the outlet
  hydrograph (unit discharge integrated across the outlet edges per frame),
  max-depth/velocity fields, and the WATER VOLUME continuity from the listing.
  NOTE (installed-build limit): runoff_scs_cn.f hardcodes RAINDEF=1, so the
  native path is CONSTANT design-storm intensity; a true time-varying MRMS
  hyetograph needs a recompiled user_rain.f (the preprocessing path collapses
  the excess series to a mean for the constant-rain engine). Offline-first: a
  synthetic tilted-plane catchment reaches CORRECT END with continuity -4e-16
  and a monotone AMC knob (dry 1.26 / normal 4.99 / wet 8.28 m3/s).
- C2 ReachConfig RoG fields + parser bump telemac-reach-2 -> telemac-reach-3
  (unknown-field rejection test names v3). Image rebuilt (id 4835445811db,
  `docker history` GRACE-2 refs = 0); parser v3 accept/reject + a solve verified
  THROUGH the baked image. 16 worker unit tests green.
- C3 template `telemac_rain_on_grid` (engine=telemac, tier=template,
  cacheable=False, ttl live-no-cache, source workflow_dispatch): composes
  acquire_watershed_mesh + fetch_landcover node CN/Manning + select_runoff_path +
  the run_solver seam + postprocess_telemac_wse (peak-depth COG). corpus.yaml +
  categories mapping + 6 offline tests + door-dissolution expected-set bump (31
  -> 32) + retrieve_visible_tools surfaces it. Showcase seeded (natural prompt
  "Coweeta Creek watershed North Carolina"); verified !run line:
  `!run telemac_rain_on_grid(location='Coweeta Creek watershed North Carolina', antecedent_moisture='normal', design_storm_mm_per_hr=25.0, storm_duration_hr=6.0)`
- C4 live Coweeta proof (through the rebuilt image, via the direct driver
  scripts/sandbox/telemac/rog_coweeta_live.py -- the SAME worker path the
  template dispatches): delineated Coweeta Creek NC catchment (28.72 km2, 4854
  nodes / 9521 tris, 3DEP bed, NLCD-distributed CN 75-90 + Manning 0.05-0.20),
  AMC II (peak 45.5 m3/s, runoff 162x10^3 m3, maxH 6.95 m, continuity 1.3e-15)
  vs AMC I dry (peak 6.1 m3/s, runoff 9.6x10^3 m3, maxH 1.21 m). Both CORRECT END
  in ~45 s wall. Proofs (EPSG:3857, both tiles + data projected): docs/proof/
  templates/telemac_rain_on_grid.png (peak depth self-organizing into the real
  dendritic drainage over ESRI + red catchment), _chart.png (dock hydrograph,
  AMC II vs AMC I overlay + NSE/R2 slot), _mesh.png (TIN wireframe). Template
  smoke, NOT the replication experiment (no calibration; NATE has not signed off
  the methodology).

## Bounded continuation (original spec; now LANDED per above) -- reasons per item

### C1 - worker RoG pipeline + deck authoring (needs image rebuild + live smoke)
A new manifest mode `mode="rain_on_grid"` in `services/workers/telemac/entrypoint.py`
that, instead of the river-dye channel pipeline, consumes a supplied watershed
SELAFIN (from Decision 1, staged into the rundir), and authors a RoG deck in
`telemac_river_dye_build.author_deck` (extended) / a sibling `rog_deck` writer:
  - full-mesh rain via the ADR 0190 `RAIN OR EVAPORATION` seam with the MRMS
    hyetograph (preprocessing path: net rain) OR constant intensity (native path);
  - native path: `RAINFALL-RUNOFF MODEL = 1`, `ANTECEDENT MOISTURE CONDITIONS =
    {1|2|3}`, `OPTION FOR INITIAL ABSTRACTION RATIO`, `FORMATTED DATA FILE 2` =
    the per-node CN2 map (+ `HYDROMAP`);
  - Manning friction distributed per NLCD (per-node `FRICTION DATA FILE` / friction
    zones), Strickler/Manning law;
  - outlet BC at the catchment pour point = normal-depth / friction-slope
    (`OPTION FOR LIQUID BOUNDARIES = 1`, elevation imposed by Manning normal depth;
    NO fixed stage), all other boundary nodes closed (rain-fed interior);
  - deliverables: outlet discharge hydrograph (integrate unit discharge across
    the outlet boundary edges per frame -> the PRIMARY product), max-depth +
    max-velocity COGs over the catchment, runoff-volume + mass-balance continuity
    from the listing WATER VOLUME block.
Reason deferred: worker code is inert until rebuild; must end with a rebuild +
behavior-proving live smoke through the image (worker-image staleness lesson).

### C2 - RoG ReachConfig fields + parser bump telemac-reach-3 + rejection test
Add RoG fields to `ReachConfig` (`mode`, `watershed_slf`, `runoff_path`,
`curve_number`, `amc_condition`, `rain_hyetograph_mm`, `rain_intensity_mm_per_hr`,
`rain_duration_s`, `node_cn2_file`, `node_manning_file`, `outlet_lonlat`,
`observed_gauge_id`) and bump `_PARSER_VERSION` `telemac-reach-2` ->
`telemac-reach-3` (the strict allowlist auto-covers new dataclass fields; the bump
is the version stamp in the unknown-field error). Rejection test: a manifest with
a bogus `reach` key raises `TelemacManifestUnknownFieldsError` naming
`telemac-reach-3`. Reason deferred: ships with C1 (same image rebuild).

### C3 - template `telemac_rain_on_grid` + registration hygiene
`rain_on_grid/rain_on_grid.py`: the registered engine template
(`engine="telemac", tier="template"`, `cacheable=False`,
`ttl_class="live-no-cache"`, `source_class="workflow_dispatch"`), mirroring
`telemac_river_dye`. KNOBS: `curve_number` (uniform override), `amc_condition`
(I/II/III), rain from an MRMS window OR `design_storm_mm_per_hr` + `duration`,
`observed_gauge_id` (USGS NWIS) wiring NSE/R2 + the hydrograph overlay chart.
Docstring carries the Godara-2024 applicability envelope verbatim-class (ADR 0195
"Applicability envelope"): single-storm ~10-20 h events only; multi-peak
sustained flow NOT reproduced (infiltrated water lost, no return flow); steep
terrain favors triangular-mesh TELEMAC. Registration: `tool_query_corpus.yaml`
queries + a model-free `retrieve_visible_tools(prompt, None, 8)` top-8 check +
`categories.py` primary mapping + offline tests BEFORE live + a
module-coverage-board row + a showcase case via `seed_showcase_cases.py` (natural
prompt "Coweeta Creek watershed North Carolina") with the verified `!run` line.
Reason deferred: a registered solver template must pass the corpus retrieval check
+ offline tests + a live smoke before acceptance (new-tool retrieval-corpus rule).

### C4 - live Coweeta proof + proofs
Coweeta Creek NC (ADR 0193 catchment, pour point -83.40402 35.05746), a verified
MRMS window (2020-10+; TS Fred remnants 2021-08-17/18 candidate, MRMS coverage to
be verified for those hours). Foreground-patient through `run_solver` + MinIO.
Knob demo: CN2 (AMC II) vs CN dry (AMC I) -- two hydrographs on one chart.
Proofs to `docs/proof/templates/`: `telemac_rain_on_grid.png` (max-depth COG over
ESRI + catchment boundary), `_chart.png` (hydrograph incl. knob overlay,
dock-exact), `_mesh.png` (separate). Norms: white box = AOI residual only, pinned
scales in captions, no annotation boxes, workflow name in the caption strip. This
is a TEMPLATE SMOKE, NOT the replication experiment (NATE has not signed off that
methodology). Reason deferred: hours-class solve, not completable/verifiable in a
single build session (ADR 0195 rationale).

## Applicability envelope (unchanged from ADR 0195; bake into the template docstring)

RoG reproduces SINGLE-STORM flash-flood events (~10-20 h) in small steep
catchments. Multi-peak / sustained rain-on-snow is NOT reproduced (infiltrated
water permanently lost, no subsurface return flow -> inter-peak baseflow missed).
TELEMAC-2D's triangular mesh is stable on steep terrain (a paper finding vs
HEC-RAS's structured grid). US-only via our fetchers; Coweeta NC is the US steep
gauged replication site for the Sleddalen (Norway) methodology.

## Consequences

- +0 registered tools this wave (mesh acquisition + the runoff selector are
  functions; the template is C3). Registry unchanged.
- No worker image rebuilt this wave; no flood seam touched -> no flood canary.
- Offline: `test_telemac_rain_on_grid_mesh_acquisition.py` (18) +
  `test_telemac_rain_on_grid_cn.py` (15, unchanged). No regression elsewhere.
- The continuation (C1-C4) is bounded: one image rebuild carries C1+C2, then
  C3 registration + C4 live proof, in that order.
