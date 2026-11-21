# PEST++ - a study engine

PEST++ (5.2.27, github.com/pestpp/pestpp) runs studies over a model: sensitivity (`pestpp-sen`), ensemble
calibration (`pestpp-ies`), Gauss-Levenberg-Marquardt calibration (`pestpp-glm`) and the release's other tools. It is an engine like the others - one image, one
`LocalSolverSpec`, one dictionary, one output table - whose trials run a model the study directory carries.
One template speaks it, `calibrate`, over a finished run of any engine with a piece.

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
manager's exit code AND PEST++'s own closing sentence (`analysis complete`) in its record AND the tool's
declared results on disk; a failure's refusal carries the record's tail through the solver's listing-tail
path. Everything the manager writes, `pestpp.*`, is uploaded under the run prefix.

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
(`event_time`) and the observations (`observe`). `workflow.py` runs one order; over a MODFLOW run
(`authoring/modflow.py`):

1. The run's staged deck is fetched from where it was staged (`authoring/modflow.py`). Each parameter is a
   multiplier written into its array's own control record through a PEST template - into the FACTOR of an
   INTERNAL record (PEST's scale the factor the run stated), or the CONSTANT itself (its scale the run's
   constant) - so the array's values are never rewritten and the trial runs `mf6` alone. The multiplier starts
   at 1 and is log-transformed. An array read from its own file, or stated per layer with different factors,
   refuses. A column of a list package's PACKAGEDATA (SFR's rlen, rwid, rgrd, rtp, rbth, rhk, man) is tuned the
   same way, the template writing the multiplier into that column of every row and PEST's scale the one value the
   rows state (rows stating different values refuse). Bounds in the run's own unit are divided by that scale, and
   refuse over an INTERNAL array (whose FACTOR is not its values) and where the run's own value lies outside them.
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
   stays as stated, and so does the sloped opening a measured outflow level opens on (`INITIAL CONDITIONS 'SPECIAL'`
   and its user Fortran, copied with the deck).
4. The calibrated run is the finished deck with the best coefficient - and, where derived, the outflow level and flat
   opening re-derived at it - written into its steering file, staged with TELEMAC's staged check, solved by the
   TELEMAC solver and published as TELEMAC publishes a run. A finished run whose record says it ended UNSTEADY is not
   calibrated (`PESTPP_RUN_UNSTEADY`).

## Weights

`weights.py`. Each target's sigma is the accuracy its record cites (the observation ingestion carries it as
`Observation.accuracy`, read off the source's `accuracy` member with its cite, or the refusal code saying why none
is), moved onto the run's unit; where the value is lifted onto a gauge zero (the record maps `above`), the zero's
accuracy (`above_accuracy`, NWIS `alt_acy_va`) joins it in quadrature. The weight written is one over sigma; no
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

## What is not built yet

Multipliers on stress-period lists (recharge or drain rows) and on per-row values of a list column; a run of several layers or time steps; progress on the
Sim card (PEST++'s run-manager lines are not parsed); a diagnostics parser.
