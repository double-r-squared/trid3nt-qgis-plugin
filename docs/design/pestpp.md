# PEST++ - a study engine

PEST++ (5.2.27, github.com/pestpp/pestpp) runs studies over a model: sensitivity (`pestpp-sen`), ensemble
calibration (`pestpp-ies`) and the release's other tools. It is an engine like the others - one image, one
`LocalSolverSpec`, one dictionary, one output table - whose trials run a model the study directory carries.
One template speaks it, `pestpp_calibrate_modflow`.

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

`outputs.OUTPUTS`, read off the study's files (`outputs.fetch_study` downloads them): `phi` per iteration of
an ensemble study (`pestpp.phi.actual.csv`: run count, mean, spread, range, the base realization's),
`parameter_ensemble` at an iteration (`pestpp.<i>.par.csv`, the last one unstated), `residuals` of the base
realization per observation (`pestpp.<i>.base.rei`: group, measured, modelled, measured minus modelled,
weight), and `sensitivity`, the Method of Morris summary per parameter (`pestpp.msn`). A read whose file the
study did not write refuses `PESTPP_OUTPUT_MISSING`.

## The dictionary

`modules/module_input/pestpp.json` holds every option of the release's tools the 5.2.27 binary reads - 170 -
extracted by `scripts/extract_pestpp_dictionary.py` from the users manual at the release tag (its control-variable
tables, run-management paragraphs and the control-data variables whose type it states), read against the
binary's own parse-only records (`tests/fixtures/pestpp/defaults`): a manual name the binary does not read is no
row (eight are not: `da_hostart_cycle`, `glm_norm_form`, `ies_mda_decl_fac`, `mou_crossover_prob`,
`mou_mutation_prob`, `mou_risk_obj`, `opt_external_dec_var_groups`, `opt_iter_toll`), and a row's default is the
value the binary takes unstated, the manual's beside it where the two differ (ten do, `ies_subset_size` -10
against 4 among them). `describe_keywords(module="pestpp")` reads it; a run states any option by name,
`keywords={"ies_num_reals": 20}`, held to its row's type and choices at fill. Five options are LOCKED, each
refusing naming why (`modules/module.py`): `save_binary` and its other name `ies_save_binary`, and
`ies_csv_by_reals` (the outputs are read from the CSVs PEST++ writes under their defaults), and
`sweep_output_csv_file` (everything the manager writes is uploaded by the `pestpp` stem). An unstated `noptmax`
is the binary's 0 - the ensemble evaluated, nothing calibrated - so a calibration refuses one.

`authoring/control.py` writes the control file in PEST++'s keyword format: `* control data keyword` stating only
what the study and the run state (every other variable is PEST++'s own default), and the parameter, observation,
template and instruction tables as CSVs beside it.

## `pestpp_calibrate_modflow`

A finished MODFLOW run (`run`), the arrays to tune (`parameters`, each a MODFLOW identifier with its lower and
upper factor and the source they are read from; unstated bounds refuse), how many of them to tune (`tune`), the
moment the wells are asked at (`event_time`) and the wells (`observe`, every groundwater level the source reports
over the run's ground). `workflow.py` runs one order:

1. The run's staged deck is fetched from where it was staged (`authoring/modflow.py`). Each parameter is a
   multiplier written into its array's own control record through a PEST template - into the FACTOR of an
   INTERNAL record (PEST's scale the factor the run stated), or the CONSTANT itself (its scale the run's
   constant) - so the array's values are never rewritten and the trial runs `mf6` alone. The multiplier starts
   at 1 and is log-transformed. An array read from its own file, or stated per layer with different factors,
   refuses.
2. Each well's reading is a head target at its cell (the point contract: a site no cell holds is left out and
   said): a depth below the land surface is the cell's top less the depth, an elevation counts only on the run's
   own frame, and a site is held once, at the first of its readings that is a head. Every well weighs 1. An OBS
   file the study adds reads the heads; a run of more than one layer or one time step refuses.
3. Where `tune` is below the number of parameters, a pestpp-sen pass ranks them by the mean absolute elementary
   effect and the top ones are confirmed on the review card (AUTO takes them as ranked, said).
4. The study is reviewed on the one review card, then pestpp-ies runs.
5. The deck is solved again with the base realization's last parameters, staged and solved as an ordinary MODFLOW
   run (`stage_run` with MODFLOW's staged check, the MODFLOW solver) and published as MODFLOW publishes a run,
   over a sheet read off the calibrated deck itself; phi per iteration, the residual at each well (simulated
   minus observed) and each multiplier's ensemble range are charted beside it.

The run's record is that MODFLOW run's, its `study` entry naming the study run, the finished run, each
parameter's bounds, source and calibrated value, phi and the last ensemble.

## What is not built yet

Multipliers on list packages (recharge or drain rows); a run of several layers or time steps; progress on the
Sim card (PEST++'s run-manager lines are not parsed); a diagnostics parser.
