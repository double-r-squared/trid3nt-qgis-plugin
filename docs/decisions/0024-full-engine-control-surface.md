# 0024 - Full engine control: entirety over scenario keyholes

Date: 2026-07-26. Status: accepted (direction); per-engine execution staged.

## Context

NATE's diagnosis of the founding pattern: replicating North-Star demos
produced narrow scenario wrappers (run_swmm_urban_flood,
run_model_*_scenario) that lose the full package's functionality and
overfit to pre-imagined analyses. The goal is the AI having full control
over all engines, usable in their entirety. Delft3D is to be integrated.

## Decision

Per engine, the exposed surface has three layers:
1. LIFECYCLE PRIMITIVES (registered tools): build/stage deck, run (run_solver
   dispatch), read_run_diagnostics, set_<engine>_parameters - the ADR 0021
   V&V-wave pattern, engine-uniform envelopes.
2. FULL NATIVE API via the code-exec playground: the engine's own Python
   surface (pyswmm + swmm-api, flopy, hydromt-sfincs, TELEMAC python
   scripts, Delft3D FM tooling) available to model-written code - entirety
   without registry explosion, per the analysis-is-playground norm.
3. SEARCHABLE ENGINE-API DOCS: the ADR 0019 on-demand capability-search
   pattern indexed over engine APIs, so the model discovers functions
   instead of us enumerating them as tools.

Scenario tools are demoted to convenience wrappers - never the ceiling.
North-Star demos remain validation milestones. New engines (Delft3D FM
queued) integrate full-surface from day one.

## Consequence

Sandbox policy work: playground engine use needs compute limits and a
docker-delegation seam (solver execution stays behind run_solver handles).
Registry stays lean (primitives only); the long tail lives in layers 2-3.
Supersedes the scenario-wrapper growth pattern, not existing wrappers.
