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

- `telemac_rain_on_grid`: `basin = tool("delineate_watershed", ...)`
  -> `sized = tool("combine", polygon=basin, lines=rivers)`
  -> `MESH = tool.build_mesh(mesher="om2d", extent=Ref("sized"), ...)`. The
  template registers again once the worker side lands (below).
- `telemac_river_dye` / `telemac_do_sag`:
  `centerline = tool("fetch_nhdplus_nldi_navigate", ...)` ->
  `ends = tool("endpoints", line=centerline)` ->
  `banks = tool("fetch_nhd_area_water", ...)` ->
  `reach_polygon = tool("section", polygon=banks,
  between=Ref("ends.between"))` -> `MESH = tool.build_mesh(mesher="om2d",
  extent=Ref("reach_polygon"), refine={"edge_length": P.mesh_resolution_m})`.
  The `between` cut keeps the two transect faces the inflow and the outflow
  are prescribed on. Both templates are REGISTERED
  (`trid3nt_server/tools/__init__.py`) and import clean.

**Two generic geometry tools** back those chains: `combine` (a polygon plus
the lines riding inside it -> one geometry document) and `endpoints` (a line
-> its two end points). Both are registered tools, so `tool("combine",
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

RESOLVED. `telemac_rain_on_grid` is now DECLARED PARKED -
`register_workflow(parked="awaiting the worker-unification port of its mesh
step")` - so the module imports with the rest of the tree, its plan validates,
the tool never registers and invoking it refuses `TEMPLATE_PARKED` naming the
reason. The roster, hygiene and corpus checks read that state (see
`PARKED_TEMPLATES` in `tests/test_door_dissolution.py`) instead of import order,
and all five formerly-order-dependent tests are green. Unparking is deleting the
one `parked=` keyword on the template's `register_workflow` call.

## Stage 1 - the server authoring substrate (lands DARK, 2026-08-30)

Nothing flips in this stage: every seam below is built, tested offline and
called by nobody. Stage 3 wires it, Stage 2 rewrites the worker that reads it.

**Role-keyed boundary sets, and the numliq order MEASURED.** The pair writer
took one `open_nodes` list and wrote every named node as a prescribed-elevation
boundary. It now takes `roles={role: [node, ...]}` against one table -
`inflow` prescribes velocity and tracer with a free depth, `outflow` and `open`
prescribe a water level with a free velocity - and the driver reports
`liquid_boundary_roles`: the role of each liquid boundary, in TELEMAC's OWN
numbering, joined from the `NUMLIQ` column `Conlim.set_numliq` wrote onto the
`.cli` it had just written.

That measurement is what retires the two-pass probe-solve. Probed through the
image on a straight 12x4 channel strip with the west cap declared `inflow` and
the east cap `outflow`, the measured order comes back **`["outflow",
"inflow"]`** - the contour walk does not start at the inflow. A deck authored
inflow-first would put the discharge on the downstream cap and drive the reach
backwards, and nothing in the run would say so.

**The topology bundle.** `workflows/mesh/topology.py` (60 lines) writes and
reads the record `MeshArtifact.topology_uri` points at: the role node sets and
the measured liquid-boundary order, as JSON, carrying NO geometry (the nodes,
cells and bed are the SELAFIN's, and a second copy could disagree with the
first). This is the seam whose absence raises `TELEMAC_MESH_NOT_ACCEPTED`: the
purged `corridor_tin` producer wrote a 20-array npz bundle, and what a deck
author actually reads out of it is these two facts.

**The fitted bed.** `fit_downstream_bed` in `mesh/shared/nodes.py` is the
gentle-slope half of the deleted worker's `fetch_dem_bed`; the sampling half was
already server-side as `sample_raster_at_nodes`. It projects the nodes onto the
centerline, fits `z ~ z0 - slope*s` over what sampled, clamps the slope into the
stated band and lays a clean plane - and reports `measured_slope` beside
`enforced_slope`, so a bed that was overruled says so instead of presenting the
overrule as the DEM.

**`steps/author.py` (~700 lines).** The `.cas` writers, harvested from the
attic'd worker payloads as the answer key: the TELEMAC-2D reach deck with its
sources pulse, WAQTEL decay (process 17) and the oxygen sag (process 2), GAIA in
its three shapes (supply-limited suspension / erodible bed / graded mixture),
NESTOR's action, polygon and surface-reference files, the oil steering and its
per-run `oil_flot.f`, and the rain-on-grid deck with its curve-number scatter,
friction pair and block hyetograph. Two things changed from the answer key:

- the deck is read through a `_Sheet` view over the manifest mapping, and the
  physics DEFAULTS the worker's `ReachConfig` carried now sit server-side in one
  `_DEFAULTS` table - the worker is the engine room and owns no opinions;
- a NESTOR box with no explicit polygon needs a MEASURED channel width, passed
  in. `channel_width_m` died with the superseded node estimate (P3), so the
  author refuses `TELEMAC_DREDGE_ZONE_UNMEASURED` rather than invent one. WHERE
  Stage 3 sources that width - the mapped-banks polygon is the obvious candidate
  - is the one open thread this stage leaves.

`oil_templates/oil_flot_template.f` is COPIED beside the author; the worker copy
stays until Stage 2 deletes the branch that reads it.

**ONE manifest writer.** `stage_open_water_manifest` becomes
`stage_telemac_manifest` and gains `case=` - `{module, steering, user_fortran?,
results, family, echo}`, built by `case_section`. `echo` is what the server
already knows and the worker cannot learn from the files it is handed; the
worker copies it into its metrics verbatim rather than re-deriving a second
answer. `deck.py::stage_manifest` and `rain_on_grid.py::_stage_inputs` now
decide only their own outputs list and delegate the document.

**`run_telemac.py` 569 -> 187.** Five copies of one classifier and one spec
factory become `_classify(label)` and `make_spec(solver, stream_prefix)` over a
`{solver: stream prefix}` table; the five per-leg metric-key tuples become one
`_COMPLETION_METRIC_KEYS` set (a key a leg never writes was already filtered out
by `if k in metrics`, so per-leg lists only duplicated that filtering). The five
solver NAMES stay: a harbour agitation field and a river-dye plume are not the
same kind of run and must not share a row identity in a listing.

## Stage 3 - the server flip, and the seam that blocks its core (2026-08-31)

What landed, and what did not.

**The server chain owns the reach refusals.** `steps/solve.py` no longer
re-raises worker gates: the banks fetch, the measured-coverage check and the
section cut all run before a manifest is staged, so a geometry refusal fires on
its own cause. `TelemacReachDegenerateError` dies with them, and
`channel_width_m` leaves the tree root-and-branch. `download_result_selafin`
stops re-reading `utm_epsg`: the zone is the server's own measurement, echoed
through `case.echo`, and already on the solve result.

**The class readers are ported.** `steps/run_reads.py` reads what the worker
used to derive in-container, off the artifacts the supervisor uploaded: GAIA's
`FINAL MASS-BALANCE OF SEDIMENTS` closure out of `full_listing.log`, the
injected mass and deposit fraction off the deck's own pulse, the surface-D50
spread off `gaia_river.slf`, and the floating slick out of the raw
`drogues.txt`. `products.py` builds `particles.json` and `slick.geojson` and
UPLOADS them before it emits the handle, which retires the dangling-handle
guard.

**The NESTOR dredge zone auto-fills** (IDEAS 2026-08-31 ruling 2). The dig field
is the cross-channel box at the dig station intersected with the reach polygon
offset inward by the declared `dredge_bank_offset_m` (5 m, labeled, user-editable,
`door=SCENARIO`). ONE mechanism, two behaviours: the setback keeps the cut off
the bank toe, and a stretch narrower than twice it has no inside left and
excludes itself. A supplied polygon wins and is validated CONTAINED in the
water. Journaled through `write_nestor_decks`.

**The rain-on-grid hyetograph names the baked fortran** (ruling 1).
`RAINDEF3_USER_FORTRAN` is the one spelling of the path the Dockerfile bakes the
RAINDEF=3 copy of `runoff_scs_cn.f` to; a constant-rain run names nothing and
stages nothing.

**`tomawac_wave_field` and `coastal_tidal_surge` are DECLARED PARKED**, per the
fork ruling and the `telemac_rain_on_grid` precedent.

**The worker fix fold.** `_run_child` regains the solve time bound
(`TRID3NT_TELEMAC_SOLVE_TIMEOUT`, 86400 s): on expiry the child is killed and
the metrics are written with `TELEMAC_SOLVE_TIMEOUT` and the listing tail. The
strict gate refuses a case declaring an EMPTY results list, because on an empty
list the ruled success convention reduces to the exit code alone.

### The seam that blocked the flip is CLOSED (2026-08-31 FLIP rulings)

Both facts the deck flip needed now have a producer, and both are DECLARED on
the mesh ask rather than derived by a server step between the mesh and the deck.

**Boundary roles.** `build_mesh(mesher="om2d")` gains `boundaries={role: face}`.
The faces are the chain's own transects: `section(between=...)` cuts the mapped
banks square at each end and now HANDS THOSE CUTS OVER as `face_start` /
`face_end`, so the reach declares
`boundaries={"inflow": Ref("reach_polygon.face_start"), "outflow":
Ref("reach_polygon.face_end")}`. Every boundary node takes the role of the face
it is nearest to, within the mesh's own mean boundary edge; a bank node is
nearer neither and is written as solid wall. `mesh/topology.py::write_topology`
gains its first caller - `om2d::_emit_formats` writes the bundle whenever any
role landed - so `MeshArtifact.topology_uri` carries the role sets and the
MEASURED numliq order the deck is authored against. The coastal gate-edit
`"open"` path is unchanged: its designation still comes from oceanmesh's own
bed-threshold section walk and simply joins the same role table.

**The bed.** The reach `DATA` body declares `bed = tool("fetch_copernicus_dem",
bbox=Ref("window.bbox"), px_per_deg=3600.0)` and the `MESH` ask consumes it as
`bed={"raster": DATA.bed, "downstream_along": DATA.centerline}` - so om2d's
implicit `fetch_topobathy` default never fires for a reach, and
`fit_downstream_bed` applies at that one seam: the sampled surface is laid down
as a monotone downstream plane along the declared centerline, held inside a
stated slope band, and the measured-vs-enforced slope rides in the probes. The
duplicate `resolve_reach_river` bed staging is gone (see DELETION_LEDGER).

**The flip itself.** `steps/deck.py` authors through `author.py` against the
measured order, uploads the decks it wrote, and stages a `case` section -
`{module, steering, results, family, echo}` - beside the mesh pair under the
deck's own `river.slf` / `river.cli` names. The npz stops travelling,
`steps/solve.py` no longer stages a `reach` section the worker has no branch
for, and `_COMPLETION_METRIC_KEYS` carries `module` / `family` / `bed_source`
and ONE `bbox` spelling.

## `telemac_rain_on_grid` stays PARKED, and why

The same mechanism does NOT reach it, so it is not half-wired to look as though
it does. Two things are missing rather than one:

- its outlet is a POUR POINT, not a cut transect. The role matcher is a
  face-matcher by construction - a role is prescribed ACROSS a transect - and a
  catchment boundary has no face at the outlet for it to match against.
- the outlet HYDROGRAPH is the answer, and the worker wrote it. With the worker
  reduced to the engine room there is no server-side reader that integrates the
  discharge through the outlet nodes off the result SELAFIN, so unparking now
  would register a template whose ANSWER field nothing fills.

`register_workflow(parked=...)` states both. `steps/rain_on_grid.py` still names
a `reach` manifest section that no worker branch dispatches; it is left standing
rather than repointed, because a `case` with no authored steering file would be
the half-wiring this note exists to avoid.
