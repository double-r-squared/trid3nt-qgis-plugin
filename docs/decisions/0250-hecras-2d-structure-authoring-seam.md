# ADR 0250 - HEC-RAS 2025 2D structure-authoring seam: the StructureLayer authoring path is LIVE and the deck prepares+solves, but the beta wires ONLY Culverts into the compute -- authored weirs/gates/pumps are silently INERT (StructureLayer->engine Weir/Gate/Pump bridge is unimplemented). ADR 0249's "bounded build front" REOPEN is CORRECTED: the weir/gate/pump rows STAY STOP on this beta; the one drivable 2D structure is the Culvert (CulvertBarrelLayer), which is the precise follow-on scope.

Date: 2026-08-13
Status: accepted
Continues: ADR 0249 (the HEC-RAS four-cluster adjudication that REOPENED
`pump_station_trigger_and_ramp_control` + `combined_1d2d_pump_station_coupling` as a
"bounded 2025-engine build front") and ADR 0207/0209/0210 (the 2025 managed engine:
prepare+solve 2D on Linux; the `scripts/sandbox/hecras/managed_solve/Driver.cs` authoring
driver). This ADR executed ADR 0249's Recipe (the constructive unblock) and reports what
the engine actually does with an authored structure.

## What was built (prototype, LANDED)

`Driver.cs` gained a `structdemo` mode (ADR 0249 Recipe step 1): it authors an inflow
channel (a `StructChannel : InOutPlanarParams`, 60 x 300 m, 6 x 30 uniform 10 m cells,
120 units ramped inflow at the top wall, 1.0 m tailwater stage at the bottom, flat terrain)
and -- WITH the weir flag -- constructs a `Ras.Hydraulics.Structure` and adds it to the
geometry's `StructureLayer`:

- centerline `Polyline` (0,150)->(60,150) crossing the flow path,
- `StationElevationProfile` crest at 2.0 m (above the ~1.5 m tailwater WSE, so it SHOULD
  pond upstream and block downstream),
- `WeirWidth=3`, `UpstreamSlope=DownstreamSlope=1`,
- `ID.Type=StructureType.Connection`, `ID.ConnectionName="Weir1"`,
- `UpstreamConnection`/`DownstreamConnection` = `{Type=FlowArea, ConnectedElementName="Base Mesh"}`.

The authoring plumbing WORKS end-to-end, verified live through
`hecras2025-authoring:latest` (id afb76f3ccd00):

1. `StructureLayer.Add(structure)` + `geometry.Save()` persists the structure into the
   geometry HDF group `/Geometry/Structures` (`Attributes` + `Centerline (2,2)` +
   `Station Elevation (2,2)` datasets round-trip byte-correct; the beta terrain-dir Save
   bug is caught exactly as in `AuthorWithPrecip`, then the project is re-opened via
   `new Project(<.ras>)` and the geometry re-saved).
2. `ras prepare -s <.ras> -o <r2r>` INGESTS the authored structure and completes
   ("Preparing Plan completed") -- no rejection, no error.
3. `ras solve <r2r> <out> --solver CPU` completes ("Computations completed") -- no
   rejection, no error.

So the ADR 0249 premise "the authored deck is accepted headless" HOLDS: the 2025 engine
reads, prepares, and solves a project whose geometry carries a `Ras.Hydraulics.Structure`.

## The finding that CORRECTS ADR 0249 (the STOP)

The A/B is DISCRIMINATING and its verdict is NEGATIVE: **the authored weir has ZERO
hydraulic effect.** Same domain/inflow/tailwater WITH vs WITHOUT the structure:

| case | struct | US mean depth | DS mean depth | US-DS | US max | DS max |
|---|---|---|---|---|---|---|
| baseline | absent | 1.458 | 1.212 | 0.246 | 1.560 | 1.354 |
| weir crest 2.0 m | present | 1.458 | 1.212 | 0.246 | 1.560 | 1.354 |

- `max|B - A|` over the final-step per-cell depth field = **0.00000000 m** (bit-identical).
- The 6 mesh faces lying on the crest line (y=150) have `Face Minimum Elevation` = **0.0 m
  in BOTH cases** (identical arrays) -- prepare NEVER raised the faces to the 2.0 m crest.

A weir with crest 2.0 m in a channel whose WSE is ~1.5 m MUST pond the upstream side up and
over the crest and cut the downstream flow. It does nothing. The `must-measurably-move-
water` gate (ADR 0143) FAILS. Proof figure:
`docs/proof/templates/hecras_structure_2d_seam_probe_ab.png`.

### Root cause (decompiled, IN-IMAGE, both `Ras.Core.dll` + `Ras.Engine.dll`)

The compute-setup bridge that turns a geometry structure into an engine compute structure
is `Ras.Layers.Geometry.InitializeComputeDriver` / `InitializeSolver`. It does exactly two
things: `InitializeDriver_Culverts(driver)` then
`driver.HydraulicStructures.IdentifyStructureCellsAndFaces(GlobalMesh)`.

- `InitializeDriver_Culverts` is the ONLY layer->engine converter. It reads
  `CulvertBarrelLayer`, does `new Culvert(barrel.Name, barrel.Polyline)` per barrel, and
  `computeDriver.HydraulicStructures.AddHydraulicStructure(culvert)`.
- There is NO code path that reads `StructureLayer` (the weir/gate authoring surface) to
  build engine structures. A full-assembly grep for `new Weir(` / `new Gate(` / `new Pump(`
  across `Ras.Core.dll`, `Ras.Engine.dll`, `ras.dll`, `Ras.Engine.Interop.dll`,
  `Ras.Migration.dll` returns **ZERO** hits. The only `new <HydraulicStructure-subclass>(`
  anywhere in the shipped beta is the single `new Culvert(` above.
- Consequence: `IdentifyStructureCellsAndFaces` (the engine-side face-pairing ADR 0249
  Finding 2 correctly identified as headless) runs on a collection that only ever contains
  Culverts. It IS capable of Weir/Gate pairing (`HydraulicStructure.
  IdentifyStructureCellsAndFacesFromLine` snaps `mesh.FacesAlongPolyline(CenterLine)` for
  `HSType.Weir|Gate`), and `Weir.Prepare`/`WeirCompute`/`WeirFlowState` all ship -- but
  nothing ever CONSTRUCTS an engine `Weir` from an authored `StructureLayer` structure, so
  the whole weir/gate/pump kernel is unreachable from the authoring path. The authored
  `Structure` persists to HDF and is simply never wired into the solve.

ADR 0249 Finding 2 asserted the authoring surface was "wired, not vestigial" because
`Ras.Layers.Geometry` constructs+Saves the `StructureLayer`. That is true for GEOMETRY
persistence (a savable layer exists) but FALSE for COMPUTE: the beta's
`InitializeComputeDriver` does not consume `StructureLayer`. The reopen assumed
"authoring surface present + engine kernels present + engine-side pairing present" implied
an end-to-end path; the missing middle link (layer->engine construction for
weir/gate/pump) makes the path dead. This ADR supplies the empirical A/B ADR 0249 lacked.

## Per-row verdict (CORRECTS ADR 0249's REOPEN)

| row | ADR 0249 | ADR 0250 (this) | grounds |
|---|---|---|---|
| `pump_station_trigger_and_ramp_control` | REOPEN (build front) | **STOP (beta gap)** | No `StructureLayer`/`Pumps`->engine wire; authored structures are inert. `Structures.Pumps.PumpCompute` ships but is unreachable from the authoring path (`new Pump(` exists nowhere). |
| `combined_1d2d_pump_station_coupling` | REOPEN (build front) | **STOP (beta gap)** | Same missing bridge; the 2D SA/2D pump is not constructible from the geometry in this beta. |
| weir / gate 2D-structure rows (e.g. board `### Structures` weir rows) | (implied unblocked) | **STOP (beta gap)** | `new Weir(`/`new Gate(` exist nowhere; the crest is never enforced onto the faces. |

The ONE 2D structure that IS drivable end-to-end in this beta is the **Culvert**, via
`CulvertBarrelLayer` (+ `BarrelPropertiesLayer` + `OpeningPropertiesLayer` associations +
a recognized Chart/Scale; `Custom`/`None` opening types hard-fail per
`InitializeDriver_Culverts`). That -- not a weir -- is the precise follow-on scope.

## Decision

**Prototype LANDED (Driver `structdemo` mode + the A/B evidence); 0 new registered tools,
0 new templates, 0 image rebuild, 0 parser bump, 0 productionization.** No composer knob,
no worker leg, no registry change -- productionizing a structure surface would be
fabricating a capability the engine does not deliver (the honesty floor: the A/B proves the
weir moves no water). Registry stays **252**; `EXPECTED_TEMPLATES` unchanged.

The load-bearing output is the corrected adjudication above + the reusable prototype
harness (`structdemo`) that will re-prove the seam THE DAY the beta ships the
`StructureLayer`->engine weir/gate/pump bridge (or a newer engine build does), and that
already exercises the exact authoring API (`Ras.Hydraulics.Structure` +
`StationElevationProfile` + `StructureConnection` + `geometry.StructureLayer.Add`).

## Precise follow-on scope (for whoever revisits)

1. **Re-probe on a newer engine build.** The gap is a BUILD-COMPLETENESS gap
   (`InitializeDriver_Weirs`/`_Gates`/`_Pumps` simply not written yet), not an
   architectural wall. Decompiler flagged the image `ras` at `0.1.0.2965-dev` and the
   ilspy tool itself is behind (11.0 available). Re-run `structdemo` against any later
   `hecras2025-authoring` image and re-check `max|B-A|` + crest-line `Face Minimum
   Elevation`; a nonzero delta = the bridge landed.
2. **Culvert path (the drivable one).** Author a `CulvertBarrelLayer` barrel +
   `BarrelPropertiesLayer` + `OpeningPropertiesLayer` with a recognized Chart/Scale, over
   an embankment terrain ridge (a culvert only matters if there IS an embankment for it to
   pass flow under), then A/B present-vs-absent. This is a distinct, heavier authoring
   surface (multiple associated layers + chart/scale parametrization) -- its own job.
3. **US validation case + productionization** remain gated behind (1) or (2) proving a
   real hydraulic effect first (US-cases/paper-first + must-move-water gates).

## Consequences

- Coded-tools metric: 0 tools added, 0 LOC in the server/worker/registry (the only code
  landed is the sandbox `Driver.cs` `structdemo` mode + `StructChannel`, ~90 C# LOC, which
  is prototype/reproduction, not registered product). Registry 252 -> 252.
- Evidence (in-image, live): authored `/Geometry/Structures` round-trips; `ras prepare` +
  `ras solve --solver CPU` both COMPLETE on the structure-carrying deck; final-step depth
  field bit-identical to baseline (`max|B-A|=0.0`); crest-line `Face Minimum Elevation`
  unchanged (base==weir, all 0.0); `new Weir(`/`new Gate(`/`new Pump(` absent from every
  shipped assembly; `InitializeComputeDriver` wires Culverts only.
- Proof: `docs/proof/templates/hecras_structure_2d_seam_probe_ab.png` (A baseline / B weir /
  B-A=0, synthetic seam-probe -- NOT a validation case; no real-site render because the
  seam delivers no physics to render).
- Board: `pump_station_trigger_and_ramp_control` + `combined_1d2d_pump_station_coupling`
  flip REOPEN -> STOP (beta gap), pointing at this ADR + the Culvert follow-on.
- Offline registry spot slices GREEN: `test_catalog_surfacing` (registry 252),
  `test_door_dissolution` (`EXPECTED_TEMPLATES` unchanged).

## Reproducibility

Build: `scripts/sandbox/hecras/managed_solve/REPRODUCE.md` (the `structdemo` mode rides the
same SDK-image build + authoring-image run). Author + A/B:
`dotnet synthdrv.dll structdemo /probe/struct_base 0` and
`dotnet synthdrv.dll structdemo /probe/struct_weir 1 2.0`, then `ras prepare` + `ras solve`
on each, then compare `Cell Depth` (final step) + crest-line `Face Minimum Elevation`.
Decompile: `local/ilspy:9`, `DOTNET_ROLL_FORWARD=Major`; the layer->engine bridge is
`Ras.Layers.Geometry.InitializeComputeDriver` / `InitializeDriver_Culverts` in
`Ras.Core.dll`.
