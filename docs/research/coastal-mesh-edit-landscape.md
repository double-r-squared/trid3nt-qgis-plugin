# Coastal mesh-edit landscape: trodden paths vs a gmsh route

Read-only web + in-repo research. Question: for programmatic editing/refinement
of coastal unstructured meshes (OceanMesh2D-generated and similar) in the
ADCIRC/SCHISM/TELEMAC/SWAN community, what is the trodden path, and is a
gmsh-based route mainstream or an outlier needing shims. All claims cited;
in-repo measurements (this repo's own docker-image inspection, not web
sources) are marked INTERNAL.

---

## 1. OceanMesh2D practice: edit-in-place vs regenerate

The MATLAB `msh` class (part of OceanMesh2D's four classes: `geodata`,
`edgefx`, `meshgen`, `msh`) ships real post-generation edit/clean methods, not
just I/O:

- `clean()` -- moderate/aggressive cleaning modes, transfers nodal attributes
- `bound_courant_number()` -- enforce a Courant-number floor on element size
  at the boundary
- `interp()` -- interpolate bathy/topo onto vertices (multiple methods)
- `remesh_patch()` -- **local remesh within a polygon, reinserted into the
  parent mesh** (added later in the project's history; this is the direct
  "incremental edit" primitive)
- `renum()`, `extract_subdomain()`, `make_bc()`, `get_boundary_of_mesh()`
- `remove_attribute()` for f13 nodal-attribute files

Repo: https://github.com/CHLNDDEV/OceanMesh2D (releases:
https://github.com/CHLNDDEV/OceanMesh2D/releases, v6.0.0 2024-02-28, active
`Projection` branch). User guide:
https://coast.nd.edu/reports_papers/2018-oceanmesh2d-user-guide.pdf.
Paper: Roberts, Pringle, Westerink, "OceanMesh2D 1.0," GMD 12, 1847-1868,
2019, https://doi.org/10.5194/gmd-12-1847-2019.

**Community norm is a mix, decided case by case, not a single doctrine.** The
ADCIRC wiki's own guidance is explicit that this is a judgment call, not a
prescribed workflow: "users should carefully weigh the merits of using an
existing mesh, revising an existing mesh, or building a new one" --
https://wiki.adcirc.org/Grid_Development_and_Editing. That page names SMS
(commercial, GUI create/edit/view), OceanMesh2D ("an end-to-end pre-processor
for ADCIRC"), and Blue Kenue as the three tools in play. The `remesh_patch()`
addition to `msh` is direct evidence that regenerate-the-whole-mesh was
recognized as wasteful for localized changes and the project added a
patch-local incremental-edit primitive rather than pushing users back to full
regeneration every time.

The OCSMesh maintainers' own framing (NOAA discussion, see section 4) sharpens
this: the data-driven, scriptable approach (OceanMesh2D/OCSMesh) exists
specifically because manual GUI editing (SMS) "require[s] extensive tweaking
and lack[s] reproducibility across regions" --
https://github.com/noaa-ocs-modeling/OCSMesh/discussions/36. So the trodden
path within this family is: **encode changes as sizing-function/constraint
changes and regenerate**, with `remesh_patch`-style local edits as the escape
hatch for changes that don't cleanly reduce to a sizing-function change (e.g.
snapping specific nodes to a feature).

### MATLAB msh methods vs the Python port (CHLNDDEV/oceanmesh)

The Python port (https://github.com/CHLNDDEV/oceanmesh, ~74 stars/23 forks,
"stable public API but still under active development", maintained by Keith
Roberts) is **generation-focused, not edit-focused**: it implements DistMesh-
style generation from signed-distance + sizing functions, plus mesh-quality
cleanup:

- `make_mesh_boundaries_traversable()` -- removes degenerate boundary faces
- `delete_faces_connected_to_one_face()` -- strips singly-connected elements
- `delete_boundary_faces()` -- removes low-quality boundary elements
- `laplacian2()` -- Laplacian smoothing that preserves the size distribution

It does **not** carry over `remesh_patch()`, `extract_subdomain()`, or the
other `msh`-class incremental-edit surface -- the Python port's edit
vocabulary is entirely "clean up what generation just produced," not "load an
existing mesh and locally modify it." This repo's own hands-on recon
(INTERNAL: `docs/research/om2d-telapy-mesh-recon.md`) confirms the working
call shape is `Region -> Shoreline -> signed_distance_function -> sizing ->
generate_mesh(..., seed=...)` -- generation, start to finish, with no
load-existing-mesh entry point exercised or documented.

INTERNAL determinism finding (not on any of the upstream pages above): this
repo measured `oceanmesh.generate_mesh` end to end inside its own pinned
`mesh:latest` image. `generate_mesh` does take a `seed=` argument and seeds
NumPy's global generator before DistMesh's initial point cloud -- but **three
rebuilds from one identical config (same AOI, same staged bed, same
shoreline, same seed) produced two distinct meshes**. The recipe/replay layer
in this repo therefore records `deterministic=False` for `om2d` and treats a
replay as "equivalent," not "sha256-identical." Source:
`docs/research/om2d-telapy-mesh-recon.md` lines ~134-147 (measured
2026-08-27). This bears directly on the VERDICT's determinism criterion.

---

## 2. seamsh (gmsh-based coastal mesher, SLIM/UCLouvain)

Repo/docs: PyPI https://pypi.org/project/seamsh/, docs at
https://jlambrechts.git-page.immc.ucl.ac.be/seamsh/ (GitLab-Pages-hosted,
group `gitlab.uliege.be`, direct gitlab.uliege.be fetch was unreachable from
this environment -- TLS handshake failure -- so docs were read via the
mirrored git-page and PyPI/Snyk listings instead).

- **Maintenance**: latest release is version **0.5, dated 2025-01-17** (Snyk
  package database, https://security.snyk.io/package/pip/seamsh) -- roughly
  19 months stale as of 2026-08. No evidence of a 2026 release found.
- **External sizing input**: yes, beyond seamsh's own distance-based sizing.
  `seamsh.field.Raster` "load[s] geotiff files or any gdal raster file," used
  directly as a mesh-size field -- i.e. an externally-produced raster (from
  any pipeline) can drive element size, not only seamsh's built-in
  distance-from-curve fields. Source: seamsh GIS-data doc page
  (https://jlambrechts.git-page.immc.ucl.ac.be/seamsh/examples/2-gis.html)
  and `seamsh.gmsh` module docs
  (https://jlambrechts.git-page.immc.ucl.ac.be/seamsh/seamsh.gmsh.html).
- **Geometry input**: ESRI shapefiles for boundary curves, with a shapefile
  field driving the physical tag per curve; automatic reprojection if the
  shapefile CRS differs from the working CRS.
- **Output**: triangular gmsh meshes, convertible to shapefile or GeoPackage.
- **Usage outside SLIM/UCLouvain**: thin but real. A 2025 storm-surge paper
  ("Assessing the sensitivity of storm surge simulation to the atmospheric
  forcing resolutions across the estuary-sea continuum,"
  https://www.researchgate.net/publication/389220773) uses seamsh, with
  co-authors from UCLouvain IMMC *and* ULiege Geography -- adjacent but not
  fully external. No independent (non-Belgian, non-SLIM-adjacent) citation
  or GitHub-issue-from-an-outside-user was found in this search pass. seamsh
  is best read as a **capable, still-current tool with a narrow adoption
  radius centered on its home group**, not a broadly cross-community default.

---

## 3. qmesh (gmsh-based, Thetis/Fluidity lineage)

Main repo: https://github.com/qmesh3/qmesh3 ("qmesh3" -- a Python-3/QGIS-3
port). Its own README states its purpose bluntly: qmesh3 "has been created as
a temporary solution to allow qmesh to function with python3 and QGIS3 within
a functional pip install to facilitate users access **until the original
qmesh updates are released**." That is the maintainers' own words describing
it as a stopgap, not a finished product. 64 total commits, 10 open issues, no
visible 2025/2026 activity signal in the repo header at fetch time. Original
authors Alexandros Avdis and Jon Hill; used by Fluidity
(https://github.com/FluidityProject/fluidity/wiki/Gmsh-tutorial) and Thetis
(https://github.com/thetisproject/thetis). **Verdict: alive only in the sense
that a stopgap fork exists to keep old workflows limping on Python 3; no
sign of active new development.** This is the weakest-maintained tool in the
survey.

---

## 4. OCSMesh (NOAA, jigsawpy-based, operational for SCHISM)

Repo: https://github.com/noaa-ocs-modeling/OCSMesh. Docs:
https://noaa-ocs-modeling.github.io/OCSMesh/. NOAA Technical Memorandum NOS
CS 47, https://repository.library.noaa.gov/view/noaa/33879.

- **Maintenance**: actively developed. Latest release **v1.7.0 on
  2026-07-07** per PyPI (https://pypi.org/project/ocsmesh/); a v2.0.0 refactor
  is in flight that makes the Jigsaw/Triangle mesh engines optional,
  post-dependency installs
  (https://github.com/noaa-ocs-modeling/OCSMesh/releases). Repo shows
  1,042+ commits, 30 stars, 13 forks, 43 issues -- modest star count but real
  commit velocity and an operational NOAA user (this is infrastructure code,
  not a popularity-contest package). INTERNAL: this repo's own recon notes
  `pip install ocsmesh` resolves cleanly to 2.2.0 in a fresh environment, but
  the JIGSAW/jigsawpy binary backend is not resolvable from the index
  available to this repo's sandboxed venv, so it does not run end-to-end
  there (`docs/research/om2d-telapy-mesh-recon.md`) -- a packaging/backend
  gap, not evidence against upstream maintenance.
- **External sizing input from raster**: yes. `ocsmesh.hfun.HfunRaster` is a
  primary class, with raster resampling/filtering/size-parameter methods, and
  the `HfunCollector` layers "topography-based constraints, Courant-number
  constraints, region-specific constraints, contours, channels, and flow
  limiters" on top -- i.e. hfun-from-raster plus composable constraint layers
  is a first-class, documented path (per OCSMesh docs site and package
  layout: `ocsmesh/hfun/`, `ocsmesh/geom/`, `ocsmesh/features/`,
  `ocsmesh/engines/`, `ocsmesh/ops/`, `ocsmesh/cli/`).
- **Editing/remeshing an existing mesh**: yes, more directly than either
  OceanMesh2D python port or seamsh. The CLI ships `remesh_by_shape` and
  `remesh_by_dem` subcommands, plus utility ops `clip_mesh_by_shape()`,
  `clip_mesh_by_vertex()`, and `cleanup_*` functions -- these operate on an
  already-built mesh, not only on raw geometry+sizing from scratch. OCSMesh
  v2's stated scope adds "mesh merging" and "post-processing" for stitching a
  river mesh into a floodplain mesh
  (https://ui.adsabs.harvard.edu/abs/2024AGUFMOS41D0468C/abstract), which is
  incremental composition of meshes, not monolithic regeneration.
- **Determinism of JIGSAW**: JIGSAW (https://github.com/dengwirda/jigsaw,
  https://github.com/dengwirda/jigsaw-python, Darren Engwirda) is described as
  combining "refinement-based algorithms for constructing new meshes,
  optimisation-driven techniques for improving existing grids" (including a
  `marche` fast-marching solver for spacing optimization on an existing
  configuration -- i.e. JIGSAW itself has a local-improvement mode, not only
  from-scratch generation). No upstream page found makes an explicit
  determinism/reproducibility claim or documents a random seed; the algorithm
  family (Delaunay-refinement / frontal-Delaunay, serial) has no inherent
  stochastic step the way DistMesh's initial point cloud does, but that is an
  inference from algorithm class, not a documented guarantee -- flag as
  **unverified, not confirmed deterministic**. Given this repo's own measured
  non-determinism for OceanMesh2D's DistMesh pipeline even with a fixed seed
  (section 1), determinism should be treated as "verify per engine, do not
  assume" generally in this space.

---

## 5. gmsh in TELEMAC practice

Evidence beyond "the parser exists":

- **The parser is real and is a first-class import path, not a converter
  bolted on the side.** INTERNAL (this repo's own image inspection of
  `trid3nt-local/telemac:latest`): `data_manip/extraction/parser_gmsh.py`
  defines `class MSH(Selafin)` -- it **subclasses Selafin** and reads a Gmsh
  `.msh` file straight into Selafin's own internal arrays. That is a stronger
  integration than a one-way converter script: TELEMAC's own Python API
  treats a Gmsh mesh as a native Selafin-equivalent object once parsed.
  Format support is capped at Gmsh format version 2, and only `line`
  (boundary) and `triangle` (element) entities are handled -- confirmed by
  web search of TELEMAC-side documentation (R interface docs mirror this:
  https://rdrr.io/cran/telemac/man/read_msh.html) matching the in-image
  finding.
- **STBTEL** (the Fortran mesh-refine/convert module, driven via
  `scripts/python3/stbtel.py` + a `.cas` steering file) infers input/output
  format from file extension, so a Gmsh `.msh -> SELAFIN` conversion/refine
  can run through the STBTEL path as well as the direct `MSH(Selafin)`
  Python path -- two independent, supported routes into TELEMAC from gmsh
  output.
- **What practitioners actually use, per the forum**: the TELEMAC-MASCARET
  forum (opentelemac.org) has recurring threads on Blue Kenue (NRC's own
  mesh generator/editor, tightly paired with TELEMAC I/O), SMS-derived
  meshes imported into Blue Kenue, and a T3 mesh generator, alongside a
  dedicated multi-page thread "using Gmsh grid in TELEMAC"
  (https://www.opentelemac.org/index.php/assistance/forum5/other/6971-using-gmsh-grid-in-telemac,
  3 pages) that opens by calling gmsh "a powerful program to generate
  unstructured grids in a rather flexible way." A companion academic source,
  "QGIS as a pre- and post-processor for TELEMAC"
  (https://henry.baw.de/server/api/core/bitstreams/0ab661ce-2549-4824-a0a1-acb59b077d8b/content),
  documents a QGIS + gmsh + Selafin-conversion pipeline as a working
  alternative to the commercial/NRC tools. Net read: **Blue Kenue is the
  TELEMAC-native default and SMS is the common commercial import source;
  gmsh is a well-worn secondary path with real forum mileage and a
  first-class Python parser, not a fringe hack** -- but it is not what most
  practitioners reach for first, because Blue Kenue/Janet/SMS carry the
  GUI-editing and boundary-condition workflows TELEMAC users expect, which
  gmsh does not replace on its own (boundary-condition segmentation still
  needs `Conlim.set_numliq()` downstream regardless of mesh source, per
  INTERNAL recon).

---

## 6. Incremental/local remesh of an EXISTING triangulation with constraints

Which python-reachable tools actually modify an existing mesh in place
(vs. regenerate from geometry), and which of those are used in coastal work
vs generic FEM:

- **gmsh on discrete meshes**: gmsh supports "discrete model entities"
  defined by an existing mesh (e.g. STL/loaded triangulation), which can be
  reparametrized and then remeshed with a standard algorithm
  (Gmsh manual, https://gmsh.info/doc/texinfo/gmsh.html; "Reclassify 2D" +
  algorithm selection is the documented interactive route). This is a real,
  supported capability, but it is a **generic FEM-meshing feature of gmsh
  itself**, not something built or documented for the coastal community
  specifically -- no coastal-specific tutorial or paper applying gmsh's
  discrete-remesh path to an ADCIRC/SCHISM/TELEMAC mesh was found in this
  pass.
- **mmg / pymmg / mmgpy** (https://www.mmgtools.org/,
  https://github.com/gnikit/pymmg, https://github.com/kmarchais/mmgpy):
  `mmg2d` is purpose-built for exactly this -- "given an input mesh and a
  metric defined upon it, Mmg applies a sequence of operations... to
  optimize the quality of its elements" via node insertion/deletion, edge
  swap, and node movement -- genuine local incremental remeshing of an
  existing triangulation under a size/anisotropy metric, plus level-set
  domain extraction (`-ls`) and boundary Lagrangian displacement (`-lag`).
  Coastal-adjacent use is documented via Thetis: "the Thetis coastal ocean
  model... uses metric-based mesh adaptation achieved using Mmg" (search
  result summary referencing MMG/Thetis integration; also
  `adapt_utils` at https://github.com/joewallwork/adapt_utils, "Mesh
  adaptation utilities for coastal ocean modelling in Firedrake and
  Thetis"). This is closer to a "trodden-in-adjacent-community" tool
  (Firedrake/Thetis metric-based adaptation) than a generic-FEM-only tool,
  but it is still not part of the ADCIRC/OceanMesh2D/SCHISM-native toolchain
  -- it is a Thetis-world pattern, imported rather than home-grown there.
- **JIGSAW's `marche`**: a fast-marching solver for optimizing mesh-spacing
  configurations on an existing setup, and JIGSAW's broader "optimisation-
  driven techniques for improving existing grids" -- this is the one
  local/incremental capability that lives **inside the exact engine
  (jigsawpy) that OCSMesh already uses operationally for SCHISM**, which
  makes it the most coastally-trodden of the pure-remesh options once OCSMesh
  is already the generation path.
- **Triangle (Shewchuk)**: available as an alternate OCSMesh engine backend,
  but it is a generic constrained-Delaunay 2D mesher with no coastal-specific
  wrapping of its own; any coastal use goes through OCSMesh, not Triangle
  directly.
- **TELEMAC's own `pretel/meshes.py`** (INTERNAL, in-image inspection of
  `trid3nt-local/telemac:latest`, `/opt/conda/opentelemac/scripts/python3/
  pretel/meshes.py`): a 54-function pure-NumPy module doing exactly this
  class of work natively inside the TELEMAC toolchain -- `subdivide_mesh3/4`
  (local refine), `merge_min_4_nodes` / `cleave_max_7_nodes` (node
  merge/cleave), `filter_mesh_resolution`, `remove_duplicate_nodes`,
  `remove_extra_nodes`, boundary-point derivation
  (`get_ipobo`/`cross_check_boundaries`), and thin-plate-spline field
  remapping. This is a **community-native, already-shipped incremental-edit
  toolkit specific to TELEMAC meshes**, independent of gmsh/mmg/jigsaw
  entirely -- worth weighing directly against a gmsh/mmg route for any
  TELEMAC-target work, since it needs zero new dependency.

---

## VERDICT

Ranked for the stated need: programmatic, automatable, conformal-breakline-
embedding, local-refinement-capable, deterministic, python-driven.

| Rank | Route | Fit | Trodden-ness | Shimming risk |
|---|---|---|---|---|
| 1 | **OCSMesh + jigsawpy** | Best overall fit. Native raster hfun, composable constraint layers, `remesh_by_shape`/`remesh_by_dem`/clip/merge ops on existing meshes, JIGSAW `marche` for local spacing optimization on an existing config, operational at NOAA for SCHISM. | High within the SCHISM/NOAA sub-community; the closest thing this landscape has to an operational, actively-released (2026-07) standard. | Real but bounded: jigsawpy binary-backend resolution is environment-dependent (this repo's own sandbox could not resolve it from its index -- INTERNAL); v2.0.0 refactor is mid-flight so API surface is moving; determinism of JIGSAW itself is not documented, only inferred from algorithm class. |
| 2 | **regenerate-with-OceanMesh2D (MATLAB) or oceanmesh (python port)** | Good fit for "encode the change as a sizing/constraint change and rebuild," which is the field's actual default habit (ADCIRC wiki explicitly frames this as a case-by-case call, and `remesh_patch` exists precisely because full regeneration isn't always right). Weak fit for "local edit without touching the rest of the mesh" in the Python port specifically -- that surface didn't carry over from MATLAB. | **This is the most trodden path**, full stop -- OceanMesh2D is the de facto ADCIRC/SCHISM/SWAN/TELEMAC-source community mesher (its own tagline lists all four solvers), with a mature `msh`-class edit API and a large citation base. | MATLAB `msh` edit methods (`remesh_patch`, `extract_subdomain`, etc.) do not exist in the Python port -- porting or shimming needed if incremental edit is required. Determinism is **measured False** even with a fixed seed (INTERNAL, this repo, three rebuilds/one config -> two distinct meshes) -- disqualifying on its own for a hard determinism requirement unless the recipe layer accepts "equivalent replay," not identical. |
| 3 | **raw gmsh API (discrete-entity remesh on an existing triangulation)** | Technically capable (discrete-model reparametrize + remesh is real and documented), fully scriptable/deterministic-by-algorithm-class, and it is TELEMAC's best-supported non-native import format (first-class `MSH(Selafin)` parser, plus an independent STBTEL route) -- so for a TELEMAC-target workflow specifically, this ranks higher. For ADCIRC/SCHISM/SWAN, it is a bare capability with no coastal-domain sizing/constraint layer of its own. | **Outlier for ADCIRC/SCHISM/SWAN** (no coastal wrapper, no coastal-specific tutorial found for the discrete-remesh path); **mainstream-adjacent for TELEMAC** (real forum mileage, first-class parser, not a fringe hack, but still secondary to Blue Kenue/SMS as the practitioner default). | High for ADCIRC/SCHISM work: needs a coastal sizing/constraint shim built from scratch (breakline embedding, raster-driven sizing, CRS/shapefile handling -- everything seamsh/OCSMesh already provide). Low-to-moderate for TELEMAC: the import path is native, but boundary-condition segmentation (`Conlim.set_numliq`) still needs building regardless of mesh source. |
| 4 | **seamsh** | Good conceptual fit (gmsh-based, external raster sizing via `field.Raster`, shapefile geometry in, mesh/shapefile/GeoPackage out) -- closest thing to a "coastal wrapper around gmsh" that isn't OCSMesh. | Narrow: home-grown at SLIM/UCLouvain, thin evidence of use outside that group/adjacent institutions, last release 0.5 (2025-01-17, ~19 months stale as of 2026-08). No ADCIRC/SCHISM/TELEMAC-community citations found (its lineage is the SLIM ocean/ice model, not the ADCIRC/SCHISM/TELEMAC family this question targets). | Moderate: docs are thin (had to reconstruct capability from a mirrored git-page + PyPI/Snyk rather than a live gitlab.uliege.be fetch, which itself failed from this environment), and stale-release risk for anything needing active upstream support. |
| 5 | **qmesh / qmesh3** | Conceptually similar to seamsh (gmsh + QGIS), historically used by Thetis/Fluidity. | Its own maintainers call qmesh3 a temporary stopgap "until the original qmesh updates are released" -- i.e. abandoned-in-place by its own description, kept alive only enough for legacy Python-3/QGIS-3 compatibility. | Highest of the group: adopting it means adopting a project that self-describes as a stopgap with no forward roadmap. |
| -- | **mmg/pymmg/mmgpy** | Best-in-class for the narrow "locally remesh an existing triangulation under a metric field" sub-problem (node insert/delete/swap/move under a metric, level-set domain extraction) -- genuinely does what section 6 asked about local incremental remesh. Not a full coastal-mesh pipeline on its own (no raster-hfun-to-metric or coastline-geometry layer built in). | Trodden in the Firedrake/Thetis metric-adaptation world (adjacent community), not in the ADCIRC/SCHISM/TELEMAC-native toolchain -- would be imported into this landscape, not found already living in it. | Moderate: need a metric-field construction shim (raster/hfun -> mmg metric format) since mmg itself doesn't speak coastal rasters/shapefiles. |
| -- | **TELEMAC's own `pretel/meshes.py`** | Purpose-built local-edit primitives (refine, merge/cleave, resolution filter, dedupe) that already ship inside the TELEMAC install used in this repo -- zero new dependency for TELEMAC-target incremental edits. | Community-native for TELEMAC specifically; not applicable to ADCIRC/SCHISM/SWAN. | Lowest of any option listed here, for TELEMAC-only work: it is already in the image, already exercised by this repo's own worker. |

**Most trodden path overall**: regenerate with OceanMesh2D (or its Python
port) from a changed sizing/constraint spec -- this is what the ADCIRC/SCHISM
community actually reaches for by default, per the wiki's own framing and
OceanMesh2D's four-solver-wide adoption (ADCIRC, FVCOM, WaveWatch3, SWAN,
SCHISM, TELEMAC per its own README tagline).

**Best fit for this repo's stated need** (programmatic, deterministic, local
refine, conformal breaklines, python): **OCSMesh + jigsawpy**, because it is
the only option in this survey that combines (a) an actively-released,
operationally-used coastal wrapper, (b) native raster-hfun and composable
constraint sizing, (c) documented existing-mesh edit ops
(`remesh_by_shape`/`remesh_by_dem`/clip/merge), and (d) a meshing engine
(JIGSAW) that itself exposes a local-improvement mode (`marche`) rather than
only from-scratch generation. Trodden-ness and best-fit **diverge here**:
OceanMesh2D is what the field defaults to, OCSMesh is the better-engineered
answer to the specific edit/determinism/automation bar this repo is asking
about -- and OceanMesh2D's Python port fails that bar on measured
determinism alone (INTERNAL finding, section 1).

**Shimming-risk summary, cleanest to messiest**: OCSMesh (moving API, binary-
backend friction) < gmsh-for-TELEMAC (native parser, but no coastal sizing
layer) < seamsh (thin docs/adoption, stale release) < gmsh-for-ADCIRC/SCHISM
(build the whole coastal wrapper yourself) < qmesh3 (adopting a
self-described stopgap). A raw-gmsh route is **not an outlier as a TELEMAC
mesh source** (first-class parser, real forum use) but **is an outlier as a
coastal-mesh-generation front end for ADCIRC/SCHISM/SWAN** (no
community-built sizing/constraint layer exists for it there the way seamsh,
OCSMesh, and OceanMesh2D provide for their respective niches).

---

## Sources

- https://github.com/CHLNDDEV/OceanMesh2D
- https://github.com/CHLNDDEV/OceanMesh2D/releases
- https://coast.nd.edu/reports_papers/2018-oceanmesh2d-user-guide.pdf
- https://doi.org/10.5194/gmd-12-1847-2019 (Roberts, Pringle, Westerink 2019)
- https://wiki.adcirc.org/Grid_Development_and_Editing
- https://github.com/CHLNDDEV/oceanmesh
- https://pypi.org/project/seamsh/
- https://security.snyk.io/package/pip/seamsh
- https://jlambrechts.git-page.immc.ucl.ac.be/seamsh/examples/2-gis.html
- https://jlambrechts.git-page.immc.ucl.ac.be/seamsh/seamsh.gmsh.html
- https://www.researchgate.net/publication/389220773 (2025 storm-surge paper using seamsh)
- https://github.com/qmesh3/qmesh3
- https://github.com/FluidityProject/fluidity/wiki/Gmsh-tutorial
- https://github.com/thetisproject/thetis
- https://github.com/noaa-ocs-modeling/OCSMesh
- https://noaa-ocs-modeling.github.io/OCSMesh/
- https://pypi.org/project/ocsmesh/
- https://repository.library.noaa.gov/view/noaa/33879 (NOAA Tech Memo NOS CS 47)
- https://github.com/noaa-ocs-modeling/OCSMesh/discussions/36
- https://ui.adsabs.harvard.edu/abs/2024AGUFMOS41D0468C/abstract (OCSMesh v2)
- https://github.com/dengwirda/jigsaw
- https://github.com/dengwirda/jigsaw-python
- https://www.opentelemac.org/index.php/assistance/forum5/other/6971-using-gmsh-grid-in-telemac
- https://rdrr.io/cran/telemac/man/read_msh.html
- https://henry.baw.de/server/api/core/bitstreams/0ab661ce-2549-4824-a0a1-acb59b077d8b/content ("QGIS as a pre- and post-processor for TELEMAC")
- https://gmsh.info/doc/texinfo/gmsh.html (discrete-entity remesh)
- https://www.mmgtools.org/
- https://github.com/gnikit/pymmg
- https://github.com/kmarchais/mmgpy
- https://github.com/joewallwork/adapt_utils
- INTERNAL: `docs/research/om2d-telapy-mesh-recon.md` (this repo, 2026-08-27
  in-image measurements of oceanmesh determinism, TELEMAC gmsh-parser
  subclassing, and `pretel/meshes.py` edit functions)
