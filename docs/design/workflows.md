# workflows/ -- the engine composers

`trid3nt_server/workflows/` (was `agent/workflows/`, ADR 0277) holds the
per-engine composer templates plus the `run_solver` dispatch surface. This is
the largest folder (104k LOC): every registered engine template is an ordinary
retrieval-pool member (doors dissolved, ADR 0112 lineage).

## What lives here

- One folder per engine family: `sfincs/`, `hecras/`, `modflow/`, `telemac/`,
  `swmm/`, `geoclaw/`, `openquake/`, `pelicun/`, `landlab/`, `elmfire/`,
  `swan/`, `schism/`, plus `mesh/` (mesh-generation composer) and `calibration/`.
- Each family: a `run_<engine>.py` driver, `postprocess_<engine>.py`, a
  `_template_card.py`, and one folder per template (with co-located
  `corpus.yaml`). Templates compose fetchers (`data/`) + mesh (`mesh/`) + the
  solver leg.
- `shared/` -- cross-engine helpers: `cog_io`, `frames`, `manning`,
  `physics_registry`, `publish_quantities`, `register_published_manifest`,
  `soil_hydraulics`, `solve_progress`, `water_table_interp`.
- `run_<engine>.py` files resolve the repo root / workers dir via
  `Path(__file__).resolve().parents[3|4]` (re-anchored in ADR 0277).

## Composition

Composers import primitives from `data/` and geometry from `mesh/` (absolute
paths), then dispatch the solver via `data/simulation/solver`. Outputs publish
through `data/publish_layer` and surface on the map via `emission/`. The
solver-confirm gate (`gates/cards/solver_confirm.py`) reads per-family
pre-solve estimates from these composers.

## Invariants / extension points

- Full engine control is the goal: primitives + playground APIs + searchable
  docs, never North-Star scenario wrappers.
- Fidelity ladder holds (SFINCS = screening; native solver for V&V).
- A new template completes the registry + corpus + retrieval checklist and
  ships QGIS-true proof renders.
