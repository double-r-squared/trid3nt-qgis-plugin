# MODFLOW 6 - the second engine

A MODFLOW run is one simulation directory the worker's image solves with mf6
6.7.0. The engine facade is `workflows/modflow/workflow.py`; it sits on the
runtime's skeleton the TELEMAC facade sits on, and the runtime's fill, review,
publish and journal are shared unchanged. What is the engine's own is the
simulation the authoring writes, the three module wrappers, the coupling, and
the one transform that brings a solved run to the map. The workflows-runtime
view - the sheet over the accepted mesh, the launch order, the raster read -
is `docs/design/workflows.md`; this page is the engine's.

## The simulation is the steering file

Unlike TELEMAC's one `.cas` per module, a MODFLOW simulation is a directory:
`mfsim.nam` names the clock (TDIS), each model's name file, the solution
packages (IMS) and the exchanges; each name file names one file per package.
`authoring/simulation.py::serialize` writes it package by package around a
filename-only DISV - THE STAGED MESH FILE IS NEVER REWRITTEN. FloPy authors in
the daemon (fork F2: the daemon writes the deck and reads results in process,
the image solves and does nothing else); the worker holds only mf6. The fill
runs mf6's own `--mode validate` over the staged directory under the one
review, and `authoring/staged_check.py` checks the references, the DISV digest,
the cell ids, the finite griddata and the TDIS window before dispatch.

## The modules

GWF (flow), GWT (solute transport) and GWE (heat transport) - the three that
solve a field on the grid through IMS. Each is a `ModflowModule` wrapper over a
dictionary extracted from the mf6 6.7.0 DFNs (`scripts/
extract_modflow_dictionary.py`, committed under `modules/module_input/`). A
keyword is flat in the DFN's own spelling - `NPF_ICELLTYPE`,
`IMS_OUTER_DVCLOSE`, `TDIS_NPER` - split on the first underscore; a body
stating an unknown keyword refuses at import by name, and a required-no-default
keyword unstated refuses at the fill (78 in GWF). The simulation's own clock
rides every body as the `Periods` composite, and the run's moment as
`TDIS_START_DATE_TIME`.

Each wrapper carries its `MODULE_OUTPUT` table - what a solved run writes,
read off the files the output control saved: GWF head, saturation and specific
discharge; GWT concentration; GWE temperature. mf6 carries no unit system, so
the units stay empty: they are the ones the modeller declared.

## The accepted mesh is the DISV

EVERY GRID IS A DISV. A structured grid is the mesher's regular lattice with
no refinement; cell ids are (layer, icpl) pairs, never row-and-column.
`authoring/mesh_files.py` writes the DISV6 file once per mesh and layering into
the accepted mesh artifact's `engine_files`, keyed by the layering's digest;
the sheet states the layering (`DISV_NLAY`, `DISV_TOP`, `DISV_BOTM`) and no
sheet states the mesh's tables.

A structured example reaches the DISV through `gridgen` with no ops: `build` states
`delr` and `delc` (a number or one width per column and row) with `xoff` and `yoff`,
and with both spacings stated the recipe needs no `resolution_m` - its finest edge is
the narrowest stated width. The grid's display face is a `.2dm` of four-node
elements (`E4Q`), which the accepted-mesh reader returns as `(M, 4)` cells in the
mesher's row-major order; a mixed file refuses. A package's stress composite takes one
statement or a list of them, so one package places different cells, layers and values
(wells at different cells on different layers) under one bound. ex-gwf-twri01 so
authored equals its DIS reference bitwise.

## The authoring

A raster is read onto the model's frame at the sampler
(`authoring/griddata.py`): each cell takes the raster's value at its centre,
and the unit the raster publishes converts through the declared unit table onto
`DISV_LENGTH_UNITS` and `TDIS_TIME_UNITS`. A raster publishing no unit, a
frame stating none, a unit of another dimension and a cell the raster measures
nothing at each refuse by name.

The aquifer is a coercion of the GWF wrapper: `Aquifer(thickness |
saturated_thickness, water_table_depth, conductivity | transmissivity [,
layers])` states an aquifer as cell expressions against the accepted mesh -
every argument required, exactly one of each pair, a thickness at or under zero
refused. `layers` (an integer from 1, unstated one) splits the saturated
thickness under the water table into that many equal layers (a whole `thickness`
splits from the land surface): K is the same in each, only layer 1 is
convertible (unconfined) and layers below it are confined, and the starting
head is the water-table elevation in every layer. The stress composites
(`Recharge`, `Drains`, `GeneralHead`, `ConstantHead`, `Wells`) place a geometry
on the cells and state their package's period data; a geometry reading nothing
states nothing, and a missing value refuses rather than defaulting. A statement may name
the geometry `{"cells": "edge"}` (the cells with a face on the mesh's outer boundary), the
layer `"all"` (every solved layer of each placed cell) and a value `{"cells": "start"}` (the
run's starting head, `IC_STRT`, at the placed cells): a `ConstantHead` so stated holds the
domain's perimeter at the head the Aquifer composite starts from.

SFR is stated by its own dictionary keywords (`SFR_NREACHES`, `SFR_PACKAGEDATA`,
`SFR_CONNECTIONDATA`, `SFR_PERIODDATA`, the options): ex-gwf-sfr-p01 authored on a
gridgen DISV equals flopy's own DIS reference bitwise (heads, every reach's stage, every
reach's downstream flow, max difference 0.0). A cellid on a DISV is `[layer, cell]`,
0-based. The `Streams` composite places lines as those reaches: `Streams(geometry, width,
slope, bed_top, bed_thickness, bed_conductivity, manning[, layer])`, or a list of them.
Each line is cut by the cells it crosses in its digitized order (downstream), one reach per
piece inside a cell, its length the line's length there on `DISV_LENGTH_UNITS`, its layer the
cell's top solved layer unless `layer` states one. Consecutive reaches connect; a line ending in
a cell that holds one reach of another line (not that line's last) joins it, and two such reaches
there refuse. Reaches are numbered from 0, upstream before downstream. `ustrf` is 1.0 and `ndv`
0 (no diversion is stated here). A line's part past the mesh edge is left out, as for the
other stresses; one that leaves the mesh and returns refuses naming the cells. A stated
`SFR_NREACHES` or `SFR_CONNECTIONDATA` beside `Streams` refuses; inflow and the period
data are stated through `SFR_PERIODDATA` by reach number. `manning` is a number, the others
a number or a raster read at each reach's cell centre.

## The coupling

A coupled run is ONE simulation holding a GWF model and a GWT model, a GWE model,
or both, whose name files ALL name the same staged DISV, each joined to the flow by
its exchange (GWF-GWT, GWF-GWE). This is
proven: mf6 6.7.0 validates and solves it, and the concentration output reads
exactly like a head - `HeadFile(..., text="CONCENTRATION")`, the same 4-D layout
the head file carries on DISV. Proven through the product: the p01 strip
authored by this walk (1 x 101 cells, joined quads) reproduces the spike's DIS
reference bitwise (max|dC| = 0.0 at dispersivity 0 and 10). The energy coupling
is proven the same way: ex-gwe-vsc's VSC-on branch (flow, transport and energy,
31 x 46 cells) authored on DISV equals its DIS reference to 2e-14 (temperature
bitwise).

THE COUPLING COMPOSITES LIVE IN `workflows/modflow/`. `Transport` and `Energy` are
composites of the GWF wrapper (`modules/gwf.py`), one factory over the coupled
module word (`gwt`, `gwe`); each takes `given` (the inputs that carry
the body - a body whose given inputs all hold nothing composes nothing) and
`slots` (the coupled module's dictionary identifiers - an unknown one refuses by name, and
`TDIS`/`DISV` refuse as the simulation's own). A stress statement in a slot is
the coupled wrapper's own composite's shape: `ConstantConcentration` and
`MassSource` (`modules/gwt.py`), `ConstantTemperature` and `EnergySource`
(`modules/gwe.py`) are the shared `_stress` factory (`modules/module.py`) bound
to CNC, SRC, CTP and ESL (their dimensions are not length-rate words, so a raster
refuses - numbers only), expanding geometry + layer + value
into the package's period data; a geometry reading nothing states nothing and a
missing value refuses. The composed body names its module and its slots and
expands into the second model and the exchange - into the simulation authoring,
not into keyword values on the carrier's sheet.

A run's `keywords` reach these bodies through ONE floor (`keyword_floor`,
`workflows/runtime/module.py`; `modflow/workflow.py::_floor` hands it
`run_bodies`): a `gwt: KEYWORD` qualifier resolves on the transport deck alone,
a bare name resolves where exactly one body spells it and two (`IMS_PRINT_OPTION`,
`IC_STRT`, the `OC_*` family) refuse naming both, and `TDIS_*`/`DISV_*` - the
simulation's own clock and grid, `GWF.CARRIER_OWNED` - resolve as the carrier's
wherever they are spelled and refuse when restated onto a coupled slot. The
carrier's values fill last over the template; a coupled body's compose through
`Transport`/`Energy` itself - the same slot checks, stress expansion and
TDIS/DISV refusals a template statement takes - and merge over the template's
statement of that deck, never replacing it. `Workflow.input` answers off the
same bodies. A qualifier also NAMES the body: where the template states no
`Transport`/`Energy`, the run's qualified names add gwt/gwe to the run's bodies
(`run_bodies`), the composite creating that deck with only what the run stated,
and its required keywords refuse at the write (`_unstated`,
`MODFLOW_KEYWORD_UNSTATED`) exactly as a template-stated body's do. A body the
carrier cannot couple (a qualifier for no registered module word) is
identify_on's to refuse naming the run's bodies.

serialize writes the coupled sheet model by model (`authoring/simulation.py`):
one name file per model over the SAME filename-only DISV, one IMS per model,
both name files listing `mesh.disv`, the `Periods`/moment filling only the
carrier, and the exchange written through `sim.exchange_files` as
`GWF6-GWT6 <carrier>.<carrier><gwt>` or `GWF6-GWE6 <carrier>.<carrier><gwe>`
(`_EXCHANGES` holds the pairs; any other refuses). Each model's required packages are
unstated-refused (IC and MST for gwt; IC and EST for gwe, whose porosity and solid
heat capacity and density are stated, never defaulted), with CNC/SRC period data placed by the
stress walk against the shared grid - a point off the mesh refuses
POINT_NOT_VIABLE. SSM is NOT defaulted: mf6 refuses an SSM whose flow model
carries no boundary flows, and flopy has no empty `SOURCES` record, so
`SSM_SOURCES` is a stated keyword like every other. The converse holds: a flow
model WITH boundary packages refuses validation with no SSM at all, so where
nothing carries concentration the run states the package itself (`SSM_SAVE_FLOWS`)
and the authoring writes an OPTIONS-only SSM carrying no source record - which
mf6 accepts.

A flow stress (`Recharge`, `Wells`, `Drains`, `GeneralHead`, `ConstantHead`) may state a
`concentration` beside its values: water carrying a solute. It places as the package's
AUXILIARY `CONCENTRATION` column (`RCH_AUXILIARY`), every statement of one package stating it
or none. Unstated, the transport body's `SSM_SOURCES` is written by the authoring: each
carrying package by the name the flow model's name file gives it, with `AUX CONCENTRATION`,
so mf6's SSM reads it; no template names a package. A transport slot naming
a run input that holds a number takes it as a constant (`MST_POROSITY`, `DSP_ALH`, `IC_STRT`).

For a coupled run the manifest's `module` is the single answering word (the first
coupled body stated - `gwt` where a run couples both; flow is the machinery).
BUY and VSC are GWF packages stated by their own dictionary keywords on the
carrier; their PACKAGEDATA names the coupled model by its module word ("gwt",
"gwe"). An option's name list (AUXILIARY) is written as stated, never placed as
cell data. The worker needs no change:
it runs whatever `mfsim.nam` names, `results` names the files that must exist
after the solve, and `module` is provenance the metrics state. The alternative
- GWT in its own simulation reading a prior flow run's head and budget through
FMI - is proven and remains the path for reusing a solved flow without
re-running it; the examples' own transport bodies use the exchange, and so does
the product.

## The publish

The engine's own reading of a solved run (`modules/outputs.py`) opens the files
the output control saved - for every model: `_SAVED` maps HEAD,
CONCENTRATION and TEMPERATURE to their own OC filerecords, and a coupled body reads its
filerecord from its own slots over the carrier's sheet (the clock and units the
simulation owns are one for both models). The publish's table walks every
module's output rows: the carrier's read off the run's own module, a coupled
body's tagged with its module word. `modules/ugrid.py` is the ONE transform
`render.md` allows an engine whose output is not in the format set to carry:
the accepted mesh and the read values in, a UGRID NetCDF out with
cell-centred, time-stepped dataset groups. DIS gets no raster path of its own -
a structured grid is a quad mesh and rides the same transform, so there is one
result path and a rotated or variably spaced grid needs no resampling. A series
at a measured well is checked against the reading in its own quantity - a head in
the model's length unit, a concentration in the unit the run states - at the saved
time the reading's window holds, nearest the moment it was sampled. On a run of several
layers a well's published depth below the land surface picks the layer it is read on; a
well publishing none, or reaching below the model base, is left out and said. mf6 carries no
concentration unit, so a template whose OBSERVE row observes `CONCENTRATION` seats a
required `concentration_units`; `published_units` reports it, the measured reading
converts into it at ingestion or refuses, and the concentration layer carries it.

## The plume template

`modflow_plume` answers "where does a dissolved release go": the steady unconfined flow of
`modflow_water_table` with a transient transport body. The release is RECHARGE over a stated
`source` polygon at `source_rate` (a depth per day, spread over the polygon as drawn so the cited
volume is conserved) carrying `source_concentration`, beside the natural recharge carrying
`background_concentration`; porosity and both dispersivities are stated inputs and the template
holds no number of its own. The period is the `Periods` composite: `until` (an ISO instant) and
`time_steps` (an integer) state one period from the run's start to that instant, over equal steps,
in `TDIS_TIME_UNITS`. The `observe` row asks the wells reporting
`solute` at the snapshot and the run charts the modelled against the measured concentration at
each: extent and shape, never a mass balance. The domain's perimeter is held at constant head at the
starting head (the fetched water-table depth under the bed, every solved layer): the aquifer
continues past the domain, and a closed edge with the streams as the only outlet piles the
recharge up above the land. The kettle ponds are not yet lakes, so the cells around them can
still stand above land. A finished run records the steps its staged clock
marched as the completion's `ntimestep`, which the packet cross-checks the frames against.

## The worker

`workers/modflow/entrypoint.py` runs mf6 in `/data` under `--network none` and
writes `modflow_metrics.json`: the module word, the normal-termination verdict,
the listing tail on failure. Status is the exit code AND mf6's own
normal-termination sentence together - a clean process that never finished is
an error (`engine.py::classify_exit`). The image holds the pinned mf6 binary
and nothing else: no FloPy, no MT3DMS, no MODFLOW-2005. The daemon authors,
the image solves, and every MODFLOW, FloPy and MDAL behaviour the authoring
builds on was proved through that image before it was built on.

## What is not built yet

The accepted-mesh reader of a ragged (UGRID) display face - a quadtree with
refinement, a Voronoi - so those meshes cannot yet be authored on; the
advanced-package composites (LAK, MAW,
UZF, CSUB; SFR is placed by `Streams` but its MVR mover is not wired), OBS and time series, the model exchanges (GWF-GWF, GWT-GWT), FMI
(the transport reading a prior flow run's saved budget), and PRT. Each is a leg
in the scope's plan; the coupling above is built for GWT on the exchange.
