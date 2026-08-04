# ADR 0120 -- S-tier template wave 1 (flood/hydraulics cluster) + template hygiene gate

Status: accepted (2026-08-04, NATE S-tier wave 1 kickoff + the mid-wave hygiene directive)
Follows: 0107 (two-mode input gate), 0109 (HEC-RAS landing), 0113 (M4 quadtree),
the physics_registry (shared advanced-physics resolve/validate seam).

## Context

The S-tier candidate table (reports/design/template-candidates-2026-08-03.md)
proposed seven new flood/hydraulics templates plus one capability rename:

1. sfincs_structures_weirs_thindams
2. sfincs_observation_points_lines
3. sfincs_advanced_infiltration_methods
4. sfincs_advanced_numerical_physics_knobs
5. hecras_1d_steady_bridge
6. telemac2d_weir_hydraulics
7. telemac2d_bridge_hydraulics
8. RENAME hecras_muncie_flood -> hecras_riverine_flood (capability naming)

The candidate table labelled 1-7 as [S] on the reading that each was a knob swap
on an already-plumbed deck surface. Ground-truth inspection of the worker/deck
wiring showed that reading held for exactly ONE of the seven.

## Decision

### Landed

- **RENAME hecras_muncie_flood -> hecras_riverine_flood.** The tool is named for
  the capability (1D/2D riverine flood); its v1 shipped geometry (HEC's Muncie
  White River project) becomes a labelled fact in the docstring, not the tool's
  identity. Renamed: the registry tool name, the `HECRAS_SOLVER_NAME` solve-seam
  key (`SOLVER_WORKFLOW_REGISTRY` + `LOCAL_SOLVER_SPEC_REGISTRY`), the module
  folder/file, the docstring, the corpus, and every consumer/test. The worker
  archetype string `muncie_riverine_flood` and the baked deck filenames are the
  image contract and are UNCHANGED. The old registry name is gone from the
  retrieval index; retrieval proofs surface the new name (and the legacy "Muncie"
  phrasing still routes to it).

- **sfincs_advanced_numerical_physics_knobs** (the one true drop-in [S]). A strict
  opt-in template over the already-plumbed SFINCS advanced-physics deck surface: it
  exposes theta / alpha / advection / huthresh / wind_drag / coriolis_latitude as
  first-class knobs, validates + range-checks them through the shared
  physics_registry (the same resolver the flood template uses), and forwards to the
  SFINCS flood composer. A run with no knob set is byte-identical to the baseline.
  It is a solver-stability / runtime / smoothness sensitivity surface, NOT an
  accuracy-calibration surface -- the cited SFINCS manual publishes no reference
  figure, so it emits no chart; the deliverable is the flood-depth map plus the
  labelled settings delta. `viscosity`/`nuvisc` and the `friction2d` toggle are not
  yet plumbed into the deck builder and are a named residual, not a silent drop.

- **Template hygiene gate** (`server/tests/test_template_hygiene.py`, NATE mid-wave
  directive). A mechanical lint over every registered tier=template tool: the tool
  docstring, the module docstring, and the module comment lines must be purely
  functional -- banned patterns are `north.?star`, `formerly`, `renamed from`,
  `folded (in) from`, scenario-era `model_*_scenario` naming, and any non-ASCII
  character. Scope is one list edit (`_LOCI`) wider. Sixteen existing template
  modules were cleaned to pass (non-ASCII dashes/arrows/math-symbols -> ASCII;
  scenario-era function names in prose -> "the composer"; a "folded from"
  parenthetical dropped); no violation was suppressed.

### Stopped (honest residuals -- exceed single-session [S] with live acceptance)

- **sfincs_structures_weirs_thindams** and **sfincs_observation_points_lines**:
  `setup_structures` (weirfile/thdfile) and `setup_observation_points/_lines`
  (his-file output) are not wired in the SFINCS deck builder or worker at all. Each
  needs new deck-authoring ingestion + (obs) his-file postprocess parsing. Real M,
  not a knob.
- **sfincs_advanced_infiltration_methods**: only the Curve-Number path
  (`infiltration=True`) is plumbed today. The candidate's method selector
  (constant qinf / Green-Ampt / Horton) needs new per-method raster ingestion in
  the deck builder.
- **hecras_1d_steady_bridge**: the HEC-RAS worker entrypoint drives only the baked
  Muncie unsteady deck; `RasSteady` is compiled in but has no archetype and no baked
  Applications-Guide bridge project. Needs a new worker archetype + a baked steady
  bridge deck + a 2.2 GB image rebuild -- not live-verifiable in one session.
- **telemac2d_weir_hydraulics** and **telemac2d_bridge_hydraulics**: the TELEMAC
  worker ships only the river-dye Gmsh channel pipeline. Weir/bridge singularities
  need new .cas/.cli/geometry deck recipes staged into the worker.

## Consequences

- Registry 180 -> 181 (authoritative full-registration count). Coded tools +1.
  Templates 22 -> 23. No deletions.
- The hygiene gate is now part of the offline suite and prevents docstring/comment
  archaeology from re-accreting on any future template.
- The six stopped rows are re-scoped from [S] to M/L in the candidate table's terms
  and are the orchestrator's to re-sequence with the worker-side work each names.
