# Mesh layer extraction (SIGNED - NATE 2026-08-03)

Sign-off decisions (NATE, decision picker 2026-08-03):
1. M4 = BUILD the SFINCS quadtree leg for real atop the mesh layer
   (cht_sfincs/hydromt quadtree, GPL-isolated worker) - the SFINCS validation case
   keeps its variable-resolution promise; the inert stub dies when the
   real leg replaces it.
2. Mesh preview/approve gate: ON by default for tin paradigms (TELEMAC
   precedent), per-run-mode (USER-GATED) elsewhere; wireframe layer
   always published.
3. MODFLOW DISV/gridgen goes INTO the M-waves: M2 gains a disv generator
   component alongside the folds - refinement-capable groundwater grids
   from day one.

NATE doctrine 2026-08-03: data + MESH + compute engine = the big 3
ingredients of solid models. Data has its universal layer (source.yaml
ingest); compute has workers + templates; mesh is the last pillar still
scattered as private steps inside engine composers. This spec extracts it.
Census: the 2026-08-03 mesh tooling inventory (agent aacb31e, full text in
the session transcript; key facts restated inline with file:line cites).

## What the census actually found

- The only real meshers in the repo: TELEMAC's gmsh channel mesher
  (telemac_river_dye_build.py:768 - triangulation, bank offsets, island
  holes, SELAFIN writer, and a WORKING mesh-preview/approve gate at
  entrypoint.py:284) and SWMM's node-link builder
  (swmm_mesh_builder.py:958 - DEM cells -> storage/conduit mesh, buildings
  as obstructions, drawn walls/flap-gates snapped to edges, autoscale
  resolution ladder).
- TRUTH CORRECTION: the SFINCS "quadtree+SnapWave" path is an INERT STUB -
  the flag falls through to the regular grid; build_sfincs_quadtree_deck
  exists nowhere; only the read-side probe was built. The flagship SFINCS
  validation case's mesh leg is 0% implemented and fails honestly when
  triggered.
- oceanmesh: ZERO footprint (no code, no dep, prose only). Purely queued.
- SFINCS/SWAN/MODFLOW/GeoClaw: regular grids or patch-AMR our deck
  builders emit directly; resolution is the only lever.
- User-drawn geometry seam (request_spatial_input -> role-tagged FC ->
  typed parse) is real and general, but only SWMM consumes drawn lines;
  drawn AOI polygons clip NOTHING today (parsed, then unused).
- No shared triangulation utility: gmsh lives ONLY in the TELEMAC worker
  image (GPL - isolation is load-bearing); sandbox/server have scipy
  spatial only.

## Design principle (NATE 2026-08-03): fold by duplication, not abstraction

The mesh landscape stays heterogeneous - that is fine. The layer does NOT
chase a universal mesh; it chases REUSABILITY: wherever templates/engines
declare the same bespoke mesh code, fold those declarations into ONE
insertable component a workflow calls. Concrete duplication found by the
census (the fold targets, in twin-fold order):
- MODFLOW ModflowGwfdis declared at 5 call sites (gwt_adapter.py:1414,
  2436, 2839, 3341, 4016) serving 12 templates -> one dis_grid component.
- Regular-grid-from-bbox geometry math re-derived by SFINCS (hydromt YAML
  config), SWAN (_grid_geometry deck_builder.py:369), MODFLOW -> one
  grid_geometry component, three consumers.
- Barrier/drawn-line edge-snapping exists once (SWMM builder :752) but is
  needed by every engine -> fold-and-share.
- Mesh preview wireframe + approve gate exists once (TELEMAC
  entrypoint.py:284) -> fold-and-share.
Components compose in a standard order (domain -> generate -> enforce ->
write), but the COMPONENTS are the product; the composition is just how a
workflow inserts them.

## The layer: reusable components + thin writers

A mesh authoring layer at server/src/trid3nt_server/agent/mesh/; engine
composers insert its components instead of carrying private mesh code.

1. DOMAIN: AOI (bbox OR drawn polygon - wiring the currently-dead
   aoi_features path), terrain handle, feature lines with roles:
   breakline | barrier (wall/flap_gate) | breach | refine_region |
   boundary (inflow/outflow). User-drawn structures enter HERE, once,
   for every engine (generalizing SWMM's snap pattern + the FR-WC-16 FC).
2. GENERATE: paradigm chosen by the target solver family -
   regular_grid (SFINCS/SWAN/MODFLOW-DIS), raster_cell_graph (SWMM),
   tin (TELEMAC, HEC-RAS; gmsh in an isolated mesh-capable worker, GPL
   respected), amr_patches (GeoClaw regions/ratios). Resolution is THE
   USER LEVER (granularity-gate doctrine): uniform target + per-region
   refinement (from refine_region features) + the SWMM autoscale ladder
   promoted to a shared suggest-then-override.
3. ENFORCE: breaklines constrain edges; barriers cut/gate connectivity;
   holes (buildings, islands) carved; boundary tags assigned.
4. WRITE: thin per-solver terminal writers - swmm_inp (relocate from
   swmm_mesh_builder), telemac_slf (relocate), swan_cgrid, modflow_dis
   (DISV later), geoclaw_setrun_regions, sfincs_regular (hydromt YAML;
   quadtree later), hecras_geometry (NEW - cells + breaklines to the
   geometry HDF; hydraulic property tables computed by HEC's own Linux
   RasGeomPreprocess inside the worker, NOT by us).

Cross-cutting, promoted from existing code to pipeline features:
- MESH PREVIEW/APPROVE GATE: TELEMAC's mesh_only + wireframe-geojson
  preview generalizes to every paradigm; slots directly into the
  two-mode run doctrine (AUTO = proceed + labeled mesh stats; USER-GATED
  = preview wireframe on canvas, approve, then solve). Render-mesh-in-
  proofs norm becomes free (the wireframe layer is a pipeline output).
- MESH METADATA in envelopes: cell count, resolution(s), refinement
  regions, source of each feature line (drawn vs fetched vs default) -
  feeds the structured-provenance work.

## What this deliberately does NOT do

- No universal mesh format (meshes are heterogeneous by construction;
  the pipeline is shared, the writers are not).
- No gmsh in the server venv (GPL isolation stays; tin generation runs
  in a mesh-capable worker container, pattern proven by TELEMAC).
- No oceanmesh adoption in the extraction waves (it lands INTO stage 2
  as a coastal tin generator when its wave comes; GPL-3 - worker-side).

## Waves (each identity-gated, orchestrator commits)

- M1 EXTRACT: pipeline skeleton + relocate the SWMM builder and TELEMAC
  mesher behind it. Zero behavior change (byte/value-identical decks on
  golden fixtures = the gate). Composers' private mesh code deleted as
  it relocates (ledger rows).
  SPOT-CHECK DELIVERABLE (NATE 2026-08-04): every relocated/folded mesh
  path publishes its mesh AS A LAYER on a real AOI - SWMM cell polygons,
  TELEMAC triangle wireframe, and a NEW regular-grid outline preview for
  the folded grid_geometry component (SFINCS/SWAN/MODFLOW today have no
  preview at all) - through the normal layer pipeline so NATE can load
  and spot-check them in QGIS, with honest screenshots attached to the
  wave report (screenshot-proof norm).
- M2 GENERALIZE: drawn-geometry roles (breakline/breach/refine_region/
  aoi-clip) wired for ALL engines; the dead aoi_features path either
  wired or deleted; mesh preview/approve gate shared; SWMM+TELEMAC
  consume the generalized stage (live proofs: drawn wall in SWMM,
  drawn refine region in TELEMAC).
- M3 HECRAS WRITER: hecras_geometry writer + RasGeomPreprocess in the
  HEC-RAS worker; Muncie replication = the acceptance gate (feeds the
  HEC-RAS engine landing).
- M4 QUADTREE TRUTH: SIGNED as BUILD (decision 1 above) - the SFINCS
  quadtree leg gets built for real atop stage 2 (cht_sfincs/hydromt
  quadtree, GPL-isolated); the inert stub dies when the real leg
  replaces it.
  DELIVERED (2026-08-04, ADR 0113): built for real with cht_sfincs==1.0.0
  (GPL-3, worker-isolated) -- hydromt_sfincs CANNOT author a quadtree from
  scratch at any version (setup_grid TODO + setup_dep NotImplementedError).
  services/workers/_sfincs_build/deck_quadtree.py builds a variable-resolution
  coastal quadtree (coarse offshore -> fine at the shore + drawn refine_regions),
  per-face bed from our topobathy (no cht_bathymetry), auto seaward boundary,
  surge forcing, mesh.geojson preview. Offline canary: production module ->
  SFINCS v2.3.3 solve -> face-indexed output (5610 faces, 3 levels 200/100/50 m,
  peak 8.0 m over 2740 faces); the read-side probe (_is_quadtree_output/
  _read_face_coords) verified REAL (not bit-rotted). The phantom DECK_BUILD_FAILED
  narrative deleted (ledger). SnapWave coupling + quadtree subgrid deferred.
- OCEANMESH WAVE (NATE pull-forward 2026-08-04, runs right after M3,
  before M4): oceanmesh in an isolated worker (GPL-3) backing a
  coastal_tin generator component in the generation stage - inherits
  drawn roles + preview gate; validated by replicating a published
  oceanmesh gallery case (paper-first); spot-check layer to NATE.
  Value = tried-and-true graded sizing functions (closes the census's
  no-graded-sizing gap) instead of hand-built grading. gmsh KEEPS the
  TELEMAC river-ribbon case initially (boundary tagging is load-bearing);
  the channel sizing-function swap is a measured follow-up comparison,
  never a blind replacement.

## Open questions for NATE at sign-off

1. M4: build the quadtree leg or re-scope + delete the stub?
2. Does the mesh preview/approve gate default ON for tin paradigms
   (TELEMAC already gates) or only in USER-GATED mode?
3. DISV/gridgen for MODFLOW: queue into M-waves or leave with the
   MODFLOW coverage track?

## Post-M4 addition (NATE-approved 2026-08-04): the build_mesh primitive

After M3/M4 mature the paradigms, register ONE standalone tool -
build_mesh: AOI/terrain + paradigm + resolution + drawn roles in ->
published mesh preview layer + stats out, NO solver run. Qualifies as an
irreducible input-authoring primitive under the atomic-tool rules (not a
composed analysis). Standard acceptance: corpus queries + model-free
retrieval proof + preview-gate integration. Until then the components
stay internal (workflows insert them; the code playground can compose
them for ad-hoc mesh authoring - the sandbox shares the server env, so
agent/mesh imports there today). Rationale (NATE): separating costs the
workflows nothing and expands functionality without breaking out of the
sandbox.

## Oceanmesh-wave leg 1 (NATE 2026-08-04): explicit TELEMAC bank fallback

The constant-width ribbon is no longer an implicit fallback. bank_source
param (nhd_area default | constant_ribbon explicit); missing NHDArea on
the default path -> typed TELEMAC_BANKS_UNAVAILABLE gate error naming
bank_source="constant_ribbon" + the width assumption as the explicit
retry (the DEM_FALLBACK_GATE pattern); bank provenance rides the tin
preview-gate stats + envelope (feeds structured provenance). The
fallback norm now covers mesh-geometry sources generally: any silent
substitution of banks/terrain/shoreline inputs is outlawed.
