# ADR 0139 -- HEC-RAS flood_2d: the C# AuthorMesh worker + the pure-2D DECK COMPOSER land, and BOTH acceptances solve end-to-end -- a genuinely-new US AOI is authored + flooded (OI-FT2 discharged to the template-promotion step)

Status: accepted (2026-08-05)
Follows: ADR 0138 (the 2D-BC-line `/Event Conditions` schema decoded + re-authored;
the fresh CARVED topology WETS end-to-end; `hecras_flood_2d` declared "now GO",
gated on BOTH acceptances -- a Muncie self-check + a genuinely-new small US AOI --
each showing wet cells where terrain is low + a monotone flow-scale delta, with the
remaining refinement being the C# `AuthorMesh` full-topology dump (c1) and whether a
fresh C#-authored topology + terrain-sampled tables SOLVE (c2, the ONE untested
risk the chain carried since ADR 0134/0135)), ADR 0137 (the Chippewa clean pure-2D
fake-reach deck), ADR 0136 (the fresh carve solves), ADR 0133/0135 (the geometry +
BC-lines writer), ADR 0132 (the Muncie transplant + `ComputeMuncie.cs`), ADR 0129
(the substituted Linux natives -- `createterrain` + `ComputeFrom` headless).

This wave lands the authoring worker (OI-B) and the deck composer, and RUNS BOTH
acceptances to completion. It discharges the last two flagged risks -- the C#
full-topology dump (c1) and the fresh-C#-topology SOLVE (c2) -- and floods a
genuinely-new US AOI (a real fetched DEM) authored ENTIRELY by the C# path. The
registered `hecras_flood_2d` template + its worker image is the remaining PROMOTION
step (local-first: the capability is nailed as a direct-call chain here).

Additive worker-component wave: NO server / tool / contract / registry change
(registry byte-identical by construction; coded-tools delta 0 -- no template
registered yet). New durable code under `services/workers/hecras2025/`; proofs +
renders under `scratchpad/flood2d_landing_proofs/`.

## What landed

### 1. The pure-2D DECK COMPOSER (`freshtopo/hecras_deck2d.py`)

`compose_pure2d_deck(rundir, mesh, tables, ...)` formalizes the ADR 0136/0137/0138
deck assembly into ONE reusable, mesh-SOURCE-agnostic composer: a `Mesh2D` +
`SubgridTables` (from EITHER the carve OR the C# AuthorMesh path) + the flow forcing
-> the complete solvable deck (plan HDF with the 2D area + Inflow/DS BC lines +
2D-BC `/Event Conditions` forcing; the Chippewa clean `.x04`/`.b04`). The Inflow line
defaults to the lowest-elevation perimeter run, DS to a distinct outlet edge (the
ADR 0138 inflow+outlet drainage physics; an override for both). `build_chippewa_
wetting_deck` now delegates to it (byte-identical `.x04`/`.b04`, identical solve).
5 offline gates (`test_hecras_deck2d.py`, green).

### 2. The C# `AuthorMesh` worker stage (OI-B link c1 -- DISCHARGED)

`subst/crux/transplant/AuthorMesh.cs` generalizes `ComputeMuncie.cs` into the
authoring worker's core: from a perimeter + cell-center seeds it regenerates a mesh
(`MeshFactory.TryCreateMesh`), computes the subgrid tables over real terrain
(`MeshPropertyTables.ComputeFrom`), and dumps the FULL topology -- every
`hecras_geometry_writer.Mesh2D` array. The 2025 beta `Mesh` exposes it directly
(re-characterized via `ApiProbe`): `Face{cellA,cellB,fpA,fpB}`, `FacePoint{Point,
Faces}`, `Cell{Faces}`, `CellCenters`, `Perimeter`, `ComputeFaceNormals()`.

**Validated (terrain-free, `validate_authormesh_topology.py`)** by re-dumping the
Muncie mesh from its own perimeter + 5391 seeds and comparing to the shipped 6.x
geometry: real cells **5391 EXACT**; faces 11166 / facepoints 5776 = the shipped
11164/5774 **+2 boundary tie-break** (the ADR 0132 fingerprint); cell-center
**bijection 5391/5391 at 0.000000 ft** (bit-identical); every face's normal
perpendicular + unit; rot_-90(fpB-fpA)==normal for 100% of faces; interior normals
point cellA->cellB (100%), boundary normals outward (100%); ragged consistency. ADR
0135 Finding 1 (byte-identity impossible; the c1 gate is the bijection + structural
consistency) is satisfied. The Dockerfile (`Dockerfile.authoring`, FROM the ADR 0129
subst image + the ~13 KB `authormesh.dll`) + `authoring_entrypoint.sh` formalize the
`createterrain -> AuthorMesh` stage; the deck-compose + solve are the existing host +
`hecras:latest` stages (two payloads: substituted-GDAL authoring vs the 6.6 solver).

### 3. The adapter (`freshtopo/authormesh_to_mesh2d.py`)

Reconstructs `Mesh2D`+`SubgridTables` from an AuthorMesh dump and runs the SAME
proven `carve` convention rebuild (ghost synthesis on the `cellB == -1` external
faces -- 374 on the full Muncie mesh, matching its 374 shipped ghosts -- Cells Face +
Orientation, FacePoints CCW adjacency, the walked perimeter). Because the dump is
already in HEC conventions (validated above), the adapter is a faithful lift.

## Both acceptances -- solved end-to-end (the arc completes)

### Acceptance (a) -- the Muncie carve through the composer

The ADR 0138 carve deck rebuilt through `compose_pure2d_deck` and solved on the
production 6.6 `RasGeomPreprocess` + `RasUnsteady`:

| run | wet cells | max depth ft | max WSE ft | vol err % | flux in / out |
| --- | --- | --- | --- | --- | --- |
| x1.0 (peak 2000) | **1906** | 12.22 | 946.94 | 0.011 | 141176 / 141011 |
| x1.5 (peak 3000) | **1987** | 16.61 | 948.39 | 0.010 | 208368 / 208140 |

Reproduces ADR 0138 (1906 / 1986 wet) within 1 cell; the monotone x1.5 delta
(+81 cells, +4.4 ft depth, +1.5 ft WSE). Render: `accept_a_muncie_carve.png`.

### Acceptance (b) -- a GENUINELY-NEW US AOI (c2 -- the last risk -- DISCHARGED)

A real fetched DEM (AWS Terrarium elevation tiles) over the lower Wabash River
floodplain near New Harmony, Indiana (~3.8 km, elevations 353-512 ft after m->ftUS,
reprojected to NAD83 Indiana West ftUS so the deck is unit-consistent with the US
Customary solver). A fresh rectangle perimeter + a 63x63 grid of seeds at 200 ft ->
`AuthorMesh` (3969 cells, `ComputeFrom` Result=True over the real terrain) -> the
adapter -> `compose_pure2d_deck` -> the production 6.6 engines. NOTHING Muncie: the
tessellation AND the subgrid tables are freshly computed.

| run | wet cells | max depth ft | vol err % | flux in / out | wetting |
| --- | --- | --- | --- | --- | --- |
| peak 3000 | 673 / 3969 | 8.51 | 0.0004 | 208368 / 206536 | partial |
| peak 8000 | **797 / 3969** | 10.89 | **0.000003** | 544325 / 541568 | partial |

**Physical + sane**: partial wetting concentrated in the LOW terrain (wet-cell beds
353-378 ft, median 361; dry-cell median 432; the flood traces the valley drainage
corridor -- `accept_b_wabash_aoi.png`), balanced draining flux, near-zero volume
error, a monotone +124-cell / +2.4-ft delta. **A fresh C#-authored topology with
terrain-sampled subgrid tables is accepted + solved by the production engines --
c2, the ONE untested risk the chain carried since ADR 0134, is discharged.**

### The per-AOI initial-condition fix (a real bug the low AOI exposed)

The Chippewa `.b02` carries a `679` ft Initial-Conditions profile stage (Chippewa
Creek's reference) that seeds the 2D area's INITIAL water surface. Harmless where
terrain sits above it (Muncie ~925 ft -> dry initial, which is why ADR 0136-0138 never
saw it), it spuriously flooded the low Wabash terrain to a flat 679 ft pool
regardless of inflow. `patch_chippewa_bnn(initial_stage=...)` now rewrites it, and
`compose_pure2d_deck` sets it to `terrain_min - 10 ft` so any AOI starts DRY.
Acceptance (a) is byte-unchanged by the fix (679 and 915 both sit below Muncie's bed).

## Why the template is NOT registered THIS wave (the honest promotion boundary)

The capability is NAILED as a direct-call chain (both acceptances solve
end-to-end). Per the local-first doctrine (prototype direct-call, THEN promote), the
registered `hecras_flood_2d` server template + its worker IMAGE is the remaining
PROMOTION step: it needs the authoring image BUILT + a `run_solver`/`LOCAL_SOLVER_
SPEC_REGISTRY` spec that orchestrates fetch_dem -> perimeter/seeds -> the authoring
stage -> the compose+solve stage -> postprocess -> the depth COG + mesh preview +
the inflow chart (the `hecras_riverine_flood` pattern), plus the corpus + retrieval
proof + the archetype literal + the template pins. Registering it before that
backend is built + deployed would be a DEAD tool (the ADR 0133-0138 no-template-
that-cannot-run doctrine). So the registry stays UNCHANGED; the template lands with
its worker image once the promotion wave wires + deploys it.

## Consequences

- No server / tool / contract / registry change; registry byte-identical.
  Coded-tools delta **0**. New durable code under `services/workers/hecras2025/`:
  `freshtopo/hecras_deck2d.py` (+`test_hecras_deck2d.py`, 5 gates),
  `freshtopo/authormesh_to_mesh2d.py`, `freshtopo/build_authormesh_deck.py`,
  `transplant/AuthorMesh.cs` (+`.csproj`), `transplant/validate_authormesh_topology.py`,
  `Dockerfile.authoring` + `authoring_entrypoint.sh`. Edited:
  `freshtopo/hecras_pure2d_deck.py` (the `initial_stage` param),
  `freshtopo/build_chippewa_wetting_deck.py` (delegates to the composer).
- No `flood.py` / SFINCS / `publish_layer` / registry reference (grep-verified).
  No server import-graph change; server offline baseline delta **zero by
  construction** (all files under `services/workers/`, not collected by
  `server/tests/` -- the documented failure set is unchanged).
- Image hygiene: NO new image built this wave (the authoring Dockerfile is authored;
  its stages -- `createterrain` + `AuthorMesh` -- are validated in the EXISTING
  `hecras2025:subst-exp`, 1.85 GB / 0.54 GB compressed; the layer it adds is the
  ~13 KB `authormesh.dll` + entrypoint). C# built with throwaway `--rm` on the
  pre-existing `mcr.microsoft.com/dotnet/sdk:9.0`; solves on `trid3nt-local/hecras:
  latest` (2.2 GB). The proprietary beta DLLs + `.NET` build outputs stay gitignored
  (`transplant/.gitignore`: `dll/`, `out_*/`). The fetched DEM + terrains +
  extractions live in scratchpad (deletable). Durable in-repo footprint ~35 KB.

## Open issues / ledger

- **OI-B (this ADR): c1 DISCHARGED.** `AuthorMesh` is built + its full-topology dump
  validated bit-identically vs shipped Muncie. The Dockerfile + entrypoint formalize
  the stage; the image BUILD is folded into the template-promotion wave.
- **c2 (this ADR): DISCHARGED.** A fresh C#-authored topology + terrain-sampled
  tables solve + wet physically on a genuinely-new US AOI.
- **OI-FT2 (the template -- PROMOTION step, the one remaining link).** Register
  `hecras_flood_2d` (engine=hecras, tier=template) + the `HECRAS_ARCHETYPES` literal
  + corpus + model-free retrieval proof + the pins (catalog registry, templates
  roster, categories), backed by the authoring worker IMAGE + a `run_solver` spec
  that orchestrates fetch_dem -> perimeter/seeds -> authoring -> compose -> solve ->
  postprocess (depth COG + mesh preview + inflow chart). synthetic_inputs lineage:
  geometry = authored-transplant-path (tables 0.99988 / writer dWSE 0.0 / topology
  0.0021% / forcing wets-at-baseline-WSE); terrain basis = fetched `fetch_dem`;
  forcing basis. Fidelity line: refinement-grade production 6.x solver on 2025-
  authored geometry, transplant-path validated end-to-end; screening-grade label
  until broader V&V; off-scope speed -> `sfincs_flood`; precip -> OI-D.
- **Per-AOI tuning (named, not a wall).** The inflow magnitude + inlet/outlet edge
  placement are per-AOI knobs (the ADR 0138 lesson): the composer defaults (lowest-
  elevation inflow, a distinct low outlet) + a granularity-gated resolution estimate
  from cell count are the template's autoscaler suggestion + override.
- Carries ADR 0134 OI-D (precipitation Meteorology+DSS residual) + ADR 0132 OI-3
  (2025 `ras` `-dev` schema-unstable; re-characterize per version bump) + OI-4
  (virtual-cell SanityCheck).
