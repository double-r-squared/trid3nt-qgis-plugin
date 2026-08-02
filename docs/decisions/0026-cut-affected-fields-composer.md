# ADR 0026: Cut the contamination-affected-fields composer (playground recipe)

Status: ACCEPTED (2026-07-26, engine-door refactor MODFLOW pilot, TEMPLATE lane).
Supersedes nothing; see ADR 0024/0025 + docs/specs/engine-door-refactor.md.

## Context

`run_model_contamination_affected_fields` was a registered composer that ran a
MODFLOW contaminant plume and then scored which farm fields the plume reached.
It bundled a simulation (the plume) with a composed analysis (zonal field
scoring) in one hard-wired tool. The analysis-is-playground norm holds that
atomic tools are DATA fetchers + irreducible primitives only; composed analyses
belong in the python playground (code_exec), where they are flexible + auditable.

## Decision

CUT `run_model_contamination_affected_fields` as a registered tool:

- its plume half IS the `modflow_contaminant_plume` template (single OR multi
  species) landed in this refactor;
- its zonal field-scoring half re-homes to a PLAYGROUND RECIPE: the model
  composes `modflow_contaminant_plume` -> `fetch_field_boundaries` ->
  `analyze_affected_fields` (or `compute_zonal_statistics`) in `code_exec_request`.

The registered `analyze_affected_fields` primitive STAYS (whether it too becomes
a playground recipe is a separate, deferred decision - RISK-6). Only the COMPOSER
is cut. The composer folder, its `@register_tool`, its category memberships, its
SOLVER_CONFIRM_TOOLS entry, its server confirm-envelope branch, its corpus
entries, and its imports are removed; renames replace old names (no aliases).

## Consequence

- One fewer registered tool; the retrieval pool is not diluted by a bundled
  composed analysis.
- The affected-fields workflow is now the documented recipe in
  docs/playbooks/modflow-affected-fields-recipe.md.
- Its test (`test_model_contamination_affected_fields.py`) is deleted; still-valid
  zonal assertions may move into a playground-recipe test later (out of scope).
