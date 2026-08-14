# ADR 0249 - HEC-RAS coverage wave: the 2025 engine is 2D-only (all 1D rows STAY STOP) but its headless structure solver + engine-side face-pairing SUPERSEDE the ADR 0171 Windows frontier (Structures + 2D-connection rows REOPEN as a bounded 2025-engine build front)

Date: 2026-08-13
Status: accepted
Continues: ADR 0157 (the "1D steady signed" board correction), ADR 0170 (1D-steady /
RasSteady reference-fixture STOP), ADR 0171 (structure-authoring RASMapper face-pairing
STOP), ADR 0207/0209/0210 (the 2025 managed engine: prepare+solve 2D on Linux, RoG
productionized). This wave adjudicated the four open HEC-RAS clusters on the board -
Structures, 1D Steady, 1D Unsteady, Breach - knob-or-STOP per row, against the 2025
managed engine that ADRs 0170/0171 predate.

## Why this wave exists

ADRs 0170 (1D steady) and 0171 (structures) are dated 2026-08-07 and STOPped every open
row in these clusters on the **6.6 Fortran path's reference-fixture / RASMapper Windows
face-pairing frontier**. The 2025 managed engine landed TWO DAYS LATER (ADR 0207,
2026-08-09) and dissolved the 6.6 Windows-preprocessing wall for 2D rain-on-grid. The
mission: re-adjudicate these clusters against the 2025 engine's ACTUAL deck surface,
verified IN-IMAGE (never from docs), before repeating a stale STOP.

## In-image findings (authoring image `trid3nt-local/hecras2025-authoring:latest`, id
afb76f3ccd00; assemblies decompiled with `local/ilspy:9`, DOTNET_ROLL_FORWARD=Major;
solver image `trid3nt-local/hecras:latest` for the 6.6 executors)

### Finding 1 - the 2025 managed engine is 2D-ONLY. No 1D solver exists.

`Ras.Engine.dll` (the solver assembly - the thing that COMPUTES) type list carries ONLY
the 2D solver stack: `ComputeDriver`, `Solver`, `SolverControl`, `SolverDWE`,
`SolverExpSWE`, `SolverImp`, `SolverImpSWE`, `Native.GPUSolver`. A full-list grep for
`SaintVenant | StandardStep | SteadyFlow | Unsteady1D | Snet | CrossSection | RiverReach |
OneDimensional | Solver1D` returns ZERO classes. The `ras` CLI verb surface is
`prepare / mesh / solve` (+ createterrain/map) - NO steady verb, NO 1D verb, NO profile
verb. The 2025 rewrite is a ground-up 2D shallow-water/diffusion-wave cell solver; 1D
(standard-step energy + Saint-Venant network) is NOT in it. (The `CrossSection` /
`RiverReach` string hits in `Ras.Core.dll` are the migration/render DATA model that reads
a legacy 6.x deck for display - not a solver.)

Consequence: the 2025 engine offers NO alternative route for ANY 1D-framed row. The only
1D executors on Linux remain the 6.6 Fortran `RasSteady` + `RasUnsteady`
(`/opt/hecras/bin/` in `hecras:latest`, confirmed present), which ADR 0170 established are
reference-fixture-gated for headless PREPARATION (a steady/1D-network-typed plan HDF that
exists nowhere and cannot be authored blind). That STOP recipe is UNCHANGED and remains
the only 1D unblock.

### Finding 2 - the ADR 0171 STOP is SUPERSEDED: the 2025 engine solves 2D hydraulic structures headless AND computes the cell-face pairing itself.

ADR 0171 STOPped structure authoring because the SA/2D-connection **cell-face pairing** is
the RASMapper-authored (Windows-DLL) geometry with no shipped reference to diff. The 2025
engine dissolves exactly that wall:

- `Ras.Engine.dll` carries a full 2D hydraulic-structures COMPUTE stack under
  `Ras.Engine.Structures.*`: `HydraulicStructure`, `HydraulicStructureCollection`,
  `DynamicStructRatingTable`; `Weirs.WeirCompute`/`WeirFlowState`; `Gates.GateCompute`/
  `GateState`; a complete `Culverts.*` family (`CulvertFlowSolver`,
  `CulvertEnergyGradeSolver`, `OutletControlSolver`, `SuperCriticalSolver`, `CulvertMath`,
  ...); AND `Pumps.PumpCompute`/`PumpGroup`/`PumpState`. (A flat binary grep for
  "PumpStation" returns 0 because the classes live under `Structures.Pumps.*` - the
  namespaced type list is authoritative.)
- `HydraulicStructureCollection.IdentifyStructureCellsAndFaces(Mesh globalMesh)` -
  **the ENGINE computes the structure-to-cell/face pairing from the global mesh at
  prepare/solve time.** This IS the ADR 0171 M3 frontier, now engine-side and headless,
  exactly as `ras prepare` computes 2D subgrid property tables headless (ADR 0207).
- `Ras.Hydraulics.Structure` (the authorable element of `Ras.Layers.StructureLayer`,
  HDF5 group `/Geometry/Structures`) exposes a CALLER-AUTHORABLE surface: `Polyline`
  (centerline), `StationElevationProfile StationElevation` (crest profile), `WeirWidth`,
  `UpstreamSlope`/`DownstreamSlope`, `Upstream/DownstreamConnection`
  (`StructureConnection` = `Type` + `RiverName`/`ReachName`/`RiverStation` +
  `ConnectedElementName`), `LWVelocityInto2D`. `StructureConnection` is DESCRIPTIVE -
  it names the connected element; it does NOT carry caller-supplied cell-face pairing
  tables (the engine derives them). Companion authoring layers: `GateGroupLayer`,
  `GateOpeningLayer`, `CulvertBarrelLayer`.
- `Ras.Layers.Geometry` constructs `StructureLayer` + `CulvertBarrelLayer` +
  `GateGroupLayer` in its ctor, `Save`s them to the geometry HDF, and gates them through
  `CanCompute()`/`PrepareForCompute()`. So every synthetic 2025 project's Geometry ALREADY
  holds a live, savable StructureLayer - the authoring path is wired, not vestigial.

Net: for 2D (SA/2D-connection / internal-structure-on-mesh) weirs, gates, culverts, and
pumps, the reference-fixture / face-pairing wall of ADR 0171 is GONE. What remains is
authoring plumbing (construct a `Ras.Hydraulics.Structure`, add it to the project's
`StructureLayer`, prove `prepare`+`solve` computes it), directly analogous to the ADR
0207->0209 RoG arc that turned the 2D-solve probe into the productionized `hecras_flood_2d`
RoG surface.

### Finding 3 - NO Breach type in the 2025 model.

A full-list grep for "breach" across `Ras.Core.dll` (the 2025 domain model) returns ZERO
types. The 2025 Synthetics framework's `DamBreakParams` (decompiled) is the idealized
Stoker/Ritter 2D dam-break RIEMANN benchmark - a step initial-stage
(`InitialStageFunction`: WaterSurfaceLeft left of centre, WaterSurfaceRight right) on a
flat flume, WetTest/DryTest. It is NOT a HEC-RAS breach: no dam structure, no breach
parameters (bottom width / side slope / formation time), no progressive growth, no
reservoir geometry. So the 2025 engine adds NOTHING to the Breach cluster's progressive-
failure physics. The only breach capability on Linux remains the 6.6 lateral-structure
breach toggle already LANDED as `hecras_levee_breach` (ADR 0125).

## Per-row adjudication

### Structures (Bridges / Culverts / Gates / Pumps)

| row | verdict | grounds |
|---|---|---|
| `multi_opening_flow_split_bridge_culvert_relief` | **STOP** | 1D STEADY standard-step energy balance across bridge+culvert+relief openings (Beaver Creek). No 1D solver in 2025; 6.6 RasSteady reference-fixture-gated (ADR 0170). |
| `advanced_inline_structure_multi_component` | **STOP** | 1D inline structure discharge rating (weir+gate+culverts+outlet, Beaver Creek Kentwood). Same 1D-solver gap. |
| `pump_station_trigger_and_ramp_control` | **REOPEN (2025 build front)** | Its physics is the 2D SA/2D interior-drainage pump (companion of the 2D row `combined_1d2d_pump_station_coupling`). The 2025 engine HAS `Structures.Pumps.PumpCompute`/`PumpGroup`/`PumpState` + engine-side face-pairing + a `GateGroupLayer`/`StructureLayer` authoring surface. ADR 0171 "no pump HDF reference / Muncie has none" STOP is superseded - the pump is authorable in managed code + engine-solved, no diff reference needed. |
| `gate_pump_user_defined_operation_rules` | **STOP (partial reopen)** | The base gate/pump STRUCTURE is now 2D-authorable+solvable (Finding 2), but the ROW is the rule-scripting layer (control variables: remote stage/flow, time-of-day, cumulative history). No user-rule/operation-controller symbol surfaced in the 2025 authoring model; rule scripting is a distinct L-effort engine-capability leg on top of the reopened structure front. STOP the rules layer; the plain gate/pump structure rides the reopened front. |

### 1D Steady Flow

| row | verdict | grounds |
|---|---|---|
| `steady_profile_calibration_to_high_water_marks` | **STOP** | Multi-profile standard-step + Manning's-n calibration to HWMs (Merced/Yosemite). No 1D solver in 2025; 6.6 RasSteady reference-fixture-gated (ADR 0170 recipe: GUI-seeded steady example project, then the `.f0N` reparameterizer). |
| `mixed_regime_multi_profile_solve` | **STOP (unchanged)** | ADR 0157/0170 STOP stands - mixed sub/supercritical steady solve, same 1D-solver gap. |
| `steady_floodway_encroachment_delineation` | **STOP** | FEMA regulatory-floodway encroachment (Beaver Creek Kentwood). Standard-step steady; same gap. Encroachment IS in the 6.6 RasSteady binary (`Overbank Encroachment Method`, ADR 0170) but gated on the same steady reference deck. |

### 1D Unsteady Flow

| row | verdict | grounds |
|---|---|---|
| `modified_puls_vs_full_unsteady_reconciliation` | **STOP** | 1D unsteady Saint-Venant network + Modified-Puls hydrologic-routing fallback (Bald Eagle). No 1D solver in 2025; 6.6 needs a 1D-network reference deck (ADR 0170 step 5). |
| `storage_area_network_flow_reversal` | **STOP (unchanged)** | ADR 0157/0170 STOP stands - synthetic Diamond River 1D network (junctions + storage areas + lateral weirs), non-US, same 1D-network gap. |
| `unsteady_hydrograph_optimization_calibration` | **STOP** | 1D unsteady + HEC's built-in BC/n-value OPTIMIZATION loop. Same 1D gap; the optimizer is a further 1D-engine feature on top. |

### Breach (Dam / Levee Failure)

| row | verdict | grounds |
|---|---|---|
| `breach_parameter_regression_ensemble` | **STOP (playground + HOLD)** | The 5 regression formulas (Froehlich 1995/2008, MacDonald-Langridge, Von Thun-Gillette, Xu-Zhang) vs NWS-BREACH are pure ARITHMETIC - playground/code_exec composition per the "analysis is playground not tools" rule, NOT an engine template; and "ensemble" spread is on the wave HOLD list. Not an engine landing. |
| `dam_breach_reservoir_to_2d_floodplain_coupling` | **STOP** | Needs a progressive SA/2D-connection BREACH hydrograph into a 2D floodplain (Sayers Dam). No Breach type in the 2025 model (Finding 3); 6.6 fresh-connection breach reference-fixture-gated (ADR 0171). NOTE: the SA/2D CONNECTION itself (weir overflow into 2D) is now authorable on the reopened structure front - only the progressive-BREACH block on it stays blocked. |
| `simple_breach_geometry_setup` | **STOP (unchanged)** | ADR 0157/0171 STOP stands - fresh dam crest/aux-spillway/low-level-outlet/progressive-growth breach. Runs-to-completion INTENT already served GREEN by `hecras_levee_breach` (ADR 0125). |

## Decision

**0 landings, 0 new registered tools, 0 new templates, 0 image rebuild, 0 parser bump this
wave** - a characterization + reclassification wave (like ADR 0170/0171), no executed code
touched, so the ADR 0148 worker-image law does not fire. Registry stays **252**;
`EXPECTED_TEMPLATES` unchanged. Fabricating a structure template without a proven
prepare+solve A/B would violate the honesty floor.

The wave's LOAD-BEARING output is a corrected adjudication:

1. **Every 1D-framed row (all of 1D Steady, all of 1D Unsteady, the two 1D-structure rows,
   and the progressive-breach rows) STAYS STOP** - now for the ARCHITECTURALLY CORRECT
   reason: **the 2025 managed engine has no 1D solver**, and the 6.6 Fortran 1D executors
   are reference-fixture-gated (ADR 0170). This is NOT a "needs Windows" claim - both 1D
   executors run on Linux; they lack an authorable steady/1D-network input deck. The ADR
   0170 GUI-seeded-fixture recipe remains the only 1D unblock.

2. **The ADR 0171 structure-authoring STOP is SUPERSEDED for 2D structures.** The 2025
   engine dissolves the RASMapper face-pairing frontier (`IdentifyStructureCellsAndFaces`
   is engine-side + headless), carries the full 2D weir/gate/culvert/PUMP compute stack,
   and exposes a wired managed authoring surface (`Geometry.StructureLayer` /
   `Ras.Hydraulics.Structure` with `Polyline` + `StationElevation` + `Connection`;
   `GateGroupLayer`; `CulvertBarrelLayer`). `pump_station_trigger_and_ramp_control` and the
   2D-cluster `combined_1d2d_pump_station_coupling` REOPEN as a bounded 2025-engine build
   front. This is HEAVY new machinery (managed-C# authoring + authoring-image rebuild +
   discriminating live A/B), not a same-wave knob - it earns its own job.

3. **No Breach machinery in the 2025 model** - progressive breach stays 6.6-only
   (`hecras_levee_breach`); the 2025 engine does not extend it.

## Recipe (the constructive unblock for the reopened 2D structure front)

Mirrors the ADR 0207->0209 RoG arc exactly:

1. Extend `scripts/sandbox/hecras/managed_solve/Driver.cs` with a `structdemo` mode:
   author a `BasicRectangleParams`-style 2D basin, then construct a
   `Ras.Hydraulics.Structure` (a centerline `Polyline` crossing the flow path, a
   `StationElevation` crest above the tailwater but below the headwater, `WeirWidth`,
   `DownstreamConnection.ConnectedElementName` = the 2D area), add it to
   `project.Geometry.StructureLayer`, `Save`. For gates use `GateGroupLayer`; for culverts
   `CulvertBarrelLayer`; for a pump the `Structures.Pumps` group.
2. `ras prepare` (proves `IdentifyStructureCellsAndFaces` + `PrepareForCompute` accept the
   authored structure headless) then `ras solve --solver CPU`.
3. DISCRIMINATING A/B: structure PRESENT vs ABSENT on the SAME basin (a weir should pond
   water upstream / cut downstream discharge; a pump should lift a measurable ramped flow).
   Gate on the 0143 must-measurably-move-water rule.
4. Productionize: a `structure` knob family on `hecras_flood_2d` (or a sibling composer)
   dispatching the 2025 structure path; worker + registry + corpus + categories +
   `EXPECTED_TEMPLATES` bookkeeping; a NATE-cited 2D structure validation case (the board's
   Beaver Creek cases are 1D - a 2D weir/gate/pump replication target must be selected +
   citation-verified before acceptance, per the US-cases/paper-first rule).

## Consequences

- Coded-tools metric: 0 tools added, 0 LOC landed (characterization + ADR + board only).
  Registry 252 -> 252; `EXPECTED_TEMPLATES` unchanged.
- Evidence (in-image): `Ras.Engine.dll` solver type list = 2D-only (no 1D class);
  `ras` verbs = prepare/mesh/solve; `Ras.Engine.Structures.{Weirs,Gates,Culverts,Pumps}`
  compute stack + `HydraulicStructureCollection.IdentifyStructureCellsAndFaces(Mesh)`;
  `Ras.Hydraulics.Structure` authorable props; `Ras.Layers.Geometry` constructs+Saves
  `StructureLayer`/`GateGroupLayer`/`CulvertBarrelLayer`; `DamBreakParams` = idealized
  Stoker step-IC (no breach); no `breach` type in `Ras.Core.dll`; 6.6 `RasSteady`+
  `RasUnsteady` present in `hecras:latest`.
- Offline registry spot slices GREEN (17 passed): `test_catalog_surfacing` (registry 252),
  `test_door_dissolution` (`EXPECTED_TEMPLATES` unchanged).
- Board reframing: the HEC-RAS front's remaining board value now splits cleanly in two -
  (a) a 1D leg (steady + unsteady network) blocked on a GUI-seeded reference deck for the
  6.6 executors (ADR 0170, unchanged); (b) a 2D-structure leg (weirs/gates/culverts/pumps
  + SA/2D connections) UNBLOCKED by the 2025 engine and awaiting an authoring build job.
  The progressive-breach block sits on top of leg (b)'s connection authoring plus a Breach
  model the 2025 beta does not yet ship.
</content>
</invoke>
