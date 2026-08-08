# OceanMesh2D mesh-front - standalone-first proposal (ADR 0192)

Author: mesh-front researcher-builder
Date: 2026-08-08
Status: sandbox landed; **pipeline placement is NATE's decision** (this doc lays
out the options and tradeoffs, it does not pick one).

This proposal accompanies the standalone sandbox under
`scripts/sandbox/oceanmesh/`. That sandbox meshes four US coastal AOIs with the
authentic OceanMesh2D methodology and emits the meshes for NATE to inspect in
QGIS. Nothing here is wired into a workflow, template, or engine.

---

## 1. Dependency choice + rationale

### The candidates (evaluated in the agent venv, `venvs/agent`)

| Candidate | Pip-installable here | Runs end-to-end here | Notes |
|---|---|---|---|
| `oceanmesh` (CHLNDDEV) | **No** - PyPI 404; source build needs CGAL (`libcgal-dev` absent) + pybind11 | **Yes** - via the isolated image `trid3nt-local/mesh:latest` (v1.0.0+0.g66bfdfe) | The authentic OceanMesh2D Python port; all sizing functions native |
| `ocsmesh` (NOAA) | **Yes** - `ocsmesh 2.2.0` installed into the venv | **No** - `jigsaw`/`jigsawpy` backend unavailable on the index | NOAA operational coastal mesher; jigsaw-based |
| `pyposeidon` (GRAML/EMOD) | partial | No | jigsaw or gmsh backend, same binary gap |

### Pick: `oceanmesh` (CHLNDDEV OceanMesh2D port), engine in `mesh:latest`

Reasoning:
- It is the **only candidate that actually produces meshes** in this environment.
- It **is** the OceanMesh2D reference implementation (same authors, same paper:
  Roberts, Pringle, Westerink 2019, GMD 12, 1847-1868), so "OceanMesh2D-class"
  is exact, not approximate. All required sizing functions are first-class
  (`feature_sizing_function`, `wavelength_sizing_function`,
  `bathymetric_gradient_sizing_function`, `enforce_mesh_gradation`).
- It is already built and GPL-isolated in `trid3nt-local/mesh:latest`, keeping
  its GPL surface off the agent venv.

Honest caveat vs the directive's "pip-install into the venv": `oceanmesh` is
**not** pip-installable into the agent venv here (no PyPI wheel, no system CGAL).
`ocsmesh` **is** pip-installable and was installed (`ocsmesh 2.2.0`), but is not
runnable end-to-end without a `jigsaw` binary. So the engine deliberately lives
behind the docker boundary; the sandbox driver runs everything else (DEM fetch,
format writing, QA, verification, rendering) natively in the venv.

---

## 2. Sizing functions implemented + knobs (future user levers)

OceanMesh2D builds a mesh-size function h(x) as the pointwise minimum of several
size functions, then gradation-limits it. This sandbox activates two required
functions and exposes the rest as knobs.

Formulas per Roberts et al. 2019 (GMD):

- **Distance / feature sizing** (ACTIVE) - `feature_sizing_function(shore, sdf, r)`.
  Local feature size from the shoreline medial axis: `h_lfs = 2*(d_MA - d)/alpha_R`,
  `r` (=`alpha_R`) = elements per local feature size (2..6). Resolves channel
  widths and shoreline curvature; equals the minimum h0 at the coast.
  - Knob: `feature_r` (default 3); `min_edge_length_m`, `max_edge_length_m`.
- **Wavelength-to-depth sizing** (ACTIVE) - `wavelength_sizing_function(dem, wl)`.
  `h_wl = T_M2 * sqrt(g*b) / alpha_wl`; `wl` = target elements per M2 tidal
  wavelength. Needs bathymetry (the DEM), so it exercises the topobathy fetch.
  - Knob: `wl` (default 10).
- **Topographic-gradient / slope sizing** (available, off by default) -
  `bathymetric_gradient_sizing_function(dem, slope_parameter)`.
  `h_slp = 2*pi*b / (alpha_slp*|grad b|)`; resolves shelf breaks/steep bathymetry.
  - Knob: `slope: true`, `slope_parameter` (default 20).
- **Gradation limiting** (ACTIVE) - `enforce_mesh_gradation(h, g)`.
  `h(x_j) <= h(x_i) + g*|x_i - x_j|`; g in 0.15..0.35. Prevents skewed
  transitions between fine and coarse zones.
  - Knob: `grade` (default 0.20).
- **Size bounds** (ACTIVE) - `enforce_mesh_size_bounds_elevation` clamps h into
  `[min_edge, max_edge]`.
- **Shoreline cleaning + traversable boundaries + sliver removal** (ACTIVE) -
  `Shoreline(smooth_shoreline=True, minimum_area_mult)`,
  `make_mesh_boundaries_traversable`, `delete_faces_connected_to_one_face`,
  `mesh_clean(min_element_qual)`. Removes islands smaller than (p*h0)^2,
  disconnected fragments, and low-quality slivers.
  - Knob: `min_element_qual` (default 0.1).

These map cleanly onto the **#154 granularity gate**: a future user surface would
expose `min_edge_length_m` / `max_edge_length_m` (resolution), `grade`
(smoothness), `wl` / `slope_parameter` (physics-driven refinement), and the
sizing-function on/off switches as labeled levers, with the autoscaler proposing
defaults and the user overriding.

CRS note: the sandbox meshes in EPSG:4326 (degrees) to stay aligned with the
GSHHG shoreline and the 4326 DEM; target metres are converted to a degree h0 via
local latitude scaling, and all reported edge/quality stats are in true metres.
A production version should mesh in a local UTM/stereographic CRS to remove the
degree anisotropy - an open question below.

---

## 3. TELEMAC + OceanMesh2D pairing (documented practice)

How practitioners hand OceanMesh2D meshes to TELEMAC:
- OceanMesh2D writes generic unstructured formats (`.2dm` SMS, ADCIRC `fort.14`,
  BlueKenue `.t3s` via `write_to_t3s`). TELEMAC geometry is **SELAFIN/SERAFIN**
  (`.slf`).
- The standard bridge is **BlueKenue** (NRC): import the OceanMesh2D nodes/edges
  (`.t3s`/`.2dm`), then "Save As" a SELAFIN `.slf` geometry, with a bathymetry
  node field written as the `BOTTOM` variable. This BlueKenue -> `.slf` step is
  the routinely-documented openTELEMAC path for externally-generated meshes.
- Alternatively openTELEMAC's own `converter.py` / `stbtel` converts supported
  mesh formats to SELAFIN.

This sandbox short-circuits BlueKenue by writing SELAFIN directly
(`selafin_io.write_selafin`, node field `BOTTOM` = elevation) and then **proving
the geometry is TELEMAC-valid** by reading each `.slf` back with the telemac
worker's own `data_manip.extraction.telemac_file.TelemacFile` reader (npoin,
nelem, IKLE connectivity, X/Y, variable list all recovered). That is the same
reader the TELEMAC-2D worker uses, so a produced `.slf` can be pointed at a
steering file's GEOMETRY FILE directly.

Primary source for the methodology: Roberts, K. J., Pringle, W. J., and
Westerink, J. J.: OceanMesh2D 1.0: MATLAB-based software for two-dimensional
unstructured mesh generation in coastal ocean modeling, Geoscientific Model
Development, 12, 1847-1868, https://doi.org/10.5194/gmd-12-1847-2019, 2019.
Software: https://github.com/CHLNDDEV/OceanMesh2D (MATLAB) and
https://github.com/CHLNDDEV/oceanmesh (Python port used here).

---

## 4. Possible integration points (OPTIONS - NATE decides)

The mesh-front produces one artifact (a graded coastal TIN with per-node
bathymetry). Where it plugs in is a separate decision. Options and tradeoffs:

### Option A - TELEMAC geometry supply
Feed `.slf` as the TELEMAC-2D GEOMETRY FILE.
- Pro: directly proven here (serafin read-back); the reason people use
  OceanMesh2D. Highest-value pairing.
- Con: TELEMAC boundary conditions (`.cli`) still need generating from the mesh
  boundary; the current sandbox writes geometry, not the liquid/solid BC file.

### Option B - SCHISM hgrid supply
Feed `<aoi>_hgrid.gr3` to the SCHISM worker.
- Pro: gr3 already written and reuses the in-repo `schism_gr3` bridge; open/land
  boundary segmentation already emitted.
- Con: SCHISM has its own meshing expectations (skew/CFL); would need its V&V.

### Option C - unstructured SWAN
Feed `fort.14` (ADCIRC/unstructured) to unstructured SWAN.
- Pro: wave coverage; fort.14 already written.
- Con: SWAN-unstructured pairing not yet exercised in-repo.

### Option D - standalone registered "build_coastal_mesh" tool
Register the sandbox as an atomic mesh-builder tool that returns a mesh layer +
the four format files, independent of any solver.
- Pro: matches the "mesh is one of the big 3 (data + MESH + engine)" doctrine and
  the mesh-layer-extraction track; usable for QGIS inspection and any downstream
  solver; user-drivable via the granularity gate.
- Con: introduces a docker-backed tool (GPL engine) into the tool surface; needs
  a tool_query_corpus entry + retrieval check before acceptance.

A natural sequence NATE might pick: **D first** (a standalone mesh tool that
emits a QGIS mesh layer + all four formats), then **A** (wire the `.slf` + a
generated `.cli` into TELEMAC-2D as the flagship pairing).

---

## 5. Promotion path: sandbox -> registered standalone tool

1. Keep the engine behind `mesh:latest` (GPL isolation); the tool shells the
   in-container `_mesh_incontainer.py` exactly as the sandbox does.
2. Wrap `build_coastal_mesh.run()` as a tool whose inputs are the granularity-gate
   levers (AOI, min/max resolution, grade, sizing switches) and whose output is a
   mesh layer (2dm/slf) + provenance (shoreline source, DEM provenance, sizing
   settings, QA report).
3. Add `tool_query_corpus.yaml` queries ("mesh this coast", "build a coastal
   grid for TELEMAC", "unstructured triangulation of <bay>") and run the
   model-free `retrieve_visible_tools(prompt, None, 8)` check BEFORE acceptance
   (new-tool retrieval-corpus-first rule).
4. Acceptance: the offline QA gates already implemented (no inverted elements,
   closed boundary, min/median quality, MDAL read-back, serafin read-back) become
   the tool's contract test; a live QGIS visual by NATE closes it.
5. Optional: replace the docker engine with an in-venv `ocsmesh` + jigsaw build to
   drop the container dependency (needs a jigsaw binary in the venv).

---

## 6. Open questions for NATE

1. **CRS**: mesh in EPSG:4326 (current, simple, DEM-aligned) or a local UTM /
   stereographic projection (removes degree anisotropy, standard for OceanMesh2D)?
2. **Shoreline fidelity**: GSHHG intermediate (`GSHHS_i`) is used; it is coarse
   for barrier islands and narrow inlets (some near-shore triangles ride onto
   marsh/land at this resolution). Upgrade to GSHHG full (`GSHHS_f`), NOAA
   CUSP/Continually-Updated Shoreline, or OSM coastline?
3. **Placement**: which of Options A-D (Section 4), and in what order?
4. **TELEMAC BC**: should the tool also emit a `.cli` boundary-conditions file
   (from the open/land boundary segmentation already computed) so the `.slf` is
   run-ready, not just geometry?
5. **In-venv engine**: worth adding a jigsaw binary so `ocsmesh` runs natively in
   the agent venv and the docker engine can be retired?
6. **BOTTOM sign convention**: `.slf` BOTTOM is written as positive-up elevation;
   TELEMAC setups vary (some expect positive depth). Confirm the convention for
   the flagship pairing.

---

## 7. ADR 0193 -- watershed-first domains + real water-edge alignment (NATE-directed)

NATE inspected the v1 coastal meshes and gave two directives that supersede the
Section 6 open questions on domain and shoreline:

1. **The AOI must not truncate the mesh.** A bbox is the wrong domain for a
   riverine/estuarine target. The domain-first method is now: **delineate the
   watershed (or take the water-body geometry) FIRST, then mesh THAT polygon.**
   The AOI box is only a residual render overlay, never a cookie-cutter.
2. **The meshed water edge must match the imagery water edge.** GSHHG
   intermediate (`GSHHS_i`) is too coarse -- "close but not really" aligned to
   the river. Replace it with high-resolution water-edge sources.

### 7.1 Watershed-first domain strategy (IMPLEMENTED, verified)

`scripts/sandbox/oceanmesh/build_watershed_mesh.py` realises the method end to
end for a river watershed:

1. **Delineate** the watershed with the registered pysheds primitive
   `delineate_watershed` (D8 catchment upstream of a snapped pour point) on a
   real **USGS 3DEP** DEM (reprojected EPSG:5070 -> 4326).
2. The **catchment polygon IS the meshing domain** -- passed to OceanMesh2D as
   the domain boundary. The mesh fills the whole catchment; the AOI box is drawn
   only as a dashed residual overlay and demonstrably does not clip the mesh.
3. **Refinement follows the river network:** the element size is
   `clip(min_edge + grade * distance_to_river, min_edge, max_edge)` where the
   river vertices are the NHDPlus HR / OSM flowlines
   (`fetch_river_geometry`) clipped to the catchment -- fine along the valleys,
   coarse on the ridges. This is the literal "mesh the watershed's valley
   network from the NHD geometry" instruction.

**Engine detail that mattered.** OceanMesh2D's coastal `Shoreline` path meshes
the water *outside* land polygons *within a rectangular region*; it cannot mesh
a fully-enclosed inland catchment (a "box with a hole" yields *no zero level
set*). The watershed mesher therefore hands `generate_mesh` a **custom
signed-distance function** (negative inside the catchment polygon) plus a
**custom distance-to-river edge-length function**, bypassing `Shoreline`
entirely. This lives in the mounted (not baked) `_mesh_watershed_incontainer.py`,
so the coastal `_mesh_incontainer.py` path is untouched.

Verified case: **Coweeta Creek watershed** (Nantahala Mtns, NC) -- catchment
30.0 km^2; mesh 4956 nodes / 9727 elements; 31-272 m (median 69 m); min element
quality qE 0.72, median 0.97; 0 inverted; single closed boundary; MDAL- and
SERAFIN-verified; render `docs/proof/templates/oceanmesh_standalone_coweeta_river.png`.

### 7.2 Shoreline-source decision (answers Section 6 Q2)

| Target type | Water-edge source | Status |
|---|---|---|
| River / valley network | **NHDPlus HR flowlines** (`fetch_river_geometry`) drive the mesh refinement; the pysheds catchment is the domain | IMPLEMENTED (Coweeta) |
| Interior lakes / reservoirs / marsh | **NHDPlus HR waterbody polygons** (`fetch_nhd_waterbodies`, ftype LakePond/Reservoir/SwampMarsh) | fetcher verified; wiring pending |
| Open coast / bay / estuary | **NOAA CUSP** (Continually Updated Shoreline Product) is the production-grade coastal source; **OSM `natural=coastline`** is the implementable high-res upgrade in-repo | DECISION recorded; estuary re-mesh is the remaining live step |

Honest finding on estuaries: NHDPlus HR **waterbody** polygons do NOT contain the
open bay (Tampa Bay `fetch_nhd_waterbodies` returns 4246 lakes/ponds/marshes but
not the bay itself -- open estuary water is NHDArea/SeaOcean, not NHDWaterbody).
So the Delaware/Tampa open-water edge needs a coastline source (CUSP or OSM
coastline), not NHD waterbodies. That is why the directive names CUSP for coasts.
The v1 coastal meshes (GSHHG_i) remain as-is pending that CUSP/OSM-coastline
water-edge builder; the watershed-first river case is the verified demonstration
of the new domain method.

### 7.3 Open finding: projected-DEM pour points

`delineate_watershed` snaps a lon/lat pour point through the DEM affine, so it
assumes a **geographic (EPSG:4326)** DEM (its default `fetch_copernicus_dem` is
4326). A raw 3DEP `dem_uri` is EPSG:5070 (metres) and would mis-snap. The
watershed mesher therefore reprojects 3DEP 5070 -> 4326 before delineation.
Making `delineate_watershed` reproject a projected `dem_uri` internally is a
small hardening worth queuing.
