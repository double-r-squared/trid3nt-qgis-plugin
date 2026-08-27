# OceanMesh2D + telapy mesh + gr3 recon (mesh wave prep)

Read-only recon. Scope: package/install facts, API surface (real signatures),
output formats, determinism, gr3 structure, and a wrapping plan. Nothing here
is wired into a workflow or tool; this only records what the two libraries
actually expose so the mesh wave wraps rather than reimplements.

---

## 1. oceanmesh (CHLNDDEV Python port of OceanMesh2D)

Repo: https://github.com/CHLNDDEV/oceanmesh
Primary paper: Roberts, K. J., Pringle, W. J., Westerink, J. J.: "OceanMesh2D
1.0: MATLAB-based software for two-dimensional unstructured mesh generation
in coastal ocean modeling," Geoscientific Model Development, 12, 1847-1868,
https://doi.org/10.5194/gmd-12-1847-2019, 2019.

### Install path -- IMPORTANT DISCREPANCY

The README (https://github.com/CHLNDDEV/oceanmesh/blob/master/README.md)
advertises `pip install -U oceanmesh` plus `conda install -c conda-forge cgal`
for the CGAL dependency. **This repo already tried that and it does not
hold**: per the prior recon in
`docs/research/oceanmesh-front-proposal.md` (ADR 0192, 2026-08-08), `pip
install oceanmesh` 404s on PyPI in this environment, and a source build needs
CGAL headers (`libcgal-dev`) plus pybind11 that are not present in the agent
venv. The working path here is a **dedicated docker image**,
`trid3nt-local/mesh:latest` (oceanmesh v1.0.0+0.g66bfdfe), built once and run
as an isolated engine boundary (keeps the GPLv3 surface off the agent venv,
which is the intended isolation anyway per the GPL-isolation norm). Treat
"pip install oceanmesh" as aspirational upstream doc text, not a fact
confirmed to hold here -- the docker path is the confirmed, exercised one.
Do not re-attempt the pip path without a reason; if CGAL/pybind11 land in the
base image later this can be revisited.

Evaluated alternatives (from the same prior recon, still true):
- `ocsmesh` (NOAA): pip-installs cleanly (`ocsmesh 2.2.0`) but needs a
  `jigsaw`/`jigsawpy` binary backend not available on the index here -- does
  not run end-to-end in this environment.
- `pyposeidon`: same jigsaw/gmsh binary gap.

### Core API (confirmed against both the CHLNDDEV README and actual working
calls in `trid3nt-local/scripts/sandbox/oceanmesh/_mesh_incontainer.py`,
which runs inside `mesh:latest`)

Domain / shoreline:
```python
om.Region(extent=(xmin, xmax, ymin, ymax), crs=4326)   # or crs="EPSG:4326"
om.Shoreline(shapefile_path, region_or_bbox, min_edge_length, crs=None, stereo=False)
om.signed_distance_function(shoreline, invert=False)
```
Working call shape actually exercised in the sandbox:
```python
region = om.Region(extent=om_bbox, crs="EPSG:4326")
shore  = om.Shoreline(cfg["shoreline_shp"], region.bbox, min_edge_deg)
sdf    = om.signed_distance_function(shore)
```

Sizing functions (pointwise-minimum composition, per Roberts et al. 2019):
```python
om.distance_sizing_function(shoreline, rate=0.15, max_edge_length=None)
om.feature_sizing_function(shoreline, sdf, max_edge_length=0.05)        # ACTIVE in sandbox
om.wavelength_sizing_function(dem, wl=100, period=12.42*3600)           # ACTIVE in sandbox (M2 tidal)
om.bathymetric_gradient_sizing_function(dem, slope_parameter=5.0,
    filter_quotient=50, min_edge_length=0.0025, max_edge_length=0.10, crs=4326)  # off by default
om.enforce_mesh_gradation(edge_length, gradation=0.15, stereo=False)    # ACTIVE (grade limiter)
om.enforce_mesh_size_bounds_elevation(edge_length, ...)                 # ACTIVE (min/max clamp)
om.compute_minimum([edge1, edge2, ...])                                 # composes multiple sizing functions
```
Formulas (from the sandbox's own derivation notes, matching the paper):
- feature/distance: `h_lfs = 2*(d_MA - d)/alpha_R`, alpha_R (elements per
  local feature size) 2..6.
- wavelength: `h_wl = T_M2 * sqrt(g*b) / alpha_wl`.
- slope: `h_slp = 2*pi*b / (alpha_slp*|grad b|)`.
- gradation: `h(x_j) <= h(x_i) + g*|x_i - x_j|`, g in 0.15..0.35.

Mesh generation:
```python
points, cells = om.generate_mesh(signed_distance_function, edge_length_function,
    stereo=False, max_iter=100, seed=0, pfix=None)   # returns numpy arrays
                                   # seed EXISTS and seeds the initial cloud
om.generate_multiscale_mesh([sdf1, sdf2], [edge1, edge2], blend_width=None,
    blend_max_iter=50, max_iter=75)
```

Cleanup / improvement (all confirmed used in the sandbox):
```python
om.make_mesh_boundaries_traversable(points, cells, min_disconnected_area=0.05)
om.delete_faces_connected_to_one_face(points, cells)
om.delete_boundary_faces(points, cells, min_qual=0.15)
om.laplacian2(points, cells, max_iter=100)
om.fix_mesh(points, cells)
om.mesh_clean(points, cells, min_element_qual=...)   # sliver removal, min_element_qual knob
```

DEM input:
```python
om.DEM(filepath, bbox=None, crs=4326)
```

### Writers -- native output formats

oceanmesh itself has **no native TELEMAC/SCHISM/ADCIRC writer**. Its native
output is a `(points, cells)` numpy array pair, documented as `meshio`-
compatible:
```python
meshio.write_points_cells("output.vtk", points, [("triangle", cells)], file_format="vtk")
```
Everything else (`.2dm`, `fort.14` ADCIRC, SELAFIN `.slf`, SCHISM `hgrid.gr3`)
is written by **this repo's own code**, not by oceanmesh:
- `scripts/sandbox/oceanmesh/mesh_formats.py`: `write_fort14()`, `write_2dm()`,
  `mesh_quality_report()`, plus `_clean_and_orient()` for the shared topology
  pass. Open-boundary SEGMENTATION is oceanmesh's own
  `identify_ocean_boundary_sections(points, cells, topobathymetry,
  depth_threshold, min_nodes_threshold)`, which returns the first and last node
  of each CONTIGUOUS ocean stretch along `edges.get_winded_boundary_edges`'s
  walk; `_open_nodes_on_side()` (a coordinate percentile) is the pre-mesh-wave
  sandbox helper and produces non-contiguous stretches.
- `scripts/sandbox/oceanmesh/selafin_io.py`: `write_selafin()` (writes SELAFIN
  geometry directly from `points, cells`, node field `BOTTOM` = elevation,
  positive-up convention -- flagged as an open question against TELEMAC setups
  that expect positive-depth).
- SCHISM `hgrid.gr3` is **not written locally** -- it is delegated to the
  already-proven in-repo bridge `workers/schism/schism_gr3.tin_to_hgrid()`
  (pure numpy), reused so gr3 and fort.14 come from one topology pass. This is
  the wrapping pattern to keep: one clean/orient/boundary-segment pass feeds
  every writer.
- Documented alternative (not used here): BlueKenue (NRC) import of
  OceanMesh2D `.t3s`/`.2dm`, "Save As" a SELAFIN `.slf` -- this is upstream
  practitioners' standard bridge but this repo short-circuits it with its own
  SELAFIN writer, proven by reading the `.slf` back with the TELEMAC worker's
  own `TelemacFile` reader (npoin, nelem, IKLE, X/Y, var list all recovered).

### Determinism (recipe-replay / sha256-identical rebuilds)

CORRECTION (2026-08-27, measured in the pinned image): **`generate_mesh` DOES
take a `seed` parameter** and seeds numpy's global generator with it before
DistMesh lays down its initial point cloud. The `om2d` mesher passes
`seed=0` on every build, so the initial cloud is not the source of drift.

Determinism nonetheless remains **measured False**: three rebuilds from one
identical config (same AOI, same staged bed, same shoreline, same seed) inside
`mesh:latest` returned two distinct meshes, so the nondeterminism is elsewhere
in the convergence path, not in the seed. `om2d` therefore registers
`deterministic=False` and the recipe records it, so a replay is read as an
equivalent rebuild rather than a sha256-identical one. Do not re-derive a
"no seed exists" conclusion from this file's earlier text.

---

## 2. telapy mesh handling (TELEMAC worker image)

Image: `trid3nt-local/telemac:latest`. `HOMETEL=/opt/conda/opentelemac`.
`telapy.__file__` -> `/opt/conda/opentelemac/scripts/python3/telapy/__init__.py`.
`telapy/api/` contains `hermes.py, art.py, generate_study.py, masc.py, t2d.py,
t3d.py, wac.py, api_module.py`. There is no single `mesh.py` -- geometry I/O
is split across three real modules, enumerated below with actual signatures
pulled from the container (not paraphrased).

### 2a. `telapy.api.hermes.HermesFile` -- low-level SELAFIN/geometry read+write
(`/opt/conda/opentelemac/scripts/python3/telapy/api/hermes.py`)

Mesh read accessors:
```python
class HermesFile():
    def __init__(self, file_name, fformat, access='r', boundary_file=None, ...)
    def get_mesh_title(self)
    def get_mesh_date(self)
    def get_mesh_nelem(self)
    def get_mesh_npoin_per_element(self)
    def get_mesh_connectivity(self)
    def get_mesh_npoin(self)
    def get_mesh_nplan(self)
    def get_mesh_dimension(self)
    def get_mesh_orig(self)
    def get_mesh_coord(self, jdim)
    def get_mesh_l2g_numbering(self)
    def get_mesh_nptir(self)
    def get_bnd_ipobo(self)
    def get_bnd_numbering(self)
    def get_bnd_connectivity(self)
    def get_bnd_npoin(self)
    def get_bnd_nelem(self)
    def get_bnd_value(self)
    def get_data_nvar(self) / get_data_var_list(self) / get_data_ntimestep(self)
    def get_data_time(self, record) / get_data_value(self, var_name, record)
```

Mesh write (full signatures, verbatim from the source):
```python
def set_header(self, title, nvar, var_name, var_unit)

def set_mesh(self, mesh_dim, typ_elem, ndp, nptfr, nptir, nelem, npoin,
             ikles, ipobo, knolg, coordx, coordy, nplan, date,
             time, x_orig, y_orig, coordz=None):
    """
    mesh_dim   Dimension of the mesh
    typ_elem   TYPE OF THE MESH ELEMENTS
    ndp        Number of points per element
    nptfr      Number of boundary points
    nptir      Number of interface points
    nelem      Number of elements
    npoin      Number of points
    ikles      Connectivity array for the main element
    ipobo      Is-a-boundary-point array
    knolg      Local-to-global numbering array
    coordx/y   Mesh point coordinates
    nplan      Number of planes
    date/time  Creation date/time
    x_orig/y_orig  Origin of coordinates
    coordz     Z coordinates (optional; zero-filled if None)
    """

def add_data(self, var_name, var_unit, time, record, first_var, values)

def set_bnd(self, typ_bnd_elem, nelebd, ikle, lihbor, liubor,
            livbor, hbor, ubor, vbor, chbord, litbor, tbor, atbor,
            btbor, color):
    """
    typ_bnd_elem  Type of boundary element
    nelebd        Number of boundary elements
    ikle          Connectivity array for boundary elements
    lihbor        Boundary-condition type on depth
    liubor/livbor Boundary-condition type on u/v
    hbor/ubor/vbor  Prescribed BC values on depth/u/v
    chbord        Friction coefficient at boundary
    litbor/tbor   Physical BC type / prescribed value for tracers
    atbor/btbor   Thermal exchange coefficients
    color         Boundary color of the element
    """
```
This is the actual LIHBOR/LIUBOR/LIVBOR write path -- `set_bnd` is the
function that assigns per-node boundary-condition *type codes* (LIHBOR etc.
are TELEMAC's standard 2/4/5 open/closed/prescribed codes), separate from
`set_mesh` which only carries geometry/connectivity.

### 2b. `data_manip.formats.selafin.Selafin` -- higher-level SELAFIN class
(`/opt/conda/opentelemac/scripts/python3/data_manip/formats/selafin.py`)
```python
class Selafin(object):
    def __init__(self, file_name)          # '' -> blank in-memory mesh; else parses an existing .slf
    def get_header_metadata_slf(self)
    def get_header_integers_slf(self)       # sizes/connectivity (nelem2/3, npoin2/3, ndp2/3, ikle2/3, ipob2/3)
    def get_header_floats_slf(self)         # meshx, meshy
    def get_time_history_slf(self)
    def get_variables_at(self, frame, vars_indexes)
    def append_header_slf(self)             # write a new header (title, nvar, varnames/units, ikle, ipobo, x, y)
    def append_core_time_slf(self, time)
    def append_core_vars_slf(self, varsor)
    def put_content(self, file_name, showbar=True)   # writes the file to disk
    def set_kd_tree(self, reset=False) / set_mpl_tri(self, reset=False)
```
Key attributes populated on a blank init: `title, nbv1, nbv2, nvar, iparam,
nelem3, npoin3, ndp3, nplan, nelem2, npoin2, ndp2, varnames, varunits, ikle2/3,
ipob2/3, meshx, meshy`. This is the class this repo's own `selafin_io.py`
mirrors when writing SELAFIN directly (bypassing BlueKenue).

`GEO`/`MSH` classes in
`data_manip/extraction/parser_gmsh.py` (`class GEO(InS)`, `class MSH(Selafin)`)
show the intended external-mesh path: `MSH` **subclasses `Selafin`** and
reads a Gmsh `.msh` file straight into Selafin's own internal arrays, i.e.
Gmsh `.msh` is a first-class import format for TELEMAC geometry via this
parser. `GEO.write_polygon(poly)` / `.put_content()` write BlueKenue `.i2s`
polygon format (boundary polylines), used upstream of Conlim generation.

### 2c. `data_manip.formats.conlim.Conlim` -- boundary conditions file
(`/opt/conda/opentelemac/scripts/python3/data_manip/formats/conlim.py`)
```python
class Conlim(object):
    def __init__(self, file_name)
        # parses '.cli' into self.bor: structured numpy array with fields
        # lih, liu, liv, h, u, v, au, lit, t, at, bt, n, line
        # (LIHBOR/LIUBOR/LIVBOR/H/U/V/... exactly the .cli column order)
        # self.kfrgl: dict mapping boundary-node-number-1 -> row index

    def set_numliq(self, closed_contours)
        # walks each closed contour (domain boundary + each island), finds
        # solid (LIH==2) vs liquid stretches, and assigns sequential NUMLIQ
        # ids (self.por['lq']) to each liquid boundary segment -- this is the
        # function that turns a raw boundary-node loop into TELEMAC's
        # per-liquid-boundary numbering

    def put_content(self, file_name)
        # writes the .cli file back to disk
```
This confirms: `.cli` boundary files are plain fixed-column-order text
(`LIHBOR LIUBOR LIVBOR H U V AU LITBOR T AT BT N ...`), and the numbering of
distinct open-boundary segments (what a mesher needs to hand off) is exactly
`set_numliq`'s job given a list of closed boundary-node contours.

### 2d. `pretel/meshes.py` -- pure-numpy mesh topology/editing utilities
(`/opt/conda/opentelemac/scripts/python3/pretel/meshes.py`)
Large function set (54 public functions) for mesh interpolation, boundary
extraction, resolution filtering, node merging/cleaving, duplicate-node
removal, thin-plate-spline field mapping. Relevant ones for edit-actions:
```python
get_ipobo(meshx, meshy, ikle2, debug=True)               # derive boundary-point flags from connectivity
cross_check_boundaries(meshx, meshy, ikle2, ipob0, debug=True)
show_boundary_nodes / show_node_connections(meshx, meshy, ikle2, debug=True)
merge_min_4_nodes / cleave_max_7_nodes(meshx, meshy, ikle2, where, debug=True)
remove_duplicate_nodes(meshx, meshy, ikle2, alpha, debug=True)
remove_extra_nodes(meshx, meshy, ikle2, debug=True)
map_thin_plate_spline(meshx, meshy, bathx, bathy, bathz, npoin, ...)
subdivide_mesh3 / subdivide_mesh4(ikle, meshx, meshy)      # refine a mesh
filter_mesh_resolution(meshx, meshy, ikle2, resolut, factor, debug=True)
```
This module is where "thin hook" edit actions (local refine, node merge,
resolution filter) would attach if the mesh tool exposes post-generation
edits on a TELEMAC mesh already in memory.

### 2e. CLI-level converters (found alongside, real and working)
`scripts/python3/converter.py` -- subcommands include `srf2med, srf2vtk,
med2srf, med2srfd, shp2i2s, shp2txt, txt2shp` (SELAFIN <-> MED/VTK, shapefile
<-> BlueKenue i2s/txt). `scripts/python3/stbtel.py` -- runs the STBTEL Fortran
module (mesh refine/convert via a `.cas` steering file); its converter
sub-layer (`data_manip/conversion/stbtel_converter.py`) infers input/output
format from file extension, so a Gmsh `.msh` -> SELAFIN conversion (or
refine) can be driven through `stbtel.py` + a `.cas` file, not just through
`MSH(Selafin)` in Python.

---

## 3. SCHISM `hgrid.gr3` ASCII structure

Primary source: https://schism-dev.github.io/schism/master/input-output/hgrid.html

```
<alphanumeric description line>            ! ignored by the code
<ne> <np>                                  ! number of elements, number of nodes
<node#> <x> <y> <depth>                    ! repeated np times
<elem#> <nvertices> <n1> <n2> [<n3> <n4>]  ! repeated ne times (3=triangle, 4=quad)
<nope>                                     ! number of open boundary segments
<neta>                                     ! total number of open boundary nodes
<nnode_1>                                  ! number of nodes on open boundary 1
<node list, one per line>
  ... repeated per open boundary ...
<nland>                                    ! number of land boundaries (incl. islands)
<nvel>                                     ! total number of land boundary nodes
<nnode_1> <ibtype>                         ! ibtype: 0=exterior land, 1=island
<node list, one per line>
  ... repeated per land boundary ...
```
Notes:
- The exterior boundary (open + land combined) must be traced
  counter-clockwise.
- The island flag (`ibtype=1`) matters specifically when WWM is coupled: no
  open boundary is permitted on an island contour.
- Depth sign convention in SCHISM is negative-down by default in most
  published examples (checked against the general SCHISM convention, not
  independently re-verified against a bathtub example here) -- confirm sign
  against whatever `schism_gr3.tin_to_hgrid` already emits, since that
  function is the one this repo actually calls.
- This repo already has a **proven writer**: `workers/schism/schism_gr3.py`
  -> `tin_to_hgrid(points, cells, depth=..., grid_name=..., ...)`, reused
  (not reimplemented) by the oceanmesh sandbox's `build_coastal_mesh.py`,
  `build_coastal_water_edge_mesh.py`, and `build_watershed_mesh.py`. Any
  mesh-wave tool should call this same function rather than writing gr3 from
  scratch a second time.

---

## 4. Wrapping plan -- mapping onto a `build(spec) -> mesh` mesher interface

Neither library gets reimplemented; the interface is a thin orchestration
layer over three already-separate concerns: **generate** (oceanmesh, in
`mesh:latest`), **write** (this repo's format writers, reusing
`schism_gr3.tin_to_hgrid` for gr3), **consume** (telapy/Selafin/Conlim inside
`telemac:latest`, or SCHISM's own gr3 reader).

```
build(spec) -> mesh
  spec: { aoi_or_watershed_polygon, min_edge, max_edge, grade,
          sizing_switches (feature/wavelength/slope), shoreline_source,
          dem_source, target_writers: [slf, gr3, fort14, 2dm] }

  1. RESOLVE DOMAIN (this repo, not oceanmesh)
     watershed/water-body polygon first (never a bbox) -> om.Region

  2. GENERATE (oceanmesh, inside mesh:latest -- unchanged upstream calls)
     om.Shoreline -> om.signed_distance_function
     sizing = [om.feature_sizing_function, om.wavelength_sizing_function(dem), ...]
     edge_length = om.enforce_mesh_gradation(om.compute_minimum(sizing), grade)
     edge_length = om.enforce_mesh_size_bounds_elevation(edge_length, min, max)
     points, cells = om.generate_mesh(sdf, edge_length, max_iter=...)
     points, cells = om.make_mesh_boundaries_traversable(...) -> delete_faces_connected_to_one_face
                      -> om.mesh_clean(min_element_qual=...)
     RETURNS: (points, cells) numpy arrays -- the one shared intermediate.

  3. ONE topology/boundary pass (this repo's mesh_formats.py pattern) --
     clean/orient + open-boundary-node segmentation computed ONCE, then fed
     to every writer so boundary numbering is consistent across formats:
       - _clean_and_orient(points, cells)
       - om.identify_ocean_boundary_sections(...) -> contiguous ocean sections

  4. WRITE (fan-out from the one topology pass; never re-detect boundaries per writer)
       - SELAFIN:  selafin_io.write_selafin() [[or Selafin().append_header_slf()
                    + put_content() for closer telapy parity]]
       - gr3:      workers/schism/schism_gr3.tin_to_hgrid(points, cells, depth, ...)
                    (REUSE, do not reimplement -- already proven)
       - fort.14:  mesh_formats.write_fort14()
       - .2dm:     mesh_formats.write_2dm()

  5. EDIT hooks (thin, optional, post-generation) -- attach directly to
     pretel/meshes.py functions run inside telemac:latest against an already
     -written SELAFIN, or re-run oceanmesh's own cleanup functions on the
     numpy arrays before writing:
       - local refine:        pretel.meshes.subdivide_mesh3/4
       - node merge/cleave:   pretel.meshes.merge_min_4_nodes / cleave_max_7_nodes
       - resolution filter:   pretel.meshes.filter_mesh_resolution
       - boundary type edit:  Conlim.set_numliq(closed_contours) + direct
                                edits to Conlim.bor['lih'/'liu'/'liv'] before
                                Conlim.put_content()

  6. VERIFY (already the pattern in this repo, keep it) -- read the .slf
     back with TelemacFile / Selafin() to confirm npoin/nelem/IKLE/X/Y and
     var list round-trip before calling a mesh "done."

  7. DETERMINISM -- MEASURED, not assumed: generate_mesh takes seed= and the
     om2d mesher passes it, yet three rebuilds from one identical config
     returned two distinct meshes, so a replay rebuilds an EQUIVALENT mesh and
     the recipe says so.
```

Boundary-condition generation (`.cli`) is the one piece not yet built
anywhere in this repo (flagged as an open question in ADR 0192 section 6):
`Conlim.set_numliq()` needs a list of closed boundary-node contours, which
the mesher's own boundary-segmentation pass (step 3 above) already computes
for the gr3/fort14 writers -- so wiring `.cli` generation is mostly reusing
that same contour list against `Conlim`, not new topology work.

---

## Sources

- https://github.com/CHLNDDEV/oceanmesh (repo)
- https://github.com/CHLNDDEV/oceanmesh/blob/master/README.md (install/API claims)
- https://doi.org/10.5194/gmd-12-1847-2019 (Roberts, Pringle, Westerink 2019, GMD 12, 1847-1868)
- https://schism-dev.github.io/schism/master/input-output/hgrid.html (hgrid.gr3 structure)
- In-image inspection: `docker run --rm --network none trid3nt-local/telemac:latest ...`
  against `/opt/conda/opentelemac/scripts/python3/{telapy,data_manip,pretel}`
- In-repo prior recon: `docs/research/oceanmesh-front-proposal.md` (ADR 0192/0193,
  2026-08-08) and `scripts/sandbox/oceanmesh/*.py` (working sandbox code, the
  source of the confirmed-vs-aspirational install-path finding and the actual
  API calls exercised against `mesh:latest`)
