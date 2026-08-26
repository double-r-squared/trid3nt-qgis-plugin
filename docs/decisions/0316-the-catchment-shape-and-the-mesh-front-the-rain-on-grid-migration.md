# ADR 0316 - The catchment shape, and the mesh front the composer was hiding

Status: LANDED (TELEMAC workflows refactor, wave C - `telemac_rain_on_grid`).
Follows ADR 0314 (the static plan) and ADR 0315 (the coastal split).
Supersedes nothing. Completes the TELEMAC family migration: 7 of 7.

## Context

`telemac_rain_on_grid` was the last imperative composer in the family and the
only one wave B deliberately left. The reason was principled rather than a matter
of size: it carried a mesh PRECONDITION GATE, a byo mesh, and an in-container
mesher - three things the mesh-gate wave was designed to rule on. Migrating it
early would either have invented a mesh-gate species ahead of that wave or kept
the gate imperative inside a composite, which is the disease the skeleton exists
to remove.

What made it migratable now is that ADR 0315 already ruled the shape: mesh is
SUPPLIED-optional `Data`, a producer-less CONTEXT SLOT with no baked default
source. That is the whole of what the byo path needed.

The steps audit had also parked rain_on_grid's entire class-C set behind this
migration - "17 bare signature defaults, the invented AOI-centroid pour point, the
unreachable Huang slope correction, the 24-hour solve timeout, the duplicated UTM
formula" - on the grounds that fixing them one at a time was the job, not a fix.

## Decision

### 1. CATCHMENT is a domain SHAPE, and the shape decides the order

`TelemacWorkflow.acquire_domain` gains a third shape beside `reach` and
`open_water`. A catchment's acquisition runs BACKWARDS from every other domain:
the OUTLET first, and the analysis window derived from it.

That is not a convenience. A geocoded place bbox names a TOWN and need not
contain the upstream catchment - the live bug was 'Otto, NC' clipping the Coweeta
basin mid-hillslope into a 20-cell sliver. So the pour point is a required USER
param behind a `DrawGate`, and the AOI is a labeled buffer around it unless the
caller drew one. An explicit `bbox` still wins, because it is the user's own
extent.

THE INVENTED CENTROID DIES. The composer defaulted the pour point to the AOI
centroid, which is a physics value nobody chose deciding which basin is modelled
at all. It is now a gate: `user_gated` asks on the canvas, `auto` refuses typed.
Door 6, exactly as the doors table says.

### 2. The catchment MESHER leaves the template for the shared mesh front

`workflows/mesh/watershed.py` holds the generation strategy - delineate, size by
distance to the channel network, triangulate in the GPL-isolated image, project,
sample a bed - and `workflows/mesh/telemac_build.py` holds the thin per-solver
SELAFIN writer. Neither knows which question is being asked; the template's own
folder keeps only `cn_infiltration.py`, the SCS curve-number surface that varies
per question.

This is the placement rule paying a debt rather than a refactor for its own sake.
The standalone `generate_mesh` TOOL was already importing four symbols out of a
TELEMAC template's private module (`acquire_watershed_mesh`,
`reproject_nodes_to_utm`, `_sample_raster_at_nodes`, `_write_bottom_selafin`), and
`hecras_flood_2d` a fifth (`_delineate_catchment`). A shared tool reaching into
one engine's template for its meshing is a leak in the direction that cannot be
defended; the three callers now share one home.

The mesher's own defaults live in that home, ONE copy, because the two callers
have different contracts: the template's params promise the numbers in prose, and
the standalone tool has no param sheet at all.

### 3. The bed DEM stops being a step that FETCHES

`mesh_acquisition.py:644-670` was the second-order offender the wave-B audit
found: a step calling `fetch_dem` with a hardcoded source and resolution, and
falling back across datasets to Copernicus - a canopy-inclusive SURFACE model
under a bare-earth contract - with the loudness of the label depending on whether
the caller had passed a `notes` sink.

It is now `Data("bed_dem", Fetch.tool(...).ladder("usgs_3dep_bare_earth",
"copernicus_glo30"))`. Three consequences: the fetch goes through the router's own
cache, ladder and provenance (the no-double-middleware law); the source and the
resolution are declared params; and the cross-dataset label rides the RETURNED
ARTIFACT rather than an out-parameter, so it cannot be bypassed by the call shape.
The same move applies to the land-cover raster, the channel network and the rain:
every world-read this template makes is a declared `Data`, and no step fetches.

The rain declaration is the ladder that matters most - `aorc_hourly ->
design_storm` - because it is where a hypothetical is distinguished from a record.
The run reports which rung answered, on the layer and in the journal.

### 4. The mesh is a SLATE

`Data("mesh").supplied(geometry="mesh").optional()`. Producer-less, so the
template names no default source for somebody's mesh. Three ways it is satisfied,
in preference order: a mesh SUPPLIED on the invocation is taken as-is; unfilled,
the run asks whether to adopt a mesh this case already holds (the precondition
gate, unchanged, still shared with SCHISM and HEC-RAS); declined or absent, it
generates one - a labeled fallback, never a stance.

`mesh_uri` is gone; the slot's own name is the wire argument.

### 5. The unreachable knob becomes reachable, and the dead one dies

`steep_slope_correction` was a hardcoded `False` in the composer's call to the
node-field builder, so the Huang (2006) correction - which exists precisely
because the engine's own branch is compiled off in the installed 9.0.0 build -
could not be asked for. It is now a declared param, and the slopes it needs come
from the mesh's OWN piecewise-linear bed rather than from a finer raster the run
does not resolve.

`observed_gauge_id` goes the other way. The docstring promised it "wires NSE/R2
vs a USGS-NWIS gauge"; the worker never read the field. A documented promise
nothing keeps is worse than an absent feature, and the gauge grading lives in the
Ball Creek replication drivers where it actually runs. DELETED, with a ledger row.

### 6. The simulated window is a SCENARIO door

Reviewed against the wave-B pattern. Unlike `tomawac_wave_field` and
`telemac3d_stratified_flow` (CONSTANT: both windows are "long enough to converge",
which is a numerics fact) and unlike `coastal_tidal_surge` (USER: the gauge record
defines it), how long you watch a catchment respond decides whether the hydrograph
carries its peak and how much of the recession - which is part of the question
being asked. SCENARIO, and therefore still on the model-facing wire. Its
`derived_when_absent` names the two rungs it falls to: the fetched hyetograph's
own span, or the design storm's own duration.

### 7. The dispatch dance had a second consumer, so it moved

The reuse-sweep norm promotes on a CONFIRMED second consumer, and the rain-on-grid
solve was one: mint the cards, bind the emitter, poll, route the terminal card
whichever way the run ends. `open_water.dispatch_and_wait` is that dance, and it
JUDGES NOTHING - what a non-complete status means is the caller's typed error,
because the code it carries is the caller's contract.

## The parity evidence

The migration is a REWRITE of the representation, so the physics owed parity and
the check is byte-level rather than statistical. Coweeta Creek canary, coarse,
pre-migration `01M0YF7BXK2H6A0JYZ3BWQ2FGG` against post-migration
`01M0YHHSFGCHXW726XDMPQSNHN`:

| artifact | verdict |
| --- | --- |
| `manifest.json` | BYTE-IDENTICAL bar the run tag |
| `watershed.slf` | sha256 IDENTICAL (`01aa0531...`) |
| `node_cn2.txt` | sha256 IDENTICAL (`244ad90a...`) |
| `node_manning.txt` | sha256 IDENTICAL (`64d2ad08...`) |
| `telemac_metrics.json` | 31/31 keys IDENTICAL (run id and wall time excluded) |
| canvas layers | the same six, in the same roles |

The mesh, the curve-number field, the roughness field and the deck are bit for bit
what the composer produced. Everything that changed is representation.

## Consequences

- The PRODUCTS GATE goes GREEN by construction. The composer persisted neither
  `metrics.json` nor `chart_spec.json`, so its canary had always read `NoSuchKey`
  for both; the skeleton's publish stage writes them, and the run now carries a
  17-field answer plus an outlet-hydrograph chart spec. That is an ADDITION, not
  drift - the numbers were always there, and nothing could cite them.
- The canary moves to `user_gated`, joining the four open-water canaries. The
  template now DECLARES its physics-consequential rows, so law 9 sees them and a
  run that showed them to nobody refuses. That is the floor working, and the
  harness answers the card rather than turning the mode off.
- Two proof-lane defects surfaced and were fixed where they were, both general:
  the animation renderer could not find a mesh CRS for a leg whose worker records
  none (it now reads the run's own `outputs.json`), and it added a local origin to
  an ALREADY-ABSOLUTE mesh, which is the same false-origin defect as the coastal
  one with the sign flipped. Whether an origin belongs is now decided by a fact
  about the FILE - a UTM easting is never below 160 km - rather than by whether a
  caller passed one.
- The run publishes `domain_bbox`, the extent it actually modelled: the mesh's own
  node bounds, not the search buffer it was delineated inside. A basin is a
  fraction of its buffer, so the two are different answers to "where is this", and
  the packet's animation check now cites the run's product rather than the ask.
- The family is 7 of 7. `data/` still exists, so the campaign does not.

## What this does NOT do

The mesh GATE species is still not built: refinement between generation and solve
remains the mesh wave's, and this template reaches the precondition gate rather
than a mesh-approval card. The worker is untouched, so the rain-on-grid half of
the worker-purity inventory (the RAINDEF staging, the in-worker fetch endpoints)
still rides the in-worker-fetch migration. FOUR worker comments still name
`mesh_acquisition`, a module that no longer exists (see the correction below);
correcting them costs a worker ledger row for zero behaviour change and belongs to
the next wave that opens that image.

## CORRECTED (ledger audit of `0f7a6351..02acbfed`, 2026-08-26)

Two claims in this note were checked against git and both were short.

### The stale worker comments are FOUR, not three

The line above originally read "Three worker comments still name `mesh_acquisition`,
a module that no longer exists". `git grep -n mesh_acquisition 02acbfed -- workers/`
returns four, in three files - none of them an import, all of them prose that sends
a reader to a module that is gone:

| where | what it says |
| --- | --- |
| `workers/telemac/entrypoint.py:822` | "the agent-side composer staged into the rundir (mesh_acquisition + fetch_landcover)" |
| `workers/telemac/rog_build.py:61` | `WATERSHED_SLF` trailing comment - "BOTTOM SELAFIN (UTM metres) from mesh_acquisition" |
| `workers/telemac/rog_build.py:88` | section banner - "1. read the watershed SELAFIN staged by mesh_acquisition" |
| `workers/telemac/telemac_river_dye_build.py:299` | "a rain-fed delineated-watershed TIN (staged by the agent-side mesh_acquisition step as watershed_slf...)" |

The fourth is the river-dye one, which is the one a count taken from `rog_build.py`
plus `entrypoint.py` alone would miss: the RoG dispatch comment lives in the
CHANNEL-DYE builder, because that is where `mode="rain_on_grid"` routes away.

### Section 2's symbol list names the OLD names and omits the renames

Section 2 says the standalone `generate_mesh` tool "was already importing four
symbols out of a TELEMAC template's private module (`acquire_watershed_mesh`,
`reproject_nodes_to_utm`, `_sample_raster_at_nodes`, `_write_bottom_selafin`), and
`hecras_flood_2d` a fifth (`_delineate_catchment`)". That count is right about the
world BEFORE the move, and it is what commit `871acc38` inherited. What the section
does not say is that four of the five did not keep their names, so a reader who
takes the list as a map of the new homes will not find them:

| old name, in `rain_on_grid/mesh_acquisition.py` | new name | new home |
| --- | --- | --- |
| `acquire_watershed_mesh` | `generate_catchment_mesh` | `workflows/mesh/watershed.py` |
| `reproject_nodes_to_utm` | unchanged | `workflows/mesh/watershed.py` |
| `_sample_raster_at_nodes` | `sample_raster_at_nodes` | `workflows/mesh/watershed.py` |
| `_delineate_catchment` | `delineate_catchment` | `workflows/mesh/watershed.py` |
| `_write_bottom_selafin` | `write_bottom_selafin` | `workflows/mesh/telemac_build.py` |

The de-underscoring is the load-bearing half of the move rather than cosmetics: a
leading underscore said "private to this template", and three shared callers were
reaching past it. Dropping it is the placement rule being stated in the name.
`generate_catchment_mesh` also changed SHAPE, not only spelling - it takes `slug`,
`bed_dem` and `rivers` as arguments now, because the strategy no longer fetches
(section 3), so the caller resolves the bed raster and the flowlines and hands them
in. `write_bottom_selafin` is the one that landed in a different file from the other
four, which is the split section 2 already describes and the flat five-name list
flattens away.
