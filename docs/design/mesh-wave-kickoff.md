# Mesh wave - kickoff

STATUS: DRAFT until the calibration methodology is signed. Q4 there (open
boundaries for the coastal domain) may pull slice 6 forward or reshape it;
nothing else here depends on calibration. Frozen on launch, per convention.

The spec is `docs/specs/workflow-blueprint.html` (rev 7, ratified
2026-08-27). This kickoff is its execution plan; where they disagree, the
spec wins. Vocabulary per the spec: engine = solver; mesher = mesh library;
fetcher = data spec; box = the network-isolated container.

## Objective

Build the mesh tool: one tool family shaped like fetch - a parametric
`tool.build_mesh` spec + a per-mesher registry of named edit actions
wrapping official mesher libraries - replacing every scattered meshing
path (deck-writer meshers, `generate_mesh`, the policy classes,
MeshHandle). Fetch and styling are frozen; the run paradigm is unchanged.

## Slices

1. **Library.** `workflows/mesh/tool.py` (the one router: spec validation
   per mesher, explicit-first / case-discovery / declared-default
   resolution order), `session.py` (MeshSession: edit / probes / snapshot /
   restart / accept; `mesh_recipe.jsonl` journaling - the recipe IS the
   record, deterministic replay, hand-edits recorded with layer hash as
   non-replayable), `meshers/` registry. `artifact.py` stays.
2. **Meshers.** Lift `corridor_tin` out of the TELEMAC deck writer; move
   the HEC-RAS graded-seed mesher and `reg_grid` out of `generate_mesh`;
   NEW: `om2d.py` (OceanMesh2D) and `telapy_mesh.py` wrappers, each
   registering its edit actions beside its build. `generate_mesh/`
   dissolves; its standalone-tool role IS `tool.build_mesh` called
   standalone (builds now, stashes in the case).
3. **Display face.** `_write_2dm` moves to `emission/mesh_display.py`;
   mesh becomes a data type on the one seam. Razor: feeds a solver ->
   mesh/; feeds a screen -> emission.
4. **Gate loop.** USER-GATED: present the built mesh as an editable MDAL
   layer + numeric probes (node/element count, edge-length histogram,
   min-angle, boundary segments, obstacles) + wireframe snapshot; agent
   edit tools are GENERATED from the action registry and mounted only
   while a session is open; accept / restart; QGIS hand-edit re-enters as
   `edit("apply_layer_edits", layer)`. AUTO builds inline. The
   template-specific approve-mesh GateSpec metadata is deleted.
5. **Template migrations + renames.** All 7 TELEMAC templates' mesh
   declarations rewritten per the spec's river_dye worked example
   (section 5). Riding renames: `ops.solver_spec -> ops.solve`,
   `ops.read_results -> ops.read`. MeshPolicy / CorridorPolicy /
   CatchmentPolicy deleted from `lib/slots.py` + facades; MeshHandle
   dissolves into MeshArtifact.
6. **Conformal enforcement + open boundaries** (from the prior mesh
   charter, now expressed as edit actions): breaklines constrained into
   the mesh with zero offset, MEASURED acceptance (max node-to-polyline
   distance reported, not asserted); open-boundary segmentation
   (`set_boundary` action -> LIHBOR classes) on coastal builds.
   CALIBRATION Q4 DEPENDENCY: if signed methodology needs open
   boundaries first, this slice leads.
7. **dt from measured edges.** `suggest_time_step_s` reads the
   MeshArtifact's measured minimum edge (probes) instead of the requested
   resolution, so gate-time refinement tightens dt automatically. The
   deck still records the derived value.
8. **Flagship.** artemis BYO OceanMesh rematch as THE canary: authored
   om2d mesh (breakwater via declared edit, conformal, open boundary) ->
   explicit mesh into the agitation template -> solve -> full proof
   packet (all layers in emission order + composite + charts + GIF if
   animated), assembled by `scripts/assemble_proof_packet.py`, adversarial
   pre-delivery review before NATE sees it.

## Acceptance

- Offline suite: ZERO failures (the standing baseline), every slice.
- Deck byte-parity for every template whose mesh ask is semantically
  unchanged by its migration.
- Recipe determinism: same spec + same chain replays to a
  sha256-identical mesh (om2d seeded; any nondeterminism surfaced, not
  hidden).
- Compat gate refusals verified live (SWAN declines a user mesh with its
  reason; SCHISM declines a closed mesh).
- LOC ledger rows per landing (rolling deltas); DELETION_LEDGER lines for
  every chop.
- 4-lens adversarial panel at wave close; Opus verifiers at the gate.

## Constraints (standing)

Fetch + styles frozen. Run paradigm unchanged (no stepping). Static plans
unchanged. ASCII hyphens only. Comments state constraints - no history, no
references, no attribution. No design-pattern names anywhere in code or
docs. Worker touched => image rebuild + smoke through the image.
Path-scoped commits while agents are in flight.
