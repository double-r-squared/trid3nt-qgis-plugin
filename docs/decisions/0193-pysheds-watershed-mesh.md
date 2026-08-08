# ADR 0193 - pysheds watershed coverage + watershed-first meshing

Status: Accepted (Part A verified live; Part B watershed-first method landed as a
verified standalone sandbox case; estuary CUSP re-mesh is a documented remaining
live step)
Date: 2026-08-08
Supersedes/extends: ADR 0192 (OceanMesh2D standalone) Section 6 open questions on
domain and shoreline.

## Context

NATE inspected the ADR 0192 coastal meshes and directed two changes: (1) the
standalone meshes "do not align with the river in the AOI ... the GSHHG-
intermediate shoreline is too coarse" -- the meshed water edge must match the
real water edge; (2) "the AOI cuts the mesh off" -- a bbox is the wrong domain;
delineate the watershed FIRST, then mesh it. NATE also asked to revisit pysheds
(a prior NATE-approved integration pick) against the python-gis-book chapter-12
watershed-analysis workflow and "make sure we cover these".

## Part A decision - pysheds coverage: no new registered tool

The chapter-12 pysheds workflow was enumerated and mapped to the registry. The
two irreducible primitives it centres on were ALREADY landed as registered
tools: `delineate_watershed` (D8 catchment polygon) and `extract_stream_network`
(D8 accumulation -> channel LineStrings), both over the shared pysheds chain in
`processing/_hydrology_common.py` (fill_pits -> fill_depressions -> resolve_flats
-> flowdir -> accumulation). pysheds 0.4 is installed in the agent venv.

The only chapter capabilities NOT in the registered surface are the
**flow-accumulation raster** and **distance-to-outlet** (flow-path length).
DECISION: these are NOT promoted to new atomic tools. Per the analysis-is-
playground doctrine (atomic tools = data fetchers + irreducible primitives only;
composed/derived analysis lives in the code_exec playground), and because both
are already computed inside the conditioning chain, they belong in the
playground. Both were demonstrated live in the playground in the Part A proof
(`scripts/sandbox/pysheds_watershed/proof_watershed.py`) on a real 3DEP DEM.

Verified: `server/tests/test_hydrology_primitives.py` 8 passed; live proof at the
**Coweeta Creek watershed** (Nantahala Mtns, NC), 3DEP 10 m -> `delineate_watershed`
30.03 km^2 / 369524 cells, `extract_stream_network` 127 branches / 108.7 km,
flow-accumulation raster (max 537342 cells) + distance-to-outlet (865 cells)
rendered over ESRI imagery: `docs/proof/templates/pysheds_watershed_coweeta.png`.

Open finding: `delineate_watershed` assumes a geographic (4326) DEM for lon/lat
pour-point snapping; a projected 3DEP `dem_uri` (EPSG:5070) would mis-snap.
Queue: make it reproject a projected `dem_uri` internally.

## Part B decision - watershed-first domains + custom-SDF meshing

The mesh DOMAIN is a delineated watershed, not a bbox. `build_watershed_mesh.py`
delineates the catchment (pysheds), takes the NHDPlus HR / OSM flowlines inside
it, and meshes the catchment INTERIOR with OceanMesh2D, refined by distance to
the river network (fine along valleys, coarse on ridges). The AOI box is a
residual render overlay only and demonstrably does not truncate the mesh.

OceanMesh2D's coastal `Shoreline` path meshes water OUTSIDE land polygons within
a rectangular region and cannot mesh a fully-enclosed inland catchment (a
box-with-a-hole yields "no zero level set"). The watershed mesher therefore
hands `generate_mesh` a custom signed-distance function (negative inside the
catchment) + a custom distance-to-river edge-length function, bypassing
`Shoreline`. This is the mounted `_mesh_watershed_incontainer.py`; the coastal
`_mesh_incontainer.py` is untouched (no image rebuild -- the in-container script
is mounted, not baked).

Verified case: Coweeta Creek watershed -- 4956 nodes / 9727 elements, 31-272 m
(median 69 m), min qE 0.72 / median 0.97, 0 inverted, single closed boundary;
MDAL- and SERAFIN-verified; `.2dm` + `.slf` + `hgrid.gr3` + `fort.14` emitted to
`docs/proof/templates/oceanmesh_meshes/coweeta_river.*`; render
`docs/proof/templates/oceanmesh_standalone_coweeta_river.png`.

Shoreline-source decision (supersedes ADR 0192 Q2): river/valley = NHDPlus HR
flowlines drive refinement + pysheds catchment is the domain (IMPLEMENTED);
interior lakes/marsh = NHDPlus HR waterbody polygons (`fetch_nhd_waterbodies`,
verified); open coast/bay/estuary = NOAA CUSP (production) or OSM
`natural=coastline` (implementable high-res). Honest finding: NHDPlus HR
waterbodies do NOT include the open bay (Tampa Bay returns lakes/ponds/marshes,
not the estuary), so the Delaware/Tampa open-water edge needs a CUSP / OSM-
coastline water-edge builder -- that estuary re-mesh is the remaining live step;
the v1 coastal meshes stay as-is until it lands.

## Consequence

- New files only: `scripts/sandbox/pysheds_watershed/proof_watershed.py`,
  `scripts/sandbox/oceanmesh/{water_edge.py,build_watershed_mesh.py,
  _mesh_watershed_incontainer.py}`, two proof renders, four watershed mesh
  files, README + this ADR + proposal Section 7. No product code, workflow,
  template, or engine tree changed; no registered tool added.
- The mesh engine stays behind the GPL-isolated `mesh:latest` boundary; the
  in-container watershed script is mounted, not baked.
- Remaining: a CUSP / OSM-coastline water-edge builder to re-mesh Delaware Bay
  and Tampa Bay full-domain with the real coastal water edge.
