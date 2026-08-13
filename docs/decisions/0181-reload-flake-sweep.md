# ADR 0181 - order-dependent SFINCSSetupError reload flake: root fix + victim hardening

Date: 2026-08-07
Status: accepted

## Context

`docs/IDEAS.md`'s "Order-dependent SFINCSSetupError reload flake" entry
(the 0162 finding): `test_sfincs_autoscale.py` called
`importlib.reload(sfincs_builder)` inside two tests
(`test_pathological_huge_aoi_clamps_to_coarsest_rung`,
`test_resolution_ladder_env_override`) to pick up
`monkeypatch.setenv`-driven overrides of module-level constants that are
computed once at import time (`SFINCS_MIN_CELL_CAP`, `SFINCS_SOLVE_BUDGET_S`,
`SFINCS_RES_LADDER`, ...).

`importlib.reload` re-executes the module body in place, which rebinds
EVERY name the module defines to a new object -- including the
`SFINCSSetupError` class (`class SFINCSSetupError(RuntimeError): ...`,
defined in `sfincs_builder.py` itself). Any function that still lives in
`sfincs_builder`'s own namespace (e.g. `build_sfincs_model`,
`validate_nlcd_vintage_against_mapping`, `_extract_unique_nlcd_classes`)
resolves the bare name `SFINCSSetupError` via that module's `__dict__` at
call time, so post-reload it raises the NEW class. A test module that
imported `SFINCSSetupError` at its own top of file (e.g. `from
trid3nt_server.agent.workflows.sfincs.sfincs_builder import
SFINCSSetupError`) captured that binding once, at collection time, before
any test body ran -- so it holds the ORIGINAL (pre-reload) class object.
If the autoscale test's reload executes first (same pytest session, e.g.
`pytest test_sfincs_autoscale.py test_model_flood_scenario.py`, or any
future plugin/order that runs it early), `pytest.raises(SFINCSSetupError)`
in the victim file no longer matches what the (still-live, un-reloaded)
production code actually raises -- an `isinstance` check on two distinct
class objects with the same name and definition. Same failure mode for a
bare `except SFINCSSetupError:` anywhere holding a pre-reload reference.

`test_sfincs_spiderweb.py` already carried the fix pattern for its one
`pytest.raises(SFINCSSetupError)` site: import the class INSIDE the test
function, right before use, so the name resolves against whatever is
CURRENTLY in `sys.modules` (i.e. current, whether or not a reload
happened). `test_sfincs_builder_surge_forcing.py` independently carries the
same hardening at its one site, with a docstring already diagnosing this
exact mechanism.

## Decision

**Root fix in `test_sfincs_autoscale.py`: removed both `importlib.reload`
calls outright** (rather than scoping them with a restore fixture) --
neither reload was actually necessary:

- `test_pathological_huge_aoi_clamps_to_coarsest_rung` no longer sets
  `TRID3NT_SFINCS_MIN_CELL_CAP` / `TRID3NT_SFINCS_SOLVE_BUDGET_S` env vars
  and reloads to pick them up; it now does
  `monkeypatch.setattr(sb, "SFINCS_MIN_CELL_CAP", 1)` and
  `monkeypatch.setattr(sb, "SFINCS_SOLVE_BUDGET_S", 0.0001)` directly on the
  already-imported module object. `compute_cell_cap` / `autoscale_grid_
  resolution` read these as bare module globals at call time, so patching
  the attribute has the identical effect to a reload picking up the env
  var, with automatic teardown (no `try/finally` + `delenv` needed) and no
  class rebinding.
- `test_resolution_ladder_env_override` no longer reloads the module to
  observe `SFINCS_RES_LADDER` get recomputed; it now calls the module's own
  parser, `sb._env_resolution_ladder(default)`, directly under
  `monkeypatch.setenv` -- the exact function the module-level constant
  assignment invokes at import time. This tests the parse behavior (the
  actual contract) without touching module identity at all.

This eliminates every `importlib.reload` of a production module anywhere in
`server/tests` (confirmed by sweep, below) -- the disease's root cause is
gone, not just papered over at each call site.

**Victim hardening (kept regardless, as defense-in-depth against any future
reload anywhere):** applied the re-fetch-at-call-time pattern to every
`pytest.raises(SFINCSSetupError)` site that exercises the REAL
`sfincs_builder` code path (not a mocked side-effect):

- `test_model_flood_scenario.py`:
  `test_nlcd_validation_gate_raises_on_unmapped_class` (calls
  `validate_nlcd_vintage_against_mapping` for real),
  `test_build_sfincs_model_malformed_yaml_surfaces_typed_error` (calls
  `build_sfincs_model` for real) -- both now re-import `SFINCSSetupError`
  from `sfincs_builder` inside the test body immediately before the
  `pytest.raises` block.
  `test_nlcd_gate_s3_read_boto3_failure_raises_landcover_read_failed` and
  `test_nlcd_gate_gs_read_unchanged_does_not_call_boto3` already held a
  local `sfincs_builder` module reference (for `patch.object`); switched
  their `pytest.raises(SFINCSSetupError)` to
  `pytest.raises(sfincs_builder.SFINCSSetupError)` (attribute access is
  always current, no new import line needed).
- `test_model_flood_scenario_v2.py`:
  `test_observed_forcing_zero_magnitude_rejected` (calls
  `build_sfincs_model` for real, twice) -- same local re-import fix.

**Left alone (not part of the disease):** the several `raise
SFINCSSetupError(...)` sites in `test_model_flood_scenario.py` that
construct a fake exception as a mock `side_effect` for a patched
`build_sfincs_model` (lines ~443, 509, 562, 605). These construct the
exception from the TEST FILE's own top-level import and get caught by
`flood.py`'s own top-level `except SFINCSSetupError:` -- both sides are
captured once at their respective modules' first (and only) import, before
any test runs, and neither `flood.py` nor the test file is ever reloaded.
They're identical objects regardless of `sfincs_builder`'s reload state, so
there was nothing to fix there.

## Sweep

`grep -rn "importlib" server/tests/*.py` across the whole directory found:

- The two reloads in `test_sfincs_autoscale.py` -- removed (root fix,
  above).
- Every other `importlib` usage in `server/tests` is `importlib.util.
  spec_from_file_location` + `module_from_spec` (loading a standalone
  script under a synthetic module name, e.g. `test_elmfire_sensitivity.py`,
  `test_router_glm.py`, `test_living_atlas.py`, `test_schism_coupled_
  waves.py`, `test_postprocess_telemac_wse.py`, `test_router_goes_
  archive.py`, `test_router_goes_animation.py`, `test_router_viirs_day_
  fire.py`) or `importlib.import_module` (`test_landlab_diagnostic_
  templates.py`, `test_living_atlas.py`, `test_router_promotion.py`).
  Neither pattern reloads an already-imported module in place: `spec_from_
  file_location` uses a one-off synthetic name that never collides with the
  real dotted module path, and `import_module` on an already-imported
  dotted path returns the cached module unchanged (no re-execution, no new
  class objects). None carry the stale-class-identity risk. No fix needed;
  reported for completeness.
- `grep -rl "SFINCSSetupError" server/tests/*.py` confirms only 5 files
  reference the class at all: the two victims (fixed above),
  `test_sfincs_autoscale.py` (root-fixed), and `test_sfincs_spiderweb.py` /
  `test_sfincs_builder_surge_forcing.py`, both of which already used the
  re-fetch-at-call-time pattern at their one `pytest.raises` site each and
  needed no change. `test_sfincs_builder_surge_forcing.py` carries an
  unused top-of-file `SFINCSSetupError` import (line 49, shadowed locally
  at its one use site) -- pre-existing, harmless (never reached by a
  `pytest.raises`/`except`), out of this job's three-file scope, flagged
  here rather than touched.

## Consequences

- `server/tests` now has zero `importlib.reload` calls against any
  production module -- the class-identity-rebinding hazard this ADR
  targets cannot recur from these files without a NEW reload being added.
- The two victim files' fixed `pytest.raises` sites are now robust to any
  future reload introduced elsewhere in the suite (defense-in-depth,
  matching the pattern `test_sfincs_spiderweb.py` and `test_sfincs_
  builder_surge_forcing.py` already established).
- Order-proof: `pytest test_sfincs_autoscale.py test_model_flood_
  scenario.py test_model_flood_scenario_v2.py` (the previously-flaky
  sequence) run 3x green (82 passed each run), plus each file alone, plus
  the reversed order (victims before autoscale) and the two already-hardened
  sibling files together -- all green.
- Not fixed by this ADR (out of scope, flagged only): `sfincs_builder.py`'s
  functions and `flood.py`'s `except SFINCSSetupError:` still resolve the
  class via ordinary Python name lookup, so a hypothetical FUTURE
  `importlib.reload(sfincs_builder)` from PRODUCTION code (not a test) would
  reproduce an analogous mismatch between `flood.py`'s captured class and
  whatever `sfincs_builder` raises post-reload. No such reload exists in
  production code today (confirmed: `grep -rn "importlib.reload"
  server/src` returns nothing) -- noted for awareness only.
