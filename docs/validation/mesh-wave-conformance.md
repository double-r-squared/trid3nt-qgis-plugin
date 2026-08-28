# Mesh wave - spec-conformance gate

Fresh-eyes walk of `docs/specs/workflow-blueprint.html` (rev 8, 2026-08-27)
clause by clause against the landed tree, plus the D2-D5 rulings recorded in
`docs/IDEAS.md`. Deviations are REPORTED, never fixed - they are design
questions.

Suite state at the time of this walk: five slices, ZERO failures
(1824 / 6813 / 2159 / 1774 passed, contracts 789 passed).

---

## 1. Conformance table

### Section 1 - where the architecture stands

| spec clause | implementation | verdict |
|---|---|---|
| Terminology: engine = solver, mesher = mesh library, fetcher = data spec, box = the isolated container | `workflows/mesh/tool.py:1-13`, `meshers/__init__.py:1-12`, `MeshSpec.mesher` field name throughout | CONFORMS |
| fetch FROZEN | no fetcher spec, router or `styles.yaml` touched by the wave (`git log --stat` over the wave range shows no `tools/fetchers/_router/` or `emission/styles.py` edits) | CONFORMS |
| emission / styles FROZEN, one seam | mesh joins as a data type: `emission/mesh_display.py`, published through `emission/layer_uri_emit.publish_input_layer` (`gate.py:270-276`), `style_preset="mesh_wireframe"` resolved by the existing resolver | CONFORMS |
| solve UNCHANGED - box dispatch, run to completion, no stepping | `ops.solve` (`telemac/workflow.py:308`) still returns the process's own dispatch step; no stepping surface anywhere in `workflows/mesh/` | CONFORMS |
| workflow library UNCHANGED - static plans, module-level declaration blocks, demand-pulled producers | the 7 templates keep `PARAMS` / `DATA` / `PHYSICS` / `FORCING` and a static `plan(ops)`; `MESH` is a new module-level block beside them | CONFORMS |
| mesh BEING REPLACED - MeshPolicy, CorridorPolicy, CatchmentPolicy, MeshHandle, `generate_mesh`, deck-writer meshers all go | `grep -rn "MeshPolicy\|CorridorPolicy\|CatchmentPolicy\|MeshHandle"` over the repo returns ZERO hits; `workflows/mesh/generate_mesh/` does not exist | CONFORMS |

### Section 2.1 - the signature

| spec clause | implementation | verdict |
|---|---|---|
| `MESH = tool.build_mesh(...)` at module level in the template, beside DATA and PARAMS | `river_dye.py:109`, `do_sag.py:78`, `rain_on_grid.py:122`, `agitation.py:121`, `coastal_tidal_surge.py:111`, `stratified_flow.py:103`, `wave_field.py:99` - all 7 | CONFORMS |
| FROZEN, NOTHING builds at import | `MeshTool.build_mesh` (`tool.py:215-221`) returns a frozen `MeshDeclaration` (`tool.py:124`); `deep_freeze` on every resolved field (`tool.py:187`); pinned by `tests/test_build_mesh_tool.py:229 test_a_declaration_builds_nothing` | CONFORMS |
| `mesher` / `kind` / `aoi` / `refine` / `bed` on the signature | `tool.py:216` takes `mesher` and `kind` explicitly; `aoi` / `refine` / `bed` are per-mesher DECLARED fields checked at the router (`om2d.py:106-119`), not signature parameters | CONFORMS (spec section 4 makes fields per-mesher; the class-view line is a summary) |
| `.edit("add_obstacle", D.breakwaters)` - a DECLARED edit, the recipe's prefix | `MeshDeclaration.edit` (`tool.py:135-144`) validates and returns a NEW declaration; the prefix is `MeshSession._declared` (`session.py:71`) | CONFORMS |
| The SAME tool called standalone builds now and stashes in the case | `build_mesh` registered tool (`tool.py:331`) -> `MeshSession(...).accept()` (`tool.py:461`) -> `stash_mesh_artifact` (`session.py:182`). Walkthrough section 2.3 below shows the stash live | CONFORMS |
| At runtime a SESSION opens over the declaration; gate-time edits APPEND to the declared chain | `MeshSession.edit` appends to `_chain` (`session.py:114`) which is seeded from `_declared` (`session.py:72`) | CONFORMS |
| `session.edit(...)` / `session.restart()` / `session.accept()` | `session.py:101` / `:119` / `:148` | CONFORMS |
| `accept()` -> MeshArtifact (.slf / .gr3 / fort.14 + MDAL display) | `session.accept` (`session.py:148-188`) writes `slf_uri` (`_selafin`, `session.py:200`), stages per-solver files the mesher wrote (`_staged_files`, `session.py:214`) and the MDAL display face (`_display_face`, `session.py:191`). No `fort.14` is written on a build - see 2.6 | CONFORMS |

### Section 2.2 - edit-action registry

| spec clause | implementation | verdict |
|---|---|---|
| Each mesher registers named actions with typed inputs | `register_mesher(..., actions=(EditAction(...), ...))` in all seven mesher files (`om2d.py:733`, `corridor_tin.py:603`, `coastal_edge.py:364`, `hecras.py:219`, `reg_grid.py:76`, `telapy_mesh.py:463`, `watershed.py:270`) | CONFORMS |
| Thin hooks wrapping the library's own functions, never reimplementations | `om2d.py` shells `trid3nt-local/mesh:latest` and calls `om.generate_mesh` / `om.identify_ocean_boundary_sections`; `telapy_mesh.py` drives `hermes`/`Selafin`/`Conlim` inside `telemac:latest`; gr3 goes through `workers/schism/schism_gr3.tin_to_hgrid` (`om2d.py:602`, `coastal_edge.py:269`) | CONFORMS |
| The router validates a spec and an edit against the registering mesher's declared fields, LOUDLY | `validate_spec` (`tool.py:166`) and `validate_edit` (`tool.py:191`); typed `MeshToolError` codes `MESH_SPEC_UNKNOWN_FIELD`, `MESH_SPEC_MISSING_FIELD`, `MESH_SPEC_BAD_TYPE`, `MESH_SPEC_BAD_VALUE`, `MESH_EDIT_UNKNOWN_INPUT`, `MESH_UNKNOWN_ACTION`, `MESH_UNKNOWN_MESHER`. Live refusals in walkthrough 2.3 | CONFORMS |
| direct - code and drivers call actions with params | `MeshSession.edit(action, *values, **inputs)` (`session.py:101`); positional binding via `bind_edit_inputs` (`tool.py:147`) | CONFORMS |
| agent - generated tools, one per registered action, mounted only while a session is open, unmounted on accept | `open_mesh_gate` mounts `_edit_tool` per action plus `mesh_accept` / `mesh_restart` (`gate.py:105-131`); `close_mesh_gate` unmounts (`gate.py:134`); `_compiled` builds a REAL signature so the schema names the inputs (`gate.py:244`). Walkthrough 2.2 shows `[]` mounted after accept | CONFORMS |
| user-drawn (later) | not built - marked LATER in the spec | CONFORMS (out of scope by the clause itself) |

### Section 2.3 - gate loop

| spec clause | implementation | verdict |
|---|---|---|
| USER-GATED runs the loop; AUTO builds inline and skips it | `gate_mesh_build` (`gate.py:333-376`): `resolve_input_gate_mode(input_mode) == "auto"` or no emitter -> `session.accept()` inline | CONFORMS |
| GATE presents the MDAL layer + probes | `present_mesh` (`gate.py:264-290`) publishes the layer, zooms to it, returns probes + recipe | CONFORMS |
| user speaks -> agent calls `edit(...)`, scoped tools only | the mounted `mesh_edit_<action>` tools (`gate.py:176-206`) | CONFORMS |
| rebuild -> probes + snapshot -> back to the gate | `MeshSession.edit` returns `probes()` (`session.py:117`); the gate re-presents each round (`gate.py:351`) | CONFORMS |
| QGIS hand-edit re-enters as `edit('apply_layer_edits', layer)` | `apply_layer_edits_action` (`meshers/__init__.py:419`), registered by every mesher; recorded hashed and `replayable: false` | CONFORMS |
| accept -> MeshArtifact; restart -> chain truncated to declared prefix | `gate.py:361-364` and `session.restart` (`session.py:119-125`). Walkthrough 2.2 shows the truncation live | CONFORMS |
| Boundary definition lives in the same loop | `set_boundary` is a registered om2d action (`om2d.py:754`), so it is one of the mounted tools and one of the card's vocabulary rows | CONFORMS |
| After a hand-edit the compat gate re-checks and solver-format translation runs as normal | `_apply_layer_edits` drops the topology-bound meta and claims (`meshers/__init__.py:452-476`) so `engine_compat` is restated at `accept`; `regenerate` rewrites the per-solver files (`corridor_tin`) | CONFORMS |
| Every action returns numeric probes (node/element count, edge-length histogram, min-angle quality, boundary segments, obstacle count) | `_probes` (`session.py:458-502`) returns exactly those, plus the mesher's own `meta["probes"]` | CONFORMS |
| ... and a WIREFRAME SNAPSHOT the agent reads with VISION | `MeshSession.snapshot` (`session.py:131) returns a `LayerURI` row pointing at the `.2dm`; `present_mesh` returns no image. No PNG is produced for the model to read | **DEVIATES (D-4)** |
| the human at the gate stays the final eye | the card is a `PayloadWarningEnvelopePayload` with `options=["proceed","cancel","narrow_scope"]` and a 300 s TTL; a timeout REFUSES rather than proceeding (`gate.py:357-360`) | CONFORMS |

### Section 2.4 - the recipe is the record

| spec clause | implementation | verdict |
|---|---|---|
| A mesh IS (spec + ordered edit chain), journaled | `recipe_lines` / `_journal` (`session.py:247-261`) write `mesh_recipe.jsonl` (`session.py:92`) beside the mesh | CONFORMS |
| first line `{"spec": {...}}`, then one line per edit | exact shape reproduced in walkthrough 2.2 | CONFORMS |
| deterministic rebuild by replay | `replay_recipe` (`session.py:321`); `tests/test_build_mesh_tool.py:300 test_recipe_replays_to_an_identical_mesh` | CONFORMS |
| a mesher whose library does not reproduce itself says so | `Mesher.deterministic` is a MEASURED claim (`meshers/__init__.py:320-334`); `om2d` registers `deterministic=False` with the three-rebuild measurement in the comment (`om2d.py:779-783`); the recipe carries `"determinism": false` (`session.py:255-256`) | CONFORMS |
| `restart` truncates | `session.py:119` | CONFORMS |
| a QGIS hand-edit enters as ONE recorded action carrying the edited layer's hash, honestly non-replayable | `_edit_line` writes the digest plus `source`, and `replayable: false` (`session.py:273-288`); `replay_recipe` REFUSES with `MESH_RECIPE_NOT_REPLAYABLE` (`session.py:341-347`) | CONFORMS |
| an action may hash at most one input (one `source` per edit) | enforced at registration: `MESH_ACTION_AMBIGUOUS_SOURCE` (`meshers/__init__.py:256-264`) | CONFORMS (tightening, in the spec's spirit) |

### Section 2.5 - resolution order + laziness

| spec clause | implementation | verdict |
|---|---|---|
| Explicit-first: an explicit mesh argument on the run always wins, never falls through | `resolve_mesh` (`tool.py:237-288`): an unreadable or incompatible explicit mesh RAISES (`MESH_EXPLICIT_UNREADABLE`, `MESH_ENGINE_INCOMPATIBLE`) | CONFORMS |
| Discovery-second: compatible MeshArtifacts already authored in the case | `find_case_mesh_artifacts` + `mesh_compatible_with_engine` filter (`tool.py:272-279`) | CONFORMS |
| ... and offers them AT THE GATE | discovery returns a `MeshResolution`; the only wired consumer is `supplied_mesh_artifact` (`tool.py:291`, used by ARTEMIS at `telemac/steps/agitation.py:238`), which is the EXPLICIT arm. No template routes a discovered mesh into a gate offer | **DEVIATES (D-9)** |
| Else the declared spec builds the default | `MeshResolution("declared", ...)` (`tool.py:286`) | CONFORMS |
| Demand-pull: the build fires when a downstream step first demands the artifact; the form stays cheap, refused runs cost nothing | LAZY inside the session (`_ensure_built`, `session.py:95`). At the PLAN level the build is an explicit named step (`ReachMesh.corridor`, `Catchment.mesh`) for 3 templates and never happens at all for the 4 `reg_grid` ones | **DEVIATES (D-1, D-2)** |

### Section 2.6 - mesh types

| spec clause | implementation | verdict |
|---|---|---|
| kind vocabulary: `structured_grid`, `unstructured_tri`, `unstructured_quad_flex`, `curvilinear`, `node_link` | declared kinds are `structured_grid` (`reg_grid.py:30`), `unstructured_tri` (om2d, telapy_mesh, corridor_tin, coastal_edge, watershed) and `graded_cells` (`hecras.py:43`). `unstructured_quad_flex`, `curvilinear`, `node_link` are unimplemented; `graded_cells` is not in the spec vocabulary | **DEVIATES (D-6)** |
| mesher roster: `om2d`, `telapy_mesh`, `hecras`, `reg_grid` | registered roster is `coastal_edge, corridor_tin, hecras_rog, om2d, reg_grid, telapy_mesh, watershed` (live output in walkthrough 2.3). `hecras` is registered as `hecras_rog`; `corridor_tin` is named in spec section 4/5; `coastal_edge` and `watershed` are additions | **DEVIATES (D-6)** |
| solver format: SELAFIN .slf +BOTTOM (TELEMAC) | `session._selafin` (`session.py:200`) + `telemac_build.write_bottom_selafin`; `ENGINE_MESH_REQUIREMENTS["telemac"]` (`artifact.py:177`) | CONFORMS |
| hgrid.gr3 + open boundary (SCHISM) | `ENGINE_MESH_REQUIREMENTS["schism"]` requires `gr3_uri` AND `needs_open_boundary` (`artifact.py:185`); written by `tin_to_hgrid` (`coastal_edge.py:269`, `om2d.py:602`) | CONFORMS |
| authoring bundle (HEC-RAS) | `hecras_inputs` bundle + `needs_validated` (`artifact.py:200`, `HECRAS_INPUT_KEYS` `artifact.py:208`) | CONFORMS |
| SWAN is regular-grid ONLY - no `fort.14` written on a build; the ADCIRC writer stays available | `ENGINE_MESH_REQUIREMENTS["swan"] = {"unstructured_unsupported": True, ...}` (`artifact.py:190-193`); nothing under `workflows/mesh/` writes fort.14 - `fort14_uri` stays a declared-but-unwritten artifact field (`artifact.py:94`) and the writer lives in `scripts/sandbox/oceanmesh/mesh_formats.py` | CONFORMS |
| display face: MDAL .2dm mesh layer, never a picture | `emission/mesh_display.write_2dm` (`mesh_display.py:47`); `snapshot()` emits `layer_type="mesh"` (`session.py:143`). Live in walkthrough 2.2 | CONFORMS |

### Section 2.7 - class view

| spec clause | implementation | verdict |
|---|---|---|
| `Workflow`: PARAMS, DATA, MESH declaration blocks; `plan(ops)`; static plan as today | all 7 templates | CONFORMS |
| `MeshTool.build_mesh(...)`: one router, validates per mesher, loudly | `tool.py:212-224` + `validate_spec` | CONFORMS |
| `MeshSession`: edit / probes / snapshot / restart / accept; recipe `mesh_recipe.jsonl`; LAZY, builds on first demand | `session.py:57-188`, `_ensure_built` (`session.py:95`) | CONFORMS (except `snapshot`, D-4) |
| `Mesher` interface: `build(spec) -> mesh`, `actions: registry`, wraps the official library | `Mesher` dataclass (`meshers/__init__.py:320`), `register_mesher` (`:374`) | CONFORMS |
| `EditAction`: name, input schema, doc; wraps one library fn; agent tools GENERATED from these; mounted only while session open | `meshers/__init__.py:238-264`, `gate.py:176` | CONFORMS |
| `MeshArtifact`: mode, crs_authid, bbox, slf_uri / gr3_uri / bundle, display_uri (MDAL 2dm), open_boundary_info, engine_compat, recipe_uri | `artifact.py:57-118` carries every one of those, plus `cli_uri`, `topology_uri`, `probes`, `provenance` | CONFORMS |
| `CompatGate.mesh_compatible_with_engine(art, engine)` - refuses loudly, never force-fits | `artifact.py:213-255`; an unknown engine is incompatible, not permissive. Live refusals in walkthrough 2.3 | CONFORMS |
| `MeshArtifact --> Workflow` : consumed by author + solve | ARTEMIS supplied-mesh path (`telemac/steps/agitation.py:216-248`), the corridor path (`telemac/steps/reach.py:906-950`), the catchment path (`telemac/steps/rain_on_grid.py:865`) | CONFORMS |

### Section 3 - what this removes or moves

| spec clause | implementation | verdict |
|---|---|---|
| MeshPolicy / CorridorPolicy / CatchmentPolicy DELETED, fields become `build_mesh` spec fields | zero hits repo-wide; `extent_km` / `width_m` / `banks` are now `corridor_tin` declared fields (`corridor_tin.py:50-66`) | CONFORMS |
| MeshHandle dissolves into MeshArtifact | zero hits repo-wide | CONFORMS |
| deck-writer-internal meshers become registered meshers | `corridor_tin.py` (lifted reach mesher), `hecras.py` (graded-seed), `reg_grid.py` | CONFORMS |
| `_write_2dm` moves to emission | `emission/mesh_display.py:47`; ledger rows at `docs/DELETION_LEDGER.md:1158-1165` | CONFORMS |

### Section 4 - where the files live

| spec clause | implementation | verdict |
|---|---|---|
| `workflows/mesh/tool.py` - the one router | present, 494 lines | CONFORMS |
| `workflows/mesh/session.py` - MeshSession + journaling | present, 503 lines | CONFORMS |
| `workflows/mesh/artifact.py` - MeshArtifact + compat (exists, stays) | present | CONFORMS |
| `workflows/mesh/meshers/{__init__, om2d, telapy_mesh, corridor_tin, hecras, reg_grid}.py` | all present; PLUS `coastal_edge.py`, `watershed.py`, `drivers/` | **DEVIATES (D-7)**, additive |
| `emission/mesh_display.py` | present | CONFORMS |
| `generate_mesh/` dissolves; its standalone-tool role IS `tool.build_mesh` called standalone | directory gone; ledger rows `docs/DELETION_LEDGER.md:1134-1180`; `tests/test_generate_mesh.py` deleted, superseded by `tests/test_mesh_meshers.py` | CONFORMS |
| `lib/slots.py` sheds MeshPolicy; `telemac/workflow.py` sheds CorridorPolicy and MeshHandle | zero hits | CONFORMS |
| adding a mesher is one file | each mesher file self-registers; `tool.py:41-47` is the only roster | CONFORMS |
| (spec layout does not name `gate.py`, `precondition_gate.py`, `telemac_build.py`, `watershed.py`, `corpus.yaml`) | all present under `workflows/mesh/` | **DEVIATES (D-7)**, additive |

### Section 5 - the river_dye worked example

| spec clause | implementation | verdict |
|---|---|---|
| Two policy blocks collapse into ONE `MESH = tool.build_mesh(mesher="corridor_tin", ...)` declaration | `river_dye.py:109-118` - side-by-side diff in walkthrough 2.1 | CONFORMS |
| `mesher="corridor_tin"`, `kind="unstructured_tri"`, `domain=Ref("reach")`, `extent_km`, `width_m`, `banks`, `refine={"edge_length","mode"}` | all seven present, verbatim | CONFORMS |
| `bed = "fetch:usgs_3dep"` | ABSENT from the landed declaration; `corridor_tin` declares no `bed` field - the reach deck fits elevation at authoring time (`artifact.py:173-179`) | **DEVIATES (D-5)** |
| `ops.solver_spec` renames to `ops.solve` | `telemac/workflow.py:308`, all 7 templates; zero `ops.solver_spec` hits | CONFORMS |
| `ops.read_results` renames to `ops.read` (D2-D5 sibling ruling) | `telemac/workflow.py:324`, all 7 templates; zero `ops.read_results` hits | CONFORMS |
| `ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING)` demand-pulls the build | `ops.author` accepts the declaration (`telemac/workflow.py:285-303`) but only translates its fields into deck keywords (`mesh_deck_fields`, `telemac/workflow.py:353-368`). The build happens in a SEPARATE plan step, `ReachMesh.corridor(...).named("corridor_mesh")` (`river_dye.py:133`) | **DEVIATES (D-2)** |
| the custom approve-mesh GateSpec metadata is deleted | no `GateSpec(kind="solver", estimate_provider=..., pin_provider=...)` for any mesh anywhere; the only remaining GateSpec is the fetch-resolution one (`tools/fetchers/_router/router.py:30`). Client half chopped in `7ee19768` | CONFORMS |
| a supplied mesh works here for free (explicit argument or case discovery) | explicit arm live for ARTEMIS (`agitation.py:238`); `river_dye` / `do_sag` have no supplied-mesh slot | PARTIAL - see D-9 |
| ResolutionSpec, sensitivity labels, provenance rows and coercions untouched | `river_dye/declarations.py` unchanged in the wave; `EDGE_RESOLUTION_SPECS` moved to `meshers/__init__.py:51-84` and rides `build_mesh`'s metadata (`tool.py:320`) | CONFORMS |
| only the two mesh policy blocks, ONE line of plan, and the gate metadata change | `river_dye.py` plan gains a line (`ReachMesh.corridor`) AND changes two (`ops.solve`, `ops.read`); the `MESH` block replaces two policy blocks | CONFORMS in substance; the plan delta is larger than the spec's "one line" |

### D2-D5 rulings (`docs/IDEAS.md`, 2026-08-27)

| ruling | implementation | verdict |
|---|---|---|
| D2 - `accept()` implies stageable: a hand-edit regenerates (via telapy) or the edit REFUSES at the gate | `_refuse_unadoptable` (`meshers/__init__.py:479-501`) raises `MESH_EDIT_NOT_STAGEABLE` at the EDIT for a bundle-realized or cell-less mesh; `apply_layer_edits_action(regenerate=...)` rewrites the per-solver record for meshers that carry one. Pinned by `tests/test_corridor_mesh_readopt.py:158, :195, :214, :219` | CONFORMS |
| D3 - numeric knobs as a `param_sheet` the shipped client renders; plugin changes only if unavoidable | `_mesh_param_sheet` / `_knob_rows` (`gate.py:469-561`) emit `ParamSheet` + `ParamSheetRow` on the EXISTING envelope; commit `271cbecd` touched only `gate.py` and its test - ZERO plugin files. Reply routing `_sheet_edits` / `_as_declared` (`gate.py:416-457`). Live card in walkthrough 2.2 | CONFORMS |
| D4 - approve-mesh chop finished client-side | commit `7ee19768` removed `plugin/ui/cards.py` (-111), `plugin/ui/gate.py` (-27), `plugin/tests/validate_bk3b_driver_offline.py`, and renamed the headless driver to `headless_mesh_gate_drive.py`; ledger at `docs/DELETION_LEDGER.md:1186-1205` | CONFORMS |
| D5 - worker-bundle round-trip test | `tests/test_corridor_mesh_readopt.py:389 test_the_staged_bundle_round_trips_every_key_the_solve_reads`, `:409`, `:420` | CONFORMS |
| D5 - one live adopted-mesh solve | `scripts/proof_corridor_hand_edit_solve.py` (commit `6cc66ae3`): builds through the box, splits one interior triangle, accepts, authors the DO-sag deck on it, solves; +1 node / +2 elements discriminates the accepted mesh from a rebuild | CONFORMS |
| (kickoff) slice 6 - conformal enforcement with MEASURED acceptance, reported not asserted | `_conformal_probe` (`om2d.py:668-691`) reports `constrained_points` and a `breakline_offset_m` block naming what was measured | CONFORMS |
| (kickoff) slice 6 - open-boundary segmentation -> LIHBOR / gr3 sections | `set_boundary` (`om2d.py:754`) via `identify_ocean_boundary_sections`; sections number the `.cli` and the `hgrid.gr3` (`om2d.py:19-22`, `:576`) | CONFORMS |
| (kickoff) slice 7 - dt from the MEASURED minimum edge, not the requested resolution | `suggest_time_step_s(mesh_size_m, *, mesh=None)` (`telemac/steps/reach.py:114-128`) prefers `measured_min_edge_m(art)` (`artifact.py:147`); pinned by `tests/test_build_mesh_tool.py:434` and `:450` | CONFORMS |
| (kickoff) slice 8 - artemis BYO OceanMesh rematch as the flagship, with a full proof packet and adversarial pre-delivery review | `docs/proof/.../artemis_om2d_rematch/refined/` - `packet.json`, `PRE_DELIVERY_FINDINGS.md` (174 lines), wireframe + zoom-crop + peak frame + chart + canary evidence, `authored_mesh.2dm` | CONFORMS |

---

## 2. Behavior walkthrough (live transcripts)

### 2.1 The real `river_dye` MESH declaration beside the spec's section-5 example

Spec 5.2 (`docs/specs/workflow-blueprint.html:249-268`):

```py
MESH = tool.build_mesh(
    mesher = "corridor_tin",         # today's reach mesher, registered behind the tool
    kind   = "unstructured_tri",
    domain = Ref("reach"),           # the corridor acquire_domain binds
    refine = {"edge_length": P.mesh_resolution_m, "mode": P.mesh_resolution},
    extent_km = P.reach_length_km,   # ex-CorridorPolicy fields, validated
    width_m   = P.channel_width_m,   #   by the corridor_tin mesher at the router
    banks     = P.bank_source,
    bed    = "fetch:usgs_3dep",
)

def plan(ops):
    return [
        FormGate(title="Review the river-tracer scenario"),
        DrawGate(param="release_coords", geometry="point", ...),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, rivers=D.rivers, ...),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),   # demand-pulls the build
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read_results(Ref("solve"), ...).chart("dye_concentration", ...),
    ]
```

Landed (`trid3nt_server/workflows/telemac/river_dye/river_dye.py:105-138`):

```py
#: The MESH ASK, frozen at declaration and building nothing at import. Every field
#: is checked at the router against what the ``corridor_tin`` mesher declares, so a
#: knob it does not read is refused by name rather than ignored.
MESH = tool.build_mesh(
    mesher="corridor_tin",
    kind="unstructured_tri",
    domain=Ref("reach"),
    extent_km=P.reach_length_km,
    width_m=P.channel_width_m,
    banks=P.bank_source,
    refine={"edge_length": P.mesh_resolution_m, "mode": P.mesh_resolution},
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The river-tracer recipe. Pure and STATIC: it reads no value, it names them.
    ...
    """
    return [
        FormGate(title="Review the river-tracer scenario"),
        DrawGate(param="release_coords", geometry="point",
                 prompt="Click where the substance enters the river"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, rivers=D.rivers,
                            discharge=P.discharge_m3s, event_time=P.event_time),
        ReachMesh.corridor(mesh=MESH, seed=Ref("seed")).named("corridor_mesh"),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("dye_concentration", builder=build_dye_chart),
    ]
```

Differences, exactly two:

1. `bed = "fetch:usgs_3dep"` is absent - `corridor_tin` declares no `bed` field
   and would refuse it by name. (D-5.)
2. The plan carries an extra line, `ReachMesh.corridor(mesh=MESH,
   seed=Ref("seed")).named("corridor_mesh")`, and `ops.author(mesh=MESH, ...)`
   is a second consumer of the same declaration rather than the demand-pull.
   (D-2.)

Everything else - the mesher, the kind, the `Ref("reach")` domain, the four
ex-policy fields, `ops.solve`, `ops.read` - matches the spec verbatim.

### 2.2 A real gate session: present -> knob revision -> rebuild -> restart -> accept

Driven live against `gate_mesh_build` with the `reg_grid` mesher and a fake
emitter (the shape the offline gate suite uses), answering each card on the
shared pending-confirmation spine. Declaration carries a DECLARED edit
(`set_resolution 800.0`) so the truncation is visible.

```
DECLARATION spec : {"mesher": "reg_grid", "kind": "structured_grid", "aoi": [-83.5, 35.0, -83.4, 35.09], "resolution_m": 400.0}
DECLARATION edits: [('set_resolution', {'resolution_m': 800.0})]

--- ANSWER card 01M131TX65... decision='narrow_scope' revised_args={"set_resolution.resolution_m": "250"}

--- ANSWER card 01M131TX6Q... decision='narrow_scope' revised_args={"restart": "yes"}

--- ANSWER card 01M131TX6T... decision='proceed' revised_args=null

=== CARDS EMITTED ===

[card 1] message_type=tool-payload-warning
  tool_name      : conformance_walkthrough
  tool_args      : {"mesh_id": "01M131TX63A7QFDS833HB0JE1R", "mesh_layer_id": "mesh-01M131TX63A7QFDS833HB0JE1R", "mesh_display_uri": "/tmp/mesh-conformance-16qzw3ic/mesh.2dm"}
  recommendation : The reg_grid mesh is on the map as an editable mesh layer (round 1/3): 168 nodes / 143 elements, EPSG:4326; NO bed sampled; edge length 770.7 - 828.5 m (mean 799.4 m); min angle 90.0 deg; 48 boundary edges in 1 loop(s). Submit unchanged to solve on it, cancel to stop, or edit a row to refine it. Registered actions: apply_layer_edits, set_extent, set_resolution.
  param_sheet.title: Review the reg_grid mesh (round 1/3)
    row name='set_resolution.resolution_m' value=None door='gate' basis='user' user_lever=True
        desc=the new uniform cell size, in metres
        badge=set_resolution - leave blank to keep this mesh
    row name='restart' value='no' door='gate' basis='user' user_lever=True
        desc=type yes to throw away the gate-time edits and rebuild the mesh as it was declared
        badge=loop action

[card 2] message_type=tool-payload-warning
  tool_name      : conformance_walkthrough
  tool_args      : {"mesh_id": "01M131TX63A7QFDS833HB0JE1R", "mesh_layer_id": "mesh-01M131TX63A7QFDS833HB0JE1R", "mesh_display_uri": "/tmp/mesh-conformance-16qzw3ic/mesh.2dm"}
  recommendation : The reg_grid mesh is on the map as an editable mesh layer (round 2/3): 1517 nodes / 1440 elements, EPSG:4326; NO bed sampled; edge length 250.5 - 253.2 m (mean 251.8 m); min angle 90.0 deg; 152 boundary edges in 1 loop(s). Submit unchanged to solve on it, cancel to stop, or edit a row to refine it. Registered actions: apply_layer_edits, set_extent, set_resolution.
  param_sheet.title: Review the reg_grid mesh (round 2/3)
    row name='set_resolution.resolution_m' value=None door='gate' basis='user' user_lever=True
        desc=the new uniform cell size, in metres
        badge=set_resolution - leave blank to keep this mesh
    row name='restart' value='no' door='gate' basis='user' user_lever=True
        desc=type yes to throw away the gate-time edits and rebuild the mesh as it was declared
        badge=loop action

[card 3] message_type=tool-payload-warning
  tool_name      : conformance_walkthrough
  tool_args      : {"mesh_id": "01M131TX63A7QFDS833HB0JE1R", "mesh_layer_id": "mesh-01M131TX63A7QFDS833HB0JE1R", "mesh_display_uri": "/tmp/mesh-conformance-16qzw3ic/mesh.2dm"}
  recommendation : The reg_grid mesh is on the map as an editable mesh layer (round 3/3): 168 nodes / 143 elements, EPSG:4326; NO bed sampled; edge length 770.7 - 828.5 m (mean 799.4 m); min angle 90.0 deg; 48 boundary edges in 1 loop(s). Submit unchanged to solve on it, cancel to stop, or edit a row to refine it. Registered actions: apply_layer_edits, set_extent, set_resolution.
  param_sheet.title: Review the reg_grid mesh (round 3/3)
    row name='set_resolution.resolution_m' value=None door='gate' basis='user' user_lever=True
        desc=the new uniform cell size, in metres
        badge=set_resolution - leave blank to keep this mesh
    row name='restart' value='no' door='gate' basis='user' user_lever=True
        desc=type yes to throw away the gate-time edits and rebuild the mesh as it was declared
        badge=loop action

=== LAYERS PUBLISHED ===
  layer_id=mesh-01M131TX63A7QFDS833HB0JE1R type=mesh style=mesh_wireframe crs=EPSG:4326
    uri=/tmp/mesh-conformance-16qzw3ic/mesh.2dm
  layer_id=mesh-01M131TX63A7QFDS833HB0JE1R type=mesh style=mesh_wireframe crs=EPSG:4326
    uri=/tmp/mesh-conformance-16qzw3ic/mesh.2dm
  layer_id=mesh-01M131TX63A7QFDS833HB0JE1R type=mesh style=mesh_wireframe crs=EPSG:4326
    uri=/tmp/mesh-conformance-16qzw3ic/mesh.2dm

=== MAP COMMANDS ===
  zoom-to {"bbox": [-83.5, 35.0, -83.4, 35.09]}
  zoom-to {"bbox": [-83.5, 35.0, -83.4, 35.09]}
  zoom-to {"bbox": [-83.5, 35.0, -83.4, 35.09]}

=== FINAL RECIPE (mesh_recipe.jsonl) ===
{"spec": {"mesher": "reg_grid", "kind": "structured_grid", "aoi": [-83.5, 35.0, -83.4, 35.09], "resolution_m": 400.0}}
{"edit": "set_resolution", "resolution_m": 800.0}

=== FINAL PROBES ===
  168 nodes / 143 elements, EPSG:4326
  NO bed sampled
  edge length 770.7 - 828.5 m (mean 799.4 m)
  min angle 90.0 deg
  48 boundary edges in 1 loop(s)

=== ACCEPTED ARTIFACT ===
  mesh_id      : 01M131TX63A7QFDS833HB0JE1R
  mode         : reg_grid
  nodes/elements: 168 / 143
  crs_authid   : EPSG:4326
  has_bathymetry: False
  engine_compat: []
  display_uri  : /tmp/mesh-conformance-16qzw3ic/mesh.2dm
  recipe_uri   : /tmp/mesh-conformance-16qzw3ic/mesh_recipe.jsonl
  provenance   : {"mesher": "reg_grid", "spec": {"mesher": "reg_grid", "kind": "structured_grid", "aoi": [-83.5, 35.0, -83.4, 35.09], "resolution_m": 400.0}, "edits": ["set_resolution"]}

=== MOUNTED TOOLS AFTER ACCEPT ===
   []
```

What the transcript proves, clause by clause:

* The declared prefix builds first: 168 nodes at the declared
  `set_resolution 800.0`, not at the spec's `400.0`.
* The card knob `set_resolution.resolution_m = "250"` routes into the
  `set_resolution` action and the mesh REBUILDS - 1517 nodes, measured edge band
  250.5-253.2 m. The string is coerced to a number by the declaration
  (`_as_declared`, `gate.py:435`).
* `restart: yes` truncates back to the declared prefix - round 3 reports the
  same 168 nodes / 770.7-828.5 m band as round 1.
* The recipe file ends with exactly the declared prefix; the gate-time edit is
  gone, which is what truncation means.
* The display face is `layer_type="mesh"` with `style_preset="mesh_wireframe"`,
  an editable MDAL `.2dm`, republished each round.
* `proceed` accepts, and the mounted `mesh_*` tools are gone afterwards.
* `engine_compat` is honestly empty - a bed-less lattice carries no SELAFIN.

### 2.3 A real `!run` of `build_mesh`

Invoked through the registry closure the `dev-tool-invoke` handler dispatches
(`server/protocol/handlers.py:68-165` -> `_dispatch_tool_and_persist` ->
`TOOL_REGISTRY[name].fn`), against the live MinIO cache bucket, with the turn
case bound as the plugin binds it.

```
!run build_mesh {"mesher": "reg_grid", "kind": "structured_grid", "bbox": [-83.5, 35.0, -83.4, 35.09], "resolution_m": 400.0}
TRID3NT_CACHE_BUCKET = trid3nt-cache
registry entry     : build_mesh cacheable= False ttl_class= live-no-cache

-- returned LayerURI --
  layer_id   : mesh-01M131WG2EKEJNPPWA5S32PGA7
  name       : Mesh: reg_grid mesh
  layer_type : mesh
  style_preset: mesh_wireframe
  role       : primary
  crs_authid : EPSG:4326
  bbox       : (-83.5, 35.0, -83.4, 35.09)
  uri        : s3://trid3nt-cache/mesh/01M131WG2EKEJNPPWA5S32PGA7/mesh.2dm

-- stashed in the case -- 1 artifact(s)
  01M131WG2EKEJNPPWA5S32PGA7 mode=reg_grid nodes=624 elements=575 compat=[]
    recipe_uri : s3://trid3nt-cache/mesh/01M131WG2EKEJNPPWA5S32PGA7/mesh_recipe.jsonl
    telemac  -> False  mesh 'reg_grid mesh' carries no SELAFIN (.slf, BOTTOM) geometry that telemac requires (this mesh was built as mode='reg_grid')
    schism   -> False  mesh 'reg_grid mesh' carries no SCHISM hgrid (.gr3) geometry that schism requires (this mesh was built as mode='reg_grid')
    swan     -> False  the SWAN worker is REGULAR-GRID (CGRID REGULAR + bottom.bot); it has no unstructured-mesh (fort.14) path, so it cannot consume a user-supplied mesh
    hecras   -> False  mesh 'reg_grid mesh' is not a HEC-RAS RoG authoring bundle (graded seeds + channel breaklines + local terrain frame) (missing seeds, breaklines, local_dem, prep_json; built as mode='reg_grid')

-- unknown field refused BY NAME --
  MeshToolError[MESH_SPEC_UNKNOWN_FIELD]: mesher 'reg_grid' declares no field 'gradation' (declared: ['aoi', 'kind', 'resolution_m']). Unknown fields: ['gradation'].

-- unknown mesher refused BY NAME --
  MeshToolError[MESH_UNKNOWN_MESHER]: no mesher named 'oceanmesh' is registered (did you mean 'telapy_mesh'? declared: ['coastal_edge', 'corridor_tin', 'hecras_rog', 'om2d', 'reg_grid', 'telapy_mesh', 'watershed']).
```

What this proves:

* The standalone call BUILDS NOW (AUTO mode, no gate) and STASHES the artifact
  in the case for discovery - spec 2.1's second block.
* The mesh, its recipe and its display face all land beside each other under
  `mesh/<mesh_id>/` in the cache bucket.
* The compat gate refuses per engine by NAME and by MISSING REQUIREMENT; SWAN's
  refusal is the spec's own regular-grid-only sentence.
* The router refuses an undeclared field and an unregistered mesher by name,
  quoting the declared roster - and the roster it quotes is the deviation
  recorded as D-6.

---

## 3. Deviations - design questions for NATE, not fixed

**D-1. Four of the seven TELEMAC templates never open a mesh session at all.**
`agitation.py:121`, `coastal_tidal_surge.py:111`, `stratified_flow.py:103` and
`wave_field.py:99` declare `mesher="reg_grid"`, but the only consumer of that
declaration is `ops.author` -> `mesh_deck_fields` (`telemac/workflow.py:353-368`),
which translates `resolution_m` into the deck keyword `mesh_resolution_m` and
hands it to the deck writer; the writer lays its own lattice. For those four
templates no `MeshSession` opens, no `MeshArtifact` is produced, no gate is
presented even in USER-GATED mode, and the registered `reg_grid` mesher and its
edit actions never execute. Spec 2.3 and 2.5 read as though every declared MESH
becomes a session. Question: is the reg_grid declaration meant to be a deck
keyword carrier, or should those four also route through the tool?

**D-2. The build is an explicit named plan step, not a demand-pull off
`ops.author`.** Spec 5.2 shows the delta as `ops.author(mesh=MESH, ...) # demand-pulls
the build`. Landed, `river_dye.py:133` and `do_sag.py:105` add
`ReachMesh.corridor(mesh=MESH, seed=Ref("seed")).named("corridor_mesh")` and
`rain_on_grid.py:150` adds `Catchment.mesh(mesh=MESH, ...)`, while `ops.author`
ALSO takes `MESH` and sends its fields down a second channel into the deck. One
declaration, two consumers, and the mesh's position in the plan is explicit
rather than demanded.

**D-3. The declared edit chain is dropped by the template mesh steps.**
`ReachMesh.corridor` reads `mesh.spec.fields` only (`telemac/steps/reach.py:897`)
and `build_corridor_mesh` reconstructs the declaration with
`tool.build_mesh(mesher="corridor_tin", kind="unstructured_tri", ...)` and no
`.edit()` chain (`telemac/steps/reach.py:929-931`); `Catchment.mesh` does the same
(`telemac/steps/rain_on_grid.py:874-882`). Both also hardcode the mesher and kind
rather than reading them off the declaration. Spec 2.1/2.4 make declared edits the
recipe's prefix. No template declares an edit today, so this is latent - but a
`MESH.edit("refine_region", ...)` added to `river_dye` would be silently ignored.

**D-4. `snapshot()` returns a map layer, not a wireframe PNG.** Spec 2.3 and the
2.7 class view both say "snapshot() wireframe png" that "the agent reads with
vision". `MeshSession.snapshot` (`session.py:131-146`) returns a `LayerURI` row
pointing at the `.2dm`, and `present_mesh` (`gate.py:282-290`) returns
`{mesh_id, mesher, layer_id, display_uri, probes, edit_tools, recipe}` - no
image. The human sees the mesh in QGIS; the agent at the gate sees numbers only.
Question: is the vision channel still wanted, or did the MDAL layer supersede it?

**D-5. The `bed` field in the section-5 worked example does not exist on
`corridor_tin`.** Spec 5.2 includes `bed = "fetch:usgs_3dep"`; the landed
`river_dye` MESH omits it and `corridor_tin` declares no `bed` field, so adding
it would be refused by name. The reason is recorded in the code (a corridor is
bed-less by construction; the reach deck fits elevation from the terrain the run
acquires - `artifact.py:173-179`), which reads as the more accurate design. The
spec's example line is the thing that is now wrong.

**D-6. The mesher roster and the `kind` vocabulary both differ from spec 2.6.**
Registered: `coastal_edge, corridor_tin, hecras_rog, om2d, reg_grid,
telapy_mesh, watershed`. The spec's axis table lists `om2d | telapy_mesh |
hecras | reg_grid`. `hecras` is registered as `hecras_rog` (`hecras.py:220`).
`coastal_edge` and `watershed` are meshers the spec's table does not name.
On the kind axis, `unstructured_quad_flex`, `curvilinear` and `node_link` are
declared nowhere, and `graded_cells` (`hecras.py:43`) is declared but is not in
the spec's vocabulary. Question: trim the spec to what exists, or keep the
unimplemented kinds as declared intent?

**D-7. The package carries more than spec section 4 names.** Extra mesher files
(`meshers/coastal_edge.py`, `meshers/watershed.py`, `meshers/drivers/`) and extra
modules (`workflows/mesh/gate.py`, `precondition_gate.py`, `telemac_build.py`,
`watershed.py`, `corpus.yaml`). All additive and all coherent - `gate.py` in
particular IS the section 2.3 loop, which the layout simply does not mention.
Flagged so the spec's layout block can be refreshed rather than quietly drifting.

**D-8. The superseded shared approve-mesh preview gate is still in the tree.**
`trid3nt_server/mesh/preview_gate.py` still describes itself as the "single
approve-mesh gate" for TELEMAC, SWMM, SFINCS, SWAN and MODFLOW. Its four exports
(`MeshGateStats`, `build_mesh_gate_envelope`, `default_gate_mode`,
`mesh_gate_should_fire`) are re-exported by `trid3nt_server/mesh/__init__.py:48-53`
and exercised only by `tests/test_mesh_preview_gate.py`; there is no production
consumer. The wave deleted the template-specific GateSpec metadata and the
client half, but this module survived. Deletion candidate for the stale sweep,
not a code change to make inside the conformance gate.

**D-9. Case DISCOVERY is implemented but not wired to any gate offer.**
`resolve_mesh` (`tool.py:272-279`) discovers compatible case meshes and returns
them as `MeshResolution("discovered", ...)`, and it is covered by tests
(`tests/test_build_mesh_tool.py:197, :206`). The only production caller is
`supplied_mesh_artifact` (`tool.py:291`), which takes the EXPLICIT arm and is
reached only from ARTEMIS (`telemac/steps/agitation.py:238`). Spec 2.5 says the
router "discovers compatible MeshArtifacts already authored in the case and
OFFERS THEM AT THE GATE" - nothing offers them today.

---

## 4. Verdict

Every load-bearing clause of the mesh tool - the frozen declaration, the router's
per-mesher validation and by-name refusals, the session's lazy build, the edit
registry and its generated tools, the recipe as the record with honest
non-replayability and measured determinism, the gate loop with its param-sheet
revision channel, the artifact and its compat gate, the display face on the
emission seam, the policy-class and `generate_mesh` demolition, the `ops.solve` /
`ops.read` renames, dt from the measured edge, conformal enforcement and open
boundaries - is implemented and behaves as specified under live exercise.

The nine deviations are all SHAPE questions, not correctness failures: where the
build fires in a plan (D-1, D-2, D-9), what a declaration carries forward (D-3,
D-5), what the gate hands the agent (D-4), and vocabulary/layout drift between
the spec text and the landed roster (D-6, D-7, D-8).
