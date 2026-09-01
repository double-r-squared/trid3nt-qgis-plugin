# `workflows/mesh/` - the one mesh front

Every template's mesh ask enters here and leaves as an accepted topology: the
router validates the ask, a mesher builds it, a session records the edit chain, a
gate presents it, and an artifact record is what the solve reads.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. Re-exports nothing: the router imports the tool registry, which imports the templates. |
| `artifact.py` | `MeshArtifact` - the accepted mesh's record (its files, counts, CRS, probes, boundary roles) and the case-scoped seam a later run rediscovers it through. |
| `corpus.yaml` | The retrieval phrasings that route a "build the mesh a solver runs on" ask to `build_mesh`. |
| `gate.py` | The gate loop: a built mesh presented with its probes and its editable layer, then edited, restarted or accepted. |
| `grid_geometry.py` | Regular-grid domain math - a geographic bbox plus a metre resolution to the canonical origin, spans, cell size and row/col counts. |
| `kinds.py` | The mesh KIND vocabulary: the shapes a mesher in this tree builds, and what a template may declare it accepts. |
| `session.py` | `MeshSession` - the mesh under construction plus the ordered chain of named edits, journalled to `mesh_recipe.jsonl` beside the mesh files. |
| `step.py` | The declared MESH step: the template's frozen ask, built under the gate, as the one step every plan puts before its author stage. |
| `tool.py` | `build_mesh` - the router. Checks an ask against the mesher's own declared fields and edit actions, and is the author word `tool.build_mesh` reaches. |
| `topology.py` | The accepted topology a geometry file cannot state: which contiguous run of boundary nodes carries which declared role, written and read back. |

## Subfolders

| folder | what it is |
| --- | --- |
| `meshers/` | One file per mesh library, each declaring its spec fields and its edit actions: `om2d.py` (OceanMesh2D), `reg_grid.py` (the regular lattice), and `drivers/` - the in-container scripts mounted into the image whose libraries they drive (the mesh box for oceanmesh, the TELEMAC box for the geometry pair and the steering-file parse). |
| `shared/` | What every mesher needs and no mesher owns: `nodes.py` (projection, sampling, slope, reading an accepted mesh's nodes and a centreline in its metres) and `selafin_cli.py` (the SELAFIN + `.cli` pair, written as one artifact). |
