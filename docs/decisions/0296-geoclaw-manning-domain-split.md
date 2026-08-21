# ADR 0296 -- GeoClaw Manning siblings: split-by-domain NLCD wiring

Status: LANDED (offline; live acceptance pending NATE's box). Date: 2026-08-19/20.
Builds on ADR 0285 P4 (the shared `roughness_resolve` NLCD-derived-or-refuse seam +
`geoclaw_storm_surge`'s `manning_n` wiring -- the precedent this ADR replicates for
`geoclaw_inundation`'s land-dominated legs) and the demo-physics-defaults-audit
row 16 (which queued `inundation`/`amr_regions`/`gauge_timeseries` as GeoClaw
siblings still sharing the literature 0.025).

## Ruling (NATE, 2026-08-19): split by domain character

`geoclaw_storm_surge` already derives its bottom-friction Manning's n from NLCD
unconditionally (P4). The remaining GeoClaw siblings default a bare `0.025` and
never resolve it from real land cover. NATE ruled the fix is NOT "derive
everywhere" -- some GeoClaw scenarios are genuinely offshore/open-water, where
NLCD (a CONUS land-cover product) cannot serve and 0.025 IS the published
standard (Chow 1959, "smooth open surface" -- the SAME value
`manning_mapping.csv` assigns NLCD class 11 "Open Water"). The ruling: wire
`roughness_resolve` into the LAND-DOMINATED legs; keep the labeled-literature
0.025 for offshore/open-water legs.

## Inventory -- every Manning-bearing GeoClaw site

| Site | Domain character | Verdict |
|---|---|---|
| `geoclaw_storm_surge` (`storm_surge.py`) | mixed coastal (surge run-up crosses land + water) | **Already wired (ADR 0285 P4)** -- unconditional `resolve_overland_manning` over the whole AOI. NLCD's own class 11 "Open Water" (n=0.025, same Chow citation) covers the water fraction, so a single area-weighted derivation over a mixed coastal AOI is correct with no offshore special-case. **This is the precedent for how "mixed" is handled** (see Mixed-domain fork below). |
| `geoclaw_inundation`, scenario=`dam_break` (`inundation.py`) | land-dominated (inland dam-failure overland flood) | **WIRED (this ADR)** -- `resolve_overland_manning`. |
| `geoclaw_inundation`, scenario=`surge` (`inundation.py`) | mixed coastal (same physical scenario `geoclaw_storm_surge` wraps) | **WIRED (this ADR)** -- `resolve_overland_manning`, matching the storm_surge precedent for the same scenario. |
| `geoclaw_inundation`, scenario=`tsunami` (`inundation.py`) | offshore (`GEOCLAW_OFFSHORE_SCENARIOS`; deep-ocean Okada-source propagation domain) | **KEPT 0.025 (this ADR)**, now loudly labeled (`basis="default_demo"`, `consequence="numerical"` -- not `"physics"`, so it never triggers the auto-mode refuse: this is an established universal constant, not a site-specific invention) instead of riding silently. |
| `geoclaw_regional_manning_friction` (`regional_manning.py`) | n/a -- see below | **N/A, no site to wire.** `manning_coefficients` is a REQUIRED param (the template's entire question is user-chosen banded friction, e.g. offshore n vs onshore n split by elevation). `GeoClawRunArgs.manning_n` still defaults 0.025 and rides through unused, but the worker's `setrun_builder.py` writes `geo_data.manning_coefficient` as EITHER the banded list OR the scalar `manning_n`, never both (`setrun_builder.py:1808-1823`; proven by `workers/geoclaw/test_setrun_builder.py:799-803`, "No manning_coefficients -> the single scalar coefficient path"). Since `manning_coefficients` is always set for this template, `manning_n` never reaches the deck -- it is dead code, not a live invented-physics leg. Nothing to derive or refuse. |
| `geoclaw_amr_refinement_regions` (`amr_regions.py`), scenario-selectable (default `tsunami`, also `dam_break`/`surge`) | mixed (same 3-way scenario shape as `geoclaw_inundation` pre-0296) | **PARKED FORK** (not named in this kickoff's scope). Structurally identical to `geoclaw_inundation`'s pre-0296 problem (`manning_n: float = 0.025` unconditional, `amr_regions.py:103`). Recommend an identical follow-up wiring in a future job. |
| `geoclaw_tsunami_gauge_timeseries` (`gauge_timeseries.py`), scenario hardcoded `"tsunami"` | offshore only | **PARKED FORK** (not named in this kickoff's scope). Always offshore, so the correct end-state is "keep 0.025" -- but it currently rides SILENT (no `SyntheticInput` at all, `gauge_timeseries.py:100`), unlike `geoclaw_inundation`'s tsunami leg after this ADR. Recommend a label-only follow-up (mirror the `inundation.py` tsunami branch's provenance entry; no NLCD derivation needed since it never varies by scenario). |

## Mixed-domain fork: how storm_surge already answered it

Step 3 of the kickoff asked whether a coastal AOI that is part land / part water
needs special handling. Reading the precedent: `geoclaw_storm_surge` does NOT
special-case water cells -- it calls `resolve_overland_manning` over the WHOLE
AOI bbox unconditionally, and the area-weighted reduction naturally blends in
NLCD's own "Open Water" class (n=0.025, the same literature value) for whatever
fraction of the AOI is water. `geoclaw_inundation`'s `scenario="surge"` leg
(the same physical scenario storm_surge wraps) follows the identical rule in
this ADR. No fork was hit -- the precedent already solved it, so this ADR
replicates it rather than inventing a new mixed-domain rule.

## What landed (agent-side only, no worker/image change)

`trid3nt_server/workflows/geoclaw/inundation/inundation.py`, composer-side only
(`model_geoclaw_inundation` / `run_geoclaw.py` / the worker's `setrun_builder.py`
are unchanged -- they already accept a resolved `manning_n` float regardless of
its provenance):

- `geoclaw_inundation(...)` signature: `manning_n: float = 0.025` ->
  `manning_n: float | None = None`.
- After the scenario is finalized (post the `earthquake_source`/`scenario_fault`
  branches, which can force `scenario="tsunami"`), a domain-character branch:
  - `scenario in GEOCLAW_OFFSHORE_SCENARIOS` (tsunami): a user-supplied value
    rides as `basis="user"`; unset -> the literal `0.025`, `SyntheticInput(
    basis="default_demo", consequence="numerical", ...)` naming the Chow (1959)
    citation and the NLCD-coverage gap. `resolve_overland_manning` /
    `fetch_landcover` are NEVER called on this leg (offline-proven: a stub that
    raises `AssertionError` if called never fires).
  - else (dam_break / surge): `resolve_overland_manning(coerced, manning_n,
    param_name="manning_n")` -- user -> NLCD-derived -> REFUSE, identical to the
    storm_surge precedent.
- The Manning entry folds into the SAME `provenance` list / SAME single
  `gate_input_review` call `geoclaw_inundation` already runs for its other
  physics inputs (dam depth, source magnitude, ...), rather than a second
  dedicated gate round -- one review presents everything, one approval runs the
  solve. `manning_n` is added to the gate's `params` dict (mirroring the
  existing `dam_break_depth_m`/`source_magnitude` re-read pattern) so a
  `user_gated` "provide values" revision of `manning_n` is honored post-gate
  (storm_surge's standalone-gate precedent does not need this, since it never
  merges Manning into a richer multi-param review).
- Post-gate backstop: `if effective_manning_n is None: return
  GEOCLAW_PHYSICS_INPUT_REQUIRED` -- mirrors storm_surge's `if not
  _manning_res.resolved: return error` (a `user_gated` "proceed" cannot make an
  unresolved friction coefficient runnable; auto mode already refuses earlier
  via the gate's own `consequence="physics"` check).
- Docstring for `manning_n` updated to describe the split.

`regional_manning.py` / `amr_regions.py` / `gauge_timeseries.py`: UNCHANGED (see
inventory verdicts above).

## Tests

New file `tests/test_geoclaw_manning_domain_split.py` (7 tests, offline, mirrors
the `test_urban_flood_publish_offloop.py` / `test_geoclaw_finite_fault.py`
composer-stub idiom: `resolve_overland_manning` + `gate_input_review` +
`model_geoclaw_inundation` stubbed at the `inundation` module namespace so no
real MRLC/NLCD network call happens offline):

- `test_dam_break_derives_manning_from_nlcd` / `test_surge_derives_manning_from_nlcd`
  -- the stubbed derived value (0.062 / 0.045) flows to `GeoClawRunArgs.manning_n`.
- `test_dam_break_user_supplied_manning_bypasses_nlcd` -- a caller value rides the
  user rung.
- `test_dam_break_unresolved_manning_refuses` -- an unresolved NLCD derivation ->
  `GEOCLAW_PHYSICS_INPUT_REQUIRED`, `model_geoclaw_inundation` never called.
- `test_tsunami_keeps_literature_0025_and_skips_nlcd` -- tsunami never calls
  `resolve_overland_manning` (an `AssertionError`-raising stub proves it), and
  `manning_n=0.025` flows through.
- `test_tsunami_user_manning_overrides_the_literature_default` -- a user value on
  the offshore leg also skips NLCD.
- `test_derived_land_value_differs_from_offshore_literature_default` -- the A/B:
  a stubbed derived land value (0.086) differs from the offshore literal (0.025)
  in the SAME test run, proving the split is real, not two paths converging on
  the same number.

Existing suite: `tests/test_geoclaw_finite_fault.py` (the only other offline
test exercising the `geoclaw_inundation` front door directly, always
`scenario="tsunami"` via `earthquake_source`) verified unaffected -- 128 tests
across the touched families (`test_geoclaw_finite_fault`,
`test_run_geoclaw_chain`, `test_geoclaw_amr_regions_gate`,
`test_input_review_gate`, `test_door_dissolution`, `test_catalog_surfacing`,
`test_solver_confirm_gate`, `test_gate_collapse_specs`) pass.

## Consequences

- `geoclaw_inundation`'s dam_break/surge legs never silently run on an invented
  0.025 friction over real land cover again; an under-specified auto-mode run
  over an AOI outside NLCD coverage now REFUSES naming the need (law 9).
- The tsunami leg's 0.025 goes from silent to loudly labeled, without changing
  its refuse-vs-proceed behavior (a deliberate `consequence="numerical"` choice,
  not `"physics"` -- this value is not an invented site-specific claim).
- `geoclaw_regional_manning_friction` needed no code change -- the audit's
  original row 16 grouping was imprecise about it; documented here with trace
  evidence so the record is accurate.
- `amr_regions` / `gauge_timeseries` remain the pre-0296 state, explicitly
  parked (not silently forgotten) for a follow-up job.

## Completion (2026-08-21): the two parked siblings

The two forks this ADR explicitly parked (`amr_regions` PARKED FORK,
`gauge_timeseries` PARKED FORK, inventory rows above) are landed -- the
full-coverage law: a ruled paradigm does not ship with partial coverage.

- `geoclaw_amr_refinement_regions` (`amr_regions.py`): structurally identical
  to `geoclaw_inundation` pre-0296 (`manning_n: float = 0.025` unconditional,
  scenario-selectable tsunami/dam_break/surge). Applied the IDENTICAL pattern,
  copied verbatim (no new design): `manning_n: float = 0.025` ->
  `manning_n: float | None = None`; a domain-character branch on
  `GEOCLAW_OFFSHORE_SCENARIOS` -- offshore (tsunami) keeps the labeled 0.025
  (`basis="default_demo", consequence="numerical"`, Chow 1959); land-dominated
  (dam_break/surge) calls `resolve_overland_manning`. The one structural
  difference from `inundation.py`: this composer already runs ONE
  `gate_input_review` call for the AMR window provenance, so the Manning entry
  folds into that SAME call (`params={"manning_n": ...}`, combined
  `entries=_window_entries + _manning_provenance`) rather than adding a second
  gate round -- the same "fold into the existing review" idiom `inundation.py`
  itself uses for its other physics inputs. Post-gate backstop identical to
  `inundation.py`: an unresolved Manning's n surviving to here ->
  `GEOCLAW_PHYSICS_INPUT_REQUIRED`, never a silent invented run.
- `geoclaw_tsunami_gauge_timeseries` (`gauge_timeseries.py`): ALWAYS offshore
  (hardcoded `scenario="tsunami"`), so no derivation branch is needed or
  possible -- a label-only pass, exactly as the original inventory
  recommended. `manning_n: float = 0.025` -> `manning_n: float | None = None`;
  when unset the literal 0.025 now carries the SAME `SyntheticInput`
  (`basis="default_demo", consequence="numerical"`, Chow 1959 open-water
  standard) `inundation.py`'s tsunami branch carries, threaded into
  `model_geoclaw_inundation(..., synthetic_inputs=[_manning_entry])` so it
  stamps onto the returned layer. This template had NO `SyntheticInput` at all
  before (worse than `inundation.py`'s pre-0296 state, which at least ran a
  solver that silently used the same constant) -- it now rides loudly instead
  of completely unlabeled. No `gate_input_review` call exists in this composer
  (it has never had a review gate) and none is added -- a label-only pass adds
  no new review surface, per the kickoff's scope.

New test file `tests/test_geoclaw_manning_domain_split_siblings.py` (10 tests,
offline, mirrors `test_geoclaw_manning_domain_split.py`'s stub idiom) covers
both composers' derive/user/refuse/offshore-skip legs plus the derived-vs-
literature A/B for `amr_regions`, and the labeled-default/user-override split
for `gauge_timeseries`. Gate evidence: the full `tests/test_[f-o]*.py` slice
(which contains this new file, both touched composer files, and the pre-
existing `amr_regions`/`gauge_timeseries` test files) re-run green alongside
`test_geoclaw_manning_domain_split.py` -- composer-side only change; no
`workers/` path touched.
