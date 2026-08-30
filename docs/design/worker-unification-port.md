# Awaiting the worker-unification port

Three TELEMAC templates - `telemac_rain_on_grid`, `telemac_river_dye`,
`telemac_do_sag` - were half repointed onto the LEGO chaining model (docs/
IDEAS.md 2026-08-30). The REACH half - `telemac_river_dye` and
`telemac_do_sag` - is now DONE and both are registered. Only
`telemac_rain_on_grid`'s worker-facing half remains, blocked on the
worker-unification wave. This note says which half landed, which half did
not, and what the remaining test failures are, so the baseline stage cites a
list rather than re-deriving one.

The rule that draws the line is the 2026-08-30 REPOINT ruling, DS-4: the
catchment template's worker-facing half completes IN THE WORKER-UNIFICATION
WAVE, because the worker still meshes the catchment from the manifest and the
last mile of the LEGO ruling is the worker's staged contract. `workers/` is
frozen until that wave.

## What landed - both reach templates, server AND mesh side

**The chains are declared and the mesh ask reads their product.** Domain
narrowing is plan-level chaining of processing tools, which is what the LEGO
ruling asks for:

- `telemac_rain_on_grid`: `Data("basin", Build.tool("delineate_watershed", ...))`
  -> `Data("sized", Build.tool("combine", polygon=Ref("basin"), lines=D.rivers))`
  -> `MESH = tool.build_mesh(mesher="om2d", extent=Ref("sized"), ...)`. The
  template registers again once the worker side lands (below).
- `telemac_river_dye` / `telemac_do_sag`:
  `Data("centerline", Fetch.tool("fetch_nhdplus_nldi_navigate", ...))` ->
  `Data("ends", Build.tool("endpoints", line=D.centerline))` ->
  `Data("banks", Fetch.tool("fetch_nhd_area_water", ...))` ->
  `Data("reach_polygon", Build.tool("section", polygon=D.banks,
  between=Ref("ends.between")))` -> `MESH = tool.build_mesh(mesher="om2d",
  extent=Ref("reach_polygon"), refine={"edge_length": P.mesh_resolution_m})`.
  The `between` cut keeps the two transect faces the inflow and the outflow
  are prescribed on. Both templates are REGISTERED
  (`trid3nt_server/tools/__init__.py`) and import clean.

**Two generic geometry tools** back those chains: `combine` (a polygon plus
the lines riding inside it -> one geometry document) and `endpoints` (a line
-> its two end points). Both are registered tools, so `Build.tool("combine",
...)` in a declaration and `combine(...)` from a chat are the same call.

**`om2d.read_geometry` unwraps a layer value**, so `extent=Ref("basin")` /
`extent=Ref("reach_polygon")` work as written: a chain binds the producing
tool's `LayerURI`, not the uri string it carries.

**AUTO EDGE DIES - the edge is always explicit (2026-08-30 ruling).** The
reach templates' `mesh_resolution` mode (`"auto" | "fine" | "coarse"`) was the
retired `corridor_tin` mesher's own sizing rung; `om2d` has no equivalent
rung, so nothing replaces it. `mesh_resolution_m` is now the ONLY granularity
lever: `door=SCENARIO`, `default=14.0` (a LABELED default under the
two-modes law, not a derived one), `user_lever=True`. The user states the
edge or the model fills the default in the open; either way the number that
reaches the mesh is one explicit sheet value, bounded on both sides by
`suggest_mesh_size_m` (raised by the node budget, lowered by the
>= 2-cells-across-the-channel rule) and narrated when a bound moved it. This
closes the DESIGN-STOP the pre-repoint version of this note left open. See
`docs/DELETION_LEDGER.md` ("AUTO EDGE DIES - the reach templates' sizing
rung") for what that deleted.

**ONE mesh step, for every template.** `ReachMesh.corridor` and
`Catchment.mesh` are gone; `workflows/mesh/step.py::MeshStep.build` is the one
declared mesh step (elegance review P2). Its `name` kwarg is presentation only -
the DOMAIN is the chain's `reach_polygon` / `sized`, already fixed in `MESH` at
declaration time. The reach plan's step is labeled `mesh`, and the deck reads
`Ref("mesh")`.

**`channel_width_m` and `bank_source` are GONE (elegance review P3).** The
parity shim that carried them on `PHYSICS` is deleted: both Params,
`normalize_bank_source` and its vocabulary, the review entry and the two
manifest fields. Only `reach_length_km` still rides `PHYSICS`, because the deck
states the stretch it wrote for. The worker keeps its own `bank_source` default
until the wave lands; the server names it nowhere. The granularity the deck
records is now the edge the ACCEPTED mesh was MEASURED at
(`mesh["min_edge_m"]`), so `suggest_mesh_size_m` and its node estimate are gone
too.

**Dead resolution removed.** `steps/rain_on_grid.py::_adopt_case_mesh` is
gone - one resolver for a mesh a case already holds, and it is the mesh
router's at the build door. `mesh_max_iter` and `outlet_snap_cells` are gone
with the retired catchment mesher.

## What did NOT land (worker-facing) - `telemac_rain_on_grid` only

- `steps/rain_on_grid.py::build_catchment_mesh` still reads the retired
  catchment mesher's fields (`min_edge_length_m`, `max_edge_length_m`,
  `grade`, `max_iter`, `snap_search_cells`) off the declaration and still
  calls `mesh/watershed.py::generate_catchment_mesh`. It becomes a
  `MeshArtifact` consumer when the worker's staged contract takes one. This
  is the ONLY thing left unported - the chain, the `om2d` mesh ask and the
  registration line are all ready and waiting on this one step
  (`trid3nt_server/tools/__init__.py` names the exact line to uncomment).
- `mesh/watershed.py` and `mesh/precondition_gate.py` are still in the tree;
  their retirement is elegance-review P2.

## The reach templates' open DESIGN-STOP is CLOSED

The prior version of this note recorded a DESIGN-STOP about what edge an
`auto` reach mesh is built at. The 2026-08-30 AUTO EDGE DIES ruling settled
it (above): there is no `auto` mode any more, `mesh_resolution_m` is required
with a labeled default, and both reach `MESH` blocks now declare `om2d`. No
DESIGN-STOP is open on either reach template.

## The failures this leaves

Measured directly, post-repoint: 5 failures, in 4 modules, ALL the same root
cause - `telemac_rain_on_grid` is not in `TOOL_REGISTRY` (by design, per
"What did NOT land" above) while a handful of pre-existing, untouched tests
still expect it there. Nothing else in the offline suite failed; the two
reach templates' own test modules (`test_run_river_dye_scenario.py`,
`test_telemac_do_sag.py`, `test_workflow_skeleton.py`,
`test_resolution_sensitivity.py`, `test_rerun_with_overrides.py`,
`test_catalog_surfacing.py`, `test_mesh_declaration_travel.py`,
`tests/reach_chain.py` - 183 tests) are green, and the three modules the
pre-repoint note flagged as UNCOLLECTABLE (`test_mesh_declaration_travel.py`,
`test_telemac_event_time.py`, `test_telemac_rain_forcing.py`) all collect
clean now that neither reach template names the purged `corridor_tin`.

| module | count | why |
|---|---|---|
| `test_door_dissolution.py` | 2 | `test_all_templates_registered_and_callable` (the roster) and `test_every_template_surfaces_in_top8` (retrieval surfacing) both still list `telemac_rain_on_grid` in `EXPECTED_TEMPLATES` |
| `test_telemac_rain_on_grid_template.py` | 1 | `test_registered_as_telemac_template` asserts the name is in `TOOL_REGISTRY` |
| `test_tool_retrieval.py` | 1 | `test_no_dead_corpus_keys` - `tool_query_corpus.yaml` still carries `telemac_rain_on_grid`'s corpus queries for a name not currently registered |
| `test_template_hygiene.py` | 1 | `test_hygiene_gate_covers_all_templates` - the hygiene gate's live template roster is one short of `EXPECTED_TEMPLATES` |

Restoring the one commented-out import line in `trid3nt_server/tools/__init__.py`
clears all 5 - each test's assertion is that the honest-absence state matches
the registry, corpus and hygiene gate consistently, which it does; they fail
only because `telemac_rain_on_grid` is deliberately parked rather than
deleted.
