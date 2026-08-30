# Awaiting the worker-unification port

Three TELEMAC templates - `telemac_rain_on_grid`, `telemac_river_dye`,
`telemac_do_sag` - are half repointed. This note says which half landed, which
half did not, and what the remaining test failures are, so the baseline stage
cites a list rather than re-deriving one.

The rule that draws the line is the 2026-08-30 REPOINT ruling, DS-4: the reach
and catchment templates' worker-facing halves complete IN THE WORKER-UNIFICATION
WAVE, because the worker still meshes the ribbon and the catchment from the
manifest and the last mile of the LEGO ruling is the worker's staged contract.
`workers/` is frozen until that wave.

## What landed (server side)

**The chains are declared.** Domain narrowing is plan-level chaining of
processing tools, which is what the LEGO ruling asks for:

- `telemac_rain_on_grid`: `Data("basin", Build.tool("delineate_watershed", ...))`
  -> `Data("sized", Build.tool("combine", polygon=Ref("basin"), lines=D.rivers))`
  -> `MESH = tool.build_mesh(mesher="om2d", extent=Ref("sized"), ...)`. The
  template registers again.
- `telemac_river_dye` / `telemac_do_sag`:
  `Data("centerline", Fetch.tool("fetch_nhdplus_nldi_navigate", ...))` ->
  `Data("ends", Build.tool("endpoints", line=D.centerline))` ->
  `Data("banks", Fetch.tool("fetch_nhd_area_water", ...))` ->
  `Data("reach_polygon", Build.tool("section", polygon=D.banks,
  between=Ref("ends.between")))`. The `between` cut keeps the two transect faces
  the inflow and the outflow are prescribed on.

**Two generic geometry tools** back those chains: `combine` (a polygon plus the
lines riding inside it -> one geometry document) and `endpoints` (a line -> its
two end points). Both are registered tools, so `Build.tool("combine", ...)` in a
declaration and `combine(...)` from a chat are the same call.

**`om2d.read_geometry` unwraps a layer value**, so `extent=Ref("basin")` works as
written: a chain binds the producing tool's `LayerURI`, not the uri string it
carries.

**Dead resolution removed.** `steps/rain_on_grid.py::_adopt_case_mesh` is gone -
one resolver for a mesh a case already holds, and it is the mesh router's at the
build door. `mesh_max_iter` and `outlet_snap_cells` are gone with the retired
catchment mesher.

## What did NOT land (worker-facing)

- `steps/rain_on_grid.py::build_catchment_mesh` still reads the retired catchment
  mesher's fields (`min_edge_length_m`, `max_edge_length_m`, `grade`,
  `max_iter`, `snap_search_cells`) off the declaration and still calls
  `mesh/watershed.py::generate_catchment_mesh`. It becomes a `MeshArtifact`
  consumer when the worker's staged contract takes one.
- `steps/reach.py::build_corridor_mesh` and `ReachMesh` still mesh the corridor,
  and the two reach `MESH` blocks still name `corridor_tin`. Repointing them is
  blocked on a separate DESIGN-STOP (below), not only on the worker.
- `mesh/watershed.py` and `mesh/precondition_gate.py` are still in the tree;
  their retirement is elegance-review P2.

## The open DESIGN-STOP

The reach templates declare `mesh_resolution="auto"` alongside an optional
`mesh_resolution_m`. `auto` was the retired `corridor_tin` mesher's own sizing
rung; `om2d` has no equivalent, and `refine.edge_length` refuses a value that is
absent. Deciding what edge an `auto` reach mesh is built at - and whether
`mesh_resolution_m` stops being optional - is a judgment nobody has ruled, so the
two reach `MESH` blocks are untouched and the two templates stay unregistered.

## The failures this leaves

The offline suite, measured. `88 failed, 8969 passed` before the repoint ->
`80 failed, 9031 passed` after, plus the same 3 collection errors either side.
Nothing new failed; everything below is one of the three templates' worker-facing
halves or the two unregistered reach templates.

Three modules cannot be COLLECTED (run the suite with `--ignore` on them):

- `tests/test_mesh_declaration_travel.py` - imports the purged `corridor_tin`
  mesher module.
- `tests/test_telemac_event_time.py`, `tests/test_telemac_rain_forcing.py` -
  import `do_sag` / `river_dye`, which still declare `corridor_tin`.

The 80 failures, by module:

| module | count | why |
|---|---|---|
| `test_run_river_dye_scenario.py` | 31 | `river_dye` declares `corridor_tin` |
| `test_telemac_do_sag.py` | 16 | `do_sag` declares `corridor_tin` |
| `test_workflow_skeleton.py` | 14 | the reach rows of the mesh parametrizations, and the corridor-shape assertions |
| `test_telemac_input_provenance.py` | 6 | the same two reach templates |
| `test_resolution_sensitivity.py` | 4 | the same two reach templates |
| `test_rerun_with_overrides.py` | 2 | the same two reach templates |
| `test_door_dissolution.py` | 2 | every template registered + surfacing |
| `test_declarative_library.py` | 2 | `do_sag`'s gate + docstring views |
| `test_tool_retrieval.py` | 1 | corpus keys for the three unregistered templates |
| `test_template_hygiene.py` | 1 | the hygiene gate's template roster |
| `test_telemac_rain_on_grid_template.py` | 1 | asserts the template is registered |
