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

## The authoring

A raster is read onto the model's frame at the sampler
(`authoring/griddata.py`): each cell takes the raster's value at its centre,
and the unit the raster publishes converts through the declared unit table onto
`DISV_LENGTH_UNITS` and `TDIS_TIME_UNITS`. A raster publishing no unit, a
frame stating none, a unit of another dimension and a cell the raster measures
nothing at each refuse by name.

The aquifer is a coercion of the GWF wrapper: `Aquifer(thickness |
saturated_thickness, water_table_depth, conductivity | transmissivity)` states
a one-layer aquifer as cell expressions against the accepted mesh - every
argument required, exactly one of each pair, a thickness at or under zero
refused. The stress composites (`Recharge`, `Drains`, `GeneralHead`,
`ConstantHead`, `Wells`) place a geometry on the cells and state their
package's period data; a geometry reading nothing states nothing, and a
missing value refuses rather than defaulting.

## The coupling

A coupled run is ONE simulation holding a GWF model and a GWT model whose name
files BOTH name the same staged DISV, joined by the GWF-GWT exchange. This is
proven: mf6 6.7.0 validates and solves it, and the concentration output reads
exactly like a head - `HeadFile(..., text="CONCENTRATION")`, the same 4-D layout
the head file carries on DISV. Proven through the product: the p01 strip
authored by this walk (1 x 101 cells, joined quads) reproduces the spike's DIS
reference bitwise (max|dC| = 0.0 at dispersivity 0 and 10).

THE COUPLING COMPOSITE LIVES IN `workflows/modflow/`. `Transport` is a composite
of the GWF wrapper (`modules/gwf.py`): it takes `given` (the inputs that carry
the body - a body whose given inputs all hold nothing composes nothing) and
`slots` (GWT dictionary identifiers - an unknown one refuses by name, and
`TDIS`/`DISV` refuse as the simulation's own). A stress statement in a slot is
the GWT wrapper's own composite's shape: `ConstantConcentration` and
`MassSource` (`modules/gwt.py`) are the shared `_stress` factory
(`modules/module.py`) bound to CNC and SRC, expanding geometry + layer + value
into the package's period data; a geometry reading nothing states nothing and a
missing value refuses. The composed body names its module and its slots and
expands into the second model and the exchange - into the simulation authoring,
not into keyword values on the carrier's sheet.

serialize writes the coupled sheet model by model (`authoring/simulation.py`):
one name file per model over the SAME filename-only DISV, one IMS per model,
both name files listing `mesh.disv`, the `Periods`/moment filling only the
carrier, and the exchange written through `sim.exchange_files` as
`GWF6-GWT6 <carrier>.<carrier><gwt>`. Each model's required packages are
unstated-refused (IC and MST for gwt), with CNC/SRC period data placed by the
stress walk against the shared grid - a point off the mesh refuses
POINT_NOT_VIABLE. SSM is NOT defaulted: mf6 refuses an SSM whose flow model
carries no boundary flows, so `SSM_SOURCES` is a stated keyword like every
other.

For a coupled run the manifest's `module` is the single answering word (`gwt` -
transport is the question, flow is its machinery). The worker needs no change:
it runs whatever `mfsim.nam` names, `results` names the files that must exist
after the solve, and `module` is provenance the metrics state. The alternative
- GWT in its own simulation reading a prior flow run's head and budget through
FMI - is proven and remains the path for reusing a solved flow without
re-running it; the examples' own transport bodies use the exchange, and so does
the product.

## The publish

The engine's own reading of a solved run (`modules/outputs.py`) opens the files
the output control saved - for either model: `_SAVED` maps HEAD and
CONCENTRATION to their own OC filerecords, and a coupled body reads its
filerecord from its own slots over the carrier's sheet (the clock and units the
simulation owns are one for both models). The publish's table walks every
module's output rows: the carrier's read off the run's own module, a coupled
body's tagged with its module word. `modules/ugrid.py` is the ONE transform
`render.md` allows an engine whose output is not in the format set to carry:
the accepted mesh and the read values in, a UGRID NetCDF out with
cell-centred, time-stepped dataset groups. DIS gets no raster path of its own -
a structured grid is a quad mesh and rides the same transform, so there is one
result path and a rotated or variably spaced grid needs no resampling. A series
at a measured well is checked against the reading in its own quantity, in the
model's length unit, at the saved time nearest the moment it was sampled.

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

The GWE body's packages (and the GWF-GWE exchange), the structured examples
through the mesher's lattice, the advanced-package composites (SFR, LAK, MAW,
UZF, CSUB), OBS and time series, the model exchanges (GWF-GWF, GWT-GWT), FMI
(the transport reading a prior flow run's saved budget), and PRT. Each is a leg
in the scope's plan; the coupling above is built for GWT on the exchange.
