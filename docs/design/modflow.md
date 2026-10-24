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
solve a field on the grid through IMS - and PRT (particle tracking), which solves
explicitly and writes tracks. Each is a `ModflowModule` wrapper over a
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
the units stay empty: they are the ones the modeller declared. PRT's table is its
tracks: pathlines, endpoints and the travel times (the section on particle tracking).

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
saturated_thickness, water_table | water_table_depth, conductivity | transmissivity [,
layers])` states an aquifer as cell expressions against the accepted mesh -
every argument required, exactly one of each pair, a thickness at or under zero
refused. `water_table` is an elevation on the run's vertical frame (`{"cells": "elevation"}`; a raster states
the datum it counts from and it names the run's frame, or the cells refuse): the starting head is that
elevation, a saturated thickness lies under it, and the top stays the bed; a head above the bed stands where it
is stated, as a negative `water_table_depth` does. `layers` (an integer from 1, unstated one) splits the saturated
thickness under the water table into that many equal layers (a whole `thickness`
splits from the land surface): only layer 1 is convertible (unconfined) and
layers below it are confined, and the starting head is the water-table
elevation in every layer. K is the same in each layer unless `conductivity` is a
list of one number or raster per layer, top first, as long as `layers` (a
length mismatch refuses naming both; the list is the `{"cells": "layers"}`
statement, each entry placed as a single K is). `conductivity` may also be a
layered source read by depth, `{"layers": [K_1..K_m], "surfaces": [S_0..S_m]}` (`{"cells": "by_depth"}`):
m conductivities and m + 1 elevations, the top of source layer 1 then each layer's bottom, each a number
or a raster read as a single K or a length is (a surface raster states a vertical datum that names the
run's `vertical_frame`, else it refuses; a number is on the model's frame). Each cell and Aquifer layer,
between the grid's placed top (or the bottom above it) and its own `DISV_BOTM`, takes the
thickness-weighted arithmetic mean of the source layers it overlaps, a source cell unmeasured at its K or
either surface weighing zero; a part of the layer outside the source is ignored, and a layer overlapping
no measured source layer at some cell refuses naming the layer and how many cells lie above the source's
top, below its bottom or over unmeasured cells - the nearest layer is never extended. Where `layers` and
`surfaces` each name an input holding a whole list of fetched frames (the two published-model rows), the
frames are paired by the layer each frame's id states, never by position: layers run 1 to the deepest K
frame, a layer or a surface with no frame is unmeasured and weighs zero. A by-depth statement whose names
the run holds nothing for is no conductivity, so `transmissivity` states the aquifer; both stated still
refuses. The two templates declare the K pair optional and standing in for `transmissivity`
(`Input.optional_need`, workflows.md): inside a published grid the sheet states conductivity by depth and
asks no transmissivity, anywhere else, or when the run states a transmissivity, it states that. Inside a published
grid the water table and the saturated thickness come from the same model as the K: `fetch_usgs_model_water_table`
(the head of the cell's topmost active layer, an elevation on NAVD88, Cape Cod put onto it per cell by the local PROJ-grid seam, VERTCON 2) and
`fetch_usgs_model_thickness` (that head over the bottom of the cell's deepest active layer) answer the templates'
optional `water_table` input, standing in for `water_table_depth`, and the thickness; both measure the same
cells, and each answers only where it measures every cell of the box (a box a grid holds in part, or one with an
unmeasured cell, is the row's empty, and the fill takes Zell-Sanford's depth and thickness for both). Outside
the grids the Zell-Sanford rows answer as before, and a stated depth or elevation wins. The run's head is then
the model's, not a depth below the run's bed, so every layer lies inside the model's range by construction. `transmissivity` stays one
value, a list or a layered source there refuses; NPF_K33 is not written, so mf6 takes it equal to
each layer's K. The stress composites
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
0 unless a `Diversions` statement says otherwise. A line's part past the mesh edge is left out, as for the
other stresses; one that leaves the mesh and returns refuses naming the cells. A stated
`SFR_NREACHES` or `SFR_CONNECTIONDATA` beside `Streams` refuses; `manning` is a number, the
others a number or a raster read at each reach's cell centre.

LAK is stated by its own dictionary keywords (`LAK_NLAKES`, `LAK_PACKAGEDATA`,
`LAK_CONNECTIONDATA`, `LAK_PERIODDATA`, the options; `LAK_NOUTLETS` and `LAK_NTABLES` are
read as required though mf6 defaults them to zero, so a keyword deck states both):
ex-gwf-lak-p01 authored on a gridgen DISV, the lake cells inactive through `DISV_IDOMAIN`,
equals flopy's own DIS reference bitwise (heads in five layers, the lake stage, max
difference 0.0). The `Lakes` composite places polygons as lakes: `Lakes(geometry, stage,
bottom, leakance[, rainfall, evaporation])`, or a list of them; each polygon (each part of a
multi-polygon) is one lake. A lake holds the cells whose centres its polygon contains, in
every solved layer whose top lies above its `bottom` (a number or a raster read at each
cell centre); those cells are inactive in the DISV the run stages. flopy's
`get_lak_connections` has no embedded lake for a vertex grid, so the connections are written
here from the mf6 LAK input description: each lake cell connects HORIZONTALLY to every solved
cell that is not a lake cell across a face they share (belev and telev the neighbour's bottom
and top, connlen the perpendicular distance from the neighbour's centre to the face, connwidth
the face's length, both on `DISV_LENGTH_UNITS`) and VERTICALLY to the solved cell below it;
connections run by lake cell (layer, cell) and then by neighbour id, which on a gridgen mesh
is flopy's order, and the same p01 footprint placed as a polygon reproduces the keyword
deck's heads and stage bitwise. `stage`, `bottom` and `leakance` are stated or the
composite refuses by name; a `rainfall` or `evaporation` not stated is absent from the
package. A polygon holding no cell centre, a cell in two lakes and a lake with no solved
cell beside or below it refuse. `LAK_NLAKES`, `LAK_CONNECTIONDATA` and, beside a stated
flux, `LAK_PERIODDATA` stated beside `Lakes` refuse. The lake stage is not a read the run
publishes.

MAW is stated by its own dictionary keywords (`MAW_NMAWWELLS`, `MAW_PACKAGEDATA`,
`MAW_CONNECTIONDATA`, `MAW_PERIODDATA`, the options): ex-gwf-maw-p01, both variants, authored
on a gridgen DISV equals flopy's own DIS reference bitwise (heads in both layers, the well
head, the head and per-connection flows of the observation file, max difference 0.0). The
`MultiAquiferWells` composite places points as wells: `MultiAquiferWells(geometry, radius,
bottom, start_head, condeqn, screen_top, screen_bottom, rate[, hk_skin, radius_skin])`, or a
list of them; each point (each part of a multi-point) is one well in the cell holding it,
numbered in order, with one connection to every solved layer its screen crosses (the screen's
bottom under the layer's top and its top over the layer's bottom, so a screen that only touches
a layer's boundary does not connect it). Each connection carries the stated screen, which mf6
clips to the cell. `condeqn` is THIEM, SPECIFIED, SKIN, CUMULATIVE or MEAN; the skin values are
those the equation reads (SKIN, CUMULATIVE and MEAN both, SPECIFIED the conductance as
`hk_skin`, THIEM none), a value the equation does not read refuses if stated and is written as
-999, the column mf6 takes any value in. `rate` is the well's rate from period 1. Every value is
stated or the composite refuses by name; a point on no cell, or in a cell the run does not
solve, refuses with the nearest viable cell, and a screen crossing no solved layer there
refuses. Two wells may share a cell. `MAW_NMAWWELLS`, `MAW_CONNECTIONDATA` and
`MAW_PERIODDATA` stated beside `MultiAquiferWells` refuse. The well head is not a read the run
publishes; a head-controlled or flowing well is stated through the keywords.

UZF is stated by its own dictionary keywords (`UZF_NUZFCELLS`, `UZF_NTRAILWAVES`,
`UZF_NWAVESETS`, `UZF_PACKAGEDATA`, `UZF_PERIODDATA`, the options): the GWF half of
ex-gwt-uzt-2d (CHD and UZF over 20 layers, a stacked column of UZF cells) authored on a
gridgen DISV equals flopy's own DIS reference bitwise (heads, the UZF budget records and the
model budget, all 120 steps, max difference 0.0). The `UnsaturatedZone` composite places
polygons: `UnsaturatedZone(geometry, surfdep, vks, thtr, thts, thti, eps, finf[, pet, extdp,
extwc, ha, hroot, rootact])`, or a list of them; the top solved cell of every column whose
centre a polygon holds is a land-surface UZF cell (landflag 1), and each solved layer under it,
down to the first layer the run does not solve, is a UZF cell joined to the one above by
`ivertcon`. Cells are numbered by layer then cell; a property is a number or a raster read at
the column's centre and holds for the whole column (surfdep is read only by the land cell), and
`finf` is the land cell's infiltration from period 1. The ET values are those the stated UZF
options read: `UZF_SIMULATE_ET` reads `pet` and `extdp`, `UZF_UNSAT_ETWC` `extwc`,
`UZF_UNSAT_ETAE` `ha`, `hroot` and `rootact`; a value stated for no option that reads it
refuses, one an option reads and nothing states refuses, and a period column no option reads is
written as -999 (mf6 takes any value in it). Every value is stated or the composite refuses by
name; a polygon holding no cell centre, or only columns the run solves on no layer, refuses, and
a column in two statements refuses. `UZF_NUZFCELLS` and `UZF_PERIODDATA` stated beside
`UnsaturatedZone` refuse, as does `Recharge` (or `RCH` rows) on the column of a land cell: the
same infiltration counted twice. `UZF_NTRAILWAVES` and `UZF_NWAVESETS` are mf6's resolution
dimensions and stay keywords.

### Period settings of the placement composites

`Streams`, `Lakes`, `MultiAquiferWells` and `UnsaturatedZone` each take every period setting
mf6 defines for their package (gwf-sfr, gwf-lak, gwf-maw, gwf-uzf `.dfn`, block `period`), by
mf6's own names: SFR `status, bedk, manning, stage, inflow, rainfall, evaporation, runoff,
upstream_fraction, auxiliary`; LAK `status, stage, rainfall, evaporation, runoff, inflow,
withdrawal, auxiliary` and `outlets` (`to, couttype, rate | invert, width, rough, slope`); MAW
`status, rate, well_head, head_limit`, the FLOWING_WELL record `fwelev, fwcond, fwrlen`, SHUT_OFF
`minrate, maxrate`, RATE_SCALING `pump_elevation, scaling_length`, `auxiliary`; UZF `finf`, the ET
values and `auxiliary`. A setting is a number from period 1 or `{period from 1: value}`; a setting
not stated is absent from the package, never a zero. A record is stated whole and in the same
periods; `status` and `head_limit` take their words (`ACTIVE|INACTIVE|CONSTANT`, SFR
`ACTIVE|INACTIVE|SIMPLE`, `OFF`). Where the package's table holds the value too (SFR `manning`,
LAK `stage` and the outlet `invert, width, rough, slope`, the SFR/LAK/MAW `auxiliary` columns) a
plain number is the table's alone and a keyed value states period 1 as the table's and every entry
as a row. A well needs a `rate` wherever it is ACTIVE; the flowing-well values need
`MAW_FLOWING_WELLS`; `auxiliary` names must be the package's AUXILIARY names. SFR settings land on
every reach of the line, `inflow` on its upstream reach. A UZF period line carries every column, so
a keyed value writes a line per period named with the value in force in each column. SFR
`diversion` is the `Diversions` statement below, not a `Streams` argument. The dictionary marks an entry not mandatory when its own DFN description says mf6
runs without it (`EVT_NSEG`, `EVTA_AUX`, `LAK_NOUTLETS`, `LAK_NTABLES`, `STO_SY`).

### Water moved between features, diversions, cross sections and lake tables

`Streams`, `Lakes`, `MultiAquiferWells` and `UnsaturatedZone` statements take an optional
`name`, which a statement placing several features gives them all. `WaterMover(from, to, type,
value[, from_at, to_at, from_outlet])` (a mapping or a list) is the MVR package: `from` and `to`
are names, `from_at` and `to_at` a point read as the cell it lies in (never moved; a miss
refuses with the nearest-viable hint), and an end resolves to exactly one feature or the
statement refuses naming how many it matched. A stream gives and takes water by its reach, a
well by its number, an `UnsaturatedZone` column by its land cell (rejected infiltration out,
extra infiltration in), a lake takes water by its number and gives it through an outlet
(`from_outlet` from 1, optional when it has one). `type` is `FACTOR` (a fraction 0 to 1),
`EXCESS`, `THRESHOLD` or `UPTO` (a rate); `value` is stated, a number from period 1 or
`{period from 1: number}`, and a period it names restates every mover with the value in force
(an mf6 period block replaces the one before). The authoring numbers the features, names the
packages as the flow model does, derives `MVR_MAXMVR`, `MVR_MAXPACKAGES`, `MVR_PACKAGES` and
sets the MOVER option of each package used; stating any of them beside `WaterMover`, or a MOVER
option false, refuses.

`Diversions(from, to, type, value[, from_at, to_at])` is the same grammar over `Streams`
reaches, `type` `FRACTION|EXCESS|THRESHOLD|UPTO` (`FRACTION` 0 to 1). The target must have no
upstream reach and not be the source's downstream reach; it becomes a downstream connection of
the source, numbered after it, with USTRF 0.0 (it receives only the diverted water); the
source's NDV counts its diversions (`idv` from 0 in statement order), `SFR_DIVERSIONS` and the
`diversion` period rows are derived. `Streams` takes `cross_section`, the rows `(xfraction,
height[, manfraction])` of the reach cross-section table every reach of the line reads, and
`Lakes` takes `table`, the rows `(stage, volume, sarea)` of the lake's stage-volume-area table
(a statement placing one polygon); a table is stated or a run input holding the rows, a raster
is no table and refuses, and mf6's own validation judges the values. Authoring writes each as a
TAB6 file beside the package (`sfr_xs<k>.tab`, one per distinct cross section;
`lak_tab<lake>.tab`) through flopy's child package, and derives `SFR_CROSSSECTIONS`,
`LAK_TABLES` and `LAK_NTABLES`.

### Interbeds, time series and the boundary givers

CSUB is stated by its own dictionary keywords: the options (`CSUB_HEAD_BASED`, `CSUB_COMPRESSION_INDICES`,
`CSUB_NDELAYCELLS`, the output records), the coarse-grained arrays (`CSUB_CG_SKE_CR`, `CSUB_CG_THETA`, `CSUB_SGM`,
`CSUB_SGS`: a number, a list per layer or a raster) and `CSUB_STRESS_PERIOD_DATA` (a period block is no
requirement while `CSUB_MAXSIG0` is unstated: the dictionary does not mark an entry shaped by an optional
dimension alone mandatory). ex-gwf-csub-p02 (a, b), p03 (a, b) and p04 authored this way on a gridgen DISV equal
flopy's own DIS reference bitwise (heads, the four compaction files, every budget record); p01, whose 0.5 m cells
the DISV's millimetre vertex coordinates distort, equals flopy authored on the same DISV bitwise and the DIS reference
to 4.5e-7 in head. The `Interbeds` composite places the interbed list:
`Interbeds(geometry, layer, cdelay, pcs0, thick_frac, ssv_cc, sse_cr, theta[, rnb, kv, h0, name])`, or a list of
them; every cell the geometry places on the layer (an integer from 1, or `"all"`) is one interbed, numbered from 0
in statement, layer then cell order, and `CSUB_NINTERBEDS` and `CSUB_PACKAGEDATA` are derived (stating either beside
it refuses). `cdelay` is `DELAY` or `NODELAY`; `rnb`, `kv` and `h0` are the delay interbed's own, required for
`DELAY`, refused for `NODELAY` and written -999 (mf6 takes any value there). `thick_frac`, `theta`, `rnb`, `kv` and
`h0` are a number or a raster read at each cell centre; `pcs0`, `ssv_cc` and `sse_cr` change dimension with the
options, so they are numbers. `name` is the boundary-name column, required exactly when `CSUB_BOUNDNAMES` is true.

`TimeSeries(package, names, rows[, units, interpolation, sfac])`, or a list of them, writes the package's time-series
file (`<package>.ts`, through flopy's child package, which also states the package's `TS6` option) for any package that
takes one, on the carrier and on a coupled body (`CNC_TS_FILERECORD` stated in the `Transport` slots as the same
mapping): `names` are the series (the columns after the time), `rows` the `[time, value per name]` lines in increasing
time, or the name of a run input holding one record (one name; its times count from the run's start and are written in
`TDIS_TIME_UNITS`, `units` converts its values through the input's own unit table), `interpolation` (`STEPWISE`,
`LINEAR`, `LINEAREND`) and `sfac` mf6's own attributes, once or per series and absent when unstated. The package's
keyword rows name a series by its name (`CSUB_STRESS_PERIOD_DATA` `[[layer, cell], "LOAD"]`, a CHD row's head), and a
stress composite's value column names one as `{"series": NAME}`, refused when the package's `TimeSeries` states no such
name; one file feeds a package, and `<PKG>_TS_FILERECORD` stated by keyword refuses. ex-gwf-csub-p01 (the
passing-train load), p03 (three CHD head series) and ex-gwf-fhb (a CHD and a WEL series) are built on it.

`Wells`, `Drains`, `GeneralHead` and the new `Rivers(geometry, layer, stage, conductance, rbot[, name])` give water to
the mover: each takes an optional `name`, and every row such a statement places is one feature (package WEL, DRN, GHB
or RIV; its number the row's index in the package's period list, which the composites state in period 1 only).
`WaterMover` takes them as `from` (by name, by a point in the row's cell, or both) and sets the package's `MOVER` option;
as `to` they refuse naming the package, since mf6 moves water to SFR, LAK, MAW and UZF only. The four givers are proven
against a flopy-authored reference (a drain to a lake, a well, a river and a general-head boundary to reaches and a
lake; heads, stages and every mover flow equal, 0.0).

### Observations

`Observations(type, name[, package, at, on, layer, connection, to_at, to_layer, digits])`, or a list, is one line of
an OBS file; it is on GWF, GWT and GWE. `type` is mf6's own observation type (head, drawdown, concentration,
flow-ja-face, or a package's: stage, downstream-flow, maw ...) and `name` mf6's observation name; mf6's validate judges
the type, the composite the id. With no `package` the line is the model's OBS file (`OBS_CONTINUOUS`, with
`OBS_DIGITS`), with one it is that package's own (`<PKG>_OBS_FILERECORD`). The id follows the point contract: a cell
is `at` (a map point; the cell is computed, a miss refuses with the nearest cell and the point is never moved) on a
`layer` from 1, with `to_at` and `to_layer` the second cell of flow-ja-face; a placed feature (SFR reach, LAK lake, MAW
well, UZF cell, CSUB interbed, or a WEL, DRN, GHB or RIV row) is `on` (the name its composite gave), `at` (the point in
its cell) or both, its id the feature's number, a boundary row's its cell, and a named boundary row resolves to its
cell for a model-level head; `connection` (from 1) is the second id of a MAW or LAK connection. One CSV is written per
file owner and type (`<module>.obs.<type>.csv`, a package's `<module>.<pkg>.obs.<type>.csv`) and listed among the
run's results, and `digits` is the file's option (one value per file). The raw path is the dictionary's:
`OBS_CONTINUOUS` stated as flopy's `{file: rows}` and `<PKG>_OBS_FILERECORD` as `{"continuous": {file: rows},
"digits": n}`; any other value of a package's OBS record refuses rather than being dropped. The publish's table adds
one chart per CSV the run states (`observations` read, `Series.label` the main line's name): a line per observation, y
in the model's length unit for head and drawdown, the run's concentration unit for concentration and unitless
otherwise, a dry or inactive value left out of its line. ex-gwf-fhb (head and flow-ja-face), ex-gwf-maw-p01 (head and
two connection flows, digits 10) and ex-gwf-sfr-p01 (four stages and four downstream flows, digits 10) equal the
published scripts' own OBS CSVs bitwise.

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

## Model exchanges and the unstructured grid

A LOCAL GRID REFINEMENT is a second GWF model on a grid cut out of the first. `LocalGridRefinement(geometry,
refinement[, layers, name, slots, exchange, transport])` is a composite of the GWF wrapper (`modules/gwf.py`), or a list of
them. The polygon `geometry` holds the parent cells whose centres it contains; `refinement` is the integer count of child
cells along a parent cell's side, from 2 and never defaulted; `layers` the child layers in each parent layer, top first
(unstated, one each - the refined layers start at layer 1 and run unbroken, 0 below them, and a parent layer not refined
stays solved under the child); `name` the child's model name (default `gwf-child`, then `gwf-child2` by statement position;
1 to 16 letters, digits, hyphens or underscores, never `gwf`, `gwt` or `gwe`). `slots` states the child's own keywords and
the composites a deck states (`Wells`, `Streams`, `Lakes`, `Observations` ... as the carrier does, placed on the CHILD's
cells), checked against the GWF dictionary; `TDIS` and `DISV` refuse as the simulation's own, and `IMS` refuses because the
child solves in the carrier's solution. `exchange` is the exchange's own options (`xt3d`, `print_flows`, `print_input`,
`save_flows`). The composed body is a coupled body of the sheet keyed by its name, carrying `refine`; a qualified run keyword
(`gwf:X`) does not route to it - a child speaks only through its statement.

THE PARENT IS A LATTICE. `authoring/refinement.py::lattice` reads the accepted mesh as one rectangle per cell on a regular
lattice (its row 0 the northern row, whatever the mesh's own numbering) or refuses naming what the refinement needs; the
triangle, Voronoi and quadtree meshers hand it no lattice. flopy's `Lgr` cuts the child out of that lattice, the parent's
stated top and bottoms, and the zeros: the parent's `idomain` gains 0 under the polygon in the refined layers (written into
the staged DISV by `mesh_files`, and into the carrier's active cells, merged with a stated `DISV_IDOMAIN` and the lake
cells; a covered cell the run does not solve refuses), the child's DISV is written into the run directory as
`<name>.disv` by the same `_write_disv` (its vertices a row-major lattice of the child, the options those of the parent's
DISV), and `Lgr.get_exchange_data()` rows are renumbered to `(layer, icell2d)` on each grid. The carrier and the child are
two models of one simulation in ONE solution group (`gwf6 gwf.nam gwf`, `gwf6 gwf-child.nam gwf-child`, `ims6 gwf.ims gwf
gwf-child`) joined by `GWF6-GWF6 gwf-gwf-child.exg`. An `xt3d` exchange and every exchange of a refinement that has a
transport carry the auxiliary variables `ANGLDEGX` and `CDIST`, which mf6 reads there.

The `transport` of a statement (`{name, slots}`, the GWT model beside the child, default `gwt-child`) is a gwt body keyed by
its name with `of` the child's name: it is placed on the child's cells, joins the run's `Transport` body's solution, and is
exchanged with it (`GWT6-GWT6` with `gwfmodelname1/2`) and with the child's flow (`GWF6-GWT6`); its SSM sources are the
child flow's carrying packages. A refinement's transport in a run with no `Transport` refuses.

WATERMOVER RESOLVES ITS ENDS ACROSS MODELS. One grammar: a statement stated on any model's deck names its two ends by a
name, a point or both among the placed features of EVERY flow model, and each end resolves to exactly one feature (a point is
read on the model whose cells hold it). A mover whose ends sit in one model is that model's MVR package whichever deck
stated it; one whose ends sit in the carrier and a child is written to the exchange's own MVR (`MODELNAMES`, the packages as
`model package`), and each package used sets its `MOVER` option. A mover between two children refuses - an exchange joins a
child to its carrier alone. The MVR keywords beside a statement refuse as they do in one model.

The outputs of a child are read on its own grid: `Solved(model=, grid=)` gives it its cells and layers, the publish's table
walks every model (a child's rows carry its name in the module tag, its group and its caption), the listing and the
observation files are named for the model, and the results the worker must find include the child's. The staged check reads
each model's period lists against that model's own grid.

THE DISU IS THE SHEET'S WHOLE GRID. A sheet that states `DISU_*` keywords (the dictionary's own rows; `nja`-shaped rows
are ARRAY like the other grid data) has no DISV: `authoring/simulation.py::_write_unstructured` writes the model's
discretization as stated, every other keyword as stated, and nothing places on the accepted mesh - a statement that places
by geometry, a `DISV_*` keyword or a refinement beside it refuses. The period rows and observation ids are node numbers
(`[[node], rate]`), the staged check reads them against `NODES`, and no DISV is written or staged. A DISU has no face on the
map unless `DISU_CELL2D` gives one and the publish reads the accepted mesh's, so its fields are not painted: the run's
observations chart and its record is the accepted mesh. The mesh front handing down a DISU (gridgen's) is the follow-on.

Proven through the image: ex-gwf-lgr's parent and child (the example's geometry in round widths and four child cells per
parent cell, with the SFR and MVR set aside) equal flopy's own DIS reference bitwise, as does the same simulation with a
transport in both models and all four exchanges (heads and concentrations); a Streams line in the parent and one in the child
joined by two cross movers moves exactly the water the other side receives; ex-gwf-rad-disu's radial DISU, stated whole,
equals the published script's heads and observation CSV bitwise.

## Particle tracking

A PRT model rides in the flow model's simulation. `ParticleTracking({given, slots})` is a composite of the GWF wrapper
(`modules/gwf.py`), stated like `Transport` and `Energy`: the body is `prt` (`modules/prt.py`, its dictionary the 6.7.0 `prt-*`
definition files plus the clock - no IMS, no OBS, `prt.json`), it takes the carrier's staged DISV as its grid, it is joined to the
flow by a `GWF6-PRT6` exchange and solved by an EMS of its own (`prt.ems`), and its FMI package, which an exchange makes
unneeded, is not written. The tracks of the exchanged form equal the published two-simulation form (the flow saved to files,
then read by a second simulation) in every column but `IMDL`; the second form is not authored, because the worker runs one
`mfsim.nam`. A qualified run keyword (`prt: PRP_EXIT_SOLVE_TOLERANCE`) reaches the body; the manifest's module word stays the flow
or the transport body's (the worker names three simulators). A PRT beside a `LocalGridRefinement` refuses: the particles never see
the child's flow.

The porosity (`MIP_POROSITY`, with `MIP_RETFACTOR` and `MIP_IZONE`) is the dictionary's, a constant, a list per layer or a run
input, and has no default. The release package is `Release(geometry | on, layer, z | depth, spacing | count)`, or a list of them,
a composite of the PRT wrapper: one PRP package whose `PRP_NRELEASEPTS` and `PRP_PACKAGEDATA` the authoring derives
(`authoring/release.py`). `geometry` is a point (one particle, never moved), a line (a particle every `spacing` metres from its
start, or `count` from end to end) or a polygon (`spacing`: a lattice of squares anchored at the polygon's bounding box, a
particle at the centre of each square the polygon holds; `count` n: n by n particles at the sub-cell centres of every
rectangular cell whose centre it holds); `on` names a placed feature of the flow model (a well, a boundary, a stream reach) and
`count` n releases n by n in each of its cells. The height is `layer` (from 1) with `z`, the fraction of the layer's thickness above
its bottom (1 the layer's top), or `depth` below the grid's top at the cell, in the model's length unit, whose layer is the one it
falls in; neither is defaulted. The cell of every particle is computed - the lowest cell covering the position - and a position
off the grid refuses with the nearest cell, as every placed point does. Everything else of the package is the PRP's own keyword,
stated by its name: `PRP_EXIT_SOLVE_TOLERANCE`, `PRP_EXTEND_TRACKING`, `PRP_STOPTRAVELTIME`, `PRP_DRAPE`, and the release times
(`PRP_PERIODDATA`, `PRP_RELEASE_TIME_FREQUENCY`, `PRP_RELEASETIMES`; a run stating none releases once at the start, mf6's own).

What a solved run writes is read off the CSV track file `OC_TRACKCSV_FILERECORD` names (`modules/tracks.py`): `PATHLINES` is a
GeoJSON vector, a LineString per particle through the places the track records, each vertex `[lon, lat, z]`, with the travel
time at every vertex (`travel_times_s`), the release point and package, the release time and how the particle ended in its
properties; `ENDPOINTS` is a Point per particle at its last record with the end in words (mf6's status codes: a boundary face,
a weak sink, a cell with no exit face, a stop zone, an inactive cell, the stop time); `TRAVEL_TIME` is a chart of the particles
that ended against their travel time. The table's rows publish them as the vector format and the chart, and a run saving no CSV
track file journals `OC_TRACKCSV_FILERECORD` and publishes none of the three.

Proven through the image: prt-mp7-p01 on a gridgen DISV (the example's geometry placed in UTM 17N) writes the published
script's particles and equals flopy's own DISV simulation of the same models bitwise in every column; against the published DIS
script's tracks the particle sets of part A agree to 3e-8 (the placement's shift in coordinates), and those of part B end in
the same cells and zones at the same statuses with end times within 1e-4 of the total (mf6's tracking on a DISV grid is not
its tracking on a DIS grid). The `Release` composite seeds the same 21 and 3780 points as the example's MODPATH 7 particle data
(`spacing`, `count`, and a polygon's `count`) to 1e-9. On the triangle mesher's 188-cell mesh a polygon lattice, a line's `count`, a `depth` and a point release 15 particles that equal flopy's own
DISV simulation of the same cells bitwise (the cells found by an independent shapely walk of the DISV).

## The publish

The engine's own reading of a solved run (`modules/outputs.py`) opens the files
the output control saved - for every model: `_SAVED` maps HEAD,
CONCENTRATION and TEMPERATURE to their own OC filerecords and COMPACTION and SUBSIDENCE to the CSUB package's
compaction and z-displacement records (rows a run states no CSUB keyword for are no rows of that run, and ones it
states but did not save are journalled by the record keyword), and a coupled body reads its
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
refinement, a Voronoi - so those meshes cannot yet be authored on; a refinement of a
mesh that is not a lattice; the mesh front handing down a DISU; and FMI (a
model reading a prior flow run's saved budget, a second simulation). Each is a
leg in the scope's plan; the coupling above is built on the exchange for GWT,
GWE and PRT.
