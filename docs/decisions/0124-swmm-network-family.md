# ADR 0124 -- SWMM real-network family (network import + dual-drainage coupling)

Status: LANDED (2026-08-04) -- rows #1 (swmm_network_import) and #2
(swmm_dual_drainage_coupling) landed to the full wave bar with LIVE cheap-smoke
evidence on a real public municipal storm-drain network. Rows #3-#7 STOPPED with
precise blockers (published-deck runner is a separate capability).
Follows: the SWMM practice-verification (2026-08-04) whose verdict ranked the
missing REAL sewer-network import as the #1 gap; 0120 (template hygiene gate),
0106 (labeled synthetic_inputs), 0107 (two-mode input gate), 0104 (SWMM subprocess
solve isolation).

## Context

The SWMM practice-verification concluded our quasi-2D overland mesh is a legitimate
published technique (PCSWMM's own "2D" mode is the same storage-node/open-conduit
pattern), BUT that every dual-drainage source treats it as ONLY HALF of the model:
real urban-drainage projects START from a municipal storm-sewer network imported
from GIS, coupled to the surface at inlets. We had NO real-network path -- the mesh
was DEM-synthesized only. This wave closes that gap.

## Decision -- per-row outcomes

### #1 swmm_network_import -- LANDED

Build a runnable SWMM model from a REAL municipal storm-drain GIS network (nodes +
conduits) instead of the DEM-synthesized mesh.

- **Engine core** (`agent/mesh/swmm_network.py`, NEW): `parse_network_features`
  turns node (Point) + conduit (LineString) GeoJSON FeatureCollections into
  SWMM JUNCTIONS + OUTFALLS + CONDUITS. Honest v1 handles real-world messiness:
  - **Flexible, alias-aware field resolution** (`invert_elev`/`InvertElev`/`IE`/
    `Geom1`/`DIAMETER`/`NODE_UP`/...), because no two municipal schemas agree.
  - **Topology from the conduit graph** (the authoritative source): explicit
    from/to ids when they match the node layer, else endpoint-snapping, else a
    SYNTHESIZED node at the conduit endpoint -- a conduit is NEVER dropped just
    because the conduit layer uses a different node-id scheme than the node layer
    (the common real case: gravity-main `NODE_UP`/`NODE_DN` vs manhole `MHno`).
  - **Labeled-degrade (ADR 0106)**: missing inverts are DEM-sampled (ground minus
    a demo burial depth) else slope-walked from known inverts; missing diameters
    take a labeled default; diameter UNITS are inferred by magnitude (m / inches /
    mm). Every fill count is surfaced (`n_inverts_filled` / `n_topology_snapped` /
    `n_diameters_defaulted`).
  - **SWMM outfall rule (ERROR 141/145)**: an outfall takes EXACTLY ONE link; a
    multi-link outfall is demoted to a junction with a dedicated single-link
    outfall appended (the raster_cell_mesh P0 pattern).
  - `build_network_inp` loads each junction with a labeled per-junction demo
    sub-area draining the Atlas-14 nested hyetograph (imported exports carry no
    sub-catchment delineation), so the network ACTUALLY ROUTES flow to its outfall;
    `run_network_deck` runs one-shot `swmm5_run` + the Flow-Routing-Continuity
    honesty gate; `network_to_geojson_4326` emits the network as a vector (nodes
    coloured by max HGL / flooding, conduits by surcharge).
- **Composer** (`workflows/swmm/network_import/network_import.py`, NEW):
  multi-source input loading -- inline FC, `s3://`, `file://`, `https://` GeoJSON,
  and keyless ArcGIS `FeatureServer`/`MapServer` layer queries (`f=geojson`,
  paginated). Optional DEM fetch for invert filling; Atlas-14 design storm. Returns
  a `SWMMNetworkLayerURI` (NEW contract) carrying the typed network + response
  scalars. A single combined file is split by geometry.
- **LIVE cheap-smoke** (real public Houston-area TX municipal
  `Storm_Sewer_System` FeatureServer, keyless): 185 manholes + 518 gravity mains
  fetched in 1.7 s -> **484 junctions, 473 conduits, 47,298 m of pipe** -> DEM-
  interpolated inverts (all 484 gap-filled; labeled) -> solve **peak outfall
  2.906 CMS, 1.115e4 m3, 464 flooded nodes, 472 surcharged conduits, continuity
  +0.482%**. Layer published to `s3://trid3nt-runs/<rid>/network.geojson`.

### #2 swmm_dual_drainage_coupling -- LANDED

Couple the overland MAJOR system (the `swmm_urban_flood` mesh) with the imported
piped MINOR system (row #1) in ONE deck, exchanging flow at inlets -- the DEFINING
dual-drainage feature.

- **Engine core** (`build_dual_drainage_inp` in `swmm_network.py`): merges the
  already-built overland mesh deck with the parsed pipe network -- pipe
  junctions/conduits/outfalls added (prefixed `P_` to avoid `S_`/`C_` collisions),
  each pipe junction linked to the overland cell it falls in (`rowcol` on the mesh
  affine) by a single INLET orifice (surface `S_<r>_<c>` -> `P_<junction>`): a
  catchbasin that captures surface flow into the pipe AND lets a surcharging pipe
  back water onto the street (bidirectional). The combined deck's `S_` nodes +
  `grid_shape` are UNCHANGED, so the existing `run_swmm_deck` (subprocess isolation
  + mass-balance gate + peak-depth grid, ADR 0104) and `postprocess_swmm`
  (depth COG) work on it VERBATIM; the pipe response is read separately from the
  same `.rpt` (`read_network_response`, filtered to the pipe nodes).
- **Composer** (`workflows/swmm/dual_drainage/dual_drainage.py`, NEW): reuses the
  urban_flood DEM/buildings/precip fetchers + `build_and_stage_swmm_deck` +
  `_publish_peak_layer`, and the network_import loaders. Returns a
  `SWMMDualDrainageLayerURI` (NEW; subclasses `SWMMDepthLayerURI`) -- the overland
  depth raster PRIMARY + the coupled minor-system scalars; the pipe network is
  emitted as a context overlay.
- **LIVE cheap-smoke** (same Houston-area TX network, ~1 km AOI with 3DEP coverage, 15 m
  mesh): overland **max_depth 0.246 m over 0.0188 km2**, **27 inlets** coupling
  surface cells to the pipe network, **34 pipe conduits surcharged** from captured
  surface flow, continuity within gate. Overland depth COG published (auto-COG
  overviews).

### #3-#7 -- STOPPED with blockers (published-deck runner is a separate capability)

`swmm_lid_raingarden_wq`, `swmm_green_grey_infra_storms`, `swmm_cso_regulator_network`,
`swmm_wwtp_detention_ponds`, `swmm_pump_pid_rtc` all cite PRE-BUILT published `.inp`
decks (openswmm.org / EPA Applications-Manual Example 8), each carrying capabilities
NEITHER the DEM-mesh builder NOR the GIS-network parser produces: LID controls,
stage-storage curves, flow regulators, pumps, and RTC/PID control rules. A standard
nodes/conduits GIS export does not carry these, so they do NOT consume the row-#1
network machinery. The honest path is a SEPARATE "published-deck runner" capability
(ingest a specific published `.inp` -> run via the existing subprocess solver ->
postprocess), OR bespoke deck synthesis per deck -- both orthogonal to network
import. Queued for a dedicated wave; see the report for per-row detail.

## Consequences

- Two new engine="swmm" tier="template" tools; CODED tools +2. Retrieval corpus +
  model-free `retrieve_visible_tools` proof for both. Hygiene lint passes.
- `swmm_network.py` is a NEW module SIBLING to `raster_cell_mesh.py`; NO edit to
  `raster_cell_mesh.py`, `urban_flood.py`, or any flood/SFINCS seam (grep-verified).
  The dual-drainage core REUSES `run_swmm_deck` + `postprocess_swmm` verbatim.
- Known limitation surfaced by the smoke (NOT introduced here): `_fetch_dem_for_urban`
  returns a 3DEP 1 m tile even when it is all-nodata for an uncovered AOI (the 10 m
  fallback never fires); the AOI for the row-#2 smoke was chosen inside coverage.
  Logged for a future urban_flood robustness fix.
