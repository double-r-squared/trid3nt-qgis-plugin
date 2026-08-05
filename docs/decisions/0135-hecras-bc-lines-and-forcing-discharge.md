# ADR 0135 -- HEC-RAS: the Boundary Condition Lines writer lands (link c3) + the pure-2D .bNN forcing is empirically discharged; the fresh-topology solve is the precise remaining STOP

Status: accepted (2026-08-05)
Follows: ADR 0134 (the pure-2D forcing reference obtained -- OI-A discharged; the
`hecras_flood_2d` template GATED on FOUR links c1-c4, with c5/`.bNN` discharged by
the shipped `b06` reference), ADR 0133 (the 2D geometry WRITER lands at dWSE 0.0 +
the deck-skeleton triage), ADR 0132 (the Muncie transplant -- VALIDATED UNLOCK),
ADR 0109 (the Muncie riverine baseline: maxWSE 951.93 ft, ~4881 wet cells, vol err
0.0058%).

This wave discharges **ADR 0134 link c3** (the writer's Boundary Condition Lines
author) with an offline round-trip gate, and **empirically discharges the `.bNN`
forcing claim** (c5) against the REAL shipped pure-2D reference. It then re-scopes
the remaining links with two sharp findings and STOPS precisely at the
fresh-topology SOLVE per the ADR 0133/0134 charter stop rule -- no template that
cannot run its own acceptance (b), no unvalidated deck composers.

Additive worker-component wave: NO server / tool / contract / registry change
(registry byte-identical by construction). New durable code lives under
`services/workers/hecras2025/`, imported only by its worker-local test and the
future authoring stage. Proofs under `scratchpad/flood2d_proofs/`.

## What landed

### c3 -- the Boundary Condition Lines author (`write_boundary_condition_lines`)

`services/workers/hecras2025/hecras_geometry_writer.py` gains
`write_boundary_condition_lines()`, the `BoundaryConditionLine` dataclass, and the
`perimeter_face_run()` selector (+90 LOC of test). It authors
`/Geometry/Boundary Condition Lines/` -- the five datasets whose schema ADR 0134
captured from the shipped pure-2D `BaldEagleDamBrk.g09.hdf`
(`pure2d_reference/g09_hdf_schema.json`):

- `Attributes` compound `[Name S32, SA-2D S16, Type S8, Length f4]`, one row per line;
- `External Faces` compound `[BC Line ID i4, Face Index i4, FP Start Index i4,
  FP End Index i4, Station Start f4, Station End f4]` -- each BC line mapped to the
  ordered perimeter faces it spans, with the per-face facepoint pair (in along-line
  order) + the cumulative station (ft);
- `Polyline Info (n,4)` / `Polyline Parts (n,2)` / `Polyline Points (P,2 f8)` --
  the along-line facepoint polyline, in HEC's `[pt start, pt count, part start,
  part count]` + `[pt start, pt count]` convention (decoded and verified against
  Muncie's own `Reference Lines/Internal Faces` layout, which uses the identical
  "line -> its faces + facepoints + stations" schema).

`perimeter_face_run()` selects a contiguous run of external (perimeter) faces --
external = a face whose `Faces Cell Indexes` reference a ghost cell
(index >= `cell_count`; Muncie has 374 such boundary faces) -- ordered around the
perimeter ring and centred by default on the LOWEST-elevation external facepoint
(the template's default inflow placement), with a compass-`edge` override.

**Validation (offline, both green):**

- **Synthetic unit gate** (`test_bc_lines_schema_and_stations_synthetic`): a
  5-facepoint / 4-external-face chain; authoring a 3-face BC line yields the exact
  schema dtypes, monotone stations `[0,10,20]->[10,20,30]`, `Length == 30.0`, and a
  round-tripping `Polyline Info/Parts/Points` (`sum(part counts) == len(Points)`).
- **Real-Muncie-perimeter gate** (`test_bc_lines_on_real_muncie_perimeter`): pick a
  10-face run on the REAL Muncie mesh's external faces; assert strictly-increasing
  stations, `Length == last Station End`, and that every referenced facepoint is a
  genuine Muncie perimeter facepoint (`FacePoints Is Perimeter`).

This is the same offline-round-trip discipline the ADR 0133 geometry writer landed
under: the writer's schema assembly is proven faithful before any live solve.

### c5 / `.bNN` -- the pure-2D forcing scaler, empirically discharged on the REAL reference

ADR 0134 CLAIMED (from a structural read) that the existing
`services/workers/hecras/deck_edit.py::scale_flow_hydrograph` drives the pure-2D
bare `Upstream Flow Hydrograph` stanza with no change. This wave PROVES it against
the actual shipped artifact (`test_scaler_drives_pure2d_bc_line_stanza_unchanged`,
in `services/workers/hecras/test_deck_edit.py`): scaling
`pure2d_reference/BaldEagleDamBrk.b06` by 2.0 correctly recognizes the bare
2D-BC-line header, doubles the flow ordinates (100 -> 200) while preserving the
times (0, 8760), and leaves the `Downstream Normal Depth` slope (`.001`)
byte-identical. **The pure-2D flow forcing is discharged empirically, not just
structurally.**

## Two sharp findings that re-scope the remaining links

### Finding 1 -- c1 byte-identity is STRUCTURALLY IMPOSSIBLE; c1 collapses into c2

ADR 0134 specified c1's validation as "re-dump Muncie's mesh via the general path
-> the writer -> byte/value-compare vs the 0133 self-check geometry." That
comparison CANNOT be byte-identical, and the reason is already in the record: the
2025 `MeshFactory.TryCreateMesh` regeneration of the Muncie mesh from its own
perimeter + seeds produces **11166 faces / 5776 facepoints vs the shipped 6.x
11164 / 5774** -- a +2 boundary-face tie-break (ADR 0132 STEP 2), even though the
cell centers are bit-identical (displacement 0.0 ft). A fresh mesh is DELIBERATELY
a different tessellation than the 6.x GUI's. Therefore the only meaningful
validation of a fresh topology is not a byte-compare -- it is whether the fresh
tessellation SOLVES (c2). **c1 (the topology dump) and c2 (does it solve) are not
separable; the dump has no independent pass/fail short of the solve.** This is a
correction to ADR 0134's link decomposition, not a new wall.

### Finding 2 -- the `.bNN` is authoritative, so c4's Event-Conditions author is COSMETIC

ADR 0109 established (flow-authority pin experiment) that `RasUnsteady` reads the
inflow from the `.bNN` ASCII boundary file, NOT from the plan HDF `/Event
Conditions` group -- scaling the HDF hydrograph left the 2D max WSE bit-identical;
only the `.bNN` moved the solve. It follows that a fresh pure-2D deck's
SOLVE-critical forcing authors are the `.xNN` (geometry preprocessor: SA
declaration + fake reach + Arrays Sizes) and the `.bNN` (discharged above) -- the
plan-HDF `/Event Conditions` author ADR 0134 listed in c4 is GUI-metadata the
solver ignores, so it is cosmetic for the solve and is deferred, not on the
critical path. This shrinks c4 to the `.xNN` author.

## Why the template does NOT land this wave (the precise STOP)

`hecras_flood_2d` stays correctly GATED. After this wave the remaining chain to its
acceptance (b) (a genuinely-new US AOI solved end-to-end) is:

- **(c1+c2, fused)** a FRESH 2D topology -- authored either by the `.NET`
  full-topology dump (OI-B, unbuilt C#) over real terrain, or by pure-Python
  topology surgery on a Muncie sub-rectangle -- serialized through the writer and
  actually SOLVING through production `RasGeomPreprocess` + `RasUnsteady`. This is
  the ONE untested risk: the fresh mesh's face normals / cell-face orientation /
  facepoint winding / rebuilt perimeter must satisfy the solver's consistency
  checks, which only a Muncie-topology round-trip (ADR 0133, dWSE 0.0) has
  exercised. It is unchanged in difficulty whether approached via C# (fresh mesh +
  terrain) or Python (Muncie subset): the depth is HEC's boundary-face/perimeter
  convention, not the language.
- **(c4, shrunk)** the `.xNN` author (pure-2D SA + fake reach + Arrays Sizes
  matching the fresh cell count, from the `x09` reference). Solve-gated: its
  validity is only confirmable by a completing solve, so it is specified here, not
  authored blind (the ADR 0133 doctrine).

Two additional this-machine gates on acceptance (a)/(b): the Muncie `Terrain.hdf`
is NO LONGER on disk (the 432 MB `Example_Projects_6_6` zip was deleted per the ADR
0134 hygiene rule) -- a real-terrain solve requires re-fetching it (or, for the
Python-subset probe, reusing Muncie's already-computed subgrid tables, terrain-free);
and the fresh-topology solve itself is the untested risk above. Landing the
template, its contract archetype, or the `.xNN`/plan composers without a completing
solve would be dead/dishonest per the ADR 0133/0134 charter. Registry stays
UNCHANGED; the template + contract remain GATED on the fused c1+c2.

## Consequences

- No server / tool / contract / registry change; registry byte-identical (git:
  this ADR + `hecras_geometry_writer.py` c3 additions + the two test additions).
  Coded-tools delta: **0** (no registered tool; the BC-lines writer is an internal
  worker component, like the ADR 0133 geometry writer). No template pin
  (`test_door_dissolution.EXPECTED_TEMPLATES`), category, or corpus change -- none
  is warranted until the template can run acceptance (b).
- New durable code: `hecras_geometry_writer.py` +246 LOC (the BC-lines author +
  `perimeter_face_run` + dtypes), `test_hecras_geometry_writer.py` +90 LOC (2 new
  gates), `test_deck_edit.py` +30 LOC (the pure-2D `.bNN` discharge). No image
  built; no flood.py / SFINCS seam touched (grep-verified: the diff carries no
  `flood.py`/`sfincs`/`publish_layer`/`postprocess` reference). No server
  import-graph change.
- Offline suite: my changed files are all under `services/workers/` and are NOT
  collected by the `server/tests/` suite, so the server baseline delta is **zero by
  construction**. NOTE (pre-existing, not this wave): on the current
  `refactor/engine-doors` checkout the `[p-r]` slice hits 4 COLLECTION errors from a
  missing `server/qgis-plugin/trid3nt/render/temporal.py` (untracked / absent --
  `git ls-files` empty), affecting `test_router_{glm,goes_animation,goes_archive,
  viirs_day_fire}.py`. This is a checkout-state issue independent of this landing.
- Worker-local suites green: `test_hecras_geometry_writer.py` 4 passed;
  `test_deck_edit.py` 22 passed (was 21; +1 the pure-2D discharge). Proofs:
  `scratchpad/flood2d_proofs/{c3_geometry_writer_tests,bnn_pure2d_scaler_discharge}.txt`.

## Open issues / ledger

- **ADR 0134 c3 (this ADR): DISCHARGED.** `write_boundary_condition_lines()` +
  `perimeter_face_run()` land with offline round-trip gates (synthetic + real
  Muncie perimeter).
- **ADR 0134 c5 / `.bNN` (this ADR): DISCHARGED EMPIRICALLY.** The existing flow
  scaler drives the shipped pure-2D `b06` bare stanza unchanged (proven, not just
  structurally read).
- **c1+c2 (FUSED, QUEUED -- the ONE untested risk).** A fresh 2D topology solving
  end-to-end through the writer + production 6.x engines. c1 has no independent
  byte-identity gate (Finding 1); validation IS the solve. Recommended first probe:
  pure-Python topology surgery on a Muncie sub-rectangle (reuses real
  HEC-convention arrays + real subgrid tables; terrain-free, C#-free) -- if it
  solves, it discharges c2 AND exercises c3 end-to-end; if it walls, the named
  solver error is the precise next STOP. The C# `AuthorMesh` full-topology dump
  (OI-B) + terrain then produces REAL-terrain fresh meshes on an already-proven
  solve path.
- **c4 (SHRUNK to the `.xNN` author, QUEUED).** Pure-2D SA + fake reach + Arrays
  Sizes from `x09`. Solve-gated (author + confirm by a completing solve, not
  blind). The plan-HDF `/Event Conditions` author is COSMETIC (Finding 2) and
  deferred.
- **The template + contract + worker image (QUEUED).** `hecras_flood_2d` +
  `HECRAS_ARCHETYPES` literal + a formalized authoring worker stage (OI-B
  Dockerfile) land TOGETHER once c1+c2 is proven, with BOTH acceptances (the Muncie
  self-check through the template + a genuinely-new small US AOI end-to-end).
- **OI-D (precipitation -- named residual, carried).** A plan-HDF Meteorology +
  HEC-DSS grid author (Atlas-14 seam preferred); no shipped `.bNN`/HDF precip
  reference in this distribution.
- Carries ADR 0132 OI-3 (the 2025 `ras` build is `-dev`/schema-unstable;
  re-characterize per version bump) and OI-4 (virtual-cell SanityCheck
  NotSupportedException).
