# `workflows/mesh/` - the one mesh front

Every template's mesh ask enters here and leaves as an accepted topology. A mesh
is defined by exactly one object: its RECIPE - three mesher-agnostic params
(`extent`, `resolution_m`, `kind`) plus one ordered list of `mesh_op(...)`
entries, which is the program that produces it. The router validates the recipe,
a mesher executes it, a session holds it, a gate presents it with its ops
numbered, and an artifact record - carrying that recipe as its provenance - is
what the solve reads.

Every change regenerates the mesh WHOLESALE. There is no edit chain and no
history object: the journal captures edit events as audit, undo is editing the
recipe back, and the one structured revert is reset-to-declaration.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. Re-exports nothing: the router imports the tool registry, which imports the templates. |
| `artifact.py` | `MeshArtifact` - the accepted mesh's record (its files, counts, CRS, probes, boundary roles) and the case-scoped seam a later run rediscovers it through. |
| `corpus.yaml` | The retrieval phrasings that route "build the mesh a solver runs on" to `build_mesh` and "change how this mesh was built" to `mesh_op`. |
| `gate.py` | The gate loop: a built mesh presented with its probes, its numbered recipe and its editable layer, then edited, reset or accepted. ONE card path for every mesher. |
| `grid_geometry.py` | Regular-grid domain math - a geographic bbox plus a metre resolution to the canonical origin, spans, cell size and row/col counts. |
| `inputs.py` | The ONE typed conversion a data-valued op kwarg passes through: raster -> the readable raster, layer -> the geometry document. Nothing is guessed. |
| `kinds.py` | The mesh KIND vocabulary: the shapes a mesher in this tree builds, and what a template may declare it accepts. |
| `op_tool.py` | `mesh_op` - the runtime face of the word a recipe is written in: append, alter by index or remove one call on the open session's recipe, then regenerate. |
| `recipe.py` | `MeshRecipe` - the one mesh-defining object, its editing methods, its JSON form and the plain mapping a plan step carries it as. |
| `session.py` | `MeshSession` - the mesh under construction: it holds THE recipe, regenerates on every change, and journals the edit events to `mesh_recipe.jsonl`. |
| `step.py` | The declared MESH step: the template's frozen recipe, built under the gate, as the one step every plan puts before its author stage. |
| `tool.py` | `build_mesh` - the router. Builds a validated recipe, and is the author word `tool.build_mesh` reaches; also the supplied-mesh resolution order. |
| `topology.py` | The accepted topology a geometry file cannot state: which contiguous run of boundary nodes carries which declared role, written and read back. |

## Subfolders

| folder | what it is |
| --- | --- |
| `meshers/` | What a mesher IS - namespaces, a role adapter, a default recipe - plus one file per mesh library: `om2d.py` (OceanMesh2D) and `reg_grid.py` (the regular lattice). `drivers/` holds the in-container scripts mounted into the images whose libraries they drive; the om2d driver INTERPRETS the recipe's ops list, calling each name verbatim on the library. |
| `shared/` | What every mesher needs and no mesher owns: `primitives.py` (the shared op namespace - `set_bed`, `set_boundary_roles`), `nodes.py` (projection, sampling, slope, boundary contours, reading an accepted mesh's nodes and a centreline in its metres) and `selafin_cli.py` (the SELAFIN + `.cli` pair, written as one artifact). |
