# PEST++ - a study engine

PEST++ (5.2.27, github.com/pestpp/pestpp) runs studies over a model: sensitivity (`pestpp-sen`), ensemble
calibration (`pestpp-ies`) and the release's other tools. It is an engine like the others - one image, one
`LocalSolverSpec`, one output table - whose trials run a model the study directory carries. No template speaks
it yet; the engine is driven by its canary.

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

## What is not built yet

The finished-run input, the calibration template and the best parameter set returned as an ordinary run;
progress on the Sim card (PEST++'s run-manager lines are not parsed); a diagnostics parser.
