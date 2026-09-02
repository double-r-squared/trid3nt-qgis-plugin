# Deletion Ledger

Every deletion candidate is REGISTERED here at decision time and stays
until DELETED (with the commit hash) - never silently dropped (NATE
2026-07-31). Rules:
1. A candidate enters with its CONDITION - the specific thing that makes
   it redundant. "Someday" is not a condition.
2. Every wave close-out checks this ledger: conditions newly met ->
   deletion executes in that wave or the next hygiene batch.
3. Status flow: QUEUED -> CONDITION-MET -> DELETED(commit). Rejected
   candidates move to the bottom with the reason (decision record).
4. Standing ratchet (hooks): a hook pattern seen twice = directive
   candidate; three times = mandatory promotion review - promoted
   directives DELETE their hooks (entries added per occurrence).

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| `MeshField` + every mesher's `_FIELDS` | trid3nt_server/workflows/mesh/meshers/ | THE SIGNATURE IS THE SCHEMA: an op's kwargs bind to the real callable's own signature, so a hand-maintained field table is a second declaration of what a library already states. Condition: build_mesh takes three agnostic params plus ops, and validation is `inspect.signature(...).bind_partial`. | DELETED (rung-3 recipe wave). CONDITION MET: `bind_ops` binds every primitive against its real signature and journals a note for a name whose callable lives in the image, where the driver's `_bind` binds it instead; `MeshRecipe` carries `mesher/kind/extent/resolution_m/ops` and nothing else. reopen: never. | mesh-recipe spec rev 2 sect 4, 8 |
| `EditAction` + `MeshDeclaration.edit` + the `DeclaredEdit` chain | trid3nt_server/workflows/mesh/ | an edit is a RECIPE DIFF, not a named action with its own plumbing: append, alter-by-index, remove. Condition: the session holds one recipe and every change regenerates wholesale. | DELETED (rung-3 recipe wave). CONDITION MET: `MeshSession.append_op/alter_op/remove_op/reset` are the whole edit surface and `mesh_op` is the one tool that reaches them; the gate mounts three loop tools and zero per-mesher ones. reopen: never. | mesh-recipe spec rev 2 sect 3, 8 |
| `refine={}` + `bed=` + `boundaries=` params, and `checked_refine` | trid3nt_server/workflows/mesh/, the TELEMAC templates | our invented words and engine vocabulary inside a generalization. Condition: `resolution_m` is the one agnostic size word and the rest are ops. | DELETED (rung-3 recipe wave). CONDITION MET: `mesh_op("enforce_mesh_gradation", ...)`, `mesh_op("set_bed", ...)` and `mesh_op("set_boundary_roles", ...)` carry what the three params carried; the coarsest edge defaults to 10x `resolution_m` and any op may state its own. reopen: never. | mesh-recipe spec rev 2 sect 2, 8 |
| `fit_downstream_bed` + `_along_channel_distance` (+ their tests) | trid3nt_server/workflows/mesh/shared/nodes.py, tests/test_mesh_topology_and_bed.py, tests/test_spill_fraction_chainage.py | scar tissue over the WRONG DATA CLASS: it fitted a monotone plane because a surface DEM was standing in for topobathy. Condition met BY DESIGN by the correct-data-class law - a bed is topobathy, and a DEM is a declared visible substitution rather than something a shim compensates for. | DELETED (rung-3 recipe wave). CONSEQUENCE, stated: the reach canaries now carry the raw sampled surface, which runs uphill between adjacent nodes and ponds. The replacement is the CHARTERED bathymetry item (topobathy coverage per water-body class, surveyed cross-sections as a supply path, synthetic-channel methods only ever as a declared PRODUCER row), not a re-landing of this. | mesh-recipe spec rev 2 sect 5, 8 |
| `contained_extent` + `staged_coverage` + `RESTAGE_TOOL` | trid3nt_server/workflows/mesh/meshers/__init__.py | they existed to police `reg_grid`'s `set_extent` EDIT ACTION. Condition: the extent is a recipe param, so changing it is a recipe diff and a regen, and the DATA rows the recipe names are what state their own coverage. | DELETED (rung-3 recipe wave). CONDITION MET: `set_extent` and `set_resolution` died with the action list; `MeshRecipe.with_params` is the one way an agnostic param moves. QUEUED as a re-landing candidate IF a measured case shows a recipe param change silently meshing ground nothing was fetched for. | mesh-recipe spec rev 2 sect 8 |
| om2d `set_boundary` (the compass `side` selection) + `_open_sections` | trid3nt_server/workflows/mesh/meshers/om2d.py | our invented vocabulary on top of a library function that already answers the question. Condition: `identify_ocean_boundary_sections` is a POST library op and EVERY section it identifies is open. | DELETED (rung-3 recipe wave). CONDITION MET: the driver returns every identified section as a run of nodes and the emit stage makes them the `open` role; picking one by compass silently numbered a multi-mouth estuary as single-mouth. reopen: never - a selection among sections is a role edit, not a compass word. | mesh-recipe spec rev 2 sect 2, 3 |
| om2d `refine_region` (the named edit action) | trid3nt_server/workflows/mesh/meshers/om2d.py | superseded by the library's OWN sizing functions under their own names. Condition: `distance_sizing_from_line_function` / `distance_sizing_from_point_function` are declarable ops. | KEPT AS A PRIMITIVE (rung-3 recipe wave), renamed `set_region_size`: the library refines TOWARD a geometry and has no function that writes a target edge INSIDE a drawn polygon, so the capability is not covered. It is now an om2d-owned PRE op rather than a gate-only edit action. | mesh-recipe spec rev 2 sect 7 |
| `mesh_restart` (the loop tool name) | trid3nt_server/workflows/mesh/gate.py | "restart" described a truncation of a chain that no longer exists. Condition: reset-to-declaration replaces prefix truncation. | DELETED (rung-3 recipe wave), replaced by `mesh_reset`. CONDITION MET: the session holds one recipe and `reset()` puts it back to the declaration. | mesh-recipe spec rev 2 sect 1 |
| `MeshSpec` + `MeshDeclaration` + `validate_spec` / `bind_edit_inputs` / `validate_edit` / `declaration_plan_value` / `declaration_from_plan_value` (the recipe-vs-declaration duality) | trid3nt_server/workflows/mesh/tool.py, session.py | a mesh was TWO objects - a frozen spec value and a live edit chain beside it - so what defined the mesh depended on which one you read, and a session's `spec` + `_chain` + `_declared` were three spellings of one program. Condition: one recipe object, and the declaration is a recipe literal. | DELETED (rung-3 recipe wave). CONDITION MET: `MeshRecipe` is the only mesh-defining object; `recipe_plan_value` / `recipe_from_plan_value` are the one round trip, `MeshSession.recipe` is the program and `MeshSession.declared` is only what `reset()` restores. Grep of trid3nt_server/, plugin/, contracts/, tests/, scripts/, workers/ for all seven names: zero. reopen: never. | mesh-recipe spec rev 2 sect 1, 8 |
| The prefix-keyed SNAPSHOT CACHE (every realized mesh held in the session, keyed by recipe-prefix hash, for instant undo/restart) | trid3nt_server/workflows/mesh/session.py | state is the RECIPE and every change regenerates wholesale, so a cache of realized meshes is a second answer to "what is this mesh" that has to be kept in step with the first. Condition: regen measured cheap on the canaries. | CHOPPED AS A CANDIDATE (rung-3 recipe wave) - and NEVER LANDED: the ruling of 2026-08-27 was not implemented, and the pre-recipe session mutated its mesh in place with no cache at all, so this closes a candidate rather than deleting code. MEASURED, wholesale `_build` of an already-staged recipe, 5 repeats each: coarse reach (river_dye Eel River canary, om2d, 12 m, 6 ops -> 1,933 nodes / 3,388 elements) median 3.5 s, range 3.5-4.7 s; basin canary (rain_on_grid, om2d, 40 m, 8 ops incl. distance sizing + gradation -> 6,079 nodes / 11,928 elements) median 7.4 s, range 7.0-9.0 s. Cheap as ruled. The one memo that stays is `MeshSession._display_face`, which memoizes a WRITE (one file + one object-store put per build, asked for by every present, the snapshot and twice by accept) and is dropped on every regen and on an adopted layer. reopen: only if a measured mesh puts regen past a gate cycle. | mesh-recipe spec rev 2 sect 8 |
| `_RIM_TOLERANCE` (the module constant the rim's measured band was read from) | trid3nt_server/workflows/mesh/meshers/drivers/om2d_driver.py | the spec asks for a rim honored within a DECLARED tolerance, and a band nobody declares is not declared. Condition: the band becomes a visible kwarg with a labeled default on the op that sizes the rim. | DELETED (rung-3 close). CONDITION MET: `set_rim_size(tolerance=2.0)` states it, `_Build.rim_tolerance` carries what the ask declared, and the rim probe reads it from there. CONSEQUENCE, stated: a recipe that declares no rim op now gets the rim's measured spread with NO `within_tolerance` verdict - an unsized rim declared no band to be held to. Every undeclared om2d ask carries a rim op, so this surfaces only on a declared recipe that leaves `set_rim_size` out. | mesh-recipe spec rev 2 sect 10; IDEAS 2026-09-01 FRAGILITY-STAGE JUDGMENTS RULED |
| `MeshPolicy` | trid3nt_server/workflows/lib/slots.py | the universal SIZING ask (`resolution` + `target_edge_m`) is two spec fields on whichever mesher reads them, checked at the router against that mesher's own declaration. Condition: every template declares its mesh through `tool.build_mesh` and the facade reads the deck keywords off the declaration. | DELETED (mesh wave, slice 5). CONDITION MET: all seven TELEMAC templates declare `MESH = tool.build_mesh(...)`; `corridor_tin.refine`, `reg_grid.resolution_m` and `watershed.min_edge_length_m` carry the sizing, and the authored deck ask is byte-identical to the one the policy produced. reopen: never - a fixed-field universal ask cannot say what a mesher of a different shape needs. | mesh wave slice 5 |
| `CorridorPolicy` | trid3nt_server/workflows/telemac/workflow.py | a corridor's extent, width and bank source are the `corridor_tin` MESHER's declared fields, validated at the router, not a value object the facade owns. Condition: the corridor mesher declares `extent_km` / `width_m` / `banks` and the reach templates declare them there. | DELETED (mesh wave, slice 5). CONDITION MET: `corridor_tin._FIELDS` declares all three; `river_dye` and `do_sag` pass them through the declaration and the deck still receives `reach_length_km` / `channel_width_m` / `bank_source`. reopen: never. | mesh wave slice 5 |
| `CatchmentPolicy` | trid3nt_server/workflows/telemac/steps/rain_on_grid.py | the edge band, the gradation and the outlet-snap window are the `watershed` MESHER's declared fields. Condition: the watershed mesher declares `max_iter` and `snap_search_cells` beside the band it already declared, and `Catchment.mesh` reads the declaration. | DELETED (mesh wave, slice 5). CONDITION MET: `watershed._FIELDS` declares all five and the strategy receives all five; `Catchment.mesh(mesh=MESH, ...)` unpacks them and the mesh step's kwargs are byte-identical to the ones `policy.as_kwargs()` produced. reopen: never. | mesh wave slice 5 |
| `MeshHandle` + `EngineOps.build_mesh` + `TelemacWorkflow.build_mesh` | trid3nt_server/workflows/telemac/workflow.py, trid3nt_server/workflows/lib/workflow.py | the handle existed to carry a domain plus two policies from a facade operation into `author`; with the ask a declaration block, `author` takes it directly and the acquired domain a deck consumes is the PROCESS's (`_Process.domain_ref`), which never varied by template. Condition: the policy classes are gone and the deck ask is unchanged. | DELETED (mesh wave, slice 5). CONDITION MET: `EngineOps.MUST_FILL` is the four operations; the seven templates' full plans (every step, every kwarg) diff byte-identical across the migration. reopen: never - `tool.build_mesh` is the one mesh entry point and a facade operation of the same name is a routing ambiguity. | mesh wave slice 5 |
| `telemac/steps/mesh_preview.py` (`preview_telemac_mesh`) | trid3nt_server/workflows/telemac/steps/mesh_preview.py | the fast mesh-only preview existed to fill one template's approve-mesh card. The reach family opens a MeshSession over the `corridor_tin` declaration now, and the standard mesh gate presents the SAME build with probes, an editable MDAL layer and every registered edit action. CONDITION: the reach plan carries a `mesh` stage whose artifact the deck consumes. | DELETED (panel lens-1 remediation, F3). CONDITION MET: `ReachMesh.corridor` is a declared step in `river_dye` and `do_sag`; its accepted topology is staged for the solve and the deck is byte-identical. Its input-emission allowlist row went with it (the gate's own row covers the presentation). reopen: never - two mesh previews is two things to keep in step. | panel lens-1 F3 |
| `_build_telemac_mesh_envelope` + `estimate_telemac_mesh` + `pin_telemac_mesh` | trid3nt_server/gates/cards/solver_confirm.py (+ the `gates/cards/__init__` export) | the template-specific approve-mesh card: preview + an edge-length lever + the release-point click, all subsumed by the standard mesh gate. CONDITION: `telemac_river_dye` declares no solver `GateSpec` and the mesh gate presents its corridor. | DELETED (panel lens-1 remediation, F3). CONDITION MET: the GateSpec block and its three `_EXTRA_ARGS` tail entries are gone; `_EXPECTED_SOLVER` no longer lists the template and a new guard pins that it declares none. reopen: never. | panel lens-1 F3 |
| `_release_seeds_reach` / `_seed_release_lon` / `_seed_release_lat` (the approve-mesh decision tail) | trid3nt_server/workflows/telemac/river_dye/river_dye.py `_EXTRA_ARGS`, `river_dye/coercions.py` | the tri-state existed because a gate could deliver a release point AFTER the reach was chosen. Coercions run on the wire args, before any door and so before any gate, so the reach seed can only be the point the CALL carried and a drawn point reaches it by no path. CONDITION: the decoupling is structural rather than threaded. | DELETED (panel lens-1 remediation, F3). BEHAVIOUR RE-HOMED, not dropped: `release_points` seats the call's release as the reach seed and the DrawGate re-seats `release_coords` alone. | panel lens-1 F3 |
| The `fort.14` emission on an om2d build | trid3nt_server/workflows/mesh/meshers/om2d.py `_emit_formats` | no engine here reads an ADCIRC mesh: the SWAN worker is regular-grid only (`CGRID REGULAR` + `bottom.bot`), which the compat gate already states. A build wrote the file and nothing opened it. CONDITION: none - the writer stays for the day a reader arrives. | DELETED (panel lens-1 remediation, item c). The shared `mesh_formats.write_fort14` is untouched and still tested; only the call on the build path is gone, and spec 2.6 states SWAN as regular-grid only. | panel lens-1 (c) |
| `SOLVE_TIME_BUDGET_S` | trid3nt_server/workflows/telemac/steps/reach.py | its only reader was the preview's `_honest_edge_length` rebuild-once loop, which went with `mesh_preview.py`. A constant nothing reads is a budget nobody is held to. | DELETED (panel lens-1 remediation, F3). | panel lens-1 F3 |
| `mesh_formats._open_nodes_on_side` (the coordinate-percentile open-boundary cut) + `write_fort14(open_boundary_side=...)` | scripts/sandbox/oceanmesh/mesh_formats.py | it produced a NON-CONTIGUOUS open stretch (land nodes declared inside it), and the product path now takes contiguous sections from `om.identify_ocean_boundary_sections` instead. It survives only because three sandbox proof drivers (`build_coastal_mesh.py`, `build_coastal_water_edge_mesh.py`, `build_watershed_mesh.py`) still pass `open_boundary_side=`. CONDITION: those three are chopped, or moved onto `open_sections=`. | QUEUED (product no longer consumes it as of the meshers-review remediation) | meshers review R5 |
| `tin_to_hgrid(open_boundary_side=...)` (the same percentile cut inside the one gr3 writer) | workers/schism/schism_gr3.py | `open_sections=` writes one open block per contiguous stretch and splits the land boundary between them; the percentile path stays only for the sandbox proofs and `deck_authoring`'s existing callers. CONDITION: every caller passes `open_sections=`. | QUEUED | meshers review R5 |
| The per-contour IPOBO reimplementation (`_ipobo`) in the telapy driver | trid3nt_server/workflows/mesh/meshers/drivers/telapy_mesh_driver.py | it restarted its position counter per contour, so a two-contour mesh wrote duplicate IPOBO values where TELEMAC requires a permutation of 1..NPTFR (measured: 120 nonzero / 80 distinct on a square-with-island synthetic). | DELETED (meshers-review remediation). Replaced by `_boundary`, one continuous count along `pretel.get_ipobo`'s own contour walk - measured 120 nonzero / 120 distinct on the same mesh. | meshers review R1 |
| The inlined gradation envelope and the inlined set-difference in the om2d driver | trid3nt_server/workflows/mesh/meshers/drivers/om2d_driver.py | `om.enforce_mesh_gradation` and `om.Difference` are the library's own, and both were reimplemented inline (`min_deg + gradation*near`, `np.maximum(base(x), -holes.signed(x))`). | DELETED (meshers-review remediation). The obstacle band seeds `min_deg` and the library grades it; `_Holes` is an `om.Domain` and `om.Difference([sdf, holes])` subtracts it. | meshers review R3, R4 |
| `session.write_2dm` (the mesh session's SMS `.2dm` writer) | trid3nt_server/workflows/mesh/session.py | it is the second `.2dm` writer in the tree - `generate_mesh._write_2dm` is the first, and that one writes triangles only, which a quad lattice cannot use. One of the two dies when the display face moves to `emission/mesh_display.py` and both callers read the quad-capable one. | DELETED (mesh wave slice 3). CONDITION MET: `emission/mesh_display.py` holds the one writer with both entry points (`write_2dm` for a built mesh, `write_2dm_arrays` for the raw arrays), the session imports it, and the cell-arity check that guarded only this copy now guards both. See the full row at the foot of this file. | mesh wave slice 3 (the display-face slice owned the merge) |
| The `.byo()` modifier name, `AuthoredProducer.byo_uri` / `byo_validate`, `DataDecl.is_byo`, `interpret(byo=...)` and `ByoCoverageError` / `BYO_COVERAGE_MISMATCH` | trid3nt_server/workflows/lib + the six templates + docs | one word for one idea: `user_supplied` is already the ladder rung's name and "supplied on this invocation" is already the provenance vocabulary, so a third spelling of the same thing on the modifier is a name the reader has to translate. | DELETED (wave A, NATE naming ruling). CONDITION MET: renamed to `.supplied()` / `supplied_uri` / `supplied_validate` / `is_supplied` / `interpret(supplied=...)` / `SuppliedCoverageError` / `SUPPLIED_COVERAGE_MISMATCH` across the library, the six templates, the tests and the design doc, with NO alias and no deprecation shim - `.byo` does not exist. A producer-less slot now reads `Data("structure").supplied(geometry="polyline").optional()`, so the slot declares the SHAPE it accepts (the only thing a template can honestly say about a layer whose source it deliberately does not name) and the geometry vocabulary is checked at declaration. | NATE naming ruling |
| The p-view READ-RECORDING machinery: `ResolvedParams._reads` / `freeze_reads` / `concrete_reads` / `_record_read`, `ParamValues(record=...)`, and `ResolvedParams.get` | trid3nt_server/workflows/lib/params.py | the plan becomes STATIC - `plan(ops)` reads no concrete value - so there are no construction-time reads left to record, and the validator check the record existed for (`_check_revisable_branches`) has nothing to check. | DELETED (wave A, static-plan rule). CONDITION MET: `plan(ops)` takes no sheet; every read is a late-bound `P.<name>` / `D.<name>` / `Ref`. The concrete read is now `ResolvedParams.value_of`, used by the interpreter's binder and by code running WITH a sheet (the four pre-skeleton templates, the resolver's re-seat comparison). `p.get` in a plan now raises `ParamNotResolved` naming the declared params, which is the honest answer: there is nothing to read at construction time. | decision 6, static plan |
| `validate._check_revisable_branches` (the refusal of a plan that declares a FormGate AND branches on a form-revisable param) | trid3nt_server/workflows/lib/validate.py | `When` is evaluated by the INTERPRETER after the gates, so a branch reading a value the gate revised is the intended behaviour rather than a contradiction. | DELETED (wave A, static-plan rule). CONDITION MET: the interpreter binds a `When` condition against the current sheet at the moment the branch is reached. RE-POINTED, not merely removed: `_check_when_conditions` now reads the When nodes and refuses a condition that names nothing - an undeclared param, an undeclared Data, or a step not visible on that branch. | decision 6, static plan |
| `Plan.flat()` and `When.taken` | trid3nt_server/workflows/lib/plan.py | branch selection moves from plan-construction time to interpretation time, so a plan value cannot answer "which steps run". | DELETED (wave A, static-plan rule). CONDITION MET: `Plan.declared()` returns every step, guarded or not; the interpreter numbers all of them and skips the ones whose guard did not fire. | decision 6, static plan |
| `workflow.DataRefs` + `UndeclaredDataError` (the per-workflow `d` object handed to `plan`) | trid3nt_server/workflows/lib/workflow.py | `D` becomes a module-level namespace so a binding block can sit above `plan()`, which means the undeclared-name check moves from attribute access to registration. | DELETED (wave A, static-plan rule). CONDITION MET: `D.<name>` yields a `DataRef` carrying its `file.py:line` origin, and `validate._resolve_root` refuses an undeclared one at registration with that origin and the declared Data list. | decision 6, static plan |
| The EAGER independent-Data batch: `interpreter._produce_independent_data` + `_eager_data_index` | trid3nt_server/workflows/lib/interpreter.py | producers become demand-pulled, so a `When`-guarded consumer whose branch does not fire costs no fetch - which an eager batch cannot honour, because it runs before any branch is decided. | DELETED (wave A, lazy producers). CONDITION MET: `_produce` runs on first `Ref` through `_deref`; the batch is gone. TRADE, stated: independent producers no longer run concurrently. The parallelism was worth less than the guarantee, and a producer set worth parallelising again would be declared as such rather than inferred from "Refs no other Data". | decision 6, mesh-wave charter (1) |
| The `.render` verb + `RenderSpec` + `interpreter._run_render` | trid3nt_server/workflows/lib | renders are the plugin's job and workflows describe PRODUCTS, so a style becomes a declaration MODIFIER over the automatic emission seam rather than a step. | DELETED (wave A, emission/styles chapter). CONDITION MET: `.style(preset=|colormap=|policy=|range=|transform=|clip=)` replaces it, resolving against the style contract; `interpreter._run_style` re-emits the DISPLAY FACE through `emission/restyle.apply_style`. `.render` had never shipped in a live template (dormant lib machinery plus one design-doc example), so nothing in the fleet changed shape. The honesty floor survives: `RenderSourceMissingError` now fires when a step declares a style and produced no layer to paint. | STYLE MODIFIER GRAMMAR / PRECISION rulings |
| `emission/quantity_styles.py` in full (`QUANTITY_STYLE_PRESETS`, `MESH_PRESETS`, `NEUTRAL_FALLBACK_PRESET`, `resolve_style_preset`, the fallback counter) | trid3nt_server/emission | the quantity -> preset table and the preset table live in ONE contract file, so the mirror between them cannot be opened. | DELETED (wave A, emission/styles chapter). CONDITION MET: `contracts/trid3nt_contracts/styles.yaml` holds `presets` and `quantity_defaults` in one file; `emission/styles.py` is the one resolver and carries `resolve_style_preset`, the family separator and the fallback counter. `tests/test_style_contract.py` pins that every declared quantity maps to a preset the SAME file declares - the mirror check became a self-consistency check. | REUSE-SWEEP / emission chapter |
| `publish._QGIS_STYLE_REGISTRY` + `_QGIS_STYLE_SAFE_DEFAULT` + `_registry_style_params` + `_band1_percentile_rescale` + `_sediment_yield_log_style_params` | trid3nt_server/emission/publish.py | the preset table becomes a declared contract and the scale decision moves to one resolver. | DELETED (wave A, emission/styles chapter). CONDITION MET: the 59 preset rows plus the sediment log-class table are declared in `styles.yaml`; `emission/styles.resolve_style` makes the scale decision and `band_range_reader` / `fixed_range_reader` supply the run's own range. `publish._resolve_qgis_style_params` keeps ONLY the three raster guards (embedded palette, RGB(A) composite, terrain token) because those are facts about the file, not about the style. | emission chapter |
| `OutputQuantitySpec.style_preset` (the field) + its 24 per-spec values | contracts/trid3nt_contracts/output_quantities.py | publishers declare QUANTITIES and the contract owns quantity -> preset, so a spec naming a colormap is a third copy of a decision that has one home. | DELETED (wave A, emission/styles chapter). CONDITION MET: every `quantity_id` is a row in `styles.yaml`'s `quantity_defaults`, and `workflows/shared/publish_quantities` resolves the preset from `spec.quantity_id` through the one resolver. | emission chapter |
| `persistence/case_lifecycle.py` in full (`ensure_case_qgs`, `CaseLifecycleError`, the `PER_CASE_QGS_UNAVAILABLE` code) + `tests/test_case_lifecycle.py` | trid3nt_server/persistence + tests | per-Case `.qgs` provisioning is not implemented and has no production caller, so the module is a lazy-init policy for a lifecycle nothing runs. | DELETED (wave A, placement debts). CONDITION MET: grep-to-zero on production callers before the delete - the only non-definition references were one docstring mention in `server/turn/cases.py` and the module's own four unit tests. `CaseSummary.qgs_project_uri` STAYS as inert data: a case handed an explicit project URI keeps it, and nothing provisions one. | placement debts |
| `tools/processing/_gdal_runner.translate_to_cog` (the COG encoder living inside the terrain-tool runner) | trid3nt_server/tools/processing | emission needs it to publish a renderable raster, and reaching backwards from `emission/publish.py` into a terrain TOOL to get it is the wrong-direction import. | DELETED (wave A, placement debts). CONDITION MET: moved verbatim to `trid3nt_server/emission/cog.py`; the six processing tools plus `emission/publish.py` import it from there. `_gdal_runner` keeps what it is actually for - gdaldem/gdal_contour binary resolution, the PROJ env wiring, the subprocess call and the raster-bytes reader. | placement debts |
| `compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES` (the literal table) + `hex_to_rgba` | trid3nt_server/tools/processing/compute_sediment_yield | the log-spaced class breaks ARE a style declaration, and `emission/publish.py` imported them backwards out of a processing tool to build its interval colormap. | DELETED (wave A, placement debts). CONDITION MET: the seven breaks are declared on the `sediment_yield_t_ha_yr` preset in `styles.yaml`; the module reads them through `emission.styles.legend_classes` so its legend key and its paint are one table. `hex_to_rgba` has one caller left, inside the resolver. | placement debts |
| `DEFAULT_AQUIFER_K_MS = 1e-4` + `DEFAULT_POROSITY = 0.3` demo constants (`contracts/trid3nt_contracts/modflow_contracts.py`) + their use as `MODFLOWRunArgs.aquifer_k_ms` / `porosity` field defaults | contracts + trid3nt_server/workflows/modflow | law 9 (charter): a physics-consequential value with no real data source must REFUSE, not run on an invented demo constant. Condition: a real data source serves aquifer K/porosity at the AOI so the family DERIVES-or-REFUSES instead of falling through to the demo value. | DELETED (ADR 0285 P2, commit-pending). CONDITION MET: the shared `_aquifer_resolve` seam derives K+porosity from SoilGrids texture (Saxton-Rawls 2006 pedotransfer) at the AOI (`basis="derived"`) and REFUSES (typed `PHYSICS_INPUT_REQUIRED`) when SoilGrids cannot serve. Both constants removed; `aquifer_k_ms`/`porosity` are now REQUIRED fields on `MODFLOWRunArgs` (no default). Grep-to-zero in product code (`DEFAULT_AQUIFER_K_MS`/`DEFAULT_POROSITY` remain only in a contract comment + docstring + `test_capture_zone_soil_k` as explicit user-supplied test inputs, never as a runtime default). Proven live: the Woburn TCE A/B -- derived K=9.1e-6 m/s vs the dead 1e-4 (~11x), porosity 0.278 vs 0.3. | ADR 0285 / demo-physics-defaults-audit.md rows 1-2 |
| `capture_zone` private `_derive_soil_k` + `_aquifer_k_caveat` + `SOIL_TEXTURE_HALF_DEG`/`SOIL_TEXTURE_DEPTH` (`trid3nt_server/workflows/modflow/capture_zone/capture_zone.py`) | trid3nt_server/workflows/modflow/capture_zone | the SoilGrids pedotransfer path generalizes into the shared archetype seam (so the whole family reuses it, not just capture_zone) | DELETED (ADR 0285 P2, commit-pending). CONDITION MET: the pedotransfer path moved to the shared `_aquifer_resolve.py` seam (`derive_soil_k` + `resolve_aquifer_properties`); capture_zone now delegates to it. The prose `_aquifer_k_caveat` (which showed the demo constant) is replaced by `provenance_summary(resolution)` reading `summary["aquifer_provenance"]`. Grep-to-zero: no `_derive_soil_k` / `_aquifer_k_caveat` remains in capture_zone. | ADR 0285 |
| `DEFAULT_MANNING_OVERLAND = 0.03` demo constant (`contracts/trid3nt_contracts/swmm_contracts.py`) + its use as the `SWMMRunArgs.manning_overland` field default + the `swmm_urban_flood` tool signature default `0.03` + the static `overland_manning_n=0.03` `SyntheticInput` (was `consequence="scenario"`, "landcover-derived n not wired") in `urban_flood.py` (both gate + envelope sites) | contracts + trid3nt_server/workflows/swmm/urban_flood | law 9: a uniform overland Manning's n over an urban catchment is invented friction. Condition: a real source serves overland n at the AOI so urban_flood DERIVES-or-REFUSES instead of the baked 0.03. | DELETED (ADR 0285 P4, commit-pending). CONDITION MET: the shared `roughness_resolve.resolve_overland_manning` seam derives the area-weighted mean of the NLCD land-cover Manning's n over the AOI (`basis="derived"`, `real_source="fetch_landcover (NLCD area-weighted Manning's n)"`, the SFINCS `manning_mapping.csv` table) and REFUSES (`SWMM_PHYSICS_INPUT_REQUIRED`, `consequence="physics"`) when NLCD cannot serve. `DEFAULT_MANNING_OVERLAND` removed; `manning_overland` is now `float | None` (None -> derive-or-refuse) on both `SWMMRunArgs` and the tool. The static 0.03 provenance entry is replaced by the resolver's entry. Grep-to-zero: no `DEFAULT_MANNING_OVERLAND` remains in product code. NOTE (law-6 scope): the engine-primitive `DEFAULT_OVERLAND_N=0.03` in `mesh/raster_cell_mesh.py` (the FROZEN `build_swmm_mesh` mechanical default) is RETAINED - the composer always resolves/refuses before the builder is reached, so it is only a direct-call/unit-test fallback, never a user-facing invented physics surface (analogous to a numerical default; ADR 0285 §numerical). | ADR 0285 P4 / demo-physics-defaults-audit.md row 23 |
| geoclaw `storm_surge` silent `manning_n = 0.025` bottom-friction default (`trid3nt_server/workflows/geoclaw/storm_surge/storm_surge.py`, tool signature + `GeoClawRunArgs.manning_n` pass-through) | trid3nt_server/workflows/geoclaw/storm_surge | law 9: a single 0.025 friction over a whole coast is invented, and it rode SILENTLY (no provenance surface). Condition: NLCD-derived-or-refuse with a provenance entry through the input-review gate. | DELETED (ADR 0285 P4, commit-pending). CONDITION MET: `storm_surge` now resolves `manning_n` through the SAME `roughness_resolve.resolve_overland_manning` seam (NLCD area-weighted derive), passes the entry through `gate_input_review` (refuses in auto when unresolved: `GEOCLAW_PHYSICS_INPUT_REQUIRED`, `consequence="physics"`), and surfaces the entry on the returned layer's `synthetic_inputs`. Tool signature + docstring changed `0.025` -> `None` (derive-or-refuse). REPORTED (not silently widened): the SAME `manning_n=0.025` default lives in geoclaw `inundation`/`amr_regions`/`gauge_timeseries` (audit row 16 named only storm_surge/regional_manning; regional_manning REQUIRES `manning_coefficients` so has no invented default) - those siblings are QUEUED for NATE's call, not converted this wave. | ADR 0285 P4 / demo-physics-defaults-audit.md row 16 |
| Landlab soil-strength demo constants `DEFAULT_SOIL_TRANSMISSIVITY_M2_DAY=20` / `DEFAULT_SOIL_COHESION_PA=10000` / `DEFAULT_SOIL_INTERNAL_FRICTION_DEG=35` / `DEFAULT_SOIL_DENSITY_KG_M3=2000` / `DEFAULT_SOIL_THICKNESS_M=1` (`contracts/trid3nt_contracts/landlab_contracts.py`) + their use as `LandlabRunArgs` field defaults | contracts + trid3nt_server/workflows/landlab/susceptibility | law 9: the infinite-slope factor-of-safety is set directly by the soil-strength block; a baked demo value silently sets the hazard. Condition: the block DERIVES what texture honestly serves and REFUSES the rest. | DELETED (ADR 0285 P3-completion, commit-pending). CONDITION MET: `landlab_susceptibility` resolves the block at the AOI - dry bulk density DERIVED from SoilGrids texture (Saxton-Rawls `rho_b = (1 - theta_s) * 2650`, `basis="derived"`), cohesion / internal friction / mantle thickness / transmissivity REFUSE in auto (`LANDLAB_PHYSICS_INPUT_REQUIRED`, literature-range user-gated offers - no fetchable value / not texture-derivable). The five fields are now `float | None` (None -> derive-or-refuse). `build_landlab_build_spec` includes them only when resolved; the overland-flow chain (rainfall-driven) does NOT gate on strength. Grep-to-zero: the five `DEFAULT_SOIL_*` symbols removed from product code (worker `component_chain.py` keeps its OWN inline `spec.get(k, literal)` mechanical fallbacks - direct-call/unit-test only, the composer resolves/refuses first; the P4 precedent). | ADR 0285 P3 / demo-physics-defaults-audit.md row 8 |
| Landlab green-ampt demo constants `DEFAULT_GREEN_AMPT_K_M_S=1e-5` / `DEFAULT_GREEN_AMPT_SOIL_TYPE="sandy loam"` (`contracts/trid3nt_contracts/landlab_contracts.py`) + their use as `LandlabRunArgs` + `landlab_green_ampt_overland_flow` tool-signature defaults | contracts + trid3nt_server/workflows/landlab/green_ampt | law 9: the Green-Ampt Ksat + capillary suction (selected by the texture class) set the infiltration/runoff partition; a baked sandy-loam value invents the soil. Condition: DERIVE Ksat + texture class from SoilGrids or REFUSE. | DELETED (ADR 0285 P3-completion, commit-pending). CONDITION MET: `landlab_green_ampt_overland_flow` DERIVES the Ksat (Saxton-Rawls) AND the USDA texture class (`aquifer_resolve.usda_texture_class`, which selects Landlab's Green-Ampt suction) from ONE SoilGrids read at the AOI centroid (`basis="derived"`), and REFUSES in auto (`LANDLAB_PHYSICS_INPUT_REQUIRED`) when SoilGrids cannot serve. Both fields `float|None` / `str|None`. Grep-to-zero in product code (worker keeps its own inline fallback per the P4 precedent). | ADR 0285 P3 / demo-physics-defaults-audit.md row 10 |
| Landlab groundwater demo constants `DEFAULT_GW_HYDRAULIC_CONDUCTIVITY_M_S=1e-4` / `DEFAULT_GW_POROSITY=0.3` (`contracts/trid3nt_contracts/landlab_contracts.py`) + their use as `LandlabRunArgs` + the `landlab_groundwater_water_table` / `landlab_groundwater_storm_recession` tool-signature defaults | contracts + trid3nt_server/workflows/landlab/groundwater_water_table + groundwater_storm_recession | law 9: aquifer K + drainable porosity set the water-table/seepage/baseflow; a baked permeable-sand value invents the aquifer. Condition: DERIVE K + porosity from SoilGrids or REFUSE. | DELETED (ADR 0285 P3-completion, commit-pending). CONDITION MET: BOTH groundwater templates DERIVE K + drainable porosity from ONE SoilGrids texture read at the AOI (the shared `derive_soil_scalars` / `soil_derived_entry` seam, `basis="derived"`) and REFUSE in auto (`LANDLAB_PHYSICS_INPUT_REQUIRED`) when SoilGrids cannot serve. Both fields `float|None`. The areal recharge (scenario forcing) + aquifer thickness (screening structural assumption) stay labeled `consequence="scenario"` and PROCEED - recharge is NOT auto-derived (a precip-fraction estimate would invent the fraction; reasoning in ADR). Grep-to-zero in product code. | ADR 0285 P3 / demo-physics-defaults-audit.md row 9 |
| Landlab channel-incision demo constant `DEFAULT_K_BEDROCK=1e-5` (`contracts/trid3nt_contracts/landlab_contracts.py`) + its use as `LandlabRunArgs.k_bedrock` + the `landlab_channel_incision_steady_state` tool-signature default | contracts + trid3nt_server/workflows/landlab/channel_incision | law 9: stream-power erodibility K_sp is a calibration coefficient with NO fetchable real-world value; a baked default invents it. Condition: REFUSE with a literature-range offer (exponents stay scenario). | DELETED (ADR 0285 P3-completion, commit-pending). CONDITION MET: `k_bedrock` is now `float|None` and REFUSES in auto (`LANDLAB_PHYSICS_INPUT_REQUIRED`, literature-range user-gated offer `~1e-6..1e-4`). The combined `uplift_erodibility_forcing` physics entry is SPLIT: K_sp -> `consequence="physics"` (refuse); uplift_rate -> `consequence="scenario"` (the tectonic what-if question); m_sp/n_sp -> `consequence="numerical"` (canonical published stream-power exponents, kept as `DEFAULT_M_SP`/`DEFAULT_N_SP`). Grep-to-zero: `DEFAULT_K_BEDROCK` removed from product code. | ADR 0285 P3 / demo-physics-defaults-audit.md row 11 |
| SWMM `swmm_aquifer_baseflow_to_node` two-zone column demo constants `porosity=0.46` / `wilting_point=0.13` / `field_capacity=0.28` / `conductivity_in_hr=0.8` (the SILENT row 27: tool + `build_aquifer_inp` signature defaults, `aquifer_baseflow.py`) | trid3nt_server/workflows/swmm/aquifer_baseflow | law 9: the [AQUIFERS] two-zone moisture column drives the baseflow recession directly and rode COMPLETELY UNLABELED (no provenance surface, no gate). Condition: DERIVE the column from SoilGrids or REFUSE, through the input-review gate. | DELETED (ADR 0285 P3-completion, commit-pending). CONDITION MET: the tool gained a `location`/`lat`/`lon` AOI input and DERIVES the two-zone column (porosity=theta_s, wilting=theta_1500, field_capacity=theta_33, conductivity=Ksat) from SoilGrids texture via the shared `derive_soil_column` (`basis="derived"`), through a new `gate_input_review` call, and REFUSES in auto (`SWMM_PHYSICS_INPUT_REQUIRED`) when neither a site nor an explicit column is given or SoilGrids cannot serve. The four column params are `float|None`; `build_aquifer_inp`'s column args are now REQUIRED (no defaults). Grep-to-zero: the four literals removed from product code. Proven live (Ames IA A/B): derived column conductivity 0.13 in/hr vs the dead 0.8 demo (~6x slower drainage), porosity 0.464 vs 0.46, and the forced-unavailable refusal. | ADR 0285 P3 / demo-physics-defaults-audit.md row 27 |
| SCHISM `schism_baroclinic_circulation` tool-signature demo default `river_discharge_m3s = 500.0` + the `river_discharge != 500.0` "user vs default_demo" basis test + the hand-built `river_discharge` `SyntheticInput` in `baroclinic_circulation.py` | trid3nt_server/workflows/schism/baroclinic_circulation | law 9: the freshwater river inflow sets the estuarine salt-intrusion length and the whole salinity gradient; a baked 500 m3/s invents the forcing. Condition: a real source serves the AOI inflow so baroclinic DERIVES-or-REFUSES instead of the baked 500. | DELETED (ADR 0285 P5, commit-pending). CONDITION MET: the new shared `discharge_resolve.resolve_dominant_discharge` seam derives the discharge from the DOMINANT NOAA National Water Model reach over the AOI (the main-stem carrier; `basis="derived"`, `real_source="fetch_noaa_nwm_streamflow (NWM analysis, dominant reach)"`, reusing river_dye's proven NWM reach-read machinery) and REFUSES in auto (`consequence="physics"` default_demo -> the input-review gate cancels) when NWM cannot serve. The tool signature default `500.0` -> `None` (derive-or-refuse); the hand-built provenance entry is replaced by the resolver's entry. NOTE (law-6 scope, P4 precedent): what DIES is the TOOL-SIGNATURE default `river_discharge_m3s: float = 500.0` (the value that made auto SILENTLY solve on an invented inflow) -> now `None` -> derive-or-refuse. RETAINED as mechanical last-resorts (never a user-facing invented physics surface, analogous to `build_swmm_mesh`'s `DEFAULT_OVERLAND_N`): (a) `deck_authoring.author_baroclinic_estuary_deck`'s `river_discharge_m3s: float = 500.0` primitive default; (b) the composer's `deck_discharge_m3s = ... else 500.0` fallback, reached ONLY on the user_gated-approved-unresolved path (explicit consent) and reported verbatim in the returned provenance. The composer resolves or refuses (auto) before either is load-bearing. | ADR 0285 P5 / demo-physics-defaults-audit.md row 19 |
| `output_quantities.py` + `publish_quantities.py` scaffold + its 4 live engine consumers' calls (`postprocess_openquake/_modflow/_swmm/_landlab` `_pq.publish_quantities` blocks) + tests (`test_{modflow,swmm,landlab,openquake}_step3_quantities`, `test_publish_quantities`, `test_output_quantity_style_presets`) | contracts + trid3nt_server/workflows + tests | ALL FOUR live consumers migrate onto the `outputs.json` writer + the `emission/quantity_styles` quantity->style registry, ONE engine at a time with byte-equivalence per engine. CRITICAL: this is NOT the recon's "empty-except-OpenQuake scaffold" -- it is a LIVE 4-engine feature (openquake hazard-curves/uhs; modflow plume-ts/water-table/drawdown/dewatering/budget/mounding/recovery/hydroperiod; swmm flooding-losses/ponded/conduit-flow/velocity; landlab drainage/slope/wetness/discharge/FoS) producing real product layers. Deleting it retires that feature, so the condition is per-engine migration, not "delete now". **MODFLOW ANNOTATION (ADR 0284, 2026-08-17): MODFLOW's scaffold half was UNIQUELY DEAD -- `publish_modflow_quantities` (the `plume-concentration-ts` + `water-table` reader) was defined + unit-tested but NEVER called by any composer (grep-to-zero), UNLIKE the LIVE `publish_swmm_quantities` / `publish_landlab_quantities` / `publish_openquake_quantities` (still wired to `urban_flood` / `susceptibility` / `psha`). So the MODFLOW half + its ONE test (`test_publish_modflow_quantities_emits_timeseries_and_head`) are DELETED now (grep-to-zero confirmed; `_modflow_src_transform` RETAINED, pinned by `test_modflow_georef_hardening`); MODFLOW's `plume-concentration-ts` is SUPERSEDED-BY-SEAM (the transport family's concentration/temperature animation is now the `outputs.json` emit-on-solve seam, ADR 0284). The MODFLOW `OUTPUT_QUANTITIES` REGISTRY specs (plume-concentration-ts/water-table/drawdown/...) REMAIN -- the head-based specs are charts-only (fork 2A), no live consumer, but the row's full-scaffold deletion still awaits the THREE remaining LIVE halves (swmm/landlab/openquake). Verified this wave: those three are NOT dead (their composers still call them), so out-of-scope halves untouched.** | QUEUED | ADR 0280 / verified 2026-08-16 / MODFLOW half DELETED per ADR 0284 |
| The FRAME entries the docker RASTER workers dual-wrote into `publish_manifest.json` (`workers/_raster_postprocess/postprocess.py` SFINCS depth+waves, `workers/_geoclaw_postprocess/postprocess.py`, `workers/_swan_postprocess/postprocess.py`) | workers | superseded when the engine's frames publish through `outputs.json` + the emit-on-solve seam instead | DELETED (ADR 0294, 2026-08-19, commit b60e67f8). NARROW SCOPE per NATE's premise-corrected ruling: `publish_manifest.json` SURVIVES as the metrics carrier + the legacy register-only fallback; only its FRAME entries die. The three producers build their frames into a local list feeding `outputs.json` ALONE; the degrade guards moved with them (`len(frames) < 2` replaces `len(layers) < 3`) and the per-frame `metrics` computation died with the entries that carried it. `list_run_frames` reads `outputs.json` FIRST (raster entries with a `t`, ordered by `t`) and keeps a LEGACY-run `publish_manifest` frame fallback. PROVEN LIVE through all three rebuilt images: SFINCS `01M0H8FWAX4G4M260VRGR2XZCC` (Mexico Beach quadtree, outputs.json 146 entries / publish_manifest 1 layer / metrics intact / list_run_frames 145), GeoClaw `01M0H882Q83BCPDMHKC3AF0TWC` (8 / 1 / 7), SWAN nonstationary `01M0H8RNCGHBV96KFFXNE6HZRW` (20 / 1). Legacy fallback proven on the pre-outputs.json run `01KZWT7J3T0V95E8HF0E5S8XHF` (7 frames served from publish_manifest). PARKED FORK RESOLVED: NATE picked option (b) 2026-08-20 -- the whole out-of-process SWMM lane is DELETED (own row below, ADR 0295), so `workers/_swmm_postprocess` died with its producer rather than being migrated. | ADR 0294 / outputs-manifest-schema.md 7.3 |
| `workers/_raster_postprocess/manifest.py` + `contracts/trid3nt_contracts/publish_manifest.py` (the bespoke publish_manifest schema) + `register_published_manifest.py` register-only path | workers + contracts + workflows/shared | superseded when the composers stop reading `publish_manifest.json` for their narration metrics (it is now the metrics carrier + legacy fallback ONLY -- its frame entries are already gone, ADR 0294) AND the legacy pre-`outputs.json` runs in the runs bucket age out | QUEUED (NARROWED 2026-08-19: the frame half is DELETED under ADR 0294; what remains is the metrics carrier + the register-only fallback) | ADR 0280 / ADR 0294 / outputs-manifest-schema.md 7.3 |
| SFINCS flood bespoke frame-emission (the S2 subsample-to-`MAX_FLOOD_FRAMES`=144 post-hoc thinning in the flood postprocess) | workers/_raster_postprocess + workflows/sfincs | the flood proving case lands: worker writes `outputs.json`, the seam publishes, byte-equivalence bar passes, and cadence resolves DECK-SIDE (`dtout`/`dtmaxout` from `output_interval_min` + a sane default) so NO post-hoc thinning is needed | DELETED (ADR 0280 live close-out 2026-08-17). The post-hoc even-subsample thinning is RETIRED: `workers/_raster_postprocess/sfincs_reader.select_frame_time_indices` now returns `list(range(n_steps))` (never-omit); the dead `MAX_FLOOD_FRAMES` constant + its `os` import removed; test re-pinned to `test_select_frame_time_indices_never_omits`. Cadence is the SOLE control DECK-SIDE (`_resolve_output_interval_min` UNPINS the pluvial lever: an explicit `output_interval_min` now flows to the deck `dtout`; unspecified pluvial keeps the legacy hourly formula). PROVEN LIVE through the rebuilt `trid3nt-local/sfincs:latest` image (run `01QT260817055751MEXBEACH`, coastal quadtree): `outputs.json` = 26 entries, 25 frames at 30-min cadence (t=0..43200 s), all 25 published (no thinning), completion status=ok; the flood seam consumer registered all 26 layers + replay stamps. Grep-to-zero: no `MAX_FLOOD_FRAMES` remains in `workers/_raster_postprocess`. NOTE: the `publish_manifest.json` frame entries this producer also wrote are GONE as of ADR 0294 (the narrow frame collapse) -- publish_manifest keeps the peak entry alone, as the metrics carrier + register-only fallback. | ADR 0280 (item 5) / live close-out 2026-08-17 |
| GeoClaw bespoke frame-emission (the post-hoc `_select_frame_indices` subsample-to-`MAX_FRAMES`=144 thinning in the GeoClaw postprocess) | workers/_geoclaw_postprocess | the GeoClaw leg lands: worker writes `outputs.json`, the seam publishes, byte-equivalence bar passes, cadence resolves DECK-SIDE (`output_frames` = the native `clawdata.num_output_times` count) so NO post-hoc thinning is needed | DELETED (ADR 0281 live close-out 2026-08-17). `workers/_geoclaw_postprocess.postprocess._select_frame_indices` now returns `list(range(n_steps))` (never-omit); the dead `MAX_FRAMES` constant + its now-unused `import os` removed; pinned by `test_geoclaw_select_frame_indices_never_omits`. The deck-side `output_frames` lever (native count) is the SOLE frame-count control. PROVEN LIVE through the rebuilt `trid3nt-local/geoclaw:latest` image (run `01M089JY3DWBZ9ZREE0TWG9ZQN`, Crescent City tsunami, `output_frames=6`): `outputs.json` = 8 entries (1 peak + 7 frames), physical `t` from `fort.t` = 0/300/600/900/1200/1500/1800 s, ALL 7 dumps published (no thinning), completion status=ok; the seam registered all 8 layers as `flood-depth-*` (preset `continuous_flood_depth`). Grep-to-zero: no `MAX_FRAMES` remains in `workers/_geoclaw_postprocess`. NOTE: the `publish_manifest.json` frames this producer also wrote are GONE as of ADR 0294 (the narrow frame collapse) -- publish_manifest keeps the peak entry alone, as the metrics carrier + register-only fallback. | ADR 0281 / live close-out 2026-08-17 |
| SWAN bespoke frame-emission (the post-hoc `_select_frame_indices` subsample-to-`MAX_FRAMES`=144 thinning in the SWAN postprocess) | workers/_swan_postprocess | the SWAN leg lands: worker writes `outputs.json`, the seam publishes, byte-equivalence bar passes, cadence resolves DECK-SIDE (`output_frames` count -> the nonstationary BLOCK `dt`) so NO post-hoc thinning is needed | DELETED (ADR 0281 live close-out 2026-08-17). `workers/_swan_postprocess.postprocess._select_frame_indices` now returns `list(range(n_steps))` (never-omit); the dead `MAX_FRAMES` constant + its now-unused `import os` removed; pinned by `test_swan_select_frame_indices_never_omits`. The deck-side `output_frames` lever is the SOLE frame-count control. PROVEN LIVE through the rebuilt `trid3nt-local/swan:latest` image (run `01M08ACMKWQ7XV23ZFJ06SND76`, nonstationary storm, `output_frames=18`): `outputs.json` = 20 entries (1 peak + 19 frames), evenly-spaced physical `t` = 0..129600 s, ALL 19 snapshots published (no thinning), completion status=ok; the seam built 20 layers (1 temporal group) as `wave-height-*` (preset `continuous_wave_height`); the SAME rebuild also fixed two latent SWAN-image gaps (the postprocess packages were never COPY'd into the image, and the COG output-sweep ran before the COGs were written) so the SWAN producer runs in-image for the first time. Grep-to-zero: no `MAX_FRAMES` remains in `workers/_swan_postprocess`. | ADR 0281 / live close-out 2026-08-17 |
| SWMM bespoke frame-emission (the post-hoc `_select_frame_time_indices` subsample-to-`MAX_FLOOD_FRAMES`=144 thinning in `postprocess_swmm` + the frame `SWMMDepthLayerURI` construction the composers consumed as `layers[1:]`) | trid3nt_server/workflows/swmm | the SWMM M-class leg lands: `postprocess_swmm` writes `outputs.json` host-side, the seam publishes the frames (frames_only), byte-equivalence bar passes on the FRAME render stream, and cadence resolves DECK-SIDE (`output_interval_min` -> deck `REPORT_STEP`) so NO post-hoc thinning is needed | DELETED (ADR 0282, commit-pending). The `_select_frame_time_indices(n_steps)` even-subsample call is GONE from `postprocess_swmm`; every reporting step's depth COG is written + recorded in `outputs.json` (`_write_frame_cogs_and_entries`, never-omit). The bespoke frame-layer builder `postprocess_swmm._emit_frame_layers` (returned `SWMMDepthLayerURI` frames) is DELETED -- `postprocess_swmm` now returns `[peak]` only; `urban_flood` + `dual_drainage` read the frames back via the seam (`_read_swmm_frame_layers` -> `build_layers_from_outputs(frames_only=True)`). Cadence is deck-side (`_report_step_hms(output_interval_min)` -> `REPORT_STEP`; `None` keeps `00:05:00` byte-identical). The typed peak stays composer-built (OPTION a -- the seam owns frames only). Grep-to-zero: no `_select_frame_time_indices` / `MAX_FLOOD_FRAMES` remains in `workflows/swmm`. Pinned by `test_swmm_outputs_seam.py` (frame byte-equivalence + never-omit + frames_only-skips-peak + cadence) + the updated `test_postprocess_swmm.py` + the end-to-end round-trip in `test_run_swmm_local_chain.py`. NO IMAGE REBUILD (SWMM is in-process pyswmm; host-exec writer). PROVEN LIVE through `model_swmm_urban_flood` (Alexandria VA, `output_interval_min=10`, run `01M08MVWRDT9FX7YA94GXT9DYE`): `outputs.json` = 13 entries (1 peak + 12 frames), 600 s deck-side cadence, all 12 seam frames published (never-omit), temporal group + reopen identical, typed peak scalars intact. | ADR 0282 |
| Landlab overland bespoke frame-emission (the worker interval FLOOR `max(output_interval_s, duration_s/48)` + `_MAX_TIMESERIES_SNAPSHOTS`=48 in `workers/landlab/component_chain.py` + the frame `LayerURI` construction in `postprocess_landlab_overland_timeseries` consumed as `layers[1:]`) | workers/landlab + trid3nt_server/workflows/landlab | the Landlab overland M-class leg lands: the postprocess writes `outputs.json` host-side, the seam publishes the frames (frames_only), byte-equivalence passes on the FRAME render stream, and cadence resolves DECK-SIDE (`output_interval_s` honored exactly) so NO interval floor is needed | DELETED (ADR 0282, commit-pending). The `interval_s = max(interval_s, duration_s/48)` floor + the `_MAX_TIMESERIES_SNAPSHOTS` constant are GONE from `component_chain._run_overland_flow_timeseries` (honor `output_interval_s` EXACTLY -- never-omit; runs exec-from-source so it takes effect immediately, NO image rebuild). The bespoke frame-layer loop in `postprocess_landlab_overland_timeseries` is DELETED -- it now writes each snapshot's COG + `outputs.json` entry (per-frame `t` = the worker's real `max_cell_series` elapsed seconds) and returns `[peak]` only; the composer reads frames back via the seam (`_read_overland_frame_layers` -> `build_layers_from_outputs(frames_only=True)`). Universal `output_interval_min` aliases native `output_interval_s` (0281 precedent, documented). Grep-to-zero: no `_MAX_TIMESERIES_SNAPSHOTS` remains. Pinned by `test_landlab_outputs_seam.py` (frame byte-equivalence + never-omit + real-elapsed-t + frames_only-skips-peak). PROVEN LIVE through `model_landlab_overland_flow_timeseries` (Boulder CO, `output_interval_s=300`, run `01M08MYKT549RFJ9F10TFDMVFV`): `outputs.json` = 13 entries (1 peak + 12 frames), per-frame `t` = the worker's REAL uneven CFL-driven snapshot elapsed seconds `[300, 603.58, ..., 3600]`, all 12 seam frames published, temporal group + reopen identical, typed peak intact. | ADR 0282 |
| rain_on_grid bespoke results-mesh emit (`_publish_full_results_mesh` in `trid3nt_server/workflows/telemac/rain_on_grid/rain_on_grid.py` -- hand-wired `publish_input_layer` of `r2d_rog.slf` as a `layer_type="mesh"` LayerURI) | trid3nt_server/workflows/telemac/rain_on_grid | the L-class TELEMAC native-mesh leg lands: the agent-side postprocess writes `outputs.json` with a `kind="mesh"` entry (crs_authid=EPSG:{utm}), the seam publishes the mesh layer (`build_layers_from_outputs`, built even under frames_only), and byte-equivalence on the mesh layer (name/style/role/crs/uri) passes so the framework owns emission (charter law 8) -- NO composer-hand-wired mesh emit needed | DELETED (ADR 0283, FINAL -- 4b live-confirmed 2026-08-17). `_publish_full_results_mesh` is GONE from `rain_on_grid.py`; the composer calls the shared `results_mesh_seam.publish_results_mesh_via_seam` (write outputs.json -> read back via the seam frames_only -> emit the mesh layer). The seam mesh layer is byte-equivalent field-for-field to the deleted helper EXCEPT the `layer_id` STEM (`model-results-mesh-{run_id}` vs the helper's `rog-results-{run_id}`) -- an idempotence key, web grouping rides `name` (`detectSequentialGroups`), so the swap renders identically (the ADR-0281 explained divergence). river_dye + coastal are ADDITIVE (no prior mesh emit to delete). Grep-to-zero: no `_publish_full_results_mesh` remains. Captured field-by-field in `test_outputs_seam.py::test_mesh_entry_publishes_native_mesh_layer`. CONDITION MET (4b live-confirmed 2026-08-17): the rebuilt telemac image (parser `telemac-reach-10` / `coastal-tidal-2`, provenance-checked in-image) solved rain_on_grid live (run `01M094YVQMZSH85D5ETMA9744P`, Otto NC, 4986 nodes) and the seam emitted `model-results-mesh-01M094YVQMZSH85D5ETMA9744P` (`mesh_grid`/`context`/EPSG:32617, uri `.../r2d_rog.slf`) -- `outputs.json` carried the `kind="mesh"` entry (crs_authid set) + the `flood_depth` peak entry; the emitted mesh layer is field-equivalent to the deleted helper (name `Model results (time series): Otto, North Carolina`, style `mesh_grid`, role `context`) modulo the explained `layer_id` stem. Dock-load: the real solved `r2d_rog.slf` loads valid through REAL QGIS/MDAL (QGIS 3.40.6, `QgsMeshLayer(...,"mdal")`, 4 dataset groups) -- the `.slf` staging the fix intends works; HONEST caveat -- on MDAL 3.40.6 the same bytes staged `.nc` also load (SELAFIN content-sniffing), so the extension-rejection the fix guards against is not exhibited on this MDAL build (the fix is correct-by-driver-selection-spec, defensive). | ADR 0283 |
| Category routing layer: `categories.py` (CategorySpec + CATEGORIES + PRIMARY_CATEGORY + SECONDARY_CATEGORIES + AllowedToolSet + validate_function_call + list_categories/list_tools_in_category tools, 1408 LOC) | trid3nt_server/agent | superseded by embedding retrieval (tool-gating log: "no-op ... already visible via the retrieval-enforce layer" every turn) | DELETED (commit-pending) | ADR 0276 / NATE order 2026-08-16. grep-to-zero across trid3nt_server + scripts + experiments + tests; the always-visible floor survives as `CORE_FLOOR` in tool_retrieval.py; `AllowedToolSet` -> plain `SessionState.visible_tools: set[str]`; the post-hoc validator's hallucination guard re-homes onto the dispatch's `ToolNotFoundError`. Registry 256 -> 254. |
| `TRID3NT_TOOL_RETRIEVAL` env knob + off/shadow modes + config plumbing (`_TOOL_RETRIEVAL_MODE`, `_TOOL_RETRIEVAL_VALID_MODES`, `_tool_retrieval_mode`) | trid3nt_server/server/config.py + _core.py | enforce proven as the surfacing path (recall sanity + baseline suite hold) | DELETED (commit-pending) | ADR 0276. Enforce is now unconditional; `K` (TRID3NT_TOOL_RETRIEVAL_K) stays the only lever. The per-turn selection event is retained (recall@k dashboard consumer), mode hardcoded "enforce". NATE's .env.local carries the now-inert `TRID3NT_TOOL_RETRIEVAL=enforce` (stale var, harmless, left alone). |
| 5 category/validator test files (`test_categories`, `test_allowed_set`, `test_validator`, `test_post_hoc_routing`, `test_dynamic_hot_set_integration`, 938 LOC) | tests | categories.py deleted | DELETED (commit-pending) | ADR 0276. Their purpose WAS the taxonomy/validator; deleted outright. Template/router/compute tests kept their registration + corpus checks, lost only the category-pin assertions. |
| Phantom SFINCS quadtree stub narrative (AWS-Batch `DECK_BUILD_FAILED` deck-builder job-def, `build_sfincs_quadtree_deck` submit path) | flood.py docstrings + inline comments | the REAL quadtree leg lands (cht_sfincs worker build+solve) -- the flag no longer falls through to the regular grid | DELETED (commit-pending) | ADR 0113 / mesh census 2026-08-03 (M4 signed BUILD). The narrated path never existed as code; the docstring/comments described an unimplemented AWS-Batch deckbuilder. Replaced by the real cht_sfincs worker-image build+solve contract; `build_sfincs_quadtree_deck` now EXISTS (services/workers/_sfincs_build/deck_quadtree.py). Read-side probe (`_is_quadtree_output`/`_read_face_coords`) KEPT -- verified real against a live quadtree solve, not bit-rotted. |
| /api/export-qgis + /file legacy routes | tool_catalog_http | NATE plugin reinstall confirms /api/case-layers | DELETED (ADR 0116) - both routes + their helpers (_ExportQgis* classes, _handle_export_qgis_post, _export_qgis_root, _resolve_export_qgis_file) removed; streaming is the path, /api/case-layers itself already deleted | hygiene batch 0058 / ADR 0116 |
| Remote materialize+download hydration fallback | cases/hydrate_case_layers | remote store access ships (presigned or agent-proxied ranges) | DELETED (ADR 0116) - CONDITION MET: remote store access ships as direct anonymous ranged /vsicurl reads over the tailnet (the trust boundary); cases/hydrate_case_layers.py + test_open_case_in_qgis[_mesh].py + test_export_qgis_http_route.py removed | 0058 amendment / ADR 0116 |
| secrets_handler file-vault + persistence secrets CRUD + server.py vault handlers (~935 LOC) | credentials/ + server.py + persistence.py | QGIS-store push seam + resolver.py ship (chop-plan wave); auth_handshake OUT of scope (session identity, not creds); credential_registry KEEPS | DELETED (commit-pending) | credentials-chop-plan.md / ADR 0062 |
| Legacy vault schemes (aws-ssm/gcp-sm/local-file) + GCP Secret Manager docstrings | secrets_handler | zero live use confirmed - dies with the vault slice | DELETED (commit-pending) | chop-plan audit / ADR 0062 |
| Inert source.yaml auth: blocks (27 specs) | specs + contract | LEFT INERT (ADR 0062): auth.user_agent IS read by the router (_router hooks/executors set the User-Agent header), so the block is NOT dead; only its api-key sub-fields are unwired to the resolver (TOOL_PROVIDER is the real surface). Deleting the field is a separate router-scoped change; not this wave. | QUEUED (api-key sub-fields only) | chop-plan audit / ADR 0062 |
| Cloud Run Jobs submitter binding | agent/tools/meta/passthroughs.py | REJECTED - NOT dead GCP code: `set_worker_submitter`/`_WORKER_SUBMITTER` is the LIVE on-box qgis_process substrate seam (main.py binds `_default_qgis_process_submitter` = local docker/subprocess runner at startup; read by qgis_discovery; covered by test_qgis_discovery + test_main_startup). Only the "Cloud Run Jobs" docstring wording was stale/misleading GCP-era prose - corrected to describe the on-box lane (ADR 0064). The binding stays. | REJECTED 2026-07-31 | ADR 0064 / 2026-07-31 inspection |
| compute_blended_composite | processing/ | QGIS-native per-layer blend modes verified to cover the product need | QUEUED | 0057 conflict (3) |
| fetch_copernicus_dem ambient declaration | declarable pool | wave 11 item 0 (absorption into fetch_dem; internal seam stays) | CONDITION-MET (wave-11 ADR 0059: tier="internal" -- declaration removed from the declarable pool AND search index; the spec/seam is retained + registry-resolvable by design, so this is a declaration removal, not a py deletion; awaiting commit) | NATE 2026-07-31 |
| TRID3NT_CATALOG_ARM flag scaffolding (arms 1-3) | _router/stratified.py + flags | capable-model re-run decides: rollout -> baseline per-source declarations die instead; no-rollout decision -> scaffolding dies | QUEUED | ADR 0050/0055-era |
| 14+ per-source ambient declarations | declarable pool | Design-3-class arm ADVANCES on a capable model | QUEUED | pools architecture |
| grace2_* identifiers | repo-wide | Layer B dual-read rename executes | QUEUED | rebrand scope |
| env-var credential paths (as co-equal) | credentials resolution | QGIS store + broker ship; env demotes to last-resort (NOT deleted - demoted) | DEMOTED (ADR 0062: resolver order = session cache -> env fallback; env is the headless/dev floor, never co-equal, never deleted) | chop-plan direction |
| Job-numbered test filenames + ~166 test-comment markers | server/tests | next test-hygiene wave (no functional condition - scheduling only) | QUEUED | deferred hygiene debt |
| Deferred station-sibling twins (asos/raws/snotel/airnow/openaq) | fetchers | ALL 5 FOLDED via the EXISTING phases (ADR 0065), ZERO new machinery: asos = resolve (multi-state discovery) + bulk-CSV main fetch; raws = resolve (multi-state RAWS discovery) + PHASE E station x day obhistory (best-effort, expanding merge); snotel = catalog main-fetch + batched PHASE E (null-tolerant; best-effort = degrade-to-locations) + output.bbox_from_features (station extent = wave-4 emission gap, already declarative); airnow = http_json single-GET keyed; openaq = offset-paging (locations) + PHASE E (per-location latest + sensor->parameter join, expanding), keyed. The wave-4 "new mode / auth subsystem" verdicts were superseded by ADR 0063 (resolve/enrich/paging phases) + the credentials wave (ADR 0062, env/TOOL_PROVIDER key path) landed AFTER the deferral. Keyed sources never register a real key: the missing-key typed error is the parity surface. | DELETED (commit-pending) | ADR 0065 |
| fetch_asos_metar twin (fetch_asos_metar.py) + test_fetch_asos_metar.py | fetchers/weather | live twin-vs-router parity PASS (11 stations, 179 obs value-identical incl tmpf/valid) -> folded to source.yaml + asos_metar resolve/main hooks | DELETED (commit-pending) | ADR 0065 |
| fetch_raws_weather twin (fetch_raws_weather.py) + test_fetch_raws_weather.py | fetchers/weather | live parity PASS (10 stations incl CA+NV via body-order state tagging, 790 obs value-identical) -> folded to source.yaml + raws_weather resolve/enrich hooks | DELETED (commit-pending) | ADR 0065 |
| fetch_snotel_snow twin (fetch_snotel_snow.py) + test_fetch_snotel_snow.py | fetchers/soil | live parity PASS (18 stations, swe/depth/date value-identical + station extent) -> folded to source.yaml + snotel_snow main/enrich hooks + bbox_from_features | DELETED (commit-pending) | ADR 0065 |
| fetch_airnow_air_quality twin (fetch_airnow_air_quality.py) + test_fetch_airnow_air_quality.py | fetchers/weather | missing-key + input byte-identical (AIRNOW_MISSING_KEY credential-shaped, bad bbox/param); data path blocked-on-live-key -> folded to source.yaml + airnow_air_quality http_json hooks | DELETED (commit-pending) | ADR 0065 |
| fetch_openaq_measurements twin (fetch_openaq_measurements.py) + test_fetch_openaq_measurements.py | fetchers/weather | missing-key + input byte-identical (OPENAQ_KEY_REQUIRED credential-shaped); structural join proven offline; data path blocked-on-live-key -> folded to source.yaml + openaq_measurements paging/enrich hooks | DELETED (commit-pending) | ADR 0065 |
| asos _STATE_BBOX / _bbox_overlaps_state import in fetch_high_water_marks | fetchers/hydrology | asos twin folded -> hwm re-pointed to a self-contained local state-bbox table (ADR 0064c small-dup-for-clean-layering precedent; a coded fetcher must not import router internals) | DELETED/re-pointed (commit-pending) | ADR 0065 |
| gzip/vsizip native-GDAL collapse | _router modes | REJECTED - empirically refuted (36MB for 8x5px window) | REJECTED 2026-07-31 | gdal-collapse verdict |
| jrc colormap-ramp DSL | would-be mode | REJECTED - one-consumer DSL fails generalization bar | REJECTED 2026-07-31 | ADR 0047/0053 |
| project.qgz generation in case hydration | open_case_in_qgis | none - dead by module's own docstring | DELETED (ea36191) | NATE 2026-07-31 |
| /api/case-layers route + build_case_layers_manifest | tool_catalog_http + cases/ | plugin-unreached since choice A - WS case-open replay already restores layers; delete (hygiene-batch HTTP path proved redundant) | DELETED (commit-pending) | 0060 finding / ADR 0062 item 0 |
| meta/probe_point.py deregistered route-server | agent/tools/meta | relocate to cases/ (same posture as hydration relocation) | DELETED/relocated (commit-pending) - `git mv` to cases/probe_point.py; tool_catalog_http + 2 test files re-pointed; agent/tools/meta now holds no non-@register_tool module (invariant restored) | ADR 0064 |
| _strip_query/_unwrap_tile_template platform import from agent tools | cases/ vs agent | hoist to a shared agent URI util | DELETED (commit-pending) - hoisted to agent/tools/_uri_util.py; the 3 agent importers (query_point_hazard / compose_case_report / publish_layer) re-pointed; cases/hydrate_case_layers keeps its OWN copy (cases=platform layer must not import agent/tools -> wrong-direction; small dup accepted for layering) | ADR 0064 |
| Plugin local case-layers manifest fetch (case_export.fetch_case_layers_manifest, net.tasks._CaseLayersTask, dock._on_case_layers_finished, open_case_in_qgis local branch) | qgis-plugin/trid3nt (case/ + net/ + ui/) | none - fully redundant with the pre-existing case-open WS layer replay (same source data, same materializer) | DELETED (commit-pending) | decision A / ADR 0060 |
| "Export GeoTIFFs" case-list context-menu action | qgis-plugin/trid3nt/ui/cases_dialog.py | none - opening the case now restores layers automatically | DELETED (commit-pending) | decision A / ADR 0060 |
| Remote materialize+download hydration fallback (plugin side: dock.hydrate_case_layers, net.tasks._ExportTask, case_export.py's post_export_case/localize_remote_export/download_export_file/plan_export_layers, materializer.materialize_export) | qgis-plugin/trid3nt (ui/dock.py + net/tasks.py + case/case_export.py + render/layers.py) | remote store access ships (presigned or agent-proxied ranges) - STILL QUEUED, unchanged by decision A; now additionally unreached from any UI trigger (menu action that called it is deleted), kept callable pending the fold-in | DELETED (ADR 0116) - CONDITION MET (remote store access ships as ranged /vsicurl over the tailnet): dock.hydrate_case_layers + _on_export_finished/_on_export_errored + _export_tasks, net.tasks._ExportTask, case/case_export.py (whole module), render/layers.materialize_export + _apply_named_style + last_added_export_extent all removed; mesh now streams-or-stages through the LIVE materializer (_add_mesh) so no behavior regresses | decision A / ADR 0060 / ADR 0116 |
| fetch_nws_event twin (fetch_nws_event.py) + test_fetch_nws_event.py | fetchers/weather | live edge-matrix parity PASS vs twin -> folded to source.yaml + nws_event hooks (single-GET) | DELETED (commit-pending) | ADR 0061 tier-3 hook wave |
| fetch_usace_nsi twin (fetch_usace_nsi.py) + test_fetch_usace_nsi.py | fetchers/socioeconomic | live edge-matrix parity PASS vs twin -> folded to source.yaml + usace_nsi hooks (single-POST) | DELETED (commit-pending) | ADR 0061 tier-3 hook wave |
| HOOK-RATCHET: chained id/detail resolution mode (name->id or list->per-item-detail) | _router modes | MANDATORY-REVIEW (rule 4, 4x) - PROMOTED to the chained_resolution executor (resolve pre-step + offset paging + bounded/deduped/best-effort detail enrichment); all 4 folded, per-source coded twins DELETED | DELETED/promoted (commit-pending) | ADR 0063 |
| fetch_gbif_occurrences twin (fetch_gbif_occurrences.py) + test_fetch_gbif_occurrences.py | fetchers/biodiversity | live edge-matrix parity PASS vs twin -> folded to source.yaml + gbif_occurrences resolve/paging hooks (name->taxonKey resolve, offset paging) | DELETED (commit-pending) | ADR 0063 chained-resolution wave |
| fetch_inaturalist_observations twin (fetch_inaturalist_observations.py) + test_fetch_inaturalist_observations.py | fetchers/biodiversity | live edge-matrix parity PASS vs twin -> folded to source.yaml + inaturalist_observations resolve/paging hooks | DELETED (commit-pending) | ADR 0063 chained-resolution wave |
| fetch_nws_alerts_conus twin (fetch_nws_alerts_conus.py) + test_fetch_nws_alerts_conus.py | fetchers/weather | live edge-matrix parity PASS vs twin -> folded to source.yaml + nws_alerts_conus hooks (zone-polygon enrichment); SFINCS composer re-pointed to the registry seam + a workflow-local raw-GeoJSON read | DELETED (commit-pending) | ADR 0063 chained-resolution wave |
| fetch_nws_river_forecast twin (fetch_nws_river_forecast.py) + test_fetch_nws_river_forecast.py | fetchers/hydrology | live edge-matrix parity PASS vs twin -> folded to source.yaml + nws_river_forecast hooks (gauges list / single detail + bounded threshold/stageflow enrichment); bool ParamType added | DELETED (commit-pending) | ADR 0063 chained-resolution wave |
| HOOK-RATCHET: offset paging ($skip/$top, stop-on-short-page) sibling of totalPages paging | _router http_source | SPENT (ADR 0064) - openfema FOLDED; its ``openfema_disasters.next_page`` hook reuses the ADR 0063 offset-paging PRIMITIVE (gbif's sibling) over ONE combined OData $filter (state OR-clause) with $skip/$top + stop-on-short-page + row cap; the declarative $skip/$top YAML variant was NOT built (the hook primitive serves it - no-op, per "extend minimally + no-op if not") | DELETED/spent (commit-pending) | ADR 0064 |
| HOOK-RATCHET: attribute-feed <- boundary-service FIPS join (openfema<-TIGERweb) | _router transforms/join.py | SPENT-via-enrich (ADR 0064) - openfema's declarations<-TIGERweb FIPS join FOLDED via the chained-resolution PHASE E (enrich_plan emits one TIGERweb county GET per state-in-scope; enrich_merge left-joins county polygons by 5-digit GEOID + bbox-clips). The transforms/join.py REUSE was REJECTED with evidence: join.py is geometry-FIRST single-value choropleth (scope->values->join); openfema is attributes-FIRST multi-field aggregate (n_declarations/sets/flags) with bbox-clip, an inverted control flow that would NOT be a no-op for the census/lehd priors. The enrich pattern IS the general join surface. | DELETED/spent-via-enrich (commit-pending) | ADR 0064 |
| HOOK-RATCHET: bulk-file-behind-an-index (regex-directory->newest->GET + CSV->point) | _router modes | SPENT (ADR 0064) - storm_events FOLDED via the EXISTING resolve phase (ADR 0063), NO new machinery: resolve_build GETs the NCEI directory index (router-owned I/O), resolve_parse regex-scrapes it for the window year(s) + picks the newest processed-date file (pure), build_request GETs each bulk gzip CSV, parse_response decompresses + filters + synthesizes points. The "impure HTML regex" concern resolved: the fetch is the router's, the regex is pure compute over a fetched body (like any resolve_parse) - so it did NOT trip the one-consumer STOP RULE. | DELETED/spent (commit-pending) | ADR 0064 |
| fetch_openfema_disasters twin (fetch_openfema_disasters.py) + test_fetch_openfema_disasters.py | fetchers/hazard | live edge-matrix parity PASS vs twin (RI/VT/DE/bbox value-identical + 5 error codes + NO_DECLARATIONS) -> folded to source.yaml + openfema_disasters hooks (offset paging + TIGERweb FIPS enrich) | DELETED (commit-pending) | ADR 0064 |
| fetch_storm_events_db twin (fetch_storm_events_db.py) + test_fetch_storm_events_db.py | fetchers/weather | live end-to-end PASS (real NCEI index resolve -> newest bulk CSV -> 77 TX-tornado points) + offline parity -> folded to source.yaml + storm_events_db hooks (directory-index resolve + gzip-CSV decode) | DELETED (commit-pending) | ADR 0064 |
| arcgis-odd wave-11 ArcGIS deferrals (fema_nfhl_zones/nwi_wetlands/wdpa_protected_areas/usace_dams/epa_frs_facilities) | fetchers | ALL 5 FOLDED via the EXISTING tier-3 hooks (build_request/next_page/parse_response), ZERO new mode + one opt-in `ingest.chained.tolerate_page_error` flag (ADR 0066): nfhl = OBJECTID-cursor next_page + server sfha/zone-IN() where + tolerate; nwi = WAF-header build_request + offset paging + prefix-strip parse (same-URL esri fallback dropped); wdpa = raise-on-unknown alias build_request + offset paging + client-filter/fail-loud parse; usace_dams = token-resolve + IN() where build_request, KEYLESS->mirror parity (token/authoritative BLOCKED-ON-KEY per ADR 0065); frs = multi-plan build_request (enum->layer set) + by-order union parse (superfund point-from-LAT/LON). The wave-11 "needs a new mode/primitive" verdicts were superseded: every bespoke step is PURE hook compute. Keyed usace_dams degrades to the public mirror (no key is NOT an error). | DELETED (commit-pending) | ADR 0066 |
| fetch_fema_nfhl_zones twin (fetch_fema_nfhl_zones.py) + test_fetch_fema_nfhl_zones.py | fetchers/hazard | live twin-vs-router value-identical (782/65/15/0/3000-across-3-cursor-pages, bad-zone code) -> folded to source.yaml + fema_nfhl_zones hooks (OBJECTID cursor + tolerate_page_error) | DELETED (commit-pending) | ADR 0066 |
| fetch_nwi_wetlands twin (fetch_nwi_wetlands.py) + test_fetch_nwi_wetlands.py | fetchers/hydrology | offline hook parity + earlier live 200 geojson probe (live feature parity BLOCKED-ON-UPSTREAM: USGS host 503 during the run) -> folded to source.yaml + nwi_wetlands hooks (WAF headers + prefix-strip) | DELETED (commit-pending) | ADR 0066 |
| fetch_wdpa_protected_areas twin (fetch_wdpa_protected_areas.py) + test_fetch_wdpa_protected_areas.py | fetchers/biodiversity | live value-identical (6/1) + bad-desig/fail-loud/bbox codes identical (fail-loud confirmed live in isolation) -> folded to source.yaml + wdpa_protected_areas hooks (raise-on-unknown alias + fail-loud) | DELETED (commit-pending) | ADR 0066 |
| fetch_usace_dams twin (fetch_usace_dams.py) + test_fetch_usace_dams.py | fetchers/hazard | live keyless-mirror value-identical (11/2/3) + bad-hazard code identical; token/auth path blocked-on-key -> folded to source.yaml + usace_dams hooks (token resolve + IN() where) | DELETED (commit-pending) | ADR 0066 |
| fetch_epa_frs_facilities twin (fetch_epa_frs_facilities.py) + test_fetch_epa_frs_facilities.py | fetchers/hazard | live value-identical (union 643 / tri 57 / superfund 1) + bad-program code identical -> folded to source.yaml + epa_frs_facilities hooks (multi-plan union + point-from-LAT/LON) | DELETED (commit-pending) | ADR 0066 |
| ZIP-member range-read transport (ADR 0052 defer premise) | _router transport | REJECTED-superseded (ADR 0067): the wave-6 evidence (DEFLATE members force a near-whole transfer; a multi-file shapefile / FileGDB needs every sibling) stands - the honest shape is WHOLE-OBJECT GET + in-memory/tmp extract (the gzip_object precedent at ZIP scale), built as ONE shared step `transport.get_zip` (fetch via get_bytes -> zipfile over BytesIO). No zip-member windowed transport was ever built. | REJECTED-superseded (commit-pending) | ADR 0067 |
| zip/multi-file family (ghsl_population, administrative_boundaries) | fetchers | 2 of 3 FOLDED via WHOLE-OBJECT get_zip (ADR 0067): ghsl = raster `fixed_tile_grid` mode (per intersecting 10-deg tile: get_zip -> read the DEFLATE .tif member in a MemoryFile -> window -> NaN merge); admin = `zip_vector` executor (get_zip -> extractall -> geopandas read shapefile -> bbox spatial-filter -> merge) + admin_boundaries.build_request PURE FIPS place-planner. river_geometry STOP-RULED (below). The shared get_zip step is no-op for all 45 priors. | DELETED/folded (commit-pending) | ADR 0067 |
| fetch_ghsl_population twin (fetch_ghsl_population.py) + test_fetch_ghsl_population.py | fetchers/socioeconomic | live twin-vs-router value-identical (Lagos bounds/min/max/mean/dtype/crs/nodata; EMPTY/INPUT_INVALID codes) -> folded to source.yaml + raster fixed_tile_grid mode | DELETED (commit-pending) | ADR 0067 |
| fetch_administrative_boundaries twin (fetch_administrative_boundaries.py) + test_fetch_administrative_boundaries.py | fetchers/socioeconomic | live value-identical (state 1 / place 43 / county 5, columns identical; EMPTY / LEVEL_INVALID codes) -> folded to source.yaml + admin_boundaries.build_request + zip_vector executor; region_choice._build_region_candidates re-pointed to a self-contained `_admin_boundaries_fgb_bytes` in-process seam (get_spec + validate_params + executor, no cache/publish) | DELETED/re-pointed (commit-pending) | ADR 0067 |
| fetch_river_geometry twin (Overpass-primary + NHDPlus HR HUC4 GDB-zip fallback) | fetchers/hydrology | STOP-RULED / DEFERRED WHOLE (ADR 0067): the PRIMARY path is Overpass QL (waterway-type closed-vocab + POST + JSON->LineString), an Overpass-family shape shared with fetch_roads_osm that no router mode yet covers; the ZIP capability touches only its FALLBACK leg, so folding half a fetcher cannot produce a single byte-identical surface. It is a DO-NOT-REGRESS flood leg (flood.py imports the twin for river-burning); left untouched, no flood-consumer seam re-pointed. Defer to the Overpass-family wave. | QUEUED (Overpass-family wave) | ADR 0067 |
| raster mapserver_export RGBA mode + array_to_cog_bytes RGBA/nodata=None branch | _router/executors/raster_cog.py | BUILT (ADR 0068): a MapServer /export server-symbolized PNG32 -> georeferenced 4-band RGBA COG access mode (service_by_param level map + res_deg grid + transport get_bytes + PIL RGBA decode) + a strictly-no-op array_to_cog_bytes extension (colorinterp="rgba" sets red/green/blue/alpha; nodata=None omits the tag). No-op for all 47 priors (proven: full offline suite green incl test_router_executors + singleband-unchanged test). | BUILT (commit-pending) | ADR 0068 |
| fetch_noaa_slr_confidence twin + shared _noaa_slr_raster.py + test_fetch_noaa_slr_siblings.py | fetchers/ocean | live twin-vs-router VALUE-IDENTICAL parity PASS (4-band RGBA array/crs/transform/dtype equal on real NOAA conf_3ft/conf_6ft; INPUT_INVALID on 3.5/11.0; transparent-still-valid) -> folded to source.yaml + raster mapserver_export mode; shared _noaa_slr_raster.py DELETED (both siblings' data path now the mode) | DELETED (commit-pending) | ADR 0068 |
| fetch_noaa_slr_marsh twin (fetch_noaa_slr_marsh.py) | fetchers/ocean | live value-identical parity PASS (marsh_300/marsh_150; INPUT_INVALID on 0.25) -> folded to source.yaml + raster mapserver_export mode (shared shape with the confidence sibling: one mode, two specs) | DELETED (commit-pending) | ADR 0068 |
| fetch_landcover fold | fetchers/terrain | STOP-RULED / DEFERRED (ADR 0068): needs (1) a DICT-return output contract carrying SFINCS-consumed sidecar fields (nlcd_vintage_year, effective_resolution_m, downsampled) - the router emits ONLY a LayerURI, no dict path; (2) an unbuilt WCS 1.0.0 GetCoverage access mode (via ogc_adapter, not the router transport); (3) a NLCD background(0)->nodata pixel remap + palette-preserving two-path COG-with-overviews translate; (4) auto-coarsen effective-resolution/pixel-budget logic + a _LANDCOVER_CACHE_VERSION salt. Genuinely new machinery, not a no-op extension. | QUEUED (WCS + dict-output wave) | ADR 0068 |
| fetch_topobathy fold | fetchers/ocean | STOP-RULED / DEFERRED (ADR 0068, restated from 0059): a 4-source composite (CUDEM manifest + ETOPO formula-grid + NCEI-regional STAC + 3DEP-via-fetch_dem) each reprojected into a shared NON-4326 UTM target grid + a vertical-datum gate + an extended TopobathyResult(LayerURI) result type. DO-NOT-REGRESS flood leg: flood.py imports fetch_topobathy + TopobathyError directly (a folded surface would need a flood-consumer re-point + FLOOD CANARY). Heaviest; multiple new-machinery gaps. STOP RE-AFFIRMED (ADR 0086, raster-modes wave -- JRC + SoilGrids folded, topobathy genuinely needs more): it is NOT a single-source raster read but a cross-collection PRECEDENCE COMPOSITE with (1) FOUR distinct discovery legs (CUDEM urllist tile-index intersect; ETOPO 15-deg global-block naming; NCEI_REGIONAL_COASTAL_DEMS STAC ItemCollections; 3DEP-land via a NESTED fetch_dem CALL inside the fetcher body -- the stateless router executor composes no nested fetcher tool), (2) a finest-resolution UTM target grid computed ACROSS heterogeneous sources (_compute_target_grid) + a precedence per-source warp merge (_merge_sources_rasterio) + a min_pixel_m floor, (3) a per-tile vertical-datum NAVD88 gate + documented-offset application (_assert_navd88/_classify_vertical_datum), and (4) a TopobathyResult(LayerURI) subclass (bathymetry_present/fallback_warning) NOT registered in LAYER_RESULT_MODELS. STOP RE-AFFIRMED + SHARPENED (ADR 0089, topobathy fold wave -- re-audited at HEAD a0dfed5; the intervening jrc/soilgrids/animation folds added NO composite/route-recursion machinery): the DECISIVE blocker is now a GATE-1 failure, not a judgement call. TopobathyResult's four fields (bathymetry_present/fallback_warning/cudem_tile_count/regional_tile_count) are FETCH-TIME provenance (which of the 4 sources painted the merge), NOT recoverable from the final single-band COG. The router's only post-serialize seam is the envelope hook `_apply_envelope(spec, params, layer, result.data)` -- PURE over final bytes, no I/O -- and on a cache hit `route()` never calls fetch_fn at all, so there is no fetch_fn->envelope provenance channel. A fold therefore cannot reproduce the twin field-for-field (fails edge-matrix parity before any live drive). The delegate-hook escape (`library_delegate` returning (array,transform,crs)) is a forced relabel of ~600 bespoke LOC AND still fails GATE 1. Also confirmed: fetch_dem is still a CODED twin (no dem SPEC), so `route()`-recursion on the topo leg is unavailable. Unblock: a utm_multi_source_composite access mode + a 4-leg source-discovery surface + a nested-tool-composition primitive (or a fetch_dem spec) + a datum-classification step + the TopobathyResult envelope AND a fetch-time provenance channel (bytes+sidecar, cache-replayable) + the flood.py re-point + FLOOD CANARY. Scoped multi-source-composite job, not a fold wave; STOP holds. RESOLVED (ADR 0110, FETCHER FINALE WAVE 1): the fold LANDED. The unblock built = the FETCH-TIME PROVENANCE CHANNEL (the one general capability 0089 named): read_through binds a ProvenanceRecorder around the fetch, the delegate record_provenance()s the four fields, the cache persists them as a <key>.provenance.json sidecar and REPLAYS them on a hit, route() hands them to the envelope hook (additive: recorder=None -> byte-identical for all 91 priors). fetch_topobathy folds onto a library_delegate raster spec + topobathy.validate/read/envelope hooks (the 4-leg CUDEM->regional->ETOPO->3DEP-land UTM warp-merge + NAVD88 datum gate). TopobathyResult -> contracts LAYER_RESULT_MODELS; TopobathyError -> hooks/topobathy.py (base FetchError so invoke's passthrough preserves TOPOBATHY_DATUM_MISMATCH). Twin DELETED (~1,587 LOC); tests -> test_router_topobathy.py (+ channel proofs: cache-hit replay identical, land_absent labeled degrade). Consumers re-pointed (flood.py shim + inundation/wave_field/coastal-test). Registry 173 UNCHANGED; spec-served 91->92; campaign coded-data-fetcher counter 4->3. Baseline EXACTLY 9 by SET; [fetch_topobathy-topobathy] gate member identical pre/post; retrieval unshifted; flood canary run. | DELETED (ADR 0110) | ADR 0089, 0110 |
| fetch_soilgrids fold | fetchers/soil | STOP-RULED / DEFERRED (ADR 0068, re-examined vs multi_url per the wave-9 stop): the multi_url VRT executor is a SAME-CRS mosaic paster (output crs = mosaic crs, NO reproject) - soilgrids needs a projected-window branch (Homolosine native-window read -> rasterio.warp.reproject to EPSG:4326 bilinear, densified bounds) PLUS a per-property Int16->physical scale-divisor directive. The serialize block adds a nodata sentinel but not a reproject or a per-param scale. Real new machinery; wave-9 stop holds. LANDED (ADR 0086): the projected_vrt_window access mode (transform_bounds 4326->source-CRS densified + the twin's floor/ceil + 2 px pad native window + member mosaic + native->4326 bilinear reproject + per-property Int16 scale via projected_window.scale_by_param) + url_template.format(**params) in _resolve_multi_url_members (no-op for hrsl). Live ISRIC parity (Louisiana AOI, clay/phh2o/soc/bdod mask+value+nodata+crs+transform identical, maxdiff 0; ocean agrees on SOILGRIDS_EMPTY). Twin DELETED. | DELETED (commit-pending) | ADR 0086 |
| fetch_noaa_sst fold | fetchers/ocean | HELD / BLOCKED-ON-UPSTREAM (ADR 0068): reachability RETESTED - the ERDDAP base (coastwatch.pfeg.noaa.gov/erddap) is HTTP 200 but the twin's dataset NOAA_DHW is HTTP 404 (.das + info both 404; moved/retired). The griddap bracket-selector fold is build_request-templating + a parse hook, BUT live byte-parity cannot be proven against a dead dataset and the twin itself is broken. HELD stays until the dataset id is re-resolved (a separate upstream-tracking task). | HELD (blocked-on-upstream) | ADR 0068 |
| fetch_3dep_extra fold | fetchers/terrain | STOP-RULED / KEEP (ADR 0068, restated from 0059): access is an OPAQUE pfdf.data.usgs.tnm.dem.read library call that owns its own tile discovery + mosaic + socket (violates the transport-owns-the-socket invariant ADR 0044). Needs a GENERIC library-delegate executor (the dataretrieval_delegate precedent, but generalized to an arbitrary module.callable with arg-mapping) + substring-based error classification + a per-resolution payload-coefficient table PayloadEstimateSpec cannot hold. Not a no-op extension; the dataretrieval_delegate is hardwired to one package. | QUEUED (generic library-delegate wave) | ADR 0068 |
| raster grib_object mode + S3-listed key resolve (weather/GRIB) | _router/executors/raster_cog.py + hooks/mrms_qpe.py | BUILT (ADR 0069): a whole-object `.grib2.gz` GET (transport get_bytes) -> gunzip -> GRIB tempfile decode (GDAL GRIB driver needs a real path) -> source-grid window -> sentinel(-3/-1/<-0.5)->nodata(-9999) -> conditional LCC-if-needed reproject to 4326; the gzip_object precedent (ADR 0055) at GRIB scale. Key discovery reuses the EXISTING single-round resolve phase (ADR 0063/0064): resolve_build emits list-object probes (targeted hourly walkback / latest date-dir listings, all HTTP 200), resolve_parse picks the key + merges _grib_key pre-cache-key. STRICTLY no-op for all 49 priors (new access string + new decode fn). | BUILT (commit-pending) | ADR 0069 |
| fetch_mrms_qpe twin (fetch_mrms_qpe.py) + test_fetch_mrms_qpe.py | fetchers/weather | live edge-matrix parity PASS vs twin VALUE-IDENTICAL (FL 24h / TX 1h / upper-alias 24H array+transform+crs+dtype+nodata equal; latest same-key; 4 error codes byte-identical) -> folded to source.yaml + grib_object mode + mrms_qpe resolve hooks. Consumer re-point: model_nws_flood_event_scenario imported the twin directly -> re-pointed to TOOL_REGISTRY[...].fn resolver + its degrade test re-pointed off MRMSQPEUpstreamError to router_upstream_error. FLOOD CANARY green (run_sfincs_direct status=ok + peak depth COG). | DELETED/re-pointed (commit-pending) | ADR 0069 |
| fetch_hrrr_forecast + fetch_hrrr_smoke fold | fetchers/weather | STOP-RULED (ADR 0069): NOT GRIB2 -- HRRR-Zarr via fsspec.get_mapper + xarray.open_zarr + rioxarray. Gaps: (a) the Zarr store socket is owned by zarr/fsspec, NOT the router transport (the ADR-0044 violation the fetch_3dep_extra STOP named); (b) unbuilt native LCC->EPSG:4326 rioxarray reproject+clip_box raster mode; (c) forecast's derived 10m_wind_speed pulls BOTH UGRD/VGRD + hypot (multi-array synthesis); (d) cycle walkback via fs.exists (s3fs, transport-external). Both are the SAME twin body -> fold together if an HRRR-Zarr mode is built. | QUEUED (HRRR-Zarr opaque-socket wave) | ADR 0069 |
| fetch_nexrad_reflectivity fold | fetchers/weather | STOP-RULED (ADR 0069): a NO-FETCH WMS-URL passthrough (cacheable=False, ttl_class=live-no-cache, source_class=None) -- builds an Iowa-Mesonet WMS GetMap URL string + returns a LayerURI, fetches no bytes, never caches (radar refreshes ~5 min). The router model is fetch-bytes->serialize->read_through->LayerURI and SourceSpec MANDATES source_class(min_length=1)+cache+payload_estimate a live passthrough has none of; no compose-a-URL-and-return-nothing executor exists. Clean STOP, not worth a bespoke no-fetch executor for one source. | KEEP (no SourceSpec home) | ADR 0069 |
| fetch_noaa_nwm_streamflow fold (THE LAST fetcher fold -- campaign closer) | fetchers/hydrology | STOP-RULED (ADR 0069): a multi-source COMPOSITE -- resolve NWM S3 key + whole-object netCDF -> xarray {feature_id: streamflow} LOOKUP DICT + NLDI 5x5-grid spatial-sample (25 point-snap reqs) -> COMIDs + per-reach NLDI geometry (up to 500 reqs) + feature_id JOIN -> point FGB. The 0069 STOP RESOLVED by ADR 0112: the composite is ORDINARY delegate socket I/O (topobathy/storm_tracks precedent) -- one library_delegate VECTOR spec whose nwm_streamflow.read delegate OWNS the S3 read, the NLDI sampling rounds, AND the in-delegate join; the join is plain in-process computation + each NLDI probe is an independent best-effort request (no transport coalescing/retry semantics needed), so the delegate shape hits NO wall (new machinery for one source fails the ADR 0056 bar). Twin fetch_noaa_nwm_streamflow.py DELETED (~869 LOC); source.yaml + nwm_streamflow.* hooks take the name; NWMStreamflowLayerURI joins LAYER_RESULT_MODELS; provenance (NWM reference time + reach count + NLDI sample stats) rides the ADR 0110 channel. CONSUMERS: sfincs_forcing_autowire re-pointed to the registry closure (import-only, grep-verified NO flood seam touched, canary NOT mandated); telemac river_dye's ADR 0102 discharge seam already used seam-1 TOOL_REGISTRY, unbroken (its 5 baseline members identical-in-kind). | DELETED (commit-pending) | ADR 0112 |
| fetch_cama_flood_discharge fold | fetchers/hydrology | STOP-RULED / HELD (ADR 0069): whole-object netCDF -> COG BUT (a) the live source is DEAD -- the U.Tokyo legacy URL returns an HTML migration page (Yamazaki Lab moved; Google-Form+Dropbox gated), so live byte-parity is unprovable (the fetch_noaa_sst HELD precedent) and the twin's primary raises CAMA_FLOOD_UNREACHABLE without a mirror; (b) candidate-filename fallback loop (6 patterns, magic-sniff netCDF-vs-HTML, first-wins) unbuilt; (c) netCDF juggling (var auto-pick + time-mean + lon 0-360 normalize + clip_box + north-up + multi-year concat) exceeds opendap/gzip modes. HELD until a mirror + whole-object-netCDF mode. | HELD (blocked-on-upstream) | ADR 0069 |
| Overpass mode (RequestPlan.data form-body + transport.post_bytes(data=) + http_json ingest.http_source.endpoint_fallback first-success mirror chain) | _router (hooks/__init__ + transport/client.py + executors/http_json.py) | BUILT (ADR 0070): the 3-mirror Overpass fallback = the data-source fallback norm as a first-success-wins endpoint chain (short-circuit on non-429 4xx, advance on 5xx/429/timeout); QL build + geometry/clip decode stay PURE hooks (overpass.py). STRICTLY no-op for all 50 priors (new plan field, new form-body path, new opt-in ingest flag; full router-mode suite 404 green). | BUILT (commit-pending) | ADR 0070 |
| fetch_overpass_pois twin (fetch_overpass_pois.py) + test_fetch_overpass_pois.py | fetchers/socioeconomic | LIVE proof (SF amenity=fire_station -> 24 node+way-centroid Points in-bbox; ocean -> OVERPASS_POIS_NO_FEATURES) + 31-test offline parity -> folded to source.yaml + overpass_pois build_request/parse_response hooks (tag resolve + node/centroid Point + honest-empty) | DELETED (commit-pending) | ADR 0070 |
| fetch_roads_osm twin (fetch_roads_osm.py) + test_fetch_roads_osm.py | fetchers/socioeconomic | LIVE proof (Fort Myers slice: primary mirror 504 -> fallback chain recovered -> 805 named LineStrings, all in-bbox) + offline parity -> folded to source.yaml + overpass_roads build_request/parse_response hooks (highway-class vocab + bbox clip-to-segments); gains the 3-mirror fallback (flagged, strictly more resilient) | DELETED (commit-pending) | ADR 0070 |
| fetch_buildings fold | fetchers/socioeconomic | STOP-RULED (ADR 0070): Overpass-primary polygon source, BUT (a) the "msft" MS/PC fallback is DEAD (abfs:// GeoParquet, never downloads) = flag-not-copy; (b) a click-to-enrich TAGS SIDECAR is an S3 side-channel WRITE (buildings_cache_uri + BUILDINGS_TAGS_SIDECAR_EXT) consumed cross-module by tool_catalog_http /api/building-detail -- the read-through-only router executor has no sidecar-write seam + the consumer needs a re-point. Deferred: sidecar-write executor extension + consumer re-point. | QUEUED (buildings-sidecar wave) | ADR 0070 |
| fetch_river_geometry fold (Overpass-family wave) | fetchers/hydrology | STOP-RULED AGAIN (ADR 0070, deferred here from ADR 0067): its PRIMARY leg (waterway QL) IS foldable via the new Overpass mode, but its FALLBACK (NHDPlus HR HUC4 FileGDB-zip via get_zip + GDB-layer read) is a DIFFERENT executor select_executor cannot chain after http_json (ONE executor per spec; no cross-executor primary->fallback chain). The NHDPlus leg is vestigial (8-envelope heuristic + ~144MB, never reached since OSM is reliable-global-primary). DO-NOT-REGRESS flood leg (flood.py imports the twin) -- left UNTOUCHED, no flood-consumer re-pointed, so NO flood canary required (ADR 0067 posture); the 5 river_dye offline baseline failures remain byte-identical. Unblock: EITHER drop the vestigial NHDPlus leg (NATE flag-not-copy call) OR build a cross-executor fallback + FileGDB zip_vector mode + flood-leg re-point + flood canary. | QUEUED (river cross-executor-fallback wave) | ADR 0070 |
| fetch_field_boundaries fold | fetchers/socioeconomic | STOP-RULED (ADR 0070): NOT an Overpass source (the wave's characterize-its-source target) -- reads PUBLISHED fiboa/FTW GeoParquet from Source Cooperative via fsspec HTTPS + CRS-aware row-group bbox PUSHDOWN (GeoParquet 1.1 covering column + reproject). No Overpass/ArcGIS/ZIP; a partial-GeoParquet range-read transport no router executor covers. Belongs to a future GeoParquet-pushdown wave. | QUEUED (GeoParquet-pushdown wave) | ADR 0070 |
| classify_status hook + stac_float asset_by_param/positive_only + round_4dp quantize | _router (source_spec HookSpec + executors/http_json._get + executors/raster_cog + router._quantize_bbox + registration) | BUILT (ADR 0071): three no-op enablers. classify_status = a keyed transport-status split ((spec,status,body)->RouterError|None, consulted in _get before the default upstream fallback; 401/403->credential-shaped _AUTH_ERROR, 4xx->_INPUT, 5xx->default). stac_float single-param asset map (mobi layer->asset) + transform.positive_only (>0 nodata gate). round_4dp bbox directive (climate_normals byte-identical cache key). STRICTLY no-op for all 52 priors. | BUILT (commit-pending) | ADR 0071 |
| fetch_mobi twin (fetch_mobi.py) + test_fetch_mobi.py | fetchers/biodiversity | LIVE twin-vs-router VALUE-IDENTICAL (Great Smoky Mtns species_richness: shape/dtype/crs/bounds/valid=1258/vmin/vmax/vmean all equal) -> folded to source.yaml + stac_float asset_by_param + positive_only + units_by_param | DELETED (commit-pending) | ADR 0071 |
| fetch_climate_normals twin (fetch_climate_normals.py) + test_fetch_climate_normals.py | fetchers/climate | offline enrich parity (inventory fixed-width filter + cap; per-station CSV enrich drops no-normal stations; drop-all -> CLIMATE_NORMALS_EMPTY) -> folded to source.yaml + climate_normals build/parse/enrich hooks (chained_resolution) | DELETED (commit-pending) | ADR 0071 |
| fetch_ebird_observations twin (fetch_ebird_observations.py) + test_fetch_ebird_observations.py | fetchers/biodiversity | offline keyed parity byte-identical (EBIRD_MISSING_KEY cred-shaped pre-network; bad species/days_back INPUT_ERROR; classify 401->AUTH cred / 404->INPUT / 500->default; tile dedup-by-subId + bbox re-clip) -> folded to source.yaml + ebird build/parse/classify_status hooks (keyed http_json multi-URL) | DELETED (commit-pending) | ADR 0071 |
| fetch_iucn_red_list_range twin (fetch_iucn_red_list_range.py) + test_fetch_iucn_red_list_range.py | fetchers/biodiversity | offline keyed parity byte-identical (IUCN_AUTH_ERROR cred-shaped for missing AND rejected key; bad region IUCN_INPUT_INVALID; classify 403->AUTH / 400->INPUT_INVALID; real+DD-placeholder feature + 200-OK token-envelope->AUTH) -> folded to source.yaml + iucn build/parse/classify_status hooks (keyed http_json single-GET) | DELETED (commit-pending) | ADR 0071 |
| fetch_usgs_groundwater_levels twin (fetch_usgs_groundwater_levels.py) + test_fetch_usgs_groundwater_levels.py | fetchers/hydrology | RE-ATTEMPT CLOSED (dataretrieval refutation superseded): offline enrich parity (selector-gate INPUT_ERROR; measurements NO_WELLS; best-effort monitoring-locations join never drops a reading). Live positive parity BLOCKED-ON-UPSTREAM (OGC endpoint 400s/hangs on the twin's OWN request shape identically for twin+router). Consumer re-point: compute_model_residuals shared-core import -> router seam (get_spec+validate_params+executor) + its bbox-fetch test re-mocked. -> folded to source.yaml + groundwater build/parse/enrich hooks | DELETED/re-pointed (commit-pending) | ADR 0071 |
| fetch_usgs_nwis_gauges fold (RE-ATTEMPT) | fetchers/hydrology | STOP-RULED (ADR 0071, old verdict predated hooks but gaps hold): (a) DERIVED-mode output selection (instant-overlay vs discharge-HYDROGRAPH: different columns/style_preset/units/layer_id switched on window-PRESENCE, not a single param); (b) parse-empty CROSS-PARSER fallback (IV WaterML-JSON -> Site RDB, different decoders); (c) FEEDS sfincs_forcing_autowire (direct twin import). Left ENTIRELY untouched -> NO flood-consumer seam re-pointed -> NO flood canary required. Unblock: derived-mode output selector + parse_fallback mode + flood-leg re-point + MANDATORY canary. | QUEUED (nwis dual-mode wave) | ADR 0071 |
| post-emit ENVELOPE hook (HookSpec.envelope + output.result_model + LAYER_RESULT_MODELS + router._apply_envelope + registration validation) | _router + contracts/execution.py + source_spec.py | BUILT (ADR 0073): reopened the ADR 0056 post_process rejection on the 3+-recurrence ratchet. A pure post-emit hook (spec,params,layer,data)->extra-field dict + a declarative result-type name; router strips uri/layer_type (honesty floor) + builds the named LayerURI subclass. STRICTLY no-op for all 57 priors (asserted). Also datetime_range ParamType (movebank rider, no-op enabler). | BUILT (commit-pending) | ADR 0073 |
| fetch_high_water_marks twin (fetch_high_water_marks.py) + test_fetch_high_water_marks.py | fetchers/hydrology | FOLDED (ADR 0073): proof-by-migration for the envelope seam -- resolve (event name->id) + states-overlap build_request + US-outside gate + bbox-clip/NO_MARKS parse + envelope (quality/type/datum breakdown -> HighWaterMarksLayerURI). error_prefix=HWM reproduces all 4 codes. Redundant event+states fallback DROPPED with proof (bbox subset of overlapping states). Consumer extract_model_at_observations reads FGB quantity column only (no import) -> no re-point. Offline hook parity (18 tests); live positive parity deferred to a live-drive (STN network gate). | DELETED (commit-pending) | ADR 0073 |
| fetch_flood_extent_observation fold | fetchers/hydrology | STOP-RULED (ADR 0073): the envelope closes class_breakdown/flood_area_km2/LegendKey, BUT needs a NEW categorical tiled-mosaic raster mode (per-10-deg tile GeoTIFF -> nearest-window -> first-valid uint8 mosaic -> embedded palette) fixed_tile_grid (continuous NaN-merge) does not express + a LANCE dir-walk date resolve. V&V consumer compute_flood_extent_skill couples by raster SHAPE in a docstring (not import); neither is on the sfincs/flood run path (grep-verified) -> non-fold breaks nothing, NO flood canary. Unblock: categorical tiled-mosaic raster mode + dir-walk resolve. | QUEUED (categorical-tiled-raster wave) | ADR 0073 |
| fetch_fault_sources fold | fetchers/hazard | STOP-RULED (ADR 0073): the envelope closes catalog/fault_count/faults/source, BUT (a) EMPTY-IS-SUCCESS returns a plain NON-LayerURI dict (honesty gate vs "fault lines displayed") + route() always emits a LayerURI (vector-empty header-FGB would fabricate a renderable layer -- the wfigs json-record gap); (b) TWO-TIER cache (whole-world GeoJSON constant-key + AOI vector second entry) read-through single-key does not express -> would re-download 10.6MB per AOI; (c) HARD import coupling -- resolve_fault_sources imports fetch_fault_sources + FaultSourcesError + reads .faults directly (needs re-point). Unblock: non-LayerURI json-record emission + two-tier cache directive + consumer re-point. | QUEUED (json-record + two-tier-cache wave) | ADR 0073 |
| fetch_wfigs_incident fold | fetchers/hazard | STOP-RULED (ADR 0071): returns a bare structured JSON dict (incident_name/lat/lon/bbox/size/containment), NEVER a LayerURI; no SourceShape fits (OutputSpec.layer_type=raster|vector, route() always emits a LayerURI). Unblock: a json-record shape + non-LayerURI emission path. | QUEUED (json-record shape wave) | ADR 0071 |
| fetch_statsgo_soils fold | fetchers/soil | STOP-RULED (ADR 0071): a pfdf LIBRARY delegation (pfdf.data.usgs.statsgo.read owns ScienceBase I/O); endpoints needs a URL there is none + the hook doctrine forbids I/O. Consumers model_debris_flow + compute_sediment_yield import it directly. Unblock: extend ingest.delegate/dataretrieval_delegate to a generic library-delegate executor (the fetch_3dep_extra QUEUED precedent). | QUEUED (generic library-delegate wave) | ADR 0071 |
| fetch_lehd_jobs fold | fetchers/socioeconomic | STOP-RULED (ADR 0071): join(two-source) geometry leg fine, but the VALUES leg is a bulk gzip-CSV whole-object download + block->tract aggregation, not the census Data-API query join.py hardcodes; the ingest.values_query delegation the join comment implies is DEAD/unimplemented. Unblock: a join.values.hook seam (named pure fetch_values override, the storm_events gzip-CSV precedent). | QUEUED (join values-hook wave) | ADR 0071 |
| fetch_population fold | fetchers/socioeconomic | STOP-RULED (ADR 0071): runtime shape-SWITCH by dataset param prefix (worldpop_*->raster download-then-window; acs_*->half-built geometry=None vector, "follow-up" in the twin); SourceSpec is one fixed shape+ext, select_executor never dispatches on a param value. PLUS compute_exposure_summary imports+calls the twin's submodule fn directly. Unblock: drop the half-built ACS branch + fold only WorldPop raster (hrsl multi_url precedent) OR spec variant dispatch; re-point compute_exposure_summary as a companion edit. | QUEUED (variant-dispatch wave) | ADR 0071 |
| fetch_movebank_tracks twin (fetch_movebank_tracks.py) + test_fetch_movebank_tracks.py | fetchers/biodiversity | DELETED (ADR 0077, supersedes the 0071/0073 STOP): folded to source.yaml + movebank_tracks build_request/parse_response/classify_status hooks on the EXISTING keyed http_json path, ZERO new router machinery. Composite user+pass resolved in-hook (kwargs -> `user:pass` secret_ref blob -> TRID3NT_MOVEBANK_USER/PASSWORD env) -> `Authorization: Basic <b64>` header (the resolver blob path; key NEVER registered; missing-creds MOVEBANK_INPUT_ERROR = the parity surface). CSV parse_response (per-geometry_type feature shape + conservative linestring bbox clip); classify_status 401->AUTH/403->LICENSE/4xx->INPUT; per-geometry_type output schema via ingest.properties_by_param; time_range via datetime_range. CONSUMER re-point NOT needed -- compute_movement_trajectory + compute_home_range_kde read the FGB by URI + alias-picked columns (no import); the fold emits BYTE-IDENTICAL schemas (twin-vs-router parity harness: value-identical columns/geometry/props on both geom types + empty header + MOVEBANK_INPUT_ERROR). Live positive path BLOCKED-ON-KEY (401). Value coverage -> test_router_movebank.py (15 tests). Retrieval unshifted (7/7 top-8). Catalog n_specs 62->63. Divergences: int study_id coercion; username/password in cache key when passed as kwargs (env is the deployed path); synthesized layer_id/name (all non-gating). | DELETED (commit-pending) | ADR 0077 |
| fetch_fault_sources fold | fetchers/hazard | STOP-RULED (ADR 0077, twin re-read): NON-empty returns a FaultSourcesResult (LayerURI subclass + categorical legend) from an AOI-keyed vector read_through; EMPTY returns a bare dict = an EMPTINESS-DRIVEN RUNTIME output switch route() (static layer_type) does not express; PLUS a genuine two-tier cache (an INNER constant-key read_through of the whole-world 10.6MB GEM GAF filtered per-AOI + the outer AOI vector entry) the single-read_through + pure-hook model cannot express (a naive fold re-downloads 10.6MB/AOI = a caching REGRESSION); PLUS FaultSourcesResult+legend must move into execution.py LAYER_RESULT_MODELS. resolve_fault_sources already tolerates dict-or-object (trivial re-point once folded). GEM GAF keyless + live 200 (fold live-provable once mechanisms exist). Unblock: output.variant_by_emptiness (or an envelope-return flip-to-record convention) + a router-owned ingest.constant_cache tier + the result-model migration. | QUEUED (emptiness-switch + constant-cache-tier wave) | ADR 0077 |
| fetch_landcover fold | fetchers/terrain | STOP-RULED (ADR 0077, 0068 residuals hold): needs a WCS 1.0.0 GetCoverage templated-GET raster access mode (ogc adapter, not the COG-window transport) + a LayerURI-plus-sidecar output the SFINCS consumer reads (nlcd_vintage_year/effective_resolution_m/downsampled -- envelope can express extra fields but the exact SFINCS attribute contract must be matched + a consumer re-point) + a NLCD background(0)->nodata remap + palette-COG serialize/parse + an auto-coarsen pixel-budget step + a _LANDCOVER_CACHE_VERSION salt. Folding TOUCHES the SFINCS seam -> FLOOD-CANARY-GATED. Left UNTOUCHED this pass (SFINCS seam not re-pointed -> NO canary owed). Unblock: WCS mode + palette-COG + sidecar-envelope + SFINCS re-point + mandatory canary (its own wave). | QUEUED (WCS + palette-COG + flood-canary-gated SFINCS wave) | ADR 0077 |
| fetch_flood_extent_observation fold | fetchers/hydrology | STOP-RULED (ADR 0077, 0073 residuals hold): the envelope closes class_breakdown/flood_area_km2/LegendKey, BUT needs a categorical tiled-mosaic raster access mode (per-10-deg tile -> nearest-window -> FIRST-VALID-wins uint8 mosaic -> embedded palette) as a minimal first-valid VARIANT of transforms/tiled_mosaic.py (fixed_tile_grid = continuous NaN-merge) + a LANCE dir-walk date resolve (latest year/doy, a pre-resolve hook). V&V consumer compute_flood_extent_skill couples by raster SHAPE in a docstring (no import) + neither is on the sfincs/flood run path (grep) -> non-fold breaks nothing, NO canary. Unblock: the categorical tiled-mosaic mode + the dir-walk resolve (its own wave). | QUEUED (categorical-tiled-raster + dir-walk-resolve wave) | ADR 0077 |
| Overpass river mode (overpass_river build_request/parse_response hooks + gates.max_bbox_km2 gate) | _router (hooks/overpass.py + source_spec GateSpec + router._apply_gates) | BUILT (ADR 0074): the river waterway QL + selectable class vocab (alias table + comma/plus/space split + closed-vocab no-injection) as PURE hooks over the existing http_json endpoint_fallback 3-mirror chain (the roads/pois precedent); the twin's 5000 km2 guardrail preserved by a new max_bbox_km2 gate (cos-lat _bbox_area_km2, byte-identical). STRICTLY no-op for all 58 priors (new hooks + opt-in gate). | BUILT (commit-pending) | ADR 0074 |
| fetch_river_geometry twin (fetch_river_geometry.py) + its test block in test_data_fetch.py | fetchers/hydrology | DELETED + NHDPlus HR HUC4 LEG DROPPED (ADR 0074, NATE-decided 2026-08-01): the vestigial NHDPlus fallback (8-envelope bbox->HUC4 heuristic + ~144MB FileGDB-zip, effectively never reached) is a flag-not-copy removal -- a NATE-decided BEHAVIOR change; the OSM Overpass primary is now the ONLY path. Folded to source.yaml + overpass_river hooks. Consumer re-point: flood.py imported the twin directly -> registry seam (TOOL_REGISTRY[...].fn); telemac river_dye + modflow already resolved by name. FLOOD CANARY green (run_sfincs_direct status=ok + depth COG, river fetched LIVE in-pipeline: Overpass 200 -> 18-feature FGB). 5 river_dye offline baseline failures byte-identical in mode (pre-existing _fake_publish mock-arity, not river-induced). Value coverage -> test_router_river.py (15 tests); retrieval unshifted (4/4 corpus phrasings top-8). | DELETED/re-pointed (commit-pending) | ADR 0074 |
| Generic library-delegate mode (HookSpec.delegate + HookSpec.delegate_validate + executors/library_delegate.py invoke/pre_validate/execute + raster_cog access=library_delegate + router select_executor/route + registration validation) | _router + contracts/source_spec.py | BUILT (ADR 0074): the ONE sanctioned impurity -- a hook that owns a library socket -- CONSTRAINED by the invoke wrapper (declared ingest.delegate.timeout_s passed in; telemetry marks the call library-owned; any unmapped library exception -> retryable upstream verbatim, the hook owns input/empty/upstream taxonomy). Vector delegate -> features -> vector_fgb; raster delegate (ingest.access=library_delegate) -> (array,transform,crs) via raster_cog. Generalizes the dataretrieval delegate (ADR 0040), which keeps its own module (routed by library==dataretrieval, no hooks.delegate). STRICT no-op for all 59 priors (dataretrieval routing + count unchanged; daemon import clean). | BUILT (commit-pending) | ADR 0074 |
| fetch_statsgo_soils twin (fetch_statsgo_soils.py) + its statsgo tests in test_pfdf_unlock_statsgo_nldi_3dep.py | fetchers/soil | DELETED (ADR 0074, supersedes the 0071 STOP-RULE): proof-by-migration for the library-delegate mode. Folded to source.yaml + pfdf_statsgo.read delegate (pfdf.data.usgs.statsgo.read -> pfdf Raster .values/.affine/.crs, nodata->NaN) + pfdf_statsgo.validate (twin CONUS envelope pre-cache); field enum router-declarative; units/style-by-field; bbox_area payload. LIVE proof: real ScienceBase KFFACT Kansas AOI -> 376x288, 0.31-0.38, 108k finite px -> valid COG in the pfdf-native Albers CRS (EPSG:5069, value-identical to twin's re-encode). Consumers model_debris_flow + compute_sediment_yield re-pointed to registry seam. Value coverage -> test_router_statsgo.py (12 tests). Divergences: timeout_s dropped from LLM surface -> ingest.delegate.timeout_s=60 (declared-timeout constraint); DEFLATE vs LZW COG compression; synthesized layer_id/name (all non-gating). | DELETED/re-pointed (commit-pending) | ADR 0074 |
| fetch_3dep_extra twin (fetch_3dep_extra.py) + its 3DEP tests in test_pfdf_unlock_statsgo_nldi_3dep.py | fetchers/terrain | DELETED (ADR 0075, condition met): the two ADR 0074 residuals are now DECLARATIVE spec fields -- output.auto_publish (bool, propagated to AtomicToolMetadata.auto_publish in register_spec; false for the role=input DEM intermediate) + payload_estimate.mb_per_sq_deg_by_param (the per-resolution 5/500/5000/1/200 coefficient table, wired into synthesize_payload_estimator's bbox_area branch). Folded to source.yaml + pfdf_3dep.read delegate (pfdf.data.usgs.tnm.dem.read -> Raster .values/.affine/.crs, twin's lowercased-message exception dispatch: NoTNMProducts->EMPTY, tile-limit->INPUT, else UPSTREAM) + pfdf_3dep.validate (twin US envelope pre-cache). Consumers model_urban_flood_swmm + model_landslide_scenario re-pointed to registry seam. Value coverage -> test_router_3dep_extra.py (15 tests). Both extensions strict no-op for the 60 priors (catalog n_specs 60->61). Divergences: DEFLATE vs LZW; timeout_s -> ingest.delegate.timeout_s=120; synthesized layer_id/name (all non-gating). | DELETED/re-pointed (commit-pending) | ADR 0075 |
| fetch_hrrr_forecast + fetch_hrrr_smoke fold (same twin body) | fetchers/weather | STOP-RULED (ADR 0074, supersedes the 0069 STOP): the library-delegate mode covers the fsspec/xarray Zarr open, BUT three residuals: (a) cycle resolution walks S3 (fs.exists) BEFORE read_through to embed the resolved cycle in the cache key -- a delegate PRE-RESOLVE phase doing socket I/O (a cycle=None request would otherwise compute a non-deterministic key); (b) the heavy native LCC->EPSG:4326 rioxarray reproject+clip_box post-array step; (c) forecast's derived 10m_wind_speed opens BOTH U and V components + hypot (multi-array synthesis). Unblock: a delegate socketed resolve-phase (pre-cache-key merge) + a post-array reproject hook + a multi-component delegate return. | QUEUED (HRRR-Zarr delegate finish wave) | ADR 0074 |
| fetch_hrrr_forecast + fetch_hrrr_smoke fold (RE-SCOPED) | fetchers/weather | STOP-RULED (ADR 0075, sharpens the 0074 seam after reading both twins): residuals (b) LCC->4326 reproject + (c) 10m_wind_speed hypot(u,v) both fold INSIDE the delegate read hook (the hook owns the library socket -> returns (array,transform,crs) already in 4326); NO new router mechanism for either. The ONLY missing mechanism is (a): hooks.delegate_resolve -- a SOCKETED pre-cache-key resolve (s3fs fs.exists cycle walk) constrained by library_delegate (timeout+telemetry+upstream backstop) whose dict return merges into params before read_through. Then 2 source.yamls (hrrr / hrrr_smoke; smoke = 2 single-array vars, no multi-component) + 2 delegate hooks. HRRR feeds NO flood seam (grep). | QUEUED (HRRR-Zarr delegate finish wave: hooks.delegate_resolve) | ADR 0075 |
| fetch_wfigs_incident fold | fetchers/hazard | STOP-RULED (ADR 0075): returns a bare JSON dict (point+bbox+discovery record), not a LayerURI -- the router has NO record-return path (OutputSpec.layer_type is raster|vector; route()->LayerURI). Needs the shared record shape (output.layer_type:"record" + a hooks.record dict return, JSON cached via read_through, dict envelope w/ honesty floor). Its resolution is bespoke (~150-line token-OR LIKE builder + 2-endpoint best-feature selection + bbox-from-point + epoch->ISO). Consumer: satellite/frame-animation playbook (grep + re-point on fold). | QUEUED (record-return output-shape wave) | ADR 0075 |
| fetch_fault_sources fold | fetchers/hazard | STOP-RULED (ADR 0075): json-record + empty-is-success dict + two-tier constant/AOI cache + resolve_fault_sources import coupling (test_seismic_real_fault_wiring + OpenQuake workflow). Needs the shared record-return shape PLUS a cache-tier knob (constant vs AOI). | QUEUED (record-return output-shape wave) | ADR 0075 |
| fetch_population fold | fetchers/socioeconomic | STOP-RULED (ADR 0075): runtime OUTPUT-shape switch BY dataset param (a raster leg AND a record/summary leg) + a direct submodule consumer compute_exposure_summary. endpoint_by_param/fan_out express the endpoint switch but not the output-SHAPE switch. Needs the shared record-return shape + a per-param output-shape switch. | QUEUED (record-return output-shape wave) | ADR 0075 |
| fetch_lehd_jobs fold | fetchers/socioeconomic | STOP-RULED (ADR 0075): bulk gzip-CSV LODES download joined on a values-leg (gzip_object + join-in-enrich precedents compose toward it) that emits a record summary. Needs the shared record-return shape + the CSV-join-to-record seam. | QUEUED (record-return output-shape wave) | ADR 0075 |
| fetch_usgs_nwis_gauges fold | fetchers/hydrology | STOP-RULED (ADR 0075): derived-mode output (instant vs hydrograph by window-presence: preset/units/columns switch) + an IV-JSON->Site-RDB parse fallback. FEEDS the flood seam (sfincs_forcing_autowire + flood/flood.py + sfincs_forcing_adapter) -> a fold MANDATES the flood canary. Needs the derived-output switch + the JSON->RDB fallback chain expressed declaratively (or a station-timeseries hook pair). NOT touched this wave (seam untouched -> no canary owed). | QUEUED (station derived-output wave; flood-canary-gated) | ADR 0075 |
| RECORD-RETURN output shape (SourceShape/OutputSpec.layer_type += "record" + HookSpec.record + executors/record.py + router.route()->LayerURI\|dict + registration validation) | _router + contracts/source_spec.py | BUILT (ADR 0076): a bare-JSON-dict output path -- shape=record source caches its hooks.record dict bytes via read_through + route() returns the parsed dict (honesty floor: the record hook raises typed input/empty/upstream, never fabricates success). The record executor walks build_request plans in order + stops at the first non-None record dict (the wfigs Current->YearToDate short-circuit). STRICT no-op for all 61 priors (none are shape=record; layer_type was raster\|vector). | BUILT (commit-pending) | ADR 0076 |
| hooks.delegate_resolve (HookSpec.delegate_resolve + library_delegate.resolve + router pre-cache merge + registration pairing gate) | _router + contracts/source_spec.py | BUILT (ADR 0076): a SOCKETED pre-cache-key resolve constrained by library_delegate (declared timeout + telemetry + upstream backstop + non-dict reject); its dict return merges into params before read_through so a resolved cycle enters the cache key (the ADR 0064e pre-cache-resolve precedent, over a socket). Requires hooks.delegate (validated). STRICT no-op for all 61 priors. | BUILT (commit-pending) | ADR 0076 |
| fetch_wfigs_incident twin (fetch_wfigs_incident.py) + test_fetch_wfigs_incident.py | fetchers/hazard | DELETED (ADR 0076, supersedes the 0075 STOP): proof-by-migration for the record shape. Folded to source.yaml + wfigs_incident.build_request (token-OR LIKE + ordered 2-endpoint Current->YearToDate plans + state/pad validation) + wfigs_incident.record (best-feature-by-size per feed, None->advance, bbox-from-point + epoch->ISO). error_prefix WFIGS_INCIDENT reproduces INPUT_INVALID/UPSTREAM_ERROR/NOT_FOUND. Consumers: NONE functional (frame-animation playbook references by name; registration import -> fold comment). Value coverage -> test_router_wfigs_incident.py (21 tests). Catalog n_specs 61->62. Retrieval unshifted (7/7 top-8). Divergences: twin had no payload estimator -> a tiny clipped bbox_area (never warns); not-found MESSAGE text differs, CODE identical (all non-gating). | DELETED (commit-pending) | ADR 0076 |
| fetch_hrrr_forecast + fetch_hrrr_smoke fold (delegate_resolve BUILT) | fetchers/weather | STOP-RULED (ADR 0076, supersedes the 0075 STOP): hooks.delegate_resolve -- the SOLE missing mechanism ADR 0075 named -- is now BUILT + mechanism-tested. The remaining delegate READ hook (fsspec.get_mapper -> xarray.open_zarr -> rioxarray LCC->4326 reproject + clip + forecast hypot(u,v)) + the fs.exists cycle-walk resolve are an ENTIRELY live-S3 zarr data path with NO offline value-parity fixture -> byte-parity unprovable offline; folding now would delete 2 WORKING twins on unprovable parity (violates offline-first). Twins INTACT; deferred to a live-drive (mechanism done, fold = a mechanical live-parity finish). HRRR feeds no flood seam. | QUEUED (HRRR-Zarr live-parity finish) | ADR 0076 |
| fetch_fault_sources fold | fetchers/hazard | STOP-RULED (ADR 0076): record shape now a satisfied prerequisite, BUT NOT a pure record -- the twin returns a FaultSourcesResult (LayerURI SUBCLASS: a renderable fault-trace vector w/ categorical legend, role=context) on the NON-empty path + a bare dict ONLY on empty = an EMPTINESS-DRIVEN output switch (vector-envelope OR record) the record shape alone does not express; PLUS a two-tier cache (constant world-GAF key + AOI-keyed vector) read_through single-key does not express; PLUS FaultSourcesResult (+ legend) must move into execution.py LAYER_RESULT_MODELS (the HighWaterMarksLayerURI precedent). resolve_fault_sources already tolerates dict-or-object (trivial re-point). Remaining seams: emptiness-switch + two-tier cache + result-model migration. | QUEUED (emptiness-switch + two-tier-cache wave) | ADR 0076 |
| fetch_population fold | fetchers/socioeconomic | STOP-RULED (ADR 0076, premise REFUTED): the tool ALWAYS returns a LayerURI (WorldPop raster leg + acs_* LayerURI(vector) leg); the "record" is only the ACS leg's cached GeoJSON bytes, and that leg is HALF-BUILT (geometry=None "follow-up" + heuristic 15-state FIPS + 9-country ISO3 tables). It is a raster-vs-vector VARIANT dispatch by dataset prefix, NOT a record -- the record shape does not apply. Real fold = WorldPop raster leg + DROP the half-built ACS branch (NATE flag-not-copy call). compute_exposure_summary reads only .uri as a raster (WorldPop) -> re-point trivial. | QUEUED (variant-dispatch wave) | ADR 0076 |
| fetch_lehd_jobs fold | fetchers/socioeconomic | STOP-RULED (ADR 0076, premise REFUTED): returns a LayerURI(vector) tract-choropleth FlatGeobuf (TIGERweb tract geometry LEFT-JOIN LODES WAC bulk gzip-CSV values on 11-digit GEOID), NOT a record summary. Needs the gzip-CSV-values-join-to-vector seam (gzip_object values leg + GEOID join into tract geometry), not the record shape. | QUEUED (join values-hook wave) | ADR 0076 |
| fetch_usgs_nwis_gauges fold | fetchers/hydrology | STOP-RULED (ADR 0076): returns a LayerURI(vector) in BOTH modes; the derived-mode switch (instant vs hydrograph by window-presence) flips style_preset/units/layer_id/COLUMNS (all vector) + an IV-WaterML-JSON->Site-RDB cross-parser fallback. Neither is the record shape nor delegate_resolve -> needs a derived-output selector + a parse-fallback chain. Left ENTIRELY untouched (autowire/flood.py/adapter still resolve the twin) -> NO flood canary owed. | QUEUED (station derived-output wave; flood-canary-gated) | ADR 0076 |
| live-no-cache metadata enabler (synthesize_metadata + register_spec: cacheable = ttl_class != "live-no-cache") | _router/router.py + registration.py | BUILT (ADR 0078): both AtomicToolMetadata synth sites hardcoded cacheable=True, which the cross-field validator REJECTS with ttl_class=live-no-cache -- so the record shape (ADR 0076) could not serve an uncacheable-by-construction source until this 2-line no-op. Now a live-no-cache spec registers cacheable=False + read_through short-circuits it (no bucket write). STRICT no-op for all 63 cacheable priors (none are live-no-cache); the record path's first uncacheable fold (slider_timestamps). | BUILT (commit-pending) | ADR 0078 |
| fetch_slider_timestamps twin (fetch_slider_timestamps/fetch_slider_timestamps.py) | fetchers/imagery | DELETED (ADR 0078): folded to source.yaml + slider_timestamps.build_request/record hooks on the record shape as the FIRST live-no-cache record source. build_request = one GET of latest_times.json via the SHARED _satellite_slider.build_times_url (single source of truth, UNCHANGED -- the animation cluster imports the raw list[int] reader directly, so _satellite_slider STAYS); record = parse+enrich (count/timestamps_int ascending/earliest_iso/latest_iso/cadence_seconds), missing-key/non-JSON -> SLIDER_UPSTREAM_ERROR (byte-identical to SliderUpstreamError.error_code), empty index = valid count 0 (never a fabricated error). Consumers: NONE functional (the registered TOOL had no importers; viirs/goes_animation call the _satellite_slider helper, not the tool; __init__ twin import -> fold comment). No dedicated twin test existed -> value coverage NEW at test_router_slider_timestamps.py (11 tests). LIVE proof: goes-19/conus/geocolor -> 100 frames value-identical to the helper data path (300s cadence, correct ISOs). Retrieval unshifted (6/6 corpus top-8). Catalog n_specs 63->64, registry 190 unchanged. Divergences: a missing required sat/sector/product raises a typed SLIDER_INPUT_ERROR (the twin would TypeError -- typed is cleaner); synthesized layer surface N/A (record) (non-gating). | DELETED (commit-pending) | ADR 0078 |
| fetch_firms_active_fire fold | fetchers/hazard | STOP-RULED (ADR 0078, FOLD-READY): folds onto the EXISTING keyed http_json path (the ebird/movebank precedent) with pure hooks -- build_request resolves the MAP_KEY (kwarg -> secret_ref blob -> TRID3NT_FIRMS_MAP_KEY env -> MISSING_KEY, the resolver-blob path) + builds the area/csv URL; a PURE CSV-text -> GeoJSON-Point parse_response (stdlib csv; features_to_fgb_bytes owns the FGB write); classify_status splits 401/403 -> FIRMS_AUTH_ERROR. ONE residual off the ebird precedent: FIRMS signals an invalid key via a 200-with-error-BODY (not just a non-2xx status), so the auth-body check must live in parse_response (classify_status only fires on a TransportError), a small pure-hook detail. KEY IS LIVE (TRID3NT_FIRMS_MAP_KEY) -> full live parity is provable; deferred to a fold+live-drive session (not landed this pass). credential_registry TOOL_PROVIDER["fetch_firms_active_fire"]="firms" stays name-keyed. Consumers: docstring cross-refs only (no Python symbol importers). | QUEUED (keyed http_json fold + live drive; fold-ready) | ADR 0078 |
| fetch_goes_satellite fold | fetchers/imagery | STOP-RULED (ADR 0078): the S3 list-then-pick-most-recent-key step folds onto the EXISTING resolve_build/resolve_parse pair (the mrms_qpe precedent: S3 list-object probes + regex key-pick, pre-cache-key merge), BUT the READ step has NO matching raster access mode -- rasterio NETCDF:<path>:<var> subdataset open + a separate netCDF4.Dataset CF scale_factor/add_offset/_FillValue read + a POST-warp DN->physical scaling; grib_object (sentinel nodata) is the closest but is GRIB-specific. Unblock: a new raster_cog access mode netcdf_cf_object (generalizing grib_object's whole-object-GET+decode to CF-scaled netCDF vars with post-warp scale/offset). Reused by 4+ GOES siblings (archive_animation/active_fire/glm share the S3-list + grid/COG plumbing). Shared _normalize_satellite/_reproject_and_clip stay.  SUPERSEDED by the ADR 0088 row (refined to the single-band raster-cog residual), RESOLVED there (ADR 0111, the fold LANDED as a library_delegate raster spec). | DELETED (ADR 0111) | ADR 0078 / 0088 / 0111 |
| fetch_landsat_imagery + fetch_sentinel2_truecolor fold | fetchers/imagery | STOP-RULED (ADR 0078): both need a NEW multi-asset RGB-composite STAC access mode the existing stac_search (single-band categorical mosaic) / stac_float (single continuous band) do NOT express -- read up to 4 SEPARATE single-band assets (RGB + a QA/SCL mask asset) per item, apply a categorical/bitmask cloud-shadow-fill mask (landsat qa_pixel CFMask bits 0-4; s2 SCL classes {0,1,3,8,9,10}), then a JOINT 2/98-percentile cross-band stretch to uint8 + DN->reflectance affine (landsat also a thermal inferno-colormap variant). PLUS scene selection: eo:cloud_cover query filter + a coverage-fraction-then-least-cloudy multi-key rank (stac_float select offers only latest/intersect_all). Unblock: a stac_multi_asset_rgb mode (per-asset windowed read + mask-list + joint-stretch directives) + a cloud/coverage scene-ranking selector. Serves 2 (landsat + s2), naip a narrower variant -> 3. FOLDED (ADR 0080): the stac_multi_asset_rgb mode BUILT; both twins -> source.yaml (landsat band_combo recipes incl thermal inferno-LST via role_by_param + units_by_param; s2 SCL-classes mask + all_bands_zero nodata_rule + span_floor 1.0). LIVE PC-STAC parity PASS (pixel-identical vs twin: s2 + landsat true/false-color/thermal) + offline synthetic parity PASS. Twins + tests DELETED. | DELETED (commit-pending) | ADR 0080 |
| fetch_naip fold | fetchers/imagery | STOP-RULED (ADR 0078): closest of the RGB trio but still a NEW mode -- stac_float reads band 1 of one asset into float32/NaN; NAIP needs a single-asset MULTI-BAND uint8 read (loop bands 1..min(3,count) of ONE asset into a stacked uint8 array, implicit-zero nodata, NO scale/offset/mask/stretch). Scene selection is trivial (search_least_cloudy_item first item). CAVEAT: a DIRECT Python consumer compute_canopy_height imports the symbol (await asyncio.to_thread(fetch_naip, bbox)) -> a fold must re-point it to TOOL_REGISTRY["fetch_naip"].fn. Unblock: a stac single-asset multi-band uint8 read mode (or fold with the RGB-composite wave as its no-math variant). FOLDED (ADR 0080): the no-math variant on stac_multi_asset_rgb -- render:passthrough reads bands 1..3 of the one `image` asset straight to uint8 (px_max 8192), most-recent item, all-black -> NAIP_NO_COVERAGE. compute_canopy_height re-pointed to TOOL_REGISTRY["fetch_naip"].fn (+ test); test_compute_canopy_height green. LIVE PC-STAC parity PASS (2226x1872 pixel-identical vs twin). Twin + test DELETED. | DELETED (commit-pending) | ADR 0080 |
| stac_multi_asset_rgb access mode + enum-aliases + OutputSpec.role_by_param | _router/executors/raster_cog.py + router.py + contracts/source_spec.py | BUILT (ADR 0080): a new raster-cog access mode baking a 3-band photometric-RGB COG from PC STAC (N reflectance/raw assets + QA/SCL bitmask/classes mask + joint-2/98 stretch / inferno LST colormap / uint8 passthrough; declarative cloud-cover query + platform filter + coverage/cloud rank via the shared _aoi_coverage helper the ADR 0079 sentinel1 coverage-select was refactored onto). Two no-op contract/router extensions: enum-param `aliases` (band_combo rgb/lst/... -> canonical, no prior enum sets it) + OutputSpec.role_by_param (thermal->primary, mirrors style_preset_by_param). STRICT no-op for the 4 prior stac_float specs (distinct access mode) + every prior spec (extensions unset). | BUILT (commit-pending) | ADR 0080 |
| fetch_sentinel1_sar fold | fetchers/imagery | STOP-RULED (ADR 0078, CLOSEST of the STAC class): folds onto stac_float with the EXISTING asset_by_param (vv/vh) + collection_by_param (sentinel-1-rtc/grd) -- both verbatim-existing -- plus TWO precise gaps: (1) stac_float's tf block does only affine scale/offset -> needs a log10_db transform kind for 10*log10(power) (the -9999.0 non-NaN nodata output is already covered by the existing serialize directive, the copernicus_dem precedent); (2) scene selection needs a coverage-fraction-then-recency multi-key sort + a per-item must-carry-asset pre-filter (stac_float select = latest/intersect_all only). Unblock: a log10_db transform kind + a coverage-ranked stac select mode. | QUEUED (stac log10_db + coverage-ranked-select wave) | ADR 0078 |
| HOOK-RATCHET: FRAMES-LIST (multi-frame) output shape | _router (route() -> LayerURI\|dict\|list[LayerURI]; a new shape:animation_frames or ingest.multi_frame block + per-frame read_through loop + "step <N>" name-token stamp + per-frame graceful-degrade honesty floor) | STOP-RULED (ADR 0078, 5x recurrence, the named animation mechanism): fetch_goes_animation / fetch_goes_blend_animation / fetch_goes_archive_animation / fetch_goes_active_fire / fetch_viirs_day_fire (+ fetch_glm_lightning in its opt-in accumulation_window_s mode) all return list[LayerURI] -- an ordered per-timestamp list the router's single-LayerURI/dict route() cannot produce (fan_out MERGES to one FGB, tiled_mosaic MERGES to one COG -- the OPPOSITE of separate frames). The frames-list shape itself is minimal + opt-in-no-op + serves 5+ sources (clears the ratchet bar), BUT building it ALONE folds ZERO sources: each animation source ALSO needs its unbuilt per-frame COG builder (goes_animation/viirs = SLIDER-stitch mosaic mode; archive/active_fire = the netcdf_cf_object CF-scaled RGB/RGBA composite; glm = numpy point-gridding np.add.at binning + log-ramp). Building the shape with no foldable source is speculative infra (the ADR 0056 post_process bar: a hook nobody can yet use). STOP the whole cluster: frames-list shape + per-source per-frame access modes, each its own wave. Name-token contract: produced NAMES must keep the "step <N>" substring the plugin scrubber groups on (workflows/shared/frames.py precedent); distinct product-label STEM keeps sibling products in separate scrubber groups. BUILT (ADR 0087, animation wave 1): shape:animation_frames added (SourceShape + HookSpec.frames_plan/frame_bytes + FramePlan/FrameDegraded + executors/animation_frames.py + route() branch). The executor owns the per-frame read_through loop + per-frame graceful-degrade (FrameDegraded skip) + the honesty floor (all-frames-degraded -> typed EMPTY); frames_plan resolves the windowed frame set + stamps the "step <N> <ISO>" scrubber name-token, frame_bytes builds one frame's COG. Scrubber-grouping PROVEN via the plugin group_frame_layers over REAL produced names for all 3 folds. STRICT no-op for the 83 priors. Serves goes_animation (single + blend) + viirs_day_fire this wave; goes_active_fire/archive_animation/glm await their per-frame access modes (netcdf_cf_object / point-gridding). | BUILT (commit-pending) | ADR 0087 |
| fetch_glm_lightning fold | fetchers/weather | STOP-RULED (ADR 0078): DUAL-MODE -- a single LayerURI by default, list[LayerURI] when accumulation_window_s is set (the frames-list gap above). Its per-frame body is POINT-GRIDDING math, not reproject: download GLM-L2-LCFA netCDF granules (anonymous NOAA S3, up to 180/bucket) -> concat group_lat/lon/energy -> _bin_ged bins energy onto the EPSG:4326 grid via numpy.add.at (NEVER warp -- GLM lat/lon carries parallax) -> log-scale purple RGBA. A genuinely distinct raster-build shape (point-binning) with no access mode. Unblock: a glm point-gridding access mode + the frames-list shape for the accumulation mode. Shares grid/COG plumbing (_grid_for_bbox/_rgba_array_to_cog_bytes) with fetch_goes_archive_animation. | QUEUED (glm point-gridding + frames-list waves) | ADR 0078 |
| fetch_goes_active_fire + fetch_viirs_day_fire fold | fetchers/imagery | STOP-RULED (ADR 0078): both are thin orchestration over shared per-frame helpers returning list[LayerURI] (the frames-list gap above). goes_active_fire reuses fetch_goes_archive_animation's _fetch_archive_frame_cog_bytes (Matson-Dozier split-window over C07/C13) + _list_archive_keys_in_window -> RGBA hotspot COG per S3 scan (cap 24). viirs_day_fire uses _satellite_slider stitch_slider_mosaic/mosaic_to_cog_bytes per JPSS daytime overpass (cap 144, day/night local-solar-time filter, even-subsample). Neither folds without the frames-list shape + its sibling's per-frame access mode (netcdf_cf_object / SLIDER-stitch). Fold TOGETHER with the animation cluster, not individually. SPLIT (ADR 0087): fetch_viirs_day_fire FOLDED this wave (SLIDER-stitch, its per-frame mode was ready); fetch_goes_active_fire STILL STOPPED -- it reuses fetch_goes_archive_animation's netCDF-band per-frame builder, which awaits the netcdf_cf_object access mode (wave 2). | PARTIAL (viirs FOLDED ADR 0087; goes_active_fire QUEUED netcdf_cf_object) | ADR 0078 / 0087 |
| fetch_goes_animation twin (fetch_goes_animation.py, both fetch_goes_animation + fetch_goes_blend_animation) + test_fetch_goes_animation.py | fetchers/imagery | DELETED (ADR 0087): folded onto shape:animation_frames. source.yaml x2 (fetch_goes_animation dir + new fetch_goes_blend_animation dir, both source_class=goes_animation, error_prefix GOES_ANIM) + shared goes_animation.frames_plan/frame_bytes hooks (SLIDER stitch-mosaic; band in the blend-token set -> the GeoColor+FireTemp composite; a band-less blend-delegate spec defaults to blend). Per-frame cache_params byte-identical to the twin (source_class/ttl/params) -> value-identical parity + reuses cached frames. Docstrings VERBATIM; metadata TWIN-identical (dynamic-1h/goes_animation/cacheable=True/global=False/tier=general). Retrieval unshifted (3/3 corpus phrasings top-8). _satellite_slider UNCHANGED. Value coverage NEW at test_router_goes_animation.py (50 tests incl group_frame_layers scrubber proof + the blend). Divergences (non-gating): bbox=None stamps GOES_ANIM_INPUT_INVALID (twin's bare BBOX_REQUIRED -- same non-retryable actionability); a payload estimator ADDED (twin had none; never warns on a normal fire AOI). | DELETED (commit-pending) | ADR 0087 |
| fetch_viirs_day_fire twin (fetch_viirs_day_fire.py) + test_fetch_viirs_day_fire.py | fetchers/imagery | DELETED (ADR 0087): folded onto shape:animation_frames. source.yaml (source_class=viirs_satellite, error_prefix VIIRS_DAY_FIRE) + viirs_day_fire.frames_plan/frame_bytes hooks (JPSS polar SLIDER stitch + the day/night local-solar-time filter + multi-satellite merge/sort). Per-frame cache_params byte-identical to the twin. Docstring VERBATIM; metadata TWIN-identical (dynamic-1h/viirs_satellite/cacheable=True). Retrieval unshifted. Value coverage NEW at test_router_viirs_day_fire.py (18 tests incl group_frame_layers scrubber proof). Divergence (non-gating): bbox=None stamps VIIRS_DAY_FIRE_INPUT_INVALID (twin's bare BBOX_REQUIRED) + a payload estimator ADDED. | DELETED (commit-pending) | ADR 0087 |
| fetch_goes_archive_animation fold | fetchers/imagery | STOP-RULED (ADR 0087): CHARACTERIZED -- it is AWS-archive-served (raw noaa-goes18 MCMIPC netCDF from S3), NOT SLIDER-tile like the folded pair, so the frames-list shape ALONE does not close it. Its per-frame builder (_fetch_archive_frame_cog_bytes) reads a netCDF subdataset per band, applies CF scale_factor/add_offset/_FillValue, builds the Fire-Temperature/true-color/hotspot RGB-RGBA composites via netCDF4.Dataset + warp -- the exact netcdf_cf_object CF-scaled access mode fetch_goes_satellite ALSO STOPs on (ADR 0078). Unblock: the netcdf_cf_object access mode as a frame_bytes builder (a thin archive per-frame hook over that mode). Pairs with fetch_goes_satellite + fetch_goes_active_fire (shared S3-list + netCDF-band plumbing). The frames-list shape (BUILT, ADR 0087) is the OTHER half it needs. | QUEUED (netcdf_cf_object per-frame builder wave 2) | ADR 0087 |
| PERMANENT VERDICT: fetch_nexrad_reflectivity | fetchers/weather | PERMANENT-BESPOKE (ADR 0078): a ZERO-BYTE WMS-URL passthrough -- the tool makes NO HTTP call; _build_wms_url pure-composes a WMS GetMap service URL (mesonet.agron.iastate.edu/.../wms/nexrad/{product}.cgi, verified live 2026-06-08) the QGIS/MapLibre client renders later. EVERY router executor assumes a fetch that PRODUCES bytes/records to cache + post-process; this tool never touches the network + registers cacheable=False/live-no-cache. No generalizable spec shape (one product family, one hardcoded WMS host). NOT dead. What would re-open a fold: a future display-services / zero-byte-service-URL pool shape (candidate, not built). Stays a coded tool. | PERMANENT-BESPOKE (held row) | ADR 0078 |
| PERMANENT VERDICT: fetch_noaa_sst | fetchers/ocean | REFUTED (ADR 0078): the "dead upstream / HELD" premise is FALSE -- the ERDDAP griddap endpoint (coastwatch.pfeg.noaa.gov/erddap, NOAA_DHW CRW_SST) is LIVE + working (no deprecated/dead language in the twin; SSTNoDataError is honest land/unpublished-date no-data, not upstream death). It is instead a raster_cog FOLD CANDIDATE (single GET .nc bbox+day subset -> xarray/rioxarray NetCDF->COG = the opendap/direct_window raster shape). Not a HELD row; enters the fold backlog. Error codes NOA_SST_INPUT_ERROR/NO_DATA/UPSTREAM_ERROR. Consumers: coastal category only (no Python importers found). | QUEUED (raster_cog NetCDF fold candidate; premise refuted, NOT held) | ADR 0078 |
| PERMANENT VERDICT: fetch_cama_flood_discharge | fetchers/hydrology | HELD (ADR 0078, dead upstream CONFIRMED): the twin's own docstring + CaMaFloodUnreachableError say the kickoff-named U.Tokyo Hydra URL family (as of 2026-02-12) returns an HTML redirect to global-hydrodynamics.github.io; new distribution is GATED (Google-Form registration + Dropbox password). The code is otherwise complete/correct (validated bbox/date/version, HTML-sentinel netCDF sniff, COG conversion). Re-opens the moment a live no-auth mirror URL is supplied via TRID3NT_CAMA_FLOOD_BASE_URL env or base_url= kwarg matching a _candidate_filenames pattern (requires completing the Yamazaki Lab gated flow OR the github.io successor publishing a no-auth path); ticket OQ-0133-CAMA-DATA-SOURCE-MIGRATION. Consumers: SFINCS fluvial forcing (sfincs_forcing_adapter/autowire/builder + flood.py) -> a future fold would be flood-canary-gated. Stays a coded tool (held), NOT folded (no live upstream to prove parity against). | HELD (re-opens on a live no-auth mirror base_url) | ADR 0078 |
| griddap raster access mode (raster_cog.fetch_source_array += access=="griddap" -> _griddap_to_array) | _router/executors/raster_cog.py | BUILT (ADR 0079): an ERDDAP griddap bracket-selector .nc GET (server-side bbox+day subset) -> in-memory xarray subset+squeeze -> north-up float32 (array,transform,crs) -> the executor's NaN-nodata COG serialize. Fully declarative (ingest.griddap: dataset/var_by_param/time_of_day/lat_descending/nodata_body_markers); a 404 with the no-data body markers -> typed EMPTY, else UPSTREAM; an all-NaN window -> EMPTY. STRICT no-op for every prior raster spec. | BUILT (commit-pending) | ADR 0079 |
| stac_float log10_db transform + coverage select (raster_cog._stac_float_to_array: tf.log10_db + select=="coverage") | _router/executors/raster_cog.py | BUILT (ADR 0079): (1) a log10_db transform kind -- 10*log10(power), non-positive/non-finite -> NaN (filled to the dB nodata sentinel by the existing serialize directive), an all-invalid window -> the existing typed EMPTY; (2) a coverage scene-select -- coverage-fraction-then-recency rank with an asset-presence pre-filter (no scene carrying the asset -> typed EMPTY). Both opt-in-no-op for the 3 prior stac_float specs (modis_lst/mobi/copernicus_dem). | BUILT (commit-pending) | ADR 0079 |
| fetch_firms_active_fire twin (fetch_firms_active_fire.py) + test_fetch_firms_active_fire.py + test_firms_historical_date.py | fetchers/hazard | DELETED (ADR 0079, supersedes the 0078 FOLD-READY verdict): folded to source.yaml + firms_active_fire hooks (keyed CSV http_json, key IN the URL path). build_request resolves the MAP_KEY (kwarg -> str secret_ref -> TRID3NT_FIRMS_MAP_KEY env -> pre-network FIRMS_MISSING_KEY); parse_response = stdlib-csv -> Point features + the 200-with-error-body auth check (FIRMS_AUTH_ERROR); classify_status re-applies the body markers on a non-2xx. error_prefix FIRMS + input_error_suffix ARG_INVALID reproduce FIRMS_ARG_INVALID/AUTH_ERROR/MISSING_KEY (credential_registry name-keyed, no re-point). Consumer test_credential_pipeline.py re-pointed (twin exception classes -> local error_code stubs; the removed vault/key_fp resolver tests dropped). Value coverage -> test_router_firms.py (15 tests). Retrieval unshifted (3/3 top-8). LIVE parity BLOCKED (TRID3NT_FIRMS_MAP_KEY absent = a NATE credential step); offline hook parity green. Divergences: vault Persistence + demo fallback + key_fp REMOVED (ebird precedent); a kwarg key enters the cache key (env path keeps it out); stable 12-col empty schema; synthesized layer_id/name (all non-gating). | DELETED (commit-pending) | ADR 0079 |
| fetch_noaa_sst twin (fetch_noaa_sst.py) + test_fetch_noaa_sst.py | fetchers/ocean | DELETED (ADR 0079, supersedes the 0078 refuted-premise verdict): folded to source.yaml + the new raster_cog griddap access mode. Single GET of the ERDDAP bracket-selector .nc (NOAA_DHW CRW_SST/CRW_SSTANOMALY) -> xarray subset -> north-up float32 COG. empty_error_suffix NO_DATA reproduces SSTNoDataError (404 axis-range markers + all-land window). variable enum sst/anomaly -> style_preset_by_param; max_bbox_deg2 25 gate. Value coverage -> test_router_noaa_sst.py (9 tests, incl. the griddap URL/orientation/404-marker/all-land/COG-serialize). Retrieval unshifted (3/3 top-8). LIVE COG parity PENDING (coastwatch.pfeg.noaa.gov ERDDAP timing out, transient upstream; URL byte-correct + transport retries honestly). Divergences: out-of-coverage date -> NO_DATA (ERDDAP 404) vs twin pre-network INPUT_ERROR; default date not in cache key (stac_float precedent); variable aliases dropped; synthesized layer_id/name (all non-gating). | DELETED (commit-pending) | ADR 0079 |
| fetch_sentinel1_sar twin (fetch_sentinel1_sar.py) + test_fetch_sentinel1_sar.py | fetchers/imagery | DELETED (ADR 0079, supersedes the 0078 CLOSEST-STAC verdict): folded to source.yaml + stac_float with asset_by_param (vv/vh) + collection_by_param (rtc/grd + product_aliases) + the new select=coverage + transform.log10_db. error_prefix SENTINEL1 (BBOX_INVALID/POLARIZATION_INVALID/COLLECTION_INVALID/NO_IMAGERY); serialize.nodata=-9999; emit_bbox false (twin omits LayerURI.bbox); units_by_param per polarization. Value coverage -> test_router_sentinel1.py (11 tests: identity, param gates, collection-alias normalization, -9999 serialize round-trip, units_by_param). Retrieval unshifted (3/3 top-8). LIVE PASS: Houston AOI, coverage-select scene, full-valid 2226x1932 EPSG:4326 dB COG -- VV median -7.7 dB, VH median -14.5 dB (textbook C-band, VH<VV). Divergences: co-pol/cross-pol polarization aliases dropped (canonical vv/vh); _pc_sign_two_tier vs sas_sign_href; synthesized layer_id/name (all non-gating). | DELETED (commit-pending) | ADR 0079 |
| constant_cache two-tier cache + variant_by_emptiness output-switch (http_json._fetch_constant_cache + router.route()->_fgb_feature_count + OutputSpec.variant_by_emptiness + registration validation + FaultSourcesResult in execution.py LAYER_RESULT_MODELS) | _router + contracts/execution.py + contracts/source_spec.py | BUILT (ADR 0081): (1) ingest.constant_cache -- an INNER constant-key read_through of a whole-world source file (GEM GAF 10.6 MB) feeding the AOI-filter parse hook, so distinct AOIs share one cached download (proven: 3 AOIs -> 1 download); the OUTER read_through still caches the per-AOI FGB. (2) output.variant_by_emptiness -- route() returns the named hook's record dict INSTEAD of the LayerURI when the produced FGB is feature-empty (fault_sources' honesty gate: a zero-fault AOI is never given a layer); an unreadable FGB counts as non-empty. FaultSourcesResult (+ legend) moved into LAYER_RESULT_MODELS (HWM precedent). STRICT no-op for all 70 priors (none declare either). | BUILT (commit-pending) | ADR 0081 |
| fetch_fault_sources twin (fetch_fault_sources.py) + test_fetch_fault_sources.py | fetchers/hazard | DELETED (ADR 0081, supersedes the 0076/0077 STOP): folded to source.yaml + fault_sources hooks on the two BUILT mechanisms. build_request = ONE GET of the constant GEM GAF file (via constant_cache); parse_response = verbatim-from-twin '(best,min,max)' triple parse + >=2-distinct-vertex + slip>0 gate + bbox filter -> LineString-per-fault ([] on a zero-fault AOI); envelope = kinematic-record reconstruction from the FGB + name + categorical legend -> FaultSourcesResult; empty_record = the variant_by_emptiness degrade dict. error_prefix FAULT_SOURCES reproduces FAULT_SOURCES_INPUT_ERROR/UPSTREAM_ERROR; catalog enum (lowercase, [gem]); payload const 0.2 via bbox_area floor=ceil=0.2. Consumer resolve_fault_sources (openquake) re-pointed to TOOL_REGISTRY["fetch_fault_sources"].fn catching the shared FetchError base (dict-or-object read unchanged); test_seismic_real_fault_wiring.py migrated to the registry-seam swap (10/10 green). TWIN-vs-ROUTER value parity PASS (non-empty records + legend + surface value-identical; empty dict value-identical). Two-tier cache proven. Value coverage -> test_router_fault_sources.py (9 tests). Retrieval unshifted (6/6 top-8). Docstring verbatim (3001 chars). Catalog n_specs 70->71. Divergences: synthesized layer_id (cosmetic); catalog_name always-present on the record (more faithful) -- both non-gating. | DELETED (commit-pending) | ADR 0081 |
| pre_resolve generic pre-cache-key resolve + categorical_tile_grid + wcs_getcoverage access modes (HookSpec.pre_resolve + router.route() merge; raster_cog._categorical_tile_grid_to_array + _wcs_getcoverage_to_array + execute branches; registration validation; FloodExtentObservationResult + LandcoverResult in execution.py LAYER_RESULT_MODELS) | _router + contracts/execution.py + contracts/source_spec.py | BUILT (ADR 0082): (1) hooks.pre_resolve -- a generic (spec,params)->dict pre-read_through HTTP resolve merged into params so a resolved value enters the cache key (the keyless-HTTP sibling of delegate_resolve/resolve_build; LANCE year->doy dir-walk + landcover PURE auto-coarsen). (2) categorical_tile_grid -- direct-GET h/v product tiles + FIRST-VALID uint8 categorical merge + 256-entry palette COG (the categorical variant of fixed_tile_grid). (3) wcs_getcoverage -- WCS 1.0.0 GetCoverage via the shared ogc adapter + NLCD background(0)->nodata remap + palette COG. STRICT no-op for all 71 priors (none declare any). | BUILT (commit-pending) | ADR 0082 |
| fetch_flood_extent_observation twin (fetch_flood_extent_observation.py) + test_fetch_flood_extent_observation.py | fetchers/hydrology | DELETED (ADR 0082, supersedes the 0077 STOP): folded to source.yaml + the categorical_tile_grid access mode + flood_extent_observation hooks. pre_resolve = date/None -> year/doy (dir-walk over the transport when latest, into the cache key); envelope = class_breakdown/flood_area/caveats/categorical legend -> FloodExtentObservationResult. error_prefix FLOOD_EXTENT + empty NO_COVERAGE; the 50 deg^2 guardrail -> gates.max_bbox_deg2. V&V consumer compute_flood_extent_skill couples by docstring (no import) -> NO re-point, NO canary owed. TWIN-vs-ROUTER value parity PASS (observation_date/product/class_breakdown/flood_area/caveats/legend value-identical). Value coverage -> test_router_flood_extent_observation.py (7). Retrieval unshifted (6/6 top-8). Docstring verbatim (2460 chars). Divergences: synthesized layer_id; source-scoped FLOOD_EXTENT_* vs generic errors (both non-gating). | DELETED (commit-pending) | ADR 0082 |
| endgame HRRR + FTW hooks (hooks/hrrr.py + hooks/field_boundaries.py) | _router/hooks | BUILT (ADR 0083): two new hook MODULES, NO new router mechanism -- both ride the EXISTING library_delegate raster/vector paths + delegate_resolve + pre_resolve. hrrr = shared resolve_cycle/read/validate over ingest.hrrr tables (variable->level/s3_var, derived hypot, fill_value); field_boundaries = select (pre_resolve, pure dataset pick) + read (vector delegate, geopandas GeoParquet pushdown). STRICT no-op for all 73 priors. | BUILT (commit-pending) | ADR 0083 |
| fetch_hrrr_forecast + fetch_hrrr_smoke twins (+ test_fetch_hrrr_forecast.py + test_fetch_hrrr_smoke.py) | fetchers/weather | DELETED (ADR 0083, supersedes the 0076 live-parity-finish QUEUED row): folded to 2 source.yaml + the SHARED hrrr delegate hooks on the library_delegate raster + delegate_resolve mechanisms (COMPLETE since ADR 0076; deferred only on live-parity). resolve_cycle = s3fs backward cycle walk (pre-cache-key merge); read = Zarr open -> LCC->4326 reproject + clip + forecast hypot(u,v); validate = CONUS + forecast_hour-horizon. LIVE twin-vs-router value parity PASS (cycle 2026-08-02T05:00Z published-on-S3; VALUE-IDENTICAL all 5 vars incl derived hypot + smoke fill-mask + both validate error edges). HRRR feeds NO flood seam (grep) -> no canary. Value coverage -> test_router_hrrr.py (16). Retrieval unshifted (8/8 + 7/7 top-8). Docstrings verbatim (5773 / 6079). Catalog n_specs 73->75. Divergences: cycle kwarg in cache key when pinned (default None stable); synthesized layer_id; NOT_AVAILABLE via hook-owned RouterError (all non-gating). | DELETED (commit-pending) | ADR 0083 |
| fetch_field_boundaries twin (fetch_field_boundaries.py) + test_fetch_field_boundaries.py | fetchers/socioeconomic | DELETED (ADR 0083, supersedes the 0070 new-pushdown-transport STOP): folded to source.yaml + field_boundaries.select (pre_resolve) + field_boundaries.read (VECTOR library_delegate). The GeoParquet 1.1 CRS-aware row-group bbox pushdown is owned by geopandas.read_parquet over an fsspec HTTPS handle (a library owning its socket, the pfdf/HRRR-Zarr pattern), NOT a router transport -- the 0070 STOP refuted (noaa_sst pattern). LIVE twin-vs-router parity PASS (select auto-pick + empty-AOI + no-coverage/unknown-key error edges value-identical; non-empty pushdown read parity on a rural Story County IA cropland AOI: feature count + total area + crop_name set). Only consumer tools/__init__ import (name-preserving re-point). Value coverage -> test_router_field_boundaries.py (8). Retrieval unshifted (8/8 top-8). Docstring verbatim (3145 chars). Catalog n_specs 75->76. Divergences: twin had no payload estimator -> tiny per_feature (never warns); GDF->GeoJSON->FGB round-trip (geometrically identical); synthesized layer_id (all non-gating). | DELETED (commit-pending) | ADR 0083 |
| fetch_population fold | fetchers/socioeconomic | STOP-RULED (ADR 0083, sharpens 0075/0076): output-SHAPE-and-ext switch (raster .tif WorldPop vs vector .json ACS) by the dataset param at runtime -- no variant-by-param-output-shape mechanism (select_executor cannot dispatch shape on a param value). ACS leg HALF-BUILT (geometry=None, heuristic FIPS/ISO3 tables). Even WorldPop-only needs a whole-object-GeoTIFF-download-then-window raster access mode (WorldPop serves HTTP 200 to range requests -> direct_window /vsicurl cannot window it). Consumer compute_exposure_summary imports it. UNBLOCK: NATE flag-not-copy call to drop the ACS leg (behavior change, the river-NHDPlus precedent) + a whole-object-download-then-window raster mode; OR a variant-by-param output-shape mechanism. | QUEUED (NATE ACS-drop decision + whole-object-window raster mode) | ADR 0083 |
| fetch_usgs_nwis_gauges fold | fetchers/hydrology | STOP-RULED (ADR 0083, sharpens 0075/0076): the FGB PROPERTY SCHEMA switches at runtime by window-presence (_resolve_window: instantaneous scalar _build_flatgeobuf vs hydrograph time_series_csv _build_window_flatgeobuf) -- the excluded output-shape-switch-by-param-value; PLUS a two-tier cross-endpoint/cross-parser fallback (IV WaterML-JSON empty -> Site RDB) that endpoint_fallback does not express. FEEDS the flood seam: sfincs_forcing_autowire.py imports + calls it directly in HYDROGRAPH mode (line 1051/1054) -> a fold MANDATES the flood canary. Left UNTOUCHED (no re-point -> no canary owed). UNBLOCK: derived-output-shape selector + parse-fallback chain + flood-leg re-point + mandatory canary. | QUEUED (station derived-output wave; flood-canary-gated) | ADR 0083 |
| fetch_buildings fold | fetchers/socioeconomic | STOP-RULED (ADR 0083, sharpens 0070): Overpass-primary polygon source (foldable via overpass mode + a polygon parse_response) BUT the blocker is a click-to-enrich TAGS SIDECAR: a boto3 put_object of a .tags.json object keyed off the same cache key as the .fgb, INSIDE the fetch, consumed cross-module by tool_catalog_http.py /api/building-detail (imports BUILDINGS_TAGS_SIDECAR_EXT + _FETCH_BUILDINGS_METADATA to re-derive the key). Router is read-through-only; NO sidecar-write seam. Dead msft/abfs leg stays flag-not-copy. Consumers compute_exposure_summary / sfincs autowire / swmm model call by name (foldable). UNBLOCK: a declarative sidecar-write executor extension (constrained like the delegate) + the /api/building-detail re-point. | QUEUED (buildings-sidecar-write wave) | ADR 0083 |
| fetch_lehd_jobs fold | fetchers/socioeconomic | CHARACTERIZED FOLD-READY / DEFERRED (ADR 0083): TIGERweb tract GeoJSON (paged next_page) LEFT-JOIN LODES8 WAC gzip-CSV values on 11-digit GEOID (gzip_object per discovered state) -> tract choropleth FGB; always LayerURI(vector), no shape switch. Per-state LODES fan-out fits enrich_plan/enrich_merge; the GEOID choropleth fits the join SHAPE but join.py's VALUES leg is hardcoded to the census Data-API, not gzip-CSV -> needs a join VALUES-hook seam (the storm_events gzip-CSV precedent) OR the enrich route wired + a live drive. No NAMED mechanism strictly blocks it. Only consumer tools/__init__ import. Deferred (the one remaining tractable fold outside animation/composite/nwis). | QUEUED (join values-hook / enrich wave; fold-ready) | ADR 0083 |
| fetch_landcover twin (fetch_landcover.py) + the twin-internal landcover test cluster (~995 lines of test_data_fetch.py + the 4 resolution-gate auto-coarsen tests) | fetchers/terrain | DELETED (ADR 0082, supersedes the 0068/0077 STOP): folded to source.yaml + the wcs_getcoverage access mode + landcover hooks. pre_resolve = PURE dataset-alias (nlcd/nlcd_ -> nlcd_2021) + vintage parse + 4000-px auto-coarsen (effective resolution + quantized bbox into the cache key; ESA raises the reserved typed error); envelope = the Manning's-validation sidecar -> LandcoverResult (LayerURI is FROZEN extra=forbid, so the sidecar lives on the subclass -- the twin returned a {layer,sidecar} dict for this reason). error_prefix LANDCOVER; role input; auto_publish false; the 5e6 km^2 ceiling -> gates.max_bbox_km2. Consumer flood.py re-pointed to a module-level fetch_landcover wrapper (registry seam; the flood-scenario tests' patch point) reading .uri + .nlcd_vintage_year (dict-tolerant); test_model_flood_scenario{,_coastal,_v2,_surge_plumbing}.py (85) + test_compute_impervious_surface.py + test_catalog_tools.py migrated + green. TWIN-vs-ROUTER value parity PASS (sidecar + LayerURI surface value-identical; paletted COG pixel-identical -- same class array w/ background(0)->nodata, same colormap, same nodata). FLOOD CANARY GREEN (run_sfincs_direct: live WCS GetCoverage -> paletted COG -> SFINCS read class codes [11..95] vintage 2021 -> solver status=ok -> depth COG). Value coverage -> test_router_landcover.py (7). Retrieval unshifted (8/8 top-8). Docstring verbatim (3708 chars). Divergences: synthesized layer_id; COG via array_to_cog_bytes (COG-driver overviews) vs the twin clip+translate dance (pixel-identical array/palette/nodata, byte layout differs); cache_version salt dropped (twin deleted -> fresh key misses stale COGs); source-scoped LANDCOVER_* errors (all non-gating). CLOSES the ADR 0077 finishers backlog. | DELETED (commit-pending) | ADR 0082 |
| fetch_lehd_jobs twin (fetch_lehd_jobs.py) + test_fetch_lehd_jobs.py | fetchers/socioeconomic | DELETED (ADR 0084, resolves the 0083 FOLD-READY row): folded to source.yaml + the join transform's new `join.values.values_hook = {plan, parse}` seam -- lehd_jobs.values_plan (pure: state FIPS -> LODES WAC gzip GETs) + lehd_jobs.values_parse (pure: per-state gzip-CSV block->tract sum), the join transform owns the I/O (storm_events gzip-CSV precedent, hooks stay pure). 3 join config keys carry the twin surface, all defaulting to census (no-op): variable_param/value_field="segment", extra_props={year}, allow_raw_code=false. error_prefix LEHD_JOBS; role primary; style lehd_jobs_choropleth. LIVE twin-vs-router parity PASS (58 tracts, GEOID sets identical, ZERO per-tract job-value mismatches total+low_wage, total jobs 355328 identical, geometry area identical; ocean-AOI both header-only 8-col empty FGB; unknown segment -> LEHD_JOBS_INPUT_INVALID). Only consumer tools/__init__ import. Value coverage test_router_lehd_jobs.py (25). Retrieval 8/8 top-8. Docstring verbatim (2809 chars). Divergences (non-gating): FGB column ORDER (same SET); synthesized layer_id/name. | DELETED (commit-pending) | ADR 0084 |
| fetch_buildings twin (fetch_buildings.py) + the buildings test block in test_data_fetch.py | fetchers/socioeconomic | DELETED (ADR 0084, resolves the 0083 sidecar STOP): folded to source.yaml + the new overpass_sidecar executor (ingest.sidecar_write) -- the SECOND sanctioned router impurity after library_delegate: fetch (endpoint_fallback) -> buildings.parse (pure: ways->Polygon, relations->(Multi)Polygon, intersects-not-clip, slim props osm_id/osm_type/fid + full tag capture) -> slim FGB + ONE declared .tags.json SIBLING of the .fgb (recomputes the exact read_through key; best-effort + telemetry-marked; honesty floor untouched). Dead msft/abfs leg flag-not-copy. Consumers re-pointed to TOOL_REGISTRY["fetch_buildings"].fn (compute_exposure_summary, sfincs_forcing_autowire obstacles, model_urban_flood_swmm) + /api/building-detail re-derives the sidecar identity from the promoted spec (registration.get_spec, literal fallback). LIVE twin-vs-router parity PASS (slim FGB schema identical; per-fid tag bags IDENTICAL for all 299 common footprints; geometry areas match; sidecar URI exact .tags.json sibling of the .fgb key). Consumer tests green: test_building_detail_http_route, test_inland_building_obstacles, test_model_flood_scenario_surge_plumbing, test_data_fetch, test_compute_exposure_summary, test_run_swmm_local_chain. Value coverage test_router_buildings.py (12). Retrieval 10/10 top-8. Docstring verbatim (2469 chars). Divergences (non-gating): synthesized layer_id/name; empty-AOI -> BUILDINGS_EMPTY (non-retryable) vs twin UPSTREAM_API_ERROR (retryable); a bbox_area payload estimate synthesized (twin had none; SourceSpec requires one). | DELETED (commit-pending) | ADR 0084 |
| fetch_usgs_nwis_gauges fold | fetchers/hydrology | STOP-RULED (ADR 0084, reaffirms 0083): left ENTIRELY untouched -- sfincs_forcing_autowire still resolves the twin directly (HYDROGRAPH mode, line 1051/1054) -> NO flood-consumer seam re-pointed -> NO flood canary owed this wave. Blockers hold: (a) output-shape-switch-by-window (5-field instantaneous vs 12-field hydrograph FGB schema); (b) two-tier cross-parser fallback (IV WaterML-JSON empty -> Site RDB). UNBLOCK: derived-output-shape selector + parse-fallback chain + flood-leg re-point + MANDATORY flood canary. | QUEUED (station derived-output wave; flood-canary-gated) | ADR 0084 |
| CDS library_delegate cds hooks + http_json parse_fallback mode | _router (hooks/cds.py + executors/http_json.py + router.build_layer_uri) | BUILT (ADR 0085): two strictly-additive enablers. (1) hooks/cds.py -- ERA5 (raster) + GTSM (vector) share ONE delegate module (era5.read/gtsm.read + *.validate) reproducing the twins' 4-path key resolution + cdsapi timeout-watchdog retrieve + the missing-key/auth/upstream classifier (ERA5 full phrase list; GTSM narrower classifier reproduced byte-for-byte incl the .cdsapirc-lacks-"key" -> UPSTREAM asymmetry). NO router changes (auth.mode='cds' is declarative; the key check lives in the hook, the ebird precedent). (2) http_json parse_fallback: an ORDERED-plan first-non-empty-parse chain (distinct from endpoint_fallback's first-HTTP-success); all-empty -> the source's typed empty error (NO_STATIONS) instead of a header-only FGB. Plus a build_layer_uri guard (a bbox param present-but-None -> no stamp; strict no-op for priors). | BUILT (commit-pending) | ADR 0085 |
| fetch_era5_reanalysis twin (fetch_era5_reanalysis.py) + test_fetch_era5_reanalysis.py | fetchers/climate | DELETED (ADR 0085): folded to source.yaml + era5.read/era5.validate (library_delegate raster). OFFLINE parity byte-identical (input-validation + missing-key/auth/upstream classification, fake-cdsapi) AND happy-path array VALUE-identical to the twin (mocked ERA5 NetCDF: time-mean 2m_temperature mean 0.077940 == twin; derived 10m_wind_speed hypot(u,v) >= 0). No CDS key present -> live-positive not exercised (missing-key parity is the offline surface). Consumers re-pointed: main.py + tools/__init__ eager imports removed (auto-registered via register_specs_from_tree). Value coverage -> test_router_cds.py. Retrieval unshifted (8/8 top-8). Divergences (non-gating): payload per_station approximates the twin's 0.5*days*area; an explicit api_key/secret_ref kwarg enters the cache key (env/rc path keeps it out, ebird precedent); a str secret_ref only (no SecretRecord-object Persistence path). | DELETED (commit-pending) | ADR 0085 |
| fetch_gtsm_tide_surge twin (fetch_gtsm_tide_surge.py) + test_fetch_gtsm_tide_surge.py | fetchers/ocean | DELETED (ADR 0085): folded to source.yaml + gtsm.read/gtsm.validate (library_delegate vector). OFFLINE parity byte-identical (input-validation + classification incl the reproduced missing-.cdsapirc asymmetry) AND happy-path FGB column+row parity vs twin (synthetic GTSM station NetCDF: 2 in-bbox gauges, identical 11-col schema incl time_series_csv). Consumer re-point: sfincs_forcing_autowire GTSM fallback -> TOOL_REGISTRY[...].fn; test_model_flood_scenario_coastal patch -> registry stub. FLOOD CANARY green (run_sfincs_direct status=ok + depth COG). Retrieval unshifted (7/7 top-8). Same non-gating divergences as ERA5 (payload approximation; kwarg cred in cache key; str secret_ref). | DELETED (commit-pending) | ADR 0085 |
| fetch_usgs_nwis_gauges twin (fetch_usgs_nwis_gauges.py) + test_fetch_usgs_nwis_gauges.py | fetchers/hydrology | DELETED (ADR 0085, resolves the 0084 QUEUED row -- THE LAST FLOOD-SEAM TWIN): folded to source.yaml + usgs_nwis.resolve/build_request/parse hooks on the http_json parse_fallback executor. usgs_nwis.resolve (pre_resolve) derives _mode pre-cache-key -> properties_by_param pins the 5-col instantaneous vs 12-col hydrograph schema + style/units_by_param the per-mode stamps (the output-shape-switch-by-window blocker); build_request emits ORDERED [IV, Site] (instantaneous) / [IV-window] (hydrograph); usgs_nwis.parse self-detects JSON-vs-RDB so a 404/empty IV degrades to Site (the cross-parser fallback blocker); all-empty -> NWIS_GAUGES_NO_STATIONS. LIVE end-to-end parity vs twin (Fort Myers FL): 13 sites both modes SET+COL identical, style/units identical (usgs_gauges/mixed(cfs/ft) + usgs_gauges_hydrograph/ft^3/s). Consumer re-point: sfincs_forcing_autowire fluvial NWIS -> TOOL_REGISTRY[...].fn. FLOOD CANARY green. Value coverage -> test_router_nwis.py (21). Retrieval unshifted (8/8 top-8). Divergences (non-gating): bbox_from_features stamps the features' extent even on a cache hit (twin fell back to the requested bbox); synthesized layer_id/name; a stray bbox passed WITH state_code enters the cache key. | DELETED/re-pointed (commit-pending) | ADR 0085 |
| fetch_jrc_global_surface_water fold | fetchers/hydrology | STOP-RULED (ADR 0085, re-attempt of the pre-palette-machinery rejection): the COLORMAP blocker is RESOLVED -- the twin's computed per-band ramp (_band_colormap: blue occurrence/recurrence, 12-step seasonality, diverging change) is a PURE function of the `band` param alone (no array read, no I/O), so it folds as a `hooks.colormap`-style post-array hook the existing array_to_cog_bytes(colormap=...) serializer bakes (NOT a declarative DSL -> the one-consumer bar does not apply). But the FETCH side genuinely needs MORE: JRC is a CONTINUOUS-value STAC mosaic (BILINEAR resample + per-band nodata sentinel 0/253) served through the PC REST /sign endpoint (_pc_sign_two_tier), whereas the existing stac_search/stac_to_mosaic mode is categorical (NEAREST, single static nodata, token sas_sign_href). LIVE-VERIFIED blocker: sas_sign_href returns HTTP 403 AuthenticationFailed on the jrc-gsw blob container (the token path the twin explicitly rejected). UNBLOCK: a stac_continuous_mosaic access mode (mosaic.resampling bilinear + nodata_by_param + two-tier REST signing) + the pure colormap hook -- a bounded fetch-side follow-up, no longer a colormap problem. LANDED (ADR 0086): the stac_continuous_mosaic access mode + the hooks.colormap field + the jrc_global_surface_water.colormap pure hook. Live PC-STAC parity (Lake Okeechobee, all 4 bands array + palette + nodata + crs + transform byte-identical; dry-AOI agrees on JRC_GSW_NO_COVERAGE). Twin DELETED. | DELETED (commit-pending) | ADR 0086 |
| netcdf_cf_object per-frame builder (hooks/goes_archive.py + imagery/_goes_archive_core.py) + FramePlan.fetch_context/style_preset | _router (hooks + executors/animation_frames.py + hooks/__init__ FramePlan) | BUILT (ADR 0088, resolves the netcdf_cf_object QUEUED at ADR 0078/0087): the raw MCMIPC per-frame builder lands as ONE frames_plan/frame_bytes pair (NOT a new router mode) over the relocated _goes_archive_core substrate (the deleted archive twin's entire reusable body -- S3 window list + CF scale/offset netCDF band read + reproject + Fire-Temp/true-color/hotspot/baked composite + RGBA COG writer). Two strictly-additive FramePlan fields: fetch_context (out-of-cache-key opaque S3 key + raw fetch args -- an archive frame is object-key-addressed, not ts-reconstructable) + style_preset (per-band goes_rgb_animation vs goes_fire_hotspots_rgba; executor uses frame.style_preset or spec.output.style_preset). Both defaulted -> strict no-op for the wave-1 SLIDER frames + the 86 priors. | BUILT (commit-pending) | ADR 0088 |
| fetch_goes_archive_animation twin (fetch_goes_archive_animation.py) + test_fetch_goes_archive_animation.py | fetchers/imagery | DELETED (ADR 0088, resolves the QUEUED at ADR 0087): folded onto shape:animation_frames (source.yaml, source_class=goes_animation, error_prefix GOES_ARCHIVE) via goes_archive.frames_plan/frame_bytes (ingest.archive.mode=full, 144-frame cap). The twin's reusable body moved to imagery/_goes_archive_core.py (no registered tool); the tool + its metadata + register_tool died. Per-frame cache_params BYTE-identical ({bbox, product, satellite, ts_start, gamma:1, res_deg[, bt_c07_min_k, bt_diff_min_k]}) -> value-identical + reuses cached frames. Docstring VERBATIM (6277 ASCII chars); signature + metadata TWIN-identical. Retrieval unshifted (3/3 top-8). LIVE: 3-frame Fire-Temp archive animation (Utah AOI) -> real ISO valid-times, one scrubber group, readback = valid 3-band uint8 EPSG:4326 COG on trid3nt-cache. Value coverage -> test_router_goes_archive.py (23). Divergences (non-gating): bbox=None -> GOES_ARCHIVE_INPUT_INVALID (twin's bare BBOX_REQUIRED); payload estimator ADDED (twin had none). | DELETED (commit-pending) | ADR 0088 |
| fetch_goes_active_fire twin (fetch_goes_active_fire.py) + test_fetch_goes_active_fire.py | fetchers/imagery | DELETED (ADR 0088, resolves the PARTIAL/QUEUED at ADR 0078/0087): folded onto shape:animation_frames (source.yaml, source_class=goes_animation, error_prefix GOES_ARCHIVE) via the SAME goes_archive hooks (ingest.archive.mode=hotspots, fixed fire_hotspots band, 24-frame cap, ~20min default window). Per-frame cache_params BYTE-identical ({bbox, product:fire_hotspots, satellite, ts_start, bt_c07_min_k, bt_diff_min_k, tool:fetch_goes_active_fire} -- the tool param keeps its keyspace distinct from the archive-hotspots key). Docstring VERBATIM (2930 ASCII chars); signature + metadata TWIN-identical. Retrieval unshifted (3/3 top-8). LIVE: 3-frame split-window active-fire run (N.California AOI) -> REAL Matson-Dozier hot-pixel detections, goes_fire_hotspots_rgba, published to trid3nt-cache. Same non-gating divergences as archive. | DELETED (commit-pending) | ADR 0088 |
| fetch_goes_satellite fold | fetchers/imagery | STOP-RULED (ADR 0088, refines the ADR 0078 STOP): the netcdf_cf_object READ plumbing now EXISTS (imagery/_goes_archive_core), but the tool's OUTPUT surface does not fit the frames-list shape -- it returns a SINGLE LayerURI (raster-cog shape, not a per-timestamp list), a SINGLE-band float32 PHYSICAL-units COG (reflectance/K, not the uint8 RGB composite the archive builder produces), most-recent-frame semantics (no window; 15-min valid_time cache-rounding), a CONUS-sector pre-gate, and a non-ASCII em-dash LayerURI name. Unblock: a raster-cog + library_delegate-style access hook returning a single float32 band + a pre_resolve for valid_time rounding + a name-string divergence (em-dash unreproducible under the ASCII rule). A THIRD access surface, not the archive builder. Stays coded; still the home of _normalize_satellite + the S3-list helpers the family imports.  RESOLVED (ADR 0111, FETCHER FINALE WAVE 2): the fold LANDED. fetch_goes_satellite folds onto a library_delegate RASTER spec (ingest.access: library_delegate) + goes_satellite.validate/resolve/read/envelope hooks. The read delegate lists the most-recent MCMIPC key, downloads the netCDF, CF-scales the band, and returns (array, transform, crs) for the shared COG writer (DEFLATE float32 NaN-nodata = twin-format-identical). pre_resolve rounds valid_time DOWN to 15 min + normalizes the satellite token INTO the cache key (twin caching semantics). validate does bbox-required (BBOX_REQUIRED), band/satellite, and the CONUS-sector honest fast-reject (GOES_EMPTY). The em-dash in the LayerURI name is PRESERVED byte-identical (envelope hook, source ASCII via \u2014 escape; one-line follow-up noted in ADR 0111). scan-time provenance rides the ADR 0110 channel (GOESSatelliteLayerURI.satellite/band/scan_time). Shared substrate RELOCATED to imagery/_goes_common.py (satellite normalizer + maps + S3 list/download; the GOES family -- _goes_archive_core, goes_archive/goes_animation/glm hooks -- re-pointed there). GOESError base -> FetchError (invoke passthrough preserves BBOX_REQUIRED/GOES_INPUT_INVALID/GOES_EMPTY). Twin DELETED; tests -> test_router_goes_satellite.py. Registry 173 UNCHANGED; spec-served 93->94. | DELETED (ADR 0111) | ADR 0078 / 0088 / 0111 |
| fetch_glm_lightning fold | fetchers/weather | STOP-RULED (ADR 0088, refines the ADR 0078 STOP): the frames-list LIST-mode is now ready (its accumulation_window_s path is a clean animation_frames fold, point-gridding as frame_bytes), BUT the twin's DEFAULT output is a SINGLE accumulated LayerURI -- and animation_frames ALWAYS returns list[LayerURI], so a pure fold would change the default output contract (layer -> [layer]). Unblock: a single-vs-list output VARIANT on the shape (new machinery for ONE source, the ADR 0056 bar) + the glm point-gridding frame_bytes. Its archive-substrate imports were re-pointed to imagery/_goes_archive_core (grid + RGBA writer); stays coded. | QUEUED (single-vs-list output-variant + glm point-gridding wave) | ADR 0078 / 0088 |
| fetch_population twin (fetch_population.py) | fetchers/socioeconomic | DELETED (ADR 0092, approved-folds wave): NATE flag-not-copy approval dissolved the QUEUED population rows (ADR 0071/0075/0076/0083). The half-built ACS leg (geometry=None follow-up + heuristic FIPS/ISO3 tables) is DROPPED -- an acs_* dataset now fails the validate gate with POPULATION_INPUT_INVALID (fetch_census_acs serves tract population). The WorldPop raster leg folds onto the EXISTING library_delegate raster mode (no new access mode): worldpop.validate (pre-cache vintage gate) + worldpop.read (whole-object-download-then-window socket, WorldPop has no range support). source.yaml took the twin's name; coded fetchers -1. | DELETED (commit-pending) | ADR 0092 |
| fetch_glm_lightning twin (fetch_glm_lightning.py) | fetchers/weather | DELETED (ADR 0092, approved-folds wave): NATE-approved output CONTRACT CHANGE dissolved the ADR 0078/0088 single-vs-list STOP -- the default output becomes a frames LIST (single accumulation = a one-frame list; accumulation_window_s fans into N step-frames). Folds onto shape:animation_frames via glm.frames_plan (bucket resolve + step-N name-token + byte-identical cache_params) + glm.frame_bytes (GLM-L2-LCFA GROUP-energy point-gridding numpy.add.at over the shared imagery/_goes_archive_core grid+writer; FrameDegraded per empty bucket, GLM_EMPTY when all degrade). No new single-vs-list machinery (N=1 carries the single case). source.yaml took the twin's name; coded fetchers -1. | DELETED (commit-pending) | ADR 0092 |
| fetch_dem fold | fetchers/terrain | STOP-RULED / QUEUED (ADR 0090, DEM+STORM_TRACKS wave): a 3DEP-primary + Copernicus-GLO-30-fallback multi-source COMPOSITE, not a single-source raster read. Four blockers at HEAD 183c653: (1) a cross-registered-tool FALLBACK LADDER with FETCH-TIME provenance restamp -- on a 3DEP outage/DemPrimaryTimeoutError fetch_dem delegates to a DIFFERENT tool (fetch_copernicus_dem) and restamps LayerURI.name + the native LayerURI.fallback_note; the router's fallback surfaces are SAME-source mirror/parse chains (vector_fgb endpoint chain, http_json endpoint_fallback/parse_fallback) that produce ONE output with NO name/fallback_note restamp and cannot delegate to another registered tool, and a cache HIT never runs fetch_fn (provenance unrecoverable) -> GATE 1 fail, the ADR 0089 envelope-provenance gap again; (2) the 3DEP leg is bespoke with NO router surface -- py3dep.get_dem (the seamless 1/3-arc-sec path, a DIFFERENT library than fetch_3dep_extra's pfdf tnm.dem, so the existing pfdf library_delegate does NOT serve it) wrapped in a hard wall-clock bounded-timeout DAEMON thread (DemPrimaryTimeoutError, abandon+discard) + a partial-coverage gate (reproject returned bounds to WGS84, assert 4-edge coverage, raise DemPartialCoverageError -- a RETRYABLE UpstreamAPIError subclass the urban 1m->10m ladder catches); a hook is PURE (no I/O, ADR 0056) so neither the reproject-coverage check nor the timeout thread is a pure hook, and no declarative bounded-timeout/coverage-gate directive exists; (3) source-enum pin (auto/3dep/copernicus) + pixel-budget auto-coarsen stamping the DELIVERED resolution into name + continent ceiling; (4) WIDE direct-import blast radius -- 8 consumers import the FUNCTION directly (flood.py, fetch_topobathy nested 3DEP-land call, compute_contours, extract_model_at_observations, run_elmfire, model_dambreak_geoclaw_scenario, model_urban_flood_swmm, model_landslide_scenario) + __init__/main; a registration-fold + module-delete breaks all 8 unless the core stays importable (hollow relabel, LOC delta ~0) or all 8 re-point to TOOL_REGISTRY (wide high-risk repoint, zero gain). flood.py + fetch_topobathy are DO-NOT-REGRESS flood legs (a real fold mandates a re-point + FLOOD CANARY). Does NOT unblock the ADR 0089 topo leg: the partial dem spec already exists (fetch_copernicus_dem + fetch_3dep_extra); the missing py3dep-seamless leg would NOT reproduce fetch_dem's full auto-fallback+coverage contract that fetch_topobathy depends on. UNBLOCK: a cross-tool fallback-with-restamp seam (cache-replayable) + a py3dep library_delegate hook + a bounded-wall-clock-timeout directive + a coverage-gate surface + the source pin + auto-coarsen + the 8-consumer re-point + FLOOD CANARY. Scoped multi-source-composite job, not a fold wave. UPDATE (ADR 0091, gated-fallback wave 2026-08-03): BLOCKER 1 (the cross-registered-tool fallback ladder with fetch-time provenance restamp) is DISSOLVED -- the auto path no longer calls fetch_copernicus_dem or restamps name/fallback_note; on a 3DEP outage it raises a user-gated DemAutoFallbackGateError (+ DemOutOfCoverageError for non-US), so there is no longer a cross-tool restamp to reproduce. REMAINING residual for a fold: blockers 2-4 stand -- the bespoke py3dep-seamless leg + bounded-timeout DAEMON thread + reproject-bounds coverage gate (DemPartialCoverageError) with no pure-hook/router surface, the source-enum pin + pixel-budget auto-coarsen, and the WIDE 8-consumer direct-import blast radius (flood.py + fetch_topobathy nested + 6 more). The FOLD itself is NOT this wave's job; still QUEUED as a scoped composite job. UPDATE (ADR 0096, dem-fold wave 2026-08-03, re-audited at HEAD 736140a): the fold was attempted and STOPS. BLOCKER 2 CLEARS -- ADR 0090's "a hook is PURE (no I/O)" was over-broad; the library_delegate DELEGATE hook is the sanctioned socket-owning impurity (ADR 0074), so py3dep.get_dem + the bounded-timeout daemon-thread watchdog (DemPrimaryTimeoutError, hook receives timeout_s) + the reproject-bounds partial-coverage gate (DemPartialCoverageError) ALL live in a new py3dep.read delegate hook, sibling of pfdf_3dep.read (COG-bytes LZW/5070 vs router DEFLATE = the accepted ADR 0074 divergence class). DECISIVE STANDING BLOCKER = source-enum blocker 3, specifically the source="copernicus" leg: the twin returns TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox) VERBATIM (copernicus_dem-cached uri + copernicus_dem layer_id/name), and the router has NO cross-registered-tool dispatch seam -- route() emits ONE LayerURI stamped with THIS spec's source_class ("dem"); access is a spec constant (no access_by_param); a delegate re-serializes+re-caches under "dem" -> divergent uri/layer + double-caches the GLO-30 mosaic the 3 other copernicus consumers read; grep confirms zero cross-tool dispatch in _router/. LOAD-BEARING: DemAutoFallbackGateError/DemOutOfCoverageError NAME source="copernicus" as the user retry, so it MUST work on the same tool -- cannot drop/diverge. Secondary residuals: naming (layer_id="dem-{lon}-{lat}-{Nm}"/name="USGS 3DEP DEM (Nm)"+coarsen note) needs the envelope hook which registration forces paired with output.result_model (a no-field DemLayerURI subclass or a pairing relaxation); the auto-coarsen itself IS a pure pre_resolve hook; the Dem*Error classes (UpstreamAPIError, NOT RouterError) are WRAPPED by library_delegate.invoke's RouterError-only passthrough, dropping the test_data_fetch-pinned codes unless invoke broadens to except FetchError. SHARPENED UNBLOCK: a hooks.dispatch(spec,params)->LayerURI|None pre-flight short-circuit (the FIRST tool-composes-tool router pattern -- a NATE architecture decision, against the atomic-tools doctrine) + the naming seam + the invoke FetchError passthrough = three new-machinery pieces = a scoped job, not a fold. No code change; counter stays 5. RESOLVED (ADR 0097, DEM completion wave 2026-08-03): the fold LANDED. All three residual machinery pieces built -- (1) the cross-sibling pre-flight DISPATCH seam (spec.dispatch + router.try_dispatch: single fixed target, spec-declared, NO chains, pre-flight-only; source="copernicus" returns fetch_copernicus_dem's layer VERBATIM, no dem-prefix double-cache, proven offline + live); (2) library_delegate.invoke/resolve broadened RouterError->FetchError passthrough so the Dem*Error pinned codes survive (Dem*Error home relocated to hooks/dem_3dep.py; no other delegate source's wrap-to-generic changed); (3) a no-field DemLayerURI result_model + dem_3dep.envelope naming override (less invasive than relaxing the envelope/result_model pairing validator). The py3dep leg is the dem_3dep.read delegate hook (get_dem + env-tunable daemon-thread watchdog + reproject-bounds coverage gate + source-conditional gating), auto-coarsen is the dem_3dep.coarsen pre_resolve (cache key twin-identical when not coarsened), continent ceiling + out-of-coverage are dem_3dep.validate. fetch_dem.py twin DELETED; DEM tests migrated to test_router_dem.py (0091 pins intact); 8 consumers re-pointed to the registry closure. Registry 175 UNCHANGED; coded tools 85->84, coded fetchers 7->6, spec-served 90->91; campaign coded-data-fetcher counter 5->4. The 0090->0091->0096->0097 chain closes. | DELETED (ADR 0097) | ADR 0090, 0091, 0096, 0097 |
| fetch_topobathy internal source fallbacks vs the gated-fallback norm | fetchers/ocean | FOLLOW-UP CHARACTERIZATION (ADR 0091, no build): audit topobathy's OWN internal fallbacks against the 2026-08-03 loud/user-gated norm. TWO silent behaviors observed. (a) NESTED LAND LEG: `_fetch_3dep_land_to_file` wraps `fetch_dem` in `except Exception -> return None` (best-effort), so the new DemAutoFallbackGateError is SWALLOWED and the merge proceeds bathy-only; crucially the LAND-ABSENT case stamps NO `fallback_warning` (the warnings only cover bathy-absent + global-fallback-bathy), so a 3DEP-land outage now silently drops land with no user signal -- a silent DEGRADE (previously it silently got Copernicus land, whose provenance topobathy already discarded by reading only `.uri`). (b) `fallback_warning` field: for CUDEM-absent it substitutes GLOBAL ETOPO 2022 (~450 m, EGM2008/MSL vs NAVD88) with a loud warning -- that IS a cross-dataset substitution but it is NOT user-GATED (it proceeds and warns, does not pause-and-ask). Per the norm both should become loud + user-gated (or the land leg should surface a warning). CONDITION-to-align: the fetch_topobathy multi-source-composite job (already QUEUED, ADR 0089) folds in the gated-fallback norm for its CUDEM/ETOPO/regional/3DEP-land legs; characterize-only here, no build this wave. RESOLVED (ADR 0110): both landed in the topobathy fold. (a) the land leg's SILENT swallow is now a LABELED land_absent degrade -- a provenance entry + a fallback_warning naming land_absent (BATHYMETRY-ONLY, onshore nodata), never a silent land drop. (b) the CUDEM->ETOPO substitution stays PROCEED-AND-WARN (GLOBAL-FALLBACK bathymetry) and the warning is verified to reach the envelope on EVERY path via the provenance channel (a cache hit carries it too). NOT hard-gated: coastal flood scenarios depend on best-effort terrain, so labeling (not pause-and-ask) is the 0091-agreed treatment for this consumer (documented in ADR 0110). | RESOLVED (ADR 0110) | ADR 0091, 0110 |
| fetch_storm_tracks fold | fetchers/weather | STOP-RULED / QUEUED (ADR 0090, DEM+STORM_TRACKS wave): the ADR 0089 worklist filed it under "track/line assembly (fetch_storm_tracks, fetch_movebank_tracks)" -- STALE, since fetch_movebank_tracks is already folded (vector-fgb + build_request/parse_response/classify_status hooks + properties_by_param linestring/point switch), so per-storm LineString grouping is PROVEN foldable. The HISTORICAL IBTrACS mode alone WOULD fold on http_json + hooks exactly like movebank (build_request = the 1-2 selected per-basin CSV plans; parse_response = multi-file CSV parse + units-row skip + spur drop + season/name filter + USA_WIND->WMO_WIND + wind-structure columns + storm-wise full-track bbox selection + line/point assembly; classify_status; properties_by_param; StormTracksNoStormsError -> router_empty_error). DECISIVE BLOCKER: the tool is ONE registered name with an active_only param and must reproduce BOTH modes field-for-field. The ACTIVE mode is a resolve-then-fetch chain whose SECOND round is a BINARY zip-shapefile -- fetch NHC CurrentStorms.json, then per active storm fetch forecastTrack.zipFile (URL from the primary JSON), extract *_pts.shp to a tempdir, geopandas.read_file + reproject to 4326, append forecast points (tau_h/is_forecast). chained_resolution's enrich fetches detail bytes via the shared transport but the enrich_merge hook that DECODES them is PURE (no I/O, ADR 0056); a zip-shapefile decode needs tempdir extractall + geopandas.read_file (file I/O) + reprojection -- I/O in a pure hook, no router surface. Dropping the enrichment changes the output -> GATE 1 fail. Two different fetch SHAPES under one name (multi-file-CSV vs JSON+per-storm-binary-zip). Consumer: only sfincs_forcing_autowire (autowire.py:838, SFINCS spiderweb wind-forcing) imports it directly -- narrow, but does not rescue the fold; seam UNTOUCHED so no canary. UNBLOCK: a binary-secondary-enrichment mode (per-item secondary fetch whose body is a zip-shapefile decoded+reprojected by an I/O-permitted delegate step, cache-consistent) OR a library_delegate-style whole-tool delegate carrying both modes (forced relabel of ~500 LOC, LOC delta ~0 -- the topobathy rejection). Historical-only folds cleanly but is not a legal fold of the single-named tool.  RESOLVED (ADR 0111, FETCHER FINALE WAVE 2): the fold LANDED. The DECISIVE blocker (the active-mode BINARY zip-shapefile second round) is expressed as the SANCTIONED delegate socket impurity, NOT a new executor phase (the topobathy precedent): fetch_storm_tracks folds onto a library_delegate VECTOR spec + storm_tracks.validate/resolve/read/envelope hooks, where storm_tracks.read OWNS both network rounds (historical IBTrACS CSV storm-wise full-track selection -> line/point features; active NHC CurrentStorms.json -> per-storm forecastTrack.zipFile -> tempdir extractall -> geopandas.read_file -> reproject 4326) and returns GeoJSON features for the shared vector_fgb serializer. DESIGN CHOICE = option (b): a delegate-mode spec whose read hook does BOTH rounds (simpler than a new chained_resolution binary-enrich phase; NATE simplicity doctrine). validate = historical bbox-required + geometry/storm_name shape; resolve (pre_resolve) = storm_name canon + season-window resolution INTO the cache key; envelope = the storm-tracks-{seed} id + 'Storm tracks - <mode> (<scope>)' name + the mode/storm-attribution provenance replayed from the ADR 0110 channel (StormTracksLayerURI). StormTracks*Error -> hooks/storm_tracks.py base FetchError (invoke passthrough preserves STORM_TRACKS_NO_STORMS etc.). Twin DELETED; tests -> test_router_storm_tracks.py; sfincs_forcing_autowire re-pointed to the registry closure (import-only, no flood seam, no canary). Registry 173 UNCHANGED; spec-served 92->94 (with goes_satellite); campaign coded-data-fetcher counter 3->1. | DELETED (ADR 0111) | ADR 0090 / 0111 |
| 10 engine-door concierge tools (run_sfincs/run_swmm/run_modflow/run_telemac/run_swan/run_geoclaw/run_landlab/run_openquake/run_elmfire/run_pelicun) + their corpus.yaml | tools/simulation/<engine>/run_<engine>/ | ADR 0094 (NATE-approved 2026-08-03, FULL dissolve): retrieval + per-tool corpus.yaml route engine templates directly, so the concierge hop is redundant. tier=template rejoins the pool; the doors are deleted. 20/20 templates surface top-8 model-free; pre-existing surface rank-stable. | DELETED (commit-pending) | ADR 0094 |
| Engine-door gate-expansion machinery (_engine_door_tool_names, _DOOR_EXPAND_CAP, the door branch of the discovery-expand block, the templates-key result reader) + the three tier=template pool-exclusion filters (search_tools index / tool_retrieval fail-open / server _default_declarable) | server.py + search_tools.py + tool_retrieval.py | ADR 0094: with the doors gone the gate-expansion side effect that made templates callable only post-door is dead; templates are ordinary pool members. | DELETED (commit-pending) | ADR 0094 |
| 10 door test files (9 test_*_door.py + test_engine_door_gating.py) | server/tests | ADR 0094: the doors they cover are deleted; replaced by test_door_dissolution.py (callability + retrieval matrix). | DELETED (commit-pending) | ADR 0094 |
| Dead shared/model_satellite_fire_animation/ dir | workflows/shared | empty dead dir (only __pycache__); no template, no importer. | DELETED (commit-pending) | ADR 0094 rider |
| TEMPLATE_CARD exports (20 template modules) + workflows/<engine>/_template_card.py (10 files) | workflows | now-unconsumed: the deleted engine doors were the only reader of TEMPLATE_CARD (via _derive_card). Valid inert dataclasses; churn deferred. CONDITION: a hygiene batch strips the TEMPLATE_CARD blocks + deletes the _template_card.py files. | QUEUED | ADR 0094 |
| _LEX_REINFORCE_GATE_DOOR + the tier=="door" branch of _lexical_reinforcement + its test | search_tools.py + test_search_tools.py | now-inert: no tool carries tier=door, so the wider door lexical-champion gate never fires. It is a ranking knob, not the concierge pattern. CONDITION: a retrieval-tuning pass collapses the gate to the general constant + drops the door-tier test case. | QUEUED | ADR 0094 |
| fetch_cama_flood_discharge (module + test + corpus + registry/catalog/credential refs) | fetchers/hydrology | DELETED (ADR 0095, hygiene wave, NATE-decided): supersedes the ADR 0078 PERMANENT-BESPOKE HELD verdict (line 145) and the ADR 0069 HELD row (line 83). Rationale: US-only doctrine + NWM covers US rivers, and the upstream is a dead registration-gated U-Tokyo mirror (no live no-auth source to prove parity against). Removed: module dir + test_fetch_cama_flood_discharge.py + corpus.yaml; registry import (tools/__init__.py) + main.py registration + categories.py CATEGORY_MAP/description + server.py _ALWAYS_OFFLOAD_SYNC_TOOLS entry + adapter.py error-class enum mention + scripts/gen_tool_support_page.py credential map + test_gemini_kwargs_fuzz sample-args + ALL sibling docstring cross-refs (NWM / NWIS / GTSM / sfincs_builder / flood.py / autowire / deck.py). Registry 176->175, coded tools 86->85. REOPEN only on a live no-auth CaMa mirror + an actual global-scope need. | DELETED (commit-pending) | ADR 0095 |
| SFINCS CaMa-COG forcing-consumption path (discharge_forcing_from_cama_cog + cama_cog / cama_cog_uri plumbing) | workflows/sfincs/sfincs_forcing_adapter.py + sfincs_forcing_autowire.py + sfincs_builder.py + flood.py | QUEUED (ADR 0095): orphaned by the fetch_cama_flood_discharge deletion -- no registered tool now produces a CaMa COG, so this generic single-band-discharge-COG sampler is unreachable via the agent. RETAINED (not excised) this wave: removal is engine-seam SFINCS work touching the flood solver path (flood-canary-risky) and outside the enumerated cama-deletion scope; NO test couples to it. CONDITION-to-delete: NATE approves excising the CaMa forcing path (adapter fn + params + autowire branch + docstrings) under a flood canary, OR it is generalized to a named external-discharge-COG hook. reopen: CaMa (or another global discharge-COG fetcher) is re-added. CONDITION MET: NATE approved the M1 RIDER excision under the flood canary (ADR 0098). DELETED: discharge_forcing_from_cama_cog + __all__ entry + build_surge_forcing cama_cog/bbox param + branch + guard, autowire import + cama branch + docstring, flood.py comment, all cama docstrings; zero test coupling. Flood canary green. | DELETED (ADR 0098) | ADR 0095 -> 0098 |
| fetch_nexrad_reflectivity RECLASSIFY + RENAME -> show_nexrad_radar | fetchers/weather -> tools/display/show_nexrad_radar | RECLASSIFIED (ADR 0095, hygiene wave, NATE-decided): the ADR 0078 PERMANENT-BESPOKE verdict (line 143) STAYS TRUE (a zero-byte WMS-URL composer, coded, no SourceSpec home) -- it was just MISCATEGORIZED as a fetcher. Moved out of the fetchers package into the new tools/display/ home (honest: it composes a live WMS GetMap URL and transfers no data bytes), renamed the registered tool fetch_nexrad_reflectivity -> show_nexrad_radar (registry name + docstring rewritten honestly + corpus re-authored + all consumers/tests re-pointed). NOT a deletion: registry count UNCHANGED (rename). Retrieval proof: show_nexrad_radar surfaces top-8 model-free for all 4 radar queries (parity with the pre-rename name). | RECLASSIFIED (commit-pending) | ADR 0095 |
| model_fire_spread_scenario | workflows/elmfire/model_fire_spread_scenario | QUEUED (ADR 0095 / composer-cull characterization, report-only, NATE decides): CULL-CANDIDATE -- 1:1 private orchestration body of the elmfire_fire_spread template; overfit scenario name. CONDITION-to-delete: fold the body into elmfire/fire_spread/fire_spread.py (or rename to model_elmfire_fire_spread) and re-point 1 test import. reopen: a 2nd elmfire template needs this exact orchestration body (keep as a generically-named shared helper). | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| model_dambreak_geoclaw_scenario | workflows/geoclaw/model_dambreak_geoclaw_scenario | QUEUED (ADR 0095 / composer-cull characterization): CULL-CANDIDATE -- 1:1 private body of the geoclaw_inundation template; overfit "dambreak" name. CONDITION-to-delete: fold into geoclaw/inundation/inundation.py (or rename to model_geoclaw_inundation) and re-point 3 test imports. reopen: a 2nd geoclaw template needs this exact body. | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| model_landslide_scenario | workflows/landlab/model_landslide_scenario | QUEUED (ADR 0095 / composer-cull characterization): CULL-CANDIDATE -- 1:1 private body of the landlab_susceptibility template; overfit "landslide" name. CONDITION-to-delete: fold into landlab/susceptibility/susceptibility.py (or rename to model_landlab_susceptibility) and re-point 2 test imports. reopen: a 2nd landlab template needs this exact body. | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| model_seismic_hazard_scenario | workflows/openquake/model_seismic_hazard_scenario | QUEUED (ADR 0095 / composer-cull characterization): CULL-CANDIDATE -- 1:1 private body of the openquake_psha template; overfit "seismic hazard" name. CONDITION-to-delete: fold into openquake/psha/psha.py (or rename to model_openquake_psha) and re-point ~7 test imports (heaviest coupling). reopen: a 2nd openquake template needs this exact body. | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| model_wave_scenario | workflows/swan/model_wave_scenario | QUEUED (ADR 0095 / composer-cull characterization): CULL-CANDIDATE -- 1:1 private body of the swan_wave_field template; overfit "wave scenario" name. CONDITION-to-delete: fold into swan/wave_field/wave_field.py (or rename to model_swan_wave_field) and re-point 2 test imports. reopen: a 2nd swan template needs this exact body. | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| model_urban_flood_swmm | workflows/swmm/model_urban_flood_swmm | QUEUED (ADR 0095 / composer-cull characterization): CULL-CANDIDATE -- 1:1 private body of the swmm_urban_flood template; overfit scenario name; most-coupled body. CONDITION-to-delete: fold into swmm/urban_flood/urban_flood.py (or rename to model_swmm_urban_flood) and re-point the solver_confirm gate import + ~9 test imports. reopen: a 2nd swmm template needs this exact body. | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| model_river_dye_release_scenario | workflows/telemac/model_river_dye_release_scenario | QUEUED (ADR 0095 / composer-cull characterization): CULL-CANDIDATE -- 1:1 private body of the telemac_river_dye template; overfit "river dye release" name. CONDITION-to-delete: fold into telemac/river_dye/river_dye.py (or rename to model_telemac_river_dye) and re-point 1 test import. reopen: a 2nd telemac template needs this exact body. | DELETED (ADR 0105, commit-pending) | ADR 0095 -> 0105 |
| swmm_mesh_builder compat shim | workflows/swmm/swmm_mesh_builder.py | QUEUED (ADR 0098, mesh M1): the SWMM builder relocated to agent/mesh/raster_cell_mesh.py (git mv). This old-path module is now a thin FULL-namespace re-export shim so the ~15 current consumers (run_swmm / postprocess_swmm / solver / solver_confirm / physics_registry / model_urban_flood_swmm + tests) keep resolving unchanged. CONDITION-to-delete: M2 re-points every consumer import to trid3nt_server.agent.mesh.raster_cell_mesh, then this shim is deleted. reopen: never (relocation is permanent). CONDITION MET (ADR 0099, mesh M2): all consumers re-pointed (server.py / solver_confirm / run_swmm / postprocess_swmm / model_urban_flood_swmm + 6 test files), the agent/mesh<->workflows.swmm import cycle broken (lazy swmm_hyetograph + dead load_manning_mapping re-export removed), consumer tests 82 passed, direct import smoke green. DELETED. | DELETED (ADR 0099) | ADR 0098 -> 0099 |
| mesh_layer compat shim | workflows/shared/mesh_layer.py | QUEUED (ADR 0098, mesh M1): the mesh-preview constructors relocated to agent/mesh/mesh_preview.py (git mv). This old-path module is now a thin FULL-namespace re-export shim so model_urban_flood_swmm + the openquake model keep resolving unchanged (test_mesh_layer.py already re-pointed to the new home). CONDITION-to-delete: M2 re-points the two model composers to trid3nt_server.agent.mesh.mesh_preview, then this shim is deleted. reopen: never. CONDITION MET (ADR 0099, mesh M2): model_urban_flood_swmm re-pointed to agent.mesh.mesh_preview (make_swmm_mesh_layer_uri); no other consumer of the shim path remained (grep-verified). DELETED. | DELETED (ADR 0099) | ADR 0098 -> 0099 |
| MODFLOW gridgen binary absent (DISV live leg) | services/workers/modflow/Dockerfile + gwt_adapter.py | QUEUED (ADR 0099, mesh M2): the DISV/gridgen generator is BUILT + tested offline (gridgen_available/GridgenUnavailableError/_disv_refinement_features/_build_disv_gridprops/_build_gwf_disv + the capture_zone opt-in refine_regions seam), but flopy Gridgen shells to the USGS gridgen binary at build(), which is NOT in the image (which gridgen = absent). The seam STOPs honestly (GridgenUnavailableError, no partial deck). CONDITION-to-unblock-live: (1) add the gridgen binary to the modflow Dockerfile (curl+unzip+SHA-256 layer mirroring mf6, ~2-4 MB, report exact image delta at rebuild); (2) port the capture_zone WEL/CHD/PRT packages from structured (lay,row,col) to DISV (lay,node) vertex cellids. reopen: never (DISV is a permanent capability). | QUEUED | ADR 0099 |
| aoi_clip SFINCS worker include_mask | services/workers/_sfincs_build/deck.py + spec.py | QUEUED (ADR 0099, mesh M2): the aoi_clip role is parsed + surfaced server-side (spatial_roles), and the worker seam is characterized (setup_mask_active include_mask in _generate_hydromt_yaml_config ~:1901-1935, mirroring the existing building-obstacle exclude_mask). NOT wired this wave -- the worker deck change touches the SFINCS flood-solve seam (flood canary MANDATORY) + needs a hydromt worker build + live drive. CONDITION-to-wire: a scoped SFINCS-worker job adds the include_mask + spec.py option under a flood canary. reopen: never. | QUEUED | ADR 0099 |
| _build_telemac_mesh_envelope fold onto shared gate | agent/gates/cards/solver_confirm.py | QUEUED (ADR 0099, mesh M2): the shared mesh preview/approve gate (agent/mesh/preview_gate.build_mesh_gate_envelope) is BUILT + unit-proven to emit the identical PayloadWarningEnvelope+GranularitySuggestion shape. The LIVE TELEMAC gate still runs through the bespoke _build_telemac_mesh_envelope. CONDITION-to-fold: re-point _build_telemac_mesh_envelope onto build_mesh_gate_envelope under a live river_dye mesh_only drive (deferred to avoid destabilizing the live path without the drive). reopen: never. | QUEUED | ADR 0099 |
| TELEMAC silent NHDArea->constant-ribbon fallback | services/workers/telemac/entrypoint.py + telemac_river_dye_build.py | DELETED-as-behaviour (ADR 0101 leg 1, NATE 2026-08-04): the implicit real-bank->ribbon fallback (bank_source default `"auto"`, silent `LOG.warning(...constant-width fallback)` on empty/too-little-water/fetch-error) is REMOVED. Replaced by an EXPLICIT `bank_source` (nhd_area default | constant_ribbon) + a typed retryable TELEMAC_BANKS_UNAVAILABLE gate (BanksUnavailableError worker -> TelemacBanksUnavailableError server, `.suggestions` naming constant_ribbon + the assumed width) -- the DEM_FALLBACK_GATE pattern. No inexplicit mesh-source fallback survives. Live-proven (Columbia nhd_area real banks / constant_ribbon ribbon / forced-empty gate). | DELETED (commit-pending) | ADR 0101 |
| RiverMapper/pyDEM as bank_source="dem_derived" (TELEMAC + compound-flood river arcs) | services/workers/mesh/ (future) + telemac bank surface | QUEUED (ADR 0101 leg 3, characterization-only): schism-dev RiverMeshTools (RiverMapper + pyDEM, github.com/schism-dev/RiverMeshTools; Apache-2.0 repo / MIT subpackages -- no copyleft) derives real bank ARCS from an approximate centerline (NHDFlowline, which exists where NHDArea is empty) + a DEM by walking perpendicular transects -- a materially better third explicit bank source than an assumed-width ribbon for the exact NHDArea-empty gap leg 1 now gates on, AND the river-arc feed for future compound-flood meshing. CONDITION-to-adopt: its OWN small river-hydraulics wave (a new GDAL-native worker + a bank-arc<->TELEMAC contract translation + canonical-case V&V on a documented Ye-et-al-2023 reach); NOT folded into leg 1. Effort M. reopen: pysheds/GRASS r.stream DEM-threshold is the S-effort cruder alternative if the compound-flood roadmap is dropped. | QUEUED | ADR 0101 |
| TELEMAC barrier gmsh constraint | services/workers/telemac/telemac_river_dye_build.py | QUEUED (ADR 0099, mesh M2): a drawn barrier in a TELEMAC channel mesh = a wall/hole/breakline in the triangulation (gmsh, worker-side, GPL-isolated). The breakline role now carries the geometry to the worker; applying it as a gmsh constraint (island-hole or forced edge) is worker-side mesh work adjacent to M4. Honest characterization accepted as the M2 TELEMAC deliverable. CONDITION-to-wire: a gmsh sizing/constraint pass in the TELEMAC mesher (M4-adjacent). reopen: never. | QUEUED | ADR 0099 |
| GeoClaw invented dam source (AOI-centroid location + baked 10 m dam_break_depth_m) | server/src/trid3nt_server/agent/workflows/geoclaw/inundation/inundation.py + geoclaw_contracts.py | DELETED-as-behaviour (ADR 0102): a dam_break no longer defaults its source to the AOI centroid + a 10 m column. The dam location + height are resolved from the USACE NID (fetch_usace_dams, seam-1) by dam_name / nearest-AOI; DAM_HEIGHT(ft)->m, NID_STORAGE surfaced on GeoClawDepthLayerURI.source_note. No NID dam + no user-supplied source_lonlat+dam_break_depth_m -> typed GEOCLAW_DAM_INPUT_REQUIRED gate. The tool default dam_break_depth_m 10.0 -> None sentinel (10.0 kept only as the ignored contract default for tsunami/surge). Live-proven (Folsom Left Wing resolved; ocean-bbox gate). | DELETED (commit-pending) | ADR 0102 |
| SWMM silent 120 mm rainfall fallback on Atlas-14 lookup failure | server/src/trid3nt_server/agent/workflows/swmm/model_urban_flood_swmm/model_urban_flood_swmm.py | DELETED-as-behaviour (ADR 0102): a failed Atlas-14 design-storm lookup no longer silently falls through to the builder's baked 120 mm. It raises a typed SWMM_PRECIP_LOOKUP_FAILED gate naming total_rain_depth_mm as the explicit-param retry (ADR 0091). The 120 mm literal in raster_cell_mesh.build_swmm_mesh survives ONLY as a labeled mechanical last-resort for direct builder callers (the composer never reaches it). Live-proven (Houston Atlas-14 289.6 mm). | DELETED (commit-pending) | ADR 0102 |
| Landlab baked triggering rainfall (recharge_mm_day 30 / rainfall_intensity_mm_hr 50 as silent unlabeled defaults) | server/src/trid3nt_server/agent/workflows/landlab/susceptibility/susceptibility.py + landlab_contracts.py | DELETED-as-behaviour (ADR 0102): the triggering rainfall is sourced from NOAA Atlas-14 (seam-1) when unset -- overland rainfall_intensity = depth/duration, landslide recharge = design-storm total depth as a 1-day pulse. Failed lookup -> typed LANDLAB_RAINFALL_INPUT_REQUIRED. The contract DEFAULT_RECHARGE_MM_DAY / DEFAULT_RAINFALL_INTENSITY_MM_HR survive only as the mechanical contract floor (never silently narrated as site values); source_note labels the rainfall provenance + the still-demo SOIL block. Live-proven (Atlas-14 74.9 mm -> 37.5 mm/hr / 75 mm/day). | DELETED (commit-pending) | ADR 0102 |
| TELEMAC hidden 250 m3/s carrier discharge (worker constant, unexposed) | server/src/trid3nt_server/agent/workflows/telemac/model_river_dye_release_scenario/model_river_dye_release_scenario.py + river_dye.py | DELETED-as-behaviour (ADR 0102): the carrier discharge (dominant dilution control) is resolved from NOAA NWM streamflow (fetch_noaa_nwm_streamflow, seam-1) at the reach seed and set into reach["inflow_q_m3s"], or from an explicit discharge_m3s. A miss -> typed TELEMAC_DISCHARGE_INPUT_REQUIRED. The worker's 250.0 config default + width-heuristic survive (the resolved value supersedes them; INDEPENDENT of the ADR 0101 bank_source seam). Live-proven (Snake reach NWM 230.6 m3/s). | DELETED (commit-pending) | ADR 0102 |
| Plugin LOCAL/REMOTE mode switch + Cognito bearer semantics | plugin_settings.py + ui/settings_dialog.py + ui/dock.py + render/layers.py | DELETED-as-behaviour (ADR 0103): the cloud/Cognito REMOTE mode dies -- the tailnet IS the remote story. Removed MODE_REMOTE, DEFAULT_REMOTE_URL, PluginSettings.remote_url, the settings Mode combo + "Remote agent URL" row + _apply_mode_field_visibility, and every MODE_REMOTE / mode!=MODE_LOCAL branch in dock + layers. KEPT: the optional shared token field (re-scoped to the LOCAL tailnet token, OFF by default) and a read-only PluginSettings.mode->MODE_LOCAL migration seam (a persisted mode=remote loads without crashing). Server auth_handshake UNCHANGED (no Cognito verifier existed; the TRID3NT_ACCESS_TOKEN gate serves the tailnet token, firebase_uid stays a dormant carrier). | DELETED (commit-pending) | ADR 0103 |
| Daemon /plugins/ on-demand plugin repository (ensure_plugin_zip, git-describe version) | server/src/trid3nt_server/plugin_repo.py + tool_catalog_http.py | DELETED (ADR 0103): superseded by the /plugin-repo/ metadata-driven route (deploy-time package_plugin_repo into run/plugin-repo/, per-request Host substitution). The <version>+<git-describe> auto-suffix + on-demand HEAD-keyed build are gone; version is now metadata.txt-driven with a deploy-time drift warning. | DELETED (commit-pending) | ADR 0103 |
| Chat note "Case '<title>' active" (every case open) | ui/dock.py _on_case_open_event | DELETED-as-behaviour (ADR 0103, NATE de-noise): the "blank case is active" line removed at the emission source (dock render side; no server event, nothing else consumed it). | DELETED (commit-pending) | ADR 0103 |
| Chat note "Zoomed to case area" (3 sites) | ui/dock.py _zoom_after_case_open | DELETED-as-behaviour (ADR 0103, NATE de-noise): the zoom subtext removed. The canvas STILL auto-focuses on case open; only the chat note is gone. The honest bbox-less fallback ("Case has no stored map area ...") stays. | DELETED (commit-pending) | ADR 0103 |
| case_export.ws_url_to_http_base | qgis-plugin/trid3nt/case/case_export.py | QUEUED (ADR 0103): orphaned by the remote-branch removal in dock._effective_http_base (its only caller). CONDITION-to-delete: confirm no future remote-URL derivation need (the export machinery it lived beside is itself condemned). Left in place this wave to minimize churn during NATE's live session. | DELETED (ADR 0116) - the whole case_export.py module (its only home) is gone; no remote-URL derivation need materialized (resolve_http_base/derive_http_base in trid3nt_client.py cover the :8766 base; the export machinery it lived beside is deleted) | ADR 0103 / ADR 0116 |
| TELEMAC in-process SIGALRM mesh watchdog | services/workers/telemac/telemac_river_dye_build.py | DELETED (ADR 0104, Bug 1): the `signal.SIGALRM(240)` guard around the gmsh build was demonstrably ineffective -- a gmsh C busy-loop never returns to Python to take the signal, so a degenerate reach hung 32+ min (docker-killed). Superseded by (a) the pre-mesh `validate_reach_geometry` gate (fast typed ReachDegenerateError) and (b) `build_channel_mesh_guarded` (the whole build runs in a killable child process the parent SIGKILLs at a wall-clock deadline -- which a C busy-loop cannot swallow). reopen: never (an in-process signal cannot preempt a C busy-loop). | DELETED (commit-pending) | ADR 0104 |
| run_model_nws_flood_event_scenario (standalone composer) + test_model_nws_flood_event_scenario.py | workflows/sfincs/model_nws_flood_event_scenario/ | DELETED (ADR 0105 composer dissolution, NATE-decided): the live-NWS-flood-warning -> observed-MRMS-precip -> SFINCS chain is now MODEL-composed from fetch_nws_alerts_conus + fetch_mrms_qpe + sfincs_flood. Registration + workflows/__init__ import + corpus + categories + scenario_reuse + tool_catalog_http + tests removed; model_flood_scenario (the sfincs_flood body) UNTOUCHED. Judgment relocated: NWS alert-type->flood-mode routing added to the system prompt + fetch_nws_alerts_conus docstring. Registry 175->172 (with the two siblings). reopen: never (aggregation is the model's job). | DELETED (commit-pending) | ADR 0105 |
| run_model_groundwater_contamination_scenario (standalone composer) + test_model_groundwater_contamination_scenario.py | workflows/modflow/model_groundwater_contamination_scenario/ | DELETED (ADR 0105): the news-article spill-ingest -> MODFLOW-GWT plume chain is now MODEL-composed (web_fetch -> extract contaminant/location/amount/duration -> derive forcing -> modflow_contaminant_plume). Registration + workflows/__init__ import + the server.py SOLVER_CONFIRM_TOOLS entry + the server confirm-gate extraction branch + corpus + categories + scenario_reuse + tool_catalog_http + river_seepage docstring cross-refs + tests removed; modflow_contaminant_plume UNTOUCHED. Judgment relocated: Invariant-9 never-invent-contamination-params stays in the modflow_contaminant_plume docstring + reinforced in the system prompt. reopen: never. | DELETED (commit-pending) | ADR 0105 |
| compute_impact_envelope (standalone composer) + test_compute_impact_envelope.py + test_impact_envelope_emission.py + _maybe_emit_impact_envelope (server.py) | workflows/pelicun/compute_impact_envelope/ | DELETED (ADR 0105): the geocode -> NSI/MS inventory -> pelicun_damage_assessment -> postprocess_pelicun aggregate is now MODEL-composed from those registered atoms. Registration (tools/__init__) + the server.py impact-envelope WS-emission dispatch block + _maybe_emit_impact_envelope helper + corpus + categories + tool_arg_normalizer/compute_flood_depth_damage docstring cross-refs + tests removed/migrated (test_job0304 re-pointed to pelicun_damage_assessment). pelicun_damage_assessment + postprocess_pelicun UNTOUCHED. Note: the web ImpactPanel is now orphaned of its server feed (no wire contract removed) -- QUEUED follow-up. reopen: never. | DELETED (commit-pending) | ADR 0105 |
| ImpactPanel WS wire ("impact-envelope" type) orphaned by compute_impact_envelope deletion | web client ImpactPanel + ws contract | QUEUED (ADR 0105): the server-side _maybe_emit_impact_envelope emitter was deleted with compute_impact_envelope, so the web ImpactPanel receives no envelope. Condition to delete: a web-side cull wave removes the ImpactPanel component + the "impact-envelope" wire type (out of scope for this server-side dissolution). reopen: a future portfolio-aggregate tool re-adds the emission. | QUEUED | ADR 0105 |
| standalone prose `source_note` field (GeoClawDepthLayerURI + LandlabSusceptibilityLayerURI) | geoclaw_contracts.py:316 + landlab_contracts.py:252 | QUEUED (ADR 0106): the free-text `source_note` (ADR 0102) is superseded by the structured `synthetic_inputs` list -- the narration seam now renders the human prose line from the structured entries (`render_assumptions_line`), so the standalone contract field is redundant. CONDITION-to-delete: a follow-up wave confirms no consumer reads `.source_note` directly (the two ADR-0102 tests migrate to asserting `synthetic_inputs`) AND the rendered assumptions line is proven across the live narration seams. Kept THIS wave to avoid breaking the ADR-0102 source_note tests + because the prose line is still emitted. reopen: a per-layer prose note is needed that is NOT decomposable into structured entries. | QUEUED | ADR 0106 |
| GeoClaw worker tsunami Okada stdout banner ("NON-SITE-SPECIFIC synthetic source") | services/workers/geoclaw/setrun_builder.py:522-541 | QUEUED (ADR 0106): the stdout-only honesty banner is superseded by the server-side structured `fault_geometry` + `source_magnitude` synthetic_inputs entry (item 2c), which rides the envelope into narration deterministically (no worker read of stdout). CONDITION-to-delete: a worker-touching wave (which rebuilds the geoclaw image anyway) drops the BANNER string + its print, and the setrun_builder test assertions (`test_setrun_builder.py:536,570`) migrate. Kept THIS wave: no worker rebuild was run (offline-first; no live tsunami solve). reopen: never (server entry is strictly better). | QUEUED | ADR 0106 |

| ChartsPanel (in-chat collapsible charts panel, ~205 LOC) + dead Qt imports in charts.py | qgis-plugin/ui | replaced by the bottom-docked TUFLOW-style ChartsWindow (chat keeps the button); charts.py is now a Qt-free pure renderer, render_spec byte-identical | DELETED (ADR 0119) | ADR 0119 |
| Bald Eagle Creek multi-2D levee archetype (BaldEagleCrkMulti2D, Lock Haven PA) | services/workers/hecras/ (future baked deck) + a `bald_eagle_2d_levee` archetype literal | QUEUED (ADR 0125, triage finding): the SHIPPED multi-2D Bald Eagle model (HEC's `hecras_hgt_dam_breach_2d_areas_bald_eagle` example, SHA-pinned in the example zip) is the real levee/breach V&V target (published ~516,000 cfs peak breach outflow / ~435,000 cfs breach component -- concrete regression numbers). BLOCKED on Windows-Phase-1: its geometry HDF's terrain subgrid tables are RASMapper-authored (Windows DLLs), so from-scratch or intermediate re-authoring is not headless-reproducible on Linux (the ADR 0100 STOP). CONDITION-to-adopt (NATE's call, referenced not decided): EITHER (a) obtain the Windows-Phase-1 intermediates (a RASMapper-preprocessed geometry HDF baked as a 2nd deck, mirroring Muncie) OR (b) adopt a neeraip-class third-party headless preprocessor to author the subgrid tables on Linux. Then bake the deck + add the archetype literal + reuse the breach deck-edit/solve/postprocess spine unchanged. reopen: never (a real multi-2D levee case is a permanent V&V asset). | QUEUED | ADR 0125 |
| HEC-RAS rain-on-grid (pluvial) archetype | services/workers/hecras/ (future baked deck) + a `rain_on_grid` archetype literal + a precip-boundary deck-edit branch | QUEUED (ADR 0125, triage STOP): a spatially-uniform/gridded precipitation boundary as the 2D forcing (a distinct QUESTION class -- pluvial vs fluvial, pairs with the NWM/precip fetchers). STOPPED this wave: needs a SHIPPED rain-on-grid tutorial geometry baked (the Muncie deck has no precipitation boundary), and the forcing is a non-hydrograph deck-edit surface (a new branch beside scale_flow_hydrograph / set_breach_enabled). CONDITION-to-adopt: bake a shipped rain-on-grid tutorial deck + add the precip-boundary deck-edit branch; the solve/postprocess-to-depth-COG/mesh-preview spine is reused unchanged. reopen: never (pluvial is a permanent capability). STATUS (ADR 0133): the headless subgrid-table frontier is now PROVEN UNLOCKED (ADR 0130/0132) and the geometry WRITER lands (0133 OI-2, solver-validated at dWSE 0.0). What still gates rain-on-grid is the deck-skeleton PRECIP FORCING stanza (Meteorology/Precipitation in .bNN + /Event Conditions), which has NO in-repo reference (see 0133 OI-A: obtain a shipped rain-on-grid pure-2D reference from Example_Projects_6_6, characterize the precip block, author + solve). Precip is the PREFERRED forcing; a 2D-BC-line inflow is the fallback. | QUEUED | ADR 0125 / ADR 0133 |
| neeraip-class third-party HEC-RAS headless preprocessor adoption | services/workers/hecras/Dockerfile + a headless subgrid-table authoring leg | QUEUED (ADR 0125, PARKED FOR NATE -- strategic decision, referenced not decided): the community neeraip/hecras-v66-linux repro (the 0.00% Muncie volume-error row cited in ADR 0100/0109) demonstrates a Linux path, but authoring RASMapper subgrid terrain tables headless (the M3 STOP, the frontier blocking every real-AOI and multi-2D archetype) is the open strategic question: adopt a third-party preprocessor vs wait for HEC's native-Linux 2025 migration vs a Windows-Phase-1 intermediates pipeline. CONDITION-to-decide: NATE weighs the third-party-dependency + licensing + maintenance cost against the native-Linux timeline; this ledger row records the decision surface, it does NOT decide it. reopen: HEC ships native-Linux geometry authoring (retires the blocker outright -- feasibility report S2b). STATUS (ADR 0133): the strategic question is substantially ANSWERED without a third-party dependency -- HEC's own 2025 beta compute path (MeshPropertyTables.ComputeFrom) authors the subgrid tables headless on Linux under substituted open-source natives (ADR 0130), those values match the 6.x GUI (0132 Q2), and the in-repo geometry WRITER (0133) serializes them into a solver-valid geometry HDF (dWSE 0.0). The neeraip-class adoption is thus NOT NEEDED for the subgrid-table frontier; the remaining net-new build is the authoring-worker STAGE + the deck-skeleton forcing link (0133 OI-A/OI-B), not a preprocessor swap. | QUEUED (PARKED for NATE; largely mooted by ADR 0133) | ADR 0125 / ADR 0133 |
| 6.x HEC-RAS worker + template-first geometry freeze (services/workers/hecras/) -> HEC-RAS 2025 native-Linux migration | services/workers/hecras/ (the 6.x MKL/binary bundle) + the frozen-geometry archetype constraint | QUEUED (ADR 0127, characterized -- NOT yet actionable): the HEC-RAS 2025 line is the direction NATE picked to retire the M3 STOP, and the spike CONFIRMS the architecture fits (portable managed `ras` CLI, single-.h5 project, `ras prepare` computes subgrid property tables headless, `ras mesh` native mesher, CPU solver). But the public 2025 beta (hec-downloads 1.0.44, `HEC-RAS_2025_Beta.zip`, sha256 0df9cf0d...) is a **win-x64-only** self-contained publish -- ALL native compute (RasNativeParallel/hdf5/hecdss/gdal_wrap) is Windows, no Linux/container build is published (HEC download page = Windows 10/11 only). Managed CLI runs on Linux; native SOLVE does not (proven: `ras healthcheck` dlopen('gdal_wrap.so') fails). NO-GO-YET. CONDITION-to-migrate: (a) HEC publishes a **linux-x64** HEC-RAS 2025 payload (the gate) AND (b) precipitation forcing is confirmed IN the shipped build (roadmap "2026", not in the current beta) for the rain-on-grid archetype. Then the `services/workers/hecras2025/` probe flips to a solver (add the linux natives, entrypoint -> createterrain/mesh/prepare/solve/map) and the 6.x frozen-geometry archetypes migrate to real-AOI authoring; run the Muncie question through BOTH solvers as the overlap-validation gate. reopen: never (native-Linux 2025 is the strategic endgame). Version to watch: first hec-downloads/container release with a linux-x64 payload. | QUEUED (CONDITION pinned) | ADR 0127 |
| swmm_green_grey_infra_storms (paired grey-vs-green infra template) | services/... future + workflows/swmm/deck_green_grey/ + a `variant` (dry_pond\|lid) knob | QUEUED (ADR 0128, triage STOP): the cited openswmm Topic 23670 hosts TWO decks on ONE page (DryPond_100yr24hr.inp + LID_100yr24hr.inp); the row's point is the paired grey-vs-green comparison. The LID deck extracts + solves clean (continuity -0.013%); the DryPond block's inline extraction still bleeds duplicate RAINGAGE ids. CONDITION-to-land: tighten extract_inline_deck's two-deck upper bound so deck0 hard-stops at deck1's [TITLE] (the select_index machinery exists), add a variant knob, solve BOTH + chart the paired runoff. Effort S. reopen: never (a paired grey-vs-green demo is a permanent capability). | QUEUED | ADR 0128 |
| swmm_cso_regulator_network (CSO regulator/pump/force-main template) | workflows/swmm/deck_cso/ (future) + a bespoke Example-8 deck | QUEUED (ADR 0128, triage STOP): the cited EPA Applications-Manual Example 8 (Combined Sewer Systems) raw .inp is NOT hosted downloadable anywhere (not epa.gov, not the USEPA GitHub repo, PDF undecodable via WebFetch). It does NOT fit the inline-fetch runner thesis (no obtainable published .inp) -- it is a BESPOKE-SYNTHESIS candidate. CONDITION-to-land: EITHER obtain the Example-8 .inp from a licensed EPA-SWMM GUI Examples folder (+ characterize redistribution) OR rebuild the regulator (transverse weir + orifice) + pump + force-main from the manual's documented parameters as a bespoke deck. Effort M-L. reopen: never (CSO regulator control is a permanent capability). | QUEUED | ADR 0128 |
| 18 per-family `_surface_*` input helpers + ~54 composer emission call sites (`publish_input_layer` / `publish_raster_input_cog` for role=context INPUTS) + the uri-threading plumbing that fed them (3-tuple returns, `uri_sink` params, `WatershedMesh.dem_input_s3_uri`/`river_input_s3_uri`) | server/src/trid3nt_server/agent/workflows/**/ (34 files: telemac/river_dye+rain_on_grid+stratified_flow, landlab/_composer_common+flow_accumulation+green_ampt+susceptibility, modflow/capture_zone+run_modflow+wetland_hydroperiod, sfincs/flood, swmm/urban_flood+dual_drainage, swan/wave_field, geoclaw/*, openquake/secondary_perils, ...) | QUEUED (ADR 0244, S2 collapse): the emit-on-fetch router SEAM (S1, LANDED) now auto-surfaces every AGENT-SIDE router fetch of renderable data as a role=context "Input:" row, so the per-family helpers + explicit call sites are redundant (they COEXIST now, uri-deduped, no visible double rows). CONDITION-to-delete: the ATOMIC S2 collapse -- delete the helpers + role=context input call sites + revert the uri-threading signatures to their natural shapes where nothing else consumes them, carry composer semantic naming as `purpose=` on the fetch, PRESERVE result/output emission (composers keep their results), add the name-pattern + call-pattern SWEEP TEST (0232 style), collapse test_input_layer_surfacing.py to the seam tests + the sweep -- landed as ONE change and gated by the live flood canary (NATE loop, "flood canary after LARGE changes"). The RESULT-emission `publish_input_layer` call sites (a solver's own output layers) are NOT candidates. reopen: never (the render declaration is the single source of truth). CONDITION MET + DELETED (ADR 0244 S2, commit-pending): 4 `_surface_*` helper defs + all call sites deleted, direct role=context input-emission call sites deleted across 13 composers, uri-threading plumbing (`WatershedMesh` fields, `uri_sink` params, `_fetch_bathymetry_cog` 3-tuple) reverted to natural shapes, semantic names moved to `purpose=` on the fetch; the SWEEP TEST (`test_input_layer_surfacing.py::test_sweep_*`) now polices it; net **-555 LOC** production code. KEPT (seam cannot cover, sweep-allowlisted): mesh previews, computed result COGs, in-worker COGs (river_dye bed / telemac3d bottom / swan bathy), bare-OSM agitation breakwaters, MODFLOW user-data well overlays. | DELETED (commit-pending) | ADR 0244 |
| IN-WORKER bathymetry NOT surfaced: artemis (agitation) + tomawac (telemac/wave_field) lake-datum bed | services/workers/telemac/ (artemis_build, tomawac_build) + agent composers agitation.py / telemac/wave_field/wave_field.py | QUEUED (ADR 0244, S3 audit loose end): these two TELEMAC modules sample their bathymetry INSIDE the solver container, so it never touches the agent-side `route()` seam and is never surfaced as a Case input (the 0231 artemis residual, now joined by tomawac). river_dye (bed_bathymetry.tif -> `_surface_bed_bathymetry_input`) + swan/wave_field (`publish_raster_input_cog`) + telemac3d/stratified_flow (`publish_input_layer(bottom_pub)`) are the TEMPLATE. CONDITION-to-land: a worker-image change writes the sampled bed as a 4326 COG + records `bed_cog` in the metrics envelope, then the composer rides it through `publish_raster_input_cog` (the river-bed-COG-seam treatment) -- deferred to a worker wave (image rebuild required; offline-green != deploy-green). reopen: never (a modeled bed is a real input the user should see). CONDITION MET + RESOLVED (commit-pending): both workers now write the sampled lake-datum bed as bed_bathymetry.tif (a 4326 COG via the shared `services/workers/telemac/_bed_cog.write_bed_cog_lonlat`) + record `bed_cog`/`bed_cog_min_m`/`bed_cog_max_m` in telemac_metrics.json (parser markers bumped: tomawac-wave-2, artemis-agitation-3); the two composers ride it through the shared `telemac/_bed_input.surface_in_worker_bed_input` -> `publish_raster_input_cog` as a role=context "Input: lake bed bathymetry" continuous_dem input, added to each manifest `outputs`. Image rebuilt (absolute -f/context), provenance-verified, live-smoked on both modules (bed COG present, 4326, bounds inside the request AOI). The artemis OSM breakwater STAYS a bare-OSM router-bypass (the router's only general overpass source collapses ways to centroids; the physics needs the way polyline) -- surfaced explicitly + sweep-allowlisted, unchanged. | RESOLVED (commit-pending) | ADR 0244 |
| Cloud-shaped seams in server/_core.py: aws-batch backend switch, TiTiler style fallbacks, dormant adapter.py Vertex/Gemini path, DynamoDB TTL/persistence residue | server/src/trid3nt_server/server/_core.py + agent/adapters/adapter.py | QUEUED (ADR 0261, server-refactor wave 2 = NEXT): these are live-but-cloud-shaped seams the wave-1 recon flagged. NOT moved/chopped in wave 1 (which was a pure package skeleton + errors/config extraction + dispatcher rename). CONDITION-to-delete, per seam: (aws-batch) confirm local-docker is the only solver backend since GRACE-2 cf7129d2 -> strip the backend switch; (TiTiler) verify nothing local dials a TiTiler URL (QGIS-native rendering replaced it) -> strip the style-fallback vocabulary; (adapter.py Vertex) the pluggable-LLM story (bedrock/openai/local) may not need the dormant seam -> its own ledger decision; (DynamoDB) local persistence honors TTL differently -> simplify. reopen: never (each is dead weight in the local product). | DELETED (commit-pending) | ADR 0261 -> 0262 |
| server-refactor wave-2 chop VERIFICATION + evidence (ADR 0262) | server/_core.py + adapter.py + persistence.py + solver.py + publish_layer.py | DELETED (commit-pending, ADR 0262). Per-seam findings: (1) AWS-BATCH SWITCH -- verified NOTHING selects aws-batch: `solver_backend()` is hardwired to `local-docker` (prior local-only slim) and `TRID3NT_SOLVER_BACKEND` is read NOWHERE for dispatch. The env-reading switch was already gone; residue was (a) a dead cloud tail in `solve_progress_vcpus` (collapsed to `return os.cpu_count()`, byte-identical -- test_solve_progress_vcpus already expected host-CPU for every input incl aws-batch env) and (b) stale `AWS_BATCH_*`/`Batch`/`aws-batch` vocabulary. `AWS_BATCH_COMPUTE_CLASS_SIZING` is LIVE-consumed (solver-confirm card sizing) so RENAMED, not deleted -> `COMPUTE_CLASS_SIZING`. `solver_backend()` KEPT: live predicate seam read by auth_handshake `_is_local_single_user_mode` + solver_confirm. ~20 LOC net removed in solver.py. (2) TITILER -- verified the plugin NEVER dials/constructs TiTiler tile URLs (only UNWRAPS legacy templates for old persisted cases; qgis-plugin grep) and the server no longer serves TiTiler URLs (publish_layer emits raw s3:// COG). No dead TiTiler fallback branch existed -- the flood-depth-default + p2/p98 percentile are LIVE QGIS styling. RENAMED the live styling seam `_resolve_titiler_style_params`->`_resolve_qgis_style_params`, `_TITILER_STYLE_REGISTRY`->`_QGIS_STYLE_REGISTRY`, `_TITILER_SAFE_DEFAULT`->`_QGIS_STYLE_SAFE_DEFAULT` (63 sites across 15 files incl 6 tests) + swept _core.py + publish_layer.py TiTiler prose; styling values byte-identical. ~0 LOC net (rename+reframe). FLAGGED: ~88 residual TiTiler prose refs in emission/ + postprocess/ (mostly accurate legacy-tile-template-unwrap descriptions) = separate whole-codebase hygiene pass, NOT this chop. (3) ADAPTER VERTEX -- verified the Vertex/Gemini `generate_content_stream` client path was ALREADY removed (an unsupported MODEL_PROVIDER raises `UnsupportedModelProviderError` naming scripted/bedrock/openai). Residue chopped: dead env-reads in `load_settings` (GOOGLE_CLOUD_PROJECT/LOCATION/GENAI_USE_VERTEXAI) removed, ModelSettings project/location/use_vertex defaulted (kept for ~8 test constructors) + module docstring reframed off "Gemini-only containment". DEPENDENCY: google-genai is LOAD-BEARING (genai_types IR imported by bedrock_adapter/openai_adapter/adapter/stratified/context_budget; pyproject documents the carve-out) -> KEPT, NOT removed. FLAGGED: ~40 residual Gemini prose refs in adapter.py describe the genai IR/schema (largely accurate) = follow-up. (4) DYNAMODB -- verified NO dynamo_backend.py module exists (already removed in local-only slim) + make_persistence_for_backend/resolve_persistence_backend already file-only. Swept all DynamoDB comments in _core.py + persistence.py to file-backend constraints. Added `UnsupportedPersistenceBackendError`: selecting any non-`file` `TRID3NT_PERSISTENCE_BACKEND` now raises a typed error (was silently ignored; env read nowhere so safe). KEPT + FLAGGED: the `expires_at`/ephemeral-Case TTL machinery is LIVE persisted data (file backend does not auto-reap; marker retained) -- NOT deleted (byte-identical persistence hard rule; wire/persisted). file+mongo persistence paths unchanged. | DELETED (commit-pending) | ADR 0262 |
| case-view SNAPSHOT machinery (build_case_view_snapshot, write_case_view_snapshot, _resolve_cross_case_vector_inline, _resolve_case_owner, _default_s3_put_case_view, build_case_manifest, write_case_manifest, _manifest_layer_from_summary, _default_s3_put_case_manifest, case_view_snapshot_key, case_manifest_key, CASE_VIEWS_BUCKET/PREFIX, CASE_MANIFESTS_PREFIX, CASE_VIEW_INLINE_GEOJSON_MAX_BYTES) | server/src/trid3nt_server/persistence.py | CONDITION MET (ADR 0266, NATE-confirmed severed): the view-without-agent browser cold view is retired; grep of server/+qgis-plugin/+scripts/ for readers of case-views/ + case-manifests/ objects = ZERO (only exclusion filters in 8 run_*_direct.py scripts, never readers). QGIS-only product rebuilds a reopened Case from the persisted store + WS replay; snapshots were never the reopen path. -612 LOC. | DELETED (commit-pending) | ADR 0266 |
| _persist_case_view_snapshot + _persist_case_manifest defs + all 7 call sites (case-open create_task pair, case-create, case-rename, case-set-bbox, turn-close finally block, publish-last-tool wrap-site, auto-publish, turn-close create_task pair) + __all__ entries | server/src/trid3nt_server/server/_core.py | CONDITION MET (ADR 0266): sole writers of the chopped snapshot/manifest objects. The sibling _persist_case_loaded_layers calls at each site (the REAL reopen-persistence path) KEPT untouched. Net -353 LOC in _core.py. | DELETED (commit-pending) | ADR 0266 |
| coldview backfill (_run_coldview_backfill, _COLDVIEW_BACKFILL_ENABLED/_CONCURRENCY, startup _coldview_task wiring) + persistence.list_all_active_case_ids | server/src/trid3nt_server/server/_core.py + persistence.py | CONDITION MET (ADR 0266): the daemon-restart re-materialize sweep served the cold snapshot; sole caller of list_all_active_case_ids. The _BG_SNAPSHOT_TASKS registry + _drain_bg_snapshot_tasks GENERALIZED (renamed _BG_TASKS / _drain_bg_tasks / _BG_DRAIN_TIMEOUT_S), NOT deleted -- the startup tool-retrieval discover-index warm task is a live non-snapshot consumer. | DELETED (commit-pending) | ADR 0266 |
| PipelineEmitter public properties inline_geojson_by_layer_id + density_meta_by_layer_id | server/src/trid3nt_server/emission/pipeline_emitter.py | CONDITION MET (ADR 0266): defensive-copy accessors whose SOLE consumer was _persist_case_view_snapshot (grep of .inline_geojson_by_layer_id / .density_meta_by_layer_id reads = ZERO after chop). The private _inline_geojson_by_layer_id / _density_meta_by_layer_id attrs KEPT (live emit_session_state wire path reads them directly). -29 LOC. | DELETED (commit-pending) | ADR 0266 |
| CaseManifest + CaseManifestLayer contract types | contracts/src/trid3nt_contracts/case.py | QUEUED (ADR 0266): fully orphaned by the manifest-writer chop -- zero consumers in src+tests. CONDITION-to-delete: schema-owner confirms no future data-island cold index need; out of server-refactor scope (contract package = schema seam). reopen: a cold-index feature returns. | QUEUED | ADR 0266 |
| SIGNED_URLS seam (SIGNED_URLS_ENV, signed_urls_enabled, emit_layer_uri WARNING branch) | server/src/trid3nt_server/emission/layer_uri_emit.py | DELETED (ADR 0267, wave 7): dormant browser-fetch signer scaffold; passthrough today, no direct-fetch surface on the QGIS-only stack. CONDITION MET: severed consumer (grep=zero). Guardrail + PASS/DROP outcomes byte-identical. reopen: never (a browser signer, if ever, lives client-side). | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| Persistence.get_user_by_firebase_uid | server/src/trid3nt_server/persistence.py | DELETED (ADR 0267, wave 7): zero callers; dormant multi-user IdP-sub lookup. CONDITION MET: severed consumer. Round-trip test retargeted to get_user_by_id. reopen: never (no IdP in the local build). | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| append_audit + audit_log collection (Persistence.append_audit, AUDIT_COLLECTION, _core mode2 audit block) | server/src/trid3nt_server/persistence.py + server/_core.py + agent/gates/mode2_classifier.py | DELETED (ADR 0267, wave 7): fire-and-forget writers, ZERO readers (no find on the collection). CONDITION MET: severed consumer. In-memory state.payload_warning_audit_log (LIVE, read locally) KEPT -- distinct. reopen: never (nothing consumes an audit stream locally). | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| Anonymous-user provisioning (_provision_anonymous_user, _try_reuse_anonymous_user, _anonymous_id_is_claimable, _local_case_adoption_done, Persistence.adopt_cases_to_user, FileMCPClient update-many) | server/src/trid3nt_server/credentials/auth_handshake.py + persistence.py | DELETED (ADR 0267, wave 7): solver_backend() hardwired local-docker -> the non-local anon branch was dead; authenticate_token now local-only + typed NonLocalAuthUnsupported. _resolve_local_single_user inlined (get_user_by_id + upsert). Adoption sweep = a spent one-time historical migration. CONDITION MET: severed consumer. reopen: multi-user returns (separate provisioning). | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| firebase_uid field threading (AuthResult, build_auth_ack, AuthAckEnvelope, SessionState, User contract, _bind_auth_result) | server/src/... + contracts/src/trid3nt_contracts/{auth,user}.py | DELETED (ADR 0267, wave 7): always None locally; provider-agnostic IdP-sub carrier, no local IdP. Plugin auth-ack read grep=zero -> removed cleanly from the wire. Persisted old rows tolerated (get_user_by_id filters to User.model_fields before validate); proven by test. reopen: multi-user IdP returns. | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| migrate_preauth_cases + _run_preauth_case_migration + MIGRATION_ANON_UID + startup wiring | server/src/trid3nt_server/persistence.py + server/_core.py | DELETED (ADR 0267, wave 7): multi-user pre-Auth case-leak governance is moot single-user (every Case has one owner). CONDITION MET: invariant it protected is multi-user-only. reopen: multi-user returns. | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| CONFIRMATION_TRIGGERS empty scaffold + docstrings | server/src/trid3nt_server/server/_core.py | DELETED (ADR 0267, wave 7): empty set(), no membership test anywhere (grep=def+docstrings). FR-AS-8 scaffold. LIVE solver-confirm gate (SOLVER_CONFIRM_TOOLS / _PENDING_CONFIRMATIONS) is separate + KEPT. reopen: a real write-carveout policy lands. | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| expires_at ephemeral-Case TTL stamping (upsert_case ephemeral kwarg + branch, touch_case, _touch_session_record touch_case call, ephemeral= call sites) | server/src/trid3nt_server/persistence.py + server/_core.py | DELETED (ADR 0267, wave 7): persisted numeric TTL with no local reader/reaper (file backend never reaps). Stamping stripped; Cases durable (no-op change). Old case docs tolerated (_doc_to_case_summary drops unknown keys); proven by test. SCOPE: ephemeral-CASE TTL only; D.6 session-record TTL (touch_session/SESSIONS_TTL/SessionDocument.expires_at) KEPT (separate, required field). reopen: a real Case-reaper lands. | DELETED (ADR 0267) | ADR 0266 -> 0267 |
| CASES_ANON_TTL_SECONDS contract constant | contracts/src/trid3nt_contracts/collections.py | QUEUED (ADR 0267, wave 7): orphaned in src after the chop-8 ephemeral-Case TTL removal (its only consumers were the deleted stamping path). CONDITION-to-delete: schema-owner confirms no Case-TTL need; out of server-refactor scope (contract package). reopen: a Case-reaper returns. | QUEUED | ADR 0267 |

| test_live_mcp_write_then_read (+ dead `from trid3nt_server.mcp import MCPClient, fetch_srv_from_secret_manager` import) + MCP-stdio `is_dev_persistence_enabled` defer/precedence tests | server/tests/test_persistence.py + server/tests/test_file_persistence.py | DELETED (ADR 0269, smell-to-code audit): the env-guarded live test imported `trid3nt_server.mcp`, a module removed with GCP decommission; `TRID3NT_MONGO_MCP_STDIO` is read NOWHERE in code so the defer/precedence tests asserted dead behavior. CONDITION MET: severed consumer + deleted-module import. ~35 LOC (test_persistence) + 2 folded tests (test_file_persistence). reopen: never (no MCP-stdio path in the local build). | DELETED (commit-pending) | ADR 0269 |
| init_persistence_from_env (thin preserve-or-None + startup diagnostic) | server/src/trid3nt_server/server/_core.py ~727 | FLAGGED (ADR 0269): NATE's proof example. Traced LIVE -- called by run_server, and the `TRID3NT_DEV_PERSISTENCE=0` None path is a tested escape hatch with 30 None-handling consumers; the Atlas-narrating docstring NATE remembered was already fixed (ADR 0262). Its only work is a boot diagnostic log. CONDITION-to-delete: NATE decides the boot diagnostic is not worth the thin wrapper -> inline `get_persistence()` at the run_server call site + retarget the 2 preserve-contract tests to assert `get_persistence()` directly. reopen: a real backend-bootstrap step returns to the function. | FLAGGED (KEEP recommended) | ADR 0269 |
| TiTiler tile-template unwrap (`_titiler_cog_uri` + Branch-3 in `_normalize_layer_uri`) | server/src/trid3nt_server/emission/uri_registry.py ~314,~843 | FLAGGED (ADR 0269, persisted-shape): unwraps OLD PERSISTED case docs / `layer_handles` maps still carrying legacy TiTiler tile-template URLs; publish_layer no longer mints them. CONDITION-to-delete: a persisted-data audit/migration proves no live Case carries a TiTiler template URL (then the read-side guard is unreachable). Same class as ADR 0262's ~88 residual-TiTiler-prose flag. reopen: never (TiTiler is decommissioned). | FLAGGED (KEEP; wire/persisted) | ADR 0269 |
| confirm-response / disambiguation-response / clarification-response tolerant no-op ack branch | server/src/trid3nt_server/server/_core.py ~10306 | FLAGGED (ADR 0269, wire-shape): LIVE `ws.py` contract types (ConfirmResponsePayload/Disambiguation/Clarification) with real script senders; the real confirm flow uses typed per-gate messages. The branch is a DELIBERATE graceful ack -- removing it turns those sends into INTERNAL_ERROR "unknown type". CONDITION-to-delete: NATE confirms these three envelope types are retired from the contract (then drop the contract types + the ack branch together). Meanwhile: drop only the "noop M1" log-string archaeology. reopen: a real trigger binds to these types. | FLAGGED (KEEP; wire contract) | ADR 0269 |
| test_mongo_mcp_wiring.py filename (dead-system misnomer) | server/tests/test_mongo_mcp_wiring.py | FLAGGED (ADR 0269): named for a decommissioned Mongo-MCP system; actually tests the Persistence-singleton startup wiring (docstring + tombstone corrected to truth THIS wave). CONDITION-to-rename: a test-hygiene pass renames -> test_persistence_wiring.py (touches the offline-suite lastfailed cache; out of this wave's cut scope). reopen: n/a. | FLAGGED (rename queued) | ADR 0269 |
| research_mode dead-wire (UserMessagePayload.research_mode + ResearchMode Literal + _stream_model_reply/_dispatch params + dispatch log field + default_research_mode pref mention) | contracts/ws.py + server/_core.py + contracts/user.py | DELETED (ADR 0270, wave 11 feature cut): a "pinned but never branched" toggle -- logged + forwarded, never read for a second pipeline. TRACE: zero plugin senders (the user-message payload builder never sets it); default_research_mode was never a contract field (User.prefs is an open dict). WIRE PROOF: extra="forbid" now rejects the field; open-dict prefs tolerate a stale key. reopen: a real deep-research pipeline lands with its own carrier. ~34 LOC. | DELETED (commit-pending) | ADR 0270 |
| Firebase H.4 TierClaim (auth.TierClaim + AuthAckEnvelope.tier + AuthResult.tier + SessionState.tier + _bind_auth_result assign + auth-ack log field) | contracts/auth.py + credentials/auth_handshake.py + server/session.py + server/_core.py | DELETED (ADR 0270, wave 11): plugin reads only user_id/is_anonymous off the ack (zero tier readers); SessionState.tier was write-only (no tier-gating anywhere). tier=template retrieval pool + OpenRouter :free are UNRELATED (word-share only), untouched. CONTRACT PROOF: test_auth_ack_tier_field_removed (extra=forbid) + not hasattr(ack, "tier"). reopen: a real commercial-tier gate ships. ~40 LOC. | DELETED (commit-pending) | ADR 0270 |
| Anonymous-identity plumbing (session anon-id mirror _SESSION_ANON_ID/_get/_set/_apply_session_anon_hint + AuthTokenEnvelope.anonymous_user_id hint + plugin sticky-replay: trid3nt_client.anonymous_user_id, plugin_settings property+_ULID_RE, ws_bridge params, dock wiring) | server/session.py + server/_core.py + contracts/auth.py + qgis-plugin (trid3nt_client/plugin_settings/ws_bridge/dock) | DELETED (ADR 0270, wave 11). TRACE-FIRST VERDICT: the local user row is CONSTANT-keyed (LOCAL_SINGLE_USER_ID); case ownership keys off state.authenticated_user_id == that constant, NOT the anon hint. authenticate_token ignores every hint. So the sticky id + dual-socket convergence mirror are 100% vestigial. Coordinated wire change (auth-token drops anonymous_user_id, extra=forbid); server + plugin ship together (0.3.16 sync). WIRE PROOF: test_auth_token_envelope_rejects_anonymous_user_id + convergence rewrite. NO persisted-shape concern (transient wire hint, sticky id lived only in plugin QSettings). reopen: real multi-user identity returns (the s2z blueprint's remit, not the local product). ~250 LOC. | DELETED (commit-pending) | ADR 0270 |
| Compute-class/vCPU instance sizing (COMPUTE_CLASS_SIZING + select_compute_class + thresholds/_env_int/FALLBACK + solve_progress_vcpus + the six composer effective_compute_class/vcpus blocks + the dead SWMM-confirm Batch else-branch) | tools/simulation/solver/solver.py + gates/cards/solver_confirm.py + 6 composers | DELETED (ADR 0270, wave 11): ONE local compute environment (host CPUs). solve_progress_vcpus already returned os.cpu_count() unconditionally; COMPUTE_CLASS_SIZING's vcpu/mem/omp map was never applied to the docker run (only stamped the telemetry ExecutionHandle.compute_class); select_compute_class picked an instance tier the single box ignores. KEPT: the #154 GranularitySuggestion (cells/MB/runtime -- gate estimate tests stay green), the compute_class param + _COMPUTE_CLASS_ALIAS + ExecutionHandle.compute_class telemetry, sfincs_builder.resolve_solve_vcpus (SEPARATE perf-model cap). Tests test_select_compute_class (459) + test_solve_progress_vcpus (88) deleted. reopen: a multi-instance cloud solver backend returns (s2z blueprint). ~360 LOC src. | DELETED (commit-pending) | ADR 0270 |
| Mode-2 offer-to-add catalog flow (mode2_classifier module + interactions catalog-offer registry/TTL/probe/complete/handle + catalog_common.append_user_catalog_entry + _core emit/dispatch + ws ProbeFindings/SuggestedCatalogEntry/OfferCatalogAddition/CatalogAdditionResponse envelopes + plugin Mode2CandidateCard/gate parse+resolve/dock wiring/client respond_catalog_addition) | server (mode2_classifier/interactions/config/catalog_common/_core) + contracts/ws.py + qgis-plugin (cards/gate/dock/trid3nt_client) | DELETED (ADR 0270, wave 11): NATE ruled moot -- endpoint additions are hand-authored + discovery covers the need. KEPT: the _merge_user_overlay READ path (hand-authored user_catalog.yaml still surfaces) -- only the append WRITER + the offer machinery go. Tests test_mode2_classifier (219) + test_catalog_offer_loop (239) + plugin test_mode2_offer (364) + qt_mode2_offer_harness (221) deleted; test_catalog_user_overlay rewritten to seed YAML directly (keeps merge-read coverage). reopen: a user-tool-builder authoring UI wants an in-app add flow. ~1400 LOC. | DELETED (commit-pending) | ADR 0270 |

| SOLVER_CONFIRM_TOOLS + FETCH_CONFIRM_TOOLS hand-wired name-set LITERALS (paragraph-comment-per-entry) | server/src/trid3nt_server/server/_core.py ~564-647 | DELETED (ADR 0273, gate-collapse): membership is now `AtomicToolMetadata.gate_spec` presence (`_gate_spec_for`). The named sets survive ONLY as registry-DERIVED views via `_core.__getattr__` (`_confirm_tools_by_kind`) so the ~15 membership-assertion tests + any reader keep working; the source of truth is the specs. ~85 LOC. reopen: never (metadata is the carrier). | DELETED (commit-pending) | ADR 0273 |
| `_gate_on_solver_confirm` seven per-engine locals + per-engine `if/elif` card-building chain + per-engine decision-tail branches | server/src/trid3nt_server/server/_core.py ~5732-6247 | DELETED (ADR 0273): replaced by the ONE generic `_gate_on_confirm` engine (spec-driven: estimate provider builds the card, pin provider applies the decision). `_gate_on_solver_confirm` KEPT as a thin compat shim (resolve gate_spec -> delegate) so the gate-behavior suite drives it unchanged. The per-engine card BUILDERS (`_build_fire/geoclaw/psha/scenario_confirm_envelope`) + tail-clamp helpers stay in solver_confirm.py, now wrapped by the tools' declared estimate/pin providers (byte-identical card payload). ~516 LOC of dispatch/tail collapsed. reopen: never. | DELETED (commit-pending) | ADR 0273 |
| P6/P7/P8 mislabel corrections (NO constant deleted) -- `flood_2d.py` peak_inflow_cfs `basis="user"` mislabel + the invalid `basis="default"` on equation_set/computation_interval; `levee_breach.py` breach_params `consequence="scenario"`; `riverine_flood.py`/`levee_breach.py` geometry `consequence="physics"` | trid3nt_server/workflows/{hecras,swmm,openquake,elmfire,telemac,swan} | law 9 tail (rows 12-15, 18, 26, 32, 34): correct mislabels / wire gates / add the demo-geometry opt-in / label the SILENT stragglers -- WITHOUT deleting a constant (the demo values that DIED as runtime defaults were removed in P2-P4; P6-P8 are relabels + gate-wirings). | DELETED-as-mislabel (ADR 0285 P6/P7/P8, commit-pending). What DIED: (a) flood_2d `peak_inflow_cfs` stamped `basis="user"` for the `_DEFAULT_PEAK_CFS=5000` fallback -> now `default_demo`+`physics` (refuses) when un-supplied; (b) the LATENT BUG `basis="default"` (not a valid `InputBasis` Literal, would raise on every default-equation-set inflow run) -> `default_demo`+`numerical`; (c) levee breach_params `scenario` -> `physics` (refuses, literature offer); (d) riverine/levee Muncie geometry `physics` -> consented `scenario` behind the new `run_demo_geometry: bool=False` opt-in (refuses via `HECRAS_DEMO_GEOMETRY_REQUIRED` when not opted in). NO `_DEFAULT_*` constant removed -- `_DEFAULT_PEAK_CFS`/`_DEFAULT_INLET_OPENING_M`/vs30 760 all stay as the value SHOWN in the refusal. secondary_perils Vs30 verified compliant (DEM-slope-derived), no change. | ADR 0285 P6/P7/P8 / demo-physics-defaults-audit.md rows 12-15,18,26,32,34 |

| SCHISM inline mesh `LayerURI` constructions (`schism-mesh-{run_id}` in `postprocess_schism`, `schism-wave-mesh-{run_id}` in `postprocess_schism_waves`, `schism-baroclinic-mesh-{run_id}` in `postprocess_schism_baroclinic`) | trid3nt_server/workflows/schism/postprocess_schism.py | DELETED (ADR 0286, Option B -- the 0283 precedent applied to SCHISM): the native out2d/salinity mesh now rides the emit-on-solve seam as a `kind="mesh"` `outputs.json` entry (`crs_authid`), authored + published by the composer via `results_mesh_seam.publish_results_mesh_via_seam`. CONDITION MET: the seam mesh layer is byte-equivalent field-for-field (name/style=`mesh_grid`/role=`context`/crs/uri/bbox) modulo the `layer_id` stem (`model-results-mesh-{run_id}` vs `schism-*-mesh-{run_id}` -- an idempotence key; web grouping rides `name`, ADR 0281/0283 bar). postprocess now returns only the typed raster peak(s); `metrics` carries `mesh_uri`/`n_nodes`/`n_layers`/`is_geographic`. reopen: never (native-mesh temporal is the seam's job now). | DELETED (commit-pending) | ADR 0286 |
| SCHISM hand-wired mesh emission `publish_input_layer(emitter, mesh_layer, role="context")` in ALL FOUR composers (tidal_hydro, pahm_surge, coupled_waves, baroclinic_circulation) + the `mesh_layer = layers[N]` unpacks | trid3nt_server/workflows/schism/{tidal_hydro,pahm_surge,coupled_waves,baroclinic_circulation}/*.py | DELETED (ADR 0286): the charter law-8 bespoke-emission the campaign removes -- superseded by the one framework-owned `results_mesh_seam` publisher. CONDITION MET: byte-equivalence (above) + live proof (surge run `01M0AP17H1729N4XWPJF0SBCM9`, baroclinic run `01M0ANVQ7SKY2M5XTNKDR3VKFV`: outputs.json mesh entry -> seam `layer_type="mesh"` layer -> MDAL temporal load). The ADR-0244 sweep-guard allowlist updated for the retired sites (tidal_hydro/coupled_waves -> 0, removed; baroclinic 2->1, pahm_surge 2->1; `results_mesh_seam.py` added). `publish_input_layer` import dropped from tidal_hydro + coupled_waves (kept in pahm_surge/baroclinic for the surviving best-track/bottom-salinity emits). reopen: never. | DELETED (commit-pending) | ADR 0286 |

| `topobathy._merge_sources` (mosaic-to-temp-GTiff helper) | trid3nt_server/data/fetchers/_router/hooks/topobathy.py ~966 | DELETED (ADR 0291, wave F1c): ZERO callers in src and tests (registered as "no callers" in ADR 0290's Parked list and left in place). It was also a THIRD copy of the merge that discarded `_composite_sources_to_array`'s painted flags -- exactly the blind spot F1c closes. CONDITION MET: severed consumer (grep = the def line only). The live merge is `_select_and_merge`; the byte-shape helper the merge tests drive is `_build_merged_topobathy` -> `_merge_topobathy_to_array`, both FIXED to key on paint rather than deleted (they carry the mosaic-precedence coverage). reopen: never (a fourth merge path is the defect). ~27 LOC. | DELETED (commit-pending) | ADR 0291 |
| `topobathy._build_merged_topobathy` + `topobathy._merge_topobathy_to_array` (the byte-shape merge twins) | trid3nt_server/data/fetchers/_router/hooks/topobathy.py ~772-876 + tests/test_router_topobathy.py (4 tests) | DELETED (ADR 0292, wave F1d): ZERO production callers -- ADR 0291 kept them because "they carry the mosaic-precedence coverage", but the only thing driving them was four tests, and they were a SECOND merge path that never received the ETOPO land-mask fix (`_mask_land_leg_ocean_fill` is applied in `_select_and_merge` only). A parallel merge that cannot reproduce the live merge's honesty is worse than no coverage: it pins the WRONG behaviour. CONDITION MET: severed consumer (grep in src+tests = zero after the four orphaned tests were deleted with them). The precedence + sentinel-mask + empty-source behaviours they covered are all exercised end to end through `_select_and_merge` in the same file's router tests. reopen: never (a second merge path is the defect). ~104 LOC src + ~90 LOC tests. | DELETED (commit-pending) | ADR 0292 |
| The OUT-OF-PROCESS SWMM lane, whole: `workers/_swmm_postprocess/` (package), `workers/swmm/` (`entrypoint.py` + `Dockerfile` + `run_inp.py` + `__init__.py`), `run_swmm.is_local_mode` / `stage_swmm_manifest` / `swmm_local_spec` / `register_swmm_solver`, the `urban_flood` `if not is_local_mode():` branch (dispatch + two-card sim cards + Batch telemetry + the register-only `_swmm_reg.layers[1:]` frame consumer + the Batch-output download fallback) with `_record_swmm_batch_solve_telemetry` / `_BatchSWMMRun` / `_download_batch_swmm_outputs`, and `tests/test_swmm_two_card_sim_observability.py` | workers + trid3nt_server/workflows/swmm + tests | the lane exists only for the AWS Batch backend; when that backend is decommissioned and the host-exec pyswmm path is the product, nothing can reach it | DELETED (ADR 0295, 2026-08-20, commit 8c6479d4). NATE ruling: ADR 0294 parked this as a fork with options (a) migrate / (b) delete; (b) picked. CONDITION MET on every count -- the AWS Batch backend is decommissioned, the `trid3nt-local/swmm` image the `TRID3NT_SWMM_LOCAL=0` dispatch targeted has never existed on any box (`docker images` confirms), and `stage_swmm_manifest` was the lane's ONLY manifest producer so deleting the composer branch severed the shim, the spec, the registration and the worker together. Deleted as the LANE, not the enumeration: stopping at the named pieces would have left a producer with no consumer. `SWMM_SOLVER_NAME` survives (the in-process lane tags its solve-progress + telemetry rows with it); `'swmm'` was never in solver.py's static `SOLVER_WORKFLOW_REGISTRY` literal so no orphan entry remains. Reference sweep to ZERO outside `docs/decisions/` + the dated `docs/validation/engine-coverage-inventory.md`. Coverage unchanged: all 15 registered SWMM tools route through the host-exec composer only and all are tested -- 153 tests green across 14 SWMM files. ~2775 LOC (1296 worker + 261 test file + 303 run_swmm + 485 urban_flood + ~430 test cases). reopen: never (a lane whose image does not exist is not a lane). | ADR 0295 |
| The eight DEAD `spec.fallback` cross-dataset lists: `fetch_gridmet [fetch_era5_reanalysis]`, `fetch_era5_reanalysis [fetch_mrms_qpe, fetch_hrrr_forecast]`, `fetch_aorc_precip [fetch_mrms_qpe, fetch_era5_reanalysis]`, `fetch_usgs_nwis_gauges [fetch_noaa_nwm_streamflow]`, `fetch_lter_records [fetch_usgs_nwis_gauges]`, `fetch_gtsm_tide_surge [fetch_noaa_coops_tides]`, `fetch_noaa_coops_tides [fetch_gtsm_tide_surge]`, `fetch_esri_landcover_10m [fetch_landcover]` -- plus the 27 empty `fallback: []` declarations | trid3nt_server/data/fetchers/**/source.yaml | a `fallback` entry that names something other than a key in its own spec's `endpoints` block cannot execute, because `vector_fgb.resolve_endpoints` is the field's ONLY reader and it resolves `spec.endpoints.get(fb)` | DELETED (ADR 0299, wave F2, 2026-08-21). The audit registered these as a silent cross-dataset substitution mechanism (row 8, "riding silently with no rung, no gate and no activation row"). PROVEN FALSE two ways: `endpoints.get(fb)` returns None for every sibling-tool name and the `if ep is not None` guard drops it, AND `select_executor` routes six of the eight to `library_delegate`/`http_json`/`record`/`tiled_mosaic` before `vector_fgb.execute` is ever reached. `http_json` never reads `spec.fallback` at all. So nothing swapped -- but `registration.spec_card` projected all nine lists verbatim and `stratified.py` rendered them into the model's own catalog text, so the model was told these sources have fallbacks they do not have. CONDITION MET: severed consumer (the entries were unreachable by construction). The one LIVE list, `fetch_nhd_waterbodies [medium]` (USGS NHDPlus HR -> medium-res: same producer, same dataset family, a resolution tier), is KEPT and renamed onto `endpoint_fallback`, whose contract is now SAME-DATA mirrors only and whose entries `_validate_hooks` refuses at registration if they name no endpoint of the spec. The 27 `fallback: []` lines went with them: an empty declaration of a mirror chain says nothing. reopen: never as `spec.fallback`; a cross-dataset alternative is a declared ladder rung (`trid3nt_server.fallbacks`), gated and stamped. Whether any of these eight pairs SHOULD become a real rung is parked for NATE in ADR 0299. | ADR 0299 |
| `generate_mesh._fetch_topobathy` (the land-DEM-as-coastal-bed fetch) | trid3nt_server/workflows/mesh/generate_mesh/generate_mesh.py ~655 | a helper named for topo-bathymetry that fetches `fetch_dem(source="3dep")` -- land only -- and samples it into a COASTAL mesh's bed | DELETED (ADR 0299, wave F2) and REPLACED by `_fetch_coastal_bed`, which routes `fetch_topobathy(target_crs="EPSG:4326", fallback=("etopo_bathy_base",))`. Not a rename: the source changed. Live on AOI (-85.45, 29.90, -85.35, 30.00) the old raster was 29.46% EXACTLY 0.0 m (flat sea-level fill, a fake landmass under every wet node of a mesh that feeds SCHISM tidal/baroclinic runs) and reached only -0.87 m; the new one is 0.00% at 0.0 and reaches -10.05 m. The constant `dem_source` string claiming "3DEP + NOAA CoNED where available" died with it -- CoNED was never fetched -- replaced by `_bed_provenance`, derived from the ladder activation rows. reopen: never (a coastal bed from a land DEM is the fake-landmass class). | ADR 0299 |
| matplotlib proof/montage interpretation scripts in `scripts/` (55 files import matplotlib; ~4.3k LOC in the top 20 alone) | scripts/ | superseded when the PyQGIS headless renderer (render toolset, declarative campaign) covers the georeferenced-raster montage class: renders via QGIS's own engine + plugin style presets + ESRI basemap. Sweep deletes the raster-montage drivers; chart-figure scripts (hydrographs etc.) survive only until chart-dock-exact rendering covers them, then follow. NATE ruling 2026-08-22: retirement MUST reduce total repo LOC - no orphaned interpreters. | QUEUED | declarative-campaign design doc |

| `_maybe_emit_do_sag_chart` + its call site in `_postprocess_and_publish_do_sag`, and that function's now-unread `location_name` parameter | `trid3nt_server/workflows/telemac/river_dye/river_dye.py` ~2892-3020 | do_sag-only chart machinery living inside the shared reach pipeline. CONDITION-to-delete: the DO-sag chart becomes a DECLARED `.chart()` node on do_sag's plan, so the declarative interpreter is the ONE builder/emitter and a second emission site would double-emit. | DELETED (ADR 0303, wave 1 of the declarative campaign). CONDITION MET: `steps.build_sag_chart` is declared as `.chart("do_sag_curve", builder=...)` on the do_sag plan and the interpreter runs it as its own ledger-tracked node. Byte-equivalence: the builder reads `sag_curve_distance_m` / `sag_curve_do_mgl` / `sag_curve_bod_mgl` / `do_standard_mgl` / `do_min_*` off the published `TelemacDoLayerURI`, which `postprocess_telemac_do` fills from the SAME expressions with the SAME rounding as the `metrics` dict the old function read - the vega-lite spec is identical. One visible delta: the chart TITLE now names the user's own `location` words instead of the geocoder's display name (the layer does not carry the geocoded name); recorded in ADR 0303 "behavior deltas". `location_name` went with it - the chart was its only reader. ~54 LOC. reopen: never (a second chart emitter for one workflow is the defect). | ADR 0303 |
| The DO-sag imperative composer body: `_do_saturation_mgl`, the `_clamp` closure and its six call sites, the four hand-written `SyntheticInput` WQ rows with their `basis="default_demo" if v == <literal> else "user"` comparisons, the `do_sag_config` assembly, and the five-branch `try/except` tail | `trid3nt_server/workflows/telemac/do_sag/do_sag.py` (HEAD 52b3b787, lines 94-314) | CONDITION-to-delete: the declarative library resolves the same values through declared doors/bounds and produces the same provenance rows and the same typed envelopes, proven by an old-vs-new reference run on the same question. | DELETED (ADR 0303). CONDITION MET: `PARAMS` + `plan(p, d)` + `resolve_params`/`validate_plan`/`interpret` replace the body; every one of the 39 inventoried behaviors is re-homed or recorded as a deliberate delta (ADR 0303 inventory table). `_do_saturation_mgl` moved to `steps.do_saturation_mgl` (same Elmore-Hayes coefficients, now reached as a door=DERIVED resolve path); its test moved with it. Three deliberate deltas recorded: a non-numeric bounded arg REFUSES instead of silently defaulting; the two banks/reach typed errors return the standard error envelope instead of propagating; the chart title source. AMENDED (ADR 0303 wave 1b): delta 2 is REVERTED - a `retryable` typed error PROPAGATES again, because `summarize_tool_result` harvests `retryable` + `.suggestions` off the raised exception and the flat envelope destroyed that channel. The envelope conversion stays for terminal errors only. A fourth delta was added: an auxiliary chart/render node failure is non-fatal. reopen: never. | ADR 0303 -> 0303 wave 1b |

| `scripts/prove_declarative_resume.py` (the live resume harness) | scripts/ | its premise - forcing the declared chart node to raise so the plan FAILS after the solve and leaves a resumable ledger - is contradicted by ADR 0303 wave 1b delta 4, under which a chart/render node failure is non-fatal and the run completes (and therefore reaps its own ledger). | DELETED (ADR 0303 wave 1b). CONDITION MET: the behavior it demonstrated cannot occur. do_sag's plan has exactly ONE recordable node (the terminal solve) and one auxiliary node, so no failure leaves anything behind to resume - do_sag can never resume, by construction. Resume-from-failed-step, the artifact-existence probe, completed-invocation re-execution, the Data-production ledger and the recorded-domain restore are all pinned offline in `tests/test_declarative_library.py` (7 tests). The LIVE resume proof moves to wave 3, where river_dye's multi-step plan produces a failure with completed work behind it. `scripts/run_do_sag_direct.py` is untouched - it is the reference-parity driver, not a resume harness. ~91 LOC. reopen: never under this name; wave 3 writes its own harness against a plan that can actually resume. | ADR 0303 wave 1b |
| `ChartSpec.x` / `ChartSpec.y` + the `x=` / `y=` arguments of `Step.chart()` | trid3nt_server/declarative/plan.py + the do_sag plan's `.chart(...)` call | declared, stored on the frozen spec, and read by NOBODY - `_run_chart` passes only `(result, params)` to the builder, and the builder writes the vega-lite `encoding` block itself | DELETED (ADR 0303 wave 1b, observation 7). The fork was "make `_run_chart` honor them or drop them"; honoring them means the interpreter rewriting a builder-authored vega-lite spec, which contradicts the design's own ruling that the chart SPEC is the product and the builder owns it. A declaration nothing reads is a claim about ownership that is not true. The design doc's illustrative `plan()` sketch still shows `x=`/`y=` - it is pseudo-code alongside constructors that do not exist either, so it is left as the sketch it is. reopen: if a future renderer needs axis metadata SEPARATE from the spec, it is a field on the payload, not on the plan. | ADR 0303 wave 1b |

| `Within` + `Gate.constrain` + the `constrain=` argument of `DrawGate()` (draw-time geometry constraints) | trid3nt_server/declarative/plan.py + validate.py + the design doc's example plan | declared on the frozen Gate, ref-checked by the validator, and read by NOBODY - `_run_gate` ignores it entirely. The fork was "enforce it or remove it"; enforcement is not possible where it sits, because the geometry to constrain against is produced AFTER the gates (wave 1b observation 10), and the wave-2 plugin draw card is what will both collect the drawn value and constrain it. | DELETED (ADR 0303 wave 1c, observation 4). Grep-to-zero in product code + tests. reopen: the wave-2 draw card lands, at which point the constraint is declared next to a gate that can enforce it. | ADR 0303 wave 1c |
| `RenderSpec.zero` + the `Transparent` sentinel + the `zero=` argument of `Step.render()` | trid3nt_server/declarative/plan.py + the design doc's example plan | same dead-declaration class: `_run_render` passes only `style_preset` to `publish_layer`, and `publish_layer` - the ONE raster-styling chokepoint - has no zero-handling knob to declare against, so honoring it would mean inventing a styling path outside the chokepoint. | DELETED (ADR 0303 wave 1c, observation 5). Grep-to-zero. reopen: the render-toolset wave promotes the publish seam into declared primitives and gives zero-as-transparent a real reader. | ADR 0303 wave 1c |
| `RENDER_SOURCE_UNRENDERABLE` error code | trid3nt_server/declarative/interpret.py | it conflated a STYLING failure (auxiliary - the run's primary result stands) with the step having produced no raster at all (a primary defect against the honesty floor), so both arrived as a non-fatal note. | DELETED (ADR 0303 wave 1c, observation 3), split into `RENDER_SOURCE_MISSING` (fatal, `RenderSourceMissingError`) and `RENDER_STYLE_FAILED` (auxiliary note). Grep-to-zero. | ADR 0303 wave 1c |
| `GateNotSupportedError` / `GATE_NOT_YET_SUPPORTED` | trid3nt_server/declarative/errors.py (+ package re-export) | it was a placeholder for the wave-2 draw card: a `user_gated` draw gate with a live session had nowhere to draw, so it said so and named the wave. Condition: the draw card ships and the gate can actually ask. | DELETED (ADR 0304). CONDITION MET: `gate_draw_input` emits the card on the existing `spatial-input-request` spine and waits; every no-value path now refuses `GATE_INPUT_REQUIRED` naming the param and the reason (no session, declined, timed out, no geometry). Grep-to-zero in code (the string survives only in the ADR 0303 history note). | ADR 0304 |

| `model_telemac_river_dye` (the 870-line reach composer) + `_maybe_emit` + the `pipeline_emitter` parameter | trid3nt_server/workflows/telemac/river_dye/river_dye.py | the composer is superseded by the declared plan over the shared `workflows/telemac/steps/` family; `_maybe_emit` + `pipeline_emitter` were a DEAD seam - nothing ever passed `pipeline_emitter`, so `_maybe_emit` always ran the invoke DIRECT and the emit-on-fetch cards came from the fetchers' own seam. Condition: the migrated plan reproduces the reference physics. | DELETED (ADR 0305). CONDITION MET: old `01M0S9PP07192HC75WPDPKE6HK` vs new `01M0SAZ6ED8Y82ZMPT1Q07F8CD`, BIT-IDENTICAL on cmax / peak time / plume reach / active frames / mesh / bbox. Grep-to-zero in code. | ADR 0305 |
| `plausible_release_coords` | trid3nt_server/workflows/telemac/river_dye/river_dye.py (+ its 4 call sites: the tool sanitize, the reach threading, the preview mirror, the approve-mesh gate) | it DROPPED an implausible release point with a warning and ran at `spill_fraction` instead - modelling a different release location than the one asked for, which is the swallow class law 9 outlaws. Condition: a coercion that REFUSES replaces it everywhere, matching `coerce_outfall_point`. | DELETED (ADR 0305). CONDITION MET: `steps.reach.coerce_lonlat_point` refuses `TELEMAC_PARAMS_INVALID`; the one fail-OPEN caller (the pre-dispatch gate builder) catches the refusal explicitly at its call site with the reason stated. Grep-to-zero. | ADR 0305 |
| `_clamp_domain_extent` + `_pos_float` | trid3nt_server/workflows/telemac/river_dye/river_dye.py | 24 inline `try/float/clamp` blocks that a declared `Param` bound now states once - and states to the docstring, the form card and the provenance row at the same time. `_pos_float` additionally SWALLOWED a below-range value by returning `None` (the knob silently reverted to the deck literal). Condition: every clamped arg is a declared Param with bounds. | DELETED (ADR 0305). CONDITION MET: rows 10-30 of the ADR 0305 inventory; the resolver stamps `CLAMPED from X to the declared maximum Y` per row, which also retires the lumped `domain_extent_clamped` provenance entry. Grep-to-zero in code. | ADR 0305 |
| `RunTelemacError` | trid3nt_server/workflows/telemac/river_dye/river_dye.py | declared, exported in `__all__`, and RAISED BY NOBODY anywhere in the repo. | DELETED (ADR 0305). Grep-to-zero (the name survives only in the ADR 0303 history table). | ADR 0305 |
| the `_release_seeds_reach` / `_seed_release_lon` / `_seed_release_lat` tri-state THREADING | trid3nt_server/workflows/telemac/river_dye/river_dye.py | three private kwargs carrying one decision - which point the worker resolves the reach centerline from - resolved twice (once in the tool, once in the composer) with a comment explaining the tri-state at each site. Condition: one declared param states the decision. | DELETED (ADR 0305). CONDITION MET: `reach_seed_coords` (door=USER, optional, `derived_when_absent`) is the one declaration; `_normalize` resolves the tri-state before any door. The three kwargs REMAIN on the tool signature because the approve-mesh decision tail writes them - they are wire shapes now, not a threaded state. | ADR 0305 |
| `preview_telemac_mesh`'s mirrored reach front (its own geocode / river fetch / seed derivation) + the MUST-MATCH NOTE that guarded it | trid3nt_server/workflows/telemac/river_dye/river_dye.py | ~100 lines duplicating Stages 1-2 of the composer, with a comment reading "If you change the seed logic THERE, change it HERE" - a drift hazard documented instead of removed. Condition: the reach front is a shared step the preview can call. | DELETED (ADR 0305). CONDITION MET: `steps/mesh_preview.py` calls `geocode_reach` / `fetch_reach_flowline` / `reach_seed`. One seed derivation, so preview-vs-solve drift is impossible rather than merely warned about. It also fixed a LIVE inconsistency the mirror had: the preview clamped the reach to 8 km while the tool allowed 15 - that clamp actually landed in wave 3b (ADR 0305 correction B1); wave 3 documented it without changing the code. | ADR 0305 |
| `scripts/drive_do_sag_cards.py`'s one-off WS client (its own `_answer_draw` / `_answer_warning` / turn pump / run-prefix reader) | scripts/ | a scripted WS client written per driver. Condition: a reusable harness exists that answers gates from DECLARED responses and asserts on the run's own artifacts. | DELETED (ADR 0305). CONDITION MET: `trid3nt_server/testing/{ws_client,live_run}.py`; the driver is now a `LiveRun` + `GateAnswers` declaration (254 -> 90 lines) and `scripts/drive_river_dye_cards.py` is a second one at 105. | ADR 0305 |
| `scripts/seed_showcase_cases.py`'s copies of the WS protocol primitives (`mk`, `_handshake`, `_create_case`, `delete_case`, `_auto_approve_request`, `_BLOCKING`, `_parse_tool_status`, `WS_URL`) | scripts/ | one implementation of the wire shapes, not one per driver. Condition: the harness owns them. | DELETED (ADR 0305). CONDITION MET: the module imports them from `trid3nt_server.testing.ws_client` and keeps its private aliases, so the four drivers importing them from here keep working through a real dependency rather than a shim. `_auto_confirm_warning` STAYS - its coarsening logic is showcase-specific. | ADR 0305 |
| `model_regional_water_budget_scenario` + `RegionalWaterBudgetResult` + `RegionalWaterBudgetScenarioError` + `RegionalWaterBudgetInputError` | trid3nt_server/workflows/modflow/regional_water_budget/regional_water_budget.py | DELETED (ADR 0306, the generalization checkpoint): the composer became PARAMS + plan(p, d) over workflows/modflow/steps/. The `*Result` GraceModel was always `.model_dump(mode="json")`-ed by the thin tool, so the wire shape is unchanged; the two error classes are replaced by the shared step family's typed errors, with `REGIONAL_WATER_BUDGET_INPUT_INVALID` preserved verbatim. TRACE: grep-to-zero across trid3nt_server/, tests/, scripts/; tests/test_modflow_archetypes.py + tests/test_engine_chart_emission.py repointed onto the registered tool. reopen: n/a. ~190 LOC. | DELETED (commit-pending) | ADR 0306 |
| the POST-solve `gate_and_stamp_modflow_inputs` review in regional_water_budget | trid3nt_server/workflows/modflow/regional_water_budget/regional_water_budget.py | DELETED (ADR 0306): a DEAD GATE - it presented the aquifer values for approval AFTER the consequential mf6 solve, so nothing it could have changed was still ahead of it (the exact placement the plan validator refuses). HEAD gated the same two values twice, once usefully (pre-solve) and once not. The STAMP survives as `merge_provenance` in the tool body. reopen: n/a. | DELETED (commit-pending) | ADR 0306 |
| `solve_aquifer_deck` + `default_two_storm_forcing` + `_node_chart_spec` + `_geocode_site` + the hand-wired `current_emitter().emit_chart` block | trid3nt_server/workflows/swmm/aquifer_baseflow/aquifer_baseflow.py | DELETED (ADR 0306): the private pyswmm loop is replaced by the shared `swmm/steps/solve.solve_deck` (declared sampling, reusable by the family); the baked demo hyetograph is replaced by five declared SCENARIO params; the chart is a declared `.chart(...)` builder; the geocode is `workflows/shared/site_resolve`; the emitter hand-wiring is law 8. TRACE: grep-to-zero; scripts/proof_swmm_aquifer_baseflow.py + scripts/proof_law9_soil_column_ab.py repointed and re-run green. reopen: n/a. ~150 LOC. | DELETED (commit-pending) | ADR 0306 |
| the `S1` subcatchment-runoff series collected by `solve_aquifer_deck` | trid3nt_server/workflows/swmm/aquifer_baseflow/aquifer_baseflow.py | DELETED (ADR 0306): sampled at every timestep and never read by any caller. The shared solve step takes its sampled objects as declared arguments, so an unread series cannot be collected by accident. reopen: a template that actually charts subcatchment runoff declares `subcatchments=(...)`. | DELETED (commit-pending) | ADR 0306 |
| `_solve_swmm_node_rdii` (the RTK template's own pyswmm loop, hard-wired to node `N1`) + `build_rdii_chart_spec` + the `current_emitter()` / `hasattr(emitter, "emit_chart")` hand-wiring + `chart_emitted` | trid3nt_server/workflows/swmm/rdii_rtk/rdii_rtk.py | a second pyswmm run loop beside the shared `steps/solve.Solve.pyswmm`, and a second chart emitter beside the interpreter's declared `.chart()` node (law 8). Condition: the declared plan reproduces the reference physics through the shared solve. | DELETED (ADR 0307 wave A). CONDITION MET: BIT-IDENTICAL on both reference invocations (defaults and the EPA Table 7-1 case) - peak 3.2951 / 1.0599 cfs, native-SWMM peak 3.2804 / 1.0534, volume identity 1.00006, and all three `curves` arrays element-for-element - and the deck text is byte-identical. `chart_emitted` (a bool the tool could not observe) becomes `chart_specs` (what the run BUILT), per ADR 0306 delta 6. Grep-to-zero in code. reopen: never - a second run loop or a second emitter for one engine is the defect. | ADR 0307 |
| `cross_check_swmm` (the RTK template's optional-native-cross-check flag) + its `try/except Exception: logger.warning(...)` swallow | trid3nt_server/workflows/swmm/rdii_rtk/rdii_rtk.py (+ the `scripts/seed_showcase_cases.py` arg that set it) | a best-effort branch that returned `status="ok"` with `swmm_rdii_peak_cfs: null` when the engine the closed form is VALIDATED AGAINST failed - an unvalidated number reported as a validated one. It also cannot survive as a `When`: the answer step must `Ref` the guarded solve, and a `When` body is a scope. Condition: the cross-check becomes a declared consequential step whose typed failure reaches the caller. | DELETED (ADR 0307 wave A, delta 1 + delta 2). CONDITION MET: `Deck.rtk_rdii` + `Solve.pyswmm` are unconditional plan nodes; a solver failure now carries `SWMM_SOLVE_FAILED`. The fast path the flag served (skip the engine) is the closed-form STEP, which the offline tests call directly. Grep-to-zero. reopen: never. | ADR 0307 |
| the four `EPA_TABLE_7_1_*` module constants (area, sum R, the published hourly rainfall, the published Figure 7-10 node flows) | trid3nt_server/workflows/swmm/rdii_rtk/rdii_rtk.py | the purity rule (charter-grade): zero demo constants in tool/workflow code; demos live in banner-labeled demo scripts. Condition: a saved, banner-labeled path-A invocation owns them and every reader imports from there. | DELETED (ADR 0307 wave A). CONDITION MET: `scripts/demo_swmm_rdii_epa_table_7_1.py` holds them as `AREA_AC` / `SUM_R` / `RAINFALL_IN_PER_HR` / `PUBLISHED_RDII_CFS` plus the `ARGS` invocation; `tests/test_swmm_rdii_rtk.py` and `scripts/proof_swmm_rdii.py` both import from it, which also collapsed the proof's own duplicate copy of `UHS` / `AREA` / `_PUBLISHED`. Grep-to-zero in `trid3nt_server/`. reopen: never - published replication numbers are a demo declaration, not workflow code. | ADR 0307 |
| `default_rain_on_snow_forcing` (the baked 5-day Buffalo demo temperature + precipitation series) + `_coerce` (which returned `None` on garbage, so a malformed series silently reverted to it) | trid3nt_server/workflows/swmm/snowmelt_degree_day/snowmelt_degree_day.py | a demo function inside workflow code, plus the swallow that made it the SILENT fallback for bad input - modelling a different storm than the caller asked for. Condition: the forcing PATTERN is declared as bounded params and a malformed series refuses typed. | DELETED (ADR 0307 wave A, rows 1 and 3 + delta 9). CONDITION MET: twelve declared Params drive `steps.rain_on_snow_forcing`, which reproduces `default_rain_on_snow_forcing(60)` BYTE-IDENTICALLY on both series; `steps.coerce_series` (shared) refuses `SWMM_DECK_INVALID`. Every number is now a labeled, bounded, editable form row - the drive revised `snowfall_intensity_in_hr` 0.05 -> 0.10 and the run's own chart narrates peak SWE 2.40 in against the reference 1.20 in. Grep-to-zero. reopen: never. | ADR 0307 |
| `solve_snowmelt_deck` (a third pyswmm run loop, hard-wired to `S1` and to a 5-tuple return) + `_total_melt_in` + `_peak` + `_swe_chart_spec` + `_runoff_chart_spec` + the two-iteration `emit_chart` loop + `charts_emitted` | trid3nt_server/workflows/swmm/snowmelt_degree_day/snowmelt_degree_day.py | the same duplication class as the RTK row: a private run loop, a private argmax and private chart-spec builders beside the shared family. Condition: the shared solve can sample MULTIPLE attributes off one object, which is what a snowpack question needs and the family did not have. | DELETED (ADR 0307 wave A). CONDITION MET: `steps/solve.solve_deck` gained `node_attrs` / `subcatchment_attrs` (one solve, three attributes) and reports BOTH continuity errors; `steps/series.peak` and `steps/charts.line_chart_spec` are shared. BIT-IDENTICAL on the reference question (peak SWE 1.2 in, melt 1.2 in, snowmelt peak 7.329 cfs @ 71 h, rain-only 6.0502, amplification 1.2114, cold-period fraction 0.3894, plowed peak SWE 0.502, continuity 0.0%) plus all five `curves` arrays. Grep-to-zero. reopen: never. | ADR 0307 |
| `snow_removal` (the flag gating the plow-removal variant) and the `None` values it produced for `removal_peak_swe_in` / `removal_runoff_peak_cfs` | trid3nt_server/workflows/swmm/snowmelt_degree_day/snowmelt_degree_day.py | same structural reason as `cross_check_swmm`: the metrics step must `Ref` the plowed solve, and a `When` body is a scope, so the variant is either declared or hidden inside a composite. On the merits the plow comparison is named in the template's own question and the deck is five days of hourly steps on one subcatchment. Condition: the variant becomes an unconditional plan node. | DELETED (ADR 0307 wave A, delta 7). CONDITION MET: three declared `Deck` + `Solve` pairs; the two removal scalars are always numbers. The DEFAULT path is unchanged (HEAD defaulted the flag to True); only a caller who explicitly passed False pays for the third solve, and gets a number instead of a `null`. Grep-to-zero. reopen: if a wave-D giant ever makes an optional variant expensive enough to matter, the answer is a separate tool, not a dead branch. | ADR 0307 |
| `aquifer_baseflow/steps.py`'s private `_coerce_series`, `_peak`, the inline `TSER_R` f-string and the inline vega-lite spec dict | trid3nt_server/workflows/swmm/aquifer_baseflow/steps.py | four idioms the checkpoint wrote privately because the shared family did not have them yet. Condition: `steps/series.py` and `steps/charts.py` exist and the repoint is proven bit-identical. | DELETED (ADR 0307 wave A). CONDITION MET: repointed onto `coerce_series` / `peak` / `timeseries_block` / `line_chart_spec`; the Ames reference re-run AFTER the repoint is BIT-IDENTICAL (2.30071 @ 8.25 h, 1.50835, 0.60124 vs 0.0, tau 703.15, bump 1.49741, continuity 0.0%, column 0.4637/0.1963/0.3568/0.1318). The file is 18 lines SHORTER - the shared family's first repayment. reopen: never. | ADR 0307 |
| The canopy species: `compute_canopy_height` tool (`trid3nt_server/tools/processing/compute_canopy_height/` -- tool + corpus.yaml + `CanopyHeightError`) + `workers/canopy/` (Dockerfile + `entrypoint.py` + `test_entrypoint_cog.py`, the Meta HighResCanopyHeight AWS Batch worker scaffold) + `tests/test_compute_canopy_height.py` + the `"canopy": "canopy_height_inference"` presence-gate entry in `SOLVER_WORKFLOW_REGISTRY` (`trid3nt_server/workflows/solver/solver.py`) + the `canopy_height_m` style preset (`trid3nt_server/tools/publish_layer/publish_layer.py`) + the tool import (`trid3nt_server/tools/__init__.py`) + the `compute_canopy_height` timeout override (`scripts/tool_sweep.py`) + the `canopy/` cloud-lane mention in `workers/README.md` | trid3nt_server/tools/processing/compute_canopy_height + workers/canopy + tests + solver.py + publish_layer.py + tools/__init__.py + scripts/tool_sweep.py + workers/README.md | NATE ruling 2026-08-24: the unfinished attempt to wrap the canopy QGIS plugin's AI canopy-height detection as an agent tool species is DELETED outright, not disabled, so the control plane stays homogeneous; the QGIS-plugins-as-tools idea is re-evaluated later from scratch. | DELETED (commit e22331c9). TRACE-FIRST VERDICT: genuinely unfinished, not an independent working capability -- `compute_canopy_height` dispatched through the generic `run_solver('canopy', ...)` seam onto a `LOCAL_SOLVER_SPEC_REGISTRY` entry that was NEVER registered (grep-to-zero on `register_local_solver_spec(CANOPY_SOLVER_NAME, ...)` anywhere in the repo, and `workers/README.md` said so outright: "canopy has no local dispatch wiring yet"); `workers/canopy/Dockerfile` was an explicit "FILE-ONLY SCAFFOLD (NOT built / pushed here)" targeting the now-decommissioned AWS Batch cloud lane; `tests/test_compute_canopy_height.py` mocked every seam (`run_solver`/`wait_for_completion`/`publish_layer`/`fetch_naip`) so green tests proved the glue shape, never a real solve. DISTINGUISHED and KEPT: `fetch_usfs_canopy_fuels` (an independent, spec-driven LANDFIRE CBH/CBD fuels fetcher, unrelated data path, registered + corpus'd + untouched) and every other "canopy" reference in the repo (ELMFIRE fuel-model prose, NDVI/UHI docstrings, the `mesh_acquisition.py` bare-earth-vs-canopy-DSM cross-dataset-fallback guard) -- all pre-existing, working, and orthogonal to the deleted species. Registry 253 tools post-deletion (`fetch_usfs_canopy_fuels` the only remaining `canopy`-named entry); `retrieve_visible_tools` fail-open sanity confirmed no deleted name resurfaces. Grep-to-zero on `compute_canopy_height` / `CANOPY_SOLVER_NAME` / `canopy_height_m` / `CanopyHeightError` / `canopy_height_inference` / `workers/canopy` across trid3nt_server, workers, tests, contracts, scripts. Historical mentions (ADR 0080, ADR 0078, ADR 0270, dated audit reports under docs/design + docs/specs + docs/reports, the generated `docs/site/tool-support.md` sweep snapshot) are left untouched as point-in-time records, per the never-rewrite-history norm. ~1885 LOC deleted (compute_canopy_height.py 607 + corpus.yaml 9 + test 333 + workers/canopy Dockerfile 190 + entrypoint.py 554 + __init__.py 11 + test_entrypoint_cog.py 181). reopen: the QGIS-plugins-as-tools evaluation, from scratch, later. | NATE ruling 2026-08-24 window |

| `trid3nt_server/workflows/telemac/do_sag/steps.py` in full: `ReachSolve.telemac_waqtel_o2` (the 17-kwarg composite), `solve_waqtel_o2` (the imperative reach pipeline), `_review_resolved_inputs`, `OutfallCoordsInvalidError` + `coerce_outfall_point`, `do_saturation_mgl` / `upstream_do_mgl`, `build_sag_chart` | trid3nt_server/workflows/telemac/do_sag | the skeleton's demolition clause: the composite is the named disease exhibit, and it dies rather than being deprecated. Condition: do_sag's pipeline is DECLARED as plan nodes over the shared TELEMAC step family (the same nodes river_dye already declares), so the composite has nothing left to funnel. | DELETED (wave 2, skeleton + cohort). CONDITION MET: `do_sag.plan(p, d, ops)` declares geocode -> seed -> carrier discharge -> WAQTEL process -> resolved-input review -> deck -> solve -> DO products through `TelemacWorkflow`'s five operations, i.e. exactly the pipeline `solve_waqtel_o2` ran imperatively. Re-homed rather than lost: the resolved-input review -> `steps/forcing.review_resolved_inputs` + `ReviewResolvedInputs` (still `self_gating`); the saturation clamp + the `do_sag_config` block -> `steps/water_quality.waqtel_o2_process`, resolved ONCE as a named step both the deck and the postprocess `Ref` (it used to be computed inside the composite and passed to both); the two derivations -> `steps/water_quality`; `coerce_outfall_point` -> the shared `steps/reach.coerce_lonlat_point` with a `label` argument (one point coercion, not two); `build_sag_chart` -> COLOCATED in `do_sag.py` beside the plan, referenced as a function object. PARITY: coarse canary DO minimum 9.0099 mg/L at 158.8 m, identical to the pre-migration run. 278 LOC deleted. reopen: never - a composite that funnels a template's whole sheet through one step is what the skeleton exists to prevent. | skeleton wave 2 |
| The dotted-STRING chart builder (`ChartSpec.builder: str` + the interpreter's `_load(spec.builder)` resolution) and every `builder=f"{_STEPS}...."` reference | trid3nt_server/workflows/lib/plan.py + interpreter.py + all 6 declarative templates | the skeleton's chart contract: a chart is a plain function object colocated with its plan, so "does this builder exist" is answered while the plan value is built, not after the solve it was meant to describe. Condition: `ChartSpec.builder` takes the callable and the interpreter calls it directly. | DELETED (wave 2, skeleton + cohort). CONDITION MET: `ChartSpec.builder` is `Callable`, and its `__post_init__` REFUSES a string with the fix in the message - no fallback path, per the demolition clause. `builder_path` (module.qualname) still names the code in the ledger record. All six declarative templates now pass function objects; `build_dye_chart` moved out of `steps/products.py` into `river_dye.py` and `build_sag_chart` into `do_sag.py`. reopen: never. | skeleton wave 2 |
| The `Workflow(name, engine=...)[...]` plan-value CONSTRUCTOR (`workflows/lib/plan.py`) | trid3nt_server/workflows/lib + all 6 declarative templates + the declarative tests | the name `Workflow` belongs to the skeleton BASE CLASS (NATE naming ruling 2026-08-24), and the constructor was also a redundant restatement: the registration metadata already carries the workflow's name and the facade carries its engine. Condition: the skeleton builds the `Plan` from the declaration, and `plan()` returns the step sequence. | DELETED (wave 2, skeleton + cohort). CONDITION MET: `Workflow` is now the abstract skeleton; `Plan` gained the shape check the subscript used to do, and the migrated cohort's `plan(p, d, ops)` returns a plain sequence the skeleton names and engines. The four UN-migrated declarative templates were adapted mechanically to `Plan(name, engine, (...))` - no shim, no alias. reopen: never. | skeleton wave 2 |

| The `TemplateCard` species: `workflows/<engine>/_template_card.py` (12 copies) + every module-level `TEMPLATE_CARD` declaration + `QUESTION` constants that only feed one | trid3nt_server/workflows/*/ (105 modules mention it) | the cards exist so an ENGINE DOOR can list a curated question + required inputs + knobs instead of deriving them from the docstring. The doors were DISSOLVED (stratified-pools amendment 2026-08-03: templates rejoined the main search surface, the 9 `run_<engine>` doors deleted), and grep-to-zero now confirms nothing reads `.question` / `.required_inputs` / `.knobs` anywhere under `tools/`. Condition: NATE confirms no future surface wants a curated card (the search corpus + the docstring routing block are what the model actually reads today), then the species is deleted engine-by-engine rather than left as decoration every new template copies. | DELETED FOR TELEMAC (family migration wave); QUEUED for the other eleven engines. CONDITION MET FOR THIS ENGINE, exactly as the row's own "engine-by-engine" clause describes: the four migrated templates dropped their cards as they were rewritten, the two cohort templates' cards were stripped with them, and `workflows/telemac/_template_card.py` then had zero readers and is gone. The QUESTION prose it carried is not lost - it moved into each template's module docstring, where a reader of the file finds it. `rain_on_grid` never had one. Each remaining engine's copy dies with that engine's migration; grep `workflows/telemac` for TEMPLATE_CARD returns nothing. reopen: a future surface actually wants a curated card, in which case it reads the declaration rather than a hand-copied dataclass. | skeleton wave 2 -> TELEMAC family wave |

| `ReachMesh` (the class NAME) | trid3nt_server/workflows/telemac/workflow.py | "Reach" is the banned domain qualifier, one layer down from the facade where the ban was written - and the class is not a mesh at all, it is the handle the deck writer meshes from. Condition: a name that describes what the object IS, with the domain word moved onto the policy that genuinely carries a domain. | DELETED (wave 2b, panel remediation). CONDITION MET: `MeshHandle` + `CorridorPolicy`; grep-to-zero on `ReachMesh` in code (the name survives only as a superseded-by note in ADR 0312 and the design doc). reopen: never. | wave 2b |
| `MeshPolicy.extent_km` / `.width_m` / `.boundary_source` | trid3nt_server/workflows/lib/slots.py | a placement leak: a corridor's length, cross-stream width and bank source are ONE domain's shape, and the universal policy every future engine reads had them as fixed fields. By the placement rule they belong to the facade that meshes corridors. Condition: they live on a facade-owned value object and reach `build_mesh` as an engine slot. | DELETED (wave 2b, panel remediation). CONDITION MET: `telemac.workflow.CorridorPolicy` holds all three; `MeshPolicy` keeps only `resolution` + `target_edge_m`; `EngineOps.build_mesh(domain, policy, **slots)` carries the engine's own shape without widening the universal signature. Both cohort templates updated; the deck fields they translate to are unchanged, so the deck is byte-identical. reopen: never - a universal type that names one domain's geometry is the leak. | wave 2b |
| `location_or_bbox`'s `code_prefix="TELEMAC"` DEFAULT | trid3nt_server/workflows/shared/aoi.py | the file is engine-agnostic BY PLACEMENT - it is in `shared/` precisely because every engine that models a place needs it - and a default engine prefix there hands the next caller TELEMAC's error codes silently. A SWMM template refusing with `TELEMAC_PARAMS_INVALID` is a wrong answer no test asks about. Condition: the prefix is a required keyword and every caller states its own. | DELETED (wave 2b, panel remediation). CONDITION MET: `code_prefix` is keyword-only with no default; both cohort call sites pass `code_prefix="TELEMAC"` explicitly. reopen: never. | wave 2b |
| Three orphaned `_STEPS` module constants | trid3nt_server/workflows/swmm/rdii_rtk/rdii_rtk.py, swmm/aquifer_baseflow/aquifer_baseflow.py, swmm/snowmelt_degree_day/snowmelt_degree_day.py | left behind by the wave-2 chart migration: they existed only to build the dotted-string chart builder paths that wave deleted, and each file now referenced its own constant exactly once - at the assignment. Condition: grep confirms one occurrence per file. | DELETED (wave 2b, panel remediation). CONDITION MET: one occurrence each, all three removed; the fourth (`aquifer_baseflow/steps.py`) is genuinely used and STAYS. reopen: never - a constant nothing reads is decoration the next author copies. | wave 2b |

## publish_layer (registered tool) - DELETED 2026-08-25 (cleanup wave phase 2)
- What: data/publish_layer/publish_layer.py @register_tool body + corpus.yaml
  entry + tool_retrieval reference (2,279-line file; mechanism inside it moves
  to emission/ FIRST, then the tool dies).
- CONDITION to delete: processing-primitive rasters auto-emit through
  emission/ (hillshade/NDVI/slope/etc. visible with no explicit publish
  call), proven by a live case showing a processing raster on the map with
  zero publish_layer invocation + flood canary green.
- Decided by: NATE (ruling b, skeleton-architecture discussion). Emission is
  automatic everywhere; "display this" intent retired - user hides unwanted
  intermediates in QGIS.
- STATUS: **DELETED** (commit 0041b1e0). CONDITION MET, proven live by
  `scripts/proof_auto_emit_seam.py` - the SCRIPT is what is committed, since
  `.gitignore` keeps `docs/proof/*` outside `templates/` as local artifacts, so
  the reproduction travels and the capture
  (`docs/proof/auto_emit_seam_evidence.json`) does not. It runs `fetch_dem` then
  `compute_hillshade`, both through `PipelineEmitter.emit_tool_call` over real
  3DEP terrain, and produced TWO published overview COGs on the map
  (`.../dem/overviews/01M0WC1V2TD0M6H1AZ08QSNAGS.tif`,
  `.../hillshade/overviews/01M0WC1V82WAFVCKJV3BH524XW.tif`) with the publish
  mechanism called exactly twice - once per raster, unasked - and
  `'publish_layer' in TOOL_REGISTRY` False, which is what zero invocation
  means once the tool is gone: not that the model declined, but that it
  cannot. Registry 253 -> 252 for this deletion; the evidence file records
  251 because it was re-captured after the same wave's
  `compute_urban_heat_island` demote.
- What moved, and why it is the mechanism that moved rather than the tool that
  was rewritten: the 2,273-line file was ~114 lines of tool shell (decorator,
  metadata, wire signature, an 83-line routing docstring) around ~1,845 lines
  of module-level mechanism that `emission/` was ALREADY importing eight
  symbols of. `emission/outputs_seam.py` imported five styling functions at
  module level, `pipeline_emitter` lazily lifted legends out of the module's
  own global stash, and `publish_layer` imported `observe_published_layer`
  back out of `emission/uri_registry.py` - a package-level import cycle that
  survived only because `uri_registry` imports nothing from `tools`. The file
  is now `trid3nt_server/emission/publish.py` and every one of those edges is
  intra-package.
- The AUTO-EMIT collapse that came with it (ADR 0244 single-seam sweep): a
  second publish call site lived in the dispatch layer
  (`server/dispatch/emitter.py`'s DETERMINISTIC LAYER AUTO-PUBLISH block ->
  `results.py::_auto_publish_droppable_raster`), parallel to the emission
  seam and reachable only from the WS server. It is gone; the publish now
  happens inside `PipelineEmitter.emit_tool_call`'s LayerURI branch via
  `layer_uri_emit.publish_for_emission`, which is the same seam
  `emit_layer_uri` guards. A new raster-producing tool gets overviews, style
  params and a legend by returning a `LayerURI`, and there is no call site to
  add. Lists of layers (frame series) each take the same trip, which the
  dispatch site handled and the seam previously did not.
- Also deleted with it, same ruling: the `auto_publish` opt-out
  (`SourceSpec.output.auto_publish` + `AtomicToolMetadata.auto_publish` + the
  registration propagation + the four `source.yaml` opt-outs on `fetch_dem` /
  `fetch_landcover` / `fetch_3dep_extra` / `fetch_topobathy`). NATE: the user
  hides what they do not want, so an intermediate is still a layer. The live
  proof asserts the DEM - the flagship opt-out - now reaches the map.
- And `trid3nt_server/server/styles.py`, whose two functions were the publish
  boundary's own: `_resolve_publish_wrap_style_preset` is now
  `emission.publish.style_preset_for_publish` (same flood/depth token ladder,
  renamed off `resolve_style_preset` because `emission/quantity_styles.py`
  already owns that name for a different question,
  behaviour unchanged, `tests/test_duplicate_flood_layer_fix.py` re-pointed),
  and `_is_droppable_object_store_raster` died with the dispatch call site it
  existed to feed.
- One real BUG fell out of the move: `workflows/lib/interpreter.py`'s declared
  `RenderSpec` path did `from trid3nt_server.tools.publish_layer import
  publish_layer` - which binds the SUBMODULE, not the function - and handed
  that to `asyncio.to_thread`. Any declared render reaching that line would
  have raised `TypeError`. It now imports the function.
- reopen: never. A "display this" intent cannot come back without re-creating
  the class of failure it existed to paper over (a computed layer the user
  cannot see because nobody asked for it).

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| The `.byo()` modifier name, `AuthoredProducer.byo_uri` / `byo_validate`, `DataDecl.is_byo`, `interpret(byo=...)` and `ByoCoverageError` / `BYO_COVERAGE_MISMATCH` | trid3nt_server/workflows/lib + the six templates + docs | one word for one idea: `user_supplied` is already the ladder rung's name and "supplied on this invocation" is already the provenance vocabulary, so a third spelling of the same thing on the modifier is a name the reader has to translate. | DELETED (wave A, NATE naming ruling). CONDITION MET: renamed to `.supplied()` / `supplied_uri` / `supplied_validate` / `is_supplied` / `interpret(supplied=...)` / `SuppliedCoverageError` / `SUPPLIED_COVERAGE_MISMATCH` across the library, the six templates, the tests and the design doc, with NO alias and no deprecation shim - `.byo` does not exist. A producer-less slot now reads `Data("structure").supplied(geometry="polyline").optional()`, so the slot declares the SHAPE it accepts (the only thing a template can honestly say about a layer whose source it deliberately does not name) and the geometry vocabulary is checked at declaration. | NATE naming ruling |
| The p-view READ-RECORDING machinery: `ResolvedParams._reads` / `freeze_reads` / `concrete_reads` / `_record_read`, `ParamValues(record=...)`, and `ResolvedParams.get` | trid3nt_server/workflows/lib/params.py | the plan becomes STATIC - `plan(ops)` reads no concrete value - so there are no construction-time reads left to record, and the validator check the record existed for (`_check_revisable_branches`) has nothing to check. | DELETED (wave A, static-plan rule). CONDITION MET: `plan(ops)` takes no sheet; every read is a late-bound `P.<name>` / `D.<name>` / `Ref`. The concrete read is now `ResolvedParams.value_of`, used by the interpreter's binder and by code running WITH a sheet (the four pre-skeleton templates, the resolver's re-seat comparison). `p.get` in a plan now raises `ParamNotResolved` naming the declared params, which is the honest answer: there is nothing to read at construction time. | decision 6, static plan |
| `validate._check_revisable_branches` (the refusal of a plan that declares a FormGate AND branches on a form-revisable param) | trid3nt_server/workflows/lib/validate.py | `When` is evaluated by the INTERPRETER after the gates, so a branch reading a value the gate revised is the intended behaviour rather than a contradiction. | DELETED (wave A, static-plan rule). CONDITION MET: the interpreter binds a `When` condition against the current sheet at the moment the branch is reached. RE-POINTED, not merely removed: `_check_when_conditions` now reads the When nodes and refuses a condition that names nothing - an undeclared param, an undeclared Data, or a step not visible on that branch. | decision 6, static plan |
| `Plan.flat()` and `When.taken` | trid3nt_server/workflows/lib/plan.py | branch selection moves from plan-construction time to interpretation time, so a plan value cannot answer "which steps run". | DELETED (wave A, static-plan rule). CONDITION MET: `Plan.declared()` returns every step, guarded or not; the interpreter numbers all of them and skips the ones whose guard did not fire. | decision 6, static plan |
| `workflow.DataRefs` + `UndeclaredDataError` (the per-workflow `d` object handed to `plan`) | trid3nt_server/workflows/lib/workflow.py | `D` becomes a module-level namespace so a binding block can sit above `plan()`, which means the undeclared-name check moves from attribute access to registration. | DELETED (wave A, static-plan rule). CONDITION MET: `D.<name>` yields a `DataRef` carrying its `file.py:line` origin, and `validate._resolve_root` refuses an undeclared one at registration with that origin and the declared Data list. | decision 6, static plan |
| The EAGER independent-Data batch: `interpreter._produce_independent_data` + `_eager_data_index` | trid3nt_server/workflows/lib/interpreter.py | producers become demand-pulled, so a `When`-guarded consumer whose branch does not fire costs no fetch - which an eager batch cannot honour, because it runs before any branch is decided. | DELETED (wave A, lazy producers). CONDITION MET: `_produce` runs on first `Ref` through `_deref`; the batch is gone. TRADE, stated: independent producers no longer run concurrently. The parallelism was worth less than the guarantee, and a producer set worth parallelising again would be declared as such rather than inferred from "Refs no other Data". | decision 6, mesh-wave charter (1) |
| The `.render` verb + `RenderSpec` + `interpreter._run_render` | trid3nt_server/workflows/lib | renders are the plugin's job and workflows describe PRODUCTS, so a style becomes a declaration MODIFIER over the automatic emission seam rather than a step. | DELETED (wave A, emission/styles chapter). CONDITION MET: `.style(preset=|colormap=|policy=|range=|transform=|clip=)` replaces it, resolving against the style contract; `interpreter._run_style` re-emits the DISPLAY FACE through `emission/restyle.apply_style`. `.render` had never shipped in a live template (dormant lib machinery plus one design-doc example), so nothing in the fleet changed shape. The honesty floor survives: `RenderSourceMissingError` now fires when a step declares a style and produced no layer to paint. | STYLE MODIFIER GRAMMAR / PRECISION rulings |
| `emission/quantity_styles.py` in full (`QUANTITY_STYLE_PRESETS`, `MESH_PRESETS`, `NEUTRAL_FALLBACK_PRESET`, `resolve_style_preset`, the fallback counter) | trid3nt_server/emission | the quantity -> preset table and the preset table live in ONE contract file, so the mirror between them cannot be opened. | DELETED (wave A, emission/styles chapter). CONDITION MET: `contracts/trid3nt_contracts/styles.yaml` holds `presets` and `quantity_defaults` in one file; `emission/styles.py` is the one resolver and carries `resolve_style_preset`, the family separator and the fallback counter. `tests/test_style_contract.py` pins that every declared quantity maps to a preset the SAME file declares - the mirror check became a self-consistency check. | REUSE-SWEEP / emission chapter |
| `publish._QGIS_STYLE_REGISTRY` + `_QGIS_STYLE_SAFE_DEFAULT` + `_registry_style_params` + `_band1_percentile_rescale` + `_sediment_yield_log_style_params` | trid3nt_server/emission/publish.py | the preset table becomes a declared contract and the scale decision moves to one resolver. | DELETED (wave A, emission/styles chapter). CONDITION MET: the 59 preset rows plus the sediment log-class table are declared in `styles.yaml`; `emission/styles.resolve_style` makes the scale decision and `band_range_reader` / `fixed_range_reader` supply the run's own range. `publish._resolve_qgis_style_params` keeps ONLY the three raster guards (embedded palette, RGB(A) composite, terrain token) because those are facts about the file, not about the style. | emission chapter |
| `OutputQuantitySpec.style_preset` (the field) + its 24 per-spec values | contracts/trid3nt_contracts/output_quantities.py | publishers declare QUANTITIES and the contract owns quantity -> preset, so a spec naming a colormap is a third copy of a decision that has one home. | DELETED (wave A, emission/styles chapter). CONDITION MET: every `quantity_id` is a row in `styles.yaml`'s `quantity_defaults`, and `workflows/shared/publish_quantities` resolves the preset from `spec.quantity_id` through the one resolver. | emission chapter |
| `persistence/case_lifecycle.py` in full (`ensure_case_qgs`, `CaseLifecycleError`, the `PER_CASE_QGS_UNAVAILABLE` code) + `tests/test_case_lifecycle.py` | trid3nt_server/persistence + tests | per-Case `.qgs` provisioning is not implemented and has no production caller, so the module is a lazy-init policy for a lifecycle nothing runs. | DELETED (wave A, placement debts). CONDITION MET: grep-to-zero on production callers before the delete - the only non-definition references were one docstring mention in `server/turn/cases.py` and the module's own four unit tests. `CaseSummary.qgs_project_uri` STAYS as inert data: a case handed an explicit project URI keeps it, and nothing provisions one. | placement debts |
| `tools/processing/_gdal_runner.translate_to_cog` (the COG encoder living inside the terrain-tool runner) | trid3nt_server/tools/processing | emission needs it to publish a renderable raster, and reaching backwards from `emission/publish.py` into a terrain TOOL to get it is the wrong-direction import. | DELETED (wave A, placement debts). CONDITION MET: moved verbatim to `trid3nt_server/emission/cog.py`; the six processing tools plus `emission/publish.py` import it from there. `_gdal_runner` keeps what it is actually for - gdaldem/gdal_contour binary resolution, the PROJ env wiring, the subprocess call and the raster-bytes reader. | placement debts |
| `compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES` (the literal table) + `hex_to_rgba` | trid3nt_server/tools/processing/compute_sediment_yield | the log-spaced class breaks ARE a style declaration, and `emission/publish.py` imported them backwards out of a processing tool to build its interval colormap. | DELETED (wave A, placement debts). CONDITION MET: the seven breaks are declared on the `sediment_yield_t_ha_yr` preset in `styles.yaml`; the module reads them through `emission.styles.legend_classes` so its legend key and its paint are one table. `hex_to_rgba` has one caller left, inside the resolver. | placement debts |
| `persistence/case_lifecycle.ensure_case_qgs` + `CaseLifecycleError`'s qgs branch + `tests/test_case_lifecycle.py`'s four `ensure_case_qgs` cases | trid3nt_server/persistence/case_lifecycle.py + tests | its ONLY caller was the dispatch layer's per-Case `.qgs` lazy-init for the `publish_layer` tool, which is deleted. The raster publish path never touched a `.qgs` (the module docstring says so outright); the only consumer left is the DORMANT `TRID3NT_QGIS_WMS_BASE` vector-WMS face, which nothing exports and nothing calls. Condition: that WMS seam is either wired to a real caller (which would give `ensure_case_qgs` a caller again) or deleted, at which point `qgs_project_uri` is dead persisted state too. | QUEUED (cleanup wave phase 2). MEASURED: `grep -rn ensure_case_qgs --include=*.py trid3nt_server/` -> 2 hits, both the definition and its `__all__`. Not deleted in this wave because `qgs_project_uri` is a LIVE persisted Case field (every record in `projects.json` carries one) and the byte-identical-persistence rule holds. | cleanup wave phase 2 |
| `emission/quantity_styles.py`'s hand-mirrored copy of `_QGIS_STYLE_REGISTRY` | trid3nt_server/emission/quantity_styles.py | the file says at `:6` that it keeps "only the idea" of the registry rather than importing it, and two tests (`test_quantity_styles_registry.py`, `test_output_quantity_style_presets.py`) exist purely to catch the drift between the two copies. The reason for the copy was the package boundary - `emission/` importing `tools/` for a style table. That boundary is gone: both files are now in `emission/`. Condition: `quantity_styles` imports the registry from `emission.publish` and the two drift tests become assertions about ONE table. | QUEUED (cleanup wave phase 2). Not folded in this wave: the drift tests are the only thing standing between a preset rename and a silently unstyled solver output, so the fold needs its own live verification per engine rather than riding a move. | cleanup wave phase 2 |
| The two lazy `emission.publish` -> `tools.processing` imports (`compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES` + `hex_to_rgba` at `:540`, `compute_hillshade._get_gdaldem_bin` + `._translate_to_cog` at `:1459`) | trid3nt_server/emission/publish.py | they were `from ..processing...` relative imports while the file lived under `tools/`, and the move made them absolute `trid3nt_server.tools.processing...` - i.e. the ONE wrong-direction edge the move otherwise removed, now explicit instead of implicit. Neither is a styling POLICY the emission layer should be asking a compute tool for: the GDAL COG translator is a raster utility and the log-class table is a quantity style. Condition: the GDAL helpers move to a shared raster util and `SEDIMENT_YIELD_LOG_CLASSES` moves to `emission/quantity_styles.py`, so `emission/` imports nothing from `tools/`. | QUEUED (cleanup wave phase 2). Both are lazy (function-body) imports, so they do not create a load-time cycle; they are a placement debt, not a defect. Note they were BROKEN before the move was fixed: `from ..processing...` from `emission/publish.py` resolves to the non-existent `trid3nt_server.processing`. | cleanup wave phase 2 |

| `workflows/telemac/_bed_input.py` (the shared in-worker bed-COG surfacing helper) | trid3nt_server/workflows/telemac | it existed because the TOMAWAC and ARTEMIS composers both needed to surface a bed sampled INSIDE the solver container, which the emit-on-fetch router seam cannot cover - one helper for the two lake modules. The open-water front now owns that mechanism for every AOI template. Condition: the last composer reading it migrates. | DELETED (TELEMAC family wave). CONDITION MET: `surface_in_worker_bed_input` lives in `telemac/steps/open_water.py`, where the coastal, wave, agitation and (future) rain templates all reach it, and the three callers that used the old module are migrated. ONE deliberate contract change came with the move: `layer_id_prefix` is now REQUIRED rather than defaulting to `"input-lake-bed"` - a shared helper that defaults to one caller's word is the same placement leak wearing a convenience's clothes the wave-2b `code_prefix` note called out, and all three callers name their own. grep-to-zero on `telemac._bed_input`. reopen: never. | TELEMAC family wave |
| `workflows/telemac/_template_card.py` + the `TEMPLATE_CARD` / `QUESTION` declarations in all six TELEMAC templates | trid3nt_server/workflows/telemac | the engine-door listing card, whose door was dissolved in 2026-08. See the species row above for the full story. | DELETED (TELEMAC family wave) - the first engine to clear. | ADR 0094 -> TELEMAC family wave |

## Cleanup wave phase 1 - 2026-08-25

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| The `.byo()` modifier name, `AuthoredProducer.byo_uri` / `byo_validate`, `DataDecl.is_byo`, `interpret(byo=...)` and `ByoCoverageError` / `BYO_COVERAGE_MISMATCH` | trid3nt_server/workflows/lib + the six templates + docs | one word for one idea: `user_supplied` is already the ladder rung's name and "supplied on this invocation" is already the provenance vocabulary, so a third spelling of the same thing on the modifier is a name the reader has to translate. | DELETED (wave A, NATE naming ruling). CONDITION MET: renamed to `.supplied()` / `supplied_uri` / `supplied_validate` / `is_supplied` / `interpret(supplied=...)` / `SuppliedCoverageError` / `SUPPLIED_COVERAGE_MISMATCH` across the library, the six templates, the tests and the design doc, with NO alias and no deprecation shim - `.byo` does not exist. A producer-less slot now reads `Data("structure").supplied(geometry="polyline").optional()`, so the slot declares the SHAPE it accepts (the only thing a template can honestly say about a layer whose source it deliberately does not name) and the geometry vocabulary is checked at declaration. | NATE naming ruling |
| The p-view READ-RECORDING machinery: `ResolvedParams._reads` / `freeze_reads` / `concrete_reads` / `_record_read`, `ParamValues(record=...)`, and `ResolvedParams.get` | trid3nt_server/workflows/lib/params.py | the plan becomes STATIC - `plan(ops)` reads no concrete value - so there are no construction-time reads left to record, and the validator check the record existed for (`_check_revisable_branches`) has nothing to check. | DELETED (wave A, static-plan rule). CONDITION MET: `plan(ops)` takes no sheet; every read is a late-bound `P.<name>` / `D.<name>` / `Ref`. The concrete read is now `ResolvedParams.value_of`, used by the interpreter's binder and by code running WITH a sheet (the four pre-skeleton templates, the resolver's re-seat comparison). `p.get` in a plan now raises `ParamNotResolved` naming the declared params, which is the honest answer: there is nothing to read at construction time. | decision 6, static plan |
| `validate._check_revisable_branches` (the refusal of a plan that declares a FormGate AND branches on a form-revisable param) | trid3nt_server/workflows/lib/validate.py | `When` is evaluated by the INTERPRETER after the gates, so a branch reading a value the gate revised is the intended behaviour rather than a contradiction. | DELETED (wave A, static-plan rule). CONDITION MET: the interpreter binds a `When` condition against the current sheet at the moment the branch is reached. RE-POINTED, not merely removed: `_check_when_conditions` now reads the When nodes and refuses a condition that names nothing - an undeclared param, an undeclared Data, or a step not visible on that branch. | decision 6, static plan |
| `Plan.flat()` and `When.taken` | trid3nt_server/workflows/lib/plan.py | branch selection moves from plan-construction time to interpretation time, so a plan value cannot answer "which steps run". | DELETED (wave A, static-plan rule). CONDITION MET: `Plan.declared()` returns every step, guarded or not; the interpreter numbers all of them and skips the ones whose guard did not fire. | decision 6, static plan |
| `workflow.DataRefs` + `UndeclaredDataError` (the per-workflow `d` object handed to `plan`) | trid3nt_server/workflows/lib/workflow.py | `D` becomes a module-level namespace so a binding block can sit above `plan()`, which means the undeclared-name check moves from attribute access to registration. | DELETED (wave A, static-plan rule). CONDITION MET: `D.<name>` yields a `DataRef` carrying its `file.py:line` origin, and `validate._resolve_root` refuses an undeclared one at registration with that origin and the declared Data list. | decision 6, static plan |
| The EAGER independent-Data batch: `interpreter._produce_independent_data` + `_eager_data_index` | trid3nt_server/workflows/lib/interpreter.py | producers become demand-pulled, so a `When`-guarded consumer whose branch does not fire costs no fetch - which an eager batch cannot honour, because it runs before any branch is decided. | DELETED (wave A, lazy producers). CONDITION MET: `_produce` runs on first `Ref` through `_deref`; the batch is gone. TRADE, stated: independent producers no longer run concurrently. The parallelism was worth less than the guarantee, and a producer set worth parallelising again would be declared as such rather than inferred from "Refs no other Data". | decision 6, mesh-wave charter (1) |
| The `.render` verb + `RenderSpec` + `interpreter._run_render` | trid3nt_server/workflows/lib | renders are the plugin's job and workflows describe PRODUCTS, so a style becomes a declaration MODIFIER over the automatic emission seam rather than a step. | DELETED (wave A, emission/styles chapter). CONDITION MET: `.style(preset=|colormap=|policy=|range=|transform=|clip=)` replaces it, resolving against the style contract; `interpreter._run_style` re-emits the DISPLAY FACE through `emission/restyle.apply_style`. `.render` had never shipped in a live template (dormant lib machinery plus one design-doc example), so nothing in the fleet changed shape. The honesty floor survives: `RenderSourceMissingError` now fires when a step declares a style and produced no layer to paint. | STYLE MODIFIER GRAMMAR / PRECISION rulings |
| `emission/quantity_styles.py` in full (`QUANTITY_STYLE_PRESETS`, `MESH_PRESETS`, `NEUTRAL_FALLBACK_PRESET`, `resolve_style_preset`, the fallback counter) | trid3nt_server/emission | the quantity -> preset table and the preset table live in ONE contract file, so the mirror between them cannot be opened. | DELETED (wave A, emission/styles chapter). CONDITION MET: `contracts/trid3nt_contracts/styles.yaml` holds `presets` and `quantity_defaults` in one file; `emission/styles.py` is the one resolver and carries `resolve_style_preset`, the family separator and the fallback counter. `tests/test_style_contract.py` pins that every declared quantity maps to a preset the SAME file declares - the mirror check became a self-consistency check. | REUSE-SWEEP / emission chapter |
| `publish._QGIS_STYLE_REGISTRY` + `_QGIS_STYLE_SAFE_DEFAULT` + `_registry_style_params` + `_band1_percentile_rescale` + `_sediment_yield_log_style_params` | trid3nt_server/emission/publish.py | the preset table becomes a declared contract and the scale decision moves to one resolver. | DELETED (wave A, emission/styles chapter). CONDITION MET: the 59 preset rows plus the sediment log-class table are declared in `styles.yaml`; `emission/styles.resolve_style` makes the scale decision and `band_range_reader` / `fixed_range_reader` supply the run's own range. `publish._resolve_qgis_style_params` keeps ONLY the three raster guards (embedded palette, RGB(A) composite, terrain token) because those are facts about the file, not about the style. | emission chapter |
| `OutputQuantitySpec.style_preset` (the field) + its 24 per-spec values | contracts/trid3nt_contracts/output_quantities.py | publishers declare QUANTITIES and the contract owns quantity -> preset, so a spec naming a colormap is a third copy of a decision that has one home. | DELETED (wave A, emission/styles chapter). CONDITION MET: every `quantity_id` is a row in `styles.yaml`'s `quantity_defaults`, and `workflows/shared/publish_quantities` resolves the preset from `spec.quantity_id` through the one resolver. | emission chapter |
| `persistence/case_lifecycle.py` in full (`ensure_case_qgs`, `CaseLifecycleError`, the `PER_CASE_QGS_UNAVAILABLE` code) + `tests/test_case_lifecycle.py` | trid3nt_server/persistence + tests | per-Case `.qgs` provisioning is not implemented and has no production caller, so the module is a lazy-init policy for a lifecycle nothing runs. | DELETED (wave A, placement debts). CONDITION MET: grep-to-zero on production callers before the delete - the only non-definition references were one docstring mention in `server/turn/cases.py` and the module's own four unit tests. `CaseSummary.qgs_project_uri` STAYS as inert data: a case handed an explicit project URI keeps it, and nothing provisions one. | placement debts |
| `tools/processing/_gdal_runner.translate_to_cog` (the COG encoder living inside the terrain-tool runner) | trid3nt_server/tools/processing | emission needs it to publish a renderable raster, and reaching backwards from `emission/publish.py` into a terrain TOOL to get it is the wrong-direction import. | DELETED (wave A, placement debts). CONDITION MET: moved verbatim to `trid3nt_server/emission/cog.py`; the six processing tools plus `emission/publish.py` import it from there. `_gdal_runner` keeps what it is actually for - gdaldem/gdal_contour binary resolution, the PROJ env wiring, the subprocess call and the raster-bytes reader. | placement debts |
| `compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES` (the literal table) + `hex_to_rgba` | trid3nt_server/tools/processing/compute_sediment_yield | the log-spaced class breaks ARE a style declaration, and `emission/publish.py` imported them backwards out of a processing tool to build its interval colormap. | DELETED (wave A, placement debts). CONDITION MET: the seven breaks are declared on the `sediment_yield_t_ha_yr` preset in `styles.yaml`; the module reads them through `emission.styles.legend_classes` so its legend key and its paint are one table. `hex_to_rgba` has one caller left, inside the resolver. | placement debts |
| `trid3nt_server/data/simulation/{elmfire,geoclaw,landlab,openquake,swan}/` (five engine-dir HUSKS) | trid3nt_server/data/simulation | the five dirs held EXACTLY one 0-byte `__init__.py` each and nothing else - every product module they once held was removed by earlier waves (the engine-door dissolve + the composer migrations), leaving a package skeleton that only advertised a home for code that is gone. Condition: the dir is genuinely empty of product code and nothing imports it. | DELETED (cleanup wave phase 1). CONDITION MET: `find` confirmed one 0-byte `__init__.py` per dir (no other file, product or otherwise); grep-to-zero on `simulation.<engine>` / `simulation/<engine>` across .py/.yaml/.yml/.toml/.cfg/.md repo-wide. Registration is EXPLICIT-IMPORT (`tools/__init__.py` imports each tool module by path, fail-fast on duplicates), never a directory walk, so an empty package dir contributed nothing to the surface - proven by the registry count holding at 253 eager / 257 with the daemon-startup catalog tools BEFORE and AFTER the removal. 0 product LOC deleted (5 empty files). reopen: an engine regains a `data/simulation/` tool, which creates its own dir. | cleanup wave phase 1 |
| `aggregate_claims_across_sources` (the module + `tests/test_aggregate_claims_across_sources.py`) + the memberless `"event-aggregation"` TOOL_CATEGORIES entry + the three dangling prose references to it | trid3nt_server/tools/processing/aggregate_claims_across_sources + tests + contracts/trid3nt_contracts/{tool_metadata,event,case_results}.py + trid3nt_server/main.py | NATE DEREGISTERED it as an LLM-facing tool in `a054d61b` (cull(processing-wave), ADR 0043, 2026-07-29) and KEPT the module as a library for a stated future consumer: `main.py` said "model_groundwater imports its private extractors". Condition: the promised consumer either arrives or is confirmed never to have been built. | DELETED (cleanup wave phase 1, tools/processing misdirection audit). CONDITION MET - the consumer was NEVER BUILT: `git log --all -S "model_groundwater"` finds zero commits ever adding such a file, and no `model_groundwater` module exists anywhere in the tree, current or historical. So the library was kept alive for eighteen waves by nothing but its own unit test, while the news intents it once served were re-homed onto `web_fetch` / `fetch_nws_event` / `fetch_storm_events_db` (main.py's own comment says so). TRACE: not in TOOL_REGISTRY (verified live, `'aggregate_claims_across_sources' in TOOL_REGISTRY` -> False, and absent from both the `tools/__init__.py` import block and `main.py`'s eager-import list); every remaining repo-wide reference was PROSE - two contract docstrings (`ClaimSet`, `DerivedEventParam`), the three generated schema `description` strings they produce, one `main.py` comment and one TOOL_CATEGORIES comment - with zero import statements and zero call sites outside the deleted test. The `ClaimSet` / `NumericClaim` CONTRACT is LIVE (every numeric intensity field on EventMetadata is a `ClaimSet | None`) and STAYS: the shape outlives its one-time producer, so the two docstrings were reworded to describe the shape's owner rather than a deleted filler, and the three schema `description` strings were patched to match. `"event-aggregation"` went with it - an open-enum category whose only member was this tool, referenced nowhere else and absent from the exported schema (`TOOL_CATEGORIES` feeds only the local `is_valid_category()`), i.e. exactly the memberless decoration the wave-2b constant row warned about. Registry 253 before and after (a deregistered tool cannot change the surface). 994 LOC deleted (module 668 + `__init__.py` 0 + test 326). reopen: a real consumer for cross-source claim aggregation appears, in which case it is written against the live `ClaimSet` contract rather than resurrected. | cleanup wave phase 1 / ADR 0043 |

## Wave 2c - QUEUED fold targets - 2026-08-25

Both rows are QUEUED, not done. Every figure in them was measured at `b24feb64`
with the command quoted in the row.

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| The `.byo()` modifier name, `AuthoredProducer.byo_uri` / `byo_validate`, `DataDecl.is_byo`, `interpret(byo=...)` and `ByoCoverageError` / `BYO_COVERAGE_MISMATCH` | trid3nt_server/workflows/lib + the six templates + docs | one word for one idea: `user_supplied` is already the ladder rung's name and "supplied on this invocation" is already the provenance vocabulary, so a third spelling of the same thing on the modifier is a name the reader has to translate. | DELETED (wave A, NATE naming ruling). CONDITION MET: renamed to `.supplied()` / `supplied_uri` / `supplied_validate` / `is_supplied` / `interpret(supplied=...)` / `SuppliedCoverageError` / `SUPPLIED_COVERAGE_MISMATCH` across the library, the six templates, the tests and the design doc, with NO alias and no deprecation shim - `.byo` does not exist. A producer-less slot now reads `Data("structure").supplied(geometry="polyline").optional()`, so the slot declares the SHAPE it accepts (the only thing a template can honestly say about a layer whose source it deliberately does not name) and the geometry vocabulary is checked at declaration. | NATE naming ruling |
| The p-view READ-RECORDING machinery: `ResolvedParams._reads` / `freeze_reads` / `concrete_reads` / `_record_read`, `ParamValues(record=...)`, and `ResolvedParams.get` | trid3nt_server/workflows/lib/params.py | the plan becomes STATIC - `plan(ops)` reads no concrete value - so there are no construction-time reads left to record, and the validator check the record existed for (`_check_revisable_branches`) has nothing to check. | DELETED (wave A, static-plan rule). CONDITION MET: `plan(ops)` takes no sheet; every read is a late-bound `P.<name>` / `D.<name>` / `Ref`. The concrete read is now `ResolvedParams.value_of`, used by the interpreter's binder and by code running WITH a sheet (the four pre-skeleton templates, the resolver's re-seat comparison). `p.get` in a plan now raises `ParamNotResolved` naming the declared params, which is the honest answer: there is nothing to read at construction time. | decision 6, static plan |
| `validate._check_revisable_branches` (the refusal of a plan that declares a FormGate AND branches on a form-revisable param) | trid3nt_server/workflows/lib/validate.py | `When` is evaluated by the INTERPRETER after the gates, so a branch reading a value the gate revised is the intended behaviour rather than a contradiction. | DELETED (wave A, static-plan rule). CONDITION MET: the interpreter binds a `When` condition against the current sheet at the moment the branch is reached. RE-POINTED, not merely removed: `_check_when_conditions` now reads the When nodes and refuses a condition that names nothing - an undeclared param, an undeclared Data, or a step not visible on that branch. | decision 6, static plan |
| `Plan.flat()` and `When.taken` | trid3nt_server/workflows/lib/plan.py | branch selection moves from plan-construction time to interpretation time, so a plan value cannot answer "which steps run". | DELETED (wave A, static-plan rule). CONDITION MET: `Plan.declared()` returns every step, guarded or not; the interpreter numbers all of them and skips the ones whose guard did not fire. | decision 6, static plan |
| `workflow.DataRefs` + `UndeclaredDataError` (the per-workflow `d` object handed to `plan`) | trid3nt_server/workflows/lib/workflow.py | `D` becomes a module-level namespace so a binding block can sit above `plan()`, which means the undeclared-name check moves from attribute access to registration. | DELETED (wave A, static-plan rule). CONDITION MET: `D.<name>` yields a `DataRef` carrying its `file.py:line` origin, and `validate._resolve_root` refuses an undeclared one at registration with that origin and the declared Data list. | decision 6, static plan |
| The EAGER independent-Data batch: `interpreter._produce_independent_data` + `_eager_data_index` | trid3nt_server/workflows/lib/interpreter.py | producers become demand-pulled, so a `When`-guarded consumer whose branch does not fire costs no fetch - which an eager batch cannot honour, because it runs before any branch is decided. | DELETED (wave A, lazy producers). CONDITION MET: `_produce` runs on first `Ref` through `_deref`; the batch is gone. TRADE, stated: independent producers no longer run concurrently. The parallelism was worth less than the guarantee, and a producer set worth parallelising again would be declared as such rather than inferred from "Refs no other Data". | decision 6, mesh-wave charter (1) |
| The `.render` verb + `RenderSpec` + `interpreter._run_render` | trid3nt_server/workflows/lib | renders are the plugin's job and workflows describe PRODUCTS, so a style becomes a declaration MODIFIER over the automatic emission seam rather than a step. | DELETED (wave A, emission/styles chapter). CONDITION MET: `.style(preset=|colormap=|policy=|range=|transform=|clip=)` replaces it, resolving against the style contract; `interpreter._run_style` re-emits the DISPLAY FACE through `emission/restyle.apply_style`. `.render` had never shipped in a live template (dormant lib machinery plus one design-doc example), so nothing in the fleet changed shape. The honesty floor survives: `RenderSourceMissingError` now fires when a step declares a style and produced no layer to paint. | STYLE MODIFIER GRAMMAR / PRECISION rulings |
| `emission/quantity_styles.py` in full (`QUANTITY_STYLE_PRESETS`, `MESH_PRESETS`, `NEUTRAL_FALLBACK_PRESET`, `resolve_style_preset`, the fallback counter) | trid3nt_server/emission | the quantity -> preset table and the preset table live in ONE contract file, so the mirror between them cannot be opened. | DELETED (wave A, emission/styles chapter). CONDITION MET: `contracts/trid3nt_contracts/styles.yaml` holds `presets` and `quantity_defaults` in one file; `emission/styles.py` is the one resolver and carries `resolve_style_preset`, the family separator and the fallback counter. `tests/test_style_contract.py` pins that every declared quantity maps to a preset the SAME file declares - the mirror check became a self-consistency check. | REUSE-SWEEP / emission chapter |
| `publish._QGIS_STYLE_REGISTRY` + `_QGIS_STYLE_SAFE_DEFAULT` + `_registry_style_params` + `_band1_percentile_rescale` + `_sediment_yield_log_style_params` | trid3nt_server/emission/publish.py | the preset table becomes a declared contract and the scale decision moves to one resolver. | DELETED (wave A, emission/styles chapter). CONDITION MET: the 59 preset rows plus the sediment log-class table are declared in `styles.yaml`; `emission/styles.resolve_style` makes the scale decision and `band_range_reader` / `fixed_range_reader` supply the run's own range. `publish._resolve_qgis_style_params` keeps ONLY the three raster guards (embedded palette, RGB(A) composite, terrain token) because those are facts about the file, not about the style. | emission chapter |
| `OutputQuantitySpec.style_preset` (the field) + its 24 per-spec values | contracts/trid3nt_contracts/output_quantities.py | publishers declare QUANTITIES and the contract owns quantity -> preset, so a spec naming a colormap is a third copy of a decision that has one home. | DELETED (wave A, emission/styles chapter). CONDITION MET: every `quantity_id` is a row in `styles.yaml`'s `quantity_defaults`, and `workflows/shared/publish_quantities` resolves the preset from `spec.quantity_id` through the one resolver. | emission chapter |
| `persistence/case_lifecycle.py` in full (`ensure_case_qgs`, `CaseLifecycleError`, the `PER_CASE_QGS_UNAVAILABLE` code) + `tests/test_case_lifecycle.py` | trid3nt_server/persistence + tests | per-Case `.qgs` provisioning is not implemented and has no production caller, so the module is a lazy-init policy for a lifecycle nothing runs. | DELETED (wave A, placement debts). CONDITION MET: grep-to-zero on production callers before the delete - the only non-definition references were one docstring mention in `server/turn/cases.py` and the module's own four unit tests. `CaseSummary.qgs_project_uri` STAYS as inert data: a case handed an explicit project URI keeps it, and nothing provisions one. | placement debts |
| `tools/processing/_gdal_runner.translate_to_cog` (the COG encoder living inside the terrain-tool runner) | trid3nt_server/tools/processing | emission needs it to publish a renderable raster, and reaching backwards from `emission/publish.py` into a terrain TOOL to get it is the wrong-direction import. | DELETED (wave A, placement debts). CONDITION MET: moved verbatim to `trid3nt_server/emission/cog.py`; the six processing tools plus `emission/publish.py` import it from there. `_gdal_runner` keeps what it is actually for - gdaldem/gdal_contour binary resolution, the PROJ env wiring, the subprocess call and the raster-bytes reader. | placement debts |
| `compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES` (the literal table) + `hex_to_rgba` | trid3nt_server/tools/processing/compute_sediment_yield | the log-spaced class breaks ARE a style declaration, and `emission/publish.py` imported them backwards out of a processing tool to build its interval colormap. | DELETED (wave A, placement debts). CONDITION MET: the seven breaks are declared on the `sediment_yield_t_ha_yr` preset in `styles.yaml`; the module reads them through `emission.styles.legend_classes` so its legend key and its paint are one table. `hex_to_rgba` has one caller left, inside the resolver. | placement debts |
| The four remaining private `_publish_peak_layer` definitions + the two docstring "mirrors `_publish_peak_layer`" references that point at them | `trid3nt_server/workflows/geoclaw/inundation/inundation.py` (:2183), `swan/wave_field/wave_field.py` (:1047), `swmm/urban_flood/urban_flood.py` (:1315), `telemac/steps/products.py` (:162); references in `elmfire/fire_spread/fire_spread.py` (:424) and `landlab/susceptibility/susceptibility.py` (:1125); consumer `swmm/dual_drainage/dual_drainage.py` (:231, imports `urban_flood`'s copy) | `b24feb64` promoted the species to `trid3nt_server/workflows/shared/publish_product_layer.py` (54 lines) under an honest name - it is NOT peak-specific; the field may be a peak envelope, a final-frame sea state, a steady agitation coefficient or a bottom sigma plane. The four TELEMAC step modules (`coastal`, `wave`, `agitation`, `stratified`) already call it. Condition to delete: each engine's publisher calls `publish_product_layer(raw, style_preset=..., update=...)` instead of its own copy, proven by that engine's own live run producing the SAME published layer URI + the same folded scalars as before the swap, and by the two "mirrors" docstring references being rewritten to name the shared seam. NOT a mechanical sed: `swmm/urban_flood` and `geoclaw/inundation` each fold engine-specific scalars in a second `model_copy`, and `telemac/steps/products.py`'s copy serves the reach cohort's DYE layer, which is not migrated. | QUEUED. MEASURED, `grep -rn "_publish_peak_layer\|publish_peak_layer" --include=*.py trid3nt_server/` -> 13 hits across 7 files; `grep -rn "def _publish_peak_layer" --include=*.py trid3nt_server/` -> 4 private definitions. (A review-panel note put the species at "8 engine copies"; that command produces 8 by no reading, so the breakdown above is what stands. The nearest true 8 is the count of FILES the species touches once the promoted module is included, which is not a count of copies.) The 54-line shared module is already product code and is already counted in the LOC ledger's wave-2c rows; folding the four copies onto it is what makes those 54 lines pay. | wave 2c / LOC ledger wave-3 verdict |
| The hand-written `@register_tool` + explicit-signature + `_normalize(locals())` registration blocks on the four templates that are already declarative underneath | `trid3nt_server/workflows/modflow/regional_water_budget/regional_water_budget.py`, `swmm/rdii_rtk/rdii_rtk.py`, `swmm/snowmelt_degree_day/snowmelt_degree_day.py`, `swmm/aquifer_baseflow/aquifer_baseflow.py` | These four already declare `PARAMS` + `plan(p, d)` and run through `resolve_params` / `interpret`, but they kept a hand-written wire signature, so the ONE definition of the wire set (`_wire_params` in the registration factory, ADR / AFK-ledger entry 1) is not what they publish. Two measured consequences: (a) CONSTANTS ARE ON THE WIRE - 4 CONSTANT-door params are still named in the hand-written signatures, one per template (`compute_class` on `modflow_regional_water_budget`; `dt_min` on each of the three SWMM tools), which is exactly what the CONSTANT-door ruling narrows the model-facing surface to exclude; (b) 23 PHANTOM DOCSTRING PARAMS - `render_docstring(**_DOC)` renders from `PARAMS`, so the LLM is told about params the hand-written function does not accept. Condition to delete: all four register through the factory, so the synthesized signature and the rendered docstring read the same `_wire_params` set; proven by a per-template live run (same question, same answer as the pre-migration reference run) plus a mechanical check that documented-params minus signature-params is EMPTY for each. | QUEUED - factory migration is the **MODFLOW wave's FIRST task**, before any template migrates, because `modflow_regional_water_budget` is one of the four and the other three are its SWMM siblings. MEASURED per template (import the module, diff the `Params:` block of `fn.__doc__` against `inspect.signature(fn)`, and read each param's door off `PARAMS`): `modflow_regional_water_budget` 7 documented / 1 phantom (`location_name`, DERIVED door); `swmm_rdii_rtk_unit_hydrograph` 6 documented / 0 phantom; `swmm_snowmelt_degree_day` 37 documented / 12 phantom; `swmm_aquifer_baseflow_to_node` 28 documented / 10 phantom. TOTAL 23 phantom, by door: 17 CONSTANT, 4 SCENARIO (`evaporation_in_day` twice, `aquifer_seepage_in_hr`, `imperviousness_pct`), 2 DERIVED (`location_name`, `site_latlon`). A review-panel note gave the figure as 17; 17 is the CONSTANT-door SUBSET, not the total - the six non-constant phantoms are the same defect (a documented param the function cannot accept) and the factory migration closes all 23 at once. | wave 2c / AFK ledger entry 1 |

## Cleanup wave phase 2 - workflow-verb demotions - 2026-08-25

NATE ordered six `processing/` verbs re-examined for demotion to the code_exec
playground, per the `compute_zonal_statistics` precedent (ADR 0043). The
outcome table with per-module reasoning is in
`docs/specs/processing-redundancy-cull-proposal.md` (top of file). One demoted;
five hold on the EMIT gate or a live cross-module consumer, which is an honest
verdict rather than a null one: the playground provably cannot paint (the
sandbox is `--unshare-net`, its workdir is destroyed in a `finally`, and
`convert_result` discriminates on chart/dataframe/scalar/json/repr, so a
LayerURI-shaped dict returns as an inert blob).

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| The `.byo()` modifier name, `AuthoredProducer.byo_uri` / `byo_validate`, `DataDecl.is_byo`, `interpret(byo=...)` and `ByoCoverageError` / `BYO_COVERAGE_MISMATCH` | trid3nt_server/workflows/lib + the six templates + docs | one word for one idea: `user_supplied` is already the ladder rung's name and "supplied on this invocation" is already the provenance vocabulary, so a third spelling of the same thing on the modifier is a name the reader has to translate. | DELETED (wave A, NATE naming ruling). CONDITION MET: renamed to `.supplied()` / `supplied_uri` / `supplied_validate` / `is_supplied` / `interpret(supplied=...)` / `SuppliedCoverageError` / `SUPPLIED_COVERAGE_MISMATCH` across the library, the six templates, the tests and the design doc, with NO alias and no deprecation shim - `.byo` does not exist. A producer-less slot now reads `Data("structure").supplied(geometry="polyline").optional()`, so the slot declares the SHAPE it accepts (the only thing a template can honestly say about a layer whose source it deliberately does not name) and the geometry vocabulary is checked at declaration. | NATE naming ruling |
| The p-view READ-RECORDING machinery: `ResolvedParams._reads` / `freeze_reads` / `concrete_reads` / `_record_read`, `ParamValues(record=...)`, and `ResolvedParams.get` | trid3nt_server/workflows/lib/params.py | the plan becomes STATIC - `plan(ops)` reads no concrete value - so there are no construction-time reads left to record, and the validator check the record existed for (`_check_revisable_branches`) has nothing to check. | DELETED (wave A, static-plan rule). CONDITION MET: `plan(ops)` takes no sheet; every read is a late-bound `P.<name>` / `D.<name>` / `Ref`. The concrete read is now `ResolvedParams.value_of`, used by the interpreter's binder and by code running WITH a sheet (the four pre-skeleton templates, the resolver's re-seat comparison). `p.get` in a plan now raises `ParamNotResolved` naming the declared params, which is the honest answer: there is nothing to read at construction time. | decision 6, static plan |
| `validate._check_revisable_branches` (the refusal of a plan that declares a FormGate AND branches on a form-revisable param) | trid3nt_server/workflows/lib/validate.py | `When` is evaluated by the INTERPRETER after the gates, so a branch reading a value the gate revised is the intended behaviour rather than a contradiction. | DELETED (wave A, static-plan rule). CONDITION MET: the interpreter binds a `When` condition against the current sheet at the moment the branch is reached. RE-POINTED, not merely removed: `_check_when_conditions` now reads the When nodes and refuses a condition that names nothing - an undeclared param, an undeclared Data, or a step not visible on that branch. | decision 6, static plan |
| `Plan.flat()` and `When.taken` | trid3nt_server/workflows/lib/plan.py | branch selection moves from plan-construction time to interpretation time, so a plan value cannot answer "which steps run". | DELETED (wave A, static-plan rule). CONDITION MET: `Plan.declared()` returns every step, guarded or not; the interpreter numbers all of them and skips the ones whose guard did not fire. | decision 6, static plan |
| `workflow.DataRefs` + `UndeclaredDataError` (the per-workflow `d` object handed to `plan`) | trid3nt_server/workflows/lib/workflow.py | `D` becomes a module-level namespace so a binding block can sit above `plan()`, which means the undeclared-name check moves from attribute access to registration. | DELETED (wave A, static-plan rule). CONDITION MET: `D.<name>` yields a `DataRef` carrying its `file.py:line` origin, and `validate._resolve_root` refuses an undeclared one at registration with that origin and the declared Data list. | decision 6, static plan |
| The EAGER independent-Data batch: `interpreter._produce_independent_data` + `_eager_data_index` | trid3nt_server/workflows/lib/interpreter.py | producers become demand-pulled, so a `When`-guarded consumer whose branch does not fire costs no fetch - which an eager batch cannot honour, because it runs before any branch is decided. | DELETED (wave A, lazy producers). CONDITION MET: `_produce` runs on first `Ref` through `_deref`; the batch is gone. TRADE, stated: independent producers no longer run concurrently. The parallelism was worth less than the guarantee, and a producer set worth parallelising again would be declared as such rather than inferred from "Refs no other Data". | decision 6, mesh-wave charter (1) |
| The `.render` verb + `RenderSpec` + `interpreter._run_render` | trid3nt_server/workflows/lib | renders are the plugin's job and workflows describe PRODUCTS, so a style becomes a declaration MODIFIER over the automatic emission seam rather than a step. | DELETED (wave A, emission/styles chapter). CONDITION MET: `.style(preset=|colormap=|policy=|range=|transform=|clip=)` replaces it, resolving against the style contract; `interpreter._run_style` re-emits the DISPLAY FACE through `emission/restyle.apply_style`. `.render` had never shipped in a live template (dormant lib machinery plus one design-doc example), so nothing in the fleet changed shape. The honesty floor survives: `RenderSourceMissingError` now fires when a step declares a style and produced no layer to paint. | STYLE MODIFIER GRAMMAR / PRECISION rulings |
| `emission/quantity_styles.py` in full (`QUANTITY_STYLE_PRESETS`, `MESH_PRESETS`, `NEUTRAL_FALLBACK_PRESET`, `resolve_style_preset`, the fallback counter) | trid3nt_server/emission | the quantity -> preset table and the preset table live in ONE contract file, so the mirror between them cannot be opened. | DELETED (wave A, emission/styles chapter). CONDITION MET: `contracts/trid3nt_contracts/styles.yaml` holds `presets` and `quantity_defaults` in one file; `emission/styles.py` is the one resolver and carries `resolve_style_preset`, the family separator and the fallback counter. `tests/test_style_contract.py` pins that every declared quantity maps to a preset the SAME file declares - the mirror check became a self-consistency check. | REUSE-SWEEP / emission chapter |
| `publish._QGIS_STYLE_REGISTRY` + `_QGIS_STYLE_SAFE_DEFAULT` + `_registry_style_params` + `_band1_percentile_rescale` + `_sediment_yield_log_style_params` | trid3nt_server/emission/publish.py | the preset table becomes a declared contract and the scale decision moves to one resolver. | DELETED (wave A, emission/styles chapter). CONDITION MET: the 59 preset rows plus the sediment log-class table are declared in `styles.yaml`; `emission/styles.resolve_style` makes the scale decision and `band_range_reader` / `fixed_range_reader` supply the run's own range. `publish._resolve_qgis_style_params` keeps ONLY the three raster guards (embedded palette, RGB(A) composite, terrain token) because those are facts about the file, not about the style. | emission chapter |
| `OutputQuantitySpec.style_preset` (the field) + its 24 per-spec values | contracts/trid3nt_contracts/output_quantities.py | publishers declare QUANTITIES and the contract owns quantity -> preset, so a spec naming a colormap is a third copy of a decision that has one home. | DELETED (wave A, emission/styles chapter). CONDITION MET: every `quantity_id` is a row in `styles.yaml`'s `quantity_defaults`, and `workflows/shared/publish_quantities` resolves the preset from `spec.quantity_id` through the one resolver. | emission chapter |
| `persistence/case_lifecycle.py` in full (`ensure_case_qgs`, `CaseLifecycleError`, the `PER_CASE_QGS_UNAVAILABLE` code) + `tests/test_case_lifecycle.py` | trid3nt_server/persistence + tests | per-Case `.qgs` provisioning is not implemented and has no production caller, so the module is a lazy-init policy for a lifecycle nothing runs. | DELETED (wave A, placement debts). CONDITION MET: grep-to-zero on production callers before the delete - the only non-definition references were one docstring mention in `server/turn/cases.py` and the module's own four unit tests. `CaseSummary.qgs_project_uri` STAYS as inert data: a case handed an explicit project URI keeps it, and nothing provisions one. | placement debts |
| `tools/processing/_gdal_runner.translate_to_cog` (the COG encoder living inside the terrain-tool runner) | trid3nt_server/tools/processing | emission needs it to publish a renderable raster, and reaching backwards from `emission/publish.py` into a terrain TOOL to get it is the wrong-direction import. | DELETED (wave A, placement debts). CONDITION MET: moved verbatim to `trid3nt_server/emission/cog.py`; the six processing tools plus `emission/publish.py` import it from there. `_gdal_runner` keeps what it is actually for - gdaldem/gdal_contour binary resolution, the PROJ env wiring, the subprocess call and the raster-bytes reader. | placement debts |
| `compute_sediment_yield.SEDIMENT_YIELD_LOG_CLASSES` (the literal table) + `hex_to_rgba` | trid3nt_server/tools/processing/compute_sediment_yield | the log-spaced class breaks ARE a style declaration, and `emission/publish.py` imported them backwards out of a processing tool to build its interval colormap. | DELETED (wave A, placement debts). CONDITION MET: the seven breaks are declared on the `sediment_yield_t_ha_yr` preset in `styles.yaml`; the module reads them through `emission.styles.legend_classes` so its legend key and its paint are one table. `hex_to_rgba` has one caller left, inside the resolver. | placement debts |
| `compute_urban_heat_island` (module + `corpus.yaml` + `__init__.py` + `tests/test_compute_urban_heat_island.py` + the `tools/__init__.py` registration + the sync-offload allowlist entry in `server/dispatch/emitter.py` + its `_OPEN_WORLD_COMPUTE_EXCEPTIONS` entry) | trid3nt_server/tools/processing/compute_urban_heat_island + tests + tools/__init__.py + server/dispatch/emitter.py + tests/test_tool_annotations.py | the EMIT reason code that protected it does not survive inspection: the tool's map product is the MODIS LST resampled onto the 10 m land-cover grid, painted with `style_preset="land_surface_temp_c"` - a preset its OWN source comments as "the fetch_modis_lst paint". Condition: the LST layer reaches the map without this tool, so that demoting loses no layer. | DELETED (cleanup wave phase 2, commit 0041b1e0). CONDITION MET by ADR 0313: `fetch_modis_lst` paints the LST itself at its NATIVE resolution and, since emission became automatic, without being asked. What the demote loses is the UPSAMPLE - a ~1 km measurement resampled onto a 10 m grid - which is a fidelity claim the data does not support, so losing it is an honesty gain rather than a capability loss. What remains is per-class means + the built-minus-vegetation delta over two staged rasters: the zonal recipe with land-cover classes as the zones. Recipe landed at `docs/playbooks/urban-heat-island-recipe.md` with its three honest LOSSES (the resampled COG, the typed delta-is-None note, the in-tool AOI clamp). Corpus SPLIT rather than dropped, per the 0043 re-home pattern: the six analysis phrasings to `meta/code_exec_tool/corpus.yaml`, the three "show me the surface heat" phrasings to `fetchers/climate/fetch_modis_lst/corpus.yaml` - the map half goes to the tool that paints the map. Registry 252 -> 251. 611 LOC module + 240 LOC test deleted. reopen: a registered primitive that can paint an arbitrary array appears, at which point the resample is expressible in the playground too and the question is moot. | cleanup wave phase 2 / ADR 0043 |
| `compute_change_detection`, `compute_flood_depth_damage`, `compute_sediment_yield`, `compute_model_residuals`, `compute_exposure_summary` (the five NOT demoted) | trid3nt_server/tools/processing | KEPT, with per-module reasons recorded so this is a decision and not an omission. FOUR fail the EMIT gate - each paints a layer (categorical gain/loss FGB, per-structure damage points, styled log-class RUSLE COG, diverging residual points) that no registered fetcher produces and the playground cannot write. `compute_exposure_summary` clears EMIT (it is tabular, the zonal profile) but fails on a live consumer: `compose_case_report.py:354` imports `get_session_exposure` and reads a Case-keyed in-process session store a sandbox return value cannot repopulate. `compute_sediment_yield` additionally has a hard importer - `emission/publish.py:540` reads its `SEDIMENT_YIELD_LOG_CLASSES` as the single source of truth for the publish styling ladder. `compute_model_residuals` is PROTECTED-VNV and `extract_model_at_observations` defines itself by reference to it. | REJECTED for this wave (decision record). Re-open conditions, per module: change_detection / flood_depth_damage / sediment_yield / model_residuals - a registered primitive that paints an arbitrary array or feature set exists; sediment_yield ALSO needs its log-class table moved to `emission/quantity_styles.py` first (its own ledger row above); exposure_summary - the Case report reads exposure from persisted Case state rather than an in-process store, at which point the tool is the zonal recipe with two fetchers in front of it. | cleanup wave phase 2 |
| `tests/test_publish_layer_map_emission_job0272.py` (232 lines, 5 cases) | tests | its whole premise is void: it guarded the job-0272 wrap-site, which existed because "the atomic `publish_layer` returns a bare WMS URL string" and `emit_tool_call` only feeds `add_loaded_layer` on a typed `LayerURI`. There is no atomic publish_layer, and the publish now happens INSIDE the seam that feeds `add_loaded_layer`, on a value that is already a `LayerURI` - so the failure it guards is unreachable by construction rather than merely unobserved. Condition: the replacement assertion exists somewhere. | DELETED (cleanup wave phase 2, commit 0041b1e0, ADR 0313). CONDITION MET twice over: `tests/test_auto_publish_droppable_raster.py::test_raster_s3_publishes_once_and_reaches_the_map` asserts the PUBLISHED uri reaches `loaded_layers`, which is the thing job-0272 was about, and the readable-name precedence the file's other two cases covered (`derive_readable_layer_name`, bare-ULID -> style-preset label) is already covered by `tests/test_publish_layer.py` items 3 and 4 against the same function, which survives in `emission/publish.py` and still has a live caller in `cases/ingest_user_layer.py:481`. reopen: never - a test whose setup has to construct a tool that does not exist is not testing the product. | cleanup wave phase 2 |

## set_telemac_parameters - DELETED 2026-08-26 (siblings still QUEUED)
- What: the four setter tools (copy-on-write deck recalibration + law-aware
  bounds), ~2k LOC family on workflows/lib/_setter_envelope.py. RELOCATED
  2026-08-26 (data/ eviction) from trid3nt_server/data/simulation/<engine>/set_<engine>_parameters/
  to trid3nt_server/workflows/<engine>/set_parameters/ (workflows/modflow/set_parameters,
  workflows/sfincs/set_parameters, workflows/swmm/set_parameters,
  workflows/telemac/set_parameters) - path change only, condition unchanged.
- CONDITION to delete: the skeleton rerun-with-overrides capability +
  coupled-validity rules land and reproduce the same recalibration LIVE
  (stage-parent, override friction, byte-identical untouched inputs
  equivalent, law-inversion refusal).
- CONDITION MET for the TELEMAC member, 2026-08-26 (ADR 0319), and it is
  DELETED - module + corpus.yaml + package dir + the `tools/__init__.py`
  registration import + tests/test_set_telemac_parameters.py (559 + 462 LOC).
  The replacement is `rerun_workflow` (workflows/lib/rerun/), and the mapping is
  one-for-one:
  * stage-parent copy-on-write -> the child inherits the parent's own LEDGER
    RECORDS for every node the overrides do not reach, so the reused artifacts
    are the parent's objects at the parent's URIs. Byte-identity is not a copy
    that has to be checked, it is the same object; the parent is untouched
    because nothing writes to it.
  * named-parameter changes -> `overrides={param: value}`, seated through the
    USER door and labelled `override of run <parent_id>`, with the derivations
    that read them re-derived and user-pinned rows keeping precedence.
  * law-aware bounds -> a declared `Validity` rule
    (`friction_coefficient_matches_law` on coastal_tidal_surge) that REFUSES
    the law inversion typed instead of warning, while an atypical-but-correct
    quantity still proceeds - the setter's own bounds policy, minus the silent
    acceptance.
  * SetterEnvelope -> the child is an ordinary run: it publishes a layer, an
    answer, a journal line naming its parent and its overrides, and a snapshot
    of its own. What the setter returned as a dict about a deck, the primitive
    returns as the ANSWER the deck was written to get.
  The one capability NOT carried over is editing a deck the workbench did not
  author (`parent_model_uri` pointing at a hand-built .cas). That was never
  reachable from the product - nothing produced such a URI - and importing a
  foreign deck is the DESCRIBE_MODEL idea in docs/IDEAS.md, not a setter.
- SIBLINGS still QUEUED: set_sfincs / set_swmm / set_modflow are OUTSIDE the
  TELEMAC sample and unmigrated. They keep `workflows/lib/_setter_envelope.py`
  alive as a pre-migration legacy lane (the module now states that constraint at
  the top); each dies at its engine's skeleton migration, and the envelope dies
  with the last of them. The new primitive does NOT import it.
- Decided by: NATE (decision 1, 2026-08-25): "this should be the behavior
  we have now, work its sentiment into the skeleton."; sample-purity ruling
  2026-08-26 (delete in the SAME series, condition met not waited on).

## scripts/run_l2_validation_harness.py - QUEUED 2026-08-25 (keep-until-superseded)
- What: the 1,477-line Harvey L2 V&V harness (repaired/repointed in wave 2c;
  imports green; two recorded live executions in docs/validation/).
- CONDITION to delete: a better replacement exists and is proven - i.e. the
  calibration-track / canonical-case V&V machinery covers the SFINCS
  hurricane-case end-to-end validation this script does, with a recorded run.
- Decided by: NATE (decision 3, 2026-08-25): "keep until we replace it with
  something better."

## The buried Overpass breakwater fetch in the ARTEMIS deck writer - DELETED 2026-08-25

**What:** `fetch_osm_breakwaters()` (39 lines), `_OVERPASS_MIRRORS` (three
hardcoded endpoints), `_stage_breakwater_layer()` (30 lines: geopandas ->
FlatGeobuf -> direct `boto3 put_object` -> a hand-built `LayerURI`), and
`_coerce_segment()` (the pinned 4-float segment normalizer), all in
`trid3nt_server/workflows/telemac/steps/agitation.py`; plus the
`fetch_osm_breakwaters` re-export from `steps/__init__.py`; plus the
`breakwater` Param on `agitation/declarations.py`.

**CONDITION to delete:** met by ADR 0315. A step that fetches bypasses the
router's cache, fallback ladders, provenance, staleness handling and typed
refusals (the no-double-middleware law), and it was doing so to seat an opinion
the question never carried - "if you named no structure, I will go and find the
real one" - inside the one tool whose entire question is whether a particular
structure shelters anything. The replacement is the `fetch_osm_breakwaters`
router spec plus the producer-less
`Data("structure").supplied(geometry="polyline").optional()` slot. The staging
re-upload dies with it: a supplied layer is already on the canvas, so a second
copy of somebody else's layer was the double-emission the input-surfacing guard
exists to catch.

**Also deleted with it:** the "LABELED schematic breakwater" fallback the step
meshed when its own Overpass call came back empty - a structure nobody asked for
in a run about whether a structure helps. Absence is now an open-water solve,
labeled on the layer, in provenance and in the run journal.

**Decided by:** NATE (TELEMAC workflows refactor, wave B). Proved live in all
three fill modes: `docs/proof/templates/artemis_harbor_agitation/addendum/artemis_harbor_agitation_structure_slot_evidence.json`.

**Status:** DELETED (commit on the wave-B branch). A regression guard rides in
`tests/test_artemis_harbor_agitation.py::test_the_step_module_makes_no_network_call_of_its_own`,
which fails if any network primitive returns to that module.

## The coastal deck's undeclared 30-hour window - DELETED 2026-08-25

**What:** `_FALLBACK_DURATION_S = 108000.0` in
`trid3nt_server/workflows/telemac/steps/coastal.py`, the third and silent rung
under `duration_hours`.

**CONDITION to delete:** met by ADR 0315. `duration_hours` declares
`derived_when_absent="the simulated window is the fetched series' own span"`, and
this constant was a window nobody had declared, reached whenever there was no ask
AND no series. It is now `SYNTHETIC_WINDOW_HOURS` beside the param whose sentence
promises it, named in that sentence, and every run emits a `duration_hours`
provenance row saying which of the three rungs set it.

**Decided by:** NATE (wave B, the sim-duration door review). **Status:** DELETED.

## The second COASTAL_PARSER_VERSION - DELETED 2026-08-25

**What:** `_COASTAL_PARSER_VERSION = "coastal-tidal-2"` in
`workers/telemac/entrypoint.py`, a second declaration of a stamp
`telemac_coastal_build.py` already owned - and which disagreed with it
(`"coastal-tidal-3"`), so the metrics echoed one version and the strict-field
refusal message quoted another. A manual provenance check gave different answers
depending on which file it greped.

**CONDITION to delete:** met the moment the disagreement was found (the
worker-purity audit). The entrypoint now imports the builder's stamp lazily, like
every other worker-payload symbol.

**Decided by:** the wave-B worker-purity audit. **Status:** DELETED.

## trid3nt_server/data/ (the category-era fossil) - DELETED 2026-08-26

**What:** the last of `trid3nt_server/data/` - the top-level `__init__.py`
fossil, `data/simulation/__init__.py`, and the six per-engine husks
(`data/simulation/{modflow,pelicun,sfincs,swmm,telemac,model_debris_flow}/`)
that still held live tenants: `model_debris_flow` (the registered
post-fire debris-flow tool), `postprocess_pelicun` (registered general
tool), the four `set_<engine>_parameters` deck-recalibration setters, and
three unregistered MODFLOW internal engine surfaces
(`run_modflow_archetype_tool`, `run_modflow_multi_species_tool`,
`run_river_seepage_tool`) backing the archetype/contaminant-plume/
river-seepage templates.

**CONDITION to delete:** every remaining tenant relocated to its
placement-rule home, with zero dead code found (every module had a live
importer, confirmed by grep + git log). No candidate qualified for
straight deletion this wave - the whole directory emptied by MOVE, not by
demotion.

**Where each tenant went:** `model_debris_flow/` -> `tools/processing/model_debris_flow/`
(sibling of `compute_sediment_yield`, the established processing-tool
folder convention - not an "engine family", so it never belonged under
`workflows/`). `pelicun/postprocess_pelicun/` -> `workflows/pelicun/postprocess_pelicun/`
(intact - a registered general tool with its own retrieval corpus, moved
whole rather than flattened to match the other engines' flat
`postprocess_<engine>.py`, because those aren't independently retrievable
tools and this one is). `modflow/set_modflow_parameters/`,
`sfincs/set_sfincs_parameters/`, `swmm/set_swmm_parameters/`,
`telemac/set_telemac_parameters/` -> `workflows/<engine>/set_parameters/`
per the standing placement rule (ledger row above, path updated in
place). `modflow/run_modflow_archetype_tool.py` -> `workflows/modflow/`
top level (multi-template consumer: `steps/archetype.py` AND
`sustainable_yield/sustainable_yield.py`, so it sits beside `run_modflow.py`
rather than inside either template dir). `modflow/run_modflow_multi_species_tool.py`
-> `workflows/modflow/contaminant_plume/` (single consumer). `modflow/run_river_seepage_tool/`
-> `workflows/modflow/river_seepage/run_river_seepage_tool/` (single
consumer, subpackage moved intact). Every internal logger namespace string
and the `_setter_envelope.py` logger tag were repointed to match; the
`tools/__init__.py` registration imports, `compute_layer_bounds.py`'s
`_bbox_from_gdf` import, the four `workflows/modflow/*` engine-bridge call
sites, and 18 test files were rewritten to the new paths - no compat shim.
`search_tools._compose_corpus_from_tree` and `catalog_http._compose_corpus_from_tree`
(the latter was ALSO missing the `workflows/**/corpus.yaml` walk entirely -
a pre-existing gap, fixed in the same pass since the fix is one line and
the alternative was leaving the catalog HTTP endpoint blind to every
engine template's retrieval corpus) both dropped the `data/` walk.

**Live evidence:** `python -c "import trid3nt_server"` + `import
trid3nt_server.mcp_server` clean; registry set byte-identical
pre/post-move (253/253, zero added, zero removed); offline slices f-o
(4 failed - the pre-existing `fetch_resolution_gate` environmental set,
unchanged) and a-e (4 failed - `test_catalog_surfacing` x3 +
`test_emit_on_fetch_equivalence` x1, pre-existing drift from concurrent
in-flight work in the fenced `workflows/telemac/rain_on_grid/` /
`workflows/mesh/` area, unrelated to this wave, confirmed absent from the
touched-file list) both held at their pre-move baseline; retrieval
spot-check (`set_telemac_parameters`, `model_debris_flow`,
`postprocess_pelicun`, `set_modflow_parameters`) all resolve in the top-8
for their canonical phrasings; grep-to-zero on `trid3nt_server.data` /
`trid3nt_server/data` across live .py/.yaml/.toml/.cfg/.ini (historical
ADRs and frozen build-report/spec docs excluded per convention).

**Decided by:** NATE (data/ eviction directive, 2026-08-26). **Status:** DELETED.

## `rain_on_grid/mesh_acquisition.py` - DELETED 2026-08-26

**What:** the 765-line watershed-mesh module inside the `telemac_rain_on_grid`
template folder: `WatershedMesh`, `acquire_watershed_mesh`, `use_supplied_mesh`,
`use_supplied_mesh_2dm`, `assemble_node_fields`, `_delineate_catchment`,
`_resolve_bare_earth_dem`, `_sample_raster_at_nodes`, `_write_bottom_selafin` and
the rest.

**CONDITION to delete:** met by ADR 0316. A catchment is a domain SHAPE, not a
TELEMAC fact, so the generation strategy is `workflows/mesh/watershed.py` and the
SELAFIN writer is `workflows/mesh/telemac_build.py`, the thin per-solver writer
beside `hecras_build`. The module's location was already provably wrong: the
standalone `generate_mesh` TOOL imported four symbols out of it and
`hecras_flood_2d` a fifth, so a shared tool was reaching into one engine's
template for its meshing. All three callers now share one home; the per-question
half (`cn_infiltration.py`) stays in the template folder where it belongs.

**Decided by:** the placement rule (wave C). **Status:** DELETED.

## `telemac_rain_on_grid`'s `observed_gauge_id` - DELETED 2026-08-26

**What:** a template argument whose docstring promised it "wires NSE/R2 vs a
USGS-NWIS gauge". It was staged into the worker manifest and the worker never read
the field - `rog_build.py` and `entrypoint.py` contain no reference to it.

**CONDITION to delete:** met the moment the promise was checked against the
worker. A documented capability nothing implements is worse than an absent one: it
invites a caller to ask for a comparison that will not happen and will not say so.
The gauge grading is real and lives where it runs, in the Ball Creek replication
drivers (`rog_ballcreek_events.py`, `rog_ballcreek_calib.py`), which grade against
EDI weir observations.

**Decided by:** the honesty floor (wave C). **Status:** DELETED.

## `telemac_rain_on_grid`'s `mesh_uri` and `mrms_window` - DELETED 2026-08-26

**What:** two wire arguments of the composer. `mesh_uri` named a user-supplied
SELAFIN; `mrms_window` named a real storm window.

**CONDITION to delete:** met by ADR 0315's context-slot ruling and by the
argument's own dishonesty. `mesh_uri` is superseded by
`Data("mesh").supplied(geometry="mesh").optional()`, where the slot's own name IS
the wire argument - a producer-less slot needs no separate uri knob. `mrms_window`
never fetched MRMS: the resolver reads AORC, deliberately, because MRMS only
covers ~2020-10 onward and a replication window predating it would silently return
nothing. It is `rain_window`. No alias survives either rename - the demolition
clause is about API shape, and both shapes were wrong.

**Decided by:** ADR 0316. **Status:** DELETED.

## The rain-on-grid AOI-centroid pour point - DELETED 2026-08-26

**What:** `pp = ((aoi[0] + aoi[2]) / 2.0, (aoi[1] + aoi[3]) / 2.0)` in the
composer - the catchment outlet, invented from the middle of the analysis window
when the caller named none. The docstring claimed it was "the AOI centroid's
lowest snapped stream cell"; the code used the raw centroid.

**CONDITION to delete:** met by the steps audit, which named it. The pour point
decides which basin is delineated AT ALL, so a guessed one silently models a
different catchment - and the prose describing a snap that never ran made it
unfalsifiable. It is now a required USER param behind a `DrawGate`: `user_gated`
asks on the canvas, `auto` refuses typed. Door 6, never invention.

**Decided by:** ADR 0316. **Status:** DELETED.

## The contradicting catchment max-edge default - DELETED 2026-08-26

**What:** `acquire_watershed_mesh(max_edge_length_m: float = 400.0)` against its
only call site, `max_edge_length_m=300.0`. Two numbers for one dial, of which only
the call site's ever ran, and a reader of either one would have been wrong about
the other.

**CONDITION to delete:** met by the migration. `DEFAULT_MAX_EDGE_M = 300.0` lives
once, in the shared mesh front, and the template's `mesh_max_edge_m` param takes
its default from it - so the number and the sentence that promises it are one
thing, and the standalone mesh tool reads the same constant.

**Decided by:** the steps audit's duplicated-default class (wave C).
**Status:** DELETED.

## Backfill: four test deletions that landed unledgered - 2026-08-26

The four rows below were written after the fact, during a ledger audit of the
review range `0f7a6351..02acbfed`. Each file was deleted inside that range and
no row named it at the time, so the deletion had no traceable record. They are
registered here in their true state - DELETED, with the commit that did it -
rather than left out because the code they guarded was ledgered elsewhere. A
guard is a deletion of its own: what stops being asserted is a fact about the
product, not a footnote to the module that went with it.

## `tests/test_mesh_acquisition_cross_dataset.py` - DELETED 2026-08-26

**What:** 58 lines asserting that the rain-on-grid mesh bed DEM's cross-dataset
fallback (3DEP bare-earth -> Copernicus GLO-30 DSM) was labeled UNCONDITIONALLY,
by raising when the caller passed no `notes` sink. It imported
`workflows.telemac.rain_on_grid.mesh_acquisition` directly.

**CONDITION to delete:** met by ADR 0316's third ruling. The module it imported
no longer exists (ledger row above), and the shape it asserted no longer exists
either: the bed DEM is `Data("bed_dem", Fetch.tool(...).ladder("usgs_3dep_bare_earth",
"copernicus_glo30"))`, so the cross-dataset label rides the RETURNED ARTIFACT and
there is no `notes` out-parameter left for a caller to decline to pass. The
"raises when no notes sink" assertion has nothing to bind to.

**Superseded by:** `tests/test_mesh_bed_dem_cross_dataset.py` (66 lines, added in
the same commit), rewritten against `resolve_bed_dem`'s producer contract - a
returned dict whose note carries the label, which is what makes the label
unbypassable rather than merely loud.

**Decided by:** ADR 0316 (wave C). **Status:** DELETED (commit 3b2fe565).

## `tests/test_quantity_styles_registry.py` - DELETED 2026-08-26

**What:** 52 lines, one of the TWO drift guards over `emission/quantity_styles.py`'s
hand-mirrored copy of `publish._QGIS_STYLE_REGISTRY` - the test that existed only
because two copies of one table had to be kept agreeing by CI.

**CONDITION to delete:** met by the style contract. `contracts/trid3nt_contracts/styles.yaml`
holds the preset table AND the quantity -> preset defaults in one file, so the
mirror is not constructible and a drift guard has no drift to catch.
`emission/quantity_styles.py` was deleted in the SAME commit (its own row is in
the main table above), so this file outlived its subject by zero commits.

**Note on the row it leaves behind:** the QUEUED candidate
"`emission/quantity_styles.py`'s hand-mirrored copy of `_QGIS_STYLE_REGISTRY`"
in the cleanup-wave table cites this file by name as one of the two tests that
"exist purely to catch the drift between the two copies". That row still reads
QUEUED and its stated reason (the drift tests are the only thing standing between
a preset rename and a silently unstyled solver output) no longer describes a
living arrangement.

**Decided by:** ADR 0314, section 7 (the style contract). **Status:** DELETED
(commit f4a378c3).

## `tests/test_output_quantity_style_presets.py` - DELETED 2026-08-26

**What:** 59 lines, the second of the two mirror drift guards - the CI test that
asserted every `OutputQuantitySpec.style_preset` value named a real preset row.

**CONDITION to delete:** met by the same style contract, one commit later than its
twin. Every `quantity_id` is now a row in `styles.yaml`'s `quantity_defaults`, and
the contract's own self-consistency check (`tests/test_style_contract.py`, which
walks `contract.quantity_defaults()` against the declared presets) asserts the
same property from inside the single file rather than across two.

**Note on the row it leaves behind:** the QUEUED candidate "`output_quantities.py`
+ `publish_quantities.py` scaffold + its 4 live engine consumers' calls" names
`test_output_quantity_style_presets` inside its test list. That candidate is still
QUEUED as a whole; this one file of its scope is already gone.

**Decided by:** ADR 0314, section 7 (the style contract). **Status:** DELETED
(commit 20e6a1cb).

## `tests/test_publish_layer_titiler_style_resolver_f51.py` - DELETED 2026-08-26

**What:** 746 lines - the largest single deletion in the range - guarding the
TiTiler-era style resolver on the AWS s3 branch: the F51 percentile fallback, the
typed registry band, the paletted-COG no-rescale rule and the safe default. Its
imports were `_band1_percentile_rescale`, `_is_rgba_or_multiband`,
`_is_terrain_token_preset`, `_registry_style_params` and `_resolve_qgis_style_params`.

**CONDITION to delete:** met by the style contract deleting the symbols underneath
it. `_registry_style_params` and `_band1_percentile_rescale` are grep-to-zero
across `trid3nt_server/` and `tests/` at `02acbfed`; the 59 preset rows they read
are declared in `styles.yaml` and resolved by `emission/styles.py`. The file's
whole premise - a hardcoded two-entry if/elif that only flood and plume escaped -
describes an arrangement that no longer exists, and its docstring's framing (the
AWS deployment, TiTiler rendering the COG) describes a deployment that no longer
exists either.

**Superseded by:** `tests/test_publish_layer_style_resolver.py` (543 lines, added
in the same commit). `_resolve_qgis_style_params` SURVIVES - it is still the single
render chokepoint - so the coverage was rewritten onto it rather than dropped: the
data-policy range, the fixed-preset range, the NaN/flat/unreadable fallbacks and
the paletted-COG rule are all still asserted, against the contract instead of
against the registry.

**Decided by:** ADR 0314, section 7 (the style contract). **Status:** DELETED
(commit 20e6a1cb).

## The MALPASSET constellation - QUEUED 2026-08-26 (row corrected on registration)

**What:** `trid3nt_server/cases/malpasset_obs.py` (the observation-layer builder:
`build_malpasset_obs_layers`, `MALPASSET_VERTICAL_DATUM`, the police-HWM /
transformer / gauge FlatGeobuf writers), `scripts/run_l2_malpasset.py` (the L2
calibration harness), `tests/test_malpasset_obs.py`, the
`tests/fixtures/telemac_malpasset/` deck fixtures and the staged
`data/cases/malpasset/` case data.

**CONDITION to delete:** the STALE SWEEP wave. Nothing else. The chop is ruled and
unblocked; what it waits on is a wave with the mandate to execute it.

**CORRECTED, and what the correction strikes:** the ruling as recorded in
`docs/IDEAS.md` fenced this chop behind "it has a live import in
postprocess_telemac.py, so the chop waits for wave C to land". THAT FENCE DOES NOT
EXIST. `postprocess_telemac.py` has no import of anything malpasset; its single
occurrence of the word is a comment at `:293` explaining where the free-surface
variable names were verified from ("Never guessed -- verified by parsing the
bundled `f2d_malpasset-small.slf` header"), which is a provenance sentence about a
fixture, not a dependency on this code. The complete importer list for
`trid3nt_server.cases.malpasset_obs`, repo-wide, is `scripts/run_l2_malpasset.py`
(three lazy function-local imports) and `tests/test_malpasset_obs.py` - a harness
script and its own test, both inside the constellation being chopped. ZERO live
product modules import it. The chop therefore awaits the stale sweep, not an
import removal, and no wave has to land first.

**Why it dies:** the L2 harness is a working mini-calibration loop (obs pairing,
NSE/KGE/RMSE skill metrics, friction adjustment toward the published band, re-run,
re-score), and the ruling is explicitly CHOP WITHOUT HARVEST: the calibration track
builds fresh, with published methods as design references under paper-first and no
code inheritance. Superseded as the V&V exemplar by coastal-surge-vs-CO-OPS.

**Decided by:** NATE (malpasset chop-no-harvest ruling, 2026-08-26). **Status:**
DELETED (commit 7808c311). One consumer the prior registration missed:
`tests/test_postprocess_telemac_wse.py` dynamically loaded
`scripts/run_l2_malpasset.py` (via `importlib`) to test the driver's
`adjust_deck_friction` helper -- that test-only import, not a product import;
the two driver-friction tests and the `_load_driver` loader were removed from
that file with the chop (the file's own WSE tests, which use "malpasset-like"
node coordinates purely as an arbitrary synthetic fixture name, are untouched
and still pass). `trid3nt_server/workflows/telemac/postprocess_telemac.py`'s
sole mention remains the pre-verified provenance comment, not an import --
left in place, unmodified, per the workflows/ no-touch rule.

## `workers/telemac/artemis_build.py`'s `demo_bw` branch - DELETED

**What:** the third source-of-structure branch in the real-bathymetry diffraction
builder (`artemis_build.py:635-642`). When the deck names neither
`breakwater_polylines` nor a `breakwater` segment, it meshes a schematic
semi-infinite barrier from the west AOI edge to an interior tip AND overrides
`wave_dir_deg` to 90.0 so the geometric shadow sits due-north.

**Why it dies:** it invents the very thing the run is asked to evaluate. The
sheltering question is meaningless without the thing that shelters, and WHICH
thing is not the worker's to decide; the step already declares the slot
producer-less and refuses to go looking. The heading override is the more
dangerous half - it silently discards a declared `wave_direction_deg` and returns
a field solved at a different incident angle than the one the run states. The
structure is a SLOT with two legal answers, supplied or absent, and this branch
is a third answer nobody can ask for.

**CONDITION to delete:** a wave that can rebuild the ARTEMIS worker image and
smoke a diffraction run through it. Worker code is inert until the image is
rebuilt, so landing the deletion without the rebuild changes nothing that runs
and proves nothing. The deletion is the branch plus the `wdir = 90.0` override;
an unfilled slot then meshes no barrier and keeps the declared heading.

**Interim, already landed:** the run no longer LIES about it.
`steps/agitation.py::_structure_row` reads the worker's own `bw_label` /
`structure_present` echo instead of the deck it sent, so a domain carrying an
unrequested barrier says so, and a solve that reports nothing reads UNMEASURED
rather than open water. That closes the honesty hole; it does not close this one.

**Decided by:** the panel-2 remediation wave's honesty pass. **Status:** DELETED
(`53591921`). The condition was met by the fetch-migration wave, which rebuilds
the TELEMAC image anyway: the branch, the `wdir = 90.0` override and the
`if demo_bw:` dispatch are gone, an empty structure set meshes nothing, and the
sheltered/exposed split projects about the DOMAIN CENTRE when there is no
structure - previously a mean over an empty selection, which is where the
all-NaN transect the first post-deletion canary produced came from.

The physics MOVED, and that is the point. The coarse canary supplies no
structure, so the recorded evidence was of the fabricated barrier: `kd_sheltered`
0.0 (a total shadow behind a wall nobody asked for) -> 0.098, `kd_exposed` 0.631
-> 0.524, `kd_max` 2.837 -> 2.764, `hs_max_m` 5.674 -> 5.527. What remains of the
shelter is Marquette's REAL land in the bathymetry, which is a harbour, not an
invention. Live diffraction smoke: the coarse canary and the refined flagship
packet (PASS, 8 deliverables) both through the rebuilt image.


## `workers/telemac/_bed_cog.py` + the in-worker bed COG - DELETED

**What:** `write_bed_cog_lonlat` (109 lines) and the three call sites that wrote
`bed_bathymetry.tif` beside a coastal / wave / agitation result, plus
`steps/open_water.py::surface_in_worker_bed_input` (26 lines) which published it,
plus the two tests that covered the writer and the two that covered the publisher.

**Why it died:** it existed for ONE reason - a bed sampled inside a container
never touched the router, so the only way to put it on the canvas was for the
worker to rasterize its own nodes and hand the composer a key. Its output was a
node lattice: a 512 px grid clipped to 1.5 cells around each node, which on a
250 m coastal mesh is dots with nodata between them. The bed is now a declared
`fetch_ncei_dem_mosaic` producer staged into the run directory, so the emit-on-fetch
seam surfaces the SOURCE raster - continuous, and literally the data the nodes
were sampled from. Evidence: `docs/proof/templates/coastal_tidal_surge/addendum/
coastal_bed_input_continuous.png`, 100% of cells painted.

**Decided by:** the fetch-migration wave (ADR 0317). **Status:** DELETED
(`53591921`). The reach family's own `write_bed_cog` in
`telemac_river_dye_build.py` SURVIVES: its bed is still fetched in-worker, so the
seam that would replace this one does not exist for it yet.

## `solver_backend()` + `_local_compute_lane()` + the branches they gated - DELETED

**What:** `solver.solver_backend()` (a function that unconditionally returned
`"local-docker"`), `gates/cards/solver_confirm._local_compute_lane()` (which
compared that constant to itself), and every branch that read them: the
`NonLocalAuthUnsupported` raise and `_is_local_single_user_mode` in
`credentials/auth_handshake.py`, the cloud arm of `gates/confirm._gate_wait_timeout`,
six "cloud solve" / "AWS Batch" wording alternates on the confirm cards, and the
three `_*_route_enabled` guards in `server/protocol/catalog_http.py` that could
only ever return True.

**Why it died:** a predicate with one possible answer is not a seam, it is a
disguise. Its else-branches described a deployment that no longer exists and could
not be tested, and one of them - the fetch card's `"fetch"` compute label - was
still being ASSERTED by a test, so the suite was pinning behaviour the product
cannot produce.

**Decided by:** the fetch-migration wave's solver tightening (ADR 0317).
**Status:** DELETED. Three stale tests went with it
(`test_solver_backend_is_always_local_docker`, `test_non_local_mode_raises`,
`test_fetch_gate_compute_label_deployment_aware`) and two stale expectations were
corrected to the one lane that exists.

## The four duplicate object-store seams - DELETED

**What:** inline `boto3.client("s3", region_name=...)` constructions in
`emission/publish.py` (x2), `emission/pipeline_emitter.py`, `tools/cache.py`,
`tools/vector_tiles.py`, `cases/ingest_user_layer.py`, `fallbacks/persist.py`,
`workflows/telemac/release_layer.py`, `workflows/shared/run_products.py`,
`server/protocol/catalog_http.py` and `testing/live_run.py`, plus two of the three
independent `_split_s3_uri` implementations.

**Why it died:** `solver._get_s3_client` is the canonical seam - it is the only
one with an injectable client, ~70 modules already import it, and the duplicates
differed from it in nothing (same region default, same implicit endpoint, no
retry config anywhere). A test that injected a client saw some reads and not
others, which is the failure mode duplication of a client always has.

`tools/fetchers/_public_s3.py` SURVIVES and is not a duplicate: it builds an
UNSIGNED client pinned to real AWS for third-party open-data buckets, which is a
different store with a different auth posture.

**Decided by:** the fetch-migration wave's solver tightening (ADR 0317).
**Status:** DELETED.

## The reach family's six in-container fetches - DELETED

**What:** in `workers/telemac/telemac_river_dye_build.py` - `_http_get`,
`_snap_comid`, `_named_flowline_seed`, `_mainstem_flowline_seed`,
`resolve_centerline_seed`, the NLDI navigate inside `fetch_river_centerline`,
the NHDArea query inside `fetch_bank_polygons`, and the whole DEM ladder
(`_retryable_dem_excs`, `_sample_dem_stac`, `_sample_dem_3dep`,
`_fetch_dem_samples`, plus the `_DEM_STAC_*` / `_3DEP_IMAGE_URL` /
`_NLDI` / `_NHDPLUS_HR` / `_NHDAREA_URL` constants and the `urllib` imports).
Four `ReachConfig` fields died with them (`river_name`, `seed_from_release`,
`seed_release_lon`, `seed_release_lat`) because they were seed-ladder inputs and
the ladder is server tier now.

**Why it died:** a fetch changes if the box moves, so it is server tier - and
these six were outside emit-on-fetch, the cache, provenance, the declared
fallback ladders and the retry doctrine. Two of them were fail-OPEN, which cost
REPEATABILITY as well as visibility: a slow NHDPlus query kept the raw seed,
meshed a different reach, and left nothing in the record saying so.

**What replaces them:** `steps/reach.resolve_reach_river` - the seed ladder over
`fetch_nhdplus_hr_flowlines`, the centerline over `fetch_nhdplus_nldi_navigate`,
the banks over `fetch_nhd_area_water`, the bed over `fetch_copernicus_dem`
(3DEP fallback) - staged into the run directory through the manifest `inputs`
the launcher already walked. The worker opens files.

**Decided by:** ADR 0318. **Status:** DELETED.

## The reach bed COG and its surfacing helper - DELETED

**What:** `telemac_river_dye_build.write_bed_cog` + `BED_COG_*`,
`entrypoint.py`'s best-effort COG write and its `bed_bathymetry.tif` output
declaration, `products._surface_bed_bathymetry_input` and
`_BED_DEM_SOURCE_LABELS`, plus `tests/test_telemac_bed_bathymetry_manifest.py`
(replaced by `test_telemac_reach_staged_inputs.py`).

**Why it died:** the COG existed ONLY because a container fetch could not reach
the emit seam, and it painted the input as a scatter of the node samples the
solver kept rather than as the terrain the run was handed - the third node-dot
instance NATE caught. The staged source raster is continuous and IS the data the
nodes are sampled from, and emit-on-fetch surfaces it for free. This closes the
last bespoke `_surface_*input*` helper: the seam is now the only path.

**Decided by:** ADR 0318. **Status:** DELETED.

## The two worker test files whose seams moved to the server - DELETED

**What:** `workers/telemac/tests/test_release_seed_preference.py` and
`workers/telemac/tests/test_dem_fallback.py`.

**Why they died:** each pinned a pure decision seam inside a function this wave
deleted - the release-vs-geocode seed preference and the STAC-then-3DEP DEM
ladder. Both behaviours moved to `steps/reach.py` and are pinned there
(`tests/test_telemac_reach_river.py`), so keeping the worker copies would have
been two tests of code that no longer exists.

**Decided by:** ADR 0318. **Status:** DELETED.

## Stale-sweep 2026-08-27 one-liners (Area A: malpasset chop-no-harvest)

- trid3nt_server/cases/malpasset_obs.py: malpasset observation-layer builder, chop-no-harvest, zero live product importers (stale-sweep 2026-08-27)
- scripts/run_l2_malpasset.py: malpasset L2 calibration harness driver, chop-no-harvest ruling (stale-sweep 2026-08-27)
- tests/test_malpasset_obs.py: test of the deleted malpasset_obs.py harness (stale-sweep 2026-08-27)
- tests/fixtures/telemac_malpasset/: malpasset deck fixtures, zero remaining consumers (stale-sweep 2026-08-27)
- data/cases/malpasset/: staged malpasset case data, chop-no-harvest ruling (stale-sweep 2026-08-27)
- tests/test_postprocess_telemac_wse.py driver-friction tests + _load_driver helper: tested the deleted run_l2_malpasset.py driver only (stale-sweep 2026-08-27)
- scripts/sandbox/oceanmesh/_runs/: mesh-build run artifacts (downloaded DEM rasters, intermediate shoreline clips, npz meshes) for the 5 already-persisted meshes in docs/proof/templates/oceanmesh_meshes/, regeneratable via build_coastal_mesh.py / build_coastal_water_edge_mesh.py / build_watershed_mesh.py (stale-sweep 2026-08-27)
- scripts/sandbox/oceanmesh/_work/: scratch logs + one temp topobathy tif from mesh-build runs, superseded by the persisted _runs summaries (stale-sweep 2026-08-27)
- scripts/sandbox/hecras/_ref/: full 350MB-source-derived HEC BaldEagleCrkMulti2D reference folder, already trimmed into the product fixture workers/hecras/fixtures/baldeagle_connection/ (PROVENANCE.md documents the SHA-pinned public re-download) (stale-sweep 2026-08-27)
- scripts/sandbox/pysheds_watershed/_work/: scratch DEM tifs + summary from a watershed-delineation proof run, regeneratable via proof_watershed.py (stale-sweep 2026-08-27)
- scripts/sandbox/oceanmesh/__pycache__/, scripts/sandbox/replication/__pycache__/, scripts/sandbox/telemac/__pycache__/, scripts/__pycache__/: compiled bytecode caches (stale-sweep 2026-08-27)

## The standalone mesh builder - DISSOLVED into the one mesh router (2026-08-27)

**What:** `trid3nt_server/workflows/mesh/generate_mesh/` entire (976 lines):
`generate_mesh.py` (the registered tool, its mode inference, its watershed and
coastal water-edge build providers, its stage-and-record, its SCHISM gr3
emission and its `.2dm` writer), `hecras_build.py` (the graded-seed build and
record), `__init__.py`, and `corpus.yaml` (its routing phrasings, merged into
the router's own).

**Why it died:** it was a second mesh entry point. Its standalone-tool role IS
`build_mesh` called standalone, and its three builders are registered meshers -
`watershed`, `coastal_edge` and `hecras_rog` - each declaring its own fields and
its own edits instead of being selected by a mode string inferred from which
arguments happened to be present. Its display writer moved to
`emission/mesh_display.py`; its stage-and-record is the mesh session's; its
edge-band `ResolutionSpec` declarations ride `build_mesh`'s metadata; its SCHISM
gr3 emission belongs to the coastal mesher, which is the only shape that has a
seaward boundary.

**Trace evidence:** zero importers of the package remain (`grep -rn
"workflows.mesh.generate_mesh"` over `trid3nt_server/`, `tests/`, `scripts/`,
`workers/`, `contracts/` and `plugin/` returns nothing; the surviving hits are
this ledger, the LOC ledger, the blueprint and the decision records, all of
which are history rather than callers); the
registered tool is gone from `TOOL_REGISTRY` (260, was 261) and the catalog
identity gate is green; the two showcases and the live HEC-RAS proof driver now
invoke `build_mesh` with an explicit `mesher`; the fallback sweep guards that
pinned its coastal bed fetch, its bed provenance and its sizing claim now pin the
same behaviours on `meshers/coastal_edge.py` and `meshers/watershed.py`.

**Status:** DELETED.

## The duplicate `.2dm` writer - DELETED (2026-08-27)

**What:** `generate_mesh._write_2dm` (the array-shaped writer) and
`workflows/mesh/session.write_2dm` (the mesh-shaped one).

**Why they died:** two implementations of one ASCII format, differing only in
what they took. `emission/mesh_display.py` holds the one writer with both entry
points, and the cell-arity check that guarded only the session's copy now guards
both. Mesh display is a data type on the emission seam: geometry that feeds a
solver is the mesh front's business, geometry that feeds a screen is emission's.

**Status:** DELETED (superseded, not removed - the behaviour moved).

## `tests/test_generate_mesh.py` - DELETED 2026-08-27

**What:** the standalone mesh builder's suite, 455 lines.

**Why it died:** its subject is gone. Its four mode-inference tests tested a
selector that no longer exists - a caller NAMES the mesher now - and every other
test in it was about behaviour the meshers, the artifact and the precondition
gate still carry.

**Superseded by:** `tests/test_mesh_meshers.py` (552 lines), which keeps the
engine-compatibility, sidecar, case-stash, gate and HEC-RAS bundle tests verbatim
and replaces the mode-inference four with the roster, the per-mesher field
declarations, the corridor's typed domain refusals and the adopted-layer
topology check.

**Status:** DELETED (superseded).

## The approve-mesh gate's CLIENT half - DELETED 2026-08-27

**What:** `plugin/ui/gate.py` `release_point_required` + `release_point_bbox` and
the `release_point` argument of `resolve_gate_decision`; `plugin/ui/cards.py`
`GateCard`'s release-point picker row, its map-tool toggle, its click handler and
its teardown (plus the now-dead `iface` / `to_lonlat` constructor arguments and
the "Continue" button relabel); `plugin/tests/headless_bk3b_approve_mesh_drive.py`
and `plugin/tests/validate_bk3b_driver_offline.py`.

**Why it died:** the server-side approve-mesh GateSpec that set
`tool_args.release_point_required` and `tool_args.mesh_bbox` was deleted with the
template-specific gate metadata; nothing has produced either key since. The card
rendered a picker no envelope asked for and the two drivers asserted a contract no
server emits.

**Superseded by:** the standard mesh gate loop
(`trid3nt_server/workflows/mesh/gate.py`), whose card is the param sheet the
shipped client already renders, and
`plugin/tests/headless_mesh_gate_drive.py` +
`plugin/tests/validate_mesh_gate_driver_offline.py`, which assert THAT sheet - the
`<action>.<input>` knob rows and the `restart` row - and keep the river-dye
peak-layer and bank-metrics witnesses verbatim.

**Status:** DELETED (superseded).
- trid3nt_server/mcp_server.py: MCP server, zero product consumers - NATE purge ruling, separate piece for its own future attention (mcp-purge 2026-08-27)
- tests/test_mcp_server.py: tested only the deleted mcp_server module (mcp-purge 2026-08-27)
- docs/design/mcp-server.md: design doc for the deleted server; decision 0302 stays as history (mcp-purge 2026-08-27)
- .mcp.json.example: client config example for the deleted server (mcp-purge 2026-08-27)
- trid3nt_server/mesh/preview_gate.py: superseded shared approve-mesh preview gate, zero production consumers - D-8 ruling at mesh wave close (2026-08-28)
- tests/test_mesh_preview_gate.py: tested only the deleted module (2026-08-28)

## Fresh-start purge (2026-08-28)

The product narrows to TELEMAC plus the shared mesh/lib/solver spine. Everything
below left the repo for `/home/nate/Documents/trid3nt-attic`, mirroring its
repo-relative path. Git history remains the archive; the attic is a reading copy
and is on no import path.

Meshers (roster after: `om2d` + `reg_grid`):
- trid3nt_server/workflows/mesh/meshers/telapy_mesh.py: TELEMAC-geometry adoption mesher; its in-container driver was already deleted - moved to attic (fresh-start purge 2026-08-28)
- trid3nt_server/workflows/mesh/meshers/watershed.py: catchment mesher wrapper; the catchment STRATEGY (workflows/mesh/watershed.py) stays - moved to attic (fresh-start purge 2026-08-28)
- trid3nt_server/workflows/mesh/meshers/coastal_edge.py: coastline/water-polygon mesher; its water-edge prep folds into om2d in a later wave - moved to attic (fresh-start purge 2026-08-28)
- trid3nt_server/workflows/mesh/meshers/corridor_tin.py, hecras.py, drivers/coastal_edge_driver.py, drivers/telapy_mesh_driver.py: deleted in the working tree ahead of this pass; recovered from HEAD - moved to attic (fresh-start purge 2026-08-28)

Engine workflow packages:
- trid3nt_server/workflows/{calibration,elmfire,geoclaw,hecras,landlab,modflow,openquake,pelicun,schism,sfincs,swan,swmm}/: every non-TELEMAC engine package - moved to attic (fresh-start purge 2026-08-28)

Old root mesh package (its two live consumers were absorbed first):
- trid3nt_server/mesh/{coastal_tin,hecras_geometry,mesh_preview,modflow_package_validation,raster_cell_mesh,refine_regions,swmm_deck_runner,swmm_mechanism_compare,swmm_network,_swmm_solve_subprocess,__init__}.py: consumed only by the moved engine packages - moved to attic (fresh-start purge 2026-08-28)
- trid3nt_server/mesh/spatial_roles.py -> trid3nt_server/gates/spatial_roles.py: ABSORBED, not moved; its only live consumer is gates/spatial_input.py (fresh-start purge 2026-08-28)
- trid3nt_server/mesh/grid_geometry.py -> trid3nt_server/workflows/mesh/grid_geometry.py: ABSORBED, not moved; its live consumers are workflows/mesh/session.py and meshers/reg_grid.py (fresh-start purge 2026-08-28)

Tests (131 files) whose subject is a moved engine or a moved module:
- tests/test_{elmfire,geoclaw,hecras,landlab,modflow,openquake,pelicun,schism,sfincs,swan,swmm}_*.py and the composer/gate/postprocess tests that drive them (model_flood_scenario, granularity gate, archetype emission, capture-zone / saltwater / subsidence / seepage / stream-depletion, mesh_layer, refine_regions, corridor readopt, worker offload, zoom-to smokes) - moved to attic (fresh-start purge 2026-08-28)
- moved-subject sections stripped from kept tests: test_gemini_kwargs_fuzz.py, test_search_tools.py, test_search_tools_mongo_backend.py, test_job0305_memoryfile_lifetime.py, test_telemac_input_provenance.py, test_publish_manifest_register_only_phase4.py, test_spatial_input_barriers.py (fresh-start purge 2026-08-28)

Scripts (74 files + 4 sandbox trees) whose subject is a moved engine:
- scripts/{drive,proof,run,smoke,plot,harvest,demo}_*.py for elmfire/geoclaw/hecras/landlab/modflow/openquake/pelicun/schism/sfincs/swan/swmm, the three law-9 A/B proofs, the corridor hand-edit proof, run_l2_validation_harness.py, _slab2_fixture.py - moved to attic (fresh-start purge 2026-08-28)
- scripts/sandbox/{hecras,modflow,schism,fire_render}/: engine sandboxes; oceanmesh, telemac, replication and pysheds_watershed stay - moved to attic (fresh-start purge 2026-08-28)

## Polygon producers + mesh/shared (2026-08-29)

The LEGO ruling makes domain narrowing a chain of processing tools, and the
RIBBON RULING settles what a reach domain may be. What that supersedes:

- trid3nt_server/tools/processing/corridor_of/: the buffered-flowline ribbon producer, written against the pre-ruling design and never committed - DELETED outright, no attic copy. CONDITION: none; the RIBBON RULING (2026-08-29) makes a buffered flowline unacceptable as a mesh domain with no fallback rung, so there is nothing here to restore. The reach producer is `section(fetch_nhd_area_water banks, between=<endpoints>)`. The APPROXIMATE-REACH RULING (2026-08-29) closes the one exemption the ribbon ruling had left: the buffer band is superseded in release-point work too - validity is containment in the actual domain polygon and the snap is to the nearest point on the real flowline inside it - so the existing buffer-band snap plumbing follows this out. That removal is its own landing and is NOT part of this one.
- trid3nt_server/workflows/mesh/tool.py `_acquire_domain` + the `"domain" in declared` branch: acquired a reach + seed INSIDE build_mesh for a mesher whose extent was a domain rather than a box. No mesher has declared a `domain` field since the mesher purge, and the LEGO ruling puts that acquisition in the plan chain rather than in the router - DELETED (superseded by the polygon-domain extent path).
- trid3nt_server/workflows/mesh/meshers/telapy_mesh.py::write_telemac_pair + its container run helpers -> trid3nt_server/workflows/mesh/shared/selafin_cli.py (module) and .../meshers/drivers/selafin_cli_driver.py (in-container writer): MOVED, not deleted. The writer is named for what it writes - the SELAFIN geometry and the `.cli` numbered from its IPOBO - and lives beside no mesher because any mesher that can hand over nodes, cells and a bed writes through it. om2d's lazy import repoints; the host module its name came from is already in the attic.

Tests whose subject left the tree:
- tests/test_mesh_om2d_telapy.py -> tests/test_mesh_om2d.py: RENAMED and trimmed. The `telapy_mesh` adoption section (its stub, its refusals, its coordinate honesty) and the roster/parametrize/driver-inventory rows naming purged meshers went with their subject; the two `hgrid.gr3` cross-checks went with `workflows/schism/deck_authoring`. The om2d half is unchanged and green.

## Tools-stage stops (2026-08-29)

- trid3nt_server/workflows/mesh/meshers/om2d.py `_gr3` + its `hgrid.gr3` emission
  + the `engine_compat.append("schism")` it fed: the bridge imported
  `trid3nt_server.workflows.schism.deck_authoring`, which the fresh-start purge
  moved to the attic, so every build took the `except` arm and warned-and-dropped.
  DELETED, no attic copy (git history is the archive). CONDITION: none - a SCHISM
  rung brings its own geometry needs and would author its writer against the mesh
  that exists then, not restore this bridge. om2d now emits the TELEMAC pair plus
  the display face only, and `engine_compat` never names schism.
- scripts/sandbox/oceanmesh/water_edge.py `river_corridor_water` + its call site in
  build_watershed_mesh.py: buffered the flowlines into a corridor clipped to the
  catchment. The RIBBON RULING (2026-08-29) forbids a buffered flowline as a mesh
  domain and the APPROXIMATE-REACH RULING extends that past domains, and this
  corridor had already stopped being the domain - the driver meshes the catchment
  polygon and the corridor survived only as a reported area. DELETED with its
  `corridor_km2` summary key and caption clause; no attic copy. CONDITION: none.

## Remedy-stage stops (2026-08-29)

Release containment moved SERVER-SIDE and the dead artifact residue went, per the
REMEDY-STAGE STOPS ruling.

- The release-point BAND SNAP, both halves: the worker's accept-radius test in
  `workers/telemac/telemac_river_dye_build.py::spill_point` (two stated channel
  widths, or 1.5x the widest real bank span, else walk `spill_fraction` and record
  the miss), and the server plumbing that read its verdict back out of the run -
  `telemac_metrics.json`'s `release_point_used` / `release_point_rejected_dist_m`,
  their two `run_telemac.py` METRIC_KEYS rows, `steps/solve.py`'s
  `raise_if_release_point_rejected` and its post-solve call site, and
  `steps/errors.py::TelemacReleasePointRejectedError`. ATTICKED to
  `workers/telemac/release_band_snap.py` (both halves in one reference file; the
  worker module itself left the tree with the worker-unification pre-delete).
  CONDITION: none - the APPROXIMATE-REACH RULING (2026-08-29) settles that release
  validity is CONTAINMENT in the domain polygon and the snap is the nearest point
  on the real flowline inside it, decided before anything is staged. The
  replacement is `workflows/telemac/release_point.py`, wired into `steps/deck.py`;
  the typed refusal keeps the `TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN` code and
  drops the band vocabulary from its message.
- `MeshArtifact.gr3_uri` + `MeshArtifact.fort14_uri`: declared-but-unwritten
  geometry fields. om2d's gr3 seam was chopped at the tools stage and nothing under
  `workflows/mesh/` ever wrote a fort.14, so both were `None` on every artifact the
  tree can build. DELETED, no attic copy (git history is the archive). CONDITION:
  none - a returning engine authors the field it actually reads from the geometry
  it actually needs.
- `ENGINE_MESH_REQUIREMENTS["schism"]` + `["swan"]` rows, and the
  `unstructured_unsupported` / `needs_open_boundary` branches plus
  `open_boundary_node_count` that only those rows reached. Both engines are in the
  attic; a requirement row written for an absent solver is a claim nothing backs,
  and `"no mesh-compatibility rule registered for engine 'schism'"` is the honest
  refusal until SCHISM returns at rung 5. DELETED, no attic copy. CONDITION:
  SCHISM/SWAN return through the new architecture and author their rows from their
  needs then; `MeshArtifact.open_boundary_info` STAYS (om2d writes it, the
  agitation step and the artemis rematch read it).
- `scripts/drive_mesh_spotcheck.py`: the two dead uri keys in its artifact dump and
  the `--mesher` help string naming five purged meshers. DELETED.

Tests whose subject left the tree:
- `tests/test_run_river_dye_scenario.py` section (7) "the RELEASE POINT: the
  worker's verdict is reconciled": every row tested the band verdict, its metrics
  fixtures and its post-solve reconciliation. DELETED with the subject; the
  replacement behaviour is covered offline by `tests/test_release_containment.py`
  (honored unmoved, snapped onto the flowline, refused outside the polygon, the
  snap never following the river out of the domain).
- `tests/test_mesh_meshers.py`: repointed to the meshers the tree carries. The
  roster, the per-mesher field table, the unknown-field refusal and the
  adopted-layer test now name `om2d` / `reg_grid`; the coastal open-side
  vocabulary, the two `corridor_tin` refusals, the `hecras` mesher source
  inspection and the whole watershed-provenance block went with their meshers; the
  three SCHISM compat rows and the SWAN one became one row asserting the honest
  "no rule registered" refusal.

## Declared-input contracts (2026-08-29)

The kind a template declares IS its accept-set, so the softer paths that stood in
for one go, per the DECLARED-INPUT CONTRACTS ruling.

- The TRUST+TELL branch in `steps/deck.py::_settle_release`: a mesh whose spec
  extent was a bbox returned `None` from `domain_polygon_of`, and the supplied
  release point rode into the deck untested with a note saying the containment
  test could not be made. DELETED, no attic copy. `domain_polygon_of` now RAISES
  the typed refusal on a mesh that states no domain polygon, so release
  containment has exactly one path and it always has a polygon to be inside of.
  CONDITION: none - a release admitted by a shape nobody mapped is the thing the
  pre-flight exists to prevent, and a run whose mesh has no polygon is a run the
  reach templates were never meant to author.
- `tests/test_release_containment.py::test_a_mesh_cut_from_a_box_states_it_has_no_domain_polygon`:
  the test OF the deleted branch. Replaced in place by
  `test_a_mesh_with_no_domain_polygon_refuses_rather_than_waving_the_point_through`,
  which pins the refusal over the same three inputs.

Tests whose subject left the tree with the mesher roster narrowing (`6368bb69`):
- `tests/test_build_mesh_tool.py::test_a_mesher_that_takes_no_extent_refuses_one_by_name`
  named the purged `telapy_mesh`. Every mesher the tree carries cuts its domain
  from an extent, so the test now stands a geometry-adopting mesher up in the
  registry for its own duration; the clause it pins - a mesher declines a field it
  never declared, BY NAME - is unchanged.
- `tests/test_mesh_gate_loop.py::test_a_mesher_with_no_vocabulary_knob_keeps_the_card_it_had`
  named the purged `corridor_tin`. Repointed to `reg_grid`, which is now the
  mesher whose every knob is a number.

## graded_cells and the HEC-RAS mesh residue (2026-08-29)

The kind word `graded_cells` named a shape no registered mesher builds: the
`hecras_rog` mesher left with the fresh-start purge, so every surface written for
it was describing an absent producer. Same dead-residue class as the chopped
`schism` / `swan` requirement rows, and chopped the same way.

- `MeshKind`'s `graded_cells` member. DELETED, no attic copy (git history is the
  archive). CONDITION: HEC-RAS brings its own vocabulary back at rung 5, and the
  word is re-declared then by the mesher that builds it.
- `ENGINE_MESH_REQUIREMENTS["hecras"]` and the `bundle` / `needs_validated`
  branches of `mesh_compatible_with_engine` that only that row reached. An ask for
  HEC-RAS now gets the honest "no mesh-compatibility rule registered for engine
  'hecras'". DELETED. CONDITION: HEC-RAS returns and authors its row from the needs
  it has then.
- `HECRAS_INPUT_KEYS`, `_HECRAS_REQUIRED_KEYS`, `materialize_hecras_mesh_inputs`,
  and `MeshArtifact`'s `hecras_inputs` / `channel_target_size_m` /
  `background_size_m` / `cells_validated` fields. Nothing wrote them and only the
  deleted bundle branch read them. DELETED.
- The `meta["bundle"]` mesher channel: its staging in
  `MeshSession._staged_files`, its membership in `_TOPOLOGY_BOUND_META`, and the
  `MESH_EDIT_NOT_STAGEABLE` branch of `_refuse_unadoptable` that quoted it. The
  channel existed to carry an engine's authoring inputs from `hecras_rog` to
  `hecras_inputs`; with both ends gone it had no producer and no consumer. DELETED.
  The `has_cells` arm of that refusal STAYS - it is about a mesh that states no
  cells, not about a bundle.
- `scripts/seed_showcase_cases.py`: the `hecras_rog` showcase row, which named a
  mesher `build_mesh` no longer registers. `scripts/drive_mesh_spotcheck.py`: the
  `hecras_inputs` key in its artifact dump and the mesher name in its location
  comment. DELETED.
- `tests/test_mesh_meshers.py` section "HEC-RAS RoG channel-refined mesh": the
  artifact round-trip, the four compat cases, the gate case and the two bundle
  materialization cases. DELETED with their subject.

## Compatible, superseded by Accepts (2026-08-29)

- `trid3nt_server/workflows/mesh/kinds.py::Compatible` and its re-export from
  `workflows/mesh/__init__.py`. The single-role accept-set generalized to the
  role-keyed `Accepts` in `workflows/lib/accepts.py`, so the mesh-only spelling had
  nothing left to say. DELETED, no attic copy. `MeshKind` stays where it is - it is
  mesh vocabulary and the `mesh` row is typed to it.
- The direct `from ...agitation.declarations import COMPATIBLE` at the ARTEMIS
  supply door in `steps/agitation.py`. The door names its tool and
  `mesh/tool.py::accepts_for` reads the contract off the registry, so a step is no
  longer a second place a template's contract is read from. DELETED.

## The three watershed resolver shims (2026-08-30)

P1 gave the interpreter's loader a TOOL-REGISTRY lookup, so a `Data` producer can
name a registered tool directly. The three `workflows/mesh/watershed.py` resolvers
existed only to reach `TOOL_REGISTRY[...]` from a declaration and to restate a
ladder the fetcher router already runs and already labels on `LayerURI.fallback_note`
- a second home for middleware that has one.

- `resolve_bed_dem`. DELETED, no attic copy. `telemac_rain_on_grid` declares
  `Fetch.tool("fetch_dem", bbox=Ref("aoi.bbox"), source="3dep", ...)`. The shim's
  automatic 3DEP -> Copernicus swap goes with it: a PINNED source never switches, so
  a 3DEP outage now surfaces the fetcher's own typed error naming copernicus and the
  cross-dataset substitution is the user's to make, which is what the fallback norm
  asks for.
- `resolve_landcover`. DELETED, no attic copy. Replaced by
  `Fetch.tool("fetch_landcover", bbox=Ref("aoi.bbox"), ...)`.
- `resolve_river_network`. DELETED, no attic copy. Replaced by
  `Fetch.tool("fetch_river_geometry", bbox=Ref("aoi.bbox"), ...)`. The shim swallowed
  a flowline-fetch failure into a "uniform sizing" note; a failed fetch is now the
  fetcher's typed error. CONDITION: if a headwater basin with no mapped channel must
  still mesh, that is a declared `.optional()` slot or a chained tool, never a
  swallow inside a producer.
- `_domain_bbox` STAYS: the rain producers in `steps/rain_on_grid.py` call it, and
  it is the domain read itself rather than a wrapper around a tool.
- `tests/test_mesh_bed_dem_cross_dataset.py` tested the shim's own loud swap, which
  no longer exists. MOVED to the attic.

## Purge residue: the SWMM gate chunk and the moved engines' diagnostics (2026-08-30)

Ruled with the DS rulings (2026-08-28). Both were surfaces whose SUBJECT left the
tree with the non-TELEMAC workflow purge, so they described absent producers.

- `trid3nt_server/gates/cards/solver_confirm.py`: `_clamp_swmm_resolution_to_cap`,
  `_build_swmm_granularity_envelope`, `estimate_swmm_granularity`,
  `pin_swmm_granularity` (237 lines). They reached
  `workflows.swmm.urban_flood` and `mesh.raster_cell_mesh`, neither of which is in
  the tree. MOVED to
  `trid3nt-attic/trid3nt_server/gates/cards/solver_confirm_swmm_granularity.py`.
  Their re-exports from `gates/cards/__init__.py` went with them. The SHARED
  scaffolding stays: `_gate_memory_key`, the fetch-resolution gate, the flood
  run-settings gate, `MAX_FETCH_PX` and the ladder tables.
- `trid3nt_server/workflows/solver/diagnostics/{geoclaw,modflow,sfincs,swmm}.py`.
  MOVED to `trid3nt-attic/trid3nt_server/workflows/solver/diagnostics/`.
  `read_run_diagnostics` now dispatches `telemac` alone; an ask for any other
  engine gets the honest `DiagnosticsEngineUnknown` it already raised for an
  unregistered name. CONDITION: each parser returns WITH its engine at rung 5,
  re-authored against the diagnostics the engine actually writes then.

## Purge-fallout test residue (2026-08-30)

The non-TELEMAC workflow purge left test modules importing absent engines, pinning
rosters that shrank, and asserting on files that moved. Repointed where the SUBJECT
is still in the tree; atticked where the subject moved with its engine.

MOVED to the attic with their subject:
- `tests/test_declarative_generalization.py` - the checkpoint's two declared
  templates were SWMM and MODFLOW. The library seams it also covered (Derived
  evidence, the honest real_source rule, RunResult.params) return with the
  templates that exercise them. CONDITION: re-authored at rung 5 against whichever
  engine lands first, never restored verbatim.
- `tests/fixtures/validation/{sfincs,swmm,modflow,geoclaw}/` - diagnostics
  fixtures whose parsers went to the attic in the same pass.

REPOINTED (subject kept, engine vocabulary swapped):
- `tests/test_solver_local_docker.py` drives the local-docker envelope through the
  registered `telemac_river_dye` LocalSolverSpec instead of `sfincs`. Its two
  SFINCS-BUILDER sections (`_default_setup_uri` / deck upload / `_to_vsigs` /
  `postprocess_flood` reads) went with `workflows/sfincs`. DELETED.
- `tests/test_read_run_diagnostics.py` keeps the registration, envelope-shape,
  handle-resolution, TELEMAC and error-path coverage on the telemac fixtures. The
  four per-engine parser sections DELETED. `test_artifact_missing_when_...` too:
  the TELEMAC parser reads both of its artifacts OPTIONALLY, so with only telemac
  registered nothing in the tree can raise `DiagnosticsArtifactMissing`. The
  exception class STAYS - it is the contract for an engine with a required
  artifact, and the next one to land re-earns the test.
- `tests/test_gate_collapse_specs.py`: the solver lane is now EMPTY (every engine
  template stops at the mesh gate), asserted as a derived property rather than a
  name list. The per-engine byte-equivalence tests DELETED with their tools.
- `tests/test_solver_confirm_gate.py`: the per-engine solver cards and the
  deployment-aware recommendation prose DELETED; the generic gate machinery is
  still covered through the FETCH lane.
- `tests/test_local_subprocess_runner.py`: the landlab/openquake spec registration
  + argv + PYTHONPATH sections DELETED; the engine-agnostic runner half stays.
- `tests/test_fallback_ladder.py`: the geoclaw/schism/swan/sfincs CONSUMER
  sections DELETED (~310 lines). The ladder machinery itself - walk, gaps,
  declines, coverage, the loudness floor, the topobathy hook - is untouched.
- `tests/test_fallback_sweep_guard.py`: SHAPE 2 (the `coastal_edge` mesher's bed)
  and SHAPE 2b (geoclaw/schism transport faults) DELETED with their code. The
  PARKED register loses rows 12b / 16 / 17 / 18 (SFINCS + SWMM files that left)
  and row 25 (the river-dye ribbon fallback, whose worker script NATE pre-deleted
  and whose server-side path the RIBBON RULING removes).
- `tests/test_input_layer_surfacing.py`: the OpenQuake fault-serialization section
  DELETED; the ADR 0244 allow-list pruned to the files that exist.
- `tests/test_resolution_doctrine_0224.py`: R-A (schism `_topobathy_fetch_kwargs`)
  and the surge estimator / mesh-density sections DELETED with the schism legs;
  R-B (payload sampling) and R-C (topobathy warnings) stay.
- `tests/test_live_drive_fixes_0104.py`: four of the six bug regressions named SWMM
  or the pre-deleted per-process TELEMAC build scripts. DELETED; the two whose code
  is still here stay.
- `tests/test_emit_on_fetch_equivalence.py`: the landlab and sfincs purpose tables
  DELETED. The rain-on-grid entry now reads the TEMPLATE's own `Fetch.tool(...)`
  declarations, which is where those fetches live after the shim deletion.

## The ribbon, in the kept tree (2026-08-30)

Per the RIBBON RULING (NATE 2026-08-29): a reach domain is the real mapped water
polygon or a typed refusal, and no fallback rung exists. The worker still carries
its own ribbon code (workers/ is frozen); nothing in the server asks for it.

- `steps/deck.py::_BANK_SOURCES` loses `constant_ribbon` and its four spellings;
  `normalize_bank_source` now coerces to the one-member set {nhd_area}. DELETED.
- `steps/errors.py::TelemacBanksUnavailableError` -> `TelemacReachBanksUnmappedError`,
  error code `TELEMAC_BANKS_UNAVAILABLE` -> `REACH_BANKS_UNMAPPED`. Its
  `assumed_channel_width_m` argument and the ribbon retry in `.suggestions` are
  DELETED; the suggestions now name the SUPPLY paths (draw the polygon, name a case
  layer, pick a covered reach). The worker's own code word is unchanged - the
  translation is `steps/solve.py::raise_if_banks_unavailable`.
- `TelemacReachDegenerateError`'s `bank_source="constant_ribbon"` retry. DELETED.
- `steps/forcing.py::review_resolved_inputs`' bank row: the `default_demo` /
  "assumed constant-width ribbon" arm. DELETED - the row is always the real banks.
- `steps/products.py`: the `bank_geometry` row's `default_demo` arm and the
  honesty-note's ribbon sentence. DELETED. `bank_provenance` defaults to
  `nhd_area` rather than `constant_ribbon`.
- `run_telemac.py`'s `assumed_channel_width_m` completion key. DELETED - nothing
  reads it now that the refusal names no width.
- The `bank_source` param prose in both reach templates' declarations.

### Same-class residue this slice did NOT touch (queued)

`gates/cards/solver_confirm.py` still holds `_build_psha_confirm_envelope`,
`_build_scenario_confirm_envelope`, `_build_fire_confirm_envelope`,
`_build_geoclaw_confirm_envelope` and their four `estimate_*` providers. Their
TOOLS (openquake / elmfire / geoclaw) left with the purge, no `gate_spec` names
them, and their byte-equivalence tests went with the tools - so they are the same
dead-residue class as the SWMM chunk above. The residue ruling named only the SWMM
functions, so they stay pending a ruling of their own.

### layer_field earns a shared home (2026-08-30)

`steps/reach.py::layer_field` MOVED to
`workflows/shared/layer_fields.py`. It reads one field off whatever shape a
fetched layer arrived in (a `LayerURI`, a replayed model, a stub mapping). The
shim deletion gave it a second consumer - `mesh/watershed.py` and
`steps/rain_on_grid.py` now read the artifacts the registered fetchers return
directly - and a second consumer is what earns the split.

### The repoint stops execute their deletions (2026-08-30)

REPOINT STOPS RULED (docs/IDEAS.md 2026-08-30). Four removals, each with the
ruling that authorized it.

**DS-2 - the retired catchment mesher's two knobs.** DELETED:
`telemac_rain_on_grid`'s `Param("mesh_max_iter")` and
`Param("outlet_snap_cells")`, with the `DEFAULT_MAX_ITER` /
`DEFAULT_OUTLET_SNAP_CELLS` imports that fed them. CONDITION: none - the
`watershed` mesher they were declared against is gone, `om2d` owns its own
iteration, and how far a clicked outlet may move to reach the channel is a fact
about the D8 grid that `delineate_watershed` declares for itself
(`snap_threshold`, default 100 upslope cells). The bounds these two carried never
reached a mesher after the purge.

**DS-6 - the duplicate mesh resolution.** DELETED:
`steps/rain_on_grid.py::_adopt_case_mesh` (~50 LOC) and the `_ENGINE` constant
that only it read. CONDITION: none - the mesh router's `resolve_mesh` is the ONE
resolver for a mesh a case already holds (explicit, then discovered, then the
declared build), reached at the build door. The deleted copy went through
`precondition_gate.gate_supplied_mesh`, a second discovery gate whose documented
AUTO behavior adopts a discovered mesh silently, which D-9 forbids. Nothing
outside the deleted function called it.

**The four dead confirm-envelope builders.** MOVED to
`trid3nt-attic/gates/dead_confirm_builders.py`, deleted from
`gates/cards/solver_confirm.py` and from the `gates/cards/__init__.py` re-export
list: `_build_psha_confirm_envelope`, `_build_scenario_confirm_envelope`,
`_build_fire_confirm_envelope`, `_build_geoclaw_confirm_envelope` and their
`estimate_psha` / `estimate_scenario` / `estimate_fire` / `estimate_geoclaw`
providers. CONDITION: none - this is the residue the previous slice queued above,
now ruled as its own class. Their TOOLS left with the fresh-start purge, no
`gate_spec` names them, nothing in `tests/`, `plugin/` or `scripts/` references
them, and their byte-equivalence tests went with the tools. A rung-5
re-registration authors its envelope from the engine's own needs; the attic copy
is the shape it would restate, not code anything imports.

**The `watershed` and `corridor_tin` mesh asks.** `telemac_rain_on_grid`'s MESH
block no longer names the purged `watershed` mesher: it declares `om2d` over the
chained basin. The two reach templates still name `corridor_tin` - see the open
DESIGN-STOP in `docs/design/worker-unification-port.md`.

**`mesh/precondition_gate.py` (222 LOC) - its chop CONDITION is now met.**
`_adopt_case_mesh` was its last production consumer; only `tests/
test_mesh_meshers.py` still imports `gate_supplied_mesh`. The module itself goes
with elegance-review P2 (the second mesh front), which owns it - recorded here so
P2 does not have to re-derive that nothing calls it.

### AUTO EDGE DIES - the reach templates' sizing rung (2026-08-30)

DELETED: the `mesh_resolution` `Param` on both `telemac_river_dye` and
`telemac_do_sag`, and the `refine.mode` field the two templates' `MESH` blocks
mapped it into. This was the retired `corridor_tin` mesher's own sizing rung -
`"auto" | "fine" | "coarse"` picked a cells-across-channel count that
`suggest_mesh_size_m` turned into an edge length. `om2d` has no such rung, and
the AUTO EDGE DIES ruling (docs/IDEAS.md 2026-08-30) settled the DESIGN-STOP
this left open: the edge is now always an explicit sheet value on
`mesh_resolution_m` (`door=SCENARIO`, `default=14.0`, `user_lever=True`) - the
user states it or the model fills the labeled default, never a derived rung.
CONDITION: none - the edge is an explicit sheet value, so there is nothing left
for a sizing mode to pick between. Both templates now declare `mesher="om2d"`
over `extent=Ref("reach_polygon")`.

Also DELETED with it, in `trid3nt_server/workflows/telemac/steps/reach.py`:
`MESH_CELLS_ACROSS_BY_PRESET` (the preset -> cells-across table) and
`_DEFAULT_MESH_SIZE_M` (the legacy-parity constant it was anchored on), and the
preset branch inside `suggest_mesh_size_m` - the function's signature dropped
`resolution=` / `override_m=` for one required `edge_length_m=`, since there is
no longer a mode to fall back to when no override is asked. What remains is
pure bounding: the asked edge, raised by the node budget, lowered by the
>= 2-cells-across-the-channel rule, both moves narrated.

`trid3nt_server/workflows/telemac/workflow.py::_MESH_DECK_FIELDS` also drops
its `extent_km` / `width_m` / `banks` rows: the reach deck now reads
`reach_length_km` / `channel_width_m` / `bank_source` off the `PHYSICS` block
(the chain's `reach_polygon` measures the extent the mesher triangulates, so the
mesh ask itself no longer carries a width or a bank source to map). CONDITION:
none - this PHYSICS placement is itself a parity shim, DIE-DATED to the
worker-unification wave (P3/DS-3, docs/IDEAS.md 2026-08-30): once the worker
takes a `MeshArtifact` instead of re-deriving a ribbon from these fields,
`channel_width_m` and `bank_source` leave `PHYSICS` too.

`tests/test_mesh_declaration_travel.py` sections 2 and 3 (the `corridor_box`
fixture: a stood-in `corridor_tin` triangulator box, its declared-edit-in-the-
recipe assertion, and its restart-truncates-to-the-declared-edit assertion) are
DELETED with the mesher they exercised. CONDITION: none - `om2d` is a real
build with no box to stand in for. The semantics they pinned are not lost: "a
declared prefix survives a restart, a gate-time edit does not" is the mesh
session's own law, already pinned mesher-agnostically on `reg_grid` in
`tests/test_build_mesh_tool.py::test_restart_truncates_to_the_declared_prefix`
and `tests/test_mesh_gate_loop.py::test_gate_restart_truncates_to_the_declared_prefix`.

## THE SECOND MESH FRONT DIES (elegance review P2/P6, 2026-08-30)

`trid3nt_server/workflows/mesh/watershed.py` (685) DELETED. It carried its own
delineation, its own DEM/landcover/river resolvers, its own generate/adopt pair
and its own `CatchmentMesh` type beside the one mesh front - a second place a
mesh got built. The chain (`delineate_watershed` -> `combine` -> `om2d`) is what
replaces it. Its four surviving primitives are not the front and moved to
`workflows/mesh/shared/nodes.py`: `reproject_nodes_to_utm`,
`sample_raster_at_nodes`, `node_slopes_from_mesh`, `read_2dm_mesh`.
`utm_epsg_for` moved to `tools/processing/_geometry_common.py` - the one place a
UTM zone is computed, beside the geometry primitives that measure in metres.
CONDITION: none.

`trid3nt_server/workflows/mesh/precondition_gate.py` (222) DELETED - a second
discovery gate whose documented AUTO behaviour ADOPTED a discovered mesh
silently, which D-9 forbids. There is ONE resolver for a mesh a case already
holds and it is the mesh router's, at the build door. CONDITION: none.

`ReachMesh` + `build_corridor_mesh` (steps/reach.py) and `Catchment.mesh` +
`build_catchment_mesh` + `_stage_supplied_mesh` + `_refuse_declared_edits`
(steps/rain_on_grid.py) DELETED, replaced by ONE generic step,
`workflows/mesh/step.py::MeshStep.build` -> `build_declared_mesh`. A per-domain
wrapper around a mesh session is a second place a mesh gets built. The reach
plan's step is now labeled `mesh` rather than `corridor_mesh`, and the deck reads
`Ref("mesh")`. `steps/reach.py` also loses `MeshSizing`, `suggest_mesh_size_m`
and `_estimate_mesh_nodes` (P3, above): the granularity a run is judged on is the
edge the ACCEPTED mesh was MEASURED at. CONDITION: none.

`trid3nt_server/workflows/mesh/telemac_build.py` (88) DELETED - 88 lines of
hand-packed SELAFIN bytes beside `mesh/shared/selafin_cli.write_telemac_pair`,
which writes the geometry AND its `.cli` through telapy/pretel inside the image.
Square Two's standing gate rejects reimplemented library IO. `MeshSession`'s
`_selafin` becomes `_telemac_pair`, so every accepted mesh now carries a `.cli`
numbered from its own measured IPOBO. Honest cost: the host-side write gains a
container hop. CONDITION: none.

`tests/test_mesh_watershed.py` DELETED; the half of it whose subject survives
(the CN runoff-path selector, the per-node CN/Manning builders and the UTM
projection) is `tests/test_rain_on_grid_cn_and_nodes.py`. The precondition-gate
decision tests in `tests/test_mesh_meshers.py` and the catchment declared-edit
refusal in `tests/test_mesh_declaration_travel.py` go with their subjects.

`scripts/sandbox/replication/rog_ballcreek_finemesh.py`,
`scripts/sandbox/replication/rog_ballcreek_live.py` and
`scripts/sandbox/telemac/rog_coweeta_live.py` ATTICKED - all three drive
`generate_catchment_mesh` directly. CONDITION: the catchment replication case
rebuilds against the chain when the worker-unification port lands.

## ONE MEMBERSHIP VOCABULARY, ONE EXECUTING LADDER, ONE SPATIAL WORD (P4/P5/P7, 2026-08-30)

`ENGINE_MESH_REQUIREMENTS`, `mesh_compatible_with_engine`,
`MeshArtifact.engine_compat` and the `engine=` thread through `resolve_mesh` /
`supplied_mesh_artifact` DELETED. After the ACCEPTS ruling two vocabularies
answered "can this mesh be used here"; the table had shrunk to one `telemac` row
whose body was "the artifact carries `slf_uri` and a bed" - a READINESS PROPERTY
of the artifact, not a contract. It is now `MeshArtifact.unsolvable_reason()`
(~15 lines, reading the facts the record already carries), and WHICH KINDS a
pipeline accepts stays the template's `Accepts` row read at the supply door.
Refusal code `MESH_ENGINE_INCOMPATIBLE` -> `MESH_NOT_SOLVABLE`. `om2d` and
`MeshSession` no longer write `engine_compat`; the gate card reports
`unsolvable_reason` instead. CONDITION: none.

`.ladder()` EXECUTES. `Producer.ladder(*rungs)` now takes PRODUCERS and refuses
anything else at declaration (`PlanValidationError`, naming what a rung must be);
`interpreter._walk_ladder` calls the primary, then each rung in order, records
the ANSWERING rung on the ledger record's `runner`, and emits the LABELED
SUBSTITUTION line once, in the one place that knows a rung fired. DELETED with
it: `kwargs.setdefault("fallback", rungs)` and the `fallback=` kwarg convention
every shim had to honour (`resolve_rain_forcing` / `_rain_forcing`,
`resolve_rain_event`, `fetch_reach_flowline`), plus the `"ladder"` echo those
producers returned. The two DECLARED string ladders go too
(`.ladder("gridmet_domain_mean", "user_rate")`,
`.ladder("aorc_hourly", "design_storm")`): both named a BRANCH ON THE ASK inside
one producer rather than a fallback chain, and an inert declaration is worse than
none. CONDITION: none - see the DESIGN-STOP in this wave's report about which
producer declares the first real ladder.

`build_mesh`'s `"extent" not in declared and "domain" not in declared` branch
DELETED (P7). Nothing declares `domain` any more - its only declarers were the
corridor templates - so `extent` is the one spatial word, and it already takes a
bbox, a polygon layer uri or inline GeoJSON (`om2d._domain`). CONDITION: none.

`telemac_rain_on_grid`'s producer-less `Data("mesh")` supply slot DELETED with
`Catchment.mesh`, its only consumer. It also collided with the one mesh step's
own label: `_deref` resolves step results BEFORE Data, so `Ref("mesh")` would
have meant different things before and after the step ran. A supplied mesh
reaches a run through the mesh ROUTER at the build door - one resolver, which is
what D-9 asks for. CONDITION: the supplied-mesh path returns with the
worker-unification port, wired through that door rather than as a template slot.

`min_spacing` DELETED root-and-branch as a sizing word. The `om2d` refine block
declared the resolution twice - `min_spacing` (the finest edge, and the base a
polygon interior is meshed uniformly at) beside `edge_length` (the coarsest) -
and the reach templates bound the DECLARED `mesh_resolution_m` to the coarse one,
so a stated resolution reached the box only as a ceiling and every reach meshed
at the 40 m default nobody asked for. The knobs are now `resolution_m` (the
finest, threading to `min_edge_length_m`) and `max_el` (the coarsest, defaulting
to ten times the finest - the shipped 40/400 pair when neither is declared, and a
band that moves with a declared resolution instead of refusing against a number
the caller never wrote). Renamed with it: the artifact provenance keys
`min_spacing_m`/`edge_length_m` -> `resolution_m`/`max_el_m`, and the same two
`synthetic_inputs` rows. CONDITION: none - the full verbatim-keyed `edgefx` /
`clean` surface is the om2d wrapper-depth rung and lands there.

`Ref("centerline.bbox")` DELETED from both reach templates' banks row. The NLDI
navigate result carries no `bbox`, so the ref was empty from the day it was
written and the templates refused at binding. The banks query window is now its
own chained row - `compute_layer_bounds(layer_uri=centerline, pad_m=3000.0)` -
where the pad is a visible tool argument on the row that needs it. CONDITION:
none.

## The non-telemac workers leave, and the five staged payload deletions land (2026-08-30)

The worker-unification wave narrows `workers/` to the one engine it dispatches.
Seventeen directories moved to `/home/nate/Documents/trid3nt-attic/workers/`,
mirroring their repo-relative paths; git history is the archive and the attic is
a reading copy on no import path. Every one of them had already lost its server
half in the fresh-start purge - `trid3nt_server/workflows/<engine>/` went to the
attic on 2026-08-28 - so what stayed behind was a container half with no
dispatcher, no deck writer and no products reader.

- workers/elmfire/ (4 py, 2,091 lines): AWS Batch fire worker; its workflow package is already attic'd - moved to attic. CONDITION: none.
- workers/geoclaw/ (6 py, 4,796) + workers/_geoclaw_postprocess/ (3 py, 791): the Clawpack worker and its raster half - moved to attic. CONDITION: none.
- workers/hecras/ (8 py, 1,317) + workers/hecras2025/ (43 py, 8,314): the 6.x Fortran worker and the 2025 managed-engine worker - moved to attic. CONDITION: none.
- workers/landlab/ (5 py, 4,038) + workers/_landlab_postprocess/ (3 py, 422): the exec-mode landlab runner and its postprocess - moved to attic. CONDITION: none.
- workers/modflow/ (16 py, 11,296) + workers/_modflow_build/ (2 py, 181) + workers/_modflow_postprocess/ (2 py, 1,844): the mf6 deck builder, adapters and head/concentration readers - moved to attic. CONDITION: none.
- workers/openquake/ (5 py, 2,093) + workers/_openquake_postprocess/ (3 py, 467): the OQ runner and its curve reader - moved to attic. CONDITION: none.
- workers/sfincs/ (3 py, 836) + workers/_sfincs_build/ (6 py, 3,552): the SFINCS entrypoint and the quadtree/deck builders - moved to attic. CONDITION: none.
- workers/swan/ (5 py, 2,723) + workers/_swan_postprocess/ (3 py, 595): the SWAN worker and its spectral reader - moved to attic. CONDITION: none.
- workers/_raster_postprocess/ (11 py, 2,623): the worker-side mirror of the COG / outputs-manifest / publish-manifest contracts writers, shipped into the docker-worker images. Its only remaining consumers were the four worker trees above - moved to attic. CONDITION: none; the agent-side originals under `trid3nt_server/workflows/shared/` and `contracts/` are untouched and are what the kept tree reads.

321 tracked files, 127,845 lines - of which 48,748 are Python and the rest are
the shipped decks, fixtures and reference geometries those workers carried.
`workers/schism/` is the eighteenth directory and is HELD, not moved - see the
design-stop below.

Pointer scrubs that followed them out:

- `workflows/solver/code_provenance.py::ENGINE_PATHS` collapses from eleven engines to the one `telemac` row. Ten rows named a worker directory, a workflow package, or both, and every one of those paths is now in the attic; a staleness answer computed over paths that do not exist reports "your engine never moved" for an engine that is not there.
- `workflows/shared/outputs_manifest_io.py`'s module docstring named SFINCS/GeoClaw/SWAN as the docker-worker half and SWMM/Landlab as the host-exec half, and pointed at `workers/_raster_postprocess/outputs_manifest.py` for the mirror. The engine names and the mirror pointer go; the host-exec-vs-worker distinction the module exists to serve stays.
- `tests/test_fallback_sweep_guard.py::_PARKED_SILENT_SUBSTITUTIONS` loses rows 11b, 12a, 14, 19 and 20 - the five parked naked-substitution sites that lived in `_raster_postprocess/cog.py`, `_sfincs_build/deck.py`, `_sfincs_build/deck_quadtree.py`, `modflow/gwt_adapter.py` and `_landlab_postprocess/postprocess.py`. The register pins sites in THIS tree; a site that leaves the tree leaves the register, which is the same treatment the SWMM/SFINCS/ribbon rows already got. Row 11's agent copy in `workflows/shared/cog_io.py` is the whole register now, and `test_the_register_covers_every_parked_row_the_adr_names` pins that set at `{"11"}`.
- `tests/test_engine_room_posture.py` drops the `resolve_engine("sfincs-quadtree") == "sfincs"` assertion, which pinned a row of the table that just left.
- `workers/README.md` is rewritten to the surviving roster. Its two-dispatch-mechanism prose, its per-engine build lines and its cloud-lane Dockerfile section described directories that are no longer here.

NATE's five staged `workers/telemac/` payload deletions land with this commit -
they were held uncommitted since the lego wave under the standing "no telemac
worker image rebuild" rule, and Stage 0 is where they were ruled to land:

- workers/telemac/telemac_river_dye_build.py (2,595 lines): the reach pipeline's in-worker half - mesh build, bank acquisition, bed sampling and `.cas` authoring. DELETED. CONDITION MET: the wave moves `.cas` authoring server-side and the reach mesh comes from the mesh router as a staged artifact, so the module's every responsibility has a server home; the pieces of it already ledgered by earlier waves (the six in-container fetches, the bed COG, the SIGALRM watchdog, the constant-ribbon fallback) were its progressive dismantling.
- workers/telemac/telemac_coastal_build.py (728): the coastal-surge pipeline. DELETED. CONDITION MET: `coastal_tidal_surge` goes DARK under the fork ruling, unregistered with an awaiting-port note, until rung-4 rebuilds it; the attic is never a restoration source.
- workers/telemac/tomawac_build.py (602): the TOMAWAC wave pipeline. DELETED. CONDITION MET: `wave_field` goes DARK under the same ruling. Its entrypoint smoke was the module the image imports at build time, which is why the tree is currently unbuildable - Stage 4 rewrites the smoke blocks.
- workers/telemac/rog_build.py (855): the rain-on-grid pipeline. DELETED. CONDITION MET: `telemac_rain_on_grid` is parked and re-registers in Stage 3 on the unified dispatch, with the catchment mesh arriving as a MeshArtifact rather than being built in the container.
- workers/telemac/rainfall_forcing_compare.py (203): a non-registered reference driver that solved one reach twice through `run_solver` - with and without a distributed rain source term - to prove the `rain_or_evap_mm_per_day` knob moved the water surface through the rebuilt image. DELETED, no relocation. This is the missing line the re-baseline noted: the file is a SERVER-SIDE driver misfiled under `workers/`, it imports boto3 and drives the solver seam from outside the container, and it drove a rain deck that `rog_build.py` no longer writes. CONDITION: none - the deletion stands on its own; a rain-forcing A/B belongs in the driver lane against the re-registered template, not beside the worker it dispatches.

DESIGN-STOP, held not resolved: `workers/schism/` (6 py, 769 lines, 40 files) is
the eighteenth directory and did NOT move. `workflows/mesh/meshers/om2d.py::
_sandbox_formats` puts `workers/schism` on `sys.path` so that
`scripts/sandbox/oceanmesh/mesh_formats.py` can import `extract_boundary_loops`,
`remove_boundary_pinch_points` and `signed_area_ccw` from `schism_gr3` - and
`_clean_once`, the one topology pass every om2d build runs before any writer sees
the mesh, calls it. Moving the directory breaks the live mesher and the mesh
slice of the suite. Where those three pure-numpy helpers should live is a
structural choice, so the directory is held whole and `workers/README.md` says
why.

## The byte-equivalence bar's producer left with the SFINCS worker (2026-08-30)

`tests/test_outputs_seam.py::test_byte_equivalence_seam_vs_register` plus its four
helpers (`_write_synthetic_map`, `_resolved_style`, `_stashed_legend`, `_row`) and
the module docstring's second concern - DELETED. The test built a synthetic
`sfincs_map.nc`, ran `workers/_raster_postprocess/postprocess.run_postprocess`
over it, and compared the layer-event stream the OLD `register_manifest_layers`
path emitted against the NEW `outputs.json` seam's, field for field. Its producer
is now in the attic, so the file raised `ModuleNotFoundError: No module named
'workers._raster_postprocess'` at the import inside the test body - the moved
subject taking its test with it, the treatment the fresh-start purge applied to
131 other files. The five seam-unit tests in the same file are untouched and
green. CONDITION: none.

CONSEQUENCE, reported not fixed: both sides of that bar - `outputs_seam.
build_layers_from_outputs` and `register_published_manifest.
register_manifest_layers` - are LIVE server code, and this was the only test that
held them byte-identical. What raster a rebuilt bar should compare them on, now
that no engine in the tree writes a `publish_manifest.json` from a solved raster,
is a design question.

## workers/schism follows the seventeen, and the gr3 helpers move beside their caller (2026-08-30)

The Stage-0 stop is ruled and resolved. `schism_gr3.py` (335 lines) was never
SCHISM-only code: `tin_to_hgrid` writes the gr3 nobody dispatches, but
`extract_boundary_loops`, `remove_boundary_pinch_points` and `signed_area_ccw`
are the pure-numpy boundary helpers `om2d.py::_clean_once` runs on every mesh
build, reached through `scripts/sandbox/oceanmesh/mesh_formats.py`. The file
RELOCATES to `scripts/sandbox/oceanmesh/`, beside the four sandbox modules that
already import it flat, and the directory it came from goes to the attic.

- `workers/schism/schism_gr3.py` -> `scripts/sandbox/oceanmesh/schism_gr3.py` (git mv, contents unchanged apart from the docstring sentence naming its old home). CONDITION: none - the module's live consumers all sit in the directory it moved into.
- `workers/schism/` (entrypoint.py 169, `__init__.py` 1, test_entrypoint_manifest.py 46, test_schism_gr3.py 138, Dockerfile 254, README.md 40, the QuarterAnnulus + Duck fixture trees; 35 files after the relocation) moved to `/home/nate/Documents/trid3nt-attic/workers/schism/`, mirrored. CONDITION: none - the SCHISM spike has no server half, no registered tool and no dispatcher; the one piece of it the live tree reads has moved out ahead of it. Noted, not fixed: the attic copy of `test_schism_gr3.py` no longer sits beside the module it imports, so the relocated helpers carry no test - and they never carried one the suite ran, since `workers/` is collected by no slice.

Pointer scrubs that went with it:

- `workflows/mesh/meshers/om2d.py::_sandbox_formats` loses the two-path loop. It inserted `workers/schism` on `sys.path` purely so a sandbox module could import a worker module; one insert of `scripts/sandbox/oceanmesh` is now the whole hack.
- `scripts/sandbox/oceanmesh/{build_coastal_mesh,build_watershed_mesh,build_coastal_water_edge_mesh}.py` drop four `sys.path.insert(0, REPO / "workers/schism")` calls and the `workers/schism` segment of the `PYTHONPATH` line in their run instructions.
- `scripts/sandbox/oceanmesh/mesh_formats.py` loses the comment telling a reader the driver puts `workers/schism` on the path, and its docstring names the module rather than the old path.
- `workers/README.md` drops the HELD roster entry.

Dated records that name the old path are left as written: `docs/validation/worker-loc-ledger.md`'s row-0 table measures a git ref, and the ADRs, metrics rows and conformance walks are records of what was true when they were taken.

## The worker's deck tests follow their subject, and five solver seams become one (2026-08-30)

Stage 1 of the worker-unification wave. Everything below is a deletion the
server-side authoring substrate makes possible; nothing here changes what runs.

- `workers/telemac/tests/` loses ten test modules (1,472 lines) plus
  `fixtures_longview_water.json` (375 KB), moved to
  `/home/nate/Documents/trid3nt-attic/workers/telemac/tests/`. Every one of them
  imported a module Stage 0 deleted: `telemac_river_dye_build`
  (test_constitutive_physics 166, test_gaia_erodible 155, test_nestor_dredging
  125, test_rain_forcing 89, test_waqtel_decay 204, test_waqtel_o2 119,
  test_water_polygon_domain 83), `telemac_coastal_build` (test_coastal_build
  78), `rog_build` (test_rog_build 327), and the purged
  `model_river_dye_release_scenario` server module (test_classify_substance
  126). CONDITION MET for the seven deck tests: their assertions are repointed
  at the authored text in `tests/test_telemac_author_decks.py` (37 tests, in the
  offline suite, which `workers/` never was - no slice collects it). CONDITION
  for the other three: their subjects are the deleted in-worker MESH builders
  and the coastal pipeline, which the mesh router and the DARK fork ruling own
  now; no assertion of theirs describes code in this tree. `fixtures_longview_
  water.json` was the water-polygon test's only reader - it is also the 375 KB
  fixture Stage 4's `.dockerignore` was going to have to exclude, and it now
  leaves the tree instead. `test_artemis_real_structure.py` and
  `test_telemac3d_vertical_grid.py` STAY: their builders stay live in-worker
  behind the unified dispatch under the fork ruling.
- `workflows/telemac/run_telemac.py` 569 -> 187 lines. Five near-identical
  `_classify_*_exit` functions, five `*_local_spec` factories, five `register_*`
  functions and five `_*_COMPLETION_METRIC_KEYS` tuples collapse to one
  `_classify(label)` closure, one `make_spec(solver, prefix)` factory, one
  registration loop over a `{solver: stream prefix}` table and one
  `_COMPLETION_METRIC_KEYS` set. CONDITION MET: the per-leg key tuples were only
  ever applied as `if k in metrics`, so a union filters identically for every
  leg - a leg does not write another leg's keys. The five solver NAMES are kept
  (run-listing identity). `tests/test_run_telemac_chain.py` reads the new surface
  and gains a per-leg registration pin.
- `open_water.py::stage_open_water_manifest` -> `stage_telemac_manifest`, and
  the two hand-rolled manifest writers beside it die: `deck.py::stage_manifest`
  loses its bucket check, its document assembly and its `put_object` (-20 lines,
  and with them the module's `json` and `os` imports), and
  `rain_on_grid.py::_stage_inputs` loses the same (-6). CONDITION MET: both now
  decide only their own outputs list and delegate; the reach path's typed
  `TELEMAC_DYE_STAGING_FAILED` / `TELEMAC_ROG_STAGING_FAILED` codes are preserved
  by translating the one writer's error at the seam.
- `selafin_cli_driver.py`'s `_OPEN`/`_LAND` code pair and the `open_nodes`
  parameter die with them: one `_ROLE_CODES` table keyed by boundary role, and
  `roles={...}` on `write_telemac_pair`. CONDITION MET: om2d's open-boundary
  designation passes `{"open": nodes}` and writes the identical `(5,4,4,4)` quad;
  `tests/test_mesh_om2d.py` reads the roles the writer was handed.

Duplication this stage KNOWINGLY leaves, resolved in Stage 2:
`oil_templates/oil_flot_template.f` now exists both under
`workers/telemac/oil_templates/` (read by the live worker's oil branch) and
under `trid3nt_server/workflows/telemac/steps/oil_templates/` (read by the
server author). Stage 2 deletes the worker branch and its copy.

## The worker becomes one dispatch (worker-unification stage 2, 2026-08-30)

The five near-identical pipelines behind one entrypoint collapse to one strict
gate, one dispatch table and one metrics envelope. `workers/telemac/
entrypoint.py` 1594 -> 346 lines.

- The four per-family run branches (`run_pipeline`, `run_rog_pipeline`,
  `run_tomawac_pipeline`, `run_coastal_pipeline`) and their four strict-config
  gates + error classes (`Telemac*`/`Tomawac*`/`Coastal*ManifestUnknownFieldsError`,
  `_reach_config`, `_tomawac_config`, `_coastal_config`, and the four per-parser
  version stamps) die. CONDITION MET: their payload modules
  (`telemac_river_dye_build`, `rog_build`, `tomawac_build`,
  `telemac_coastal_build`) were already deleted, so the module could not import
  and no branch of it could run; `wave_field` and `coastal_tidal_surge` are DARK
  under the fork ruling, and the reach/rog decks are authored server-side by
  `steps/author.py`. What replaces all four is `case` -> the telapy child
  runner: `_MODULES` names the four telapy API classes, and the child runs
  `set_case -> init_state_default -> run_all_time_steps -> finalize`.
- The `agitation` and `stratified` gates collapse into the same
  `_strict_section` + one `UnknownManifestFieldError`, stamped
  `telemac-unified-1`. CONDITION MET: both gates were the same loop over
  `dataclasses.fields` with a different message; `artemis_build.solve` and
  `telemac3d_build.solve` stay live behind the dispatch, unextended.
- The `mesh_only` branch dies (zero production callers - only tests pass
  `mesh_only=True`). CONDITION: `deck.py::stage_manifest` still accepts the
  flag; the server flip removes it.
- `DEFAULT_OUTPUTS` (22 filenames) dies. CONDITION MET: its documentation value
  moves to the server outputs list that actually drives the upload -
  `deck.py::stage_manifest` now declares the oil (`drogues.txt` /
  `particles.json` / `slick.geojson` / `oil_spill.txt`), WAQTEL
  (`t2d_river.waqtel`) and NESTOR (`nestor.act` / `.pol` / `.ref`) files per
  substance class beside the GAIA pair it already declared.
- Two divergent success conventions (a `correct_end` the builder decided, and a
  zero exit code the launcher trusted) become one: a clean child exit AND every
  `case.results` file present. A solver that returns zero without writing its
  result has not solved anything.
- `_parse_gaia_mass_balance` (49 lines) dies with the reach branch. CONDITION:
  the listing it parsed is uploaded as `full_listing.log`, and the reader lands
  server-side in `ops.read` with the other ported readers.
- `_staged_mesh.py` (119) and `_staged_reach.py` (77) delete. CONDITION MET:
  their only importer was the deleted reach branch; the mesh and the reach
  geometry now arrive as the authored `mesh.slf`/`mesh.cli` pair.
  `_staged_bed.py` and `_supplied_mesh.py` STAY - the two live builders read
  them (real Great Lakes bathymetry, and the ARTEMIS BYO geometry).
- `_staged_bed.py` (53) is QUEUED, not kept. CONDITION to chop: its only
  importers are `artemis_build.py` and `telemac3d_build.py`, which are live and
  never extended behind the unified dispatch; the file dies on the same date they
  do, when rung 4 rebuilds agitation and stratified as server-authored `case`
  sections and the bed reaches the worker inside `mesh.slf`. reopen: never.
- The four rain-on-grid helpers the deleted `run_rog_pipeline` owned -
  `_mesh_bbox4326`, `_guess_utm_epsg`, `_read_node_field` (per-node CN2 /
  Manning fields) and `_write_max_fields_slf` (`rog_max_fields.slf`) - die with
  it. CONDITION: `steps/rain_on_grid.py::_OUTPUTS` still declares
  `rog_max_fields.slf`, which nothing writes now; the rain-on-grid port either
  re-declares that product from a server-side reader or drops the name from the
  outputs list.
- `workers/telemac/oil_templates/` (3 files, 220 lines) deletes, resolving the
  duplication the previous stage knowingly left. CONDITION MET:
  `steps/author.py::write_oil_inputs` writes both the per-run `oil_flot.f` from
  its own copy of the template and `oil_spill.txt` from `OIL_PRESETS`, so the
  worker's `oil_flot_template.f`, `oil_spill_light_crude.txt` and
  `cas_oil_keywords.txt` have no reader.
- `workers/telemac/test_entrypoint.py` rewritten 140 -> 189 lines: the
  ReachConfig-mapping tests die with ReachConfig; what is tested now is the one
  gate, the dispatch, the refusals, the echo copied verbatim, the
  clean-exit-no-result verdict, and crash isolation through a REAL child.
- `tests/test_artemis_real_structure.py::test_parser_version_bumped_for_real_structure`
  deletes. CONDITION MET: its subject was the per-builder stamp
  `_ARTEMIS_PARSER_VERSION`, and there is one stamp now
  (`telemac-unified-1`), asserted in `test_entrypoint.py`. The two gate tests
  beside it repoint at `_strict_section` + `UnknownManifestFieldError`.

## The server chain owns the reach refusals (worker-unification stage 3, 2026-08-31)

The container meshes nothing and fetches nothing, so no refusal about the
reach's GEOMETRY can arise inside it any more. Everything that carried a
worker-side gate to the server dies with the gate.

- `steps/solve.py::raise_if_banks_unavailable` and `raise_if_reach_degenerate`
  (35) DELETE. CONDITION MET: the banks fetch, the measured coverage check and
  the section cut all run server-side before a manifest is staged, so
  `TELEMAC_BANKS_UNAVAILABLE` / `TELEMAC_REACH_DEGENERATE` are codes no worker
  writes. `ReachBanksUnmapped` STAYS - it is raised by `reach.py`'s own
  measured-coverage check, which is where the real cause is. reopen: never.
- `steps/errors.py::TelemacReachDegenerateError` (41) DELETES with them, and
  `channel_width_m` leaves the tree root-and-branch with it (P3): the error's
  message and both its suggestions were written around a width the elegance
  review deleted. reopen: never - a degenerate domain now refuses at the section
  cut, on the geometry that was measured.
- `steps/solve.py::download_result_selafin`'s second metrics read (26 -> 0)
  DELETES; the function returns the path alone. CONDITION MET: `utm_epsg` is the
  SERVER's own measurement, echoed through `case.echo` into the worker's metrics
  and already on the solve result, so reading it again out of the same file was
  a second answer that could disagree with the first.
- `steps/products.py::_s3_object_exists` (12) and
  `tests/test_live_drive_fixes_0104.py::test_s3_object_exists_guard` DELETE, and
  the file goes with its last test. CONDITION MET: its subject was the worker's
  fail-open drogues parse leaving `slick.geojson` unwritten while its URI was
  registered anyway. The server now BUILDS the slick from the uploaded track and
  uploads the bytes before it emits the handle, so there is no window in which a
  handle can outrun its object. reopen: never.
- `steps/author.py::TELEMAC_DREDGE_ZONE_UNMEASURED` RETIRES to the NESTOR
  auto-fill (IDEAS 2026-08-31 ruling 2): the dig field is the cross-channel box
  at the dig station intersected with the reach polygon offset inward by the
  declared `dredge_bank_offset_m`, so a field is measured rather than refused
  for want of a width. What survives it is
  `TELEMAC_DREDGE_ZONE_TOO_NARROW` (the shrunken polygon vanished - the setback
  and the measured width are named), `TELEMAC_DREDGE_ZONE_OUTSIDE_WATER` (a
  supplied polygon on dry land) and `TELEMAC_DREDGE_ZONE_UNMAPPED` (no reach
  polygon reached the author). `zone_width_m` / `dredge_zone_width_m` leave both
  signatures.
- `steps/author.py::author_rog_deck(user_fortran_dir=)` DELETES. CONDITION MET:
  the RAINDEF=3 patch bakes at IMAGE BUILD (IDEAS 2026-08-31 ruling 1), so the
  deck names one path - `RAINDEF3_USER_FORTRAN` - and a per-run directory name
  was a knob with one legal value.
- `tomawac_wave_field` and `coastal_tidal_surge` leave the REGISTRY (declared
  parked, not deleted - ADR 0322). CONDITION to unpark: rung 4 rebuilds each as
  a server-authored `case`; their in-worker builders were deleted in stage 0 and
  the attic is never a restoration source. Their own test modules read the
  declaration on the module instead of a registry row; the roster pins move to
  `PARKED_TEMPLATES`, `_REGISTRY_SIZE` drops 170 -> 168, and the six roster and
  retrieval fixtures that used one of them as their EXAMPLE template repoint at
  `telemac_river_dye`.
- `steps/deck.py::stage_manifest(mesh_only=)` and its preview outputs list (16)
  DELETE, with the two tests whose subject they were. CONDITION MET: stage 2
  deleted the worker's `mesh_only` branch with a ledger line stating it had zero
  server callers, and this was the other half of that pair - staging a manifest
  with `mesh_only: True` now writes a document naming no runnable section. The
  mesh PREVIEW is `MeshStep.build`'s gate, which never dispatches a solver.
- `steps/reach.py::resolve_reach_bed` (~38), `BED_DEST`, `_BED_PAD_DEG`,
  `_GLO30_PX_PER_DEG`, the `with_bed` parameter and the `bed_uri` /
  `bed_source` / `bed_fallback_reason` provenance rows DELETE, with the four
  tests whose subject they were
  (`test_the_bed_is_asked_for_the_sources_own_lattice`,
  `test_the_bed_window_covers_the_whole_centerline_with_room_for_the_corridor`,
  `test_a_copernicus_outage_falls_to_3dep_and_NAMES_the_reason`,
  `test_the_mesh_preview_stages_geometry_and_skips_the_bed`). CONDITION MET
  (IDEAS 2026-08-31 FLIP ruling 2): the reach DATA body declares ONE bed row and
  the `MESH` ask consumes it, so the bed the run solves on is the one the
  geometry file carries. A second raster fetched and staged here was a bed no
  run read. `bed_source` on the deck and in the echo is now the MESHER's own
  `dem_source`. reopen: never - two beds is the defect.
- `steps/deck.py::_accepted_mesh_inputs` + `_ACCEPTED_MESH_DESTS` (23) DELETE,
  and the `river_mesh.npz` dest with them. CONDITION MET: the accepted mesh
  travels as the bed-carrying `river.slf` / `river.cli` pair the authored deck's
  own GEOMETRY and BOUNDARY CONDITIONS lines name, plus the topology bundle read
  server-side at authoring time. Nothing reconstructs a mesh from arrays any
  more, so the npz had no reader on either side.
- `steps/open_water.py::stage_telemac_manifest(case=)` DELETES. CONDITION MET:
  an authored run's section IS `case` - `stage_manifest` passes it as the
  section the worker dispatches on - so a second way to write the same key was a
  document that could carry two.
- `plugin/tests/headless_mesh_gate_drive.py`'s `E2E_MIN_MEAN_WIDTH_M` knob and
  its `bank_source` / `bank_width_mean_m` metrics assertions DELETE. CONDITION
  MET (IDEAS 2026-08-31 FLIP ruling 4): both keys died with the PHYSICS parity
  shim and no run writes either. The witness they stood for is now the
  JOURNAL's measured banks-coverage line on the published layer, plus
  `correct_end` and the echoed `npoin` / `nelem` - facts the server measured and
  the worker copied back, which is what makes "it solved on the accepted mesh"
  checkable.
- `run_telemac.py::_COMPLETION_METRIC_KEYS`: `bbox4326` and `bank_width_mean_m`
  DELETE; `module`, `family`, `bed_source` and the ONE `bbox` spelling land.
  CONDITION MET: `bbox4326` was the reach worker's own name for the extent the
  open-water legs already called `bbox`, and two names for one fact is a reader
  choosing between two answers.
- `run_telemac.py::_COMPLETION_METRIC_KEYS`: `dye_var`, `dye_cmax_final`,
  `dye_cmax_overall`, `dye_active_frames`, `dye_front_x_final_m`,
  `centerline_length_m`, `seed_comid`, `geometry_slf` and `lb_order` DELETE, and
  the dead-key arguments in `test_classify_exit_ok_folds_metrics` with them.
  CONDITION MET: every one was written by the RETIRED reach worker; zero writers
  remain (the unified worker writes `correct_end`, `module`, `family`, the echo
  and the error fields, and nothing else), so the filter was carrying names no
  run can produce. Rung 4 authors its own completion contract fresh.
- `steps/deck.py::_centerline_utm` and `meshers/om2d.py::_fit_bed`'s private
  `_split_geometry` vertex read DELETE, replaced by one
  `mesh/shared/nodes.py::read_centerline_utm`. CONDITION MET: the two readings
  of the same navigated flowline disagreed on both continuity (one merged and
  took the longest piece, the other concatenated every vertex of every row) and
  ORDER, and the order decides which way the fitted bed slopes. The one reading
  joins the parts into a single continuous line - refusing a network that stays
  in pieces - and orients it head-to-tail from the CHAIN's own fact, the navigate
  seed, declared on the mesh ask as `bed={"downstream_from": ...}`.
- `steps/deck.py`'s inline `case` dict DELETES; `write_reach_deck` calls
  `open_water.case_section`. CONDITION MET: `case_section` is the declared writer
  of that section and was called by nobody, so the reach's manifest and the
  documented contract were two spellings that could drift - as they had, over
  `user_fortran`, which the reach never populated.
- `steps/rain_on_grid.py`: `_MODE` / `_HYDROGRAPH` / `_read_hydrograph`, the
  `_OUTPUTS` rows `rog_geometry.slf` / `rog_max_fields.slf` /
  `rog_outlet_hydrograph.json`, the `watershed.slf` + `node_cn2.txt` +
  `node_manning.txt` staging, and the `section="reach"` manifest write all
  DELETE. CONDITION MET: the worker's `rain_on_grid` branch is gone, no branch
  dispatches on `reach`, and nothing writes any of those three artifacts. The
  front now authors a `case` - `t2d_rog.cas` plus the curve-number scatter, the
  friction pair and (on the time-varying path) the block hyetograph - and the
  outlet hydrograph is MEASURED server-side by
  `steps/run_reads.py::outlet_hydrograph` off the run's own result SELAFIN.
- `rain_on_grid/declarations.py::outlet_node_count` (Param) DELETES. CONDITION
  MET (IDEAS 2026-08-31 FLIP ruling 3): the outlet is DECLARED on the mesh ask at
  the delineation's snapped pour point and the mesher matches the boundary nodes
  within its own mean boundary edge of it, so which nodes carry the boundary is
  the declaration's answer and a k-nearest count was a second one.
- CHOP CANDIDATE (not chopped): `steps/rain_on_grid.py::_soil_store_spin_up` and
  `test_soil_store_spin_up_fills_from_antecedent`. The Michel-2005 continuous
  soil-moisture store was the RETIRED in-worker runoff model; the authored deck
  drives the engine's own static SCS-CN, so `soil_store=True` now refuses typed
  (`TELEMAC_ROG_SOIL_STORE_UNAUTHORED`) rather than reading as applied, and the
  spin-up has no caller. CONDITION: NATE rules whether the store is re-homed
  server-side (V0 folded into the CN field is a PHYSICS choice) or the four
  `soil_*` params go with it.
- `meshers/om2d.py::MESH_BED_NO_CHANNEL` DELETES with `_fit_bed`'s private read.
  CONDITION MET: the ONE centerline reading refuses on its own codes -
  `MESH_CENTERLINE_NO_LINE` (the source maps no polyline) and
  `MESH_CENTERLINE_NOT_CONTINUOUS` (the parts stay a network) - and the second
  of those is a refusal the vertex-heap read could not make at all.
- CHOP CANDIDATE (not chopped): `TelemacRainOnGridLayerURI.max_velocity_peak_ms`
  and its ANSWER row. The row LEAVES the template's `ANSWER` now, because the
  retired worker was its only writer and an answer field nothing fills is a
  reader checking a `None`. CONDITION to restore: a server-side field maximum
  over the result SELAFIN's velocity - the arrays the outlet-hydrograph read
  already loads, if NATE wants that reader to answer for more than the outlet.

## The image drops what the worker stopped importing (worker-unification stage 4, 2026-08-31)

- `workers/telemac/Dockerfile`: the `gmsh==4.15.2` pip pin and the seven-package
  apt block that existed only to satisfy the gmsh wheel's `dlopen` at import
  (`libglu1-mesa`, `libgl1`, `libxrender1`, `libxcursor1`, `libxinerama1`,
  `libxft2`, `libgomp1`) DELETE. CONDITION MET: nothing under `workers/telemac/`
  imports gmsh - the mesh is built server-side and arrives staged - and the conda
  base env ships its own `libgomp.so.1`, so the engine keeps its OpenMP runtime.
- `workers/telemac/Dockerfile`: the `pystac-client==0.9.0` and
  `planetary-computer==1.0.0` pins DELETE. CONDITION MET: the DEM fetch left the
  container with the fetch migration; the worker reads a staged bed and does no
  network at all.
- `workers/telemac/Dockerfile`: the TOMAWAC build-time smoke block DELETES.
  CONDITION MET: `tomawac_build` was one of the five pre-deleted payloads, so the
  block imported a module that is not in the tree - it was the image's
  unbuildability. TOMAWAC survives as one of the four telapy classes a `case` may
  name, which the replacement smoke asserts.
- `workers/telemac/Dockerfile`: the four per-process strict-field smoke blocks
  (`_reach_config`, `_tomawac_config`, `_artemis_config`, `_telemac3d_config`)
  DELETE with the parsers they exercised. CONDITION MET: the worker has ONE gate
  (`_strict_section`) raising ONE error, and the replacement pair asserts it
  positively (both surviving builder configs map) and negatively (an unknown
  `case` field refuses).
- `workers/telemac/Dockerfile`: the telemac3d binary/dico/sources presence
  assertion DELETES. CONDITION MET: the RAINDEF bake now reads
  `$HOMETEL/sources/telemac2d/runoff_scs_cn.f` at build time and stops the build
  if it is not there, so the installed source tree is proven by a step that needs
  it rather than by an assertion beside it.
- `workers/telemac/.dockerignore` gains `tests/`: the two builder tests under
  `workers/telemac/tests/` were riding into the image, where nothing runs them.

## The image drops the geometry dep nothing reads (worker-unification stage 5, 2026-08-31)

- `workers/telemac/Dockerfile`: the `shapely==2.1.2` pip pin DELETES with its
  entry in the geo-deps import smoke. CONDITION MET: nothing under
  `workers/telemac/` imports shapely, and neither does the installed engine -
  `grep -rl "import shapely" $HOMETEL/scripts $HOMETEL/sources` and the same grep
  over the image's site-packages (excluding shapely itself) both return nothing.
  Verified through the rebuilt image: `importlib.util.find_spec("shapely") is
  None`, every build smoke green, and the seven live proofs of this stage run on
  that image.
- `om2d_driver.py`: the swallowed `clean passes stopped` note DELETES. CONDITION
  MET: it let a run continue over whatever half-cleaned points/cells the throwing
  pass had left, and read afterwards as a mesh that was merely cleaned less. The
  chain now re-types connectivity after every pass (the passes hand back floats
  and the next one indexes with them), names the pass that removed the last
  element, and refuses typed - through the existing empty-mesh refusal when a
  pass emptied the mesh, through a new one otherwise. Covered offline in
  `tests/test_mesh_om2d.py`; re-smoked on the preserved failing rundir
  `mesh-01M1AFSK2RNXAXR3TBF0DQQSXH`, which now refuses by naming the 100 m edge
  length its domain cannot hold.

## The boundary run is constructed, and WAQTEL forks to the launcher (worker-unification proof stops, 2026-08-31)

- `topology.py::match_boundary_roles`'s nearest-node scatter and its per-node
  distance threshold DELETE. CONDITION MET: a TELEMAC liquid boundary IS a
  contiguous run of one contour, and the per-node rule produced holes - the
  do_sag mesh measured `.III...OO.OO...IIII` along its own contour, which
  `set_numliq` counts as five liquid boundaries where two were declared. The
  matcher now takes the CONTOURS in walk order and constructs the run between the
  nodes nearest a face's two ends (a point-declared role takes the run standing
  within the tolerance of it), so the holes are inside the stretch by
  construction. Proven offline against the measured scatter and against the
  isolating probe's hand-closed result, and through the image: the probe's
  rewritten `.cli` numbered exactly two boundaries.
- `section.py::_end_face`'s probe-line intersection DELETES with its `reach`
  argument and its `[]` return. CONDITION MET: the end cut is exactly collinear
  with the probe line, and over a domain-sized probe (132 km on the Eel) the
  collinear intersection came back whole at one end and EMPTY at the other - the
  river_dye 1.0 km reach reached the mesher with `face_end == []` and refused
  there as "[] is not a face". The face is now projected off the section's own
  boundary vertices, and an end the cut never reached refuses as
  `SECTION_END_FACE_UNMEASURED`, naming how many vertices stand on the cut plane
  and how far the nearest one is off it.
- `workers/telemac/entrypoint.py`: WAQTEL- and GAIA-coupled cases run the
  module's own CLI launcher instead of the telapy arm. A SCOPED, LEDGERED
  DEVIATION, not a deletion. CONDITION TO DIE: telapy's API arm drives them. It
  does not today, and both failures were measured in the image on the run
  directories the server authored:
  * WAQTEL (do_sag) - `set_case` + `run_all_time_steps` stops at iteration 0 with
    `OS (BIEF): OBJECT TYPE NOT IMPLEMENTED: 0`, after a `Cannot open file
    'WAQDICO'` fallback crash on the first attempt.
  * GAIA (sediment) - the time loop runs to the END and the FINALIZE fails,
    `ERROR 1003 DURING CALL OF BIEF_CLOSE_FILE:CLOSE_BND /
    HERMES_FILE_NOT_OPENED_ERR`, so the results never land. GAIA was plumbed on
    telapy first and FELL to the same deviation, which the proof ruling required
    be said loudly.
  `telemac2d.py <cas>` on both of those same run directories reaches CORRECT END
  and writes every declared result (rc=0), and returns rc=1 on a bad deck, so the
  success convention is unchanged. The deviation sits BEHIND the one runner seam
  (`_run_child`), keeps the same manifest, the same tee, the same timeout and the
  same convention (clean exit AND every declared result on disk), and is chosen on
  one manifest word (`case.coupling`). Pure telemac2d, oil/user-Fortran, artemis
  and telemac3d cases all stay on telapy - proven live in the same wave.
- `steps/deck.py`: `_class_files`'s unreachable `do_sag` branch DELETES as dead -
  it is now REACHED. CONDITION MET: the class was read off `classify_substance`,
  which answers `tracer` for a DO-sag run (its substance IS dye); the `do_sag`
  class is threaded onto the DECK, which is where the author reads it from. One
  reading (`deck["substance_class"]`) now serves both the file lists and the
  coupling word, so the manifest could not say `tracer` about a deck the author
  wrote a WAQTEL coupling into.
- `steps/author.py`: the BARE absolute path in the rain-on-grid `FORTRAN FILE`
  line DELETES; the value is quoted. CONDITION MET: `/` opens a comment in a
  steering file, so damocles read the keyword as empty AND swallowed the line
  after it - measured in the image, `TelemacCas` returned `['EQUATIONS',
  'SAINT-VENANT FE']` as the FORTRAN FILE value and `None` for EQUATIONS, and the
  hyetograph run died as "missing file for FORTRAN FILE: EQUATIONS". Quoted, the
  same probe returns the path and EQUATIONS survives.
- `delineate_watershed`: the grid-CRS leak DELETES. CONDITION MET: the D8 trace
  runs in the DEM's own grid and a supplied `dem_uri` is under no obligation to
  be lon/lat - 3DEP arrives in EPSG:5070 - so the tool was returning Albers
  metres on a layer that declares EPSG:4326. Downstream that is not a wrong
  answer but a lattice millions of cells wide: the rain_on_grid catchment mesh
  died on a 104 GiB allocation inside the triangulator. The pour point now goes
  into the grid's CRS and the catchment comes back out of it, and `om2d` refuses
  a non-lon/lat extent as `MESH_DOMAIN_NOT_LONLAT` rather than sizing a lattice
  from it.
- `tests/reach_chain.py::BANKS_HALF`'s use as the partly-mapped-reach PROCEEDS
  fixture DELETES; `BANKS_GAPPED` replaces it. CONDITION MET: `BANKS_HALF` maps
  the west half only, so the reach's downstream END stands on the polygon's own
  bank and the cut leaves no transect there - the run never proceeded live, it
  reached the mesher with an empty face. The ruled behaviour (above zero
  coverage PROCEEDS with the measured fraction journalled) is now proven on banks
  that map both ends with a gap between them, and `BANKS_HALF` proves the refusal
  at the cut.
- `steps/reach.py::resolve_reach_river` and its whole seed ladder DELETE -
  `resolve_reach_seed_point`, `_named_seed`, `_mainstem_seed`, `_nearest_vertex`,
  `_feature_vertices`, `_lonlat_extent`, `_stage_geojson`, `CENTERLINE_DEST`,
  `BANKS_DEST`, `named_watercourse` and the `river_name` field, with
  `tests/test_telemac_reach_river.py`. CONDITION MET: it was a SECOND acquisition
  of the reach beside the declared `DATA.centerline` row - a different seed, four
  COMIDs and 3472 m against the declared 1290 m - so the line the section was cut
  between and the mesh was built over was not the line the deck read. A release
  derived at `spill_fraction` along the second one landed 350 m outside the
  meshed domain and the solver refused it as SOURCE POINT OUTSIDE DOMAIN. Its
  banks fetch duplicated `DATA.banks`, and nothing consumed the two GeoJSONs it
  staged (they were absent from the deck's own manifest inputs). The seed the
  ladder produced never reached the meshed reach: `DATA.centerline` was already
  navigated from the raw `ReachSeed`. `reach_seed_coords` survives, wired to the
  ONE seed that centerline is navigated from.
- `steps/run_reads.py::_outlet_edges` and the depth-weighted flux integral in
  `outlet_hydrograph` DELETE; the reader parses the listing's own per-boundary
  FLUX series. CONDITION MET: the re-derivation measured 0.0 m3/s on a run whose
  solver was printing FLUX BOUNDARY 1 = -20.25 m3/s across that same boundary, so
  the rain-on-grid hydrograph, its runoff volume and its runoff coefficient were
  all zero on a run that drained. `build_hydrograph_chart`'s compensating
  negation dies with it - one sign convention (outflow positive) is stated at the
  reader. `deck["outlet_nodes"]` becomes `deck["outlet_boundary"]`, the role
  resolved against the numbering the solver uses.
- The interpreter's resume-from-FAILED-attempt DELETES: every terminal state
  tombstones the invocation ledger. CONDITION MET: the records a failed attempt
  left behind were produced by the code as it was then, so re-running the
  corrected question replayed a superseded deck and reported its artifact as the
  new run's answer - the cache-provenance staleness class, and green that costs
  an afternoon. Replay survives where it is earned: a derived rerun seeds this
  ledger from its successful parent's snapshot. `canaries.run`'s per-canary
  `restart_clean` override deletes too - `live_run.drive` passes it for every
  driven run.
- The `T= ... OUT OF RANGE OF THE SOURCES FILE` halt on continued reach runs
  DELETES with the horizon it was measured against. CONDITION MET: the source
  series ended at the deck's own `DURATION + 100 s`, which a continued run
  passes on its first leg-second step, so the split run only ever completed
  with that row extended by hand in the run directory. `write_sources_pulse`
  now authors the same declared scenario over `start + DURATION + 100 s` on one
  absolute clock, with `start` read off the restart file the run continues.
- Continuation from the RESULTS file DELETES in favour of the engine's own
  `RESTART FILE`. CONDITION MET: the results file is a single-precision record
  on the graphic period, so a continued run started at whichever graphic instant
  the period happened to land on (521.0 s of a 600 s leg) carrying a 3.3e-3 m/s
  velocity residual. From the restart record the handover closes to 4.4e-8 m/s
  and the split run's end is BIT-IDENTICAL to the straight-through run's.

## The cloud-deployed era leaves the suite (test cull, scope 1, 2026-08-31)

Tests pinning the browser/AWS deployment, and the one contract shape that
deployment wrote. Evidence per row is a zero-reference grep over
`trid3nt_server/ plugin/ workers/ scripts/ contracts/`.

- `CaseManifest` + `CaseManifestLayer` (`contracts/trid3nt_contracts/case.py`,
  ~70 LOC) and their six cases in `contracts/tests/test_case.py` DELETE. The
  thin per-case S3 index existed for a cold-serve browser path with the agent
  box asleep; that machinery went with the case-view snapshot. `CaseManifest`
  had ZERO references outside its own class body, its `__all__` entry and those
  six tests - a test-only wire type. No `contracts/schemas/` mirror existed, so
  the export is unchanged.
- The `TRID3NT_SOLVER_BACKEND="aws-batch"` arms DELETE from
  `test_case_list_http_route.py`, `test_gate_timeout_local.py`,
  `test_probe_point_http_route.py`, `test_ingest_layer_http_route.py`. The env
  is read NOWHERE for dispatch (`solver_backend()` is hardwired local-docker),
  so an arm setting it to a decommissioned backend and asserting the route is
  served anyway exercises a no-op. The env-unset arm survives in each file as
  the honest statement: no arming needed.
- The `TRID3NT_TILE_SERVER_BASE` tombstone cases DELETE:
  `test_tile_server_base_env_is_dead` + `test_unset_env_no_longer_fails`
  (`test_publish_layer_titiler_base_sprint14aws.py`),
  `test_register_manifest_layers_needs_no_tile_server` + the `_TILE_BASE`
  constant (`test_publish_manifest_register_only_phase4.py`), and the fixture
  setenv in `test_publish_layer_durable_vector_geojson_165p0.py`. The env has
  zero reads in `trid3nt_server/` since the TiTiler exit. The tile-template
  UNWRAP tests are NOT in this row and stay: 199 `/cog/tiles/` URIs live in the
  persisted store and `_unwrap_tile_template` has four live callers.
- `test_stray_case_adoption_removed` + `test_session_anon_registry_removed`
  (`test_anon_identity_convergence.py`) DELETE - `assert not hasattr` guards
  over `Persistence.adopt_cases_to_user`, `_local_case_adoption_done` and the
  session anon-id mirror, all deleted with the multi-user plumbing. The rest of
  the file pins LIVE `LOCAL_SINGLE_USER_ID` convergence and stays, including the
  `extra="forbid"` wire proof (a live contract, not an absence guard).
- `test_case_open_writes_no_case_view_snapshot`
  (`test_case_history_rehydrate_f17.py`) DELETES - `assert not hasattr` over
  `_persist_case_view_snapshot` / `_persist_case_manifest`. Its live half (a
  real open rehydrating chat history) is already
  `test_case_open_rehydrates_chat_history` in the same file.

Standing norm applied: a surviving test of a deleted function is an ANCHOR, not
coverage. Absence guards over already-deleted symbols are that anchor's weakest
form - they cost a run and prove nothing the tree does not already say.

## The attic'd engines' wire contracts follow their engines (test cull, scope 2, 2026-08-31)

`trid3nt_server/workflows/` is telemac-only and `tests/` already carried zero
files naming the moved engines. The CONTRACTS package was not swept with them:
ten modules describing wire shapes for engines that left, four contract suites
whose only subject was those modules, and one dead function inside a live one.
Measured: ZERO import statements anywhere in the repo outside the modules
themselves, their own tests, and the `contracts/__init__` re-export block.

MOVED to the attic (`contracts/trid3nt_contracts/`), suites DELETED:
- `landlab_contracts.py` (1139), `schism_contracts.py` (463),
  `elmfire_contracts.py` (411), `hecras_contracts.py` (200),
  `geoclaw_thacker.py` (111) - not imported by ANY module, not even
  `contracts/__init__.py`. No test existed to delete.
- `openquake_contracts.py` (456) + `contracts/tests/test_openquake_contracts.py`
  (126) - its sole importer WAS that test. The definition of an anchor.
- `geoclaw_contracts.py` (668) + `test_geoclaw_contracts.py` (412) -
  `GeoClawDepthLayerURI` / `GeoClawRunArgs` in zero production files.
- `modflow_contracts.py` (2004) + `test_modflow_contracts.py` (1651) - fourteen
  LayerURI subclasses, `PlumeLayerURI` in zero production files.
- `swmm_contracts.py` (757) + `test_swmm_contracts.py` (457) - the four
  production mentions (`gates/spatial_roles.py`, `gates/confirm.py`,
  `gates/cards/spatial_input.py`, `contracts/ws.py`) are comments and docstrings,
  not imports.
- `swan_contracts.py` (339) - reachable only from the dead function below.

DELETED with them, in live modules:
- `register_published_manifest.register_swan_wave_layers` (+ its `__all__` row)
  and its one test case. ZERO callers in the tree: a wave-layer builder inside a
  live depth-path module, and the only thing importing `swan_contracts` (a lazy
  import written so the generic module would stay SWAN-agnostic - the shape of a
  function that never belonged there).
- `contracts/tests/test_ws.py::test_spatial_input_response_barriers_feed_swmm_contract`
  - it asserted the drawn barriers validate against `SWMMRunArgs.barriers`. The
  wire shape it cared about (roles, barrier tags, flap direction, protected side)
  is already pinned by the sibling test above it.
- The `contracts/__init__.py` re-export block for geoclaw / modflow / swan /
  swmm: 3 module names, 4 `from` statements, 21 `__all__` rows. `__all__` is 59.
  `contracts/schemas/` regenerated through `trid3nt_contracts.export_schemas`:
  ZERO drift - none of these shapes was ever exported.

REWRITTEN, not culled (the subject is LIVE, only the fixture was dead):
- `tests/test_scenario_reuse_dispatch_job0326.py` drives the real
  `_invoke_tool_via_emitter` reuse guard; it merely used `PlumeLayerURI` as the
  layer a stub returns. Now returns `TelemacDyeLayerURI` with `dye_cmax_mgl` -
  the live tracer-concentration contract, the same fact under the live name. The
  guard reads the call's params and the result's `layer_id`, never the layer's
  class, so the coverage is unchanged.
- `tests/test_spatial_input_barriers.py` covers the spatial-input gate; the SWMM
  contract was a SECOND validator of the same FeatureCollection. It now asserts
  against the gate's OWN output (`barriers_feature_collection`): LineStrings,
  both barrier tags, and the drawing-side `role` property dropped. One validator,
  which is what the parse step was always for.

NOT touched, flagged: `contracts/ws.py`, `common.py`, `telemac_contracts.py`,
`output_quantities.py` and `gates/spatial_roles.py` still NAME the moved
contracts in docstrings and comments, and `output_quantities.py` still carries
engine-family enum members for them. Live modules with dead ENTRIES inside is a
different measurement from a dead module, and it gets its own pass.

## The declared-resolution enforcer, superseded by the user's own lever (test cull, scope 3, 2026-08-31)

- `trid3nt_server/tools/resolution_declared.py` (166) MOVES to the attic and
  `tests/test_resolution_declared_0225.py` (282) DELETES. Every public name -
  `ResolutionOutOfRangeError`, `enforce_resolution`, `resolution_review_note`,
  `resolve_resolution` - has ZERO references outside the module's own body and
  that one test, in the server, the plugin, the workers, the scripts and the
  experiment harness alike. Its subject was the autoscale-then-quote-back
  decision, superseded by the ruling that RESOLUTION IS THE USER'S LEVER: the
  autoscaler is a suggestion, a declared value is what the user asked for, and a
  run that is too coarse is re-run finer rather than refused. Its sweep test
  (every workflow file must call `resolve_resolution` rather than hand-roll it)
  was pinning a seam nothing routes through. `trid3nt_server/tools/README.md`
  loses the row in the same commit.

ROW NOT EXECUTED - the sweep's premise is false, reported instead:
- `trid3nt_server/tools/fetchers/_router/stratified.py` (328) STAYS. The sweep
  measured "0 prod importers" over `trid3nt_server/ workers/ plugin/ scripts/
  contracts/` and never looked in `experiments/`, where the catalog-surfacing
  harness imports it FIVE times (`run.py:123,376,530,583`,
  `run_forensic.py:228`) as the Arm-3 mechanism under test, with a recorded
  verdict (`experiments/catalog_surfacing/results/VERDICT.md`) and its spec
  (`docs/specs/stratified-pools.md`) still in the tree. Deleting it breaks a
  concluded experiment's reproducibility, which is a methodology call, not a
  staleness call. Its two test consumers stay with it.

## The QGIS passthrough and discovery pair leave (test cull, scope 4, 2026-08-31)

Telemetry, not opinion: `data/persistence/trid3nt_dev/tool_call_telemetry.json`
(2,550 records, 190 distinct tools) shows `list_qgis_algorithms` invoked 14
times with `result_usable=False` on 14/14, `describe_qgis_algorithm` 5 times (3
TYPEERROR) and `qgis_process` 5 times - all 24 inside a single two-day window,
2026-07-07/08, and NOTHING since. The agent logs across the whole
`trid3nt_server`-namespace era (2026-07-23 -> 2026-08-31) carry ZERO invocation
lines; every non-registry hit is the boot readiness probe's own log line. Every
historical RUN was refused by the offload gate, whose user-facing message names
a decommissioned lane ("will run on AWS Batch in an upcoming update"), and both
`TRID3NT_QGIS_ONBOX_DOCKER` and `TRID3NT_QGIS_DOCKER_IMAGE` are unset.

PREMISE CORRECTED, and it does not change the verdict: the pair is UNUSED, not
BROKEN. Driven live against the host binary (QGIS 3.40.6, 651 algorithms),
`list_qgis_algorithms(include_all=True)` returns 50 well-formed rows - the July
`total=0` was substrate absence on the cloud box, not a parser defect. The cull
rests on zero invocations plus a dead offload gate. This supersedes the ledger's
2026-07-31 REJECT of the same candidate, whose verdict rested on the seam being
live.

MOVED to the attic:
- `trid3nt_server/tools/meta/passthroughs/` (349 + corpus) - `qgis_process`,
  `set_worker_submitter`, `_WORKER_SUBMITTER`, `_qgis_offloaded_result`,
  `QGIS_OFFLOADED_ERROR_CODE`.
- `trid3nt_server/tools/search/qgis_discovery/` (862 + corpus) -
  `list_qgis_algorithms`, `describe_qgis_algorithm`, `CURATED_ALLOWLIST`,
  `MAX_LIST_RESULTS`, `SOURCE_CLASS` - every one with zero references outside
  its own file.
- `workers/qgis/` (Dockerfile + .dockerignore) - headered "Built on the EC2
  agent box", purpose "the QGIS install used for QGIS Server (WMS) render".
  Both decommissioned; never built on this box; the live readiness probe
  resolved the HOST binary, never the image.

DELETED outright:
- `tests/test_qgis_discovery.py` (614) + `tests/test_qgis_process_run_job0308.py`
  (103) - they die with their subject.
- `main.py`: `_default_qgis_process_submitter` (~95, carrying a stale
  `~/miniforge3/envs/grace2/bin/qgis_process` Mac fallback),
  `_bind_worker_submitter` (~63) with its boot call site, and
  `_run_readiness_probe` (~39). `TRID3NT_SKIP_WORKER_SUBMITTER` dies with them.
  The probe was the ONLY thing keeping any of this warm: a boot daemon thread
  logging that a substrate nothing calls is ready.
- The registration imports: `main.py`'s eager `qgis_discovery` import and
  `tools/__init__.py`'s eager `passthroughs` import, plus the docstring
  paragraphs describing the Level-1a discovery loop they completed.
- `contracts/tool_registry.py`: the `read_only_hint` / `idempotent_hint` field
  descriptions stop naming `qgis_process` and `pelicun_damage_assessment`, and
  their cloud-era substrate nouns (GCS / MongoDB / Cloud Run) become the live
  ones. These ride into the exported schema, so `atomic_tool_metadata.json` is
  regenerated in the same commit.

REPOINTED (eleven roster pins - each an edit, no file culled):
- `test_tool_annotations.py`: registration import, two expected-name strings,
  and the whole `test_qgis_process_annotations` (whose asserts still read
  "dispatches a Cloud Run Job" / "is intra-GCP").
- `test_main_startup.py`: the three readiness-probe tests monkeypatching
  `_default_qgis_process_submitter` / `passthroughs._WORKER_SUBMITTER` DELETE;
  the two surviving tests assert on `run_solver`, which is what the eager block
  actually adds over a bare `import trid3nt_server.tools`.
- `test_tools_registry.py`: `test_passthroughs_eager_import_registers_qgis`
  becomes `test_eager_import_registers_meta_tools` on `code_exec_request` - same
  claim (a module-level `@register_tool` fires at package import), live subject.
- `test_tools_cache.py`: the live-no-cache metadata fixture is named
  `code_exec_request` (the name was always arbitrary).
- `plugin/tests/test_run_invocation.py`: the `!run` PARSER tests used the name
  as a sample token; now `list_run_frames`.
- `test_data_fetch.py`, `test_tool_retrieval.py`, `test_search_tools.py`,
  `test_search_tools_mongo_backend.py`, `test_gemini_kwargs_fuzz.py`: roster
  entries and registration-side-effect imports removed; the job-0039 floor drops
  8 -> 7 with the passthrough gone.
- `test_catalog_surfacing.py`: `_REGISTRY_SIZE` 169 -> 166. MEASURED through
  `main._import_tools_registry()`: 166 exactly.

Directory maps updated in the same commits: `trid3nt_server/tools/README.md`
loses the `passthroughs` mention on the `meta/` row and the QGIS-discovery
clause on the `search/` row; `workers/README.md` loses the `qgis/` roster entry.
`tools/meta/__init__.py`, `workflows/solver/solver.py` (which cited
`passthroughs.py` as the DI-seam pattern it mirrors) and
`tools/search/tool_retrieval.py` stop naming modules that are gone, and
`scripts/tool_sweep.py` drops the `describe_qgis_algorithm` argument row. Grep
for `qgis_process` / `qgis_discovery` / `list_qgis_algorithms` /
`describe_qgis_algorithm` across `trid3nt_server/ tests/ plugin/ contracts/
workers/ scripts/` is ZERO.

FLAGGED, not touched: `EXPENSIVE_SCENARIO_TOOLS` (`scenario_reuse.py`) still
keys on `sfincs_flood` / `modflow_contaminant_plume` / `swmm_urban_flood`, and
`test_gemini_kwargs_fuzz.py` still carries `sfincs_flood` /
`pelicun_damage_assessment` argument rows. Live modules holding dead ENTRIES -
the same measurement the engine-contract sweep flagged, and the same separate
pass.

## The soil-moisture store leaves the template (findings walkthrough ruling 13, 2026-08-31)

CONDITION: the continuous store was the RETIRED in-worker runoff model. The
authored deck drives the engine's own static SCS-CN, so every path through
`soil_store` ended in `TELEMAC_ROG_SOIL_STORE_UNAUTHORED` - a knob whose only
behaviour was to refuse. A refusal that can never become a run is a parameter
the model still has to read past, so the knob goes rather than the refusal.

DELETED:
- `rain_on_grid/declarations.py`: the four params - `soil_store`,
  `soil_store_capacity_mm`, `soil_recovery_hr`, `soil_spinup_days`.
- `steps/rain_on_grid.py`: `_soil_store_spin_up` (the 45-day antecedent AORC
  integration of the Michel-2005 store level), the four keyword arguments on
  `write_rain_on_grid_deck`, and the three-arm refusal block
  (`TELEMAC_ROG_SOIL_STORE_NEEDS_WINDOW`, `..._NO_CAPACITY`, `..._UNAUTHORED`).
- `rain_on_grid/rain_on_grid.py`: the four `PHYSICS` members.
- `tests/test_telemac_rain_on_grid_template.py`:
  `test_soil_store_spin_up_fills_from_antecedent` and the three-case
  `test_the_soil_store_refuses_typed_rather_than_reading_as_applied`.
- `scripts/sandbox/replication/rog_ballcreek_soilstore.py` and
  `rog_ballcreek_soilstore_proofs.py` - the two drivers staged
  `soil_store`/`soil_store_capacity_mm`/`soil_store_recovery_h` into a worker
  manifest and read `soil_store_*` back out of its metrics; both channels went
  with the retired in-worker model.

REPOINTED: `contracts/telemac_contracts.py`'s `runoff_path` docstring lists the
two paths that run. `docs/validation/module-coverage-board.md` keeps its
2026-08-10 fidelity-ladder entry: that is the measured history of a run that
happened, not a map of the live tree.

## `graphic_period` leaves the deck for the cadence it was converted from (findings walkthrough ruling 3, 2026-08-31)

CONDITION: the deck said the SAME thing twice. `output_interval_min` (minutes
between frames) rode the reach deck and reached no writer at all - the author
read only `graphic_period` and its 200-step default, so the reach cadence knob
was silently dropped - while the rain-on-grid step converted minutes to steps
itself, upstream of the author, using a time step it had to be handed.

DELETED:
- `steps/author.py`: the `graphic_period` entry in `_DEFAULTS`. The count is
  computed by `_graphic_period` from `output_interval_min` and the deck's own
  `time_step_s`, beside the keyword it is written into, with
  `_DEFAULT_GRAPHIC_PERIOD` standing when no cadence was stated.
- `steps/rain_on_grid.py`: the minutes-to-steps arithmetic in the deck literal;
  the deck now carries the cadence in minutes like every other one.

ADDED, and the reason the pair could diverge unnoticed: `author._consume` -
every key a deck carries is one a writer reads (`_DEFAULTS`) or a record row
names (`_RECORD_ONLY`), or the authoring refuses by name
(`TELEMAC_DECK_KEY_UNCONSUMED`). The one live caller the gate caught on landing
was the dredge test helper, which spread `polygon` into the deck before popping
it off its own kwargs.

REPOINTED: `tests/test_telemac_author_decks.py` states the rain-on-grid cadence
as `output_interval_min`. The `graphic_period` spelling survives there as a
refusal case - the keyword the author stopped reading is exactly the shape the
gate exists to catch.

FLAGGED, not touched: `scripts/prove_telemac_seam.py` and the three
`scripts/sandbox/**/rog_*` drivers still stage a `reach` manifest section with
its own `graphic_period`. That section left the worker at worker-unification
stage 2; they are dead against the current image for reasons that have nothing
to do with this landing, and belong to the stale-script sweep.

## The two DARK FRONTS leave the tree (findings walkthrough ruling 8, 2026-08-31)

CONDITION: both templates were DECLARED PARKED - the in-worker builders they
dispatched to (`telemac_coastal_build.py`, `tomawac_build.py`) went with the
worker unification, so neither front could reach a solve. A parked declaration
that no run can execute is a second tree to keep in step with every seam it
touches, and the rebuild is rung 4.

MOVED to `/home/nate/Documents/trid3nt-attic`, mirroring their repo-relative
paths (reading copies; on no import path):
- `trid3nt_server/workflows/telemac/wave_field/` (5 files, 426 LOC): the
  `tomawac_wave_field` declaration, its four wave modes and its corpus.
- `trid3nt_server/workflows/telemac/coastal_tidal_surge/` (5 files, 546 LOC): the
  `coastal_tidal_surge` declaration, its two series types and its corpus.
- `trid3nt_server/workflows/telemac/steps/wave.py` (271 LOC) and
  `steps/coastal.py` (357 LOC): the two deck writers and their publishers.
- `scripts/proof_coastal_tidal_surge.py` + `proof_coastal_tidal_surge_registered.py`:
  proof drivers whose subject left the tree.

DELETED, tests with their subjects:
- `tests/test_tomawac_wave_field.py` (178) + `tests/test_coastal_tidal_surge.py`
  (330) - 508 LOC.
- `tests/test_telemac_open_water_front.py`: the two coastal/wave deck sections and
  the `_APALACH` / `_COASTAL_PHYSICS` fixtures they needed. The agitation,
  stratified, sizing-provenance and mesh-origin halves are unchanged and green.
- `tests/test_rerun_with_overrides.py`: the coastal VALIDITY IMPORT, not the
  coupled-validity tests. The friction law/coefficient rule is re-declared on the
  probe workflow beside the params it reads, so the rerun lane's coupled check
  keeps its only coverage without a dead template underneath it.
- `scripts/proof_rerun_with_overrides.py` scenario (c) `_law_inversion`: it drove
  the coastal tool's declared VALIDITY. The two typed-refusal probes beside it
  (undeclared name, inert override) stay and now name the scenario.

DELETED, the solver rows:
- `run_telemac.py`: `TOMAWAC_SOLVER_NAME` / `TELEMAC_COASTAL_SOLVER_NAME` and
  their `_SOLVERS` entries - the family is three names now. Their
  `_COMPLETION_METRIC_KEYS` blocks go with them; `wave_mode` and `hs_max_m` move
  to the artemis block (artemis_build writes both) and `ntimestep` to the
  every-leg block (the entrypoint measures it for every leg).
- `code_provenance._SOLVER_ENGINE_OVERRIDES`: the `tomawac_wave` row.

TOMBSTONED, not removed: the two parked registrations in `tools/__init__.py` are
one line each naming the rung-4 rebuild. A reader looking for the front finds why
it is absent rather than nothing.

SLIMMED to its live consumers - `steps/open_water.py`:
- `solves_on_real_bed(domain_kind=...)`: the `coast` arm and the parameter. Only
  the coastal front ever passed `coast`; every remaining caller passed `lake`, so
  the gate is the lake gate and the two-kind refusal branch guarded nothing.
  `steps/agitation.py`, `steps/stratified.py` and the two templates' `DATA.bed`
  declarations drop the keyword.

REPOINTED: `workflows/telemac/workflow.py` (`coastal_surge` + `wave_spectrum`
processes, their readers and `_COASTAL_FORCING`), `steps/__init__.py` exports,
`testing/canaries.py` (both canaries + both refined rows + `_COASTAL_BBOX`),
`testing/proof_animations.py` (both rows), the `not_for` lines on the agitation,
stratified and rain-on-grid declarations, `results_mesh_seam.py`,
`workflows/telemac/README.md`, and the usage examples in
`assemble_proof_packet.py` / `render_selafin_animation.py`.
`proof_wave_bed_input_render.py` and `proof_wave_bed_input_live.py` keep their
ARTEMIS half and lose their TOMAWAC half.

QUEUED, orphaned BY this landing and deliberately not taken here:
`postprocess_telemac.postprocess_tomawac` and `postprocess_coastal` (~500 LOC)
plus the `TelemacWaveLayerURI` / `TelemacCoastalLayerURI` contract types they
return. Their only callers were `steps/wave.py` and `steps/coastal.py`. CONDITION
MET as of this landing; the removal reaches into `contracts/` and belongs to a
landing that owns that path.

## `workflows/shared/` ORPHANS (findings walkthrough ruling 10, 2026-08-31)

CONDITION: nine modules with ZERO product consumers. Each was written for an
engine that left in the fresh-start purge - the SFINCS/SWMM roughness ladder, the
MODFLOW aquifer and soil-hydraulics resolvers, the GeoClaw/SWMM discharge and
site resolvers - or, in `publish_quantities`' case, for a per-engine registry
(`trid3nt_contracts.output_quantities`) that still ships as an EMPTY scaffold no
engine ever opted into. The only things importing them were each other and their
own anchor tests: a module whose only reader is its own test is a test.

MOVED to `/home/nate/Documents/trid3nt-attic`, mirroring their repo-relative
paths (reading copies; on no import path) - 2,408 LOC:
- `aquifer_resolve.py` (659) + `soil_hydraulics.py` (261): the aquifer property
  ladder and the Rosetta/Rawls pedotransfer functions under it.
- `roughness_resolve.py` (315) + `manning.py` (150) + `data/manning_mapping.csv`:
  the NLCD -> Manning's n ladder and the version-pinned mapping it read. The CSV
  had no other reader, so `shared/data/` goes with it.
- `water_table_interp.py` (351): the water-table surface interpolator.
- `publish_quantities.py` (358): the declared-quantity publish executor.
- `discharge_resolve.py` (209), `site_resolve.py` (56), `point_memo.py` (49).

DELETED, tests with their subjects - 522 LOC: `tests/test_discharge_resolve.py`
(46), `tests/test_publish_quantities.py` (308),
`tests/test_shared_soil_hydraulics.py` (84),
`tests/test_shared_water_table_interp.py` (84).

KEPT: `streeter_phelps.py` - it is not an orphan, it wires into the do-sag proof
as the deterministic analytical overlay (findings walkthrough ruling 4).

REPOINTED: `contracts/trid3nt_contracts/output_quantities.py` no longer names the
deleted executor as importable-and-tested (it names the STEP-3 fan-out that would
bind one); `public_data_source_catalog.yaml`'s NLCD entry no longer sends a reader
to a mapping CSV that is not in the tree; `workflows/README.md` lists what
`shared/` actually holds.

QUEUED, adjacent orphans NOT taken here:
- `workflows/shared/tide_series.py`: its consumer was the coastal front (ruling
  8). Only `tests/test_tide_series_datum.py` reads it now. CONDITION: none named
  - a tide series is what a coastal rebuild at rung 4 would want first.
- `contracts/trid3nt_contracts/output_quantities.py`: the registry ships empty,
  no engine declares quantities, and its executor is now gone. CONDITION MET for
  the scaffold; the removal is a `contracts/` landing.

## The BARRIER role leaves the drawn-geometry vocabulary (findings walkthrough ruling 11, 2026-08-31)

CONDITION: scoped read across `gates/spatial_roles.py`, `gates/confirm.py`,
`gates/cards/spatial_input.py` and `contracts/ws.py` found ZERO live invocations.
The barrier role had exactly one terminus - `swmm_urban_flood(barriers=...)` ->
`SWMMRunArgs.barriers` -> the PySWMM wall=omit-conduit / flap_gate=one-way-orifice
seam - and the whole SWMM package left in the fresh-start purge. Everything from
the draw affordance to the engine-ready FeatureCollection was validating, counting
and surfacing structures no tool could receive. The GENERIC spatial-input gate
stays whole: the AOI polygon, the neutral section line, the drawn point, and the
mesh roles (breakline / breach / refine_region / aoi_clip / boundary).

Removed IN PLACE (no attic copy - these are members of surviving files; git
history is the archive, as with every partial removal in this ledger):
- `gates/spatial_roles.py`: the `barrier` role from `CANONICAL_ROLES`,
  `barriers_feature_collection` in full, `_VALID_BARRIER_TYPES` /
  `_VALID_FLAP_DIRECTIONS` / `_VALID_PROTECTED_SIDES`, and
  `DrawnRoles.barriers` / `.n_walls` / `.n_flap_gates`.
- `gates/spatial_input.py`: the same three fields on `ParsedSpatialInput` and the
  `barriers_feature_collection` re-export.
- `gates/cards/spatial_input.py`: the `barriers` / `n_walls` / `n_flap_gates`
  result keys.
- `contracts/ws.py`: the `role == "barrier"` arm of
  `_validate_spatial_input_feature_collection` with its three closed enums, and
  `"barrier"` from the `SpatialInputRequestPayload.purpose` Literal.
- `plugin/ui/gate.py` + `plugin/ui/cards.py`: `SpatialInputRequest.supported` and
  both branches it guarded. It existed to say the plugin cannot draw TAGGED
  barriers; with only `aoi` and `line` left, every parseable request is drawable
  and the property was a tautology guarding an unreachable honest-degrade card.

CONSEQUENCE, stated because it is model-facing: the `purpose` default moves
`"barrier"` -> `"aoi"`. Removing a value from a three-value enum forces a new
default, and `aoi` is the generic one - it is what the tool's own routing text
already names first, and its `aoi_bbox` is what every bbox-taking tool reads.

DELETED, tests with their subject: `tests/test_spatial_input_barriers.py` ->
`tests/test_spatial_input_gate.py`. RENAMED and trimmed rather than dropped,
because two thirds of it is the GENERIC gate: the barrier fixtures and the nine
barrier-specific cases (engine-ready FC, wall/flap counts, the numeric flap
bearing, the five malformed-barrier refusals) went with the role; the role split,
the malformed-FC honesty floor, the response->result mapping, the pause/resume
registry, the cross-session refusal, the emit/await round trip and the tool
sentinel all stay, now exercised through an AOI + section line + point.

REPOINTED: `tests/test_spatial_roles.py` (the mixed-roles case reads a
`breakline` where it read a wall), `tests/test_spatial_input_neutral_line.py`
(the "no line keys" no-regression case is an AOI-only reply; the default-purpose
case reads `aoi`), `tests/test_spatial_input_invalid_resolve.py` (the malformed
inbound reply is an unknown ROLE, refused as `SPATIAL_INPUT_BAD_ROLE`),
`tests/test_declarative_cards.py` (the two pick modes carry the new wire
default), `plugin/tests/*` and `plugin/tests/stub_server.py`'s vector row, the
`request_spatial_input` docstring + corpus phrasings, and the barrier prose in
`server/spatial.py`, `server/errors.py`, `server/protocol/loop.py`,
`server/turn/stream.py`, `gates/confirm.py`, `compute_cross_section` and
`workflows/lib/user_input.py`.

FLAGGED, not touched: `adapters/adapter.py`'s routing block still tells the model
to select `swmm_urban_flood` on "urban / barrier / flap gate" phrasing. That tool
left with the fresh-start purge, so the block routes to nothing - but it is
model-facing wording and belongs to ruling 12's substrate-neutral sweep, not here.

## The dead-name residue the honesty tail scrubbed (findings walkthrough rulings 9 + 15, 2026-08-31)

DELETED, staleness evidence in each line (AGGRESSIVE posture: everything outside
workflows deletes on evidence, ledger line only).

- `scripts/seed_showcase_cases.py`: the `build_mesh` / `mesher="watershed"`
  showcase. The watershed mesher was dissolved into the chained delineation
  (`delineate_watershed` -> `om2d`); the registered meshers are `om2d` and
  `reg_grid`, so the entry named a mesher the router refuses. Its capability is
  the `telemac_rain_on_grid` entry immediately below it - same pour point, same
  catchment, and its plan IS the chain the showcase described.

- `tests/test_gemini_kwargs_fuzz.py`: the `sfincs_flood` and
  `pelicun_damage_assessment` minimal-param rows and the PELICUN fold comment.
  Neither tool is in the registry; the rows were never selected by the fuzz
  parametrization, which reads `TOOL_REGISTRY`. `run_solver`'s sample solver
  moves `sfincs` -> `telemac` for the same reason.

- `docs/authoring/adding-an-engine.md`: the MODFLOW-era body. Every path it
  named is gone - `modflow_contracts.py`, `workers/modflow/`,
  `workflows/run_modflow.py`, `workflows/postprocess_modflow.py`,
  `tools/run_modflow_archetype_tool.py`, `workflows/model_*_scenario.py`, the
  sprint-17 engine bridges. REWRITTEN against the live tree (the declarative
  template, the engine facade's process table, `workers/telemac/`) rather than
  dropped, because `README.md` links it as the authoring entry point.

- `experiments/bench/routing_sweep/run.py` docstring: the ADR reference and the
  "replaces this after the current server batch" schedule promise. The
  client-side block is a standing CONSTRAINT, not a stage in a plan, and the
  docstring now states it as one.

## The inflow-boundary DO load and the no-structure agitation canary (findings walkthrough rulings 4 + 14, 2026-09-01)

DELETED, and what replaced each.

- `steps/author.py` the WAQTEL O2 inflow-load branch: `do_sag_bod_mgl` rode in on
  `PRESCRIBED TRACERS VALUES`, and which boundary got it depended on the
  measured liquid-boundary order agreeing with the engine's own numbering. It did
  not: `ORGANIC LOAD` was identically zero everywhere and the "sag" was the
  imposed inflow value. The load now enters at the OUTFALL as a continuous point
  source (`write_sources_outfall`, four tracer columns), and both liquid
  boundaries carry the SAME clean river, so the ordering can no longer decide the
  answer.

- `telemac_do_sag` params `discharge_bod_mgl`: it named a fully-mixed
  concentration at the top of the reach, which is not a thing a discharger has.
  Replaced by the effluent trio - `effluent_bod_mgl`, `effluent_q_m3s`,
  `effluent_do_mgl` - which is what an outfall IS, and the mixing is the solve's.

- `TelemacDoLayerURI.bod_upstream_mgl`: it published the declared inflow number
  back to the reader as if it were a measurement. Replaced by `bod_mixed_mgl`,
  the MODELED peak CBOD along the reach.

- The `artemis_harbor_agitation` coarse canary's UNFILLED structure slot. It was
  named for a sheltering question over a domain with nothing to shelter, and it
  reported `kd_sheltered` / `kd_exposed` / `sheltering_ratio` for two halves of an
  empty AOI. The canary now supplies the surveyed Marquette Lower Harbor
  breakwater; the unfilled slot keeps its cover in
  `scripts/drive_artemis_structure_slot.py --mode omitted`, which is where all
  three ways of filling it are proved against each other.

## The urban-vs-SFINCS flood routing block (findings walkthrough ruling 12, 2026-09-01)

DELETED from `adapters/adapter.py`: the `Flood-engine routing -- urban PySWMM vs
SFINCS (CRITICAL)` block and the `Key behaviors` clause that pointed at it. Both
instructed the model to call `swmm_urban_flood` on urban / street / storm-drain /
barrier phrasing. That tool left the registry at the engine purge, so the block
was routing prompts to nothing - the worst shape a model-facing instruction can
take, because the model follows it and then invents a recovery.

CONDITION for the residue this left standing: the same system prompt still names
`swmm_urban_flood` in the reuse rule, the fidelity ladder and the rain-on-grid
tier list, alongside `sfincs_flood`, `geoclaw_inundation`, `swan_wave_field`,
`modflow_*`, `openquake_psha`, `landlab_susceptibility`, `elmfire_fire_spread`
and `pelicun_*` - every one of them absent from the registry. Those are not a
routing block; they are the prompt's whole engine roster, and replacing it is a
wording decision about what TRID3NT tells a user it can model. It goes to NATE
rather than being swept here.

## The system prompt's absent-engine roster (NATE ruling 2026-09-01)

CONDITION MET by the ruling above: NATE decided the wording, so the roster is
swept rather than held.

DELETED from `adapters/adapter.py`'s `SYSTEM_PROMPT`: every registry-absent tool
name and the prose built around it. The `Groundwater / MODFLOW routing` block
(fourteen `modflow_*` names, none registered) goes whole; its news-article
never-invent rule survives repointed at `telemac_river_dye`. The
`publish_layer is for RASTER COGs ONLY` block goes whole - `publish_layer` left
the registry, so a vector-publish prohibition guards a call the model cannot
make. The `Cross-engine OVERLAP routing` WAVES bullet
(`schism_coupled_waves` / `swan_wave_field`) and the WATER QUALITY bullet's
SWMM half go with their engines. `sfincs_flood` leaves the opening `Key
behaviors` clause, the reuse rule, the scope-discipline block, the NWS
live-warning chain and a narration example; `fetch_osm_roads` (never a
registered name - the tool is `fetch_roads_osm`) goes with the publish block.

REPLACED, not deleted: the capability surface now states the live TELEMAC
families and the fetch/analysis substrate as question classes, with an honest-
absence paragraph naming what has no solver here. The fidelity ladder survives
ENGINE-NAME-FREE - rung by the question, a narrow stream below 2D's useful
range, calibration last - so it no longer rots with the roster.

`tests/test_system_prompt.py` loses the four vector-publish tests and the two
MODFLOW-routing tests with their subjects, and gains
`test_system_prompt_names_no_absent_tool`, which makes the whole class
impossible: no purged family by name, and no token sharing a first segment with
a registered tool unless it is itself registered.

Model-facing residue swept with it: the `TRID3NT_OPENAI_EXTRA_SYSTEM` default
in `scripts/start_agent.sh` (and its verbatim mirror in
`scripts/telemac_routing_probe.py`) told the local model when to call
`publish_layer`.

## The tool-description surface's absent-engine names (2026-09-01)

The system-prompt sweep above fixed the roster the model reads BEFORE it routes.
This is the same class one layer down: the roster it reads WHILE it routes. A
tool docstring, a `source.yaml` `docstring`/`caveats` block and a `corpus.yaml`
query are all indexed as the description the model and the retrieval ranker
reason over, and every registry-absent name in one advertised a capability the
product does not have.

DELETED across 116 files: every `sfincs*` / `swmm*` / `modflow*` / `geoclaw*` /
`pelicun*` / `openquake*` / `landlab*` / `elmfire*` / `schism*` / `swan*` /
`hydromt*` / `hec-ras` / `publish_layer` / `fetch_osm_roads` token from the
model-facing surface: 21 tool docstrings, 65 `source.yaml` descriptions, 26
retrieval corpora, and the server/gate prose that named a dead engine as the
consumer of a live param or role.

REPLACED, not deleted, per the rule that a routing warning is signal: a "do NOT
use this for X" clause either repoints at a LIVE template or becomes the honest
absence. `telemac_river_dye` and `telemac_do_sag` now decline flood depth toward
`telemac_rain_on_grid` and say plainly that groundwater plumes, dam-break and
tsunami run-up are not currently modeled here. `compute_flood_depth_damage` and
`compute_exposure_summary` keep their honesty floor without pointing it at a
component-level assessment that does not exist. `compute_model_residuals`,
`compute_skill_metrics` and `read_run_diagnostics` restate calibration and
diagnostics substrate-neutrally, so they stop rotting with the roster.

`sfincs_flood` LEAVES `tool_retrieval.CORE_FLOOR`. It was force-included in
EVERY turn's visible set, so a deleted tool held one of eight retrieval slots on
every query in the product. No live template replaces it: a template answers one
question class, and flooring one biases every turn toward it.

`read_run_diagnostics._normalize_engine` loses its `sfincs` / `swmm` / `modflow`
/ `geoclaw` branches. `_PARSERS` has only `telemac`, so those branches turned a
typed `DiagnosticsEngineUnknown` into a `KeyError`; every name the mapper now
returns is a key of `_PARSERS`.

`catalog_http._FLOW_BY_SOLVER_TOOL` repoints from the three dead engines to the
five registered templates, and the per-flow breakdown derives its flow list from
that map, so a flow can no longer be reported without a tool that produces it.

`tests/test_tool_description_surface.py` makes the class impossible rather than
fixing it once: no retired family name in any registered tool's docstring, in any
spec description or caveat, or in any corpus query. The prompt test's
prefix-sharing lock does NOT transfer - a docstring is full of ordinary
identifiers sharing a first segment with a registered tool.

HELD, not swept: `scenario_reuse.EXPENSIVE_SCENARIO_TOOLS` keys, and the
`gates/cards/solver_confirm.py` flood run-settings provider trio
(`_build_flood_run_settings_envelope` / `estimate_flood_run_settings` /
`pin_flood_run_settings`, already on `docs/validation/code-graph/dead_symbols.md`
and importing a `workflows.sfincs` package that no longer exists). CONDITION:
both are dead-by-dead-name rather than dead-string, both carry a live test
surface, and keying a template into the reuse guard is a correctness decision
(a false short-circuit hands the user a stale answer). They go as their own cull
with NATE's read, not inside a description sweep.


## The bed's implicit ladder rung, and the ETOPO base laid under the primary (rung-3 pre-step)

DELETED from `workflows/mesh/shared/primitives.py`: the `_BED_FALLBACK =
("etopo_bathy_base",)` tuple and the `fallback=` kwarg `set_bed` passed with it.
DELETED from `fetchers/_router/hooks/topobathy.py::_select_and_merge`: the
`or not cudem_vsicurl` half of the ETOPO-base condition, and the zero-CUDEM
exemption in `_assert_nearshore_coverage`.

**Why they died.** Together they made a cross-dataset substitution that nobody
declared and no gate saw. `set_bed` permitted the ETOPO rung on the author's
behalf, so a recipe reading `set_bed(source=DATA.topobathy)` could come back
painted from a 450 m EGM2008/MSL global relief with nothing in the recipe saying
so; and the fetch never even needed the permission, because a zero-CUDEM AOI was
exempted from the coverage gap and `_select_and_merge` laid the ETOPO column
down ITSELF. The evidence is the fetch's own note, measured over the Marquette
AOI before the change:

    Fallback ladder (fetch_topobathy): etopo_bathy_base [cross_dataset].
    GATE-UNSEEN: etopo_bathy_base was laid under this result by fetch_topobathy
    itself rather than by descending the ladder, so the fallback loudness gate
    never saw it -- the substitution is reported here, not approved.

**What replaces them.** Nothing. A coast the nearshore composite does not reach
is a 0% coverage gap like any other: the primary refuses, and the coarser bed
is laid only when the caller permitted the declared `etopo_bathy_base` rung
(or set `force_bathy_base`), which is what puts it in front of the loudness gate.
Whether ETOPO belongs in that ladder at all is the bathymetry charter's question.

**Residue, stated.** With the ETOPO base off the primary, a refusal that reaches
the caller after a PERMITTED rung also gapped surfaces the primary's message,
which still advises permitting the rung. The activation rows carry the rung's own
short paint, so the record is complete; the sentence is over-helpful. CONDITION
to close: a walker pass that lets a terminal refusal name the alternatives that
were tried and gapped.

## `mesh.meta["fallback_note"]` - RENAMED to `bed_fallback_note`

`set_bed` is the only thing that writes it and the bed is the only thing it
describes, but it travelled under a generic name and stopped at the mesh layer,
so `steps/rain_on_grid.py:480` read `provenance["bed_fallback_note"]` - a key
nothing wrote - and every rain-on-grid deck reported the default bed note
whatever the fetch had narrated. The datum now carries one name from the op
through `accept()`'s provenance to that reader. `om2d._emitted`'s re-carry of the
same key onto meta it was already in went with it.
