# `mesh/` - the one mesh front

Every template's mesh ask enters here and leaves as an accepted topology. A mesh
is defined by exactly one object: its RECIPE - three mesher-agnostic params
(`extent`, `resolution_m`, `kind`), the backend constructor's own `build` kwargs
(checked against the mesher's declared roster, recorded only when declared), and
one ordered list of `mesh_op(...)` entries, which is the program that produces
it. The router validates the recipe, a mesher executes it, a session holds it
and regenerates WHOLESALE on every change, a gate presents it with its ops
numbered, and an artifact record - carrying that recipe as its provenance - is
what the solve reads. Cells may have any vertex count: a uniform triangle or
quad mesh keeps its `.2dm` display face, a polygon mesh of any other arity is
written CF-UGRID, and the probes measure edge over edge either way.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. Re-exports nothing. |
| `artifact.py` | `MeshArtifact` - the accepted mesh's record (its files, counts, CRS, probes, boundary roles) and the case-scoped seam a later run rediscovers it through. |
| `corpus.yaml` | The retrieval phrasings that route "build the mesh a solver runs on" to `build_mesh` and "change how this mesh was built" to `mesh_op`. |
| `gate.py` | The mesh's face on the ONE gate (`inputs/gate/input_review.py`): it builds the round's card - the probes, the numbered recipe, the editable layer, and the rows for the size word, the revert and a hand-edited layer - and applies the reply back onto the session. One card path for every mesher, and no gate machine of its own. |
| `grid_geometry.py` | Regular-grid domain math - a geographic bbox plus a metre resolution to the canonical origin, spans, cell size and row/col counts. |
| `inputs.py` | The ONE typed conversion a data-valued op kwarg passes through: a typed slot value -> its own collection, raster -> the readable raster, layer -> the geometry document. Nothing is guessed. |
| `op_tool.py` | `mesh_op` - the runtime face of the word a recipe is written in: append, alter by index or remove one call on the open session's recipe, then regenerate. |
| `recipe.py` | `MeshRecipe` - the one mesh-defining object, its editing methods, its JSON form and the plain mapping a plan step carries it as. |
| `session.py` | `MeshSession` - the mesh under construction: it holds THE recipe, regenerates on every change, and journals the edit events to `mesh_recipe.jsonl`. |
| `step.py` | The declared MESH step: the template's frozen recipe, built under the gate, as the one step every plan puts before its author stage. |
| `tool.py` | `build_mesh` - the router. Builds a validated recipe, and is the author word `tool.build_mesh` reaches. |
| `water.py` | `water_polygon` - the water a mapped coastline leaves inside a box, which is the polygon a mesh is cut from: OSM draws land on the LEFT of a way, so the ways split the box into faces and what no way calls land is what is meshed. |

## Subfolders

| folder | what it is |
| --- | --- |
| `meshers/` | What a mesher IS - namespaces, a role adapter, a default recipe - plus one file per mesh library: `om2d.py` (OceanMesh2D), `reg_grid.py` (the regular lattice), `claw_grid.py` (the GeoClaw grid recipe: a lon/lat level-1 box, refinement ratios and `flagregion` ops, built in the daemon as the level-1 lattice with its per-level cell counts as gate-card rows), and `flopy_grid.py` (the FloPy facade - `triangle`, `voronoi` and `gridgen`, three registrations in one file) over `meshers/flopy_build.py` (the in-process build: it INTERPRETS the recipe's ops list, calling each name verbatim on FloPy, and runs the `triangle` and `gridgen` executables `scripts/fetch_binaries.sh` installs into `bin/`, found by `TRID3NT_TRIANGLE_BIN` / `TRID3NT_GRIDGEN_BIN`; `set_region_size` is the one refine word it defines itself, expanded into the library's own calls: `add_polygon` plus `add_region` for triangle and voronoi, `add_refinement_features` for gridgen). OceanMesh2D's in-container script lives beside its Dockerfile at `workers/mesh/scripts/om2d.py`, mounted read-only at `/drivers` by `run_image_script` (`workflows/solver/image_script.py`), and interprets its ops the same way. The TELEMAC steering scripts (`cas.py`, `dico.py`) live at `workflows/telemac/authoring/engine_scripts/` and run in the server's own interpreter over the opentelemac tree `scripts/fetch_opentelemac.sh` installs - they drive the engine's own steering readers for `workflows/telemac/` and the catalog instrument, not the mesh library. |
| `shared/` | What every mesher needs and no mesher owns: `primitives.py` (the shared op namespace - `set_bed` over ONE surface, recording what painted the nodes, and `set_boundary_roles` over the domain's declared runs), `nodes.py` (projection, a raster or a fetched layer sampled at the nodes, slope, boundary contours, reading an accepted mesh's nodes off its record and a centreline in its metres). `formats/` holds the TIN writers a solver reads - `hgrid.gr3`, `fort.14`, SMS `2dm` - behind one topology pass. |
