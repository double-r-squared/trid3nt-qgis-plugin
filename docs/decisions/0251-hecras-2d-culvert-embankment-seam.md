# ADR 0251 - HEC-RAS 2025 2D culvert-through-embankment seam: PROVEN LIVE. The one drivable 2D structure ADR 0250 named (the Culvert, via CulvertBarrelLayer) authors, prepares, solves, AND moves water: a barrel through a raised terrain embankment passes flow that the ridge otherwise blocks. The A/B/C is DECISIVE and POSITIVE (unlike the weir A/B of ADR 0250, which was bit-identical inert). Stage-1 seam proof LANDED in the sandbox harness; productionization (worker leg + composer template + real US site) is a well-scoped separate wave, deferred with a precise recipe.

Date: 2026-08-13
Status: accepted
Continues: ADR 0250 (the 2D structure-authoring seam: the beta wires ONLY the Culvert into
the compute -- `InitializeComputeDriver` -> `InitializeDriver_Culverts` -> `new Culvert(...)`
from `CulvertBarrelLayer`; authored weirs/gates/pumps are silently inert). ADR 0250's precise
follow-on item (2) was: "author a `CulvertBarrelLayer` barrel + `BarrelPropertiesLayer` +
`OpeningPropertiesLayer` over an embankment terrain ridge, then A/B present-vs-absent." This
ADR EXECUTES that and reports the result.

## What was built (prototype, LANDED)

`Driver.cs` gained a `culvertdemo` mode. It reuses the ADR 0250 `StructChannel` inflow channel
(60 x 300 m, 6 x 30 uniform 10 m cells, ramped inflow at the top wall, 1.0 m tailwater stage at
the bottom, flat terrain) and -- with the culvert flag -- authors the full 2025 culvert surface
onto the geometry (the three associated layers `InitializeDriver_Culverts` consumes):

- `OpeningProperties` "Opening1": `OpeningType=ConcretePipeCulvert_SquareEdgeWithHeadwall`
  (`[ChartScale(1,1)]`, a recognized inlet-control chart/scale -- `Custom`/`None` hard-fail per
  `SetOpeningProps`), `KIn=0.5`, `KOut=1.0` -> `geometry.OpeningPropertiesLayer.Add`.
- `BarrelProperties` "Barrel1": `Shape=Circle`, `Rise=Span=2.0` m, `Mannings=0.013` ->
  `geometry.BarrelPropertiesLayer.Add`.
- `CulvertBarrel` "Culvert1": `Polyline` (30,175)->(30,125) crossing UNDER the ridge (endpoints
  land on channel-bed cells, not ridge cells), `BarrelPropertyName="Barrel1"`,
  `UpstreamOpeningName=DownstreamOpeningName="Opening1"`, `UpstreamInvert=DownstreamInvert=0.0`
  (at the bed = cell minimum elevation) -> `geometry.CulvertBarrelLayer.Add`.

then `geometry.Save()` persists them to the HDF groups `/Geometry/Culverts/Barrels`,
`/Geometry/Culverts/Barrel Types`, `/Geometry/Culverts/Opening Types` (re-opening the project
via `new Project(<.ras>)` after the known beta terrain-dir Save bug, then re-saving the geometry
-- the same proven sequence as ADR 0250's `structdemo`). The **embankment ridge** is a raised
terrain band the host writes into `Terrains/Terrain.tif` AFTER authoring (a full-width y-band at
`y=[140,160]` raised to 6.0 m, `raise_ridge.py`); the barrel geometry the host must match is
emitted to `culvert_probe.json` (single source of truth). Inflow is sized to 4 m3/s so the barrel
can pass it at a modest head: WITH the culvert the system reaches quasi-steady, WITHOUT it the
upstream ponds toward the 6 m crest (it never overtops within the 9000 s sim, so the culvert is
the ONLY outlet).

The authoring plumbing works end-to-end through `hecras2025-authoring:latest` (id afb76f3ccd00):
`ras prepare -s <.ras> -o <r2r>` INGESTS the barrel (completes, no rejection) and
`ras solve <r2r> <out> --solver CPU` COMPLETES on the culvert-carrying deck.

## The finding (DECISIVE, POSITIVE -- the culvert MOVES WATER)

The A/B/C trio is the discriminator. Final-step per-cell depth, upstream (y>160) vs downstream
(y<140) of the ridge, plus the upstream mass balance:

| case | ridge | culvert | US mean depth | US dV/dt (m3/s) | verdict |
|---|---|---|---|---|---|
| C free  | absent  | absent  | 1.001 | -0.000 | flows freely, no ponding |
| B block | 6 m     | absent  | 4.933 | +3.733 | ponds upstream, unbounded (traps ~inflow) |
| A pass  | 6 m     | present | 1.521 | +0.035 | quasi-steady -- barrel conveys the inflow |

- `max|A - B|` over the final-step per-cell depth field = **3.413 m** (the culvert is emphatically
  LIVE; contrast the ADR 0250 weir A/B = 0.00000000 m, bit-identical inert).
- **US mean depth: C=1.001 < A=1.521 < B=4.933** -- A is strictly between C and B (the culvert
  relieves the ponding the ridge causes, but not to the free-flow level: it passes flow at a head).
- **Mass balance = the downstream-arrival proof.** The ridge blocks the surface, so the ONLY
  upstream exit is the barrel. In B the upstream storage rises at +3.73 m3/s (~= the 4 m3/s inflow,
  trapped -- nowhere to go). In A the upstream storage is quasi-steady (+0.035 m3/s), so the
  4 m3/s inflow is EXITING upstream via the barrel and ARRIVING downstream (where the tailwater BC
  removes it -- downstream depth is pinned by that BC and cannot show arrival as depth, so
  conservation is the correct arrival metric). The barrel conveys ~3.70 m3/s (B traps 3.73, A
  traps 0.035).

The `must-measurably-move-water` gate (ADR 0143) PASSES. Proof figure:
`docs/proof/templates/hecras_culvert_embankment_flow_seam_probe_abc.png` (three plan-view depth
maps + the upstream ponding-vs-steady time series + the mass-balance bars; synthetic seam-probe
labeling per the ADR 0250 precedent -- NOT yet a real-site validation case).

### Root cause the effect is real (decompiled, IN-IMAGE, `Ras.Core.dll`)

`Ras.Layers.Geometry.InitializeDriver_Culverts` is the wired layer->engine converter: for each
`CulvertBarrel` it does `new Culvert(barrel.Name, barrel.Polyline)` and copies the full physics
into `CulvertPhysProps` -- `InvertUS/DS`, `Length` (from `Polyline.Length`), `Shape`
(`EngineShape`: Circle->Circular / Box / Ellipse), `FullRise`/`Span`, `Mann`, entrance/exit loss
(`usOpeningProps.KIn`/`KOut`), and the inlet-control chart/scale via
`ChartScaleHelpers.TryParametrize(chart, scale)` (`SetOpeningProps`) -- then
`computeDriver.HydraulicStructures.AddHydraulicStructure(culvert)`.
`IdentifyStructureCellsAndFaces(GlobalMesh)` then pairs the barrel endpoints to mesh cells/faces
headless. Every field the barrel/opening properties carry lands in the solve; the A/B/C confirms
the kernel then conveys flow. This is the exact path ADR 0250 identified as the one live one.

## Decision

**Stage-1 seam proof LANDED (Driver `culvertdemo` mode + the A/B/C evidence); 0 new registered
tools, 0 new templates, 0 image rebuild, 0 parser bump.** The one code landed is the sandbox
`culvertdemo` mode + host `raise_ridge.py`/`run_culvert_abc.sh`/`make_culvert_fig.py`
(reproduction tooling, ~110 C# LOC + ~90 Python LOC, NOT registered product). Registry stays
**252**; `EXPECTED_TEMPLATES` unchanged.

**Stage-2 productionization is DEFERRED as a well-scoped separate wave** (below). The seam proof
is the load-bearing deliverable: it converts ADR 0250's decompile-level prediction ("the Culvert
is the one drivable 2D structure") into an empirical, water-moving A/B/C. Productionizing responsibly
(image/dll rebuild + live-smoke-through-the-image + a real US embankment/culvert site + the 4-slice
test law + a QGIS-true real-site render) is genuinely a new build front, not a same-wave increment;
landing a half-tested version would violate the honesty floor + the image-staleness law.

## Precise Stage-2 follow-on scope (grounded in the actual seams)

1. **Worker authoring leg.** The worker's `services/workers/hecras2025/subst/crux/freshtopo/
   rog2025_pipeline.py` already runs THIS `synthdrv.dll` (built from this `Driver.cs`) via the
   `realrog` mode over a real DEM. Add a `culvert` authoring path: a new `culvertreach` (or extend
   `realrog`) mode that authors a `CulvertBarrel` + `BarrelProperties` + `OpeningProperties` on the
   real-terrain deck, parameterized by (barrel polyline in local SI m, US/DS invert, Rise/Span,
   Shape, Manning n, OpeningType/KIn/KOut). Rebuild `synthdrv.dll` (the worker mounts it at runtime
   -- `cp /probe/synthdrv.dll .` -- so no full authoring-image rebuild, but the dll IS the staleness
   surface: rebuild + a through-dll live smoke is mandatory).
2. **The embankment.** At a real road/levee crossing the embankment is usually ALREADY in the 3DEP
   DEM; where it is not (or is under-resolved), burn a raised polyline band into the terrain (the
   `raise_ridge.py` idea generalized to an arbitrary embankment centerline + crest + width). The
   barrel endpoints must snap to channel-bed cells either side of the ridge (validation
   `Barrel_UpstreamInvertBelowCell` hard-errors if an invert is below the cell minimum elevation).
3. **Composer surface.** A small template `culvert_embankment_flow` (question class: "how does flow
   route through a culvert under a road/levee embankment vs the blocked case", NOT a place name) OR
   a `culvert` knob on `hecras_flood_2d`. The barrel engineering params (diameter/rise-span, invert,
   opening type, entrance/exit loss) are un-fetchable -> the input-review gate with labeled defaults
   (e.g. circular 2 ft-to-metric default, SquareEdgeWithHeadwall, KIn 0.5 / KOut 1.0). Input layers
   (reach via NHD, DEM via 3DEP) through the emit-on-fetch seam (`purpose=` on router fetches).
4. **Registry bookkeeping** (only when a tool/template registers): `categories.py` + `tools/__init__.py`
   + `test_catalog_surfacing` pins (registry 252 -> 253) + `test_door_dissolution` `EXPECTED_TEMPLATES`
   + a co-located `corpus.yaml` + a model-free `retrieve_visible_tools(prompt, None, 8)` top-8 check.
5. **Live E2E + validation.** A real US road-embankment/culvert crossing on a real reach (US-cases
   /paper-first: cite a documented crossing to NATE first), discriminating present-vs-absent pair +
   the must-move-water gate, QGIS-true render (ESRI World Imagery + mesh overlay) to
   `docs/proof/templates/`.

## Consequences

- Coded-tools metric: 0 tools added, 0 LOC in server/worker/registry (sandbox + docs only).
  Registry 252 -> 252; `EXPECTED_TEMPLATES` unchanged.
- Evidence (in-image, live): authored `/Geometry/Culverts/{Barrels,Barrel Types,Opening Types}`
  round-trip; `ras prepare` + `ras solve --solver CPU` COMPLETE on the culvert-carrying deck;
  `max|A-B|=3.413 m` (culvert LIVE); US mean C=1.001 < A=1.521 < B=4.933 (A strictly between);
  upstream mass balance B +3.73 m3/s (trapped) vs A +0.035 m3/s (conveyed) -- downstream arrival by
  conservation. `InitializeDriver_Culverts` copies every barrel/opening field into the solve.
- Proof: `docs/proof/templates/hecras_culvert_embankment_flow_seam_probe_abc.png` (synthetic
  seam-probe A/B/C; a real-site render is a Stage-2 deliverable).
- Board: HEC-RAS `### Structures` gains a `culvert_embankment_flow_2d_seam` row = SEAM-PROVEN
  (this ADR); the weir/gate/pump rows stay STOP (ADR 0250 beta gap).
- Offline registry spot slices GREEN: `test_catalog_surfacing` (registry 252),
  `test_door_dissolution` (`EXPECTED_TEMPLATES` unchanged).

## Stage-2 productionization -- LANDED (2026-08-13)

The deferred Stage-2 wave (worker leg + composer template + real US site) is DONE. The
seam is now a registered product.

**Worker leg.** `Driver.cs` gained a `culvertreach` mode -- the spec.json-driven
generalization of `culvertdemo` (strict parser v2: any unknown key, top-level or in the
`culvert` block, hard-errors; `synthdrv.dll` rebuilt from
`/home/nate/hecras_probe2025/driver`, provenance-checked (`CulvertReach`/`_CulvertReachKeys`
strings in the dll, md5 1ab8e0ce...), and live-smoked THROUGH `hecras2025-authoring:latest`:
authors `/Geometry/Culverts/{Barrels,Barrel Types,Opening Types}`, strict parser rejects a
`bogus_key`). It authors the barrel + BarrelProperties + OpeningProperties from the manifest
fields (barrel polyline in local SI m, US/DS invert, rise/span, shape, Manning, opening
type/K in/out) on a StructChannel inflow deck whose terrain the host overwrites with the real
DEM. The Python leg is
`services/workers/hecras2025/subst/crux/freshtopo/culvert_reach_pipeline.py`: reproject 3DEP
to a local SI grid (reusing `rog2025_pipeline.prepare_local_terrain`), orient the reach down
the y-axis, DERIVE the embankment band (detrended cross-stream-minimum local anomaly) + the
channel thalweg + the barrel endpoints (on-channel per-row argmin) + inverts (endpoint-cell
minimum + margin, clearing `Barrel_*InvertBelowCell`), author the A (barrel) and B (no barrel)
decks, prepare + solve both on the CPU, extract the A/B discriminant + a depth COG.

**Composer.** `culvert_embankment_flow` (engine=hecras, tier=template) -- question class, not
a place. The un-fetchable barrel engineering (diameter/opening-type/K in/out/Manning) goes
through the input-review gate with labeled defaults (1.0 m circular pipe,
SquareEdgeWithHeadwall, KIn 0.5/KOut 1.0, n 0.013). Inputs surface via the emit-on-fetch seam
(`fetch_dem(purpose=terrain)` + `fetch_river_geometry(purpose=reach)`). Registry 252 -> 253;
`categories.py` + `tools/__init__.py` + `test_catalog_surfacing` (x4 pins + tally) +
`test_door_dissolution` `EXPECTED_TEMPLATES` + co-located `corpus.yaml` all updated;
`retrieve_visible_tools(prompt, None, 8)` HITs top-8 on 6 natural prompts.

**Live E2E (real US site).** North Fork Salt Creek x Green Valley Road, Brown County IN
(lon -86.2883, lat 39.1893; a real NHD reach + a named road crossing tagged `culvert=yes` in
OSM, framed via a road-over-stream crossing finder). Real 3DEP terrain, real reach.
Present-vs-absent A/B (2 solves, 21 s):

| case | barrel | upstream max depth | upstream storage rate | verdict |
|---|---|---|---|---|
| B blocked | absent  | 2.316 m | 2.00 m3/s | traps the full inflow (embankment blocks) |
| A pass    | present | 1.705 m | 0.784 m3/s | barrel conveys 1.22 m3/s under the road |

`max|A-B|` final-step per-cell depth = **0.613 m** (barrel LIVE); upstream ponding relieved
0.611 m; the barrel conveys **1.216 m3/s** (B traps 2.0, A traps 0.784). `moves_water=TRUE` --
the must-move-water gate PASSES on the real site.

**The embankment (honest).** The North Fork Salt Creek road fill is ~0.9 m in the lidar and
the road (~10 m) is narrower than the 20 m screening cell, so the mesh road cell's subgrid
captures the buried channel pixel and never blocks (the pure-real B leaks ~0.22 of 0.5 m3/s in
an early probe). Per the Stage-2 scope ("burn a raised band where the DEM is under-resolved"),
the `auto_seal` mode raises a 1-cell crest cap at the REAL road centerline (widened >= one mesh
cell) so the blocked case genuinely ponds -- disclosed in the result (`embankment_basis`) and
the layer note. `embankment_mode=real_terrain` uses the lidar fill as-is for a tall real fill
(dam-road / causeway) that already blocks at the mesh scale. The reach + terrain + road
location are all real; only the crest cap is synthesized, and only because the sub-cell fill
under-resolves at screening resolution.

**Proof.** `docs/proof/templates/culvert_embankment_flow_ab.png` -- the WITH-culvert (A) vs
BLOCKED (B) depth pair over ESRI World Imagery with the structured 2D mesh wireframe + the
barrel (red) crossing the embankment band (orange) at the real Green Valley Road crossing.

**Screening constraint (recipe for the next site).** The structured-grid inflow is on the top
wall, so the reach must run roughly along a domain axis (the leg flips N/S automatically). A
strongly-slanted reach (North Fork Salt is 159 deg) drifts across a narrow box, so the AOI is
framed valley-wide and the inflow sheets -- decisive ponding needs the flow to reach quasi-
steady at the embankment (a pipe-matched inflow + the crest seal, as here). General reach
rotation + a channel-concentrated inflow BC are the follow-on for tighter real-site fidelity.

## Reproducibility

Build: `scripts/sandbox/hecras/managed_solve/REPRODUCE.md` (the `culvertdemo` mode rides the same
SDK-image build). Author + embankment + A/B/C:
`dotnet synthdrv.dll culvertdemo /probe/cv_free 0` (C), `... /probe/cv_block 0` (B),
`... /probe/cv_pass 1` (A); `raise_ridge.py` on cv_block + cv_pass; then `ras prepare` +
`ras solve --solver CPU` on each; compare final `Cell Depth` upstream/downstream + the upstream
storage rate. Orchestration: `run_culvert_abc.sh`; figure: `make_culvert_fig.py`. Decompile:
`local/ilspy:9`, `DOTNET_ROLL_FORWARD=Major`; the wired bridge is
`Ras.Layers.Geometry.InitializeDriver_Culverts` in `Ras.Core.dll`.

## Proof-norms addendum (2026-08-14, render fix, no physics changed)

NATE caught three proof figures in this family (`hecras_structure_2d_seam_probe_ab.png`,
`hecras_culvert_embankment_flow_seam_probe_abc.png`, `culvert_embankment_flow_ab.png`)
rendering the solver's per-cell depths as cell-center scatter dots / gappy bars instead of
filled cell footprints on the structured mesh grid -- the same render-lie class the marina
griddata precedent (ADR 0237) named. Fixed in place (`make_culvert_fig.py` scatter->pcolormesh;
new `make_struct_fig.py` and `make_culvert_realsite_fig.py` under
`scripts/sandbox/hecras/managed_solve/`; all numbers recomputed live from the same result HDFs,
unchanged). Norm, stated once for the family: **field proofs render filled cells (pcolormesh on
the solver's cell grid) or the published depth COG, never cell-center scatter.**
