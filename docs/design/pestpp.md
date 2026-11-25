# PEST++ - a study engine

PEST++ (5.2.27, github.com/pestpp/pestpp) runs studies over a model: sensitivity (`pestpp-sen`), ensemble
calibration (`pestpp-ies`), Gauss-Levenberg-Marquardt calibration (`pestpp-glm`) and the release's other tools. It is an engine like the others - one image, one
`LocalSolverSpec`, one dictionary, one output table - whose trials run a model the study directory carries.
`calibrate` studies a finished run. `optimize` studies a pinned native MODFLOW 6 deck for one objective.
`tradeoff` studies the same kind of deck for several objectives at once.

## What it consumes

A study directory: the control file, the template files PEST++ writes parameters through, the instruction
files it reads outputs through, and every model file a trial needs. `staging.stage_study` copies the
directory, renames its control file to `pestpp.pst` (PEST++ names everything it writes after the control
file, and the engine reads its record as `pestpp.rec`), checks that every template and instruction file the
control file names is staged and every folder a template writes into exists (version 1's model input/output
section, or version 2's two external tables), and stages it through the one `stage_run`. The case's `module`
is the tool; its `cores` are the agents, unstated the `CONTAINER_CORES` setting.

## The worker

`workers/pestpp/` holds the PEST++ linux release, the same mf6 6.7.0 release the MODFLOW worker holds, and
pyEMU 1.7.0 with numpy and pandas, every one pinned. The entrypoint copies the staged directory once per agent
into the container's own `/work`, starts one manager in `/data` (`pestpp-<tool> pestpp.pst /h :4004`) and
the agents against it on the loopback (`/h localhost:4004`), all under `--network none`. Status is the
manager's exit code, the tool's own completion marker and the declared artifacts on disk. OPT writes its
marker to the manager's stdout. The other tools write their marker to the record. A failure carries
the record's tail through the solver's listing-tail path. Everything the manager writes, `pestpp.*`, is uploaded under the run prefix.

## What it produces

`outputs.OUTPUTS`, read off the study's files (`outputs.fetch_study` downloads them): `phi` per iteration
(an ensemble study's `pestpp.phi.actual.csv`: run count, mean, spread, range, the base realization's; a glm
study's `pestpp.iobj`: run count and the phi of its one parameter set, measurement and regularization parts),
`parameter_ensemble` at an iteration (`pestpp.<i>.par.csv`, the last one unstated), glm's `parameters` (the
best set, `pestpp.par`, or the set at an iteration, `pestpp.<i>.par`) and `parameter_iterations` (`pestpp.ipar`,
from the start), `jacobian` (glm's last, `pestpp.jcb`: each observation's derivative by each parameter, in PEST's
transformed space), `residuals` per observation (the base realization's, `pestpp.<i>.base.rei`, or glm's best set's,
`pestpp.rei`: group, measured, modelled, measured minus modelled, weight), and `sensitivity`, the Method of Morris
summary per parameter (`pestpp.msn`). A read both ies and glm answer takes the tool, ies unstated. A read whose file
the study did not write refuses `PESTPP_OUTPUT_MISSING`. A glm study also writes, by PEST++'s default, a FOSM
posterior from the observations' weights (`pestpp.post.cov`, `pestpp.par.usum.csv`); nothing reads it.

## The dictionary

`modules/module_input/pestpp.json` holds every option of the release's tools the 5.2.27 binary reads - 187 -
extracted by `scripts/extract_pestpp_dictionary.py` from the users manual at the release tag (its control-variable
tables, run-management paragraphs, the appendix's table of control-data variables, and the parameter-group
variables and parameter columns of sections 4.8.2 and 4.9.2), read against the
binary's own parse-only records (`tests/fixtures/pestpp/defaults`): a manual name the binary does not read, or
reads and ignores (its forgiving parse warns `unused`), is no row (eight are not read at all: `da_hostart_cycle`,
`glm_norm_form`, `ies_mda_decl_fac`, `mou_crossover_prob`, `mou_mutation_prob`, `mou_risk_obj`,
`opt_external_dec_var_groups`, `opt_iter_toll`), and a row's default is the
value the binary takes unstated, the manual's beside it where the two differ (ten do, `ies_subset_size` -10
against 4 among them). `describe_keywords(module="pestpp")` reads it; a run states any option by name,
`keywords={"ies_num_reals": 20}`, held to its row's type and choices at fill. Five options are LOCKED, each
refusing naming why (`modules/module.py`): `save_binary` and its other name `ies_save_binary`, and
`ies_csv_by_reals` (the outputs are read from the CSVs PEST++ writes under their defaults), and
`sweep_output_csv_file` (everything the manager writes is uploaded by the `pestpp` stem). An unstated `noptmax`
is the binary's 0 - the ensemble evaluated, nothing calibrated - so a calibration refuses one.

The control-data variables the binary reads are rows (`relparmax`, `facparmax`, `facorig`, `phiredswh`,
`noptswitch`, `splitswh`, `noptmax`, `phiredstp`, `nphistp`, `nphinored`, `relparstp`, `nrelpar`), rubrique `control
data`, each default the value the binary's record prints (`facparmax` 1.1, `relparmax` 1.0, so a factor-limited
parameter moves about that factor an iteration, x1.11 in the recorded glm study, unless the run states more); the other control-data variables
of the manual's table (the lambda, Jacobian and save variables) it reads and ignores. `noptswitch` and `splitswh`
are read and echoed nowhere, so they carry no default. Beside them, the parameter-group variables (`inctyp`,
`derinc`, `derinclb`, `forcen`, `derincmul`, `dermthd`; rubrique `parameter groups`, defaults off the record's group
table, `dermthd` none) and the per-parameter columns `partrans` (`none` or `log`) and `parchglim` (`relative` or
`factor`; rubrique `parameter data`), which the binary requires and so has no default for.

`authoring/control.py` writes the control file in PEST++'s keyword format: `* control data keyword` stating only
what the study and the run state (every other variable is PEST++'s own default), and the parameter, observation,
template and instruction tables as CSVs beside it. A group variable stated by name writes the group table for
the study's groups, its other columns the dictionary's defaults (the table requires every column; `dermthd`, which
the binary prints no default for, is written `parabolic`, the manual's usual value); unstated, no table is
written and PEST++ makes its own groups. A stated `partrans` or `parchglim` replaces the pieces' own (log, factor)
for every parameter; a log-transformed parameter stated `relative` refuses (`PESTPP_OPTION_CONFLICT`), the manual's
rule. A stated value is one for every group or parameter of the study.

## `calibrate`

One study over a finished run (`run`), whatever engine solved it: the run's record names its engine and the study
takes that engine's PIECE, `authoring/<engine>.py` - the parameter writer, the observation reader, the command a
trial runs, and the calibrated run's solve and publish. A run of an engine with no piece refuses
(`PESTPP_ENGINE_UNCALIBRATED`) naming the engines a study calibrates (`workflow.ENGINES`: MODFLOW and TELEMAC), and
what is tunable is the piece's own. Each piece reads its own observation row - the wells (`observe`) for MODFLOW, the
gauges (`gauges`) for TELEMAC - and a stated row the run's piece does not read refuses (`PESTPP_OBSERVATIONS_UNREAD`). Unstated, `run` is this case's finished run of one of those engines: the one there is,
taken; of several, the one confirmed on the review card, which lists them newest first and offers the newest (AUTO
takes it, said on the run); none refuses `RUN_NONE_IN_CASE` naming the engines. A stated run wins.

Beside the run: the method (`method`, `ies` unstated: an ensemble smoother whose realizations are drawn inside the
bounds; or `glm`, regression of one parameter set from the run's own values over a finite-difference Jacobian; any
other refuses `PESTPP_METHOD_UNKNOWN` at fill), the parameters to tune (`parameters`, each an identifier of the run's engine with its lower and
upper factor - or, as `lower_value` and `upper_value`, its bounds in the run's own unit - and the source they are
read from; unstated bounds refuse), how many of them to tune (`tune`), the moment the observations are asked at
(`event_time`) and the observations (`observe`).

`stages` accepts an ordered list of distinct names. `screen` runs Morris sensitivity, `calibrate` fits with IES or
GLM, and `resolve` solves the original model with the calibrated parameters. Omit a stage to skip it. `resolve`
requires an earlier `calibrate` (`PESTPP_STAGES_ORDER`). With `stages` unstated, the template screens if `tune` is
smaller than the declared parameter count, then calibrates and resolves. A prior screen can limit the selection via
`tune`. A screen after calibration reports the ranking without changing that fit. A study ending without `resolve`
publishes a `StudyResult` with its study ID, stage run IDs and any ranking. Calibration charts use the normal
chart publication channel. The SDK's `open_study`, `screen`, `calibrate` and `resolve` expose the same stages.

The template prepares the first selected tool's checked deck before execution.
It reviews inputs available at that point. A ranking card and any fit review
that needs its selection still follow screening. The reviewed `calibrate.py`
child requests those cards from the daemon and uses its decisions to stage and
run the next tool. Standalone runs use the unattended inputs and skip the wait.
The generated script pins the finished case model. A named study records its
choices and outputs under `studies/<name>/`, including the native resolve deck
when requested. See [Calibration stages](sdk.md#calibration-stages).

`parameter_data` sets the native PEST++ table columns for individual parameters, indexed by authored `PARNME`.
For example, `{"npf_k": {"PARTRANS": "none", "PARVAL1": 0.8}}` keeps the declared MODFLOW array and uses 0.8
as its initial PEST-coordinate multiplier. Rows take `PARNME`, `PARTRANS`, `PARCHGLIM`, `PARVAL1`,
`PARLBND`, `PARUBND`, `PARGP`, `SCALE`, `OFFSET`, `DERCOM`, and `PARTIED`. Use the final name of an
adjustable parameter for `PARTIED` on a tied row. The study declares one model command, so `DERCOM` takes 1.
Pilot points can be addressed by their authored names, such as `npf_k_pp0000`. Existing dictionary keywords
remain the setting path for controls and group variables, including `dermthd` and `gsa_morris_r`. Stated
per-parameter columns take precedence over their corresponding global columns.

`authoring/modflow.py` implements the default MODFLOW sequence.

1. The run's staged deck is fetched from where it was staged (`authoring/modflow.py`). Eligibility is the MODFLOW 6
   dictionary (`modules/module_input`): an ARRAY whose `dtype` is REAL, or a list column its `columns` table
   declares REAL, is tunable by `<PACKAGE>_<NAME>`; an INTEGER flag, a string or a keyword refuses
   `PESTPP_PARAMETER_NOT_REAL` naming it. No package or variable is listed by hand. Each parameter is a multiplier
   written into its array's own control record through a PEST template - into the FACTOR of an INTERNAL or
   OPEN/CLOSE record (PEST's scale the factor the run stated), or the CONSTANT itself (its scale the run's
   constant) - so the array's values are never rewritten and the trial writes nothing else before `mf6`. The
   multiplier starts at 1 and is log-transformed. A layered array is one control record per layer, so `NAME@N`
   tunes layer N alone; a homogeneous constant array expands into that form. A second package of one kind is named
   by the name its name file gives it (`RIV_1_COND`), and a kind with two instances refuses the bare name. A list
   column (RIV COND/STAGE/RBOT, DRN COND, GHB COND/BHEAD, WEL Q, LAK, SFR, ...) is tuned the same way, the template
   writing the multiplier into that column of every row in its own block and PEST's scale the one value the rows
   state (rows stating different values refuse). `zones` divides a command into one parameter per stated zone -
   cells, reaches, wells, layers or a polygon intersected with the grid - writing the field only into the rows a
   zone holds and leaving the rest as written; overlapping zones refuse. A zone polygon is longitude/latitude
   (EPSG:4326) unless it names its CRS, and is reprojected into the run's grid frame, never handed to the caller
   that way. An array's zones are its layers. Bounds in
   the run's own unit are divided by that scale, and refuse over an array whose value is not a constant (whose
   FACTOR is not its values) and where the run's own value lies outside them. An array stated with `pilot_points`
   is laid by pilot points instead (Pilot points, below), and a trial runs `workers/pestpp/modflow_trial.py`, which
   lays each such array and then execs `mf6`.
2. Each well's reading is a head target at its cell (the point contract: a site no cell holds is left out and
   said): a depth below the land surface is the cell's top less the depth, an elevation counts only on the run's
   own frame, and a site is held once, at the first of its readings that is a head. Each well weighs one over its
   sigma (Weights, below). An OBS file the study adds reads the heads; a run of more than one layer or one time step
   refuses.
3. Where `tune` is below the number of parameters, a pestpp-sen pass ranks them by the mean absolute elementary
   effect and the top ones are confirmed on the review card (AUTO takes them as ranked, said).
4. The study is reviewed on the one review card (the method on it), then the method's tool runs: pestpp-ies, or
   pestpp-glm. noptmax unstated refuses for either.
5. The deck is solved again with the calibrated parameters - the base realization's last (ies), the best set
   (glm) - staged and solved as an ordinary MODFLOW run (`stage_run` with MODFLOW's staged check, the MODFLOW
   solver) and published as MODFLOW publishes a run, over a sheet read off the calibrated deck itself; phi per
   iteration, simulated against observed, the residual at each well (simulated minus observed) and each
   multiplier's range (across the last ensemble, or across glm's iterations from the start) are charted beside it.

Over a TELEMAC run (`authoring/telemac.py`), a TELEMAC-2D deck (another module refuses):

1. The parameter is `FRICTION_COEFFICIENT`, a multiplier written into the steering file's own `FRICTION COEFFICIENT`
   through a PEST template (PEST's scale the coefficient the run stated); bounds in the run's own unit are divided by
   it. Zoned friction (`FRICTION DATA`) is not written and refuses; so does any other identifier.
2. Each gauge's water level, lifted onto the run's frame by the level slot's own ingestion (a gauge on a frame the
   offset row cannot bridge is left out), is a target at the node the run's own point read (`read_series` over the
   finished run's result, the point contract: a gauge the mesh does not hold is left out and said) lands it on.
   TELEMAC-2D prints the free surface at its remarkable points (`LIST OF POINTS`, `NAMES OF POINTS`, `VARIABLES TO
   BE PRINTED = 'S'`) into its `FORMATTED RESULTS FILE` once, at the last time step, so a target is held against the
   run's END; a deck already stating one of those keywords refuses.
3. A trial runs `workers/pestpp/telemac_trial.py`, which ends in TELEMAC's own launcher (`telemac2d.py <steering>`),
   in the image `trid3nt-local/pestpp-telemac` (workers/pestpp/Dockerfile.telemac: the PEST++ release and the steering
   driver on the TELEMAC worker image), which the engine picks by the case's `server_facts.model`. Where the finished
   run's outflow level is the normal depth it derived (the stated level equals the derivation at its own coefficient),
   the study writes `outflow_levels.json`: that level derived at 81 coefficients across the tuned range, over the
   length the run measured its friction slope along its walked line (`server_facts.reach_length_m`; the chord between
   the faces where the domain holds no line). The trial reads
   its own coefficient, takes its level off the table and restates `PRESCRIBED ELEVATIONS` and `INITIAL ELEVATION`
   through TelemacCas before solving, so no trial is bounded by a level another friction set. A level a gauge held
   stays as stated - moved only by its own offset parameter where its accuracy is cited (Datums and supplied inputs,
   below) - and so does the sloped opening a measured outflow level opens on (`INITIAL CONDITIONS 'SPECIAL'` and its
   user Fortran, copied with the deck).
4. The calibrated run is the finished deck with the best coefficient - and, where derived, the outflow level and flat
   opening re-derived at it - written into its steering file, staged with TELEMAC's staged check, solved by the
   TELEMAC solver and published as TELEMAC publishes a run. A finished run whose record says it ended UNSTEADY is not
   calibrated (`PESTPP_RUN_UNSTEADY`).

## Pilot points

A MODFLOW array may be laid by pilot points rather than one multiplier: `{"NPF_K": {"lower": 0.1, "upper": 10,
"source": "...", "pilot_points": {}}}`. Each point is a log-transformed multiplier starting at 1 inside the
parameter's factor bounds (bounds in the run's own unit refuse: the run's values vary by cell); a cell's value is the
run's own times 10 ** (its ordinary kriging factors over the log10 of the points), as pyEMU's fac2real lays a
log-transformed field. Each layer is laid (the whole array, or the one `@N` names) into a file of its own
(`<package file>.<array>.<layer>.dat`, OPEN/CLOSE, FACTOR 1), which the trial writes before mf6 runs and the calibrated
deck carries; a multiplier over another array of the same file is templated over the file the points leave. A list
column refuses; so does a TELEMAC parameter (its friction is one coefficient of the steering file; a field of it would
be a per-node friction in the geometry file, which no trial writes).

The numerics are pyEMU 1.7.0's (`helpers/geostat.py`), matched to what pyEMU itself computes over the Des Moines grid
(a recorded fixture): the points placed as `get_zoned_ppoints_for_vertexgrid` places them (an even grid over the cell
centres' extent, ceil(extent / spacing) points a side, kept where the cells buffered by one spacing cover them), the
factors as `OrdinaryKrige.calc_factors` (the 20 nearest points, one within 1e-7 taken alone, negative factors dropped
and the rest renormalised), the variogram exponential (pyEMU's `ExpVario`, contribution 1, nugget 0: exp(-h / a)).
Unstated, the spacing is pyEMU PstFrom's default of every 10th cell, a cell counted as the median edge of the deck's own
DISV grid (each edge once: pyEMU counts its cells in a structured cell's side; 122.9 m on the Des Moines deck, so 1229 m
and 63 points), and the range `a` is that spacing. Set `spacing` and `variogram.a` in metres of the run's grid.
Set `maxpts_interp` and `remove_negative_factors` under `pilot_points`. The `variogram` mapping accepts
`type` (`exponential`, `spherical`, `gaussian`), `contribution`, `nugget`, `anisotropy`, and `bearing`
(degrees clockwise from north). pyEMU's `GeoStruct` and `Vario2d` supply the default nugget of 0, anisotropy of 1
and bearing of 0. The selected structure applies to the kriging factors and the prior covariance.
Unsupported types refuse with `PESTPP_VARIOGRAM_UNKNOWN` and unrecognized members with `PESTPP_PILOT_POINTS_UNREAD`.

The prior: every parameter's mean is its starting value and its standard deviation the one its bounds imply, as PEST++
reads them (par_sigma_range standard deviations between the bounds, 4 unstated; of log10 where log-transformed). Where a
study holds pilot points it states that prior to PEST++ as `parcov` (`study_prior.unc`: a STANDARD_DEVIATION block for
every other parameter and one COVARIANCE_MATRIX, `study_prior.cov`, for the points), each group's points correlated by
their variogram and scaled to that variance as pyEMU's `geostatistical_draws` scales it (sqrt(v_i v_j) off the
diagonal); pestpp-ies draws its initial ensemble from it, and pestpp-glm reads it for its FOSM. A run stating its own
parcov beside pilot points refuses `PESTPP_OPTION_CONFLICT`. The review card states each parameter's prior (mean, sd,
its source) and each field (spacing, range and their sources); the study record states, per parameter group, the prior
(mean, bounds, transform, sd and its source, the variogram, the points' count and spacing with their sources) and, for
an ensemble study, the mean and spread across the prior ensemble PEST++ drew and the last one with how many of their
values stand at a bound, and each point's site.

An ensemble study over pilot points localizes: unstated, it states `ies_autoadaloc` true, PEST++'s own automatic adaptive
localization (users manual 9.1.11 Localization; PEST++'s default is false), which keeps only the parameter-observation
correlations an ensemble of this size can tell from noise. The run states `ies_autoadaloc` by name to choose otherwise.
The card shows it, the record states it (`localization`) with its source.

## Bounds after the fit

After the fit, the calibrated parameters (the base realization's last, glm's best set) at a bound are counted per
parameter group, by PEST++'s own rule (`Pest::get_pars_at_near_bounds` at its tolerance 0: at or past the bound, a
log-transformed value also at 10 ** the log10 of the bound pestpp-ies clips it to), and stated as the warning
`PARAMETERS_AT_BOUNDS` (each group's identifier, how many at the lower and at the upper bound, of how many); the record
keeps them as `at_bounds`. The study does not stop on it.

## Weights

`weights.py`. Each target's sigma is the accuracy its record cites (the observation ingestion carries it as
`Observation.accuracy`, read off the source's `accuracy` member with its cite, or the refusal code saying why none
is), moved onto the run's unit; where the value is lifted onto a gauge zero (the record maps `above`), the zero's
accuracy (`above_accuracy`, NWIS `alt_acy_va`) joins it in quadrature - except on a TELEMAC study, where a zero that
cites its accuracy is a parameter of its own (Datums, below) and leaves sigma, so it is counted once. The weight written is one over sigma; no
`standard_deviation` column is written, so pestpp-ies keeps its own no-noise default unless the run states
`ies_no_noise`. Cited targets are in the piece's group (`heads`, `levels`). A target whose reading or zero cites
none still runs, in group `not_cited`, with the warning `ACCURACY_NOT_CITED`: its sigma starts at the widest its
source cites elsewhere (the member's table, or for a per-record field the widest any other site of the set states),
in quadrature with what it does cite, marked `floor`; where its source cites no accuracy at all it starts at 1 in
the run's unit (`unit start`). A sigma the user states per group (`sigma`, `{"not_cited": 0.3}`, in the run's unit)
wins (`stated by the user`); a group the study does not hold refuses `PESTPP_SIGMA_GROUP_UNKNOWN`. Each group's
sigma range is on the review card.

Group balancing runs only when asked (`balance`): PEST++'s own `ies_phi_factor_file`, each group an equal share of
the initial ensemble's phi; pestpp-glm has no such mechanism, so a balanced glm study refuses
`PESTPP_BALANCE_UNSUPPORTED`.

After the fit, the widening is a stated step (the discrepancy rule, PWTADJ2 / Morozov): off PEST++'s residual file at
the best set, each group's sigma is scaled by one factor, sqrt(phi_g / n_g), so its phi is its count; a cited, floor
or stated group never narrows (factor at least 1), a `unit start` group takes its factor either way. The calibrated
parameters are the fit's. pestpp-glm's own FOSM reweight (`pestpp.fosm_reweight.rei`) is a different, per-observation
rule PEST++ applies for its posterior; nothing reads it.

The run's record is that engine's run's, its `study` entry naming the study run, the finished run, each
parameter's bounds, source and calibrated value, phi, the method (`tool`) and the last ensemble (ies) or the set at
each iteration (glm), whether it was balanced, the widening (phi before and after, each group's count, phi and
factor), and each observation's target, group, sigma, weight, basis, parts (member, sigma, cite) and widened sigma.

## Datums and supplied inputs

An uncertain input is a parameter whose prior sd is its stated accuracy, never a widened sigma. On a TELEMAC study each
gauge target whose zero cites its accuracy holds a datum parameter (`datum_<k>`, its own group, transform none): the
trial (`telemac_trial.py`) solves through the launcher, then writes each point's free surface less its datum
(`study_levels.txt`, off `study_datums.dat`, which PEST writes), and the instruction reads that. The datum's prior is
mean 0, sd the zero's cited accuracy, stated to PEST++ in parcov's STANDARD_DEVIATION block; its bounds are 2 sd either
side (PEST++'s own par_sigma_range of 4, so the bounds imply the same sd), held by PEST 10 bound-widths above zero with
the offset taking that back (a zero-valued parameter cannot be factor-limited). A zero citing no accuracy stays in
sigma (its floor or unit start) and is no parameter. The card states each datum's sd and cite; the record each
observation's datum (sd, cite) and each datum's calibrated `written` value. The calibrated run is solved with the tuned
friction only: a datum moves where the gauge stands, not the river.

A finished run's record keeps, beside each input's value, origin and source, the accuracy an ingested reading carries
(`accuracy`, and `zero_accuracy` where a gauge zero lifts it: value and cite, or the code saying why none). On a TELEMAC
study, a measured outflow level (the record's `level` input) whose reading and gauge zero both cite a value is an offset
parameter, `outflow_level`: mean 0, sd the two in quadrature, bounds 2 sd either side as a datum's, held the same way
above PEST's zero. It is placed at the deck's own boundary - the liquid boundaries file's one `SL(n)` column (a fetched
window), else the one `PRESCRIBED ELEVATIONS` entry the deck writes at an elevation boundary - and each trial moves that
entry, a flat opening's `INITIAL ELEVATION` stated at the level, and every instant of the column (off its `.unmoved` copy)
by the offset PEST writes (`study_outflow.dat`); a sloped opening's routine is an initial state and stays. The calibrated
run is solved at the calibrated offset beside the friction. A level whose record cites no accuracy, or that the deck
holds at no one boundary, is no parameter, and the study says which.

The rest - the bed, a derived outflow level (re-derived per trial from the friction instead), stated values - carry no
accuracy on the record, so none is a parameter; the study says so on every run. The eHydro packages publish their
vertical accuracy as free text only (FGDC `vertaccr`: "15 cm", "Bar Test, 0.5 Foot, Bar Check"), no value with a unit a
member reads, so a bed offset waits on a source that states one.

## What is not built yet

Array cell or polygon zones (a scalar per zone over an array's cells needs the trial to lay it; an array's zones are
its layers); pilot points on a TELEMAC friction field; a bed survey offset as a parameter (no bed source states its
accuracy as a value); a run of several layers or time steps read for heads; progress on the
Sim card (PEST++'s run-manager lines are not parsed); a diagnostics parser.


## `optimize`

Provide a pinned MODFLOW 6 simulation, its GWF model name, bounded WEL rate decisions, one objective,
an explicit list of constraints, and the iteration limit. Each decision states the WEL package, the
one-based stress period and cell ID, bounds in native signed rate units, and their source. The initial
value comes from the named inline WEL row. Extraction has a negative rate. The template keeps native
rates in periods outside the stated decisions.

Each forecast states the native OBS type and IDs, units, the source, and a one-based stress period and
time step. Use the GWF model as the observation owner by leaving `package` out, or name a package or
exchange with its filename, package name, or unambiguous type. A forecast can state `model` for another model in the same simulation. `id` and optional `id2` take native tokens,
including cell IDs and boundnames. MODFLOW validates the types and selectors supported by each owner.
The author adds a continuous CSV block to the native OBS package and writes PEST++ instructions for the
stated rows. A fixed TDIS clock supplies those row positions. The objective states `min` or `max`.
Each constraint states `<=` or `>=` and a signed limit. Risk is 0.5.

PESTPP-OPT uses the installed MODFLOW trial command. The result records feasibility, the selected rates,
the objective history, and each constraint's simulated value and signed slack. Nonnegative slack
satisfies either constraint sense. An infeasible SLP result has its own outcome. Call
`resolve_optimum(case, result)` for a separate MODFLOW solve that applies the selected rates and measures
the constraints again on the pinned native deck.


## `tradeoff`

Provide the same pinned MODFLOW 6 simulation and bounded WEL rate decisions `optimize` takes, two or more
native OBS forecasts as objectives, optional signed constraints, the population, the generator, the seed,
and the generation limit. Each objective states its native selectors, units, source and `min` or `max`;
each constraint states `<=` or `>=` and a signed limit. The author writes the objectives' observation rows
in `less_than` or `greater_than` groups by their sense, names them in `mou_objectives`, and writes the
decisions as `opt_dec_var_groups`. `mou_population_size`, `mou_generator`, `rand_seed` and `noptmax` carry
the search.

The result is read from the pinned archive files: `pestpp.pareto.archive.summary.csv`,
`pestpp.archive.dv_pop.csv` and `pestpp.archive.obs_pop.csv`. The final archive's population are the
candidates; each member's latest archive summary row states its feasibility and Pareto front, and its
observation row supplies the raw objective values. Each candidate carries its decisions, its objective
values, its feasibility, the file's Pareto front and a rank, and every constraint's simulated value and
signed slack. Nondominance is checked again in the stated directions: a feasible member another dominates
refuses `PESTPP_MOU_DOMINATED`. A duplicate or missing member refuses `PESTPP_MOU_ARCHIVE`, and no
feasible member is the `empty` outcome. The run publishes a Pareto scatter of the objectives and a
constraint-slack chart; it publishes no raster or mesh, so no candidate becomes the answer unless the
caller names one.

`resolve_candidate(case, result, candidate)` solves the pinned native deck with exactly that candidate's
recorded decision vector and reads its native OBS outputs again. The result names the candidate and the
trade-off run it came from. No candidate is selected by default.

The search is deterministic. Chance mode (`opt_chance_points`, `mou_risk_objective`) is not built: the
pinned source reads `mou_risk_objective` and the dictionary does not carry it, and reported chance-shifted
values would need their own raw/adjusted columns. The dictionary's `mou_generator` default is PSO where the
manual states DE, so the template requires the caller to state a generator and never falls back to either.
