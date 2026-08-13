# 0197 - Coastal/watershed mesh proof-render vertical misalignment fix

Status: accepted
Date: 2026-08-08
Relates: 0192 (oceanmesh standalone), 0193 (pysheds watershed mesh), 0194 (coastal
water edge); docs/research/oceanmesh-front-proposal.md Q1 (degree-frame meshing).

## Context

NATE flagged the coastal mesh proof renders (`oceanmesh_standalone_delaware_bay`,
`oceanmesh_standalone_tampa_bay`, and closeups): the cyan mesh is vertically
misaligned against the ESRI satellite basemap - "the top needs to be shifted down
and the bottom needs to be shifted up." That profile - zero error at mid-image,
growing symmetrically toward the top and bottom edges, no horizontal error - is a
symmetric vertical SCALE error. Two candidate causes:

- **A - render tile/projection math**: the sandbox PNG compositor places imagery
  and mesh in different vertical scales.
- **B - mesh node coordinates**: oceanmesh internally projects (e.g. local
  stereographic) during `generate_mesh`/`clean` and round-trips imperfectly back
  to lon/lat, displacing nodes proportional to distance from the domain center.

## Discriminating evidence

**Tile math, standalone (proves A).** For the Delaware Bay full-domain fetch box
at the render's chosen zoom (z=11), the imagery `extent` handed to
`imshow(extent=[left,right,bottom,top])` was measured against the mosaic's true
outer mercator bounds:

- x-width ratio (code / true) = **1.000000** (no horizontal error),
- y-height ratio (code / true) = **0.7500**,
- extent top error = **-1.000 tile**, extent bottom error = **+1.000 tile**.

The imagery was declared to span one tile too little at BOTH ends, i.e. the whole
mosaic was squished into 75% of its true vertical range, pulling every imagery
feature toward the frame center. The mesh, drawn in true mercator, then appears to
ride high at the top and low at the bottom - exactly NATE's symptom. Analytic
imagery displacement vs latitude (full domain): north edge -16.3 km, center
-1.4 km, south edge +13.4 km; linear coefficient -0.25 m per m of mercator-y
(Tampa: -7.5 km / +7.1 km, coeff -0.20). Linear in latitude, ~zero at center =
the reported signature.

**Mesh vs truth, in lon/lat (rules out B).** Distance from every mesh BOUNDARY
node to the authoritative OSM+NHD water polygon the mesh was built to fill:

| AOI          | boundary nodes | median | p95   | max     | mesh min-edge |
|--------------|----------------|--------|-------|---------|---------------|
| delaware_bay | 1186           | 192 m  | 329 m | 3284 m  | 150 m         |
| tampa_bay    | 2900           | 148 m  | 268 m | 2481 m  | 120 m         |

The mesh boundary sits within ~one element of the true polygon EVERYWHERE, with no
latitude-dependent growth. A stereographic round-trip error (cause B) would grow
with distance from center into the hundreds-to-thousands of metres systematically;
it does not. The water-edge/watershed mesher runs DistMesh directly in EPSG:4326
degrees with a custom lon/lat SDF - there is no intermediate projection to
round-trip. Mesh nodes are geographically faithful.

**Visual (before/after, top-edge closeups).** In both the buggy and fixed panels
the cyan mesh and the magenta water polygon are COINCIDENT (mesh faithful to
polygon); only the imagery placement differs. Buggy: imagery squished into a
letterboxed band, shoreline mismatched. Fixed: imagery fills the frame, mesh sits
pixel-tight on the imagery shoreline. See
`docs/proof/templates/mesh_alignment_{delaware_bay,tampa_bay}_{before,after}.png`.

## Verdict: cause A (render tile math). The meshes are correct and untouched.

## Root cause

`_fetch_basemap` computed the imagery `extent` from the wrong tile edges. With
`tile_merc_bounds(x, y, z)` returning `(west, east, north, south)`:

```
left, _, _, top     = tile_merc_bounds(xa, ya, zoom)   # top  <- 4th = SOUTH edge of north tile
_, right, bottom, _ = tile_merc_bounds(xb, yb, zoom)   # bottom <- 3rd = NORTH edge of south tile
```

`top` and `bottom` selected the INNER edges of the corner tiles instead of the
mosaic's outer edges, shrinking the vertical extent by one tile at each end. (x
was correct because `left`/`right` take the 1st/2nd returns = the true outer W/E
edges.) The correct selection is the north tile's north edge and the south tile's
south edge:

```
left, _, top, _     = tile_merc_bounds(xa, ya, zoom)   # west + NORTH edge
_, right, _, bottom = tile_merc_bounds(xb, yb, zoom)   # east + SOUTH edge
```

This identical bug was copy-pasted into FOUR sandbox renderers:
`build_coastal_water_edge_mesh.py`, `render_mesh.py`, `build_watershed_mesh.py`,
`pysheds_watershed/proof_watershed.py`.

## Decision

Lift the tile + Web-Mercator math into a single shared module
`scripts/sandbox/oceanmesh/merc_render.py` (`ll_to_merc`, `lonlat_to_tile`,
`tile_merc_bounds`, `pick_zoom`, `fetch_basemap`) with the corrected extent, and
have all four renderers import it. The fix lands once; a constraint comment on
`fetch_basemap` records why the outer tile edges are required. The now-superseded
`_rerender.py` (which globbed every run and would clobber the water-edge/watershed
proofs with the coastal caption) is deleted; each driver gained a `--render-only`
flag that re-renders its own proof from the cached mesh.

Because the cause is A, the mesh files (`.2dm/.slf/.gr3/.fort.14`) are NOT
regenerated - they were already geographically correct. MDAL/SERAFIN
verification is unchanged (no mesh bytes changed; mtimes predate this fix).

## Consequence

- One source of truth for basemap math; three duplicate copies removed.
- All eight affected proofs re-rendered in place from cached meshes (no re-mesh,
  no Overpass re-fetch): `oceanmesh_standalone_{delaware_bay,tampa_bay}{,_closeup}`,
  `{duck_nc,puget_sound}`, `coweeta_river`, `pysheds_watershed_coweeta`.
- Before/after top-edge proof pairs added for Delaware and Tampa.
- QGIS ground truth is unchanged and unaffected by this render fix: loading
  `docs/proof/templates/oceanmesh_meshes/<aoi>.2dm` as a QgsMeshLayer in EPSG:4326
  places the mesh on its true lon/lat coordinates directly over any 4326/3857
  basemap - the misalignment was only ever in the sandbox matplotlib compositor,
  never in the emitted mesh.
- Scope was sandbox render code + proof PNGs only; no mesh, workflow, worker,
  template, or engine tree touched.
